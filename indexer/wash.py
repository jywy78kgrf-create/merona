"""Wash / self-dealing filter — the "clean numbers" layer for the report.

Artemis demonstrated (verified, see docs/report/strategy_research.md) that
roughly half of observed x402 transactions are artificial, via two patterns:
  1. SELF-DEALING  — same wallet as payer and seller. Already captured per
     seller in every snapshot (self_pay_tx).
  2. WASH LOOPS    — the seller funds a buyer wallet, which immediately pays
     the money back as a "settlement".

This module computes, nightly and best-effort:
  a. RECIPROCAL settlement pairs — (chain, A, B) where A pays B AND B pays A
     inside our own settlements table (a settlement-visible wash loop).
  b. FUNDED-PAYER lookback — for the TOP-N sellers by settlement count (who
     dominate the counts), fetch on-chain USDC Transfer logs SENT BY the
     seller over the coverage window and intersect recipients with that
     seller's payer set. funded_payer_ratio ~ how much of their "demand" the
     seller manufactured. SAMPLED coverage (top N sellers, EVM chains only) —
     the cap is logged, never hidden.
  c. CLEAN METRICS — per chain: settlements/volume excluding flagged sellers
     (self_pay_ratio > SELF_THRESH, or funded_payer_ratio > FUNDED_THRESH, or
     reciprocal-pair participant). These are the publishable clean numbers.

Classification is PER SELLER (Artemis-style), thresholds documented below and
stored with every row. Observe-only; writes only wash_signals/clean_metrics.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evm_chains import CHAINS  # noqa: E402

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# Coverage/threshold knobs (env-overridable, all recorded in output rows).
TOP_SELLERS = int(os.environ.get("WASH_TOP_SELLERS", "150"))
SELF_THRESH = float(os.environ.get("WASH_SELF_THRESH", "0.20"))
FUNDED_THRESH = float(os.environ.get("WASH_FUNDED_THRESH", "0.20"))
RPC_TIMEOUT = float(os.environ.get("WASH_RPC_TIMEOUT", "20"))
WASH_CHAINS = ("base", "polygon")   # EVM lookback; Solana funding graph = v2
_REGISTRY = Path(__file__).resolve().parent.parent / "data" / "indexer" / "relayer_registry.json"


def _scoped_relayers(chain: str) -> list[str]:
    """The registry relayer set — the ONLY correct scope for clean metrics.
    Clean was computed over `facilitator IS NOT NULL`, which is far broader:
    it includes enriched-but-unregistered submitters, i.e. the payout-rail
    mirage (on Base a single such address moved $47M). That inflated the scoped
    base ~140x and made 'clean volume' meaningless as an x402 figure. Scoped =
    facilitator in registry, matching store.scoped_stats and the dashboard."""
    try:
        r = json.loads(_REGISTRY.read_text())
        return [a.lower() for a in r.get("relayers_by_chain", {}).get(chain, [])]
    except Exception:
        return []


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _batch_flags(chain: str) -> tuple[set, str | None]:
    """Curated sellers to EXCLUDE FROM CLEAN METRICS, read from
    data/indexer/wash_flags_<chain>.csv — but only rows whose header-named
    `clean_exclude` column is "1". The marker keeps two very different flag
    populations apart inside one file format:
      - curated evidence rows (e.g. the 2026-07-31 Polygon payment ring)
        carry clean_exclude=1 and subtract from the published clean series;
      - bulk HISTORICAL batch flags (analytics/wash_history.py wrote 7,842
        Base sellers on the box) have no such column and stay what they
        always were — a grade penalty in scores.py, never a silent
        restatement of the current clean number.
    scores.py reads the same files first-column-only, so both populations
    cap grades. Returns (sellers, sha12); the sha lands in the clean-metrics
    coverage note so the exclusion is re-derivable from the published file."""
    p = _REGISTRY.parent / f"wash_flags_{chain}.csv"
    if not p.exists():
        return set(), None
    body = p.read_bytes()
    lines = body.decode().splitlines()
    if not lines:
        return set(), None
    header = [h.strip().lower() for h in lines[0].split(",")]
    try:
        ci = header.index("clean_exclude")
    except ValueError:
        return set(), None   # no marker column -> grades-only file
    sellers = set()
    for ln in lines[1:]:
        if not ln.strip():
            continue
        cols = ln.split(",")
        if len(cols) > ci and cols[ci].strip() == "1":
            sellers.add(cols[0].strip())
    return sellers, hashlib.sha256(body).hexdigest()[:12]


def _pad_addr(a: str) -> str:
    return "0x" + a.lower().replace("0x", "").rjust(64, "0")


def _unpad(topic: str) -> str:
    return "0x" + topic[-40:]


def fetch_sent_recipients(session, rpc: str, usdc: str, seller: str,
                          lo: int, hi: int, depth: int = 0) -> set | None:
    """Recipients of USDC Transfers SENT BY `seller` in [lo,hi]. Splits the
    range on RPC errors (result-too-large); None = gave up (recorded, not
    silently treated as empty)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
            "params": [{"address": usdc, "fromBlock": hex(lo), "toBlock": hex(hi),
                        "topics": [TRANSFER_TOPIC, _pad_addr(seller)]}]}
    try:
        r = session.post(rpc, json=body, timeout=RPC_TIMEOUT).json()
    except Exception:
        r = {"error": {"message": "transport"}}
    if "result" in r:
        return {_unpad(lg["topics"][2]) for lg in r["result"]
                if len(lg.get("topics", [])) >= 3}
    if depth >= 6 or hi - lo < 2000:
        return None
    mid = (lo + hi) // 2
    left = fetch_sent_recipients(session, rpc, usdc, seller, lo, mid, depth + 1)
    right = fetch_sent_recipients(session, rpc, usdc, seller, mid + 1, hi, depth + 1)
    if left is None or right is None:
        return None
    return left | right


def _witnessed_floor(store, chain: str) -> int:
    """WITNESSED-regime lower bound for nightly analysis. After a historical
    backfill, the nightly wash/clean pipeline analyzes only what the indexer
    observes live (>= witnessed_start_<chain>); the reconstructed history gets
    its own one-time batch pass for the report. Without this, the self-join /
    per-seller scans against 148M+ historical rows starved the DB and blew the
    service timeout (observed 2026-07-11)."""
    try:
        return int(store.get_meta(f"witnessed_start_{chain}") or 0)
    except Exception:
        return 0


def _reciprocal_sellers(store) -> dict:
    """{(chain, wallet)} participating in A<->B reciprocal settlement pairs
    within the witnessed regime. Self-join is index-assisted via
    (chain, seller); runs nightly with the statement timeout lifted."""
    floors = {ch: _witnessed_floor(store, ch) for ch in WASH_CHAINS}
    case = " ".join(f"WHEN '{ch}' THEN {fl}" for ch, fl in floors.items() if fl)
    bound = ""
    if case:
        bound = (f" AND s1.block_number >= CASE s1.chain {case} ELSE 0 END"
                 f" AND s2.block_number >= CASE s2.chain {case} ELSE 0 END")
    rows = store.db.execute(
        "SELECT DISTINCT s1.chain, s1.seller, s1.payer FROM settlements s1 "
        "JOIN settlements s2 ON s2.chain=s1.chain AND s2.seller=s1.payer "
        "AND s2.payer=s1.seller WHERE s1.seller <> s1.payer" + bound).fetchall()
    out: dict = {}
    for ch, seller, payer in rows:
        out.setdefault(ch, set()).update((seller, payer))
    return out


def run_wash(store, date: str) -> None:
    """Compute wash signals for the top sellers + clean per-chain metrics."""
    session = requests.Session()
    session.headers["User-Agent"] = "x402-index-wash/1.0 (+read-only analysis)"

    # Lift the statement timeout HERE, not in the caller: the reciprocal
    # self-join outgrew the default as the witnessed set grew, and a timeout
    # used to abort the whole pass (silently, pre-rollback-fix). No-op on
    # SQLite, which has no SET.
    try:
        store.db.execute("SET statement_timeout='600000'")   # 10 min
    except Exception:
        pass

    recip_ok = True
    try:
        reciprocal = _reciprocal_sellers(store)
        n_recip = sum(len(v) for v in reciprocal.values())
        print(f"[wash] reciprocal-pair participants: {n_recip} wallets")
    except Exception as e:
        reciprocal = {}
        recip_ok = False   # recorded per signal row: reciprocal=False here
        # means "scan unavailable", NOT "scanned clean" (2026-08-01 lesson)
        print(f"[wash] reciprocal scan failed (continuing): {e}", file=sys.stderr)
        # Postgres: a failed statement aborts the transaction; without a
        # rollback every later query dies with InFailedSqlTransaction
        # (observed 2026-08-01 when the scan hit the statement timeout)
        try:
            store.db.rollback()
        except Exception:
            pass

    flagged: dict = {ch: set() for ch in WASH_CHAINS}   # chain -> flagged sellers
    # batch/manual flags (e.g. the 2026-07-31 Polygon payment ring): loaded
    # up front so clean metrics exclude them even when the sellers fall
    # outside the nightly top-N funding lookback
    batch_sha: dict = {}
    for ch in WASH_CHAINS:
        bf, sha = _batch_flags(ch)
        if bf:
            flagged[ch] |= bf
            batch_sha[ch] = sha
            print(f"[wash] {ch}: {len(bf)} batch-flagged sellers "
                  f"(wash_flags_{ch}.csv sha {sha})")

    # WASH V3 — cycle detection (SCC + net-flow balance). Depth-unbounded:
    # this is the check the 2026-07-31 Polygon ring defeated at depth ≤ 2.
    # Flagged wallets subtract from clean metrics exactly like batch flags.
    # Kill switch X402_WASH_CYCLES=0; isolated so a failure never blocks
    # the rest of the pass.
    #
    # Runs on Solana too — the detector is chain-agnostic (opaque wallet
    # strings, so base58 addresses just work) and Solana's own clean path is
    # still depth-2 (self-pay + reciprocal + funded-loop, the exact set the
    # ring defeated). Solana cycle flags are recorded here UNGATED so scores.py
    # caps a Solana ring hub even when the Solana clean number is switched off
    # (scores' cycle-flag join is chain-agnostic); the Solana clean SUBTRACTION
    # still happens only in the gated compute_clean_solana, which reads this
    # recorded set. Solana flags go in their own bucket, not `flagged` (which
    # is keyed to the EVM WASH_CHAINS clean loop below).
    cycle_note: dict = {}
    solana_cycle_flagged: set = set()
    if os.environ.get("X402_WASH_CYCLES", "1") == "1":
        import wash_cycles
        for ch in (*WASH_CHAINS, "solana"):
            try:
                # Solana relayers are base58 (case-sensitive) — the EVM helper
                # lowercases, which would match zero settlements. Use the
                # case-preserving Solana helper for that chain.
                if ch == "solana":
                    import wash_solana
                    rel = wash_solana.solana_relayers()
                else:
                    rel = _scoped_relayers(ch)
                if not rel:
                    continue
                res = wash_cycles.detect(store, ch, rel, _witnessed_floor(store, ch))
                wash_cycles.record(store, date, res, _utcnow())
                if ch in flagged:
                    flagged[ch] |= res["flagged"]
                else:
                    solana_cycle_flagged |= res["flagged"]
                warn_only = len(res["balanced"]) - len(res["flagged"])
                cycle_note[ch] = (f"; cycle v3 (k={res['params']['k']}, "
                                  f"bal<{res['params']['balance_thresh']}): "
                                  f"{len(res['flagged'])} flagged")
                print(f"[wash] {ch} cycles: {res['n_sccs']} SCCs, "
                      f"{len(res['cycle_wallets'])} on-cycle wallets, "
                      f"{len(res['flagged'])} flagged (SCC+balance), "
                      f"{warn_only} balance-warn-only"
                      + (f"; NOTES: {'; '.join(res['notes'])}"
                         if res["notes"] else ""))
            except Exception as e:
                print(f"[wash] {ch} cycle detection failed (isolated): "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                try:
                    store.db.rollback()
                except Exception:
                    pass
    for ch in WASH_CHAINS:
        cfg = CHAINS.get(ch)
        if not cfg:
            continue
        rel = _scoped_relayers(ch)
        if not rel:
            print(f"[wash] {ch}: no registry relayers — flagging skipped",
                  file=sys.stderr)
            continue
        rph = ",".join(["?"] * len(rel))
        rpc = os.environ.get(cfg.rpc_env) or cfg.default_rpc
        wb = _witnessed_floor(store, ch)
        # SCOPED, and MERCHANTS ONLY: examine wash within the x402 economy, not
        # within the rails, and exclude sellers that are themselves registered
        # relayer addresses. A facilitator's own address is not a merchant; when
        # it receives, that's fee/self-relay, not agent-to-merchant commerce
        # (mrdn's address self-washed $142k and was 98.6% of the Base wash band).
        scope = (f"chain=? AND facilitator IN ({rph}) AND block_number >= ? "
                 f"AND seller NOT IN ({rph})")
        sargs = (ch, *rel, wb, *rel)
        rng = store.db.execute(store.q(
            f"SELECT MIN(block_number), MAX(block_number) FROM settlements "
            f"WHERE {scope}"), sargs).fetchone()
        if not rng or rng[0] is None:
            continue
        lo, hi = int(rng[0]), int(rng[1])
        tops = store.db.execute(store.q(
            f"SELECT seller, COUNT(*) c, SUM(CASE WHEN payer=seller THEN 1 ELSE 0 END) "
            f"FROM settlements WHERE {scope} GROUP BY seller "
            f"ORDER BY c DESC LIMIT ?"), (*sargs, TOP_SELLERS)).fetchall()
        covered = sum(t[1] for t in tops)
        total = store.db.execute(store.q(
            f"SELECT COUNT(*) FROM settlements WHERE {scope}"),
            sargs).fetchone()[0]
        print(f"[wash] {ch}: lookback on top {len(tops)} scoped sellers "
              f"({covered:,}/{total:,} scoped settlements = "
              f"{covered/max(1,total):.0%} coverage)")
        for seller, cnt, self_pay in tops:
            payers = {p for (p,) in store.db.execute(store.q(
                "SELECT DISTINCT payer FROM settlements WHERE chain=? AND seller=? "
                "AND block_number >= ?"), (ch, seller, wb)).fetchall()}
            recips = fetch_sent_recipients(session, rpc, cfg.usdc, seller, lo, hi)
            funded = (payers & recips) if recips is not None else None
            funded_ratio = (len(funded) / len(payers)) if (funded is not None and payers) else None
            self_ratio = (self_pay or 0) / cnt if cnt else 0
            is_recip = seller in reciprocal.get(ch, set())
            is_flagged = (self_ratio > SELF_THRESH
                          or (funded_ratio is not None and funded_ratio > FUNDED_THRESH)
                          or is_recip
                          or seller in flagged[ch])   # batch/cycle flags
            if is_flagged:
                flagged[ch].add(seller)
            store.record_wash_signal(date, ch, seller, {
                "tx_count": cnt, "unique_payers": len(payers),
                "self_pay_ratio": round(self_ratio, 4),
                "funded_payers": len(funded) if funded is not None else None,
                "funded_payer_ratio": round(funded_ratio, 4) if funded_ratio is not None else None,
                "lookback_ok": recips is not None,
                "reciprocal": is_recip, "flagged": is_flagged,
                "thresholds": {"self": SELF_THRESH, "funded": FUNDED_THRESH,
                               "top_n": TOP_SELLERS,
                               "recip_scan_ok": recip_ok}}, _utcnow())
            time.sleep(0.15)
        store.db.commit()

    # clean metrics: exclude flagged sellers' settlements + ALL self-pay rows.
    # WITNESSED regime only — the nightly clean series measures what we watch
    # live; historical clean numbers come from the one-time report batch pass.
    for ch in WASH_CHAINS:
        try:
            wb = _witnessed_floor(store, ch)
            rel = _scoped_relayers(ch)
            if not rel:
                print(f"[wash] {ch}: no registry relayers — clean metrics "
                      f"skipped (not scopable)", file=sys.stderr)
                continue
            rph = ",".join(["?"] * len(rel))
            # merchants only — a relayer address receiving is not a merchant sale
            scope = (f"chain=? AND facilitator IN ({rph}) AND block_number >= ? "
                     f"AND seller NOT IN ({rph})")
            sargs = (ch, *rel, wb, *rel)
            total, vol = store.db.execute(store.q(
                f"SELECT COUNT(*), COALESCE(SUM(amount),0) FROM settlements "
                f"WHERE {scope}"), sargs).fetchone()
            excl = list(flagged[ch])
            if excl:
                ph = ",".join(["?"] * len(excl))
                ex_n, ex_v = store.db.execute(store.q(
                    f"SELECT COUNT(*), COALESCE(SUM(amount),0) FROM settlements "
                    f"WHERE {scope} AND seller IN ({ph})"),
                    (*sargs, *excl)).fetchone()
            else:
                ex_n, ex_v = 0, 0
            sp_n, sp_v = store.db.execute(store.q(
                f"SELECT COUNT(*), COALESCE(SUM(amount),0) FROM settlements "
                f"WHERE {scope} AND payer=seller "
                + (f"AND seller NOT IN ({ph})" if excl else "")),
                (*sargs, *excl) if excl else sargs).fetchone()
            clean_n = int(total) - int(ex_n) - int(sp_n)
            clean_v = int(vol) - int(ex_v) - int(sp_v)
            store.record_clean_metrics(date, ch, {
                "scoped_settlements": int(total),
                "scoped_volume_usd": round(int(vol) / 1e6, 2),
                "flagged_sellers": len(excl),
                "excluded_settlements": int(ex_n) + int(sp_n),
                "clean_settlements": clean_n,
                "clean_volume_usd": round(clean_v / 1e6, 2),
                "coverage_note": f"x402-scoped (facilitator in registry, "
                                 f"{len(rel)} relayers), witnessed regime "
                                 f"(block >= {wb}); funding lookback covers top "
                                 f"{TOP_SELLERS} scoped sellers by count; "
                                 f"self-pay excluded globally"
                                 + (f"; batch flags sha {batch_sha[ch]}"
                                    if ch in batch_sha else "")
                                 + cycle_note.get(ch, "")},
                _utcnow())
            print(f"[wash] {ch} clean: {clean_n:,} settlements / "
                  f"${clean_v/1e6:,.2f} (excluded {int(ex_n)+int(sp_n):,} from "
                  f"{len(excl)} flagged sellers + self-pay)")
        except Exception as e:
            print(f"[wash] clean metrics {ch} failed: {e}", file=sys.stderr)

    # SOLANA clean (v2) — GATED. Solana has no eth_getLogs equivalent, so its
    # funded-loop signal comes from an off-rail funding graph built by a separate
    # low-priority monthly job (deploy/solana_funding.sh); we only READ that file
    # here, never RPC in the nightly. Off unless X402_SOLANA_CLEAN=1, so shipping
    # this code cannot by itself publish a Solana clean number — the flag is the
    # switch, flipped only after the dry-run verifies.
    if os.environ.get("X402_SOLANA_CLEAN") == "1":
        try:
            import wash_solana
            gpath = os.environ.get(
                "X402_SOLANA_FUNDING",
                str(Path(__file__).resolve().parent.parent
                    / "analytics" / "out_scoped" / "funding_solana.csv.gz"))
            if os.path.exists(gpath):
                graph = wash_solana.load_funding_graph(gpath)
                # cycle flags (recorded ungated above) subtract from the
                # gated Solana clean number, same as they do on base/polygon
                wash_solana.compute_clean_solana(
                    store, date, graph, write=True,
                    extra_flagged=solana_cycle_flagged,
                    cycle_note=cycle_note.get("solana", ""))
            else:
                print(f"[wash] solana clean enabled but no funding graph at "
                      f"{gpath} — skipped", file=sys.stderr)
        except Exception as e:
            print(f"[wash] solana clean failed (isolated): {e}", file=sys.stderr)

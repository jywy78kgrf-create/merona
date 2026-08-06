"""Daily snapshot runner — the moat-builder. Starts the history clock.

Each run, idempotently:
  1. Indexes new Base blocks to the (reorg-safe) tip.
  2. HALTS LOUDLY if there is any gap below the frontier — a longitudinal
     dataset with silent holes is worthless, so a hole is a hard error, not a
     warning that scrolls past.
  3. Writes an immutable, date-stamped per-seller aggregate snapshot to
     data/indexer/snapshots/<UTC-date>/ — never overwriting. These snapshots
     are the time series; the SQLite DB is the queryable substrate.

Run it from cron/systemd daily (or more often). Fully resumable; if a run is
missed, the next run's index step simply covers the wider range.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain import ChainClient, RpcError  # noqa: E402
from index_base import CHAIN, DB_PATH, run  # noqa: E402
import index_base  # noqa: E402
import index_permit2  # noqa: E402
import coverage_canary  # noqa: E402
from index_base import finalized_end  # noqa: E402
from evm_chains import (ARBITRUM, AVALANCHE, BASE, OPTIMISM,  # noqa: E402
                        POLYGON, start_meta_key)
from index_permit2 import permit2_label  # noqa: E402
from storage import Store, open_store  # noqa: E402

# Structured mechanisms live in this dataset (for the snapshot fingerprint).
MECHANISMS = ("eip3009_usdc", "spl_facilitator", "permit2_erc20")

# Additive EVM chains indexed after Base+Solana each run, best-effort. Base is
# the primary series; these never block its snapshot. Arbitrum uses the
# receipt-based fetch (use_receipts=True) so its ~345k blocks/day + high USDC
# volume stays cheap. (Sei is configured in evm_chains.py but NOT indexed yet —
# no x402 volume on its native USDC so far.)
ADDITIVE_EVM_CHAINS = (POLYGON, AVALANCHE, OPTIMISM, ARBITRUM)

# EVM chains on which we also index the Permit2 (non-USDC ERC-20) path via the
# canonical x402ExactPermit2Proxy. Best-effort, tiny volume today. Tracked under
# a separate '<chain>_permit2' coverage label so it never collides with the
# chain's EIP-3009 (USDC) index.
PERMIT2_CHAINS = (BASE, POLYGON, AVALANCHE, OPTIMISM, ARBITRUM)

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "data" / "indexer" / "snapshots"
REGISTRY = ROOT / "data" / "indexer" / "relayer_registry.json"
PUBLIC_BASE_RPC = "https://mainnet.base.org"


def utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def last_snapshot_chain_heads(snap_dir: Path = SNAP_DIR) -> dict:
    """Per-chain max indexed block from the most recent COMMITTED snapshot.

    The dated snapshots are the durable git record, so they anchor recovery: if
    the working DB is ever wiped (a fresh box, an evicted actions/cache), we can
    resume from the last snapshot head instead of leaving a silent gap between
    the old data and a fresh head-bootstrap. Returns {chain: max_block}.
    """
    metas = sorted(snap_dir.glob("*/snapshot_meta.json")) if snap_dir.exists() else []
    if not metas:
        return {}
    try:
        m = json.loads(metas[-1].read_text())
    except Exception:
        return {}
    return {k: v["max_block"] for k, v in m.items()
            if isinstance(v, dict) and isinstance(v.get("max_block"), int)}


def bootstrap_start_block(store, chain, end: int, snapshot_heads: dict) -> int:
    """Start block for `chain`'s EIP-3009 index.

    If already bootstrapped, the stored start_block. On a FRESH bootstrap:
    RECOVER by anchoring to the last committed snapshot's head for this chain
    (durable, no silent gap) when one exists; otherwise seed ~24h back from tip.
    """
    sk = start_meta_key(chain)
    stored = store.get_meta(sk)
    if stored is not None:
        return int(stored)
    anchor = snapshot_heads.get(chain.name)
    if anchor:
        boot = min(int(anchor) + 1, end)
        print(f"[snapshot:{chain.name}] RECOVERY: fresh DB but committed "
              f"snapshots exist — resuming from block {boot} (last snapshot "
              f"head {anchor}); no gap.", file=sys.stderr)
    else:
        boot = end - chain.bootstrap_lookback_blocks
        print(f"[snapshot:{chain.name}] first run: seeding "
              f"~{chain.bootstrap_lookback_blocks} blocks from {boot}")
    store.set_meta(sk, str(boot))
    store.set_meta(f"usdc_{chain.name}", chain.usdc)
    store.set_meta(f"created_at_{chain.name}",
                   datetime.now(timezone.utc).isoformat())
    return boot


def build_coverage_start(store: Store) -> dict:
    """Per-chain coverage-start stamp: the block/slot + date each chain ENTERED
    the dataset. Distinguishes 'chain not yet covered' (0 in an early snapshot)
    from 'no activity'. Solana is explicit (P0.1); EVM chains carry created_at +
    their bootstrap start_block."""
    cs: dict = {}
    sslot = store.get_meta("solana_coverage_start_slot")
    if sslot is not None:
        cs["solana"] = {
            "start_slot": int(sslot),
            "start_date": store.get_meta("solana_coverage_start_date"),
            "note": "forward-bootstrap at deploy; Solana slots before start_slot "
                    "are NOT indexed (a 0 in earlier snapshots = not-yet-covered)",
        }
    for ch in (BASE, POLYGON, AVALANCHE, OPTIMISM, ARBITRUM):
        sb = store.get_meta(start_meta_key(ch))
        ca = store.get_meta(f"created_at_{ch.name}")
        if sb is not None or ca is not None:
            cs[ch.name] = {"start_block": int(sb) if sb is not None else None,
                           "created_at": ca}
    return cs


def build_coverage_fingerprint(store: Store, finalized_heights: dict,
                               canary: dict) -> dict:
    """Structured coverage fingerprint per snapshot: which chains/mechanisms/
    registry version were live, the finalized read heights, and the day's
    attribution ratios. Lets a future reader tell an ECOSYSTEM change from a
    COVERAGE change."""
    # Defense in depth: this function is the last DB reader before the snapshot
    # meta is written, so it's the crash point if ANY upstream step left the
    # transaction aborted. Clear any poison before our own reads so a stray
    # failure elsewhere can never take the whole snapshot down here.
    try:
        store.db.rollback()
    except Exception:
        pass
    try:
        n_relayers = len(json.load(open(REGISTRY)).get("solana_relayers", [])) \
            if REGISTRY.exists() else None
    except Exception:
        n_relayers = None
    ratios = {c: v.get("eip3009", {}).get("ratio")
              for c, v in (canary or {}).items()}
    return {
        "fingerprint_version": 1,
        "chains": ["base", "polygon", "avalanche", "optimism", "arbitrum", "solana"],
        "mechanisms": list(MECHANISMS),
        "read_height_policy": "finalized",  # snapshots source from finalized, not tip
        "relayer_registry_commit": store.get_meta("relayer_registry_commit"),
        "solana_relayer_count": n_relayers,
        "finalized_heights": finalized_heights or {},
        "eip3009_coverage_ratio": ratios,
    }


def _witnessed_filter(store: Store) -> str:
    """SQL fragment restricting per-chain rows to the WITNESSED regime (at or
    above meta witnessed_start_<chain>, set when a historical backfill loads).
    Chains without a boundary are unrestricted. Empty string when no chain has
    a backfill. Values are ints from our own meta — safe to inline."""
    parts = []
    for ch in ("base", "polygon", "avalanche", "optimism", "arbitrum", "solana"):
        v = store.get_meta(f"witnessed_start_{ch}")
        if v is not None:
            parts.append(f"WHEN '{ch}' THEN {int(v)}")
    if not parts:
        return ""
    return (" AND block_number >= CASE chain " + " ".join(parts) +
            " ELSE 0 END")


def write_seller_snapshot(store: Store, snapshot_date: str, head: int,
                          finalized_heights: dict | None = None,
                          canary: dict | None = None) -> Path:
    out_dir = SNAP_DIR / snapshot_date
    out_dir.mkdir(parents=True, exist_ok=True)
    sellers_csv = out_dir / "seller_aggregates.csv"
    if sellers_csv.exists():
        raise FileExistsError(
            f"snapshot for {snapshot_date} already exists — refusing to "
            f"overwrite immutable history: {sellers_csv}")
    # group per (chain, seller) so a wallet active on both chains stays split.
    # The Permit2 (non-USDC) labels are EXCLUDED here: they span many tokens with
    # different decimals, so a single `volume` base-unit sum would be meaningless.
    # They're reported per-token in snapshot_meta (see permit2_summary).
    #
    # WITNESSED REGIME ONLY: chains with a historical backfill carry a
    # meta witnessed_start_<chain> boundary (set by backfill_load.py). The
    # snapshot aggregates only rows AT/ABOVE it — snapshots attest what this
    # indexer observed live, and stay git-committable in size; the
    # reconstructed history lives in the DB (externally verifiable, never
    # re-snapshotted). The public anchors only ever attest this regime.
    wf = _witnessed_filter(store)
    rows = store.db.execute(
        r"SELECT chain, seller, COUNT(*) tx_count, COUNT(DISTINCT payer) unique_payers, "
        rf"{store.volume_agg()} volume, MIN(block_timestamp) first_ts, "
        r"MAX(block_timestamp) last_ts, "
        r"SUM(CASE WHEN payer=seller THEN 1 ELSE 0 END) self_pay_tx "
        r"FROM settlements WHERE chain NOT LIKE '%\_permit2' ESCAPE '\' "
        + wf +
        # Deterministic tiebreak (chain, seller) so equal-volume rows always
        # sort identically -> the anchored seller_aggregates.csv is byte-for-byte
        # reproducible by anyone re-deriving it (audit LB-9 / snapshot integrity).
        r" GROUP BY chain, seller ORDER BY volume DESC, chain, seller").fetchall()
    # Normalize the volume column (SQLite total()->REAL, Postgres SUM->Decimal) to
    # a clean integer so snapshots stay byte-identical across backends.
    rows = [(*r[:4], store._norm_vol(r[4]), *r[5:]) for r in rows]
    with open(sellers_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["chain", "seller", "tx_count", "unique_payers",
                    "volume_base_units", "first_ts", "last_ts", "self_pay_tx"])
        w.writerows(rows)

    # PAYER side of the same ledger. Sellers are scored on how many distinct
    # payers they attract; payers are scored on how many distinct sellers they
    # pay — and `unique_sellers` is the column that makes a farm wallet
    # separable from a power user, since both have a high tx_count. Written
    # into the snapshot (not derived later) so payer scores are re-derivable
    # from the anchored file, exactly like seller scores.
    payers_csv = out_dir / "payer_aggregates.csv"
    prows = store.db.execute(
        r"SELECT chain, payer, COUNT(*) tx_count, COUNT(DISTINCT seller) "
        rf"unique_sellers, {store.volume_agg()} volume, "
        r"MIN(block_timestamp) first_ts, MAX(block_timestamp) last_ts, "
        r"SUM(CASE WHEN payer=seller THEN 1 ELSE 0 END) self_pay_tx "
        r"FROM settlements WHERE chain NOT LIKE '%\_permit2' ESCAPE '\' "
        + wf +
        r" GROUP BY chain, payer ORDER BY volume DESC, chain, payer").fetchall()
    prows = [(*r[:4], store._norm_vol(r[4]), *r[5:]) for r in prows]
    with open(payers_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["chain", "payer", "tx_count", "unique_sellers",
                    "volume_base_units", "first_ts", "last_ts", "self_pay_tx"])
        w.writerows(prows)
    meta = {
        "snapshot_date": snapshot_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "head_block_base": head,
        "definition": ("settlement = USDC gasless/authorized transfer — EVM "
                       "chains (Base, Polygon, Avalanche, Optimism, Arbitrum) via "
                       "EIP-3009, Solana via SPL transfer from a facilitator "
                       "relayer. Superset of facilitator-scoped x402."),
        "base": store.stats("base"),
        "polygon": store.stats("polygon"),
        "avalanche": store.stats("avalanche"),
        "optimism": store.stats("optimism"),
        "arbitrum": store.stats("arbitrum"),
        "solana": store.stats("solana"),
        # non-USDC ERC-20 x402 via Permit2 — per-chain, per-token (no cross-token
        # volume sum; tokens have different decimals).
        "permit2_non_usdc": permit2_summary(store),
        # x402-SCOPED (relayer in facilitator registry) vs the EIP-3009 superset.
        # The scoped block is the true x402 metric; superset conflates non-x402
        # gasless USDC. Complete as relayer_enriched_fraction -> 1.0.
        "x402_scoped_vs_superset": build_x402_scoped(store),
        # P0.1 — per-chain coverage-start (block/slot + date each chain entered).
        "coverage_start": build_coverage_start(store),
        # P0.4 — structured coverage fingerprint (chains/mechanisms/registry/
        # finalized heights/ratios) so ecosystem change != coverage change.
        "coverage_fingerprint": build_coverage_fingerprint(
            store, finalized_heights, canary),
    }
    (out_dir / "snapshot_meta.json").write_text(json.dumps(meta, indent=2))
    return sellers_csv


def index_evm_best_effort(store: Store, chain, snapshot_heads: dict,
                          finalized_heights: dict | None = None) -> None:
    """Index one additive EVM chain to tip, best-effort.

    Never raises to the caller: a bad RPC or exhausted key on an additive chain
    must not block the primary Base snapshot. Keyed->public RPC fallback; the
    chain's own per-chain gap ledger keeps coverage honest (gaps WARN, not
    abort, since these rows are additive to the Base series). Fresh-DB bootstrap
    recovers from the last committed snapshot head (no silent gap).
    """
    try:
        keyed = os.environ.get(chain.rpc_env)
        client = ChainClient(keyed or chain.default_rpc)
        try:
            head = client.block_number()
        except RpcError:
            if not keyed:
                raise
            client = ChainClient(chain.default_rpc)
            head = client.block_number()
        end, src = finalized_end(client, chain, head)
        if finalized_heights is not None:
            finalized_heights[chain.name] = {"finalized_block": end, "source": src,
                                             "head": head, "lag_blocks": head - end}
        start = bootstrap_start_block(store, chain, end, snapshot_heads)
        frontier = store.covered_frontier(chain.name, start)
        try:
            index_base.run(client, store, max(start, frontier + 1), end, chain)
        except RpcError as e:
            if not keyed:
                raise
            print(f"[snapshot:{chain.name}] keyed RPC failing ({e}); falling "
                  f"back to public", file=sys.stderr)
            client = ChainClient(chain.default_rpc)
            frontier = store.covered_frontier(chain.name, start)
            index_base.run(client, store, max(start, frontier + 1), end, chain)
        cmax = store.stats(chain.name)["max_block"]
        if cmax:
            gaps = store.find_gaps(chain.name, start, cmax)
            if gaps:
                print(f"[snapshot:{chain.name}] WARNING {len(gaps)} gap(s) below "
                      f"{cmax}; first {gaps[0]} (non-fatal; retried next run)",
                      file=sys.stderr)
    except Exception as e:
        print(f"[snapshot] {chain.name} index step error (Base snapshot "
              f"unaffected): {e}", file=sys.stderr)


def index_permit2_best_effort(store: Store, chain) -> None:
    """Index one chain's Permit2 (non-USDC) settlements to tip, best-effort.
    Separate '<chain>_permit2' coverage label + start block; never blocks Base."""
    try:
        label = permit2_label(chain)
        sk = f"start_block_{label}"
        keyed = os.environ.get(chain.rpc_env)
        client = ChainClient(keyed or chain.default_rpc)
        try:
            head = client.block_number()
        except RpcError:
            if not keyed:
                raise
            client = ChainClient(chain.default_rpc)
            head = client.block_number()
        end, _src = finalized_end(client, chain, head)
        if store.get_meta(sk) is None:
            boot = end - chain.bootstrap_lookback_blocks
            store.set_meta(sk, str(boot))
            print(f"[permit2:{chain.name}] first run: seeding "
                  f"~{chain.bootstrap_lookback_blocks} blocks from {boot}")
        start = int(store.get_meta(sk))
        frontier = store.covered_frontier(label, start)
        try:
            index_permit2.run_permit2(client, store, max(start, frontier + 1), end, chain)
        except RpcError as e:
            if not keyed:
                raise
            print(f"[permit2:{chain.name}] keyed RPC failing ({e}); falling "
                  f"back to public", file=sys.stderr)
            client = ChainClient(chain.default_rpc)
            frontier = store.covered_frontier(label, start)
            index_permit2.run_permit2(client, store, max(start, frontier + 1), end, chain)
    except Exception as e:
        print(f"[snapshot] {chain.name} permit2 step error (Base snapshot "
              f"unaffected): {e}", file=sys.stderr)


def permit2_summary(store: Store) -> dict:
    """Per-chain, per-token Permit2 counts for snapshot_meta. Deliberately NOT a
    single 'volume' — these are many different tokens (different decimals), so a
    summed base-unit number would be meaningless. Count + distinct sellers only."""
    rows = store.db.execute(
        r"SELECT chain, token, COUNT(*), COUNT(DISTINCT seller) FROM settlements "
        r"WHERE chain LIKE '%\_permit2' ESCAPE '\' GROUP BY chain, token "
        r"ORDER BY 3 DESC").fetchall()
    out: dict = {}
    for chain, token, n, sellers in rows:
        base_chain = chain[:-len("_permit2")]
        entry = out.setdefault(base_chain, {"settlements": 0, "tokens": {}})
        entry["settlements"] += n
        entry["tokens"][token] = {"settlements": n, "sellers": sellers}
    return out


def _fmt_since(store: Store, key: str):
    """A chain's coverage-start date as an ISO string (or None). Kept as ISO so
    the front-end can format it; cheap meta read, no table scan."""
    return store.get_meta(key)


# Meta key under which the nightly run stashes the precomputed dashboard metrics.
DASHBOARD_METRICS_KEY = "dashboard_metrics"


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    return vals[len(vals) // 2] if vals else None


def build_instr_metrics(store: Store) -> dict:
    """Dashboard-sized digest of the passive instrumentation tables (liveness,
    domains, facilitators, hourly pulse, canary) — each section isolated so one
    empty/missing table never blanks the rest. All queries hit tiny per-day
    tables, so this adds ~nothing to the precompute cost."""
    out: dict = {}

    # Bazaar directory size vs what we actually probed (stored by instrumentation)
    try:
        raw = store.get_meta("bazaar_last")
        if raw:
            out["bazaar"] = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        pass

    # Network watch (mainnets the Bazaar lists that we DON'T index) is
    # deliberately NOT in this payload: stats.json is public, and the watch is
    # an operator signal, not an audience one. It lives in the journal alerts,
    # the network_watch/-_ledger meta keys, and the committed WATCHLIST.md
    # (private repo). See instrumentation.py.

    # Endpoint liveness: latest day's probe results
    try:
        d = store.db.execute(
            "SELECT MAX(measured_date) FROM endpoint_liveness").fetchone()[0]
        if d:
            rows = store.db.execute(store.q(
                "SELECT http_status, latency_ms, valid_402, error "
                "FROM endpoint_liveness WHERE measured_date=?"), (d,)).fetchall()
            alive = [r for r in rows if r[0] is not None and not r[3]]
            out["liveness"] = {
                "date": d, "probed": len(rows), "alive": len(alive),
                "valid_402": sum(1 for r in rows if r[2]),
                "median_latency_ms": _median([r[1] for r in alive]),
            }
    except Exception:
        pass

    # Domain intel: latest day — how established are the seller domains?
    try:
        d = store.db.execute(
            "SELECT MAX(measured_date) FROM domain_intel").fetchone()[0]
        if d:
            rows = store.db.execute(store.q(
                "SELECT whois_created, cert_age_days, changed_since_last, error "
                "FROM domain_intel WHERE measured_date=?"), (d,)).fetchall()
            now = datetime.now(timezone.utc)
            ages = []
            for w, _, _, err in rows:
                if err or not w:
                    continue
                try:
                    created = datetime.fromisoformat(str(w).replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    ages.append((now - created).days)
                except Exception:
                    pass
            out["domains"] = {
                "date": d, "tracked": len(rows),
                "median_age_days": _median(ages),
                "young_30d": sum(1 for a in ages if a < 30),
                "dns_changed": sum(1 for r in rows if r[2]),
            }
    except Exception:
        pass

    # Facilitator health: latest day — distinct facilitators + top 3 per chain
    try:
        d = store.db.execute(
            "SELECT MAX(measured_date) FROM facilitator_health").fetchone()[0]
        if d:
            fac: dict = {}
            # NB: the placeholder-parameterized pattern (not a literal '%…' in
            # the SQL) matters — a literal % breaks psycopg's placeholder parser.
            skip = r"\_%"
            for ch in ("base", "polygon", "solana"):
                rows = store.db.execute(store.q(
                    "SELECT facilitator, settlement_count, volume_share "
                    "FROM facilitator_health WHERE measured_date=? AND chain=? "
                    "AND facilitator NOT LIKE ? ESCAPE '\\' "
                    "ORDER BY settlement_count DESC LIMIT 3"), (d, ch, skip)).fetchall()
                n = store.db.execute(store.q(
                    "SELECT COUNT(DISTINCT facilitator) FROM facilitator_health "
                    "WHERE measured_date=? AND chain=? "
                    "AND facilitator NOT LIKE ? ESCAPE '\\'"), (d, ch, skip)).fetchone()[0]
                fac[ch] = {"active": n, "top": [
                    {"addr": r[0], "count": r[1],
                     "share": round(r[2], 4) if r[2] is not None else None}
                    for r in rows]}
            out["facilitators"] = {"date": d, **fac}
    except Exception:
        pass

    # Hourly pulse: ROLLING last-24h window per chain. The collector writes the
    # last 2 UTC days of (date,hour) rows, and the nightly runs at ~02:17 — a
    # calendar-day window would show 2 real bars and 22 blanks every morning.
    # Instead, take the trailing 24 populated hours across the date boundary.
    try:
        rows = store.db.execute(
            "SELECT measured_date, hour, chain, settlement_count "
            "FROM hourly_settlements ORDER BY measured_date DESC LIMIT 400").fetchall()
        if rows:
            dates = sorted({r[0] for r in rows})[-2:]     # latest 2 days present
            grid: dict = {}                               # (date,hour) -> {chain: n}
            for d0, hour, ch, cnt in rows:
                if d0 in dates and 0 <= hour < 24:
                    grid.setdefault((d0, hour), {})[ch] = cnt
            keys = sorted(grid.keys())
            # end the window at the last populated hour; take the 24 slots up to it
            end_d, end_h = keys[-1]
            slots = []
            for i in range(23, -1, -1):
                h = end_h - i
                d0 = end_d
                if h < 0:
                    h += 24
                    d0 = dates[0] if len(dates) > 1 and end_d == dates[1] else None
                slots.append((d0, h))
            pulse = {ch: [grid.get(k, {}).get(ch, 0) if k[0] else 0 for k in slots]
                     for ch in ("base", "polygon", "solana")}
            out["pulse"] = {"date": f"24h to {end_d} {end_h:02d}:00 UTC", **pulse}
    except Exception:
        pass

    # Price book: distribution of asked prices across the latest census
    try:
        d = store.db.execute(
            "SELECT MAX(measured_date) FROM bazaar_census").fetchone()[0]
        if d:
            amts = sorted(a for (a,) in store.db.execute(store.q(
                "SELECT amount_usd FROM bazaar_census WHERE measured_date=? "
                "AND amount_usd IS NOT NULL"), (d,)).fetchall())
            listed = store.db.execute(store.q(
                "SELECT COUNT(*) FROM bazaar_census WHERE measured_date=?"),
                (d,)).fetchone()[0]
            if amts:
                pct = lambda p: amts[min(len(amts) - 1, int(len(amts) * p))]
                out["prices"] = {"date": d, "listed": listed, "priced": len(amts),
                                 "median_usd": round(pct(0.5), 6),
                                 "p10_usd": round(pct(0.10), 6),
                                 "p90_usd": round(pct(0.90), 6)}
    except Exception:
        pass

    # Unit economics: latest per-chain fee-vs-payment medians
    try:
        d = store.db.execute(
            "SELECT MAX(measured_date) FROM unit_economics").fetchone()[0]
        if d:
            ue = {}
            for ch, fee, pay, ratio in store.db.execute(store.q(
                    "SELECT chain, median_fee_usd, median_payment_usd, "
                    "fee_over_payment FROM unit_economics WHERE measured_date=? "
                    "AND median_fee_usd IS NOT NULL"), (d,)).fetchall():
                ue[ch] = {"fee_usd": round(fee, 6), "pay_usd": round(pay, 4) if pay else None,
                          "ratio": round(ratio, 5) if ratio else None}
            if ue:
                out["unit_econ"] = {"date": d, **ue}
    except Exception:
        pass

    # Scores digest: grade histograms for the latest scoring day
    try:
        d = store.db.execute(
            "SELECT MAX(measured_date) FROM endpoint_scores").fetchone()[0]
        if d:
            hist = dict(store.db.execute(store.q(
                "SELECT grade, COUNT(*) FROM endpoint_scores "
                "WHERE measured_date=? GROUP BY grade"), (d,)).fetchall())
            out["endpoint_grades"] = {"date": d, **{g: hist.get(g, 0)
                                                    for g in "ABCDF"},
                                      # provisional origins carry no letter
                                      "unrated": hist.get(None, 0)}
        d = store.db.execute(
            "SELECT MAX(measured_date) FROM seller_scores").fetchone()[0]
        if d:
            hist = dict(store.db.execute(store.q(
                "SELECT grade, COUNT(*) FROM seller_scores "
                "WHERE measured_date=? GROUP BY grade"), (d,)).fetchall())
            prov = store.db.execute(store.q(
                "SELECT COUNT(*) FROM seller_scores WHERE measured_date=? "
                "AND provisional"), (d,)).fetchone()[0]
            out["seller_grades"] = {"date": d, "provisional": bool(prov),
                                    **{g: hist.get(g, 0) for g in "ABCDF"}}
    except Exception:
        pass

    # Clean numbers: latest wash-filtered per-chain totals
    try:
        d = store.db.execute(
            "SELECT MAX(measured_date) FROM clean_metrics").fetchone()[0]
        if d:
            cm = {}
            for ch, cn, cv, ex, fl in store.db.execute(store.q(
                    "SELECT chain, clean_settlements, clean_volume_usd, "
                    "excluded_settlements, flagged_sellers FROM clean_metrics "
                    "WHERE measured_date=?"), (d,)).fetchall():
                cm[ch] = {"settlements": cn, "volume_usd": cv,
                          "excluded": ex, "flagged_sellers": fl}
            if cm:
                out["clean"] = {"date": d, **cm}
    except Exception:
        pass

    # Coverage canary: latest attributed/total ratio per chain
    try:
        rows = store.db.execute(
            "SELECT chain, ratio, measured_date FROM coverage_ratio "
            "WHERE mechanism='eip3009' AND (measured_date, chain) IN "
            "(SELECT MAX(measured_date), chain FROM coverage_ratio "
            " WHERE mechanism='eip3009' GROUP BY chain)").fetchall()
        if rows:
            out["canary"] = {r[0]: {"ratio": round(r[1], 4) if r[1] is not None else None,
                                    "date": r[2]} for r in rows}
    except Exception:
        pass
    return out


def build_dashboard_metrics(store: Store) -> dict:
    """The DB-HEAVY half of the dashboard payload: per-chain scoped/superset
    aggregates + totals + coverage-start dates + db size + row count.

    These are full-table COUNT(DISTINCT)/SUM aggregates over ~2M rows across 6
    chains — seconds-to-minutes on a busy or small Postgres tier, and they were
    timing out when the live stats server ran them on every refresh. The nightly
    run (nothing else competing, no tight timeout) computes them ONCE and stores
    the result in meta[DASHBOARD_METRICS_KEY]; the server reads that blob and
    serves it instantly. The cheap, filesystem-derived parts (snapshot freshness,
    cycle timing) are still computed live by the server so staleness stays honest
    even if a nightly run is missed.
    """
    scoped = build_x402_scoped(store)
    chains, tot_set, tot_vol = {}, 0, 0.0
    for ch, v in scoped.items():
        sup, sc = v["eip3009_superset"], v.get("x402_scoped")
        row = {"scopable": v["scopable"], "registry": v["relayer_registry_size"],
               "enriched": round(v["relayer_enriched_fraction"], 4),
               "superset_settlements": sup["settlements"],
               "superset_volume_usd": round(sup["volume_base_units"] / 1e6, 2),
               "min_block": sup.get("min_block"), "max_block": sup.get("max_block")}
        if sc:
            s = sc["settlements"]
            vol = sc["volume_base_units"] / 1e6
            tot_set += s
            tot_vol += vol
            row.update({"settlements": s, "volume_usd": round(vol, 2),
                        "sellers": sc["unique_sellers"],
                        "payers": sc.get("unique_payers"),
                        "avg_usd": round(vol / s, 4) if s else 0,
                        "share_count": round(s / sup["settlements"], 4) if sup["settlements"] else 0,
                        "share_vol": round(vol / (sup["volume_base_units"] / 1e6), 5) if sup["volume_base_units"] else 0})
        chains[ch] = row

    # Tier ladder. MERCHANT = scoped minus facilitator relayer addresses acting
    # as sellers (a facilitator's own address is not a merchant). Computed
    # DIRECTLY here — a plain query, always fresh, INDEPENDENT of the wash pass,
    # so the key tier (the facilitator-address gap) always renders. CLEAN
    # (wash-adjusted) comes from the nightly clean_metrics only when present.
    try:
        by_chain = json.load(open(REGISTRY)).get("relayers_by_chain", {})
    except Exception:
        by_chain = {}
    for ch in list(chains):
        rel = [a.lower() for a in (by_chain.get(ch) or [])]
        if not rel or "volume_usd" not in chains[ch]:
            continue
        try:
            ph = ",".join([store.PH] * len(rel))
            mn, mv = store.db.execute(store.q(
                f"SELECT COUNT(*), COALESCE({store.volume_agg()},0) FROM settlements "
                f"WHERE chain=? AND facilitator IN ({ph}) "
                f"AND seller NOT IN ({ph})"), (ch, *rel, *rel)).fetchone()
            chains[ch]["merchant_settlements"] = int(mn)
            chains[ch]["merchant_volume_usd"] = round(store._norm_vol(mv) / 1e6, 2)
        except Exception:
            try:
                store.db.rollback()
            except Exception:
                pass
    try:
        cd = store.db.execute(
            "SELECT MAX(measured_date) FROM clean_metrics").fetchone()[0]
        if cd:
            for ch, cv in store.db.execute(store.q(
                    "SELECT chain, clean_volume_usd FROM clean_metrics "
                    "WHERE measured_date=?"), (cd,)).fetchall():
                if ch in chains and cv is not None:
                    chains[ch]["clean_volume_usd"] = float(cv)
    except Exception:
        pass

    since = {"base": _fmt_since(store, "created_at_base"),
             "polygon": _fmt_since(store, "created_at_polygon"),
             "solana": _fmt_since(store, "solana_coverage_start_date")}
    try:
        if store.PH == "%s":
            db_size = store.db.execute(
                "SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
        else:
            db_size = f"{DB_PATH.stat().st_size / 1e6:.0f} MB"
    except Exception:
        db_size = None
    try:
        rows = store.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    except Exception:
        rows = tot_set
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "chains": chains, "since": since,
            "totals": {"settlements": tot_set, "volume_usd": round(tot_vol, 2), "rows": rows},
            "db_size": db_size,
            "instr": build_instr_metrics(store)}


def vacuum_settlements(store: Store) -> None:
    """Reclaim dead tuples left by the nightly relayer-enrichment UPDATEs so the
    aggregate scans (snapshot stats + dashboard precompute) don't slowly bloat
    back to the timeouts we just fixed. Postgres-only, best-effort: VACUUM can't
    run inside a transaction, so we flip autocommit for the duration and restore
    it after. Never raises — a skipped vacuum must not fail the run."""
    if store.PH != "%s":
        return
    try:
        store.db.commit()            # close any open txn — VACUUM needs none
        store.db.autocommit = True
        store.db.execute("VACUUM (ANALYZE) settlements")
        print("[vacuum] VACUUM (ANALYZE) settlements complete")
    except Exception as e:
        print(f"[vacuum] skipped (non-fatal): {e}", file=sys.stderr)
    finally:
        try:
            store.db.autocommit = False
        except Exception:
            pass


def store_dashboard_metrics(store: Store) -> dict | None:
    """Compute + persist the dashboard metrics into meta. Best-effort: on the
    small tier these aggregates can be slow, so we lift the statement timeout for
    just this step (it runs once, off-hours, with nothing competing). Never
    raises to the caller — a metrics-precompute failure must not fail the run."""
    try:
        if store.PH == "%s":
            try:
                store.db.execute("SET statement_timeout='0'")   # unbounded: runs once, off-peak
                store.db.commit()
            except Exception:
                store.db.rollback()
        metrics = build_dashboard_metrics(store)
        store.set_meta(DASHBOARD_METRICS_KEY, metrics)
        print(f"[dashboard] precomputed metrics stored "
              f"({metrics['totals']['settlements']:,} scoped settlements)")
        # Facilitator totals for the /probe name/address lookups. A live
        # COUNT+SUM over a big facilitator's rows (Coinbase on base = 8M+)
        # blows any serving-side statement timeout — the probe must only ever
        # READ these. Isolated: a failure here keeps the metrics above.
        try:
            store_facilitator_totals(store)
        except Exception as fe:
            print(f"[dashboard] facilitator totals precompute failed "
                  f"(non-fatal; probe shows relayer counts only): {fe}",
                  file=sys.stderr)
        return metrics
    except Exception as e:
        try:
            store.db.rollback()
        except Exception:
            pass
        print(f"[dashboard] metrics precompute failed (non-fatal; server falls "
              f"back to last stored/live): {e}", file=sys.stderr)
        return None


FACILITATOR_TOTALS_KEY = "probe_facilitator_totals"


def store_facilitator_totals(store: Store) -> None:
    """Precompute per-facilitator settlement totals for the probe. One bounded
    GROUP BY per chain over the REGISTRY relayer addresses only (a handful of
    IN-list values against the (chain,facilitator) index) — runs in the same
    unbounded-timeout window as the metrics precompute. Stored as:
      {"date": ..., "by_facilitator": {fid: {chain: [n, vol]}},
       "by_address": {chain: {addr: [n, vol]}}}"""
    reg = json.load(open(REGISTRY))
    by_chain = reg.get("relayers_by_chain", {})
    addr_to_fid: dict[tuple, str] = {}
    for fid, nets in (reg.get("facilitators") or {}).items():
        for net, lst in nets.items():
            if not isinstance(lst, list):
                continue
            for a in lst:
                addr = a["address"] if net == "solana" else a["address"].lower()
                addr_to_fid[(net, addr)] = fid
    by_fac: dict = {}
    by_addr: dict = {}
    for ch, addrs in by_chain.items():
        if not addrs:
            continue
        ph = ",".join(["?"] * len(addrs))
        rows = store.db.execute(store.q(
            f"SELECT facilitator, COUNT(*), COALESCE(SUM(amount),0) "
            f"FROM settlements WHERE chain=? AND facilitator IN ({ph}) "
            f"GROUP BY facilitator"), (ch, *addrs)).fetchall()
        for addr, n, vol in rows:
            by_addr.setdefault(ch, {})[addr] = [int(n), int(vol)]
            fid = addr_to_fid.get((ch, addr))
            if fid:
                cur = by_fac.setdefault(fid, {}).setdefault(ch, [0, 0])
                cur[0] += int(n)
                cur[1] += int(vol)
    store.set_meta(FACILITATOR_TOTALS_KEY, {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "by_facilitator": by_fac, "by_address": by_addr})
    print(f"[dashboard] facilitator totals stored "
          f"({len(by_fac)} facilitators, {sum(len(v) for v in by_addr.values())} "
          f"relayer addresses)")


def build_x402_scoped(store: Store) -> dict:
    """Per-chain x402-SCOPED subset (relayer in the facilitator registry) next to
    the EIP-3009 superset + enrichment coverage. The scoped numbers are the TRUE
    x402 metric; the superset (store.stats) conflates x402 with all gasless USDC
    (measured: Polygon superset is ~0% x402; Base ~95% by count / ~5% by volume).
    A chain with no registry (arbitrum/avalanche/optimism) is not scopable ->
    superset-only, labeled. Scoped numbers are complete only as enriched_fraction
    -> 1.0 (see enrich_relayers.py)."""
    try:
        by_chain = json.load(open(REGISTRY)).get("relayers_by_chain", {})
    except Exception:
        by_chain = {}
    out: dict = {}
    for ch in ("base", "polygon", "avalanche", "optimism", "arbitrum", "solana"):
        relayers = by_chain.get(ch)
        cov = store.relayer_enriched_fraction(ch)
        out[ch] = {
            "scopable": bool(relayers),
            "relayer_registry_size": len(relayers) if relayers else 0,
            "relayer_enriched_fraction": round(cov["fraction"], 4),
            "x402_scoped": store.scoped_stats(ch, relayers) if relayers else None,
            "eip3009_superset": store.stats(ch),
        }
    return out


# Chains the EIP-3009 canary measures (all live EVM chains). Solana settles via
# SPL (a different signal), so it isn't part of the AuthorizationUsed ratio.
CANARY_CHAINS = (BASE, POLYGON, AVALANCHE, OPTIMISM, ARBITRUM)


def run_coverage_canary(store: Store, date: str, finalized_heights: dict) -> dict:
    """Measure + store attributed/total per chain over a finalized window; loud
    alert when attribution leaves the band. Best-effort — never blocks the
    snapshot. Returns {chain: {mechanism: result}}."""
    results: dict = {}
    for chain in CANARY_CHAINS:
        try:
            client = ChainClient(os.environ.get(chain.rpc_env) or chain.default_rpc)
            fh = finalized_heights.get(chain.name)
            fin = (fh or {}).get("finalized_block") or client.finalized_block() \
                or (client.block_number() - chain.confirmations)
            lo, hi = coverage_canary.window_for(chain, fin)
            m = coverage_canary.measure_eip3009(client, chain, lo, hi)
            ratio = store.record_coverage_ratio(
                date, chain.name, "eip3009", (lo, hi),
                m["total"], m["attributed"], m["volume"],
                datetime.now(timezone.utc).isoformat())
            results.setdefault(chain.name, {})["eip3009"] = {**m, "ratio": ratio}
            band = (m["total"] and ratio < coverage_canary.RATIO_ALERT_BELOW)
            if band:
                print(f"[canary] ALERT {chain.name} eip3009 ratio={ratio:.4f} "
                      f"({m['attributed']}/{m['total']}) below "
                      f"{coverage_canary.RATIO_ALERT_BELOW} — possible new "
                      f"facilitator/pattern our attribution misses",
                      file=sys.stderr)
            rtxt = f"{ratio:.4f}" if m["total"] else "n/a"
            print(f"[canary] {chain.name} eip3009 {m['attributed']}/{m['total']} "
                  f"ratio={rtxt} vol=${m['volume']/1e6:,.0f}"
                  f"{'  <<< ALERT' if band else ''}")
        except Exception as e:
            print(f"[canary] {chain.name} measurement failed (non-fatal): {e}",
                  file=sys.stderr)
            # CRITICAL: a failed canary query (e.g. a chain whose RPC is down)
            # can leave the shared psycopg transaction ABORTED. Without this
            # rollback the poison propagates to the next chain and — fatally —
            # to build_coverage_fingerprint after the loop, whose uncaught
            # InFailedSqlTransaction crashed the entire run and lost the
            # 2026-07-07/08 snapshots. Clear it so "non-fatal" is actually true.
            try:
                store.db.rollback()
            except Exception:
                pass
    return results


def main() -> None:
    store = open_store(DB_PATH)
    # The nightly run is the one place these full-table aggregates (the snapshot's
    # per-chain stats + the scoped/superset build) SHOULD be allowed to take their
    # time: it runs once, off-peak, with nothing else on the DB. A short serving
    # timeout here just aborts the query mid-snapshot, poisons the transaction
    # (InFailedSqlTransaction), and cascade-crashes the whole run. Lift it.
    if store.PH == "%s":
        try:
            store.db.execute("SET statement_timeout='0'")
            store.db.commit()
        except Exception:
            store.db.rollback()
    # Keyed RPC (Alchemy/QuickNode free tier) handles the large USDC getLogs in
    # one shot instead of the public endpoint's split-and-crawl (~28min -> ~2min
    # per run, and far more reliable). Falls back to public if the secret is
    # unset, so the job still runs without it — just slowly.
    keyed = os.environ.get("X402_BASE_RPC")
    client = ChainClient(keyed or PUBLIC_BASE_RPC)
    try:
        head = client.block_number()
    except RpcError:
        if not keyed:
            raise
        keyed = None
        client = ChainClient(PUBLIC_BASE_RPC)
        head = client.block_number()
    # Reorg discipline: read only to the FINALIZED height, never head-confirms
    # (a reorg above finalized is silent corruption the ledger can't catch).
    finalized_heights: dict = {}
    end, src = finalized_end(client, BASE, head)
    finalized_heights[BASE.name] = {"finalized_block": end, "source": src,
                                    "head": head, "lag_blocks": head - end}
    print(f"[snapshot:base] head={head} end={end} (via {src}, lag {head-end} blk)")
    # Bootstrap forward from launch on a fresh DB — never anchor to full history.
    # On a WIPED DB with committed snapshots present, recover from the last
    # snapshot head instead of head-lookback (no silent gap). Shared by every
    # EIP-3009 chain; computed once from the durable git-committed snapshots.
    snapshot_heads = last_snapshot_chain_heads()
    start = bootstrap_start_block(store, BASE, end, snapshot_heads)
    if store.get_meta("relayer_registry_commit") is None and REGISTRY.exists():
        store.set_meta("relayer_registry_commit",
                       json.load(open(REGISTRY)).get("source_commit", "?"))

    frontier = store.covered_frontier(CHAIN, start)
    try:
        run(client, store, max(start, frontier + 1), end)
    except RpcError as e:
        if not keyed:
            raise
        # A misbehaving keyed endpoint (bad key, exhausted quota) must not
        # stall the daily accrual. Committed ranges are already durable, so
        # resume the remainder on the public RPC — slow, but it finishes.
        print(f"[snapshot] keyed Base RPC failing ({e}); "
              f"falling back to public RPC", file=sys.stderr)
        client = ChainClient(PUBLIC_BASE_RPC)
        frontier = store.covered_frontier(CHAIN, start)
        run(client, store, max(start, frontier + 1), end)

    # HARD gap check — refuse to snapshot over holes
    max_blk = store.stats(CHAIN)["max_block"]
    if max_blk:
        gaps = store.find_gaps(CHAIN, start, max_blk)
        if gaps:
            print(f"[snapshot] ABORT: {len(gaps)} gap(s) below max indexed block "
                  f"{max_blk}. First: {gaps[0]}. Fix with "
                  f"`index_base.py --from {gaps[0][0]} --to {gaps[0][1]}` "
                  f"then re-run.", file=sys.stderr)
            store.close()
            sys.exit(1)

    # Solana: cursor-based per-relayer index (gap-free forward by construction —
    # no block-range gap check applies). Failures preserve per-relayer cursors
    # and resume next run; a Solana RPC outage never blocks the Base snapshot.
    try:
        from solana_chain import SolanaClient
        import index_solana
        index_solana.run(SolanaClient(index_solana.DEFAULT_RPC), store)
    except Exception as e:
        print(f"[snapshot] Solana index step error (Base snapshot unaffected): {e}",
              file=sys.stderr)

    # Additive EVM chains (Polygon, Avalanche, Optimism): same EIP-3009 engine
    # as Base, best-effort like Solana — an RPC issue on any never blocks the
    # Base snapshot. Each keeps its own per-chain gap ledger.
    for _chain in ADDITIVE_EVM_CHAINS:
        index_evm_best_effort(store, _chain, snapshot_heads, finalized_heights)

    # Permit2 (non-USDC ERC-20) settlements per EVM chain, best-effort. Tiny
    # volume today; captured for longitudinal completeness under '<chain>_permit2'.
    for _chain in PERMIT2_CHAINS:
        index_permit2_best_effort(store, _chain)

    # Dark-matter canary: per-chain attributed-vs-total ratio over a finalized
    # window. Stored daily; loud alert if attribution drops out of band (a new
    # facilitator/pattern our labels miss). Best-effort — never blocks the snapshot.
    date = utc_date()
    canary = run_coverage_canary(store, date, finalized_heights)

    # The index above always runs (keeps the DB current). The daily snapshot is
    # one-per-UTC-day and immutable: a same-day re-run advances the index but
    # cleanly skips re-writing the snapshot rather than crashing or clobbering.
    fingerprint = build_coverage_fingerprint(store, finalized_heights, canary)
    if (SNAP_DIR / date / "seller_aggregates.csv").exists():
        print(f"[snapshot] {date} already captured; index advanced to "
              f"{store.stats(CHAIN)['max_block']}. Skipping duplicate snapshot.")
    else:
        path = write_seller_snapshot(store, date, head, finalized_heights, canary)
        print(f"[snapshot] {date}: wrote {path}")
        print(json.dumps(store.stats(CHAIN), indent=2))

    # Relayer enrichment (A) — fill tx.from on EVM rows so the x402-scoped view
    # is computable. Best-effort, bounded per run, ledger-safe (only sets a NULL
    # relayer; never mutates a settlement). Runs after the snapshot so it never
    # delays the moat; scoped numbers converge as coverage -> 1.0 over runs.
    try:
        import enrich_relayers
        # Only EVM chains with a facilitator registry are x402-SCOPABLE — enriching
        # the rest spends RPC for no scoping benefit (scoped_stats is None without a
        # registry). Derive from the registry so a newly-registered chain auto-joins.
        try:
            _by_chain = json.load(open(REGISTRY)).get("relayers_by_chain", {})
        except Exception:
            _by_chain = {}
        scopable = [c for c in enrich_relayers.EVM if _by_chain.get(c)]
        # The per-run cap must EXCEED a day's inflow or coverage decays over time
        # (a chain adds up to a few hundred k settlements/day). Default is sized to
        # clear a full day plus catch-up; bump DAILY_ENRICH_LIMIT if a chain surges.
        limit = int(os.environ.get("DAILY_ENRICH_LIMIT", "1500000"))
        for _ch in scopable:
            enrich_relayers.enrich_chain(store, _ch, limit)
    except Exception as e:
        print(f"[enrich] runner error (isolated; ledger unaffected): {e}",
              file=sys.stderr)

    # Passive instrumentation — STRICTLY AFTER the settlement index + snapshot.
    # Fully isolated: writes only to its own tables, never raises, cannot corrupt
    # the ledger or block the snapshot (which is already done above).
    try:
        import instrumentation
        instrumentation.run_instrumentation(
            store, date, fingerprint,
            chains=["base", "polygon", "avalanche", "optimism", "arbitrum", "solana"])
    except Exception as e:
        print(f"[instr] runner error (isolated; settlement data unaffected): {e}",
              file=sys.stderr)

    # payTo integrity feed — the free public mismatch JSON (catalog payTo vs
    # live-probe payTo + rotation history). Pure derivation over the
    # instrumentation tables; isolated, best-effort.
    try:
        import payto_watch
        payto_watch.write_feed(store)
    except Exception as e:
        print(f"[payto-watch] failed (isolated): {e}", file=sys.stderr)

    # Wash/self-dealing filter (indexer/wash.py): reciprocal pairs +
    # funded-payer lookback on the top sellers -> nightly CLEAN metrics
    # (the publishable numbers). Isolated, best-effort. MUST run before the
    # scoring layer: scores v2 joins the same-day wash_signals rows.
    try:
        import wash
        wash.run_wash(store, date)
    except Exception as e:
        print(f"[wash] runner error (isolated): {e}", file=sys.stderr)

    # Scoring layer — endpoint reliability grades + payment-grounded seller
    # trust scores, computed from the day's immutable snapshot + the
    # instrumentation tables + tonight's wash signals (see indexer/scores.py).
    # Isolated, best-effort.
    try:
        import scores
        scores.run_scores(store, date)
    except Exception as e:
        print(f"[scores] runner error (isolated): {e}", file=sys.stderr)

    # Reclaim the dead tuples the enrichment UPDATEs just created, so the table
    # doesn't bloat back into the aggregate-timeout territory we fixed. Best-effort
    # and Postgres-only; runs before the metrics precompute so it scans a clean table.
    vacuum_settlements(store)

    # Precompute the DB-heavy dashboard metrics and stash them in meta, so the
    # live stats server serves them instantly instead of re-running full-table
    # aggregates (COUNT DISTINCT / SUM over ~2M rows) on every refresh — which
    # timed out on the serving tier. Runs last, off-peak, with the statement
    # timeout lifted; best-effort so it never fails the nightly run.
    store_dashboard_metrics(store)
    store.close()


if __name__ == "__main__":
    main()

"""Nightly scoring layer: endpoint reliability grades + payment-grounded
seller trust scores. OBSERVE-ONLY — reads the day's immutable snapshot and the
instrumentation tables, writes only to its own tables, never touches the
settlement ledger. Best-effort: isolated by the caller, never blocks the run.

Why this exists (each demand signal independently verified, see
docs/report/strategy_research.md):
  - x402 endpoints have practical validated attacks and no independent
    reliability measurement -> per-origin RELIABILITY GRADES from our nightly
    liveness probes.
  - Agent reputation is Sybil-saturated and only ~1% payment-grounded; the
    literature explicitly recommends tying trust to settled x402 payments ->
    SELLER TRUST SCORES derived from actual settlements.

Auditability property (the moat): seller scores are computed deterministically
from the day's snapshot file — whose SHA-256 is publicly anchored (see
deploy/anchor.sh). Anyone holding the published snapshot can recompute and
verify any score. A trust score whose own integrity is provable.

SCORE_VERSION history:
  1 — v1 components: payer diversity, scale/longevity, self-pay penalty,
      endpoint liveness, domain age. No funding-graph wash detection yet
      (needs non-settlement transfer data); no reciprocal-pair detection.
      Scores are PROVISIONAL until MIN_MATURE_DAYS of observation.
  2 — wash signals folded in: joins the nightly wash_signals table
      (indexer/wash.py: funded-payer ratio + reciprocal pairs over the top
      sellers) and the one-time full-history batch flags
      (analytics/wash_history.py → data/indexer/wash_flags_<chain>.csv,
      flags-file sha recorded with every score for auditability).
      Graded penalties (funded up to -25, reciprocal -20, history-only -15)
      plus a hard cap: a wash-flagged seller grades D at best (score ≤ 49).
  3 — the post-ring version (2026-08-01, after a depth-3 payment ring
      defeated every v2 input and held a C while manufacturing ~100% of
      Polygon's clean figure):
      (a) CYCLE input — wash v3 (indexer/wash_cycles.py) SCC+balance flags
          join with a -40 penalty and the same D-at-best cap; detector
          params recorded per affected row so a published grade is
          reproducible against the exact detector that produced it.
      (b) SIGNALS_LIVE — every row records which wash signals were actually
          available that night. v2 could not distinguish "scanned clean"
          from "scan silently failed" (the reciprocal join timed out for an
          unknown stretch pre-2026-08-01 and scores kept flowing); rows now
          carry the evidence of their own blind spots.
      (c) DIVERSITY split into breadth (0-25, log distinct payers) +
          LOYALTY (0-10, log repeat tx/payer, payers ≥ 2). v2's single
          log-payer curve punished small sellers with genuine repeat
          customers while the ring's 30 Sybil payers earned points.
      (d) A-GATE — score ≥ 90 additionally requires census tier T2+
          (settled, service-verified: data/processed/service_directory_v0
          .json). An A must mean verified commerce, not clean-looking flow;
          gated rows are capped at 89 with the reason recorded.
  4 — the distribution-honesty version (2026-08-06). The 2026-08-06 audit
      found 131,959 sellers graded F with exactly 201 of them carrying wash
      evidence: the v3 rubric compresses clean sellers into a ~35-50 score
      band while the letter map started C at 60, so 99.5% of the index wore
      F for being small, the 201 real flags were indistinguishable from
      newcomers, and no A had ever been earned (the bar sat 40 points above
      the best clean score). Three changes, letters only — the numeric
      score and its components are untouched and remain comparable across
      versions:
      (a) LETTER GATE — clean sellers below SELLER_LETTER_MIN_TX
          settlements or SELLER_LETTER_MIN_PAYERS distinct payers publish
          grade NULL ("unrated"), same maturation-reveals-the-letter design
          as endpoint v4. Absence of history is not evidence of misconduct.
      (b) CONDUCT ANCHORS — F and D are earned by observed conduct only,
          and conduct letters IGNORE the gate (a wash-flagged wallet cannot
          hide behind being small): live wash signal (cycle or funded-ratio
          flag) or self-pay ≥ SELF_PAY_CONDUCT → F; history-flag-only
          (past evidence, no live signal) → D.
      (c) BANDS re-anchored to the observed clean distribution (p25 36 /
          p50 41 / p90 49 at calibration): B ≥ 47, C ≥ 36, D below. A
          requires score ≥ 55 AND census T2+ evidence — rare but now
          actually reachable, where v3's A required a score the rubric
          cannot produce. The ≥90 T2 cap from v3 is retained unchanged.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCORE_VERSION = 4
ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "data" / "indexer" / "snapshots"
REGISTRY = ROOT / "data" / "indexer" / "relayer_registry.json"
# full-history wash flags (batch export); env-overridable for tests/deploys.
FLAGS_DIR = Path(os.environ.get("WASH_FLAGS_DIR", str(ROOT / "data" / "indexer")))
# census service directory (per-origin evidence tiers) — the A-gate source.
CENSUS_DIRECTORY = Path(os.environ.get(
    "CENSUS_DIRECTORY",
    str(ROOT / "data" / "processed" / "service_directory_v0.json")))
A_GATE_TIERS = {"T2", "T3"}   # settled / delivery-verified


def _relayer_addresses() -> dict:
    """{chain: {lowercased relayer address}}. A facilitator's own relayer
    address is not a merchant, so it is never scored as a seller — when it
    receives, that is fee/self-relay, not agent-to-merchant commerce (mrdn's
    address self-washed and would otherwise score as a top seller). A verified
    finding about such an address is surfaced separately via the API's
    adverse-findings layer, not as a merchant grade."""
    try:
        r = json.loads(REGISTRY.read_text()).get("relayers_by_chain", {})
        return {c: {a.lower() for a in v} for c, v in r.items()}
    except Exception:
        return {}
WASH_FLAG_CAP = 49.0   # wash-flagged sellers: numeric score capped (v2 rule)
# --- seller letter gate + bands (v4) ------------------------------------------
# A letter is a public claim; publishing one demands an evidence base. Clean
# sellers below BOTH thresholds carry grade NULL ("unrated") — the score is
# still computed and recorded, and crossing the gate reveals the letter, the
# same maturation design as endpoint v4. Conduct letters ignore the gate.
SELLER_LETTER_MIN_TX = int(os.environ.get("SELLER_LETTER_MIN_TX", "25"))
SELLER_LETTER_MIN_PAYERS = int(os.environ.get("SELLER_LETTER_MIN_PAYERS", "5"))
# self-pay share that is conduct in its own right, not just a point penalty
SELF_PAY_CONDUCT = float(os.environ.get("SELLER_SELF_PAY_CONDUCT", "0.25"))
# Bands anchored to the observed clean-seller distribution at calibration
# (2026-08-06: p25 36 / p50 41 / p75 42 / p90 49 over 2,647 gated clean
# sellers). v3's C-at-60 sat above a score the rubric essentially never
# produces for clean flow, which is how 99.5% of the index came to wear F.
SELLER_A_MIN = 55.0    # + census T2+ evidence required (see _seller_letter)
SELLER_A_MIN_PAYERS = int(os.environ.get("SELLER_A_MIN_PAYERS", "50"))
# v4.2 (2026-08-07): BlockRun audit — 10.07M settlements, 99.6% from ONE
# payer, sailed past the payer floor with 1,100 nominal payers. A seller
# whose volume is a single counterparty caps at B, share recorded.
SELLER_A_MAX_TOP_SHARE = float(os.environ.get("SELLER_A_MAX_TOP_SHARE", "0.90"))
# v4.1 (2026-08-07): first A cohort audit found A's resting on 6-9 lifetime
# payers via endpoint/domain points. "A = verified commerce" must not be
# earnable with six customers; below the payer floor the same seller is a B.
SELLER_B_MIN = 47.0    # ≈ top decile of gated clean sellers
SELLER_C_MIN = 36.0    # ≈ p25 — the broad clean middle grades C
# grades stay 'provisional' until this many distinct observation days exist —
# a young index must not hand out confident A's.
MIN_MATURE_DAYS = int(os.environ.get("SCORE_MATURE_DAYS", "14"))
# endpoint letters appear at this many observed days (marked provisional) and
# become 'verified' at MIN_MATURE_DAYS — a week of nightly probes shows
# active, continuous service; two weeks earns the unqualified letter.
MIN_LETTER_DAYS = int(os.environ.get("SCORE_LETTER_DAYS", "7"))
LIVENESS_WINDOW_DAYS = int(os.environ.get("SCORE_LIVENESS_WINDOW", "14"))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _grade(score: float) -> str:
    """Endpoint letter map (uptime-style scores genuinely span 0-100)."""
    return ("A" if score >= 90 else "B" if score >= 75 else
            "C" if score >= 60 else "D" if score >= 40 else "F")


def _seller_letter(score: float, tx: int, payers: int, self_ratio: float,
                   nflag: bool, recip: bool, hflag: bool, cflag: bool,
                   t2_ok: bool, top_share: float = 0.0
                   ) -> tuple[str | None, str | None]:
    """(letter | None, note). The v4 seller letter: conduct sinks, absence
    caps, only conduct earns an F.

    - F/D are earned by OBSERVED conduct only, and conduct ignores the
      evidence gate — a wash-flagged wallet cannot hide behind being small.
      Live signals (cycle, nightly funded-ratio, reciprocal pair) or extreme
      self-pay → F; a history-only flag with no live signal → D.
    - Clean sellers below the gate publish no letter at all (None =
      "unrated"): absence of history is not evidence of misconduct, and an
      F shared by 99.5% of the population stops nobody.
    - Clean gated sellers band on the observed distribution; A additionally
      requires census T2+ evidence — verified commerce, not clean-looking
      flow.
    """
    if cflag or nflag or recip or self_ratio >= SELF_PAY_CONDUCT:
        return "F", "conduct_live"
    if hflag:
        return "D", "conduct_history_only"
    if tx < SELLER_LETTER_MIN_TX or payers < SELLER_LETTER_MIN_PAYERS:
        return None, "unrated_below_letter_gate"
    if score >= SELLER_A_MIN and t2_ok and payers >= SELLER_A_MIN_PAYERS:
        if top_share >= SELLER_A_MAX_TOP_SHARE:
            return "B", "concentration_capped"
        return "A", None
    if score >= SELLER_B_MIN:
        return "B", None
    if score >= SELLER_C_MIN:
        return "C", None
    return "D", "clean_bottom_band"


# --- 1. endpoint reliability grades ------------------------------------------
# v4 (2026-08-05): rates are per observed DAY, not per probe, and provisional
# origins carry grade NULL instead of a letter. Two causes, one incident:
# the prober now escalates through alternate paths seeking a 402, so
# probe-weighted rates would punish an origin for our own extra attempts; and
# a confident letter derived from 2 observation days is an invented fact — an
# origin with $52 of real settlements wore "C, valid_402_rate 0.0" for three
# weeks because one param-requiring path was probed twice. Publication of
# grades gates on this: no letter until the evidence carries it.
ENDPOINT_SCORE_VERSION = 4


def compute_endpoint_scores(store, date: str) -> int:
    """Per-origin reliability over the trailing liveness window:
    uptime (50%), valid-402 correctness (30%), latency band (20%).
    A day counts as up / valid-402 if ANY probe that day was. Letters are
    tiered by evidence: NULL below MIN_LETTER_DAYS distinct probe days, a
    provisional letter from MIN_LETTER_DAYS, a verified (non-provisional)
    letter from MIN_MATURE_DAYS. The numeric score is always recorded so
    maturation is a reveal, not a recompute."""
    rows = store.db.execute(store.q(
        "SELECT endpoint_url, measured_date, http_status, latency_ms, valid_402, "
        "error FROM endpoint_liveness WHERE measured_date >= ?"),
        (_window_floor(LIVENESS_WINDOW_DAYS),)).fetchall()
    from urllib.parse import urlparse
    agg: dict = {}   # origin -> {"day": {date: {alive, v402}}, "lats": []}
    for url, mdate, status, lat, v402, err in rows:
        p = urlparse(url)
        if not p.hostname:
            continue
        origin = f"{p.scheme}://{p.netloc}"
        a = agg.setdefault(origin, {"day": {}, "lats": [], "probes": 0})
        a["probes"] += 1
        d = a["day"].setdefault(mdate, {"alive": False, "v402": False})
        if status is not None and not err:
            d["alive"] = True
            if lat is not None:
                a["lats"].append(lat)
        if v402:
            d["v402"] = True
    n = 0
    for origin, a in agg.items():
        days = a["day"]
        n_days = len(days) or 1
        uptime = sum(1 for v in days.values() if v["alive"]) / n_days
        v402_rate = sum(1 for v in days.values() if v["v402"]) / n_days
        lats = sorted(a["lats"])
        med_lat = lats[len(lats) // 2] if lats else None
        lat_score = (1.0 if med_lat is not None and med_lat < 500 else
                     0.7 if med_lat is not None and med_lat < 1500 else
                     0.4 if med_lat is not None else 0.0)
        score = round(100 * (0.5 * uptime + 0.3 * v402_rate + 0.2 * lat_score), 1)
        n_obs = len(days)
        store.record_endpoint_score(date, origin, {
            "days_observed": n_obs, "probes": a["probes"],
            "uptime_rate": round(uptime, 4), "valid_402_rate": round(v402_rate, 4),
            "median_latency_ms": med_lat, "score": score,
            "grade": None if n_obs < MIN_LETTER_DAYS else _grade(score),
            "provisional": n_obs < MIN_MATURE_DAYS,
            "score_version": ENDPOINT_SCORE_VERSION}, _utcnow())
        n += 1
    store.db.commit()
    print(f"[scores] endpoint grades: {n} origins "
          f"(window {LIVENESS_WINDOW_DAYS}d, v{ENDPOINT_SCORE_VERSION})")
    return n


def _window_floor(days: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()


# --- 2. payment-grounded seller trust scores ---------------------------------
def _latest_snapshot_csv():
    if not SNAP_DIR.exists():
        return None, None
    dates = sorted(p.name for p in SNAP_DIR.iterdir()
                   if p.is_dir() and (p / "seller_aggregates.csv").exists())
    if not dates:
        return None, None
    return dates[-1], SNAP_DIR / dates[-1] / "seller_aggregates.csv"


def _load_wash_signals(store):
    """Latest nightly wash signals: {(chain, seller): (funded_ratio,
    reciprocal, flagged)}. Latest-available (not strictly today's) so a failed
    wash night degrades gracefully; the date used is recorded per score.
    Also returns recip_scan_ok — whether that night's reciprocal self-join
    actually ran (from the thresholds blob; None on pre-v3 rows). Without it,
    reciprocal=False is ambiguous between "scanned clean" and "scan died"."""
    sig: dict = {}
    sig_date = None
    recip_ok = None
    try:
        sig_date = store.db.execute(
            "SELECT MAX(measured_date) FROM wash_signals").fetchone()[0]
        if sig_date:
            for ch, s, fr, rec, flg, thr in store.db.execute(store.q(
                    "SELECT chain, seller, funded_payer_ratio, reciprocal, "
                    "flagged, thresholds FROM wash_signals WHERE measured_date=?"),
                    (sig_date,)):
                sig[(ch, s)] = (fr, bool(rec), bool(flg))
                if recip_ok is None and thr:
                    try:
                        recip_ok = json.loads(thr).get("recip_scan_ok")
                    except Exception:
                        pass
    except Exception:
        pass
    return sig, sig_date, recip_ok


def _load_census_tiers():
    """{hostname: tier} from the published census service directory — the
    evidence layer behind the A-gate. Missing/unreadable file degrades to an
    empty map, which simply means no seller can clear the gate (fail-closed:
    a young or broken census must not hand out A's)."""
    try:
        d = json.loads(CENSUS_DIRECTORY.read_text())
        return {o["origin"]: o.get("tier") for o in d.get("origins", [])
                if o.get("origin")}
    except Exception:
        return {}


def _load_history_flags():
    """Full-history batch wash flags per chain (wash_flags_<chain>.csv from
    analytics/wash_history.py). Returns ({chain: {seller}}, {chain: sha12}) —
    the file sha goes into every affected score row, so a published flags file
    makes the penalty independently re-derivable."""
    flags: dict = {}
    shas: dict = {}
    if not FLAGS_DIR.exists():
        return flags, shas
    for p in sorted(FLAGS_DIR.glob("wash_flags_*.csv")):
        ch = p.stem.replace("wash_flags_", "")
        try:
            body = p.read_bytes()
            lines = body.decode().splitlines()
            flags[ch] = {ln.split(",", 1)[0].strip() for ln in lines[1:]
                         if ln.strip()}
            shas[ch] = hashlib.sha256(body).hexdigest()[:12]
        except Exception:
            pass
    return flags, shas


def compute_seller_scores(store, date: str) -> int:
    """v3 trust score per (chain, seller), from the day's IMMUTABLE snapshot
    (auditable against its public anchor) + instrumentation + wash joins.

    Components (sum 100 before penalties):
      payer breadth    0-25  unique_payers, log-scaled (Sybil-resistant core)
      payer loyalty    0-10  repeat depth (tx/payer), log-scaled, payers >= 2
      scale/longevity  0-20  settlement count + active-window, log-scaled
      endpoint health  0-25  best reliability grade among the seller's listed
                             origins (via bazaar_census payTo); 10 if unlisted
      domain age       0-20  oldest domain among listings; 8 if unlisted
    Penalties:
      self-pay ratio   up to -40  (payer==seller settlements / total)
      cycle flag       -40        (wash_cycles: SCC membership + net-flow
                                   balance — the depth-unbounded carousel
                                   detector; params recorded per row)
      funded payers    up to -25  (wash_signals.funded_payer_ratio, 50% -> max)
      reciprocal pair  -20        (wash_signals.reciprocal)
      history flag     -15        (batch flags file; only when no live signal)
    Caps: any wash flag (nightly, cycle, or history) -> score ≤ 49 (grade D);
    score ≥ 90 (grade A) additionally requires census tier T2+ evidence for
    one of the seller's listed origins — an ungated 89 otherwise, with the
    gate decision recorded in components.
    Every row records signals_live: which wash inputs actually ran the night
    it was computed. Provisional until MIN_MATURE_DAYS of snapshots."""
    snap_date, csv_path = _latest_snapshot_csv()
    if not csv_path:
        print("[scores] no snapshot to score from", file=sys.stderr)
        return 0
    n_snap_days = len([p for p in SNAP_DIR.iterdir() if p.is_dir()])
    provisional = n_snap_days < MIN_MATURE_DAYS

    # wash joins (v3): nightly signals + cycle flags + full-history batch flags
    wash_sig, wash_date, recip_ok = _load_wash_signals(store)
    hist_flags, hist_shas = _load_history_flags()
    import wash_cycles
    cycle_flags, cycle_date, cycle_params = wash_cycles.load_flagged(store)
    census_tiers = _load_census_tiers()
    if wash_sig or hist_flags or cycle_flags:
        print(f"[scores] wash join: {len(wash_sig)} nightly signals "
              f"({wash_date}, recip_scan_ok={recip_ok}), "
              f"{len(cycle_flags)} cycle flags ({cycle_date}), history flags: "
              + (", ".join(f"{c}:{len(s)}" for c, s in hist_flags.items())
                 or "none"))
    signals_live = {"wash_signals": wash_date, "recip_scan_ok": recip_ok,
                    "cycles": cycle_date}

    # instrumentation joins: seller wallet -> listed origins / domains
    wallet_origins: dict = {}
    wallet_domains: dict = {}
    try:
        d = store.db.execute(
            "SELECT MAX(measured_date) FROM bazaar_census").fetchone()[0]
        if d:
            for res, dom, wallet in store.db.execute(store.q(
                    "SELECT resource, domain, seller_wallet FROM bazaar_census "
                    "WHERE measured_date=? AND seller_wallet IS NOT NULL"), (d,)):
                w = (wallet or "").lower()
                from urllib.parse import urlparse
                p = urlparse(res)
                if p.hostname:
                    wallet_origins.setdefault(w, set()).add(
                        f"{p.scheme}://{p.netloc}")
                if dom:
                    wallet_domains.setdefault(w, set()).add(dom)
    except Exception:
        pass
    # endpoint grades (latest scoring run today) + domain ages
    origin_score: dict = {}
    try:
        for origin, sc in store.db.execute(store.q(
                "SELECT origin, score FROM endpoint_scores WHERE measured_date=?"),
                (date,)):
            origin_score[origin] = sc
    except Exception:
        pass
    domain_age: dict = {}
    try:
        d = store.db.execute(
            "SELECT MAX(measured_date) FROM domain_intel").fetchone()[0]
        if d:
            now = datetime.now(timezone.utc)
            for dom, created in store.db.execute(store.q(
                    "SELECT domain, whois_created FROM domain_intel "
                    "WHERE measured_date=? AND whois_created IS NOT NULL"), (d,)):
                try:
                    dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    domain_age[dom] = (now - dt).days
                except Exception:
                    pass
    except Exception:
        pass

    relayers = _relayer_addresses()
    skipped_relayer = 0
    n = 0
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            chain, seller = row["chain"], row["seller"]
            # a registered relayer's own address is not a merchant seller
            if seller.lower() in relayers.get(chain, ()):
                skipped_relayer += 1
                continue
            tx = int(row["tx_count"] or 0)
            payers = int(row["unique_payers"] or 0)
            self_pay = int(row["self_pay_tx"] or 0)
            first_ts, last_ts = int(row["first_ts"] or 0), int(row["last_ts"] or 0)
            if tx <= 0:
                continue
            # payer breadth (0-25): log-scaled distinct payers; a seller with
            # thousands of distinct payers is structurally hard to Sybil.
            breadth = min(25.0, 25 * math.log10(1 + payers) / 4)     # 10k payers -> max
            # payer loyalty (0-10): repeat depth. v2's breadth-only curve
            # floored small sellers with genuine repeat customers at F while
            # a ring's Sybil payer set earned breadth points. payers >= 2 so
            # a single exclusive counterparty (payer_scores' own red flag)
            # earns nothing; circulation abuse is the cycle detector's job.
            repeat = tx / payers if payers else 0.0
            loyalty = (min(10.0, 10 * math.log10(1 + repeat) / 2)    # 100 tx/payer -> max
                       if payers >= 2 else 0.0)
            diversity = breadth + loyalty
            # scale/longevity (0-20)
            days_active = max(0, (last_ts - first_ts) / 86400)
            scale = min(12.0, 12 * math.log10(1 + tx) / 5)           # 100k tx -> max
            longevity = min(8.0, days_active / 30 * 8)               # 30d -> max
            # endpoint health (0-25)
            w = seller.lower()
            origins = wallet_origins.get(w) or set()
            if origins:
                best = max((origin_score.get(o, 0) for o in origins), default=0)
                endpoint = 25 * best / 100
                listed = True
            else:
                endpoint, listed = 10.0, False                       # unlisted: neutral
            # domain age (0-20)
            doms = wallet_domains.get(w) or set()
            ages = [domain_age[d] for d in doms if d in domain_age]
            if ages:
                a = max(ages)
                dom_pts = 20.0 if a >= 365 else 14.0 if a >= 90 else 6.0 if a >= 30 else 0.0
            else:
                dom_pts = 8.0 if not listed else 4.0
            # self-pay penalty (0 to -40)
            self_ratio = self_pay / tx
            penalty = min(40.0, 40 * self_ratio * 2)                 # 50% self-pay -> max
            # wash penalties (v3): live signals dominate; the history flag
            # only adds weight when no live signal already covers the seller.
            fr, recip, nflag = wash_sig.get((chain, seller), (None, False, False))
            hflag = seller in hist_flags.get(chain, ())
            cflag = (chain, seller) in cycle_flags
            wash_pen = 0.0
            if cflag:
                wash_pen += 40.0     # structural manufacture, worst signal
            if fr:
                wash_pen += min(25.0, 25 * float(fr) / 0.5)          # 50% funded -> max
            if recip:
                wash_pen += 20.0
            if hflag and not (recip or nflag or cflag):
                wash_pen += 15.0
            score = max(0.0, min(100.0, round(
                diversity + scale + longevity + endpoint + dom_pts
                - penalty - wash_pen, 1)))
            # hard cap: an actively wash-flagged seller cannot out-score its
            # flag on volume alone — D at best until the flag clears.
            if nflag or hflag or cflag:
                score = min(score, WASH_FLAG_CAP)
            # A-gate: a 90+ score must be backed by census T2+ evidence
            # (settled, service-verified origin). Clean-looking flow alone
            # stops at 89 — an A is a claim about verified commerce.
            hosts = {o.split("://", 1)[-1] for o in origins}
            t2_ok = any(census_tiers.get(h) in A_GATE_TIERS for h in hosts)
            a_gate = None
            if score >= 90:                    # v3 cap retained unchanged
                if t2_ok:
                    a_gate = "T2+"
                else:
                    score, a_gate = 89.0, "capped_no_T2_evidence"
            top_tx = int(row.get("top_payer_tx") or 0)
            top_share = (top_tx / tx) if tx and top_tx else 0.0
            grade, letter_note = _seller_letter(
                score, tx, payers, self_ratio, nflag, recip, hflag, cflag,
                t2_ok, top_share)
            if top_tx:
                components_top = round(top_share, 4)
            else:
                components_top = None
            if grade == "A" and a_gate is None:
                a_gate = "T2+"                 # A below 90 still rests on T2
            components = {"diversity": round(breadth, 1),
                          "loyalty": round(loyalty, 1),
                          "scale": round(scale + longevity, 1),
                          "endpoint": round(endpoint, 1),
                          "domain": round(dom_pts, 1),
                          "self_pay_penalty": round(-penalty, 1),
                          "signals_live": signals_live}
            if a_gate:
                components["a_gate"] = a_gate
            if wash_pen or nflag or hflag or cflag:
                components["wash_penalty"] = round(-wash_pen, 1)
                components["wash"] = {
                    "funded_ratio": fr, "reciprocal": recip,
                    "nightly_flag": nflag, "history_flag": hflag,
                    "cycle_flag": cflag,
                    "signals_date": wash_date,
                    "flags_sha": hist_shas.get(chain)}
                if cflag:
                    components["wash"]["cycle"] = {
                        "date": cycle_date, "detector": cycle_params}
            if letter_note:
                components["letter"] = letter_note
            if components_top is not None:
                components["top_payer_share"] = components_top
            store.record_seller_score(date, chain, seller, {
                "score": score, "grade": grade,
                "provisional": provisional, "snapshot_date": snap_date,
                "tx_count": tx, "unique_payers": payers,
                "self_pay_ratio": round(self_ratio, 4),
                "listed": listed,
                "components": components,
                "score_version": SCORE_VERSION}, _utcnow())
            n += 1
    store.db.commit()
    print(f"[scores] seller trust scores: {n} (chain,seller) rows from "
          f"snapshot {snap_date} (provisional={provisional}"
          + (f", skipped {skipped_relayer} relayer addresses" if skipped_relayer
             else "") + ")")
    return n


def run_scores(store, date: str) -> None:
    """All scorers, each isolated — a failure in one never blocks the others."""
    from payer_scores import compute_payer_scores
    for name, fn in (("endpoint_scores", compute_endpoint_scores),
                     ("seller_scores", compute_seller_scores),
                     ("payer_scores", compute_payer_scores)):
        try:
            fn(store, date)
        except Exception as e:
            print(f"[scores] {name} failed (isolated): "
                  f"{type(e).__name__}: {str(e)[:140]}", file=sys.stderr)

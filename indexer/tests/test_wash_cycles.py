"""Wash v3 acceptance suite — the ring is the test.

docs/WASH_V3_SPEC.md: "A v3 that does not flag this set is not done." This
file holds a synthetic replica of the 2026-07-31 Polygon payment ring
(hub → leg-2 origin → 30-wallet cluster → back to hub; every node
near-balanced) and asserts, against the REAL detector + REAL run_wash +
REAL scores:

  1. detect() flags exactly the 32 ring wallets — no more, no fewer;
  2. a balanced-but-acyclic pass-through WARNS but is never flagged;
  3. an imbalanced 2-cycle is on-cycle but not flagged (balance required);
  4. out-of-scope (non-registry facilitator) cycles are invisible;
  5. run_wash subtracts the ring from clean metrics automatically —
     no hand-curated CSV involved;
  6. scores v3 caps a cycle-flagged seller at D and records the detector;
  7. the A-gate: a perfect-looking seller stops at 89 without census T2+
     evidence, and clears 90 with it.

All SQLite, no network (funding lookback stubbed).
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # indexer/

from storage import Store  # noqa: E402
import wash_cycles  # noqa: E402

RELAYER = "0x" + "fa" * 20
HUB = "0x" + "66" * 20          # lnpay/aisa stand-in
LEG2 = "0x" + "89" * 20         # sole payer of the cluster
CLUSTER = [f"0x{i:02d}" + "cc" * 19 for i in range(30)]
RING = {HUB, LEG2, *CLUSTER}


def _seed(store, facilitator=RELAYER):
    """Synthetic replica, scaled: hub→leg2 $6,000; leg2→30 wallets $199
    each; each wallet→hub $197. Every ring node balances within 2%.
    Plus: honest fan-in sellers, a balanced pass-through, an imbalanced
    2-cycle (reciprocal territory, not cycle-flag territory)."""
    ins = store._settlement_insert_sql(with_facilitator=True)
    n = [0]

    def tx(payer, seller, usd, count=1):
        for _ in range(count):
            n[0] += 1
            store.db.execute(ins, (f"0xtx{n[0]:06d}", 0, "polygon", "0xusdc",
                                   payer, seller, int(usd * 1e6),
                                   100 + n[0], 1750000000 + n[0], facilitator))

    tx(HUB, LEG2, 300.0, 20)                 # $6,000 into leg2
    for w in CLUSTER:
        tx(LEG2, w, 199.0)                   # $5,970 out of leg2
        tx(w, HUB, 49.25, 4)                 # $197 back to hub each ($5,910)
    for s, tag in (("0x" + "aa" * 20, "h1"), ("0x" + "ab" * 20, "h2")):
        for i in range(10):                  # honest: 10 distinct payers
            tx(f"0xpay{tag}{i:02d}", s, 10.0)
    tx("0x" + "d1" * 20, "0x" + "d2" * 20, 500.0)   # pass-through in
    tx("0x" + "d2" * 20, "0x" + "d3" * 20, 495.0)   # pass-through out
    tx("0x" + "e1" * 20, "0x" + "e2" * 20, 1000.0)  # imbalanced 2-cycle
    tx("0x" + "e2" * 20, "0x" + "e1" * 20, 200.0)
    store.db.commit()


def test_detect_flags_ring_exactly(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    res = wash_cycles.detect(s, "polygon", [RELAYER], 0)
    assert res["flagged"] == RING                       # all 32, nothing else
    # pass-through: balanced (warn) but acyclic — never flagged
    mid = "0x" + "d2" * 20
    assert mid in res["balanced"] and mid not in res["cycle_wallets"]
    # imbalanced 2-cycle: on-cycle but not balanced — not flagged
    e1, e2 = "0x" + "e1" * 20, "0x" + "e2" * 20
    assert {e1, e2} <= res["cycle_wallets"]
    assert not ({e1, e2} & res["flagged"])
    # evidence: 30 concrete 3-cycles (hub, leg2, wallet_i), float = min edge
    three = [c for c in res["cycles"] if c["length"] == 3]
    assert len(three) == 30
    assert all(abs(c["float_usd"] - 197.0) < 0.01 for c in three)
    s.close()


def test_out_of_scope_cycles_invisible(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    _seed(s, facilitator="0x" + "99" * 20)   # NOT the registry relayer
    res = wash_cycles.detect(s, "polygon", [RELAYER], 0)
    assert not res["flagged"] and not res["cycle_wallets"]
    s.close()


def test_record_and_load_roundtrip(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    res = wash_cycles.detect(s, "polygon", [RELAYER], 0)
    wash_cycles.record(s, "2026-08-01", res, "t")
    flags, date, params = wash_cycles.load_flagged(s)
    assert date == "2026-08-01" and params["k"] == wash_cycles.CYCLE_K
    assert {w for (ch, w) in flags if ch == "polygon"} == RING
    # the flagged row carries each wallet's balance ratio (auditability)
    row = s.db.execute("SELECT params FROM wash_cycles WHERE cycle_id="
                       "'flagged'").fetchone()
    ratios = json.loads(row[0])["ratios"]
    assert set(ratios) == RING and all(r < 0.05 for r in ratios.values())
    s.close()


def test_run_wash_excludes_ring_from_clean(tmp_path, monkeypatch):
    """The full nightly path: run_wash must reclassify the ring with NO
    hand-curated flags file — clean volume ends at the honest remainder."""
    import wash

    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    reg = tmp_path / "relayer_registry.json"
    reg.write_text(json.dumps({"relayers_by_chain": {"polygon": [RELAYER]}}))
    monkeypatch.setattr(wash, "_REGISTRY", reg)      # also relocates flags dir
    monkeypatch.setattr(wash, "fetch_sent_recipients",
                        lambda *a, **k: set())       # no network
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    wash.run_wash(s, "2026-08-01")

    row = s.db.execute("SELECT clean_volume_usd, flagged_sellers, "
                       "coverage_note FROM clean_metrics WHERE chain='polygon'"
                       ).fetchone()
    clean_v, n_flagged, note = row
    # honest sellers $200 + pass-through $995; ring AND the reciprocal
    # 2-cycle both excluded ($1,200 of it by the v2 reciprocal check)
    assert abs(clean_v - 1195.0) < 0.01
    assert n_flagged >= 34                            # 32 ring + 2 reciprocal
    assert "cycle v3" in note                         # detector recorded
    s.close()


def test_solana_ring_detected_base58(tmp_path):
    """The detector is chain-agnostic: a Solana ring in base58 addresses is
    flagged exactly like the EVM one, and compute_clean_solana subtracts the
    cycle-flagged set via extra_flagged (Solana's own path is depth-2 and
    would miss this ring, same as the EVM funded-loop check missed Polygon)."""
    import wash_solana

    s = Store(tmp_path / "t.sqlite")
    # base58-style Solana addresses (case-sensitive, no 0x) — a 3-node ring
    hub, leg, spoke = "So1Hub" + "x" * 26, "So1Leg" + "y" * 26, "So1Spk" + "z" * 26
    ring = {hub, leg, spoke}
    rel = "So1Relayer" + "r" * 22
    ins = s._settlement_insert_sql(with_facilitator=True)
    n = [0]

    def tx(payer, seller, usd, count=1):
        for _ in range(count):
            n[0] += 1
            s.db.execute(ins, (f"sig{n[0]:06d}", 0, "solana", "usdc-mint",
                               payer, seller, int(usd * 1e6),
                               100 + n[0], 1750000000 + n[0], rel))
    tx(hub, leg, 300.0, 10)      # $3,000
    tx(leg, spoke, 297.0, 10)    # $2,970
    tx(spoke, hub, 296.0, 10)    # $2,960 — every node balances within 2%
    tx("So1Buyer" + "b" * 24, "So1Honest" + "h" * 23, 50.0, 5)  # honest seller
    s.db.commit()

    res = wash_cycles.detect(s, "solana", [rel], 0)
    assert res["flagged"] == ring
    wash_cycles.record(s, "2026-08-01", res, "t")

    # scores.py's cross-chain flag loader must surface Solana flags too
    flags, _, _ = wash_cycles.load_flagged(s)
    assert {w for (ch, w) in flags if ch == "solana"} == ring

    # clean path subtracts them via extra_flagged (empty funding graph)
    import json as _json
    reg = tmp_path / "relayer_registry.json"
    reg.write_text(_json.dumps({"facilitators": {"f": {"solana": [
        {"address": rel}]}}, "relayers_by_chain": {"solana": [rel]}}))
    wash_solana._REGISTRY = reg
    m = wash_solana.compute_clean_solana(
        s, "2026-08-01", {}, write=False, extra_flagged=res["flagged"])
    assert m["flagged_sellers"] == 3          # the ring, caught by cycles alone
    assert abs(m["clean_volume_usd"] - 250.0) < 0.01   # only the honest $250
    s.close()


def _snapshot(dirpath, rows):
    dirpath.mkdir(parents=True)
    with open(dirpath / "seller_aggregates.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["chain", "seller", "tx_count",
                                          "unique_payers", "self_pay_tx",
                                          "first_ts", "last_ts"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_scores_v3_cycle_cap_and_signals_live(tmp_path, monkeypatch):
    import scores
    now = int(time.time())
    snap = tmp_path / "snapshots" / "2026-08-01"
    _snapshot(snap, [
        dict(chain="polygon", seller=HUB, tx_count=150000, unique_payers=30,
             self_pay_tx=0, first_ts=now - 30 * 86400, last_ts=now),
        dict(chain="polygon", seller="0x" + "aa" * 20, tx_count=90,
             unique_payers=3, self_pay_tx=0,
             first_ts=now - 60 * 86400, last_ts=now)])
    flags = tmp_path / "flags"
    flags.mkdir()
    monkeypatch.setattr(scores, "SNAP_DIR", snap.parent)
    monkeypatch.setattr(scores, "FLAGS_DIR", flags)
    monkeypatch.setattr(scores, "MIN_MATURE_DAYS", 1)
    monkeypatch.setattr(scores, "CENSUS_DIRECTORY", tmp_path / "none.json")

    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    res = wash_cycles.detect(s, "polygon", [RELAYER], 0)
    wash_cycles.record(s, "2026-08-01", res, "t")
    assert scores.compute_seller_scores(s, "2026-08-01") == 2

    score, comp = s.db.execute("SELECT score, components FROM seller_scores "
                               "WHERE seller=?", (HUB,)).fetchone()
    comp = json.loads(comp)
    # the ring hub can never again hold a C: cycle flag caps it at D
    assert score <= 49.0
    assert comp["wash"]["cycle_flag"]
    assert comp["wash"]["cycle"]["detector"]["k"] == wash_cycles.CYCLE_K
    # every row records which wash inputs were live that night
    assert comp["signals_live"]["cycles"] == "2026-08-01"
    # the small honest seller: loyalty (repeat depth) now earns points
    score2, comp2 = s.db.execute(
        "SELECT score, components FROM seller_scores WHERE seller=?",
        ("0x" + "aa" * 20,)).fetchone()
    comp2 = json.loads(comp2)
    assert comp2["loyalty"] > 0 and "wash" not in comp2
    assert score2 > 0
    s.close()


def test_a_gate_requires_census_t2(tmp_path, monkeypatch):
    """A perfect-scoring seller stops at 89 (grade B) without census T2+
    evidence, and reaches A when the origin is settled/service-verified."""
    import scores
    now = int(time.time())
    seller = "0x" + "9a" * 20
    snap = tmp_path / "snapshots" / "2026-08-01"
    _snapshot(snap, [dict(chain="base", seller=seller, tx_count=2000000,
                          unique_payers=20000, self_pay_tx=0,
                          first_ts=now - 90 * 86400, last_ts=now)])
    flags = tmp_path / "flags"
    flags.mkdir()
    census = tmp_path / "directory.json"
    census.write_text(json.dumps({"origins": [
        {"origin": "api.real.example", "tier": "T1+"}]}))
    monkeypatch.setattr(scores, "SNAP_DIR", snap.parent)
    monkeypatch.setattr(scores, "FLAGS_DIR", flags)
    monkeypatch.setattr(scores, "MIN_MATURE_DAYS", 1)
    monkeypatch.setattr(scores, "CENSUS_DIRECTORY", census)

    s = Store(tmp_path / "t.sqlite")
    # listed origin with a perfect endpoint grade + old domain
    s.db.execute("INSERT INTO bazaar_census(measured_date,resource,domain,"
                 "seller_wallet) VALUES(?,?,?,?)",
                 ("2026-08-01", "https://api.real.example/v1", "real.example",
                  seller))
    s.record_endpoint_score("2026-08-01", "https://api.real.example", {
        "days_observed": 20, "probes": 40, "uptime_rate": 1.0,
        "valid_402_rate": 1.0, "median_latency_ms": 100, "score": 100.0,
        "grade": "A", "provisional": False, "score_version": 3}, "t")
    s.db.execute("INSERT INTO domain_intel(measured_date,domain,whois_created,"
                 "measured_at) VALUES(?,?,?,?)",
                 ("2026-08-01", "real.example", "2020-01-01T00:00:00Z", "t"))
    s.db.commit()

    assert scores.compute_seller_scores(s, "2026-08-01") == 1
    score, grade, comp = s.db.execute(
        "SELECT score, grade, components FROM seller_scores WHERE seller=?",
        (seller,)).fetchone()
    comp = json.loads(comp)
    assert score == 89.0 and grade == "B"
    assert comp["a_gate"] == "capped_no_T2_evidence"

    # promote the origin to T2 — the same seller now clears the gate
    census.write_text(json.dumps({"origins": [
        {"origin": "api.real.example", "tier": "T2"}]}))
    assert scores.compute_seller_scores(s, "2026-08-01") == 1
    score, grade, comp = s.db.execute(
        "SELECT score, grade, components FROM seller_scores WHERE seller=?",
        (seller,)).fetchone()
    comp = json.loads(comp)
    assert score >= 90.0 and grade == "A" and comp["a_gate"] == "T2+"
    s.close()

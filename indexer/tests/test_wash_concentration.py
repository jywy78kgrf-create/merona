"""Wash pattern 3 — single-payer concentration (indexer/wash.py).

The class every existing detector was blind to: BlockRun on Base had zero
self-pay, no reciprocal settlement pair, and no funded-loop hit (nothing
ever flows back to the seller), yet 99.58% of its volume came from ONE
payer — it was 38.8% of the *published clean* Base volume before this
detector existed (caught 2026-08-09). self-pay/reciprocal/cycles all look
for money coming BACK to the seller; a one-directional monoculture has no
such edge to find, so it needs its own top-payer-VOLUME-share scan.

All SQLite, no network (funding lookback stubbed exactly like
test_wash_cycles.py) and cycle detection turned off (X402_WASH_CYCLES=0) so
these tests isolate the concentration class; case (d) below uses a batch
flag (wash_flags_base.csv) to exercise the "already wash-flagged" overlap
instead, which is equally valid per the wash-bucket-vs-concentration-bucket
split in run_wash's clean-metrics loop.
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
import wash  # noqa: E402

RELAYER = "0x" + "aa" * 20
CHAIN = "base"

SELLER_CONC = "0x" + "c1" * 20         # case (a): concentrated, over floors
SELLER_DIVERSE = "0x" + "d1" * 20      # case (b): same volume, many payers
SELLER_SMALL_CONC = "0x" + "e1" * 20   # case (c): concentrated, under floors
SELLER_BOTH = "0x" + "f1" * 20         # case (d): batch-flagged AND concentrated
SELLER_BASELINE = "0x" + "b1" * 20     # case (d): honest control


def _mk_tx(store, facilitator=RELAYER):
    """Returns a tx(payer, seller, usd, count=1) closure, same shape as the
    helper in test_wash_cycles.py's _seed()."""
    ins = store._settlement_insert_sql(with_facilitator=True)
    n = [0]

    def tx(payer, seller, usd, count=1):
        for _ in range(count):
            n[0] += 1
            store.db.execute(ins, (f"0xtx{n[0]:06d}", 0, CHAIN, "0xusdc",
                                   payer, seller, int(round(usd * 1e6)),
                                   100 + n[0], 1750000000 + n[0], facilitator))
    return tx


def _seed_concentrated(tx, seller, big_n, big_usd, small_n, small_usd,
                       payer_prefix):
    """`big_n` tx of `big_usd` each from ONE payer, plus `small_n` tx of
    `small_usd` each from a second payer — i.e. everything except a sliver
    comes from a single counterparty."""
    tx(f"0x{payer_prefix}big" + "0" * 33, seller, big_usd, big_n)
    tx(f"0x{payer_prefix}sml" + "0" * 33, seller, small_usd, small_n)


def _seed_diverse(tx, seller, n, usd_each):
    """`n` tx of `usd_each` each from `n` DISTINCT payers — the honest
    shape: nobody's share comes close to CONC_SHARE."""
    for i in range(n):
        tx(f"0xdiverse{i:032x}", seller, usd_each)


def _write_batch_flags(tmp_path, sellers):
    p = tmp_path / f"wash_flags_{CHAIN}.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seller", "clean_exclude"])
        for s in sellers:
            w.writerow([s, "1"])


def _run_wash(store, tmp_path, monkeypatch, date="2026-08-01"):
    """Neutralizes network + cycle detection exactly like
    test_wash_cycles.py::test_run_wash_excludes_ring_from_clean, then runs
    the real run_wash() end to end."""
    reg = tmp_path / "relayer_registry.json"
    reg.write_text(json.dumps({"relayers_by_chain": {CHAIN: [RELAYER]}}))
    monkeypatch.setattr(wash, "_REGISTRY", reg)          # also relocates flags dir
    monkeypatch.setattr(wash, "fetch_sent_recipients", lambda *a, **k: set())
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    # Cycle detection is a different detector with its own acceptance suite
    # (test_wash_cycles.py); off here so these tests isolate concentration.
    monkeypatch.setenv("X402_WASH_CYCLES", "0")
    wash.run_wash(store, date)
    return store.db.execute(
        "SELECT clean_settlements, clean_volume_usd, flagged_sellers, "
        "excluded_settlements, coverage_note FROM clean_metrics "
        "WHERE chain=?", (CHAIN,)).fetchone()


def _signal_thresholds(store, seller, date="2026-08-01"):
    row = store.db.execute(
        "SELECT flagged, thresholds FROM wash_signals WHERE measured_date=? "
        "AND chain=? AND seller=?", (date, CHAIN, seller)).fetchone()
    if row is None:
        return None, None
    return row[0], json.loads(row[1])


def test_concentrated_seller_excluded(tmp_path, monkeypatch):
    """(a) 300 tx / $600, 99% from one payer -> over every floor -> excluded,
    and the exclusion shows up as its own named class (not folded into the
    wash-flagged count, since nothing else flagged this seller)."""
    s = Store(tmp_path / "t.sqlite")
    tx = _mk_tx(s)
    _seed_concentrated(tx, SELLER_CONC, big_n=297, big_usd=2.0,
                       small_n=3, small_usd=2.0, payer_prefix="conc")
    s.db.commit()

    clean_n, clean_v, flagged_sellers, excluded, note = _run_wash(
        s, tmp_path, monkeypatch)

    assert flagged_sellers == 0                 # no wash-bucket flag fired
    assert clean_n == 0 and clean_v == 0.0       # fully excluded
    assert excluded == 300
    assert "single-payer concentration excluded" in note
    assert ": 1 sellers" in note

    flagged, thr = _signal_thresholds(s, SELLER_CONC)
    assert flagged == 1
    # thresholds carries the concentration knobs, not the tops-loop ones —
    # this row is the concentration write (same PK as the tops-loop row,
    # which the concentration scan intentionally supersedes for the day)
    assert thr["conc_share"] == wash.CONC_SHARE
    assert thr["conc_min_tx"] == wash.CONC_MIN_TX
    s.close()


def test_diverse_seller_not_excluded(tmp_path, monkeypatch):
    """(b) same total volume as (a), but 300 distinct payers -> top-payer
    share ~0.3% -> never excluded."""
    s = Store(tmp_path / "t.sqlite")
    tx = _mk_tx(s)
    _seed_diverse(tx, SELLER_DIVERSE, n=300, usd_each=2.0)
    s.db.commit()

    clean_n, clean_v, flagged_sellers, excluded, note = _run_wash(
        s, tmp_path, monkeypatch)

    assert flagged_sellers == 0
    assert clean_n == 300 and abs(clean_v - 600.0) < 0.01   # untouched
    assert excluded == 0
    assert ": 0 sellers" in note

    flagged, thr = _signal_thresholds(s, SELLER_DIVERSE)
    assert "conc_share" not in thr               # concentration never fired
    s.close()


def test_small_concentrated_seller_under_floor_not_excluded(tmp_path, monkeypatch):
    """(c) share = 100% (one payer), but only 50 tx / $100 — under BOTH the
    CONC_MIN_TX and CONC_MIN_VOL floors -> not excluded. Without the floors
    a seller's first customer would trip this on day one."""
    s = Store(tmp_path / "t.sqlite")
    tx = _mk_tx(s)
    tx("0xonlypayer" + "0" * 30, SELLER_SMALL_CONC, 2.0, 50)   # 50 tx, $100, 100% one payer
    s.db.commit()

    clean_n, clean_v, flagged_sellers, excluded, note = _run_wash(
        s, tmp_path, monkeypatch)

    assert flagged_sellers == 0
    assert clean_n == 50 and abs(clean_v - 100.0) < 0.01
    assert excluded == 0
    assert ": 0 sellers" in note

    flagged, thr = _signal_thresholds(s, SELLER_SMALL_CONC)
    assert "conc_share" not in thr
    s.close()


def test_both_wash_and_concentration_flagged_not_double_subtracted(tmp_path, monkeypatch):
    """(d) SELLER_BOTH is batch-flagged (wash bucket) AND independently
    matches the concentration criteria. It must be subtracted exactly once:
    the wash bucket takes it, concentration_flagged excludes it as "already
    flagged", and the clean remainder is exactly the honest baseline
    seller's volume — if it were double-subtracted this would go negative /
    undercount by $600."""
    s = Store(tmp_path / "t.sqlite")
    tx = _mk_tx(s)
    _seed_concentrated(tx, SELLER_BOTH, big_n=297, big_usd=2.0,
                       small_n=3, small_usd=2.0, payer_prefix="both")
    _seed_diverse(tx, SELLER_BASELINE, n=200, usd_each=2.0)   # honest, $400
    s.db.commit()
    _write_batch_flags(tmp_path, [SELLER_BOTH])

    clean_n, clean_v, flagged_sellers, excluded, note = _run_wash(
        s, tmp_path, monkeypatch)

    assert flagged_sellers == 1                  # SELLER_BOTH, via the batch file
    assert clean_n == 200 and abs(clean_v - 400.0) < 0.01   # baseline only
    assert excluded == 300                        # SELLER_BOTH's rows, once
    # concentration's OWN count is 0 new exclusions — it matched SELLER_BOTH
    # too, but that seller was already in the wash bucket, so it does not
    # count a second time here
    assert ": 0 sellers" in note

    # the seller still shows up as a concentration MATCH in wash_signals
    # (evidence is recorded for every match, not just new exclusions)
    flagged, thr = _signal_thresholds(s, SELLER_BOTH)
    assert flagged == 1
    assert thr["conc_share"] == wash.CONC_SHARE
    s.close()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

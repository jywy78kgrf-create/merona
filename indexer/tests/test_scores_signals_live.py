"""indexer/scores.py — signals_live must be chain-truthful (2026-08-12 audit).

indexer/wash.py's WASH_CHAINS = ("base", "polygon") — Solana sellers never
get funded-payer, reciprocal-pair, or concentration scanning. Before this
fix, every row's `components.signals_live` published the SAME GLOBAL
MAX(measured_date) over the whole wash_signals table and the SAME GLOBAL
recip_scan_ok, regardless of the row's own chain — so a Solana seller's
published components asserted "wash scanned as of <date>" when zero wash
scanning ever touched that chain. SCORE_VERSION 3's own docstring says
signals_live exists precisely so "rows carry the evidence of their own
blind spots"; the bug did the opposite.

Uses the REAL scores.compute_seller_scores() end to end (same technique as
test_wash_cycles.py's test_scores_v3_cycle_cap_and_signals_live), seeding a
wash_signals row for ONLY the base seller — exactly what wash.py's nightly
funded-payer lookback writes, and exactly what it never writes for solana.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # indexer/

import scores                                   # noqa: E402
from storage import Store                       # noqa: E402
import wash                                     # noqa: E402

BASE_SELLER = "0x" + "ba" * 20
SOLANA_SELLER = "SoLSeLLeR1111111111111111111111111111111"


def _snapshot(dirpath, rows):
    dirpath.mkdir(parents=True)
    with open(dirpath / "seller_aggregates.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["chain", "seller", "tx_count",
                                          "unique_payers", "self_pay_tx",
                                          "first_ts", "last_ts"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _rows_by_chain(store) -> dict:
    out = {}
    for chain, comp in store.db.execute(
            "SELECT chain, components FROM seller_scores"):
        out[chain] = json.loads(comp)
    return out


def test_solana_row_shows_wash_not_scanned_base_row_shows_scanned(
        tmp_path, monkeypatch):
    """The core regression: a Solana seller's signals_live must say
    wash_scanned=False with null wash_signals/recip_scan_ok — never the
    Base/Polygon scan date — while a Base seller with an actual wash_signals
    row on file gets wash_scanned=True and ITS OWN chain's date."""
    now = int(time.time())
    first = now - 400 * 86400
    snap = tmp_path / "snapshots" / "2026-08-12"
    _snapshot(snap, [
        dict(chain="base", seller=BASE_SELLER, tx_count=500,
             unique_payers=60, self_pay_tx=0, first_ts=first, last_ts=now),
        dict(chain="solana", seller=SOLANA_SELLER, tx_count=500,
             unique_payers=60, self_pay_tx=0, first_ts=first, last_ts=now),
    ])
    monkeypatch.setattr(scores, "SNAP_DIR", snap.parent)
    monkeypatch.setattr(scores, "FLAGS_DIR", tmp_path / "flags")
    (tmp_path / "flags").mkdir()
    monkeypatch.setattr(scores, "MIN_MATURE_DAYS", 1)
    monkeypatch.setattr(scores, "CENSUS_DIRECTORY", tmp_path / "none.json")

    store = Store(tmp_path / "t.sqlite")
    # exactly what wash.py's nightly funded-payer lookback writes for a
    # scanned Base seller — and ONLY for base, matching the real pipeline
    # (WASH_CHAINS=("base","polygon") — nothing ever writes a solana row).
    store.record_wash_signal("2026-08-12", "base", BASE_SELLER, {
        "tx_count": 500, "unique_payers": 60, "self_pay_ratio": 0.0,
        "funded_payers": 10, "funded_payer_ratio": 0.05,
        "lookback_ok": True, "reciprocal": False, "flagged": False,
        "thresholds": {"self": 0.20, "funded": 0.20, "top_n": 150,
                       "recip_scan_ok": True}}, "2026-08-12T04:00:00Z")
    store.db.commit()

    assert scores.compute_seller_scores(store, "2026-08-12") == 2
    rows = _rows_by_chain(store)

    base_live = rows["base"]["signals_live"]
    assert base_live["wash_scanned"] is True
    assert base_live["wash_signals"] == "2026-08-12"
    assert base_live["recip_scan_ok"] is True

    sol_live = rows["solana"]["signals_live"]
    assert sol_live["wash_scanned"] is False
    assert sol_live["wash_signals"] is None
    assert sol_live["recip_scan_ok"] is None
    # never silently inherit base's scan date/status
    assert sol_live["wash_signals"] != base_live["wash_signals"]


def test_wash_scanned_matches_wash_module_constant_not_a_relisted_set(
        tmp_path, monkeypatch):
    """wash_scanned must be derived from wash.WASH_CHAINS (imported), not a
    separately maintained list in scores.py — every chain in WASH_CHAINS
    reads wash_scanned True, everything else False, and this can never
    drift because scores.py doesn't re-enumerate the chains itself."""
    now = int(time.time())
    first = now - 400 * 86400
    snap = tmp_path / "snapshots" / "2026-08-12"
    rows = [dict(chain=ch, seller=f"0x{i:02d}" + "aa" * 19, tx_count=500,
                unique_payers=60, self_pay_tx=0, first_ts=first, last_ts=now)
           for i, ch in enumerate(("base", "polygon", "solana"))]
    _snapshot(snap, rows)
    monkeypatch.setattr(scores, "SNAP_DIR", snap.parent)
    monkeypatch.setattr(scores, "FLAGS_DIR", tmp_path / "flags")
    (tmp_path / "flags").mkdir()
    monkeypatch.setattr(scores, "MIN_MATURE_DAYS", 1)
    monkeypatch.setattr(scores, "CENSUS_DIRECTORY", tmp_path / "none.json")

    store = Store(tmp_path / "t.sqlite")
    assert scores.compute_seller_scores(store, "2026-08-12") == 3
    got = _rows_by_chain(store)
    for chain, comp in got.items():
        expected = chain in wash.WASH_CHAINS
        assert comp["signals_live"]["wash_scanned"] is expected, chain


def test_load_wash_signals_returns_per_chain_dicts_not_one_global_date(
        tmp_path):
    """Unit-level pin on _load_wash_signals itself: wash_dates/recip_ok are
    dicts keyed by chain, and a chain with no wash_signals rows at all is
    simply absent (caller must .get() it, defaulting to None) — never
    populated from another chain's row."""
    store = Store(tmp_path / "t.sqlite")
    store.record_wash_signal("2026-08-10", "base", BASE_SELLER, {
        "tx_count": 10, "unique_payers": 5, "self_pay_ratio": 0.0,
        "funded_payers": None, "funded_payer_ratio": None,
        "lookback_ok": True, "reciprocal": False, "flagged": False,
        "thresholds": {"recip_scan_ok": True}}, "2026-08-10T00:00:00Z")
    store.record_wash_signal("2026-08-11", "polygon", "0x" + "cc" * 20, {
        "tx_count": 10, "unique_payers": 5, "self_pay_ratio": 0.0,
        "funded_payers": None, "funded_payer_ratio": None,
        "lookback_ok": True, "reciprocal": False, "flagged": False,
        "thresholds": {"recip_scan_ok": False}}, "2026-08-11T00:00:00Z")
    store.db.commit()

    sig, wash_dates, recip_ok = scores._load_wash_signals(store)
    assert wash_dates == {"base": "2026-08-10", "polygon": "2026-08-11"}
    assert recip_ok == {"base": True, "polygon": False}
    assert "solana" not in wash_dates and "solana" not in recip_ok


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

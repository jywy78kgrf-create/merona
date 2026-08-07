"""clean_series_daily: the hero-chart feed must be DAILY flow, current era.

Live incident 2026-08-06: the first emission published the raw cumulative
clean_volume_usd — the hero charted lifetime totals as a "$50M daily"
staircase, and pre-wash-v3 rows rendered the July 31 restatement as a market
crash. This pins the three rules that prevent a recurrence: deltas not
levels, nothing before CLEAN_SERIES_SINCE, negative deltas clamp to zero.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import daily_snapshot                                    # noqa: E402
from storage import Store                                # noqa: E402


def test_series_is_daily_delta_current_era_only(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_snapshot, "CLEAN_SERIES_SINCE", "2026-08-01")
    s = Store(tmp_path / "t.sqlite")
    rows = [
        # old-methodology era: inflated cumulative levels — must never appear
        ("2026-07-29", 25_000_000.0),
        ("2026-07-30", 50_000_000.0),
        # restatement night: cumulative collapses — the delta would be hugely
        # negative; as the first in-era row it only seeds the first diff
        ("2026-08-01", 400_000.0),
        ("2026-08-02", 414_000.0),      # +14,000
        ("2026-08-03", 413_500.0),      # -500 -> clamps to 0 (restatement)
        ("2026-08-04", 417_882.0),      # +4,382
    ]
    for d, cv in rows:
        s.record_clean_metrics(d, "base", {
            "scoped_settlements": 1, "scoped_volume_usd": cv,
            "flagged_sellers": 0, "excluded_settlements": 0,
            "clean_settlements": 1, "clean_volume_usd": cv,
            "coverage_note": "t"}, "t")
    s.db.commit()
    out = daily_snapshot.build_instr_metrics(s)
    ser = out["clean_series_daily"]["base"]
    assert [p["d"] for p in ser] == ["2026-08-02", "2026-08-03", "2026-08-04"]
    assert [p["v"] for p in ser] == [14000.0, 0.0, 4382.0]
    # levels are cumulative lifetime totals; none may leak through as a "day"
    assert all(p["v"] < 100_000 for p in ser)
    s.close()


def test_single_in_era_row_emits_nothing(tmp_path, monkeypatch):
    """One night of corrected data = zero deltas to chart. The dashboard's
    seed shapes (no dollar ticks) are more honest than a one-point 'series'."""
    monkeypatch.setattr(daily_snapshot, "CLEAN_SERIES_SINCE", "2026-08-01")
    s = Store(tmp_path / "t.sqlite")
    s.record_clean_metrics("2026-08-04", "base", {
        "scoped_settlements": 1, "scoped_volume_usd": 1.0,
        "flagged_sellers": 0, "excluded_settlements": 0,
        "clean_settlements": 1, "clean_volume_usd": 1.0,
        "coverage_note": "t"}, "t")
    s.db.commit()
    out = daily_snapshot.build_instr_metrics(s)
    assert "clean_series_daily" not in out or \
        "base" not in out.get("clean_series_daily", {})
    s.close()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

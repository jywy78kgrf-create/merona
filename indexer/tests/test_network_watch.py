"""Network-watch ledger: streak/day accounting, alert thresholds, idempotent
re-runs, and the committed watchlist rendering."""

from pathlib import Path

from indexer.instrumentation import (WATCH_ALERT_COUNT, WATCH_ALERT_STREAK,
                                     WATCH_STREAK_MIN_COUNT, net_name,
                                     update_watch_ledger, write_watchlist)


def test_first_sighting_creates_entry():
    led, alerts = update_watch_ledger({}, {"eip155:56": 1}, "2026-07-19")
    e = led["eip155:56"]
    assert e["first_seen"] == e["last_seen"] == "2026-07-19"
    assert e["days_seen"] == e["streak"] == e["last_count"] == e["max_count"] == 1
    assert alerts == []


def test_same_day_rerun_is_idempotent():
    led, _ = update_watch_ledger({}, {"eip155:56": 1}, "2026-07-19")
    led, _ = update_watch_ledger(led, {"eip155:56": 2}, "2026-07-19")
    e = led["eip155:56"]
    assert e["days_seen"] == 1 and e["streak"] == 1        # no double count
    assert e["last_count"] == 2 and e["max_count"] == 2    # counts refresh


def test_consecutive_days_build_streak_and_gap_resets_it():
    led = {}
    for d in ("2026-07-01", "2026-07-02", "2026-07-03"):
        led, _ = update_watch_ledger(led, {"x": 1}, d)
    assert led["x"]["streak"] == 3 and led["x"]["days_seen"] == 3
    led, _ = update_watch_ledger(led, {"x": 1}, "2026-07-10")  # 7-day gap
    assert led["x"]["streak"] == 1 and led["x"]["days_seen"] == 4


def test_count_threshold_alerts_and_does_not_realert_until_doubled():
    led, alerts = update_watch_ledger({}, {"x": WATCH_ALERT_COUNT}, "2026-07-19")
    assert len(alerts) == 1 and alerts[0]["count"] == WATCH_ALERT_COUNT
    # next day, small growth: silent
    led, alerts = update_watch_ledger(led, {"x": WATCH_ALERT_COUNT + 2}, "2026-07-20")
    assert alerts == []
    # doubled: re-alerts
    led, alerts = update_watch_ledger(led, {"x": WATCH_ALERT_COUNT * 2}, "2026-07-21")
    assert len(alerts) == 1


def test_persistent_lone_listing_never_alerts():
    """The polkadot-junk case: 1 listing forever must stay silent."""
    led = {}
    for i in range(1, 31):
        led, alerts = update_watch_ledger(led, {"junk": 1}, f"2026-06-{i:02d}")
        assert alerts == []
    assert led["junk"]["streak"] == 30


def test_streak_alert_needs_min_count():
    led = {}
    days = [f"2026-06-{i:02d}" for i in range(1, WATCH_ALERT_STREAK + 1)]
    for d in days[:-1]:
        led, alerts = update_watch_ledger(led, {"x": WATCH_STREAK_MIN_COUNT}, d)
        assert alerts == []
    led, alerts = update_watch_ledger(led, {"x": WATCH_STREAK_MIN_COUNT}, days[-1])
    assert len(alerts) == 1 and alerts[0]["streak"] == WATCH_ALERT_STREAK


def test_net_name_maps_and_falls_back():
    assert net_name("eip155:56") == "BNB Chain"
    assert net_name("polkadot:2f0555cc76fc2840a25a6ea3b9d68e50").startswith(
        "Polkadot-family (2f0555cc")
    assert net_name("weird:thing") == "weird:thing"


def test_watchlist_renders_and_is_stable(tmp_path: Path):
    led, _ = update_watch_ledger({}, {"eip155:56": 1, "x": 12}, "2026-07-19")
    p = tmp_path / "WATCHLIST.md"
    write_watchlist(led, "2026-07-19", path=p)
    txt = p.read_text()
    assert "BNB Chain" in txt and "`eip155:56`" in txt and "⚠ at 12" in txt
    write_watchlist(led, "2026-07-19", path=p)      # idempotent rewrite
    assert p.read_text() == txt

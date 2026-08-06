"""Passive-instrumentation tests. The load-bearing requirement: OBSERVE-ONLY
isolation — a crash in any probe must not touch settlements or stop the others.
Network is stubbed so tests are deterministic and offline."""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instrumentation as I  # noqa: E402
from storage import Store  # noqa: E402

PG_URL = os.environ.get("X402_TEST_DB_URL")


def _seed(store):
    now = int(time.time())

    def row(tx, li, chain, amt, ts):
        return dict(tx_hash=tx, log_index=li, chain=chain, token="0xusdc",
                    payer="0xp", seller="0xs", amount=amt,
                    block_number=1000 + li, block_timestamp=ts)
    store.commit_range("base", 1000, 1001,
                       [row("0xa", 0, "base", 1_000_000, now - 3600)], "t")
    store.commit_solana_batch([
        {**row("s1", 0, "solana", 500_000, now - 7200), "facilitator": "relAlpha"},
        {**row("s2", 1, "solana", 900_000, now - 5400), "facilitator": "relBeta"}])
    return now


def test_hourly_and_facilitator_derive(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    assert I.aggregate_hourly(s, "2026-07-05") >= 2      # base + solana hours
    assert I.compute_facilitator_health(s, "2026-07-05", ["base", "solana"]) >= 3
    # EVM has NO facilitator -> recorded as unattributed superset, with a note
    evm = s.db.execute("SELECT facilitator,derivable_note FROM facilitator_health "
                       "WHERE chain='base'").fetchone()
    assert evm[0] == "_unattributed_evm_superset" and "superset" in evm[1]
    # Solana IS attributed per-facilitator, latency/revert NULL (not derivable)
    sol = s.db.execute("SELECT facilitator,revert_count,median_latency_ms "
                       "FROM facilitator_health WHERE chain='solana' "
                       "AND facilitator='relAlpha'").fetchone()
    assert sol[0] == "relAlpha" and sol[1] is None and sol[2] is None
    s.close()


def test_valid_402_detection():
    class R:
        status_code = 402
        def iter_content(self, n):
            yield b'{"x402Version":1,"accepts":[]}'
    assert I._looks_like_x402_402(R()) is True

    class NotJson:
        status_code = 402
        def iter_content(self, n):
            yield b'<html>pay me</html>'
    assert I._looks_like_x402_402(NotJson()) is False

    class OK:
        status_code = 200
        def iter_content(self, n):
            yield b'{}'
    assert I._looks_like_x402_402(OK()) is False


def test_isolation_probe_crash_does_not_touch_settlements(tmp_path, monkeypatch):
    """Kill a probe mid-run: settlement rows unchanged, other probes still run,
    nothing propagates."""
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    before = s.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]

    # no network: Bazaar returns nothing (endpoint probes skip)
    monkeypatch.setattr(I, "fetch_bazaar_resources", lambda *a, **k: ([], 0))
    # facilitator_health explodes
    def boom(*a, **k):
        raise RuntimeError("simulated probe crash")
    monkeypatch.setattr(I, "compute_facilitator_health", boom)

    # must not raise
    I.run_instrumentation(s, "2026-07-05", {"fingerprint_version": 1},
                          chains=["base", "solana"])

    after = s.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    assert after == before                      # settlement ledger untouched
    # the crashing probe wrote nothing; the healthy one (hourly) still ran
    assert s.db.execute("SELECT COUNT(*) FROM facilitator_health").fetchone()[0] == 0
    assert s.db.execute("SELECT COUNT(*) FROM hourly_settlements").fetchone()[0] > 0
    # fingerprint still stamped
    assert s.db.execute("SELECT COUNT(*) FROM daily_fingerprint").fetchone()[0] == 1
    s.close()


def test_isolation_runner_never_raises(tmp_path, monkeypatch):
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    for fn in ("aggregate_hourly", "compute_facilitator_health",
               "fetch_bazaar_resources"):
        def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(I, fn, boom)
    # everything broken -> still returns cleanly, settlements safe (1 base + 2 sol)
    I.run_instrumentation(s, "2026-07-05", {"fingerprint_version": 1}, ["base"])
    assert s.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 3
    s.close()


@pytest.mark.skipif(not PG_URL, reason="X402_TEST_DB_URL not set")
def test_settlement_collectors_postgres():
    """Regression: the settlement-derived collectors must run on Postgres. A
    literal '%' in a parameterized LIKE broke psycopg's placeholder parser and
    failed hourly_settlements + facilitator_health on the live box."""
    import psycopg

    from storage import PostgresStore
    with psycopg.connect(PG_URL, autocommit=True) as c:
        for t in ("settlements", "indexed_ranges", "meta", "solana_cursors",
                  "coverage_ratio", "daily_fingerprint", "hourly_settlements",
                  "facilitator_health", "endpoint_liveness", "domain_intel"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
    s = PostgresStore(PG_URL)
    _seed(s)
    assert I.aggregate_hourly(s, "2026-07-05") >= 2
    assert I.compute_facilitator_health(s, "2026-07-05", ["base", "solana"]) >= 3
    s.close()


def test_domain_change_detection(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.record_domain_intel("2026-07-04", "x.com",
                          {"dns_fingerprint": "1.1.1.1"}, "t")
    assert s.last_domain_fingerprint("x.com", "2026-07-05") == "1.1.1.1"
    s.record_domain_intel("2026-07-05", "x.com",
                          {"dns_fingerprint": "2.2.2.2",
                           "changed_since_last": s.last_domain_fingerprint("x.com", "2026-07-05") != "2.2.2.2"},
                          "t")
    changed = s.db.execute("SELECT changed_since_last FROM domain_intel "
                           "WHERE measured_date='2026-07-05'").fetchone()[0]
    assert changed  # 1.1.1.1 -> 2.2.2.2
    s.close()

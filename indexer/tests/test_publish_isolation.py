"""PUBLISH-step hardening — 2026-08-13 production incident.

Real sequence: wash + scores now complete (the 2026-08-12 cascade-rollback
fix), so the nightly reaches the PUBLISH step — build_dashboard_metrics,
which calls build_seller_index and build_x402_scoped. Those run heavy
COUNT(DISTINCT ...) aggregates over the settlements table (~21M rows in
production) with `SET statement_timeout='0'` (UNBOUNDED — "runs once,
off-peak"). On the grown Base table, one of these unbounded scans ran long
enough that the whole nightly hit its systemd start-timeout wall and got
SIGTERM'd mid-publish — so NOTHING published (dashboard/seller-index blob
never written), even though wash and scores had already succeeded.

Two-part fix exercised here:
  PART A: the publish statement_timeout is now BOUNDED
          (daily_snapshot.PUBLISH_STATEMENT_TIMEOUT_MS), not '0'.
  PART B: build_seller_index and build_x402_scoped isolate EACH chain: a
          failing chain rolls back and either carries forward its
          LAST-PUBLISHED value (from meta[DASHBOARD_METRICS_KEY]) or gets an
          explicit {"unavailable": True} marker — never a fabricated zero,
          and never an exception that blanks every OTHER chain's numbers too.

Follows test_seller_index.py's seeding style and
test_wash_scores_isolation.py's Postgres-transaction-poisoning simulation
(SQLite has no native equivalent of a failed statement aborting the whole
transaction, so a thin proxy reproduces that semantics explicitly against the
REAL build_seller_index/build_x402_scoped/build_dashboard_metrics control
flow).
"""
from __future__ import annotations

import inspect
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # indexer/

import daily_snapshot                            # noqa: E402
from storage import Store                        # noqa: E402

RELAYER = "0x" + "fa" * 20

INS = ("INSERT INTO settlements(tx_hash,log_index,chain,token,payer,seller,"
       "amount,block_number,block_timestamp,facilitator) "
       "VALUES(?,?,?,?,?,?,?,?,?,?)")


def _registry(tmp_path, monkeypatch, chains=("base", "polygon")):
    """Point daily_snapshot.REGISTRY at a tmp registry whose relayer set is
    just RELAYER for each given chain — mirrors test_seller_index.py."""
    p = tmp_path / "relayer_registry.json"
    p.write_text(json.dumps(
        {"relayers_by_chain": {ch: [RELAYER] for ch in chains}}))
    monkeypatch.setattr(daily_snapshot, "REGISTRY", p)


def _seed_chain(store, chain, n_sellers, payers_each, usd_each=5.0, blk_base=1000):
    """n_sellers distinct sellers, each paid by payers_each distinct external
    payers of usd_each — well clear of self-pay, so tier math is trivial."""
    now = int(time.time())
    blk = blk_base
    for si in range(n_sellers):
        seller = f"0x{chain[:2]}se{si:036x}"
        for pi in range(payers_each):
            store.db.execute(INS, (
                f"0x{chain}tx{blk:08x}", 0, chain, "0xusdc",
                f"0x{chain}pa{pi:036x}", seller,
                int(round(usd_each * 1e6)), blk, now, RELAYER))
            blk += 1
    store.db.commit()


class _PoisoningSpyCursor:
    """Proxies a real sqlite3 cursor; execute() defers the poisoning check to
    the parent connection so state (fired/poisoned/rollback_calls) is shared
    across both direct store.db.execute(...) calls and store.db.cursor()...
    execute(...) calls (storage.py's stats()/scoped_stats()/
    relayer_enriched_fraction() all go through a cursor, not db.execute)."""

    def __init__(self, real_cursor, parent):
        self._real = real_cursor
        self._parent = parent

    def execute(self, sql, params=()):
        self._parent._check(sql, params)
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


class PoisoningSpyConn:
    """Wraps a real sqlite3 connection to reproduce Postgres
    transaction-poisoning semantics for a specific matched statement (see
    test_wash_scores_isolation.py for the earlier production incident this
    mirrors): the matched call raises once, and every execute() after it ALSO
    raises — through either store.db.execute(...) or
    store.db.cursor().execute(...) — until rollback() is called, exactly like
    a Postgres connection stuck in an aborted transaction."""

    def __init__(self, real, trigger):
        self._real = real
        self._trigger = trigger
        self._fired = False
        self.poisoned = False
        self.rollback_calls = 0

    def _check(self, sql, params):
        if self.poisoned:
            raise Exception("simulated: current transaction is aborted, "
                             "commands ignored until end of transaction block")
        if not self._fired and self._trigger(sql, params):
            self._fired = True
            self.poisoned = True
            raise Exception("simulated: canceling statement due to "
                             "statement timeout")

    def execute(self, sql, params=()):
        self._check(sql, params)
        return self._real.execute(sql, params)

    def cursor(self):
        return _PoisoningSpyCursor(self._real.cursor(), self)

    def rollback(self):
        self.rollback_calls += 1
        self.poisoned = False
        return self._real.rollback()

    def commit(self):
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


# --- PART A: the timeout that let this happen at all ------------------------

def test_publish_statement_timeout_is_bounded_and_used():
    """daily_snapshot's publish-step SET statement_timeout must be an
    explicit bound, never '0'/unbounded (that's what let one slow aggregate
    run past systemd's TimeoutStartSec wall on 2026-08-13 and take the whole
    nightly down mid-publish, losing the ENTIRE blob). Mirrors
    test_wash_clean_statement_timeout_raised_for_multiday_catchup's style for
    wash.py's own WASH_STATEMENT_TIMEOUT_MS."""
    assert daily_snapshot.PUBLISH_STATEMENT_TIMEOUT_MS > 0

    src = inspect.getsource(daily_snapshot)
    assert "statement_timeout='0'" not in src, (
        "found an unbounded SET statement_timeout='0' still in "
        "daily_snapshot.py — the publish path must use "
        "PUBLISH_STATEMENT_TIMEOUT_MS instead")
    # Both sites (store_dashboard_metrics + main()'s early lift) must use the
    # constant, not a bare hardcoded number.
    assert src.count("PUBLISH_STATEMENT_TIMEOUT_MS") >= 3, (
        "expected the constant's definition plus at least two SET call "
        "sites (store_dashboard_metrics and main()) to reference it")


# --- PART B: build_seller_index per-chain isolation --------------------------

def test_seller_index_chain_failure_carries_forward_prior_value(tmp_path, monkeypatch):
    """A poisoned polygon tier query must not blank base's tiers, and polygon
    must carry forward its LAST-PUBLISHED tier list rather than dropping the
    chain or publishing a fabricated zero-sellers row."""
    s = Store(tmp_path / "t.sqlite")
    _registry(tmp_path, monkeypatch, chains=("base", "polygon"))
    _seed_chain(s, "base", n_sellers=2, payers_each=3, blk_base=1000)
    _seed_chain(s, "polygon", n_sellers=1, payers_each=2, blk_base=5000)

    prior_polygon_tiers = [
        {"tier": "any", "min_payers": 0, "min_usd": 0,
         "sellers": 41, "volume_usd": 900.0},
        {"tier": "active", "min_payers": 2, "min_usd": 1,
         "sellers": 9, "volume_usd": 700.0},
    ]
    s.set_meta(daily_snapshot.DASHBOARD_METRICS_KEY, {
        "seller_index": {"tiers": {"polygon": prior_polygon_tiers}}})

    real_db = s.db

    def trigger(sql, params):
        # the seller-index tier query is parameterless (chain inlined) — see
        # test_seller_index_queries_bind_no_parameters — so match on SQL text.
        return "GROUP BY seller" in sql and "chain='polygon'" in sql

    spy = PoisoningSpyConn(real_db, trigger)
    s.db = spy
    try:
        idx = daily_snapshot.build_seller_index(s)
    finally:
        s.db = real_db

    assert spy.rollback_calls >= 1, (
        "build_seller_index must roll back after a chain's tier query fails, "
        "or the poison would carry into whatever runs next on this connection")

    # base: unaffected, freshly computed
    assert isinstance(idx["tiers"]["base"], list)
    by_tier = {t["tier"]: t for t in idx["tiers"]["base"]}
    assert by_tier["any"]["sellers"] == 2

    # polygon: carried forward EXACTLY the prior published tiers
    assert idx["tiers"]["polygon"] == prior_polygon_tiers
    assert "stale_chains" in idx
    assert idx["stale_chains"]["polygon"]["carried_forward"] is True
    s.close()


def test_seller_index_chain_failure_no_prior_marks_unavailable(tmp_path, monkeypatch):
    """With no prior published blob to carry forward, a failing chain must be
    marked unavailable — never a fabricated all-zero tier list (which would
    read as 'measured: no sellers' instead of 'not measured tonight')."""
    s = Store(tmp_path / "t.sqlite")
    _registry(tmp_path, monkeypatch, chains=("base", "polygon"))
    _seed_chain(s, "base", n_sellers=1, payers_each=2, blk_base=1000)
    _seed_chain(s, "polygon", n_sellers=1, payers_each=2, blk_base=5000)
    # deliberately no meta[DASHBOARD_METRICS_KEY] at all

    real_db = s.db

    def trigger(sql, params):
        return "GROUP BY seller" in sql and "chain='polygon'" in sql

    spy = PoisoningSpyConn(real_db, trigger)
    s.db = spy
    try:
        idx = daily_snapshot.build_seller_index(s)
    finally:
        s.db = real_db

    assert spy.rollback_calls >= 1
    assert isinstance(idx["tiers"]["base"], list)      # unaffected

    polygon_entry = idx["tiers"]["polygon"]
    assert isinstance(polygon_entry, dict)
    assert polygon_entry.get("unavailable") is True
    assert "sellers" not in polygon_entry, (
        "an unavailable chain must never carry a numeric sellers count")
    assert idx["stale_chains"]["polygon"]["carried_forward"] is False
    s.close()


def test_seller_index_happy_path_has_no_stale_chains_key(tmp_path, monkeypatch):
    """Byte-compatibility guard: when nothing fails, the new 'stale_chains'
    key must not appear at all (existing consumers only ever saw
    method/tiers)."""
    s = Store(tmp_path / "t.sqlite")
    _registry(tmp_path, monkeypatch, chains=("base",))
    _seed_chain(s, "base", n_sellers=1, payers_each=2, blk_base=1000)
    idx = daily_snapshot.build_seller_index(s)
    assert "stale_chains" not in idx
    assert set(idx) == {"method", "tiers"}
    s.close()


# --- PART B: build_dashboard_metrics / build_x402_scoped isolation ----------

def test_x402_scoped_chain_failure_marks_that_chain_only(tmp_path, monkeypatch):
    """build_x402_scoped used to have NO per-chain try/except at all — any
    chain's failing aggregate raised straight out of the function. It must
    now isolate each chain: a failing chain gets an unavailable marker while
    every other chain's dict is untouched."""
    s = Store(tmp_path / "t.sqlite")
    _registry(tmp_path, monkeypatch, chains=("base", "polygon"))
    _seed_chain(s, "base", n_sellers=2, payers_each=3, blk_base=1000)
    _seed_chain(s, "polygon", n_sellers=1, payers_each=2, blk_base=5000)

    real_db = s.db

    def trigger(sql, params):
        return (sql.startswith("SELECT COUNT(*) FROM settlements WHERE")
                and params and params[0] == "polygon")

    spy = PoisoningSpyConn(real_db, trigger)
    s.db = spy
    try:
        out = daily_snapshot.build_x402_scoped(s)
    finally:
        s.db = real_db

    assert spy.rollback_calls >= 1
    assert out["base"]["eip3009_superset"]["settlements"] == 6
    assert out["polygon"].get("unavailable") is True
    assert "reason" in out["polygon"]
    s.close()


def test_build_dashboard_metrics_always_publishes_under_chain_failure(
        tmp_path, monkeypatch):
    """THE overarching invariant: build_dashboard_metrics ALWAYS returns a
    writable blob — and so the nightly ALWAYS publishes — even when one
    chain's queries fail/timeout. The publish step degrades to
    partial/stale-marked, but must never raise and must never come back
    empty."""
    s = Store(tmp_path / "t.sqlite")
    _registry(tmp_path, monkeypatch, chains=("base", "polygon"))
    _seed_chain(s, "base", n_sellers=2, payers_each=3, blk_base=1000)
    _seed_chain(s, "polygon", n_sellers=1, payers_each=2, blk_base=5000)

    prior_polygon_row = {
        "scopable": True, "registry": 1, "enriched": 1.0,
        "superset_settlements": 50, "superset_volume_usd": 500.0,
        "min_block": 1, "max_block": 100,
        "settlements": 40, "volume_usd": 400.0, "sellers": 7, "payers": 20,
        "avg_usd": 10.0, "share_count": 0.8, "share_vol": 0.8,
    }
    s.set_meta(daily_snapshot.DASHBOARD_METRICS_KEY, {
        "chains": {"polygon": prior_polygon_row},
        "seller_index": {"tiers": {}},
    })

    real_db = s.db

    def trigger(sql, params):
        return (sql.startswith("SELECT COUNT(*) FROM settlements WHERE")
                and params and params[0] == "polygon")

    spy = PoisoningSpyConn(real_db, trigger)
    s.db = spy
    try:
        metrics = daily_snapshot.build_dashboard_metrics(s)   # must not raise
    finally:
        s.db = real_db

    assert spy.rollback_calls >= 1
    assert isinstance(metrics, dict)

    # base: fresh, unaffected by polygon's failure
    assert metrics["chains"]["base"].get("settlements") == 6

    # polygon: carried forward the prior published scoped/superset numbers —
    # the section that failed. (The independent merchant-tier query below in
    # build_dashboard_metrics isn't poisoned by this trigger, so it still
    # refreshes on top of the carried-forward base row; that's correct
    # layered degradation, not a bug — only the failed section's numbers
    # should be stale.)
    for k, v in prior_polygon_row.items():
        assert metrics["chains"]["polygon"][k] == v, k
    assert "stale_chains" in metrics and "polygon" in metrics["stale_chains"]
    assert any(n.get("carried_forward")
               for n in metrics["stale_chains"]["polygon"])

    # the blob is a normal, JSON-serializable dict — exactly what
    # store_dashboard_metrics hands to store.set_meta() to publish
    json.dumps(metrics)
    assert "totals" in metrics and "generated_at" in metrics
    assert "seller_index" in metrics   # still built, despite polygon's failure
    s.close()


def test_build_dashboard_metrics_publishes_even_with_no_prior_and_chain_failure(
        tmp_path, monkeypatch):
    """Same invariant, but with nothing to carry forward — the blob must
    still come back complete-enough and writable, with polygon explicitly
    marked unavailable rather than silently missing or fabricated."""
    s = Store(tmp_path / "t.sqlite")
    _registry(tmp_path, monkeypatch, chains=("base", "polygon"))
    _seed_chain(s, "base", n_sellers=1, payers_each=2, blk_base=1000)
    _seed_chain(s, "polygon", n_sellers=1, payers_each=2, blk_base=5000)
    # no prior blob at all

    real_db = s.db

    def trigger(sql, params):
        return (sql.startswith("SELECT COUNT(*) FROM settlements WHERE")
                and params and params[0] == "polygon")

    spy = PoisoningSpyConn(real_db, trigger)
    s.db = spy
    try:
        metrics = daily_snapshot.build_dashboard_metrics(s)   # must not raise
    finally:
        s.db = real_db

    assert isinstance(metrics, dict)
    assert metrics["chains"]["base"].get("settlements") == 2
    assert metrics["chains"]["polygon"].get("unavailable") is True
    json.dumps(metrics)
    s.close()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

"""Transaction-poisoning cascade — 2026-08-12 production incident.

Real log sequence from a 3-day catch-up run:
  [wash] base clean: 2,600,258 settlements / $352,015 ...        <- SUCCEEDED
  [wash] clean metrics polygon failed: canceling statement due to statement timeout
  [wash] solana clean failed (isolated): current transaction is aborted, ...
  [scores] endpoint_scores failed (isolated): InFailedSqlTransaction: ...
  [scores] seller_scores failed (isolated): InFailedSqlTransaction: ...
  [scores] payer_scores failed (isolated): InFailedSqlTransaction: ...

Postgres aborts the WHOLE transaction on a failed statement; every later
statement on that connection then raises InFailedSqlTransaction until a
rollback. indexer/wash.py's per-chain clean-metrics loop and
indexer/scores.py's run_scores loop both caught their own exceptions and
printed, but never rolled back — so a Polygon statement timeout took out
Solana clean AND every downstream score pass as collateral, even though
each section is individually exception-wrapped and labeled "(isolated)".

SQLite has no native equivalent of Postgres transaction-poisoning (a failed
statement here doesn't taint the connection), so the regression tests below
build a thin proxy around the real sqlite3 connection that reproduces the
Postgres semantics explicitly: once "poisoned", every execute() raises until
rollback() is called. That lets these tests exercise the REAL wash.run_wash()
/ scores.run_scores() control flow and genuinely fail against the pre-fix
code (no rollback -> stays poisoned -> the next section's query also raises)
while passing once the rollback is in place.
"""
from __future__ import annotations

import inspect
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # indexer/

from storage import Store  # noqa: E402
import scores  # noqa: E402
import wash  # noqa: E402

RELAYER = "0x" + "aa" * 20


class PoisoningSpyConn:
    """Wraps a real sqlite3 connection. `trigger(sql, params)` fires exactly
    once (single-shot); when it does, the wrapped call raises AND the proxy
    becomes "poisoned" — every subsequent execute() raises too, exactly like
    a Postgres connection stuck in an aborted transaction, until rollback()
    is called (which clears the poison, matching a real ROLLBACK)."""

    def __init__(self, real, trigger):
        self._real = real
        self._trigger = trigger
        self._fired = False
        self.poisoned = False
        self.rollback_calls = 0

    def execute(self, sql, params=()):
        if self.poisoned:
            raise Exception("simulated: current transaction is aborted, "
                             "commands ignored until end of transaction block")
        if not self._fired and self._trigger(sql, params):
            self._fired = True
            self.poisoned = True
            raise Exception("simulated: canceling statement due to "
                             "statement timeout")
        return self._real.execute(sql, params)

    def rollback(self):
        self.rollback_calls += 1
        self.poisoned = False
        return self._real.rollback()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _mk_tx(store, chain):
    ins = store._settlement_insert_sql(with_facilitator=True)
    n = [0]

    def tx(payer, seller, usd, count=1):
        for _ in range(count):
            n[0] += 1
            store.db.execute(ins, (f"0xtx{chain}{n[0]:06d}", 0, chain, "0xusdc",
                                   payer, seller, int(round(usd * 1e6)),
                                   100 + n[0], 1750000000 + n[0], RELAYER))
    return tx


def _seed_diverse(tx, seller, n, usd_each, prefix):
    """n tx of usd_each from n distinct payers — well clear of every wash /
    concentration / self-pay / reciprocal trigger, so the clean-metrics
    number is simply the raw total."""
    for i in range(n):
        tx(f"0x{prefix}{i:032x}", seller, usd_each)


def test_clean_metrics_polygon_survives_base_statement_timeout(
        tmp_path, monkeypatch):
    """The core regression for wash.py's clean-metrics loop: a Postgres
    statement-timeout on BASE's clean-metrics query must not prevent
    POLYGON's clean metrics from being computed on the same connection."""
    s = Store(tmp_path / "t.sqlite")
    tx_base = _mk_tx(s, "base")
    tx_poly = _mk_tx(s, "polygon")
    _seed_diverse(tx_base, "0x" + "b1" * 20, n=20, usd_each=2.0, prefix="base")
    _seed_diverse(tx_poly, "0x" + "p1" * 20, n=30, usd_each=3.0, prefix="poly")
    s.db.commit()

    reg = tmp_path / "relayer_registry.json"
    reg.write_text(json.dumps({"relayers_by_chain": {
        "base": [RELAYER], "polygon": [RELAYER]}}))
    monkeypatch.setattr(wash, "_REGISTRY", reg)
    monkeypatch.setattr(wash, "fetch_sent_recipients", lambda *a, **k: set())
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setenv("X402_WASH_CYCLES", "0")   # isolate this class of bug

    real_db = s.db

    def trigger(sql, params):
        # exactly the clean-metrics loop's scoped-total query (wash.py:486),
        # matched to the FIRST (base) iteration only via the bound chain arg.
        return (sql.startswith(
                    "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM settlements WHERE")
                and params and params[0] == "base")

    spy = PoisoningSpyConn(real_db, trigger)
    s.db = spy

    wash.run_wash(s, "2026-08-12")

    assert spy.rollback_calls >= 1, (
        "clean-metrics except handler must call store.db.rollback() after "
        "a chain's query fails, or the poison carries into the next chain")

    base_row = real_db.execute(
        "SELECT clean_settlements FROM clean_metrics WHERE chain='base'"
    ).fetchone()
    assert base_row is None, "base's own failed pass should not have written"

    poly_row = real_db.execute(
        "SELECT clean_settlements, clean_volume_usd FROM clean_metrics "
        "WHERE chain='polygon'").fetchone()
    assert poly_row is not None, (
        "polygon clean metrics must still run despite base's statement "
        "timeout on the same connection")
    assert poly_row[0] == 30
    assert abs(poly_row[1] - 90.0) < 0.01
    s.close()


def test_solana_clean_failure_rolls_back(tmp_path, monkeypatch):
    """wash.py's solana-clean except handler (the LAST section of run_wash,
    immediately upstream of scores.run_scores on the same connection) must
    also roll back, or its failure would carry straight into scores.py."""
    s = Store(tmp_path / "t.sqlite")
    s.db.commit()
    reg = tmp_path / "relayer_registry.json"
    reg.write_text(json.dumps({"relayers_by_chain": {}}))   # base/polygon skip
    monkeypatch.setattr(wash, "_REGISTRY", reg)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setenv("X402_WASH_CYCLES", "0")
    monkeypatch.setenv("X402_SOLANA_CLEAN", "1")

    gpath = tmp_path / "funding_solana.csv.gz"
    gpath.write_bytes(b"")   # only existence is checked before load
    monkeypatch.setenv("X402_SOLANA_FUNDING", str(gpath))

    import wash_solana

    def boom(path):
        raise Exception("simulated: current transaction is aborted, "
                         "commands ignored until end of transaction block")

    monkeypatch.setattr(wash_solana, "load_funding_graph", boom)

    class RollbackSpy:
        def __init__(self, real):
            self._real = real
            self.rollback_calls = 0

        def rollback(self):
            self.rollback_calls += 1
            return self._real.rollback()

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_db = s.db
    spy = RollbackSpy(real_db)
    s.db = spy

    wash.run_wash(s, "2026-08-12")

    assert spy.rollback_calls >= 1
    s.close()


def test_run_scores_rollback_lets_seller_and_payer_scores_run_after_endpoint_failure(
        tmp_path, monkeypatch):
    """The other half of the incident: scores.run_scores' per-scorer except
    handler must roll back so a poisoned connection from endpoint_scores'
    failure doesn't also take out seller_scores and payer_scores."""
    s = Store(tmp_path / "t.sqlite")
    s.db.commit()

    class PoisonFlagConn:
        """Simpler poisoning proxy driven by an explicit flag the fake
        scorer functions set/observe directly (they don't issue matching SQL
        of their own — they stand in for the real scorers)."""
        def __init__(self, real):
            self._real = real
            self.poisoned = False
            self.rollback_calls = 0

        def execute(self, sql, params=()):
            if self.poisoned:
                raise Exception("simulated: current transaction is aborted, "
                                 "commands ignored until end of transaction "
                                 "block")
            return self._real.execute(sql, params)

        def rollback(self):
            self.rollback_calls += 1
            self.poisoned = False
            return self._real.rollback()

        def __getattr__(self, name):
            return getattr(self._real, name)

    s.db = PoisonFlagConn(s.db)

    entered = []
    succeeded = []

    def fail_endpoint(store, date):
        entered.append("endpoint_scores")
        store.db.poisoned = True   # what a real statement-timeout leaves behind
        raise Exception("simulated: canceling statement due to statement timeout")

    def try_seller(store, date):
        entered.append("seller_scores")
        store.db.execute("SELECT 1")   # raises if the connection is still poisoned
        succeeded.append("seller_scores")
        return 0

    def try_payer(store, date):
        entered.append("payer_scores")
        store.db.execute("SELECT 1")
        succeeded.append("payer_scores")
        return 0

    import payer_scores
    monkeypatch.setattr(scores, "compute_endpoint_scores", fail_endpoint)
    monkeypatch.setattr(scores, "compute_seller_scores", try_seller)
    monkeypatch.setattr(payer_scores, "compute_payer_scores", try_payer)

    scores.run_scores(s, "2026-08-12")

    # the loop always visits every scorer regardless of the fix (no early
    # exit) — the real bug is whether their DB work could actually complete.
    assert entered == ["endpoint_scores", "seller_scores", "payer_scores"]
    assert succeeded == ["seller_scores", "payer_scores"], (
        "seller_scores/payer_scores must complete their DB work even though "
        "endpoint_scores failed first on the same connection")
    assert s.db.rollback_calls >= 1
    s.close()


# --- minimum-bar source pin (per task spec: guards against a silent
# regression even in an environment where the behavioral tests above somehow
# can't distinguish poisoned/unpoisoned semantics) --------------------------
def test_isolated_except_handlers_source_calls_rollback():
    """Every except handler this incident's fix touches must contain a
    store.db.rollback() call in its body, not just a print. Cheap regression
    guard that doesn't depend on DB semantics at all."""
    wash_src = inspect.getsource(wash.run_wash)
    # pre-existing: reciprocal scan, cycle detection, concentration scan (3).
    # this fix adds: per-chain clean-metrics, solana clean (2). Total >= 5.
    assert wash_src.count("store.db.rollback()") >= 5, (
        "run_wash must roll back after BOTH the per-chain clean-metrics "
        "failure and the solana-clean failure, in addition to the "
        "pre-existing reciprocal/cycle/concentration rollbacks")

    scores_src = inspect.getsource(scores.run_scores)
    assert "store.db.rollback()" in scores_src


# --- Fix 2: the statement timeout that fired -------------------------------
def test_wash_clean_statement_timeout_raised_for_multiday_catchup():
    """wash.py:289's SET statement_timeout must be generous enough for a
    multi-day catch-up (the 600000/10min value is what actually fired on
    Polygon during the 2026-08-12 incident), but still an explicit bound —
    not unbounded '0' (that trades a cascading DB failure for a risk of
    running past systemd's TimeoutStartSec, see deploy/run.sh)."""
    assert wash.WASH_STATEMENT_TIMEOUT_MS >= 1800000
    src = inspect.getsource(wash.run_wash)
    assert "WASH_STATEMENT_TIMEOUT_MS" in src, (
        "the SET statement_timeout inside run_wash must use the raised "
        "constant, not a hardcoded '600000'")


# --- Fix 3: the attest hang that burned to the systemd wall -----------------
def test_run_sh_wraps_anchor_and_attest_in_timeout():
    run_sh = (HERE.parent.parent / "deploy" / "run.sh").read_text()
    assert "timeout" in run_sh
    for script in ("./deploy/anchor.sh", "./deploy/attest.sh"):
        for line in run_sh.splitlines():
            if script in line:
                assert line.strip().startswith("timeout "), (
                    f"{script} must be invoked as `timeout N {script}`, "
                    f"got: {line!r}")
                assert line.rstrip().endswith("|| true"), (
                    f"{script} must keep `|| true` so a timeout kill "
                    f"(exit 124) stays non-fatal")
                break
        else:
            raise AssertionError(f"{script} invocation not found in run.sh")


def test_run_sh_and_deploy_scripts_are_valid_bash():
    import subprocess
    for rel in ("deploy/run.sh", "deploy/attest.sh", "deploy/anchor.sh"):
        p = HERE.parent.parent / rel
        r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, f"{rel}: {r.stderr}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

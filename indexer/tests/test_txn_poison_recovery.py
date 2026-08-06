"""Regression: a poisoned DB transaction must not crash the snapshot run.

On 2026-07-07/08 a failed canary query left the psycopg transaction ABORTED;
the next DB read (build_coverage_fingerprint -> get_meta) then raised
InFailedSqlTransaction uncaught and killed the whole run, losing those
snapshots. build_coverage_fingerprint now rolls back any poisoned txn before
its own reads, so an upstream failure can never take the snapshot down here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import daily_snapshot  # noqa: E402


class _AbortedError(Exception):
    """Stand-in for psycopg.errors.InFailedSqlTransaction."""


class _PoisonedDB:
    """Mimics a psycopg connection stuck in an aborted transaction: every
    execute raises until rollback() is called."""
    def __init__(self):
        self.aborted = True
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1
        self.aborted = False

    def execute(self, *a, **k):
        if self.aborted:
            raise _AbortedError("current transaction is aborted")
        return self

    def fetchone(self):
        return (None,)


class _Store:
    PH = "%s"

    def __init__(self):
        self.db = _PoisonedDB()

    def q(self, s):
        return s

    def get_meta(self, key):
        # the exact call that crashed: a read on the (maybe poisoned) connection
        row = self.db.execute("SELECT value FROM meta WHERE key=%s",
                              (key,)).fetchone()
        return row[0] if row else None


def test_fingerprint_recovers_from_poisoned_transaction(tmp_path, monkeypatch):
    # no registry file -> the json.load path is skipped safely
    monkeypatch.setattr(daily_snapshot, "REGISTRY", tmp_path / "nope.json")
    store = _Store()
    assert store.db.aborted is True                      # start poisoned

    fp = daily_snapshot.build_coverage_fingerprint(
        store, finalized_heights={"base": {"finalized_block": 1}},
        canary={"base": {"eip3009": {"ratio": 0.97}}})

    assert store.db.rollbacks == 1                       # cleared the poison
    assert fp["fingerprint_version"] == 1                # and produced the fp
    assert fp["eip3009_coverage_ratio"]["base"] == 0.97  # canary data preserved
    assert "base" in fp["chains"]


def test_canary_rollback_is_wired():
    """The canary except path must roll back — the primary fix. Assert the
    source contains the rollback in the failure handler (behavioural test needs
    live RPC; this guards against the line being removed)."""
    src = Path(daily_snapshot.__file__).read_text()
    i = src.index("[canary] {chain.name} measurement failed")
    # within the except block that follows, a rollback must appear
    assert "store.db.rollback()" in src[i:i + 900]

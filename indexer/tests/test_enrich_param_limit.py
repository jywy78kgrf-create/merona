"""Regression: relayer enrichment must not exceed Postgres's 65535-parameter
statement limit. A dense block chunk yielded >32767 (tx, from) pairs, so the
single VALUES-join UPDATE blew the ceiling and aborted the whole nightly
enrichment pass — letting x402-scoped coverage decay (observed 2026-07-14,
Base fell 97.7% -> 77.5%). _apply_updates must sub-batch under the limit.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import enrich_relayers                                   # noqa: E402

PG_MAX_PARAMS = 65535


class _FakePGCursor:
    """Mimics psycopg's hard parameter ceiling and NULL-only UPDATE semantics."""

    def __init__(self, store):
        self.store = store
        self.rowcount = 0

    def execute(self, sql, params=None):
        params = params or []
        if len(params) > PG_MAX_PARAMS:
            raise Exception("number of parameters must be between 0 and 65535")
        # params = [tx0, frm0, tx1, frm1, ..., chain]; apply NULL-only fills
        chain = params[-1]
        stamped = 0
        for i in range(0, len(params) - 1, 2):
            tx, frm = params[i], params[i + 1]
            key = (chain, tx)
            if key in self.store.rows and self.store.rows[key] is None:
                self.store.rows[key] = frm
                stamped += 1
        self.rowcount = stamped


class _FakePGStore:
    PH = "%s"

    def __init__(self):
        self.rows: dict = {}          # (chain, tx) -> relayer or None
        self.db = self

    def cursor(self):
        return _FakePGCursor(self)

    def commit(self):
        pass


def test_apply_updates_stays_under_param_limit():
    store = _FakePGStore()
    # 50,000 pairs in one call — the old single-VALUES path (100,001 params)
    # would raise; the sub-batched path must apply every one.
    pairs = [(f"0x{i:064x}", f"0xrelayer{i % 7}") for i in range(50000)]
    for tx, _ in pairs:
        store.rows[("base", tx)] = None

    n = enrich_relayers._apply_updates(store, "base", pairs)

    assert n == 50000, f"expected all 50k stamped, got {n}"
    assert all(v is not None for v in store.rows.values()), "left NULL rows"


def test_old_singlevalues_would_have_failed():
    """Guard the guard: prove the FakePGCursor actually enforces the ceiling,
    so the test above is meaningful (a 50k-pair single statement must raise)."""
    store = _FakePGStore()
    cur = store.cursor()
    huge = ["x"] * (PG_MAX_PARAMS + 1)
    raised = False
    try:
        cur.execute("UPDATE ...", huge)
    except Exception as e:
        raised = "65535" in str(e)
    assert raised, "fake cursor must enforce the 65535 limit"


def test_idempotent_never_overwrites():
    """NULL-only fill: a second pass with different relayers must not clobber."""
    store = _FakePGStore()
    store.rows[("base", "0xabc")] = None
    enrich_relayers._apply_updates(store, "base", [("0xabc", "0xr1")])
    enrich_relayers._apply_updates(store, "base", [("0xabc", "0xr2")])
    assert store.rows[("base", "0xabc")] == "0xr1"

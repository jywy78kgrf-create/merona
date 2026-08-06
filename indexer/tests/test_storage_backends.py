"""Storage backend parity: the SQLite (default) and Postgres backends must
behave identically. The Postgres test is skipped unless X402_TEST_DB_URL points
at a throwaway database, so the default suite stays SQLite-only and needs no DB.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import PostgresStore, Store  # noqa: E402

PG_URL = os.environ.get("X402_TEST_DB_URL")


def _row(tx, li, chain="base", amt=1000, fac=None):
    d = dict(tx_hash=tx, log_index=li, chain=chain, token="0xusdc", payer="0xp",
             seller="0xs", amount=amt, block_number=100, block_timestamp=1730000000)
    if fac is not None:
        d["facilitator"] = fac
    return d


def _exercise(store):
    # meta
    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"
    assert store.get_meta("missing") is None
    # idempotent commit_range
    assert store.commit_range("base", 100, 199,
                              [_row("0xa", 0), _row("0xb", 1)], "t") == 2
    store.commit_range("base", 200, 299, [_row("0xa", 0), _row("0xc", 2)], "t")
    assert store.stats("base")["settlements"] == 3   # 0xa,0 dupe ignored
    # frontier + gaps
    assert store.covered_frontier("base", 100) == 299
    assert store.find_gaps("base", 100, 399) == [(300, 399)]
    # solana cursor + batch
    store.commit_solana_batch([_row("sig", 0, chain="solana", fac="rX")])
    store.set_solana_cursor("rX", "sig", 7, "t")
    assert store.get_solana_cursor("rX") == ("sig", 7)
    assert store.stats("solana")["settlements"] == 1


def _snapshot_volume_clean_int(store, snap_dir):
    """write_seller_snapshot must produce a clean-integer volume on BOTH backends.
    Regression guard: it used to hardcode SQLite's total(), which does not exist on
    Postgres (function total(numeric) does not exist)."""
    import csv

    import daily_snapshot as ds
    store.commit_range("base", 100, 199,
                       [_row("0xa", 0, amt=1000), _row("0xb", 1, amt=2000)], "t")
    ds.SNAP_DIR = Path(snap_dir)
    p = ds.write_seller_snapshot(store, "2026-07-05", 199)
    with open(p) as f:
        vols = [r["volume_base_units"] for r in csv.DictReader(f)]
    assert vols == ["3000"], f"expected clean-int volume ['3000'], got {vols}"


def test_sqlite_backend(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    _exercise(s)
    s.close()


def test_snapshot_volume_sqlite(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    _snapshot_volume_clean_int(s, tmp_path / "snaps")
    s.close()


@pytest.mark.skipif(not PG_URL, reason="X402_TEST_DB_URL not set")
def test_snapshot_volume_postgres(tmp_path):
    import psycopg
    with psycopg.connect(PG_URL, autocommit=True) as c:
        for t in ("settlements", "indexed_ranges", "meta", "solana_cursors"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
    s = PostgresStore(PG_URL)
    _snapshot_volume_clean_int(s, tmp_path / "snaps")
    s.close()


@pytest.mark.skipif(not PG_URL, reason="X402_TEST_DB_URL not set")
def test_postgres_backend():
    import psycopg
    with psycopg.connect(PG_URL, autocommit=True) as c:   # clean slate
        for t in ("settlements", "indexed_ranges", "meta", "solana_cursors"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
    s = PostgresStore(PG_URL)
    _exercise(s)
    # uint256 amount: NUMERIC holds it exactly; SQLite INTEGER would overflow
    big = 10 ** 30
    s.commit_range("polygon", 1, 1, [_row("0xbig", 9, chain="polygon", amt=big)], "t")
    assert s.stats("polygon")["volume_base_units"] == big
    s.close()

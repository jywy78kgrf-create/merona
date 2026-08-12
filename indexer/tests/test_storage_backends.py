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


# --- bazaar_census: multi-leg schema + migration off the old single-leg PK ----
# Audit fix: bazaar_census used to be PRIMARY KEY(measured_date, resource) —
# accepts[0] only — so a resource with 2+ payment networks had legs 2..N
# permanently invisible to the catalog side of the payTo-mismatch feed. It's
# now PRIMARY KEY(measured_date, resource, accept_index).

def test_bazaar_census_multi_leg_same_date_resource_both_persist(tmp_path):
    """Two legs of the SAME resource on the SAME date (different network,
    different payTo) must both persist — this is the whole point of the
    fix. Exercises the new PK on a fresh SQLite table."""
    s = Store(tmp_path / "t.sqlite")
    n = s.record_bazaar_census_rows("2026-08-11", [
        {"resource": "https://api.multi.example/v1", "accept_index": 0,
         "network": "eip155:8453", "seller_wallet": "0x" + "aa" * 20},
        {"resource": "https://api.multi.example/v1", "accept_index": 1,
         "network": "eip155:137", "seller_wallet": "0x" + "bb" * 20},
    ])
    assert n == 2
    rows = s.db.execute(
        "SELECT accept_index, network, seller_wallet FROM bazaar_census "
        "WHERE measured_date='2026-08-11' AND resource='https://api.multi.example/v1' "
        "ORDER BY accept_index").fetchall()
    assert rows == [(0, "eip155:8453", "0x" + "aa" * 20),
                    (1, "eip155:137", "0x" + "bb" * 20)]
    s.close()


def test_bazaar_census_migrates_existing_old_schema_table(tmp_path):
    """The production shape this migration exists for: an EXISTING sqlite
    file created under the OLD PK(measured_date, resource) — pre-dating
    accept_index — must open cleanly (existing row preserved as leg 0) and
    then accept a second leg for the same date+resource without conflict."""
    import sqlite3

    path = tmp_path / "legacy.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("""CREATE TABLE bazaar_census (
        measured_date TEXT NOT NULL, resource TEXT NOT NULL, domain TEXT,
        network TEXT, asset TEXT, amount_raw TEXT, amount_usd REAL,
        seller_wallet TEXT, service TEXT,
        PRIMARY KEY (measured_date, resource))""")
    raw.execute(
        "INSERT INTO bazaar_census(measured_date,resource,domain,network,"
        "seller_wallet) VALUES(?,?,?,?,?)",
        ("2026-08-11", "https://api.legacy.example/v1", "legacy.example",
         "eip155:8453", "0x" + "11" * 20))
    raw.commit()
    raw.close()

    s = Store(path)   # open_store-equivalent: runs _init_schema -> migration
    assert "accept_index" in s._table_columns("bazaar_census")
    pre = s.db.execute(
        "SELECT accept_index, network, seller_wallet FROM bazaar_census "
        "WHERE measured_date='2026-08-11'").fetchall()
    assert pre == [(0, "eip155:8453", "0x" + "11" * 20)]   # old row = leg 0

    # write a second leg for that SAME date+resource, different network —
    # under the old PK this INSERT would collide; it must succeed now.
    n = s.record_bazaar_census_rows("2026-08-11", [
        {"resource": "https://api.legacy.example/v1", "accept_index": 1,
         "network": "eip155:137", "seller_wallet": "0x" + "22" * 20},
    ])
    assert n == 1
    rows = s.db.execute(
        "SELECT accept_index, network, seller_wallet FROM bazaar_census "
        "WHERE measured_date='2026-08-11' ORDER BY accept_index").fetchall()
    assert rows == [(0, "eip155:8453", "0x" + "11" * 20),
                    (1, "eip155:137", "0x" + "22" * 20)]
    s.close()

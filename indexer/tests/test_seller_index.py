"""Real Seller Index: per-chain gradient of seller counts at increasing
evidence thresholds (build_seller_index), plus its wiring into the nightly
dashboard blob (build_dashboard_metrics).

Two synthetic sellers pin the evidence bar itself:
  - seller A: 3 distinct EXTERNAL payers, $20 lifetime volume -> clears
    "active" (>=2 payers, >=$1) and "established" (>=3 payers, >=$5), but not
    "substantial" (needs >=5 payers).
  - seller B: self-heavy (2 self-pay rows) with exactly 1 external payer and
    $0.01 total volume -> clears only "any". A self-pay must never count
    toward the distinct-payers bar, or a wallet paying itself would pass as
    "real repeat demand".
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import daily_snapshot                                    # noqa: E402
from storage import Store                                # noqa: E402

INS = ("INSERT INTO settlements(tx_hash,log_index,chain,token,payer,seller,"
       "amount,block_number,block_timestamp) VALUES(?,?,?,?,?,?,?,?,?)")

SELLER_A = "0x" + "AA" * 20
SELLER_B = "0x" + "BB" * 20


def _seed(store: Store):
    now = int(time.time())
    blk = 1000
    rows = [
        # seller A: 3 distinct external payers, amounts summing to $20
        ("0xa1", 0, "base", "0xusdc", "0xp1", SELLER_A, 5_000_000, blk, now),
        ("0xa2", 0, "base", "0xusdc", "0xp2", SELLER_A, 5_000_000, blk + 1, now),
        ("0xa3", 0, "base", "0xusdc", "0xp3", SELLER_A, 10_000_000, blk + 2, now),
        # seller B: self-heavy — two self-pay rows (payer == seller, tiny) plus
        # exactly one external payer, totalling $0.01
        ("0xb1", 0, "base", "0xusdc", SELLER_B, SELLER_B, 1_000, blk + 3, now),
        ("0xb2", 0, "base", "0xusdc", SELLER_B, SELLER_B, 1_000, blk + 4, now),
        ("0xb3", 0, "base", "0xusdc", "0xp4", SELLER_B, 8_000, blk + 5, now),
    ]
    for r in rows:
        store.db.execute(INS, r)
    store.db.commit()


def test_seller_index_tiers(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    idx = daily_snapshot.build_seller_index(s)

    assert "method" in idx and "self-pay" in idx["method"]
    tiers = {t["tier"]: t for t in idx["tiers"]["base"]}
    assert set(tiers) == {"any", "active", "established", "substantial", "busy"}

    # any = both sellers, regardless of payers/volume
    assert tiers["any"]["sellers"] == 2
    assert tiers["any"]["volume_usd"] == 20.01

    # active (>=2 payers, >=$1): only seller A — B's single external payer
    # (and self-pay rows, which must NOT count as payers) keep it out
    assert tiers["active"]["sellers"] == 1
    assert tiers["active"]["volume_usd"] == 20.0

    # established (>=3 payers, >=$5): A qualifies exactly at the payer floor
    assert tiers["established"]["sellers"] == 1
    assert tiers["established"]["volume_usd"] == 20.0

    # substantial (>=5 payers): neither seller has 5 distinct external payers
    assert tiers["substantial"]["sellers"] == 0
    assert tiers["substantial"]["volume_usd"] == 0

    # busy (>=10 payers): same, nobody clears it
    assert tiers["busy"]["sellers"] == 0
    assert tiers["busy"]["volume_usd"] == 0

    s.close()


def test_seller_index_excludes_self_pay_from_payer_count(tmp_path):
    """A seller paid only by itself must never clear the 'active' bar (>=2
    external payers) no matter how many times it self-pays."""
    s = Store(tmp_path / "t.sqlite")
    now = int(time.time())
    for i in range(5):
        s.db.execute(INS, (f"0xs{i}", 0, "base", "0xusdc", SELLER_A, SELLER_A,
                           2_000_000, 2000 + i, now))
    s.db.commit()
    idx = daily_snapshot.build_seller_index(s)
    tiers = {t["tier"]: t for t in idx["tiers"]["base"]}
    assert tiers["any"]["sellers"] == 1          # the seller exists...
    assert tiers["active"]["sellers"] == 0        # ...but earns no external demand
    s.close()


def test_seller_index_queries_bind_no_parameters(tmp_path):
    """REGRESSION (2026-08-10): the tier query carries a literal '%' (the
    '%\\_permit2' exclusion). psycopg treats '%' as a format placeholder as
    soon as bound parameters are passed, so binding the chain raised on
    Postgres — and build_dashboard_metrics' try/except swallowed it, shipping
    a nightly blob with no seller_index and a silently missing dashboard
    section. SQLite ignores '%' entirely, so only this invariant catches it:
    these queries must execute WITHOUT bound parameters (chain inlined from
    the hardcoded tuple), exactly like every other permit2-filtered query in
    daily_snapshot.py."""
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    calls: list = []

    class _SpyDB:
        """sqlite3.Connection.execute is read-only, so proxy the whole
        connection rather than patching the method."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args):
            calls.append((sql, args))
            return self._real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real = s.db
    s.db = _SpyDB(real)
    try:
        daily_snapshot.build_seller_index(s)
    finally:
        s.db = real
    pct = [(sql, args) for sql, args in calls if "%" in sql]
    assert pct, "expected the permit2 '%' filter in the tier SQL"
    for sql, args in pct:
        assert not args, (
            "seller-index SQL containing a literal '%' was executed with bound "
            f"parameters {args!r} — this raises on Postgres. Inline the value "
            "or escape '%' as '%%'.")
    s.close()


def test_dashboard_metrics_carries_seller_index(tmp_path):
    """build_dashboard_metrics must fold seller_index into the published blob
    (best-effort — see the try/except around the call), so the dashboard has
    something to render once the nightly regenerates it."""
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    out = daily_snapshot.build_dashboard_metrics(s)
    assert "seller_index" in out
    tiers = {t["tier"]: t for t in out["seller_index"]["tiers"]["base"]}
    assert tiers["any"]["sellers"] == 2
    assert tiers["active"]["sellers"] == 1
    s.close()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

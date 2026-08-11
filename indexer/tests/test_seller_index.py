"""Real Seller Index: per-chain gradient of seller counts at increasing
evidence thresholds (build_seller_index), plus its wiring into the nightly
dashboard blob (build_dashboard_metrics).

SCOPE (restated 2026-08-11): the index is x402-SCOPED — only settlements whose
facilitator is in the registry relayer set count. The original superset scope
published 104,814 Base "sellers" under an x402 label when the scoped count was
89,337 (active tier 2,094 vs scoped 1,184) — the exact inflation merona exists
to correct. Tests pin the scope with an off-registry facilitator row that must
never count.

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

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import daily_snapshot                                    # noqa: E402
from storage import Store                                # noqa: E402

INS = ("INSERT INTO settlements(tx_hash,log_index,chain,token,payer,seller,"
       "amount,block_number,block_timestamp,facilitator) "
       "VALUES(?,?,?,?,?,?,?,?,?,?)")

SELLER_A = "0x" + "AA" * 20
SELLER_B = "0x" + "BB" * 20
RELAYER = "0x" + "fa" * 20          # the registry facilitator for these tests
OFF_REGISTRY = "0x" + "0d" * 20     # a facilitator NOT in the registry


def _registry(tmp_path, monkeypatch, chains=("base",)):
    """Point daily_snapshot.REGISTRY at a tmp registry whose relayer set is
    just RELAYER — the scope every seeded row must clear to count."""
    p = tmp_path / "relayer_registry.json"
    p.write_text(json.dumps(
        {"relayers_by_chain": {ch: [RELAYER] for ch in chains}}))
    monkeypatch.setattr(daily_snapshot, "REGISTRY", p)


def _seed(store: Store):
    now = int(time.time())
    blk = 1000
    rows = [
        # seller A: 3 distinct external payers, amounts summing to $20
        ("0xa1", 0, "base", "0xusdc", "0xp1", SELLER_A, 5_000_000, blk, now, RELAYER),
        ("0xa2", 0, "base", "0xusdc", "0xp2", SELLER_A, 5_000_000, blk + 1, now, RELAYER),
        ("0xa3", 0, "base", "0xusdc", "0xp3", SELLER_A, 10_000_000, blk + 2, now, RELAYER),
        # seller B: self-heavy — two self-pay rows (payer == seller, tiny) plus
        # exactly one external payer, totalling $0.01
        ("0xb1", 0, "base", "0xusdc", SELLER_B, SELLER_B, 1_000, blk + 3, now, RELAYER),
        ("0xb2", 0, "base", "0xusdc", SELLER_B, SELLER_B, 1_000, blk + 4, now, RELAYER),
        ("0xb3", 0, "base", "0xusdc", "0xp4", SELLER_B, 8_000, blk + 5, now, RELAYER),
        # SCOPE PIN: a third seller paid handsomely by many payers — but via a
        # facilitator that is NOT in the registry. If the index ever loses its
        # facilitator scoping (the 2026-08-11 superset bug), this seller leaks
        # into every tier and the counts below break.
        *[(f"0xc{i}", 0, "base", "0xusdc", f"0xq{i}", "0x" + "CC" * 20,
           20_000_000, blk + 10 + i, now, OFF_REGISTRY) for i in range(6)],
    ]
    for r in rows:
        store.db.execute(INS, r)
    store.db.commit()


def test_seller_index_tiers(tmp_path, monkeypatch):
    s = Store(tmp_path / "t.sqlite")
    _registry(tmp_path, monkeypatch)
    _seed(s)
    idx = daily_snapshot.build_seller_index(s)

    assert "method" in idx and "self-pay" in idx["method"]
    assert "x402-scoped" in idx["method"]
    # the OFF_REGISTRY seller (6 payers, $120) must be absent from every tier
    # below — the 'any' count staying at 2 IS the scope pin.
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


def test_seller_index_excludes_self_pay_from_payer_count(tmp_path, monkeypatch):
    """A seller paid only by itself must never clear the 'active' bar (>=2
    external payers) no matter how many times it self-pays."""
    s = Store(tmp_path / "t.sqlite")
    _registry(tmp_path, monkeypatch)
    now = int(time.time())
    for i in range(5):
        s.db.execute(INS, (f"0xs{i}", 0, "base", "0xusdc", SELLER_A, SELLER_A,
                           2_000_000, 2000 + i, now, RELAYER))
    s.db.commit()
    idx = daily_snapshot.build_seller_index(s)
    tiers = {t["tier"]: t for t in idx["tiers"]["base"]}
    assert tiers["any"]["sellers"] == 1          # the seller exists...
    assert tiers["active"]["sellers"] == 0        # ...but earns no external demand
    s.close()


def test_seller_index_queries_bind_no_parameters(tmp_path, monkeypatch):
    """REGRESSION (2026-08-10): the tier query carries a literal '%' (the
    '%\\_permit2' exclusion). psycopg treats '%' as a format placeholder as
    soon as bound parameters are passed, so binding the chain raised on
    Postgres — and build_dashboard_metrics' try/except swallowed it, shipping
    a nightly blob with no seller_index and a silently missing dashboard
    section. SQLite ignores '%' entirely, so only this invariant catches it:
    every tier query must execute WITHOUT bound parameters (chain + registry
    relayers inlined from our own constants/file). The permit2 '%' literal is
    gone from the scoped SQL, so the invariant is now the stronger, simpler
    form: NO seller-index query binds parameters at all."""
    s = Store(tmp_path / "t.sqlite")
    _registry(tmp_path, monkeypatch)
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
    tier_calls = [(sql, args) for sql, args in calls
                  if "FROM settlements" in sql and "payers" in sql]
    assert tier_calls, "expected at least one tier query against settlements"
    for sql, args in tier_calls:
        assert not args, (
            "seller-index SQL was executed with bound parameters "
            f"{args!r} — a literal '%' anywhere in this SQL would then raise "
            "on Postgres (2026-08-10 regression). Inline values or escape "
            "'%' as '%%'.")
    s.close()


def test_dashboard_metrics_carries_seller_index(tmp_path, monkeypatch):
    """build_dashboard_metrics must fold seller_index into the published blob
    (best-effort — see the try/except around the call), so the dashboard has
    something to render once the nightly regenerates it."""
    s = Store(tmp_path / "t.sqlite")
    _registry(tmp_path, monkeypatch)
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

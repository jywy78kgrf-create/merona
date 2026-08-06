"""A: x402-scoping the EVM EIP-3009 superset. The relayer (tx.from) enrichment
must only fill NULL relayers (never mutate settlement identity), and scoped_stats
must isolate the facilitator-relayed subset. Network is stubbed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import enrich_relayers as E  # noqa: E402
from storage import Store  # noqa: E402

REL = "0xfacilitator00000000000000000000000000abcd"   # a "registry" relayer
OTHER = "0xrandomsubmitter000000000000000000009999"    # non-x402 sender


def _seed(store):
    def row(tx, li, seller, amt, blk):
        return dict(tx_hash=tx, log_index=li, chain="base", token="0xusdc",
                    payer="0xp", seller=seller, amount=amt,
                    block_number=blk, block_timestamp=1730000000 + li)
    # 3 settlements across 2 txs in 2 blocks, relayer NULL (as the index writes
    # them): tx A emits 2 logs in block 100, tx B one log in block 101.
    store.commit_range("base", 100, 110, [row("0xtxA", 0, "0xs1", 1000, 100),
                                          row("0xtxA", 1, "0xs2", 2000, 100),
                                          row("0xtxB", 0, "0xs1", 5000, 101)], "t")


class FakeClient:
    # 0xtxA relayed by a registry facilitator; 0xtxB by a random sender.
    # Enrichment fetches by BLOCK: block 100 carries tx A, block 101 carries B.
    _blocks = {100: [{"hash": "0xtxA", "from": REL}],
               101: [{"hash": "0xtxB", "from": OTHER}]}

    def get_block_transactions(self, blk):
        return self._blocks.get(blk, [])


def test_enrich_only_fills_null_and_scoped_isolates(tmp_path, monkeypatch):
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    assert s.relayer_enriched_fraction("base")["fraction"] == 0.0
    monkeypatch.setattr(E, "_client", lambda name: FakeClient())

    resolved, updated = E.enrich_chain(s, "base", 100)
    assert resolved == 2 and updated == 3          # 2 txs -> 3 rows stamped
    assert s.relayer_enriched_fraction("base")["fraction"] == 1.0

    # scoped = only the facilitator-relayed tx (0xtxA -> 2 rows, $3000)
    sc = s.scoped_stats("base", [REL])
    assert sc["settlements"] == 2 and sc["volume_base_units"] == 3000
    # superset is untouched: still all 3 rows
    assert s.stats("base")["settlements"] == 3
    s.close()


def test_enrich_is_idempotent_and_ledger_safe(tmp_path, monkeypatch):
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    before = s.db.execute("SELECT tx_hash,log_index,seller,amount FROM "
                          "settlements ORDER BY log_index").fetchall()
    monkeypatch.setattr(E, "_client", lambda name: FakeClient())
    E.enrich_chain(s, "base", 100)
    r2, u2 = E.enrich_chain(s, "base", 100)     # second pass: nothing left
    assert r2 == 0 and u2 == 0
    # identity/amount/seller of every row unchanged by enrichment
    after = s.db.execute("SELECT tx_hash,log_index,seller,amount FROM "
                         "settlements ORDER BY log_index").fetchall()
    assert before == after
    s.close()


def test_scoped_none_when_no_registry(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    assert s.scoped_stats("arbitrum", []) is None      # no registry -> not scopable
    assert s.scoped_stats("arbitrum", None) is None
    s.close()

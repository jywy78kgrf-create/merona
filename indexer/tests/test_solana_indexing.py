"""Solana concurrent indexing: transactions are fetched in parallel but decoded
and committed in CHRONOLOGICAL order, and the cursor advances only past
fully-processed signatures. A not-yet-finalized tx must STOP progress (no skip)
so the next run resumes from exactly that point. Network is faked."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import index_solana as I  # noqa: E402
from storage import Store  # noqa: E402

REL = "SoLRelayerTest1111111111111111111111111111"


def _sig(name, slot, err=None):
    return {"signature": name, "slot": slot, "err": err}


class FakeSol:
    """Fixed chronological signature list + per-sig tx. A tx of None models a
    signature that isn't finalized/available yet."""

    def __init__(self, sigs, txs):
        self._sigs = sigs
        self._txs = txs
        self.fetched = []

    def signatures_since(self, address, until_sig, page=1000):
        names = [s["signature"] for s in self._sigs]
        if until_sig is None:
            return list(self._sigs)
        return list(self._sigs[names.index(until_sig) + 1:])

    def _call(self, method, params):
        return list(reversed(self._sigs))     # RPC returns newest-first

    def get_transaction(self, sig):
        self.fetched.append(sig)
        return self._txs.get(sig)


def _fake_decode(tx, sig):
    if not tx:
        return []
    return [dict(tx_hash=sig, log_index=j, chain="solana", token="usdc",
                 payer="p", seller="s%d" % j, amount=1,
                 block_number=tx["slot"], block_timestamp=0)
            for j in range(tx["n"])]


def test_all_good_sigs_indexed_and_cursor_at_tip(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "decode_solana_settlements", _fake_decode)
    monkeypatch.setattr(I, "SOLANA_CHUNK", 2)          # force multiple chunks
    s = Store(tmp_path / "t.sqlite")
    sigs = [_sig("a", 10), _sig("b", 11), _sig("bad", 12, err="X"), _sig("c", 13)]
    txs = {"a": {"n": 2, "slot": 10}, "b": {"n": 1, "slot": 11},
           "c": {"n": 3, "slot": 13}}                  # 'bad' errored -> no tx
    client = FakeSol(sigs, txs)
    n = I.index_relayer(client, s, REL)
    assert n == 6                                       # 2 + 1 + 3
    assert s.get_solana_cursor(REL)[0] == "c"           # advanced to tip
    assert "bad" not in client.fetched                  # errored sig never fetched
    assert s.stats("solana")["settlements"] == 6
    s.close()


def test_unavailable_tx_stops_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "decode_solana_settlements", _fake_decode)
    monkeypatch.setattr(I, "SOLANA_CHUNK", 10)
    s = Store(tmp_path / "t.sqlite")
    # 'b' not finalized yet -> must stop AT b; cursor stays at 'a', c NOT indexed
    sigs = [_sig("a", 10), _sig("b", 11), _sig("c", 12)]
    client = FakeSol(sigs, {"a": {"n": 1, "slot": 10}, "b": None,
                            "c": {"n": 5, "slot": 12}})
    n = I.index_relayer(client, s, REL)
    assert n == 1                                       # only 'a'
    assert s.get_solana_cursor(REL)[0] == "a"           # did NOT skip past b to c
    # next run: b now available -> resumes above 'a', indexes b and c, not a again
    client2 = FakeSol(sigs, {"a": {"n": 1, "slot": 10}, "b": {"n": 2, "slot": 11},
                             "c": {"n": 5, "slot": 12}})
    n2 = I.index_relayer(client2, s, REL)
    assert n2 == 7                                      # b(2) + c(5)
    assert s.get_solana_cursor(REL)[0] == "c"
    assert "a" not in client2.fetched                  # resumed above the cursor
    s.close()

"""Live settlement pulse: measured deltas, nightly anchor, honest failure.

The pulse lets the headline count build during the day, so its rules are the
index's credibility in miniature: count with the indexer's own predicate,
finalized blocks only, reset when the nightly lands, and when the RPC fails
leave the cursor alone so the window is re-counted rather than skipped.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import live_pulse                                        # noqa: E402
from chain import RpcError                               # noqa: E402
from decode import (AUTHORIZATION_USED_TOPIC,            # noqa: E402
                    TRANSFER_TOPIC)
from evm_chains import BASE                              # noqa: E402
from storage import Store                                # noqa: E402

RELAYER = "0x" + "aa" * 20
OUTSIDER = "0x" + "bb" * 20
PAYER = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
SELLER = "0x" + "33" * 20


def pad(addr):  # 20-byte address -> 32-byte topic
    return "0x" + "00" * 12 + addr[2:]


def tlog(tx, frm, to, block):
    return {"address": BASE.usdc, "transactionHash": tx,
            "topics": [TRANSFER_TOPIC, pad(frm), pad(to)],
            "data": "0x" + "01".zfill(64), "blockNumber": hex(block),
            "logIndex": "0x0", "blockTimestamp": hex(1700000000)}


def alog(tx, authorizer, block):
    return {"address": BASE.usdc, "transactionHash": tx,
            "topics": [AUTHORIZATION_USED_TOPIC, pad(authorizer), pad(SELLER)],
            "blockNumber": hex(block), "logIndex": "0x1"}


class FakeClient:
    def __init__(self, head, logs, tx_from, fail=False):
        self.head, self.logs, self.tx_from, self.fail = head, logs, tx_from, fail

    def block_number(self): return self.head
    def finalized_block(self): return self.head

    def get_logs_chunked(self, *, address, topics, from_block, to_block):
        if self.fail:
            raise RpcError("boom")
        for lg in self.logs:
            b = int(lg["blockNumber"], 16)
            if (from_block <= b <= to_block
                    and lg["topics"][0].lower() == topics[0].lower()):
                yield lg

    def get_transaction(self, tx):
        return {"from": self.tx_from.get(tx, OUTSIDER)}


def _publish_baseline(s, head):
    """The anchor is the PUBLISHED dashboard baseline's block edge — the metrics
    blob the nightly writes LAST (meta[DASHBOARD_METRICS_KEY]). Publishing a new
    blob is the ONLY thing that may move the anchor."""
    s.set_meta(live_pulse.DASHBOARD_METRICS_KEY,
               {"chains": {"base": {"max_block": head, "settlements": 1}}})


def setup(tmp_path, monkeypatch, anchor=100):
    root = tmp_path / "root"
    (root / "data" / "indexer").mkdir(parents=True)
    (root / "data" / "indexer" / "relayer_registry.json").write_text(
        json.dumps({"relayers_by_chain": {"base": [RELAYER]}}))
    monkeypatch.setattr(live_pulse, "ROOT", root)
    s = Store(tmp_path / "t.sqlite")
    _publish_baseline(s, anchor)
    # ranges also exist in the store; committing them must NOT move the live
    # anchor (the original bug anchored on indexed_ranges MAX).
    s.commit_range("base", 0, anchor, [], "t")
    s.db.commit()
    return s


def test_counts_with_the_indexers_predicate(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    logs = [
        alog("0xa1", PAYER, 105),
        tlog("0xa1", PAYER, SELLER, 105),   # authorized: counts
        tlog("0xa1", PAYER, SELLER, 105),   # second transfer, same tx: counts
        tlog("0xa1", OTHER, SELLER, 105),   # payer != authorizer: no
        tlog("0xb2", OTHER, SELLER, 106),   # no authorization at all: no
        alog("0xc3", PAYER, 107),
        tlog("0xc3", PAYER, SELLER, 107),   # authorized but tx.from unknown
    ]
    cl = FakeClient(110, logs, {"0xa1": RELAYER, "0xc3": OUTSIDER})
    state = live_pulse.tick(s, {}, mk_client=lambda ch: cl)
    b = state["base"]
    assert b["superset_delta"] == 3         # all authorized transfers
    assert b["scoped_delta"] == 2           # only the relayer-submitted tx
    assert b["cursor"] == 110 and b["behind"] == 0 and b["anchor"] == 100


def test_deltas_accumulate_and_nightly_reset(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    logs = [alog("0xa1", PAYER, 105), tlog("0xa1", PAYER, SELLER, 105)]
    cl = FakeClient(110, logs, {"0xa1": RELAYER})
    state = live_pulse.tick(s, {}, mk_client=lambda ch: cl)
    assert state["base"]["scoped_delta"] == 1
    # next tick, new settlement in a later block
    cl.logs += [alog("0xd4", PAYER, 112), tlog("0xd4", PAYER, SELLER, 112)]
    cl.tx_from["0xd4"] = RELAYER
    cl.head = 115
    state = live_pulse.tick(s, state, mk_client=lambda ch: cl)
    assert state["base"]["scoped_delta"] == 2 and state["base"]["cursor"] == 115
    # REGRESSION (caught live, 12,929,862 -> 12,779,835): the nightly INDEXES
    # hours before it PUBLISHES the metrics blob. Indexing progress — committed
    # ranges, inserted settlement rows — must NOT move the anchor; only the
    # published baseline may, since the headline is baseline + delta.
    s.commit_range("base", 101, 130, [
        {"tx_hash": "0xe5", "log_index": 0, "chain": "base", "token": BASE.usdc,
         "payer": PAYER, "seller": SELLER, "amount": "1",
         "block_number": 125, "block_timestamp": 1700000000}], "t")
    s.db.commit()
    cl.head = 118
    state = live_pulse.tick(s, state, mk_client=lambda ch: cl)
    assert state["base"]["anchor"] == 100          # indexing didn't move it
    assert state["base"]["scoped_delta"] == 2      # delta held -> no drop
    # the nightly PUBLISH lands (new metrics blob): anchor moves WITH the
    # baseline it accompanies, and the live delta restarts at zero.
    _publish_baseline(s, 120)
    s.db.commit()
    cl.head = 121
    state = live_pulse.tick(s, state, mk_client=lambda ch: cl)
    assert state["base"]["anchor"] == 120
    assert state["base"]["scoped_delta"] == 0 and state["base"]["cursor"] == 121


def test_rpc_failure_leaves_the_window_uncounted_not_skipped(tmp_path,
                                                             monkeypatch):
    s = setup(tmp_path, monkeypatch)
    good_logs = [alog("0xa1", PAYER, 105), tlog("0xa1", PAYER, SELLER, 105)]
    cl = FakeClient(110, good_logs, {"0xa1": RELAYER}, fail=True)
    state = live_pulse.tick(s, {}, mk_client=lambda ch: cl)
    assert state["base"]["cursor"] == 100      # untouched
    assert "error" in state["base"]
    cl.fail = False                             # RPC recovers
    state = live_pulse.tick(s, state, mk_client=lambda ch: cl)
    assert state["base"]["scoped_delta"] == 1  # the window was re-counted
    assert "error" not in state["base"]


def test_catchup_is_chunked(tmp_path, monkeypatch):
    s = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(live_pulse, "MAX_BLOCKS_PER_TICK", 5)
    cl = FakeClient(200, [], {})
    state = live_pulse.tick(s, {}, mk_client=lambda ch: cl)
    assert state["base"]["cursor"] == 105      # 101..105 only
    assert state["base"]["behind"] == 95       # visible, not silent


def _put_settlement(s, block, facilitator, i):
    ins = s._settlement_insert_sql(with_facilitator=True)
    s.db.execute(ins, (f"0x{block}{i}", i, "base", BASE.usdc, PAYER, SELLER,
                       "1", block, 1700000000, facilitator))


def test_count_gap_measures_the_pending_publish_window(tmp_path, monkeypatch):
    """The proof-of-disparity: scoped settlements already in the store above the
    PUBLISHED baseline's block — counted with the tally's own predicate. This is
    what the headline was dropping, shown to still exist."""
    s = setup(tmp_path, monkeypatch, anchor=100)   # published baseline @ 100
    # the indexer has since recorded settlements past the published edge:
    _put_settlement(s, 105, RELAYER, 1)            # scoped (registered relayer)
    _put_settlement(s, 110, RELAYER, 2)            # scoped
    _put_settlement(s, 120, OUTSIDER, 3)           # NOT scoped (unknown relayer)
    _put_settlement(s, 90, RELAYER, 4)             # already published: excluded
    s.db.commit()
    g = live_pulse.count_gap(s, "base", [RELAYER])
    assert g["published_block"] == 100 and g["store_head"] == 120
    assert g["gap_blocks"] == 20
    assert g["scoped_in_gap"] == 2                  # the two unpublished scoped


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

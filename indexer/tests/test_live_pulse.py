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
    def __init__(self, head, logs, tx_from, fail=False, max_window=None):
        self.head, self.logs, self.tx_from, self.fail = head, logs, tx_from, fail
        # max_window: reproduces the real "Response is too big" RPC error —
        # any get_logs_chunked call spanning more than this many blocks fails,
        # regardless of `fail`. Used to prove the cap actually keeps the
        # requested range small enough for the RPC to answer.
        self.max_window = max_window
        self.calls = []   # records (from_block, to_block) of every call

    def block_number(self): return self.head
    def finalized_block(self): return self.head

    def get_logs_chunked(self, *, address, topics, from_block, to_block):
        self.calls.append((from_block, to_block))
        if self.fail:
            raise RpcError("boom")
        if self.max_window is not None and (to_block - from_block + 1) > self.max_window:
            raise RpcError("Response is too big")
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
    # the state file is served verbatim at merona.io/live.json — the raw
    # exception ("RpcError: boom", RPC method names, provider messages) must
    # never reach it; the public marker is generic (2026-08-12 audit)
    assert "boom" not in state["base"]["error"]
    assert "RpcError" not in state["base"]["error"]
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


def test_stale_baseline_caps_the_window_and_still_produces_a_number(
        tmp_path, monkeypatch):
    """The Base incident, reproduced: the published anchor is frozen far
    below the store head. count_window over the full [anchor, head] range
    would ask the RPC for every USDC transfer across ~900 blocks and (per
    max_window) get "Response is too big" back — exactly what happened live
    on Base over its real ~71k-block gap. With the cap in place, tick()
    counts only the most recent PULSE_MAX_WINDOW_BLOCKS blocks instead, so
    the RPC call stays inside a range it can answer and the chain produces a
    live number rather than erroring forever."""
    s = setup(tmp_path, monkeypatch, anchor=100)
    monkeypatch.setattr(live_pulse, "PULSE_MAX_WINDOW_BLOCKS", 50)
    logs = [
        alog("0xold", PAYER, 105),                  # inside the full gap,
        tlog("0xold", PAYER, SELLER, 105),           # OUTSIDE the capped
                                                      # window -> must NOT count
        alog("0xnew", PAYER, 960),                   # inside [951, 1000]:
        tlog("0xnew", PAYER, SELLER, 960),           # the capped window
    ]
    cl = FakeClient(1000, logs, {"0xold": RELAYER, "0xnew": RELAYER},
                    max_window=50)   # any call over 50 blocks -> RpcError
    state = live_pulse.tick(s, {}, mk_client=lambda ch: cl)
    b = state["base"]
    assert "error" not in b                    # no "rpc window failed"
    assert b["scoped_delta"] == 1               # only the recent-window tx
    assert b["superset_delta"] == 1
    assert b["window_capped"] is True
    assert b["window_blocks"] == 50
    assert b["baseline_stale"] is True
    assert b["cursor"] == 1000 and b["behind"] == 0 and b["anchor"] == 100
    # every RPC call was bounded to the trailing window, never [anchor, head]
    for (lo, hi) in cl.calls:
        assert lo == 951 and hi == 1000
        assert hi - lo + 1 == 50


def test_window_within_cap_is_byte_for_byte_unchanged(tmp_path, monkeypatch):
    """When the gap from anchor to head is within PULSE_MAX_WINDOW_BLOCKS,
    behavior (and state shape) must be identical to before the cap existed:
    the full [anchor, head] window, a cumulative delta, and no capped
    markers."""
    s = setup(tmp_path, monkeypatch, anchor=100)
    monkeypatch.setattr(live_pulse, "PULSE_MAX_WINDOW_BLOCKS", 5000)
    logs = [alog("0xa1", PAYER, 105), tlog("0xa1", PAYER, SELLER, 105)]
    cl = FakeClient(110, logs, {"0xa1": RELAYER})
    state = live_pulse.tick(s, {}, mk_client=lambda ch: cl)
    b = state["base"]
    assert b["scoped_delta"] == 1 and b["cursor"] == 110 and b["behind"] == 0
    assert "window_capped" not in b
    assert "window_blocks" not in b
    assert "baseline_stale" not in b
    assert cl.calls and cl.calls[0] == (101, 110)   # full since-anchor range


def test_capped_state_is_what_the_server_serves_honestly(tmp_path, monkeypatch):
    """serving/stats_server.py's /live.json handler serves live_pulse.json
    verbatim (no reshaping) — so the JSON round-tripped through
    _write_state/_load_state IS the shape the dashboard reads. Confirm the
    capped chain's serialized state carries the fields the dashboard needs to
    avoid presenting a partial recent count as a complete since-baseline
    total: window_capped (gate), scoped_delta (the bounded, honest number),
    and window_blocks (how much of a window it actually covers)."""
    s = setup(tmp_path, monkeypatch, anchor=100)
    monkeypatch.setattr(live_pulse, "PULSE_MAX_WINDOW_BLOCKS", 50)
    logs = [alog("0xnew", PAYER, 960), tlog("0xnew", PAYER, SELLER, 960)]
    cl = FakeClient(1000, logs, {"0xnew": RELAYER}, max_window=50)
    state_path = tmp_path / "live_pulse.json"
    monkeypatch.setattr(live_pulse, "STATE_PATH", state_path)
    state = live_pulse.tick(s, live_pulse._load_state(), mk_client=lambda ch: cl)
    live_pulse._write_state(state)
    served = json.loads(state_path.read_text())   # exactly what /live.json returns
    b = served["base"]
    assert b["window_capped"] is True
    assert isinstance(b["scoped_delta"], int) and b["scoped_delta"] == 1
    assert b["window_blocks"] == 50
    # dashboard/index.html's pollLive() reads scoped_delta + window_capped
    # from this exact shape and, when window_capped, holds the headline at
    # the verified nightly baseline instead of adding a partial number to
    # it — never inventing an overstated total.


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


def test_tx_budget_paces_the_cursor_without_going_amber(tmp_path, monkeypatch):
    """The wall-clock cost of a tick is one get_transaction per candidate tx,
    budgeted at PULSE_TX_LOOKUP_CAP. REGRESSION (the 2026-08-13 flap): the
    old budget handling kept the most RECENT cap-many txs, marked the tick
    window_capped (-> amber "Catching up" on the dashboard), and CLOBBERED
    the accumulated day delta. Base runs ~4-5 candidate txs/block, so the
    routine 900-block catch-up windows after every nightly publish sat
    right at the 4k budget — both chains flipped amber for ~2h after every
    publish, and any mid-day superset burst (Polygon mint spam, nothing to
    do with x402) re-flipped them and reset the count again.

    A budget overrun is pacing, not partial data. Now: classify the OLDEST
    txs up to a whole-block cutoff, advance the cursor only that far,
    accumulate normally, stay green — `behind` carries the remaining work
    to the next tick, and nothing is skipped or clobbered."""
    s = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(live_pulse, "PULSE_TX_LOOKUP_CAP", 2)
    logs, tx_from = [], {}
    for i, blk in enumerate((104, 105, 106)):     # 3 txs > budget of 2
        tx = f"0xt{i}"
        logs += [alog(tx, PAYER, blk), tlog(tx, PAYER, SELLER, blk)]
        tx_from[tx] = RELAYER
    cl = FakeClient(110, logs, tx_from)
    state = live_pulse.tick(s, {}, mk_client=lambda ch: cl)
    b = state["base"]
    assert "window_capped" not in b               # budget != incident: green
    assert "baseline_stale" not in b
    assert b["scoped_delta"] == 2                 # oldest 2 (blocks 104, 105)
    assert b["cursor"] == 105                     # only fully-counted blocks
    assert b["behind"] == 5                       # remaining work is visible
    assert "error" not in b
    # next tick picks up exactly where the budget stopped: nothing skipped,
    # nothing double-counted, and the delta ACCUMULATES instead of resetting
    state = live_pulse.tick(s, state, mk_client=lambda ch: cl)
    b = state["base"]
    assert b["scoped_delta"] == 3                 # 2 + the deferred block-106 tx
    assert b["cursor"] == 110 and b["behind"] == 0
    assert "window_capped" not in b


def test_tx_budget_cutoff_never_half_counts_a_block(tmp_path, monkeypatch):
    """The prefix cutoff is a WHOLE-BLOCK boundary: when the budget lands
    mid-block, every tx of the cutoff block is still classified (soft
    budget) — otherwise the cursor would advance past a block whose txs
    were only partially counted and the remainder would be lost forever."""
    s = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(live_pulse, "PULSE_TX_LOOKUP_CAP", 2)
    logs, tx_from = [], {}
    # 3 txs all in block 105: budget of 2 lands mid-block
    for i in range(3):
        tx = f"0xs{i}"
        logs += [alog(tx, PAYER, 105), tlog(tx, PAYER, SELLER, 105)]
        tx_from[tx] = RELAYER
    logs += [alog("0xlater", PAYER, 108), tlog("0xlater", PAYER, SELLER, 108)]
    tx_from["0xlater"] = RELAYER
    cl = FakeClient(110, logs, tx_from)
    state = live_pulse.tick(s, {}, mk_client=lambda ch: cl)
    b = state["base"]
    assert b["scoped_delta"] == 3                 # ALL of block 105, soft budget
    assert b["cursor"] == 105                     # 0xlater deferred, not lost
    assert "window_capped" not in b
    state = live_pulse.tick(s, state, mk_client=lambda ch: cl)
    assert state["base"]["scoped_delta"] == 4     # 0xlater arrives next tick
    assert state["base"]["cursor"] == 110


def test_stale_baseline_trim_still_keeps_the_recent_slice(tmp_path, monkeypatch):
    """Inside the stale-baseline (window_capped) branch the numbers are
    already an explicit bounded recent-activity window, so a tx-budget
    overrun there keeps the old most-RECENT-slice semantics — the state
    stays amber and the delta stays a recent-window count."""
    s = setup(tmp_path, monkeypatch, anchor=100)
    monkeypatch.setattr(live_pulse, "PULSE_MAX_WINDOW_BLOCKS", 50)
    monkeypatch.setattr(live_pulse, "PULSE_TX_LOOKUP_CAP", 1)
    logs = [alog("0xn1", PAYER, 955), tlog("0xn1", PAYER, SELLER, 955),
            alog("0xn2", PAYER, 990), tlog("0xn2", PAYER, SELLER, 990)]
    cl = FakeClient(1000, logs, {"0xn1": RELAYER, "0xn2": RELAYER},
                    max_window=50)
    state = live_pulse.tick(s, {}, mk_client=lambda ch: cl)
    b = state["base"]
    assert b["window_capped"] is True             # still the incident state
    assert b["scoped_delta"] == 1                 # the most recent tx (990)
    assert b["cursor"] == 1000

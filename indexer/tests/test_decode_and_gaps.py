"""Unit tests for the correctness-critical pure logic: log decoding and gap
detection. Run: python -m pytest indexer/tests/  (or python tests/...).

These guard the two failure modes that would silently corrupt months of data:
misdecoded amounts/addresses, and a resume that steps over an unindexed hole.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decode import (TRANSFER_TOPIC, decode_transfer, parse_uint,  # noqa: E402
                    topic_to_address)
from storage import Store  # noqa: E402


def test_topic_to_address_strips_padding():
    t = "0x000000000000000000000000dbdf3d8ed80f84c35d01c6c9f9271761bad90ba6"  # padded test address, not-a-key
    assert topic_to_address(t) == "0xdbdf3d8ed80f84c35d01c6c9f9271761bad90ba6"


def test_topic_to_address_rejects_bad_length():
    try:
        topic_to_address("0x1234")
        assert False, "should raise"
    except ValueError:
        pass


def test_parse_uint():
    assert parse_uint("0x") == 0
    assert parse_uint("0x0f4240") == 1_000_000          # 1.0 USDC (6dp)
    assert parse_uint("0x" + "0" * 63 + "1") == 1


def test_decode_transfer_happy_path():
    log = {
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "topics": [
            TRANSFER_TOPIC,
            "0x000000000000000000000000" + "aa" * 20,   # payer
            "0x000000000000000000000000" + "bb" * 20,   # seller
        ],
        "data": "0x00000000000000000000000000000000000000000000000000000000000f4240",  # abi uint 1e6, not-a-key
        "blockNumber": "0x2dedb6c",
        "blockTimestamp": "0x6a4813bb",
        "transactionHash": "0xDEADBEEF",
        "logIndex": "0xb",
    }
    r = decode_transfer(log)
    assert r["payer"] == "0x" + "aa" * 20
    assert r["seller"] == "0x" + "bb" * 20
    assert r["amount"] == 1_000_000
    assert r["block_number"] == 0x2dedb6c
    assert r["block_timestamp"] == 0x6a4813bb
    assert r["tx_hash"] == "0xdeadbeef"        # lowercased
    assert r["log_index"] == 11


def test_decode_transfer_rejects_nonstandard_topics():
    # a Transfer-topic log with only 1 topic (non-standard) must raise, not
    # silently produce a garbage row
    log = {"address": "0x833589fc", "topics": [TRANSFER_TOPIC],
           "data": "0x", "blockNumber": "0x1", "transactionHash": "0x1",
           "logIndex": "0x0"}
    try:
        decode_transfer(log)
        assert False, "should raise on wrong topic count"
    except ValueError:
        pass


def _mkstore():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    return Store(Path(tmp.name))


def test_idempotent_insert():
    s = _mkstore()
    row = {"tx_hash": "0xa", "log_index": 0, "chain": "base", "token": "0xu",
           "payer": "0x1", "seller": "0x2", "amount": 5,
           "block_number": 100, "block_timestamp": 1}
    s.commit_range("base", 100, 100, [row], "t")
    s.commit_range("base", 100, 100, [row], "t")   # re-process same block
    assert s.stats("base")["settlements"] == 1     # not 2


def test_frontier_stops_at_gap():
    s = _mkstore()
    s.commit_range("base", 10, 19, [], "t")
    s.commit_range("base", 20, 29, [], "t")
    # a hole at 30-39, then 40-49
    s.commit_range("base", 40, 49, [], "t")
    assert s.covered_frontier("base", 10) == 29     # must NOT jump to 49


def test_find_gaps():
    s = _mkstore()
    s.commit_range("base", 10, 19, [], "t")
    s.commit_range("base", 40, 49, [], "t")
    gaps = s.find_gaps("base", 10, 49)
    assert gaps == [(20, 39)]
    # fully covered -> no gaps
    s.commit_range("base", 20, 39, [], "t")
    assert s.find_gaps("base", 10, 49) == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nall {len(fns)} tests passed")

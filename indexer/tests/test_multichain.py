"""Multi-chain EVM config + decode labeling.

Guards the Polygon (and future EVM) add against silently regressing Base: the
decoder must still default to base, must label rows by the chain it's told, and
the per-chain start-block meta key must keep Base on its legacy unscoped key so
existing Base DBs resume unchanged.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decode import (AUTHORIZATION_USED_TOPIC, TRANSFER_TOPIC,  # noqa: E402
                    decode_transfer)
from evm_chains import BASE, CHAINS, POLYGON, start_meta_key  # noqa: E402


def _transfer_log(addr: str) -> dict:
    return {
        "topics": [
            TRANSFER_TOPIC,
            "0x000000000000000000000000dbdf3d8ed80f84c35d01c6c9f9271761bad90ba6",
            "0x0000000000000000000000005a52e96bacdabb82fd05763e25335261b270efcb",
        ],
        "data": "0x" + "64".rjust(64, "0"),  # value = 0x64
        "address": addr,
        "blockNumber": "0x1",
        "blockTimestamp": "0x64",
        "transactionHash": "0xabc",
        "logIndex": "0x0",
    }


def test_decode_defaults_to_base():
    assert decode_transfer(_transfer_log(BASE.usdc))["chain"] == "base"


def test_decode_labels_by_chain():
    rec = decode_transfer(_transfer_log(POLYGON.usdc), "polygon")
    assert rec["chain"] == "polygon"
    assert rec["token"] == POLYGON.usdc  # contract lowercased into the row


def test_polygon_config_sane():
    assert POLYGON.chain_id == 137
    assert POLYGON.usdc == "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
    assert POLYGON.usdc == POLYGON.usdc.lower()
    assert {"base", "polygon"} <= set(CHAINS)


def test_start_meta_key_scoping():
    # base keeps the legacy unscoped key (existing DBs resume); others scoped
    assert start_meta_key(BASE) == "start_block"
    assert start_meta_key(POLYGON) == "start_block_polygon"


def test_timestamp_enrichment_fills_missing(tmp_path):
    """Chains whose RPC omits blockTimestamp on logs (e.g. Avalanche) must still
    get real timestamps via getBlockByNumber, not fail or write nulls."""
    import index_base
    from storage import Store

    payer_topic = ("0x000000000000000000000000"
                   "dbdf3d8ed80f84c35d01c6c9f9271761bad90ba6")
    seller_topic = ("0x0000000000000000000000005a52e96bacdabb82fd05763e25335261b270efcb")
    transfer = {  # note: NO blockTimestamp key (RPC omitted it)
        "topics": [TRANSFER_TOPIC, payer_topic, seller_topic],
        "data": "0x" + "64".rjust(64, "0"),
        "address": BASE.usdc, "blockNumber": "0x10",
        "transactionHash": "0xaa", "logIndex": "0x0",
    }
    auth = {"transactionHash": "0xaa",
            "topics": [AUTHORIZATION_USED_TOPIC, payer_topic]}

    class FakeClient:
        def get_logs_chunked(self, address, topics, from_block, to_block):
            return [auth] if topics == [AUTHORIZATION_USED_TOPIC] else [transfer]

        def get_block_timestamp(self, bn):
            return 1730000000

    store = Store(tmp_path / "t.sqlite")
    n = index_base.index_subrange(FakeClient(), store, 16, 16, BASE)
    assert n == 1
    ts = store.db.execute("SELECT block_timestamp FROM settlements").fetchone()[0]
    assert ts == 1730000000   # enriched from getBlockByNumber, not null
    store.close()


def test_receipt_mode_extracts_settlement(tmp_path):
    """use_receipts chains pull the USDC Transfer from the x402 tx's receipt
    (never scanning all transfers) and enrich the timestamp receipts lack."""
    import dataclasses

    import index_base
    from evm_chains import BASE
    from storage import Store

    payer_topic = ("0x000000000000000000000000"
                   "dbdf3d8ed80f84c35d01c6c9f9271761bad90ba6")
    seller_topic = ("0x0000000000000000000000005a52e96bacdabb82fd05763e25335261b270efcb")
    auth = {"transactionHash": "0xaa",
            "topics": [AUTHORIZATION_USED_TOPIC, payer_topic]}
    receipt = {"logs": [
        {"address": "0xdeadbeef", "topics": ["0xother"], "data": "0x"},  # ignored
        {"address": BASE.usdc, "topics": [TRANSFER_TOPIC, payer_topic, seller_topic],
         "data": "0x" + "64".rjust(64, "0"), "blockNumber": "0x10",
         "transactionHash": "0xaa", "logIndex": "0x2"},  # the settlement
    ]}

    class FakeReceiptClient:
        def get_logs_chunked(self, address, topics, from_block, to_block):
            assert topics == [AUTHORIZATION_USED_TOPIC]  # never scans transfers
            return [auth]

        def get_transaction_receipt(self, tx):
            return receipt if tx == "0xaa" else None

        def get_block_timestamp(self, bn):
            return 1730000000

    chain = dataclasses.replace(BASE, name="base", use_receipts=True)
    store = Store(tmp_path / "r.sqlite")
    n = index_base.index_subrange(FakeReceiptClient(), store, 16, 16, chain)
    assert n == 1
    row = store.db.execute(
        "SELECT seller, amount, block_timestamp, log_index FROM settlements"
    ).fetchone()
    assert row[1] == 0x64 and row[2] == 1730000000 and row[3] == 2
    store.close()


def test_permit2_extracts_non_usdc_settlement(tmp_path):
    """Permit2 path: a Settled marker on the proxy -> the tx's ERC-20 Transfer is
    the settlement, in a NON-USDC token, labeled '<chain>_permit2'."""
    import index_permit2
    from evm_chains import BASE
    from storage import Store

    payer_topic = ("0x000000000000000000000000"
                   "dbdf3d8ed80f84c35d01c6c9f9271761bad90ba6")
    seller_topic = ("0x0000000000000000000000005a52e96bacdabb82fd05763e25335261b270efcb")
    token = "0x1fec9700000000000000000000000000000000ab"  # arbitrary non-USDC
    settled = {"transactionHash": "0xbb", "topics": [index_permit2.SETTLED_TOPIC]}
    receipt = {"logs": [
        {"address": token,
         "topics": [TRANSFER_TOPIC, payer_topic, seller_topic],
         "data": "0x" + "3e8".rjust(64, "0"), "blockNumber": "0x20",
         "transactionHash": "0xbb", "logIndex": "0x1"},
    ]}

    class FakeClient:
        def get_logs_chunked(self, address, topics, from_block, to_block):
            assert address == index_permit2.PERMIT2_PROXY  # never scans a token
            return [settled]

        def get_transaction_receipt(self, tx):
            return receipt if tx == "0xbb" else None

        def get_block_timestamp(self, bn):
            return 1730000000

    store = Store(tmp_path / "p.sqlite")
    n = index_permit2.index_subrange_permit2(FakeClient(), store, 32, 32, BASE)
    assert n == 1
    row = store.db.execute(
        "SELECT chain, token, amount, block_timestamp FROM settlements").fetchone()
    assert row[0] == "base_permit2"     # separate coverage label
    assert row[1] == token              # non-USDC token recorded
    assert row[2] == 0x3e8 and row[3] == 1730000000
    store.close()

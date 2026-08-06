"""Pure decoders for x402 settlement signals. No I/O — unit-testable.

An x402 settlement on Base is a gasless USDC transfer executed via EIP-3009
(transferWithAuthorization), submitted by a facilitator relayer. Two USDC log
events describe it in the same tx:

  Transfer(from indexed, to indexed, value)         -> who paid whom, how much
  AuthorizationUsed(authorizer indexed, nonce indexed) -> marks it EIP-3009

We key settlements off the ERC-20 Transfer event (it carries payer/seller/
amount) and use tx.from ∈ relayer-set to confirm it's facilitator-relayed. The
AuthorizationUsed event is retained for auditing but the Transfer is the record.
"""

from __future__ import annotations

TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # ERC-20 Transfer topic (public constant, not-a-key)
)
AUTHORIZATION_USED_TOPIC = (
    "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"  # EIP-3009 AuthorizationUsed topic (public constant, not-a-key)
)
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def topic_to_address(topic: str) -> str:
    """A 32-byte log topic holding an address -> lowercase 0x-address.

    Addresses are right-aligned in the 32-byte word: the address is the last 20
    bytes (40 hex chars). Guards against malformed input length.
    """
    h = topic[2:] if topic.startswith("0x") else topic
    if len(h) != 64:
        raise ValueError(f"topic not 32 bytes: {topic!r}")
    return "0x" + h[24:].lower()


def parse_uint(data: str) -> int:
    """ABI-encoded uint256 (the Transfer `value`) -> int. Empty data -> 0."""
    h = data[2:] if data.startswith("0x") else data
    if h == "":
        return 0
    return int(h, 16)


def decode_transfer(log: dict, chain: str = "base") -> dict:
    """Decode a USDC Transfer log into a settlement-candidate record.

    Returns a dict with sender(payer), recipient(seller), amount (base units,
    6dp for USDC), plus block/tx coordinates. Raises on a non-Transfer log so a
    topic-filter mistake can't silently produce garbage rows.

    `chain` labels the row (the Transfer/EIP-3009 topics are identical on every
    EVM chain, so the same decoder serves Base, Polygon, …). Defaults to "base"
    for backward compatibility.
    """
    topics = log["topics"]
    if not topics or topics[0].lower() != TRANSFER_TOPIC:
        raise ValueError("not a Transfer log")
    if len(topics) != 3:
        # canonical ERC-20 Transfer has exactly from+to indexed; anything else
        # is a non-standard event we must not misread as a payment
        raise ValueError(f"Transfer expects 3 topics, got {len(topics)}")
    return {
        "chain": chain,
        "token": log["address"].lower(),
        "payer": topic_to_address(topics[1]),
        "seller": topic_to_address(topics[2]),
        "amount": parse_uint(log["data"]),
        "block_number": int(log["blockNumber"], 16),
        "block_timestamp": (
            int(log["blockTimestamp"], 16) if log.get("blockTimestamp") else None
        ),
        "tx_hash": log["transactionHash"].lower(),
        "log_index": int(log["logIndex"], 16),
    }


def is_facilitator_settlement(tx_from: str, relayer_set: set[str]) -> bool:
    """True iff the tx was submitted by a known facilitator relayer."""
    return (tx_from or "").lower() in relayer_set

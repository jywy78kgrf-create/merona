"""Index x402 Permit2 (non-USDC ERC-20) settlements.

The EIP-3009 path (index_base) captures USDC/EURC. x402's "exact" scheme also
settles ANY other ERC-20 via Permit2, through the canonical on-chain contract
`x402ExactPermit2Proxy` (same CREATE2 address on every EVM chain). That proxy
emits a **parameterless** `Settled()` / `SettledWithPermit()` marker; the actual
money movement is an ordinary ERC-20 `Transfer` in the same tx. Verified on Base
2026-07-04: every sampled Settled tx carries exactly one ERC-20 Transfer (the
settlement), in non-USDC tokens.

So the recipe mirrors the EIP-3009 receipt path, with a different marker:
  1. getLogs on the proxy for Settled/SettledWithPermit  -> the x402 permit2 txs
  2. fetch each tx's receipt, take its ERC-20 Transfer(s) -> the settlement rows
  3. enrich timestamps (receipts carry none)

Coverage is tracked under the chain label `<chain>_permit2` so it never collides
with the EIP-3009 index for the same chain. Rows carry the real (non-USDC) token
in `settlements.token`. Same idempotency + gap-ledger guarantees as everything
else: (tx_hash, log_index) PK, ranges committed only when their rows commit.

Addresses/topics are on-chain constants (verified via the contract's Blockscout
ABI + keccak of the event signatures), not keys.
"""

from __future__ import annotations

from datetime import datetime, timezone

from chain import ChainClient
from decode import TRANSFER_TOPIC, decode_transfer
from storage import Store

# x402ExactPermit2Proxy — canonical CREATE2 address, identical bytecode on
# Base / Polygon / Arbitrum (and other EVM chains).
PERMIT2_PROXY = "0x402085c248eea27d92e8b30b2c58ed07f9e20001"
# keccak256("Settled()") / keccak256("SettledWithPermit()")
SETTLED_TOPIC = "0x97088ec3606cfe8cc112180570d03fcde05f9b8e1bfef8e27784eaf5dd5691b6"
SETTLED_WITH_PERMIT_TOPIC = "0xde5b89d10fc800c459329c382fabfcad0be0ed7e5328e01fae04e507b09ef5d8"


def permit2_label(chain) -> str:
    """Coverage label for a chain's Permit2 index (separate ledger/start block
    from its EIP-3009 index)."""
    return f"{chain.name}_permit2"


def index_subrange_permit2(client: ChainClient, store: Store, lo: int, hi: int,
                           chain, ts_cache: dict | None = None) -> int:
    """Index Permit2 settlements in [lo, hi] for `chain`. Returns rows written."""
    label = permit2_label(chain)
    # 1. Settled / SettledWithPermit markers in range -> x402 permit2 tx hashes.
    #    One getLogs with a topic-OR filter ([[a, b]] = topic0 is a OR b).
    settled_txs: set[str] = set()
    for lg in client.get_logs_chunked(
            address=PERMIT2_PROXY,
            topics=[[SETTLED_TOPIC, SETTLED_WITH_PERMIT_TOPIC]],
            from_block=lo, to_block=hi):
        settled_txs.add(lg["transactionHash"].lower())
    if not settled_txs:
        return store.commit_range(label, lo, hi, [], _utcnow())

    # 2. Each settled tx's receipt carries the ERC-20 Transfer(s) — the actual
    #    settlement. Token is arbitrary (non-USDC); recorded per row.
    rows = []
    for tx in settled_txs:
        rcpt = client.get_transaction_receipt(tx)
        if not rcpt:
            continue
        for lg in rcpt.get("logs", []):
            topics = lg.get("topics") or []
            if len(topics) == 3 and topics[0].lower() == TRANSFER_TOPIC:
                rows.append(decode_transfer(lg, label))

    # 3. Enrich timestamps (receipts never carry blockTimestamp).
    if ts_cache is None:
        ts_cache = {}
    for r in rows:
        if not r["block_timestamp"]:
            bn = r["block_number"]
            if bn not in ts_cache:
                ts_cache[bn] = client.get_block_timestamp(bn)
            r["block_timestamp"] = ts_cache[bn]
    missing = [r for r in rows if not r["block_timestamp"]]
    if missing:
        raise RuntimeError(
            f"{len(missing)}/{len(rows)} {label} settlements in [{lo},{hi}] "
            f"have no block_timestamp even after enrichment.")
    return store.commit_range(label, lo, hi, rows, _utcnow())


def run_permit2(client: ChainClient, store: Store, start: int, end: int,
                chain) -> None:
    label = permit2_label(chain)
    gaps = store.find_gaps(label, start, end)
    if not gaps:
        print(f"[permit2:{chain.name}] [{start},{end}] already fully covered")
        return
    total = sum(g[1] - g[0] + 1 for g in gaps)
    print(f"[permit2:{chain.name}] target [{start},{end}] "
          f"{len(gaps)} gap(s), {total} blocks")
    done, written = 0, 0
    ts_cache: dict[int, int] = {}
    for gap_lo, gap_hi in gaps:
        lo = gap_lo
        while lo <= gap_hi:
            hi = min(lo + chain.subrange - 1, gap_hi)
            written += index_subrange_permit2(client, store, lo, hi, chain, ts_cache)
            done += hi - lo + 1
            lo = hi + 1
    print(f"[permit2:{chain.name}] done: {written} settlements over {done} blocks")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

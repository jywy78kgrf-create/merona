"""Minimal, defensive JSON-RPC client for Base (and EVM-compatible chains).

Design goals for a process that runs unattended for months:
- Every call retries with backoff; a transient RPC hiccup never silently
  returns partial data.
- getLogs is chunked to the RPC's hard range cap (measured: 10,000 blocks on
  base.org) with an adaptive split on "range too large"/result-size errors, so
  we can point it at a different RPC later without silent truncation.
- No dependency on any indexer/API we don't control — only raw JSON-RPC.
"""

from __future__ import annotations

import time
from typing import Any

import requests


class RpcError(RuntimeError):
    pass


class ChainClient:
    def __init__(self, url: str, *, max_range: int = 10_000,
                 timeout: int = 45, max_retries: int = 6,
                 user_agent: str = "x402-audit-indexer/1.0"):
        self.url = url
        self.max_range = max_range
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self._id = 0

    def _call(self, method: str, params: list) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id,
                   "method": method, "params": params}
        last: Exception | None = None
        for attempt in range(self.max_retries):
            wait = min(2 ** attempt, 30)
            try:
                r = self.session.post(self.url, json=payload, timeout=self.timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    # a throttling RPC (keyed free tiers) says how long to hold
                    # off — honor it, else the backoff just re-trips the limit
                    ra = r.headers.get("Retry-After", "")
                    if ra.isdigit():
                        wait = max(wait, min(int(ra), 60))
                    raise RpcError(f"http {r.status_code}")
                body = r.json()
                if "error" in body:
                    # surface RPC errors to caller (getLogs uses them to split).
                    # error may be a JSON-RPC object {code,message} or, on some
                    # providers, a bare string — handle both without crashing.
                    err = body["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise RpcError(msg)
                return body["result"]
            except (requests.RequestException, RpcError, ValueError) as e:
                last = e
                # do not retry a deterministic "range too large" — caller splits
                if isinstance(e, RpcError) and "range" in str(e).lower():
                    raise
                time.sleep(wait)
        raise RpcError(f"{method} failed after {self.max_retries} tries: {last}")

    def block_number(self) -> int:
        return int(self._call("eth_blockNumber", []), 16)

    def finalized_block(self) -> int | None:
        """Highest FINALIZED block via the 'finalized' tag, or None if the RPC
        doesn't expose it. Finality — not a fixed confirmations buffer — is the
        only reorg-proof read height: a reorg above finalized is silent
        corruption the range ledger cannot catch. Callers fall back to
        head - confirmations only when this returns None."""
        try:
            b = self._call("eth_getBlockByNumber", ["finalized", False])
        except RpcError:
            return None
        if not b or "number" not in b:
            return None
        return int(b["number"], 16)

    def get_block_timestamp(self, block: int) -> int:
        b = self._call("eth_getBlockByNumber", [hex(block), False])
        return int(b["timestamp"], 16)

    def get_transaction(self, tx_hash: str):
        """The transaction object (incl. `from` = the relayer/submitter), or None.
        Used to x402-scope the EIP-3009 superset: a settlement is x402 only if
        tx.from is a known facilitator relayer."""
        return self._call("eth_getTransactionByHash", [tx_hash])

    def get_block_transactions(self, block: int):
        """Every transaction in a block as full objects (each carries `hash` and
        `from`). Enrichment fetches by block, not by tx: on chains where many
        settlements share a block, one getBlockByNumber(full=true) yields all
        their relayers at once — ~8x fewer calls and ~8x less RPC compute than
        per-tx getTransactionByHash, which is what keeps us under a keyed RPC's
        rate cap on a large backfill. Returns [] if the block isn't found."""
        b = self._call("eth_getBlockByNumber", [hex(block), True])
        if not b:
            return []
        return b.get("transactions", []) or []

    def get_transaction_receipt(self, tx_hash: str):
        """Full receipt (incl. `logs`) for one tx, or None if not found.
        Used by the receipt-based fetch: on chains where USDC transfers vastly
        outnumber x402 txs, pulling only the known x402 txs' receipts is far
        cheaper than scanning every Transfer log."""
        return self._call("eth_getTransactionReceipt", [tx_hash])

    def get_logs_chunked(self, *, address: str, topics: list,
                         from_block: int, to_block: int):
        """Yield logs across [from_block, to_block] inclusive, splitting on the
        RPC range cap and adaptively halving on any range/size error. Never
        skips a sub-range: a failure that can't be split raises."""
        lo = from_block
        while lo <= to_block:
            hi = min(lo + self.max_range - 1, to_block)
            span = hi - lo
            try:
                logs = self._call("eth_getLogs", [{
                    "fromBlock": hex(lo), "toBlock": hex(hi),
                    "address": address, "topics": topics}])
            except RpcError as e:
                msg = str(e).lower()
                # 429 splits too: on throughput-capped keyed RPCs a smaller
                # range is a cheaper query, which is what clears the throttle
                if ("range" in msg or "too large" in msg or "limit" in msg
                        or "more than" in msg or "429" in msg) and span > 0:
                    # halve and retry the lower half; upper half re-loops
                    self.max_range = max(1, (span + 1) // 2)
                    continue
                raise
            for lg in logs:
                yield lg
            lo = hi + 1

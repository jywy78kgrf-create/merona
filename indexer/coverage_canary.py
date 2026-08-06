"""Dark-matter canary — daily coverage-ratio measurement.

The indexer attributes x402 settlements from on-chain signals. This measures, in
a bounded FINALIZED window per chain, how much of the TOTAL on-chain signal the
indexer actually attributes:

  EIP-3009: attributed USDC settlements  /  total AuthorizationUsed events
  Permit2 : txs yielding a settlement    /  total Settled/SettledWithPermit markers

A ratio ~1.0 means we capture essentially all of the mechanism's activity. A
DROP is the early warning that a new facilitator/contract launched, or a new
settlement pattern appeared, that our attribution logic misses — i.e. dark
matter. Stored per chain per day (`coverage_ratio` table) and alerted on when it
leaves the band. This is a measurement, never a writer of settlement rows.
"""

from __future__ import annotations

from decode import (AUTHORIZATION_USED_TOPIC, TRANSFER_TOPIC, decode_transfer,
                    topic_to_address)
from index_permit2 import (PERMIT2_PROXY, SETTLED_TOPIC,
                           SETTLED_WITH_PERMIT_TOPIC)

# Alert when attribution drops below this fraction of total signal. ~1.0 is the
# observed baseline for EIP-3009 on every chain; a real regime change shows here.
RATIO_ALERT_BELOW = 0.98


def measure_eip3009(client, chain, lo: int, hi: int) -> dict:
    """Attributed USDC settlements vs total AuthorizationUsed in [lo, hi].

    Uses the same attribution rule as the indexer (a settlement = a USDC
    Transfer whose payer authorized it in-tx), so the ratio directly reflects
    what the live index would/would not capture. Read-only; scans logs only.
    """
    auth_by_tx: dict[str, set[str]] = {}
    for lg in client.get_logs_chunked(address=chain.usdc,
                                      topics=[AUTHORIZATION_USED_TOPIC],
                                      from_block=lo, to_block=hi):
        auth_by_tx.setdefault(lg["transactionHash"].lower(), set()).add(
            topic_to_address(lg["topics"][1]))
    total_events = sum(len(v) for v in auth_by_tx.values())
    attributed, volume = 0, 0
    if auth_by_tx and chain.use_receipts:
        # high-volume chains (Arbitrum): fetch only the auth txs' receipts, don't
        # scan every USDC Transfer in the window (mirrors the indexer's strategy).
        for tx, au in auth_by_tx.items():
            rcpt = client.get_transaction_receipt(tx)
            if not rcpt:
                continue
            for lg in rcpt.get("logs", []):
                topics = lg.get("topics") or []
                if (lg.get("address", "").lower() != chain.usdc
                        or len(topics) != 3
                        or topics[0].lower() != TRANSFER_TOPIC):
                    continue
                rec = decode_transfer(lg, chain.name)
                if rec["payer"] in au:
                    attributed += 1
                    volume += rec["amount"]
    elif auth_by_tx:
        for lg in client.get_logs_chunked(address=chain.usdc,
                                          topics=[TRANSFER_TOPIC],
                                          from_block=lo, to_block=hi):
            au = auth_by_tx.get(lg["transactionHash"].lower())
            if not au:
                continue
            rec = decode_transfer(lg, chain.name)
            if rec["payer"] in au:
                attributed += 1
                volume += rec["amount"]
    return {"mechanism": "eip3009", "window": (lo, hi),
            "total": total_events, "attributed": attributed, "volume": volume}


def measure_permit2(client, chain, lo: int, hi: int) -> dict:
    """Permit2 Settled/SettledWithPermit markers vs txs that carry an ERC-20
    Transfer we'd attribute, in [lo, hi]. Tiny volume today; the point is to
    catch it the day it stops being tiny."""
    settled_txs: set[str] = set()
    for lg in client.get_logs_chunked(
            address=PERMIT2_PROXY,
            topics=[[SETTLED_TOPIC, SETTLED_WITH_PERMIT_TOPIC]],
            from_block=lo, to_block=hi):
        settled_txs.add(lg["transactionHash"].lower())
    total = len(settled_txs)
    attributed = 0
    for tx in settled_txs:
        rcpt = client.get_transaction_receipt(tx)
        if not rcpt:
            continue
        if any((lg.get("topics") or [None])[0]
               and lg["topics"][0].lower() == TRANSFER_TOPIC
               for lg in rcpt.get("logs", [])):
            attributed += 1
    return {"mechanism": "permit2", "window": (lo, hi),
            "total": total, "attributed": attributed, "volume": 0}


def window_for(chain, finalized_end: int, span: int = 3000) -> tuple[int, int]:
    """A bounded, finalized measurement window ending at the finalized head."""
    return (max(0, finalized_end - span), finalized_end)

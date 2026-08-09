"""The Solana volume ceiling — the independent denominator Solana otherwise lacks.

Pins the two pieces that make "we aren't missing volume" a number, not a claim:
  * summarize_block: a transfer counts as GASLESS (x402-shaped) only when the
    payer isn't the fee payer, USDC only, failed txs excluded, and the gasless
    total splits correctly into known-relayer vs UNKNOWN-relayer (the miss).
  * extrapolate: point estimate = per-slot mean x span, with a bootstrap upper
    bound that never falls below the point estimate.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "indexer"))
pytest.importorskip("requests")

from decode_solana import USDC_SOLANA                        # noqa: E402
from solana_volume_ceiling import extrapolate, summarize_block  # noqa: E402

OTHER_MINT = "So11111111111111111111111111111111111111112"


def _tx(fee_payer, src_ta, dst_ta, src_owner, amount, mint=USDC_SOLANA,
        err=None):
    return {
        "meta": {"err": err, "preTokenBalances": [
            {"accountIndex": 1, "owner": src_owner, "mint": mint},
            {"accountIndex": 2, "owner": "seller_" + dst_ta, "mint": mint}]},
        "transaction": {"message": {
            "accountKeys": [fee_payer, src_ta, dst_ta],
            "instructions": [{"program": "spl-token", "parsed": {
                "type": "transfer",
                "info": {"source": src_ta, "destination": dst_ta,
                         "amount": str(amount)}}}]}}}


def test_summarize_block_splits_gasless_known_and_unknown():
    block = {"transactions": [
        _tx("KNOWN_RELAYER", "src1", "dst1", "payer1", 1_000_000),   # gasless/known
        _tx("UNKNOWN_FP",    "src2", "dst2", "payer2", 2_000_000),   # gasless/unknown
        _tx("payer3",        "src3", "dst3", "payer3", 5_000_000),   # self-paid
        _tx("UNKNOWN_FP",    "src4", "dst4", "payer4", 9_000_000,    # non-USDC
            mint=OTHER_MINT),
        _tx("UNKNOWN_FP",    "src5", "dst5", "payer5", 7_000_000,    # failed
            err={"InstructionError": [0, "x"]}),
    ]}
    s = summarize_block(block, known={"KNOWN_RELAYER"})
    assert s["total_usdc"] == 8_000_000          # 1+2+5 (non-USDC & failed out)
    assert s["gasless_usdc"] == 3_000_000        # 1+2 (self-paid excluded)
    assert s["gasless_known"] == 1_000_000
    assert s["gasless_unknown"] == 2_000_000
    assert s["n_usdc"] == 3 and s["n_gasless"] == 2
    assert s["unknown_fee_payers"] == {"UNKNOWN_FP": 2_000_000}


def test_summarize_block_handles_empty_and_none():
    assert summarize_block(None, set())["total_usdc"] == 0
    assert summarize_block({"transactions": []}, set())["gasless_usdc"] == 0


def test_extrapolate_point_and_upper_bound():
    per_slot = [0, 0, 3_000_000, 0]              # one hot slot in four
    r = extrapolate(per_slot, total_slots=100, seed=7)
    assert r["n"] == 4
    assert r["point"] == 3_000_000 / 4 * 100     # mean x span
    assert r["upper99"] >= r["point"]            # upper bound never below point


def test_extrapolate_empty_is_zero():
    r = extrapolate([], total_slots=1000)
    assert r["point"] == 0.0 and r["upper99"] == 0.0 and r["n"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

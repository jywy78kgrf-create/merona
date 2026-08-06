"""Fixture tests for the facilitator entry bar (no network).

Each test is one way a non-facilitator can look like a facilitator on-chain.
These are the failures that would otherwise land a wrong address in the
registry, and a wrong registry entry manufactures volume that never happened
across every downstream figure — so the bar is tested, not assumed.

Run:  python indexer/tests/test_verify_solana_facilitator.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decode_solana import USDC_SOLANA, decode_solana_settlements  # noqa: E402
from verify_solana_facilitator import (  # noqa: E402
    judge, new_stats, observe, role_split)

CAND = "CandidateRelayer1111111111111111111111111111"
OTHER_FEE_PAYER = "SomeoneElse11111111111111111111111111111111"


def make_tx(fee_payer: str, payer: str, seller: str, amount: int = 1_000_000,
            payer_signs: bool = True) -> dict:
    """A minimal jsonParsed USDC settlement.

    accountKeys: 0=fee payer, 1=payer wallet, 2=src ATA, 3=dst ATA. The payer
    wallet is carried as a separate key so `signer` can be toggled — that flag
    is the x402 delegation shape the bar tests for.
    """
    src, dst = f"ATA_SRC_{payer}", f"ATA_DST_{seller}"
    return {
        "slot": 500_000_000,
        "blockTime": 1_780_000_000,
        "transaction": {"message": {
            "accountKeys": [
                {"pubkey": fee_payer, "signer": True},
                {"pubkey": payer, "signer": payer_signs},
                {"pubkey": src, "signer": False},
                {"pubkey": dst, "signer": False},
            ],
            "instructions": [
                {"program": "spl-token", "parsed": {
                    "type": "transferChecked",
                    "info": {"source": src, "destination": dst,
                             "authority": payer, "mint": USDC_SOLANA,
                             "tokenAmount": {"amount": str(amount),
                                             "decimals": 6}}}},
            ],
        }},
        "meta": {
            "err": None, "innerInstructions": [],
            "preTokenBalances": [
                {"accountIndex": 2, "owner": payer, "mint": USDC_SOLANA},
                {"accountIndex": 3, "owner": seller, "mint": USDC_SOLANA},
            ],
            "postTokenBalances": [],
        },
    }


def run(txs, candidate=CAND) -> dict:
    st = new_stats(candidate)
    for tx in txs:
        observe(st, tx, decode_solana_settlements(tx, "sig"), candidate)
    return st


def test_real_facilitator_passes():
    txs = [make_tx(CAND, f"Payer{i%10}", f"Seller{i%5}") for i in range(30)]
    st = run(txs)
    verdict, _ = judge(st)
    assert verdict == "FACILITATOR", verdict
    assert st["settlements"] == 30
    assert len(st["sellers"]) == 5 and len(st["payers"]) == 10
    assert st["volume_units"] == 30_000_000


def test_merchant_hot_wallet_is_not_a_relayer():
    """Pays its own fees, but is the payer in its own settlements. Passes the
    fee-payer test; must fail on being a counterparty."""
    txs = [make_tx(CAND, CAND, f"Seller{i%5}") for i in range(30)]
    verdict, _ = judge(run(txs))
    assert verdict == "COUNTERPARTY_NOT_RELAYER", verdict


def test_seller_collecting_through_its_own_submitter_is_caught():
    txs = [make_tx(CAND, f"Payer{i%10}", CAND) for i in range(30)]
    verdict, _ = judge(run(txs))
    assert verdict == "COUNTERPARTY_NOT_RELAYER", verdict


def test_custodial_operator_fails_the_cosign_test():
    """Moves funds it already controls: nobody but the fee payer signs. Looks
    exactly like a facilitator on volume, seller count and payer count."""
    txs = [make_tx(CAND, f"Payer{i%10}", f"Seller{i%5}", payer_signs=False)
           for i in range(30)]
    st = run(txs)
    assert len(st["sellers"]) == 5 and len(st["payers"]) == 10
    verdict, _ = judge(st)
    assert verdict == "CUSTODIAL_NOT_X402", verdict


def test_single_seller_is_flagged_not_approved():
    txs = [make_tx(CAND, f"Payer{i%10}", "OnlySeller") for i in range(30)]
    verdict, reason = judge(run(txs))
    assert verdict == "SELF_RELAY_SINGLE_SELLER", verdict
    assert "off-chain source" in reason


def test_thin_traffic_is_not_promoted():
    txs = [make_tx(CAND, f"Payer{i%4}", f"Seller{i%4}") for i in range(24)]
    verdict, _ = judge(run(txs))
    assert verdict == "THIN", verdict


def test_too_few_settlements_is_insufficient_not_a_pass():
    txs = [make_tx(CAND, f"Payer{i}", f"Seller{i%5}") for i in range(5)]
    verdict, _ = judge(run(txs))
    assert verdict == "INSUFFICIENT_EVIDENCE", verdict


def test_txs_the_candidate_did_not_submit_are_not_credited():
    """getSignaturesForAddress returns every tx TOUCHING the address. If those
    counted, any busy counterparty would clear the bar."""
    txs = [make_tx(OTHER_FEE_PAYER, CAND, f"Seller{i%5}") for i in range(40)]
    st = run(txs)
    assert st["txs"] == 40 and st["settlement_txs"] == 40
    assert st["fee_paid_txs"] == 0 and st["settlements"] == 0
    verdict, _ = judge(st)
    assert verdict == "INSUFFICIENT_EVIDENCE", verdict


def test_disqualification_beats_promotion():
    """A candidate with excellent breadth but self-dealing traffic must not
    fall through to FACILITATOR — order of tests in judge() is load-bearing."""
    txs = [make_tx(CAND, f"Payer{i%10}", f"Seller{i%5}") for i in range(60)]
    txs += [make_tx(CAND, CAND, f"Seller{i%5}") for i in range(5)]
    st = run(txs)
    assert st["self_hits"] / st["settlements"] > 0.02
    verdict, _ = judge(st)
    assert verdict == "COUNTERPARTY_NOT_RELAYER", verdict


def test_role_split_separates_a_buyer_agent_from_a_relayer():
    """Both fail the counterparty test. Only one of them is wrongly excluded,
    and the split is what tells them apart."""
    buyer = make_tx(CAND, CAND, "SellerA")
    assert role_split(buyer, decode_solana_settlements(buyer, "s"),
                      CAND)["payer"] == 1
    relay = make_tx(CAND, "PayerA", "SellerA")
    assert role_split(relay, decode_solana_settlements(relay, "s"),
                      CAND)["third_party"] == 1


def test_role_split_ignores_txs_the_address_did_not_submit():
    tx = make_tx(OTHER_FEE_PAYER, CAND, "SellerA")
    assert role_split(tx, decode_solana_settlements(tx, "s"), CAND) == {
        "payer": 0, "seller": 0, "third_party": 0}


def test_failed_transactions_contribute_nothing():
    tx = make_tx(CAND, "PayerX", "SellerX")
    tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    st = run([tx])
    assert st["txs"] == 1 and st["settlement_txs"] == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(dict(globals()).items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nall {len(fns)} facilitator-bar tests passed")

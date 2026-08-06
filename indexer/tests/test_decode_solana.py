"""Unit tests for the Solana settlement decoder — the token-account -> owner
resolution that, if wrong, silently misattributes every seller.

Fixture is a real Base-mainnet... er, Solana-mainnet USDC transferChecked tx
(shape captured from getTransaction jsonParsed), reduced to the fields the
decoder reads.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decode_solana import USDC_SOLANA, decode_solana_settlements  # noqa: E402

# accountKeys indices: 0=relayer(feepayer), 1=src token acct, 2=dst token acct
FIXTURE = {
    "slot": 430592775,
    "blockTime": 1783110754,
    "transaction": {"message": {
        "accountKeys": [
            {"pubkey": "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4"},
            {"pubkey": "SRC_TOKEN_ACCT"},
            {"pubkey": "DST_TOKEN_ACCT"},
        ],
        "instructions": [
            {"program": "spl-token", "parsed": {
                "type": "transferChecked",
                "info": {"source": "SRC_TOKEN_ACCT",
                         "destination": "DST_TOKEN_ACCT",
                         "authority": "PAYER_WALLET",
                         "mint": USDC_SOLANA,
                         "tokenAmount": {"amount": "1000", "decimals": 6}}}},
        ],
    }},
    "meta": {
        "err": None,
        "innerInstructions": [],
        "preTokenBalances": [
            {"accountIndex": 1, "owner": "PAYER_WALLET", "mint": USDC_SOLANA,
             "uiTokenAmount": {"amount": "5000"}},
            {"accountIndex": 2, "owner": "SELLER_WALLET", "mint": USDC_SOLANA,
             "uiTokenAmount": {"amount": "0"}},
        ],
        "postTokenBalances": [
            {"accountIndex": 1, "owner": "PAYER_WALLET", "mint": USDC_SOLANA,
             "uiTokenAmount": {"amount": "4000"}},
            {"accountIndex": 2, "owner": "SELLER_WALLET", "mint": USDC_SOLANA,
             "uiTokenAmount": {"amount": "1000"}},
        ],
    },
}


def test_decodes_owner_not_token_account():
    rows = decode_solana_settlements(FIXTURE, "SIG123")
    assert len(rows) == 1
    r = rows[0]
    # payer/seller must be the WALLET OWNERS, not the token accounts
    assert r["payer"] == "PAYER_WALLET"
    assert r["seller"] == "SELLER_WALLET"
    assert r["amount"] == 1000
    assert r["token"] == USDC_SOLANA
    assert r["tx_hash"] == "SIG123"
    assert r["block_number"] == 430592775
    assert r["block_timestamp"] == 1783110754
    assert r["log_index"] == 0


def test_skips_failed_tx():
    bad = {**FIXTURE, "meta": {**FIXTURE["meta"], "err": {"InstructionError": []}}}
    assert decode_solana_settlements(bad, "SIG") == []


def test_skips_non_usdc():
    other = {
        "slot": 1, "blockTime": 1,
        "transaction": {"message": {
            "accountKeys": [{"pubkey": "R"}, {"pubkey": "S"}, {"pubkey": "D"}],
            "instructions": [{"program": "spl-token", "parsed": {
                "type": "transferChecked",
                "info": {"source": "S", "destination": "D",
                         "mint": "SOMEOTHERMINT",
                         "tokenAmount": {"amount": "1"}}}}]}},
        "meta": {"err": None, "innerInstructions": [], "preTokenBalances": [
            {"accountIndex": 1, "owner": "P", "mint": "SOMEOTHERMINT",
             "uiTokenAmount": {"amount": "1"}},
            {"accountIndex": 2, "owner": "Q", "mint": "SOMEOTHERMINT",
             "uiTokenAmount": {"amount": "0"}}],
            "postTokenBalances": []},
    }
    assert decode_solana_settlements(other, "SIG") == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nall {len(fns)} solana decode tests passed")

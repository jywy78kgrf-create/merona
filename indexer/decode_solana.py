"""Pure decoder for Solana x402 settlements. No I/O — unit-testable.

A Solana x402 settlement is an SPL-token `transfer`/`transferChecked` of USDC
inside a transaction whose fee payer is a facilitator relayer. Unlike EVM, the
instruction references token *accounts*, not wallets — so payer and seller are
the *owners* of the source/destination token accounts, resolved via the tx's
pre/postTokenBalances (each entry maps accountKeys[accountIndex] -> owner,mint).

Getting that owner resolution right is the whole correctness game on Solana:
decode the token accounts naively and every seller is wrong.
"""

from __future__ import annotations

USDC_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _token_account_owner_map(tx: dict) -> dict[str, tuple[str, str]]:
    """token_account_pubkey -> (owner_wallet, mint), from pre+post balances."""
    msg = tx["transaction"]["message"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
    meta = tx.get("meta") or {}
    owner: dict[str, tuple[str, str]] = {}
    for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        idx = b["accountIndex"]
        if 0 <= idx < len(keys) and b.get("owner"):
            owner[keys[idx]] = (b["owner"], b.get("mint", ""))
    return owner


def _iter_spl_transfers(tx: dict):
    """Yield (instruction_index, info) for every spl-token transfer(-Checked).

    Indexes are assigned across top-level then inner instructions in a stable
    order so (signature, index) is a deterministic idempotency key.
    """
    msg = tx["transaction"]["message"]
    idx = 0
    for ix in msg.get("instructions", []):
        p = ix.get("parsed")
        if (ix.get("program") == "spl-token" and isinstance(p, dict)
                and p.get("type") in ("transfer", "transferChecked")):
            yield idx, p["info"]
        idx += 1
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in inner["instructions"]:
            p = ix.get("parsed")
            if (ix.get("program") == "spl-token" and isinstance(p, dict)
                    and p.get("type") in ("transfer", "transferChecked")):
                yield idx, p["info"]
            idx += 1


def decode_solana_settlements(tx: dict, signature: str) -> list[dict]:
    """Return USDC settlement rows for one confirmed jsonParsed transaction.

    Skips failed txs and non-USDC transfers. Amount is read from the instruction
    (transferChecked carries tokenAmount; bare transfer carries amount).
    """
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    owner = _token_account_owner_map(tx)
    slot = tx.get("slot")
    block_time = tx.get("blockTime")
    # The fee payer (first account key) is the relayer that submitted the tx —
    # i.e. the facilitator. Recording it lets us attribute per-facilitator
    # volume and bound reconciliation to each relayer's indexed depth.
    msg = tx["transaction"]["message"]
    keys0 = msg["accountKeys"][0]
    facilitator = (keys0["pubkey"] if isinstance(keys0, dict) else keys0)
    rows = []
    for i, info in _iter_spl_transfers(tx):
        src, dst = info.get("source"), info.get("destination")
        payer = owner.get(src)
        seller = owner.get(dst)
        # both endpoints must resolve to USDC token accounts, else it's not a
        # USDC settlement (or a token account we can't attribute) — skip, don't
        # guess.
        if not payer or not seller:
            continue
        if payer[1] != USDC_SOLANA or seller[1] != USDC_SOLANA:
            continue
        amt = info.get("tokenAmount", {}).get("amount") or info.get("amount")
        if amt is None:
            continue
        rows.append({
            "chain": "solana",
            "token": USDC_SOLANA,
            "payer": payer[0],
            "seller": seller[0],
            "amount": int(amt),
            "block_number": slot,          # Solana slot (reused column)
            "block_timestamp": block_time,
            "tx_hash": signature,
            "log_index": i,
            "facilitator": facilitator,
        })
    return rows

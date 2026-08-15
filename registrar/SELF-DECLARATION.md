# merona facilitator self-declaration (v1)

**For x402 facilitators on chains without a public registry** (today:
Avalanche, Optimism, Arbitrum, Sei — declarations for registry-covered chains
are also accepted). merona indexes the raw EIP-3009 settlement flow on these
chains every night, but cannot *attribute* any of it to x402 without knowing
which relayer addresses belong to real facilitators. Declaring yours fixes
that — and because the full history is already indexed, **your entire past
relayed volume becomes attributed retroactively** the moment your declaration
is merged. There is no fee, and never will be: a registry you can pay into is
a registry nobody can trust.

## How it works

1. **You sign a message from each relayer key.** Control of the relayer key
   *is* the identity proof — no forms, no accounts.
2. **We verify the signature** (`indexer/verify_declaration.py`, runnable by
   anyone) **and that the address actually facilitates x402.** A valid
   signature is necessary but not sufficient: before pinning, we check the
   declared address relays genuine x402 settlements (a live facilitator
   endpoint we can drive a settle flow through, or existing on-chain
   settlements consistent with x402 traffic). This is what keeps a generic
   gasless-USDC relayer from laundering its volume into "x402".
3. **The entry lands in `data/indexer/relayer_registry_declared.json`** with
   the signature stored alongside it — every declaration remains
   independently re-verifiable forever — and merges into the canonical
   registry on the next build. Scoped settlement counts for your chain(s)
   appear from the next nightly cycle, full history included.

## The message

One message per (chain, relayer address). Exact format, five lines,
`\n`-separated, no trailing newline:

```
merona facilitator declaration v1
facilitator: <your-facilitator-id>
chain: <base|polygon|solana|avalanche|optimism|arbitrum|sei>
relayer: <address>
contact: <email or URL>
date: <YYYY-MM-DD>
```

- `facilitator` — a short stable id (lowercase, `[a-z0-9-]`), e.g. `anyspend`.
- `relayer` — EVM: the 0x address checksummed or lowercase; Solana: the base58
  relayer pubkey. Must be the *tx-sender* address that submits settlements.
- `date` — the day you sign. Declarations are public statements; replaying one
  changes nothing, so no nonce is needed.

**EVM chains:** sign with EIP-191 (`personal_sign`) from the relayer key.
**Solana:** ed25519-sign the UTF-8 bytes with the relayer keypair; encode the
signature base64.

## Submitting

Open a PR against this repository adding your entry to
`data/indexer/relayer_registry_declared.json` (shape below). The signature is
the credential — the PR is just the transport.

```json
"your-facilitator-id": {
  "avalanche": [{"address": "0x…", "first_tx_date": null}],
  "_declaration": {
    "contact": "ops@example.com",
    "date": "2026-07-19",
    "signatures": {
      "avalanche:0x…": "0x<65-byte EIP-191 signature, hex>"
    }
  }
}
```

Verify locally before submitting:

```bash
pip install -e ".[registrar]"
python indexer/verify_declaration.py path/to/your-entry.json
```

## Revocation

Sign the same five-line message with `merona facilitator revocation v1` as the
first line and submit it the same way. The entry is removed from the merge
(the declaration and revocation both stay on record).

## What merona commits to

- **Verification is mechanical and public** — the same script we run is in
  this repo; nobody gets pinned without a signature that you can check.
- **No fees, no tiers, no discretion beyond the x402-activity check.**
- **Drift-checked** — declared entries are excluded from the weekly x402scan
  drift diff (upstream not listing them is expected); their provenance is the
  stored signature.

# On-chain attestations

merona publishes attestations on Base via [EAS](https://attest.org) — the
attestation predeploy every OP-stack chain ships at
`0x4200000000000000000000000000000000000021`. Phase 1 (this directory) is the
**nightly snapshot anchor**: the same combined SHA-256 that
`deploy/anchor.sh` pushes to the public merona-anchors git repo, attested on
the chain the commerce itself settles on. Git proves priority via GitHub's
timestamp; the chain proves it via consensus. Either ledger verifies the
other byte-for-byte.

## Policy (binding, enforced in code where possible)

1. **Revocable, always.** `eas.encode_attest()` raises on `revocable=False`.
   merona publishes recorded facts with a working corrections mechanism —
   an irrevocable on-chain claim has no corrections mechanism.
2. **Positive/neutral content only goes on-chain.** Snapshot anchors (a date
   and a hash) are content-free by construction. When grade attestations ship
   (phase 2), only A/B grades are attested; F/D grades, wash flags, and any
   adverse finding stay off-chain, where they can be argued with, corrected,
   and read in context. An on-chain F is a public accusation with a
   permanent timestamp — that violates "recorded facts, not accusations".
3. **Never self-pay, never simulate commerce.** The attester wallet makes
   attestation transactions only. merona never generates transactions that
   look like x402 settlements toward its own payTo — that is literally the
   mrdn pattern we flag. Correspondingly, merona's own payTo is excluded
   from clean metrics as a seller (`indexer/wash.py`, `X402_OWN_PAYTO`),
   exactly like relayer addresses: even the *appearance* of our numbers
   containing our own receipts is off the table.
4. **Dedicated attester wallet.** Never the payTo/revenue wallet, never a
   wallet holding meaningful funds. It carries a few dollars of Base ETH for
   gas and its key exists in exactly two places: `/etc/x402-attest.key` on
   the box (owner `x402`, mode 600) and the password manager entry
   "merona attester key". The key is read from a file, never from env or
   argv (env leaks via /proc and `systemctl show`).
5. **Every revocation carries a public-record reason** in the local ledger
   (`revoke.py` refuses to run without one).

## Public identity

Attester: **`0x644678AD37833C0d52f0170f1F73A5e62Bc3e6d5`** (Base) —
[attestations](https://base.easscan.org/address/0x644678AD37833C0d52f0170f1F73A5e62Bc3e6d5).
The binding is two-way, so neither half can be faked alone:

- **site → chain**: this address is published on merona.io, the API landing
  page, and the anchors repo README. Only attestations from it are merona's.
- **chain → site**: a one-time identity attestation
  (`identity_attest.py`, schema `string name,string url,string anchorsRepo`)
  points the address back at merona.io and the anchors repo.

If the attester key is ever lost or rotated: mint a new wallet, re-run
`identity_attest.py` from it, update the address everywhere the site names
it, and note the change in the anchors repo README — the old address's
history stays valid for the dates it covers.

## Files

| file | role |
|---|---|
| `eas.py` | encoding + signing layer (eth-abi/eth-account, no web3) |
| `anchor_attest.py` | nightly: attest unattested snapshot days (cap 3/run) |
| `register_schema.py` | one-time schema registration on Base |
| `revoke.py` | revocation runbook, executable |
| `../data/indexer/attest/anchor_attestations.jsonl` | append-only local ledger |

Schema v1: `string snapshotDate,bytes32 combinedSha256,string sourceHeadCommit`
(revocable, no resolver). The schema UID is deterministic —
`eas.schema_uid()` computes it locally; nothing reads registry state.

## Setup (once, on the box)

```
# 1. dedicated attester key (on a trusted machine; back up to password manager)
python3 -c "from eth_account import Account; a = Account.create(); \
print(a.address); print(a.key.hex())"
#    -> address is public (fund it); key goes into the password manager AND:
sudo sh -c 'umask 077; cat > /etc/x402-attest.key'   # paste key, ctrl-d
sudo chown x402:x402 /etc/x402-attest.key

# 2. fund the address with ~$5 of Base ETH (years of nightly anchors)

# 3. deps + schema (idempotent; checks on-chain state first)
sudo -u x402 .venv/bin/pip install -e ".[attest]"
sudo -u x402 .venv/bin/python attest/register_schema.py

# 4. done — run.sh picks it up nightly; verify the first anchor with
sudo -u x402 .venv/bin/python attest/anchor_attest.py --dry-run
```

## Revocation runbook

A wrong anchor (bad hash, wrong date — e.g. snapshot files rewritten after
attestation) gets corrected, not abandoned:

1. `sudo -u x402 .venv/bin/python attest/revoke.py <uid> --reason "…"` —
   the reason lands in the ledger; EAS keeps the revoked attestation visible
   with a revocation timestamp (the correction is part of the record).
2. The revoked day is automatically re-attested with the corrected hash on
   the next nightly run (`attested_dates()` treats revoked anchors as
   pending).
3. If the correction changes published numbers, it also gets a
   `docs/SCORE_REVISIONS.md` entry like every other revision.

## Failure model

Best-effort by design, mirroring `anchor.sh`: no key file → skip with a
pending count; RPC down / fee above `ATTEST_MAX_FEE_GWEI` (default 0.5, ~10×
normal Base fees) / tx unconfirmed → skip, retry next night. A backlog
drains at `ATTEST_MAX_PER_RUN` (default 3) days per night, newest first.
Nothing here can block or fail the index run.

## Phase 2 (not built): grade attestations

Revocable EAS attestations of A/B seller grades (grade, seller, chain,
score_version, snapshot sha), from the same attester wallet under the same
policy. Revocation triggers: grade drops below B at any nightly cycle, or a
conduct flag appears. Blocked on: a few stable nightly cycles of scores
v4.2 first, so we're not attesting grades we'd revoke a week later.

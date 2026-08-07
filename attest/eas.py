"""Minimal EAS (Ethereum Attestation Service) client for merona's anchors.

EAS is a predeploy on every OP-stack chain, Base included:

  EAS             0x4200000000000000000000000000000000000021
  SchemaRegistry  0x4200000000000000000000000000000000000020

so there is nothing to deploy — merona registers one schema once and then
attests to it nightly. This module is the encoding + signing layer only:
pure functions over eth-abi/eth-account (the same optional-dependency family
the registrar verifier already uses), JSON-RPC via requests. No web3.

POLICY (attest/README.md is the full document; the code enforces what it can):
  * Every attestation is REVOCABLE. encode_attest() raises on revocable=False —
    merona publishes recorded facts with a corrections mechanism, never
    irrevocable claims.
  * Only positive/neutral content goes on-chain. This layer carries snapshot
    anchors (a date and a hash — content-free by construction); adverse
    findings (F grades, wash flags) stay off-chain where they can be argued
    with, corrected, and contextualized.
"""

from __future__ import annotations

import time

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

# OP-stack predeploys (identical address on Base mainnet and every OP chain).
EAS_ADDRESS = "0x4200000000000000000000000000000000000021"
SCHEMA_REGISTRY_ADDRESS = "0x4200000000000000000000000000000000000020"
ZERO_ADDRESS = "0x" + "00" * 20
ZERO_BYTES32 = b"\x00" * 32

# The merona anchor schema, v1. Field names follow EAS camelCase convention;
# combinedSha256 is the exact value anchor.sh publishes to the public
# merona-anchors repo, so the two ledgers cross-verify byte-for-byte.
ANCHOR_SCHEMA = "string snapshotDate,bytes32 combinedSha256,string sourceHeadCommit"
ANCHOR_SCHEMA_VERSION = 1

# One-time identity attestation: the chain→site half of the two-way binding
# (the site→chain half is the attester address published on merona.io, the
# API landing page, and the anchors repo README). Lets anyone who finds the
# attester cold follow it home — and makes an impostor wallet's lack of a
# matching claim on merona.io self-evident.
IDENTITY_SCHEMA = "string name,string url,string anchorsRepo"

# Selectors (keccak-derived at import so a typo'd signature fails loudly).
SEL_ATTEST = keccak(text="attest((bytes32,(address,uint64,bool,bytes32,bytes,uint256)))")[:4]
SEL_REGISTER = keccak(text="register(string,address,bool)")[:4]
SEL_REVOKE = keccak(text="revoke((bytes32,(bytes32,uint256)))")[:4]
ATTESTED_TOPIC = "0x" + keccak(text="Attested(address,address,bytes32,bytes32)").hex()

ATTEST_TYPES = ["(bytes32,(address,uint64,bool,bytes32,bytes,uint256))"]
REVOKE_TYPES = ["(bytes32,(bytes32,uint256))"]


# ---------------------------------------------------------------- encoding --

def schema_uid(schema: str = ANCHOR_SCHEMA, resolver: str = ZERO_ADDRESS,
               revocable: bool = True) -> bytes:
    """The deterministic EAS schema UID: keccak256(abi.encodePacked(
    schema, resolver, revocable)). Computable locally, so nothing needs to
    read it back from the registry after the one-time registration."""
    packed = (schema.encode() + bytes.fromhex(resolver[2:])
              + (b"\x01" if revocable else b"\x00"))
    return keccak(packed)


def encode_anchor_data(snapshot_date: str, combined_sha256: str,
                       head_commit: str) -> bytes:
    """ABI payload for ANCHOR_SCHEMA. combined_sha256 is the 64-hex-char
    value from the anchors ledger (with or without 0x)."""
    sha = combined_sha256.removeprefix("0x")
    if len(sha) != 64:
        raise ValueError(f"combined_sha256 must be 32 bytes hex, got {sha!r}")
    return abi_encode(["string", "bytes32", "string"],
                      [snapshot_date, bytes.fromhex(sha), head_commit])


def encode_attest(uid: bytes, data: bytes, *, recipient: str = ZERO_ADDRESS,
                  revocable: bool = True, expiration: int = 0,
                  ref_uid: bytes = ZERO_BYTES32) -> bytes:
    """Calldata for EAS.attest(). revocable=False is a policy violation, not
    a parameter — merona never publishes an attestation it cannot revoke."""
    if not revocable:
        raise ValueError("merona policy: every on-chain attestation must be "
                         "revocable (see attest/README.md)")
    req = (uid, (to_checksum_address(recipient), expiration, True,
                 ref_uid, data, 0))
    return SEL_ATTEST + abi_encode(ATTEST_TYPES, [req])


def encode_register(schema: str = ANCHOR_SCHEMA, resolver: str = ZERO_ADDRESS,
                    revocable: bool = True) -> bytes:
    """Calldata for SchemaRegistry.register() — the one-time setup call."""
    if not revocable:
        raise ValueError("merona policy: schemas are registered revocable")
    return SEL_REGISTER + abi_encode(["string", "address", "bool"],
                                     [schema, to_checksum_address(resolver), True])


def encode_revoke(uid: bytes, attestation_uid: bytes) -> bytes:
    """Calldata for EAS.revoke() — the corrections mechanism the revocable
    policy exists for. See the revocation runbook in attest/README.md."""
    return SEL_REVOKE + abi_encode(REVOKE_TYPES, [(uid, (attestation_uid, 0))])


# ------------------------------------------------------------------- chain --

SEL_GET_SCHEMA = keccak(text="getSchema(bytes32)")[:4]


def schema_exists(session, rpc_url: str, uid: bytes) -> bool:
    """getSchema(uid) returns a record whose uid field is zero when absent —
    the idempotency check before any register() (re-registering REVERTS)."""
    data = SEL_GET_SCHEMA + abi_encode(["bytes32"], [uid])
    out = rpc(session, rpc_url, "eth_call",
              [{"to": SCHEMA_REGISTRY_ADDRESS, "data": "0x" + data.hex()},
               "latest"])
    # first 32-byte word after the struct offset is the stored uid
    return len(out) >= 130 and int(out[66:130], 16) != 0


def rpc(session, url: str, method: str, params: list):
    r = session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                "params": params}, timeout=30).json()
    if "error" in r:
        raise RuntimeError(f"{method}: {r['error'].get('message', r['error'])}")
    return r["result"]


def load_key(path: str) -> Account:
    """Read a hex private key from a file (never from env or argv — a key in
    an environment variable leaks via /proc and `systemctl show`)."""
    import os
    st = os.stat(path)
    if st.st_mode & 0o077:
        print(f"[attest] WARNING: {path} is group/other-readable "
              f"(mode {oct(st.st_mode & 0o777)}); chmod 600 it", flush=True)
    key = open(path).read().strip()
    return Account.from_key(key)


def build_fees(session, url: str, max_fee_gwei: float) -> tuple[int, int] | None:
    """(max_fee, max_priority) in wei, or None if the chain is currently more
    expensive than our cap — in which case tonight's anchor is simply skipped
    and tomorrow's run attests both days. Base's base fee is normally well
    under 0.1 gwei, so the cap only bites during genuine fee events."""
    blk = rpc(session, url, "eth_getBlockByNumber", ["latest", False])
    base_fee = int(blk.get("baseFeePerGas", "0x0"), 16)
    try:
        prio = int(rpc(session, url, "eth_maxPriorityFeePerGas", []), 16)
    except Exception:
        prio = 1_000_000  # 0.001 gwei
    max_fee = 2 * base_fee + prio
    if max_fee > int(max_fee_gwei * 1e9):
        return None
    return max_fee, prio


def send_tx(session, url: str, acct: Account, to: str, data: bytes,
            chain_id: int, max_fee_gwei: float) -> str | None:
    """Sign + send one EIP-1559 tx. Returns the tx hash, or None when skipped
    on the fee cap. Raises on RPC/signing errors (caller decides fail-soft)."""
    fees = build_fees(session, url, max_fee_gwei)
    if fees is None:
        print(f"[attest] fee cap {max_fee_gwei} gwei exceeded — skipping this "
              f"run (will retry next night)", flush=True)
        return None
    max_fee, prio = fees
    nonce = int(rpc(session, url, "eth_getTransactionCount",
                    [acct.address, "pending"]), 16)
    gas = int(rpc(session, url, "eth_estimateGas",
                  [{"from": acct.address, "to": to_checksum_address(to),
                    "data": "0x" + data.hex()}]), 16)
    tx = {"chainId": chain_id, "nonce": nonce, "to": to_checksum_address(to),
          "value": 0, "data": data, "gas": int(gas * 12 // 10),
          "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio}
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    return rpc(session, url, "eth_sendRawTransaction", ["0x" + raw.hex()])


def wait_receipt(session, url: str, tx_hash: str, timeout_s: int = 120) -> dict | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            rcpt = rpc(session, url, "eth_getTransactionReceipt", [tx_hash])
        except Exception:
            rcpt = None
        if rcpt is not None:
            return rcpt
        time.sleep(3)
    return None


def attestation_uid_from_receipt(receipt: dict) -> str | None:
    """The new attestation's UID out of the Attested event (uid is the sole
    non-indexed field, so it IS the log data)."""
    for lg in receipt.get("logs", []):
        if (lg.get("address", "").lower() == EAS_ADDRESS
                and (lg.get("topics") or [""])[0].lower() == ATTESTED_TOPIC):
            data = lg.get("data", "0x")
            if len(data) >= 66:
                return "0x" + data[2:66]
    return None

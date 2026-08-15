#!/usr/bin/env python3
"""Recompute-from-chain evidence verifier — the demo.

Takes a /v1/trust/evaluate response (or a bare sha256 + snapshot date) and
walks the FULL chain of custody back to the public record, using nothing but
the Python stdlib and public infrastructure a third party already trusts
(GitHub's raw content CDN, a public Base JSON-RPC):

  1. Fetch the public anchors file (deploy/anchor.sh's output) from the
     merona-anchors GitHub repo for the response's snapshot_date.
  2. Confirm the response's evidence_uri sha256 is the recorded per-file
     hash for that day (seller_aggregates.csv — see WHY below).
  3. Recompute the day's COMBINED hash from the anchor record's per-file
     hashes, using anchor.sh's exact recipe, and confirm it matches the
     anchor's own combined_sha256 (so step 2's single-file match isn't
     being checked against a record that was itself tampered with).
  4. Read the EAS attestation for that combined hash directly off Base via
     a public JSON-RPC (schema + attester address are the real constants
     from attest/eas.py and attest/README.md — cited inline below) and
     confirm the on-chain value matches, noting when it was attested.

Every step is independently re-derivable by anyone: this script trusts
GitHub's push history for priority (step 1) and Base consensus for
timestamping (step 4), not merona's servers — the response being verified
is never fetched from merona at all, only read from a file/stdin/CLI arg.

WHAT THIS PROVES: the evidence snapshot behind the decision existed, is
byte-identical to what was hashed at verification time, and was anchored on
Base at a specific, independently-checkable time (integrity + timestamp of
the SNAPSHOT FILE — including whichever nightly grades and event history it
already contains, since seller_aggregates.csv IS the score computation's
input).

WHAT THIS DOES NOT PROVE: that the SCORING RULES applied to that snapshot
are the ones being claimed, or that the score in the response is what
indexer/scores.py would output from it — re-deriving the actual number
requires the dataset and the scoring code (indexer/scores.py), which this
script deliberately does not need or fetch. It also doesn't prove today's
LIVE score matches this evidence (scores are recomputed nightly); it proves
this specific dated, hashed evidence file is the one the decision cited.

Usage:
  python3 conformance/verify_evidence.py response.json
  python3 conformance/verify_evidence.py < response.json
  python3 conformance/verify_evidence.py --sha256 <hex> --date 2026-08-08
  python3 conformance/verify_evidence.py response.json --no-chain   # skip step 4
  python3 conformance/verify_evidence.py response.json \\
      --rpc https://your-keyed-rpc.example --from-block 30000000

Zero-dependency: Python 3 stdlib only (urllib for both GitHub and JSON-RPC;
a pure-Python Keccak-256 for the EAS schema UID / event topic / function
selector, since Ethereum's keccak differs from hashlib's NIST SHA3).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

TIMEOUT_S = 20
USER_AGENT = "merona-verify-evidence/1.0"

# --- public source-of-truth locations ---------------------------------------
# The repo URL merona's own README/findings pages publish
# (dashboard/findings.html footer) — this script fetches the SAME public
# file deploy/anchor.sh pushes to, not a copy bundled with merona's code.
ANCHORS_REPO = "jywy78kgrf-create/merona-anchors"
ANCHORS_BRANCHES = ("main", "master")   # tried in order; whichever exists
ANCHORS_FILE = "anchors.jsonl"

# The file trust_api.py's /v1/trust/evaluate ALWAYS hashes for evidence_uri.
# _evaluate_seller_record() (serving/trust_api.py) is fed exclusively by
# trust_lookup(), and trust_lookup() calls `_verification(snap)` with NO
# fname argument — i.e. always the default "seller_aggregates.csv"
# (serving/trust_api.py, _verification()'s signature and trust_lookup's
# single call site). agent_lookup() uses payer_aggregates.csv instead, but
# /v1/trust/evaluate never reaches agent_lookup (direction=buyer short-
# circuits before any lookup — see _trust_evaluate_body). This is the one
# assumption this script makes about a response it wasn't given the
# filename for; --file lets you override it if that ever changes.
DEFAULT_SNAPSHOT_FILE = "seller_aggregates.csv"

# --- EAS constants — SAME VALUES as attest/eas.py and attest/README.md,
# copied (not imported: this script must run standalone) so a reader can
# diff them against the source of truth by hand. ----------------------------
EAS_ADDRESS = "0x4200000000000000000000000000000000000021"   # attest/eas.py EAS_ADDRESS
# attest/eas.py ANCHOR_SCHEMA — the exact field order/types anchor_attest.py
# encodes into every snapshot-anchor attestation.
ANCHOR_SCHEMA = "string snapshotDate,bytes32 combinedSha256,string sourceHeadCommit"
# attest/README.md "Public identity": the ONE wallet whose attestations are
# merona's (0x644678AD37833C0d52f0170f1F73A5e62Bc3e6d5 / merona.base.eth).
# An attestation from any other address is not merona's, however it decodes.
ATTESTER_ADDRESS = "0x644678AD37833C0d52f0170f1F73A5e62Bc3e6d5"
ZERO_ADDRESS = "0x" + "00" * 20

# Base launched ~2023-06; x402/this indexer's history_anchor_block (BASE,
# indexer/evm_chains.py) is 29_700_000 (~2025-05, "~x402 launch on Base") —
# a defensible floor: no merona anchor attestation can predate x402 existing
# on Base at all. Only used as a binary-search sanity bound, not scanned
# block-by-block (see find_block_at_or_after below).
BASE_FLOOR_BLOCK = 29_700_000

DEFAULT_BASE_RPCS = (
    # indexer/evm_chains.py BASE.default_rpc — the same public endpoint the
    # indexer itself falls back to without a keyed X402_BASE_RPC.
    "https://mainnet.base.org",
    # Additional public, no-key Base RPCs for failover — not exhaustive;
    # override with --rpc (repeatable) if these are down or rate-limiting.
    "https://base.publicnode.com",
    "https://base.drpc.org",
    "https://1rpc.io/base",
)
# Free public RPCs commonly cap eth_getLogs to a few thousand blocks per call
# AND rate-limit bursts (observed empirically: fine for one-off calls, but a
# tight loop of getLogs against mainnet.base.org/publicnode/drpc starts
# returning 403/413/429 within seconds). DEFAULT_CHUNK_BLOCKS keeps each call
# small enough for that; DEFAULT_CHUNK_DELAY_S paces successive calls to the
# same window of RPCs. A keyed/paid endpoint (--rpc) removes both limits —
# same tradeoff evm_chains.py documents for X402_BASE_RPC vs the public default.
DEFAULT_CHUNK_BLOCKS = 8_000
DEFAULT_CHUNK_DELAY_S = 0.5
MAX_CHUNKS = 2_000


class Step:
    """One line of the stepwise report."""

    def __init__(self, name: str, status: str, detail: str = ""):
        self.name, self.status, self.detail = name, status, detail

    def line(self) -> str:
        tail = f" — {self.detail}" if self.detail else ""
        return f"[{self.status:4}] {self.name}{tail}"


# =============================================================================
# pure-Python Keccak-256 (Ethereum's keccak: legacy 0x01 padding, NOT NIST
# SHA3's 0x06) — needed for the EAS schema UID, the Attested(...) event
# topic, and the getAttestation(bytes32) function selector. No stdlib hash
# implements this variant. Validated against attest/eas.py's eth_utils-
# backed constants and RFC-style Keccak test inputs — see
# indexer/tests/test_verify_evidence.py.
# =============================================================================
_KECCAK_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
# Rotation offsets r[x][y] — note the outer index is y (row), inner is x
# (column): _KECCAK_ROT[y][x] == r(x,y) in the Keccak spec's own notation.
_KECCAK_ROT = [
    [0, 1, 62, 28, 27],
    [36, 44, 6, 55, 20],
    [3, 10, 43, 25, 39],
    [41, 45, 15, 21, 8],
    [18, 2, 61, 56, 14],
]
_MASK64 = (1 << 64) - 1


def _rol64(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _MASK64


def _keccak_f1600(state: list[int]) -> list[int]:
    for rnd in range(24):
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20]
             for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol64(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= d[x]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol64(state[x + 5 * y],
                                                           _KECCAK_ROT[y][x])
        for y in range(5):
            for x in range(5):
                state[x + 5 * y] = (b[x + 5 * y]
                                    ^ ((~b[(x + 1) % 5 + 5 * y]) & _MASK64
                                       & b[(x + 2) % 5 + 5 * y]))
        state[0] ^= _KECCAK_RC[rnd]
    return state


def keccak256(data: bytes) -> bytes:
    rate = 136                              # 1088-bit rate for 256-bit Keccak
    pad_len = rate - (len(data) % rate) or rate
    padded = bytearray(data) + bytearray(pad_len)
    padded[len(data)] = 0x01                # legacy Keccak domain byte (SHA3 uses 0x06)
    padded[-1] |= 0x80
    state = [0] * 25
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[i * 8:(i + 1) * 8], "little")
        state = _keccak_f1600(state)
    return b"".join(state[i].to_bytes(8, "little") for i in range(4))


def schema_uid(schema: str = ANCHOR_SCHEMA, resolver: str = ZERO_ADDRESS,
               revocable: bool = True) -> bytes:
    """Same recipe as attest/eas.py's schema_uid(): keccak256(schema ||
    resolver_bytes20 || revocable_byte). Deterministic — nothing to look up."""
    packed = (schema.encode() + bytes.fromhex(resolver[2:])
              + (b"\x01" if revocable else b"\x00"))
    return keccak256(packed)


ATTESTED_TOPIC = "0x" + keccak256(b"Attested(address,address,bytes32,bytes32)").hex()
_SEL_GET_ATTESTATION = keccak256(b"getAttestation(bytes32)")[:4]


def _topic_addr(addr: str) -> str:
    return "0x" + "00" * 12 + addr[2:].lower()


def _topic_bytes32(b: bytes) -> str:
    return "0x" + b.hex()


# =============================================================================
# JSON-RPC (stdlib urllib), with failover across DEFAULT_BASE_RPCS
# =============================================================================
class RpcError(Exception):
    pass


def _rpc_call_one(url: str, method: str, params: list):
    req = urllib.request.Request(
        url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                              "params": params}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        body = json.loads(resp.read())
    if "error" in body:
        raise RpcError(f"{method}: {body['error']}")
    return body["result"]


def rpc_call(rpc_urls: list[str], method: str, params: list, retries: int = 2):
    """Try each RPC in order, with a couple of short-backoff retries per
    endpoint before moving on — free public RPCs answer a burst of calls
    with transient 403/413/429-shaped errors that a 1-2s pause clears (this
    is rate-limiting, not the org-egress-policy 403 the sandbox's own proxy
    would raise, which is a different failure with its own class — see
    /root/.ccr/README.md — and is NOT retried here). Raises RpcError (with
    every endpoint's last failure) only once ALL of them are exhausted."""
    errors = []
    for url in rpc_urls:
        for attempt in range(retries + 1):
            try:
                return _rpc_call_one(url, method, params)
            except (RpcError, urllib.error.URLError, TimeoutError,
                    json.JSONDecodeError, OSError) as e:
                last = f"{url}: {e}"
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        errors.append(last)
    raise RpcError("all RPC endpoints failed: " + "; ".join(errors))


def scan_eas_logs(rpc_urls: list[str], uid: bytes, from_block: int, to_block: int,
                  chunk_blocks: int = DEFAULT_CHUNK_BLOCKS,
                  max_chunks: int = MAX_CHUNKS,
                  delay_s: float = DEFAULT_CHUNK_DELAY_S) -> tuple[list[dict], bool]:
    """eth_getLogs for the Attested(recipient=0x0, attester=merona,
    schema=uid) filter, walked in `chunk_blocks`-sized windows rather than
    one call over the whole [from_block, to_block] span. Public RPCs
    routinely cap (and rate-limit) wide getLogs ranges — see
    DEFAULT_CHUNK_BLOCKS's comment — so one big call reliably 403s/413s on
    the free endpoints this script defaults to. Returns (all matching logs,
    whether the FULL range was scanned) — a caller must treat `not logs`
    as "not found in the scanned range" only when the bool is True."""
    logs: list[dict] = []
    lo = from_block
    chunks = 0
    while lo <= to_block:
        if chunks >= max_chunks:
            return logs, False
        hi = min(lo + chunk_blocks - 1, to_block)
        logs += rpc_call(rpc_urls, "eth_getLogs", [{
            "address": EAS_ADDRESS, "fromBlock": hex(lo), "toBlock": hex(hi),
            "topics": [ATTESTED_TOPIC, _topic_addr(ZERO_ADDRESS),
                      _topic_addr(ATTESTER_ADDRESS), _topic_bytes32(uid)],
        }])
        chunks += 1
        lo = hi + 1
        if lo <= to_block and delay_s:
            time.sleep(delay_s)          # pace successive calls (see above)
    return logs, True


def get_block_number(rpc_urls: list[str]) -> int:
    return int(rpc_call(rpc_urls, "eth_blockNumber", []), 16)


def get_block_timestamp(rpc_urls: list[str], block_number: int) -> int:
    blk = rpc_call(rpc_urls, "eth_getBlockByNumber",
                   [hex(block_number), False])
    if blk is None:
        raise RpcError(f"block {block_number} not found (ahead of chain tip?)")
    return int(blk["timestamp"], 16)


def find_block_at_or_after(rpc_urls: list[str], target_ts: int,
                           lo: int, hi: int) -> int:
    """Binary search over block NUMBERS by TIMESTAMP: the earliest block
    whose timestamp is >= target_ts. O(log(hi-lo)) RPC calls regardless of
    how wide [lo, hi] is — this is what lets step 4 search "from the
    snapshot date to today" without a fixed, possibly-wrong block-time
    formula, and without scanning millions of blocks one chunk at a time."""
    while lo < hi:
        mid = (lo + hi) // 2
        ts = get_block_timestamp(rpc_urls, mid)
        if ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


# =============================================================================
# getAttestation(bytes32) — manual ABI decode (fixed shape, no generic
# decoder needed: this is the ONLY return shape this script ever decodes).
# Layout: EAS.Attestation { bytes32 uid; bytes32 schema; uint64 time;
# uint64 expirationTime; uint64 revocationTime; bytes32 refUID;
# address recipient; address attester; bool revocable; bytes data; }
# =============================================================================
def decode_get_attestation(raw_hex: str) -> dict:
    data = bytes.fromhex(raw_hex[2:] if raw_hex.startswith("0x") else raw_hex)
    if len(data) < 32:
        raise ValueError("getAttestation: return data too short")
    tuple_start = int.from_bytes(data[0:32], "big")
    f = data[tuple_start:tuple_start + 32 * 10]
    if len(f) < 32 * 10:
        raise ValueError("getAttestation: truncated tuple")

    def word(i):
        return f[32 * i:32 * (i + 1)]

    uid = word(0)
    schema = word(1)
    time_ = int.from_bytes(word(2)[-8:], "big")
    expiration_time = int.from_bytes(word(3)[-8:], "big")
    revocation_time = int.from_bytes(word(4)[-8:], "big")
    ref_uid = word(5)
    recipient = "0x" + word(6)[-20:].hex()
    attester = "0x" + word(7)[-20:].hex()
    revocable = word(8)[-1] != 0
    data_offset = int.from_bytes(word(9), "big")
    d_pos = tuple_start + data_offset
    d_len = int.from_bytes(data[d_pos:d_pos + 32], "big")
    inner = data[d_pos + 32:d_pos + 32 + d_len]
    return {
        "uid": "0x" + uid.hex(), "schema": "0x" + schema.hex(), "time": time_,
        "expiration_time": expiration_time, "revocation_time": revocation_time,
        "ref_uid": "0x" + ref_uid.hex(), "recipient": recipient,
        "attester": attester, "revocable": revocable, "data": inner,
    }


def decode_anchor_data(inner: bytes) -> dict:
    """Decode ANCHOR_SCHEMA's payload: (string snapshotDate, bytes32
    combinedSha256, string sourceHeadCommit) — the top-level ABI encoding
    of a 3-tuple (string, bytes32, string): two dynamic offsets around one
    static 32-byte value."""
    off1 = int.from_bytes(inner[0:32], "big")
    combined = inner[32:64]
    off2 = int.from_bytes(inner[64:96], "big")

    def read_string(off):
        length = int.from_bytes(inner[off:off + 32], "big")
        return inner[off + 32:off + 32 + length].decode("utf-8")

    return {
        "snapshot_date": read_string(off1),
        "combined_sha256": combined.hex(),
        "source_head_commit": read_string(off2),
    }


# =============================================================================
# steps 1-3: public git anchors
# =============================================================================
def fetch_anchors_jsonl() -> list[dict]:
    last_err = None
    for branch in ANCHORS_BRANCHES:
        url = (f"https://raw.githubusercontent.com/{ANCHORS_REPO}/{branch}/"
              f"{ANCHORS_FILE}")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                text = resp.read().decode("utf-8")
        except urllib.error.URLError as e:
            last_err = e
            continue
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows
    raise RpcError(f"could not fetch {ANCHORS_FILE} from any of "
                   f"{ANCHORS_BRANCHES}: {last_err}")


def find_snapshot_anchor(rows: list[dict], snapshot_date: str) -> dict | None:
    # anchor.sh's per-day rows carry `snapshot_date` and no `type` key (the
    # historical_results rows use type="historical_results" and have no
    # snapshot_date at all) — filtering on snapshot_date alone is enough to
    # exclude those, but the "no type" check documents the distinction
    # explicitly rather than relying on it being incidentally true.
    matches = [r for r in rows
              if r.get("snapshot_date") == snapshot_date and "type" not in r]
    return matches[-1] if matches else None       # append-only; last wins if >1


def recompute_combined_sha256(files_sha256: dict) -> str:
    """EXACT recipe from deploy/anchor.sh's hashing block: sha256 of the
    sorted "name:hash" pairs, concatenated with no separator."""
    return hashlib.sha256(
        "".join(f"{n}:{h}" for n, h in sorted(files_sha256.items())).encode()
    ).hexdigest()


# =============================================================================
# main verification flow
# =============================================================================
def verify(evidence_sha256: str, snapshot_date: str,
          snapshot_file: str = DEFAULT_SNAPSHOT_FILE,
          check_chain: bool = True, rpc_urls: list[str] = DEFAULT_BASE_RPCS,
          from_block: int | None = None,
          chunk_blocks: int = DEFAULT_CHUNK_BLOCKS,
          max_chunks: int = MAX_CHUNKS) -> tuple[list[Step], bool]:
    steps: list[Step] = []
    evidence_sha256 = evidence_sha256.lower().removeprefix("sha256:")

    # --- step 1: fetch the public anchor record for this date --------------
    try:
        rows = fetch_anchors_jsonl()
    except RpcError as e:
        steps.append(Step("1.fetch_anchors_jsonl", "FAIL", str(e)))
        return steps, False
    steps.append(Step("1.fetch_anchors_jsonl", "PASS",
                      f"{len(rows)} row(s) from {ANCHORS_REPO}/{ANCHORS_FILE}"))
    record = find_snapshot_anchor(rows, snapshot_date)
    if record is None:
        steps.append(Step("1.find_snapshot_date", "FAIL",
                          f"no anchor row for snapshot_date={snapshot_date!r}"))
        return steps, False
    steps.append(Step("1.find_snapshot_date", "PASS",
                      f"anchored_at={record.get('anchored_at')}"))

    # --- step 2: evidence_uri sha256 == the recorded per-file hash --------
    files = record.get("files_sha256") or {}
    matched_file = None
    if files.get(snapshot_file) == evidence_sha256:
        matched_file = snapshot_file
    else:
        for name, h in files.items():
            if h == evidence_sha256:
                matched_file = name
                break
    if matched_file is None:
        steps.append(Step("2.evidence_matches_anchor", "FAIL",
                          f"sha256:{evidence_sha256} not found under any "
                          f"filename in this day's anchor "
                          f"({sorted(files)})"))
        return steps, False
    note = "" if matched_file == snapshot_file else (
        f"NOTE: matched under {matched_file!r}, not the expected "
        f"{snapshot_file!r} — see DEFAULT_SNAPSHOT_FILE's assumption")
    steps.append(Step("2.evidence_matches_anchor", "PASS",
                      f"sha256:{evidence_sha256} == files_sha256[{matched_file!r}]"
                      + (f" ({note})" if note else "")))

    # --- step 3: recompute the combined hash from the SAME record ---------
    recomputed = recompute_combined_sha256(files)
    anchor_combined = record.get("combined_sha256")
    if recomputed != anchor_combined:
        steps.append(Step("3.recompute_combined_hash", "FAIL",
                          f"recomputed {recomputed} != anchor's own "
                          f"combined_sha256 {anchor_combined} — the anchor "
                          f"record is internally inconsistent"))
        return steps, False
    steps.append(Step("3.recompute_combined_hash", "PASS",
                      f"combined_sha256={recomputed}"))

    if not check_chain:
        steps.append(Step("4.eas_attestation", "WARN", "--no-chain: skipped"))
        return steps, True

    # --- step 4: find + decode the EAS attestation on Base -----------------
    try:
        uid = schema_uid()
        latest = get_block_number(rpc_urls)
        if from_block is None:
            target_ts = int(datetime.strptime(snapshot_date, "%Y-%m-%d")
                            .replace(tzinfo=timezone.utc).timestamp())
            from_block = find_block_at_or_after(
                rpc_urls, target_ts, BASE_FLOOR_BLOCK, latest)
        logs, scan_complete = scan_eas_logs(rpc_urls, uid, from_block, latest,
                                            chunk_blocks, max_chunks)
    except (RpcError, ValueError) as e:
        steps.append(Step("4.eas_getLogs", "FAIL", str(e)))
        return steps, False
    n_chunks = -(-(latest - from_block + 1) // chunk_blocks)     # ceil div
    steps.append(Step(
        "4.eas_getLogs",
        "PASS" if scan_complete else "WARN",
        f"{len(logs)} candidate attestation(s) in blocks "
        f"[{from_block}, {latest}]"
        + ("" if scan_complete else
           f" — INCOMPLETE: hit --max-chunks ({max_chunks}) before covering "
           f"all {n_chunks} chunk(s); pass --max-chunks or --chunk-blocks to "
           f"widen, or a narrower --from-block")))
    if not logs:
        detail = ("no Attested log from the merona attester for this "
                  "schema in the scanned range")
        if not scan_complete:
            detail += " (scan was INCOMPLETE — see 4.eas_getLogs above)"
        else:
            detail += (" — try --from-block to widen it (a large attest "
                       "backlog can delay anchoring well past the "
                       "snapshot date)")
        steps.append(Step("4.eas_attestation", "FAIL", detail))
        return steps, False

    live, revoked = None, None
    for lg in logs:
        att_uid = lg["data"][2:66] if lg.get("data", "0x") != "0x" else None
        if att_uid is None:
            continue
        try:
            raw = rpc_call(rpc_urls, "eth_call",
                           [{"to": EAS_ADDRESS,
                             "data": "0x" + _SEL_GET_ATTESTATION.hex()
                                     + att_uid}, "latest"])
            att = decode_get_attestation(raw)
            anchor = decode_anchor_data(att["data"])
        except (RpcError, ValueError):
            continue
        if (anchor["snapshot_date"] == snapshot_date
                and anchor["combined_sha256"] == recomputed
                and att["attester"].lower() == ATTESTER_ADDRESS.lower()):
            att["_anchor"] = anchor
            att["_tx_hash"] = lg.get("transactionHash")
            if att["revocation_time"]:
                revoked = att
            else:
                live = att
                break
    chosen = live or revoked
    if chosen is None:
        steps.append(Step("4.eas_attestation", "FAIL",
                          "candidate log(s) found but none decoded to this "
                          "exact (snapshot_date, combined_sha256, attester)"))
        return steps, False
    attested_at = datetime.fromtimestamp(chosen["time"], tz=timezone.utc).isoformat()
    if revoked and not live:
        # A revoked attestation with no newer, live re-attestation superseding
        # it is evidence merona ITSELF has disavowed on-chain (attest/README.md's
        # documented revocation/correction mechanism). Reporting this as
        # VERIFIED would make the verifier affirm evidence its own publisher
        # retracted — the single worst failure mode for a tool whose entire
        # pitch is "don't trust us, verify". This must FAIL (exit non-zero),
        # not WARN-and-pass.
        steps.append(Step("4.eas_attestation", "FAIL",
                          f"attestation {chosen['uid']} MATCHES but was "
                          f"REVOKED (revoked at unix {chosen['revocation_time']}) "
                          f"and no newer live re-attestation supersedes it — "
                          f"see attest/README.md's revocation runbook (a "
                          f"revocation requires a reason + re-attestation; "
                          f"none was found in the scanned range)"))
        return steps, False
    steps.append(Step("4.eas_attestation", "PASS",
                      f"uid={chosen['uid']} tx={chosen['_tx_hash']} "
                      f"attested_at={attested_at} attester={chosen['attester']}"))
    return steps, True


def _load_response(path: str | None) -> dict:
    text = sys.stdin.read() if path in (None, "-") else open(path).read()
    return json.loads(text)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Recompute an x402-trust-evaluation-v0.1 response's "
                    "evidence back to the public anchors repo + Base EAS.")
    ap.add_argument("response", nargs="?",
                    help="path to a saved /v1/trust/evaluate response JSON, "
                         "or '-'/omitted for stdin")
    ap.add_argument("--sha256", help="evidence sha256 (with or without the "
                                     "'sha256:' prefix), skips reading a response")
    ap.add_argument("--date", help="snapshot date YYYY-MM-DD, required with --sha256")
    ap.add_argument("--file", default=DEFAULT_SNAPSHOT_FILE,
                    help=f"snapshot filename to expect the hash under "
                         f"(default {DEFAULT_SNAPSHOT_FILE!r})")
    ap.add_argument("--no-chain", action="store_true",
                    help="skip step 4 (EAS on Base) — verify only against "
                         "the public git anchors")
    ap.add_argument("--rpc", action="append",
                    help="Base JSON-RPC URL (repeatable); overrides the "
                         "built-in public-RPC failover list")
    ap.add_argument("--from-block", type=int,
                    help="skip the timestamp binary search and scan from "
                         "this block number")
    ap.add_argument("--chunk-blocks", type=int, default=DEFAULT_CHUNK_BLOCKS,
                    help=f"blocks per eth_getLogs call (default "
                         f"{DEFAULT_CHUNK_BLOCKS}; lower this if your RPC "
                         f"rejects the default range)")
    ap.add_argument("--max-chunks", type=int, default=MAX_CHUNKS,
                    help=f"safety cap on eth_getLogs calls for step 4 "
                         f"(default {MAX_CHUNKS})")
    args = ap.parse_args(argv[1:])

    if args.sha256:
        if not args.date:
            ap.error("--sha256 requires --date")
        evidence_sha256, snapshot_date = args.sha256, args.date
    else:
        try:
            resp = _load_response(args.response)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[FAIL] could not read response: {e}", file=sys.stderr)
            return 2
        evidence_uri = resp.get("evidence_uri")
        snapshot_date = (resp.get("evidence") or {}).get("snapshot_date")
        if not evidence_uri or not snapshot_date:
            print("[FAIL] response has no evidence_uri / evidence.snapshot_date "
                 "to verify (an UNCERTAIN/insufficient-evidence decision has "
                 "nothing on-chain to check)", file=sys.stderr)
            return 2
        evidence_sha256 = evidence_uri

    rpc_urls = args.rpc or list(DEFAULT_BASE_RPCS)
    steps, verified = verify(evidence_sha256, snapshot_date,
                             snapshot_file=args.file,
                             check_chain=not args.no_chain,
                             rpc_urls=rpc_urls, from_block=args.from_block,
                             chunk_blocks=args.chunk_blocks,
                             max_chunks=args.max_chunks)
    for s in steps:
        print(s.line())
    print()
    print("VERIFIED" if verified else "NOT VERIFIED")
    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Nightly on-chain anchor: attest each snapshot day's hash on Base via EAS.

The git anchors repo (deploy/anchor.sh) proves a snapshot existed on a date
via GitHub's push timestamp. This upgrades that proof to the chain the
commerce itself happened on: one EAS attestation per snapshot day, from the
merona attester wallet, carrying (snapshotDate, combinedSha256,
sourceHeadCommit) — the combinedSha256 is byte-identical to the value in the
public anchors.jsonl, so the two ledgers cross-verify. A hash reveals nothing
about the private dataset; the attestation timestamp is consensus-final.

BEST-EFFORT like anchor.sh: invoked from run.sh via deploy/attest.sh after
the snapshot lands, and a failure (no key, RPC down, fee spike) must never
block or fail the index run — unattested days are picked up next night.

Setup (once, on the box — full runbook in attest/README.md):
  1. generate a DEDICATED attester wallet (never the payTo/revenue wallet),
     hex key into /etc/x402-attest.key, owner x402, chmod 600
  2. fund it with a few dollars of Base ETH (an attestation costs well under
     a cent at normal Base fees)
  3. register the schema once: .venv/bin/python attest/register_schema.py
  4. the nightly picks it up automatically once the key file exists

Usage:
  anchor_attest.py [--dry-run] [--date YYYY-MM-DD]

Env: X402_ATTEST_KEY_FILE (default /etc/x402-attest.key), X402_BASE_RPC,
     ATTEST_MAX_PER_RUN (default 3), ATTEST_MAX_FEE_GWEI (default 0.5).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "indexer"))

import eas                                    # noqa: E402
from evm_chains import BASE                   # noqa: E402

SNAP_DIR = ROOT / "data" / "indexer" / "snapshots"
LEDGER = ROOT / "data" / "indexer" / "attest" / "anchor_attestations.jsonl"
KEY_FILE = os.environ.get("X402_ATTEST_KEY_FILE", "/etc/x402-attest.key")
MAX_PER_RUN = int(os.environ.get("ATTEST_MAX_PER_RUN", "3"))
MAX_FEE_GWEI = float(os.environ.get("ATTEST_MAX_FEE_GWEI", "0.5"))


def combined_sha(day_dir: Path) -> str | None:
    """The snapshot day's combined hash — the EXACT algorithm anchor.sh uses
    (sorted "name:sha256" concatenation), so the on-chain value equals the
    git-anchored value and either ledger verifies the other."""
    files = {}
    for p in sorted(day_dir.iterdir()):
        if p.is_file():
            files[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    if not files:
        return None
    return hashlib.sha256(
        "".join(f"{n}:{h}" for n, h in sorted(files.items())).encode()).hexdigest()


def attested_dates() -> set[str]:
    """Days with a live (non-revoked) anchor. A revoked anchor's day becomes
    attestable again — revocation is a correction, and the corrected value
    should go back on-chain the next night."""
    anchors, revoked = [], set()
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("type") == "snapshot_anchor":
                anchors.append(row)
            elif row.get("type") == "revocation":
                revoked.add(row.get("attestation_uid"))
    return {r["snapshot_date"] for r in anchors
            if r.get("attestation_uid") not in revoked}


def head_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True,
                              timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    only_date = None
    if "--date" in argv:
        only_date = argv[argv.index("--date") + 1]

    if not SNAP_DIR.is_dir():
        print("[attest] no snapshots dir — nothing to anchor")
        return 0

    seen = attested_dates()
    days = sorted(d.name for d in SNAP_DIR.iterdir()
                  if d.is_dir() and d.name not in seen
                  and (only_date is None or d.name == only_date))
    # newest first, capped: a long-unattested backlog drains a few days per
    # night instead of producing a burst of transactions
    todo = list(reversed(days))[:MAX_PER_RUN]
    if not todo:
        print("[attest] all snapshot days already attested")
        return 0

    acct = None
    if os.path.exists(KEY_FILE):
        acct = eas.load_key(KEY_FILE)
    elif not dry:
        print(f"[attest] no attester key at {KEY_FILE} — skipping "
              f"({len(days)} day(s) pending; see attest/README.md setup)")
        return 0

    uid = eas.schema_uid()
    head = head_commit()
    rpc_url = os.environ.get(BASE.rpc_env) or BASE.default_rpc
    session = requests.Session()
    session.headers["User-Agent"] = "merona-attest/0.1 (hello@merona.io)"

    print(f"[attest] attester {acct.address if acct else '(no key: dry only)'}, "
          f"schema {uid.hex()[:16]}…, "
          f"{len(todo)}/{len(days)} pending day(s) this run"
          + (" [DRY RUN]" if dry else ""))

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    for day in todo:
        sha = combined_sha(SNAP_DIR / day)
        if sha is None:
            continue
        data = eas.encode_anchor_data(day, sha, head)
        calldata = eas.encode_attest(uid, data)
        if dry:
            print(f"[attest] DRY {day} sha {sha[:16]}… "
                  f"calldata {len(calldata)} bytes")
            continue
        try:
            tx_hash = eas.send_tx(session, rpc_url, acct, eas.EAS_ADDRESS,
                                  calldata, BASE.chain_id, MAX_FEE_GWEI)
        except Exception as e:
            print(f"[attest] {day}: send failed ({type(e).__name__}: {e}) — "
                  f"will retry next night", file=sys.stderr)
            break   # RPC/balance trouble affects the rest of the batch too
        if tx_hash is None:
            break   # fee cap — message already printed
        rcpt = eas.wait_receipt(session, rpc_url, tx_hash)
        if rcpt is None or int(rcpt.get("status", "0x0"), 16) != 1:
            print(f"[attest] {day}: tx {tx_hash} not confirmed/reverted — "
                  f"NOT ledgered; will retry next night", file=sys.stderr)
            break
        att_uid = eas.attestation_uid_from_receipt(rcpt)
        with open(LEDGER, "a") as fh:
            fh.write(json.dumps({
                "type": "snapshot_anchor",
                "schema_version": eas.ANCHOR_SCHEMA_VERSION,
                "snapshot_date": day,
                "combined_sha256": sha,
                "source_head_commit": head,
                "chain": "base",
                "schema_uid": "0x" + uid.hex(),
                "attestation_uid": att_uid,
                "tx_hash": tx_hash,
                "attester": acct.address,
                "attested_at": datetime.now(timezone.utc).isoformat(),
            }, sort_keys=True) + "\n")
        print(f"[attest] {day} anchored on base: uid {att_uid} tx {tx_hash}")
        done += 1
    if not dry:
        print(f"[attest] {done} day(s) anchored, "
              f"{len(days) - done} still pending")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

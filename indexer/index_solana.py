"""Independent Solana x402 settlement indexer.

Per facilitator relayer: fetch signatures newer than the stored cursor, decode
each tx's USDC settlements, insert idempotently, then advance the cursor — but
ONLY after the batch commits, so a crash re-does the batch rather than skipping
it. Sellers are self-discovered as transfer recipients' owners.

Correctness posture mirrors the Base indexer:
- idempotent inserts keyed on (signature, instruction_index);
- per-relayer cursor advanced only post-commit (no silent skip);
- `until`=cursor guarantees no gap above the cursor within a run;
- reorg-safe by indexing finalized data (getTransaction returns finalized).

Usage:
  python index_solana.py --to-head     # all relayers, newer-than-cursor
  python index_solana.py --status
"""

from __future__ import annotations

import argparse
import os
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from decode_solana import decode_solana_settlements
from solana_chain import SolanaClient, SolAuthError
from storage import Store, open_store

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "indexer" / "base_settlements.sqlite"  # shared DB
REGISTRY = ROOT / "data" / "indexer" / "relayer_registry.json"
# The free public endpoint 429-throttles under sustained load — fine for
# spot checks, NOT for bulk indexing. Point --rpc / X402_SOLANA_RPC at a keyed
# free-tier endpoint (Helius / Triton / QuickNode) for real operation.
DEFAULT_RPC = os.environ.get("X402_SOLANA_RPC", "https://api.mainnet-beta.solana.com")
CHAIN = "solana"
# Per-tx getTransaction is the whole cost on Solana. Fetch each relayer's backlog
# through a bounded pool (a keyed RPC handles it) instead of one-at-a-time — the
# serial fetch made one busy relayer take ~50 min and blow the run timeout.
SOLANA_WORKERS = int(os.environ.get("SOLANA_WORKERS", "10"))
SOLANA_CHUNK = int(os.environ.get("SOLANA_CHUNK", "400"))
# Budgets keep one high-volume relayer from starving the other 24: cap wall-time
# per relayer, and total Solana time per run. Cursors persist, so a cut relayer
# resumes next run exactly where it stopped.
RELAYER_BUDGET_S = float(os.environ.get("SOLANA_RELAYER_BUDGET_S", "240"))
RUN_BUDGET_S = float(os.environ.get("SOLANA_RUN_BUDGET_S", "2400"))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def relayers() -> list[str]:
    return json.load(open(REGISTRY))["solana_relayers"]


def index_relayer(client: SolanaClient, store: Store, relayer: str,
                  bootstrap_pages: int = 1, deadline: float | None = None) -> int:
    """Index settlements for one relayer's signatures newer than its cursor.

    Txs are fetched CONCURRENTLY (bounded pool) but decoded/committed strictly in
    CHRONOLOGICAL order, and the cursor advances only past fully-processed
    signatures — so a crash, timeout, or budget cut resumes exactly where it
    stopped, never skipping. Bounded by a per-relayer wall-clock budget (and an
    optional global `deadline`) so one busy relayer can't eat the whole run."""
    last_sig, _ = store.get_solana_cursor(relayer)
    if last_sig is None:
        # FRESH relayer: bootstrap forward from now — take only the most recent
        # page(s), not the entire signature history (100k+ for a busy relayer).
        # Full history is an explicit backfill. Mirrors the Base forward-bootstrap.
        recent = client._call("getSignaturesForAddress",
                              [relayer, {"limit": 1000 * bootstrap_pages}])
        if not recent:
            return 0
        sigs = list(reversed(recent))   # chronological
    else:
        sigs = client.signatures_since(relayer, last_sig)
    if not sigs:
        return 0
    written = 0
    start = time.monotonic()
    for i in range(0, len(sigs), SOLANA_CHUNK):
        if time.monotonic() - start > RELAYER_BUDGET_S:
            break
        if deadline is not None and time.monotonic() > deadline:
            break
        chunk = sigs[i:i + SOLANA_CHUNK]
        # concurrently fetch only the txs we need (failed sigs carry no
        # settlements — skip the fetch but still advance the cursor past them)
        need = [s["signature"] for s in chunk if s.get("err") is None]
        fetched: dict = {}
        with ThreadPoolExecutor(max_workers=SOLANA_WORKERS) as ex:
            futs = {ex.submit(client.get_transaction, sig): sig for sig in need}
            for fut in as_completed(futs):
                sig = futs[fut]
                try:
                    fetched[sig] = fut.result()
                except SolAuthError:
                    raise                 # one key serves all — abort up to run()
                except Exception:
                    fetched[sig] = None   # transient — treat as not-yet-available
        # commit strictly in order; stop at the first unavailable tx so we resume
        # from exactly there next run (no gap, no skip)
        stop = False
        for s in chunk:
            sig = s["signature"]
            if s.get("err") is None:
                tx = fetched.get(sig)
                if tx is None:
                    stop = True
                    break
                rows = decode_solana_settlements(tx, sig)
                if rows:
                    written += store.commit_solana_batch(rows)
            store.set_solana_cursor(relayer, sig, s.get("slot"), utcnow())
        if stop:
            break
    return written


def run(client: SolanaClient, store: Store) -> None:
    rels = relayers()
    # Attack the MOST-BEHIND relayers first (oldest / absent cursor). When the run
    # budget can't clear all 25 in one pass, this rotation means no relayer is
    # perpetually starved — each run works down whoever is furthest behind.
    stamps = dict(store.db.execute(
        "SELECT relayer, updated_at FROM solana_cursors").fetchall())
    rels = sorted(rels, key=lambda r: stamps.get(r) or "")
    print(f"[sol] indexing {len(rels)} relayers (concurrency {SOLANA_WORKERS})")
    deadline = time.monotonic() + RUN_BUDGET_S
    total = 0
    for i, r in enumerate(rels):
        if time.monotonic() > deadline:
            print(f"[sol] run budget {RUN_BUDGET_S:.0f}s reached; {len(rels) - i} "
                  f"relayers deferred to next run (cursors preserved)")
            break
        try:
            n = index_relayer(client, store, r, deadline=deadline)
        except SolAuthError as e:
            # one key serves every relayer — a rejected key can't succeed for
            # the next one, so stop the sweep instead of burning 25 retry loops
            print(f"[sol] ABORT: RPC rejected the API key ({e}). Fix "
                  f"X402_SOLANA_RPC; all cursors preserved, next run resumes.")
            break
        except Exception as e:
            print(f"[sol] relayer {r[:10]} FAILED: {e} (cursor preserved; "
                  f"will resume next run)")
            continue
        total += n
        if n:
            print(f"[sol] {i+1}/{len(rels)} {r[:10]}… +{n} settlements")
    print(f"[sol] done: {total} settlements this run")
    _stamp_coverage_start(store)


def _stamp_coverage_start(store: Store) -> None:
    """Record WHEN Solana entered the dataset + its earliest indexed slot, once.

    Solana was bootstrapped forward at deploy (no prior history), so a snapshot
    showing `solana: 0` before this date means NOT-YET-COVERED, not no-activity.
    This stamp makes that unambiguous for any future aggregate. Idempotent: set
    only on the first run that has Solana rows."""
    if store.get_meta("solana_coverage_start_slot") is not None:
        return
    mn = store.chain_min_block("solana")
    if mn is None:
        return
    store.set_meta("solana_coverage_start_slot", str(mn))
    store.set_meta("solana_coverage_start_date",
                   datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    print(f"[sol] coverage-start stamped: slot {mn} "
          f"@ {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")


def cmd_status(store: Store) -> None:
    st = store.stats(CHAIN)
    cursors = store.db.execute(
        "SELECT relayer,last_slot,updated_at FROM solana_cursors "
        "ORDER BY updated_at DESC").fetchall()
    print(json.dumps({**st, "relayers_with_cursor": len(cursors)}, indent=2))
    for r, slot, ts in cursors[:10]:
        print(f"  {r[:12]}… slot={slot} @ {ts}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=DEFAULT_RPC)
    ap.add_argument("--to-head", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()
    store = open_store(Path(args.db))
    if args.status:
        cmd_status(store)
        store.close()
        return
    if args.to_head:
        run(SolanaClient(args.rpc), store)
    else:
        print("specify --to-head or --status")
    store.close()


if __name__ == "__main__":
    main()

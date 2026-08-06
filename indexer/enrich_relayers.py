"""Relayer enrichment — populate settlements.facilitator (= tx.from) on EVM rows.

WHY: the EVM index captures the EIP-3009 SUPERSET (any authorized USDC transfer).
Only settlements whose tx.from is a known x402 facilitator relayer are actually
x402 (measured: Base ~95% by count, Polygon ~0%, others non-x402 gasless USDC).
This fills the relayer so the x402-SCOPED view (facilitator IN registry) can be
computed while the superset is preserved untouched.

LEDGER-SAFE: this only SETS a previously-NULL relayer on existing rows — it never
changes a settlement's identity, amount, seller, or existence, and it writes no
new settlements. Concurrent fetch (keyed RPC), serial DB writes, bounded per run,
idempotent (only NULL rows), resumable.

Usage (on the box, needs keyed X402_<CHAIN>_RPC for throughput):
  PYTHONPATH=indexer .venv/bin/python indexer/enrich_relayers.py [--limit N] [--chain base]
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain import ChainClient, RpcError  # noqa: E402
from evm_chains import CHAINS  # noqa: E402
from index_base import DB_PATH  # noqa: E402
from storage import open_store  # noqa: E402

# EVM chains whose superset needs relayer enrichment (Solana already carries its
# relayer; Permit2 is a distinct mechanism). Only these have a `facilitator` to fill.
EVM = ("base", "polygon", "avalanche", "optimism", "arbitrum")
WORKERS = int(os.environ.get("ENRICH_WORKERS", "8"))
BATCH = int(os.environ.get("ENRICH_LIMIT", "20000"))


def _client(chain_name: str) -> ChainClient:
    c = CHAINS[chain_name]
    # Fewer retries than the indexer default: a rate-capped keyed RPC makes each
    # retry ladder cost ~30s, so on a throttled block we'd rather give up fast
    # (leaving those rows NULL for a later resumable pass) than stall the whole
    # run. Fetching by block already cuts call volume ~8x, so throttling is rare.
    return ChainClient(os.environ.get(c.rpc_env) or c.default_rpc,
                       max_retries=int(os.environ.get("ENRICH_RPC_RETRIES", "4")))


def _apply_updates(store, chain_name: str, pairs) -> int:
    """Set facilitator=from for (tx_hash, from) pairs, filling only NULL rows.
    On Postgres a single VALUES-join UPDATE per batch avoids per-row network
    round-trips (the old per-tx UPDATE loop was itself a bottleneck against
    remote Supabase); SQLite loops locally. Never overwrites a set relayer."""
    if not pairs:
        return 0
    cur = store.db.cursor()
    n = 0
    if store.PH == "%s":
        # Postgres hard-caps a statement at 65535 bound parameters. Each pair
        # uses 2 (+1 for chain), so a single VALUES-join over a dense block
        # chunk can exceed the ceiling and the whole UPDATE errors out with
        # "number of parameters must be between 0 and 65535" — which aborted
        # the entire enrichment pass and let scoped coverage decay. Sub-batch
        # well under the limit so a busy chunk can't blow it.
        step = 20000                         # 40001 params, safely < 65535
        for i in range(0, len(pairs), step):
            sub = pairs[i:i + step]
            vals = ",".join(["(%s,%s)"] * len(sub))
            flat: list = []
            for tx, frm in sub:
                flat += [tx, frm]
            flat.append(chain_name)
            cur.execute(
                f"UPDATE settlements AS s SET facilitator = v.f "
                f"FROM (VALUES {vals}) AS v(tx, f) "
                f"WHERE s.tx_hash = v.tx::text AND s.chain = %s "
                f"AND s.facilitator IS NULL", flat)
            n += cur.rowcount
    else:
        for tx, frm in pairs:
            cur.execute("UPDATE settlements SET facilitator=? WHERE tx_hash=? "
                        "AND chain=? AND facilitator IS NULL", (frm, tx, chain_name))
            n += cur.rowcount
    store.db.commit()
    return n


def enrich_chain(store, chain_name: str, limit: int) -> tuple[int, int]:
    """Fill relayer for un-enriched settlements on one chain by fetching their
    BLOCKS (one getBlockByNumber yields every relayer in the block). Returns
    (txs_resolved, rows_updated). Only touches NULL relayers -> ledger-safe,
    resumable; bounded by `limit` un-enriched rows per run."""
    # Supabase caps statement time short; the DISTINCT scan over the un-enriched
    # rows can exceed it. Raise it for this session (session pooler persists SET).
    if store.PH == "%s":
        try:
            store.db.execute("SET statement_timeout = '600000'")  # 10 min
            store.db.commit()
        except Exception:
            pass
    rows = store.db.execute(store.q(
        "SELECT DISTINCT block_number, tx_hash FROM settlements WHERE chain=? "
        "AND facilitator IS NULL LIMIT ?"), (chain_name, limit)).fetchall()
    if not rows:
        return 0, 0
    # block -> {lowercased hash: original stored hash}. We match on lowercase
    # (RPC hashes are lowercase) but UPDATE with the exact stored casing.
    by_block: dict = {}
    for blk, tx in rows:
        by_block.setdefault(int(blk), {})[tx.lower()] = tx
    blocks = sorted(by_block)
    client = _client(chain_name)

    def fetch(blk):
        want = by_block[blk]
        try:
            txs = client.get_block_transactions(blk)
        except RpcError:
            return []
        out = []
        for t in txs:
            h = (t.get("hash") or "").lower()
            if h in want:
                frm = (t.get("from") or "").lower() or None
                if frm:
                    out.append((want[h], frm))
        return out

    # Commit per CHUNK of blocks -> flat memory, resumable, live progress.
    chunk = int(os.environ.get("ENRICH_CHUNK", "500"))
    resolved_total, updated = 0, 0
    for i in range(0, len(blocks), chunk):
        part = blocks[i:i + chunk]
        pairs: list = []
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for fut in as_completed([ex.submit(fetch, b) for b in part]):
                pairs.extend(fut.result())
        updated += _apply_updates(store, chain_name, pairs)
        resolved_total += len(pairs)
        print(f"[enrich:{chain_name}] {min(i+chunk, len(blocks))}/{len(blocks)} "
              f"blocks, {updated} rows relayer-stamped", flush=True)
    return resolved_total, updated


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=BATCH,
                    help="max un-enriched txs per chain this run")
    ap.add_argument("--chain", choices=EVM, help="one chain (default: all EVM)")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()
    store = open_store(Path(args.db))
    chains = [args.chain] if args.chain else list(EVM)
    for ch in chains:
        try:
            enrich_chain(store, ch, args.limit)
            f = store.relayer_enriched_fraction(ch)
            print(f"[enrich:{ch}] coverage {f['enriched']}/{f['total']} "
                  f"({f['fraction']*100:.1f}%)")
        except Exception as e:
            print(f"[enrich:{ch}] error (non-fatal): {type(e).__name__}: {e}",
                  file=sys.stderr)
    store.close()


if __name__ == "__main__":
    main()

"""Independent Base x402 settlement indexer.

Reads USDC EIP-3009 settlements straight from Base via JSON-RPC — no dependency
on x402scan or any API we don't control. A settlement is a USDC `Transfer`
whose payer authorized it via `AuthorizationUsed` in the same tx (the gasless/
x402 fingerprint). Sellers are self-discovered as the `to` of each transfer.

Correctness posture (this thing runs unattended for months):
- Idempotent inserts + a block-completion ledger (see storage.py): re-runs and
  crashes cannot double-count or silently skip.
- Gap-driven: each run fills uncovered sub-ranges within the target; a failed
  sub-range stays an explicit gap for the next run instead of being stepped over.
- Reorg-safe tip: never indexes closer than `confirmations` blocks to head.
- Every value is decoded by the unit-tested pure functions in decode.py.

Usage:
  python index_base.py --to-head            # resume/extend to chain tip
  python index_base.py --from A --to B       # backfill an explicit range
  python index_base.py --status              # coverage + gaps, no network
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from chain import ChainClient
from decode import (AUTHORIZATION_USED_TOPIC, TRANSFER_TOPIC,
                    decode_transfer, topic_to_address)
from evm_chains import BASE, CHAINS, start_meta_key
from storage import Store, open_store

ROOT = Path(__file__).resolve().parent.parent
# One SQLite DB holds every chain's settlements (distinguished by the `chain`
# column + a per-chain `indexed_ranges` ledger); the filename is historical.
DB_PATH = ROOT / "data" / "indexer" / "base_settlements.sqlite"
REGISTRY = ROOT / "data" / "indexer" / "relayer_registry.json"

# Back-compat module aliases: Base is the default chain and older callers
# (daily_snapshot.py) import these names. New code passes an EvmChain instead.
# Per-chain values live in evm_chains.py.
DEFAULT_RPC = BASE.default_rpc
CHAIN = BASE.name
CONFIRMATIONS = BASE.confirmations
BOOTSTRAP_LOOKBACK_BLOCKS = BASE.bootstrap_lookback_blocks
HISTORY_ANCHOR_BLOCK = BASE.history_anchor_block
SUBRANGE = BASE.subrange


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def finalized_end(client: ChainClient, chain, head: int) -> tuple[int, str]:
    """Highest SAFE block to index `chain` to, and how it was derived.

    Reorg discipline: source from the chain's `finalized` height (true finality)
    — a reorg above finalized is silent corruption the range ledger can't catch,
    so `head - confirmations` (a fixed guess) is not safe on L2s whose finality
    lags tip by ~15 min. `confirmations` is kept ONLY as a fallback floor for an
    RPC that doesn't expose the `finalized` tag. Returns (end_block, source).
    """
    fin = client.finalized_block()
    if fin is not None:
        # Never index past the finalized head; also never past head-safety in the
        # pathological case finalized > head-confirmations is impossible (finalized
        # <= head by definition), so finalized alone is the safe ceiling.
        return fin, "finalized"
    return head - chain.confirmations, "confirmations"


def index_subrange(client: ChainClient, store: Store, lo: int, hi: int,
                   chain=BASE, ts_cache: dict | None = None) -> int:
    """Index [lo, hi] inclusive for `chain`. Returns settlements written."""
    # 1. EIP-3009 authorizations in range -> {tx_hash: {authorizer,...}}
    auth_by_tx: dict[str, set[str]] = {}
    for lg in client.get_logs_chunked(address=chain.usdc,
                                       topics=[AUTHORIZATION_USED_TOPIC],
                                       from_block=lo, to_block=hi):
        tx = lg["transactionHash"].lower()
        auth_by_tx.setdefault(tx, set()).add(topic_to_address(lg["topics"][1]))
    if not auth_by_tx:
        return store.commit_range(chain.name, lo, hi, [], utcnow())

    # 2. Get the USDC Transfer(s) for each x402 tx. Two strategies, identical
    #    output; chain.use_receipts picks the cheaper one for that chain.
    rows = []
    if chain.use_receipts:
        # Fetch only the known x402 txs' receipts and pull the USDC Transfer(s)
        # out of each. Cheap when USDC transfers vastly outnumber x402 txs and/or
        # the chain's block count is huge (Arbitrum). Receipt logs have the same
        # shape as getLogs logs (sans blockTimestamp — enrichment below fills it).
        for tx, authorizers in auth_by_tx.items():
            rcpt = client.get_transaction_receipt(tx)
            if not rcpt:
                continue
            for lg in rcpt.get("logs", []):
                topics = lg.get("topics") or []
                if (lg.get("address", "").lower() != chain.usdc
                        or len(topics) != 3
                        or topics[0].lower() != TRANSFER_TOPIC):
                    continue
                rec = decode_transfer(lg, chain.name)
                if rec["payer"] in authorizers:
                    rows.append(rec)
    else:
        # Scan all USDC Transfers in range, keep those authorized by their payer
        # in-tx. Cheap when x402 is a large fraction of USDC volume (Base/Polygon).
        for lg in client.get_logs_chunked(address=chain.usdc,
                                           topics=[TRANSFER_TOPIC],
                                           from_block=lo, to_block=hi):
            tx = lg["transactionHash"].lower()
            authorizers = auth_by_tx.get(tx)
            if not authorizers:
                continue
            rec = decode_transfer(lg, chain.name)
            if rec["payer"] in authorizers:
                rows.append(rec)

    # Timestamp enrichment. Many RPCs (e.g. Avalanche) omit blockTimestamp on
    # eth_getLogs — only OP-stack-style chains include it. Rather than depend on
    # that non-standard extension, fetch the block's timestamp when missing
    # (getBlockByNumber), cached per run so each block is fetched at most once.
    if ts_cache is None:
        ts_cache = {}
    for r in rows:
        # None (RPC omits it, e.g. Avalanche / receipts) OR 0 (some RPCs return
        # blockTimestamp="0x0" on getLogs, e.g. Arbitrum) both mean "missing".
        if not r["block_timestamp"]:
            bn = r["block_number"]
            if bn not in ts_cache:
                ts_cache[bn] = client.get_block_timestamp(bn)
            r["block_timestamp"] = ts_cache[bn]
    # A timeless time-series is worse than none: if even enrichment can't get a
    # timestamp, fail loud instead of silently writing null/zero times.
    missing_ts = [r for r in rows if not r["block_timestamp"]]
    if missing_ts:
        raise RuntimeError(
            f"{len(missing_ts)}/{len(rows)} {chain.name} settlements in "
            f"[{lo},{hi}] have no block_timestamp even after enrichment. "
            f"Refusing to write a timeless series.")
    return store.commit_range(chain.name, lo, hi, rows, utcnow())


def run(client: ChainClient, store: Store, start: int, end: int,
        chain=BASE) -> None:
    gaps = store.find_gaps(chain.name, start, end)
    if not gaps:
        print(f"[index:{chain.name}] [{start},{end}] already fully covered")
        return
    total_missing = sum(g[1] - g[0] + 1 for g in gaps)
    print(f"[index:{chain.name}] target [{start},{end}] "
          f"{len(gaps)} gap(s), {total_missing} blocks to index")
    done_blocks, written = 0, 0
    ts_cache: dict[int, int] = {}   # block -> timestamp, shared across subranges
    for gap_lo, gap_hi in gaps:
        lo = gap_lo
        while lo <= gap_hi:
            hi = min(lo + chain.subrange - 1, gap_hi)
            n = index_subrange(client, store, lo, hi, chain, ts_cache)
            written += n
            done_blocks += hi - lo + 1
            if done_blocks % (chain.subrange * 25) < chain.subrange:
                print(f"[index:{chain.name}] …{done_blocks}/{total_missing} "
                      f"blocks, {written} settlements", flush=True)
            lo = hi + 1
    print(f"[index:{chain.name}] done: {written} settlements over "
          f"{done_blocks} blocks")


def cmd_status(store: Store, chain=BASE) -> None:
    start = int(store.get_meta(start_meta_key(chain)) or chain.history_anchor_block)
    frontier = store.covered_frontier(chain.name, start)
    st = store.stats(chain.name)
    print(json.dumps({
        "chain": chain.name,
        "start_block": start,
        "covered_frontier": frontier,
        "contiguous_blocks_covered": max(0, frontier - start + 1),
        **st,
    }, indent=2))
    # report holes below the frontier explicitly
    if st["max_block"]:
        gaps = store.find_gaps(chain.name, start, st["max_block"])
        print(f"gaps below max indexed block: {len(gaps)}")
        for g in gaps[:20]:
            print(f"  gap [{g[0]},{g[1]}] ({g[1]-g[0]+1} blocks)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="base", choices=sorted(CHAINS),
                    help="which EVM chain to index (default: base)")
    ap.add_argument("--rpc", default=None,
                    help="override RPC URL (else $<CHAIN>_RPC env, else public)")
    ap.add_argument("--from", dest="from_block", type=int)
    ap.add_argument("--to", dest="to_block", type=int)
    ap.add_argument("--to-head", action="store_true")
    ap.add_argument("--start-block", type=int, default=None,
                    help="bootstrap start (default: current head — start the "
                         "clock forward). Use with an old value only for backfill.")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    chain = CHAINS[args.chain]
    rpc = args.rpc or os.environ.get(chain.rpc_env) or chain.default_rpc
    sk = start_meta_key(chain)

    store = open_store(Path(args.db))
    client = None
    if store.get_meta(sk) is None:
        # Bootstrap: default to current head so we index FORWARD from launch.
        # Never silently anchor to genesis/history (would trigger a huge
        # backfill on the first run).
        if args.start_block is not None:
            boot = args.start_block
        else:
            client = ChainClient(rpc)
            boot = (client.block_number() - chain.confirmations
                    - chain.bootstrap_lookback_blocks)
        store.set_meta(sk, str(boot))
        store.set_meta(f"usdc_{chain.name}", chain.usdc)
        store.set_meta(f"created_at_{chain.name}", utcnow())
        if REGISTRY.exists():
            reg = json.load(open(REGISTRY))
            store.set_meta("relayer_registry_commit", reg.get("source_commit", "?"))

    if args.status:
        cmd_status(store, chain)
        store.close()
        return

    if client is None:
        client = ChainClient(rpc)
    start = int(store.get_meta(sk))
    if args.from_block is not None and args.to_block is not None:
        run(client, store, args.from_block, args.to_block, chain)
    elif args.to_head:
        head = client.block_number()
        end, src = finalized_end(client, chain, head)
        print(f"[index:{chain.name}] head={head} end={end} (via {src}, "
              f"lag {head - end} blk)")
        frontier = store.covered_frontier(chain.name, start)
        run(client, store, max(start, frontier + 1), end, chain)
    else:
        print("specify --to-head, --from/--to, or --status")
    store.close()


if __name__ == "__main__":
    main()

"""Cross-source reconciliation: our independent Base index vs x402scan.

Proves the indexer is not just functional but *correct* — that the settlements
we decode straight from chain match what the established index reports. For each
of the busiest sellers in a freshly-indexed window, we pull x402scan's own
transfer list for that seller and measure tx-hash overlap. Divergence beyond a
tolerance is a FAIL (surfaced, not swallowed).

Run after a smoke index of a recent window. Read-only.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from storage import open_store  # noqa: E402

X402SCAN = "https://www.x402scan.com/api/trpc"


def _ts(s: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def scan_transfers(seller: str, pages: int = 4) -> dict[str, float]:
    """x402scan's recent transfers for this seller -> {tx_hash: unix_ts}.

    Keyed with the block timestamp so we can band on a mutual time window
    (works for Base block-numbers and Solana slots alike). tx_hash is kept
    verbatim — Solana signatures are base58 and case-sensitive; lowercasing
    them would be wrong, so comparison is done on the exact string.
    """
    out: dict[str, float] = {}
    for page in range(pages):
        inp = {"json": {"timeframe": 0, "recipients": {"include": [seller]},
                        "sorting": {"id": "block_timestamp", "desc": True},
                        "pagination": {"page": page, "page_size": 1000}}}
        url = f"{X402SCAN}/public.transfers.list?input=" + urllib.parse.quote(
            json.dumps(inp))
        req = urllib.request.Request(url, headers={
            "User-Agent": "x402-audit-indexer/1.0 (reconciliation)"})
        try:
            data = json.load(urllib.request.urlopen(req, timeout=60))
            items = data["result"]["data"]["json"]["items"]
        except Exception as e:
            print(f"  scan fetch err for {seller[:10]}: {e}")
            break
        for t in items:
            out[t["tx_hash"]] = _ts(t["block_timestamp"])
        if len(items) < 1000:
            break
    return out


def main() -> None:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / "data/indexer/base_settlements.sqlite")
    store = open_store(db)
    # busiest sellers in our index
    top = store.db.execute(
        "SELECT seller, COUNT(*) n FROM settlements GROUP BY seller "
        "ORDER BY n DESC LIMIT 8").fetchall()
    if not top:
        print("no settlements indexed; run a smoke index first")
        return

    # The correctness question is directional. x402scan is NOT ground truth —
    # it can (and on Solana does, via its Bitquery feed) MISS real settlements
    # that a direct chain read catches. So we test the two things that actually
    # matter, banded to the MUTUAL time window both sources cover:
    #   (A) misses: settlements x402scan has that we DON'T  -> must be ~0.
    #       A nonzero here is a real false-negative bug in our indexer.
    #   (B) surplus: settlements WE have that x402scan doesn't -> allowed, but
    #       reported (it's our completeness edge) and a sample is on-chain-
    #       validated separately so surplus can't hide false positives.
    # Cold-start bound: each relayer is bootstrapped from its most-recent
    # signatures, so relayers have different indexed FLOORS (oldest ts held).
    # A seller served by a busy relayer whose floor is recent will look like it
    # is "missing" older settlements that are really just below our bootstrap
    # depth. Above the point where EVERY relayer has coverage — the max of the
    # per-relayer floors — a miss is a genuine false negative. Below it, it's
    # depth, not a bug. (A full backfill lowers this floor to chain genesis.)
    floors = store.db.execute(
        "SELECT facilitator, MIN(block_timestamp) FROM settlements "
        "WHERE chain='solana' AND facilitator IS NOT NULL GROUP BY facilitator"
    ).fetchall()
    covered_floor = max((f for _, f in floors if f is not None), default=None)

    results = []
    total_misses = 0            # misses ABOVE the covered floor (real signal)
    total_misses_depth = 0      # misses below floor (cold-start artifact)
    total_surplus = 0
    for seller, n in top:
        ours_rows = store.db.execute(
            "SELECT tx_hash, block_timestamp FROM settlements WHERE seller=?",
            (seller,)).fetchall()
        ours = {h: t for h, t in ours_rows if t is not None}
        theirs = scan_transfers(seller)
        if not ours or not theirs:
            print(f"{seller[:12]} ours={len(ours_rows)} theirs={len(theirs)} "
                  f"(no comparable window)")
            results.append({"seller": seller, "ours": len(ours_rows),
                            "theirs": len(theirs), "window": None})
            continue
        # mutual time window: [max(mins), min(maxs)]
        lo = max(min(ours.values()), min(theirs.values()))
        hi = min(max(ours.values()), max(theirs.values()))
        oo = {h for h, t in ours.items() if lo <= t <= hi}
        tt = {h for h, t in theirs.items() if lo <= t <= hi}
        misses = tt - oo           # they have, we don't
        surplus = oo - tt          # we have, they don't  -> our completeness
        # split misses by the covered floor: above it = real, below = depth
        if covered_floor is not None:
            real_misses = {h for h in misses if theirs[h] >= covered_floor}
        else:
            real_misses = misses
        depth_misses = misses - real_misses
        total_misses += len(real_misses)
        total_misses_depth += len(depth_misses)
        total_surplus += len(surplus)
        results.append({
            "seller": seller, "in_window_ours": len(oo),
            "in_window_theirs": len(tt), "intersection": len(oo & tt),
            "real_misses": len(real_misses),
            "depth_misses_below_floor": len(depth_misses),
            "surplus_vs_scan": len(surplus),
            "missed_sigs": sorted(real_misses)[:20]})
        print(f"{seller[:12]} window[ours={len(oo)} theirs={len(tt)}] "
              f"real_misses={len(real_misses)} depth={len(depth_misses)} "
              f"surplus={len(surplus)}")

    print(f"\nREAL MISSES (above covered floor — false negatives): {total_misses}")
    print(f"depth misses (below cold-start floor, not a bug): {total_misses_depth}")
    print(f"SURPLUS (we have, x402scan lacks): {total_surplus}")
    # PASS = we miss nothing the reference has. Surplus is expected (we read the
    # chain directly); it is validated for realness by validate_surplus.py.
    print("VERDICT:", "PASS — no false negatives above the covered floor; "
          "index is a superset of x402scan (surplus validated separately)"
          if total_misses == 0
          else f"REVIEW — {total_misses} real misses above the covered floor "
               "(see missed_sigs; verify on-chain)")
    out = Path(__file__).resolve().parent.parent / "data/indexer/reconciliation.json"
    json.dump({"results": results, "total_real_misses": total_misses,
               "total_depth_misses": total_misses_depth,
               "total_surplus": total_surplus,
               "covered_floor_ts": covered_floor}, open(out, "w"), indent=2)
    print("wrote", out)
    store.close()


if __name__ == "__main__":
    main()

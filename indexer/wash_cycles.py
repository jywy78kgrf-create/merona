"""Wash v3 — cycle detection over the scoped settlement graph.

Why (docs/WASH_V3_SPEC.md): every v2 check is depth ≤ 2, and a depth-3
payment ring (Polygon, 2026-07-31: lnpay hub → leg-2 origin → 30-wallet
cluster → back to hub) passed all of them while accounting for ~100% of the
chain's "clean" figure. For a fixed detection depth d, a manufacturer wins
with a cycle of length d+1 — so v3 detects cycles of ANY length instead of
adding another special case.

Two independent signals, combined per the spec's false-positive discipline:

  1. STRUCTURAL — membership in a non-trivial strongly connected component
     of the payer→seller graph. Every node of a non-trivial SCC lies on some
     directed cycle (of arbitrary length), so SCC membership is the complete
     "is on a cycle" test; bounded simple-cycle enumeration (length ≤ k)
     then documents the concrete cycles as evidence rows. This is a
     deliberate strengthening of the spec's "cycle of length ≤ k" wording:
     flags cannot be dodged by making the loop one hop longer than k.
  2. BEHAVIORAL — net-flow balance. A carousel node must pass on what it
     receives (inflow ≈ outflow). Computed from the same edge list.

  FLAG  = in a non-trivial SCC  AND  balanced (ratio < thresh, floor $).
  WARN  = balanced only (legit pass-throughs also balance — never flagged
          on balance alone) or on-cycle only (e.g. two real merchants that
          genuinely buy from each other).

Scope matches clean metrics exactly: settlements whose facilitator is a
registry relayer, witnessed regime, payer<>seller (self-pay is the depth-1
check and is excluded globally elsewhere).

Pure SQL + in-memory graph; no RPC. All parameters env-tunable and recorded
in every output row. Known-positive acceptance case: the Polygon ring — a
build of this module that does not flag those 32 wallets is not done
(tests/test_wash_cycles.py holds the synthetic replica).
"""
from __future__ import annotations

import hashlib
import json
import os

# Detection knobs (env-overridable; recorded in output rows).
CYCLE_K = int(os.environ.get("WASH_CYCLE_K", "5"))
MIN_EDGE_USD = float(os.environ.get("WASH_CYCLE_MIN_USD", "1"))
BALANCE_THRESH = float(os.environ.get("WASH_BALANCE_THRESH", "0.05"))
BALANCE_MIN_USD = float(os.environ.get("WASH_BALANCE_MIN_USD", "100"))
MAX_CYCLES = int(os.environ.get("WASH_CYCLE_MAX", "5000"))
MAX_SCC_ENUM = int(os.environ.get("WASH_SCC_ENUM_MAX", "4000"))


def params() -> dict:
    return {"k": CYCLE_K, "min_edge_usd": MIN_EDGE_USD,
            "balance_thresh": BALANCE_THRESH,
            "balance_min_usd": BALANCE_MIN_USD}


def _edges(store, chain: str, relayers: list[str], wb: int) -> dict:
    """Collapsed payer→seller edge list over the scoped, witnessed window.
    {(payer, seller): usd}; edges below the dust threshold dropped so noise
    cannot manufacture cycles."""
    ph = ",".join(["?"] * len(relayers))
    rows = store.db.execute(store.q(
        f"SELECT payer, seller, {store.volume_agg()} FROM settlements "
        f"WHERE chain=? AND facilitator IN ({ph}) AND block_number >= ? "
        f"AND payer <> seller GROUP BY payer, seller"),
        (chain, *relayers, wb)).fetchall()
    out = {}
    for p, s, v in rows:
        usd = float(store._norm_vol(v)) / 1e6
        if usd >= MIN_EDGE_USD:
            out[(p, s)] = usd
    return out


def _core(edges: dict) -> dict:
    """Prune to the subgraph where every node has both in- and out-edges
    (necessary for cycle membership); returns adjacency {node: {succ}}.
    Iterates until stable — most of the graph is fan-in and drops out."""
    adj: dict = {}
    radj: dict = {}
    for (p, s) in edges:
        adj.setdefault(p, set()).add(s)
        radj.setdefault(s, set()).add(p)
    while True:
        dead = [n for n in set(adj) | set(radj)
                if not adj.get(n) or not radj.get(n)]
        if not dead:
            return adj
        for n in dead:
            for s in adj.pop(n, ()):  # detach from successors' radj
                r = radj.get(s)
                if r:
                    r.discard(n)
            for p in radj.pop(n, ()):
                a = adj.get(p)
                if a:
                    a.discard(n)


def _sccs(adj: dict) -> list[set]:
    """Iterative Tarjan; returns non-trivial SCCs (size ≥ 2) only.
    (A single node in this pruned graph without a self-edge is acyclic;
    self-edges cannot exist here because payer<>seller edges only.)"""
    index: dict = {}
    low: dict = {}
    on: set = set()
    stack: list = []
    out: list = []
    counter = [0]
    for root in adj:
        if root in index:
            continue
        work = [(root, iter(sorted(adj.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for succ in it:
                if succ not in adj:
                    continue
                if succ not in index:
                    index[succ] = low[succ] = counter[0]
                    counter[0] += 1
                    stack.append(succ)
                    on.add(succ)
                    work.append((succ, iter(sorted(adj.get(succ, ())))))
                    advanced = True
                    break
                if succ in on:
                    low[node] = min(low[node], index[succ])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = set()
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.add(w)
                    if w == node:
                        break
                if len(comp) >= 2:
                    out.append(comp)
    return out


def _enumerate_cycles(adj: dict, comp: set, k: int, cap: int) -> list[list]:
    """Simple cycles of length ≤ k inside one SCC, each rooted at (and
    reported starting from) its lexicographically smallest node so every
    cycle is enumerated exactly once. Bounded DFS; stops at `cap` total."""
    cycles: list = []
    nodes = sorted(comp)
    for start in nodes:
        if len(cycles) >= cap:
            break
        # DFS visiting only nodes > start (start is the canonical minimum)
        path = [start]
        iters = [iter(sorted(n for n in adj.get(start, ()) if n in comp))]
        inpath = {start}
        while iters:
            if len(cycles) >= cap:
                break
            nxt = next(iters[-1], None)
            if nxt is None:
                inpath.discard(path.pop())
                iters.pop()
                continue
            if nxt == start and len(path) >= 2:
                cycles.append(list(path))
                continue
            if nxt <= start or nxt in inpath or len(path) >= k:
                continue
            path.append(nxt)
            inpath.add(nxt)
            iters.append(iter(sorted(n for n in adj.get(nxt, ()) if n in comp)))
    return cycles


def detect(store, chain: str, relayers: list[str], wb: int = 0) -> dict:
    """Full v3 pass for one chain. Returns:
      flagged        set — SCC-member AND balanced (the hard flag; feeds
                     clean-metrics exclusion and the scores.py grade cap)
      cycle_wallets  set — all nodes in non-trivial SCCs (on some cycle)
      balanced       {wallet: ratio} — near-balanced above the floor (warn)
      cycles         evidence rows for enumerated cycles of length ≤ k
      notes          coverage caveats (enumeration caps hit, etc.)
    """
    edges = _edges(store, chain, relayers, wb)
    inflow: dict = {}
    outflow: dict = {}
    for (p, s), usd in edges.items():
        outflow[p] = outflow.get(p, 0.0) + usd
        inflow[s] = inflow.get(s, 0.0) + usd
    balanced: dict = {}
    for w in set(inflow) | set(outflow):
        i, o = inflow.get(w, 0.0), outflow.get(w, 0.0)
        hi = max(i, o)
        if hi >= BALANCE_MIN_USD:
            r = abs(i - o) / hi
            if r < BALANCE_THRESH:
                balanced[w] = round(r, 5)

    adj = _core(edges)
    comps = _sccs(adj)
    cycle_wallets: set = set().union(*comps) if comps else set()
    flagged = {w for w in cycle_wallets if w in balanced}

    notes: list = []
    cycles: list = []
    budget = MAX_CYCLES
    for comp in sorted(comps, key=len):
        if len(comp) > MAX_SCC_ENUM:
            notes.append(f"scc of {len(comp)} nodes exceeds enum cap "
                         f"{MAX_SCC_ENUM}; members still flagged via "
                         f"SCC+balance, concrete cycles not enumerated")
            continue
        found = _enumerate_cycles(adj, comp, CYCLE_K, budget)
        budget -= len(found)
        for cyc in found:
            vols = [edges[(cyc[i], cyc[(i + 1) % len(cyc)])]
                    for i in range(len(cyc))]
            cycles.append({
                "cycle_id": hashlib.sha256(
                    "|".join(cyc).encode()).hexdigest()[:12],
                "length": len(cyc), "wallets": cyc,
                "float_usd": round(min(vols), 2),      # the actual capital
                "minted_usd": round(sum(vols), 2)})    # the volume it prints
        if budget <= 0:
            notes.append(f"cycle enumeration capped at {MAX_CYCLES}")
            break
    return {"chain": chain, "params": params(), "flagged": flagged,
            "cycle_wallets": cycle_wallets, "balanced": balanced,
            "cycles": cycles, "n_edges": len(edges),
            "n_sccs": len(comps), "notes": notes}


def record(store, date: str, res: dict, measured_at: str) -> None:
    """Persist one chain's pass: evidence rows per enumerated cycle, plus a
    single cycle_id='flagged' row carrying the exact flagged set (with each
    wallet's balance ratio) — the row scores.py caps grades from. Storing the
    flag set explicitly keeps the cap reproducible even when an SCC was too
    large to enumerate."""
    ch = res["chain"]
    for c in res["cycles"]:
        store.record_wash_cycle(date, ch, c["cycle_id"], {
            "length": c["length"], "wallets": c["wallets"],
            "float_usd": c["float_usd"], "minted_usd": c["minted_usd"],
            "params": res["params"]}, measured_at)
    flagged = sorted(res["flagged"])
    store.record_wash_cycle(date, ch, "flagged", {
        "length": None, "wallets": flagged,
        "float_usd": None, "minted_usd": None,
        "params": {**res["params"],
                   "ratios": {w: res["balanced"].get(w) for w in flagged},
                   "notes": res["notes"]}}, measured_at)
    store.db.commit()


def load_flagged(store) -> tuple[dict, str | None, dict | None]:
    """Latest flagged sets for scores.py: ({(chain, wallet)}, date, params).
    Latest-available (not strictly today's) so a failed cycle night degrades
    gracefully; the date used is recorded per score."""
    try:
        d = store.db.execute(
            "SELECT MAX(measured_date) FROM wash_cycles").fetchone()[0]
        if not d:
            return {}, None, None
        out: dict = {}
        prm = None
        for ch, wallets, p in store.db.execute(store.q(
                "SELECT chain, wallets, params FROM wash_cycles "
                "WHERE measured_date=? AND cycle_id='flagged'"), (d,)):
            for w in json.loads(wallets):
                out[(ch, w)] = True
            if prm is None and p:
                prm = {k: v for k, v in json.loads(p).items()
                       if k not in ("ratios", "notes")}
        return out, d, prm
    except Exception:
        return {}, None, None

# Wash v3 — cycle detection + net-flow balance

*Scoped 2026-07-31, after a depth-3 payment ring passed every published
filter and accounted for ~100% of Polygon's "clean" figure. Status: **BUILT
2026-08-01** — `indexer/wash_cycles.py`, wired into `run_wash` (kill switch
`X402_WASH_CYCLES=0`) and `scores.py` (SCORE_VERSION 3). The known-positive
ring is the permanent acceptance test: `indexer/tests/test_wash_cycles.py`.*

**Build deviation (deliberate strengthening):** the flag criterion is
membership in a non-trivial SCC of the scoped payer→seller graph AND
near-balanced net flow — not "in an enumerated cycle of length ≤ k" as
written below. Every node of a non-trivial SCC lies on a directed cycle of
*some* length, so the flag cannot be dodged by making the loop longer than
k; bounded enumeration (k=5) is retained purely to record concrete cycles
as evidence rows (`wash_cycles` table, one row per cycle with float/minted
USD, plus one `cycle_id='flagged'` row per chain carrying the exact flagged
set and each wallet's balance ratio). Balance alone still only warns.*

## What exists today (v2) and why it failed

| Check | Depth | Where |
|---|---:|---|
| self-dealing (payer == seller) | 1 | `self_pay_tx` per seller, every snapshot |
| funded-loop (seller → its own payer → seller) | 2 | `wash.py::fetch_sent_recipients` (EVM), `wash_solana.py` + `funding_solana.py` (SOL) |
| reciprocal pair (A ↔ B) | 2 | `wash.py::_reciprocal_sellers` |

**Every check is depth ≤ 2.** The Polygon ring is depth 3: each node funds
the *next* hop, never its own payer, and no pair reciprocates. It passed
cleanly. The Solana build earlier in July extended the *funded-loop* check to
a third chain — breadth, not depth — so no chain is protected against this.

**Generalization worth stating plainly:** for a fixed detection depth `d`, a
manufacturer defeats every check by using a cycle of length `d+1`. Their cost
is one more wallet; ours is a re-architecture. Depth-limited detection loses
this race by construction, which is why v3 must detect *cycles of arbitrary
length within a bound*, not add another special case.

## Known-positive test case (build against this)

Polygon, window 2026-07-04 → 07-31, scoped to registry facilitators:

```
0x66fa4d79ca84016b42352be33c908dd812952ec8   (lnpay/aisa)
   -> 0x897720c5b19f93f3194990d0121d3a64783bbab8    166 tx x $300.00 = $49,800
   -> 30 wallets (0x002d0d…, 0xf5ddb8…, 0xd997f7…, …)  2,499 tx x $20.00 = $49,980
   -> back to 0x66fa…2ec8                        4,968,264 tx x $0.01 = $49,683
```

Acceptance: v3 flags all 32 wallets and reclassifies ~$143.9K of Polygon
clean volume as manufactured, leaving ~$1.13. A v3 that does not flag this
set is not done.

## Two independent signals (require both, or flag at different severities)

### 1. Cycle detection (structural)

Build the directed payer→seller graph per (chain, window) from `settlements`,
edges weighted by (tx_count, volume). Find simple cycles up to length `k`
(default `k=5`, env `WASH_CYCLE_K`).

- Restrict to scoped settlements (registry facilitators) — the superset graph
  is enormous and mostly non-x402.
- Collapse multi-edges; ignore edges below a dust threshold (`WASH_CYCLE_MIN_USD`,
  default $1) so noise doesn't manufacture cycles.
- Practical algorithm: Johnson's simple-cycle enumeration on the scoped
  subgraph, or bounded DFS from each node with depth `k` and a visited set.
  Scale check first: Polygon scoped is ~43 sellers / ~40 payers — trivial.
  Base scoped is the real target (~80K sellers); expect to need
  strongly-connected-component decomposition first (`tarjan`), then
  enumerate cycles only *within* non-trivial SCCs. Most of the graph is a
  DAG-ish fan-in and drops out immediately.
- A wallet in any detected cycle of length ≤ k is `cycle_flagged`, with the
  cycle id, length, and the min-edge volume (the float size — the actual
  capital, as opposed to the minted volume).

### 2. Net-flow balance (behavioral, cheap, no graph walk)

For each wallet in the window: `inflow_usd` (as seller) and `outflow_usd` (as
payer). A carousel node cannot avoid `inflow ≈ outflow` — it must pass on
what it receives. Compute:

```
balance_ratio = |inflow - outflow| / max(inflow, outflow)
```

Flag when `balance_ratio < WASH_BALANCE_THRESH` (default 0.05) **and** both
sides exceed a floor (`WASH_BALANCE_MIN_USD`, default $100). The Polygon ring
nets −$117, −$180, and +$25×30 on ~$50K legs — ratios of 0.002–0.004.

This signal is O(n) over settlements, needs no RPC, and catches long cycles
(length > k) that enumeration misses. It is the cheap always-on screen; cycle
detection is the expensive confirmatory one.

**False-positive discipline:** a legitimate pass-through (payment processor,
custodial relayer) also balances. That is why balance alone should *warn*,
and balance **+** membership in a detected cycle should *flag*. Publish the
distinction; do not collapse them.

## Integration

- New table `wash_cycles(measured_date, chain, cycle_id, length, wallets,
  float_usd, minted_usd)` and columns on `wash_signals`: `cycle_flagged`,
  `cycle_len`, `balance_ratio`.
- `clean_metrics` subtracts cycle-flagged sellers' volume, same as existing
  flags. Expect Polygon clean → ~$1.13; re-check Base and Solana.
- `scores.py`: cycle membership is a hard cap (grade D at best), same
  treatment as existing wash flags. Bump `SCORE_VERSION` to 3 and record the
  cycle-detection parameters (`k`, thresholds) alongside each score, so a
  published grade is reproducible against the exact detector that produced it.
- Retroactive: re-run over full history, publish a revision note per chain.
  **Do not silently restate** — the never-silently-restated series is the
  product; a correction published loudly is worth more than a number quietly
  fixed.

## Ordering

1. ~~Net-flow balance over the current window, all chains~~ — DONE
   (`analytics/wash_balance.py`).
2. ~~Cycle enumeration on Polygon scoped~~ — DONE; reproduces the ring.
3. ~~Base + Solana scoped, with SCC decomposition~~ — DONE. All three chains
   with a registered facilitator set (base, polygon, solana) run the cycle
   pass nightly. The detector is chain-agnostic (opaque wallet strings, so
   base58 works); Solana flags are recorded ungated so `scores.py` caps a
   Solana ring hub even when the gated Solana clean number is off. The other
   EVM chains (optimism/arbitrum/avalanche) are INDEXED nightly (the EIP-3009
   USDC superset is in the DB) but have zero registered facilitator relayers,
   so they are not scopable — and cycle detection runs only on the scoped
   subgraph. This is a REGISTRY gap, not an absence of activity: per web
   research (2026-08) Coinbase's CDP facilitator supports Arbitrum and
   Questflow (already in our registry for Base) runs a multichain facilitator
   on Base/Optimism/Arbitrum, so x402 traffic likely sits in our superset on
   arb/opt, unscoped and unmeasured, until relayer discovery is run for those
   chains. Volume expected small (Base is ~85% of x402 per Chainalysis) but
   non-zero; Avalanche more marginal. TODO: run build_relayer_registry /
   enrich_relayers discovery on arbitrum + optimism.
4. ~~Wire to `clean_metrics` + `scores.py`~~ — DONE (2026-08-01).
5. Full-history re-run + published revision notes — PENDING (nightly covers
   the witnessed regime; historical rings need the batch pass).

## Remaining gap — cross-chain rings (v3.1)

Detection is per-chain. A ring that pays out on Base and returns on Polygon
(or hops through Solana) is invisible to a per-chain SCC pass: no single
chain's subgraph closes the loop. Closing this needs a unified graph keyed
by address across chains — but EVM↔Solana isn't even the same address space,
so it also needs an identity-linking layer (a wallet controlling both an
EVM `0x…` and a Solana base58 key). Nobody is looking at this today; it is
the honest next frontier, and it is out of scope for v3.

## Open questions

- Should cross-chain cycles be detected (hop on Base, return on Polygon)?
  Nothing prevents them and no one is looking. Requires a unified graph
  keyed by address across chains — v3.1.
- Time-ordering: current spec ignores edge timestamps. A true carousel is
  also *sequential* (A pays B, then B pays C, then C pays A). Adding temporal
  ordering would cut false positives but risks missing pipelined rings that
  run all legs continuously — as this one does. Start without it.

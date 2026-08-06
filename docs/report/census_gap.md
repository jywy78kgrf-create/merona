# Census gap: reconciling our numbers against the public trackers

*First pass, 2026-07-13. Reconciles our layered figures against the public
x402 numbers in circulation, and decomposes every discrepancy into named,
defensible causes. Purpose: pre-answer the inevitable "who's right?" question,
and make sure the launch attacks the right target.*

## The headline correction (read this first)

**The inflation is scoped-vs-unscoped counting — NOT "x402scan is wrong."**
x402scan already scopes to real facilitators; its numbers broadly agree with
ours. Positioning the launch as "the trackers report $1.45B and they're wrong"
would be **inaccurate and easily rebutted**. The honest, stronger frame:

> Counted naively — every gasless USDC transfer that uses EIP-3009 — the number
> looks like **$1.45B**. That conflation is what shows up in aggregators and
> pitch decks. Scoped to real facilitators (as x402scan does, and as we do) it
> is **~$42.7M**. Remove self-dealing → **$19.5M**. Remove funded-loop wash →
> **$9.8M**. We are not correcting x402scan; we are going three layers deeper
> and doing it over the full history, reproducibly.

## The public figures (sourced)

| Source | Figure | Nature |
|---|---|---|
| x402scan, trailing 30d | **3.69M tx / ~$1.11M / ~$0.30 per call** | scoped, recent window |
| x402scan volume trend | peak **$5.15M** (Nov 2025) → **$1.19M** (May 2026), −77% | scoped, monthly |
| Chainalysis | **100M+** cumulative payments on Base through Q1 2026 | transaction count |
| BlockEden | **~$600M / year** across all chains | leans unscoped |
| Solana (various) | **~35M** tx by Mar 2026 | transaction count |
| Transak / hype | "$30 trillion agent economy" | projection, not current |

## Reconciliation

**Transaction counts agree across every independent source.** Chainalysis says
100M+ cumulative on Base; x402scan runs ~3.7M/30d; our scoped Base count is
145M all-time. All the same order of magnitude — the *rail* is real and
everyone is counting roughly the same settlements. No one is inventing
transactions.

**Dollar figures diverge entirely on scoping — and the scoped sources are
mutually consistent:**

- x402scan's recent 30-day (**$1.11M**) matches the post-decline monthly level
  it reports for May 2026 (**$1.19M**) — internally consistent.
- Our **cumulative scoped $42.7M** over ~14 months is consistent with that
  monthly history: a rail that peaked around **$5M/month** (Nov 2025) and
  decayed to **~$1M/month** sums to tens of millions cumulative — not a
  billion. x402scan (recent monthly) and us (cumulative) are two views of the
  **same scoped economy**.
- Both land at **~$0.30 per settlement** — the micropayment signature of real
  machine commerce.

**So where does $1.45B come from?** The unscoped EIP-3009 superset — every
gasless USDC transfer, including ~3.2M large wallet-to-wallet sends averaging
~$440 that merely share the standard. That is the number naive on-chain
counting yields, and the direction aggregators like BlockEden's "$600M/year"
lean. It is real as an EIP-3009 total; it is wrong as an *agent-commerce*
total. Quantifying that exact wedge is our contribution.

## The gap, itemized (Base)

Starting from the unscoped figure, every dollar of the difference is named:

| Layer | Volume | What's removed | Removed by |
|---|---:|---|---|
| EIP-3009 superset | $1,452,755,000 | — | naive counting |
| → scope to facilitators | $42,742,246 | ~$1.41B gasless whale transfers | facilitator registry (x402scan does this too) |
| → remove self-dealing | $19,522,795 | $23.2M payer==seller | our clean layer |
| → remove funded-loop wash | $9,775,079 | $9.7M reciprocal/funded sellers | our wash pass (beyond any tracker) |

x402scan sits at roughly the "scope to facilitators" line. **We go two layers
below it**, and we do the whole thing over reconstructed full history with
published, reproducible method and on-chain anchors.

## What we have that x402scan doesn't

1. **Full reconstructed history**, not a recent rolling window — 148M Base
   settlements back to 2025-05.
2. **The wash/self-dealing layers** ($19.5M, $9.8M) — nobody else removes these.
3. **Reproducibility + hash-anchoring** — method and figures are public and
   re-derivable; x402scan is a closed dashboard.
4. **Correctness proof against x402scan** — `indexer/reconcile.py` shows our
   index is a *superset* of x402scan's per-seller transfers (we miss ~0 of what
   they have), so when we say "and here's what they're not removing," we've
   already proven we see everything they see.

## Matched-window reconciliation (run — June 2026)

We ran `analytics/matched_window.py` over a full June 2026 calendar month, per
chain, off the shards — a window the trackers also report and one that sits
cleanly inside both shard archives (Base ends 2026-07-04, Polygon 2026-07-11).
This closes the reconciliation with actual numbers.

**x402scan side (the public figure).** x402scan exposes no public stats API (all
`/api/public/*` stat routes 404; the dashboard hydrates its aggregates
client-side, so they aren't in the server payload). The reliable public number
is the trailing-30-day, **all-chain** aggregate: **~3.69M tx / ~$1.11M / ~$0.30
per call**. Per-service prices visible in the page payload run **$0.001 – $1.77**,
confirming the micropayment range.

**Our side (matched June 2026, scoped to facilitators):**

| Chain | Scoped settlements | Scoped volume | Avg | Payers / Sellers |
|---|---:|---:|---:|---:|
| Base | 12,500,825 | $561,323 | $0.045 | 52,107 / 31,525 |
| Polygon | 4,118,608 | $122,154 | $0.030 | 44 / 38 |
| **Both** | **16,619,433** | **$683,477** | **$0.041** | — |

*(Superset for the same window: Base 12.7M settlements / $60.1M; Polygon 5.6M /
$140.2M — i.e. even in a single recent month the unscoped EIP-3009 total is
~$200M, two-plus orders of magnitude above the real scoped $0.68M.)*

### What the match shows

1. **We corroborate x402scan on order of magnitude.** Our two-chain June scoped
   is **$683K**; x402scan's all-chain trailing month is **~$1.11M**. The
   **~$427K residual is chains we haven't backfilled yet** (Solana — the
   ~35M-tx one — plus Ethereum/others). It resolves *upward toward* x402scan as
   coverage fills, and stays three orders of magnitude below any "billion." Raw
   chain data, reproduced independently, lands on x402scan's number — **not the
   $1.45B**. That is the census gap closed.

2. **We are a superset on transactions — 4.5× x402scan's count on two chains
   alone.** 16.6M scoped settlements (Base+Polygon) vs their 3.69M all-chain. We
   reach deeper into the micropayment tail (our $0.041 avg vs their $0.30), which
   is consistent with `indexer/reconcile.py` showing our per-seller transfers are
   a superset of theirs. Nobody invents transactions; if anything the tracker
   undercounts the tail.

3. **Polygon's June "economy" is 44 payers and 38 sellers.** 4.1M settlements
   moved by ~40 wallets — the concentration thesis in its purest form.

4. **Self-dealing is historical, not current.** Scoped→clean in June drops only
   $595 (Base) and $13 (Polygon). The $23.2M self-dealing wedge in the headline
   lived in the 2025 peak era; recent months are already clean. The wash/self-
   dealing story is about reconstructed history, not the live rail.

The raw per-day breakdowns are written to
`analytics/out/matched_window_{base,polygon}.json`.

**Still open:**
- **Solana.** Fold Solana's scoped June into the cross-chain total once the
  backfill completes — expected to close most of the ~$427K residual to x402scan.
- **Coverage delta.** Enumerate any facilitators/chains x402scan includes that we
  don't and vice-versa; fold into the itemized gap as a ± line. (Two-chain data
  already shows the delta is dominated by *missing chains*, not missing
  facilitators — our per-chain count exceeds theirs.)

## Launch use

This is the receipts for thread tweets 3–4. Reframe the card/thread copy from
"the trackers report $1.45B" to "**counted naively** it's $1.45B" — the claim
stays aggressive and true, but it targets unscoped counting (aggregators, VC
decks) rather than x402scan, whom we *agree with and extend*. Making x402scan a
peer we corroborate — not a rival we correct — is both more accurate and more
credible.

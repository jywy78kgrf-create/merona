# The $1.45B mirage: what x402 volume actually is

*Computed 2026-07-11 over the full reconstructed history (2025-05-02 →
2026-07-04) — 148,251,048 EIP-3009 settlements on Base, extracted from public
chain data via HyperSync and analyzed with DuckDB directly over the archive
(`analytics/history.py`, no database load required). These are Base-only
figures; Polygon/Solana history is pending and will only add to the clean base.*

## The number

| Layer | Settlements | Volume (USD) | Avg/settlement |
|---|---:|---:|---:|
| **Superset** — all EIP-3009 transfers | 148,251,048 | **$1,452,755,000** | $9.80 |
| **Scoped** — facilitator ∈ registry (true x402 rail) | 145,051,540 | **$42,742,246** | $0.29 |
| **Clean** — scoped, minus self-dealing (`payer==seller`) | 117,555,127 | **$19,522,795** | $0.166 |

**The widely-quoted "$1.45B of x402 volume" overstates the real
facilitator-relayed, non-self-dealing agentic economy by 74×. That economy is
$19.5M.**

## Why — the mechanism matters more than the multiple

The inflation is **not** where "x402 numbers are fake" usually implies. It is
almost entirely in the *dollars*, not the *transactions*:

- **The rail is real.** 145.05M of 148.25M settlements (97.8%) genuinely route
  through registered facilitators. Almost nobody is faking the x402 rail
  itself — the transaction *count* is close to honest.
- **The dollars are a mirage.** Scoping to registered facilitators removes
  **$1.41B — 97% of the volume — while removing only 2.2% of the
  transactions.** Those ~3.2M non-facilitator settlements average ~$440 each:
  they are ordinary **gasless USDC transfers** (raw EIP-3009), miscounted as
  agentic commerce because they share the same on-chain signature. That single
  conflation manufactures the fake billion.
- **Self-dealing is dollar-heavy.** Removing `payer==seller` cuts another
  **$23.2M — over half the remaining scoped dollars — while cutting 19% of the
  transactions.** The self-pay/wash wallets move big tickets, not micro ones.
- **The true unit of machine commerce is a $0.17 micropayment.** 117.6M of them
  over 14 months. That is exactly the shape agentic commerce *should* have —
  and it is invisible to anyone reporting dollar volume.

## The wash-adjusted floor: $9.78M (149×)

The $19.5M clean layer removes only self-dealing — a threshold-free,
unarguable filter. Applying the full Artemis-style wash classification
(computed 2026-07-12, `analytics/wash_history.py`, results committed in
`analytics/results/base/wash_history.json`) cuts it roughly in half:

| | Settlements | Volume |
|---|---:|---:|
| Scoped (true x402 rail) | 145,051,540 | $42,742,246 |
| − flagged sellers (self/funded/reciprocal) | 75,272,358 | $32,953,529 |
| − remaining self-pay | 36,475 | $13,638 |
| **= Wash-adjusted clean** | **69,742,707** | **$9,775,079** |

**7,842 sellers get flagged** (self-pay ratio > 0.20, funded-payer ratio >
0.20, or participation in reciprocal A↔B pairs) and they account for **52% of
the scoped settlements and over half the remaining dollars**. On this measure
the $1.45B headline overstates the real economy by **149×**.

Method notes: the funding-graph lookback covered the top 655 sellers by count
∪ volume = **99% of scoped settlements**; reciprocal-pair and self-pay
exclusion are global (all 479k sellers); thresholds are recorded in the
committed output. The two clean layers are deliberately reported side by
side: $19.5M is definition-only arithmetic; $9.78M adds documented,
contestable-but-published thresholds.

## Polygon confirms it — and it's worse: 1,790×

Full Polygon history (2025-05-01 → 2026-07-11, 36,518,044 EIP-3009
settlements, extracted 2026-07-12, same method):

| Layer | Settlements | Volume |
|---|---:|---:|
| Superset — all EIP-3009 | 36,518,044 | $1,141,332,377 |
| Scoped — facilitator ∈ registry | 22,363,535 | $637,659 |
| Clean — self-dealing removed | 22,363,107 | $637,659 |

Polygon carries its own **$1.14B mirage over a $638K real economy** — an
overstatement of **1,790×**. The mechanism is identical and even purer than
Base: 61% of superset *transactions* are genuinely facilitator-relayed, but
they carry only 0.06% of the *dollars*; the real rail is 22.4M micropayments
averaging **$0.03**; self-dealing is negligible. Peak Polygon DAA: 13,835
(2025-08-24).

**Cross-chain total: ~$2.59B reported vs ~$20.2M real.** The mirage is not a
Base anomaly — it is how EIP-3009 superset counting fails everywhere it is
used.

## Caveats (stated, never hidden)
- **Base only.** Polygon and Solana history are not yet reconstructed. Adding
  them raises all three layers but does not change the mechanism.
- **Reconstructed regime.** Pre-2026-07-04 rows are reconstructed from public
  chain data (verifiable, re-derivable) but are not part of the git-committed,
  hash-anchored *witnessed* snapshots, which begin 2026-07-04. The distinction
  is a provenance guarantee, not a data-quality one.
- **Registry-bounded.** "Scoped" is defined by the facilitator registry
  (`data/indexer/relayer_registry.json`). A facilitator we have not yet
  catalogued would be scored as non-x402 and *undercount* the real rail —
  the scoped figure is conservative by construction.

## Daily active agents (the adoption curve)

Distinct paying wallets per UTC day — the DAU of machine commerce (superset;
exact from the `--full` run):

- **Peak: 41,152 active agents on 2025-10-28.**
- Recent steady state: ~3,000–5,600/day (late June–early July 2026), of which
  ~60–70% are *returning* wallets day-over-day.
- (2026-07-04 is right-censored at the witnessed boundary — ignore its partial
  count.)

## The agent count is a mirage too

The volume finding above deflates the *dollars*. The buyer-side distribution
deflates the *agent count* just as hard — a second, independent correction from
the same clean dataset:

- **Median agent = 1.0 payment.** Half of the 925,494 "agents" transacted
  exactly once. The count is a vanity metric.
- **Top 1% of agents = 94.2% of all settlements.** ~9,255 wallets drive 94% of
  the activity.
- **A single wallet did 11,547,905 settlements — 7.8% of the entire history by
  itself** (p90 across all agents is just 15 payments).

So the real agentic economy is not ~1M agents; it is **~9,000 industrial bots**
with a long one-shot tail behind them. The healthy-looking returning-DAU base is
those same bots.

## Seller retention: high churn, worsening

Monthly seller cohorts — of the sellers first seen in month *M*, how many were
still earning ≥30 days later:

| Cohort | New sellers | 30-day retention |
|---|---:|---:|
| 2025-05 | 35,554 | 20.6% |
| 2025-06 | 44,705 | 10.4% |
| 2025-07 | 41,398 | 10.4% |
| 2025-08 | 32,232 | 12.4% |
| 2025-09 | 34,327 | 8.3% |
| 2025-10 | 49,598 | 5.5% |
| 2025-11 | 52,271 | 4.0% |
| 2025-12 | 24,203 | 28.3% |
| 2026-01–2026-05 | 10.6k–52.5k/mo | 3.8%–10.7% |

Most sellers are one-and-done, and full-window retention **declined through
2025** (20.6% → 4.0%). The 2025-12 spike (28.3%) is an anomaly worth a closer
look. Cohorts from 2026-06 (0.3%) and 2026-07 (0%) are **right-censored** — their
30-day window runs past the 2026-07-04 data cutoff, so they are not real
retention figures and are excluded from the trend.

## How to reproduce

```
python analytics/history.py --shards 'backfill_data/*.csv.gz' --out analytics/out
```

Runs directly over the gzipped CSV shard archive with DuckDB — no Postgres, no
warehouse. The `--full` flag adds exact new-vs-returning DAU, agent-spend
concentration, and monthly seller cohorts. Output JSON in `analytics/out/`.

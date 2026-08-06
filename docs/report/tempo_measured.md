# Tempo, measured: a $682M chain with $445,722 of provable machine commerce

*Finding № 002. Computed 2026-07-23 over the full history of the Tempo L1
(chain 4217, the Stripe/Paradigm-incubated "payments chain"), 2026-01-19 →
2026-07-23: 5,985,016 scoped stablecoin settlements extracted from public
chain data (`tempo/extract_tempo.py`, rpc.tempo.xyz) and analyzed with DuckDB
directly over the gzip shard archive. Gas-fee rows (~52% of all Transfer
logs) and DEX-internal legs were excluded at extraction; MPP channel legs are
excluded from scope. Method: `tempo/BAND_METHOD.md`. All artifacts committed:
`tempo/results/band.json`, `tempo/results/hop2.json`,
`tempo/results/wash/wash_tempo.json`.*

## The number

We publish a bracket, not a point — one number would lie in three directions
at once:

| Tier | Volume (USD) | What it is |
|---|---:|---|
| **Floor** — proven | **$445,722** | memo-reconciled settlements (wash-cut) + fee-sponsored volume (loop-sponsored excluded) |
| **Center** — estimated | **$1,098,388** | floor + behaviorally machine-shaped unreconciled flow |
| **Ceiling** — upper bound | **$93,014,676** | everything that could conceivably be commerce |

Against the $682.2M the chain settled in stablecoins over the window, the
center is an overstatement factor of **621×**; the proven floor, **1,530×**.
The unit of machine commerce on Tempo is a **$0.23 memo'd micropayment** —
1,014,883 of them.

## Why Tempo is the best possible test of the mirage

Tempo is the strongest case yet *against* our thesis, which is what makes it
worth measuring. Unlike x402-on-Base, Tempo was purpose-built for honest
machine payments, with the verification machinery in the protocol itself:

- **`TransferWithMemo`** — a native reconciliation primitive that binds a
  payment to an order/invoice reference. If a payment is real commerce, the
  memo is where it says so.
- **Fee sponsorship** — a `feePayer` envelope where an operator co-signs and
  pays gas for a user's settlement. Someone with a business reason for the
  payment to happen, visible on-chain. This is Tempo's facilitator analog.
- **MPP payment channels** — per-request micropayments netted off-chain.

If agentic commerce were as big as its headlines, *this* chain — the one
that can prove it — is where the proof would be. Instead, both verification
layers turned out to be where the wash was hiding.

## Finding 1 — the "platform-verified" tier was 99.4% self-dealing

Fee-sponsored settlements looked like the strongest tier of evidence: an
operator paid gas for $32.2M of settlement volume. We enriched every
transaction in the archive with its fee-payer (`tempo/enrich_sponsored.py`,
via `eth_getBlockReceipts`) and then intersected sponsored flow with the
funded-loop wash pass:

| | Volume |
|---|---:|
| Fee-sponsored settlement volume (non-memo, floor-eligible) | $32,195,320 |
| − paying wash-flagged sellers (loop-sponsored, excluded) | $31,987,646 |
| **= sponsored volume that survives** | **$207,674** |

**99.4% of all sponsored volume pays wallets inside the sponsors' own wash
loops.** The tier that looked like platform verification was almost entirely
three bots paying their own circulation. On Tempo, gas sponsorship is not a
trust signal; it is where the trust signal was counterfeited.

## Finding 2 — the sponsors are three bots, and we traced their money

Three fee-payer addresses cover ~98% of all sponsorship
(`tempo/SPONSORS.md`). None is identifiable as Stripe, a launch partner, or
any named facilitator — no labels, no registry entries, no documentation.

- **Sponsor A** (`0x3851…4fab`, ~41% of sponsorship) runs a **confirmed
  closed funding ring**: A funds a layer of shell wallets; the shells fund
  `0x10c14002…` (the chain's #2 payment recipient); `0x10c14002…` funds A —
  $190.6K in, $188.3K back out, within 1%. Verified at full history by
  walking the in-shard funding graph (`tempo/trace_hop2.py`,
  `tempo/results/hop2.json`).
- **Sponsors B and C** (~58% jointly) share a single funding wallet
  (`0x1086b62b…`) that seeded them with under $1,300 total. Hop-2 shows that
  seeder is **mint-adjacent**: it received $100,050 from a distribution
  wallet with $18.3M of throughput whose own funding includes $0.7M of
  direct issuance — and **$10,010 was minted directly to the seeder
  itself**. Whoever operates 58% of the chain's "sponsorship" sits one hop
  from token issuance. Whether that operator is launch-affiliated or a
  faucet-draining bot is not decidable on-chain; we publish the facts and
  the addresses.

## Finding 3 — even the memo floor is 79% loops

`TransferWithMemo` is the chain's own proof-of-commerce primitive. The full
memo-reconciled, non-self, non-issuance cut is $1,148,530 across 1.89M
settlements. Applying the funded-loop filter to *sellers* of memo'd flow:

| | Settlements | Volume |
|---|---:|---:|
| Memo-reconciled machine commerce | 1,891,202 | $1,148,530 |
| − memo'd flow to wash-flagged sellers | 876,319 | $910,482 |
| **= memo floor that survives** | **1,014,883** | **$238,048** |

A memo does not launder a funded loop: **79% of the chain's own
reconciliation-stamped volume is wash-flagged.** The bots write invoices to
themselves.

## The wash pass at chain scale

`tempo/wash_tempo.py` ports the x402 Artemis-style classification (same
signals, same 0.20 thresholds) with two upgrades Tempo hands us for free:
the funding graph is in-shard, so coverage is **global** (every one of
300,790 sellers — no top-N cap), and the funded-payer signal is
**time-ordered** (the seller's seed must precede the payer's payments),
isolating seed-then-spend loops from mere two-way flow.

| | Settlements | Volume |
|---|---:|---:|
| Scoped (non-fee, non-MPP) | 5,985,016 | $682,230,342 |
| − flagged sellers (47,331 wallets: reciprocal 46,409 · funded-loop 9,268 · self 1,201) | 3,575,486 | $449,989,211 |
| − remaining self-pay | 974 | $980 |
| **= wash-adjusted clean** | **2,408,556** | **$232,240,151** |

**66% of everything the chain settled is wash-flagged.** The band then cuts
further: of the wash-adjusted remainder, $102.3M is issuance (mints), $58.9M
is redemption (burns), $59.4M is unreconciled single transfers ≥ $50K
(treasury-shaped), leaving the $93.0M ceiling — of which only $1.1M behaves
like machine commerce and only $445.7K can be proven.

## The cross-rail pattern

Every agentic-commerce rail we have measured runs two to three orders of
magnitude hot, and always by the same mechanism — the headline counts a
superset event type; the real economy is a thin stream of micropayments
buried inside it:

| Rail | Headline basis | Survives verification | Factor |
|---|---:|---:|---:|
| x402 / Base | $1.45B (all EIP-3009) | $9.78M | 149× |
| x402 / Polygon | $1.14B (all EIP-3009) | $638K | 1,790× |
| **Tempo** | **$682M (all stablecoin settlement)** | **$446K–$1.1M** | **621–1,530×** |

Tempo adds the sharpest twist: it is the first rail where the *verification
primitives themselves* — memos and sponsorship — were the primary vehicle of
inflation. The lesson for anyone building trust rails: a reconciliation
stamp proves intent to *look* reconciled. Only flow-graph analysis says
whose money it was.

## Caveats (stated, never hidden)

- **MPP netting undercounts events.** Channel micropayments settle net
  off-chain; the band measures settled value. Never cite our settlement
  counts as "number of machine payments."
- **The center is an inference.** The floor is evidence; the center adds
  behaviorally machine-shaped pairs (`tempo/classify_machine.py`, ≥2
  independent signals). Thresholds are published and contestable.
- **Reciprocity is a broad net.** 46,409 of the 47,331 flagged wallets flag
  on reciprocal pairs; a legitimate refund-heavy merchant would be caught.
  The time-ordered funded-loop signal (9,268 wallets) is the tighter core.
  "Wash-flagged" is a structural classification, not a legal claim.
- **Six months, one chain, high concentration.** A handful of operators
  dominate; single-operator changes move every tier. Cite with the window.
- **Attribution is bounded by the chain.** We can prove the sponsors are
  three unlabeled operators with looped/mint-adjacent funding. We cannot
  prove who runs them.

## How to reproduce

```
bash tempo/run_wash_band.sh        # wash pass → hop-2 trace → band, one shot
```

DuckDB over the gzip shard archive; no database, no API keys. The extractor
(`tempo/extract_tempo.py`) is resumable and re-derives the archive from any
Tempo RPC. Tests: `tempo/tests_wash.py`, `tempo/tests_band.py` (42 green).

## The citable sentence

> Memo-reconciled machine commerce on Tempo (2026-01-19 to 2026-07-23) was
> at least **$445,721.79** (floor); our central estimate including
> behaviorally machine-shaped unreconciled flow is **$1,098,388.13**; it
> cannot have exceeded **$93,014,675.58** (ceiling). Machine-payment event
> counts are undercounted because MPP channels settle net off-chain.
> Method: `tempo/BAND_METHOD.md`.

# Cross-chain coverage audit — Arbitrum, Optimism, Avalanche

*2026-08-02. Question: are we undercounting x402 by not scoping the EVM chains
beyond Base and Polygon? Short answer: no. We index all three (the raw
EIP-3009 USDC superset is in our DB), enriched the submitter on every row,
and the volume there is disbursement rails and whale/bridge transfers — not
agentic micropayments. We are deliberately NOT scoping them, because
registering those submitters would manufacture the exact mirage this index
exists to expose. This is the evidence.*

## Why this audit ran

Web research (2026-08) confirms x402 facilitators are *available* on more
chains than we scope: Coinbase's CDP facilitator lists Arbitrum; Avalanche
documents four facilitators (Thirdweb, PayAI, Ultravioleta DAO, x402-rs). We
track all four operators on Base/Solana already. So the fair question is
whether real x402 traffic is sitting on those chains, unscoped and
uncounted. We had never run relayer enrichment outside Base/Polygon, so we
couldn't answer it — the `facilitator` column was NULL on every arb/opt/avax
row. This audit filled that column and read the result.

## Method

- **Indexed already:** the nightly EIP-3009 engine runs on Polygon,
  Avalanche, Optimism, Arbitrum (`ADDITIVE_EVM_CHAINS`), so the superset was
  in the DB.
- **Enrichment:** `indexer/enrich_relayers.py` stamped `tx.from` (the
  submitter/relayer) onto every un-enriched row on all three chains
  (Arbitrum 277,543; Avalanche 72,705; Optimism 25,434 — 100% coverage).
  Ledger-safe: only NULL facilitators filled, settlement identity untouched.
- **Read two ways:** top submitters by (a) volume and (b) distinct sellers,
  with average USD per settlement — because a single number can't tell
  x402 from a payout rail, but *payment size* can.

## The discriminator: dollars per settlement

Genuine x402 on the chains we do scope is a **micropayment**: Base and
Polygon clean sellers run **~$0.03–$1 per settlement**. Agentic API calls
are cheap. So the test is simple — an address relaying for many "sellers" at
$100–$5,000 per transfer is not facilitating agent commerce; it is paying a
lot of recipients a lot of money, which is disbursement/payroll/airdrop
riding the same gasless-USDC rail.

## Findings

| Chain | Superset rows | What the top submitters actually are | x402-shaped $ found |
|---|---:|---|---|
| Arbitrum | 277,543 | Whale/bridge: top address moved **$55.6M in 13 tx** (1 seller). By-seller: a fleet of **11 near-identical addresses, each serving exactly 715 sellers at ~$100/tx** (~$2M each), plus `0x47e9…00e0` at **$2,936/tx** across 872 sellers. | ~$0 |
| Optimism | 25,434 | `0x47e9…00e0` again at **$5,513/tx** (65 sellers, $750K). Everything else genuinely micropayment-shaped ($0.01–$0.39/tx) but totalling **a few hundred dollars**. | ~$300 |
| Avalanche | 72,705 | `0xc3e5…4e02` at **$673/tx across 1,140 recipients** ($2.3M); `0x6a77…59d0` 62,938 tx at $140/tx ($8.9M); the same 11-address cluster at $300–450/tx. | ~$0 |

### Two structural tells that it's programmatic payout, not agents

1. **One operator, three chains.** `0x47e926c8b1bbbc2767989e6523b82f08455700e0`
   is a high-value, multi-recipient submitter on Arbitrum, Optimism, *and*
   Avalanche ($2.9K–$5.5K/tx). A cross-chain bulk-payment operation, not an
   agent-commerce facilitator.
2. **The uniform fleet.** Eleven Arbitrum addresses each serve *exactly* 715
   sellers with ~20,000 tx and ~$2M at ~$100/tx — and the same set reappears
   on Avalanche. That uniformity is a scripted relayer fleet doing
   programmatic disbursement, three-to-five orders of magnitude above x402
   payment sizes.

## Cross-check against the named facilitators

The facilitators the ecosystem advertises on these chains do not show up as
x402 flow in our enriched data:

- **Coinbase (CDP)** lists Arbitrum support, but no micropayment-shaped flow
  from any Coinbase-style submitter appears there — its Arbitrum x402 usage
  is effectively nil.
- **Thirdweb / PayAI / Ultravioleta DAO / x402-rs** are the four facilitators
  Avalanche documents. We track all four on Base/Solana. None publishes a
  fixed on-chain relayer address (x402-rs is self-hosted with an
  operator-supplied key), and none produces micropayment-shaped flow in our
  Avalanche data. Their Avalanche x402 volume is negligible or not yet live.

Sources: Coinbase CDP network support (docs.cdp.coinbase.com/x402/network-support);
Avalanche Builder Hub x402 facilitators
(build.avax.network/academy/.../04-x402-on-avalanche/03-facilitators);
x402-rs integration docs (build.avax.network/integrations/x402-rs).

## Decision

**Arbitrum, Optimism, and Avalanche remain indexed but UNSCOPED.** No
facilitator is registered for them. Rationale:

- There is no x402 micropayment flow of any material size to scope — the
  largest genuine x402-shaped total across all three is ~$300 (Optimism).
- The large volume that *is* there is disbursement and bridge traffic.
  Registering those submitters as "facilitators" would publish millions of
  dollars of non-x402 transfers as agentic commerce — the payout-rail mirage
  (a single unregistered Base address once inflated our scoped figure ~140×;
  the Polygon "clean" figure was ~$144K of a wash ring until restated to
  $1.14). We will not reintroduce it on a new chain.

This is the same discipline as the clean-volume series: publish only what we
can honestly scope, and say plainly what we exclude and why.

## Retention & re-check

- The enrichment persists in production Postgres (backed up to R2). It is a
  point-in-time snapshot through 2026-08-02; new rows on these chains land
  `facilitator=NULL` because nightly enrichment runs only on Base/Polygon.
- If a real x402 facilitator appears on one of these chains, the dark-matter
  coverage canary will surface the attribution shift; re-running
  `enrich_relayers.py --chain <ch>` (or adding the chain to the nightly)
  refreshes the picture, and the existing scope/clean/cycle pipeline —
  already chain-agnostic — would then just work.

## How to falsify this

Point us at an x402 facilitator relayer address on Arbitrum, Optimism, or
Avalanche that settles agent micropayments (sub-dollar, many distinct
payers). If it is in our enriched data and we missed it, we will register it
and restate — loudly, as always. The claim here is narrow and checkable:
*as of 2026-08-02, no such flow of material size exists on these chains.*

# Claims vs. Chains — who says they're on x402, and what the ledger shows

*Draft section for the "State of x402" report. Drafted 2026-07-10; observed
figures from the index as of the 2026-07-09 cycle. Every observed number below
is reproducible from the immutable daily snapshots in `data/indexer/snapshots/`.*

## The gap this section measures

x402 has extraordinary institutional endorsement: the **x402 Foundation counts
22 launch members**, including Mastercard, Visa, American Express, Stripe,
Adyen, AWS, Google, Microsoft, Shopify, and Circle. Coverage of those
announcements routinely implies the payment giants are "doing x402."

Membership is not settlement. This section puts each claim next to what an
independent, forward-only index of the actual settlement rails observes.

## What the index observes (2026-07-04 → 2026-07-09 window)

| Chain | x402-scoped settlements | Scoped volume | Sellers | Avg |
|---|---:|---:|---:|---:|
| Base | 3,110,955 | $84,398 | 12,437 | ~$0.03 |
| Polygon | 995,307 | $30,367 | 38 | ~$0.03 |
| Solana | 43,170 | $8,443 | 450 | ~$0.20 |
| **Total** | **4,149,432** | **$123,208** | | |

Scoped = settlements whose relayer (`tx.from`) is in the facilitator registry
(32 named facilitators, incl. Coinbase CDP's 41-relayer pool). The raw EIP-3009
"superset" overstates x402 dollar volume by ~2 orders of magnitude; the scoped
figure is the true signal. For cumulative scale context, Chainalysis reports
~100M x402 transactions on Base since mid-2025; this index measures forward
from 2026-07-04 with immutable daily snapshots.

## Claim-by-claim

| Player | The claim | What the ledger shows | Verdict |
|---|---|---|---|
| **Coinbase** | Created x402; runs the Bazaar directory + CDP facilitator | Largest relayer pool in the registry (41 addresses); dominant share of scoped settlements on Base | **Settling — verified on-chain** |
| **Stripe** | "x402 payments" product (launched 2026-02-11, preview): USDC on Base, Solana, **Tempo** | Stripe runs **no mainnet facilitator of its own** — its docs route mainnet settlement through **Coinbase's CDP facilitator**. Stripe-originated flow on Base/Solana is therefore already inside our Coinbase-scoped numbers, indistinguishable from other CDP traffic | **Real, but rides Coinbase's rails** |
| **Mastercard** | Agent Pay for Machines (2026-06), built "with open standards like x402," partners incl. Coinbase, Adyen, Cloudflare | No Mastercard-attributable relayer observed in any settlement. Foundation member; no distinct on-chain settlement footprint yet | **Committed, not (yet) settling** |
| **Visa / Amex / AWS / Google / Microsoft / Shopify / Adyen / Circle** | x402 Foundation launch members | Google's AP2 x402 extension (`a2a-x402`) settles through paths this index already captures (verified); Circle issues the USDC every settlement moves. No distinct relayer footprint for the others | **Endorsement layer** |

## Original finding: Tempo settles x402 invisibly to EIP-3009 measurement

Stripe's own L1 (**Tempo**, mainnet 2026-03-18, chain id 4217) is named in
Stripe's x402 docs as a settlement network, with an *enshrined* USDC contract
(`0x20c0…8b50`). Probed 2026-07-10 via public RPC:

- ~130k USDC Transfer events/day — the chain is genuinely busy;
- **zero EIP-3009 `AuthorizationUsed` events across ~17 hours**;
- the majority of sampled transfer transactions carry **empty calldata**
  (`to = None`, `input = 0x`) — USDC moves at the **protocol level**, not
  through token-contract calls.

Consequence: any x402 measurement keyed on EIP-3009 (including this index's
EVM engine, and — we believe — every public x402 tracker) **structurally
cannot see Tempo**. A Tempo-native path (Transfer events scoped by facilitator
`tx.from`, the same model this index already uses for Solana) is required, and
depends on Tempo facilitator relayer addresses becoming identifiable. Until
then we deliberately do NOT index Tempo, rather than publish a false zero.

### Update (2026-07-10, deep probe): Tempo's facilitator role exists — as MPP fee-sponsors

A dedicated on-chain probe (51k sampled USDC transfers + a fee-payer census
over ~2.5 days of blocks) identified Tempo's native equivalent of the
facilitator relayer: the **MPP (Machine Payments Protocol) fee-payer/cosigner**.
Tempo's tx type `0x76` lets a third party attach a `feePayerSignature` and pay
gas for a buyer's `transferWithMemo` payment, with the memo carrying the MPP
attribution tag (`0xef1ed712` = keccak("mpp")[0:4]) + serverId/clientId. Every
sponsored payment is auditable via USDC transfers to the protocol fee collector
`0xfeec…0000`.

Two candidate fee-sponsor relayers were found operating live:
- `0x3851…4fab` — dominant sponsor (~40% of all fee events; 12,667 sampled
  transfers to 2,119 counterparties, median $0.0057). Caveat: its funding loops
  back from the very merchant its buyers pay — consistent with one platform's
  internal/demo traffic. Operator unattributed.
- `0x3025…2356` — pure fee-sponsor (100% of sampled txs cosigned, many distinct
  buyers paying one merchant $0.002–$0.005/call — textbook per-request API
  micropayments). Operator unattributed.

Corroboration: Coinbase CDP's network-support page does NOT list Tempo (so
Stripe's mainnet-via-CDP path cannot settle there yet); the Bazaar has exactly
ONE Tempo listing (molty.cash) with ZERO observed on-chain payments to its
payTo across 2M blocks. Registry stance: record the two addresses as "MPP
fee-sponsor (facilitator-equivalent), operator unattributed" — not as confirmed
third-party x402 facilitators. Indexing signal when we build the Tempo path:
fee-collector transfers where fee payer ≠ tx.from (definitionally exact), plus
MPP memo serverId clustering. Watch: `0xf70d…dbef` (master distributor, $3.17M
to 13.6k wallets/90d, unattributed) and CDP network-support for Tempo addition.

## Method notes / honesty box

- Index window starts 2026-07-04 (forward-only; no backfill). Cumulative
  claims (e.g. 100M since 2025) are not directly comparable to window totals.
- "No observed footprint" for a Foundation member means no relayer attributable
  to that member appears in settlements — it does not preclude flow through a
  shared facilitator (as Stripe demonstrates via CDP).
- Registry: 32 facilitators; enrichment coverage at 97–100% per chain as of
  2026-07-09, so scoped counts are effectively complete for the window.

## Sources

- Stripe x402 docs — https://docs.stripe.com/payments/machine/x402
- Mastercard Agent Pay for Machines (2026-06) —
  https://www.mastercard.com/us/en/news-and-trends/press/2026/june/mastercard-launches-agent-pay-for-machines.html
- Fortune on Mastercard's protocol launch —
  https://fortune.com/2026/06/10/mastercard-ai-payments-protocol-launch-agentic-finance/
- Chainalysis, "Inside x402: 100M Agentic Payments on Base" —
  https://www.chainalysis.com/blog/x402-agentic-payments-adoption/
- Forbes, "Visa, Mastercard and Coinbase Are Fighting Over How AI Agents Pay" —
  https://www.forbes.com/sites/digital-assets/2026/06/07/visa-mastercard-and-coinbase-are-fighting-over-how-ai-agents-pay/
- Tempo mainnet announcement — https://tempo.xyz/blog/mainnet/
- Tempo RPC probe: this repo, 2026-07-10 (see evm_chains.py TEMPO note)

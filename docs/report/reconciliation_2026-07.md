# One economy, five numbers — reconciling merona × Artemis × Allium × x402scan × arXiv 2607.12575

*Drafted 2026-07-31. Extends `census_gap.md` (2026-07-13) to cover the two
measurements published since: Artemis's wash-adjusted series and arXiv
2607.12575 (submitted 2026-07-14). Every merona figure below is re-derivable
from the immutable snapshots in `data/indexer/snapshots/` and the published
method in `indexer/`; external figures are sourced inline. Corrections are
invited and will be published, not buried.*

## TL;DR — the disagreement is the criterion, not the facts

Independent measurers of x402 on Base — a VC-backed explorer, an analytics
firm, an academic team, and this index — **converge on the same scoped economy
and diverge only on how strict a "real" filter they apply.** Ordered by
criterion strictness, survivor-share of scoped/attributed volume:

| Measurer | Criterion | Survives |
|---|---|---:|
| x402scan | scoped to registry facilitators, no wash filter | 100% of scoped |
| arXiv 2607.12575 (permissive tier) | "not provably manufactured" | **45.9%** ($20.26M of $44.12M) |
| **merona (clean layer)** | remove self-dealing + funded-loop wash *(depth ≤ 2 — upper bound, see correction)* | **22.9%** ($9.78M of $42.74M) |
| Artemis | wallet-graph wash filter | **~19%** (81% of volume flagged) |
| arXiv 2607.12575 (strict tier) | "demonstrably reaches a nameable service" | **0.43%** ($187,861) |

Nobody credible is measuring a billion-dollar agent economy. The numbers that
circulate above this table are unscoped EIP-3009 supersets (merona measures
that wedge precisely: ~$1.45B lifetime; in the current witnessed window
(2026-07-04 → 07-31) the superset runs $64.5M on Base and $105.5M on Polygon
against clean figures of $332K and $144K — real as gasless-USDC totals, wrong
as agent-commerce totals). **The $144K Polygon clean figure was withdrawn on
2026-07-31 and restated to ~$1 on 2026-08-01 — see the correction immediately
below.**

## Correction, and the methodological finding that came with it

On 2026-07-30 this index published Polygon as **wash-verified with nothing to
remove**: $143,926 clean, 43 sellers, $0 stripped. On 2026-07-31 a payer-side
trace of those same 43 scoped sellers showed that number is **~100%
manufactured**, by a structure our filters are not built to see:

```
lnpay/aisa 0x66fa…2ec8  ──166 tx × $300.00──▶  0x8977…bab8
0x8977…bab8             ──2,499 tx × $20.00──▶  30 fresh wallets
30 fresh wallets        ──4.97M tx × $0.01───▶  lnpay/aisa 0x66fa…2ec8
```

Each hop's dollar total is ~$49.8K; each wallet's inflow ≈ its outflow (net
positions −$117, −$180, +$25 × 30); the 30 wallets' combined outbound tx count
(≈4,968,000) matches lnpay's inbound (4,968,264) to within drift. One ~$50K
float circulates and mints ~$149K of "settled volume" per lap. Polygon's
genuinely independent x402 volume is the **$1.13 dust tail**.

**Why it passed.** Our filters were correctly applied and structurally blind:
a 3-cycle has no payer==seller edge (self-dealing), no A↔B pair (reciprocal),
and no seller→own-payer edge (funded-loop) — every node funds the *next* hop,
never its own. **Every check we publish is depth ≤ 2. This ring is depth 3.**

**The general result, which is the part worth other measurers' attention:**
a wash filter's detection depth is a hard ceiling on the wash topology it can
see, and the cost of adding a hop is trivial for the manufacturer (one more
wallet) while being invisible to any fixed-depth check. Filters described in
public as catching "wallets that repeatedly transacted with themselves or
cycled funds between addresses" (Artemis) or "internal settlement within a
linked cluster" (2607.12575) *may* catch this — cluster-based methods
plausibly do, pairwise methods plausibly don't — but we cannot determine that
from the outside.

**So we offer it as a test case rather than a claim.** The ring above is fully
specified, on public Polygon data, in a window everyone can query. If your
filter flags it, your method is strictly stronger than ours on this axis and
we will say so publicly. If it doesn't, we have a shared blind spot worth
fixing together. Either answer improves the shared number.

merona's remediation, and where it stands:

- **2026-08-01 — restated.** The 32 ring wallets are excluded from the
  nightly clean series via a published, sha-cited flags file
  (`data/indexer/wash_flags_polygon.csv`); Polygon clean now publishes at
  ~$1 with a provenance note on the dashboard, not a silent fix.
- **The balance screen ran on all three chains** (net-flow fingerprint,
  `analytics/wash_balance.py`): Polygon 100% of scoped volume in carousel
  candidates (the known ring — the screen reproduces it independently);
  **Base 0.4%** ($1,892 of $532K across 93,996 wallets); **Solana 0.5%**.
  As of this writing, merona's Base figure is the only published x402 clean
  number that has been screened for this failure mode.
- **2026-08-01 — wash v3 is built and wired into the nightly**
  (`indexer/wash_cycles.py`): cycle membership via strongly-connected-
  component decomposition of the scoped payer→seller graph — depth-unbounded,
  so a ring cannot dodge it by adding a hop — combined with the net-flow
  balance fingerprint (flag = on-cycle AND balanced; balance alone only
  warns, because honest pass-throughs balance too). The synthetic replica of
  this ring is the permanent acceptance test in the repo
  (`indexer/tests/test_wash_cycles.py`); a build that misses those 32
  wallets fails CI. Flagged wallets subtract from clean metrics
  automatically and cap the seller's trust grade at D
  (`scores.py` SCORE_VERSION 3, detector parameters recorded per affected
  score row). Nightly clean figures are now screened for this failure mode
  by construction, not by curation; the hand-curated flags file remains as
  the published evidence record of the original finding.

## The three convergences (the news here)

**1. Total scoped volume: three-way agreement within ~3%.**
merona's full-history Base reconstruction: **$42.74M** scoped (148.3M
settlements, 2025-05 → 2026-07). arXiv 2607.12575, independently, over a
280-day Base window: **$44.12M** (136.7M settlements). x402scan's monthly
series (peak $5.15M Nov 2025, ~$1.19M May 2026) integrates to the same tens of
millions. Different teams, different pipelines, same economy.

**2. The permissive-filter tier: agreement within ~4%.**
merona after removing payer==seller self-dealing: **$19.52M**. The paper's
"not provably manufactured" tier: **$20.26M**. These are independently built
exclusion classes (our self-dealing filter vs their fictitious/cluster
analysis) landing on the same number. This is the strongest external
corroboration our clean-layer methodology has received.

**3. The strict band: merona 77% removed, Artemis 81% removed.**
Our funded-loop pass (funding-graph walk: sellers paid by wallets they funded)
removes a further $9.7M → **$9.78M clean, 77% of scoped volume removed**.
Coverage of Artemis's historical analysis reports their wallet-graph filter
flagging **81% of volume** (~48% of transactions) as gamed (secondary
attribution — Cryptopolitan; Artemis's own primary-published figure is the
$1.6M-vs-$3M window, ~47% flagged post-peak, see below). Two independent wash
methodologies, four points apart on the historical era. The paper's by-count
split (21.20% fictitious + 63.78% cluster-internal = 85%) brackets the same
range.

**Also agreed by everyone measuring: the trend.** The rail peaked ~Nov 2025
and declined ~77% by May 2026 (x402scan series); Artemis's own real-
transaction series shows the same collapse — **~731K real txns/day (Dec 2025)
→ ~57K/day (Feb 2026), "just ~8% of prior highs"** (@artemis, 2026-02-09:
"the x402 'agent payments' boom is still mostly a mirage… demand isn't here
yet"). Measured *flag share* also falls over time — our June 2026 matched
window shows self-dealing down to $595/month on Base (see `census_gap.md`
§matched-window), and Artemis's window figures fall from ~81% (historical) to
~47% (Feb–Mar).

**We no longer read that decline as straightforward cleaning.** The Polygon
ring above sits inside exactly such a "clean" recent window, and it is
invisible to depth-≤2 checks. Falling flag share is consistent with two very
different worlds: manufacturing genuinely receding, or manufacturing
migrating to topologies past our detection depth — and a filter cannot
distinguish "less wash" from "wash I can no longer see." Anyone whose filter
depth is fixed should treat their own declining flag-share series with the
same suspicion. What remains solid: the *real-activity* collapse (~731K →
~57K txns/day) is a volume measurement, not a filter output, so it stands
independent of this problem.

## The 30-day dispute, resolved by denominator

The canonical public comparison is a16z's (Noah Levine, @nlevine19,
2026-03-11), for the **2026-02-05 → 03-07 window**: **x402.org/Bloomberg
$24M** · **Allium $3M** (on-chain observed) · **Artemis $1.6M** (wash-
adjusted — "applied over the same 30-day period, the adjusted number is $1.6
million," @artemis, 2026-03-11). Note the derivable middle figure: $1.6M
against $3M observed means Artemis's filter flagged **~47% of observed volume
in that post-peak window** — lower than the ~81% reported for their historical
analysis, consistent with the wash-decay trend everyone's data shows.
x402scan's trailing-30d (~$1.11M) and merona's June matched window (**$683K**
scoped, Base+Polygon only; Solana backfill closes most of the gap) sit in the
same cluster. Verdict: the scoped/filtered measurers cluster at $1–3M/30d;
the outlier is the registry's own $24M. Quote the cluster; treat the headline
as unaudited.

**And note the dates: every public figure above is from February–March 2026.**
No measurer has published a post-March 30-day number. merona's nightly
witnessed window (2026-07-04 →) is, as of this writing, the only current,
continuously updated figure in the conversation.

## Open divergences (flagged, not spun)

**Solana.** Artemis's revamped methodology reports **86% of Solana x402
activity inorganic**. merona's *current* witnessed window (top-150 scoped
sellers, monthly funding-graph walk) removes only **3.9%** ($56.5K scoped →
$54.3K clean) — while our *lifetime* Solana archive shows substantial
historical wash and a claim-understatement anomaly ($1.1M claimed vs $2.40M
measured wash-adjusted; see `docs/MERONA_OVERVIEW.md`). Likely explanation:
different windows (historical peak vs current) and different populations
(registry-scoped facilitator settlements vs all x402-tagged activity). One
structural note sharpens the comparison: on Solana the gasless superset and
the scoped figure nearly coincide ($57K vs $56.5K in the current window) —
the rail-miscounting wedge that inflates Base 194× and Polygon 733× barely
exists there, so any Solana inorganic share is a *wash* question, never a
*denominator* question. We'd welcome a matched-window, matched-population
comparison with Artemis — our shards and seller lists are available for
exactly this.

**The strict tier ($9.78M vs $187,861).** The gap between our clean layer and
the paper's nameable-service figure is not a contradiction — it answers a
different question. Ours: *is the money circular?* Theirs: *did an
identifiable service demonstrably serve something for it?* The bridge is
service-layer measurement, which is where merona's non-chain data sits: in
the July 2026 full-catalog audit (6,551 listed endpoints probed; the catalog
has since grown to 15,293 listings, now continuously probed), only **26.9%
returned a machine-payable x402 response**; an independent domain census
finds **150 active seller domains, median age 165 days**; and in a paid pilot
(n=25 settled endpoints), **24% of payments returned nothing or garbage** and
20% took money while returning an error.
Money that is non-circular but reaches a dead or non-delivering endpoint is
exactly the wedge between $9.78M and $187K. Quantifying that wedge
continuously — clean volume × delivery verification — is on our roadmap and,
to our knowledge, no one else's.

**Tempo blindness (affects every measurer including us).** Stripe's Tempo L1
settles machine payments at the protocol level with zero EIP-3009 events —
every EIP-3009-keyed x402 measurement **structurally cannot see it** (probe
data in `claims_vs_chains.md`). All numbers above are therefore floors on
machine-payment activity and should say so.

## What this index adds to the reconciliation

1. **Continuity.** Nightly, hash-anchored, never-silently-restated series —
   the paper is one-shot, Artemis is periodic; the manufactured-volume story
   needs a living baseline.
2. **Full history.** 148.3M Base settlements to 2025-05; 201M+ across chains.
3. **A superset proof.** `indexer/reconcile.py` shows our per-seller transfers
   are a superset of x402scan's — we corroborate the explorer before going
   two layers deeper (self-dealing, funded loops).
4. **The service layer.** Liveness census + paid delivery probes — the only
   measurement addressing the strict criterion from the delivery side.
5. **Independence.** No token, no facilitator, no ecosystem grant; methodology
   and revisions public.

## Sources

- arXiv 2607.12575, "How Agentic Is Agentic Commerce?" (Ling, Zhou, Wu, Wang;
  2026-07-14) — abstract figures quoted verbatim: 136,708,672 settlements,
  $44,121,383.81, 280-day window, 21.20% fictitious, 63.78% cluster-internal,
  $20,258,746.09 (45.92%) not provably manufactured, $187,861.35 nameable-
  service, Gini > 0.98. https://arxiv.org/abs/2607.12575
- Artemis primary sources: @artemis 2026-03-11 (OnchainLu wash filter; "the
  adjusted number is $1.6 million"; Real-vs-Gamed daily chart) and @artemis
  2026-02-09 (~731K → ~57K real txns/day, "~8% of prior highs"). a16z chart:
  Noah Levine (@nlevine19), 2026-03-11, "The Honest Number Behind AI Agent
  Payments" — $24M (x402.org/Bloomberg) / $3M (Allium) / $1.6M (Artemis),
  data window 2026-02-05 → 03-07.
- Artemis 81%/48% + Solana 86% figures: secondary attribution via
  https://www.cryptopolitan.com/x402-agentic-ai-commerce-growth/ ;
  https://www.mexc.com/news/914144 (primary research post not yet located —
  correction welcome)
- x402scan 30d + monthly series: `census_gap.md` (matched-window run,
  2026-07-13) ; https://github.com/Merit-Systems/x402scan
- merona figures: `census_gap.md` (lifetime ladder $1.45B → $42.74M → $19.52M
  → $9.78M; June matched window), `report/state-of-x402-endpoints.md` (26.9%),
  `ventures/receipt/BRIEF.md` (delivery pilot), nightly blob (witnessed
  window, 2026-07-04 →).

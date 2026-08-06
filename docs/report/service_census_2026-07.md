# Service census v0 — what x402 sellers are, and what actually gets paid

*2026-07-31. Produced by `analytics/service_census.py`, which synthesizes the
four derivable evidence layers into one per-origin directory
(`data/processed/service_directory_v0.json`, rollups in
`service_census_rollup.json`). Layer vintages differ (catalog 2026-07-23,
probes 2026-07-03, wallet map 2026-07-01) — v0 is a composition study; use
shares, not absolute dollar totals, until the layers are re-run same-day.*

## Method: four layers, one evidence ladder

Every catalog listing self-declares nothing but a URL and a price. What a
seller *is* must be derived, and each derivation carries a different level of
proof. Per origin we assign a category (keyword rules over host/path tokens +
manual labels for high-volume origins; seller self-descriptions fold in
automatically as the patched collectors capture them) and an **evidence
tier** — the service-side mirror of gross → scoped → clean:

| Tier | Meaning | Origins (of 1,642) |
|---|---|---:|
| T0 | listed in a catalog; no other evidence | 636 (38.7%) |
| T1 | responds to an unpaid probe | 251 (15.3%) |
| T1+ | serves a *valid machine-payable* x402 402 | 173 (10.5%) |
| T2 | on-chain settlements reach its payTo | 582 (35.4%) |
| T3 | paid probe returned what was advertised | 0 published (pilot data on box) |

## Finding 1 — the catalog and the economy are two different objects

By **listings**, the catalog is 44.2% one spam operation: `lowpaymentfee.com`
(10,028 listings) plus `clonecho.builda.company` (993) — two origins, two
wallets. Their combined lifetime settled flow: **$191.** Meanwhile the
categories that dominate listings (market-data 16.7%, dev-utilities 5.9%)
collect a rounding error of the money.

By **settled dollars**, the economy is violently concentrated elsewhere:

| Category | Origins | Listings share | Settled share |
|---|---:|---:|---:|
| swarm-platform (Virtuals ACP, Questflow) | 4 | 0.9% | 32.3% |
| ai-services | 97 | 2.2% | 23.1% |
| *unclassified* | 887 | 15.9% | 22.5% |
| market-data | 308 | 16.7% | 20.6% |
| everything else (9 categories) | 346 | 64.3% | 1.5% |

**Four origins carry 95.0% of all settled flow** reaching cataloged origins:
`acp-x402.virtuals.io` ($19.6M — Virtuals' agent-commerce protocol),
`www.qrbase.xyz` ($14.0M), `ainalyst-api.xyz` ($13.7M), `api.barvis.io`
($13.0M). The payer/recipient Gini > 0.98 that arXiv 2607.12575 measured on
wallets reproduces here on *services*.

## Finding 2 — the biggest unattributed seller is a $14M question mark

`www.qrbase.xyz`: one API path (`api/x402`), zero self-description, no
category signal — and **$14.0M of settled flow (22% of everything)**. This is
the case study for why the trust layer exists: the second-largest earner in
the cataloged x402 economy is a wallet with a URL and nothing else to say.
Attribution/verification of this origin is an open item.

## Finding 3 — what the agent economy actually buys vs what it's sold

Listings sell *data to agents* (market-data + real-world-data + scraping ≈
half of non-farm listings; real brands present: Glassnode, Arkham,
Finnhub-backed feeds). Dollars flow to *agent-economy platforms* — swarm
protocols and AI-service billing rails. The "agentic commerce" catalog is a
data bazaar; the settled flow is mostly platform economies settling their own
token/agent activity. These are different markets sharing one payment rail.

## Finding 4 — hygiene

- 32 single-wallet origins carry ≥50 listings each (template farms).
- 14 staging/dev instances leak into the public catalog (`staging.*`,
  `api-dev.*`), including one dev clone with per-listing wallets.
- 2 marketplace-shaped origins (one payTo per listing: Questflow + its dev
  twin) need operator-level, not wallet-level, treatment in scoring.

## Caveats (read before quoting)

1. **Dollar figures are composition-only.** The origin→wallet map predates
   the catalog snapshot by ~3 weeks and its group totals are census-vintage
   (recipient-duplicated rows are equal-split — see script — but provenance
   of the underlying totals is the 2026-07-03 census run, not the current
   clean layer). Shares are meaningful; headline totals should come from the
   nightly index.
2. **887 origins (22.5% of settled flow) remain unclassified** — mostly
   small, plus qrbase. The collectors patch (2026-07-31) starts capturing
   seller self-descriptions; each nightly snapshot from here shrinks this
   bucket.
3. **T3 is empty in-repo.** The paid pilot's aggregates are published (24%
   of paid calls returned nothing/garbage; n=75 sample across 3 pools) but
   per-endpoint artifacts live on the box. Publishing them (redacted as
   needed) lights up the top of the evidence ladder.
4. **Chain scope.** The category layer (L1) and behavior-window stats are
   cross-chain (catalog listings declare Base/Solana/Polygon/Arbitrum/
   Stellar; aggregates cover six chains). The **settled-dollar attribution
   is effectively Base-only**: the underlying census wallet set is Base
   139,251 + Solana 24,160 + Polygon 4 — Polygon's revenue composition is
   simply not represented, and Solana joins were degraded by payTo
   lowercasing (fixed in collectors 2026-07-31; snapshots before then carry
   mangled Solana payTos). Do not quote the settled-share table as
   cross-chain. Polygon's clean economy is 43 sellers — small enough to
   hand-attribute completely and make it the first 100%-attributed chain.

## Next steps

1. Re-run all layers same-day on the box (probe + wallet map + catalog) so
   dollar totals become quotable, not just shares.
2. Publish per-endpoint paid-probe artifacts → first real T3 tier.
3. Attribute `qrbase.xyz` (and the top unclassified tail) — descriptions,
   homepage, WHOIS/TLS, payer-graph shape.
4. Surface category + tier on the dashboard trust pages: "market-data API ·
   T2 · 89 payers" is what makes a badge legible.

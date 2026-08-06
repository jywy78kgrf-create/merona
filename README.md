# merona — the x402 settlement index

**merona** is an independent, open measurement index for x402 / agentic-commerce
settlement. It reads public blockchain data across six chains nightly, strips
wash trading and self-dealing, and publishes clean numbers **anyone can
recompute** — plus free tools to act on them.

Live: **[merona.io](https://merona.io)** · API: **[api.merona.io](https://api.merona.io)**

## Why this exists

The raw x402 volume most dashboards quote is the unfiltered EIP-3009
gasless-USDC stream — bridges, ramps, exchange internals, and a great deal of
self-dealing. It overstates real agent commerce by roughly 100×. On Base in
July 2026 the raw stream was **$64.5M**; what actually settled through the
x402 flow was **~$332K**. merona measures the difference, and shows its work.

## Products

| | | |
|---|---|---|
| **Free check** | `merona.io/check?q=<wallet or url>` | Does an endpoint's advertised payout address match what it actually asks for? On-chain history attached. No key, no signup. |
| **Mismatch feed** | `merona.io/mismatches.json` | The full payTo-integrity feed. Free bulk JSON. |
| **MCP server** | `POST api.merona.io/mcp` | Wire merona into your agent. Four tools; free payTo checks, paid wash-aware scores. Listed as `io.merona/settlement-index` in the official MCP registry. |
| **m.Score API** | `api.merona.io` | Wash-aware seller trust scores (A–F). $0.005/call over x402 — no key, no signup — or an API key. |

## How the numbers are made

- **Scoping** — settlements are read from chain and restricted to the registered
  x402 facilitator set (`indexer/`).
- **Wash detection** — a payer→seller graph is decomposed with an iterative
  Tarjan SCC pass; any non-trivial strongly-connected component that is
  net-flow balanced is flagged as a funded loop, catching cycles of any length,
  not just reciprocal pairs (`indexer/wash_cycles.py`, `indexer/wash.py`).
- **Scoring** — clean volume excludes flagged wallets; trust grades are gated on
  evidence (unrated until enough observation days, provisional, then verified),
  and never invent a number for an unseen wallet (`indexer/scores.py`).
- The method and its revisions are documented in `docs/` (`WASH_V3_SPEC.md`,
  `SCORE_REVISIONS.md`) and the findings in `docs/report/`.

## Verify it yourself

Every published figure is re-derivable from an immutable daily snapshot whose
SHA-256 digest is committed **before** publication to a separate append-only
log: **[merona-anchors](https://github.com/jywy78kgrf-create/merona-anchors)**.
GitHub's public commit timestamps attest when each snapshot existed — proof the
numbers can't be quietly backdated. The wash-flag files that drive both the
clean-volume exclusion and the grade caps are published here
(`data/indexer/wash_flags_*.csv`), so each affected number is independently
re-derivable.

We correct in public. When a figure is wrong we withdraw and restate it with
the reasoning intact — see the Polygon correction in
`docs/report/reconciliation_2026-07.md`.

## Disclaimer

merona publishes recorded facts from public chain data and nightly probes. They
are signals — not guarantees of any seller's performance, and not financial
advice. Payout-address integrity is one signal among several.

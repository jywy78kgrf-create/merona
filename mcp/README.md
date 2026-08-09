# merona MCP server

[![smithery badge](https://smithery.ai/badge/michaelfitz/merona)](https://smithery.ai/servers/michaelfitz/merona)

Wire merona into your agent over MCP — free payTo-integrity checks and a
mismatch feed, plus paid wash-aware seller trust scores.

- **Endpoint:** `POST https://api.merona.io/mcp` (Streamable HTTP)
- **Manifest:** `https://api.merona.io/mcp.json`
- **Registry:** `io.merona/settlement-index` in the official MCP registry

## Tools

| Tool | Access | What it returns |
|---|---|---|
| `payto_check` | free | Whether an endpoint's advertised payout address matches what it asks for live, with on-chain history. |
| `mismatch_feed` | free | The full payTo-integrity feed. |
| `clean_stats` | free | Wash-adjusted settlement volume per chain. |
| `trust_score` | paid | Wash-aware seller trust score (A–F). Via API key, or pay per call in USDC over x402 — no key, no signup. |

`server.json` in this directory is the registry metadata for this server.

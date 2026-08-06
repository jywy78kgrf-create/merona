"""EVM chain configs for the x402 settlement indexer.

Every EVM x402 chain settles the same way — a gasless USDC transfer via EIP-3009
(`transferWithAuthorization`), emitting the same `Transfer` + `AuthorizationUsed`
log pair (identical topics on every chain). So one indexer engine (`index_base`)
serves all of them; only these per-chain constants differ:

- the USDC contract address,
- the chain id / name,
- the RPC (public default + the env var holding a keyed one),
- tip-safety (`confirmations`) and bootstrap sizing, which follow block time.

Settlements from every chain land in ONE SQLite DB, distinguished by the `chain`
column and the per-chain `indexed_ranges` ledger, so adding a chain never
touches another chain's coverage.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvmChain:
    name: str                       # settlements.chain value, e.g. "base"
    chain_id: int                   # EIP-155 id (8453 Base, 137 Polygon)
    usdc: str                       # USDC contract, lowercase
    default_rpc: str                # public fallback endpoint
    rpc_env: str                    # env var holding a keyed RPC URL
    confirmations: int              # blocks kept clear of the reorg-prone tip
    bootstrap_lookback_blocks: int  # one-time day-1 seed (~24h of blocks)
    history_anchor_block: int       # anchor for OPT-IN backfill only
    subrange: int                   # blocks per getLogs walk step
    # fetch strategy: False = scan all USDC Transfers in range (cheap when x402
    # is a large fraction of USDC volume, e.g. Base/Polygon). True = fetch only
    # the known x402 txs' receipts (cheap when USDC transfers >> x402 txs and/or
    # block count is huge, e.g. Arbitrum). Both produce identical rows.
    use_receipts: bool = False


# USDC (native, Circle-issued, EIP-3009-compliant) per chain.
BASE = EvmChain(
    name="base",
    chain_id=8453,
    usdc="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    default_rpc="https://mainnet.base.org",
    rpc_env="X402_BASE_RPC",
    confirmations=30,               # ~60s at Base's ~2s/block
    bootstrap_lookback_blocks=43_200,   # ~24h
    history_anchor_block=29_700_000,    # ~x402 launch on Base (2025-05)
    subrange=400,
)

POLYGON = EvmChain(
    name="polygon",
    chain_id=137,
    # native Circle USDC on Polygon PoS (implements EIP-3009); NOT bridged USDC.e
    usdc="0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
    # polygon-rpc.com is disabled (tenant-disabled 403); drpc.org's public
    # endpoint works AND returns blockTimestamp on logs (the indexer requires
    # it). A keyed X402_POLYGON_RPC is still preferred for the daily job.
    default_rpc="https://polygon.drpc.org",
    rpc_env="X402_POLYGON_RPC",
    # Polygon PoS sees occasional short reorgs; keep well clear of the tip.
    # Conservative default (~4–5 min at ~2.1s/block); refine after the probe.
    confirmations=128,
    bootstrap_lookback_blocks=40_000,   # ~24h at ~2.1s/block
    # Anchor for opt-in backfill only (forward-bootstrap ignores it). x402 went
    # live on Polygon later than Base; refine to the true launch block via probe.
    history_anchor_block=68_000_000,
    subrange=400,
)

# CDP-facilitator EVM chain (native Circle USDC, confirmed by CDP docs).
ARBITRUM = EvmChain(
    name="arbitrum",
    chain_id=42161,
    usdc="0xaf88d065e77c8cc2239327c5edb3a432268e5831",
    default_rpc="https://arb1.arbitrum.io/rpc",   # drpc.org 500s on getLogs
    rpc_env="X402_ARBITRUM_RPC",
    # Arbitrum's block number is its own fast sequence (~0.25s); confirmations
    # in Arbitrum blocks. Kept generous for L1-finality safety.
    confirmations=120,
    bootstrap_lookback_blocks=300_000,   # ~24h at ~0.25s/block
    history_anchor_block=280_000_000,
    subrange=2000,                       # wider walk: auth getLogs is cheap
    # ~345k blocks/day + very high USDC volume → receipt-based fetch, not a
    # full Transfer scan.
    use_receipts=True,
)

# Broader-ecosystem x402 EVM chains (x402-rs / PayAI / Dexter facilitators).
# native Circle USDC per chain.
AVALANCHE = EvmChain(
    name="avalanche",
    chain_id=43114,
    usdc="0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e",
    default_rpc="https://avalanche.drpc.org",
    rpc_env="X402_AVALANCHE_RPC",
    confirmations=20,                    # sub-second finality on Avalanche
    bootstrap_lookback_blocks=40_000,    # ~24h at ~2s/block
    history_anchor_block=52_000_000,
    subrange=400,
)

OPTIMISM = EvmChain(
    name="optimism",
    chain_id=10,
    usdc="0x0b2c639c533813f4aa9d7837caf62653d097ff85",
    default_rpc="https://optimism.drpc.org",
    rpc_env="X402_OPTIMISM_RPC",
    confirmations=30,                    # ~60s at ~2s/block (OP-stack, like Base)
    bootstrap_lookback_blocks=43_200,
    history_anchor_block=128_000_000,
    subrange=400,
)

SEI = EvmChain(
    name="sei",
    chain_id=1329,
    usdc="0xe15fc38f6d8c56af07bbcbe3baf5708a2bf42392",  # native Circle USDC on Sei
    default_rpc="https://sei.drpc.org",
    rpc_env="X402_SEI_RPC",
    confirmations=40,                    # Sei ~400ms blocks; fast finality
    bootstrap_lookback_blocks=200_000,   # ~24h at ~0.4s/block
    history_anchor_block=160_000_000,
    subrange=400,
)

# Tempo — Stripe/Paradigm's payments L1 (mainnet live 2026-03-18). Stripe's
# x402 product settles USDC here (docs.stripe.com/payments/machine/x402), BUT:
# probed 2026-07-10, the enshrined USDC contract emitted ZERO EIP-3009
# AuthorizationUsed events over ~17h while doing ~130k Transfers/day — most
# transfer txs have EMPTY calldata (to=None, input=0x), i.e. Tempo settles
# stablecoin payments NATIVELY at the protocol level, not via
# transferWithAuthorization. The EIP-3009 engine would therefore report a
# FALSE ZERO ("no x402 on Tempo") when x402 flows through a different
# mechanism. Config kept here for probes; DO NOT add to the nightly
# ADDITIVE_EVM_CHAINS until a Tempo-native path (Transfer events scoped by
# facilitator tx.from, Solana-style) exists and Tempo facilitator relayer
# addresses are known.
TEMPO = EvmChain(
    name="tempo",
    chain_id=4217,
    usdc="0x20c000000000000000000000b9537d11c60e8b50",  # enshrined USDC
    default_rpc="https://rpc.tempo.xyz",
    rpc_env="X402_TEMPO_RPC",
    confirmations=40,                    # ~0.5s blocks, fast deterministic finality
    bootstrap_lookback_blocks=172_800,   # ~24h at ~0.5s/block
    history_anchor_block=0,              # mainnet young; refine if backfill wanted
    subrange=2000,
)

CHAINS: dict[str, EvmChain] = {
    c.name: c for c in (BASE, POLYGON, ARBITRUM, AVALANCHE, OPTIMISM, SEI, TEMPO)
}


def start_meta_key(chain: EvmChain) -> str:
    """Per-chain `start_block` meta key.

    Base keeps the legacy unscoped key so existing Base DBs (with `start_block`
    already set in the Actions cache) resume unchanged; new chains are scoped.
    """
    return "start_block" if chain.name == "base" else f"start_block_{chain.name}"

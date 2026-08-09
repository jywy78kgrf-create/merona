"""Facilitator-watch triage — the split between auto-heal and human review.

Solana facilitators are AUTO-ADMITTED by the promotion engine (rotation or
declared-seller evidence), so they are NOT returned to the human. EVM leads
still need a person (EVM auto-admission isn't built), so they pass through.
This pins that split so a refactor can't accidentally start emailing Solana
leads for a rubber-stamp — the exact anti-pattern the operator rejected.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))
sys.path.insert(0, str(ROOT / "indexer"))

import facilitator_watch as fw                                # noqa: E402


def test_solana_leads_never_go_to_human_review():
    fresh = [
        {"chain": "solana", "address": "A", "verdict": "FACILITATOR"},
        {"chain": "solana", "address": "B", "verdict": "COUNTERPARTY_NOT_RELAYER"},
        {"chain": "solana", "address": "D", "verdict": None},
    ]
    assert fw.evm_review_leads(fresh) == []    # all solana -> auto-handled


def test_evm_leads_pass_through():
    fresh = [
        {"chain": "base", "address": "0xabc", "settlements": 900},
        {"chain": "polygon", "address": "0xdef", "settlements": 700},
    ]
    assert len(fw.evm_review_leads(fresh)) == 2


def test_mixed_returns_only_evm():
    fresh = [
        {"chain": "base", "address": "0x1"},
        {"chain": "solana", "address": "S1", "verdict": "FACILITATOR"},
        {"chain": "solana", "address": "S2", "verdict": "THIN"},
    ]
    got = {l["address"] for l in fw.evm_review_leads(fresh)}
    assert got == {"0x1"}


def test_verify_solana_noops_without_rpc_or_addrs():
    assert fw.verify_solana([], "https://rpc") == {}
    assert fw.verify_solana(["A"], None) == {}
    assert fw.verify_solana(["A"], "") == {}


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

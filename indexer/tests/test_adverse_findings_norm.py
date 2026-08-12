"""Adverse-findings cache normalization (2026-08-12 audit).

Every lookup path (trust_lookup, agent_lookup, ...) normalizes addresses by
the one rule: EVM lowercased, base58 case preserved. _adverse_findings()
lowercased unconditionally, so a curated Solana finding was stored under a
key no lookup could ever produce — a hand-verified fraud record that would
silently never surface, on the chain type where curated findings matter
most. Dormant until the first Solana finding was filed; pinned here so it
stays fixed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "serving"))

import trust_api                                     # noqa: E402

SOL = "FZAdmQBEeSXFkAsGtvNa8TS8jP9UkKC2sVWEo7VBQ7Ep"   # mixed-case base58
EVM = "0x" + "AB" * 20                                  # checksummed-style hex


def _load(tmp_path, monkeypatch, findings):
    p = tmp_path / "adverse.json"
    p.write_text(json.dumps({"findings": findings}))
    monkeypatch.setattr(trust_api, "ADVERSE_PATH", p)
    monkeypatch.setattr(trust_api, "_adverse", None)   # bust the cache
    out = trust_api._adverse_findings()
    monkeypatch.setattr(trust_api, "_adverse", None)   # don't leak to others
    return out


def test_solana_finding_keeps_base58_case(tmp_path, monkeypatch):
    out = _load(tmp_path, monkeypatch,
                [{"chain": "solana", "address": SOL, "kind": "self_wash"}])
    # stored under the case-preserved key every lookup path produces
    assert ("solana", SOL) in out
    assert ("solana", SOL.lower()) not in out


def test_evm_finding_still_lowercases(tmp_path, monkeypatch):
    out = _load(tmp_path, monkeypatch,
                [{"chain": "base", "address": EVM, "kind": "self_wash"}])
    assert ("base", EVM.lower()) in out
    assert ("base", EVM) not in out


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

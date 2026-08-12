"""Auto-admission of Solana facilitators — the gate on merona's own numbers.

This is the one place merona writes its OWN published tally without a human, so
the bar is pinned hard: only a rotation of a registered relayer or a candidate
serving confirmed x402 sellers is admitted; everything else declines. And the
registry mutation must be durable (curated file), immediate (live registry),
idempotent, and auditable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "indexer"))

import promote_solana_facilitators as ps                      # noqa: E402


def _cand(addr, sellers, verdict="FACILITATOR"):
    return {"address": addr, "verdict": verdict, "sellers_all": sellers}


def test_rotation_of_registered_relayer_is_promoted():
    reg = {"KNOWN_RELAYER": {"s1", "s2", "s3", "s4"}}
    cand = _cand("NEW_WALLET", ["s1", "s2", "s3"])       # 3/3 overlap
    decision, ev = ps.classify_promotion(cand, reg, confirmed_sellers=set())
    assert decision == "ROTATION" and ev["rotates_from"] == "KNOWN_RELAYER"
    assert ev["shared_sellers"] == 3


def test_declared_match_when_serves_confirmed_sellers():
    confirmed = {"m1", "m2", "m3", "m4"}
    cand = _cand("NOVEL", ["m1", "m2", "m3", "x9"])       # 3/4 confirmed
    decision, ev = ps.classify_promotion(cand, {}, confirmed)
    assert decision == "DECLARED_MATCH"
    assert ev["confirmed_sellers_served"] == 3


def test_decline_when_unprovable():
    # passes behaviour bar but serves unknown sellers, not a rotation
    cand = _cand("MYSTERY", ["u1", "u2", "u3", "u4", "u5"])
    decision, ev = ps.classify_promotion(cand, {"K": {"a", "b"}}, {"m1"})
    assert decision == "DECLINE"


def test_non_facilitator_verdict_never_promoted():
    cand = _cand("X", ["m1", "m2", "m3"], verdict="COUNTERPARTY_NOT_RELAYER")
    # even with perfect confirmed overlap, a failed bar can't be promoted
    d, _ = ps.classify_promotion(cand, {}, {"m1", "m2", "m3"})
    assert d == "DECLINE"


def test_rotation_needs_both_count_and_fraction():
    reg = {"K": {"s1", "s2", "s3"}}
    # 3 shared but only 3/10 of the candidate's sellers -> not a clean rotation
    cand = _cand("N", ["s1", "s2", "s3", "a", "b", "c", "d", "e", "f", "g"])
    d, _ = ps.classify_promotion(cand, reg, set())
    assert d == "DECLINE"


def _live(tmp_path, sol_existing):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({
        "relayers_by_chain": {"solana": sorted(sol_existing)},
        "solana_relayers": sorted(sol_existing),
        "counts": {"solana_relayers": len(sol_existing),
                   "by_chain": {"solana": len(sol_existing)}}}))
    return p


def test_apply_writes_curated_live_and_log_idempotently(tmp_path):
    live = _live(tmp_path, {"OLD1"})
    cur = tmp_path / "curated.json"
    log = tmp_path / "promotions.json"
    promos = [
        {"address": "NEW1", "decision": "ROTATION", "evidence": {}},
        {"address": "NEW2", "decision": "DECLARED_MATCH", "evidence": {}},
        {"address": "NOPE", "decision": "DECLINE", "evidence": {}},
        {"address": "OLD1", "decision": "ROTATION", "evidence": {}},  # already in
    ]
    added = ps.apply_promotions(promos, "2026-08-08T00:00:00",
                                curated_path=cur, live_path=live, log_path=log)
    assert set(added) == {"NEW1", "NEW2"}                 # DECLINE + existing out

    livej = json.loads(live.read_text())
    assert set(livej["relayers_by_chain"]["solana"]) == {"OLD1", "NEW1", "NEW2"}
    assert livej["counts"]["solana_relayers"] == 3
    curj = json.loads(cur.read_text())
    caddr = {e["address"] for e in
             curj["facilitators"][ps.AUTO_ID]["solana"]}
    assert caddr == {"NEW1", "NEW2"}
    assert len(json.loads(log.read_text())) == 2

    # re-apply the same batch: nothing new, no duplicates anywhere
    added2 = ps.apply_promotions(promos, "2026-08-09T00:00:00",
                                 curated_path=cur, live_path=live, log_path=log)
    assert added2 == []


# --- declared-payTo corroboration source (audit fix) -------------------------
# _catalog_solana_paytos used to walk service_directory_v0.json for dict nodes
# with singular 'network'+'payTo'/'pay_to' keys — that schema doesn't exist
# anywhere in the file (it's a 'networks' list, no payTo at all), so the
# function always returned set() and DECLARED_MATCH's extra corroboration was
# dead code. It now sources declared Solana payTos from the committed Bazaar
# CSV instead.

@pytest.mark.skipif(not ps.CATALOG.exists(),
                    reason="data/processed/bazaar_resources.csv not in this checkout")
def test_catalog_solana_paytos_nonempty_from_real_csv():
    """Run against the real committed CSV (not a synthetic fixture) — the
    whole point of the fix is that the OLD source (service_directory_v0.json)
    silently returned set() forever; this must not repeat that with a new
    silent-empty source."""
    out = ps._catalog_solana_paytos()
    assert len(out) > 0
    # base58 case preserved (repo-wide rule: a lowercased base58 matches
    # nothing) -- the real CSV has case-preserved addresses, so at least one
    # entry must carry uppercase characters.
    assert any(p != p.lower() for p in out)


def test_catalog_solana_paytos_handles_both_network_dialects(tmp_path, monkeypatch):
    """'solana' (legacy label) and 'solana:<genesis>' (CAIP-2) both count;
    non-Solana rows and blank payTo are excluded."""
    csv_path = tmp_path / "bazaar_resources.csv"
    csv_path.write_text(
        "resource_url,network,pay_to\n"
        "https://a.example/x,solana,SoLLegacyPayTo11111111111111111111111111\n"
        "https://b.example/y,solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp,"
        "SoLCaip2PayTo222222222222222222222222222\n"
        "https://c.example/z,eip155:8453,0xNotSolana000000000000000000000000000000\n"
        "https://d.example/w,solana,\n")
    monkeypatch.setattr(ps, "CATALOG", csv_path)
    out = ps._catalog_solana_paytos()
    assert out == {"SoLLegacyPayTo11111111111111111111111111",
                   "SoLCaip2PayTo222222222222222222222222222"}


def test_catalog_solana_paytos_missing_file_returns_empty(tmp_path, monkeypatch):
    """CI checkouts without the big committed CSV must not crash — empty set,
    same graceful-absence contract as the old catalog source."""
    monkeypatch.setattr(ps, "CATALOG", tmp_path / "does_not_exist.csv")
    assert ps._catalog_solana_paytos() == set()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

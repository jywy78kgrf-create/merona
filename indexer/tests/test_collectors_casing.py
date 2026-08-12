"""collectors/ payTo casing: EVM (0x-prefixed) addresses are case-insensitive
and get canonicalized to lowercase; Solana/base58 addresses are CASE-SENSITIVE
— lowercasing one destroys it (matches nothing ever again). Two audit fixes
pinned here:

  1. joins.py unconditionally lowercased pay_to (collectors/joins.py:57) —
     any Solana join row was silently corrupted.
  2. collect.py's diff_snapshots compares accepts by exact JSON string, so
     historical snapshots written before canon_listing's casing fix (which
     had the SAME unconditional-lowercase bug, since fixed) permanently
     carry lowercased Solana pay_to on disk. Comparing those literally
     against a case-preserved successor misreported 63.7% of "price changes"
     between two real committed snapshots as real when they were pure
     casing artifacts (see price_key()'s docstring for the exact numbers).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "collectors"))

import collect  # noqa: E402
import joins  # noqa: E402

BASE58_PAYTO = "6apuZvJQ51Led9iEjnHw6f5jfnXL4qjt8S1h58PeXzuR"   # real casing
BASE58_LOWER = BASE58_PAYTO.lower()                             # old-bug form
EVM_PAYTO = "0x" + "AB" * 20


# --- joins.py ------------------------------------------------------------

def test_norm_pay_to_preserves_base58_lowercases_evm():
    assert joins.norm_pay_to(BASE58_PAYTO) == BASE58_PAYTO      # untouched
    assert joins.norm_pay_to(EVM_PAYTO) == EVM_PAYTO.lower()    # folded
    assert joins.norm_pay_to(None) == ""
    assert joins.norm_pay_to("  " + EVM_PAYTO + "  ") == EVM_PAYTO.lower()


def test_joins_main_preserves_base58_case(tmp_path, monkeypatch):
    """End-to-end: a Solana accepts leg written to a catalog snapshot must
    reach joins.jsonl with its payTo case intact."""
    import gzip
    import json

    out = tmp_path / "data" / "collectors"
    (out / "catalog").mkdir(parents=True)
    snap = out / "catalog" / "2026-08-11.jsonl.gz"
    with gzip.open(snap, "wt") as f:
        f.write(json.dumps({
            "url": "https://api.sol.example/v1",
            "accepts": [{"pay_to": BASE58_PAYTO, "network": "solana",
                        "asset": "usdc"}],
        }) + "\n")
    monkeypatch.setattr(joins, "OUT", out)
    monkeypatch.setattr(joins, "utc_today", lambda: "2026-08-11")
    assert joins.main() == 0
    rows = [json.loads(line) for line in
            (out / "joins.jsonl").read_text().splitlines() if line.strip()]
    assert rows[0]["pay_to"] == BASE58_PAYTO   # case preserved end-to-end


# --- collect.py: canon_listing (storage-side canonicalization) -----------

def test_canon_listing_preserves_base58_lowercases_evm():
    listing = collect.canon_listing({
        "resource": "https://api.mixed.example/v1",
        "accepts": [
            {"payTo": BASE58_PAYTO, "network": "solana", "asset": "usdc",
             "amount": "1000"},
            {"payTo": EVM_PAYTO, "network": "eip155:8453", "asset": "usdc",
             "amount": "1000"},
        ],
    })
    pay_tos = {a["pay_to"] for a in listing["accepts"]}
    assert BASE58_PAYTO in pay_tos          # base58: untouched
    assert EVM_PAYTO.lower() in pay_tos     # 0x: folded
    assert EVM_PAYTO not in pay_tos         # not left mixed-case


# --- collect.py: diff_snapshots / price_key (comparison-only casefold) ----

def _listing(pay_to, network="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"):
    return collect.canon_listing({
        "resource": "https://api.diff.example/v1",
        "accepts": [{"payTo": pay_to, "network": network, "asset": "usdc",
                    "amount": "1000"}],
    })


def test_diff_ignores_historical_lowercasing_artifact():
    """The exact historical bug: prev snapshot has Solana pay_to lowercased
    (old canon_listing bug, since fixed); cur snapshot has the SAME address
    properly cased. Must NOT be reported as a price change."""
    prev = [_listing(BASE58_LOWER)]
    cur = [_listing(BASE58_PAYTO)]
    diff = collect.diff_snapshots(prev, cur)
    assert diff["price_changes"] == []
    assert diff["added"] == [] and diff["removed"] == []


def test_diff_still_reports_a_real_payto_change():
    """A genuine payTo rotation (not just a casing artifact) must still be
    caught — the fix must not blind the diff to real changes."""
    other_wallet = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
    prev = [_listing(BASE58_PAYTO)]
    cur = [_listing(other_wallet)]
    diff = collect.diff_snapshots(prev, cur)
    assert len(diff["price_changes"]) == 1
    assert diff["price_changes"][0]["url"] == "https://api.diff.example/v1"


def test_diff_still_reports_a_real_evm_payto_change():
    """0x-side regression guard: EVM payTo changes (already case-folded at
    storage time) must still be detected by the comparison."""
    prev = [_listing("0x" + "11" * 20, network="eip155:8453")]
    cur = [_listing("0x" + "22" * 20, network="eip155:8453")]
    diff = collect.diff_snapshots(prev, cur)
    assert len(diff["price_changes"]) == 1

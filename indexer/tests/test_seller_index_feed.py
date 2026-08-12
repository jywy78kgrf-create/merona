"""build_seller_index_feed (serving/stats_server.py): maps the cached stats
blob to the public, citable /real-seller-index.json feed.

Pure function, no server/DB needed — a synthetic blob shaped like compute()'s
return value is enough. See test_seller_index.py for the DB-backed test of
build_seller_index itself (the thing that fills stats["seller_index"]); this
file only tests the mapping from that blob to the public feed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                          # indexer/
sys.path.insert(0, str(HERE.parent.parent / "serving"))        # serving/

import stats_server                                            # noqa: E402

# Mirrors the context numbers this task was scoped against (Base tiers
# ~ any 90,530 / active 1,227 / established 444 / substantial 275 / busy 100)
# so a reader skimming this fixture recognizes the real shape.
BASE_TIERS = [
    {"tier": "any", "min_payers": 0, "min_usd": 0,
     "sellers": 90530, "volume_usd": 54200.11},
    {"tier": "active", "min_payers": 2, "min_usd": 1,
     "sellers": 1227, "volume_usd": 48900.02},
    {"tier": "established", "min_payers": 3, "min_usd": 5,
     "sellers": 444, "volume_usd": 46500.50},
    {"tier": "substantial", "min_payers": 5, "min_usd": 10,
     "sellers": 275, "volume_usd": 45210.00},
    {"tier": "busy", "min_payers": 10, "min_usd": 50,
     "sellers": 100, "volume_usd": 46600.00},
]

POLYGON_TIERS = [
    {"tier": "any", "min_payers": 0, "min_usd": 0, "sellers": 3210, "volume_usd": 900.0},
    {"tier": "active", "min_payers": 2, "min_usd": 1, "sellers": 88, "volume_usd": 700.0},
]


def _blob(**overrides):
    blob = {
        "generated_at": "2026-08-11T02:20:00+00:00",
        "metrics_at": "2026-08-11T02:17:03+00:00",
        "chains": {}, "since": {}, "totals": {}, "instr": {},
        "seller_index": {
            "method": ("x402-scoped: settlements via registry facilitators "
                       "only; distinct external payers = payers excluding "
                       "self-pay; lifetime USDC volume"),
            "tiers": {"base": BASE_TIERS},
        },
        "ledger": None, "tempo": None,
        "cycle": {}, "health": {},
    }
    blob.update(overrides)
    return blob


def test_citation_has_active_any_and_as_of_date():
    feed = stats_server.build_seller_index_feed(_blob())
    assert feed is not None
    assert "1,227" in feed["citation"]          # active
    assert "90,530" in feed["citation"]         # any
    assert "Base" in feed["citation"]
    assert "2026-08-11" in feed["citation"]     # as_of date, from metrics_at
    assert feed["as_of"] == "2026-08-11T02:17:03+00:00"
    assert feed["citation"].endswith("merona.io/real-seller-index")


def test_prefers_metrics_at_over_generated_at():
    blob = _blob(metrics_at=None)               # nightly precompute stamp missing
    feed = stats_server.build_seller_index_feed(blob)
    assert feed["as_of"] == blob["generated_at"]
    assert "2026-08-11" in feed["citation"]      # generated_at's date, as fallback


def test_license_mentions_cc_by():
    feed = stats_server.build_seller_index_feed(_blob())
    assert "CC BY" in feed["license"]


def test_chains_present_only_when_in_blob():
    # base only in the blob -> only base in the feed
    feed = stats_server.build_seller_index_feed(_blob())
    assert set(feed["chains"]) == {"base"}
    assert feed["chains"]["base"]["tiers"] == BASE_TIERS

    # base + polygon in the blob -> both present, solana absent
    blob = _blob()
    blob["seller_index"]["tiers"]["polygon"] = POLYGON_TIERS
    feed2 = stats_server.build_seller_index_feed(blob)
    assert set(feed2["chains"]) == {"base", "polygon"}
    assert "solana" not in feed2["chains"]
    assert feed2["chains"]["polygon"]["tiers"] == POLYGON_TIERS


def test_scope_and_links_and_name():
    feed = stats_server.build_seller_index_feed(_blob())
    assert feed["name"] == "merona Real Seller Index"
    assert feed["version"] == "1"
    assert "x402-scoped" in feed["scope"]
    assert "facilitator" in feed["scope"].lower()
    assert feed["links"]["page"] == "https://merona.io/real-seller-index"
    assert feed["links"]["methodology"] == "https://merona.io/real-seller-index#method"
    assert feed["links"]["dashboard"] == "https://merona.io"
    assert feed["links"]["anchors"] == "https://github.com/jywy78kgrf-create/merona-anchors"
    assert feed["links"]["verify"] == (
        "https://github.com/jywy78kgrf-create/merona/tree/main/conformance")


def test_none_when_no_seller_index():
    assert stats_server.build_seller_index_feed(_blob(seller_index=None)) is None
    blob = _blob()
    del blob["seller_index"]
    assert stats_server.build_seller_index_feed(blob) is None


def test_none_when_seller_index_not_a_dict():
    assert stats_server.build_seller_index_feed(_blob(seller_index="oops")) is None


def test_none_when_stats_blob_itself_missing():
    assert stats_server.build_seller_index_feed(None) is None
    assert stats_server.build_seller_index_feed({}) is None


def test_json_round_trips():
    feed = stats_server.build_seller_index_feed(_blob())
    again = json.loads(json.dumps(feed))
    assert again == feed


def test_citation_numbers_match_blob_base_tiers():
    """Pull active/any straight from the fixture (not hardcoded twice) so this
    test breaks if the mapping ever reads the wrong tier."""
    blob = _blob()
    by_tier = {t["tier"]: t for t in blob["seller_index"]["tiers"]["base"]}
    active_n = by_tier["active"]["sellers"]
    any_n = by_tier["any"]["sellers"]
    feed = stats_server.build_seller_index_feed(blob)
    assert f"{active_n:,}" in feed["citation"]
    assert f"{any_n:,}" in feed["citation"]


def test_missing_active_or_any_tier_degrades_not_crashes():
    """Base present but without the active/any tiers (an unexpected but not
    impossible shape) must never raise — the citation just can't state a
    number it doesn't have."""
    blob = _blob()
    blob["seller_index"]["tiers"]["base"] = [
        t for t in BASE_TIERS if t["tier"] not in ("active", "any")]
    feed = stats_server.build_seller_index_feed(blob)
    assert feed is not None
    assert "—" in feed["citation"]

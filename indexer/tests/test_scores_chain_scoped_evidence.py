"""Chain-scoped evidence joins in compute_seller_scores (2026-08-12 audit).

The endpoint-health and domain-age components join bazaar_census evidence to
scored sellers. Keying that join by wallet address alone let an address's
Base listing (endpoint grade, domain age) leak into the SAME address's
Polygon score — an EVM key works on every chain, but the two sellers are
economically independent. The join is now keyed (chain, wallet), with census
network labels mapped to chain names through payto_watch's canonicalizer
('base' and 'eip155:8453' are the same chain in the wild), and a wildcard
bucket for unmappable network labels so exotic listings degrade to the old
behavior instead of vanishing.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import scores                                        # noqa: E402
from storage import Store                            # noqa: E402

ADDR = "0x" + "de" * 20          # same address, seller on two chains
DATE = "2026-08-06"


def _setup(tmp_path, monkeypatch, census_network: str):
    now = int(time.time())
    snap = tmp_path / "snapshots" / DATE
    snap.mkdir(parents=True)
    with open(snap / "seller_aggregates.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["chain", "seller", "tx_count",
                                          "unique_payers", "self_pay_tx",
                                          "first_ts", "last_ts"])
        w.writeheader()
        for chain in ("base", "polygon"):
            w.writerow(dict(chain=chain, seller=ADDR, tx_count=5000,
                            unique_payers=900, self_pay_tx=0,
                            first_ts=now - 60 * 86400, last_ts=now))
    monkeypatch.setattr(scores, "SNAP_DIR", snap.parent)
    monkeypatch.setattr(scores, "FLAGS_DIR", tmp_path)
    monkeypatch.setattr(scores, "MIN_MATURE_DAYS", 1)
    monkeypatch.setattr(scores, "CENSUS_DIRECTORY", tmp_path / "none.json")
    s = Store(tmp_path / "t.sqlite")
    # census lists the address under census_network ONLY, with a strong
    # endpoint grade and a 10-year-old domain behind it
    s.record_bazaar_census_rows(DATE, [{
        "resource": "https://good-seller.example/api",
        "domain": "good-seller.example", "network": census_network,
        "asset": "USDC", "amount_raw": "1000", "amount_usd": 0.001,
        "seller_wallet": ADDR, "service": "t"}])
    s.db.execute(
        "INSERT INTO endpoint_scores(measured_date,origin,days_observed,"
        "probes,score,measured_at) VALUES(?,?,?,?,?,?)",
        (DATE, "https://good-seller.example", 30, 100, 95.0, DATE))
    s.record_domain_intel(DATE, "good-seller.example",
                          {"whois_created": "2016-01-01T00:00:00Z"}, DATE)
    s.db.commit()
    return s


def _components(s):
    rows = s.db.execute("SELECT chain, components FROM seller_scores "
                        "WHERE seller=?", (ADDR,)).fetchall()
    return {ch: json.loads(c) for ch, c in rows}


def test_base_listing_does_not_leak_into_polygon_score(tmp_path, monkeypatch):
    s = _setup(tmp_path, monkeypatch, census_network="base")
    assert scores.compute_seller_scores(s, DATE) == 2
    comp = _components(s)
    # Base: listed, earns the graded endpoint component (25 * 95/100)
    assert comp["base"]["endpoint"] > 20
    # Polygon: never listed there — must score as UNLISTED (neutral 10.0),
    # not inherit Base's endpoint grade or domain age
    assert comp["polygon"]["endpoint"] == 10.0
    assert comp["polygon"]["domain"] != comp["base"]["domain"] or \
        comp["base"]["domain"] == 8.0
    s.close()


def test_caip2_network_label_still_reaches_its_chain(tmp_path, monkeypatch):
    """The census names Base both 'base' and 'eip155:8453'; the CAIP-2 form
    must land on the base-chain seller identically."""
    s = _setup(tmp_path, monkeypatch, census_network="eip155:8453")
    assert scores.compute_seller_scores(s, DATE) == 2
    comp = _components(s)
    assert comp["base"]["endpoint"] > 20
    assert comp["polygon"]["endpoint"] == 10.0
    s.close()


def test_unmappable_network_degrades_to_wildcard(tmp_path, monkeypatch):
    """A network label the canonicalizer doesn't know must not vanish — it
    lands in the wildcard bucket and remains visible to any chain that has
    no evidence of its own (the pre-fix behavior, scoped to last resort)."""
    s = _setup(tmp_path, monkeypatch, census_network="weirdnet-v9")
    assert scores.compute_seller_scores(s, DATE) == 2
    comp = _components(s)
    assert comp["base"]["endpoint"] > 20
    assert comp["polygon"]["endpoint"] > 20
    s.close()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

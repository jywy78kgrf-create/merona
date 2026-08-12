"""payTo-mismatch feed: probe offer capture + watch logic, end to end on a
synthetic SQLite store. The feed is public — the false-positive discipline
IS the product, so the negative cases (match must not flag, EVM case must
not flag, base58 case MUST flag) matter as much as the positives."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from storage import Store  # noqa: E402
import instrumentation as I  # noqa: E402
import payto_watch  # noqa: E402

ADV = "0x" + "aa" * 20          # catalog-advertised payTo
LIVE = "0x" + "bb" * 20         # what the endpoint actually asks for
URL_MISMATCH = "https://api.shady.example/v1/data"
URL_OK = "https://api.honest.example/v1/data"
URL_ROT = "https://api.rotator.example/v1/feed"


class _Resp402:
    status_code = 402

    def __init__(self, accepts):
        self._body = json.dumps({"x402Version": 1, "accepts": accepts}).encode()

    def iter_content(self, n):
        yield self._body


def test_parse_402_offers_extracts_and_normalizes():
    valid, offers = I._parse_402_offers(_Resp402([
        {"payTo": "0x" + "AB" * 20, "network": "eip155:8453", "asset": "0xusdc"},
        {"payTo": "0x" + "ab" * 20},                      # dup after lowercase
        {"payTo": "So1BASE58casePres" + "x" * 15},         # base58: preserved
        {"payTo": ""}, {"payTo": None}, "notadict",        # garbage: skipped
    ]))
    assert valid
    assert [o["payto"] for o in offers] == [
        "0x" + "ab" * 20, "So1BASE58casePres" + "x" * 15]
    assert offers[0]["network"] == "eip155:8453"
    # boolean shim unchanged for legacy callers
    assert I._looks_like_x402_402(_Resp402([{"payTo": ADV}])) is True


def _seed(store):
    """Census (catalog claims) on two dates + live offers on the probe date."""
    ins_c = ("INSERT INTO bazaar_census(measured_date,resource,domain,network,"
             "seller_wallet) VALUES(?,?,?,?,?)")
    # day 1: rotator advertised wallet A; day 2: rotated to wallet B
    store.db.execute(ins_c, ("2026-08-01", URL_ROT, "rotator.example",
                             "eip155:8453", "0x" + "11" * 20))
    store.db.execute(ins_c, ("2026-08-02", URL_ROT, "rotator.example",
                             "eip155:8453", "0x" + "22" * 20))
    # mismatch endpoint: catalog says ADV (checksummed casing — must still match
    # by value, flag only because LIVE differs)
    store.db.execute(ins_c, ("2026-08-02", URL_MISMATCH, "shady.example",
                             "eip155:8453", "0x" + "AA" * 20))
    # honest endpoint: catalog and live agree (different casing on purpose)
    store.db.execute(ins_c, ("2026-08-02", URL_OK, "honest.example",
                             "eip155:8453", "0x" + "CC" * 20))
    store.record_live_offer("2026-08-02", URL_MISMATCH,
                            {"payto": LIVE, "network": "eip155:8453"}, "t")
    store.record_live_offer("2026-08-02", URL_OK,
                            {"payto": "0x" + "cc" * 20,
                             "network": "eip155:8453"}, "t")
    # LIVE wallet has actually received a settlement -> seen_onchain True
    store.db.execute(
        "INSERT INTO settlements(tx_hash,log_index,chain,token,payer,seller,"
        "amount,block_number,block_timestamp) VALUES(?,?,?,?,?,?,?,?,?)",
        ("0xt1", 0, "base", "0xusdc", "0xp", LIVE, 1000, 1,
         int(time.time())))
    store.db.commit()


def test_feed_flags_mismatch_and_rotation_only(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    feed = payto_watch.write_feed(s, out=tmp_path / "feed.json")

    # catalog-vs-live: exactly the shady endpoint — honest (case-diff) is NOT
    # flagged, unprobed rotator is NOT flagged here
    cvl = feed["catalog_vs_live"]
    assert [e["resource"] for e in cvl] == [URL_MISMATCH]
    e = cvl[0]
    assert e["advertised_payto"] == ADV and e["live_paytos"] == [LIVE]
    assert e["advertised_seen_onchain"] is False        # never received
    assert e["live_seen_onchain"][LIVE] is True         # has settlements
    assert e["catalog_date"] == "2026-08-02" and e["probe_date"] == "2026-08-02"

    # rotation: exactly the rotator, dated timeline in order
    rot = feed["payto_rotation"]
    assert [r["resource"] for r in rot] == [URL_ROT]
    tl = rot[0]["timeline"]
    assert [d["payto"] for d in tl] == ["0x" + "11" * 20, "0x" + "22" * 20]
    assert tl[0]["first_seen"] == "2026-08-01" and tl[1]["first_seen"] == "2026-08-02"

    # the disclaimer travels inside the JSON, and the file is what was returned
    on_disk = json.loads((tmp_path / "feed.json").read_text())
    assert "not accusations" in on_disk["meta"]["disclaimer"]
    assert on_disk["meta"]["counts"] == {"catalog_vs_live": 1,
                                         "payto_rotation": 1}
    s.close()


def test_base58_case_difference_is_a_real_mismatch(tmp_path):
    """EVM addresses compare case-insensitively; base58 must NOT — a
    case-twiddled base58 payTo is a different wallet (classic swap scam)."""
    s = Store(tmp_path / "t.sqlite")
    url = "https://api.sol.example/v1"
    s.db.execute("INSERT INTO bazaar_census(measured_date,resource,domain,"
                 "network,seller_wallet) VALUES(?,?,?,?,?)",
                 ("2026-08-02", url, "sol.example", "solana",
                  "So1WalletAAAA" + "x" * 20))
    s.record_live_offer("2026-08-02", url,
                        {"payto": "so1walletaaaa" + "x" * 20,
                         "network": "solana"}, "t")
    s.db.commit()
    feed = payto_watch.compute_feed(s)
    assert [e["resource"] for e in feed["catalog_vs_live"]] == [url]
    s.close()


def test_search_feed_lookup(tmp_path):
    """The /check convenience path: one query answers from the same feed."""
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    feed = payto_watch.compute_feed(s)

    # by address — the LIVE wallet hits the mismatch entry
    r = payto_watch.search_feed(feed, LIVE)
    assert r["query_type"] == "address" and r["records_found"] == 1
    assert r["catalog_vs_live"][0]["resource"] == URL_MISMATCH
    # checksummed EVM input matches (normalized)
    r2 = payto_watch.search_feed(feed, "0x" + "BB" * 20)
    assert r2["records_found"] == 1

    # by endpoint URL and by bare hostname
    r3 = payto_watch.search_feed(feed, URL_ROT)
    assert r3["query_type"] == "endpoint"
    assert [e["resource"] for e in r3["payto_rotation"]] == [URL_ROT]
    r4 = payto_watch.search_feed(feed, "api.rotator.example")
    assert r4["records_found"] == 1

    # a rotation wallet hits its timeline entry
    r5 = payto_watch.search_feed(feed, "0x" + "11" * 20)
    assert [e["resource"] for e in r5["payto_rotation"]] == [URL_ROT]

    # absence is NOT clearance — stated in the response
    r6 = payto_watch.search_feed(feed, "0x" + "99" * 20)
    assert r6["records_found"] == 0 and "NOT a clearance" in r6["note"]
    # disclaimer + dates travel with every answer
    assert r6["disclaimer"] and r6["as_of"]["catalog"] == "2026-08-02"
    s.close()


def test_probe_records_live_offers(tmp_path, monkeypatch):
    """probe_endpoint_liveness persists what the endpoint asked for."""
    s = Store(tmp_path / "t.sqlite")

    class _Sess:
        headers = {}

        def get(self, url, **kw):
            return _Resp402([{"payTo": LIVE, "network": "eip155:8453"}])

    monkeypatch.setattr(I.requests, "Session", _Sess)
    monkeypatch.setattr(I.time, "sleep", lambda *_: None)
    _Resp402.headers = {}
    _Resp402.close = lambda self: None
    n = I.probe_endpoint_liveness(s, "2026-08-02", [
        {"resource": URL_MISMATCH, "origin": "https://api.shady.example",
         "seller_wallet": ADV, "network": "eip155:8453"}])
    assert n == 1
    rows = s.db.execute("SELECT endpoint_url, payto FROM live_offers").fetchall()
    assert rows == [(URL_MISMATCH, LIVE)]
    ok = s.db.execute("SELECT valid_402 FROM endpoint_liveness").fetchone()[0]
    assert bool(ok)
    s.close()


# --- per-network comparison (false-negative report by @chikop2p, 2026-08-11) --

INS_C = ("INSERT INTO bazaar_census(measured_date,resource,domain,network,"
         "seller_wallet) VALUES(?,?,?,?,?)")
D = "2026-08-11"
A, B, X = "0x" + "aa" * 20, "0x" + "bb" * 20, "0x" + "ee" * 20


def _feed(s):
    s.db.commit()
    return payto_watch.compute_feed(s)


def test_cross_network_mask_is_flagged(tmp_path):
    """THE reported bug: live payTos were unioned across networks, so a
    correct Polygon leg masked a mismatched Base leg. Catalog: Base pays A.
    Live: Base asks X (mismatch!), Polygon asks A. The union {X, A} contains
    A, so the old code stayed silent; per-network comparison must flag the
    Base leg."""
    s = Store(tmp_path / "t.sqlite")
    url = "https://api.mask.example/v1"
    s.db.execute(INS_C, (D, url, "mask.example", "eip155:8453", A))
    s.record_live_offer(D, url, {"payto": X, "network": "eip155:8453"}, "t")
    s.record_live_offer(D, url, {"payto": A, "network": "eip155:137"}, "t")
    cvl = _feed(s)["catalog_vs_live"]
    assert [e["resource"] for e in cvl] == [url]
    assert cvl[0]["advertised_payto"] == A
    assert cvl[0]["live_paytos"] == [X]           # the Base leg only
    s.close()


def test_flag_isolates_the_offending_leg(tmp_path):
    """Precision: catalog advertises Polygon->B; live serves a Base leg
    (uncatalogued, not a mismatch) and a Polygon leg asking X. The flag
    must carry ONLY the Polygon leg's payTo — the old union reported every
    chain's wallets as 'live asks', muddying the entry."""
    s = Store(tmp_path / "t.sqlite")
    url = "https://api.multi.example/v1"
    s.db.execute(INS_C, (D, url, "multi.example", "eip155:137", B))
    s.record_live_offer(D, url, {"payto": A, "network": "eip155:8453"}, "t")
    s.record_live_offer(D, url, {"payto": X, "network": "eip155:137"}, "t")
    cvl = _feed(s)["catalog_vs_live"]
    assert len(cvl) == 1
    assert cvl[0]["advertised_payto"] == B
    assert cvl[0]["live_paytos"] == [X]
    assert cvl[0]["live_network"] == "eip155:137"
    s.close()


def test_network_alias_pairs_base_with_caip2(tmp_path):
    """The committed census names the same chain both 'base' and
    'eip155:8453'; the two sides must canonicalize before pairing or naming
    drift silently unpairs them. Same wallet under different labels: no
    flag. Different wallet: flag."""
    s = Store(tmp_path / "t.sqlite")
    ok, bad = "https://api.alias-ok.example/v1", "https://api.alias-bad.example/v1"
    s.db.execute(INS_C, (D, ok, "alias-ok.example", "base", A))
    s.record_live_offer(D, ok, {"payto": A, "network": "eip155:8453"}, "t")
    s.db.execute(INS_C, (D, bad, "alias-bad.example", "base", A))
    s.record_live_offer(D, bad, {"payto": X, "network": "eip155:8453"}, "t")
    cvl = _feed(s)["catalog_vs_live"]
    assert [e["resource"] for e in cvl] == [bad]
    s.close()


def test_advertised_chain_unprobed_falls_back_to_union(tmp_path):
    """Catalog advertises Polygon->B but the probe only captured a Base
    leg asking A. There is no same-network pair, so the legacy union
    comparison applies and the divergence is still surfaced (recorded fact
    with both networks named — the disclaimer does the judging), rather
    than dropped."""
    s = Store(tmp_path / "t.sqlite")
    url = "https://api.perchain.example/v1"
    s.db.execute(INS_C, (D, url, "perchain.example", "eip155:137", B))
    s.record_live_offer(D, url, {"payto": A, "network": "eip155:8453"}, "t")
    cvl = _feed(s)["catalog_vs_live"]
    assert len(cvl) == 1
    assert cvl[0]["advertised_payto"] == B
    assert cvl[0]["advertised_network"] == "eip155:137"
    assert cvl[0]["live_paytos"] == [A]
    s.close()


def test_unpairable_network_falls_back_to_union(tmp_path):
    """A naming scheme _canon_net doesn't know must degrade to the legacy
    resource-level union comparison (one entry per URL), not to silence."""
    s = Store(tmp_path / "t.sqlite")
    url = "https://api.exotic.example/v1"
    s.db.execute(INS_C, (D, url, "exotic.example", "weirdnet-v9", A))
    s.record_live_offer(D, url, {"payto": X, "network": "weirdnet-vTEN"}, "t")
    cvl = _feed(s)["catalog_vs_live"]
    assert [e["resource"] for e in cvl] == [url]
    assert cvl[0]["advertised_payto"] == A and cvl[0]["live_paytos"] == [X]
    s.close()


# --- multi-leg census (audit fix: accepts[0]-only blinded the catalog side) --
# bazaar_census went from one row per resource (accepts[0] only) to one row
# per accepts LEG (PK measured_date,resource,accept_index). These pin the
# false-negative it closed, and the rotation false-positive that multi-leg
# census would otherwise open.

INS_LEG = ("INSERT INTO bazaar_census(measured_date,resource,accept_index,"
          "domain,network,seller_wallet) VALUES(?,?,?,?,?,?)")


def test_second_census_leg_mismatch_is_caught(tmp_path):
    """THE catalog-side false negative this fix closes: a resource lists TWO
    legs (Base->A, Polygon->B). Live Base asks A (fine); live Polygon asks X
    (a payTo swap). Under the old accepts[0]-only census, the Polygon leg
    was never recorded at all, so this mismatch was structurally
    undetectable. It must now be flagged, isolated to the Polygon leg."""
    s = Store(tmp_path / "t.sqlite")
    url = "https://api.twoleg.example/v1"
    s.db.execute(INS_LEG, (D, url, 0, "twoleg.example", "eip155:8453", A))
    s.db.execute(INS_LEG, (D, url, 1, "twoleg.example", "eip155:137", B))
    s.record_live_offer(D, url, {"payto": A, "network": "eip155:8453"}, "t")
    s.record_live_offer(D, url, {"payto": X, "network": "eip155:137"}, "t")
    cvl = _feed(s)["catalog_vs_live"]
    assert len(cvl) == 1
    assert cvl[0]["advertised_payto"] == B
    assert cvl[0]["advertised_network"] == "eip155:137"
    assert cvl[0]["live_network"] == "eip155:137"
    assert cvl[0]["live_paytos"] == [X]
    s.close()


def test_rotation_is_keyed_per_network_not_per_resource(tmp_path):
    """A resource legitimately advertising a DIFFERENT wallet per chain
    (Base->A, Polygon->B, unchanged across two census dates) must NOT read
    as a payTo rotation — that would be a false accusation on a public
    feed. A genuine same-network change (Base: A -> A2) must still be
    caught, with the network labeled on the entry."""
    s = Store(tmp_path / "t.sqlite")
    static_url = "https://api.static-multichain.example/v1"
    for d in ("2026-08-10", "2026-08-11"):
        s.db.execute(INS_LEG, (d, static_url, 0, "x", "eip155:8453", A))
        s.db.execute(INS_LEG, (d, static_url, 1, "x", "eip155:137", B))

    rotated_url = "https://api.real-rotation.example/v1"
    A2 = "0x" + "cc" * 20
    s.db.execute(INS_LEG, ("2026-08-10", rotated_url, 0, "y", "eip155:8453", A))
    s.db.execute(INS_LEG, ("2026-08-11", rotated_url, 0, "y", "eip155:8453", A2))

    rot = _feed(s)["payto_rotation"]
    resources = {r["resource"] for r in rot}
    assert static_url not in resources           # per-net wallets: not a rotation
    assert rotated_url in resources               # real same-network change: caught
    entry = next(r for r in rot if r["resource"] == rotated_url)
    assert entry["network"] == "eip155:8453"
    assert [t["payto"] for t in entry["timeline"]] == [A, A2]
    s.close()

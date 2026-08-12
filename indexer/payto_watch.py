"""payTo integrity watch — the FREE public mismatch feed.

Two recorded-fact signals, computed nightly from data we already collect:

  1. CATALOG vs LIVE — the Bazaar catalog claims the endpoint pays out to X
     (bazaar_census.seller_wallet); a live no-payment probe of the same URL
     got a valid x402 402 asking for Y (live_offers, captured by the
     liveness probe). An agent trusting the catalog would pay a different
     wallet than the endpoint currently demands.
  2. PAYTO ROTATION — the payout address a listing advertises has CHANGED
     over time (date-over-date diff of the census). Rotation can be
     legitimate ops hygiene; it can also be a hijacked listing or a
     take-the-money-and-rotate exit. We publish the dated timeline and let
     the reader judge.

Framing discipline (this feed is public): every entry is a RECORDED FACT
with dates and both addresses — never an accusation. The disclaimer ships
inside the JSON so no one can quote an entry without the caveat traveling
with it.

Output: data/indexer/payto_mismatches.json (box-written, served by the
dashboard at /mismatches.json). Observe-only; reads instrumentation tables,
writes only the JSON. Address normalization matches the pipeline rule:
EVM lowercased, base58 preserved (a lowercased base58 matches nothing).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "indexer" / "payto_mismatches.json"
FEED_CAP = 500          # per section; truncation is recorded, never silent

DISCLAIMER = (
    "Recorded facts, not accusations. Each entry states what the public "
    "catalog advertised and what the endpoint itself asked for (or previously "
    "advertised), with dates. A payout-address change or divergence can be "
    "legitimate (wallet rotation, multi-wallet ops, catalog lag). Verify "
    "before acting; verify before quoting.")


# The catalog and live 402 bodies name the same chain inconsistently in the
# wild — the committed census carries BOTH "base" (13,107 rows) and
# "eip155:8453" (9,914 rows). Mismatch comparison is per-network (see
# compute_feed), so the two sides must agree on what a network is CALLED
# before pairing, or naming drift silently unpairs them. Canonical form is
# CAIP-2 where one exists; original labels are preserved in feed entries
# for display.
_NET_ALIASES = {
    "base": "eip155:8453", "base-sepolia": "eip155:84532",
    "polygon": "eip155:137", "polygon-amoy": "eip155:80002",
    "arbitrum": "eip155:42161", "arbitrum-one": "eip155:42161",
    "optimism": "eip155:10", "avalanche": "eip155:43114",
    "bsc": "eip155:56", "bnb": "eip155:56",
    # Solana mainnet's CAIP-2 genesis-hash form, lowercased (labels are
    # lowercased for canonicalization only — addresses never are)
    "solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp": "solana",
}


def _canon_net(n: str | None) -> str:
    s = (n or "").strip().lower()
    return _NET_ALIASES.get(s, s)


def _norm(a: str | None) -> str | None:
    if not a or not isinstance(a, str):
        return None
    a = a.strip()
    if not a:
        return None
    return a.lower() if a.startswith(("0x", "0X")) else a


def _seen_onchain(store, wallets: set) -> dict:
    """{wallet: bool} — has this address ever received a settlement we
    indexed (any chain)? Existence only; bounded to the feed's wallets."""
    out = {}
    for w in wallets:
        try:
            row = store.db.execute(store.q(
                "SELECT 1 FROM settlements WHERE seller=? LIMIT 1"),
                (w,)).fetchone()
            out[w] = bool(row)
        except Exception:
            out[w] = None
    return out


def compute_feed(store) -> dict:
    """Build the feed dict (pure compute; write_feed persists it)."""
    census_date = store.db.execute(
        "SELECT MAX(measured_date) FROM bazaar_census").fetchone()[0]
    offers_date = store.db.execute(
        "SELECT MAX(measured_date) FROM live_offers").fetchone()[0]

    # --- 1. catalog vs live (same URL, both sides observed) ------------------
    # Comparison is PER NETWORK (canonicalized via _canon_net), not per
    # resource. The original version unioned live payTos across networks,
    # so a correct leg on chain A masked a mismatched leg on chain B — a
    # false negative reported by @chikop2p (2026-08-11). Per-network keying
    # fixes the masking and narrows a flagged entry's live_paytos to the
    # offending leg instead of every chain's. (bazaar_census is now one row
    # per ACCEPTS LEG — PRIMARY KEY(measured_date, resource, accept_index),
    # audit fix for the same class of catalog-side blind spot — so a
    # multi-network resource genuinely has multiple rows here; the per-net
    # sets below are exactly what makes that multi-row shape usable.)
    # When a URL's networks cannot be paired AT ALL (a naming scheme
    # _canon_net doesn't know, or the probe never captured the advertised
    # chain's leg), fall back to the legacy resource-level union comparison
    # so unknown schemes degrade to old coverage instead of silence.
    catalog_vs_live = []
    if census_date and offers_date:
        adv_by_net: dict = {}   # (resource, canon net) -> set of payTos
        adv_by_url: dict = {}   # resource -> [(payTo, original net label)]
        for res, w, net in store.db.execute(store.q(
                "SELECT resource, seller_wallet, network FROM bazaar_census "
                "WHERE measured_date=? AND seller_wallet IS NOT NULL"),
                (census_date,)):
            p = _norm(w)
            if p:
                adv_by_net.setdefault((res, _canon_net(net)), set()).add(p)
                adv_by_url.setdefault(res, []).append((p, net))
        live: dict = {}   # (endpoint_url, canon net) -> (orig label, {payTo})
        for url, pt, net in store.db.execute(store.q(
                "SELECT endpoint_url, payto, network FROM live_offers "
                "WHERE measured_date=?"), (offers_date,)):
            p = _norm(pt)
            if p:
                live.setdefault((url, _canon_net(net)),
                                (net, set()))[1].add(p)

        paired = {url for (url, cnet) in live if (url, cnet) in adv_by_net}
        legacy_done: set = set()
        for (url, cnet), (live_net, live_set) in sorted(live.items()):
            advset = adv_by_net.get((url, cnet))
            if advset:
                # same network on both sides: any advertised wallet being
                # asked for live means no mismatch on this leg
                if not advset.isdisjoint(live_set):
                    continue
                orig = [n for p, n in adv_by_url[url] if p in advset]
                catalog_vs_live.append({
                    "resource": url,
                    "advertised_payto": sorted(advset)[0],
                    "advertised_network": orig[0] if orig else live_net,
                    "live_network": live_net,
                    "live_paytos": sorted(live_set),
                    "catalog_date": census_date,
                    "probe_date": offers_date})
            elif url not in paired and url not in legacy_done:
                # no network pairing anywhere for this URL — legacy
                # resource-level comparison against the live union (one
                # entry per URL), kept so unknown naming schemes degrade to
                # the old coverage instead of silence
                legacy_done.add(url)
                advs = adv_by_url.get(url)
                union: dict = {}   # payTo -> orig live net label (any)
                for (u, _), (n, s) in live.items():
                    if u == url:
                        for p in s:
                            union.setdefault(p, n)
                if advs and all(p not in union for p, _ in advs):
                    catalog_vs_live.append({
                        "resource": url,
                        "advertised_payto": advs[0][0],
                        "advertised_network": advs[0][1],
                        "live_network": "/".join(sorted(set(union.values()))),
                        "live_paytos": sorted(union),
                        "catalog_date": census_date,
                        "probe_date": offers_date})

    # --- 2. payTo rotation over census history -------------------------------
    # Keyed PER NETWORK (canonicalized via _canon_net), same reasoning as
    # section 1: bazaar_census now carries multiple legs per resource (one
    # per accepts entry), so a resource that legitimately advertises a
    # DIFFERENT wallet per chain (base->A, polygon->B, unchanged over time)
    # must never read as a "rotation" — that would be a false accusation on
    # a public feed. Only a wallet change WITHIN the same network is a
    # rotation.
    rotations = []
    hist: dict = {}   # (resource, canon net) -> {payto: [first, last]}
    for res, w, net, lo, hi in store.db.execute(
            "SELECT resource, seller_wallet, network, MIN(measured_date), "
            "MAX(measured_date) FROM bazaar_census "
            "WHERE seller_wallet IS NOT NULL "
            "GROUP BY resource, seller_wallet, network"):
        p = _norm(w)
        if p:
            hist.setdefault((res, _canon_net(net)), {})[p] = [lo, hi]
    for (res, cnet), wallets in hist.items():
        if len(wallets) < 2:
            continue
        timeline = sorted(
            ({"payto": p, "first_seen": lo, "last_seen": hi}
             for p, (lo, hi) in wallets.items()),
            key=lambda d: (d["first_seen"], d["payto"]))
        rotations.append({
            "resource": res, "network": cnet, "n_wallets": len(wallets),
            "timeline": timeline,
            "latest_change": max(d["first_seen"] for d in timeline)})
    rotations.sort(key=lambda d: d["latest_change"], reverse=True)

    # --- enrich with the one thing only a settlement index can add ----------
    wallets: set = set()
    for e in catalog_vs_live[:FEED_CAP]:
        wallets.add(e["advertised_payto"])
        wallets.update(e["live_paytos"])
    for e in rotations[:FEED_CAP]:
        wallets.update(d["payto"] for d in e["timeline"])
    seen = _seen_onchain(store, wallets)
    for e in catalog_vs_live:
        e["advertised_seen_onchain"] = seen.get(e["advertised_payto"])
        e["live_seen_onchain"] = {p: seen.get(p) for p in e["live_paytos"]}
    for e in rotations:
        for d in e["timeline"]:
            d["seen_onchain"] = seen.get(d["payto"])

    truncated = {}
    if len(catalog_vs_live) > FEED_CAP:
        truncated["catalog_vs_live"] = len(catalog_vs_live) - FEED_CAP
    if len(rotations) > FEED_CAP:
        truncated["payto_rotation"] = len(rotations) - FEED_CAP

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "merona.io settlement index — free payTo integrity feed",
            "as_of": {"catalog": census_date, "live_probe": offers_date},
            "method": ("catalog_vs_live: Bazaar catalog payTo vs the payTo the "
                       "same URL returned to a no-payment 402 probe. "
                       "payto_rotation: dated history of advertised payTo "
                       "changes per listing. seen_onchain: address has "
                       "received at least one settlement in our index."),
            "disclaimer": DISCLAIMER,
            "counts": {"catalog_vs_live": len(catalog_vs_live),
                       "payto_rotation": len(rotations)},
            **({"truncated": truncated} if truncated else {}),
        },
        "catalog_vs_live": catalog_vs_live[:FEED_CAP],
        "payto_rotation": rotations[:FEED_CAP],
    }


def search_feed(feed: dict, q: str, cap: int = 25) -> dict:
    """Single-lookup convenience over the SAME free feed data: q is a wallet
    address (0x… or base58) or an endpoint URL/hostname. Returns the matching
    entries with the feed's dates and disclaimer attached. Pure function —
    the /check route serves it, tests exercise it directly.

    Honesty contract: zero records is NOT a clearance — the response says so
    explicitly. This checks payTo-integrity signals only."""
    q = (q or "").strip()
    is_addr = q.startswith(("0x", "0X")) or ("/" not in q and "." not in q)
    cvl, rot = [], []
    if is_addr:
        a = _norm(q)
        for e in feed.get("catalog_vs_live", []):
            if e.get("advertised_payto") == a or a in (e.get("live_paytos") or []):
                cvl.append(e)
        for e in feed.get("payto_rotation", []):
            if any(t.get("payto") == a for t in e.get("timeline", [])):
                rot.append(e)
    else:
        try:
            from urllib.parse import urlparse
            host = urlparse(q if "://" in q else "https://" + q).hostname
        except Exception:
            host = None
        for e in feed.get("catalog_vs_live", []):
            r = e.get("resource") or ""
            if r == q or (host and host in r):
                cvl.append(e)
        for e in feed.get("payto_rotation", []):
            r = e.get("resource") or ""
            if r == q or (host and host in r):
                rot.append(e)
    meta = feed.get("meta", {})
    return {
        "query": q, "query_type": "address" if is_addr else "endpoint",
        "as_of": meta.get("as_of"),
        "records_found": len(cvl) + len(rot),
        "catalog_vs_live": cvl[:cap],
        "payto_rotation": rot[:cap],
        "note": ("no payTo-integrity records for this query — absence of a "
                 "record is NOT a clearance; this checks payout-address "
                 "integrity signals only" if not (cvl or rot) else
                 "records are facts with dates, not accusations — see "
                 "disclaimer"),
        "disclaimer": meta.get("disclaimer"),
        "full_feed": "https://merona.io/mismatches.json",
        "trust_grades": "per-wallet seller grades: https://api.merona.io (keyed)",
    }


def write_feed(store, out: Path = OUT) -> dict:
    feed = compute_feed(store)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(feed, indent=1, sort_keys=True))
    tmp.replace(out)   # atomic: the served file is never half-written
    c = feed["meta"]["counts"]
    print(f"[payto-watch] feed written: {c['catalog_vs_live']} catalog-vs-live, "
          f"{c['payto_rotation']} rotations -> {out}")
    return feed


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from storage import open_store
    store = open_store(init_schema=False)
    write_feed(store)
    store.db.close()

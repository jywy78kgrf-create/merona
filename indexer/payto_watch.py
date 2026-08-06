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
    catalog_vs_live = []
    if census_date and offers_date:
        advertised = {}   # resource -> (advertised payTo, network)
        for res, w, net in store.db.execute(store.q(
                "SELECT resource, seller_wallet, network FROM bazaar_census "
                "WHERE measured_date=? AND seller_wallet IS NOT NULL"),
                (census_date,)):
            advertised[res] = (_norm(w), net)
        live: dict = {}   # endpoint_url -> [(payto, network)]
        for url, pt, net in store.db.execute(store.q(
                "SELECT endpoint_url, payto, network FROM live_offers "
                "WHERE measured_date=?"), (offers_date,)):
            live.setdefault(url, []).append((_norm(pt), net))
        for url, offs in sorted(live.items()):
            adv = advertised.get(url)
            if not adv or not adv[0]:
                continue
            live_set = {p for p, _ in offs if p}
            if not live_set or adv[0] in live_set:
                continue
            catalog_vs_live.append({
                "resource": url,
                "advertised_payto": adv[0],
                "advertised_network": adv[1],
                "live_paytos": sorted(live_set),
                "catalog_date": census_date,
                "probe_date": offers_date})

    # --- 2. payTo rotation over census history -------------------------------
    rotations = []
    hist: dict = {}   # resource -> {payto: [first, last]}
    for res, w, lo, hi in store.db.execute(
            "SELECT resource, seller_wallet, MIN(measured_date), "
            "MAX(measured_date) FROM bazaar_census "
            "WHERE seller_wallet IS NOT NULL GROUP BY resource, seller_wallet"):
        p = _norm(w)
        if p:
            hist.setdefault(res, {})[p] = [lo, hi]
    for res, wallets in hist.items():
        if len(wallets) < 2:
            continue
        timeline = sorted(
            ({"payto": p, "first_seen": lo, "last_seen": hi}
             for p, (lo, hi) in wallets.items()),
            key=lambda d: (d["first_seen"], d["payto"]))
        rotations.append({
            "resource": res, "n_wallets": len(wallets),
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

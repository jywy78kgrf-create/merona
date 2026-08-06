"""Passive, read-only daily instrumentation around the settlement indexer.

OBSERVE-ONLY. Everything here runs AFTER the settlement index + snapshot are
complete, writes to its OWN tables (never settlements/indexed_ranges), and is
best-effort: any probe can crash and it neither corrupts settlement data nor
stops the others. Each collector is wrapped in isolation by run_instrumentation.

Collectors:
  1. endpoint_liveness  — HTTP GET each Bazaar-listed endpoint (no payment);
     record status/latency/valid-402/content-type. Responses treated as INERT
     (never parsed-for-exec; JSON shape checked defensively only).
  2. facilitator_health — derived from settlements already indexed. Solana has
     per-facilitator attribution; EVM is a superset with NO relayer captured, so
     it's recorded chain-level (facilitator unattributed). Latency/revert are
     NOT on-chain-derivable from a Transfer-based index → stored NULL w/ reason.
  3. hourly_settlements — same settlement rows, grouped by UTC hour.
  4. domain_intel       — per unique endpoint domain: RDAP registration age, TLS
     cert issuer+age, a DNS A-record fingerprint + change-since-last. All free.

Interpretability: each run stamps daily_fingerprint (the same coverage
fingerprint as the day's snapshot); every table joins to it on measured_date.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

BAZAAR_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
# Politeness / cost caps (env-overridable). Silent truncation is a lie, so what
# gets dropped is logged, never hidden.
# 300 pages × 100 = full census of the ~25k-listing directory (~75s of polite
# paging). The census feeds the price book + churn record; endpoint PROBES stay
# capped separately below.
BAZAAR_MAX_PAGES = int(os.environ.get("BAZAAR_MAX_PAGES", "300"))    # 100/page
# Raised 250 -> 2500 (2026-08-05): the old cap plus first-N-in-catalog-order
# selection probed the SAME ~250 origins nightly and left ~1,270 others nearly
# never observed — an origin listed for 21 days had 2 observed days. With
# grades gated on MIN_MATURE_DAYS distinct observations, capped coverage made
# a letter unreachable for most of the catalog. At ~1,500 origins one polite
# probe each (escalation only where the first answer was inconclusive) runs
# ~15-40 min nightly — affordable. Any overflow beyond the cap rotates by
# date-salted hash so no origin is ever permanently excluded.
LIVENESS_MAX_ORIGINS = int(os.environ.get("LIVENESS_MAX_ORIGINS", "2500"))
DOMAIN_MAX = int(os.environ.get("DOMAIN_MAX", "150"))
PROBE_DELAY_S = float(os.environ.get("PROBE_DELAY_S", "0.3"))
PROBE_TIMEOUT_S = float(os.environ.get("PROBE_TIMEOUT_S", "8"))
UNIT_ECON_SAMPLE = int(os.environ.get("UNIT_ECON_SAMPLE", "30"))

# Bazaar `network` values arrive in TWO dialects: legacy names ("base",
# "base-sepolia") and CAIP-2 ("eip155:8453", "solana:<genesis>"). Normalize to
# our chain names so the census/price-book/network-watch never mistake a
# dialect for a new chain.
_CAIP2 = {"eip155:8453": "base", "eip155:137": "polygon",
          "eip155:43114": "avalanche", "eip155:10": "optimism",
          "eip155:42161": "arbitrum", "eip155:1329": "sei",
          "eip155:4217": "tempo",
          "eip155:84532": "base-sepolia", "eip155:80002": "polygon-amoy",
          "solana:5eykt4usfv8p8njdtreepy1vzqkqzkvdp": "solana"}


def norm_network(n: str | None) -> str:
    n = (n or "").strip()
    nl = n.lower()
    if nl in _CAIP2:
        return _CAIP2[nl]
    if nl.startswith("solana:"):
        # mainnet genesis handled above; any other solana genesis is a devnet
        return "solana-devnet"
    return nl

# USDC contracts per chain (6 decimals) — the assets whose asked prices we can
# express in USD without a price feed.
_USDC_ASSETS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",   # base
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",   # polygon
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831",   # arbitrum
    "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e",   # avalanche
    "0x0b2c639c533813f4aa9d7837caf62653d097ff85",   # optimism
    "0xe15fc38f6d8c56af07bbcbe3baf5708a2bf42392",   # sei
    "0x20c000000000000000000000b9537d11c60e8b50",   # tempo (enshrined)
    "epjfwdd5aufqssqem2qn1xzybapc8g4wegqkzwytdt1v",  # solana (lowercased)
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Bazaar endpoint list ---------------------------------------------------
def fetch_bazaar_resources(max_pages: int = BAZAAR_MAX_PAGES) -> tuple[list, int]:
    """Return ([{resource, origin, seller_wallet, network, service}], total).
    Bounded to max_pages (100/page); `total` is the Bazaar's full count so the
    caller can log sampled-vs-total (no silent truncation)."""
    out, total, offset = [], None, 0
    s = requests.Session()
    for _ in range(max_pages):
        r = s.get(BAZAAR_URL, params={"limit": 100, "offset": offset},
                  timeout=PROBE_TIMEOUT_S)
        r.raise_for_status()
        body = r.json()
        total = (body.get("pagination") or {}).get("total", total)
        items = body.get("items") or []
        for it in items:
            url = it.get("resource")
            if not url:
                continue
            acc = (it.get("accepts") or [{}])[0]
            p = urlparse(url)
            if not p.scheme or not p.hostname:
                continue
            # asked price: 'amount' in current Bazaar payloads; older listings
            # used 'maxAmountRequired'. USD only derivable for USDC (6 dp).
            amount_raw = acc.get("amount") or acc.get("maxAmountRequired")
            asset = (acc.get("asset") or "").strip()
            amount_usd = None
            try:
                if amount_raw is not None and asset.lower() in _USDC_ASSETS:
                    amount_usd = int(amount_raw) / 1e6
            except Exception:
                pass
            out.append({"resource": url,
                        "origin": f"{p.scheme}://{p.netloc}",
                        "domain": p.hostname,
                        "seller_wallet": acc.get("payTo"),
                        "network": norm_network(acc.get("network")),
                        "network_raw": acc.get("network"),
                        "asset": asset or None,
                        "amount_raw": str(amount_raw) if amount_raw is not None else None,
                        "amount_usd": amount_usd,
                        "service": it.get("serviceName")})
        offset += 100
        if not items or (total is not None and offset >= total):
            break
        time.sleep(PROBE_DELAY_S)
    return out, (total if total is not None else len(out))


def _dedupe_by_origin(resources: list) -> list:
    seen, reps = set(), []
    for res in resources:
        if res["origin"] in seen:
            continue
        seen.add(res["origin"])
        reps.append(res)
    return reps


# When an origin's representative path answers but NOT with a valid 402, try
# up to this many alternate listed paths before the day is scored. One
# catalog-ordered path must not speak for a whole origin: a metrics API whose
# first listed path wants query params GETs a 400 forever, and that zeroed
# valid_402_rate for all 297 of one origin's endpoints while 25 payers were
# settling real money to it. Only alive-but-not-402 origins escalate, so the
# request budget grows only where the first answer was inconclusive.
LIVENESS_402_FALLBACKS = int(os.environ.get("LIVENESS_402_FALLBACKS", "2"))


def _origin_probe_plan(resources: list) -> list:
    """[(origin, [rep, fallback...])] in catalog order. Fallbacks are the
    middle and last of the origin's listing — deterministic, spread, and
    re-derivable from the same census anyone else can fetch."""
    by_origin, order = {}, []
    for res in resources:
        o = res["origin"]
        if o not in by_origin:
            by_origin[o] = []
            order.append(o)
        by_origin[o].append(res)
    plan = []
    for o in order:
        lst = by_origin[o]
        picks = [lst[0]]
        for cand in (lst[len(lst) // 2], lst[-1]):
            if cand not in picks:
                picks.append(cand)
        plan.append((o, picks[:1 + LIVENESS_402_FALLBACKS]))
    return plan


# --- 1. endpoint liveness ---------------------------------------------------
def probe_endpoint_liveness(store, date: str, resources: list) -> int:
    """GET a representative listed path per origin (no payment); if it answers
    but not with a valid x402 402, escalate through alternate listed paths
    seeking one. Every probe records its own liveness row (scoring aggregates
    per origin per day). Capped + polite. Responses are inert: we read a small
    bounded chunk and only shape-check for an x402 402 body — never execute or
    fully deserialize."""
    plan = _origin_probe_plan(resources)
    if len(plan) > LIVENESS_MAX_ORIGINS:
        # Fair rotation for the overflow: rank by date-salted hash so the
        # excluded set changes nightly and every origin accrues observation
        # days over time. First-N-in-catalog-order froze the sample: the same
        # origins nightly, the rest never — and grades gate on observed days,
        # so "never sampled" silently meant "never gradable". Deterministic
        # per date, so the night's plan is re-derivable.
        import hashlib
        plan.sort(key=lambda op: hashlib.sha256(
            f"{date}:{op[0]}".encode()).hexdigest())
        dropped = len(plan) - LIVENESS_MAX_ORIGINS
        plan = plan[:LIVENESS_MAX_ORIGINS]
        print(f"[instr] liveness cap: probing {LIVENESS_MAX_ORIGINS} of "
              f"{LIVENESS_MAX_ORIGINS + dropped} origins tonight "
              f"({dropped} rotate to later nights)")
    s = requests.Session()
    s.headers["User-Agent"] = "x402-index-liveness/1.0 (+read-only probe)"
    n = 0
    for _origin, picks in plan:
        for res in picks:
            row = {"endpoint_url": res["resource"],
                   "seller_wallet": res["seller_wallet"],
                   "network": res["network"]}
            alive = valid = False
            t0 = time.monotonic()
            try:
                r = s.get(res["resource"], timeout=PROBE_TIMEOUT_S,
                          allow_redirects=False, stream=True)
                row["latency_ms"] = int((time.monotonic() - t0) * 1000)
                row["http_status"] = r.status_code
                row["content_type"] = r.headers.get("content-type")
                row["raw_headers"] = dict(list(r.headers.items())[:25])
                valid, offers = _parse_402_offers(r)
                row["valid_402"] = valid
                alive = True
                # what the endpoint ACTUALLY asks for right now — the live side
                # of the payTo-mismatch feed (vs bazaar_census, the catalog claim)
                for off in offers:
                    store.record_live_offer(date, res["resource"], off, _utcnow())
                r.close()
            except Exception as e:
                row["latency_ms"] = int((time.monotonic() - t0) * 1000)
                row["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            store.record_endpoint_liveness(date, row, _utcnow())
            n += 1
            time.sleep(PROBE_DELAY_S)
            if valid or not alive:
                # found the 402 we were looking for, or the origin is down —
                # further probes add cost, not information
                break
    return n


def _parse_402_offers(r) -> tuple[bool, list]:
    """(valid_402, offers) — valid iff status==402 and the body defensively
    parses to an x402 shape; offers are the payTo/network/asset triples the
    endpoint asks for LIVE (capped at 8, strings clipped, inert: at most 8 KB
    read, shape-checked field extraction only, never executed)."""
    if r.status_code != 402:
        return False, []
    try:
        chunk = next(r.iter_content(8192), b"") or b""
        import json as _json
        body = _json.loads(chunk.decode("utf-8", "replace"))
        if not (isinstance(body, dict)
                and ("accepts" in body or "x402Version" in body)):
            return False, []
        offers, seen = [], set()
        for acc in (body.get("accepts") or [])[:8]:
            if not isinstance(acc, dict):
                continue
            pt = acc.get("payTo")
            if not isinstance(pt, str) or not pt.strip() or len(pt) > 128:
                continue
            pt = pt.strip()
            # EVM lowercased, base58 preserved — same rule as collect.py
            key = pt.lower() if pt.startswith(("0x", "0X")) else pt
            if key in seen:
                continue
            seen.add(key)
            offers.append({
                "payto": key,
                "network": str(acc.get("network"))[:64]
                if acc.get("network") is not None else None,
                "asset": str(acc.get("asset"))[:128]
                if acc.get("asset") is not None else None})
        return True, offers
    except Exception:
        return False, []


def _looks_like_x402_402(r) -> bool:
    """Back-compat shim over _parse_402_offers (boolean-only callers/tests)."""
    return _parse_402_offers(r)[0]


# --- 2. facilitator health (from settlements) -------------------------------
_NO_LAT_REVERT = ("latency/revert not on-chain-derivable from a Transfer-based "
                  "index: submission time isn't on-chain and reverted txs emit "
                  "no Transfer")


def compute_facilitator_health(store, date: str, chains: list) -> int:
    """Per-facilitator (Solana) / per-chain (EVM superset, unattributed) counts +
    volume share over the last 2 UTC days, from indexed settlements. Portable:
    aggregates in Python (no backend-specific date SQL)."""
    since = int(time.mktime(time.gmtime())) - 2 * 86400
    agg: dict = {}          # (day, chain, facilitator) -> [count, volume]
    chain_day_vol: dict = {}  # (day, chain) -> volume
    # NB: exclude Permit2 (<chain>_permit2) in Python, not via a LIKE '%...' —
    # a literal '%' in a parameterized query breaks psycopg's placeholder parser.
    rows = store.db.execute(
        store.q("SELECT chain, facilitator, amount, block_timestamp FROM "
                "settlements WHERE block_timestamp >= ?"), (since,)).fetchall()
    for chain, fac, amount, ts in rows:
        if not ts or chain.endswith("_permit2"):
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        facil = fac if fac else "_unattributed_evm_superset"
        k = (day, chain, facil)
        e = agg.setdefault(k, [0, 0])
        e[0] += 1
        e[1] += int(amount)
        chain_day_vol[(day, chain)] = chain_day_vol.get((day, chain), 0) + int(amount)
    n = 0
    for (day, chain, facil), (cnt, vol) in agg.items():
        tot = chain_day_vol.get((day, chain), 0)
        share = (vol / tot) if tot else None
        note = None if facil != "_unattributed_evm_superset" else \
            "EVM superset: relayer/facilitator not captured by the EIP-3009 index"
        store.record_facilitator_health(day, chain, facil, cnt, vol, share,
                                        None, None, note or _NO_LAT_REVERT, _utcnow())
        n += 1
    return n


# --- 3. hourly settlements --------------------------------------------------
def aggregate_hourly(store, date: str) -> int:
    """Group the last 2 UTC days of settlements by (date, hour, chain)."""
    since = int(time.mktime(time.gmtime())) - 2 * 86400
    agg: dict = {}
    rows = store.db.execute(
        store.q("SELECT chain, amount, block_timestamp FROM settlements "
                "WHERE block_timestamp >= ?"), (since,)).fetchall()
    for chain, amount, ts in rows:
        if not ts or chain.endswith("_permit2"):
            continue
        g = time.gmtime(ts)
        k = (time.strftime("%Y-%m-%d", g), g.tm_hour, chain)
        e = agg.setdefault(k, [0, 0])
        e[0] += 1
        e[1] += int(amount)
    for (day, hour, chain), (cnt, vol) in agg.items():
        store.record_hourly_settlement(day, hour, chain, cnt, vol)
    return len(agg)


# --- 4. domain intel --------------------------------------------------------
def _rdap_created(domain: str) -> str | None:
    """RDAP registration date (free/open). RDAP resolves the REGISTRABLE domain,
    not a sub-domain, so walk up the labels (api.exa.ai -> exa.ai) until a
    registration event is found. Naive on multi-part TLDs (co.uk) — returns None
    there rather than a paid public-suffix lookup."""
    labels = domain.split(".")
    for i in range(len(labels) - 1):
        cand = ".".join(labels[i:])
        if cand.count(".") < 1:
            break
        try:
            r = requests.get(f"https://rdap.org/domain/{cand}",
                             timeout=PROBE_TIMEOUT_S)
            if r.status_code == 200:
                for ev in r.json().get("events", []):
                    if ev.get("eventAction") == "registration":
                        return ev.get("eventDate")
        except Exception:
            continue
    return None


def _tls_cert(domain: str):
    """(issuer, age_days) from the live TLS cert via stdlib (validating context;
    an invalid/self-signed cert surfaces as an error upstream — itself a signal)."""
    ctx = ssl.create_default_context()
    with socket.create_connection((domain, 443), timeout=PROBE_TIMEOUT_S) as sock:
        with ctx.wrap_socket(sock, server_hostname=domain) as ss:
            cert = ss.getpeercert()
    issuer = dict(x[0] for x in cert.get("issuer", ())).get(
        "organizationName") or dict(x[0] for x in cert.get("issuer", ())).get(
        "commonName")
    nb = cert.get("notBefore")
    age = None
    if nb:
        t = time.strptime(nb, "%b %d %H:%M:%S %Y %Z")
        age = int((time.time() - time.mktime(t)) / 86400)
    return issuer, age


def _dns_fingerprint(domain: str) -> str | None:
    try:
        infos = socket.getaddrinfo(domain, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        return ",".join(ips) if ips else None
    except Exception:
        return None


def probe_domain_intel(store, date: str, resources: list) -> int:
    """RDAP registration age + TLS cert + DNS fingerprint per unique domain,
    capped. changed_since_last compares the DNS fingerprint to the prior run."""
    domains, seen = [], set()
    for res in resources:
        d = res["domain"]
        if d and d not in seen:
            seen.add(d)
            domains.append(d)
    n = 0
    for domain in domains[:DOMAIN_MAX]:
        row: dict = {}
        try:
            row["whois_created"] = _rdap_created(domain)
        except Exception as e:
            row["error"] = f"rdap: {str(e)[:60]}"
        try:
            issuer, age = _tls_cert(domain)
            row["cert_issuer"], row["cert_age_days"] = issuer, age
        except Exception as e:
            row["error"] = (row.get("error", "") + f" tls: {str(e)[:60]}").strip()
        fp = _dns_fingerprint(domain)
        row["dns_fingerprint"] = fp
        prev = store.last_domain_fingerprint(domain, date)
        row["changed_since_last"] = (prev is not None and fp is not None and prev != fp)
        store.record_domain_intel(date, domain, row, _utcnow())
        n += 1
        time.sleep(PROBE_DELAY_S)
    return n


# --- isolated runner --------------------------------------------------------
def _isolate(name: str, fn, *a):
    """Run a collector; a crash is logged and swallowed so it never stops the
    others or the (already-complete) settlement snapshot."""
    import sys
    try:
        t0 = time.monotonic()
        out = fn(*a)
        print(f"[instr] {name}: ok ({out}) in {time.monotonic()-t0:.1f}s")
        return out
    except Exception as e:
        print(f"[instr] {name}: FAILED (isolated, non-fatal): "
              f"{type(e).__name__}: {str(e)[:160]}", file=sys.stderr)
        return None


# --- 5. full Bazaar census + price book --------------------------------------
def record_bazaar_census(store, date: str, resources: list) -> int:
    """One compact row per listed resource per day — the price book (asked
    prices) and the churn record (arrivals/departures) in one table."""
    rows = [{"resource": r["resource"], "domain": r.get("domain"),
             "network": r.get("network"), "asset": r.get("asset"),
             "amount_raw": r.get("amount_raw"), "amount_usd": r.get("amount_usd"),
             "seller_wallet": r.get("seller_wallet"), "service": r.get("service")}
            for r in resources]
    n = store.record_bazaar_census_rows(date, rows)
    priced = sum(1 for r in rows if r["amount_usd"] is not None)
    print(f"[instr] bazaar_census: {n} listings recorded ({priced} with USD ask)")
    return n


# --- 6. cross-tracker reconciliation -----------------------------------------
# Record other trackers' headline numbers (or their unavailability) daily next
# to ours. x402scan (Merit Systems) has no documented public API — we record
# reachability and any parseable headline stats best-effort; a hard failure is
# itself a data point. Add fetchers here as trackers expose APIs.
def tracker_recon(store, date: str) -> int:
    n = 0
    s = requests.Session()
    s.headers["User-Agent"] = "x402-index-recon/1.0 (+daily reconciliation)"
    try:
        r = s.get("https://www.x402scan.com/", timeout=PROBE_TIMEOUT_S)
        store.record_tracker_metric(date, "x402scan", "reachable",
                                    1.0 if r.ok else 0.0,
                                    f"http {r.status_code}", _utcnow())
        n += 1
        # headline numbers, if server-rendered (best-effort; absence recorded)
        import re as _re
        hits = _re.findall(r'"(?:totalTransactions|total_transactions|txCount)"'
                           r'\s*:\s*"?([\d.]+)"?', r.text or "")
        if hits:
            store.record_tracker_metric(date, "x402scan", "total_transactions",
                                        float(hits[0]), "parsed from homepage",
                                        _utcnow())
            n += 1
        else:
            store.record_tracker_metric(date, "x402scan", "total_transactions",
                                        None, "no parseable headline (JS-rendered; "
                                        "no public API as of 2026-07)", _utcnow())
            n += 1
    except Exception as e:
        store.record_tracker_metric(date, "x402scan", "reachable", 0.0,
                                    f"{type(e).__name__}: {str(e)[:100]}", _utcnow())
        n += 1
    return n


# --- 7. unit economics: settlement gas fee vs payment size --------------------
_SPOT = {"base": "ETH-USD", "optimism": "ETH-USD", "arbitrum": "ETH-USD",
         "polygon": "POL-USD", "avalanche": "AVAX-USD", "solana": "SOL-USD"}
_EVM_RPC = {"base": ("X402_BASE_RPC", "https://mainnet.base.org"),
            "polygon": ("X402_POLYGON_RPC", "https://polygon.drpc.org"),
            "avalanche": ("X402_AVALANCHE_RPC", "https://avalanche.drpc.org"),
            "optimism": ("X402_OPTIMISM_RPC", "https://optimism.drpc.org"),
            "arbitrum": ("X402_ARBITRUM_RPC", "https://arb1.arbitrum.io/rpc")}


def _spot_usd(sess, pair: str):
    r = sess.get(f"https://api.coinbase.com/v2/prices/{pair}/spot",
                 timeout=PROBE_TIMEOUT_S)
    return float(r.json()["data"]["amount"])


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    return vals[len(vals) // 2] if vals else None


def unit_economics(store, date: str, chains: list) -> int:
    """Sample recent SCOPED settlements per chain; record median gas fee (USD)
    vs median payment. The '$0.0001 to move $0.03' series. ~30 receipt fetches
    per chain per night — negligible RPC cost."""
    s = requests.Session()
    s.headers["User-Agent"] = "x402-index-unitecon/1.0"
    n = 0
    for ch in chains:
        if ch not in _SPOT:
            continue
        row: dict = {}
        try:
            # witnessed-regime floor: after a backfill, sorting the full
            # 148M-row history for "recent" samples is a giant wasted sort
            wb = int(store.get_meta(f"witnessed_start_{ch}") or 0)
            samp = store.db.execute(store.q(
                "SELECT tx_hash, amount FROM settlements WHERE chain=? AND "
                "facilitator IS NOT NULL AND block_number >= ? "
                "ORDER BY block_number DESC LIMIT ?"),
                (ch, wb, UNIT_ECON_SAMPLE)).fetchall()
            if not samp:
                continue
            price = _spot_usd(s, _SPOT[ch])
            fees = []
            if ch == "solana":
                rpc = os.environ.get("X402_SOLANA_RPC",
                                     "https://api.mainnet-beta.solana.com")
                for sig, _amt in samp:
                    try:
                        rr = s.post(rpc, json={
                            "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                            "params": [sig, {"encoding": "json",
                                             "maxSupportedTransactionVersion": 0}]},
                            timeout=PROBE_TIMEOUT_S).json()
                        fee = ((rr.get("result") or {}).get("meta") or {}).get("fee")
                        if fee is not None:
                            fees.append(fee / 1e9 * price)
                    except Exception:
                        pass
                    time.sleep(0.1)
            else:
                env, dflt = _EVM_RPC[ch]
                rpc = os.environ.get(env, dflt)
                for txh, _amt in samp:
                    try:
                        rr = s.post(rpc, json={
                            "jsonrpc": "2.0", "id": 1,
                            "method": "eth_getTransactionReceipt",
                            "params": [txh]}, timeout=PROBE_TIMEOUT_S).json()
                        rec = rr.get("result") or {}
                        gu, gp = rec.get("gasUsed"), rec.get("effectiveGasPrice")
                        if gu and gp:
                            fees.append(int(gu, 16) * int(gp, 16) / 1e18 * price)
                    except Exception:
                        pass
                    time.sleep(0.1)
            pay = _median([int(a) / 1e6 for _t, a in samp])
            fee = _median(fees)
            row = {"sample_n": len(fees), "median_fee_usd": fee,
                   "median_payment_usd": pay,
                   "fee_over_payment": (fee / pay) if (fee and pay) else None,
                   "native_price_usd": price}
            print(f"[instr] unit_econ {ch}: fee ${fee:.6f} vs pay ${pay:.4f} "
                  f"(n={len(fees)})" if fee else
                  f"[instr] unit_econ {ch}: no fees derived (n=0)")
        except Exception as e:
            row = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
            print(f"[instr] unit_econ {ch} failed (isolated): {row['error']}",
                  file=sys.stderr)
        store.record_unit_economics(date, ch, row, _utcnow())
        n += 1
    return n


# ---- network watch: running ledger + threshold alerts ----------------------
# The nightly detector (below, inside run_instrumentation) sees which unindexed
# mainnets the Bazaar lists TODAY. These helpers keep the durable memory: a
# per-network ledger (first/last seen, consecutive-day streak, peak count) and
# a threshold alert so sustained or sudden growth pings loudly instead of
# scrolling by. Thresholds are deliberately conservative — the census showed
# most listings are ghosts, so a lone junk listing that persists forever must
# NOT alert weekly.
WATCH_ALERT_COUNT = int(os.environ.get("X402_WATCH_ALERT_COUNT", "10"))
WATCH_ALERT_STREAK = int(os.environ.get("X402_WATCH_ALERT_STREAK", "7"))
WATCH_STREAK_MIN_COUNT = int(os.environ.get("X402_WATCH_STREAK_MIN_COUNT", "3"))

# Human names for the committed watchlist (raw CAIP-2 ids are unreadable in a
# weekly review). The dashboard keeps its own copy of this map in JS.
NET_NAMES = {
    "eip155:1": "Ethereum", "eip155:10": "Optimism", "eip155:25": "Cronos",
    "eip155:56": "BNB Chain", "eip155:100": "Gnosis", "eip155:130": "Unichain",
    "eip155:137": "Polygon", "eip155:250": "Fantom", "eip155:324": "zkSync Era",
    "eip155:480": "World Chain", "eip155:999": "HyperEVM",
    "eip155:1101": "Polygon zkEVM", "eip155:1284": "Moonbeam",
    "eip155:1329": "Sei", "eip155:2222": "Kava", "eip155:5000": "Mantle",
    "eip155:8453": "Base", "eip155:42161": "Arbitrum", "eip155:42220": "Celo",
    "eip155:43114": "Avalanche", "eip155:59144": "Linea",
    "eip155:81457": "Blast", "eip155:534352": "Scroll",
}


def net_name(nid: str) -> str:
    if nid in NET_NAMES:
        return NET_NAMES[nid]
    ns = nid.split(":", 1)[0]
    family = {"polkadot": "Polkadot-family", "solana": "Solana-family",
              "cosmos": "Cosmos-family", "bip122": "Bitcoin-family",
              "sui": "Sui", "aptos": "Aptos", "near": "NEAR",
              "tron": "Tron", "stellar": "Stellar"}
    return f"{family[ns]} ({nid[len(ns) + 1:][:8]}…)" if ns in family else nid


def update_watch_ledger(ledger: dict, unindexed: dict, date: str) -> tuple[dict, list]:
    """Fold today's unindexed-network counts into the running ledger.
    Pure: returns (ledger, alerts). Re-running the same date only refreshes
    counts (idempotent — no double-counted days/streaks). An alert fires when a
    network hits WATCH_ALERT_COUNT listings outright, or has been present
    WATCH_ALERT_STREAK consecutive days with at least WATCH_STREAK_MIN_COUNT
    listings; it re-fires only once the count doubles past the alerted level,
    so a stable network alerts once, not nightly."""
    try:
        yday = (datetime.strptime(date, "%Y-%m-%d")
                - timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        yday = ""
    alerts = []
    for net, count in unindexed.items():
        e = ledger.get(net) or {"first_seen": date, "last_seen": "",
                                "days_seen": 0, "streak": 0, "last_count": 0,
                                "max_count": 0, "alerted_count": 0}
        if e["last_seen"] != date:               # first sighting today
            e["streak"] = e["streak"] + 1 if e["last_seen"] == yday else 1
            e["days_seen"] += 1
            e["last_seen"] = date
        e["last_count"] = count
        e["max_count"] = max(e["max_count"], count)
        fire = (count >= WATCH_ALERT_COUNT
                or (e["streak"] >= WATCH_ALERT_STREAK
                    and count >= WATCH_STREAK_MIN_COUNT))
        if fire and (e["alerted_count"] == 0 or count >= 2 * e["alerted_count"]):
            alerts.append({"network": net, "name": net_name(net), "count": count,
                           "streak": e["streak"], "first_seen": e["first_seen"]})
            e["alerted_count"] = count
        ledger[net] = e
    return ledger, alerts


def write_watchlist(ledger: dict, date: str, path: Path | None = None) -> None:
    """Render the ledger as data/indexer/WATCHLIST.md — the standing review
    list. run.sh commits it with the nightly snapshot, so the running tab is
    versioned and reviewable from the repo (weekly or whenever)."""
    p = path or Path(os.environ.get("X402_WATCHLIST_PATH")
                     or Path(__file__).resolve().parents[1]
                     / "data" / "indexer" / "WATCHLIST.md")
    rows = sorted(ledger.items(),
                  key=lambda kv: (kv[1].get("last_seen", ""),
                                  kv[1].get("max_count", 0)), reverse=True)
    lines = [
        "# Network watch — unindexed mainnets seen in the Bazaar",
        "",
        f"Updated {date} by the nightly instrumentation run. Every mainnet the",
        "Bazaar has EVER listed x402 activity on that merona does not index,",
        "with sighting history. Alerted = crossed the growth threshold",
        f"(≥{WATCH_ALERT_COUNT} listings, or {WATCH_ALERT_STREAK}+ consecutive days",
        f"at ≥{WATCH_STREAK_MIN_COUNT}). Review cadence: weekly is plenty —",
        "a lone listing that persists is a ghost, not a gap.",
        "",
        "| network | id | first seen | last seen | days | streak | now | peak | alerted |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for net, e in rows:
        lines.append(
            f"| {net_name(net)} | `{net}` | {e.get('first_seen', '?')} "
            f"| {e.get('last_seen', '?')} | {e.get('days_seen', 0)} "
            f"| {e.get('streak', 0)} | {e.get('last_count', 0)} "
            f"| {e.get('max_count', 0)} "
            f"| {'⚠ at ' + str(e['alerted_count']) if e.get('alerted_count') else '—'} |")
    if not rows:
        lines.append("| — none ever sighted — | | | | | | | | |")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")


def run_instrumentation(store, date: str, fingerprint: dict,
                        chains: list | None = None) -> None:
    """Stamp the day's fingerprint, then run the four collectors each isolated.
    Never raises. Bazaar fetch failure only disables the two endpoint-based
    collectors; the settlement-derived ones still run."""
    import sys
    store.set_daily_fingerprint(date, fingerprint, _utcnow())
    # settlement-derived (no network beyond the DB): always attempt
    _isolate("hourly_settlements", aggregate_hourly, store, date)
    _isolate("facilitator_health", compute_facilitator_health, store, date,
             chains or [])
    # endpoint-derived: one bounded Bazaar fetch feeds both
    resources = None
    try:
        resources, total = fetch_bazaar_resources()
        origins = len({r["origin"] for r in resources})
        print(f"[instr] bazaar: sampled {len(resources)} resources / {origins} "
              f"origins (of {total} total listed)")
        # Persist the sampled-vs-listed counts: the dashboard reports Bazaar
        # size next to measured liveness, and the log line alone isn't queryable.
        try:
            store.set_meta("bazaar_last", {"date": date, "total_listed": total,
                                           "sampled": len(resources),
                                           "origins": origins})
        except Exception:
            pass
        # NETWORK WATCH — new-chain detector. Every Bazaar listing names its
        # network (normalized from both the legacy-name and CAIP-2 dialects by
        # fetch_bazaar_resources); when sellers start listing on a chain we
        # don't index, it appears here before it matters anywhere else. With
        # the census at full pagination this is now directory-wide, not a sample.
        try:
            indexed = {"base", "polygon", "solana", "avalanche", "optimism",
                       "arbitrum"}
            testnet = ("sepolia", "testnet", "devnet", "amoy", "fuji", "mumbai",
                       "goerli", "holesky")
            nets: dict = {}
            for r in resources:
                n = (r.get("network") or "").lower().strip()
                if n:
                    nets[n] = nets.get(n, 0) + 1
            unindexed = {n: c for n, c in sorted(nets.items(), key=lambda x: -x[1])
                         if n not in indexed and not any(t in n for t in testnet)}
            store.set_meta("network_watch",
                           {"date": date, "networks": nets, "unindexed": unindexed})
            if unindexed:
                print(f"[instr] NETWORK WATCH: Bazaar lists x402 activity on "
                      f"network(s) we don't index: {unindexed} — evaluate adding "
                      f"coverage (evm_chains.py + registry)", file=sys.stderr)
            # running ledger + threshold alerts (isolated: a ledger bug must
            # not take down the day's network_watch meta written above)
            try:
                raw = store.get_meta("network_watch_ledger")
                ledger = (json.loads(raw) if isinstance(raw, str) else raw) or {}
                ledger, alerts = update_watch_ledger(ledger, unindexed, date)
                store.set_meta("network_watch_ledger", ledger)
                for a in alerts:
                    print(f"[instr] NETWORK WATCH ALERT: {a['name']} "
                          f"({a['network']}) at {a['count']} Bazaar listings, "
                          f"{a['streak']}-day streak (first seen "
                          f"{a['first_seen']}) — crossed the growth threshold; "
                          f"evaluate indexing it (evm_chains.py + registry). "
                          f"Standing list: data/indexer/WATCHLIST.md",
                          file=sys.stderr)
                write_watchlist(ledger, date)
            except Exception as we:
                print(f"[instr] network-watch ledger FAILED (isolated): "
                      f"{type(we).__name__}: {str(we)[:120]}", file=sys.stderr)
        except Exception:
            pass
    except Exception as e:
        print(f"[instr] bazaar fetch FAILED (isolated): {type(e).__name__}: "
              f"{str(e)[:120]} — endpoint probes skipped this run", file=sys.stderr)
    if resources:
        # full census + price book first (pure DB write), then bounded probes
        _isolate("bazaar_census", record_bazaar_census, store, date, resources)
        _isolate("endpoint_liveness", probe_endpoint_liveness, store, date, resources)
        _isolate("domain_intel", probe_domain_intel, store, date, resources)
    # independent of the Bazaar: cross-tracker recon + unit economics
    _isolate("tracker_recon", tracker_recon, store, date)
    _isolate("unit_economics", unit_economics, store, date,
             [c for c in (chains or []) if c in _SPOT])

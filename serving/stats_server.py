"""Live stats service for the merona dashboard.

Serves the dashboard page AND a same-origin /stats.json computed from the live
index (the same Supabase the nightly job writes). It runs the exact
build_x402_scoped the daily snapshot uses — read-only, never mutates anything —
on a background timer, so page loads are instant and the DB is queried at most
once per refresh regardless of how many viewers are open.

Run on the box (reuses the venv + /etc/x402-index.env):
  STATS_PORT=8088 .venv/bin/python serving/stats_server.py
View via an SSH tunnel (ssh -L 8088:localhost:8088 …) or behind your own proxy.
The dashboard falls back to its seeded values if this service is unreachable.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "indexer"))
from storage import open_store                      # noqa: E402
from daily_snapshot import (build_dashboard_metrics, SNAP_DIR,  # noqa: E402
                            DASHBOARD_METRICS_KEY)
from index_base import DB_PATH                       # noqa: E402

PAGE = ROOT / "dashboard" / "index.html"
# Refresh at a calm cadence — the index moves daily, not by the second, and the
# aggregate queries are heavy — so we don't pile load on the DB. The last good
# result is persisted to disk so a restart (or a busy DB) still serves real
# numbers immediately instead of blanking to the page's seed values.
REFRESH = int(os.environ.get("STATS_REFRESH_S", "300"))
CYCLE_UTC = os.environ.get("CYCLE_UTC", "02:17")
CACHE = ROOT / "serving" / ".stats_cache.json"
_state = {"data": None, "err": None, "ts": 0}

# Bind to loopback by default — a reverse proxy (Caddy) terminates TLS and is the
# only thing that should talk to this process. Override STATS_HOST=0.0.0.0 only
# for a firewalled dev box.  (Security audit LB-2.)
BIND_HOST = os.environ.get("STATS_HOST", "127.0.0.1")
# Trust X-Forwarded-For only when the peer is the local reverse proxy.
TRUST_PROXY = os.environ.get("STATS_TRUST_PROXY", "1") == "1"
# Set EDGE_CF=1 ONLY once Cloudflare fronts Caddy AND the origin firewall admits
# only Cloudflare IPs (deploy/CLOUDFLARE.md): behind CF the real client is
# CF-Connecting-IP, and XFF is either a CF edge IP or client-spoofable.
EDGE_CF = os.environ.get("EDGE_CF", "0") == "1"
# Prefer the read-only serving DSN when configured (audit LB-4); open_store
# falls back to X402_DB_URL when unset.
DB_URL_RO = os.environ.get("X402_DB_URL_RO") or None


class _Bucket:
    """Per-client-IP token bucket — defense-in-depth behind the edge; the real
    rate limiting belongs at Cloudflare/Caddy (audit LB-7)."""
    def __init__(self, rpm, burst):
        self.rate = rpm / 60.0
        self.burst = burst
        self.b = {}
        self.lock = threading.Lock()

    def take(self, ip):
        now = time.monotonic()
        with self.lock:
            if len(self.b) > 20000:                      # prune unconditionally
                cut = now - 3600
                self.b = {k: v for k, v in self.b.items() if v[1] > cut}
                if len(self.b) > 20000:
                    # sub-hour flood of fresh IPs: age prune removed nothing —
                    # hard-cap by keeping the most recently seen half
                    keep = sorted(self.b.items(), key=lambda kv: kv[1][1])
                    self.b = dict(keep[len(keep) // 2:])
            tok, ts = self.b.get(ip, (self.burst, now))
            tok = min(self.burst, tok + (now - ts) * self.rate)
            if tok >= 1.0:
                self.b[ip] = (tok - 1.0, now)
                return True
            self.b[ip] = (tok, now)
            return False


_FEED_PATH = ROOT / "data" / "indexer" / "payto_mismatches.json"
_FEED_CACHE = {"mtime": None, "feed": None}
_FEED_LOCK = threading.Lock()


def _load_feed():
    """Parsed mismatch feed, cached on file mtime — /check answers hundreds
    of lookups per parse instead of re-reading a few hundred KB per query.
    None when the nightly hasn't generated the file yet."""
    try:
        m = _FEED_PATH.stat().st_mtime
    except OSError:
        return None
    with _FEED_LOCK:
        if _FEED_CACHE["mtime"] != m:
            try:
                _FEED_CACHE["feed"] = json.loads(_FEED_PATH.read_text())
                _FEED_CACHE["mtime"] = m
            except Exception:
                return _FEED_CACHE["feed"]   # keep serving last good parse
        return _FEED_CACHE["feed"]


_CHECK_RL = _Bucket(float(os.environ.get("CHECK_RPM", "60")),
                    int(os.environ.get("CHECK_BURST", "10")))

# --- privacy-safe usage counters for the FREE endpoints --------------------
# Route + UTC-day counts ONLY — no IPs, no queries, nothing per-visitor.
# Deliberate: the free check is anonymous by design, so usage must be
# provable without being surveillable. Flushed by the refresher thread;
# read by the nightly health digest. 30-day window.
# Lives in serving/, NOT data/indexer/, on purpose. The unit runs with
# ProtectSystem=strict and grants ReadWritePaths=serving only — data/indexer
# holds published evidence (wash flags, the mismatch feed, snapshots) and the
# internet-facing service must not be able to rewrite it. An index whose whole
# claim is "recompute this yourself" cannot have its web server holding a pen
# over the exhibits. This counter is serving telemetry, so it belongs here.
_USE_PATH = ROOT / "serving" / "free_usage.json"
_USE_LOCK = threading.Lock()
_USE = {"days": None, "dirty": False, "warned": False}


def _use_bump(route: str) -> None:
    try:
        with _USE_LOCK:
            if _USE["days"] is None:
                try:
                    _USE["days"] = json.loads(_USE_PATH.read_text())
                except Exception:
                    _USE["days"] = {}
            day = datetime.now(timezone.utc).date().isoformat()
            d = _USE["days"].setdefault(day, {})
            d[route] = d.get(route, 0) + 1
            _USE["dirty"] = True
    except Exception:
        pass                     # counting must never break serving


def _use_flush() -> None:
    # Failures used to be swallowed silently, which cost an evening: the file
    # simply never appeared and there was nothing anywhere to say why. Serving
    # still must never break on a counter, so we keep catching — but we say so
    # once, and we say where we were trying to write.
    try:
        with _USE_LOCK:
            if not _USE["dirty"] or _USE["days"] is None:
                return
            _USE["days"] = dict(sorted(_USE["days"].items())[-30:])
            tmp = _USE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(_USE["days"], indent=1, sort_keys=True))
            tmp.replace(_USE_PATH)
            _USE["dirty"] = False
            _USE["warned"] = False
    except Exception as e:
        if not _USE.get("warned"):
            _USE["warned"] = True
            print(f"[stats] usage counters NOT persisting to {_USE_PATH}: "
                  f"{type(e).__name__}: {e}", flush=True)
_PROBE_RL = _Bucket(float(os.environ.get("PROBE_RPM", "30")),
                    float(os.environ.get("PROBE_BURST", "10")))
# In-memory page cache (mtime-checked) so we don't re-read 130KB per request.
_page_cache = {"bytes": None, "mtime": 0}


def _page_bytes():
    try:
        m = PAGE.stat().st_mtime
        if _page_cache["bytes"] is None or m != _page_cache["mtime"]:
            _page_cache["bytes"] = PAGE.read_bytes()
            _page_cache["mtime"] = m
        return _page_cache["bytes"]
    except Exception:
        return b"dashboard/index.html not found"


def _client_ip(handler):
    peer = handler.client_address[0]
    if TRUST_PROXY and peer in ("127.0.0.1", "::1"):
        if EDGE_CF:
            cf = handler.headers.get("CF-Connecting-IP")
            if cf:
                return cf.strip()[:45]
        xff = handler.headers.get("X-Forwarded-For")
        if xff:
            # Caddy APPENDS the real peer as the LAST xff entry; earlier
            # entries are client-supplied and spoofable — trust the rightmost.
            return xff.split(",")[-1].strip()[:45]
    return peer


def _fmt_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%d %b")
    except Exception:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d %b")
        except Exception:
            return None


def _db_metrics(store):
    """The DB-heavy half of the payload: prefer the blob the nightly run
    precomputed into meta (instant, no scan). Only if it's missing do we run the
    aggregates live — bounded by a statement timeout so a busy DB fails fast to
    the last-good cache instead of hanging every viewer."""
    stored = store.get_meta(DASHBOARD_METRICS_KEY)
    if stored:
        try:
            return json.loads(stored) if isinstance(stored, str) else stored
        except Exception:
            pass
    try:
        if store.PH == "%s":
            store.db.execute("SET statement_timeout='90000'")   # generous but bounded
            store.db.commit()
    except Exception:
        pass
    return build_dashboard_metrics(store)


def compute():
    store = open_store(url=DB_URL_RO, init_schema=False)
    try:
        m = _db_metrics(store)
        chains = m["chains"]
        # serve the raw ISO since-dates; the front-end (fmtSince) renders them
        # as "04 July 2026". Do NOT _fmt_date here — that produced "04 Jul" (no
        # year), which the front-end can't reparse, so it flashed the correct
        # date then reverted to the short string.
        since = m.get("since", {})

        dates = sorted(p.name for p in SNAP_DIR.iterdir() if p.is_dir()) if SNAP_DIR.exists() else []
        last = dates[-1] if dates else None
        fresh = True
        if last:
            try:
                fresh = datetime.strptime(last, "%Y-%m-%d").date() >= (
                    datetime.now(timezone.utc).date() - timedelta(days=1))
            except Exception:
                fresh = True
        cycle = {"snapshot_count": len(dates), "next": CYCLE_UTC,
                 "last_date": (f"{_fmt_date(last)} {CYCLE_UTC} UTC" if last else None)}

        enr_ok = all(chains[c]["enriched"] > 0.9 for c in chains
                     if chains[c].get("scopable") and "settlements" in chains[c])
        health = {"database": "good", "rpc": "good",
                  "enrichment": "good" if enr_ok else "warn",
                  "indexer": "good" if fresh else "warn",
                  "timer": "good" if fresh else "warn",
                  "db_size": m.get("db_size")}
        # exact archive counts recorded by analytics/verify_shards.sh — the
        # dashboard's historical-ledger line renders these when present and
        # falls back to its static (anchored) numbers when absent
        ledger = None
        try:
            lp = ROOT / "data" / "indexer" / "archive_ledger.json"
            if lp.exists():
                ledger = json.loads(lp.read_text())
        except Exception:
            ledger = None
        # Tempo is a dated whole-chain MEASUREMENT (a band), not a live meter.
        # The page carries Finding No 002's numbers as static fallback; this
        # file, written by a future re-measurement run, moves the date forward.
        tempo = None
        try:
            tp = ROOT / "data" / "indexer" / "tempo_measured.json"
            if tp.exists():
                tempo = json.loads(tp.read_text())
        except Exception:
            tempo = None
        return {"generated_at": datetime.now(timezone.utc).isoformat(),
                "metrics_at": m.get("generated_at"),
                "chains": chains, "since": since,
                "totals": m.get("totals", {}),
                "instr": m.get("instr") or {},
                "ledger": ledger, "tempo": tempo,
                "cycle": cycle, "health": health}
    finally:
        store.close()


# ---- probe: live, indexed lookups only ------------------------------------
# Address/tx/seller lookups are bounded index seeks (PK on tx_hash; the
# (chain,seller) / (chain,facilitator) covering indexes). The substring
# search over endpoint_url / domain is a LIKE '%q%', which needs the pg_trgm
# GIN indexes (ix_*_trgm, from deploy/migrations/2026-07-19-probe-trgm.sql) to
# be an index seek rather than a scan. statement_timeout (set on the probe
# connection) is the backstop so a query that still goes wide is cancelled
# fast instead of hanging.
REGISTRY = ROOT / "data" / "indexer" / "relayer_registry.json"
PROBE_CHAINS = ("base", "polygon", "solana", "avalanche", "optimism", "arbitrum")
_probe = {"store": None, "lock": threading.Lock(), "registry": None}

_HEX_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _registry():
    if _probe["registry"] is None:
        try:
            r = json.loads(REGISTRY.read_text())
            # curated facilitators are already merged into `facilitators` by the
            # registry builder; `curated_facilitators` is just the list of names.
            fac = dict(r.get("facilitators") or {})
            cur = r.get("curated_facilitators")
            if isinstance(cur, dict):
                fac.update(cur)
            _probe["registry"] = fac
        except Exception:
            _probe["registry"] = {}
    return _probe["registry"]


def _probe_store():
    if _probe["store"] is None:
        s = open_store(url=DB_URL_RO, init_schema=False)
        try:
            if s.PH == "%s":
                # Autocommit: the probe issues read-only SELECTs. Without it the
                # connection sits idle *inside an open transaction* between
                # probes; the server (Supabase) then kills it on
                # idle_in_transaction_session_timeout, so the NEXT probe hits a
                # dead connection and fails fast. Autocommit means no dangling
                # txn — each SELECT stands alone.
                s.db.autocommit = True
                # Bounded so a pathological query can't hold the shared
                # connection: a probe answers fast or not at all (the client
                # falls back gracefully). Low enough that the user sees the
                # fallback in ~2.5s instead of hanging for 10s.
                s.db.execute("SET statement_timeout='2500'")
        except Exception:
            pass
        _probe["store"] = s
    return _probe["store"]


def _usd(v):
    try:
        return f"${int(v) / 1e6:,.2f}"
    except Exception:
        return "$?"


def _short(a, n=6):
    return a if len(a) <= 13 else f"{a[:n]}…{a[-4:]}"


def _trust_of(s, chain, addr):
    """Latest payment-grounded trust score for a (chain, seller), or None."""
    try:
        r = s.db.execute(s.q(
            "SELECT score, grade, provisional FROM seller_scores "
            "WHERE seller=? AND chain=? ORDER BY measured_date DESC LIMIT 1"),
            (addr, chain)).fetchone()
        if r and r[0] is not None:
            return f"trust {r[1]} {r[0]:.0f}" + ("*" if r[2] else "")
    except Exception:
        pass
    return None


def _seller_rows(s, addr):
    out = []
    for ch in PROBE_CHAINS:
        r = s.db.execute(s.q(
            "SELECT COUNT(*), COALESCE(SUM(amount),0), MIN(block_timestamp), "
            "MAX(block_timestamp) FROM settlements WHERE chain=? AND seller=?"),
            (ch, addr)).fetchone()
        if r and r[0]:
            first = datetime.fromtimestamp(r[2], timezone.utc).strftime("%d %b") if r[2] else "?"
            last = datetime.fromtimestamp(r[3], timezone.utc).strftime("%d %b") if r[3] else "?"
            trust = _trust_of(s, ch, addr)
            out.append({"label": _short(addr), "tag": f"seller · {ch}",
                        "amt": f"{r[0]:,} setts · {_usd(r[1])} · {first}–{last}"
                               + (f" · {trust}" if trust else "")})
    return out


def _fac_totals(s):
    """Nightly-precomputed facilitator totals (probe_facilitator_totals meta).
    The probe must NEVER live-aggregate a facilitator's settlement rows — a big
    relayer (Coinbase on base: 8M+ rows) blows the statement timeout. Cached in
    process for 15 min."""
    now = time.monotonic()
    cached = _probe.get("fac_totals")
    if cached and now - cached[0] < 900:
        return cached[1]
    data = {"by_facilitator": {}, "by_address": {}}
    try:
        raw = s.get_meta("probe_facilitator_totals")
        if raw:
            data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        pass
    _probe["fac_totals"] = (now, data)
    return data


def _facilitator_rows(s, addr):
    out = []
    by_addr = _fac_totals(s).get("by_address") or {}
    for ch in PROBE_CHAINS:
        # registry relayers come from the precompute (constant-time; the live
        # aggregate below would time out on the big ones)
        hit = (by_addr.get(ch) or {}).get(addr if ch == "solana" else addr.lower())
        if hit:
            out.append({"label": _short(addr), "tag": f"relayer · {ch}",
                        "amt": f"{hit[0]:,} setts relayed · {_usd(hit[1])}"})
            continue
        # non-registry address: live lookup, cheap unless it relayed millions —
        # then statement_timeout cancels it and the caller's isolation keeps
        # the rest of the probe response intact
        r = s.db.execute(s.q(
            "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM settlements "
            "WHERE chain=? AND facilitator=?"), (ch, addr)).fetchone()
        if r and r[0]:
            out.append({"label": _short(addr), "tag": f"relayer · {ch}",
                        "amt": f"{r[0]:,} setts relayed · {_usd(r[1])}"})
    return out


def _tx_rows(s, txh):
    rows = s.db.execute(s.q(
        "SELECT chain, seller, payer, amount, block_number, facilitator "
        "FROM settlements WHERE tx_hash=?"), (txh,)).fetchall()
    return [{"label": _short(txh, 8), "tag": f"tx · {r[0]} · block {r[4]:,}",
             "amt": f"{_short(r[2] or '?')} → {_short(r[1] or '?')} · {_usd(r[3])}"
                    + (f" · via {_short(r[5])}" if r[5] else "")}
            for r in rows]


def _name_rows(s, q):
    out = []
    by_fac = _fac_totals(s).get("by_facilitator") or {}
    for name, chains in _registry().items():
        if q not in name.lower():
            continue
        n_addr = sum(len(lst) for lst in chains.values() if isinstance(lst, list))
        # totals come ONLY from the nightly precompute — a live COUNT+SUM here
        # was the probe's original timeout bug ("base" matches coinbase -> 8M-row
        # aggregate). Missing precompute degrades to the relayer count, never a
        # live scan.
        parts = [f"{ch} {n:,}·{_usd(vol)}"
                 for ch, (n, vol) in sorted((by_fac.get(name) or {}).items())]
        out.append({"label": name, "tag": f"facilitator · {n_addr} relayers",
                    "amt": " / ".join(parts) if parts else "no settlements indexed"})
        if len(out) >= 4:
            break
    return out


def _domain_rows(s, q):
    out = []
    d = s.db.execute("SELECT MAX(measured_date) FROM endpoint_liveness").fetchone()[0]
    if not d:
        return out
    like = f"%{q}%"
    # per-origin reliability grades (latest scoring day), joined onto matches
    grades = {}
    try:
        gd = s.db.execute(
            "SELECT MAX(measured_date) FROM endpoint_scores").fetchone()[0]
        if gd:
            grades = dict(s.db.execute(s.q(
                "SELECT origin, grade FROM endpoint_scores WHERE measured_date=?"),
                (gd,)).fetchall())
    except Exception:
        pass
    from urllib.parse import urlparse as _up
    for url, status, lat, v402, wallet in s.db.execute(s.q(
            "SELECT endpoint_url, http_status, latency_ms, valid_402, seller_wallet "
            "FROM endpoint_liveness WHERE measured_date=? AND endpoint_url LIKE ? "
            "LIMIT 4"), (d, like)).fetchall():
        state = ("live" if status else "unreachable") + (f" · {status}" if status else "")
        p = _up(url)
        g = grades.get(f"{p.scheme}://{p.netloc}") if p.hostname else None
        out.append({"label": url[:52], "tag": f"endpoint · {state}"
                           + (f" · grade {g}" if g else ""),
                    "amt": (f"{lat}ms" if lat else "—")
                           + (" · valid 402" if v402 else "")
                           + (f" · pays {_short(wallet)}" if wallet else "")})
        if wallet and _HEX_ADDR.match(wallet or ""):
            out.extend(_seller_rows(s, wallet.lower())[:1])
    di = s.db.execute("SELECT MAX(measured_date) FROM domain_intel").fetchone()[0]
    if di:
        for dom, created, issuer, cage in s.db.execute(s.q(
                "SELECT domain, whois_created, cert_issuer, cert_age_days "
                "FROM domain_intel WHERE measured_date=? AND domain LIKE ? LIMIT 3"),
                (di, like)).fetchall():
            age = ""
            try:
                dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                days = (datetime.now(timezone.utc) - dt.replace(tzinfo=dt.tzinfo or timezone.utc)).days
                age = f"domain {days / 365:.1f}y" if days >= 365 else f"domain {days}d"
            except Exception:
                pass
            out.append({"label": dom, "tag": "domain intel",
                        "amt": " · ".join(x for x in (age, issuer and f"cert {issuer}",
                                                      cage is not None and f"cert {cage}d") if x)})
    return out


def probe(q: str) -> dict:
    q = (q or "").strip()[:200]      # bounded: q feeds LIKE patterns + echo
    if len(q) < 3:
        return {"query": q, "results": []}
    with _probe["lock"]:
        try:
            s = _probe_store()

            def part(fn, *a):
                """One probe section. A section-level failure (e.g. a
                statement timeout on one lookup) degrades that section to
                empty instead of erroring the whole probe — but a DEAD
                connection re-raises so the outer handler reconnects."""
                try:
                    return fn(s, *a)
                except Exception as pe:
                    try:
                        s.db.execute("SELECT 1").fetchone()   # still alive?
                    except Exception:
                        raise pe                              # dead -> outer
                    print(f"[stats] probe section {fn.__name__} failed "
                          f"(isolated): {type(pe).__name__}: {str(pe)[:80]}",
                          file=sys.stderr, flush=True)
                    return []

            ql = q.lower()
            res = []
            if _HEX_TX.match(q):
                res = part(_tx_rows, ql)
            elif _HEX_ADDR.match(q):
                res = part(_seller_rows, ql) + part(_facilitator_rows, ql)
            elif _B58.match(q):
                res = part(_seller_rows, q) + part(_facilitator_rows, q)
            else:
                res = part(_name_rows, ql)
                if "." in q or len(res) < 2:
                    res += part(_domain_rows, ql)
            return {"query": q, "results": res[:8]}
        except Exception as e:
            # drop the connection so the next probe reconnects cleanly
            try:
                _probe["store"].close()
            except Exception:
                pass
            _probe["store"] = None
            # static body only — don't leak the exception class to clients (LB-5)
            print(f"[stats] probe error: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            return {"query": q, "results": [], "error": "probe unavailable"}


def _load_cache():
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return None


def _save_cache(data):
    try:
        CACHE.write_text(json.dumps(data))
    except Exception:
        pass


def refresher():
    # Serve the last persisted compute immediately so the dashboard shows real
    # numbers from the first request, even before a fresh compute lands (or if the
    # DB is currently too busy to answer).
    if _state["data"] is None:
        cached = _load_cache()
        if cached:
            _state["data"] = cached
            print("[stats] seeded from cached compute", flush=True)
    while True:
        _use_flush()             # persist free-tier counters (route+day only)
        try:
            _state["data"] = compute()
            _state["err"] = None
            _state["ts"] = time.time()
            _save_cache(_state["data"])          # persist last-good across restarts
        except Exception as e:
            _state["err"] = f"{type(e).__name__}: {e}"
            # Reflect the outage in the SERVED copy so the dashboard doesn't sit
            # all-green while the DB is down (audit: hardcoded-green health). A
            # subsequent successful compute replaces the whole blob and recovers.
            # Copy-on-write: request threads may be serializing the current dict.
            data = _state.get("data")
            if isinstance(data, dict) and isinstance(data.get("health"), dict):
                _state["data"] = {**data, "health": {
                    **data["health"], "database": "crit", "stale": True}}
            print("[stats] compute failed; keeping last good data:", _state["err"], flush=True)
        time.sleep(REFRESH)


class Handler(BaseHTTPRequestHandler):
    server_version = "merona-stats/1.0"
    sys_version = ""                              # don't advertise the Python version
    protocol_version = "HTTP/1.1"                 # keep-alive under load
    timeout = 30          # slowloris backstop: a stalled peer frees its thread

    def _send(self, code, body, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        body = body or b""
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":   # HEAD: headers only
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/probe":
            if not _PROBE_RL.take(_client_ip(self)):
                return self._send(429, json.dumps({"error": "rate limited"}).encode(),
                                  "application/json")
            q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0]
            # short edge cache: identical probes coalesce, but results still refresh
            self._send(200, json.dumps(probe(q)).encode(), "application/json",
                       cache="public, max-age=15")
        elif path == "/stats.json":
            if _state["data"]:
                # TTL must exceed the client poll interval or the edge can never
                # hit: the dashboard polls every 60s, so a 30s TTL guaranteed a
                # miss every single time and every poll reached this box to hand
                # back bytes it had already served. The payload itself only
                # changes every REFRESH (300s), so 120s costs nothing real.
                self._send(200, json.dumps(_state["data"]).encode(),
                           "application/json", cache="public, max-age=120")
            else:
                # never echo _state["err"] (raw backend/DB text) to clients (audit LB-5)
                self._send(503, json.dumps({"error": "warming up"}).encode(),
                           "application/json")
        elif path == "/mismatches":
            # human-readable viewer for the free feed (renders /mismatches.json
            # client-side) — the dashboard's floating card lands here
            try:
                body = (ROOT / "dashboard" / "mismatches.html").read_bytes()
                _use_bump("viewer")
                # static shell; the feed it renders is written once a night
                self._send(200, body, "text/html; charset=utf-8",
                           cache="public, max-age=900")
            except Exception:
                self._send(404, b"not found", "text/plain")
        elif path in ("/privacy", "/editorial"):
            # Static policy pages. Named from a fixed whitelist, never from the
            # request path — the mapping is the only thing that can reach disk.
            try:
                name = {"/privacy": "privacy.html",
                        "/editorial": "editorial.html"}[path]
                body = (ROOT / "dashboard" / name).read_bytes()
                self._send(200, body, "text/html; charset=utf-8",
                           cache="public, max-age=3600")
            except Exception:
                self._send(404, b"not found", "text/plain")
        elif path == "/mismatches.json":
            # FREE public payTo-integrity feed (indexer/payto_watch.py writes
            # it nightly, atomically). Static read, edge-cacheable; the
            # disclaimer travels inside the JSON itself.
            try:
                body = (ROOT / "data" / "indexer" /
                        "payto_mismatches.json").read_bytes()
                _use_bump("bulk")
                # regenerated once a night — a 5-min TTL bought nothing at this
                # request rate (arrivals are spaced wider than the TTL, so every
                # one missed). 15 min still surfaces a nightly rewrite promptly.
                self._send(200, body, "application/json",
                           cache="public, max-age=900")
            except Exception:
                self._send(404, json.dumps(
                    {"error": "feed not yet generated"}).encode(),
                    "application/json")
        elif path == "/check":
            # FREE single lookup over the same feed: /check?q=<payto|url>.
            # Answers from the cached feed file only — no DB, no key; rate
            # limited like /probe. The agent pre-pay convenience path.
            if not _CHECK_RL.take(_client_ip(self)):
                return self._send(429, json.dumps({"error": "rate limited"}).encode(),
                                  "application/json")
            q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0].strip()
            if not q or len(q) > 256:
                return self._send(400, json.dumps(
                    {"error": "pass ?q=<payTo address or endpoint URL>"}).encode(),
                    "application/json")
            feed = _load_feed()
            if feed is None:
                return self._send(404, json.dumps(
                    {"error": "feed not yet generated"}).encode(),
                    "application/json")
            import payto_watch
            _use_bump("check")
            # derived from the same nightly feed: the answer for a given q is
            # stable all day, so repeat lookups of a hot wallet coalesce at the
            # edge instead of recomputing here. Anonymity is unaffected — the
            # q= never lands in a log either way (deploy/Caddyfile).
            self._send(200, json.dumps(payto_watch.search_feed(feed, q)).encode(),
                       "application/json", cache="public, max-age=900")
        elif path in ("/", "/index.html"):
            # static between deploys; kept short so a push shows up quickly
            self._send(200, _page_bytes(), "text/html; charset=utf-8",
                       cache="public, max-age=120")
        elif path in ("/favicon.png", "/favicon.ico", "/apple-touch-icon.png"):
            # real files: Safari ignores data-URI favicons. .ico requests get
            # the PNG too — every modern browser sniffs content-type.
            name = "apple-touch-icon.png" if "apple" in path else "favicon.png"
            try:
                body = (ROOT / "dashboard" / name).read_bytes()
                self._send(200, body, "image/png", cache="public, max-age=86400")
            except Exception:
                self._send(404, b"not found", "text/plain")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    do_HEAD = do_GET          # uptime monitors probe with HEAD

    def log_message(self, *a):
        pass


def main():
    threading.Thread(target=refresher, daemon=True).start()
    for _ in range(60):                       # let the first compute land before serving
        if _state["data"] or _state["err"]:
            break
        time.sleep(0.5)
    port = int(os.environ.get("STATS_PORT", "8088"))
    print(f"[stats] serving dashboard + /stats.json on {BIND_HOST}:{port} "
          f"(refresh {REFRESH}s)", flush=True)
    ThreadingHTTPServer((BIND_HOST, port), Handler).serve_forever()


if __name__ == "__main__":
    main()

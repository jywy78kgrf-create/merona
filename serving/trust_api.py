"""m.Score API (Merona) — payment-grounded trust scores as a service.

The productized read path over the nightly scoring layer (indexer/scores.py,
indexer/payer_scores.py) — BOTH SIDES of an x402 transaction:
    GET /v1/trust/{chain}/{address}   SELL side: is this endpoint worth paying?
    GET /v1/agent/{chain}/{address}   BUY side: an agent paid me, who is it?
    GET /v1/endpoint?origin=URL       endpoint reliability grade
    GET /v1/health                    liveness (also /healthz)
    GET /                             API index

The buy side deliberately returns `score: null` / grade UNKNOWN for wallets
with too little history, and says so in the payload. Most agents have none —
the median Base agent made exactly one payment — and a reputation product that
converts absence of evidence into a number is inventing the figure that
matters most.

Every trust response carries a VERIFICATION block: the snapshot date the score
was computed from, that snapshot file's sha256, and the public anchors repo
where that hash was committed before publication. Anyone holding the published
snapshot can recompute the score and check the hash — a trust score whose own
integrity is provable (the moat; see indexer/scores.py docstring).

Auth and limits:
  - API keys from TRUST_API_KEYS ("key1:name1,key2:name2") or
    TRUST_API_KEYS_FILE (one "key,name" per line, # comments ok), sent as
    the X-API-Key header ONLY (URL keys are rejected — they leak into access
    logs). With no keys configured the API serves anonymously, rate-limited
    per client IP — the demo/launch mode.
  - Token-bucket rate limit per identity: TRUST_API_RPM (default 60/min),
    burst TRUST_API_BURST (default 20). 429 + Retry-After when exhausted.
  - Usage metering: one JSONL line per request appended to TRUST_API_USAGE
    (default serving/trust_api_usage.jsonl) — {ts, id, path, status}.

READ-ONLY by construction: issues only SELECTs; reconnects on DB failure.

Run on the box (reuses the venv + /etc/x402-index.env):
  TRUST_API_PORT=8402 .venv/bin/python serving/trust_api.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "indexer"))
from storage import open_store                      # noqa: E402

PORT = int(os.environ.get("TRUST_API_PORT", "8402"))
RPM = float(os.environ.get("TRUST_API_RPM", "60"))
BURST = float(os.environ.get("TRUST_API_BURST", "20"))
USAGE_PATH = Path(os.environ.get(
    "TRUST_API_USAGE", str(ROOT / "serving" / "trust_api_usage.jsonl")))
SNAP_DIR = ROOT / "data" / "indexer" / "snapshots"
ADVERSE_PATH = ROOT / "data" / "indexer" / "adverse_findings.json"
ANCHORS_URL = os.environ.get(
    "TRUST_API_ANCHORS_URL",
    "https://github.com/jywy78kgrf-create/merona-anchors")

_HEX_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_B58_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_CHAIN = re.compile(r"^[a-z][a-z0-9_-]{1,15}$")

_db = {"store": None, "lock": threading.Lock()}
_snap_sha: dict = {}      # snapshot_date -> sha256 of seller_aggregates.csv
_usage_lock = threading.Lock()


# --- keys ---------------------------------------------------------------------
def _load_keys() -> dict:
    """{key: name}. Empty dict => anonymous mode (per-IP rate limiting)."""
    keys: dict = {}
    env = os.environ.get("TRUST_API_KEYS", "")
    for part in env.split(","):
        part = part.strip()
        if part:
            k, _, name = part.partition(":")
            keys[k.strip()] = name.strip() or k[:8]
    path = os.environ.get("TRUST_API_KEYS_FILE")
    if path and Path(path).exists():
        for ln in Path(path).read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                k, _, name = ln.partition(",")
                keys[k.strip()] = name.strip() or k[:8]
    return keys


KEYS = _load_keys()


# --- rate limiting (token bucket per identity) ---------------------------------
class _Buckets:
    def __init__(self, rpm: float, burst: float):
        self.rate = rpm / 60.0
        self.burst = burst
        self.b: dict = {}
        self.lock = threading.Lock()

    def take(self, ident: str) -> float:
        """0 if allowed; else seconds to wait."""
        now = time.monotonic()
        with self.lock:
            # Prune BEFORE the early return so a flood of fresh identities (e.g.
            # spoofed X-Forwarded-For) can't grow the map without bound and OOM
            # the box (audit LB-6). Was previously unreachable on the allow path.
            if len(self.b) > 20000:
                cutoff = now - 3600
                self.b = {k: v for k, v in self.b.items() if v[1] > cutoff}
                if len(self.b) > 20000:
                    # Age prune removed nothing (a sub-hour flood of fresh
                    # identities). Hard-cap: keep the most recently seen half so
                    # memory stays bounded even mid-attack.
                    keep = sorted(self.b.items(), key=lambda kv: kv[1][1])
                    self.b = dict(keep[len(keep) // 2:])
            tokens, ts = self.b.get(ident, (self.burst, now))
            tokens = min(self.burst, tokens + (now - ts) * self.rate)
            if tokens >= 1.0:
                self.b[ident] = (tokens - 1.0, now)
                return 0.0
            self.b[ident] = (tokens, now)
            return (1.0 - tokens) / self.rate


BUCKETS = _Buckets(RPM, BURST)
# Trust X-Forwarded-For only when the peer is the local reverse proxy (Caddy).
# Anything else lets a client spoof its own rate-limit identity (audit LB-6).
TRUST_PROXY = os.environ.get("TRUST_API_TRUST_PROXY", "1") == "1"
# Set EDGE_CF=1 in /etc/x402-index.env ONLY once Cloudflare fronts Caddy AND the
# origin firewall admits only Cloudflare IPs (deploy/CLOUDFLARE.md). Behind CF,
# X-Forwarded-For carries CF edge IPs (or a client-spoofable chain), so the real
# client is CF-Connecting-IP. Flipping this early lets direct callers spoof it.
EDGE_CF = os.environ.get("EDGE_CF", "0") == "1"
BIND_HOST = os.environ.get("TRUST_API_HOST", "127.0.0.1")   # loopback default (LB-2)
MAX_PATH_LOG = 120        # cap the logged path length (usage-file abuse, LB-6)
# x402 payments (env-gated: no X402_PAYTO -> keys-only, exactly as before)
import x402pay  # noqa: E402
PUBLIC_BASE = os.environ.get("TRUST_API_PUBLIC_URL",
                             "https://api.merona.io").rstrip("/")
PAY_DESC = "merona m.Score lookup — wash-aware x402 trust data"
# mismatch feed (free MCP tools read it; same file the dashboard serves)
FEED_PATH = Path(os.environ.get(
    "X402_FEED_PATH", str(ROOT / "data" / "indexer" / "payto_mismatches.json")))
_feed_cache = {"mtime": None, "feed": None}


def _feed():
    try:
        m = FEED_PATH.stat().st_mtime
    except OSError:
        return None
    if _feed_cache["mtime"] != m:
        try:
            _feed_cache["feed"] = json.loads(FEED_PATH.read_text())
            _feed_cache["mtime"] = m
        except Exception:
            return _feed_cache["feed"]
    return _feed_cache["feed"]

# Rotate the usage file once instead of growing without bound (audit LB-6): at
# the cap it is renamed to <name>.1 (previous generation overwritten).
USAGE_MAX_BYTES = int(os.environ.get("TRUST_API_USAGE_MAX_MB", "64")) * 1024 * 1024


# --- store (cached + lock + reconnect, same idiom as stats_server) -------------
def _open():
    # Prefer the read-only serving DSN when configured (audit LB-4): the public
    # API should never hold the writer role's credentials. Falls back to
    # X402_DB_URL (inside open_store) until the read-only role exists.
    s = open_store(os.environ.get("TRUST_API_SQLITE"),
                   url=os.environ.get("X402_DB_URL_RO") or None,
                   init_schema=False)     # read-only role: no DDL on connect
    try:
        if s.PH == "%s":
            # answers are single-row index seeks; a busy DB should fail fast
            s.db.execute("SET statement_timeout='10000'")
            s.db.commit()
    except Exception:
        pass
    if hasattr(s, "path"):
        # SQLite backend: handler threads share this one handle, serialized by
        # _db["lock"], so cross-thread use is safe — but sqlite3 forbids it by
        # default. Reopen the connection with the check disabled.
        import sqlite3
        s.db.close()
        s.db = sqlite3.connect(s.path, timeout=30, check_same_thread=False)
    return s


def _store():
    if _db["store"] is None:
        _db["store"] = _open()
    return _db["store"]


def _query(sql: str, params=()):
    """Locked read with one reconnect attempt on failure."""
    with _db["lock"]:
        try:
            s = _store()
            return s.db.execute(s.q(sql), params).fetchall()
        except Exception:
            try:
                _db["store"] = None
                s = _store()
                return s.db.execute(s.q(sql), params).fetchall()
            except Exception:
                raise


# --- verification block ---------------------------------------------------------
def _snapshot_sha(snapshot_date: str):
    """sha256 of that day's seller_aggregates.csv (cached). None if the file
    isn't on this host — the anchor repo still holds the published hash."""
    if not snapshot_date:
        return None
    if snapshot_date in _snap_sha:
        return _snap_sha[snapshot_date]
    p = SNAP_DIR / snapshot_date / "seller_aggregates.csv"
    sha = None
    try:
        if p.exists():
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception:
        sha = None
    _snap_sha[snapshot_date] = sha
    return sha


def _snapshot_sha_file(snapshot_date: str, fname: str):
    if not snapshot_date:
        return None
    key = f"{snapshot_date}/{fname}"
    if key in _snap_sha:
        return _snap_sha[key]
    sha = None
    try:
        p = SNAP_DIR / snapshot_date / fname
        if p.exists():
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception:
        sha = None
    _snap_sha[key] = sha
    return sha


def _verification(snapshot_date: str, fname: str = "seller_aggregates.csv"
                  ) -> dict:
    return {
        "snapshot_date": snapshot_date,
        "snapshot_file": fname,
        "snapshot_sha256": (_snapshot_sha(snapshot_date)
                            if fname == "seller_aggregates.csv"
                            else _snapshot_sha_file(snapshot_date, fname)),
        "anchors": ANCHORS_URL,
        "how": ("hash the published snapshot file, match it against the "
                "public anchor for that date, then re-run indexer/scores.py "
                "on it — the score re-derives exactly"),
    }


def agent_lookup(chain: str, address: str) -> tuple[int, dict]:
    """Buy-side score: an agent paid me — who am I dealing with?

    A 404 here means the index has never seen this wallet settle, which is the
    COMMON case and must read as "no evidence", not "bad actor". The response
    says so explicitly, because the whole failure mode of agent reputation is a
    product that quietly converts absence of history into a low score.
    """
    if not _CHAIN.match(chain):
        return 400, {"error": "bad chain"}
    if _HEX_ADDR.match(address):
        address = address.lower()
    elif not _B58_ADDR.match(address):
        return 400, {"error": "bad address"}
    rows = _query(
        "SELECT measured_date, score, grade, status, provisional, "
        "snapshot_date, tx_count, unique_sellers, self_pay_ratio, "
        "exclusive_repeat, components, score_version "
        "FROM payer_scores WHERE chain=? AND payer=? "
        "ORDER BY measured_date DESC LIMIT 1", (chain, address))
    if not rows:
        return 404, {
            "error": "no_history", "chain": chain, "agent": address,
            "score": None, "grade": "UNKNOWN",
            "detail": ("this wallet has never settled an x402 payment in the "
                       "witnessed index. That is the normal case — the median "
                       "agent has made one payment — and it is NOT evidence "
                       "of bad behaviour. Treat it as a first-time customer, "
                       "not a declined one."),
        }
    (mdate, score, grade, status, prov, snap, tx, sellers, spr, excl, comp,
     ver) = rows[0]
    try:
        comp = json.loads(comp) if isinstance(comp, str) else (comp or {})
    except Exception:
        comp = {}
    resp_extra = {}
    if status == "thin_history" and "indicative_score" in comp:
        # the computed number for an ungraded wallet — shown, never sortable
        # as a score (top-level "score" stays null on purpose)
        resp_extra["indicative_score"] = comp["indicative_score"]
    return 200, {
        "chain": chain, "agent": address,
        "score": score, "grade": grade, "status": status,
        **resp_extra,
        "provisional": bool(prov), "measured_date": mdate,
        "score_version": ver,
        "tx_count": tx, "unique_sellers": sellers,
        "self_pay_ratio": spr, "exclusive_repeat": bool(excl),
        "components": comp,
        "reading": ("A LOW score is strong evidence of structure (one "
                    "counterparty, self-payment). A HIGH score is weak "
                    "evidence of virtue — it means we saw nothing wrong, "
                    "across the sellers we observe."),
        "limits": ["tenure is rail tenure (first to last observed x402 "
                   "payment), not wallet age",
                   "witnessed regime only — excludes backfilled history",
                   "v1 has no funding-graph component; a wallet funded by "
                   "the seller it pays is not yet penalised"],
        "verification": _verification(snap, "payer_aggregates.csv"),
    }


# --- handlers -------------------------------------------------------------------
_adverse: dict | None = None


def _adverse_findings() -> dict:
    """{(chain, address_lower): finding} from the curated file. Cached; a
    missing/broken file is treated as no findings, never an error."""
    global _adverse
    if _adverse is None:
        out: dict = {}
        try:
            for f in json.loads(ADVERSE_PATH.read_text()).get("findings", []):
                if f.get("chain") and f.get("address"):
                    out[(f["chain"], f["address"].lower())] = f
        except Exception:
            out = {}
        _adverse = out
    return _adverse


def trust_lookup(chain: str, address: str) -> tuple[int, dict]:
    if not _CHAIN.match(chain):
        return 400, {"error": "bad chain"}
    if _HEX_ADDR.match(address):
        address = address.lower()
    elif not _B58_ADDR.match(address):
        return 400, {"error": "bad address"}
    finding = _adverse_findings().get((chain, address))
    rows = _query(
        "SELECT measured_date, score, grade, provisional, snapshot_date, "
        "tx_count, unique_payers, self_pay_ratio, listed, components, "
        "score_version FROM seller_scores WHERE chain=? AND seller=? "
        "ORDER BY measured_date DESC LIMIT 1", (chain, address))
    if not rows:
        # A verified adverse finding stands even when the address is no longer
        # scored as a merchant (a facilitator's own address is excluded from the
        # seller universe, so mrdn 404s on the normal path — the finding is the
        # whole answer here).
        if finding:
            return 200, {
                "chain": chain, "seller": address,
                "score": None, "grade": finding.get("grade_override", "F"),
                "status": "adverse_finding", "measured_date": None,
                "adverse_findings": [finding]}
        return 404, {"error": "unknown seller",
                     "detail": "no settlements observed for this address on "
                               "this chain in the witnessed index"}
    (mdate, score, grade, prov, snap, tx, payers, spr, listed, comp,
     ver) = rows[0]
    try:
        comp = json.loads(comp) if isinstance(comp, str) else (comp or {})
    except Exception:
        comp = {}
    resp = {
        "chain": chain, "seller": address,
        "score": score, "grade": grade, "provisional": bool(prov),
        "measured_date": mdate, "score_version": ver,
        "tx_count": tx, "unique_payers": payers,
        "self_pay_ratio": spr, "listed": bool(listed),
        "components": comp,
        "verification": _verification(snap),
    }
    if finding:
        # A hand-verified finding overrides the automated grade downward — never
        # upward — and rides in the payload so the caller sees the evidence.
        resp["grade"] = finding.get("grade_override", resp["grade"])
        resp["score"] = None
        resp["status"] = "adverse_finding"
        resp["adverse_findings"] = [finding]
    return 200, resp


def endpoint_lookup(origin: str) -> tuple[int, dict]:
    origin = (origin or "").strip().rstrip("/")
    if not origin or len(origin) > 300:
        return 400, {"error": "bad origin"}
    rows = _query(
        "SELECT measured_date, score, grade, provisional, days_observed, "
        "probes, uptime_rate, valid_402_rate, median_latency_ms "
        "FROM endpoint_scores WHERE origin=? "
        "ORDER BY measured_date DESC LIMIT 1", (origin,))
    if not rows:
        return 404, {"error": "unknown origin",
                     "detail": "origin not observed by the nightly probes"}
    mdate, score, grade, prov, days, probes, up, v402, lat = rows[0]
    resp = {
        "origin": origin, "score": score, "grade": grade,
        "provisional": bool(prov), "measured_date": mdate,
        "days_observed": days, "probes": probes,
        "uptime_rate": up, "valid_402_rate": v402,
        "median_latency_ms": lat,
    }
    # evidence tier travels with the letter: unrated below 7 observed days,
    # provisional letter from 7, verified from 14 — same UNKNOWN-over-invented
    # rule the agent scores follow
    if grade is None:
        resp["tier"] = "unrated"
        resp["note"] = (f"unrated: {days} observation day(s) so far — a "
                        "letter grade is published only after enough "
                        "distinct probe days to support one")
    else:
        resp["tier"] = "provisional" if prov else "verified"
    return 200, resp


INDEX = {
    "service": "m.Score API",
    "endpoints": {
        "GET /v1/trust/{chain}/{address}": "seller trust score (0-100, A-F)",
        "GET /v1/agent/{chain}/{address}": "payer/agent trust score; returns "
                                           "UNKNOWN rather than inventing a "
                                           "score for an unseen wallet",
        "GET /v1/endpoint?origin=URL": "endpoint reliability grade",
        "GET /v1/health": "liveness",
    },
    "mcp": {"endpoint": "POST /mcp (streamable HTTP)",
            "manifest": "GET /mcp.json",
            "tools": "payto_check + mismatch_feed + clean_stats free; "
                     "trust_score paid"},
    "auth": "X-API-Key header, or pay per call via x402 (X-PAYMENT) when "
            "enabled — no key, no signup",
    "verification": "every score carries the snapshot sha256 + public anchor "
                    "so it can be independently re-derived",
}


# Browsers clicking through from the dashboard get a readable landing page;
# agents (no text/html in Accept) keep getting the JSON index untouched.
LANDING_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>m.Score API — merona</title><style>
:root{--bg:#0d0b11;--ink:#efe9e2;--sub:#8a8095;--teal:#2fd6c3;--amber:#eda23e;
--vio:#a98ae0;--line:rgba(239,233,226,.12)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:13px;line-height:1.65;-webkit-font-smoothing:antialiased}
.wrap{max-width:760px;margin:0 auto;padding:44px 22px 60px}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:6px}
.mark{width:34px;height:34px;border-radius:9px;background:#efe9e2;color:#0d0b11;
display:flex;align-items:center;justify-content:center;font-weight:700;
font-size:19px;position:relative}
.mark i{position:absolute;right:5px;bottom:5px;width:6px;height:6px;
border-radius:2px;background:#0fa88c;font-style:normal}
h1{font-size:19px;margin:0;font-weight:600}
.tag{color:var(--sub);margin:0 0 18px}
h2{font-size:11px;letter-spacing:.22em;text-transform:uppercase;
color:var(--sub);margin:30px 0 10px;font-weight:600}
.ep{display:flex;gap:14px;padding:7px 0;border-bottom:1px solid var(--line);
flex-wrap:wrap}
.ep code{color:var(--teal);flex:none}
.ep span{color:var(--sub)}
.ep .paid{color:var(--amber)}
pre{background:rgba(239,233,226,.05);border:1px solid var(--line);
border-radius:10px;padding:14px 16px;overflow-x:auto;color:var(--ink);
font-size:12px;line-height:1.7}
pre b{color:var(--teal);font-weight:500}
a{color:var(--vio);text-decoration:none;border-bottom:1px solid rgba(169,138,224,.35)}
.foot{margin-top:34px;color:var(--sub);font-size:11px;line-height:1.8}
.mcp code{color:var(--vio)}
nav.topnav{display:flex;gap:24px;flex-wrap:wrap;margin:0 0 30px;
padding-bottom:14px;border-bottom:1px solid var(--line);font-size:10px;
letter-spacing:.18em;text-transform:uppercase;font-weight:600}
nav.topnav a{color:var(--sub);border-bottom:none}
nav.topnav a:hover{color:var(--ink)}
nav.topnav a.on{color:var(--teal)}
</style></head><body><div class="wrap">
<div class="brand"><div class="mark">m<i></i></div><h1>m.Score API</h1></div>
<p class="tag">wash-aware trust scores for the x402 economy —
<a href="https://merona.io">merona.io</a></p>

<nav class="topnav" aria-label="site navigation"><a href="https://merona.io/">INDEX</a><a href="https://merona.io/mismatches">FREE CHECK</a><a href="https://merona.io/findings">FINDINGS</a><a class="on" href="/">API</a></nav>

<h2>Scores — $0.005/call via x402, or an API key</h2>
<div class="ep"><code>GET /v1/trust/{chain}/{address}</code><span>seller trust score (0&#8211;100, A&#8211;F)</span><span class="paid">paid</span></div>
<div class="ep"><code>GET /v1/agent/{chain}/{address}</code><span>payer/agent score — UNKNOWN if unseen</span><span class="paid">paid</span></div>
<div class="ep"><code>GET /v1/endpoint?origin=URL</code><span>endpoint reliability grade</span><span class="paid">paid</span></div>
<div class="ep"><code>GET /v1/health</code><span>liveness</span></div>

<h2>Pay per call — no key, no signup</h2>
<pre>curl https://api.merona.io/v1/trust/base/0xSELLER
<b># -&gt; 402 + payment instructions (x402 exact scheme, Base USDC)</b>
<b># retry with X-PAYMENT; receipt returned in X-PAYMENT-RESPONSE</b></pre>

<h2 class="mcp">MCP — wire it into your agent</h2>
<div class="ep mcp"><code>POST /mcp</code><span>streamable HTTP · manifest at <a href="/mcp.json">/mcp.json</a></span></div>
<div class="ep mcp"><code>tools</code><span>payto_check · mismatch_feed · clean_stats free — trust_score paid</span></div>

<h2>Free, no key</h2>
<div class="ep"><code>merona.io/check?q=</code><span>one-shot payTo-integrity check (wallet or url)</span></div>
<div class="ep"><code>merona.io/mismatches.json</code><span>bulk feed · <a href="https://merona.io/mismatches">human view</a></span></div>

<p class="foot">Every score carries the snapshot sha256 + public anchor so it
can be independently re-derived. Agents: this same URL returns JSON unless the
request asks for text/html.</p>
</div></body></html>"""


def _meter(ident: str, path: str, status: int) -> None:
    line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                       "id": ident[:64], "path": path[:MAX_PATH_LOG],
                       "status": status})
    try:
        with _usage_lock:
            if (USAGE_PATH.exists()
                    and USAGE_PATH.stat().st_size > USAGE_MAX_BYTES):
                USAGE_PATH.replace(USAGE_PATH.with_name(USAGE_PATH.name + ".1"))
            with open(USAGE_PATH, "a") as f:
                f.write(line + "\n")
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "merona-trust/1.0"
    sys_version = ""                              # don't advertise the Python version
    protocol_version = "HTTP/1.1"                 # keep-alive under load
    timeout = 30          # slowloris backstop: a stalled peer frees its thread

    def _send(self, code: int, obj: dict, extra: dict | None = None,
              cache: str = "public, max-age=300") -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":            # HEAD: headers only
            self.wfile.write(body)

    def _client_ip(self) -> str:
        peer = self.client_address[0]
        # Only honor forwarded-client headers from the trusted local proxy;
        # otherwise a client spoofs its own identity and bypasses the limiter
        # (audit LB-6). Values are length-capped: they become bucket keys and
        # usage-log fields, and must never be attacker-sized.
        if TRUST_PROXY and peer in ("127.0.0.1", "::1"):
            if EDGE_CF:
                # Behind Cloudflare the XFF chain is NOT trustworthy (leftmost
                # entry is client-supplied); CF-Connecting-IP is (origin is
                # firewalled to CF ranges — deploy/CLOUDFLARE.md).
                cf = self.headers.get("CF-Connecting-IP")
                if cf:
                    return cf.strip()[:45]
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                # Caddy APPENDS the real peer as the LAST xff entry; earlier entries
                # are client-supplied and spoofable. Trust the rightmost hop.
                return xff.split(",")[-1].strip()[:45]
        return peer

    def _identity(self) -> tuple[str, bool]:
        """(identity, authorized). Keys are accepted ONLY via the X-API-Key
        header — never in the URL, where they would persist in proxy/CDN access
        logs and browser history (audit should-fix). With keys configured a
        valid key is required; without any, fall back to per-IP anonymous."""
        key = self.headers.get("X-API-Key") or ""
        if KEYS:
            if key in KEYS:
                return f"key:{KEYS[key]}", True
            return f"ip:{self._client_ip()}", False
        return f"ip:{self._client_ip()}", True

    def do_GET(self):        # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/v1/health", "/healthz"):
            return self._send(200, {"ok": True}, cache="no-store")
        if path == "/":
            if "text/html" in (self.headers.get("Accept") or ""):
                body = LANDING_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "public, max-age=300")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            return self._send(200, INDEX)
        if path == "/mcp.json":
            return self._send(200, MCP_MANIFEST)

        ident, ok = self._identity()
        # Rate-limit EVERY request (including unauthenticated 401s) BEFORE any
        # metering — else an anonymous flood fills the usage file unthrottled and
        # contends the write lock (audit LB-6).
        wait = BUCKETS.take(ident)
        if wait > 0:
            return self._send(429, {"error": "rate limited"},
                              {"Retry-After": str(max(1, int(wait + 0.5)))},
                              cache="no-store")
        if not ok:
            # x402 lane: no key needed — pay per call on the rail we index.
            payable = path.startswith(("/v1/trust/", "/v1/agent/",
                                       "/v1/endpoint"))
            if x402pay.enabled() and payable:
                resource = PUBLIC_BASE + path
                pay = self.headers.get("X-PAYMENT")
                if not pay:
                    self._send(402, x402pay.payment_required(resource, PAY_DESC),
                               cache="no-store")
                    return _meter(ident, path, 402)
                # answer FIRST, settle ONLY on success — never charge for a
                # 404/503
                code, obj = self._route(path)
                if code != 200:
                    self._send(code, obj, cache="no-store")
                    return _meter(ident, path, code)
                ok2, receipt, payer, err = x402pay.settle(pay, resource,
                                                          PAY_DESC)
                if not ok2:
                    body = x402pay.payment_required(resource, PAY_DESC)
                    body["error"] = err
                    self._send(402, body, cache="no-store")
                    return _meter(ident, path, 402)
                self._send(200, obj, {"X-PAYMENT-RESPONSE": receipt},
                           cache="no-store")
                return _meter(f"x402:{payer or 'paid'}", path, 200)
            self._send(401, {"error": "invalid or missing API key",
                             "detail": "send X-API-Key"
                             + (" or pay per call via x402"
                                if x402pay.enabled() else "")},
                       cache="no-store")
            # Only record 401s aimed at a real endpoint. A public IP takes a
            # constant drizzle of scanner probes (/.env, /.git/config, CMS and
            # phishing-kit paths); metering those buried genuine usage under
            # ~1300 lines of noise and made the log unable to answer the only
            # question it exists for — is anyone using this?
            if path.startswith("/v1/"):
                _meter(ident, path, 401)
            return

        # A keyed response must never be cached by a shared cache (LB-6).
        resp_cache = ("no-store" if ident.startswith("key:")
                      else "public, max-age=300")
        code, obj = self._route(path)
        self._send(code, obj, cache=resp_cache if code == 200 else "no-store")
        _meter(ident, path, code)

    def _route(self, path: str):
        try:
            m = re.match(r"^/v1/trust/([^/]+)/([^/]+)$", path)
            ma = re.match(r"^/v1/agent/([^/]+)/([^/]+)$", path)
            if m:
                return trust_lookup(m.group(1), m.group(2))
            if ma:
                return agent_lookup(ma.group(1), ma.group(2))
            if path == "/v1/endpoint":
                q = (parse_qs(urlparse(self.path).query).get("origin")
                     or [""])[0]
                return endpoint_lookup(q)
            return 404, {"error": "not found", "index": "/"}
        except Exception as e:
            # Never echo raw backend/DB exception text to clients (audit LB-5).
            print(f"[trust-api] backend error on {path[:MAX_PATH_LOG]}: "
                  f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            return 503, {"error": "backend unavailable"}

    def do_OPTIONS(self):     # noqa: N802 — CORS preflight for /mcp
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-API-Key, X-PAYMENT, "
                         "Mcp-Session-Id, MCP-Protocol-Version")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_POST(self):        # noqa: N802 — MCP streamable-HTTP endpoint
        path = urlparse(self.path).path.rstrip("/")
        if path != "/mcp":
            return self._send(404, {"error": "not found", "index": "/"},
                              cache="no-store")
        ident, _ok = self._identity()
        wait = BUCKETS.take(ident)
        if wait > 0:
            return self._send(429, {"error": "rate limited"},
                              {"Retry-After": str(max(1, int(wait + 0.5)))},
                              cache="no-store")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not 0 < n <= 65536:
            return self._send(413, {"error": "body required, max 64KB"},
                              cache="no-store")
        try:
            msg = json.loads(self.rfile.read(n))
            assert isinstance(msg, dict)
        except Exception:
            return self._send(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700,
                                              "message": "parse error"}},
                              cache="no-store")
        method = msg.get("method") or ""
        mid = msg.get("id")
        if method.startswith("notifications/"):
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        if method == "initialize":
            result = {"protocolVersion": MCP_PROTOCOL,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "merona-mcp", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": MCP_TOOLS}
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = str(params.get("name") or "")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            try:
                out = _mcp_tool_call(self, name, args)
            except Exception as e:
                print(f"[trust-api] mcp tool error {name}: "
                      f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
                out = {"error": "backend unavailable"}
            result = {"content": [{"type": "text",
                                   "text": json.dumps(out)}],
                      "isError": bool(isinstance(out, dict)
                                      and out.get("error"))}
            _meter(ident, f"/mcp:{name[:40]}", 200)
        else:
            return self._send(200, {"jsonrpc": "2.0", "id": mid,
                                    "error": {"code": -32601,
                                              "message": "method not found"}},
                              cache="no-store")
        self._send(200, {"jsonrpc": "2.0", "id": mid, "result": result},
                   cache="no-store")

    do_HEAD = do_GET          # uptime monitors probe with HEAD

    def log_message(self, fmt, *args):   # quiet; metering covers it
        pass


MCP_PROTOCOL = "2025-03-26"
MCP_TOOLS = [
    {"name": "payto_check",
     "description": "FREE — payTo-integrity lookup for a wallet address or "
                    "endpoint URL: catalog-vs-live payout mismatches and "
                    "payout-rotation history, with on-chain settlement "
                    "presence. Recorded facts with dates, not accusations; "
                    "absence of a record is not a clearance.",
     "inputSchema": {"type": "object", "required": ["query"],
                     "properties": {"query": {"type": "string",
                                              "maxLength": 256}}}},
    {"name": "mismatch_feed",
     "description": "FREE — summary of the nightly payTo mismatch feed: "
                    "counts, as-of dates, current catalog-vs-live entries.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "clean_stats",
     "description": "FREE — latest wash-adjusted clean x402 settlement "
                    "figures per chain (the numbers behind merona.io).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "trust_score",
     "description": f"PAID (${x402pay.PRICE_USD:g}/call via x402, or "
                    "api_key) — wash-aware m.Score for a seller wallet: "
                    "grade A-F, component breakdown, wash flags incl. "
                    "cycle detection, snapshot sha for independent "
                    "re-derivation. Pass payment=<base64 X-PAYMENT payload> "
                    "to pay per call, or api_key=<key>.",
     "inputSchema": {"type": "object", "required": ["chain", "address"],
                     "properties": {"chain": {"type": "string"},
                                    "address": {"type": "string"},
                                    "api_key": {"type": "string"},
                                    "payment": {"type": "string"}}}},
]
MCP_MANIFEST = {
    "name": "merona-mcp",
    "description": "merona — the x402 settlement index. Wash-aware trust "
                   "data: payTo integrity checks (free) and m.Score seller "
                   "grades (paid via x402 or API key).",
    "endpoint": "/mcp",
    "transport": "streamable-http",
    "protocolVersion": MCP_PROTOCOL,
    "tools": [{"name": t["name"], "description": t["description"]}
              for t in MCP_TOOLS],
    "site": "https://merona.io",
    "free_feed": "https://merona.io/mismatches.json",
}


def _mcp_tool_call(handler, name: str, args: dict):
    """Returns the MCP tool result content (a dict to be JSON-encoded)."""
    if name == "payto_check":
        feed = _feed()
        if feed is None:
            return {"error": "feed not yet generated"}
        import payto_watch
        q = str(args.get("query") or "")[:256]
        return payto_watch.search_feed(feed, q)
    if name == "mismatch_feed":
        feed = _feed()
        if feed is None:
            return {"error": "feed not yet generated"}
        return {"meta": feed.get("meta"),
                "catalog_vs_live": (feed.get("catalog_vs_live") or [])[:10],
                "payto_rotation_count":
                    len(feed.get("payto_rotation") or [])}
    if name == "clean_stats":
        rows = _query(
            "SELECT chain, clean_settlements, clean_volume_usd, measured_date "
            "FROM clean_metrics cm WHERE measured_date = (SELECT MAX("
            "measured_date) FROM clean_metrics c2 WHERE c2.chain = cm.chain)")
        return {"clean": [{"chain": c, "clean_settlements": int(n or 0),
                           "clean_volume_usd": float(v or 0),
                           "as_of": d} for c, n, v, d in rows],
                "note": "wash-adjusted, registry-scoped; method: merona.io"}
    if name == "trust_score":
        chain = str(args.get("chain") or "")[:32]
        addr = str(args.get("address") or "")[:128]
        key = str(args.get("api_key") or "")
        resource = PUBLIC_BASE + "/mcp/trust_score"
        if KEYS and key in KEYS:
            code, obj = trust_lookup(chain, addr)
            return obj if code == 200 else {"error": obj, "status": code}
        pay = str(args.get("payment") or "")
        if x402pay.enabled():
            if not pay:
                return {"payment_required":
                        x402pay.payment_required(resource, PAY_DESC),
                        "how": "sign an exact-scheme x402 payment for the "
                               "accepts entry above and re-call this tool "
                               "with payment=<base64 payload>"}
            code, obj = trust_lookup(chain, addr)
            if code != 200:
                return {"error": obj, "status": code,
                        "note": "not settled — you were not charged"}
            ok2, receipt, payer, err = x402pay.settle(pay, resource, PAY_DESC)
            if not ok2:
                return {"payment_error": err,
                        "payment_required":
                        x402pay.payment_required(resource, PAY_DESC)}
            obj["payment_receipt_b64"] = receipt
            return obj
        return {"error": "provide api_key (x402 payment not enabled on this "
                         "deployment)"}
    return {"error": f"unknown tool: {name}"}


def main() -> None:
    mode = (f"{len(KEYS)} API key(s)" if KEYS
            else "ANONYMOUS mode (per-IP limits)")
    print(f"[trust-api] {BIND_HOST}:{PORT} · {mode} · {RPM:.0f} rpm "
          f"burst {BURST:.0f} · usage → {USAGE_PATH}", flush=True)
    ThreadingHTTPServer((BIND_HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

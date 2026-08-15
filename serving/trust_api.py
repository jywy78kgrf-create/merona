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

import base64
import csv
import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "indexer"))
from storage import open_store                      # noqa: E402
import jcs                                           # noqa: E402 (serving/jcs.py)

PORT = int(os.environ.get("TRUST_API_PORT", "8402"))
RPM = float(os.environ.get("TRUST_API_RPM", "60"))
BURST = float(os.environ.get("TRUST_API_BURST", "20"))
# Pro tier: a SEPARATE token-bucket pool, so pro traffic and everyone else's
# free/keyed traffic never compete for the same tokens in either direction.
PRO_RPM = float(os.environ.get("TRUST_API_PRO_RPM", "600"))
PRO_BURST = float(os.environ.get("TRUST_API_PRO_BURST", "120"))
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
        # An unreadable/malformed keys file must not take the service down:
        # this runs at import, and the free tools + the x402 payment lane do
        # not depend on keys at all. Fail loud in the journal, run degraded.
        # (Learned live 2026-08-06: a root-owned 600 file under User=x402
        #  crashed the whole API on restart.)
        try:
            for ln in Path(path).read_text().splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    k, _, name = ln.partition(",")
                    keys[k.strip()] = name.strip() or k[:8]
        except OSError as e:
            print(f"[trust-api] WARNING: cannot read TRUST_API_KEYS_FILE "
                  f"{path!r} ({e}) — starting WITHOUT file keys; api_key "
                  f"auth will fail until this is fixed", file=sys.stderr,
                  flush=True)
    return keys


KEYS = _load_keys()


def _is_pro(name: str | None) -> bool:
    """Pro tier rides in the freeform CONFIGURED name of a key ("pro:acme"),
    set by the operator in TRUST_API_KEYS / TRUST_API_KEYS_FILE — e.g.
    "abc123:pro:acme" (env) or "abc123,pro:acme" (keys-file line). This is
    the ONLY input: never derived from anything the client sends, so a
    request cannot forge its own tier by sending a key value that merely
    looks pro-shaped — it must be a key this server was configured with."""
    return bool(name) and name.startswith("pro:")


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
# Pro identities (key name starting "pro:") draw from this pool instead of
# BUCKETS — non-pro traffic never touches it and vice versa (product spec:
# 600 rpm / 120 burst default, 10x the free/keyed default).
PRO_BUCKETS = _Buckets(PRO_RPM, PRO_BURST)
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

# --- signed card envelope (x402 draft-02 / twzrd) --------------------------
# Ed25519 signing over the JCS canonical bytes of a /v1/trust/evaluate body,
# so a verifier with only the response + this API's public key can confirm
# the decision wasn't altered in transit or by a relaying intermediary.
# Env-gated and loaded ONCE at import: X402_TRUST_SIGNING_KEY names a file
# holding the RAW 32-byte Ed25519 private key (the seed, NOT a PEM — see
# serving/README.md for the keygen one-liner and why raw-bytes was chosen
# over PEM here). Missing/unset/unreadable/wrong-length -> signing stays
# disabled: responses are byte-for-byte what they were before this feature,
# just without an "envelope" key. A single stderr warning fires here, at
# import, never per-request — the request path must not know or care why
# signing is off.
#
# WHY module-level globals rather than a class: the handler is instantiated
# per-request (BaseHTTPRequestHandler) and every request would otherwise
# reload/reparse the key file for nothing — signing state is process-wide
# and immutable after import, so it belongs at module scope like KEYS above.
# Tests monkeypatch these three names directly (no key file, no I/O) — see
# indexer/tests/test_trust_envelope.py.
# GUARDED import: `cryptography` is only needed when signing is enabled.
# An environment without it (or with a broken cffi backend — observed
# 2026-08-11 after a container rebuild) must degrade to the same "signing
# disabled, responses omit `envelope`" posture as a missing key file — a
# trust API that crashes at import because an OPTIONAL feature's dependency
# is absent would take down every unsigned deployment for nothing.
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat)
except Exception:                                                  # noqa: E402
    Ed25519PrivateKey = None


def _load_signing_key():
    """(Ed25519PrivateKey | None, raw public key bytes | None, kid | None).
    NEVER generates a key implicitly — an operator who wants signing must
    mint one themselves (serving/README.md) and point the env var at it."""
    path = os.environ.get("X402_TRUST_SIGNING_KEY")
    if not path:
        return None, None, None
    if Ed25519PrivateKey is None:
        print("[trust-api] WARNING: X402_TRUST_SIGNING_KEY is set but the "
              "`cryptography` package is unavailable — envelope signing "
              "DISABLED; /v1/trust/evaluate responses will omit `envelope`",
              file=sys.stderr, flush=True)
        return None, None, None
    try:
        raw = Path(path).read_bytes()
        if len(raw) != 32:
            raise ValueError(f"expected 32 raw bytes, got {len(raw)}")
        priv = Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as e:
        print(f"[trust-api] WARNING: cannot load X402_TRUST_SIGNING_KEY "
              f"{path!r} ({type(e).__name__}: {e}) — envelope signing "
              f"DISABLED; /v1/trust/evaluate responses will omit `envelope`",
              file=sys.stderr, flush=True)
        return None, None, None
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    # kid: first 12 hex chars of sha256(raw public key) — short, stable,
    # collision-safe enough for the one-or-two keys a single deployment
    # rotates through, and cheap to eyeball-match against jwks.json.
    kid = hashlib.sha256(pub_raw).hexdigest()[:12]
    return priv, pub_raw, kid


_SIGNING_KEY, _SIGNING_PUB, _SIGNING_KID = _load_signing_key()


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwks() -> dict:
    """GET /.well-known/jwks.json body. Empty `keys` (never an error) when
    signing is disabled — the same "absent, not broken" posture the rest of
    this module uses for missing evidence."""
    if _SIGNING_PUB is None:
        return {"keys": []}
    return {"keys": [{
        "kty": "OKP", "crv": "Ed25519", "kid": _SIGNING_KID,
        "x": _b64url_nopad(_SIGNING_PUB), "use": "sig",
    }]}


def _build_envelope(payload: dict) -> dict | None:
    """The signed-card `envelope` block for one /v1/trust/evaluate response
    body, or None when signing is disabled (caller must then omit the key
    entirely — never emit a null/empty envelope).

    Verification recipe (what a verifier does with `payload` + this
    envelope + the public key from `public_key_url`):
      1. Take the FULL response body and delete the top-level `envelope`
         key — the signature covers the card WITHOUT its own envelope,
         since the envelope (in particular `signature` itself) cannot sign
         over its own bytes.
      2. Re-canonicalise the remaining object with the SAME algorithm named
         in `envelope.canonicalisation`
         (urn:x402:canonicalisation:jcs-rfc8785-v1 -> serving/jcs.py here).
      3. Recompute sha256 of those canonical bytes and confirm it equals
         `card_ref` with the "sha256:" prefix stripped — this also proves
         `card_ref` itself wasn't tampered with independently of the
         signature.
      4. Base64url-decode `signature` (no padding) and Ed25519-verify it
         against the SAME canonical bytes from step 2, using the public key
         named by `kid` in the JWKS document at `public_key_url`.
    Steps 3 and 4 both operate on the canonical bytes computed in step 2 —
    a verifier that only checks one of them accepts a tampered card_ref
    with a still-valid-looking signature over different bytes, or vice
    versa; conformance/check.py's pure-Python verifier performs both.
    """
    if _SIGNING_KEY is None:
        return None
    canonical = jcs.canonicalize_bytes(payload)
    card_ref = "sha256:" + hashlib.sha256(canonical).hexdigest()
    signature = _SIGNING_KEY.sign(canonical)
    return {
        "canonicalisation": "urn:x402:canonicalisation:jcs-rfc8785-v1",
        "alg": "Ed25519",
        "kid": _SIGNING_KID,
        "card_ref": card_ref,
        "signature": _b64url_nopad(signature),
        "public_key_url": PUBLIC_BASE + "/.well-known/jwks.json",
    }


PAY_DESC = "merona m.Score lookup — wash-aware x402 trust data"
# per-seller delivery evidence keeps its own env var so it stays
# independently tunable from the score-lookup price.
DELIVERY_PRICE_USD = float(os.environ.get("X402_PRICE_DELIVERY_USD", "0.001"))
# Free-for-now (2026-08-15): every payable route — REST /v1/ lanes and the
# trust_score MCP tool — serves anonymous callers without a key or payment.
# The x402 lane stays live as an OPTIONAL receipts path: attach X-PAYMENT
# (or payment= on the tool) and verify+settle+receipt runs exactly as
# before. Flip X402_FREE_MODE=0 to restore the hard paywall.
FREE_MODE = os.environ.get("X402_FREE_MODE", "1") == "1"


def _free_pricing_note() -> str:
    if x402pay.enabled():
        return ("free — no key, no signup. Optional: re-send with an x402 "
                f"payment (${x402pay.PRICE_USD:g}) for a settlement-"
                "receipted response.")
    return "free — no key, no signup."
DELIVERY_PAY_DESC = ("merona delivery evidence — genuine-payer probe "
                     "history with settlement txs")
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

# paid delivery receipts (attest/paysweep.py) — the ground-truth ledger of
# merona actually buying from sellers; mtime-cached like the feed
RECEIPTS_PATH = Path(os.environ.get(
    "X402_RECEIPTS_PATH",
    str(ROOT / "data" / "indexer" / "payprobe" / "receipts.jsonl")))
_delivery_cache = {"mtime": None, "states": {}, "history": {}}
_DELIVERY_HISTORY_CAP = 200
_RECEIPT_CHAINS = {"base": "base", "eip155:8453": "base",
                   "polygon": "polygon", "eip155:137": "polygon",
                   "solana": "solana",
                   # CAIP-2 mainnet form (genesis hash); keys here must
                   # already be lowercase — the .lower() below is applied to
                   # the receipt's `network` field before this lookup.
                   "solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp": "solana"}

# attest/payprobe.py (BASE_NETWORKS) can currently only PAY on Base: plain
# EIP-3009 USDC transferWithAuthorization, no polygon or solana signing path
# exists yet. So even though _RECEIPT_CHAINS/_EVAL_SUPPORTED_CHAINS accept
# receipts and trust-evaluate requests for polygon/solana (trust SCORES do
# cover all three chains), delivery EVIDENCE — a receipt from merona actually
# paying a seller — can only ever exist for base today. delivery_lookup uses
# this to tell "no evidence because we haven't probed this chain" apart from
# "no evidence because this seller wasn't in the discovery set" (2026-08-12
# audit, see SECURITY.md).
PROBE_COVERAGE_CHAINS = ("base",)
_PROBE_COVERAGE_REASON = (
    "merona's paid delivery probes currently run on base only — absence of "
    "evidence on this chain is a coverage statement, not a seller signal")


def _norm_payto(addr: str) -> str:
    """The one normalization rule for every key in the delivery path: EVM
    (0x-prefixed) addresses are lowercased for case-insensitive comparison;
    base58 (Solana) addresses are case-sensitive and must be preserved
    exactly. Mirrors _adverse_findings' rule (fixed 2026-08-12) — this file
    previously lowercased every pay_to/lookup key unconditionally, which is
    harmless for EVM hex but silently case-folds a base58 wallet into one
    that never matches the real address once solana receipts exist."""
    return addr.lower() if _HEX_ADDR.match(addr) else addr


def _delivery_states(lines) -> dict:
    """{(chain, payto): delivery state} — the LATEST settled receipt wins per
    seller, so a seller who fixes their endpoint clears a charged_unserved
    flag on the next paid probe. Only settled outcomes carry delivery
    information: `delivered` proves the seller serves paying customers;
    `settled_no_content` proves a charge with nothing served (settlement tx
    recorded, AND the facilitator's own `success` field confirmed the
    charge — see payprobe._classify_outcome). Rejected/unreachable rows say
    nothing about delivery.

    `settle_failed` (facilitator reported the settlement FAILED) and
    `settled_unconfirmed` (older facilitator, no `success` field at all —
    ambiguous) are deliberately excluded here, not just by omission from
    the tuple below: neither is proof the seller was paid, so neither may
    ever produce a `charged_unserved` public accusation, and neither counts
    as delivery either. A receipt with one of these outcomes carries no
    delivery verdict at all (falls through to whatever the next-most-recent
    settled receipt says, same as `rejected`)."""
    rows = []
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            continue
        chain = _RECEIPT_CHAINS.get(str(r.get("network", "")).lower())
        if not chain or not r.get("pay_to"):
            continue
        # explicit allowlist, not a denylist of the bad outcomes above — a
        # new outcome string added later defaults to "no delivery verdict"
        # rather than silently becoming adverse evidence.
        if r.get("outcome") not in ("delivered", "settled_no_content"):
            continue
        rows.append((str(r.get("ts") or ""), chain, r))
    out: dict = {}
    for ts, chain, r in sorted(rows):
        key = (chain, _norm_payto(r["pay_to"]))
        if r["outcome"] == "delivered":
            out[key] = {
                "status": "delivery_verified", "as_of": ts[:10],
                "probe_url": r.get("url"),
                "note": "merona paid this seller's listed endpoint with a "
                        "real x402 settlement and received well-formed "
                        "content on the dated probe."}
        else:
            out[key] = {
                "status": "charged_unserved", "as_of": ts[:10],
                "probe_url": r.get("url"),
                "http_status": r.get("http_status"),
                "settlement_tx": (r.get("settlement") or {}).get("transaction"),
                "note": "on the dated probe the payment settled on-chain but "
                        "the request was then refused (HTTP status above). "
                        "A recorded fact about one request, not an intent "
                        "claim; a later delivering probe replaces this."}
    return out


def _delivery_history_states(lines) -> dict:
    """{(chain, payto): [probe dicts, newest-first, capped at
    _DELIVERY_HISTORY_CAP]} — every parsed receipt row (any outcome, not
    just the settled ones _delivery_states collapses to a latest state),
    built from the same lines _delivery_states parses so the two together
    still cost exactly one read of receipts.jsonl per mtime change (see
    _delivery_map)."""
    rows = []
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            continue
        chain = _RECEIPT_CHAINS.get(str(r.get("network", "")).lower())
        if not chain or not r.get("pay_to"):
            continue
        rows.append((str(r.get("ts") or ""), chain, r))
    rows.sort(key=lambda row: row[0])
    out: dict = {}
    for ts, chain, r in reversed(rows):
        key = (chain, _norm_payto(r["pay_to"]))
        bucket = out.setdefault(key, [])
        if len(bucket) >= _DELIVERY_HISTORY_CAP:
            continue
        amount = r.get("amount_base_units")
        bucket.append({
            "ts": ts,
            "outcome": r.get("outcome"),
            "http_status": r.get("http_status"),
            "url": r.get("url"),
            "amount_usdc": (round(amount / 1e6, 6)
                            if isinstance(amount, (int, float)) else None),
            "settlement_tx": (r.get("settlement") or {}).get("transaction"),
        })
    return out


def _delivery_map() -> dict:
    """Refresh _delivery_cache from RECEIPTS_PATH iff its mtime changed, and
    return the full {(chain, payto): state} map. The single reader of
    receipts.jsonl — _delivery() (one seller), pro_delivery() (bulk, a whole
    chain), and _delivery_history_map() (per-seller probe history) all call
    through here (directly or via _delivery_history_map) rather than
    re-reading the file. Each mtime-triggered refresh reads the file once and
    derives both the states map and the history map from those same lines."""
    try:
        m = RECEIPTS_PATH.stat().st_mtime
    except OSError:
        return {}
    if _delivery_cache["mtime"] != m:
        try:
            lines = RECEIPTS_PATH.read_text().splitlines()
            _delivery_cache["states"] = _delivery_states(lines)
            _delivery_cache["history"] = _delivery_history_states(lines)
            _delivery_cache["mtime"] = m
        except Exception:
            pass
    return _delivery_cache["states"]


def _delivery_history_map() -> dict:
    """{(chain, payto): [probe history]} — refreshed by the same mtime-cache
    pass as _delivery_map(); calling it here ensures a cold/stale cache gets
    (re)built before we read the history half of it."""
    _delivery_map()
    return _delivery_cache["history"]


def _delivery(chain: str, address: str) -> dict | None:
    return _delivery_map().get((chain, _norm_payto(address)))


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


# --- discovery basis (x402-foundation/x402#2300 commitment #1) -----------------
# A paid sweep's denominator is its DISCOVERY SET, not "the market": another
# operator in #2300 was absent from the Bazaar snapshot that fed our
# 502-seller paid sweep, so he was never evaluated at all — and nothing in a
# plain 404/absence told him that was the reason, rather than a refusal. This
# block lets a consumer check whether a subject was even discoverable at
# evaluation time, the same "recomputable, not just asserted" posture
# VERIFICATION already gives scores: the catalog is content-addressed
# (sha256), so anyone holding the same snapshot file can confirm it
# themselves rather than trust our say-so.
#
# Loaded ONCE at import (like _SIGNING_KEY above) — the catalog file doesn't
# change mid-process, so there is nothing to gain by re-hashing several MB on
# every request. `_load_discovery_basis` takes an explicit path (rather than
# reading the env var itself) so it stays a pure, unit-testable function —
# env resolution happens once, right below, at the one real call site.
X402_CATALOG_FILE = os.environ.get(
    "X402_CATALOG_FILE", str(ROOT / "data" / "processed" / "bazaar_resources.csv"))


def _catalog_max_last_updated(path) -> str | None:
    """Max value of the `last_updated` column across the catalog CSV at
    `path`, or None if the column/file is missing or every value is empty.
    Streams the file row by row (csv.reader over an open file handle) rather
    than slurping ~32k rows into a list/DataFrame — this runs once at import
    against a file that can be several MB. `last_updated` is an ISO-ish
    (YYYY-MM-DD[...]) string, so plain lexicographic max is a valid max."""
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "last_updated" not in reader.fieldnames:
                return None
            newest = None
            for row in reader:
                v = row.get("last_updated")
                if v and (newest is None or v > newest):
                    newest = v
            return newest
    except OSError:
        return None


def _load_discovery_basis(path) -> dict | None:
    """{"catalog": "bazaar", "snapshot_sha256": sha256 hex of the file,
    "snapshot_date": UTC date (ISO) the snapshot FILE was last written
    (mtime) — i.e. when this box last ran the pull, NOT when the underlying
    catalog data itself is current, "catalog_max_last_updated": the newest
    `last_updated` value actually present in the catalog's rows (the DATA
    date)} for the catalog snapshot at `path`, or None when it's missing/
    unreadable — NEVER a fabricated digest for a catalog we can't actually
    read. Every consumer of this value (below) must omit the
    `discovery_basis` key entirely on None, the same "absent, not invented"
    rule _load_signing_key follows for envelope signing.

    snapshot_date and catalog_max_last_updated answer different questions
    and can legitimately diverge (a re-run that silently replayed a stale
    cache is exactly the case that produces a fresh snapshot_date over old
    data) — publishing BOTH, rather than just the mtime-derived one, is the
    fix for that failure mode: the divergence itself is the signal a caller
    needs, instead of a single date that looks authoritative either way."""
    try:
        p = Path(path)
        data = p.read_bytes()
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    return {
        "catalog": "bazaar",
        "snapshot_sha256": hashlib.sha256(data).hexdigest(),
        "snapshot_date": mtime.date().isoformat(),
        "catalog_max_last_updated": _catalog_max_last_updated(p),
    }


# Loaded/cached exactly once here, at import — the same posture the
# docstring above describes (no per-request re-scan of a multi-MB CSV).
DISCOVERY_BASIS = _load_discovery_basis(X402_CATALOG_FILE)


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
                    # normalization must MATCH the lookup side (trust_lookup
                    # et al.): EVM lowercased, base58 case PRESERVED — a
                    # lowercased base58 key matches nothing, which would
                    # silently swallow any Solana adverse finding
                    # (2026-08-12 audit)
                    a = f["address"]
                    out[(f["chain"],
                         a.lower() if _HEX_ADDR.match(a) else a)] = f
        except Exception:
            out = {}
        _adverse = out
    return _adverse


def _approve_mod():
    """Lazy, once-only import of outreach/approve — isolated so a missing
    outreach module never affects the trust API's core routes, and the path
    insert happens exactly once (not per request)."""
    p = str(ROOT / "outreach")
    if p not in sys.path:
        sys.path.insert(0, p)
    import approve
    return approve


def _outreach_review(token: str) -> tuple[int, str]:
    """GET /outreach/review — the approval page (read-only; POST approves)."""
    try:
        return _approve_mod().review_html(token)
    except Exception as e:
        print(f"[trust-api] outreach review error: {e}", file=sys.stderr)
        return 500, "<!doctype html>approval temporarily unavailable"


def _outreach_worklist(token: str) -> tuple[int, str]:
    """GET /outreach/worklist — read-only view of the standing to-do list.
    Bookmarkable; token-gated; never sends."""
    try:
        return _approve_mod().worklist_html(token)
    except Exception as e:
        print(f"[trust-api] outreach worklist error: {e}", file=sys.stderr)
        return 500, "<!doctype html>worklist temporarily unavailable"


def _outreach_approve(token: str) -> tuple[bool, str]:
    """POST /outreach/approve — record approval only; no send here."""
    try:
        approve = _approve_mod()
        if approve.mark_approved(token):
            return True, approve.approved_page()
        return False, "<!doctype html>link expired or already used"
    except Exception as e:
        print(f"[trust-api] outreach approve error: {e}", file=sys.stderr)
        return False, "<!doctype html>approval temporarily unavailable"


ACTION_RULE_VERSION = 2


def _suggested_action(resp: dict) -> dict:
    """Map the evidence already in `resp` to an action, deterministically.

    This is advice to the caller about the caller's own funds — never a claim
    about the seller. That distinction is what keeps it inside the published
    editorial policy ("we do not present flags as accusations"), and it is why
    the strongest negative value is DECLINE rather than a verdict of guilt.

    The rule version and every reason that fired travel with the answer, so a
    caller can re-derive the decision from the same response, disagree with a
    threshold, or ignore this field entirely and use the grade directly.

    INSUFFICIENT_EVIDENCE is a first-class answer: an address we have not
    observed long enough is not an address we are calling bad. Collapsing
    "no data" into "unsafe" is the standard failure of this genre and it
    punishes every honest newcomer.
    """
    grade = str(resp.get("grade") or "").upper()
    prov = bool(resp.get("provisional"))
    wash = (resp.get("components") or {}).get("wash") or {}
    flags = [k for k in ("cycle_flag", "history_flag", "nightly_flag")
             if wash.get(k)]
    adverse = bool(resp.get("adverse_findings"))
    unscored = resp.get("score") is None and not adverse
    # scores v4: a clean seller below the letter gate has a score but no
    # letter — unrated, which is INSUFFICIENT_EVIDENCE with a truthful reason
    unrated = (not unscored and not adverse
               and grade in ("", "UNKNOWN", "NONE"))
    # rule v2: paid-probe delivery evidence (the `delivery` field of this
    # same response). charged_unserved is settled-payment-grade evidence, so
    # it floors the action at INVESTIGATE even for an unscored address —
    # unlike absence of history, this is something that happened.
    dlv = resp.get("delivery") or {}
    charged = dlv.get("status") == "charged_unserved"
    reasons = []
    if adverse:
        reasons.append("hand-verified adverse finding on record")
    if flags:
        reasons.append("wash signals: " + ", ".join(flags))
    if charged:
        reasons.append("paid probe: payment settled on-chain but the request "
                       "was refused (settlement tx in `delivery`)")
    elif dlv.get("status") == "delivery_verified":
        reasons.append(f"paid probe delivered content "
                       f"({dlv.get('as_of', 'dated')})")
    if unrated:
        reasons.append("unrated — evidence base below the letter-publication "
                       "gate; the score is computed but no letter is claimed")
    elif unscored or grade in ("", "UNKNOWN"):
        reasons.append("no score for this address in the witnessed index")
    else:
        reasons.append(f"grade {grade}")
    if prov:
        reasons.append("provisional — observation window still short")

    if adverse:
        action = "DECLINE"
    elif unscored or grade in ("", "UNKNOWN"):
        action = "INSUFFICIENT_EVIDENCE"
    elif grade in ("D", "F") and not prov:
        action = "DECLINE"
    elif flags or grade in ("D", "F"):
        # a provisional D/F is too thin to decline on but too poor to wave
        # through as ordinary caution — send the caller to look at the evidence
        action = "INVESTIGATE"
    elif prov or grade == "C":
        action = "CAUTION"
    else:
        action = "PROCEED"
    if charged and action in ("PROCEED", "CAUTION", "INSUFFICIENT_EVIDENCE"):
        action = "INVESTIGATE"
    return {"action": action,
            "rule_version": ACTION_RULE_VERSION,
            "because": reasons,
            "note": "Suggested action for the caller's own funds, derived by "
                    "a published deterministic rule from the evidence in this "
                    "response. Not a claim about the seller."}


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
        dstate = _delivery(chain, address)
        if finding:
            out = {"chain": chain, "seller": address,
                   "score": None, "grade": finding.get("grade_override", "F"),
                   "status": "adverse_finding", "measured_date": None,
                   "adverse_findings": [finding]}
            if dstate:
                out["delivery"] = dstate
            out["suggested_action"] = _suggested_action(out)
            return 200, out
        # The 404 carries an action too: an agent asking "should I pay this?"
        # needs "we have no record" to be distinguishable from "we have bad
        # news", and it is the same field either way. Paid-probe evidence
        # rides along even here — a charged_unserved receipt is something we
        # KNOW about an address the index hasn't scored yet.
        body = {"error": "unknown seller",
                "detail": "no settlements observed for this address on "
                          "this chain in the witnessed index"}
        if dstate:
            body["delivery"] = dstate
        body["suggested_action"] = _suggested_action(body)
        return 404, body
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
    # evidence tier, mirroring endpoint grades: no letter = unrated (scores
    # v4 letter gate), else provisional/verified by observation maturity
    if resp.get("grade") is None and not finding:
        resp["tier"] = "unrated"
        resp["note"] = ("score computed but no letter published: evidence "
                        "base is below the letter gate (settlements or "
                        "distinct payers). Not an adverse signal.")
    else:
        resp["tier"] = "provisional" if resp.get("provisional") else "verified"
    dstate = _delivery(chain, address)
    if dstate:
        resp["delivery"] = dstate
    resp["suggested_action"] = _suggested_action(resp)
    return 200, resp


# --- POST /v1/trust/evaluate ----------------------------------------------------
# x402 "trust provider" gate decision: x402-trust-query-v0.1 in,
# x402-trust-evaluation-v0.1 out (x402-foundation/x402 #2299), plus a
# `direction` extension (seller/buyer). This maps merona's EXISTING data
# (trust_lookup — the same function GET /v1/trust/{chain}/{address} calls —
# and its _delivery/_suggested_action machinery) to PASS/FAIL/UNCERTAIN. It
# never computes a new score and never invents one: absent evidence is
# UNCERTAIN with score null, never a guess.
TRUST_EVALUATE_MAX_BODY = 16384
_EVAL_SUPPORTED_CHAINS = {"base", "polygon", "solana"}
_EVAL_CHAIN_ALIASES = {"eip155:8453": "base", "eip155:137": "polygon"}
# letter -> score used ONLY when no numeric score exists at all (e.g. a bare
# adverse-finding override with score=None); otherwise the real number wins.
_EVAL_GRADE_SCORE = {"A": 0.9, "B": 0.75, "C": 0.55, "D": 0.4, "F": 0.15}
EVAL_DEFAULT_TTL = 21600     # 6h: seller_scores/delivery refresh roughly nightly
EVAL_CHARGED_TTL = 3600      # short: the very next paid probe can clear this
EVAL_DISCLAIMER = (
    "This decision reflects recorded facts with dates, re-derivable from the "
    "anchored snapshot referenced in evidence_uri; it is not an accusation "
    "or an endorsement of either party.")

# --- basis[] evidence typing (twzrd draft-02 of x402#2299) ---------------------
# draft-02 replaced a bare list of strings with typed entries carrying a
# required evidence CLASS, plus a "block-provenance" rule: a FAIL decision
# must rest on curated or behavioral evidence (never attested/community
# alone — those two are either unverified-by-us or informational-only in
# this implementation). Every basis[] entry built below has this shape.
_EVAL_EVIDENCE_CLASSES = ("curated", "behavioral", "community", "attested")
# One-line meaning of each class, reused by both the wire helper below and
# the provider descriptor / spec dicts near the bottom of this section — kept
# in one place so the docs can't drift from what the code actually emits.
_EVAL_EVIDENCE_CLASS_NOTES = {
    "curated": "operator-verified fact — merona itself paid the seller's "
               "endpoint and recorded the outcome (settlement tx + HTTP "
               "status). Permitted to support a FAIL.",
    "behavioral": "derived from merona's own seller_scores history (the "
                  "nightly wash-aware grade). Permitted to support a FAIL.",
    "community": "third-party reported evidence — reserved for draft-02 "
                 "compatibility; merona does not currently emit this class.",
    "attested": "on-chain anchored via EAS on Base; evidence_uri's sha256: "
               "content-addresses the exact nightly snapshot, so the same "
               "evidence is independently re-derivable, not merely "
               "asserted. Never emitted alone as a FAIL's only support.",
}


def _eval_basis(evidence_class: str, kind: str, ref=None, observed_at=None) -> dict:
    """One typed basis[] entry. `evidence_class` is asserted against the
    fixed draft-02 vocabulary rather than trusted blindly — a typo here
    would silently violate the block-provenance rule downstream."""
    assert evidence_class in _EVAL_EVIDENCE_CLASSES, evidence_class
    return {"class": evidence_class, "kind": kind, "ref": ref,
            "observed_at": observed_at}


def _eval_attested_entry(ev: dict, evidence_uri) -> dict | None:
    """attested `eas_anchor` entry, appended only when _eval_base_evidence
    found an on-chain anchor for this snapshot (ev["anchors"] non-empty).
    WHY a separate class at all: the EAS attestation on Base makes the same
    snapshot independently re-derivable — attested AND recomputable, which
    is merona's differentiator over a provider that only asserts facts."""
    if not ev.get("anchors"):
        return None
    return _eval_basis("attested", "eas_anchor", evidence_uri,
                       ev.get("snapshot_date"))


def _eval_curated_probe_entry(delivery: dict, record: dict,
                              chain: str | None) -> dict:
    """curated `first_party_probe` entry shared by the charged_unserved and
    delivery_verified paths: merona itself paid and watched the endpoint, so
    this is an operator-verified fact (draft-02 "curated"), not a derived
    score — the class the block-provenance rule permits a FAIL to rest on.
    `chain` is threaded in from the caller's already-validated query chain
    rather than trusted from `record` alone: trust_lookup's 404 "unknown
    seller" shape (see _evaluate_seller_record's docstring) can carry a
    `delivery` block with no top-level `chain` field at all.

    Carries OPTIONAL `discovery_basis` (#2300 commitment #1) when the module
    catalog snapshot loaded — omitted entirely on None, never null. This is
    the ONE basis entry that gets it: a first-party PAID PROBE is exactly the
    evidence whose absence is ambiguous without a discovery-set denominator
    (not_evaluated vs probed-but-inconclusive); behavioral/attested entries
    derive from merona's own scoring pipeline, not the paid sweep, so a
    discovery basis says nothing about them."""
    settlement_tx = delivery.get("settlement_tx")
    probe_chain = chain or record.get("chain") or "unknown"
    ref = f"{probe_chain}:tx:{settlement_tx}" if settlement_tx else None
    # trust_lookup's delivery dicts key the probe date as "as_of" (see
    # _delivery_states/_delivery_history_states above) — not checked_at/date.
    entry = _eval_basis("curated", "first_party_probe", ref,
                        delivery.get("as_of"))
    if DISCOVERY_BASIS is not None:
        entry["discovery_basis"] = DISCOVERY_BASIS
    return entry


# score:null must always carry a reason (draft-02: never fabricate a
# number). Most null-score paths map 1:1 onto a reason_code; anything else
# that reaches _eval_result with score=None collapses to the generic
# "insufficient_signal" — still honest, just less specific.
_EVAL_NULL_REASON = {
    "unknown_subject": "unknown_subject",
    "unsupported_chain": "unsupported_chain",
    "insufficient_evidence": "insufficient_signal",
    "payer_scoring_not_available": "insufficient_signal",
}

# Every reason_code this module actually emits (grep-checked against the
# _eval_result() call sites above), one-line meaning each — the source for
# both TRUST_API_SPEC's reason_codes and anyone reading this file top to
# bottom trying to enumerate the decision tree without re-deriving it.
_EVAL_REASON_CODE_NOTES = {
    "charged_unserved": "a paid probe settled on-chain but the request was "
                        "then refused — FAIL",
    "grade_decline": "suggested_action is DECLINE, or a bare F grade with "
                     "no DECLINE reached — FAIL",
    "delivery_verified": "a paid probe both settled and returned real "
                         "content, and grade is A/B — PASS",
    "grade_pass": "grade A/B with no delivery probe on file — PASS",
    "grade_marginal": "grade C/D — UNCERTAIN",
    "insufficient_evidence": "no grade and no delivery record for this "
                             "wallet — UNCERTAIN, score null",
    "unknown_subject": "no wallet given, or it doesn't parse as an address "
                       "on the resolved chain — UNCERTAIN, score null",
    "unsupported_chain": "chain isn't base/polygon/solana (or its eip155 "
                         "alias) — UNCERTAIN, score null",
    "payer_scoring_not_available": "direction=buyer — merona doesn't score "
                                   "payers yet — UNCERTAIN, score null",
}


def _eval_score(raw_score, grade) -> float | None:
    """Normalize whatever numeric score trust_lookup returned to 0-1 (the
    stored score is 0-100). Falls back to a fixed letter->score map ONLY when
    no numeric score exists; returns None (never a guess) when neither does."""
    if raw_score is not None:
        try:
            return round(max(0.0, min(1.0, float(raw_score) / 100.0)), 4)
        except (TypeError, ValueError):
            pass
    return _EVAL_GRADE_SCORE.get(str(grade or "").upper())


def _eval_chain(raw) -> str | None:
    if not raw:
        return None
    c = str(raw).strip().lower()
    return _EVAL_CHAIN_ALIASES.get(c, c)


def _eval_result(direction: str, decision: str, score, reason_code: str,
                 basis: list, evidence_uri, evidence: dict, ttl: int) -> dict:
    if decision == "FAIL":
        # Structural guard for draft-02's block-provenance rule: a FAIL must
        # rest on curated or behavioral evidence. Every call site above this
        # function already satisfies it, so tripping this means a future
        # FAIL path was added without matching evidence — a bug in this
        # module, not bad caller input (hence assert, not a 4xx).
        assert any(b.get("class") in ("curated", "behavioral")
                  for b in basis), f"FAIL with no curated/behavioral basis: {basis!r}"
    # One `now` for both timestamps so expires_at - issued_at == ttl exactly,
    # rather than drifting by however long the two calls straddle.
    now = datetime.now(timezone.utc)
    resp = {
        "schema": "x402-trust-evaluation-v0.1",
        "decision": decision,
        "score": score,
    }
    if score is None:
        # Present iff score is null (never fabricate a number, but also
        # never leave a null score unexplained) — absent entirely otherwise.
        resp["null_reason"] = _EVAL_NULL_REASON.get(reason_code,
                                                     "insufficient_signal")
    resp.update({
        "reason_code": reason_code,
        "direction": direction,
        "basis": basis,
        "evidence_uri": evidence_uri,
        "evidence": evidence,
        "ttl": ttl,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        "provider": "merona",
        "disclaimer": EVAL_DISCLAIMER,
    })
    return resp


def _eval_base_evidence(record: dict) -> tuple[dict, str | None]:
    """Small evidence object + evidence_uri from trust_lookup's own
    `verification` block. Never invents a hash: no on-host snapshot file (or
    no verification block at all, e.g. an unscored wallet) -> no evidence_uri."""
    verification = record.get("verification") or {}
    sha = verification.get("snapshot_sha256")
    ev = {}
    if verification.get("snapshot_date"):
        ev["snapshot_date"] = verification["snapshot_date"]
    if verification.get("anchors"):
        ev["anchors"] = verification["anchors"]
    return ev, (f"sha256:{sha}" if sha else None)


def _eval_charged_unserved(delivery: dict, record: dict,
                           chain: str | None) -> dict:
    ev, uri = _eval_base_evidence(record)
    if delivery.get("settlement_tx"):
        ev["settlement_tx"] = delivery["settlement_tx"]
    if delivery.get("http_status") is not None:
        ev["http_status"] = delivery["http_status"]
    grade = record.get("grade")
    score = _eval_score(record.get("score"), grade) if grade else None
    # curated: a first-party paid-probe receipt is an operator-verified fact
    # — the class draft-02's block-provenance rule permits this FAIL to
    # rest on. `attested` rides along too when the same snapshot is anchored.
    basis = [_eval_curated_probe_entry(delivery, record, chain)]
    attested = _eval_attested_entry(ev, uri)
    if attested:
        basis.append(attested)
    return _eval_result("seller", "FAIL", score, "charged_unserved",
                        basis, uri, ev, EVAL_CHARGED_TTL)


def _evaluate_seller_record(status: int, record: dict,
                            chain: str | None = None) -> dict:
    """Pure: maps an already-fetched trust_lookup() result (its 200 body, or
    its 404 error body — both may carry `delivery`) to the
    x402-trust-evaluation-v0.1 wire shape. No I/O of its own — the DB call
    already happened in trust_lookup() — so this is directly unit-testable
    with hand-built records, matching the rule order in the spec:
      1. charged_unserved (checked first, regardless of grade/status —
         receipt-proven and the most perishable signal)
      2. suggested_action DECLINE
      3. delivery_verified + grade A/B
      4. grade alone (A/B pass, C/D marginal, bare F -> decline)
      5. no grade at all -> insufficient_evidence

    `chain` is the caller's already-validated query chain (threaded down
    from _trust_evaluate); it feeds the curated basis entry's chain-prefixed
    tx ref and is preferred over record.get("chain") because the 404
    "unknown seller" shape trust_lookup returns has no top-level `chain`
    field at all (see the test fixture below that exercises exactly this).
    """
    record = record or {}
    delivery = record.get("delivery") or {}

    if delivery.get("status") == "charged_unserved":
        return _eval_charged_unserved(delivery, record, chain)

    if status != 200:
        # unknown wallet (404, no charged_unserved receipt) or a backend
        # error shape reaching here some other way: nothing to score.
        return _eval_result("seller", "UNCERTAIN", None,
                            "insufficient_evidence", [], None, {},
                            EVAL_DEFAULT_TTL)

    grade = str(record.get("grade") or "").upper() or None
    suggested = record.get("suggested_action") or {}
    ev, uri = _eval_base_evidence(record)
    attested = _eval_attested_entry(ev, uri)

    def _behavioral_basis() -> list:
        # behavioral: derived from merona's own seller_scores history —
        # also permitted to support a FAIL. attested rides along whenever
        # the same snapshot has an on-chain anchor.
        basis = [_eval_basis("behavioral", "seller_score", uri,
                             ev.get("snapshot_date"))]
        if attested:
            basis.append(attested)
        return basis

    if suggested.get("action") == "DECLINE":
        score = _eval_score(record.get("score"), grade)
        return _eval_result("seller", "FAIL", score, "grade_decline",
                            _behavioral_basis(), uri, ev, EVAL_DEFAULT_TTL)

    if delivery.get("status") == "delivery_verified" and grade in ("A", "B"):
        score = _eval_score(record.get("score"), grade)
        basis = [_eval_curated_probe_entry(delivery, record, chain)] \
            + _behavioral_basis()
        return _eval_result("seller", "PASS", score, "delivery_verified",
                            basis, uri, ev, EVAL_DEFAULT_TTL)

    if grade:
        score = _eval_score(record.get("score"), grade)
        if grade in ("A", "B"):
            return _eval_result("seller", "PASS", score, "grade_pass",
                                _behavioral_basis(), uri, ev, EVAL_DEFAULT_TTL)
        if grade in ("C", "D"):
            return _eval_result("seller", "UNCERTAIN", score, "grade_marginal",
                                _behavioral_basis(), uri, ev, EVAL_DEFAULT_TTL)
        # bare F with no DECLINE action (e.g. still provisional) — still FAIL
        return _eval_result("seller", "FAIL", score, "grade_decline",
                            _behavioral_basis(), uri, ev, EVAL_DEFAULT_TTL)

    # no grade, no delivery record: unknown/unrated wallet
    return _eval_result("seller", "UNCERTAIN", None, "insufficient_evidence",
                        [], None, {}, EVAL_DEFAULT_TTL)


def _trust_evaluate(body: bytes, lookup=None) -> tuple[int, dict]:
    """POST /v1/trust/evaluate wire entry point: computes the decision body
    (_trust_evaluate_body, unchanged logic) and, on a 200, attaches the
    signed `envelope` block when signing is enabled (_build_envelope
    returns None when it's not — see its docstring for the verification
    recipe). Split out so every 200 return path gets the envelope exactly
    once, in one place, instead of each branch below remembering to add it.
    """
    code, obj = _trust_evaluate_body(body, lookup)
    if code == 200:
        envelope = _build_envelope(obj)
        if envelope is not None:
            obj["envelope"] = envelope
    return code, obj


def _trust_evaluate_body(body: bytes, lookup=None) -> tuple[int, dict]:
    """POST /v1/trust/evaluate. Free (no x402 payment lane — that only
    exists in do_GET's payable-prefix branch, which this never touches) and
    HTTP 200 for every well-formed query; the decision lives in the body.

    `lookup` defaults to the module's own trust_lookup() — the exact function
    the existing GET route calls — but is overridable so the mapping logic
    stays unit-testable without a running server or a live DB.
    """
    lookup = lookup or trust_lookup
    try:
        req = json.loads(body.decode("utf-8")) if body else {}
        if not isinstance(req, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return 400, {"error": "malformed JSON body"}

    direction = "buyer" if str(req.get("direction") or "").strip().lower() \
        == "buyer" else "seller"
    if direction == "buyer":
        # Honest: merona doesn't score payers yet (agent_lookup is a
        # different, richer thing than a pass/fail gate — see its docstring).
        return 200, _eval_result("buyer", "UNCERTAIN", None,
                                 "payer_scoring_not_available", [], None, {},
                                 EVAL_DEFAULT_TTL)

    subject = req.get("subject") if isinstance(req.get("subject"), dict) else {}
    resource = req.get("resource") if isinstance(req.get("resource"), dict) else {}
    amount = (resource.get("amount")
              if isinstance(resource.get("amount"), dict) else {})

    wallet = subject.get("wallet")
    wallet = str(wallet).strip() if wallet else ""
    if not wallet:
        return 200, _eval_result("seller", "UNCERTAIN", None,
                                 "unknown_subject", [], None, {},
                                 EVAL_DEFAULT_TTL)

    chain = _eval_chain(subject.get("chain") or amount.get("chain"))
    if chain not in _EVAL_SUPPORTED_CHAINS:
        return 200, _eval_result("seller", "UNCERTAIN", None,
                                 "unsupported_chain", [], None, {},
                                 EVAL_DEFAULT_TTL)

    if _HEX_ADDR.match(wallet):
        addr = wallet.lower()
    elif _B58_ADDR.match(wallet):
        addr = wallet
    else:
        # Not a recognizable address on this chain — liberal parsing: this is
        # "no subject we can resolve", not a 400.
        return 200, _eval_result("seller", "UNCERTAIN", None,
                                 "unknown_subject", [], None, {},
                                 EVAL_DEFAULT_TTL)

    status, record = lookup(chain, addr)
    return 200, _evaluate_seller_record(status, record, chain)


# --- GET /.well-known/x402-trust-provider ---------------------------------------
# Machine-readable provider descriptor for the x402 trust-provider extension
# (x402#2299, twzrd draft-02) — lets an agent discover the endpoint, evidence
# vocabulary, and decision set without reading prose docs. A plain
# module-level dict, same as GLAMA_WELL_KNOWN/MCP_MANIFEST below: PUBLIC_BASE
# is itself resolved once at import time from an env var, so there is nothing
# to recompute per request.
TRUST_PROVIDER_DESCRIPTOR = {
    "provider": "merona",
    "description": "wash-aware x402 trust provider — verifies what actually "
                   "settles, from recomputable, on-chain anchored evidence.",
    "endpoint": {
        "method": "POST",
        "url": PUBLIC_BASE + "/v1/trust/evaluate",
        "request_schema": "x402-trust-query-v0.1",
        "response_schema": "x402-trust-evaluation-v0.1",
    },
    "directions": [
        {"direction": "seller",
         "note": "fully supported: PASS/FAIL/UNCERTAIN from paid-probe and "
                 "seller-score evidence"},
        {"direction": "buyer",
         "note": "always UNCERTAIN — payer scoring is not yet exposed as a "
                 "gate (reason_code payer_scoring_not_available)"},
    ],
    "evidence_classes": dict(_EVAL_EVIDENCE_CLASS_NOTES),
    "decisions": ["PASS", "FAIL", "UNCERTAIN"],
    "score": "0-1, or null with a null_reason — never a fabricated number",
    "freshness": "ttl (seconds), issued_at, and expires_at (= issued_at + "
                "ttl) are all ISO-8601 UTC; treat a stale expires_at as "
                "grounds to re-evaluate, not to trust the cached decision",
    "spec_url": PUBLIC_BASE + "/v1/spec",
    "docs": PUBLIC_BASE + "/#evaluate",
    # #2300 commitment #3: canonical crawler-UA registry — lets a seller who
    # sees merona-looking traffic hitting them look up what it actually is,
    # instead of guessing (CRAWLERS, near the other /.well-known/ dicts).
    "crawlers": PUBLIC_BASE + "/.well-known/merona-crawlers.json",
    "pricing": "free, rate-limited",
    "chains": ["base", "polygon", "solana"],
    # Signed-card envelope (twzrd draft-02 / x402#2299): the canonicalisation
    # PIN is fixed regardless of whether THIS deployment currently signs —
    # an unsigned deployment simply omits `envelope` from every response
    # body rather than advertising a pin it doesn't use.
    "envelope": {
        "canonicalisation": "urn:x402:canonicalisation:jcs-rfc8785-v1",
        "alg": "Ed25519",
        "jwks_url": PUBLIC_BASE + "/.well-known/jwks.json",
        "note": ("present on every /v1/trust/evaluate response body iff "
                 "this deployment has X402_TRUST_SIGNING_KEY configured; "
                 "an unsigned deployment omits the `envelope` field "
                 "entirely rather than sending an empty/null one — check "
                 "for the key's absence, not a falsy value"),
    },
}

# --- GET /v1/spec -----------------------------------------------------------------
# Hand-written field-level schema for both wire objects (not derived from the
# code, so it can't silently drift out of sync unnoticed — test_trust_evaluate
# pins the evidence-class list against this dict so at least that much stays
# honest). One-line description + type + required-ness + enum per field.
TRUST_API_SPEC = {
    "request": {
        "schema": {"type": "string", "required": True,
                  "enum": ["x402-trust-query-v0.1"],
                  "description": "wire schema identifier for the query"},
        "direction": {"type": "string", "required": False,
                     "enum": ["seller", "buyer"],
                     "description": "which side of the trade to evaluate; "
                                    "defaults to seller when absent"},
        "subject.wallet": {"type": "string", "required": True,
                           "description": "address being evaluated — hex "
                                          "(0x…) or base58, per chain"},
        "subject.chain": {"type": "string", "required": False,
                          "enum": ["base", "polygon", "solana"],
                          "description": "chain the wallet lives on; "
                                         "eip155:8453/eip155:137 CAIP-2 ids "
                                         "are also accepted and mapped to "
                                         "base/polygon; falls back to "
                                         "resource.amount.chain if absent"},
        "resource.url": {"type": "string", "required": False,
                         "description": "resource URL the caller is about "
                                        "to pay for (informational — chain "
                                        "+ wallet alone identify the "
                                        "subject)"},
        "resource.method": {"type": "string", "required": False,
                            "description": "HTTP method of the pending "
                                           "request (informational)"},
        "resource.amount.value": {"type": "string", "required": False,
                                  "description": "amount about to be paid "
                                                 "(informational)"},
        "resource.amount.currency": {"type": "string", "required": False,
                                     "description": "currency of the "
                                                    "pending amount "
                                                    "(informational)"},
        "resource.amount.chain": {"type": "string", "required": False,
                                  "description": "fallback source for "
                                                 "chain when subject.chain "
                                                 "is absent"},
        "requested_at": {"type": "string", "required": False,
                         "description": "ISO-8601 timestamp of the "
                                        "caller's request (informational, "
                                        "not validated)"},
    },
    "response": {
        "schema": {"type": "string", "required": True,
                  "enum": ["x402-trust-evaluation-v0.1"],
                  "description": "wire schema identifier for the result"},
        "decision": {"type": "string", "required": True,
                    "enum": ["PASS", "FAIL", "UNCERTAIN"],
                    "description": "the gate decision"},
        "score": {"type": "number|null", "required": True,
                 "description": "0-1 normalized trust score, or null when "
                                "there is not enough evidence to publish "
                                "one — never a fabricated number"},
        "null_reason": {"type": "string", "required": False,
                        "enum": ["unknown_subject", "unsupported_chain",
                                 "insufficient_signal"],
                        "description": "present iff score is null, "
                                       "explaining why; absent whenever "
                                       "score is a number"},
        "reason_code": {"type": "string", "required": True,
                        "enum": list(_EVAL_REASON_CODE_NOTES),
                        "description": "machine-readable rule that fired; "
                                       "see reason_codes below for meanings"},
        "direction": {"type": "string", "required": True,
                     "enum": ["seller", "buyer"],
                     "description": "echoes the request's direction"},
        "basis": {
            "type": "array of objects", "required": True,
            "description": "typed evidence entries supporting the "
                           "decision; empty when there is no evidence at "
                           "all (e.g. an unknown wallet)",
            "entry_fields": {
                "class": {"type": "string",
                         "enum": list(_EVAL_EVIDENCE_CLASSES),
                         "description": "evidence class — see `classes` "
                                        "below; a FAIL always has at least "
                                        "one entry with class curated or "
                                        "behavioral (block-provenance rule)"},
                "kind": {"type": "string",
                        "description": "what was observed, e.g. "
                                       "first_party_probe, seller_score, "
                                       "eas_anchor"},
                "ref": {"type": "string|null",
                       "description": "pointer to the evidence: a "
                                      "chain-prefixed tx ref "
                                      "(\"base:tx:0x…\") for a probe, or "
                                      "the same sha256: evidence_uri for "
                                      "score/anchor entries; null if none"},
                "observed_at": {"type": "string|null",
                                "description": "date the underlying fact "
                                               "was observed (probe date "
                                               "or snapshot date); null if "
                                               "unknown"},
                "discovery_basis": {"type": "object", "required": False,
                    "description": "present ONLY on the curated "
                                   "first_party_probe entry, and only when "
                                   "this deployment has a readable catalog "
                                   "snapshot configured (X402_CATALOG_FILE) "
                                   "— absent entirely otherwise, never "
                                   "null. {catalog, snapshot_sha256, "
                                   "snapshot_date, catalog_max_last_"
                                   "updated}: the content-addressed "
                                   "discovery-set snapshot the paid sweep "
                                   "drew its targets from, so a consumer "
                                   "can independently check whether a "
                                   "subject was even discoverable at "
                                   "evaluation time (x402#2300). "
                                   "snapshot_date is when this snapshot "
                                   "FILE was last written (mtime); "
                                   "catalog_max_last_updated is the newest "
                                   "`last_updated` value actually present "
                                   "in the catalog's rows (the DATA date) "
                                   "— the two can diverge (e.g. a re-run "
                                   "that replayed a stale cache), and that "
                                   "divergence is itself the signal"},
            },
            "classes": dict(_EVAL_EVIDENCE_CLASS_NOTES),
        },
        "evidence_uri": {"type": "string|null", "required": True,
                         "description": "sha256: content address of the "
                                        "nightly snapshot backing this "
                                        "decision; null when there is no "
                                        "on-host snapshot to point at"},
        "evidence": {"type": "object", "required": True,
                    "description": "small supporting details keyed by "
                                   "field name (settlement_tx, "
                                   "http_status, snapshot_date, anchors, "
                                   "…) — never the full snapshot itself"},
        "ttl": {"type": "integer", "required": True,
               "description": "seconds this decision should be considered "
                              "fresh"},
        "issued_at": {"type": "string", "required": True,
                     "description": "ISO-8601 UTC timestamp this decision "
                                    "was computed"},
        "expires_at": {"type": "string", "required": True,
                       "description": "ISO-8601 UTC timestamp = issued_at "
                                      "+ ttl seconds"},
        "provider": {"type": "string", "required": True,
                    "enum": ["merona"],
                    "description": "constant identifying this provider"},
        "disclaimer": {"type": "string", "required": True,
                       "description": "human-readable framing of what the "
                                      "decision does and doesn't mean"},
        "envelope": {
            "type": "object", "required": False,
            "description": "signed-card envelope; present iff this "
                           "deployment has envelope signing enabled "
                           "(X402_TRUST_SIGNING_KEY configured) — absent "
                           "entirely otherwise, never null/empty",
            "entry_fields": {
                "canonicalisation": {"type": "string",
                    "enum": ["urn:x402:canonicalisation:jcs-rfc8785-v1"],
                    "description": "canonicalisation algorithm the "
                                   "signature is computed over (RFC 8785 "
                                   "JCS) — pinned, one value today"},
                "alg": {"type": "string", "enum": ["Ed25519"],
                       "description": "signature algorithm"},
                "kid": {"type": "string",
                       "description": "first 12 hex chars of sha256(raw "
                                      "Ed25519 public key) — matches a "
                                      "`kid` in the jwks.json document at "
                                      "`public_key_url`"},
                "card_ref": {"type": "string",
                            "description": "\"sha256:\" + sha256 of the "
                                           "JCS-canonical response body "
                                           "WITHOUT this envelope field — "
                                           "a content address for the "
                                           "signed card"},
                "signature": {"type": "string",
                             "description": "Ed25519 signature over the "
                                            "same canonical bytes "
                                            "card_ref hashes, "
                                            "base64url-encoded, no "
                                            "padding"},
                "public_key_url": {"type": "string",
                                   "description": "JWKS document "
                                                  "publishing the public "
                                                  "key for `kid` "
                                                  "(GET /.well-known/"
                                                  "jwks.json)"},
            },
        },
    },
    "reason_codes": dict(_EVAL_REASON_CODE_NOTES),
    # GET /v1/delivery/{chain}/{address} (paid; not part of the free
    # trust-provider `request`/`response` pair above, but small enough to
    # document here rather than invent a second spec doc for one route).
    "delivery": {
        "not_evaluated": {
            "description": "x402#2300 commitment #2: what an address with "
                           "NO paid-probe history on file now returns — "
                           "HTTP 200 (not 404), so it's still billable, "
                           "because discovery_basis is itself the fact "
                           "being sold. Distinct from a probed-but-"
                           "inconclusive outcome (which returns the normal "
                           "delivery/probes/counts shape below).",
            "shape": {"status": "\"not_evaluated\"",
                      "discovery_basis": "optional, see below — omitted "
                                         "entirely with no catalog snapshot "
                                         "configured",
                      "note": "human-readable framing of what "
                             "not_evaluated does and doesn't mean",
                      "probe_coverage": "see below"},
        },
        "discovery_basis": {"type": "object", "required": False,
            "description": "present on EVERY /v1/delivery response "
                           "(not_evaluated or probed) when this deployment "
                           "has a readable catalog snapshot configured — "
                           "same {catalog, snapshot_sha256, snapshot_date, "
                           "catalog_max_last_updated} shape as the curated "
                           "basis entry above, from the same "
                           "_load_discovery_basis loader"},
        "probe_coverage": {"type": "boolean", "required": True,
            "description": "present on EVERY /v1/delivery response "
                           "(not_evaluated or probed), evaluated or not: "
                           "true iff `chain` is in PROBE_COVERAGE_CHAINS "
                           "(base only today — payprobe pays plain "
                           "EIP-3009 USDC on Base; polygon/solana probes "
                           "don't exist yet, see attest/payprobe.py "
                           "BASE_NETWORKS). Trust SCORES cover base/"
                           "polygon/solana; delivery EVIDENCE does not. "
                           "false responses also carry `probed_chains` "
                           "(the chains actually covered) and `reason`."},
    },
}


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


# --- Pro tier: bulk + history endpoints (GET, JSON, no x402 lane) --------------
# Gated on a pro key (see _is_pro / Handler._identity), never on payment — the
# do_GET payable-prefix tuple ("/v1/trust/", "/v1/agent/", "/v1/endpoint")
# does not and must not include "/v1/pro/".
PRO_SCORES_HISTORY_DEFAULT_LIMIT = 90
PRO_SCORES_HISTORY_MAX_LIMIT = 365
_PRO_SELLER_COLS = ("seller", "score", "grade", "provisional", "tx_count",
                    "unique_payers", "self_pay_ratio", "measured_date",
                    "snapshot_date")


def pro_scores(chain: str) -> tuple[int, dict]:
    """Every seller's LATEST seller_scores row for `chain`. The nightly
    writer (scores.compute_seller_scores) scores every seller for a chain in
    one run under a single `date`, so all sellers in a batch share
    measured_date — the simpler latest-BATCH query (one MAX subquery per
    chain) is exact here, not an approximation of a true per-seller window."""
    if chain not in _EVAL_SUPPORTED_CHAINS:
        return 400, {"error": "bad chain"}
    rows = _query(
        "SELECT seller, score, grade, provisional, tx_count, unique_payers, "
        "self_pay_ratio, measured_date, snapshot_date FROM seller_scores "
        "WHERE chain=? AND measured_date=(SELECT MAX(measured_date) FROM "
        "seller_scores WHERE chain=?) ORDER BY seller", (chain, chain))
    sellers = [dict(zip(_PRO_SELLER_COLS, r)) for r in rows]
    for s in sellers:
        s["provisional"] = bool(s["provisional"])
    snap = rows[0][8] if rows else None
    return 200, {
        "chain": chain, "count": len(sellers), "sellers": sellers,
        # re-derivability travels with the bulk data too, same block the
        # single-seller lookups expose (_verification) — never a new hash.
        "snapshot": _verification(snap) if snap else None,
    }


def pro_scores_history(chain: str, address: str, limit_raw) -> tuple[int, dict]:
    """Full seller_scores time series for one seller, newest first. Address
    validation is identical to trust_lookup's (_HEX_ADDR/_B58_ADDR, lowercase
    hex) — the same rule, not a re-derived one."""
    if chain not in _EVAL_SUPPORTED_CHAINS:
        return 400, {"error": "bad chain"}
    if _HEX_ADDR.match(address):
        address = address.lower()
    elif not _B58_ADDR.match(address):
        return 400, {"error": "bad address"}
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = PRO_SCORES_HISTORY_DEFAULT_LIMIT
    limit = max(1, min(PRO_SCORES_HISTORY_MAX_LIMIT, limit))
    rows = _query(
        "SELECT seller, score, grade, provisional, tx_count, unique_payers, "
        "self_pay_ratio, measured_date, snapshot_date FROM seller_scores "
        "WHERE chain=? AND seller=? ORDER BY measured_date DESC LIMIT ?",
        (chain, address, limit))
    history = [dict(zip(_PRO_SELLER_COLS, r)) for r in rows]
    for h in history:
        h["provisional"] = bool(h["provisional"])
    return 200, {"chain": chain, "seller": address, "count": len(history),
                "limit": limit, "history": history}


def pro_delivery(chain: str) -> tuple[int, dict]:
    """The full delivery-state map for a chain, from the same cached loader
    _delivery() uses (_delivery_map) — receipts.jsonl is read exactly once,
    never re-parsed here."""
    if chain not in _EVAL_SUPPORTED_CHAINS:
        return 400, {"error": "bad chain"}
    states = _delivery_map()
    sellers = {addr: state for (c, addr), state in states.items()
              if c == chain}
    return 200, {"chain": chain, "count": len(sellers), "sellers": sellers}


DELIVERY_DISCLAIMER = (
    "dated facts from real paid probes, re-derivable from the anchored "
    "snapshot; probes repeat and newer outcomes supersede older ones")


def delivery_lookup(chain: str, address: str) -> tuple[int, dict]:
    """Per-seller genuine-payer delivery evidence: the LATEST delivery state
    (the same block trust_lookup's `delivery` field carries, via _delivery())
    plus the FULL paid-probe history with settlement txs (_delivery_history_
    map(), the history half of the same cached receipts.jsonl reader
    _delivery()/pro_delivery() use — no separate parse). This is per-seller
    depth; GET /v1/pro/delivery is the bulk latest-state-only counterpart.

    Chain validation matches /v1/trust/evaluate (_EVAL_SUPPORTED_CHAINS —
    base/polygon/solana, exact set); address validation is identical to
    trust_lookup's (_HEX_ADDR lowercased / _B58_ADDR).

    `probe_coverage` (see PROBE_COVERAGE_CHAINS) is stamped on every
    response, evaluated or not: trust-evaluate accepts all three chains, but
    payprobe can currently only PAY (and therefore only produce delivery
    evidence) on base. A caller must be able to tell "we probed and found
    nothing yet" apart from "we cannot probe this chain at all" without
    guessing from an empty `probes` list."""
    if chain not in _EVAL_SUPPORTED_CHAINS:
        return 400, {"error": "bad chain"}
    if _HEX_ADDR.match(address):
        address = _norm_payto(address)
    elif not _B58_ADDR.match(address):
        return 400, {"error": "bad address"}
    covered = chain in PROBE_COVERAGE_CHAINS
    probes = _delivery_history_map().get((chain, address)) or []
    if not probes:
        # #2300 commitment #2: an explicit not_evaluated state. This used to
        # be a bare 404 — indistinguishable from "never in the discovery set
        # that fed the paid sweep" AND from a rate-limited/broken client.
        # Note the payment-gating consequence: do_GET's paid lane settles on
        # ANY 200 (never on non-200) — this response IS a 200, so a paying
        # caller is charged for it, same as for a probed seller. That's
        # deliberate: discovery_basis is itself the useful fact being sold
        # here, not a placeholder for "nothing to report."
        resp = {"status": "not_evaluated",
                "note": "address was not in the discovery set that fed the "
                        "paid sweep — distinct from probed-but-inconclusive",
                "probe_coverage": covered}
        if not covered:
            resp["probed_chains"] = list(PROBE_COVERAGE_CHAINS)
            resp["reason"] = _PROBE_COVERAGE_REASON
        if DISCOVERY_BASIS is not None:
            resp["discovery_basis"] = DISCOVERY_BASIS
        return 200, resp
    counts: dict = {}
    for p in probes:
        counts[p["outcome"]] = counts.get(p["outcome"], 0) + 1
    # Same provenance rule as trust_lookup: the seller's own latest scored
    # snapshot, if any — never a fabricated hash for a seller never scored.
    rows = _query(
        "SELECT snapshot_date FROM seller_scores WHERE chain=? AND seller=? "
        "ORDER BY measured_date DESC LIMIT 1", (chain, address))
    snap = rows[0][0] if rows else None
    resp = {
        "chain": chain, "seller": address,
        "delivery": _delivery(chain, address),
        "probes": probes,
        "counts": counts,
        "verification": _verification(snap),
        "disclaimer": DELIVERY_DISCLAIMER,
        "probe_coverage": covered,
    }
    if not covered:
        resp["probed_chains"] = list(PROBE_COVERAGE_CHAINS)
        resp["reason"] = _PROBE_COVERAGE_REASON
    if DISCOVERY_BASIS is not None:
        resp["discovery_basis"] = DISCOVERY_BASIS
    return 200, resp


def _pro_forbidden() -> tuple[int, dict]:
    return 403, {"error": "pro tier required",
                "pricing": "https://api.merona.io/#pro",
                "contact": "hello@merona.io"}


INDEX = {
    "service": "m.Score API",
    "endpoints": {
        "GET /v1/trust/{chain}/{address}": "seller trust score (0-100, A-F)",
        "GET /v1/agent/{chain}/{address}": "payer/agent trust score; returns "
                                           "UNKNOWN rather than inventing a "
                                           "score for an unseen wallet",
        "GET /v1/endpoint?origin=URL": "endpoint reliability grade",
        "GET /v1/delivery/{chain}/{address}": "per-seller genuine-payer "
                                              "delivery evidence: latest "
                                              "state + full paid-probe "
                                              "history with settlement txs",
        "GET /v1/health": "liveness",
        "POST /v1/trust/evaluate": "x402 trust-provider PASS/FAIL/UNCERTAIN "
                                   "gate decision (x402-trust-query-v0.1); "
                                   "free, no key or payment required",
        "GET /v1/pro/scores?chain=": "every seller's latest score for a "
                                     "chain, bulk (pro)",
        "GET /v1/pro/scores/history?chain=&address=&limit=": "full score "
                                     "time series for one seller (pro)",
        "GET /v1/pro/delivery?chain=": "delivery-state map (paid-probe "
                                       "evidence) for a chain, bulk (pro)",
    },
    "mcp": {"endpoint": "POST /mcp (streamable HTTP)",
            "manifest": "GET /mcp.json",
            "tools": "payto_check + mismatch_feed + clean_stats + "
                     "trust_score — all free (optional x402 receipts)"},
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

<h2>Scores $0.005 &#183; delivery evidence $0.01 — via x402, or an API key</h2>
<div class="ep"><code>GET /v1/trust/{chain}/{address}</code><span>seller trust score (0&#8211;100, A&#8211;F)</span><span class="paid">paid</span></div>
<div class="ep"><code>GET /v1/agent/{chain}/{address}</code><span>payer/agent score — UNKNOWN if unseen</span><span class="paid">paid</span></div>
<div class="ep"><code>GET /v1/endpoint?origin=URL</code><span>endpoint reliability grade</span><span class="paid">paid</span></div>
<div class="ep"><code>GET /v1/delivery/{chain}/{address}</code><span>per-seller delivery evidence — latest state + full paid-probe history</span><span class="paid">$0.01</span></div>
<div class="ep"><code>GET /v1/health</code><span>liveness</span></div>

<h2>Pay per call — no key, no signup</h2>
<pre>curl https://api.merona.io/v1/trust/base/0xSELLER
<b># -&gt; 402 + payment instructions (x402 exact scheme, Base USDC)</b>
<b># retry with X-PAYMENT; receipt returned in X-PAYMENT-RESPONSE</b></pre>

<h2 id="evaluate">Agent gate — free · x402 trust-provider</h2>
<div class="ep"><code>POST /v1/trust/evaluate</code><span>PASS / FAIL / UNCERTAIN
before an agent pays — x402-trust-query-v0.1 in, x402-trust-evaluation-v0.1
out, <code>direction: seller</code></span></div>
<pre>curl -sX POST https://api.merona.io/v1/trust/evaluate \\
  -H 'content-type: application/json' \\
  -d '{"schema":"x402-trust-query-v0.1","direction":"seller",
       "subject":{"wallet":"0xSELLER","chain":"base"}}'
<b># FAILs rest on recorded on-chain facts, never community reports: a</b>
<b># charged-but-unserved probe ships its settlement tx in `evidence`; a</b>
<b># grade FAIL cites the anchored score snapshot it derives from</b>
<b># basis[] entries are typed {class,kind,ref,observed_at}; class is one of</b>
<b># curated/behavioral/community/attested — a FAIL always cites curated or</b>
<b># behavioral evidence (never attested/community alone)</b>
<b># unknown sellers return score:null + null_reason, never a fabricated number</b>
<b># evidence_uri is content-addressed (sha256: nightly anchored snapshot);</b>
<b># issued_at/expires_at (= issued_at + ttl) bound how long the decision is fresh</b>
<b># responses are SIGNED: Ed25519 over the JCS (RFC 8785) canonical bytes —</b>
<b># strip `envelope`, canonicalise, verify against /.well-known/jwks.json</b></pre>
<p class="tag">Implements the trust-provider extension proposed in
<a href="https://github.com/x402-foundation/x402/issues/2299">x402#2299</a> —
the seller direction: trust scores cover base · polygon · solana; merona
actually pays sellers and records whether they deliver — that paid-probe
evidence is base-only today (payprobe pays plain EIP-3009 USDC on Base;
polygon/solana probes don't exist yet — see <code>probe_coverage</code> on
<code>GET /v1/delivery</code>). Provider descriptor:
<a href="/.well-known/x402-trust-provider">/.well-known/x402-trust-provider</a>
· field-level schema: <a href="/v1/spec">/v1/spec</a>
· signing key: <a href="/.well-known/jwks.json">/.well-known/jwks.json</a>.</p>
<p class="tag">Wondering if a hit in your logs is really us? Every outbound
UA we send is listed at
<a href="/.well-known/merona-crawlers.json">/.well-known/merona-crawlers.json</a>
(x402#2300) — and delivery evidence (curated <code>first_party_probe</code>
basis entries, <code>GET /v1/delivery</code>) carries its own
<code>discovery_basis</code>: the content-addressed catalog snapshot the
paid sweep drew its targets from, so absence from a sweep is never
confused with a refusal. It carries two dates on purpose —
<code>snapshot_date</code> (when the snapshot file was last written) and
<code>catalog_max_last_updated</code> (the newest listing date actually in
the data) — because a re-run that silently replays a stale cache produces a
fresh file with old data, and the divergence between those two dates is
exactly how you'd catch that.</p>
<p class="tag">Verify it yourself — zero-dependency, no auth:
<a href="https://github.com/jywy78kgrf-create/merona/blob/main/conformance/check.py">conformance/check.py</a>
runs 146 shape + signature checks against this live API, and
<a href="https://github.com/jywy78kgrf-create/merona/blob/main/conformance/verify_evidence.py">conformance/verify_evidence.py</a>
walks any evaluation back to primary data: anchors record → sha256
membership → combined-hash recomputation → the EAS attestation on Base.
A FAIL that cites our evidence can be audited without trusting us.</p>

<h2 id="pro">Pro — bulk + history · $300/mo</h2>
<div class="ep"><code>GET /v1/pro/scores?chain=</code><span>every seller's latest score for a chain, bulk</span></div>
<div class="ep"><code>GET /v1/pro/scores/history?chain=&amp;address=&amp;limit=</code><span>full score time series for one seller</span></div>
<div class="ep"><code>GET /v1/pro/delivery?chain=</code><span>delivery-state map (paid-probe evidence) for a chain, bulk</span></div>
<pre>curl -H 'X-API-Key: YOUR_PRO_KEY' \\
  https://api.merona.io/v1/pro/scores?chain=base
<b># 600 rpm (10&#215;) · X-API-Key header · email SLA</b></pre>
<p class="tag">$300/mo · $3,000/yr — manually onboarded, mail
<a href="mailto:hello@merona.io">hello@merona.io</a> — USDC or invoice.
All per-call pricing and the free feeds above are unchanged.</p>

<h2 class="mcp">MCP — wire it into your agent</h2>
<div class="ep mcp"><code>POST /mcp</code><span>streamable HTTP · manifest at <a href="/mcp.json">/mcp.json</a></span></div>
<div class="ep mcp"><code>tools</code><span>payto_check · mismatch_feed · clean_stats · trust_score — all free</span></div>
<div class="ep mcp"><code>registry</code><span>listed on <a href="https://smithery.ai/servers/michaelfitz/merona">Smithery</a> and the official MCP registry (io.merona/settlement-index)</span></div>
<div class="ep mcp"><code>claude</code><span>Settings &rarr; Connectors &rarr; Add custom connector &rarr;
paste <code>https://api.merona.io/mcp</code> — no key, no signup; the three
free tools work on the first call</span></div>
<div class="ep mcp"><code>cursor / cline / continue</code><span>add to your MCP
config as a streamable-HTTP server with the same URL</span></div>

<h2>Free, no key</h2>
<div class="ep"><code>merona.io/check?q=</code><span>one-shot payTo-integrity check (wallet or url)</span></div>
<div class="ep"><code>merona.io/mismatches.json</code><span>bulk feed · <a href="https://merona.io/mismatches">human view</a></span></div>

<p class="foot">Every score carries the snapshot sha256 + public anchor so it
can be independently re-derived. Snapshot hashes are attested nightly on Base
by merona's attester
<a href="https://base.easscan.org/address/0x644678AD37833C0d52f0170f1F73A5e62Bc3e6d5">0x6446&#8230;e6d5</a>
— only attestations from that address are merona's. Agents: this same URL
returns JSON unless the request asks for text/html.</p>
</div></body></html>"""


def _pay_params(path: str) -> tuple[float | None, str]:
    """(price_usd, description) for the x402 paid lane, keyed on path. Every
    payable prefix except /v1/delivery/ prices at the module default
    (price_usd=None -> x402pay.PRICE_USD); /v1/delivery/ is the one route
    priced separately. The SAME pair must be used for a given path's 402
    advertisement and its settle() call, or the facilitator rightly rejects
    the payment on amount mismatch — this is the one place that decision is
    made, so both call sites agree by construction."""
    if path.startswith("/v1/delivery/"):
        return DELIVERY_PRICE_USD, DELIVERY_PAY_DESC
    return None, PAY_DESC


_LOG_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._/ -]+")


def _mcp_log_name(raw: str) -> str:
    """Sanitize attacker-controlled text (MCP clientInfo, unknown method
    names) before it lands in the usage-log path field: keep a conservative
    charset, collapse any run of anything else to one underscore, trim the
    edges. No length cap here — callers slice to whatever fits their slot;
    _meter's MAX_PATH_LOG truncates the whole path as the final backstop
    regardless."""
    return _LOG_NAME_UNSAFE.sub("_", raw).strip()


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

    def _identity(self) -> tuple[str, bool, bool]:
        """(identity, authorized, is_pro). Keys are accepted ONLY via the
        X-API-Key header — never in the URL, where they would persist in
        proxy/CDN access logs and browser history (audit should-fix). With
        keys configured a valid key is required; without any, fall back to
        per-IP anonymous (never pro — pro requires a configured key)."""
        key = self.headers.get("X-API-Key") or ""
        if KEYS:
            if key in KEYS:
                name = KEYS[key]
                return f"key:{name}", True, _is_pro(name)
            return f"ip:{self._client_ip()}", False, False
        return f"ip:{self._client_ip()}", True, False

    def do_GET(self):        # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/v1/health", "/healthz"):
            return self._send(200, {"ok": True}, cache="no-store")
        # Rate-limit EVERY non-health route up front — including the static
        # ones below (/, /mcp.json, glama, /outreach/review). Previously these
        # served before any limiter, so an unauthenticated flood of GET / was
        # unthrottled; now only /healthz (for uptime monitors) bypasses.
        ident, ok, is_pro = self._identity()
        # Pro identities draw from a SEPARATE token-bucket pool (product
        # spec): exhausting one pool never throttles the other.
        wait = (PRO_BUCKETS if is_pro else BUCKETS).take(ident)
        if wait > 0:
            return self._send(429, {"error": "rate limited"},
                              {"Retry-After": str(max(1, int(wait + 0.5)))},
                              cache="no-store")
        if path == "/":
            # "/" is content-negotiated (HTML for browsers, JSON for agents),
            # so BOTH representations must send `Vary: Accept` — without it
            # an edge cache (Cloudflare, max-age=300) serves whichever
            # representation warmed the PoP first to every client for the
            # TTL, Accept header ignored (observed live, 2026-08-12 audit).
            if "text/html" in (self.headers.get("Accept") or ""):
                body = LANDING_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "public, max-age=300")
                self.send_header("Vary", "Accept")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            return self._send(200, INDEX, {"Vary": "Accept"})
        if path == "/v1/trust/evaluate":
            # A human clicking the gate link (or an agent probing with GET)
            # must get USAGE, not the paid lane's 402 — this route is POST-only
            # and free. Answer here, before the payable /v1/trust/ prefix
            # matching below can claim the path.
            return self._send(405, {
                "error": "use POST",
                "method": "POST /v1/trust/evaluate",
                "schema": "x402-trust-query-v0.1",
                "example": {"schema": "x402-trust-query-v0.1",
                            "direction": "seller",
                            "subject": {"wallet": "0x…", "chain": "base"}},
                "free": True,
                "descriptor": PUBLIC_BASE + "/.well-known/x402-trust-provider",
                "spec": PUBLIC_BASE + "/v1/spec",
                "docs": PUBLIC_BASE + "/#evaluate"}, cache="public, max-age=300")
        if path == "/outreach/review":
            # Read-only approval review page. Loading it NEVER sends (email
            # scanners pre-fetch links); the token is the bearer secret,
            # emailed only to the operator, and an invalid token gets no
            # batch contents. (Rate-limited up top, with every route.)
            token = (parse_qs(urlparse(self.path).query).get("token")
                     or [""])[0]
            code, page = _outreach_review(token)
            body = page.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        if path == "/outreach/worklist":
            # Read-only, bookmarkable to-do list. Same security shape as
            # /outreach/review: token is the bearer secret, an invalid token
            # gets no batch contents, and this page NEVER sends (no POST target).
            token = (parse_qs(urlparse(self.path).query).get("token")
                     or [""])[0]
            code, page = _outreach_worklist(token)
            body = page.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        if path == "/mcp.json":
            return self._send(200, MCP_MANIFEST)
        if path == "/.well-known/glama.json":
            # Directory ownership proof: Glama treats control of this path as
            # proof that whoever holds GLAMA_MAINTAINER_EMAIL runs the server.
            # Public contact address only — never a key, never a token.
            return self._send(200, GLAMA_WELL_KNOWN)
        if path == "/.well-known/x402-trust-provider":
            # Machine-readable provider descriptor for the x402
            # trust-provider extension (x402#2299 draft-02) — same
            # caching/no-auth style as the glama.json route above; this
            # one's audience is agents deciding whether/how to call
            # POST /v1/trust/evaluate.
            return self._send(200, TRUST_PROVIDER_DESCRIPTOR)
        if path == "/.well-known/jwks.json":
            # Public half of the envelope signing key (see _build_envelope's
            # docstring for the verification recipe). {"keys": []} — not an
            # error — when X402_TRUST_SIGNING_KEY isn't configured.
            return self._send(200, _jwks())
        if path == "/.well-known/merona-crawlers.json":
            # #2300 commitment #3: canonical crawler-UA registry, same
            # no-auth/cached style as the other /.well-known/ routes above.
            return self._send(200, CRAWLERS)
        if path.startswith("/.well-known/"):
            # any OTHER well-known probe gets an honest 404 — before this,
            # unknown discovery paths fell through to the paid lane's 401
            # ("invalid or missing API key"), telling a stranger a
            # nonexistent file required payment (2026-08-12 audit)
            return self._send(404, {"error": "not found"})
        if path == "/v1/spec":
            # Field-level schema for x402-trust-query-v0.1 (request) and
            # x402-trust-evaluation-v0.1 (response) — hand-written, not
            # derived, so it documents what merona actually returns.
            return self._send(200, TRUST_API_SPEC)
        if path.startswith("/v1/pro/"):
            # Read-only bulk tier, gated on a pro key — never x402 (this
            # prefix is deliberately outside the payable-prefix tuple below)
            # and never a 402. No key / a non-pro key both get the same 403
            # with a pricing pointer, not the generic 401 the keyed lane uses.
            if not is_pro:
                code, obj = _pro_forbidden()
                self._send(code, obj, cache="no-store")
                return _meter(ident, path, code)
            code, obj = self._route_pro(path)
            self._send(code, obj, cache="no-store")
            return _meter(ident, path, code)

        # (identity + rate limit already applied at the top of do_GET, so the
        # anonymous-flood / usage-file abuse guard from LB-6 still holds here.)
        if not ok:
            # x402 lane: no key needed — pay per call on the rail we index.
            payable = path.startswith(("/v1/trust/", "/v1/agent/",
                                       "/v1/endpoint", "/v1/delivery/"))
            # Free-for-now: anonymous + no X-PAYMENT -> served, with a note
            # pointing at the optional receipts lane. Attaching X-PAYMENT
            # opts into the unchanged verify+settle+receipt flow below, and
            # the anonymous rate limit applied at the top of do_GET still
            # bounds this lane.
            if FREE_MODE and payable and not self.headers.get("X-PAYMENT"):
                code, obj = self._route(path)
                if code == 200 and isinstance(obj, dict):
                    obj["pricing"] = _free_pricing_note()
                self._send(code, obj,
                           cache="public, max-age=300" if code == 200
                           else "no-store")
                return _meter(ident, path, code)
            if x402pay.enabled() and payable:
                resource = PUBLIC_BASE + path
                price_usd, desc = _pay_params(path)
                pay = self.headers.get("X-PAYMENT")
                if not pay:
                    self._send(402, x402pay.payment_required(
                                   resource, desc, price_usd=price_usd),
                               cache="no-store")
                    return _meter(ident, path, 402)
                # answer FIRST, settle ONLY on success — never charge for a
                # 404/503
                code, obj = self._route(path)
                if code != 200:
                    self._send(code, obj, cache="no-store")
                    return _meter(ident, path, code)
                ok2, receipt, payer, err = x402pay.settle(
                    pay, resource, desc, price_usd=price_usd)
                if not ok2:
                    body = x402pay.payment_required(
                        resource, desc, price_usd=price_usd)
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
            md = re.match(r"^/v1/delivery/([^/]+)/([^/]+)$", path)
            if m:
                return trust_lookup(m.group(1), m.group(2))
            if ma:
                return agent_lookup(ma.group(1), ma.group(2))
            if md:
                return delivery_lookup(md.group(1), md.group(2))
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

    def _route_pro(self, path: str):
        # Isolated try/except like _route (audit LB-5): a bug in the pro
        # handlers must never take the process down or leak backend text.
        try:
            q = parse_qs(urlparse(self.path).query)
            chain = (q.get("chain") or [""])[0].strip().lower()
            if path == "/v1/pro/scores":
                return pro_scores(chain)
            if path == "/v1/pro/scores/history":
                address = (q.get("address") or [""])[0].strip()
                limit_raw = (q.get("limit")
                            or [str(PRO_SCORES_HISTORY_DEFAULT_LIMIT)])[0]
                return pro_scores_history(chain, address, limit_raw)
            if path == "/v1/pro/delivery":
                return pro_delivery(chain)
            return 404, {"error": "not found", "index": "/"}
        except Exception as e:
            print(f"[trust-api] pro backend error on {path[:MAX_PATH_LOG]}: "
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
        if path == "/v1/trust/evaluate":
            # x402 trust-provider gate decision. FREE by construction: the
            # x402 payment lane (X402_PAYTO / x402pay.enabled()) only exists
            # in do_GET's payable-prefix branch below — this POST route never
            # consults it and answers unconditionally once past rate limiting.
            ident, _ok, _is_pro = self._identity()
            wait = BUCKETS.take(ident)
            if wait > 0:
                return self._send(429, {"error": "rate limited"},
                                  {"Retry-After": str(max(1, int(wait + 0.5)))},
                                  cache="no-store")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n > TRUST_EVALUATE_MAX_BODY:
                self._send(413, {"error": "body too large, max 16384 bytes"},
                          cache="no-store")
                return _meter(ident, path, 413)
            try:
                raw = self.rfile.read(n) if n > 0 else b""
            except Exception:
                raw = b""
            try:
                code, obj = _trust_evaluate(raw)
            except Exception as e:
                # Isolated failure: a bug here must never take the process
                # down or leak backend/DB exception text to the client
                # (audit LB-5 — same rule _route follows).
                print(f"[trust-api] trust evaluate error: "
                      f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
                code, obj = 500, {"error": "backend unavailable"}
            self._send(code, obj, cache="no-store")
            return _meter(ident, path, code)
        if path == "/outreach/approve":
            # Records "operator approved the current batch" — nothing more.
            # No credentials here; a privileged poller on the box does the
            # actual send. Token (bearer secret from the emailed link) is the
            # only auth. Rate-limited like everything else.
            wait = BUCKETS.take(self._identity()[0])
            if wait > 0:
                return self._send(429, {"error": "rate limited"},
                                  cache="no-store")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                token = parse_qs(self.rfile.read(min(n, 4096)).decode(
                    "utf-8", "replace")).get("token", [""])[0]
            except Exception:
                token = ""
            ok, page = _outreach_approve(token)
            body = page.encode()
            self.send_response(200 if ok else 410)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path != "/mcp":
            return self._send(404, {"error": "not found", "index": "/"},
                              cache="no-store")
        ident, _ok, _is_pro = self._identity()
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
            # notifications/initialized is the signal a client finished the
            # full handshake — worth a meter line. Every other notification
            # (cancelled, progress, ...) stays unmetered: high-volume, no
            # signal.
            if method == "notifications/initialized":
                _meter(ident, "/mcp:notif:initialized", 202)
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        if method == "initialize":
            params = msg.get("params")
            ci = params.get("clientInfo") if isinstance(params, dict) else None
            parts = []
            if isinstance(ci, dict):
                # clientInfo is attacker-controlled input that becomes a log
                # field — sanitize both parts before they ever touch disk.
                if ci.get("name"):
                    parts.append(_mcp_log_name(str(ci["name"])))
                if ci.get("version"):
                    parts.append(_mcp_log_name(str(ci["version"])))
            client = "/".join(p for p in parts if p)[:60]
            _meter(ident, f"/mcp:initialize:{client}" if client
                   else "/mcp:initialize", 200)
            result = {"protocolVersion": MCP_PROTOCOL,
                      "capabilities": {"tools": {}},
                      "instructions": MCP_INSTRUCTIONS,
                      "serverInfo": {"name": "merona-mcp",
                                    "title": "merona — x402 settlement "
                                             "index",
                                    "version": "1.2"}}
        elif method == "tools/list":
            _meter(ident, "/mcp:tools/list", 200)
            result = {"tools": MCP_TOOLS}
        elif method in ("resources/list", "resources/templates/list"):
            # We serve tools only, and say so in `capabilities`. Directory
            # scanners probe these anyway; -32601 is a legal answer but reads
            # as a failure in their logs, so answer empty instead of erroring.
            _meter(ident, f"/mcp:{method}", 200)
            result = {"resources": []} if method == "resources/list" else \
                {"resourceTemplates": []}
        elif method == "prompts/list":
            _meter(ident, "/mcp:prompts/list", 200)
            result = {"prompts": []}
        elif method == "tools/call":
            params = msg.get("params")
            if not isinstance(params, dict):     # scanners send lists/nulls
                params = {}
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
            if isinstance(out, dict):
                # structured twin of the text content (spec 2025-06-18);
                # older clients simply ignore the extra key
                result["structuredContent"] = out
            # metered status: 500 tool error, 402 unpaid lane answered with
            # payment instructions (the JSON-RPC response itself is still
            # 200) — so a paid trust_score call is distinguishable from a
            # probe that only ever collects the paywall.
            st = 200
            if result["isError"]:
                st = 500
            elif isinstance(out, dict) and "payment_required" in out:
                st = 402
            _meter(ident, f"/mcp:{name[:40]}", st)
        else:
            # method itself is attacker-controlled (same as clientInfo
            # above) — sanitize before it hits the log.
            _meter(ident, f"/mcp:?{_mcp_log_name(method)[:40]}", 200)
            return self._send(200, {"jsonrpc": "2.0", "id": mid,
                                    "error": {"code": -32601,
                                              "message": "method not found"}},
                              cache="no-store")
        self._send(200, {"jsonrpc": "2.0", "id": mid, "result": result},
                   cache="no-store")

    do_HEAD = do_GET          # uptime monitors probe with HEAD

    def log_message(self, fmt, *args):   # quiet; metering covers it
        pass


# 2025-06-18: the spec revision that added tool outputSchema +
# structuredContent, both of which we now serve
MCP_PROTOCOL = "2025-06-18"
# Injected into model context by clients that honor `instructions`
# (spec 2025-06-18). Factual only — no pitch, the tools speak for themselves.
MCP_INSTRUCTIONS = (
    "merona is an independent x402 settlement index — it does not run "
    "payment rails and holds no seller relationships.\n"
    "All four tools are free — no API key, payment, or signup required: "
    "payto_check checks one wallet or endpoint's payout integrity before "
    "you pay it; "
    "mismatch_feed lists the current payout mismatches (top 10; full feed "
    "at merona.io/mismatches.json); "
    "clean_stats reports wash-adjusted settlement volume per chain; "
    "trust_score returns a wash-aware seller grade with a re-derivable "
    "snapshot hash (optionally pay "
    f"${x402pay.PRICE_USD:g} via x402 for a settlement-receipted response).\n"
    "Call them freely — no auth handshake needed."
)
MCP_TOOLS = [
    {"name": "payto_check",
     "description": "Check whether an x402 endpoint or wallet pays out where "
                    "it claims to, before your agent sends funds. Takes one "
                    "wallet address or endpoint URL. Returns any "
                    "catalog-vs-live payout mismatch, dated payout-rotation "
                    "history, and whether the address has settled on chain. "
                    "Free, no key. Use mismatch_feed instead to list every "
                    "known mismatch. Records are dated facts, not "
                    "accusations, and no record found is not a clearance.",
     "annotations": {"title": "payTo integrity check", "readOnlyHint": True,
                     "destructiveHint": False, "idempotentHint": True,
                     "openWorldHint": False},
     "inputSchema": {"type": "object", "required": ["query"],
                     "properties": {"query": {
                         "type": "string", "maxLength": 256,
                         "description": "Wallet address (0x… or base58) or "
                                        "endpoint URL/hostname to look up."}}},
     "outputSchema": {
         "type": "object", "additionalProperties": True,
         "description": "Match results for the queried wallet/endpoint.",
         "properties": {
             "query": {"type": "string",
                       "description": "The query as interpreted."},
             "matches": {"type": "array",
                         "description": "Mismatch/rotation records touching "
                                        "the query; empty when none known."},
             "note": {"type": "string",
                      "description": "Interpretation guidance — dated facts, "
                                     "not accusations."},
             "error": {"type": "string",
                       "description": "Present only on backend failure."}}}},
    {"name": "mismatch_feed",
     "description": "List x402 endpoints currently advertising one payout "
                    "address while asking payers for another, from merona's "
                    "nightly probe of the public catalog — the top 10 "
                    "entries with advertised vs live addresses, full counts "
                    "and as-of dates (complete feed, uncapped: "
                    "merona.io/mismatches.json). Takes no arguments. Free, "
                    "no key. Use payto_check instead when you already have "
                    "a specific address or endpoint to check.",
     "annotations": {"title": "payTo mismatch feed", "readOnlyHint": True,
                     "destructiveHint": False, "idempotentHint": True,
                     "openWorldHint": False},
     "inputSchema": {"type": "object", "properties": {},
                     "additionalProperties": False,
                     "description": "This tool takes no arguments."},
     "outputSchema": {
         "type": "object", "additionalProperties": True,
         "description": "The current public mismatch feed.",
         "properties": {
             "meta": {"type": "object",
                      "description": "Feed provenance: generated-at, counts, "
                                     "method pointer."},
             "catalog_vs_live": {
                 "type": "array",
                 "description": "Endpoints advertising one payout address "
                                "while asking payers for another (top 10)."},
             "payto_rotation_count": {
                 "type": "integer",
                 "description": "Endpoints with dated payout-address "
                                "rotation history."},
             "error": {"type": "string",
                       "description": "Present only on backend failure."}}}},
    {"name": "clean_stats",
     "description": "Get wash-adjusted x402 settlement volume per chain — "
                    "spend that survives removal of funded loops and "
                    "self-dealing cycles, which can differ from raw transfer "
                    "totals by orders of magnitude. Returns clean settlement "
                    "count, clean volume in USD and an as-of date per chain. "
                    "Takes no arguments. Free, no key. These are the figures "
                    "published on merona.io.",
     "annotations": {"title": "clean settlement stats", "readOnlyHint": True,
                     "destructiveHint": False, "idempotentHint": True,
                     "openWorldHint": False},
     "inputSchema": {"type": "object", "properties": {},
                     "additionalProperties": False,
                     "description": "This tool takes no arguments."},
     "outputSchema": {
         "type": "object", "additionalProperties": True,
         "description": "Wash-adjusted settlement figures per chain.",
         "properties": {
             "clean": {
                 "type": "array",
                 "description": "One entry per chain.",
                 "items": {"type": "object", "properties": {
                     "chain": {"type": "string"},
                     "clean_settlements": {"type": "integer"},
                     "clean_volume_usd": {"type": "number"},
                     "as_of": {"type": "string",
                               "description": "Measurement date (UTC)."}}}},
             "note": {"type": "string",
                      "description": "Scope and method pointer."},
             "error": {"type": "string",
                       "description": "Present only on backend failure."}}}},
    {"name": "trust_score",
     "description": "Score a specific seller wallet before paying it: "
                    "wash-aware m.Score with an A–F grade, component "
                    "breakdown, wash flags including multi-hop cycle "
                    "detection, and the snapshot SHA so the result can be "
                    "re-derived independently. Requires chain and address. "
                    "Free — no key or payment needed. Optional: pass "
                    "payment=<base64 X-PAYMENT payload> to pay "
                    f"${x402pay.PRICE_USD:g} via x402 and receive a "
                    "settlement-receipted response.",
     "annotations": {"title": "seller trust score", "readOnlyHint": True,
                     "destructiveHint": False, "idempotentHint": True,
                     "openWorldHint": False},
     "inputSchema": {"type": "object", "required": ["chain", "address"],
                     "properties": {
                         "chain": {"type": "string",
                                   "description": "Chain the seller settles "
                                                  "on, e.g. base, polygon, "
                                                  "solana."},
                         "address": {"type": "string",
                                     "description": "Seller wallet address "
                                                    "to score."},
                         "api_key": {"type": "string",
                                     "description": "merona API key. "
                                                    "Alternative to per-call "
                                                    "x402 payment."},
                         "payment": {"type": "string",
                                     "description": "Base64 X-PAYMENT "
                                                    "payload for a single "
                                                    "x402 payment."}}},
     "outputSchema": {
         "type": "object", "additionalProperties": True,
         "description": "The seller's score, or x402 payment instructions "
                        "when called without api_key/payment.",
         "properties": {
             "grade": {"type": ["string", "null"],
                       "description": "A–F letter; null means unrated "
                                      "(evidence gate, not an adverse "
                                      "signal)."},
             "score": {"type": ["number", "null"],
                       "description": "Numeric m.Score behind the letter."},
             "tier": {"type": "string",
                      "description": "unrated | provisional | verified."},
             "suggested_action": {
                 "type": "object",
                 "description": "Deterministic action hint (PROCEED/CAUTION/"
                                "INVESTIGATE/DECLINE/INSUFFICIENT_EVIDENCE) "
                                "with reasons and rule version."},
             "components": {"type": "object",
                            "description": "Score component breakdown."},
             "snapshot_sha256": {
                 "type": "string",
                 "description": "Snapshot hash for independent "
                                "re-derivation."},
             "delivery": {
                 "type": "object",
                 "description": "Paid-probe delivery evidence, when merona "
                                "has bought from this seller: status "
                                "delivery_verified (dated) or "
                                "charged_unserved (payment settled, request "
                                "refused; settlement tx included). This "
                                "score covers base/polygon/solana, but "
                                "delivery evidence is base-only today "
                                "(see PROBE_COVERAGE_CHAINS) — null here on "
                                "polygon/solana means no probe was possible, "
                                "not a clean record."},
             "payment_required": {
                 "type": "object",
                 "description": "x402 payment instructions; present only "
                                "when the call is unpaid."},
             "error": {"type": "string",
                       "description": "Present only on failure."}}}},
]
MCP_MANIFEST = {
    "name": "merona-mcp",
    "description": "merona — the x402 settlement index. See what actually "
                   "settles: wash-adjusted volume per chain after funded "
                   "loops are removed, plus payTo payout-integrity checks. "
                   "Three tools free with no key and no signup; m.Score "
                   "seller grades paid via x402 or API key.",
    "endpoint": "/mcp",
    "transport": "streamable-http",
    "protocolVersion": MCP_PROTOCOL,
    "tools": [{"name": t["name"], "description": t["description"]}
              for t in MCP_TOOLS],
    "site": "https://merona.io",
    "free_feed": "https://merona.io/mismatches.json",
}

# Served at /.well-known/glama.json so the Glama directory listing can be
# claimed. Glama matches this against the email on the Glama account, which
# for a GitHub sign-in is whatever GitHub handed over — not necessarily the
# published contact address. GLAMA_MAINTAINER_EMAIL takes a comma-separated
# list so a second address can be added without a code change.
# This file is world-readable and harvesters walk /.well-known/, so prefer
# changing the directory account's email over publishing a personal one.
GLAMA_MAINTAINER_EMAIL = os.environ.get("GLAMA_MAINTAINER_EMAIL",
                                        "hello@merona.io")
GLAMA_WELL_KNOWN = {
    "$schema": "https://glama.ai/mcp/schemas/connector.json",
    "maintainers": [{"email": e.strip()}
                    for e in GLAMA_MAINTAINER_EMAIL.split(",") if e.strip()],
}

# --- GET /.well-known/merona-crawlers.json ------------------------------------
# x402-foundation/x402#2300 commitment #3: a canonical, machine-readable
# registry of every outbound UA merona's own tooling sends. The other
# operator in #2300 had to reverse-engineer which of FIVE user-agents hitting
# his endpoint were merona's; this makes that a lookup instead of a guess.
#
# Every literal below was found by grepping the tree for outbound
# `User-Agent`/`user_agent`/`USER_AGENT` assignments (not written from
# memory) — indexer/tests/test_merona_crawlers.py re-reads each cited
# `source` file and asserts the literal is actually there, so this registry
# can't silently drift from the code that sends it.
#
# INCLUSION CRITERION for `user_agents` (the list a seller should actually
# consult): an outbound request that (a) can land in an x402 SELLER's, a
# catalog/marketplace's, or a rival TRACKER's access log, AND (b) either
# self-identifies as merona, or is one of the "x402-endpoint-quality-audit" /
# "x402-audit-indexer" family this project has publicly run probes under.
# Two things are deliberately NOT in that list, and not merely folded into
# `rpc_or_internal` either — they never belong in a CRAWLER registry at all:
#   * `server_version` strings (merona-trust/1.0, merona-canary/0.1,
#     merona-stats/1.0, sift/0.1, pulse/0.1) — these label OUR responses to
#     inbound requests (the HTTP `Server:` header), the opposite direction
#     from everything else here. A seller never receives one.
#   * ventures/pulse's `pulse-monitor/0.1` and ventures/receipt's
#     `receipt-probe/0.1` — real outbound probes, but neither literal
#     contains "merona" or claims this identity, so a seller confused about
#     "is this merona?" was never going to suspect them either; claiming them
#     here would be inaccurate, not helpful.
#   * merona-outreach/1.0 (outreach/send.py) — its one call target is the
#     Resend transactional-email API (a vendor merona pays), never a seller.
#   * the wayback harvester (triangulation/wayback/harvest.py) — its only
#     target is archive.org, a third-party archive, not a seller or tracker.
#
# `rpc_or_internal`: real outbound UAs, "merona"-branded or from the audited
# family, that we still exclude from the seller-facing list because their
# only targets are blockchain RPC nodes / block explorers / attestation
# infra, OR merona's own live API (self-targeting conformance/regression
# tooling) — none of which is a seller who'd need this registry either, but
# listed (not silently dropped) so the registry is still a complete map of
# "everything that says merona" for anyone auditing the claim.
CRAWLERS = {
    "provider": "merona",
    "contact": "hello@merona.io",
    "policy": "read-only probes never pay; the paid sweep pays exactly once "
             "per seller and identifies itself; no hammering/retries",
    "user_agents": [
        {"ua": "merona-collectors/0.1 (read-only research; hello@merona.io)",
         "purpose": "registry/directory/catalog collectors (foundation, "
                    "mcpdirs, sdkstats, agent402, rivals, facwatch, names, "
                    "facstatus, ardcat, farcaster — ardcat also requests "
                    "/.well-known/ai-catalog.json; the same literal is "
                    "embedded, prefixed with a browser UA, in okxasp.py, "
                    "see the entry below)",
         "pays": False, "source": "collectors/foundation.py"},
        {"ua": "merona-collectors/0.1 (independent research; read-only; "
               "contact: hello@merona.io)",
         "purpose": "nightly Bazaar catalog/quote/diff snapshot collector",
         "pays": False, "source": "collectors/collect.py"},
        {"ua": "Mozilla/5.0 (X11; Linux x86_64) merona-collectors/0.1 "
               "(read-only research; hello@merona.io)",
         "purpose": "okx.ai ASP-catalog watch — polite HTML fetching of "
                    "public pages; the browser-style prefix is there "
                    "because okx.ai's blind /api paths are WAF'd, not to "
                    "hide the merona-collectors identity riding inside it",
         "pays": False, "source": "collectors/okxasp.py"},
        {"ua": "merona-pulse/0.1 (x402 shape survey; hello@merona.io)",
         "purpose": "x402 body-shape survey (x402Version/scheme/network "
                    "drift) over the deterministic endpoint rotation",
         "pays": False, "source": "collectors/v2watch.py"},
        {"ua": "x402-index-liveness/1.0 (+read-only probe)",
         "purpose": "nightly liveness + 402-shape probe; never sends "
                    "X-PAYMENT",
         "pays": False, "source": "indexer/instrumentation.py"},
        {"ua": "x402-endpoint-quality-audit/0.1 (independent research; "
               "read-only; contact: audit maintainer via repo)",
         "purpose": "Phase-2 UNPAID liveness sweep (pipeline/liveness_"
                    "probe.py) — same UA prefix as the PAID pilot below "
                    "but a different literal and pays:false; check the "
                    "full string, not just the prefix",
         "pays": False, "source": "pipeline/config.yaml"},
        {"ua": "x402-endpoint-quality-audit/0.1 (paid delivery verification "
               "pilot; one payment per endpoint)",
         "purpose": "Phase-2.5 paid-delivery pilot sweep (pipeline/paid/"
                    "paid_probe.mjs) — one settled payment per endpoint, "
                    "ever; ledgered so a re-run cannot double-pay",
         "pays": True, "source": "pipeline/paid/paid_probe.mjs"},
        {"ua": "merona-payprobe/0.1 (genuine paid probe; hello@merona.io)",
         "purpose": "Phase-3 genuine-payer probe (attest/payprobe.py) — "
                    "the LIVE ongoing paid prober: its receipts ledger "
                    "(data/indexer/payprobe/receipts.jsonl) is exactly what "
                    "GET /v1/delivery and the charged_unserved/delivery_"
                    "verified evidence above are built from",
         "pays": True, "source": "attest/payprobe.py"},
        {"ua": "merona-contact/0.1 (operator outreach; hello@merona.io)",
         "purpose": "post-sweep contact-surface reconnaissance on sellers "
                    "that need notifying (402 body, /.well-known/"
                    "security.txt, landing page) — read-only, never writes",
         "pays": False, "source": "attest/contact_harvest.py"},
        # The x402-audit-indexer family: the registry lists each EXACT
        # literal a log would show, not just the shared prefix — the whole
        # point is that an operator greps their access log and finds the
        # verbatim string here (2026-08-12 audit: the two suffixed variants
        # were previously uncited and the bare form cited the wrong file).
        {"ua": "x402-audit-indexer/1.0",
         "purpose": "default UA for the Base/Solana chain-read clients — "
                    "hits RPC providers, not sellers",
         "pays": False, "source": "indexer/chain.py"},
        {"ua": "x402-audit-indexer/1.0 (reconciliation)",
         "purpose": "daily cross-tracker reconciliation fetches of "
                    "x402scan's public pages — read-only",
         "pays": False, "source": "indexer/reconcile.py"},
        {"ua": "x402-audit-indexer/1.0 (surplus-validation)",
         "purpose": "spot-validation of surplus settlement attribution "
                    "against x402scan's public pages — read-only",
         "pays": False, "source": "indexer/validate_surplus.py"},
        {"ua": "x402-index-recon/1.0 (+daily reconciliation)",
         "purpose": "daily cross-tracker reconciliation against x402scan's "
                    "public pages (reachability + best-effort headline "
                    "numbers) — read-only",
         "pays": False, "source": "indexer/instrumentation.py"},
    ],
    "rpc_or_internal": [
        {"ua": "merona-chainsignals-gas/1.0", "reason": "rpc (HyperSync)",
         "source": "triangulation/chainsignals/gas_extract.py"},
        {"ua": "merona-onramp-hopfetch/1.0", "reason": "rpc (HyperSync)",
         "source": "triangulation/onramp/hop_fetch.py"},
        {"ua": "merona-backfill/1.0", "reason": "rpc (HyperSync)",
         "source": "indexer/backfill_extract_hypersync.py"},
        {"ua": "merona-wash-history/1.0", "reason": "rpc (HyperSync)",
         "source": "analytics/wash_history.py"},
        {"ua": "merona-tempo-index/1.0", "reason": "rpc (Tempo L1 public "
                                                    "RPC)",
         "source": "tempo/extract_tempo.py"},
        {"ua": "merona-verify-evidence/1.0",
         "reason": "rpc (Base) + self-published anchors repo on GitHub",
         "source": "conformance/verify_evidence.py"},
        {"ua": "merona-conformance-checker/1.0",
         "reason": "self-targeting — exercises merona's own live API, "
                   "never a third party",
         "source": "conformance/check.py"},
        {"ua": "x402-index-wash/1.0 (+read-only analysis)",
         "reason": "rpc", "source": "indexer/wash.py"},
        {"ua": "x402-index-payer-kinds/1.0 (+read-only analysis)",
         "reason": "rpc", "source": "indexer/enrich_payer_kinds.py"},
        {"ua": "x402-index-unitecon/1.0", "reason": "rpc",
         "source": "indexer/instrumentation.py"},
        {"ua": "x402-endpoint-quality-audit/0.1",
         "reason": "rpc (Base block explorer) — settlement-truth "
                  "reconciliation for the paid-probe ledger, not a seller "
                  "fetch despite sharing the family prefix",
         "source": "pipeline/paid/reconcile_chain.mjs"},
        {"ua": "merona-attest/0.1 (hello@merona.io)",
         "reason": "rpc/EAS attestation infra (identity_attest.py, "
                  "revoke.py, register_schema.py, anchor_attest.py, "
                  "basename.py) — publishing merona's own on-chain "
                  "attestations, never a seller fetch",
         "source": "attest/identity_attest.py"},
    ],
    "note": "canonical registry; traffic claiming merona that matches none "
           "of these can be verified via contact",
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
        # FREE BY DEFAULT (2026-08-15): adoption is the scarce resource at
        # this stage, not revenue — the observed effect of the 402 gate was
        # users walking, not paying. The x402 PAID PATH stays alive at a
        # nominal price for agents that want an on-chain receipt (and so
        # merona remains a functioning x402 seller on the rails it audits):
        # attach `payment` and the verify+settle+receipt flow runs exactly
        # as before. Flip X402_FREE_MODE=0 to restore the hard gate.
        if FREE_MODE and not pay:
            code, obj = trust_lookup(chain, addr)
            if code != 200:
                return {"error": obj, "status": code}
            obj["pricing"] = _free_pricing_note()
            return obj
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


# ThreadingHTTPServer spawns one unbounded thread per connection. The
# per-identity rate limiter throttles REQUESTS but not concurrent CONNECTIONS,
# so a burst of slow/held connections (e.g. many hitting the paid lane's
# facilitator round-trip) could exhaust threads/memory. Cap in-flight worker
# threads; excess connections are closed immediately (Caddy, the only client
# since we bind loopback, sees a dropped conn and can 502/retry) — a bounded
# backstop, not a substitute for a reverse-proxy connection limit.
MAX_INFLIGHT = int(os.environ.get("TRUST_API_MAX_INFLIGHT", "96"))


class _BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    _sem = threading.BoundedSemaphore(MAX_INFLIGHT)

    def process_request(self, request, client_address):
        if not self._sem.acquire(blocking=False):
            print("[trust-api] max in-flight reached — shedding a connection",
                  file=sys.stderr, flush=True)
            try:
                self.shutdown_request(request)
            except Exception:
                pass
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._sem.release()


def main() -> None:
    mode = (f"{len(KEYS)} API key(s)" if KEYS
            else "ANONYMOUS mode (per-IP limits)")
    print(f"[trust-api] {BIND_HOST}:{PORT} · {mode} · {RPM:.0f} rpm "
          f"burst {BURST:.0f} · max-inflight {MAX_INFLIGHT} · "
          f"usage → {USAGE_PATH}", flush=True)
    _BoundedServer((BIND_HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""x402 trust-provider conformance checker for merona's /v1/trust/evaluate
(x402#2299 twzrd draft-02) — ZERO-DEPENDENCY: Python 3 stdlib only, no
`requests`, no `cryptography`, and NO import of this repo's own code. That
last constraint is deliberate: this script is meant to be handed to a THIRD
PARTY (an agent operator, a competing implementation, an auditor) who has
none of merona's source tree and no reason to trust it blindly — it must run
standalone from a single file against nothing but the public HTTP surface.

Usage:
    python3 conformance/check.py https://api.merona.io
    python3 conformance/check.py http://localhost:8402       # local box

    python3 conformance/check.py --offline response.json       # one saved
    python3 conformance/check.py --offline response.json --jwks jwks.json
        # shape + envelope check of a single saved response body, no
        # network at all; --jwks (a local file OR a URL) additionally lets
        # the signature itself be cryptographically verified offline.

What it checks, in order:
  1. GET /.well-known/x402-trust-provider — required descriptor fields.
  2. GET /v1/spec — the four evidence classes (curated/behavioral/
     community/attested) are all documented.
  3. GET /.well-known/jwks.json — fetched once, cached, used below.
  4. Every vector in conformance/vectors.json: POST it, check the HTTP
     status, check any vector-specific assertions, and (for every 200
     response) the BASELINE_SHAPE_CHECKS that apply to any conformant
     x402-trust-evaluation-v0.1 body regardless of what today's data says
     — decision enum, score-null-iff-null_reason, basis[] entry typing,
     the block-provenance rule (a FAIL must cite curated/behavioral
     evidence), evidence_uri's "sha256:" content-address prefix, and
     expires_at == issued_at + ttl.
  5. If a response carries an `envelope`, it is verified: card_ref is
     recomputed from the JCS-canonical bytes of the body MINUS the
     envelope, and the signature is checked against the public key named
     by the envelope's `kid` in jwks.json — using the pure-Python Ed25519
     verifier below (RFC 8032 §5.1.7). An ABSENT envelope is a WARN
     ("unsigned deployment"), never a FAIL: draft-02 does not require
     signing, and this checker validates any conformant provider, not
     only merona's.

Output: one PASS/FAIL/WARN line per check. Exit 0 iff there is no FAIL
(WARNs — an unsigned deployment, an unreachable jwks — do not fail the run).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import sys
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
USER_AGENT = "merona-conformance-checker/1.0"
TIMEOUT_S = 15

EVIDENCE_CLASSES = {"curated", "behavioral", "community", "attested"}
DECISIONS = {"PASS", "FAIL", "UNCERTAIN"}
CANONICALISATION_PIN = "urn:x402:canonicalisation:jcs-rfc8785-v1"


# =============================================================================
# JCS (RFC 8785) canonicaliser — INTENTIONALLY DUPLICATED from serving/jcs.py,
# not imported. This script must run with nothing but the stdlib and nothing
# but itself: a verifier's trust in the signature check must not depend on
# trusting (or even having) the rest of merona's source tree. Any change to
# serving/jcs.py's canonicalisation rules needs a matching change here —
# indexer/tests/test_conformance_checker.py cross-checks the two stay in
# sync by running real payloads through both and comparing bytes.
# =============================================================================
_SHORT_ESCAPES = {
    0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r",
    0x22: '\\"', 0x5C: "\\\\",
}


def _jcs_encode_string(s: str) -> str:
    out = ['"']
    for ch in s:
        cp = ord(ch)
        esc = _SHORT_ESCAPES.get(cp)
        if esc is not None:
            out.append(esc)
        elif cp < 0x20:
            out.append(f"\\u{cp:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _jcs_number(x: float) -> str:
    # RFC 8785 §3.2.2.3 (ECMAScript Number::toString), same algorithm as
    # serving/jcs.py — see that module for the derivation/comments.
    if math.isnan(x) or math.isinf(x):
        raise ValueError(f"jcs: cannot canonicalise non-finite float {x!r}")
    if x == 0.0:
        return "0"
    sign = "-" if x < 0 else ""
    _s, digits, exp = Decimal(repr(abs(x))).as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exp += 1
    k = len(digits)
    n = exp + k
    s = "".join(str(d) for d in digits)
    if k <= n <= 21:
        body = s + "0" * (n - k)
    elif 0 < n <= 21:
        body = s[:n] + "." + s[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + s
    else:
        e = n - 1
        mantissa = s if k == 1 else s[0] + "." + s[1:]
        body = mantissa + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + body


def jcs_canonicalize(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _jcs_encode_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _jcs_number(value)
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(
            _jcs_encode_string(k) + ":" + jcs_canonicalize(v)
            for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(jcs_canonicalize(v) for v in value) + "]"
    raise TypeError(f"jcs: unsupported type {type(value)!r}")


def jcs_canonicalize_bytes(value) -> bytes:
    return jcs_canonicalize(value).encode("utf-8")


# =============================================================================
# Pure-Python Ed25519 — VERIFY ONLY (RFC 8032 §5.1.7), stdlib only (hashlib
# for SHA-512, big-int arithmetic via plain Python ints for the field/group
# math on edwards25519). No signing, no secret-key handling anywhere in this
# file: verification operates entirely on PUBLIC data (a public key and a
# signature someone else produced), so the constant-time-arithmetic concerns
# that make hand-rolled crypto risky for SIGNING do not apply here — an
# attacker learning "how long verification took" learns nothing they didn't
# already have (the public key and signature are already public). This is
# the classic reference algorithm (as published by the Ed25519 authors and
# mirrored in RFC 8032's worked description), trimmed to the decode+verify
# path only. Validated against RFC 8032 §7.1 test vectors — see
# indexer/tests/test_conformance_checker.py.
# =============================================================================
_ED_B = 256
_ED_Q = 2 ** 255 - 19


def _ed_H(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _ed_expmod(b: int, e: int, m: int) -> int:
    if e == 0:
        return 1
    t = _ed_expmod(b, e // 2, m) ** 2 % m
    if e & 1:
        t = (t * b) % m
    return t


def _ed_inv(x: int) -> int:
    return _ed_expmod(x, _ED_Q - 2, _ED_Q)


_ED_D = -121665 * _ed_inv(121666) % _ED_Q
_ED_I = _ed_expmod(2, (_ED_Q - 1) // 4, _ED_Q)


def _ed_xrecover(y: int) -> int:
    xx = (y * y - 1) * _ed_inv(_ED_D * y * y + 1)
    x = _ed_expmod(xx, (_ED_Q + 3) // 8, _ED_Q)
    if (x * x - xx) % _ED_Q != 0:
        x = (x * _ED_I) % _ED_Q
    if x % 2 != 0:
        x = _ED_Q - x
    return x


_ED_BY = 4 * _ed_inv(5)
_ED_BX = _ed_xrecover(_ED_BY)
_ED_BASE = (_ED_BX % _ED_Q, _ED_BY % _ED_Q)


def _ed_add(p, q_):
    x1, y1 = p
    x2, y2 = q_
    x3 = (x1 * y2 + x2 * y1) * _ed_inv(1 + _ED_D * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _ed_inv(1 - _ED_D * x1 * x2 * y1 * y2)
    return (x3 % _ED_Q, y3 % _ED_Q)


def _ed_scalarmult(p, e: int):
    if e == 0:
        return (0, 1)
    q_ = _ed_scalarmult(p, e // 2)
    q_ = _ed_add(q_, q_)
    if e & 1:
        q_ = _ed_add(q_, p)
    return q_


def _ed_bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def _ed_decodeint(s: bytes) -> int:
    return sum(2 ** i * _ed_bit(s, i) for i in range(_ED_B))


def _ed_is_on_curve(p) -> bool:
    x, y = p
    return (-x * x + y * y - 1 - _ED_D * x * x * y * y) % _ED_Q == 0


def _ed_decodepoint(s: bytes):
    y = sum(2 ** i * _ed_bit(s, i) for i in range(_ED_B - 1))
    x = _ed_xrecover(y)
    if x & 1 != _ed_bit(s, _ED_B - 1):
        x = _ED_Q - x
    p = (x, y)
    if not _ed_is_on_curve(p):
        raise ValueError("Ed25519 point decode: not on curve")
    return p


def _ed_encodepoint(p) -> bytes:
    x, y = p
    bits = [(y >> i) & 1 for i in range(_ED_B - 1)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8))
                 for i in range(_ED_B // 8))


def _ed_hint(m: bytes) -> int:
    h = _ed_H(m)
    return sum(2 ** i * _ed_bit(h, i) for i in range(2 * _ED_B))


def ed25519_verify(signature: bytes, message: bytes, public_key: bytes) -> bool:
    """RFC 8032 §5.1.7 verify. Returns False for a bad signature/point
    rather than letting a malformed point decode raise past the caller —
    the ONLY thing this function promises is a bool."""
    if len(signature) != _ED_B // 4 or len(public_key) != _ED_B // 8:
        return False
    try:
        r = _ed_decodepoint(signature[:_ED_B // 8])
        a = _ed_decodepoint(public_key)
    except ValueError:
        return False
    s = _ed_decodeint(signature[_ED_B // 8:_ED_B // 4])
    if s >= 2 ** 252 + 27742317777372353535851937790883648493:   # >= group order l
        return False           # RFC 8032: reject non-canonical S
    h = _ed_hint(_ed_encodepoint(r) + public_key + message)
    x1, y1 = _ed_scalarmult(_ED_BASE, s)
    x2, y2 = _ed_add(r, _ed_scalarmult(a, h))
    return x1 == x2 and y1 == y2


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# =============================================================================
# reporting
# =============================================================================
class Result:
    __slots__ = ("name", "status", "detail")

    def __init__(self, name: str, status, detail: str = ""):
        # `status` may be a bool (True/False -> PASS/FAIL) or an explicit
        # "PASS"/"FAIL"/"WARN" string (WARN has no boolean equivalent).
        if isinstance(status, str):
            self.status = status
        else:
            self.status = "PASS" if status else "FAIL"
        self.name = name
        self.detail = detail

    def line(self) -> str:
        tail = f" — {self.detail}" if self.detail else ""
        return f"[{self.status:4}] {self.name}{tail}"


# =============================================================================
# HTTP (urllib only)
# =============================================================================
class FetchError(Exception):
    pass


def _http(method: str, url: str, body: bytes | None = None,
         timeout: int = TIMEOUT_S):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent", USER_AGENT)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        # A non-2xx is still a valid, inspectable response for this checker
        # (e.g. the malformed-JSON vector expects 400) — not a fetch failure.
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise FetchError(f"{method} {url}: {e.reason}") from e


def _get_json(url: str):
    status, body = _http("GET", url)
    return status, json.loads(body.decode("utf-8"))


# =============================================================================
# descriptor + spec checks
# =============================================================================
REQUIRED_DESCRIPTOR_FIELDS = (
    "provider", "description", "endpoint", "directions", "evidence_classes",
    "decisions", "score", "freshness", "spec_url", "docs", "pricing", "chains",
)
REQUIRED_ENDPOINT_FIELDS = ("method", "url", "request_schema", "response_schema")
REQUIRED_ENVELOPE_DESCRIPTOR_FIELDS = ("canonicalisation", "alg", "jwks_url")


def check_descriptor(base_url: str) -> list[Result]:
    results = []
    try:
        status, body = _get_json(base_url + "/.well-known/x402-trust-provider")
    except (FetchError, json.JSONDecodeError) as e:
        return [Result("descriptor.fetch", False, str(e))]
    results.append(Result("descriptor.http_200", status == 200, f"got {status}"))
    if status != 200 or not isinstance(body, dict):
        return results
    missing = [f for f in REQUIRED_DESCRIPTOR_FIELDS if f not in body]
    results.append(Result("descriptor.required_fields", not missing,
                          f"missing {missing}" if missing else ""))
    endpoint = body.get("endpoint") or {}
    missing_ep = [f for f in REQUIRED_ENDPOINT_FIELDS if f not in endpoint]
    results.append(Result("descriptor.endpoint_fields", not missing_ep,
                          f"missing {missing_ep}" if missing_ep else ""))
    classes = set(body.get("evidence_classes") or {})
    results.append(Result("descriptor.evidence_classes",
                          classes == EVIDENCE_CLASSES,
                          f"got {sorted(classes)}, want {sorted(EVIDENCE_CLASSES)}"))
    decisions = set(body.get("decisions") or [])
    results.append(Result("descriptor.decisions",
                          decisions == DECISIONS,
                          f"got {sorted(decisions)}, want {sorted(DECISIONS)}"))
    # `envelope` is OPTIONAL at the protocol level (draft-02 doesn't require
    # signing) — absence is a WARN, not a FAIL, so this checker still passes
    # a conformant-but-unsigned provider.
    envelope = body.get("envelope")
    if envelope is None:
        results.append(Result("descriptor.envelope_section", "WARN",
                              "no `envelope` section — provider does not "
                              "advertise signed cards"))
    else:
        missing_env = [f for f in REQUIRED_ENVELOPE_DESCRIPTOR_FIELDS
                       if f not in envelope]
        results.append(Result("descriptor.envelope_section", not missing_env,
                              f"missing {missing_env}" if missing_env else ""))
        if not missing_env:
            results.append(Result(
                "descriptor.envelope_canonicalisation_pin",
                envelope["canonicalisation"] == CANONICALISATION_PIN,
                envelope["canonicalisation"]))
    return results


def check_spec(base_url: str) -> list[Result]:
    results = []
    try:
        status, body = _get_json(base_url + "/v1/spec")
    except (FetchError, json.JSONDecodeError) as e:
        return [Result("spec.fetch", False, str(e))]
    results.append(Result("spec.http_200", status == 200, f"got {status}"))
    if status != 200 or not isinstance(body, dict):
        return results
    try:
        classes = set(body["response"]["basis"]["classes"])
    except (KeyError, TypeError):
        classes = set()
    results.append(Result("spec.four_evidence_classes",
                          classes == EVIDENCE_CLASSES,
                          f"got {sorted(classes)}, want {sorted(EVIDENCE_CLASSES)}"))
    try:
        decision_enum = set(body["response"]["decision"]["enum"])
    except (KeyError, TypeError):
        decision_enum = set()
    results.append(Result("spec.decision_enum",
                          decision_enum == DECISIONS,
                          f"got {sorted(decision_enum)}"))
    return results


def fetch_jwks(base_url: str) -> dict | None:
    """None means "could not fetch" (network error) — distinct from a
    successfully-fetched {"keys": []} (signing disabled), which is a valid,
    fetchable document."""
    try:
        status, body = _get_json(base_url + "/.well-known/jwks.json")
    except (FetchError, json.JSONDecodeError):
        return None
    if status != 200 or not isinstance(body, dict) or "keys" not in body:
        return None
    return body


# =============================================================================
# baseline shape checks — apply to ANY 200 x402-trust-evaluation-v0.1 body,
# regardless of vector; robust to whatever today's scoring data says.
# =============================================================================
def baseline_shape_checks(obj: dict, prefix: str) -> list[Result]:
    results = []
    results.append(Result(f"{prefix}.schema_id",
                          obj.get("schema") == "x402-trust-evaluation-v0.1",
                          f"schema={obj.get('schema')!r}"))
    decision = obj.get("decision")
    results.append(Result(f"{prefix}.decision_enum", decision in DECISIONS,
                          f"decision={decision!r}"))
    score = obj.get("score")
    has_null_reason = "null_reason" in obj
    results.append(Result(
        f"{prefix}.score_null_iff_null_reason",
        (score is None) == has_null_reason,
        f"score={score!r} null_reason_present={has_null_reason}"))
    basis = obj.get("basis")
    basis_ok = isinstance(basis, list) and all(
        isinstance(e, dict)
        and e.get("class") in EVIDENCE_CLASSES
        and isinstance(e.get("kind"), str)
        and (e.get("ref") is None or isinstance(e.get("ref"), str))
        and (e.get("observed_at") is None or isinstance(e.get("observed_at"), str))
        for e in basis) if isinstance(basis, list) else False
    results.append(Result(f"{prefix}.basis_entry_shape", basis_ok,
                          f"basis={basis!r}"))
    if decision == "FAIL":
        fail_ok = isinstance(basis, list) and any(
            isinstance(e, dict) and e.get("class") in ("curated", "behavioral")
            for e in basis)
        results.append(Result(f"{prefix}.fail_requires_curated_or_behavioral",
                              fail_ok, f"basis classes="
                              f"{[e.get('class') for e in basis] if isinstance(basis, list) else None}"))
    evidence_uri = obj.get("evidence_uri")
    if evidence_uri is not None:
        ok = (isinstance(evidence_uri, str)
              and evidence_uri.startswith("sha256:")
              and len(evidence_uri) == len("sha256:") + 64)
        results.append(Result(f"{prefix}.evidence_uri_sha256_prefix", ok,
                              f"evidence_uri={evidence_uri!r}"))
    issued, expires, ttl = obj.get("issued_at"), obj.get("expires_at"), obj.get("ttl")
    if issued and expires and isinstance(ttl, (int, float)):
        try:
            delta = ((datetime.fromisoformat(expires)
                     - datetime.fromisoformat(issued)).total_seconds())
            ok = abs(delta - ttl) < 1e-6
        except ValueError:
            ok = False
        results.append(Result(f"{prefix}.expires_equals_issued_plus_ttl", ok,
                              f"issued_at={issued} expires_at={expires} ttl={ttl}"))
    results.append(Result(f"{prefix}.provider_is_merona",
                          obj.get("provider") == "merona",
                          f"provider={obj.get('provider')!r}"))
    results.append(Result(f"{prefix}.disclaimer_nonempty",
                          bool(obj.get("disclaimer")), ""))
    return results


def envelope_checks(obj: dict, jwks: dict | None, prefix: str) -> list[Result]:
    """Verify the `envelope` block per _build_envelope's documented recipe
    (serving/trust_api.py): strip envelope, JCS-canonicalise the rest,
    confirm card_ref, then Ed25519-verify signature against the public key
    named by `kid` in jwks.json. Absent envelope -> single WARN, not a
    failure (draft-02 does not require signing)."""
    if "envelope" not in obj:
        return [Result(f"{prefix}.envelope", "WARN",
                       "no envelope field — unsigned deployment")]
    results = []
    env = obj["envelope"]
    required = ("canonicalisation", "alg", "kid", "card_ref", "signature",
               "public_key_url")
    missing = [f for f in required if f not in env]
    if missing:
        results.append(Result(f"{prefix}.envelope.fields", False,
                              f"missing {missing}"))
        return results
    results.append(Result(f"{prefix}.envelope.canonicalisation_pin",
                          env["canonicalisation"] == CANONICALISATION_PIN,
                          env["canonicalisation"]))
    results.append(Result(f"{prefix}.envelope.alg", env["alg"] == "Ed25519",
                          env["alg"]))
    body_without_envelope = {k: v for k, v in obj.items() if k != "envelope"}
    try:
        canonical = jcs_canonicalize_bytes(body_without_envelope)
    except (TypeError, ValueError) as e:
        results.append(Result(f"{prefix}.envelope.canonicalise", False, str(e)))
        return results
    expected_ref = "sha256:" + hashlib.sha256(canonical).hexdigest()
    results.append(Result(f"{prefix}.envelope.card_ref_matches",
                          env["card_ref"] == expected_ref,
                          f"expected {expected_ref}, got {env['card_ref']}"))
    if jwks is None:
        results.append(Result(f"{prefix}.envelope.signature", "WARN",
                              "jwks.json unavailable — signature not "
                              "cryptographically verified"))
        return results
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == env["kid"]),
              None)
    if key is None:
        results.append(Result(f"{prefix}.envelope.kid_in_jwks", False,
                              f"kid {env['kid']!r} not in jwks.json"))
        return results
    try:
        pub_raw = _b64url_decode(key["x"])
        sig_raw = _b64url_decode(env["signature"])
        ok = len(pub_raw) == 32 and ed25519_verify(sig_raw, canonical, pub_raw)
    except Exception as e:                              # noqa: BLE001
        results.append(Result(f"{prefix}.envelope.signature_verifies", False,
                              f"{type(e).__name__}: {e}"))
        return results
    results.append(Result(f"{prefix}.envelope.signature_verifies", ok,
                          "pure-Python Ed25519 verify (RFC 8032 §5.1.7) over "
                          "the JCS canonical bytes"))
    return results


# =============================================================================
# vector-driven checks
# =============================================================================
def _check_targeted(obj: dict, assertions: dict, prefix: str) -> list[Result]:
    results = []
    if "reason_code" in assertions:
        want = assertions["reason_code"]
        results.append(Result(f"{prefix}.reason_code", obj.get("reason_code") == want,
                              f"want {want!r}, got {obj.get('reason_code')!r}"))
    if "reason_code_not" in assertions:
        avoid = assertions["reason_code_not"]
        results.append(Result(f"{prefix}.reason_code_not",
                              obj.get("reason_code") != avoid,
                              f"got {obj.get('reason_code')!r}"))
    if "null_reason" in assertions:
        want = assertions["null_reason"]
        results.append(Result(f"{prefix}.null_reason", obj.get("null_reason") == want,
                              f"want {want!r}, got {obj.get('null_reason')!r}"))
    if "score_is_null" in assertions:
        want = assertions["score_is_null"]
        results.append(Result(f"{prefix}.score_is_null",
                              (obj.get("score") is None) == want,
                              f"score={obj.get('score')!r}"))
    if "direction" in assertions:
        want = assertions["direction"]
        results.append(Result(f"{prefix}.direction", obj.get("direction") == want,
                              f"want {want!r}, got {obj.get('direction')!r}"))
    if "basis_empty" in assertions:
        want = assertions["basis_empty"]
        results.append(Result(f"{prefix}.basis_empty",
                              (obj.get("basis") == []) == want,
                              f"basis={obj.get('basis')!r}"))
    if "decision_in" in assertions:
        want = assertions["decision_in"]
        results.append(Result(f"{prefix}.decision_in", obj.get("decision") in want,
                              f"decision={obj.get('decision')!r}, want one of {want}"))
    return results


def check_vector(base_url: str, vector: dict, jwks: dict | None) -> list[Result]:
    name = vector["name"]
    if "raw_body" in vector:
        body = vector["raw_body"].encode("utf-8")
    else:
        body = json.dumps(vector["request"]).encode("utf-8")
    try:
        status, raw = _http("POST", base_url + "/v1/trust/evaluate", body=body)
    except FetchError as e:
        return [Result(f"{name}.fetch", False, str(e))]

    expect = vector["expect"]
    results = [Result(f"{name}.status", status == expect["status"],
                      f"want {expect['status']}, got {status}")]
    if status != 200:
        return results          # nothing further to inspect for non-200

    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        results.append(Result(f"{name}.response_is_json", False, str(e)))
        return results
    if not isinstance(obj, dict):
        results.append(Result(f"{name}.response_is_object", False,
                              f"got {type(obj).__name__}"))
        return results

    results += baseline_shape_checks(obj, prefix=name)
    results += _check_targeted(obj, expect.get("assertions", {}), prefix=name)
    results += envelope_checks(obj, jwks, prefix=name)
    return results


# =============================================================================
# main
# =============================================================================
def run_online(base_url: str) -> list[Result]:
    base_url = base_url.rstrip("/")
    results = []
    results += check_descriptor(base_url)
    results += check_spec(base_url)
    jwks = fetch_jwks(base_url)
    results.append(Result("jwks.fetch", jwks is not None,
                          "" if jwks is not None else
                          "GET /.well-known/jwks.json unreachable/invalid — "
                          "envelope signatures below will WARN instead of "
                          "verifying"))
    vectors_path = HERE / "vectors.json"
    vectors = json.loads(vectors_path.read_text())["vectors"]
    for vector in vectors:
        results += check_vector(base_url, vector, jwks)
    return results


def run_offline(response_path: str, jwks_path: str | None) -> list[Result]:
    obj = json.loads(Path(response_path).read_text())
    if not isinstance(obj, dict):
        return [Result("offline.response_is_object", False,
                       f"got {type(obj).__name__}")]
    jwks = None
    if jwks_path:
        try:
            if jwks_path.startswith(("http://", "https://")):
                _status, jwks = _get_json(jwks_path)
            else:
                jwks = json.loads(Path(jwks_path).read_text())
        except (FetchError, json.JSONDecodeError, OSError) as e:
            return [Result("offline.jwks_fetch", False, str(e))]
    results = baseline_shape_checks(obj, prefix="offline")
    results += envelope_checks(obj, jwks, prefix="offline")
    return results


def main(argv: list[str]) -> int:
    args = argv[1:]
    if "--offline" in args:
        i = args.index("--offline")
        if i + 1 >= len(args):
            print("usage: check.py --offline response.json [--jwks jwks.json|url]",
                  file=sys.stderr)
            return 2
        response_path = args[i + 1]
        jwks_path = None
        if "--jwks" in args:
            j = args.index("--jwks")
            jwks_path = args[j + 1] if j + 1 < len(args) else None
        results = run_offline(response_path, jwks_path)
    else:
        if not args:
            print("usage: check.py <base_url>\n"
                  "       check.py --offline response.json [--jwks jwks.json|url]",
                  file=sys.stderr)
            return 2
        results = run_online(args[0])

    for r in results:
        print(r.line())
    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_warn = sum(1 for r in results if r.status == "WARN")
    n_pass = sum(1 for r in results if r.status == "PASS")
    print(f"\n{n_pass} passed, {n_warn} warned, {n_fail} failed "
         f"({len(results)} checks total)")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

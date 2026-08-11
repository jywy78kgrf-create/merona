"""conformance/check.py — the zero-dependency x402 trust-provider conformance
checker. check.py is deliberately NOT part of this repo's importable package
(it must run standalone, stdlib-only, with no dependency on merona's own
source tree — see its module docstring) — loaded here via importlib from its
file path, same technique the checker itself doesn't need but this test
does, to exercise its assertion functions directly without a running server.

Three things this file pins:
  1. The pure-Python Ed25519 verify function against RFC 8032 Section 7.1's
     own test vectors (hardcoded from ed25519.cr.yp.to/python/sign.input,
     the canonical machine-readable source for these — see the note above
     RFC8032_TEST_VECTORS for how to re-derive them if ever in doubt).
  2. That check.py's embedded JCS canonicaliser produces byte-identical
     output to serving/jcs.py's for the same payloads — the two are
     independent implementations (check.py's is intentionally duplicated,
     never imported, per its module docstring) and must not silently drift.
  3. A full sign-with-cryptography / verify-with-check.py's-pure-Ed25519
     round trip, and the shape-check functions against synthetic responses.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "serving"))

import jcs as real_jcs                                     # noqa: E402


def _load_check_module():
    spec = importlib.util.spec_from_file_location(
        "conformance_check", ROOT / "conformance" / "check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check = _load_check_module()


# ================================================================
# RFC 8032 Section 7.1 test vectors, pure-Python Ed25519 verify
# ================================================================
# Sourced directly from https://ed25519.cr.yp.to/python/sign.input (the
# machine-readable file RFC 8032's own worked test vectors come from — the
# RFC's rendered text wraps the 128-hex-char signature across lines in a way
# that is easy to mis-transcribe by hand; sign.input's colon-separated
# format has no such ambiguity). Each line is
# "sk32||pk32 : pk32 : message_hex : sig64||message_hex".
RFC8032_TEST_VECTORS = [
    # (public_key_hex, message_hex, signature_hex)  — vector 1: empty message
    ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901"
     "555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    # vector 2: 1-byte message
    ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69"
     "da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]


@pytest.mark.parametrize("pk_hex,msg_hex,sig_hex", RFC8032_TEST_VECTORS)
def test_ed25519_verify_matches_rfc8032_vectors(pk_hex, msg_hex, sig_hex):
    pk = bytes.fromhex(pk_hex)
    msg = bytes.fromhex(msg_hex)
    sig = bytes.fromhex(sig_hex)
    assert len(pk) == 32 and len(sig) == 64          # pin the vectors themselves
    assert check.ed25519_verify(sig, msg, pk) is True


@pytest.mark.parametrize("pk_hex,msg_hex,sig_hex", RFC8032_TEST_VECTORS)
def test_ed25519_verify_rejects_tampered_signature(pk_hex, msg_hex, sig_hex):
    pk = bytes.fromhex(pk_hex)
    msg = bytes.fromhex(msg_hex)
    sig = bytearray(bytes.fromhex(sig_hex))
    sig[0] ^= 0xFF
    assert check.ed25519_verify(bytes(sig), msg, pk) is False


@pytest.mark.parametrize("pk_hex,msg_hex,sig_hex", RFC8032_TEST_VECTORS)
def test_ed25519_verify_rejects_wrong_message(pk_hex, msg_hex, sig_hex):
    pk = bytes.fromhex(pk_hex)
    sig = bytes.fromhex(sig_hex)
    assert check.ed25519_verify(sig, b"not the message", pk) is False


def test_ed25519_verify_rejects_wrong_length_inputs():
    assert check.ed25519_verify(b"short", b"m", b"x" * 32) is False
    assert check.ed25519_verify(b"x" * 64, b"m", b"short") is False


# ================================================================
# cross-check: sign with `cryptography` (real Ed25519), verify with
# check.py's pure-Python implementation
# ================================================================
def test_cryptography_signed_message_verifies_with_pure_python_ed25519():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    message = b'{"decision":"PASS","score":0.92}'
    signature = priv.sign(message)
    assert check.ed25519_verify(signature, message, pub) is True


def test_cryptography_signed_message_fails_pure_python_verify_when_tampered():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    message = b'{"decision":"PASS","score":0.92}'
    signature = priv.sign(message)
    assert check.ed25519_verify(signature, message + b"x", pub) is False


# ================================================================
# check.py's embedded JCS matches serving/jcs.py (independent implementations)
# ================================================================
JCS_CROSS_CHECK_PAYLOADS = [
    {"b": 1, "a": 2},
    {"score": 0.92, "decision": "PASS", "provisional": False, "null_reason": None},
    {"nested": {"z": 1e30, "a": 0.49}, "arr": [3, 1, 2], "s": "é中\nquoted\""},
    1, 1.5, 0.49, 100.0, "plain string", None, True, False, [],
]


@pytest.mark.parametrize("payload", JCS_CROSS_CHECK_PAYLOADS)
def test_check_py_jcs_matches_serving_jcs(payload):
    assert check.jcs_canonicalize(payload) == real_jcs.canonicalize(payload)


# ================================================================
# assertion functions against synthetic responses
# ================================================================
def _valid_response(**overrides):
    resp = {
        "schema": "x402-trust-evaluation-v0.1",
        "decision": "PASS",
        "score": 0.9,
        "reason_code": "grade_pass",
        "direction": "seller",
        "basis": [{"class": "behavioral", "kind": "seller_score",
                   "ref": "sha256:" + "a" * 64, "observed_at": "2026-08-08"}],
        "evidence_uri": "sha256:" + "a" * 64,
        "evidence": {"snapshot_date": "2026-08-08"},
        "ttl": 21600,
        "issued_at": "2026-08-10T00:00:00+00:00",
        "expires_at": "2026-08-10T06:00:00+00:00",
        "provider": "merona",
        "disclaimer": "recorded facts, not an accusation",
    }
    resp.update(overrides)
    return resp


def test_baseline_shape_checks_all_pass_on_a_well_formed_response():
    results = check.baseline_shape_checks(_valid_response(), prefix="t")
    assert results, "expected at least one check to run"
    assert all(r.status == "PASS" for r in results), \
        [r.line() for r in results if r.status != "PASS"]


def test_baseline_shape_checks_catch_score_null_reason_mismatch():
    bad = _valid_response(score=None)          # null_reason missing
    results = check.baseline_shape_checks(bad, prefix="t")
    by_name = {r.name: r for r in results}
    assert by_name["t.score_null_iff_null_reason"].status == "FAIL"


def test_baseline_shape_checks_catch_bad_basis_entry_class():
    bad = _valid_response(basis=[{"class": "not_a_real_class", "kind": "x",
                                  "ref": None, "observed_at": None}])
    results = check.baseline_shape_checks(bad, prefix="t")
    by_name = {r.name: r for r in results}
    assert by_name["t.basis_entry_shape"].status == "FAIL"


def test_baseline_shape_checks_enforce_block_provenance_on_fail():
    # FAIL with only a community/attested basis violates draft-02's rule
    bad = _valid_response(decision="FAIL",
                          basis=[{"class": "attested", "kind": "eas_anchor",
                                 "ref": "sha256:" + "a" * 64,
                                 "observed_at": "2026-08-08"}])
    results = check.baseline_shape_checks(bad, prefix="t")
    by_name = {r.name: r for r in results}
    assert by_name["t.fail_requires_curated_or_behavioral"].status == "FAIL"


def test_baseline_shape_checks_pass_fail_with_curated_basis():
    ok = _valid_response(decision="FAIL", reason_code="grade_decline",
                         basis=[{"class": "curated", "kind": "first_party_probe",
                                "ref": "base:tx:0x" + "1" * 40,
                                "observed_at": "2026-08-08"}])
    results = check.baseline_shape_checks(ok, prefix="t")
    by_name = {r.name: r for r in results}
    assert by_name["t.fail_requires_curated_or_behavioral"].status == "PASS"


def test_baseline_shape_checks_catch_bad_evidence_uri_prefix():
    bad = _valid_response(evidence_uri="not-a-sha256-uri")
    results = check.baseline_shape_checks(bad, prefix="t")
    by_name = {r.name: r for r in results}
    assert by_name["t.evidence_uri_sha256_prefix"].status == "FAIL"


def test_baseline_shape_checks_catch_expires_not_equal_issued_plus_ttl():
    bad = _valid_response(expires_at="2026-10-10T06:00:00+00:00")  # way off
    results = check.baseline_shape_checks(bad, prefix="t")
    by_name = {r.name: r for r in results}
    assert by_name["t.expires_equals_issued_plus_ttl"].status == "FAIL"


def test_envelope_checks_warn_when_envelope_absent():
    results = check.envelope_checks(_valid_response(), jwks=None, prefix="t")
    assert len(results) == 1
    assert results[0].status == "WARN"


def test_envelope_checks_full_round_trip_with_synthetic_signed_response():
    """The end-to-end happy path: build a response, sign it exactly like
    _build_envelope() does (JCS canonical bytes, Ed25519, sha256 card_ref),
    then verify it with check.py's envelope_checks() + pure-Python Ed25519 —
    entirely offline, no server, no network."""
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    kid = hashlib.sha256(pub_raw).hexdigest()[:12]

    payload = _valid_response()
    canonical = real_jcs.canonicalize_bytes(payload)
    card_ref = "sha256:" + hashlib.sha256(canonical).hexdigest()
    signature = priv.sign(canonical)
    payload["envelope"] = {
        "canonicalisation": "urn:x402:canonicalisation:jcs-rfc8785-v1",
        "alg": "Ed25519", "kid": kid, "card_ref": card_ref,
        "signature": base64.urlsafe_b64encode(signature).rstrip(b"=").decode(),
        "public_key_url": "https://example.invalid/.well-known/jwks.json",
    }
    jwks = {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": kid,
                      "x": base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode(),
                      "use": "sig"}]}

    results = check.envelope_checks(payload, jwks, prefix="t")
    by_name = {r.name: r for r in results}
    assert by_name["t.envelope.card_ref_matches"].status == "PASS"
    assert by_name["t.envelope.signature_verifies"].status == "PASS"


def test_envelope_checks_detect_tampered_card():
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    kid = hashlib.sha256(pub_raw).hexdigest()[:12]

    payload = _valid_response()
    canonical = real_jcs.canonicalize_bytes(payload)
    card_ref = "sha256:" + hashlib.sha256(canonical).hexdigest()
    signature = priv.sign(canonical)
    payload["envelope"] = {
        "canonicalisation": "urn:x402:canonicalisation:jcs-rfc8785-v1",
        "alg": "Ed25519", "kid": kid, "card_ref": card_ref,
        "signature": base64.urlsafe_b64encode(signature).rstrip(b"=").decode(),
        "public_key_url": "https://example.invalid/.well-known/jwks.json",
    }
    jwks = {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": kid,
                      "x": base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode(),
                      "use": "sig"}]}
    payload["score"] = 0.01                       # tamper AFTER signing

    results = check.envelope_checks(payload, jwks, prefix="t")
    by_name = {r.name: r for r in results}
    # card_ref no longer matches the (now-different) canonical bytes...
    assert by_name["t.envelope.card_ref_matches"].status == "FAIL"
    # ...and the signature (over the OLD bytes) doesn't verify against the
    # NEW canonical bytes either.
    assert by_name["t.envelope.signature_verifies"].status == "FAIL"


def test_targeted_assertions_reason_code_and_decision_in():
    obj = _valid_response(reason_code="grade_pass", decision="PASS")
    results = check._check_targeted(
        obj, {"reason_code": "grade_pass", "decision_in": ["PASS", "FAIL"]},
        prefix="t")
    assert all(r.status == "PASS" for r in results)

    results_fail = check._check_targeted(
        obj, {"reason_code": "grade_marginal"}, prefix="t")
    assert results_fail[0].status == "FAIL"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

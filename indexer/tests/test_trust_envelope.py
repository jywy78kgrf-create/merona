"""serving/jcs.py (RFC 8785 canonicalisation) and the signed-card envelope
attached to POST /v1/trust/evaluate responses when X402_TRUST_SIGNING_KEY is
configured.

Follows test_trust_evaluate.py's convention of importing the real modules
directly (both import cleanly with no DB/network at import time). The
envelope tests generate an EPHEMERAL Ed25519 key in-test with `cryptography`
and monkeypatch the module's three signing globals directly — the least
invasive hook available (see _load_signing_key's docstring in trust_api.py)
— rather than writing a real key file to disk.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)
from cryptography.exceptions import InvalidSignature

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "indexer"))
sys.path.insert(0, str(ROOT / "serving"))

import jcs                                                 # noqa: E402
import trust_api as api                                    # noqa: E402


# ================================================================
# jcs.py — RFC 8785 canonicalisation unit tests
# ================================================================

# --- number serialization (RFC 8785 §3.2.2.3, ECMAScript Number::toString) --
@pytest.mark.parametrize("value,expected", [
    (1, "1"),                      # python int: no decimal point
    (0, "0"),
    (-1, "-1"),
    (1.5, "1.5"),
    (0.49, "0.49"),
    (1e30, "1e+30"),
    (100.0, "100"),                # whole-valued float: no trailing ".0"
    (3.0, "3"),
    (1e21, "1e+21"),                # JS switches to exponential at 1e21
    (1e20, "1" + "0" * 20),         # ...but not yet at 1e20 (plain form)
    (1e-6, "0.000001"),
    (1e-7, "1e-7"),                 # JS switches to exponential below 1e-6
    (0.0, "0"),
    (-0.0, "0"),                    # sign bit not observable in ES toString
    (-1.5, "-1.5"),
])
def test_number_serialization_matches_ecmascript_number_tostring(value, expected):
    assert jcs.canonicalize(value) == expected


def test_nan_and_infinity_are_rejected():
    with pytest.raises(ValueError):
        jcs.canonicalize(float("nan"))
    with pytest.raises(ValueError):
        jcs.canonicalize(float("inf"))
    with pytest.raises(ValueError):
        jcs.canonicalize(float("-inf"))


# --- key sorting (RFC 8785 §3.2.3: UTF-16 code unit order) ------------------
def test_object_keys_are_sorted():
    assert jcs.canonicalize({"b": 1, "a": 2, "c": 3}) == '{"a":2,"b":1,"c":3}'


def test_object_key_sorting_is_unicode_aware():
    # "é" (U+00E9 = 233) sorts after ascii "z" (122) by code point/UTF-16
    # code unit alike — both agree inside the BMP (see jcs.py's comment on
    # the one place they could diverge: supplementary-plane keys).
    obj = {"z": 1, "é": 2, "a": 3}
    assert jcs.canonicalize(obj) == '{"a":3,"z":1,"é":2}'


def test_nested_objects_sort_keys_at_every_level():
    obj = {"outer_b": {"y": 1, "x": 2}, "outer_a": 1}
    assert jcs.canonicalize(obj) == '{"outer_a":1,"outer_b":{"x":2,"y":1}}'


# --- string escaping (RFC 8785 §3.2.2.2) ------------------------------------
def test_string_escapes_control_chars_and_quote_backslash():
    assert jcs.canonicalize("a\nb\tc\"d\\e") == '"a\\nb\\tc\\"d\\\\e"'


def test_string_escapes_other_control_chars_as_u00xx():
    assert jcs.canonicalize("\x01\x1f") == '"\\u0001\\u001f"'


def test_string_leaves_non_ascii_as_literal_utf8_not_u_escaped():
    # The most common way a hand-rolled canonicaliser diverges from JCS:
    # json.dumps's default ensure_ascii=True would emit "é中" here.
    assert jcs.canonicalize("é中") == '"é中"'


def test_forward_slash_is_not_escaped():
    assert jcs.canonicalize("a/b") == '"a/b"'


# --- booleans / null / arrays -----------------------------------------------
def test_bool_is_not_serialized_as_an_integer():
    # bool is an int subclass in Python — this pins that True/False never
    # fall through to the int branch and come out as 1/0.
    assert jcs.canonicalize(True) == "true"
    assert jcs.canonicalize(False) == "false"
    assert jcs.canonicalize([True, False, 1, 0]) == "[true,false,1,0]"


def test_none_is_null():
    assert jcs.canonicalize(None) == "null"


def test_array_order_is_preserved_not_sorted():
    assert jcs.canonicalize([3, 1, 2]) == "[3,1,2]"


def test_canonicalize_bytes_is_utf8_of_canonicalize():
    obj = {"a": "é"}
    assert jcs.canonicalize_bytes(obj) == jcs.canonicalize(obj).encode("utf-8")


def test_full_shape_matches_hand_built_string():
    # A composite check exercising sorting + numbers + strings + nesting +
    # booleans + null together, the way a real trust-evaluate payload would.
    obj = {
        "schema": "x402-trust-evaluation-v0.1",
        "score": 0.92,
        "decision": "PASS",
        "provisional": False,
        "null_reason": None,
        "basis": [{"class": "curated", "ref": None}],
    }
    expected = (
        '{"basis":[{"class":"curated","ref":null}],"decision":"PASS",'
        '"null_reason":null,"provisional":false,"schema":'
        '"x402-trust-evaluation-v0.1","score":0.92}'
    )
    assert jcs.canonicalize(obj) == expected


# ================================================================
# envelope construction + verification (Ed25519 over JCS bytes)
# ================================================================
SELLER = "0x" + "ab" * 20


def _req():
    return json.dumps({
        "schema": "x402-trust-query-v0.1", "direction": "seller",
        "subject": {"wallet": SELLER, "chain": "base"},
    }).encode()


def _lookup_a_grade():
    def f(chain, addr):
        return 200, {
            "chain": "base", "seller": SELLER, "grade": "A", "score": 92,
            "provisional": False,
            "verification": {"snapshot_date": "2026-08-08",
                             "snapshot_sha256": "f" * 64},
            "suggested_action": {"action": "PROCEED"},
        }
    return f


@pytest.fixture
def signing_key(monkeypatch):
    """Ephemeral in-test Ed25519 key, wired into the module's signing
    globals directly (monkeypatch, no file I/O) — the least invasive of the
    two hooks the module supports (see _load_signing_key's docstring)."""
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat)
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    kid = hashlib.sha256(pub_raw).hexdigest()[:12]
    monkeypatch.setattr(api, "_SIGNING_KEY", priv)
    monkeypatch.setattr(api, "_SIGNING_PUB", pub_raw)
    monkeypatch.setattr(api, "_SIGNING_KID", kid)
    return priv, pub_raw, kid


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def test_signing_disabled_by_default_no_envelope_key():
    # No fixture applied: module-level globals are whatever import-time
    # left them (no X402_TRUST_SIGNING_KEY in this test process's env).
    assert api._SIGNING_KEY is None
    code, obj = api._trust_evaluate(_req(), lookup=_lookup_a_grade())
    assert code == 200
    assert "envelope" not in obj


def test_envelope_present_with_all_fields_when_signing_enabled(signing_key):
    priv, pub_raw, kid = signing_key
    code, obj = api._trust_evaluate(_req(), lookup=_lookup_a_grade())
    assert code == 200
    env = obj["envelope"]
    assert env["canonicalisation"] == "urn:x402:canonicalisation:jcs-rfc8785-v1"
    assert env["alg"] == "Ed25519"
    assert env["kid"] == kid
    assert env["card_ref"].startswith("sha256:")
    assert len(env["card_ref"]) == len("sha256:") + 64
    assert env["public_key_url"] == api.PUBLIC_BASE + "/.well-known/jwks.json"
    # base64url, no padding
    assert "=" not in env["signature"]
    _b64url_decode(env["signature"])       # must decode without error


def test_card_ref_matches_sha256_of_canonical_bytes_without_envelope(signing_key):
    code, obj = api._trust_evaluate(_req(), lookup=_lookup_a_grade())
    env = obj.pop("envelope")
    canonical = jcs.canonicalize_bytes(obj)
    expected = "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert env["card_ref"] == expected


def test_stripping_envelope_and_verifying_signature_succeeds(signing_key):
    priv, pub_raw, kid = signing_key
    code, obj = api._trust_evaluate(_req(), lookup=_lookup_a_grade())
    env = obj.pop("envelope")                      # verifier's step 1
    canonical = jcs.canonicalize_bytes(obj)         # step 2
    assert "sha256:" + hashlib.sha256(canonical).hexdigest() == env["card_ref"]  # step 3
    pubkey = Ed25519PublicKey.from_public_bytes(pub_raw)
    pubkey.verify(_b64url_decode(env["signature"]), canonical)   # step 4: no raise


def test_tampering_one_payload_field_breaks_verification(signing_key):
    priv, pub_raw, kid = signing_key
    code, obj = api._trust_evaluate(_req(), lookup=_lookup_a_grade())
    env = obj.pop("envelope")
    obj["score"] = 0.01                             # tamper AFTER signing
    tampered_canonical = jcs.canonicalize_bytes(obj)
    pubkey = Ed25519PublicKey.from_public_bytes(pub_raw)
    with pytest.raises(InvalidSignature):
        pubkey.verify(_b64url_decode(env["signature"]), tampered_canonical)


def test_jwks_empty_when_signing_disabled():
    assert api._SIGNING_KEY is None
    assert api._jwks() == {"keys": []}


def test_jwks_has_matching_kid_and_x_when_signing_enabled(signing_key):
    priv, pub_raw, kid = signing_key
    body = api._jwks()
    assert len(body["keys"]) == 1
    key = body["keys"][0]
    assert key == {
        "kty": "OKP", "crv": "Ed25519", "kid": kid,
        "x": api._b64url_nopad(pub_raw), "use": "sig",
    }
    # jwks.json's x round-trips to the exact public key bytes used to verify.
    assert _b64url_decode(key["x"]) == pub_raw


def test_malformed_request_400_never_carries_an_envelope(signing_key):
    # Envelope only makes sense for a decision body — a 400 has no card.
    code, obj = api._trust_evaluate(b"{not json")
    assert code == 400
    assert "envelope" not in obj


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

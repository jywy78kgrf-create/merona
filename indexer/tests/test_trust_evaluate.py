"""POST /v1/trust/evaluate — the x402-trust-query-v0.1 gate decision.

trust_api.py imports cleanly in this environment (the DB and x402pay's
facilitator calls are both lazy — nothing connects at import time), so this
follows test_payprobe.py's convention of importing the real module directly,
rather than test_suggested_action.py's regex/exec extraction (that one exists
specifically to dodge importing "the whole serving stack ... for a pure
function"; here the function under test already lives in that stack and is
tested through its own module).

`_trust_evaluate(body, lookup=...)` takes an injectable `lookup` in place of
the module's real `trust_lookup` (DB-backed), so every case below is a pure,
server-less, DB-less unit test: hand-built records in, wire-shape dict out.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "indexer"))
sys.path.insert(0, str(ROOT / "serving"))

import trust_api as api                                    # noqa: E402

SELLER = "0x" + "ab" * 20
# WHY no "anchors" key by default: most fixtures below care about the
# curated/behavioral shape only, not the attested entry — anchors is added
# explicitly (ANCHORED_VERIFICATION) in the handful of tests that exercise
# the attested-entry path, so those stay the one place a reader needs to
# think about it.
VERIFICATION = {"snapshot_date": "2026-08-08", "snapshot_sha256": "f" * 64}
ANCHORED_VERIFICATION = dict(VERIFICATION,
                             anchors="https://github.com/example/merona-anchors")


def _req(**kw):
    body = {"schema": "x402-trust-query-v0.1", "direction": "seller",
            "subject": {"wallet": SELLER, "chain": "base"}}
    body.update(kw)
    return json.dumps(body).encode()


def _record(grade=None, score=None, provisional=False, action="PROCEED",
           delivery=None, verification=None):
    r = {"chain": "base", "seller": SELLER, "grade": grade, "score": score,
         "provisional": provisional,
         "verification": dict(verification if verification is not None
                              else VERIFICATION),
         "suggested_action": {"action": action}}
    if delivery:
        r["delivery"] = delivery
    return r


def _lookup(status, record):
    def f(chain, addr):
        return status, record
    return f


# --- charged_unserved -----------------------------------------------------------
def test_charged_unserved_is_fail_first_party_probe():
    delivery = {"status": "charged_unserved", "as_of": "2026-08-07",
                "settlement_tx": "0x" + "11" * 32, "http_status": 402}
    record = _record(grade="A", score=92, action="INVESTIGATE",
                     delivery=delivery)
    code, obj = api._trust_evaluate(
        _req(), lookup=_lookup(200, record))
    assert code == 200
    assert obj["decision"] == "FAIL"
    assert obj["reason_code"] == "charged_unserved"
    # typed basis[]: a curated first-party-probe entry with a chain-prefixed
    # tx ref (block-provenance: FAIL rests on curated evidence here)
    assert obj["basis"] == [{"class": "curated", "kind": "first_party_probe",
                             "ref": "base:tx:" + "0x" + "11" * 32,
                             "observed_at": "2026-08-07"}]
    assert obj["ttl"] == api.EVAL_CHARGED_TTL
    assert obj["evidence_uri"].startswith("sha256:")
    assert len(obj["evidence_uri"]) == len("sha256:") + 64
    assert obj["evidence"]["settlement_tx"] == "0x" + "11" * 32
    assert obj["evidence"]["http_status"] == 402


def test_charged_unserved_wins_even_from_the_404_unknown_seller_shape():
    """trust_lookup's 404 body still carries `delivery` for an unscored
    address — charged_unserved must be checked before the 404 short-circuit."""
    delivery = {"status": "charged_unserved", "as_of": "2026-08-07",
                "settlement_tx": "0x" + "22" * 32, "http_status": 500}
    body_404 = {"error": "unknown seller", "delivery": delivery,
               "suggested_action": {"action": "INVESTIGATE"}}
    code, obj = api._trust_evaluate(
        _req(), lookup=_lookup(404, body_404))
    assert code == 200
    assert obj["decision"] == "FAIL" and obj["reason_code"] == "charged_unserved"
    assert obj["score"] is None                 # no grade in this shape


# --- DECLINE -> grade_decline -----------------------------------------------------
def test_decline_action_is_fail_grade_decline():
    record = _record(grade="F", score=10, provisional=False, action="DECLINE")
    code, obj = api._trust_evaluate(_req(), lookup=_lookup(200, record))
    assert obj["decision"] == "FAIL"
    assert obj["reason_code"] == "grade_decline"
    assert obj["basis"] == [{"class": "behavioral", "kind": "seller_score",
                             "ref": obj["evidence_uri"],
                             "observed_at": VERIFICATION["snapshot_date"]}]


def test_adverse_finding_decline_is_also_grade_decline():
    record = _record(grade="F", score=None, action="DECLINE")
    code, obj = api._trust_evaluate(_req(), lookup=_lookup(200, record))
    assert obj["decision"] == "FAIL" and obj["reason_code"] == "grade_decline"


# --- delivery_verified + A/B -> PASS -----------------------------------------------
def test_delivery_verified_grade_a_is_pass_with_both_basis_entries():
    delivery = {"status": "delivery_verified", "as_of": "2026-08-07"}
    record = _record(grade="A", score=95, action="PROCEED", delivery=delivery)
    code, obj = api._trust_evaluate(_req(), lookup=_lookup(200, record))
    assert obj["decision"] == "PASS"
    assert obj["reason_code"] == "delivery_verified"
    # curated (no settlement_tx on this delivery -> ref None) + behavioral
    assert obj["basis"] == [
        {"class": "curated", "kind": "first_party_probe",
         "ref": None, "observed_at": "2026-08-07"},
        {"class": "behavioral", "kind": "seller_score",
         "ref": obj["evidence_uri"],
         "observed_at": VERIFICATION["snapshot_date"]},
    ]
    assert obj["score"] == 0.95


def test_delivery_verified_grade_c_falls_through_to_grade_marginal():
    delivery = {"status": "delivery_verified", "as_of": "2026-08-07"}
    record = _record(grade="C", score=60, action="CAUTION", delivery=delivery)
    code, obj = api._trust_evaluate(_req(), lookup=_lookup(200, record))
    assert obj["decision"] == "UNCERTAIN"
    assert obj["reason_code"] == "grade_marginal"


# --- plain grade mapping ----------------------------------------------------------
def test_grade_c_is_uncertain_grade_marginal_with_numeric_score():
    record = _record(grade="C", score=65, action="CAUTION")
    code, obj = api._trust_evaluate(_req(), lookup=_lookup(200, record))
    assert obj["decision"] == "UNCERTAIN"
    assert obj["reason_code"] == "grade_marginal"
    assert obj["score"] == 0.65


def test_grade_d_is_also_grade_marginal():
    record = _record(grade="D", score=42, provisional=True,
                     action="INVESTIGATE")
    code, obj = api._trust_evaluate(_req(), lookup=_lookup(200, record))
    assert obj["decision"] == "UNCERTAIN" and obj["reason_code"] == "grade_marginal"


def test_grade_b_no_delivery_is_grade_pass():
    record = _record(grade="B", score=78, action="PROCEED")
    code, obj = api._trust_evaluate(_req(), lookup=_lookup(200, record))
    assert obj["decision"] == "PASS" and obj["reason_code"] == "grade_pass"
    assert obj["basis"] == [{"class": "behavioral", "kind": "seller_score",
                             "ref": obj["evidence_uri"],
                             "observed_at": VERIFICATION["snapshot_date"]}]


def test_grade_pass_a_basis_ref_and_expiry():
    """grade_pass with a numeric A score: behavioral entry carries the
    sha256 ref + snapshot observed_at, no null_reason key at all (score is
    not null), and expires_at is exactly issued_at + ttl."""
    record = _record(grade="A", score=90, action="PROCEED")
    code, obj = api._trust_evaluate(_req(), lookup=_lookup(200, record))
    assert obj["decision"] == "PASS" and obj["reason_code"] == "grade_pass"
    assert obj["evidence_uri"] == "sha256:" + "f" * 64
    assert obj["basis"] == [{"class": "behavioral", "kind": "seller_score",
                             "ref": obj["evidence_uri"],
                             "observed_at": VERIFICATION["snapshot_date"]}]
    assert "null_reason" not in obj
    issued = datetime.fromisoformat(obj["issued_at"])
    expires = datetime.fromisoformat(obj["expires_at"])
    assert (expires - issued).total_seconds() == api.EVAL_DEFAULT_TTL


def test_bare_f_without_decline_is_still_fail_grade_decline():
    """Provisional F doesn't reach DECLINE in _suggested_action (rule v2) but
    must still fail here — spec: 'F handled by DECLINE above, but if you see
    F without DECLINE, return FAIL grade_decline'."""
    record = _record(grade="F", score=12, provisional=True,
                     action="INVESTIGATE")
    code, obj = api._trust_evaluate(_req(), lookup=_lookup(200, record))
    assert obj["decision"] == "FAIL" and obj["reason_code"] == "grade_decline"


# --- unknown wallet ---------------------------------------------------------------
def test_unknown_wallet_is_uncertain_insufficient_evidence_score_null():
    code, obj = api._trust_evaluate(
        _req(), lookup=_lookup(404, {"error": "unknown seller"}))
    assert code == 200
    assert obj["decision"] == "UNCERTAIN"
    assert obj["reason_code"] == "insufficient_evidence"
    assert obj["score"] is None
    assert obj["null_reason"] == "insufficient_signal"
    assert obj["basis"] == []
    assert obj["evidence_uri"] is None


def test_unrated_tier_no_grade_is_also_insufficient_evidence():
    """scores v4 unrated tier: a numeric score exists but no letter is
    published — the letter gate rule means the caller never sees the number."""
    record = _record(grade=None, score=55, action="INSUFFICIENT_EVIDENCE")
    code, obj = api._trust_evaluate(_req(), lookup=_lookup(200, record))
    assert obj["decision"] == "UNCERTAIN"
    assert obj["reason_code"] == "insufficient_evidence"
    assert obj["score"] is None
    assert obj["null_reason"] == "insufficient_signal"


# --- attested basis entry (EAS anchors) --------------------------------------------
def test_anchors_present_adds_attested_entry():
    record = _record(grade="A", score=90, action="PROCEED",
                     verification=ANCHORED_VERIFICATION)
    code, obj = api._trust_evaluate(_req(), lookup=_lookup(200, record))
    classes = [b["class"] for b in obj["basis"]]
    assert classes == ["behavioral", "attested"]
    attested = obj["basis"][1]
    assert attested["kind"] == "eas_anchor"
    assert attested["ref"] == obj["evidence_uri"]
    assert attested["observed_at"] == ANCHORED_VERIFICATION["snapshot_date"]


def test_no_anchors_means_no_attested_entry():
    # default VERIFICATION carries no "anchors" key
    record = _record(grade="A", score=90, action="PROCEED")
    code, obj = api._trust_evaluate(_req(), lookup=_lookup(200, record))
    assert "attested" not in [b["class"] for b in obj["basis"]]


# --- direction=buyer ---------------------------------------------------------------
def test_direction_buyer_is_payer_scoring_not_available():
    body = json.dumps({"schema": "x402-trust-query-v0.1", "direction": "buyer",
                       "payer": {"agent_id": "agent_1", "wallet": "0x" + "cc" * 20}
                       }).encode()
    code, obj = api._trust_evaluate(body)      # no lookup call at all
    assert code == 200
    assert obj["direction"] == "buyer"
    assert obj["decision"] == "UNCERTAIN"
    assert obj["reason_code"] == "payer_scoring_not_available"
    assert obj["score"] is None and obj["basis"] == []
    assert obj["null_reason"] == "insufficient_signal"


# --- block-provenance (draft-02) ----------------------------------------------------
def test_block_provenance_every_fail_has_curated_or_behavioral():
    """draft-02: a FAIL decision must rest on curated or behavioral evidence
    — never attested/community alone. Sweep every FAIL-producing fixture
    used elsewhere in this file and pin the invariant structurally, not just
    per-test (the assertion inside _eval_result already enforces this at
    call time; this test pins it at the wire-shape level too)."""
    fixtures = [
        (200, _record(grade="F", score=10, action="DECLINE")),
        (200, _record(grade="F", score=None, action="DECLINE")),
        (200, _record(grade="F", score=12, provisional=True,
                      action="INVESTIGATE")),
    ]
    delivery = {"status": "charged_unserved", "as_of": "2026-08-07",
                "settlement_tx": "0x" + "33" * 32, "http_status": 402}
    fixtures.append((200, _record(grade="A", score=92, action="INVESTIGATE",
                                  delivery=delivery)))
    seen_fail = False
    for status, record in fixtures:
        code, obj = api._trust_evaluate(_req(), lookup=_lookup(status, record))
        assert obj["decision"] == "FAIL"
        seen_fail = True
        assert any(b["class"] in ("curated", "behavioral")
                  for b in obj["basis"]), obj["basis"]
    assert seen_fail


# --- provider descriptor + field spec ------------------------------------------------
def test_provider_descriptor_and_spec_are_importable_and_serializable():
    assert json.dumps(api.TRUST_PROVIDER_DESCRIPTOR)
    assert json.dumps(api.TRUST_API_SPEC)
    assert api.TRUST_PROVIDER_DESCRIPTOR["endpoint"]["url"].endswith(
        "/v1/trust/evaluate")


def test_spec_lists_all_four_evidence_classes():
    classes = set(api.TRUST_API_SPEC["response"]["basis"]["classes"])
    assert classes == {"curated", "behavioral", "community", "attested"}
    assert classes == set(api.TRUST_PROVIDER_DESCRIPTOR["evidence_classes"])


def test_direction_defaults_to_seller_when_absent():
    body = json.dumps({"subject": {"wallet": SELLER, "chain": "base"}}).encode()
    record = _record(grade="A", score=90, action="PROCEED")
    code, obj = api._trust_evaluate(body, lookup=_lookup(200, record))
    assert obj["direction"] == "seller" and obj["decision"] == "PASS"


# --- missing subject / bad chain ---------------------------------------------------
def test_missing_subject_wallet_is_unknown_subject():
    body = json.dumps({"direction": "seller", "subject": {"chain": "base"}
                       }).encode()
    code, obj = api._trust_evaluate(body)
    assert code == 200
    assert obj["reason_code"] == "unknown_subject"
    assert obj["decision"] == "UNCERTAIN" and obj["score"] is None
    assert obj["null_reason"] == "unknown_subject"


def test_unsupported_chain():
    body = json.dumps({"direction": "seller",
                       "subject": {"wallet": SELLER, "chain": "ethereum"}
                       }).encode()
    code, obj = api._trust_evaluate(body)
    assert code == 200
    assert obj["reason_code"] == "unsupported_chain"
    assert obj["null_reason"] == "unsupported_chain"


def test_chain_falls_back_to_resource_amount_chain():
    body = json.dumps({"direction": "seller", "subject": {"wallet": SELLER},
                       "resource": {"amount": {"value": "1", "chain": "base"}}
                       }).encode()
    record = _record(grade="A", score=90, action="PROCEED")
    code, obj = api._trust_evaluate(body, lookup=_lookup(200, record))
    assert obj["decision"] == "PASS"


def test_eip155_chain_ids_map_to_base_and_polygon():
    for cid, expect_ok in (("eip155:8453", True), ("eip155:137", True),
                           ("eip155:1", False)):
        body = json.dumps({"direction": "seller",
                           "subject": {"wallet": SELLER, "chain": cid}
                           }).encode()
        record = _record(grade="A", score=90, action="PROCEED")
        code, obj = api._trust_evaluate(body, lookup=_lookup(200, record))
        if expect_ok:
            assert obj["reason_code"] != "unsupported_chain", cid
        else:
            assert obj["reason_code"] == "unsupported_chain", cid


# --- malformed / oversized ----------------------------------------------------------
def test_malformed_json_is_400():
    code, obj = api._trust_evaluate(b"{not json")
    assert code == 400
    assert "error" in obj


def test_non_object_json_is_400():
    code, obj = api._trust_evaluate(b"[1, 2, 3]")
    assert code == 400


def test_empty_body_is_a_well_formed_query():
    code, obj = api._trust_evaluate(b"")
    assert code == 200
    assert obj["reason_code"] == "unknown_subject"


def test_oversized_body_returns_413_at_the_wiring_layer():
    """The 16384-byte cap is enforced in do_POST via Content-Length before
    the body is ever read/parsed — pin the constant the wiring checks."""
    assert api.TRUST_EVALUATE_MAX_BODY == 16384


# --- score normalization -------------------------------------------------------------
def test_score_normalization_0_to_100_becomes_0_to_1():
    assert api._eval_score(92, "A") == 0.92
    assert api._eval_score(0, "F") == 0.0
    assert api._eval_score(100, "A") == 1.0


def test_score_normalization_falls_back_to_letter_grade_map_when_no_number():
    assert api._eval_score(None, "A") == 0.9
    assert api._eval_score(None, "B") == 0.75
    assert api._eval_score(None, "C") == 0.55
    assert api._eval_score(None, "D") == 0.4
    assert api._eval_score(None, "F") == 0.15


def test_score_normalization_is_null_with_nothing_to_go_on():
    assert api._eval_score(None, None) is None
    assert api._eval_score(None, "") is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

"""The suggested_action rule table, pinned case by case.

This field is advice about a caller's money, so the mapping is not allowed to
drift silently: every branch is asserted here and the rule version is asserted
alongside, so changing a threshold without bumping ACTION_RULE_VERSION and
updating docs/SCORE_REVISIONS.md fails the suite.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "serving" / "trust_api.py").read_text()


def _load():
    """Exec just the rule function — importing trust_api pulls in the whole
    serving stack (x402pay, storage, a live DB) for a pure function."""
    src = re.search(r"ACTION_RULE_VERSION = \d+\n\n\ndef _suggested_action.*?"
                    r'seller\."\}\n', SRC, re.S)
    assert src, "rule function not found — did the shape of trust_api change?"
    ns: dict = {}
    exec(src.group(0), ns)
    return ns["_suggested_action"], ns["ACTION_RULE_VERSION"]


ACT, VERSION = _load()


def test_rule_version_is_pinned():
    # bump deliberately, with a dated note in docs/SCORE_REVISIONS.md
    assert VERSION == 2


def test_no_data_is_never_bad_news():
    """The failure mode this whole field exists to avoid: an address we have
    not observed must not be lumped in with the ones we have observed and
    found wanting."""
    for resp in ({}, {"grade": "UNKNOWN", "score": None},
                 {"grade": "", "score": None}):
        assert ACT(resp)["action"] == "INSUFFICIENT_EVIDENCE", resp


def test_adverse_finding_outranks_everything():
    r = ACT({"grade": "A", "score": 95, "adverse_findings": [{"id": 1}]})
    assert r["action"] == "DECLINE"
    assert any("adverse" in b for b in r["because"])


def test_full_window_failures_decline():
    for g in ("D", "F"):
        assert ACT({"grade": g, "score": 30,
                    "provisional": False})["action"] == "DECLINE"


def test_thin_window_failures_investigate_not_decline():
    """A provisional D/F is too thin to decline on and too poor to wave
    through — it sends the caller to the evidence."""
    for g in ("D", "F"):
        assert ACT({"grade": g, "score": 30,
                    "provisional": True})["action"] == "INVESTIGATE"


def test_wash_signals_investigate_even_on_a_good_grade():
    for flag in ("cycle_flag", "history_flag", "nightly_flag"):
        r = ACT({"grade": "A", "score": 95,
                 "components": {"wash": {flag: True}}})
        assert r["action"] == "INVESTIGATE", flag
        assert any(flag in b for b in r["because"]), r


def test_caution_band():
    assert ACT({"grade": "C", "score": 65,
                "provisional": False})["action"] == "CAUTION"
    assert ACT({"grade": "A", "score": 95,
                "provisional": True})["action"] == "CAUTION"


def test_proceed_requires_good_grade_full_window_and_no_flags():
    for g in ("A", "B"):
        assert ACT({"grade": g, "score": 90,
                    "provisional": False})["action"] == "PROCEED"


CHARGED = {"status": "charged_unserved", "as_of": "2026-08-07",
           "settlement_tx": "0x" + "ab" * 32}


def test_charged_unserved_floors_at_investigate():
    """Rule v2: a settled payment the seller then refused to serve is
    evidence, not absence — it lifts PROCEED/CAUTION/INSUFFICIENT_EVIDENCE
    to INVESTIGATE, even for an address the index has never scored."""
    r = ACT({"grade": "A", "score": 95, "provisional": False,
             "delivery": CHARGED})
    assert r["action"] == "INVESTIGATE"
    assert any("settled on-chain" in b for b in r["because"])
    assert ACT({"grade": "C", "score": 60, "provisional": False,
                "delivery": CHARGED})["action"] == "INVESTIGATE"
    assert ACT({"delivery": CHARGED})["action"] == "INVESTIGATE"   # unscored


def test_charged_unserved_never_softens_a_decline():
    assert ACT({"grade": "F", "score": 10, "provisional": False,
                "delivery": CHARGED})["action"] == "DECLINE"


def test_delivery_verified_adds_a_reason_but_no_upgrade():
    """Delivering once is worth recording, not worth overriding the grade —
    C stays CAUTION, A stays PROCEED, the receipt shows in `because`."""
    ok = {"status": "delivery_verified", "as_of": "2026-08-07"}
    r = ACT({"grade": "C", "score": 60, "provisional": False, "delivery": ok})
    assert r["action"] == "CAUTION"
    assert any("delivered content" in b for b in r["because"])
    assert ACT({"grade": "A", "score": 95, "provisional": False,
                "delivery": ok})["action"] == "PROCEED"


def test_every_answer_carries_its_reasons_and_version():
    for resp in ({}, {"grade": "A", "score": 95, "provisional": False},
                 {"grade": "F", "score": 10, "provisional": False}):
        r = ACT(resp)
        assert r["rule_version"] == VERSION
        assert r["because"] and all(isinstance(b, str) for b in r["because"])
        # the framing is load-bearing: advice about the caller's funds, not a
        # claim about the seller
        assert "Not a claim about the seller" in r["note"]


def test_actions_are_a_closed_set():
    allowed = {"PROCEED", "CAUTION", "INVESTIGATE", "DECLINE",
               "INSUFFICIENT_EVIDENCE"}
    grades = ["A", "B", "C", "D", "F", "UNKNOWN", ""]
    for g in grades:
        for prov in (True, False):
            for flag in (None, "cycle_flag"):
                for adverse in (True, False):
                    resp = {"grade": g, "score": None if g in ("UNKNOWN", "")
                            else 50, "provisional": prov}
                    if flag:
                        resp["components"] = {"wash": {flag: True}}
                    if adverse:
                        resp["adverse_findings"] = [{"id": 1}]
                    assert ACT(resp)["action"] in allowed, resp


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

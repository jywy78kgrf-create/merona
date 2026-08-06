"""Adverse-findings layer on the sell-side m.Score API.

A hand-verified finding (mrdn's self-wash) must surface on a trust lookup,
override the automated grade DOWNWARD only, and stand even when the address is
no longer scored as a merchant (facilitators are excluded from the seller
universe, so mrdn 404s on the normal path — the finding is the whole answer).
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                    # indexer/
sys.path.insert(0, str(HERE.parent.parent / "serving"))

import trust_api  # noqa: E402

ADDR = "0x8e7769d440b3460b92159dd9c6d17302b036e2d6"


def _point_adverse(tmp_path, grade="F"):
    p = tmp_path / "adverse.json"
    p.write_text(json.dumps({"findings": [
        {"chain": "base", "address": ADDR, "operator": "mrdn (Meridian)",
         "finding": "self_wash", "grade_override": grade,
         "summary": "self-wash", "evidence": {"circular_pct": 86}}]}))
    trust_api.ADVERSE_PATH = p
    trust_api._adverse = None            # bust the cache


def test_finding_surfaces_when_address_is_not_scored(tmp_path, monkeypatch):
    _point_adverse(tmp_path)
    monkeypatch.setattr(trust_api, "_query", lambda *a, **k: [])   # 404 path
    mixed = "0x" + ADDR[2:].upper()                                # case-insensitive body
    code, body = trust_api.trust_lookup("base", mixed)
    assert code == 200
    assert body["grade"] == "F"
    assert body["score"] is None
    assert body["status"] == "adverse_finding"
    assert body["adverse_findings"][0]["finding"] == "self_wash"


def test_finding_overrides_an_automated_grade_downward(tmp_path, monkeypatch):
    _point_adverse(tmp_path)
    # pretend the automated scorer gave it a benign B (82)
    row = ("2026-07-28", 82, "B", 0, "2026-07-28", 6573, 22, 0.0, 0, "{}", 2)
    monkeypatch.setattr(trust_api, "_query", lambda *a, **k: [row])
    monkeypatch.setattr(trust_api, "_verification", lambda *a, **k: {})
    code, body = trust_api.trust_lookup("base", ADDR)
    assert code == 200
    assert body["grade"] == "F"          # not B
    assert body["score"] is None
    assert body["adverse_findings"][0]["finding"] == "self_wash"


def test_no_finding_leaves_a_normal_score_untouched(tmp_path, monkeypatch):
    _point_adverse(tmp_path)
    other = "0x" + "c" * 40
    row = ("2026-07-28", 82, "B", 0, "2026-07-28", 100, 40, 0.0, 1, "{}", 2)
    monkeypatch.setattr(trust_api, "_query", lambda *a, **k: [row])
    monkeypatch.setattr(trust_api, "_verification", lambda *a, **k: {})
    code, body = trust_api.trust_lookup("base", other)
    assert code == 200
    assert body["grade"] == "B"
    assert "adverse_findings" not in body


def test_no_finding_and_no_score_is_still_404(tmp_path, monkeypatch):
    _point_adverse(tmp_path)
    monkeypatch.setattr(trust_api, "_query", lambda *a, **k: [])
    code, body = trust_api.trust_lookup("base", "0x" + "d" * 40)
    assert code == 404


def test_real_adverse_file_carries_meridian():
    """The committed file must actually contain the mrdn finding."""
    trust_api.ADVERSE_PATH = (HERE.parent.parent / "data" / "indexer"
                              / "adverse_findings.json")
    trust_api._adverse = None
    f = trust_api._adverse_findings().get(("base", ADDR))
    assert f and f["finding"] == "self_wash" and f["grade_override"] == "F"

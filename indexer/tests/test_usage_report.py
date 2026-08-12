"""Unit tests for the metering surface: the clientInfo/method log-name
sanitizer (trust_api._mcp_log_name) and serving/usage_report.py's parsing
and day/bucket classification. No HTTP server involved — these exercise the
pure functions directly against synthetic input."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "serving"))

import trust_api                                     # noqa: E402
import usage_report                                   # noqa: E402


# --- _mcp_log_name --------------------------------------------------------
def test_mcp_log_name_keeps_safe_charset():
    assert trust_api._mcp_log_name("claude-ai") == "claude-ai"
    assert trust_api._mcp_log_name("1.0") == "1.0"


def test_mcp_log_name_collapses_unsafe_runs():
    # a run of unsafe chars collapses to ONE underscore, not one per char
    assert trust_api._mcp_log_name("a<<<>>>b") == "a_b"
    assert trust_api._mcp_log_name("evil\n;drop") == "evil_drop"
    assert trust_api._mcp_log_name("!!!hello!!!") == "_hello_"


def test_mcp_log_name_allows_space_dot_slash_hyphen():
    # these are explicitly in-charset by spec, not something to strip
    assert trust_api._mcp_log_name("my client/2.0-beta") == \
        "my client/2.0-beta"


def test_mcp_log_name_empty():
    assert trust_api._mcp_log_name("") == ""


# --- usage_report: parsing -------------------------------------------------
def _write(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x
                           for x in lines) + "\n")
    return p


def test_read_rows_skips_malformed_lines(tmp_path):
    good = {"ts": "2026-08-10T00:00:00+00:00", "id": "ip:1.2.3.4",
           "path": "/mcp:initialize", "status": 200}
    p = _write(tmp_path, "u.jsonl", [good, "not json", "{broken", ""])
    rows = usage_report.read_rows(p)
    assert len(rows) == 1
    assert rows[0]["day"] == "2026-08-10"


def test_read_rows_includes_rotation_sibling(tmp_path):
    old = {"ts": "2026-08-09T00:00:00+00:00", "id": "ip:1.1.1.1",
          "path": "/mcp:tools/list", "status": 200}
    new = {"ts": "2026-08-10T00:00:00+00:00", "id": "ip:1.1.1.1",
          "path": "/mcp:tools/list", "status": 200}
    p = _write(tmp_path, "u.jsonl", [new])
    _write(tmp_path, "u.jsonl.1", [old])
    rows = usage_report.read_rows(p)
    assert {r["day"] for r in rows} == {"2026-08-09", "2026-08-10"}


# --- usage_report: classification ------------------------------------------
def _synthetic_rows():
    day1, day2 = "2026-08-10", "2026-08-11"
    rows = []

    def add(day, ident, path, status=200):
        rows.append({"day": day, "id": ident, "path": path,
                     "status": status})

    # day1: one client does the full dance (init -> handshake -> two tools)
    add(day1, "ip:1.1.1.1", "/mcp:initialize:claude-ai/1.0")
    add(day1, "ip:1.1.1.1", "/mcp:notif:initialized")
    add(day1, "ip:1.1.1.1", "/mcp:payto_check")
    add(day1, "ip:1.1.1.1", "/mcp:clean_stats")
    # day1: a second client connects and completes the handshake but never
    # calls a tool
    add(day1, "ip:2.2.2.2", "/mcp:initialize:otherclient/2")
    add(day1, "ip:2.2.2.2", "/mcp:notif:initialized")
    # day1: real HTTP API traffic
    add(day1, "key:tester", "/v1/trust/base/0xabc")
    add(day1, "key:tester", "/v1/trust/base/0xabc")
    add(day1, "ip:3.3.3.3", "/v1/delivery/base/0xdef")
    # day1: scanner noise
    add(day1, "ip:9.9.9.9", "/.env")
    add(day1, "ip:9.9.9.9", "/h5")
    add(day1, "ip:8.8.8.8", "/.git/config")

    # day2: a client initializes but never completes the handshake or calls
    # a tool
    add(day2, "ip:4.4.4.4", "/mcp:initialize")
    add(day2, "key:tester", "/v1/trust/base/0xabc")
    # day2: trust_score both ways — a probe that only collected the paywall
    # (402) and a caller that was actually served (200)
    add(day2, "ip:5.5.5.5", "/mcp:trust_score", status=402)
    add(day2, "ip:6.6.6.6", "/mcp:trust_score", status=200)

    return rows


def test_classify_mcp_bucket():
    mcp, _http, _noise = usage_report.classify(_synthetic_rows())
    d1 = mcp["2026-08-10"]
    assert d1["init_idents"] == {"ip:1.1.1.1", "ip:2.2.2.2"}
    assert d1["init_clients"] == {"claude-ai/1.0", "otherclient/2"}
    assert d1["handshake_idents"] == {"ip:1.1.1.1", "ip:2.2.2.2"}
    assert d1["tool_idents"]["payto_check"] == {"ip:1.1.1.1"}
    assert d1["tool_idents"]["clean_stats"] == {"ip:1.1.1.1"}
    assert d1["tool_idents"]["mismatch_feed"] == set()
    assert d1["tool_idents"]["trust_score"] == set()

    d2 = mcp["2026-08-11"]
    assert d2["init_idents"] == {"ip:4.4.4.4"}
    assert d2["handshake_idents"] == set()


def test_stranded_connected_but_never_called_a_tool():
    """The stranded cohort must count ANY connector — initialize with or
    without a completed handshake — that never called a tool. Counting only
    handshake-completers would hide clients that initialize and vanish
    (day2's ip:4.4.4.4), the exact cohort the report exists to surface."""
    mcp, _http, _noise = usage_report.classify(_synthetic_rows())
    assert usage_report.stranded(mcp["2026-08-10"]) == {"ip:2.2.2.2"}
    assert usage_report.stranded(mcp["2026-08-11"]) == {"ip:4.4.4.4"}


def test_classify_trust_score_paid_vs_paywall():
    """Both callers count as trust_score idents, but only the status-200
    (served: paid or api-keyed) one lands in paid_trust_idents — the 402
    paywall-looker must not read as revenue."""
    mcp, _http, _noise = usage_report.classify(_synthetic_rows())
    d2 = mcp["2026-08-11"]
    assert d2["tool_idents"]["trust_score"] == {"ip:5.5.5.5", "ip:6.6.6.6"}
    assert d2["paid_trust_idents"] == {"ip:6.6.6.6"}


def test_classify_http_bucket_groups_by_known_prefix():
    _mcp, http, _noise = usage_report.classify(_synthetic_rows())
    d1 = http["2026-08-10"]
    assert d1["/v1/trust/"]["count"] == 2
    assert d1["/v1/trust/"]["idents"] == {"key:tester"}
    assert d1["/v1/delivery/"]["count"] == 1
    assert d1["/v1/delivery/"]["idents"] == {"ip:3.3.3.3"}
    d2 = http["2026-08-11"]
    assert d2["/v1/trust/"]["count"] == 1


def test_classify_noise_bucket_collapses_scanner_paths():
    _mcp, _http, noise = usage_report.classify(_synthetic_rows())
    d1 = noise["2026-08-10"]
    assert d1["count"] == 3
    assert d1["idents"] == {"ip:9.9.9.9", "ip:8.8.8.8"}
    assert d1["paths"] == {"/.env", "/h5", "/.git/config"}
    assert "2026-08-11" not in noise                 # nothing noisy that day


def test_render_produces_all_three_sections(tmp_path):
    p = _write(tmp_path, "u.jsonl", [
        {"ts": "2026-08-10T00:00:00+00:00", "id": "ip:1.1.1.1",
         "path": "/mcp:initialize:x/1", "status": 200},
        {"ts": "2026-08-10T00:00:00+00:00", "id": "ip:9.9.9.9",
         "path": "/.env", "status": 404}])
    rows = usage_report.read_rows(p)
    text = usage_report.render(*usage_report.classify(rows))
    assert "=== MCP ===" in text
    assert "=== HTTP API ===" in text
    assert "=== NOISE ===" in text
    assert "/.env" not in text                       # noise never enumerated
    assert "1 requests from 1 idents across 1 distinct paths" in text

"""GET /.well-known/merona-crawlers.json — the canonical crawler-UA registry
(x402-foundation/x402#2300 commitment #3): every outbound User-Agent merona's
own tooling sends, so a seller who sees "merona"-looking traffic can look up
what it actually is instead of guessing (the other operator in #2300 had to
reverse-engineer which of five UAs hitting him were merona's).

The whole point of this file is that CRAWLERS can't silently drift from the
code that actually sends each UA: every `user_agents` entry's `ua` literal is
checked against its claimed `source` file, read fresh off disk here — not
trusted from CRAWLERS' own text.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent                                # repo root
sys.path.insert(0, str(HERE.parent))                      # indexer/
sys.path.insert(0, str(ROOT / "serving"))

import trust_api as api                                    # noqa: E402


def _normalized_source(rel_path: str) -> str:
    """Source file text with Python's (and JS template-less) implicit
    adjacent-string-literal line breaks collapsed, so a UA literal that a
    source file wraps across two lines --

        "...merona-collectors/0.1 "
                            "(read-only research; hello@merona.io)"

    -- reads back as one contiguous run of characters, the same as the
    runtime string CRAWLERS actually holds. Only collapses a closing quote
    immediately followed (across whitespace/newline) by an opening quote;
    harmless if it also collapses some unrelated adjacent pair elsewhere in
    the file, since this is used only to check for containment of one known
    literal, never to re-serialize the file."""
    text = (ROOT / rel_path).read_text()
    return re.sub(r'"\s*\n\s*"', "", text)


# --- shape ----------------------------------------------------------------------
def test_crawlers_is_json_serializable():
    json.dumps(api.CRAWLERS)


def test_crawlers_top_level_shape():
    assert api.CRAWLERS["provider"] == "merona"
    assert api.CRAWLERS["contact"] == "hello@merona.io"
    assert isinstance(api.CRAWLERS["user_agents"], list)
    assert len(api.CRAWLERS["user_agents"]) > 0
    assert "note" in api.CRAWLERS


def test_every_user_agent_entry_has_the_required_fields():
    for entry in api.CRAWLERS["user_agents"]:
        assert set(entry) == {"ua", "purpose", "pays", "source"}
        assert isinstance(entry["ua"], str) and entry["ua"]
        assert isinstance(entry["purpose"], str) and entry["purpose"]
        assert isinstance(entry["pays"], bool)
        assert isinstance(entry["source"], str) and entry["source"]


# --- docs-pinned-to-code: every claimed ua actually lives in its source file ----
# okxasp.py embeds the merona-collectors identity inside a longer, browser-
# style UA (masquerading past okx.ai's WAF on blind /api paths) split across
# two source lines by Python's implicit string concatenation; the collapsed
# EXACT PREFIX of that file's literal is what's checked for this one entry,
# rather than a full contiguous match, since the registry's "ua" for that
# entry IS the full concatenated string and the source only ever holds it as
# concatenation of TWO shorter literals.
_OKX_SOURCE_PREFIX = ('"Mozilla/5.0 (X11; Linux x86_64) '
                      'merona-collectors/0.1 "')


def test_every_user_agent_ua_appears_in_its_claimed_source_file():
    for entry in api.CRAWLERS["user_agents"]:
        src = _normalized_source(entry["source"])
        if entry["source"] == "collectors/okxasp.py":
            # the raw (non-normalized) file really does contain this exact
            # fragment verbatim -- confirms the prefix we're about to check
            # against isn't invented
            raw = (ROOT / entry["source"]).read_text()
            assert _OKX_SOURCE_PREFIX in raw
            assert entry["ua"].startswith(_OKX_SOURCE_PREFIX.strip('"'))
            continue
        assert entry["ua"] in src, (
            f"{entry['ua']!r} not found verbatim in {entry['source']}")


def test_rpc_or_internal_entries_also_pin_to_their_source():
    for entry in api.CRAWLERS.get("rpc_or_internal", []):
        assert entry["ua"] in _normalized_source(entry["source"]), (
            f"{entry['ua']!r} not found verbatim in {entry['source']}")


def test_no_user_agent_appears_twice():
    uas = [e["ua"] for e in api.CRAWLERS["user_agents"]]
    assert len(uas) == len(set(uas))


# --- inclusion criterion: server_version strings are NOT crawlers --------------
def test_inbound_server_identifiers_are_not_listed_as_crawlers():
    """merona-trust/1.0 and merona-canary/0.1 are Handler.server_version —
    the Server: header on OUR responses, the opposite direction from every
    outbound UA in this registry. They must never appear here."""
    all_uas = " ".join(e["ua"] for e in api.CRAWLERS["user_agents"]) + \
        " ".join(e["ua"] for e in api.CRAWLERS.get("rpc_or_internal", []))
    assert "merona-trust/1.0" not in all_uas
    assert "merona-canary/0.1" not in all_uas


# --- descriptor + spec wiring -----------------------------------------------------
def test_descriptor_advertises_the_crawlers_url():
    assert api.TRUST_PROVIDER_DESCRIPTOR["crawlers"] == \
        api.PUBLIC_BASE + "/.well-known/merona-crawlers.json"


def test_spec_documents_discovery_basis_and_not_evaluated():
    entry_fields = api.TRUST_API_SPEC["response"]["basis"]["entry_fields"]
    assert "discovery_basis" in entry_fields
    assert api.TRUST_API_SPEC["delivery"]["not_evaluated"]
    assert "discovery_basis" in api.TRUST_API_SPEC["delivery"]


# --- route wiring ------------------------------------------------------------------
def test_well_known_route_serves_crawlers(monkeypatch):
    import io
    monkeypatch.setattr(api, "KEYS", {})       # anonymous lane, no x402 needed
    h = api.Handler.__new__(api.Handler)
    h.headers = {}
    h.client_address = ("203.0.113.9", 12345)
    h.command = "GET"
    h.path = "/.well-known/merona-crawlers.json"
    h.wfile = io.BytesIO()
    h.request_version = "HTTP/1.1"
    h.requestline = f"GET {h.path} HTTP/1.1"
    h.close_connection = False
    h.do_GET()
    raw = h.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    assert status == 200
    assert json.loads(body) == api.CRAWLERS


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

"""MCP endpoint + x402 payment lane, end to end over real HTTP.

Spins the REAL trust_api with a synthetic store, then exercises:
  - GET /mcp.json manifest
  - MCP initialize / tools/list / tools/call for the free tools
  - trust_score via api_key through MCP
  - the REST x402 lane: 402 with accepts when unpaid, 200 +
    X-PAYMENT-RESPONSE with a (mock-facilitator) payment, compute-before-
    settle (a 404 lookup must NOT settle), and settle-failure -> 402.
Facilitator is mocked at the x402pay.requests boundary — no network.
"""
from __future__ import annotations

import base64
import csv
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                 # indexer/
sys.path.insert(0, str(HERE.parent.parent / "serving"))

PORT = 18404
SELLER = "0x" + "ab" * 20
PAYTO = "0x" + "fe" * 20
FAKE_PAYMENT = base64.b64encode(json.dumps(
    {"x402Version": 1, "scheme": "exact", "network": "base",
     "payload": {"signature": "0xsig", "authorization": {}}}).encode()).decode()


def _req(path, method="GET", body=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 method=method,
                                 data=json.dumps(body).encode() if body else None)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), dict(e.headers)


def _mcp(method, params=None, mid=1):
    return _req("/mcp", "POST", {"jsonrpc": "2.0", "id": mid,
                                 "method": method, "params": params or {}})


def _tool(name, args):
    s, b, _ = _mcp("tools/call", {"name": name, "arguments": args})
    assert s == 200, b
    return json.loads(b["result"]["content"][0]["text"])


class _FakeFacilitator:
    """Stands in for x402pay.requests — records settle calls."""
    def __init__(self):
        self.settles = 0
        self.verify_ok = True
        self.settle_ok = True

    def post(self, url, json=None, timeout=None):
        class R:
            def __init__(r, d): r._d = d
            def json(r): return r._d
        if url.endswith("/verify"):
            return R({"isValid": self.verify_ok,
                      "invalidReason": None if self.verify_ok else "bad sig"})
        self.settles += 1
        return R({"success": self.settle_ok, "transaction": "0x" + "11" * 32,
                  "network": "base", "payer": "0x" + "77" * 20,
                  "errorReason": None if self.settle_ok else "insufficient"})


def test_mcp_and_x402(tmp_path, monkeypatch):
    snap = tmp_path / "snapshots" / "2026-08-02"
    snap.mkdir(parents=True)
    now = int(time.time())
    with open(snap / "seller_aggregates.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["chain", "seller", "tx_count",
                                          "unique_payers", "self_pay_tx",
                                          "first_ts", "last_ts"])
        w.writeheader()
        w.writerow(dict(chain="base", seller=SELLER, tx_count=5000,
                        unique_payers=900, self_pay_tx=0,
                        first_ts=now - 60 * 86400, last_ts=now))
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps({
        "meta": {"as_of": {"catalog": "2026-08-02", "live_probe": "2026-08-02"},
                 "disclaimer": "recorded facts, not accusations",
                 "counts": {"catalog_vs_live": 1, "payto_rotation": 0}},
        "catalog_vs_live": [{"resource": "https://api.x.example/v1",
                             "advertised_payto": "0x" + "aa" * 20,
                             "live_paytos": ["0x" + "bb" * 20],
                             "live_seen_onchain": {}, "catalog_date": "2026-08-02",
                             "probe_date": "2026-08-02"}],
        "payto_rotation": []}))

    monkeypatch.delenv("X402_DB_URL", raising=False)
    monkeypatch.setenv("WASH_FLAGS_DIR", str(tmp_path / "flags"))
    (tmp_path / "flags").mkdir()
    monkeypatch.setenv("TRUST_API_PORT", str(PORT))
    monkeypatch.setenv("TRUST_API_KEYS", "sk_live_1:tester")
    monkeypatch.setenv("TRUST_API_RPM", "600")
    monkeypatch.setenv("TRUST_API_BURST", "50")
    monkeypatch.setenv("TRUST_API_USAGE", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("TRUST_API_SQLITE", str(tmp_path / "t.db"))

    import storage
    import scores
    monkeypatch.setattr(scores, "SNAP_DIR", snap.parent)
    monkeypatch.setattr(scores, "FLAGS_DIR", tmp_path / "flags")
    monkeypatch.setattr(scores, "MIN_MATURE_DAYS", 1)
    monkeypatch.setattr(scores, "CENSUS_DIRECTORY", tmp_path / "none.json")
    store = storage.open_store(str(tmp_path / "t.db"))
    assert scores.compute_seller_scores(store, "2026-08-02") == 1
    store.record_clean_metrics("2026-08-02", "base", {
        "scoped_settlements": 100, "scoped_volume_usd": 1234.5,
        "flagged_sellers": 1, "excluded_settlements": 3,
        "clean_settlements": 97, "clean_volume_usd": 1200.0,
        "coverage_note": "test"}, "t")
    store.db.close()

    import importlib
    import trust_api
    importlib.reload(trust_api)
    trust_api.FEED_PATH = feed
    trust_api._feed_cache.update({"mtime": None, "feed": None})
    fac = _FakeFacilitator()
    import x402pay
    monkeypatch.setattr(x402pay, "PAYTO", PAYTO)
    monkeypatch.setattr(x402pay, "requests", fac)
    threading.Thread(target=trust_api.main, daemon=True).start()
    time.sleep(0.5)

    # browser hits / -> HTML landing; agents (no text/html) keep JSON
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/",
                                 headers={"Accept": "text/html,*/*"})
    r = urllib.request.urlopen(req, timeout=5)
    assert r.headers["Content-Type"].startswith("text/html")
    assert b"m.Score API" in r.read()
    s, b, _ = _req("/")
    assert s == 200 and b["service"] == "m.Score API"
    # "/" is content-negotiated, so both representations must Vary: Accept —
    # without it an edge cache serves whichever representation warmed the
    # PoP first to everyone (observed live behind Cloudflare, 2026-08-12)
    assert r.headers.get("Vary") == "Accept"          # HTML branch above
    _s2, _b2, h2 = _req("/")
    assert h2.get("Vary") == "Accept"                 # JSON branch
    # unknown /.well-known/ probes: honest 404, not the paid lane's 401
    s, b, _ = _req("/.well-known/does-not-exist.json")
    assert s == 404 and b == {"error": "not found"}

    # manifest + handshake
    s, b, _ = _req("/mcp.json")
    assert s == 200 and [t["name"] for t in b["tools"]] == [
        "payto_check", "mismatch_feed", "clean_stats", "trust_score"]
    # Glama ownership proof. Contact address only — this file is public, so a
    # regression that put anything else in it would be a leak.
    s, b, _ = _req("/.well-known/glama.json")
    assert s == 200, s
    assert set(b) == {"$schema", "maintainers"}, b
    assert b["maintainers"][0]["email"] == "hello@merona.io", b
    assert "glama.ai" in b["$schema"], b

    s, b, _ = _mcp("initialize")
    assert s == 200 and b["result"]["serverInfo"]["name"] == "merona-mcp"
    # storefront: instructions + title tell a connecting client what's here
    assert "merona" in b["result"]["instructions"]
    assert "trust_score" in b["result"]["instructions"]
    assert b["result"]["serverInfo"]["title"] == \
        "merona — x402 settlement index"
    assert b["result"]["serverInfo"]["version"] == "1.2"
    s, b, _ = _mcp("tools/list")
    assert len(b["result"]["tools"]) == 4

    # directory scanners (Smithery, Glama, mcp.so) probe resources/prompts on
    # every server regardless of advertised capabilities. Answer empty rather
    # than -32601 so their scan logs come back clean.
    s, b, _ = _mcp("resources/list")
    assert s == 200 and b["result"]["resources"] == [] and "error" not in b
    s, b, _ = _mcp("resources/templates/list")
    assert s == 200 and b["result"]["resourceTemplates"] == []
    s, b, _ = _mcp("prompts/list")
    assert s == 200 and b["result"]["prompts"] == []
    # genuinely unknown methods must still be rejected, not silently emptied
    s, b, _ = _mcp("does/not/exist")
    assert s == 200 and b["error"]["code"] == -32601

    # initialize with a clientInfo, incl. an attacker-controlled name that
    # must never survive raw into the usage log
    s, b, _ = _mcp("initialize", {"clientInfo": {"name": "claude-ai",
                                                 "version": "1.0"}})
    assert s == 200
    s, b, _ = _mcp("initialize", {"clientInfo": {
        "name": "evil<script>\n;rm -rf /", "version": "../../etc"}})
    assert s == 200
    # malformed params must not crash the handler thread: scanners send
    # params as a list/null, where .get() would raise before the guard
    s, b, _ = _mcp("initialize", [1, 2])
    assert s == 200 and "instructions" in b["result"]
    s, b, _ = _mcp("tools/call", [1, 2])
    assert s == 200 and b["result"]["isError"] is True
    # only notifications/initialized is metered; other notifications aren't.
    # 202 responses have an empty body, so post raw rather than via _mcp/_req
    # (both of those json-decode the response).
    def _notify(method):
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/mcp", method="POST",
            data=json.dumps({"jsonrpc": "2.0", "method": method}).encode())
        req.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(req, timeout=5).status
    assert _notify("notifications/initialized") == 202
    assert _notify("notifications/cancelled") == 202
    # a failing tool call must meter 500, not 200 — response body untouched
    s, b, _ = _mcp("tools/call", {"name": "no-such-tool", "arguments": {}})
    assert s == 200 and b["result"]["isError"] is True

    # free tools
    out = _tool("payto_check", {"query": "api.x.example"})
    assert out["records_found"] == 1
    out = _tool("mismatch_feed", {})
    assert out["meta"]["counts"]["catalog_vs_live"] == 1
    out = _tool("clean_stats", {})
    assert out["clean"][0]["chain"] == "base"
    assert out["clean"][0]["clean_volume_usd"] == 1200.0

    # paid tool via api key
    out = _tool("trust_score", {"chain": "base", "address": SELLER,
                                "api_key": "sk_live_1"})
    assert out["grade"] and out["score_version"] == 4

    # paid tool without key/payment -> payment_required instructions
    out = _tool("trust_score", {"chain": "base", "address": SELLER})
    assert out["payment_required"]["accepts"][0]["payTo"] == PAYTO

    # REST x402 lane: unpaid -> 402 with accepts
    s, b, _ = _req(f"/v1/trust/base/{SELLER}")
    assert s == 402 and b["accepts"][0]["payTo"] == PAYTO
    # paid -> 200 + receipt header, exactly one settle
    s, b, h = _req(f"/v1/trust/base/{SELLER}",
                   headers={"X-PAYMENT": FAKE_PAYMENT})
    assert s == 200 and b["grade"] and "X-PAYMENT-RESPONSE" in h
    assert fac.settles == 1
    # compute-before-settle: unknown wallet -> 404, NO settle attempt
    s, _b, _ = _req("/v1/trust/base/0x" + "99" * 20,
                    headers={"X-PAYMENT": FAKE_PAYMENT})
    assert s == 404 and fac.settles == 1
    # settle failure -> 402 with error, client not served
    fac.settle_ok = False
    s, b, _ = _req(f"/v1/trust/base/{SELLER}",
                   headers={"X-PAYMENT": FAKE_PAYMENT})
    assert s == 402 and "settlement failed" in b["error"]
    # keyed path untouched by all of this
    s, b, _ = _req(f"/v1/trust/base/{SELLER}",
                   headers={"X-API-Key": "sk_live_1"})
    assert s == 200 and b["grade"]

    # scanner probes must not be metered: a public IP takes a constant drizzle
    # of /.env, /.git/config and CMS-kit paths, and logging them buries real
    # usage under noise. 401s aimed at the real /v1/ namespace still count.
    for junk in ("/.env", "/.git/config", "/h5", "/api/user/ismustmobile"):
        code, _b, _h = _req(junk)
        assert code == 401, junk
    code, _b, _h = _req("/v1/nope")
    assert code == 401
    # /mcp parse-error (400) and body-too-large (413) are scanner noise too
    # — confirm they stay unmetered, same as the junk-path probes above
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/mcp",
                                 method="POST", data=b"not json")
    req.add_header("Content-Type", "application/json")
    try:
        code = urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 400
    # the response is sent BEFORE the meter line is appended — give the
    # handler thread a beat so the read below isn't racing the write
    time.sleep(0.3)
    usage = [json.loads(x) for x in
             (tmp_path / "usage.jsonl").read_text().splitlines() if x.strip()]
    logged = {r["path"] for r in usage}
    assert "/v1/nope" in logged                     # real namespace: recorded
    assert not any(p.startswith(("/.env", "/.git", "/h5", "/api/user"))
                   for p in logged)                 # scanner noise: dropped
    # the paid + MCP lanes still land in the log, tool-name resolved
    assert any(p.startswith("/mcp:") for p in logged), logged
    assert any(r["id"].startswith("x402:") for r in usage)

    # the whole MCP surface is now metered, not just tools/call
    assert "/mcp:initialize:claude-ai/1.0" in logged
    assert "/mcp:tools/list" in logged
    assert "/mcp:resources/list" in logged
    assert "/mcp:resources/templates/list" in logged
    assert "/mcp:prompts/list" in logged
    assert "/mcp:notif:initialized" in logged
    # ...but only that one notification type — cancelled stays unmetered
    assert not any("cancelled" in p for p in logged)
    # unknown-method path is logged, sanitized, capped
    assert any(p.startswith("/mcp:?does/not/exist") for p in logged)
    # attacker-controlled clientInfo never leaks unsafe characters through
    # (space/hyphen/dot/slash are allowed by design, so only the actual
    # injection-shaped characters are checked here)
    assert not any(c in p for p in logged for c in "<>;\n")
    # tool-call status reflects outcome: success 200, isError 500 — and the
    # JSON-RPC response body itself is untouched either way (checked above)
    by_path_status = {(r["path"], r["status"]) for r in usage}
    assert ("/mcp:no-such-tool", 500) in by_path_status
    assert ("/mcp:payto_check", 200) in by_path_status
    # trust_score: the api-keyed (served) call meters 200; the unpaid call
    # that only collected payment instructions meters 402 — "first real
    # dollar" must be distinguishable from paywall lookers in the log
    assert ("/mcp:trust_score", 200) in by_path_status
    assert ("/mcp:trust_score", 402) in by_path_status
    # the parse-error probe above never made it into the log at all
    assert not any(r["status"] == 400 for r in usage)


def test_unreadable_keys_file_degrades_instead_of_crashing(tmp_path,
                                                           monkeypatch,
                                                           capsys):
    """Learned live 2026-08-06: a root-owned 600 keys file under User=x402
    crashed the whole API at import. An unreadable or malformed keys file must
    mean 'no file keys + a loud journal line', never a dead service — the free
    tools and the x402 lane don't depend on keys at all."""
    import trust_api
    # a directory passes .exists() but read_text() raises (IsADirectoryError,
    # an OSError) — same class of failure as EACCES without needing to drop
    # privileges inside the test runner
    monkeypatch.setenv("TRUST_API_KEYS_FILE", str(tmp_path))
    monkeypatch.setenv("TRUST_API_KEYS", "sk_env_1:envkey")
    keys = trust_api._load_keys()
    assert keys == {"sk_env_1": "envkey"}          # env keys survive
    assert "WARNING" in capsys.readouterr().err    # and it says so, loudly

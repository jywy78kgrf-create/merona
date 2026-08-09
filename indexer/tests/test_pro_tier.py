"""merona's Pro API tier ($300/mo, manually onboarded).

A key is Pro when its SERVER-CONFIGURED name starts with "pro:" — never from
anything the client sends. Pro gets a separate rate-limit pool and three
read-only bulk GET endpoints under /v1/pro/, gated on a pro key (403, never
402 — /v1/pro/ sits outside the x402 payable-prefix tuple).

Follows test_adverse_findings.py's convention of importing trust_api directly
(the DB and x402pay's facilitator calls are both lazy at import time). The
route/gating tests drive the REAL Handler.do_GET through a bare instance
(Handler.__new__, no socket) with an in-memory wfile, so the gating and
bucket-separation assertions exercise the actual wiring, not a restatement
of it.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                    # indexer/
sys.path.insert(0, str(HERE.parent.parent / "serving"))

import trust_api as api                                   # noqa: E402

SELLER = "0x" + "ab" * 20
SELLER_B = "0x" + "cd" * 20


# --- fake-handler plumbing (no socket; exercises the real do_GET) -------------
def _handler(path: str, headers: dict | None = None) -> api.Handler:
    h = api.Handler.__new__(api.Handler)
    h.headers = headers or {}
    h.client_address = ("203.0.113.9", 12345)
    h.command = "GET"
    h.path = path
    h.wfile = io.BytesIO()
    h.request_version = "HTTP/1.1"
    h.requestline = f"GET {path} HTTP/1.1"
    h.close_connection = False
    return h


def _get(path: str, headers: dict | None = None) -> tuple[int, dict | None]:
    h = _handler(path, headers)
    h.do_GET()
    raw = h.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, (json.loads(body) if body else None)


# --- pro detection: derived ONLY from the server-configured name --------------
def test_is_pro_pure_function():
    assert api._is_pro("pro:acme") is True
    assert api._is_pro("acme") is False
    assert api._is_pro(None) is False
    assert api._is_pro("") is False


def test_identity_pro_key_is_pro(monkeypatch):
    monkeypatch.setattr(api, "KEYS", {"k1": "pro:acme"})
    h = _handler("/", headers={"X-API-Key": "k1"})
    ident, ok, is_pro = h._identity()
    assert ok is True and is_pro is True and ident == "key:pro:acme"


def test_identity_non_pro_key_is_not_pro(monkeypatch):
    monkeypatch.setattr(api, "KEYS", {"k2": "acme"})
    h = _handler("/", headers={"X-API-Key": "k2"})
    ident, ok, is_pro = h._identity()
    assert ok is True and is_pro is False


def test_identity_anonymous_mode_never_pro(monkeypatch):
    monkeypatch.setattr(api, "KEYS", {})
    h = _handler("/")
    ident, ok, is_pro = h._identity()
    assert ok is True and is_pro is False


def test_tier_not_derivable_from_client_input(monkeypatch):
    """A client sending X-API-Key: pro:spoofed (not a configured key) must
    not be pro — the string only means something once it is the CONFIGURED
    name behind an actual key the operator minted."""
    monkeypatch.setattr(api, "KEYS", {"k2": "acme"})
    h = _handler("/", headers={"X-API-Key": "pro:spoofed"})
    ident, ok, is_pro = h._identity()
    assert ok is False and is_pro is False


# --- gating: /v1/pro/* without a pro key -> 403; with one -> 200 --------------
def test_pro_route_403_with_no_key_at_all(monkeypatch):
    monkeypatch.setattr(api, "KEYS", {})
    code, body = _get("/v1/pro/scores?chain=base")
    assert code == 403
    assert body["error"] == "pro tier required"
    assert body["pricing"] == "https://api.merona.io/#pro"
    assert body["contact"] == "hello@merona.io"


def test_pro_route_403_with_a_valid_but_non_pro_key(monkeypatch):
    monkeypatch.setattr(api, "KEYS", {"k2": "acme"})
    code, body = _get("/v1/pro/scores?chain=base",
                      headers={"X-API-Key": "k2"})
    assert code == 403
    assert body["error"] == "pro tier required"


def test_pro_route_never_402_even_with_x402_enabled(monkeypatch):
    """/v1/pro/ sits outside the payable-prefix tuple ("/v1/trust/",
    "/v1/agent/", "/v1/endpoint") — verify that tuple still excludes it, and
    that a gate failure is 403, never the x402 402."""
    assert not "/v1/pro/".startswith(("/v1/trust/", "/v1/agent/",
                                      "/v1/endpoint"))
    monkeypatch.setattr(api, "KEYS", {})
    monkeypatch.setattr(api.x402pay, "enabled", lambda: True)
    code, _body = _get("/v1/pro/scores?chain=base")
    assert code == 403


def test_pro_route_200_with_a_pro_key(monkeypatch):
    monkeypatch.setattr(api, "KEYS", {"k1": "pro:acme"})
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    code, body = _get("/v1/pro/scores?chain=base",
                      headers={"X-API-Key": "k1"})
    assert code == 200
    assert body["chain"] == "base" and body["sellers"] == []


def test_pro_history_route_200_with_a_pro_key(monkeypatch):
    monkeypatch.setattr(api, "KEYS", {"k1": "pro:acme"})
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    code, body = _get(
        f"/v1/pro/scores/history?chain=base&address={SELLER}&limit=10",
        headers={"X-API-Key": "k1"})
    assert code == 200
    assert body["seller"] == SELLER and body["limit"] == 10


def test_pro_delivery_route_200_with_a_pro_key(monkeypatch):
    monkeypatch.setattr(api, "KEYS", {"k1": "pro:acme"})
    monkeypatch.setattr(api, "_delivery_map", lambda: {})
    code, body = _get("/v1/pro/delivery?chain=base",
                      headers={"X-API-Key": "k1"})
    assert code == 200 and body["chain"] == "base"


def test_unknown_pro_path_is_404(monkeypatch):
    monkeypatch.setattr(api, "KEYS", {"k1": "pro:acme"})
    code, body = _get("/v1/pro/nope", headers={"X-API-Key": "k1"})
    assert code == 404


# --- pro bucket pool is a separate pool ----------------------------------------
def test_pro_bucket_pool_is_separate_from_anonymous(monkeypatch):
    monkeypatch.setattr(api, "KEYS", {"prok": "pro:acme"})
    monkeypatch.setattr(api, "BUCKETS", api._Buckets(60, 1))
    monkeypatch.setattr(api, "PRO_BUCKETS", api._Buckets(600, 1))
    # exhaust the anonymous pool (burst=1)
    assert _get("/")[0] == 200
    assert _get("/")[0] == 429
    # a pro-keyed request draws from the OTHER pool — must still succeed
    assert _get("/", headers={"X-API-Key": "prok"})[0] == 200
    # exhausting the pro pool (burst=1 too) doesn't touch the anon one either
    assert _get("/", headers={"X-API-Key": "prok"})[0] == 429
    assert _get("/")[0] == 429           # still exhausted from before, unaffected


# --- pro_scores: latest measured_date wins, via REAL SQL --------------------------
def test_pro_scores_latest_batch_wins_over_an_older_measured_date(
        tmp_path, monkeypatch):
    import storage
    store = storage.open_store(str(tmp_path / "t.db"))
    old = {"score": 40.0, "grade": "D", "provisional": False,
          "snapshot_date": "2026-08-01", "tx_count": 10, "unique_payers": 5,
          "self_pay_ratio": 0.0, "listed": True, "components": {},
          "score_version": 4}
    new_a = dict(old, score=90.0, grade="A", snapshot_date="2026-08-08")
    new_b = {"score": 55.0, "grade": "C", "provisional": False,
            "snapshot_date": "2026-08-08", "tx_count": 3, "unique_payers": 2,
            "self_pay_ratio": 0.0, "listed": True, "components": {},
            "score_version": 4}
    store.record_seller_score("2026-08-01", "base", SELLER, old, "t")
    store.record_seller_score("2026-08-08", "base", SELLER, new_a, "t")
    store.record_seller_score("2026-08-08", "base", SELLER_B, new_b, "t")
    store.db.commit()
    monkeypatch.setitem(api._db, "store", store)

    code, obj = api.pro_scores("base")
    assert code == 200
    assert obj["count"] == 2                        # not 3 — old batch excluded
    by_seller = {s["seller"]: s for s in obj["sellers"]}
    assert by_seller[SELLER]["grade"] == "A"         # newest wins, not D
    assert by_seller[SELLER]["measured_date"] == "2026-08-08"
    assert by_seller[SELLER_B]["grade"] == "C"
    assert obj["snapshot"]["snapshot_date"] == "2026-08-08"
    store.db.close()


def test_pro_scores_bad_chain():
    code, obj = api.pro_scores("ethereum")
    assert code == 400 and obj["error"] == "bad chain"


def test_pro_scores_empty_chain_has_null_snapshot(monkeypatch):
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    code, obj = api.pro_scores("base")
    assert code == 200 and obj["sellers"] == [] and obj["snapshot"] is None


# --- pro_scores_history: limit clamp + address validation ---------------------
def test_history_limit_clamped_to_max_365(monkeypatch):
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    code, obj = api.pro_scores_history("base", SELLER, "99999")
    assert code == 200 and obj["limit"] == 365


def test_history_limit_defaults_to_90_on_garbage_input(monkeypatch):
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    code, obj = api.pro_scores_history("base", SELLER, "not-a-number")
    assert code == 200 and obj["limit"] == 90


def test_history_limit_defaults_to_90_when_absent(monkeypatch):
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    code, obj = api.pro_scores_history("base", SELLER, None)
    assert code == 200 and obj["limit"] == 90


def test_history_limit_floors_at_1(monkeypatch):
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    code, obj = api.pro_scores_history("base", SELLER, "-5")
    assert code == 200 and obj["limit"] == 1


def test_history_address_validation_rejects_garbage():
    code, obj = api.pro_scores_history("base", "not-an-address", "10")
    assert code == 400 and obj["error"] == "bad address"


def test_history_address_validation_accepts_checksummed_hex(monkeypatch):
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    mixed = "0x" + SELLER[2:].upper()
    code, obj = api.pro_scores_history("base", mixed, "10")
    assert code == 200 and obj["seller"] == SELLER      # lowercased, like trust_lookup


def test_history_bad_chain():
    code, obj = api.pro_scores_history("ethereum", SELLER, "10")
    assert code == 400 and obj["error"] == "bad chain"


def test_history_returns_full_time_series_newest_first(monkeypatch):
    rows = [
        (SELLER, 90.0, "A", 0, 100, 20, 0.0, "2026-08-08", "2026-08-08"),
        (SELLER, 80.0, "B", 0, 90, 18, 0.0, "2026-08-07", "2026-08-07"),
    ]
    monkeypatch.setattr(api, "_query", lambda *a, **k: rows)
    code, obj = api.pro_scores_history("base", SELLER, "90")
    assert code == 200
    assert obj["count"] == 2
    assert [h["measured_date"] for h in obj["history"]] == [
        "2026-08-08", "2026-08-07"]
    assert obj["history"][0]["grade"] == "A"


# --- pro_delivery: reuses the existing cached loader ---------------------------
def test_pro_delivery_reuses_delivery_map_and_carries_settlement_tx(
        monkeypatch):
    tx = "0x" + "11" * 32
    states = {
        ("base", "0x" + "aa" * 20): {"status": "delivery_verified",
                                     "as_of": "2026-08-07"},
        ("base", "0x" + "bb" * 20): {"status": "charged_unserved",
                                     "as_of": "2026-08-07",
                                     "settlement_tx": tx, "http_status": 500},
        ("polygon", "0x" + "cc" * 20): {"status": "delivery_verified"},
    }
    calls = {"n": 0}

    def fake_map():
        calls["n"] += 1
        return states

    monkeypatch.setattr(api, "_delivery_map", fake_map)
    code, obj = api.pro_delivery("base")
    assert code == 200
    assert obj["count"] == 2
    assert "0x" + "cc" * 20 not in obj["sellers"]     # other chain filtered out
    assert obj["sellers"]["0x" + "bb" * 20]["settlement_tx"] == tx
    assert obj["sellers"]["0x" + "bb" * 20]["http_status"] == 500
    assert calls["n"] == 1                            # loader called exactly once


def test_pro_delivery_bad_chain():
    code, obj = api.pro_delivery("ethereum")
    assert code == 400 and obj["error"] == "bad chain"


def test_delivery_and_pro_delivery_share_one_cache(tmp_path, monkeypatch):
    """_delivery() (single lookup) and pro_delivery() (bulk) must not each
    parse receipts.jsonl — both go through _delivery_map()."""
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(
        json.dumps({"ts": "2026-08-07T00:00:00Z", "network": "base",
                    "pay_to": "0x" + "aa" * 20, "outcome": "delivered",
                    "url": "https://x.example"}) + "\n")
    monkeypatch.setattr(api, "RECEIPTS_PATH", receipts)
    monkeypatch.setitem(api._delivery_cache, "mtime", None)
    monkeypatch.setitem(api._delivery_cache, "states", {})

    calls = {"n": 0}
    real_states = api._delivery_states

    def counting_states(lines):
        calls["n"] += 1
        return real_states(lines)

    monkeypatch.setattr(api, "_delivery_states", counting_states)
    api._delivery("base", "0x" + "aa" * 20)
    api.pro_delivery("base")
    assert calls["n"] == 1                            # parsed once, cached after


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

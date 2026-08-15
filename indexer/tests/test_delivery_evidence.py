"""GET /v1/delivery/{chain}/{address} — merona's premium delivery-evidence
endpoint: per-seller LATEST delivery state (the same block trust_lookup's
`delivery` field carries) plus the FULL paid-probe history with settlement
txs. Priced above a score lookup (delivery vs the nominal score default) via x402, or free with
any configured API key — distinct from the Pro tier's bulk, latest-state-only
GET /v1/pro/delivery.

Follows test_pro_tier.py's convention: import trust_api directly (DB and
x402pay's facilitator calls are both lazy at import time) and drive the REAL
Handler.do_GET through a bare instance (no socket) for route/wiring tests.
"""
from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                    # indexer/
sys.path.insert(0, str(HERE.parent.parent / "serving"))

import trust_api as api                                   # noqa: E402
import x402pay                                            # noqa: E402

SELLER = "0x" + "ab" * 20
SELLER_B = "0x" + "cd" * 20
PAYTO = "0x" + "fe" * 20
SOL = "FZAdmQBEeSXFkAsGtvNa8TS8jP9UkKC2sVWEo7VBQ7Ep"          # mixed-case base58
EVM_CHECKSUM_MIXED = "0x" + "aB" * 20                          # same wallet, mixed case
FAKE_PAYMENT = base64.b64encode(json.dumps(
    {"x402Version": 1, "scheme": "exact", "network": "base",
     "payload": {"signature": "0xsig", "authorization": {}}}).encode()).decode()


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


class _FakeFacilitator:
    """Stands in for x402pay.requests — records the requirements body each
    verify/settle call was made with, so tests can assert the SAME price was
    used for the 402 advertisement and the settle() call."""
    def __init__(self):
        self.verify_bodies = []
        self.settle_bodies = []
        self.verify_ok = True
        self.settle_ok = True

    def post(self, url, json=None, timeout=None):
        class R:
            def __init__(r, d): r._d = d
            def json(r): return r._d
        if url.endswith("/verify"):
            self.verify_bodies.append(json)
            return R({"isValid": self.verify_ok,
                      "invalidReason": None if self.verify_ok else "bad sig"})
        self.settle_bodies.append(json)
        return R({"success": self.settle_ok, "transaction": "0x" + "11" * 32,
                  "network": "base", "payer": "0x" + "77" * 20,
                  "errorReason": None if self.settle_ok else "insufficient"})


def _reset_delivery_cache(monkeypatch, receipts_path):
    monkeypatch.setattr(api, "RECEIPTS_PATH", receipts_path)
    monkeypatch.setitem(api._delivery_cache, "mtime", None)
    monkeypatch.setitem(api._delivery_cache, "states", {})
    monkeypatch.setitem(api._delivery_cache, "history", {})


# --- Part 1: per-route pricing in x402pay.py -----------------------------------
def test_requirements_default_price_matches_module_price_usd():
    req = x402pay.requirements("https://x/y", "d")
    assert req["maxAmountRequired"] == str(int(round(x402pay.PRICE_USD * 1e6)))
    assert req["maxAmountRequired"] == "1000"


def test_requirements_price_usd_overrides_default():
    req = x402pay.requirements("https://x/y", "d", price_usd=0.01)
    assert req["maxAmountRequired"] == "10000"


def test_payment_required_threads_price_usd_into_accepts():
    body = x402pay.payment_required("https://x/y", "d", price_usd=0.01)
    assert body["accepts"][0]["maxAmountRequired"] == "10000"
    body2 = x402pay.payment_required("https://x/y", "d")
    assert body2["accepts"][0]["maxAmountRequired"] == "1000"


def test_settle_uses_price_usd_in_its_verify_and_settle_bodies(monkeypatch):
    fac = _FakeFacilitator()
    monkeypatch.setattr(x402pay, "PAYTO", PAYTO)
    monkeypatch.setattr(x402pay, "requests", fac)
    ok, receipt, payer, err = x402pay.settle(
        FAKE_PAYMENT, "https://x/y", "d", price_usd=0.01)
    assert ok and err is None
    assert fac.verify_bodies[0]["paymentRequirements"]["maxAmountRequired"] == "10000"
    assert fac.settle_bodies[0]["paymentRequirements"]["maxAmountRequired"] == "10000"


def test_settle_backward_compatible_without_price_usd(monkeypatch):
    """Existing call sites that never pass price_usd keep working unchanged."""
    fac = _FakeFacilitator()
    monkeypatch.setattr(x402pay, "PAYTO", PAYTO)
    monkeypatch.setattr(x402pay, "requests", fac)
    ok, receipt, payer, err = x402pay.settle(FAKE_PAYMENT, "https://x/y", "d")
    assert ok and err is None
    assert fac.settle_bodies[0]["paymentRequirements"]["maxAmountRequired"] == "1000"


# --- Part 2: trust_api._pay_params + payable-prefix wiring ---------------------
def test_pay_params_delivery_path_is_price_aware():
    price, desc = api._pay_params(f"/v1/delivery/base/{SELLER}")
    assert price == api.DELIVERY_PRICE_USD == 0.001
    assert desc == api.DELIVERY_PAY_DESC
    assert desc != api.PAY_DESC


def test_pay_params_non_delivery_path_uses_module_default():
    price, desc = api._pay_params(f"/v1/trust/base/{SELLER}")
    assert price is None
    assert desc == api.PAY_DESC


def test_delivery_prefix_is_payable_via_x402(monkeypatch):
    """With FREE_MODE off, an unpaid GET on /v1/delivery/... must 402 (not
    401), at the delivery price — proving /v1/delivery/ was added to the
    payable prefix tuple and that the 402 advertisement is price-aware. A
    configured (but non-matching) key forces the anonymous/unpaid lane, same
    as test_mcp_x402."""
    monkeypatch.setattr(api, "FREE_MODE", False)
    monkeypatch.setattr(x402pay, "PAYTO", PAYTO)
    monkeypatch.setattr(api, "KEYS", {"k1": "tester"})
    code, body = _get(f"/v1/delivery/base/{SELLER}")
    assert code == 402
    assert body["accepts"][0]["maxAmountRequired"] == str(
        int(round(api.DELIVERY_PRICE_USD * 1e6)))
    assert body["accepts"][0]["description"] == api.DELIVERY_PAY_DESC


def test_trust_prefix_still_prices_at_the_score_default(monkeypatch):
    monkeypatch.setattr(api, "FREE_MODE", False)
    monkeypatch.setattr(x402pay, "PAYTO", PAYTO)
    monkeypatch.setattr(api, "KEYS", {"k1": "tester"})
    code, body = _get(f"/v1/trust/base/{SELLER}")
    assert code == 402
    assert body["accepts"][0]["maxAmountRequired"] == str(
        int(round(x402pay.PRICE_USD * 1e6)))
    assert body["accepts"][0]["description"] == api.PAY_DESC


def test_free_mode_serves_unpaid_delivery_without_402(tmp_path, monkeypatch):
    """Free-for-now default (2026-08-15): an anonymous GET with no key and no
    X-PAYMENT header is SERVED — 200 with a pricing note pointing at the
    optional receipts lane — and the facilitator is never contacted. The two
    tests above prove X402_FREE_MODE=0 restores the hard 402 paywall."""
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(json.dumps({
        "ts": "2026-08-07T00:00:00Z", "network": "base", "pay_to": SELLER,
        "outcome": "delivered", "url": "https://s.example/x",
        "amount_base_units": 20000,
        "settlement": {"transaction": "0x" + "22" * 32}}) + "\n")
    _reset_delivery_cache(monkeypatch, receipts)
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    monkeypatch.setattr(api, "KEYS", {"k1": "tester"})
    fac = _FakeFacilitator()
    monkeypatch.setattr(x402pay, "PAYTO", PAYTO)
    monkeypatch.setattr(x402pay, "requests", fac)
    assert api.FREE_MODE                       # the shipped default is free

    code, body = _get(f"/v1/delivery/base/{SELLER}")
    assert code == 200, body
    assert body["delivery"]["status"] == "delivery_verified"
    assert body["pricing"].startswith("free — no key, no signup")
    assert f"${x402pay.PRICE_USD:g}" in body["pricing"]
    assert fac.verify_bodies == [] and fac.settle_bodies == []


def test_delivery_paid_lane_settles_at_delivery_price_end_to_end(
        tmp_path, monkeypatch):
    """Full 402 -> pay -> 200 round trip on /v1/delivery/, asserting the
    facilitator saw the SAME delivery price in both verify and settle — the
    exact hazard the task calls out (mismatched price -> facilitator
    rightly rejects)."""
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(json.dumps({
        "ts": "2026-08-07T00:00:00Z", "network": "base", "pay_to": SELLER,
        "outcome": "delivered", "url": "https://s.example/x",
        "amount_base_units": 20000,
        "settlement": {"transaction": "0x" + "22" * 32}}) + "\n")
    _reset_delivery_cache(monkeypatch, receipts)
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    monkeypatch.setattr(api, "KEYS", {"k1": "tester"})
    fac = _FakeFacilitator()
    monkeypatch.setattr(x402pay, "PAYTO", PAYTO)
    monkeypatch.setattr(x402pay, "requests", fac)

    code, body = _get(f"/v1/delivery/base/{SELLER}",
                      headers={"X-PAYMENT": FAKE_PAYMENT})
    assert code == 200, body
    assert body["delivery"]["status"] == "delivery_verified"
    delivery_units = str(int(round(api.DELIVERY_PRICE_USD * 1e6)))
    assert fac.verify_bodies[0]["paymentRequirements"]["maxAmountRequired"] == delivery_units
    assert fac.settle_bodies[0]["paymentRequirements"]["maxAmountRequired"] == delivery_units


def test_delivery_paid_lane_settles_on_not_evaluated_200(tmp_path, monkeypatch):
    """x402#2300 commitment #2 turned the empty-history case from a bare 404
    into a billable 200 (not_evaluated) — unlike the old 404, THIS response
    DOES trigger settle(), same as any other payable 200. discovery_basis is
    itself the fact being sold (see delivery_lookup's docstring) — it isn't
    a placeholder for "nothing to report.\""""
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text("")
    _reset_delivery_cache(monkeypatch, receipts)
    monkeypatch.setattr(api, "KEYS", {"k1": "tester"})
    fac = _FakeFacilitator()
    monkeypatch.setattr(x402pay, "PAYTO", PAYTO)
    monkeypatch.setattr(x402pay, "requests", fac)

    code, body = _get(f"/v1/delivery/base/{SELLER}",
                      headers={"X-PAYMENT": FAKE_PAYMENT})
    assert code == 200
    assert body["status"] == "not_evaluated"
    assert fac.settle_bodies != []          # unlike the old 404, this settles


def test_delivery_paid_lane_never_settles_on_400(monkeypatch):
    """answers-first-settles-only-on-200 still holds for genuine errors: a
    bad chain/address never reaches delivery_lookup's not_evaluated branch
    at all, so it must still never settle."""
    monkeypatch.setattr(api, "KEYS", {"k1": "tester"})
    fac = _FakeFacilitator()
    monkeypatch.setattr(x402pay, "PAYTO", PAYTO)
    monkeypatch.setattr(x402pay, "requests", fac)

    code, body = _get(f"/v1/delivery/ethereum/{SELLER}",
                      headers={"X-PAYMENT": FAKE_PAYMENT})
    assert code == 400
    assert fac.settle_bodies == []


def test_delivery_free_with_api_key(tmp_path, monkeypatch):
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(json.dumps({
        "ts": "2026-08-07T00:00:00Z", "network": "base", "pay_to": SELLER,
        "outcome": "delivered", "url": "https://s.example/x"}) + "\n")
    _reset_delivery_cache(monkeypatch, receipts)
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    monkeypatch.setattr(api, "KEYS", {"k1": "tester"})
    code, body = _get(f"/v1/delivery/base/{SELLER}",
                      headers={"X-API-Key": "k1"})
    assert code == 200
    assert body["seller"] == SELLER


# --- Part 3: delivery_lookup ---------------------------------------------------
def test_delivery_lookup_bad_chain():
    code, body = api.delivery_lookup("ethereum", SELLER)
    assert code == 400 and body["error"] == "bad chain"


def test_delivery_lookup_bad_address():
    code, body = api.delivery_lookup("base", "not-an-address")
    assert code == 400 and body["error"] == "bad address"


def test_delivery_lookup_not_evaluated_when_no_probes_recorded(tmp_path,
                                                               monkeypatch):
    """x402#2300 commitment #2: an address with no paid-probe history is now
    an explicit 200 not_evaluated, not a bare 404 — distinct from probed-
    but-inconclusive (see the history/counts tests below, which DO get a
    404-free 200 with `probes`)."""
    monkeypatch.setattr(api, "DISCOVERY_BASIS", None)      # see basis tests below
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text("")
    _reset_delivery_cache(monkeypatch, receipts)
    code, body = api.delivery_lookup("base", SELLER)
    assert code == 200
    assert body == {"status": "not_evaluated",
                    "note": "address was not in the discovery set that fed "
                            "the paid sweep — distinct from "
                            "probed-but-inconclusive",
                    "probe_coverage": True}


# --- discovery_basis (x402-foundation/x402#2300 commitment #1) -------------------
def test_delivery_not_evaluated_omits_discovery_basis_when_none(tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(api, "DISCOVERY_BASIS", None)
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text("")
    _reset_delivery_cache(monkeypatch, receipts)
    code, body = api.delivery_lookup("base", SELLER)
    assert code == 200 and "discovery_basis" not in body


def test_delivery_not_evaluated_carries_discovery_basis_when_configured(
        tmp_path, monkeypatch):
    fake_basis = {"catalog": "bazaar", "snapshot_sha256": "b" * 64,
                  "snapshot_date": "2026-08-08"}
    monkeypatch.setattr(api, "DISCOVERY_BASIS", fake_basis)
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text("")
    _reset_delivery_cache(monkeypatch, receipts)
    code, body = api.delivery_lookup("base", SELLER)
    assert code == 200
    assert body["status"] == "not_evaluated"
    assert body["discovery_basis"] == fake_basis


def test_delivery_probed_response_carries_discovery_basis_additively(
        tmp_path, monkeypatch):
    """A probed seller's response keeps every existing field AND gains
    discovery_basis — additive, per the task: probed-address responses
    stay unchanged apart from this one new key."""
    fake_basis = {"catalog": "bazaar", "snapshot_sha256": "c" * 64,
                  "snapshot_date": "2026-08-08"}
    monkeypatch.setattr(api, "DISCOVERY_BASIS", fake_basis)
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(json.dumps({
        "ts": "2026-08-07T00:00:00Z", "network": "base", "pay_to": SELLER,
        "outcome": "delivered", "url": "https://s/x"}) + "\n")
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    _reset_delivery_cache(monkeypatch, receipts)
    code, body = api.delivery_lookup("base", SELLER)
    assert code == 200
    assert body["discovery_basis"] == fake_basis
    # every pre-existing field is still there, untouched
    for key in ("chain", "seller", "delivery", "probes", "counts",
               "verification", "disclaimer"):
        assert key in body


def test_delivery_lookup_history_newest_first_counts_and_amount_conversion(
        tmp_path, monkeypatch):
    receipts = tmp_path / "receipts.jsonl"
    rows = [
        {"ts": "2026-08-01T00:00:00Z", "network": "eip155:8453",
         "pay_to": SELLER, "outcome": "settled_no_content", "http_status": 405,
         "url": "https://s/x", "amount_base_units": 5000,
         "settlement": {"transaction": "0x" + "11" * 32}},
        {"ts": "2026-08-05T00:00:00Z", "network": "base",
         "pay_to": SELLER, "outcome": "rejected", "http_status": 402,
         "url": "https://s/x", "amount_base_units": None},
        {"ts": "2026-08-07T00:00:00Z", "network": "base",
         "pay_to": SELLER, "outcome": "delivered", "http_status": 200,
         "url": "https://s/x", "amount_base_units": 12345,
         "settlement": {"transaction": "0x" + "33" * 32}},
        # a different seller must never leak into SELLER's probes/counts
        {"ts": "2026-08-08T00:00:00Z", "network": "base",
         "pay_to": SELLER_B, "outcome": "delivered", "url": "https://other"},
    ]
    receipts.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    _reset_delivery_cache(monkeypatch, receipts)

    code, body = api.delivery_lookup("base", SELLER)
    assert code == 200
    assert body["chain"] == "base" and body["seller"] == SELLER
    ts_order = [p["ts"] for p in body["probes"]]
    assert ts_order == sorted(ts_order, reverse=True)          # newest-first
    assert len(body["probes"]) == 3                            # SELLER_B excluded
    assert body["counts"] == {"settled_no_content": 1, "rejected": 1,
                              "delivered": 1}
    delivered = next(p for p in body["probes"] if p["outcome"] == "delivered")
    assert delivered["amount_usdc"] == 0.012345
    assert delivered["settlement_tx"] == "0x" + "33" * 32
    rejected = next(p for p in body["probes"] if p["outcome"] == "rejected")
    assert rejected["amount_usdc"] is None

    # latest-status block must match _delivery()'s own answer for the same
    # seller — rejected (newest) carries no delivery info, so the latest
    # *settled* probe (delivered, 08-07) still wins.
    assert body["delivery"] == api._delivery("base", SELLER)
    assert body["delivery"]["status"] == "delivery_verified"

    assert "disclaimer" in body and "verification" in body
    assert body["verification"]["anchors"] == api.ANCHORS_URL


def test_delivery_lookup_history_capped_at_200(tmp_path, monkeypatch):
    receipts = tmp_path / "receipts.jsonl"
    rows = [{"ts": f"2026-01-01T00:00:00.{i:06d}Z",
            "network": "base", "pay_to": SELLER, "outcome": "delivered",
            "url": "https://s/x", "amount_base_units": 1000 + i}
           for i in range(250)]
    receipts.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    _reset_delivery_cache(monkeypatch, receipts)

    code, body = api.delivery_lookup("base", SELLER)
    assert code == 200
    assert len(body["probes"]) == api._DELIVERY_HISTORY_CAP == 200
    ts_order = [p["ts"] for p in body["probes"]]
    assert ts_order == sorted(ts_order, reverse=True)
    # the cap keeps the newest 200, so the very latest row must be present
    assert ts_order[0] == rows[-1]["ts"]


# --- reader unification: one loader call serves _delivery, pro_delivery, and
# the new history -----------------------------------------------------------
def test_delivery_lookup_and_pro_delivery_and_delivery_share_one_reader(
        tmp_path, monkeypatch):
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(json.dumps({
        "ts": "2026-08-07T00:00:00Z", "network": "base", "pay_to": SELLER,
        "outcome": "delivered", "url": "https://s/x"}) + "\n")
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    _reset_delivery_cache(monkeypatch, receipts)

    calls = {"n": 0}
    real_states = api._delivery_states

    def counting_states(lines):
        calls["n"] += 1
        return real_states(lines)

    monkeypatch.setattr(api, "_delivery_states", counting_states)
    api._delivery("base", SELLER)
    api.pro_delivery("base")
    api.delivery_lookup("base", SELLER)
    assert calls["n"] == 1                             # parsed once, cached after


# --- Part 4: settle_failed / settled_unconfirmed must never read as adverse ---
def test_delivery_lookup_never_publishes_charged_unserved_from_settle_failed(
        tmp_path, monkeypatch):
    """Public-credibility regression for the payprobe fix: a facilitator
    that reports success:false (settle_failed) or omits `success` entirely
    (settled_unconfirmed) must never surface here as charged_unserved — the
    raw outcome is still visible in `probes`/`counts` (it's real probe
    history), but `delivery` (the field trust_lookup/_eval_charged_unserved
    key off of) must not carry an adverse status from it."""
    for bad_outcome in ("settle_failed", "settled_unconfirmed"):
        receipts = tmp_path / f"receipts_{bad_outcome}.jsonl"
        receipts.write_text(json.dumps({
            "ts": "2026-08-07T00:00:00Z", "network": "base", "pay_to": SELLER,
            "outcome": bad_outcome, "http_status": 500,
            "url": "https://s.example/x",
            "settlement": {"transaction": "0x" + "44" * 32}}) + "\n")
        _reset_delivery_cache(monkeypatch, receipts)
        monkeypatch.setattr(api, "_query", lambda *a, **k: [])

        code, body = api.delivery_lookup("base", SELLER)
        assert code == 200, bad_outcome
        # no delivery verdict at all from this receipt standing alone
        assert body["delivery"] is None, bad_outcome
        assert body["counts"] == {bad_outcome: 1}, bad_outcome
        assert body["probes"][0]["outcome"] == bad_outcome, bad_outcome

        # and the trust-evaluation mapping (the FAIL-gate) must agree: no
        # record ever carries delivery.status == "charged_unserved" from
        # this, so _evaluate_seller_record can't route it to a FAIL either.
        record = {"delivery": body["delivery"], "grade": None, "score": None}
        result = api._evaluate_seller_record(200, record, "base")
        assert result["decision"] != "FAIL", bad_outcome
        assert result["reason_code"] != "charged_unserved", bad_outcome


def test_delivery_map_output_shape_unchanged_for_existing_consumers(
        tmp_path, monkeypatch):
    """_delivery_map() must keep returning the flat {(chain, seller): state}
    dict directly — not a tuple — so trust_lookup/_delivery/pro_delivery
    keep working unchanged."""
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(json.dumps({
        "ts": "2026-08-07T00:00:00Z", "network": "base", "pay_to": SELLER,
        "outcome": "delivered", "url": "https://s/x"}) + "\n")
    _reset_delivery_cache(monkeypatch, receipts)
    states = api._delivery_map()
    assert isinstance(states, dict)
    assert states[("base", SELLER)]["status"] == "delivery_verified"


# --- Part 5: solana receipts (2026-08-12 audit — public issue #2 + our own
# review) — _RECEIPT_CHAINS used to silently drop network="solana" receipts
# entirely, and the same ingest/lookup path lowercased EVERY pay_to
# unconditionally, which would have case-folded any base58 wallet into one
# no lookup could ever reproduce. Both are fixed by _norm_payto: EVM (0x…)
# lowercased, base58 case preserved — everywhere in the delivery path. -----
def test_solana_receipt_visible_via_delivery_map_case_folded_gets_nothing(
        tmp_path, monkeypatch):
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(json.dumps({
        "ts": "2026-08-07T00:00:00Z", "network": "solana", "pay_to": SOL,
        "outcome": "delivered", "url": "https://s.example/x"}) + "\n")
    _reset_delivery_cache(monkeypatch, receipts)
    # exact case: the receipt is there
    exact = api._delivery("solana", SOL)
    assert exact is not None and exact["status"] == "delivery_verified"
    # case-folded variant: nothing — a lowercased base58 key must never match
    assert api._delivery("solana", SOL.lower()) is None
    # and directly in the underlying map, both directions
    states = api._delivery_map()
    assert ("solana", SOL) in states
    assert ("solana", SOL.lower()) not in states


def test_solana_receipt_visible_via_delivery_lookup_case_folded_gets_nothing(
        tmp_path, monkeypatch):
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(json.dumps({
        "ts": "2026-08-07T00:00:00Z", "network": "solana", "pay_to": SOL,
        "outcome": "delivered", "url": "https://s.example/x"}) + "\n")
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    _reset_delivery_cache(monkeypatch, receipts)

    code, body = api.delivery_lookup("solana", SOL)
    assert code == 200
    assert body["seller"] == SOL                    # case preserved on the way out
    assert body["delivery"]["status"] == "delivery_verified"

    code2, body2 = api.delivery_lookup("solana", SOL.lower())
    assert code2 == 200
    assert body2["status"] == "not_evaluated"        # folded variant: nothing


def test_solana_caip2_network_form_ingests(tmp_path, monkeypatch):
    """The CAIP-2 mainnet id (genesis hash) must ingest exactly like the
    bare "solana" network string — _RECEIPT_CHAINS keys are lowercase
    because the lookup lowercases `network` before the dict get()."""
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(json.dumps({
        "ts": "2026-08-07T00:00:00Z",
        "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        "pay_to": SOL, "outcome": "delivered",
        "url": "https://s.example/x"}) + "\n")
    _reset_delivery_cache(monkeypatch, receipts)
    dstate = api._delivery("solana", SOL)
    assert dstate is not None and dstate["status"] == "delivery_verified"


def test_evm_stays_case_insensitive_checksummed_lookup_finds_lowercased_receipt(
        tmp_path, monkeypatch):
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(json.dumps({
        "ts": "2026-08-07T00:00:00Z", "network": "base", "pay_to": SELLER,
        "outcome": "delivered", "url": "https://s.example/x"}) + "\n")
    _reset_delivery_cache(monkeypatch, receipts)
    assert EVM_CHECKSUM_MIXED.lower() == SELLER
    dstate = api._delivery("base", EVM_CHECKSUM_MIXED)
    assert dstate is not None and dstate["status"] == "delivery_verified"


# --- Part 6: honest coverage — payprobe can only pay on base, so a chain
# outside PROBE_COVERAGE_CHAINS must say so rather than read as a clean
# record. -----------------------------------------------------------------
def test_delivery_lookup_polygon_reports_probe_coverage_false(
        tmp_path, monkeypatch):
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text("")
    _reset_delivery_cache(monkeypatch, receipts)
    code, body = api.delivery_lookup("polygon", SELLER)
    assert code == 200
    assert body["status"] == "not_evaluated"
    assert body["probe_coverage"] is False
    assert body["probed_chains"] == ["base"]
    assert "reason" in body and body["reason"]


def test_delivery_lookup_solana_reports_probe_coverage_false(
        tmp_path, monkeypatch):
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text("")
    _reset_delivery_cache(monkeypatch, receipts)
    code, body = api.delivery_lookup("solana", SOL)
    assert code == 200
    assert body["probe_coverage"] is False
    assert body["probed_chains"] == ["base"]


def test_delivery_lookup_base_with_receipt_reports_probe_coverage_true(
        tmp_path, monkeypatch):
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(json.dumps({
        "ts": "2026-08-07T00:00:00Z", "network": "base", "pay_to": SELLER,
        "outcome": "delivered", "url": "https://s/x"}) + "\n")
    monkeypatch.setattr(api, "_query", lambda *a, **k: [])
    _reset_delivery_cache(monkeypatch, receipts)
    code, body = api.delivery_lookup("base", SELLER)
    assert code == 200
    assert body["probe_coverage"] is True
    assert "probed_chains" not in body        # only stamped when NOT covered


def test_probe_coverage_chains_is_base_only():
    assert api.PROBE_COVERAGE_CHAINS == ("base",)


# --- Part 7: no advertisement couples "delivery" with polygon/solana without
# the coverage qualifier — shape-level grep over the spec/descriptor dicts
# and the MCP tool schema. -------------------------------------------------
def test_delivery_spec_never_lists_polygon_solana_without_coverage_qualifier():
    section = json.dumps(api.TRUST_API_SPEC["delivery"])
    if "polygon" in section or "solana" in section:
        assert "PROBE_COVERAGE_CHAINS" in section


def test_mcp_trust_score_delivery_description_states_base_only_coverage():
    tool = next(t for t in api.MCP_TOOLS if t["name"] == "trust_score")
    desc = tool["outputSchema"]["properties"]["delivery"]["description"]
    if "polygon" in desc or "solana" in desc:
        assert "base-only" in desc or "PROBE_COVERAGE_CHAINS" in desc


def test_landing_html_no_longer_overclaims_delivery_across_all_chains():
    # the old overclaim ("we pay sellers and record delivery" bundled with
    # all three chains, no qualifier) must be gone
    assert "deliver. Chains: base · polygon · solana." not in api.LANDING_HTML
    # the replacement must state the real, narrower coverage
    assert "polygon/solana probes don't exist yet" in api.LANDING_HTML
    assert "probe_coverage" in api.LANDING_HTML


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

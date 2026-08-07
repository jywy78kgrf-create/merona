"""Genuine-payer probe (attest/payprobe.py): the refusals are the product.

A paid probe that can be talked into paying the wrong party or the wrong
amount is worse than no probe — these tests pin every PolicyError branch,
the EIP-3009 signing round-trip, and the month-cap arithmetic.
"""
from __future__ import annotations

import base64
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "attest"))

pytest.importorskip("eth_account")
import payprobe                                            # noqa: E402
from eth_account import Account                            # noqa: E402
from eth_account.messages import encode_typed_data         # noqa: E402
from evm_chains import BASE                                # noqa: E402
from payprobe import PolicyError, select_accept            # noqa: E402

PAYER = "0x" + "aa" * 20
SELLER = "0x" + "bb" * 20


def offer(**kw):
    return {"network": "eip155:8453", "asset": BASE.usdc, "payTo": SELLER,
            "maxAmountRequired": 1000, "scheme": "exact", **kw}


def test_accepts_a_clean_base_usdc_offer():
    acc = select_accept([offer()], PAYER)
    assert acc["payTo"] == SELLER and acc["amount_units"] == 1000


def test_refuses_own_payto_and_self_and_relayer(monkeypatch):
    with pytest.raises(PolicyError, match="own payTo"):
        select_accept([offer(payTo=payprobe.OWN_PAYTO[0])], PAYER)
    with pytest.raises(PolicyError, match="payer wallet itself"):
        select_accept([offer(payTo=PAYER)], PAYER)
    monkeypatch.setattr(payprobe, "_relayers", lambda: {SELLER})
    with pytest.raises(PolicyError, match="relayer"):
        select_accept([offer()], PAYER)


def test_refuses_over_cap_never_negotiates():
    with pytest.raises(PolicyError, match="cap"):
        select_accept([offer(maxAmountRequired=payprobe.MAX_UNITS + 1)], PAYER)
    # a wrong-network or wrong-asset accept is skipped, not paid
    with pytest.raises(PolicyError):
        select_accept([offer(network="eip155:1"),
                       offer(asset="0x" + "cc" * 20)], PAYER)


def test_month_cap_reads_only_this_months_paid_rows(tmp_path, monkeypatch):
    ledger = tmp_path / "receipts.jsonl"
    now = datetime.now(timezone.utc)
    rows = [
        {"ts": now.isoformat(), "paid": True, "amount_base_units": 3000},
        {"ts": now.isoformat(), "paid": False, "amount_base_units": 999999},
        {"ts": "2020-01-01T00:00:00+00:00", "paid": True,
         "amount_base_units": 888888},
    ]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(payprobe, "LEDGER", ledger)
    assert payprobe.month_spend_units(now) == 3000


def test_authorization_signs_as_the_payer():
    acct = Account.create()
    domain = {"name": "USD Coin", "version": "2",
              "chainId": BASE.chain_id, "verifyingContract": BASE.usdc}
    wire, sig = payprobe.sign_authorization(acct, domain, SELLER, 1000, 300)
    assert wire["from"] == acct.address and wire["to"] == SELLER
    assert wire["value"] == "1000" and len(wire["nonce"]) == 66
    msg = encode_typed_data(domain, payprobe.AUTH_TYPES, {
        "from": wire["from"], "to": wire["to"], "value": int(wire["value"]),
        "validAfter": int(wire["validAfter"]),
        "validBefore": int(wire["validBefore"]),
        "nonce": bytes.fromhex(wire["nonce"][2:])})
    assert Account.recover_message(msg, signature=sig) == acct.address
    assert wire["validAfter"] == "0"                # v2 SDK convention
    assert int(wire["validBefore"]) > time.time()


def test_permit2_accepts_are_skipped_for_plain_3009():
    p2 = offer(extra={"assetTransferMethod": "permit2"})
    plain = offer(maxAmountRequired=2000)
    assert select_accept([p2, plain], PAYER)["amount_units"] == 2000
    with pytest.raises(PolicyError):
        select_accept([p2], PAYER)


def test_v2_amount_string_field_is_read():
    acc = offer(maxAmountRequired=None, amount="1500",
                extra={"name": "USD Coin", "version": "2"})
    assert select_accept([acc], PAYER)["amount_units"] == 1500


def test_v2_payment_header_wraps_accepted_and_resource():
    raw = offer(extra={"name": "USD Coin", "version": "2"})
    acc = {"payTo": SELLER, "_raw": raw, "scheme": "exact",
           "network": "eip155:8453"}
    off = {"x402Version": 2, "resource": {"url": "https://s/x"},
           "extensions": {"bazaar": {}}}
    name, val = payprobe.payment_header(off, acc, {"from": PAYER}, "0xsig")
    assert name == "PAYMENT-SIGNATURE"
    body = json.loads(base64.b64decode(val))
    assert body["accepted"] == raw                  # echoed verbatim
    assert body["resource"] == {"url": "https://s/x"}
    assert body["extensions"] == {"bazaar": {}}
    assert body["payload"]["authorization"]["from"] == PAYER
    assert "scheme" not in body                     # v2 has no flat scheme


def test_v1_payment_header_is_flat_x_payment():
    name, val = payprobe.payment_header(
        {"x402Version": 1}, offer(), {"from": PAYER}, "0xsig")
    assert name == "X-PAYMENT"
    body = json.loads(base64.b64decode(val))
    assert body["x402Version"] == 1 and body["scheme"] == "exact"
    assert body["network"] == "eip155:8453"


def test_sweep_targets_one_cheapest_concrete_url_per_seller():
    import paysweep
    rows = [
        {"network": "eip155:8453", "asset": payprobe.BASE.usdc,
         "resource_url": "https://a.com/x", "pay_to": SELLER,
         "amount_base_units": "5000"},
        {"network": "eip155:8453", "asset": payprobe.BASE.usdc,
         "resource_url": "https://a.com/cheap", "pay_to": SELLER,
         "amount_base_units": "1000"},
        {"network": "eip155:8453", "asset": payprobe.BASE.usdc,   # placeholder
         "resource_url": "https://a.com/u/:id", "pay_to": SELLER,
         "amount_base_units": "100"},
        {"network": "eip155:8453", "asset": payprobe.BASE.usdc,   # own payTo
         "resource_url": "https://own.com/x",
         "pay_to": payprobe.OWN_PAYTO[0], "amount_base_units": "1000"},
        {"network": "eip155:8453", "asset": payprobe.BASE.usdc,   # over cap
         "resource_url": "https://big.com/x", "pay_to": "0x" + "dd" * 20,
         "amount_base_units": str(payprobe.MAX_UNITS + 1)},
        {"network": "eip155:1", "asset": payprobe.BASE.usdc,      # wrong net
         "resource_url": "https://l1.com/x", "pay_to": "0x" + "ee" * 20,
         "amount_base_units": "1000"},
    ]
    targets = paysweep.build_targets(rows, payprobe.OWN_PAYTO)
    assert [t["url"] for t in targets] == ["https://a.com/cheap"]
    assert targets[0]["census_units"] == 1000


def test_sweep_resume_skips_recent_receipts(tmp_path, monkeypatch):
    import paysweep
    from datetime import datetime, timedelta, timezone
    ledger = tmp_path / "receipts.jsonl"
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=40)
    ledger.write_text(
        json.dumps({"ts": now.isoformat(), "pay_to": SELLER}) + "\n"
        + json.dumps({"ts": old.isoformat(), "pay_to": "0x" + "dd" * 20})
        + "\n")
    monkeypatch.setattr(payprobe, "LEDGER", ledger)
    seen = paysweep.recently_probed(30)
    assert SELLER in seen and ("0x" + "dd" * 20) not in seen


def test_classify_body_flags_json_error_shapes():
    ok, err = payprobe.classify_body(b'{"data": 1}', "application/json")
    assert ok and not err
    ok, err = payprobe.classify_body(b'{"error": "missing param"}',
                                     "application/json; charset=utf-8")
    assert ok and err
    ok, err = payprobe.classify_body(b"<html>", "text/html")
    assert not ok and not err


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

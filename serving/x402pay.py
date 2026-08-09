"""Server-side x402 payments for the trust API — the index becomes a
merchant on the rail it measures.

Flow (exact scheme, Base USDC, x402 v1):
  1. Client hits a paid endpoint with no key and no payment ->
     402 + `accepts` (payTo, amount, asset) — payment_required().
  2. Client retries with an X-PAYMENT header (base64 JSON: a signed EIP-3009
     transferWithAuthorization) -> we POST the payload + our advertised
     requirements to a facilitator: /verify (signature/funds check), then
     /settle (broadcasts on-chain). settle() returns the settle receipt,
     which the caller base64s into X-PAYMENT-RESPONSE.

Design constraints:
  - ENV-GATED: X402_PAYTO unset -> the whole path is disabled and the API
    behaves exactly as before (keys only). Deploying this code changes
    nothing until the wallet is configured.
  - Verify-then-settle happens ONLY after the lookup succeeded, so a client
    is never charged for a 404/503.
  - One settle attempt, no retry: a retried settle risks double-charging on
    an ambiguous timeout. Ambiguity fails CLOSED (client not served, and told
    settlement state is unknown).
  - Payloads are size-capped and parsed strictly; signatures are never
    logged (tx hash + payer are — they're public on-chain anyway).
"""
from __future__ import annotations

import base64
import binascii
import json
import os

import requests

PAYTO = (os.environ.get("X402_PAYTO") or "").strip().lower() or None
FACILITATOR = os.environ.get("X402_FACILITATOR",
                             "https://x402.org/facilitator").rstrip("/")
PRICE_USD = float(os.environ.get("X402_PRICE_USD", "0.005"))
NETWORK = os.environ.get("X402_NETWORK", "base")
# native USDC on Base (6 decimals)
ASSET = os.environ.get(
    "X402_ASSET", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913").lower()
TIMEOUT_S = float(os.environ.get("X402_FACILITATOR_TIMEOUT", "12"))
MAX_PAYLOAD = 16 * 1024


def enabled() -> bool:
    return PAYTO is not None


def requirements(resource: str, description: str,
                 price_usd: float | None = None) -> dict:
    """The single accepts-entry we advertise AND verify against — the same
    dict must be used in both places or the facilitator rightly rejects.

    price_usd overrides the module-wide PRICE_USD for this one entry (e.g. a
    route priced differently from the default). None -> PRICE_USD, exactly
    the prior behavior, so every existing call site is unaffected."""
    price = PRICE_USD if price_usd is None else price_usd
    return {
        "scheme": "exact",
        "network": NETWORK,
        "maxAmountRequired": str(int(round(price * 1e6))),
        "resource": resource,
        "description": description,
        "mimeType": "application/json",
        "payTo": PAYTO,
        "maxTimeoutSeconds": 60,
        "asset": ASSET,
        "extra": {"name": "USD Coin", "version": "2"},
    }


def payment_required(resource: str, description: str,
                     price_usd: float | None = None) -> dict:
    return {"x402Version": 1,
            "error": "payment required",
            "accepts": [requirements(resource, description, price_usd)]}


def settle(payment_b64: str, resource: str, description: str,
          price_usd: float | None = None):
    """(ok, receipt_b64_or_None, payer_or_None, client_error_msg).
    Facilitator errors surface as client-facing strings without internals.

    price_usd MUST match what was advertised in the 402 for this resource —
    requirements() builds the verify/settle body from it, so a mismatch
    between what a caller advertised and what it settles at makes the
    facilitator rightly reject the payment."""
    if not enabled():
        return False, None, None, "x402 not enabled"
    if not payment_b64 or len(payment_b64) > MAX_PAYLOAD:
        return False, None, None, "invalid X-PAYMENT header"
    try:
        payload = json.loads(base64.b64decode(payment_b64, validate=True))
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, binascii.Error, json.JSONDecodeError):
        return False, None, None, "X-PAYMENT is not valid base64 JSON"

    req = requirements(resource, description, price_usd)
    body = {"x402Version": 1, "paymentPayload": payload,
            "paymentRequirements": req}
    try:
        v = requests.post(f"{FACILITATOR}/verify", json=body,
                          timeout=TIMEOUT_S).json()
    except Exception:
        return False, None, None, "payment verification unavailable"
    if not (v.get("isValid") or v.get("valid")):
        return False, None, None, (
            "payment invalid: "
            + str(v.get("invalidReason") or v.get("error") or "rejected")[:120])

    # single settle attempt — an ambiguous outcome fails closed
    try:
        s = requests.post(f"{FACILITATOR}/settle", json=body,
                          timeout=TIMEOUT_S).json()
    except Exception:
        return False, None, None, ("settlement outcome unknown — do not "
                                   "retry the same authorization; check "
                                   "on-chain before paying again")
    if not s.get("success"):
        return False, None, None, (
            "settlement failed: " + str(s.get("errorReason")
                                        or s.get("error") or "unknown")[:120])
    receipt = base64.b64encode(json.dumps(s).encode()).decode()
    payer = (s.get("payer") or "")[:64] or None
    return True, receipt, payer, None

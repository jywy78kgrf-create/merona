"""RFC 8785 JSON Canonicalisation Scheme (JCS) — minimal, for the JSON types
this API actually emits: dict, list, str, int, float, bool, None.

WHY this exists instead of `json.dumps(obj, sort_keys=True)`: JCS is a wire
CONTRACT (pinned by the envelope as urn:x402:canonicalisation:jcs-rfc8785-v1)
— a verifier on a different language/runtime must re-derive the exact same
byte string from the same JSON value, or the Ed25519 signature won't check
out. `sort_keys=True` gets object-key order right but leaves two things
unspecified that JCS pins precisely:
  1. NUMBER formatting — JSON allows "1.50", "1.5e0", "1.5" for the same
     value; JCS requires ECMAScript's Number::toString shortest-round-trip
     form (RFC 8785 §3.2.2.3), so every implementation picks the same digits.
  2. STRING escaping — JCS requires literal UTF-8 for every character above
     U+001F (RFC 8785 §3.2.2.2), where `json.dumps`'s default `ensure_ascii=
     True` would \\u-escape anything non-ASCII, producing different bytes.

Not a general JCS library: no support for the Python types this API never
emits (bytes, tuples-as-non-arrays, Decimal, etc.) — deliberately narrow so
every code path here is exercised by real trust-evaluate payloads.
"""
from __future__ import annotations

import math
from decimal import Decimal

__all__ = ["canonicalize", "canonicalize_bytes"]


# --- string encoding (RFC 8785 §3.2.2.2, via the ECMAScript JSON.stringify
# quoting algorithm it points to) ------------------------------------------
# Only control characters (U+0000-U+001F) are escaped, using the short
# two-char forms where ECMAScript defines one and \u00XX (lowercase hex)
# otherwise. " and \\ always need escaping (they'd otherwise terminate/alter
# the string). Everything else — including all non-ASCII text — is emitted
# as the literal UTF-8 character, NOT a \u escape: this is the opposite of
# json.dumps's default ensure_ascii=True and is where a naive canonicaliser
# usually first diverges from JCS.
_SHORT_ESCAPES = {
    0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r",
    0x22: '\\"', 0x5C: "\\\\",
}


def _encode_string(s: str) -> str:
    out = ['"']
    for ch in s:
        cp = ord(ch)
        esc = _SHORT_ESCAPES.get(cp)
        if esc is not None:
            out.append(esc)
        elif cp < 0x20:
            out.append(f"\\u{cp:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


# --- number encoding (RFC 8785 §3.2.2.3: ECMAScript Number::toString) ------
# JCS treats every JSON number as an IEEE-754 double and requires the
# shortest decimal string that round-trips back to that double, formatted by
# the classic ECMA-262 algorithm (old §7.1.12.1 / current §6.1.6.1.20):
# given the shortest significant-digit string `s` (k digits) and `n` such
# that the value equals s * 10**(n-k), plain decimal notation is used for
# -6 < n <= 21 and exponential notation ("de+NN"/"d.ddde+NN") otherwise.
#
# CPython's `repr(float)` already computes the shortest round-trip decimal
# digit string (has since 3.1, via David Gay's / Grisu-style dtoa) — we
# don't need to reimplement shortest-digit-string search, only REFORMAT
# python's chosen digits/exponent into the ECMAScript layout, which differs
# from Python's own repr formatting (different plain/exponential thresholds,
# "e+05" vs "e+5", etc). Decimal(repr(x)) parses that exact digit string
# back into a lossless (digits, exponent) pair to reformat from.
def _es_number_to_string(x: float) -> str:
    if math.isnan(x) or math.isinf(x):
        raise ValueError(f"jcs: cannot canonicalise non-finite float {x!r} "
                         "(NaN/Infinity have no JSON representation)")
    if x == 0.0:
        # +0.0 and -0.0 both stringify to "0" in ECMAScript — the sign bit
        # is not observable in Number::toString.
        return "0"
    sign = "-" if x < 0 else ""
    _sign, digits, exp = Decimal(repr(abs(x))).as_tuple()
    digits = list(digits)
    # Strip trailing zero digits (each one bumps the exponent by one): JCS's
    # `s` is defined as the MINIMAL digit string, but repr()'s decimal text
    # can carry trailing zeros (e.g. repr(100.0) == "100.0" -> digits
    # (1,0,0,0)) that the shortest-`k` definition requires removed.
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exp += 1
    k = len(digits)
    n = exp + k
    s = "".join(str(d) for d in digits)
    if k <= n <= 21:
        body = s + "0" * (n - k)                      # plain integer form
    elif 0 < n <= 21:
        body = s[:n] + "." + s[n:]                     # plain, point inside s
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + s                   # plain, point before s
    else:
        e = n - 1
        mantissa = s if k == 1 else s[0] + "." + s[1:]
        body = mantissa + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + body


def _encode(value) -> str:
    # bool MUST be checked before int (bool is an int subclass in Python).
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, int):
        # No decimal point, exact arbitrary-precision text — never routed
        # through the ES-double reformatter above (which would risk lossy
        # rounding on integers outside the 2**53 safe-integer range).
        return str(value)
    if isinstance(value, float):
        return _es_number_to_string(value)
    if isinstance(value, dict):
        # RFC 8785 §3.2.3: object members sorted by their UTF-16 CODE UNIT
        # sequence, not by Unicode code point. For the Basic Multilingual
        # Plane (U+0000-U+FFFF) a code point IS its own UTF-16 code unit, so
        # plain `sorted(str)` already agrees with JCS — the two diverge only
        # for supplementary-plane keys (surrogate pairs), which start with a
        # code unit in D800-DBFF, numerically BELOW E000-FFFF BMP characters
        # even though the code point they encode is numerically ABOVE them.
        # This API never emits non-BMP object keys, but we sort on the
        # UTF-16BE encoding (not the str itself) so the comparison is
        # correct even if that ever changes, rather than leaving the BMP-only
        # shortcut baked in silently.
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(
            _encode_string(k) + ":" + _encode(v) for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(v) for v in value) + "]"
    raise TypeError(f"jcs: unsupported type for canonicalisation: {type(value)!r}")


def canonicalize(value) -> str:
    """RFC 8785 canonical JSON text for `value` (dict/list/str/int/float/
    bool/None, arbitrarily nested)."""
    return _encode(value)


def canonicalize_bytes(value) -> bytes:
    """canonicalize(value) as UTF-8 bytes — what actually gets hashed/signed."""
    return canonicalize(value).encode("utf-8")

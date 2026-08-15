"""Verify a facilitator self-declaration (registrar/SELF-DECLARATION.md).

  verify_declaration.py <entry.json>          # verify one facilitator entry
  verify_declaration.py --registry            # re-verify EVERY declared entry

<entry.json> is either a single facilitator object ({"<fid>": {...}}) or the
whole relayer_registry_declared.json. For each (chain, address) with a
signature, the canonical v1 message is rebuilt and the signature checked
against the relayer key itself — control of the key is the identity proof.

EVM chains need eth-account, Solana needs PyNaCl: pip install -e ".[registrar]"
A missing dependency is a loud per-entry FAIL, never a silent pass: this tool
exists so nobody lands in the registry unverified.

Exit code 0 only if every signature present checks out (and at least one was
checked). The x402-activity check (spec step 2) is deliberately manual.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MSG_V1 = ("merona facilitator declaration v1\n"
          "facilitator: {fid}\n"
          "chain: {chain}\n"
          "relayer: {address}\n"
          "contact: {contact}\n"
          "date: {date}")

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def build_message(fid: str, chain: str, address: str, contact: str,
                  date: str) -> str:
    """The exact five-line signing payload from the spec. Any deviation
    (ordering, case, whitespace) fails verification — canonical on purpose."""
    return MSG_V1.format(fid=fid, chain=chain, address=address,
                         contact=contact, date=date)


def b58decode(s: str) -> bytes:
    """Minimal base58 decode (Solana pubkeys) — avoids a dependency."""
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def verify_evm(message: str, address: str, signature: str) -> bool:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    recovered = Account.recover_message(encode_defunct(text=message),
                                        signature=signature)
    return recovered.lower() == address.lower()


def verify_solana(message: str, address: str, signature_b64: str) -> bool:
    import base64

    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
    try:
        VerifyKey(b58decode(address)).verify(
            message.encode(), base64.b64decode(signature_b64))
        return True
    except BadSignatureError:
        return False


def verify_entry(fid: str, entry: dict) -> tuple[int, int]:
    """Verify one facilitator's declared addresses. Returns (checked, failed)."""
    decl = entry.get("_declaration") or {}
    contact, date = decl.get("contact", ""), decl.get("date", "")
    sigs = decl.get("signatures") or {}
    checked = failed = 0
    for chain, lst in entry.items():
        if chain.startswith("_") or not isinstance(lst, list):
            continue
        for a in lst:
            address = a["address"]
            sig = sigs.get(f"{chain}:{address}")
            if not sig:
                print(f"  FAIL {fid} {chain}:{address} — no signature")
                failed += 1
                continue
            msg = build_message(fid, chain, address, contact, date)
            try:
                ok = (verify_solana(msg, address, sig) if chain == "solana"
                      else verify_evm(msg, address, sig))
            except ImportError as e:
                print(f"  FAIL {fid} {chain}:{address} — missing dependency "
                      f"({e.name}); pip install -e '.[registrar]'")
                failed += 1
                continue
            except Exception as e:
                print(f"  FAIL {fid} {chain}:{address} — {type(e).__name__}: {e}")
                failed += 1
                continue
            checked += 1
            if ok:
                print(f"  OK   {fid} {chain}:{address}")
            else:
                print(f"  FAIL {fid} {chain}:{address} — signature does not "
                      f"recover to the relayer key")
                failed += 1
    return checked, failed


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    if len(sys.argv) > 1 and sys.argv[1] == "--registry":
        src = root / "data" / "indexer" / "relayer_registry_declared.json"
    elif len(sys.argv) > 1:
        src = Path(sys.argv[1])
    else:
        print(__doc__)
        sys.exit(2)
    j = json.loads(src.read_text())
    entries = j.get("facilitators", j)   # whole file or bare {fid: entry}
    total_checked = total_failed = 0
    for fid, entry in entries.items():
        if fid.startswith("_"):
            continue
        c, f = verify_entry(fid, entry)
        total_checked += c
        total_failed += f
    print(f"\n{total_checked} signature(s) verified, {total_failed} failure(s)")
    if total_failed or not total_checked:
        sys.exit(1)


if __name__ == "__main__":
    main()

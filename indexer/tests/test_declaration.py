"""Registrar self-declaration: canonical message, base58, registry merge, and
(when eth-account is installed) a real sign/recover round-trip."""

import json
from pathlib import Path

import pytest

from indexer.verify_declaration import (b58decode, build_message, verify_entry)

ROOT = Path(__file__).resolve().parents[2]


def test_message_is_exactly_the_spec_format():
    msg = build_message("anyspend", "avalanche", "0xAbC", "ops@x.com",
                        "2026-07-19")
    assert msg == ("merona facilitator declaration v1\n"
                   "facilitator: anyspend\n"
                   "chain: avalanche\n"
                   "relayer: 0xAbC\n"
                   "contact: ops@x.com\n"
                   "date: 2026-07-19")
    assert not msg.endswith("\n")        # no trailing newline, per spec


def test_b58decode_known_vector():
    # base58("StV1DL6CwTryKyV") == b"hello world" is the classic vector
    assert b58decode("StV1DL6CwTryKyV") == b"hello world"
    assert b58decode("1StV1DL6CwTryKyV") == b"\x00hello world"  # pad-1 rule


def test_entry_without_signature_fails_loudly(capsys):
    entry = {"avalanche": [{"address": "0xdead", "first_tx_date": None}],
             "_declaration": {"contact": "x", "date": "2026-07-19",
                              "signatures": {}}}
    checked, failed = verify_entry("f", entry)
    assert (checked, failed) == (0, 1)
    assert "no signature" in capsys.readouterr().out


def test_declared_file_parses_and_is_empty_scaffold():
    j = json.loads((ROOT / "data" / "indexer"
                    / "relayer_registry_declared.json").read_text())
    assert j["facilitators"] == {}
    assert "verify_declaration" in j["_note"]


def test_evm_sign_recover_roundtrip():
    eth_account = pytest.importorskip("eth_account")
    from eth_account.messages import encode_defunct
    acct = eth_account.Account.create()
    msg = build_message("testfac", "optimism", acct.address, "t@x", "2026-07-19")
    sig = acct.sign_message(encode_defunct(text=msg)).signature.hex()
    entry = {"optimism": [{"address": acct.address, "first_tx_date": None}],
             "_declaration": {"contact": "t@x", "date": "2026-07-19",
                              "signatures": {f"optimism:{acct.address}": sig}}}
    assert verify_entry("testfac", entry) == (1, 0)
    # tampered facilitator id -> recovery mismatch -> FAIL
    assert verify_entry("evil", entry) == (1, 1)

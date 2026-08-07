"""On-chain anchor attestation layer (attest/): encoding pins + policy gates.

The encoder talks to contracts merona doesn't control (the EAS predeploys),
so the selectors and the schema UID are PINNED as literals: a refactor that
drifts any of them would produce transactions that revert (or worse, silently
call nothing) — these tests make that a red bar instead.

Skipped wholesale when eth-account isn't installed (it's the optional
[attest]/[registrar] extra, same pattern as the registrar verifier).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                       # indexer/
sys.path.insert(0, str(HERE.parent.parent / "attest"))     # attest/

pytest.importorskip("eth_account")
import anchor_attest                                       # noqa: E402
import eas                                                 # noqa: E402
import wash                                                # noqa: E402
from eth_abi import decode                                 # noqa: E402

# Deterministic on-chain identifiers. If any of these change, deployed
# attestations and the registered schema stop lining up — that is a breaking
# schema revision (bump ANCHOR_SCHEMA_VERSION), never a silent edit.
PINNED_SCHEMA_UID = "366031b8f57a79710eea82a6f4dfffc1f35b46147a926e19ee5d7008b8703c8c"
PINNED_IDENTITY_UID = "64b514cece98b2fa70f1b5bf9087fd887c081e43f69a692cc923ee47021e1678"
SEL_ATTEST = "f17325e7"      # EAS.attest — matches the deployed contract
SEL_REVOKE = "46926267"      # EAS.revoke
SEL_REGISTER = "60d7a278"    # SchemaRegistry.register


def test_pinned_selectors_and_schema_uid():
    assert eas.SEL_ATTEST.hex() == SEL_ATTEST
    assert eas.SEL_REVOKE.hex() == SEL_REVOKE
    assert eas.SEL_REGISTER.hex() == SEL_REGISTER
    assert eas.schema_uid().hex() == PINNED_SCHEMA_UID
    assert eas.schema_uid(eas.IDENTITY_SCHEMA).hex() == PINNED_IDENTITY_UID


def test_anchor_data_roundtrips():
    d = eas.encode_anchor_data("2026-08-07", "0x" + "ab" * 32, "deadbeef")
    date, sha, head = decode(["string", "bytes32", "string"], d)
    assert (date, sha, head) == ("2026-08-07", b"\xab" * 32, "deadbeef")
    with pytest.raises(ValueError):
        eas.encode_anchor_data("2026-08-07", "ab" * 16, "x")   # not 32 bytes


def test_attest_calldata_decodes_and_is_revocable():
    data = eas.encode_anchor_data("2026-08-07", "cd" * 32, "head")
    calldata = eas.encode_attest(eas.schema_uid(), data)
    assert calldata[:4].hex() == SEL_ATTEST
    (uid, (recipient, expiration, revocable, ref, payload, value)), = \
        decode(eas.ATTEST_TYPES, calldata[4:])
    assert uid == eas.schema_uid()
    assert revocable is True and expiration == 0 and value == 0
    assert payload == data


def test_irrevocable_attestation_is_a_policy_error_not_a_parameter():
    data = eas.encode_anchor_data("2026-08-07", "cd" * 32, "head")
    with pytest.raises(ValueError, match="revocable"):
        eas.encode_attest(eas.schema_uid(), data, revocable=False)
    with pytest.raises(ValueError, match="revocable"):
        eas.encode_register(revocable=False)


def test_combined_sha_matches_anchor_sh_algorithm(tmp_path):
    # independent reimplementation of deploy/anchor.sh's hashing — the two
    # ledgers (git + chain) only cross-verify if this never drifts
    (tmp_path / "b.csv").write_bytes(b"world")
    (tmp_path / "a.csv").write_bytes(b"hello")
    (tmp_path / "sub").mkdir()                       # dirs ignored, like the shell
    files = {n: hashlib.sha256((tmp_path / n).read_bytes()).hexdigest()
             for n in ("a.csv", "b.csv")}
    expected = hashlib.sha256(
        "".join(f"{n}:{h}" for n, h in sorted(files.items())).encode()).hexdigest()
    assert anchor_attest.combined_sha(tmp_path) == expected
    assert anchor_attest.combined_sha(tmp_path / "sub") is None   # empty day


def test_revoked_anchor_day_becomes_attestable_again(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"type": "snapshot_anchor", "snapshot_date": "2026-08-05",
         "attestation_uid": "0x" + "aa" * 32},
        {"type": "snapshot_anchor", "snapshot_date": "2026-08-06",
         "attestation_uid": "0x" + "bb" * 32},
        {"type": "revocation", "attestation_uid": "0x" + "bb" * 32,
         "reason": "snapshot files corrected"},
    ]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(anchor_attest, "LEDGER", ledger)
    assert anchor_attest.attested_dates() == {"2026-08-05"}


def test_attestation_uid_extracted_from_receipt():
    uid_hex = "cc" * 32
    receipt = {"logs": [
        {"address": "0x0000000000000000000000000000000000000001",
         "topics": [eas.ATTESTED_TOPIC], "data": "0x" + "ee" * 32},   # not EAS
        {"address": eas.EAS_ADDRESS, "topics": [eas.ATTESTED_TOPIC],
         "data": "0x" + uid_hex},
    ]}
    assert eas.attestation_uid_from_receipt(receipt) == "0x" + uid_hex
    assert eas.attestation_uid_from_receipt({"logs": []}) is None


# ---- the mirror-image rule: our receipts never enter our clean numbers ------

def test_own_payto_excluded_from_clean_scope():
    rel = ["0x" + "11" * 20, "0x" + "22" * 20]
    exs = wash._excluded_sellers(rel)
    assert exs[:2] == rel
    for own in wash.OWN_PAYTO:
        assert own in exs
    # no duplicate placeholders when the payTo is already excluded
    assert len(wash._excluded_sellers(rel + wash.OWN_PAYTO)) == \
        len(rel) + len(wash.OWN_PAYTO)


def test_own_payto_default_is_the_published_wallet():
    # the exclusion must survive an unset env — a deploy without X402_OWN_PAYTO
    # still guards the published address
    assert "0x31ee6253e34df1ab3a45e00ffa9f5dc2be1040b6" in wash.OWN_PAYTO


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

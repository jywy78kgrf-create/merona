"""conformance/verify_evidence.py — the revocation path (verify()'s step 4).

Public-credibility regression: attest/README.md documents on-chain
revocation as merona's own correction mechanism for evidence it no longer
stands behind. Before the fix, a REVOKED-and-not-superseded attestation
still made verify() return True — the CLI would print VERIFIED and exit 0
for evidence merona itself disavowed. That is the verifier affirming
disavowed evidence: the worst possible bug in the one tool the "don't
trust us, verify" pitch rests on.

These tests drive the REAL verify() function body; only the network I/O
functions it calls (fetch_anchors_jsonl, get_block_number,
find_block_at_or_after, scan_eas_logs, rpc_call) are monkeypatched, same
technique test_verify_sanity.py / test_revoked_verify.py (the auditor's
offline repros) used to confirm the bug and the fix.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "conformance"))

import verify_evidence as ve                               # noqa: E402

SNAP_DATE = "2026-08-08"
GOOD_SHA = hashlib.sha256(b"real file bytes").hexdigest()
FILES_SHA256 = {"seller_aggregates.csv": GOOD_SHA, "snapshot_meta.json": "b" * 64}
COMBINED = ve.recompute_combined_sha256(FILES_SHA256)
ANCHOR_ROW = {"snapshot_date": SNAP_DATE, "anchored_at": "2026-08-08T04:00:00Z",
             "files_sha256": FILES_SHA256, "combined_sha256": COMBINED}


def _pack_str(s: str) -> bytes:
    b = s.encode()
    pad = (-len(b)) % 32
    return len(b).to_bytes(32, "big") + b + b"\x00" * pad


def _encode_anchor_payload(snapshot_date: str, combined_hex: str,
                           head_commit: str) -> bytes:
    """Mirrors attest/eas.py's abi_encode(["string","bytes32","string"], ..)
    layout that decode_anchor_data() (verify_evidence.py) expects."""
    head_off = 3 * 32
    s1 = _pack_str(snapshot_date)
    s2_off = head_off + len(s1)
    s2 = _pack_str(head_commit)
    head = (head_off.to_bytes(32, "big") + bytes.fromhex(combined_hex)
           + s2_off.to_bytes(32, "big"))
    return head + s1 + s2


def _pack_get_attestation(att_uid_hex: str, schema_b: bytes, time_: int,
                          revoc_: int, attester: str, data: bytes) -> str:
    """Mirrors the ABI shape decode_get_attestation() expects for EAS's
    getAttestation(bytes32) -> Attestation tuple return."""
    def w_addr(a):
        return b"\x00" * 12 + bytes.fromhex(a[2:])

    def w_uint(v):
        return v.to_bytes(32, "big")

    def w_bool(b):
        return (b"\x00" * 31) + (b"\x01" if b else b"\x00")

    head = (bytes.fromhex(att_uid_hex) + schema_b + w_uint(time_) + w_uint(0)
           + w_uint(revoc_) + b"\x00" * 32 + w_addr("0x" + "00" * 20)
           + w_addr(attester) + w_bool(True) + w_uint(10 * 32))
    pad = (-len(data)) % 32
    data_enc = len(data).to_bytes(32, "big") + data + b"\x00" * pad
    tuple_bytes = head + data_enc
    return "0x" + ((32).to_bytes(32, "big") + tuple_bytes).hex()


def _fake_log(att_uid_hex: str) -> dict:
    # Attested(address indexed recipient, address indexed attester,
    # bytes32 uid, bytes32 indexed schemaUID) — `uid` is the sole
    # non-indexed param, so it's the entire 32-byte log data word verbatim
    # (verify_evidence.py reads it back with `lg["data"][2:66]`).
    return {"data": "0x" + att_uid_hex, "transactionHash": "0x" + "cc" * 32}


def _wire(monkeypatch, logs: list[dict], attestation_returns: dict[str, str]):
    """Monkeypatch verify_evidence's four network-touching functions:
    anchors row lookup, block-number/timestamp binary search (fixed dummy
    values — irrelevant once scan_eas_logs is stubbed), the EAS log scan
    (returns `logs`), and eth_call for getAttestation (keyed by the att uid
    hex embedded in each log's `data` field)."""
    monkeypatch.setattr(ve, "fetch_anchors_jsonl", lambda: [ANCHOR_ROW])
    monkeypatch.setattr(ve, "get_block_number", lambda rpc_urls: 30_000_100)
    monkeypatch.setattr(ve, "find_block_at_or_after",
                        lambda rpc_urls, target_ts, lo, hi: 30_000_000)
    monkeypatch.setattr(ve, "scan_eas_logs",
                        lambda rpc_urls, uid_, from_block, to_block,
                               chunk_blocks, max_chunks, delay_s=0:
                        (logs, True))

    def fake_rpc_call(rpc_urls, method, params, retries=2):
        assert method == "eth_call"
        att_uid = params[0]["data"][2 + 8:]        # strip 0x + 4-byte selector
        return attestation_returns[att_uid]

    monkeypatch.setattr(ve, "rpc_call", fake_rpc_call)


def test_revoked_attestation_with_no_live_supersession_fails(monkeypatch):
    """The exact bug: a matching, REVOKED attestation with nothing live
    superseding it must make verify() return False (CLI exits non-zero),
    not the old WARN-then-True."""
    schema_b = ve.schema_uid()
    inner = _encode_anchor_payload(SNAP_DATE, COMBINED, "deadbeef")
    att_uid_hex = "11" * 32
    raw = _pack_get_attestation(att_uid_hex, schema_b, time_=1754625600,
                                revoc_=1754700000,          # REVOKED
                                attester=ve.ATTESTER_ADDRESS, data=inner)
    _wire(monkeypatch, [_fake_log(att_uid_hex)], {att_uid_hex: raw})

    steps, verified = ve.verify(GOOD_SHA, SNAP_DATE, check_chain=True)
    assert verified is False
    last = steps[-1]
    assert last.name == "4.eas_attestation"
    assert last.status == "FAIL"
    assert "REVOKED" in last.detail


def test_revoked_but_superseded_by_live_still_passes(monkeypatch):
    """attest/README.md's documented correction path: revoke the bad
    attestation, publish a new live one. The verifier must still PASS when
    a live attestation for the same (snapshot_date, combined_sha256,
    attester) exists alongside the revoked one."""
    schema_b = ve.schema_uid()
    inner = _encode_anchor_payload(SNAP_DATE, COMBINED, "deadbeef")

    revoked_uid = "11" * 32
    revoked_raw = _pack_get_attestation(
        revoked_uid, schema_b, time_=1754625600, revoc_=1754700000,
        attester=ve.ATTESTER_ADDRESS, data=inner)

    live_uid = "22" * 32
    live_raw = _pack_get_attestation(
        live_uid, schema_b, time_=1754700500, revoc_=0,       # LIVE
        attester=ve.ATTESTER_ADDRESS, data=inner)

    logs = [_fake_log(revoked_uid), _fake_log(live_uid)]
    _wire(monkeypatch, logs, {revoked_uid: revoked_raw, live_uid: live_raw})

    steps, verified = ve.verify(GOOD_SHA, SNAP_DATE, check_chain=True)
    assert verified is True
    last = steps[-1]
    assert last.name == "4.eas_attestation"
    assert last.status == "PASS"
    assert live_uid in last.detail or f"0x{live_uid}" in last.detail
    assert "REVOKED" not in last.detail


def test_cli_exit_code_is_nonzero_for_revoked_evidence(monkeypatch, capsys):
    """End-to-end through main(): a revoked attestation must make the CLI
    print NOT VERIFIED and return a non-zero exit code — the actual
    user-facing surface (`echo $?` after running the tool)."""
    schema_b = ve.schema_uid()
    inner = _encode_anchor_payload(SNAP_DATE, COMBINED, "deadbeef")
    att_uid_hex = "33" * 32
    raw = _pack_get_attestation(att_uid_hex, schema_b, time_=1754625600,
                                revoc_=1754700000, attester=ve.ATTESTER_ADDRESS,
                                data=inner)
    _wire(monkeypatch, [_fake_log(att_uid_hex)], {att_uid_hex: raw})

    rc = ve.main(["verify_evidence.py", "--sha256", GOOD_SHA,
                 "--date", SNAP_DATE])
    out = capsys.readouterr().out
    assert rc != 0
    assert "NOT VERIFIED" in out
    assert "VERIFIED" not in out.replace("NOT VERIFIED", "")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

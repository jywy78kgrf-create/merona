"""Payer account-kind enrichment (indexer/enrich_payer_kinds.py): classify
each distinct x402-scoped EVM payer as an EOA / EIP-7702-delegated EOA /
contract via batched eth_getCode.

Covers, against the REAL classify_code / _fetch_codes_batch / enrich_chain /
storage helpers (sqlite Store, requests replaced with a small in-memory
stub — no network):
  (a) classification of the three kinds, incl. the EXACT 0xef0100 delegation
      prefix and an 0xEF01-but-not-00 near-miss that must fall to 'contract'.
  (b) batch id/address matching survives a shuffled response order, and a
      malformed per-entry response (missing/errored result) is dropped
      without corrupting the rest of the batch.
  (c) a whole-batch transport failure leaves those payers UNCHECKED (no row)
      — never recorded as 'eoa' — and the next run picks them up (idempotent
      resume via the LEFT JOIN work-selection).
  (d) the per-run cap (X402_PAYER_KIND_CAP / enrich_chain(cap=)) is respected
      and the honest remaining-unchecked count is logged.
  (e) only x402-SCOPED payers (facilitator in the registry) are ever selected
      — an off-registry facilitator's payer must never be checked.
  (f) a re-check upserts (ON CONFLICT) rather than being ignored: eoa can
      become eoa_delegated once 7702 delegation code appears.
  (g) the nightly dashboard blob (build_dashboard_metrics) carries a
      payer_kinds counts block per chain.
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # indexer/

import daily_snapshot                             # noqa: E402
import enrich_payer_kinds as epk                   # noqa: E402
from storage import Store                          # noqa: E402

CHAIN = "base"
RELAYER = "0x" + "aa" * 20            # the registry facilitator these tests scope to
OFF_REGISTRY = "0x" + "0d" * 20       # a facilitator NOT in the registry


# --- shared fixtures -------------------------------------------------------

class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._body


class _StubSession:
    """Stand-in for requests.Session — every POST goes through an injectable
    `responder(payload) -> body` callable, which may also raise to simulate a
    transport failure. Records every payload it was asked to send."""

    def __init__(self, responder):
        self.headers: dict = {}
        self._responder = responder
        self.calls: list = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        body = self._responder(json)
        return _FakeResponse(body)


def _install_fake_session(monkeypatch, responder):
    """Point enrich_payer_kinds' `requests.Session()` calls at one shared
    stub instance so a test can both control responses and inspect .calls."""
    fake = _StubSession(responder)
    monkeypatch.setattr(epk.requests, "Session", lambda: fake)
    return fake


def _set_registry(tmp_path, monkeypatch, chains=(CHAIN,), relayer=RELAYER):
    p = tmp_path / "relayer_registry.json"
    p.write_text(json.dumps({"relayers_by_chain": {ch: [relayer] for ch in chains}}))
    monkeypatch.setattr(epk, "_REGISTRY", p)
    return p


def _seed_settlement(store, ins, tx, payer, seller, facilitator, blk, ts=1_750_000_000):
    store.db.execute(ins, (tx, 0, CHAIN, "0xusdc", payer, seller,
                           1_000_000, blk, ts, facilitator))


def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)


# --- (a) classification -----------------------------------------------------

def test_classify_plain_eoa_variants():
    for code in ("0x", "0x0", "", None):
        kind, prefix = epk.classify_code(code)
        assert kind == "eoa"
        assert prefix is None


def test_classify_exact_7702_delegation_prefix():
    delegate = "11" * 20   # 20-byte delegate address
    code = "0xef0100" + delegate
    kind, prefix = epk.classify_code(code)
    assert kind == "eoa_delegated"
    # first 8 bytes = 'ef0100' + first 5 bytes of the delegate address
    assert prefix == "0x" + ("ef0100" + delegate)[:16]


def test_classify_7702_near_miss_is_contract_not_delegated():
    """0xEF01 immediately NOT followed by 00 must never be bucketed as
    delegated — only the exact 0xef0100 designator counts."""
    code = "0xEF01FF" + "00" * 17   # looks close, wrong 4th byte
    kind, prefix = epk.classify_code(code)
    assert kind == "contract"
    assert prefix == "0x" + code[2:].lower()[:16]


def test_classify_ordinary_contract_code():
    code = "0x608060405234801561001057600080fd5b50"
    kind, prefix = epk.classify_code(code)
    assert kind == "contract"
    assert prefix == "0x" + code[2:18]


# --- (b) batch id/address matching ------------------------------------------

def test_batch_matching_survives_shuffled_response_order():
    addrs = [f"0x{i:040x}" for i in range(6)]
    codes = {a: f"0x60{idx:02x}" for idx, a in enumerate(addrs)}   # distinct per addr

    def responder(payload):
        entries = [{"jsonrpc": "2.0", "id": item["id"], "result": codes[item["params"][0]]}
                   for item in payload]
        random.Random(7).shuffle(entries)   # deterministic but out-of-order
        return entries

    session = _StubSession(responder)
    out = epk._fetch_codes_batch(session, "http://rpc.example", addrs, 5.0)
    assert out == codes


def test_batch_matching_drops_malformed_entry_keeps_the_rest():
    addrs = [f"0x{i:040x}" for i in range(4)]
    codes = {a: "0x6001" for a in addrs}
    bad = addrs[2]

    def responder(payload):
        entries = []
        for item in payload:
            addr = item["params"][0]
            if addr == bad:
                entries.append({"jsonrpc": "2.0", "id": item["id"],
                                "error": {"code": -32000, "message": "boom"}})
            else:
                entries.append({"jsonrpc": "2.0", "id": item["id"],
                                "result": codes[addr]})
        random.Random(3).shuffle(entries)
        return entries

    session = _StubSession(responder)
    out = epk._fetch_codes_batch(session, "http://rpc.example", addrs, 5.0)
    assert bad not in out
    assert set(out) == set(addrs) - {bad}


def test_whole_batch_transport_failure_returns_nothing():
    def responder(payload):
        raise RuntimeError("connection reset")

    session = _StubSession(responder)
    out = epk._fetch_codes_batch(session, "http://rpc.example", ["0x" + "1" * 40], 5.0)
    assert out == {}


# --- (c) failed batch leaves payers unchecked; resumable --------------------

def test_failed_batch_leaves_payers_unchecked_then_resumes(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    _set_registry(tmp_path, monkeypatch)
    s = Store(tmp_path / "t.sqlite")
    ins = s._settlement_insert_sql(with_facilitator=True)
    p1, p2 = "0x" + "11" * 20, "0x" + "22" * 20
    _seed_settlement(s, ins, "0xtx1", p1, "0xseller", RELAYER, 100)
    _seed_settlement(s, ins, "0xtx2", p2, "0xseller", RELAYER, 101)
    s.db.commit()

    # Run 1: RPC transport is down for the whole batch.
    _install_fake_session(monkeypatch, lambda payload: (_ for _ in ()).throw(
        RuntimeError("down")))
    checked, remaining = epk.enrich_chain(s, CHAIN, cap=10)
    assert checked == 0
    assert remaining == 2
    rows = s.db.execute("SELECT COUNT(*) FROM payer_kinds").fetchone()[0]
    assert rows == 0, "an RPC failure must never be recorded as a classified payer"

    # Run 2: RPC recovers — both payers resolve as plain EOAs (no code).
    _install_fake_session(monkeypatch, lambda payload: [
        {"jsonrpc": "2.0", "id": item["id"], "result": "0x"} for item in payload])
    checked, remaining = epk.enrich_chain(s, CHAIN, cap=10)
    assert checked == 2
    assert remaining == 0
    kinds = dict(s.db.execute(
        "SELECT payer, kind FROM payer_kinds WHERE chain=?", (CHAIN,)).fetchall())
    assert kinds == {p1: "eoa", p2: "eoa"}
    s.close()


# --- (d) cap respected + remaining logged -----------------------------------

def test_cap_respected_and_remaining_count_logged(tmp_path, monkeypatch, capsys):
    _no_sleep(monkeypatch)
    _set_registry(tmp_path, monkeypatch)
    s = Store(tmp_path / "t.sqlite")
    ins = s._settlement_insert_sql(with_facilitator=True)
    payers = [f"0x{i:040x}" for i in range(10)]
    for i, p in enumerate(payers):
        _seed_settlement(s, ins, f"0xtx{i}", p, "0xseller", RELAYER, 100 + i)
    s.db.commit()

    _install_fake_session(monkeypatch, lambda payload: [
        {"jsonrpc": "2.0", "id": item["id"], "result": "0x"} for item in payload])
    checked, remaining = epk.enrich_chain(s, CHAIN, cap=3)
    assert checked == 3
    assert remaining == 7

    n = s.db.execute("SELECT COUNT(*) FROM payer_kinds").fetchone()[0]
    assert n == 3

    out = capsys.readouterr().out
    assert "3/10" in out
    assert "7 remain unchecked" in out
    s.close()


# --- (e) only scoped payers selected ----------------------------------------

def test_only_registry_scoped_payers_are_checked(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    _set_registry(tmp_path, monkeypatch)
    s = Store(tmp_path / "t.sqlite")
    ins = s._settlement_insert_sql(with_facilitator=True)
    in_scope = "0x" + "aa" * 19 + "01"
    out_of_scope = "0x" + "bb" * 19 + "02"
    _seed_settlement(s, ins, "0xtx-in", in_scope, "0xseller", RELAYER, 100)
    _seed_settlement(s, ins, "0xtx-out", out_of_scope, "0xseller", OFF_REGISTRY, 101)
    s.db.commit()

    fake = _install_fake_session(monkeypatch, lambda payload: [
        {"jsonrpc": "2.0", "id": item["id"], "result": "0x"} for item in payload])
    checked, remaining = epk.enrich_chain(s, CHAIN, cap=10)
    assert checked == 1
    assert remaining == 0   # only the in-scope payer counted toward the backlog

    requested = {addr for call in fake.calls for item in call for addr in [item["params"][0]]}
    assert in_scope in requested
    assert out_of_scope not in requested

    kinds = dict(s.db.execute(
        "SELECT payer, kind FROM payer_kinds WHERE chain=?", (CHAIN,)).fetchall())
    assert kinds == {in_scope: "eoa"}
    assert out_of_scope not in kinds
    s.close()


# --- (f) re-run updates kind via ON CONFLICT --------------------------------

def test_record_payer_kind_upserts_eoa_to_delegated(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    payer = "0x" + "33" * 20
    s.record_payer_kind(CHAIN, payer, "eoa", None, "2026-08-10T00:00:00+00:00")
    s.db.commit()
    row = s.db.execute(
        "SELECT kind, code_prefix FROM payer_kinds WHERE chain=? AND payer=?",
        (CHAIN, payer)).fetchone()
    assert row == ("eoa", None)

    # 7702 delegation code shows up on a later check -> re-classify in place.
    s.record_payer_kind(CHAIN, payer, "eoa_delegated", "0xef0100112233445566",
                        "2026-08-11T00:00:00+00:00")
    s.db.commit()
    rows = s.db.execute(
        "SELECT kind, code_prefix FROM payer_kinds WHERE chain=? AND payer=?",
        (CHAIN, payer)).fetchall()
    assert len(rows) == 1, "ON CONFLICT must update the existing row, not add a second one"
    assert rows[0] == ("eoa_delegated", "0xef0100112233445566")
    s.close()


def test_payer_kind_counts(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.record_payer_kind(CHAIN, "0x" + "01" * 20, "eoa", None, "t")
    s.record_payer_kind(CHAIN, "0x" + "02" * 20, "eoa", None, "t")
    s.record_payer_kind(CHAIN, "0x" + "03" * 20, "eoa_delegated", "0xef0100", "t")
    s.record_payer_kind(CHAIN, "0x" + "04" * 20, "contract", "0x6080", "t")
    s.db.commit()
    assert s.payer_kind_counts(CHAIN) == {"eoa": 2, "eoa_delegated": 1, "contract": 1}
    assert s.payer_kind_counts("polygon") == {}
    s.close()


# --- (g) dashboard blob carries payer_kinds counts --------------------------

def test_dashboard_metrics_carries_payer_kinds(tmp_path, monkeypatch):
    s = Store(tmp_path / "t.sqlite")
    monkeypatch.setattr(daily_snapshot, "REGISTRY", _set_registry(tmp_path, monkeypatch))
    ins = s._settlement_insert_sql(with_facilitator=True)
    p1, p2, p3 = ("0x" + "aa" * 19 + "01", "0x" + "aa" * 19 + "02",
                  "0x" + "aa" * 19 + "03")
    _seed_settlement(s, ins, "0xtx1", p1, "0xseller", RELAYER, 100)
    _seed_settlement(s, ins, "0xtx2", p2, "0xseller", RELAYER, 101)
    _seed_settlement(s, ins, "0xtx3", p3, "0xseller", RELAYER, 102)
    s.db.commit()

    s.record_payer_kind(CHAIN, p1, "eoa", None, "t")
    s.record_payer_kind(CHAIN, p2, "eoa", None, "t")
    s.record_payer_kind(CHAIN, p3, "eoa_delegated", "0xef0100", "t")
    s.db.commit()

    out = daily_snapshot.build_dashboard_metrics(s)
    pk = out["chains"][CHAIN]["payer_kinds"]
    assert pk["eoa"] == 2
    assert pk["eoa_delegated"] == 1
    assert pk["contract"] == 0
    assert pk["checked"] == 3
    # "payers" is the already-computed scoped distinct-payer count reused,
    # not a fresh query — all 3 seeded payers are scoped and distinct.
    assert pk["payers"] == 3

    # a chain with no payer_kinds rows at all must carry no key (never an
    # all-zero row masquerading as "checked and found nothing")
    assert "payer_kinds" not in out["chains"]["polygon"]
    s.close()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

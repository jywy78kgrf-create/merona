"""P0 correctness regressions: finality sourcing, coverage-start stamp, the
dark-matter canary recorder, and the snapshot coverage fingerprint. Pure/unit
(no network): the RPC-facing helpers are exercised with a fake client so the
attribution logic and finalized-vs-fallback branch are pinned deterministically.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coverage_canary  # noqa: E402
import daily_snapshot as ds  # noqa: E402
import index_solana  # noqa: E402
from evm_chains import BASE  # noqa: E402
from index_base import finalized_end  # noqa: E402
from storage import PostgresStore, Store  # noqa: E402

PG_URL = os.environ.get("X402_TEST_DB_URL")


class FakeClient:
    """Minimal ChainClient stand-in for the finality branch."""
    def __init__(self, head, finalized):
        self._head, self._fin = head, finalized

    def block_number(self):
        return self._head

    def finalized_block(self):
        return self._fin


def _row(tx, li, chain, blk, ts, payer="0xp", seller="0xs", amt=1000):
    return dict(tx_hash=tx, log_index=li, chain=chain, token="0xusdc",
                payer=payer, seller=seller, amount=amt,
                block_number=blk, block_timestamp=ts)


# --- P0.2 finality sourcing -------------------------------------------------
def test_finalized_end_prefers_finalized_over_confirmations():
    # finalized is DEEPER than head-confirmations on L2s -> must win (safe).
    end, src = finalized_end(FakeClient(head=1000, finalized=560), BASE, 1000)
    assert (end, src) == (560, "finalized")


def test_finalized_end_falls_back_when_tag_absent():
    end, src = finalized_end(FakeClient(head=1000, finalized=None), BASE, 1000)
    assert src == "confirmations" and end == 1000 - BASE.confirmations


# --- P0.1 Solana coverage-start stamp --------------------------------------
def test_solana_coverage_stamp(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.commit_solana_batch([{**_row("sig", 0, "solana", 430_000_000, 1751700000),
                            "facilitator": "relayerX"}])
    index_solana._stamp_coverage_start(s)
    assert int(s.get_meta("solana_coverage_start_slot")) == 430_000_000
    assert s.get_meta("solana_coverage_start_date")  # date recorded
    # idempotent: a later, lower min doesn't move the stamp
    before = s.get_meta("solana_coverage_start_slot")
    s.commit_solana_batch([{**_row("sig2", 0, "solana", 1, 1751700001),
                            "facilitator": "relayerY"}])
    index_solana._stamp_coverage_start(s)
    assert s.get_meta("solana_coverage_start_slot") == before
    s.close()


# --- P0.3 canary recorder ---------------------------------------------------
def test_coverage_ratio_recorder(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    r = s.record_coverage_ratio("2026-07-06", "base", "eip3009",
                                (1, 3000), 100, 98, 12345, "t")
    assert abs(r - 0.98) < 1e-9
    # zero total -> ratio NULL, no divide-by-zero
    r0 = s.record_coverage_ratio("2026-07-06", "sei", "eip3009",
                                 (1, 10), 0, 0, 0, "t")
    assert r0 != r0  # nan
    row = s.db.execute("SELECT total_events,attributed,ratio FROM coverage_ratio "
                       "WHERE chain='base'").fetchone()
    assert row[0] == 100 and row[1] == 98
    s.close()


# --- P0.3 canary attribution logic (fake logs) ------------------------------
class LogClient:
    """Fake client returning canned logs for the canary's two getLogs calls."""
    def __init__(self, auth_logs, transfer_logs):
        self._auth, self._xfer = auth_logs, transfer_logs

    def get_logs_chunked(self, *, address, topics, from_block, to_block):
        t0 = topics[0]
        return self._auth if t0 == coverage_canary.AUTHORIZATION_USED_TOPIC else self._xfer


def test_canary_attribution_counts_only_authorized_transfers():
    A = coverage_canary.AUTHORIZATION_USED_TOPIC
    T = coverage_canary.TRANSFER_TOPIC
    pad = lambda a: "0x" + "0" * 24 + a[2:]
    U = BASE.usdc
    auth = [{"transactionHash": "0xtx1", "topics": [A, pad("0x" + "11" * 20)]}]
    xfer = [
        {"address": U, "transactionHash": "0xtx1", "topics": [T, pad("0x" + "11" * 20), pad("0x" + "22" * 20)],
         "data": hex(1000), "blockNumber": "0x1", "logIndex": "0x0", "blockTimestamp": "0x1"},
        {"address": U, "transactionHash": "0xtx2", "topics": [T, pad("0x" + "33" * 20), pad("0x" + "22" * 20)],
         "data": hex(9), "blockNumber": "0x1", "logIndex": "0x1", "blockTimestamp": "0x1"},
    ]
    m = coverage_canary.measure_eip3009(LogClient(auth, xfer), BASE, 1, 10)
    # one AuthorizationUsed event; tx1 transfer's payer==authorizer -> attributed;
    # tx2 has no auth -> ignored. ratio == 1.0
    assert m["total"] == 1 and m["attributed"] == 1 and m["volume"] == 1000


# --- P0.4 coverage fingerprint ----------------------------------------------
def _fingerprint_has_required_keys(store):
    fp = ds.build_coverage_fingerprint(
        store,
        {"base": {"finalized_block": 100, "source": "finalized", "head": 130, "lag_blocks": 30}},
        {"base": {"eip3009": {"ratio": 0.999}}})
    for k in ("fingerprint_version", "chains", "mechanisms", "read_height_policy",
              "finalized_heights", "eip3009_coverage_ratio"):
        assert k in fp, k
    assert fp["read_height_policy"] == "finalized"
    assert fp["finalized_heights"]["base"]["source"] == "finalized"
    assert fp["eip3009_coverage_ratio"]["base"] == 0.999


def test_fingerprint_sqlite(tmp_path):
    _fingerprint_has_required_keys(Store(tmp_path / "t.sqlite"))


@pytest.mark.skipif(not PG_URL, reason="X402_TEST_DB_URL not set")
def test_fingerprint_and_ratio_postgres():
    import psycopg
    with psycopg.connect(PG_URL, autocommit=True) as c:
        for t in ("settlements", "indexed_ranges", "meta", "solana_cursors",
                  "coverage_ratio"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
    s = PostgresStore(PG_URL)
    r = s.record_coverage_ratio("2026-07-06", "base", "eip3009",
                                (1, 3000), 100, 98, 12345, "t")
    assert abs(r - 0.98) < 1e-9
    _fingerprint_has_required_keys(s)
    s.close()

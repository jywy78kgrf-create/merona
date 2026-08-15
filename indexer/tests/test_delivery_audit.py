"""attest/delivery_audit.py: the public paid-delivery audit export.

Every number in the exported JSON must be computed from the receipts
ledger — these tests pin the funnel arithmetic (especially the
charged_unserved / settled_no_content non-double-counting rule and the
spent_usd accounting, which must agree with attest/paysweep.py's own spend
loop), query-string stripping, newest-first receipt order, and the
missing-ledger refusal.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "attest"))

import delivery_audit as da                                # noqa: E402

SELLER = "0x" + "bb" * 20
PAYER = "0x" + "aa" * 20


def _row(ts, outcome, **kw):
    row = {
        "ts": ts, "tag": "sweep", "outcome": outcome,
        "url": "https://seller.example/api/data?token=secret&x=1",
        "pay_to": SELLER, "network": "eip155:8453",
        "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "amount_base_units": 1000, "paid": False,
        "http_status": 200, "latency_ms": 12,
        "delivered_bytes": 0, "delivered_sha256": "e3b0c4" + "0" * 58,
        "content_type": "application/json", "json_ok": True,
        "error_shaped": False,
        "settlement": {"success": None, "transaction": None,
                       "network": None, "payer": None},
        "payer": PAYER,
    }
    row.update(kw)
    return row


def _fixture_rows():
    return [
        _row("2026-08-10T10:00:00+00:00", "delivered", paid=True,
             delivered_bytes=53, http_status=200),
        _row("2026-08-11T09:00:00+00:00", "settled_no_content",
             http_status=500, paid=False,
             settlement={"success": True, "transaction": "0x" + "22" * 32,
                        "network": "base", "payer": PAYER}),
        _row("2026-08-11T09:05:00+00:00", "settle_failed",
             http_status=500, paid=False,
             settlement={"success": False, "transaction": "0x" + "33" * 32,
                        "network": "base", "payer": PAYER}),
        _row("2026-08-12T08:00:00+00:00", "settled_unconfirmed",
             http_status=500, paid=False,
             settlement={"success": None, "transaction": "0x" + "44" * 32,
                        "network": "base", "payer": PAYER}),
        # paid is True on error-shaped rows: the payment settled, the seller
        # answered with an error body — settled AND spent, never "delivered"
        _row("2026-08-12T09:00:00+00:00", "delivered_error_shaped", paid=True,
             delivered_bytes=27, http_status=200, error_shaped=True,
             settlement={"success": True, "transaction": "0x" + "55" * 32,
                        "network": "base", "payer": PAYER}),
    ]


def write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


# --- funnel arithmetic -------------------------------------------------------
def test_funnel_counts_and_charged_unserved_not_double_counted():
    agg = da._aggregate(_fixture_rows())
    assert agg["attempts"] == 5
    assert agg["by_outcome"] == {
        "delivered": 1, "delivered_error_shaped": 1, "settle_failed": 1,
        "settled_no_content": 1, "settled_unconfirmed": 1,
    }
    assert agg["delivered"] == 1
    assert agg["charged_unserved"] == 1                # == settled_no_content
    # delivered + delivered_error_shaped + settled_no_content +
    # settled_unconfirmed; settle_failed excluded. error_shaped rows are
    # settled (paid=True) but must never inflate `delivered`.
    assert agg["total_settled"] == 4


def test_spent_usd_matches_paysweeps_own_spend_rule():
    """paid rows, settled_no_content, and settled_unconfirmed all count
    toward spend; settle_failed and plain rejected never do — mirrors
    attest/paysweep.py's `row.get("paid") or out in (...)` loop exactly."""
    rows = _fixture_rows()   # 2 paid=True (delivered + error_shaped, 1000u each)
    agg = da._aggregate(rows)  # + settled_no_content + settled_unconfirmed
    assert agg["spent_usd"] == pytest.approx(0.004)     # 4 * 1000 units / 1e6
    # settle_failed's 1000 units must NOT be in that sum
    only_failed = [r for r in rows if r["outcome"] == "settle_failed"]
    assert da._aggregate(only_failed)["spent_usd"] == 0.0


def test_retry_error_is_maybe_spent_but_never_settled_or_delivered():
    """retry_error = payment authorization sent, then a client-side error
    before any response — settlement unknowable at record time. Spend must
    count it (conservative: the auth may have settled), but it must never
    inflate total_settled or delivered."""
    rows = [_row("2026-08-14T22:00:00+00:00", "retry_error", paid=False,
                 amount_base_units=1000,
                 settlement={"success": None, "transaction": None,
                            "network": None, "payer": None})]
    agg = da._aggregate(rows)
    assert agg["spent_usd"] == pytest.approx(0.001)
    assert agg["total_settled"] == 0
    assert agg["delivered"] == 0
    assert da.TAXONOMY.get("retry_error")


def test_rejected_outcome_excluded_from_settled_and_spend():
    rows = [_row("2026-08-10T00:00:00+00:00", "rejected", paid=False,
                 settlement={"success": None, "transaction": None,
                            "network": None, "payer": None})]
    agg = da._aggregate(rows)
    assert agg["attempts"] == 1
    assert agg["total_settled"] == 0
    assert agg["spent_usd"] == 0.0


# --- query-string stripping --------------------------------------------------
def test_path_strips_query_string_entirely():
    origin, path = da._origin_and_path(
        "https://seller.example/api/data?token=secret&x=1")
    assert origin == "https://seller.example"
    assert path == "/api/data"
    assert "?" not in path and "token" not in path and "secret" not in path


def test_public_receipt_never_carries_query_string():
    r = da._public_receipt(_fixture_rows()[0])
    assert "?" not in r["path"]
    assert r["origin"] == "https://seller.example"


# --- newest-first order -------------------------------------------------------
def test_receipts_sorted_newest_first():
    rows = _fixture_rows()
    audit = da.build_audit(rows, {})
    ts_order = [r["ts"] for r in audit["receipts"]]
    assert ts_order == sorted(ts_order, reverse=True)
    assert ts_order[0] == "2026-08-12T09:00:00+00:00"


# --- by_run grouping + sweep summary merge -----------------------------------
def test_by_run_groups_by_utc_date_of_ts():
    rows = _fixture_rows()
    audit = da.build_audit(rows, {})
    assert set(audit["by_run"].keys()) == {"2026-08-10", "2026-08-11",
                                           "2026-08-12"}
    assert audit["by_run"]["2026-08-11"]["attempts"] == 2
    assert audit["by_run"]["2026-08-11"]["charged_unserved"] == 1


def test_by_run_merges_matching_sweep_summary():
    rows = [_fixture_rows()[0]]                          # 2026-08-10
    summaries = {"2026-08-10": {
        "swept_at": "2026-08-10T23:59:00+00:00", "targets": 42,
        "duration_s": 90, "repay_window_days": 30,
        "per_purchase_cap_usd": 0.05}}
    audit = da.build_audit(rows, summaries)
    sweep = audit["by_run"]["2026-08-10"]["sweep"]
    assert sweep["targets"] == 42 and sweep["repay_window_days"] == 30
    # a date with no matching summary carries no "sweep" key
    assert "sweep" not in da.build_audit([_fixture_rows()[1]], summaries)[
        "by_run"]["2026-08-11"]


# --- taxonomy / definitions carried through -----------------------------------
def test_taxonomy_covers_every_outcome_seen_plus_pre_ledger_ones():
    audit = da.build_audit(_fixture_rows(), {})
    for outcome in audit["funnel"]["by_outcome"]:
        assert outcome in audit["taxonomy"], outcome
    assert "not_402" in audit["taxonomy"] and "dry" in audit["taxonomy"]
    assert "charged_unserved" in audit["definitions"]


# --- missing-ledger refusal ---------------------------------------------------
def test_missing_ledger_exits_nonzero_and_writes_nothing(tmp_path, monkeypatch):
    ledger = tmp_path / "receipts.jsonl"                  # never created
    out = tmp_path / "delivery_audit.json"
    monkeypatch.setattr(da, "LEDGER", ledger)
    monkeypatch.setattr(da, "OUT_PATH", out)
    monkeypatch.setattr(da, "SWEEP_DIR", tmp_path)
    assert da.main() != 0
    assert not out.exists()


def test_present_but_empty_ledger_is_not_a_refusal(tmp_path, monkeypatch):
    """An empty (zero-attempt) ledger is a true statement, not the
    'ledger missing' failure mode — only a MISSING file refuses to write."""
    ledger = tmp_path / "receipts.jsonl"
    ledger.write_text("")
    out = tmp_path / "delivery_audit.json"
    monkeypatch.setattr(da, "LEDGER", ledger)
    monkeypatch.setattr(da, "OUT_PATH", out)
    monkeypatch.setattr(da, "SWEEP_DIR", tmp_path)
    assert da.main() == 0
    assert out.exists()
    written = json.loads(out.read_text())
    assert written["funnel"]["attempts"] == 0


def test_main_writes_valid_audit_end_to_end(tmp_path, monkeypatch):
    ledger = tmp_path / "receipts.jsonl"
    write_ledger(ledger, _fixture_rows())
    out = tmp_path / "delivery_audit.json"
    monkeypatch.setattr(da, "LEDGER", ledger)
    monkeypatch.setattr(da, "OUT_PATH", out)
    monkeypatch.setattr(da, "SWEEP_DIR", tmp_path)
    assert da.main() == 0
    written = json.loads(out.read_text())
    assert written["method_version"] == 1
    assert written["funnel"]["attempts"] == 5
    assert len(written["receipts"]) == 5
    assert "generated_at" in written


def test_malformed_ledger_lines_are_skipped_not_fatal(tmp_path, monkeypatch):
    ledger = tmp_path / "receipts.jsonl"
    ledger.write_text(json.dumps(_row("2026-08-10T00:00:00+00:00",
                                      "delivered")) + "\n"
                      + "{not json\n"
                      + "\n")                              # blank line too
    out = tmp_path / "delivery_audit.json"
    monkeypatch.setattr(da, "LEDGER", ledger)
    monkeypatch.setattr(da, "OUT_PATH", out)
    monkeypatch.setattr(da, "SWEEP_DIR", tmp_path)
    assert da.main() == 0
    written = json.loads(out.read_text())
    assert written["funnel"]["attempts"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

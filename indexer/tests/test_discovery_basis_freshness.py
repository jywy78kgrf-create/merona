"""_load_discovery_basis's freshness invariant — the regression test for the
2026-08-08 incident: a re-run that silently replayed cached (stale) Bazaar
pages rewrote data/processed/bazaar_resources.csv with a FRESH mtime but
data whose newest `last_updated` was still 2026-07-03. The old
_load_discovery_basis derived `snapshot_date` from the file's mtime alone,
so the published discovery_basis actively concealed the staleness.

Pins: the published block must expose BOTH the mtime-derived date
(`snapshot_date`) and the data-derived date (`catalog_max_last_updated`),
and the latter must reflect the CSV's own `last_updated` column, not the
file's mtime — exercised by writing a synthetic catalog with a known newest
`last_updated`, then touching the file's mtime forward to a much later date
(exactly what a stale-cache replay produces) and asserting the two fields
diverge as expected.
"""
from __future__ import annotations

import csv
import hashlib
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                    # indexer/
sys.path.insert(0, str(HERE.parent.parent / "serving"))

import trust_api as api                                   # noqa: E402

SELLER = "0x" + "ab" * 20
_COLS = ["resource_url", "resource_type", "x402_version", "last_updated",
        "accept_index", "network", "asset", "amount_base_units",
        "pay_to", "scheme", "max_timeout_seconds", "description_len"]


def _write_catalog(path: Path, last_updated_values: list[str]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(_COLS)
        for i, lu in enumerate(last_updated_values):
            w.writerow([f"https://s{i}.example/x", "http", "1", lu, 0,
                       "base", "USDC", "1000", "0x" + "a" * 40, "exact",
                       60, 10])


def _touch(path: Path, iso_date: str) -> None:
    t = time.mktime(time.strptime(iso_date, "%Y-%m-%d"))
    os.utime(path, (t, t))


# --- _catalog_max_last_updated -----------------------------------------------
def test_catalog_max_last_updated_is_the_newest_row_value(tmp_path):
    p = tmp_path / "bazaar_resources.csv"
    _write_catalog(p, ["2026-07-01T00:00:00Z", "2026-07-03T03:57:14Z",
                       "2026-06-15T00:00:00Z"])
    assert api._catalog_max_last_updated(p) == "2026-07-03T03:57:14Z"


def test_catalog_max_last_updated_none_when_column_missing(tmp_path):
    p = tmp_path / "no_lu.csv"
    p.write_text("a,b\n1,2\n")
    assert api._catalog_max_last_updated(p) is None


def test_catalog_max_last_updated_none_when_all_values_empty(tmp_path):
    p = tmp_path / "empty_lu.csv"
    _write_catalog(p, ["", ""])
    assert api._catalog_max_last_updated(p) is None


def test_catalog_max_last_updated_none_when_file_missing(tmp_path):
    assert api._catalog_max_last_updated(tmp_path / "nope.csv") is None


# --- _load_discovery_basis: the actual regression ---------------------------
def test_discovery_basis_exposes_both_dates_and_they_diverge_on_stale_replay(
        tmp_path):
    p = tmp_path / "bazaar_resources.csv"
    _write_catalog(p, ["2026-07-03T03:57:14Z", "2026-06-20T00:00:00Z"])

    # Simulate the actual incident: the file gets REWRITTEN (fresh mtime)
    # while its content is still the July 3rd pull, replayed from a stale
    # cache rather than genuinely re-fetched.
    _touch(p, "2026-08-08")

    basis = api._load_discovery_basis(p)
    assert basis is not None
    assert basis["catalog"] == "bazaar"
    assert basis["snapshot_date"] == "2026-08-08"                      # mtime
    assert basis["catalog_max_last_updated"] == "2026-07-03T03:57:14Z"  # data
    # the whole point: these must NOT be silently collapsed into one date
    assert basis["snapshot_date"] != basis["catalog_max_last_updated"][:10]
    assert basis["snapshot_sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()


def test_discovery_basis_dates_agree_on_a_genuine_same_day_pull(tmp_path):
    """Sanity check the other direction: a real same-day pull has the two
    dates agree (at least at day granularity) — divergence is a signal, not
    an artifact of always disagreeing."""
    p = tmp_path / "bazaar_resources.csv"
    _write_catalog(p, ["2026-08-08T09:00:00Z", "2026-08-07T00:00:00Z"])
    _touch(p, "2026-08-08")
    basis = api._load_discovery_basis(p)
    assert basis["snapshot_date"] == basis["catalog_max_last_updated"][:10]


def test_discovery_basis_none_when_catalog_file_missing(tmp_path):
    assert api._load_discovery_basis(tmp_path / "does_not_exist.csv") is None


def test_module_level_discovery_basis_has_both_fields_for_the_real_catalog():
    """The module's own DISCOVERY_BASIS (loaded once at import from
    X402_CATALOG_FILE) must carry the new field whenever a catalog is
    configured — not just the synthetic fixtures above."""
    if api.DISCOVERY_BASIS is None:
        import pytest
        pytest.skip("no catalog file on this host at import time")
    assert "catalog_max_last_updated" in api.DISCOVERY_BASIS
    assert "snapshot_date" in api.DISCOVERY_BASIS


# --- discovery_basis omission is untouched by the new field -----------------
def test_discovery_basis_still_omitted_entirely_when_catalog_absent(
        tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DISCOVERY_BASIS",
                        api._load_discovery_basis(tmp_path / "missing.csv"))
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text("")
    monkeypatch.setattr(api, "RECEIPTS_PATH", receipts)
    monkeypatch.setitem(api._delivery_cache, "mtime", None)
    monkeypatch.setitem(api._delivery_cache, "states", {})
    monkeypatch.setitem(api._delivery_cache, "history", {})
    code, body = api.delivery_lookup("base", SELLER)
    assert code == 200 and "discovery_basis" not in body


def test_delivery_lookup_propagates_catalog_max_last_updated(tmp_path,
                                                              monkeypatch):
    """Additive-only: a response that already carried discovery_basis keeps
    doing so, now with the new field riding along unchanged."""
    fake_basis = {"catalog": "bazaar", "snapshot_sha256": "d" * 64,
                  "snapshot_date": "2026-08-08",
                  "catalog_max_last_updated": "2026-07-03T03:57:14Z"}
    monkeypatch.setattr(api, "DISCOVERY_BASIS", fake_basis)
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text("")
    monkeypatch.setattr(api, "RECEIPTS_PATH", receipts)
    monkeypatch.setitem(api._delivery_cache, "mtime", None)
    monkeypatch.setitem(api._delivery_cache, "states", {})
    monkeypatch.setitem(api._delivery_cache, "history", {})
    code, body = api.delivery_lookup("base", SELLER)
    assert code == 200
    assert body["status"] == "not_evaluated"
    assert body["discovery_basis"] == fake_basis
    assert body["discovery_basis"]["catalog_max_last_updated"] == \
        "2026-07-03T03:57:14Z"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

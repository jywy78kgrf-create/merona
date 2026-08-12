"""pipeline/pull_bazaar.py + pipeline/lib.py — the stale-catalog-cache bug
and its fix:

  - snapshot.label defaulting to "auto" (today's UTC date) instead of a
    frozen literal, so an ordinary re-run lands in a fresh cache directory
    and genuinely re-fetches; an explicit pin still reproduces an old pull.
  - cache reuse being SAFE by default: an existing-but-old label directory
    is not silently trusted, it's warned about and re-fetched.
  - the post-pull DATA freshness check: the newest `lastUpdated` across
    pulled items is always printed, and a prominent (non-fatal) WARNING
    fires when it's far behind now — this is the check that would have
    caught the actual incident (cached July 3rd pages replayed for a month
    of "fresh" re-runs).

No network I/O anywhere here — pipeline.Client is monkeypatched to a fake
that records calls instead of ever hitting the real Bazaar API.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "pipeline"))

import lib                                                # noqa: E402
import pull_bazaar                                         # noqa: E402


def _cfg(label="testlabel", max_cache_age_days=2, max_data_age_days=14):
    return {
        "sources": {"bazaar": {
            "base_url": "https://api.example/x402/discovery",
            "page_limit": 1000, "min_interval_seconds": 0}},
        "snapshot": {"label": label,
                    "max_cache_age_days": max_cache_age_days,
                    "max_data_age_days": max_data_age_days},
        "http": {"user_agent": "test", "timeout_seconds": 5,
                "max_retries": 1, "backoff_base_seconds": 0},
    }


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
    """Stands in for lib.Client — records every .get() call instead of
    making one, and always answers with a single fresh-dated item."""
    instances: list = []

    def __init__(self, *a, **k):
        self.calls = []
        _FakeClient.instances.append(self)

    def get(self, url, params=None, **kw):
        self.calls.append((url, params))
        return _FakeResponse({
            "items": [{"resource": "https://fetched.example/x",
                      "lastUpdated": "2026-01-01T00:00:00Z",
                      "accepts": []}],
            "pagination": {"total": 1},
        })


def _cached_page(out_dir: Path, resource: str, last_updated: str,
                 mtime: float | None = None) -> Path:
    page = out_dir / "page_0000.json.gz"
    lib.save_raw_gz(page, {
        "items": [{"resource": resource, "lastUpdated": last_updated,
                  "accepts": []}],
        "pagination": {"total": 1},
    })
    if mtime is not None:
        os.utime(page, (mtime, mtime))
    return page


# --- label resolution (pipeline/lib.py load_config) -----------------------
def test_label_auto_resolves_to_today_utc(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("snapshot:\n  label: auto\n")
    monkeypatch.setattr(lib, "CONFIG_PATH", cfg_path)
    cfg = lib.load_config()
    expected = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert cfg["snapshot"]["label"] == expected


def test_label_missing_key_also_defaults_to_today_utc(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("snapshot: {}\n")
    monkeypatch.setattr(lib, "CONFIG_PATH", cfg_path)
    cfg = lib.load_config()
    expected = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert cfg["snapshot"]["label"] == expected


def test_label_explicit_pin_is_preserved_verbatim(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text('snapshot:\n  label: "2026-07-03"\n')
    monkeypatch.setattr(lib, "CONFIG_PATH", cfg_path)
    cfg = lib.load_config()
    assert cfg["snapshot"]["label"] == "2026-07-03"


def test_real_repo_config_defaults_label_to_auto_today(monkeypatch):
    """The actual pipeline/config.yaml shipped in this repo — not a
    synthetic fixture — must itself use "auto" and resolve to today, not a
    frozen literal (the exact regression that shipped 2026-07-03)."""
    cfg = lib.load_config()
    expected = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert cfg["snapshot"]["label"] == expected


# --- _refresh_requested (CLI flag / env var) --------------------------------
def test_refresh_requested_by_cli_flag():
    assert pull_bazaar._refresh_requested(["--refresh"], {})
    assert not pull_bazaar._refresh_requested([], {})


def test_refresh_requested_by_env_var():
    assert pull_bazaar._refresh_requested([], {"PULL_BAZAAR_REFRESH": "1"})
    assert not pull_bazaar._refresh_requested([], {"PULL_BAZAAR_REFRESH": "0"})
    assert not pull_bazaar._refresh_requested([], {})


# --- _cache_is_stale ---------------------------------------------------------
def test_cache_is_stale_false_when_nothing_cached(tmp_path):
    stale, msg = pull_bazaar._cache_is_stale(tmp_path / "bazaar" / "none", 2)
    assert stale is False and msg is None


def test_cache_is_stale_false_when_fresh(tmp_path):
    out_dir = tmp_path / "bazaar" / "lbl"
    out_dir.mkdir(parents=True)
    (out_dir / "page_0000.json.gz").write_bytes(b"x")
    stale, msg = pull_bazaar._cache_is_stale(out_dir, 2)
    assert stale is False and msg is None


def test_cache_is_stale_true_when_old(tmp_path):
    out_dir = tmp_path / "bazaar" / "lbl"
    out_dir.mkdir(parents=True)
    page = out_dir / "page_0000.json.gz"
    page.write_bytes(b"x")
    old = time.time() - 10 * 86400
    os.utime(page, (old, old))
    stale, msg = pull_bazaar._cache_is_stale(out_dir, 2)
    assert stale is True
    assert "days" in msg and str(out_dir) in msg


# --- pull(): cache reuse / staleness / refresh, end to end (fake client) ---
def test_pull_reuses_fresh_cache_and_never_fetches(tmp_path, monkeypatch):
    monkeypatch.setattr(pull_bazaar, "RAW_DIR", tmp_path)
    monkeypatch.setattr(pull_bazaar, "Client", _FakeClient)
    _FakeClient.instances.clear()
    out_dir = tmp_path / "bazaar" / "testlabel"
    _cached_page(out_dir, "https://cached.example/x", "2026-08-10T00:00:00Z")

    items = pull_bazaar.pull(_cfg(), refresh=False)
    assert items[0]["resource"] == "https://cached.example/x"
    assert _FakeClient.instances[0].calls == []      # constructed, never used


def test_pull_stale_cache_warns_and_refetches(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pull_bazaar, "RAW_DIR", tmp_path)
    monkeypatch.setattr(pull_bazaar, "Client", _FakeClient)
    _FakeClient.instances.clear()
    out_dir = tmp_path / "bazaar" / "testlabel"
    old = time.time() - 5 * 86400            # 5 days old, max_cache_age_days=2
    _cached_page(out_dir, "https://cached.example/x", "2026-06-01T00:00:00Z",
                mtime=old)

    items = pull_bazaar.pull(_cfg(max_cache_age_days=2), refresh=False)
    out = capsys.readouterr().out
    assert "WARNING: cached pages look stale" in out
    assert "will NOT be" in out
    assert items[0]["resource"] == "https://fetched.example/x"   # re-fetched
    assert _FakeClient.instances[0].calls != []


def test_pull_refresh_flag_forces_refetch_even_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(pull_bazaar, "RAW_DIR", tmp_path)
    monkeypatch.setattr(pull_bazaar, "Client", _FakeClient)
    _FakeClient.instances.clear()
    out_dir = tmp_path / "bazaar" / "testlabel"
    _cached_page(out_dir, "https://cached.example/x", "2026-08-10T00:00:00Z")

    items = pull_bazaar.pull(_cfg(), refresh=True)
    assert items[0]["resource"] == "https://fetched.example/x"
    assert _FakeClient.instances[0].calls != []


def test_pull_refresh_overwrites_stale_page_on_disk(tmp_path, monkeypatch):
    """Re-fetched pages must land back in the SAME label dir (append-safe,
    not a parallel location) so downstream tooling keeps finding them."""
    monkeypatch.setattr(pull_bazaar, "RAW_DIR", tmp_path)
    monkeypatch.setattr(pull_bazaar, "Client", _FakeClient)
    _FakeClient.instances.clear()
    out_dir = tmp_path / "bazaar" / "testlabel"
    page = _cached_page(out_dir, "https://cached.example/x",
                        "2026-06-01T00:00:00Z")

    pull_bazaar.pull(_cfg(), refresh=True)
    assert page.exists()
    body = lib.load_raw_gz(page)
    assert body["items"][0]["resource"] == "https://fetched.example/x"


# --- check_data_freshness (the regression this whole task is about) --------
def test_check_data_freshness_warns_when_stale(capsys):
    items = [{"lastUpdated": "2026-07-01T00:00:00Z"},
             {"lastUpdated": "2026-07-03T03:57:14Z"},
             {"lastUpdated": "2026-06-15T00:00:00Z"}]
    pull_bazaar.check_data_freshness(items, max_data_age_days=14)
    out = capsys.readouterr().out
    assert "newest item lastUpdated: 2026-07-03T03:57:14Z" in out
    assert "WARNING: CATALOG LOOKS STALE" in out
    assert "2026-07-03T03:57:14Z" in out                          # data date
    today = datetime.now(timezone.utc).date().isoformat()
    assert today in out                                           # now date


def test_check_data_freshness_silent_when_within_window(capsys):
    now = datetime.now(timezone.utc)
    items = [{"lastUpdated": now.isoformat()}]
    pull_bazaar.check_data_freshness(items, max_data_age_days=14)
    out = capsys.readouterr().out
    assert "WARNING: CATALOG LOOKS STALE" not in out
    assert "newest item lastUpdated" in out


def test_check_data_freshness_warns_when_no_items_have_last_updated(capsys):
    pull_bazaar.check_data_freshness([{"resource": "x"}], max_data_age_days=14)
    out = capsys.readouterr().out
    assert "WARNING" in out and "no item" in out


def test_pull_then_check_data_freshness_end_to_end_fires_warning(
        tmp_path, monkeypatch, capsys):
    """The exact shape of the real incident: cached/fetched items all carry
    an old lastUpdated far outside max_data_age_days — pull() + the post-
    pull freshness check must together make that impossible to miss."""
    monkeypatch.setattr(pull_bazaar, "RAW_DIR", tmp_path)
    monkeypatch.setattr(pull_bazaar, "Client", _FakeClient)
    _FakeClient.instances.clear()
    # No cache at all: pull() falls through to the fake client, whose items
    # are fixed at lastUpdated=2026-01-01 — far in the past relative to
    # "now" in this environment.
    items = pull_bazaar.pull(_cfg(max_data_age_days=14), refresh=False)
    pull_bazaar.check_data_freshness(items, max_data_age_days=14)
    out = capsys.readouterr().out
    assert "WARNING: CATALOG LOOKS STALE" in out
    assert "2026-01-01T00:00:00Z" in out


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

import pytest


@pytest.fixture(autouse=True)
def _watchlist_to_tmp(tmp_path, monkeypatch):
    """Tests exercising run_instrumentation must never write the network-watch
    WATCHLIST.md into the real working tree (that file is the box's nightly
    artifact). Redirect the default path to a per-test temp dir."""
    monkeypatch.setenv("X402_WATCHLIST_PATH", str(tmp_path / "WATCHLIST.md"))

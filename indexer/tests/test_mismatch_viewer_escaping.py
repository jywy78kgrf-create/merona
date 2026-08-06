"""The /mismatches viewer renders ATTACKER-CONTROLLABLE strings — any
endpoint operator controls the payTo in their 402 body, and catalog listers
control resource URLs. This test loads the REAL dashboard/mismatches.html in
headless Chromium against a hostile feed and asserts nothing executes:

  - <script>/<img onerror> payloads in payto/resource render as inert text
  - no element is actually created from feed content (no img, no extra script)
  - no anchor's href ever comes from feed data (javascript: URLs can't
    become clickable)

Skips (not passes) where Playwright/Chromium is unavailable — e.g. the box —
so a skip is visible in CI output rather than a silent green.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent.parent
VIEWER = ROOT / "dashboard" / "mismatches.html"
CHROMIUM = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium")

XSS = "<img src=x onerror=window.__pwned=1>"
SCRIPT = "<script>window.__pwned=2</script>"
JSURL = "javascript:window.__pwned=3"

HOSTILE_FEED = {
    "meta": {
        "as_of": {"catalog": "2026-08-02", "live_probe": "2026-08-02"},
        "disclaimer": "recorded facts " + XSS,
        "counts": {"catalog_vs_live": 1, "payto_rotation": 1},
    },
    "catalog_vs_live": [{
        "resource": JSURL + SCRIPT,
        "advertised_payto": XSS,
        "advertised_seen_onchain": False,
        "live_paytos": [SCRIPT],
        "live_seen_onchain": {SCRIPT: False},
        "catalog_date": "2026-08-02", "probe_date": XSS,
    }],
    "payto_rotation": [{
        "resource": XSS, "n_wallets": 2, "latest_change": "2026-08-02",
        "timeline": [
            {"payto": SCRIPT + "aaaa", "first_seen": XSS,
             "last_seen": "2026-08-02", "seen_onchain": True},
        ],
    }],
}


def test_hostile_feed_renders_inert(tmp_path):
    if not Path(CHROMIUM).exists():
        pytest.skip(f"chromium not at {CHROMIUM}")
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROMIUM)
        pg = b.new_page()
        pg.route("**/mismatches.json",
                 lambda r: r.fulfill(json=HOSTILE_FEED))
        pg.goto("file://" + str(VIEWER))
        pg.wait_for_timeout(500)

        # nothing executed
        assert pg.evaluate("window.__pwned") is None
        # no element materialized from feed content
        assert pg.evaluate("document.querySelectorAll('img').length") == 0
        assert pg.evaluate(
            "[...document.scripts].filter(s=>s.textContent.includes('__pwned')).length") == 0
        # payloads are visible as TEXT (escaped), i.e. the data still renders
        body = pg.evaluate("document.body.innerText")
        assert "onerror" in body and "<script>" in body
        # no href derives from feed data — all anchors are ours
        hrefs = pg.evaluate(
            "[...document.querySelectorAll('a')].map(a=>a.getAttribute('href'))")
        assert hrefs and all(
            h.startswith(("/", "https://api.merona.io")) for h in hrefs)
        b.close()

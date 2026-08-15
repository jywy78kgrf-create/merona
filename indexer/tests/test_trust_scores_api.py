"""Score v2 (wash-signal joins, caps) + the trust API end to end.

Builds a synthetic SQLite store + snapshot fixture, runs the REAL
compute_seller_scores, then serves the REAL trust_api over HTTP and asserts
auth, lookups, verification block, normalization, 404/400, rate limiting,
and usage metering. No network beyond localhost; no Postgres needed.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # indexer/
sys.path.insert(0, str(HERE.parent.parent / "serving"))

PORT = 18403
ADDR_CLEAN = "0x" + "a" * 40
ADDR_WASH = "0x" + "b" * 40


def _get(path, key=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}")
    if key:
        req.add_header("X-API-Key", key)
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_score_v2_and_trust_api(tmp_path, monkeypatch):
    snap = tmp_path / "snapshots" / "2026-07-13"
    snap.mkdir(parents=True)
    flags = tmp_path / "flags"
    flags.mkdir()
    monkeypatch.delenv("X402_DB_URL", raising=False)
    monkeypatch.setenv("WASH_FLAGS_DIR", str(flags))
    monkeypatch.setenv("TRUST_API_PORT", str(PORT))
    monkeypatch.setenv("TRUST_API_KEYS", "sk_test_abc:demo")
    monkeypatch.setenv("TRUST_API_RPM", "600")
    monkeypatch.setenv("TRUST_API_BURST", "8")
    monkeypatch.setenv("TRUST_API_USAGE", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("TRUST_API_SQLITE", str(tmp_path / "test.db"))

    import storage
    import scores
    monkeypatch.setattr(scores, "SNAP_DIR", snap.parent)
    monkeypatch.setattr(scores, "FLAGS_DIR", flags)
    monkeypatch.setattr(scores, "MIN_MATURE_DAYS", 1)

    now = int(time.time())
    with open(snap / "seller_aggregates.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["chain", "seller", "tx_count",
                                          "unique_payers", "self_pay_tx",
                                          "first_ts", "last_ts"])
        w.writeheader()
        for s, tx, up in ((ADDR_CLEAN, 50000, 8000), (ADDR_WASH, 80000, 5000)):
            w.writerow(dict(chain="base", seller=s, tx_count=tx,
                            unique_payers=up, self_pay_tx=0,
                            first_ts=now - 90 * 86400, last_ts=now))
    # history flags: a third seller, flagged only by the batch pass
    (flags / "wash_flags_base.csv").write_text(
        f"seller,reasons\n{ADDR_WASH},funded\n")

    store = storage.open_store(str(tmp_path / "test.db"))
    store.record_wash_signal("2026-07-13", "base", ADDR_WASH, {
        "tx_count": 80000, "unique_payers": 5000, "self_pay_ratio": 0.0,
        "funded_payers": 3000, "funded_payer_ratio": 0.60, "lookback_ok": True,
        "reciprocal": False, "flagged": True, "thresholds": {}}, "t")
    store.db.commit()
    assert scores.compute_seller_scores(store, "2026-07-13") == 2

    # v2 semantics straight from the table
    r = store.db.execute("SELECT score, components FROM seller_scores "
                         "WHERE seller=?", (ADDR_WASH,)).fetchone()
    comp = json.loads(r[1])
    assert r[0] <= 49.0 and comp["wash"]["nightly_flag"]
    assert comp["wash_penalty"] == -25.0
    r = store.db.execute("SELECT score, components FROM seller_scores "
                         "WHERE seller=?", (ADDR_CLEAN,)).fetchone()
    assert r[0] >= 60 and "wash" not in json.loads(r[1])
    store.db.close()

    import importlib
    import trust_api
    importlib.reload(trust_api)                  # re-read env config
    monkeypatch.setattr(trust_api, "SNAP_DIR", snap.parent)
    threading.Thread(target=trust_api.main, daemon=True).start()
    time.sleep(0.5)

    assert _get("/")[0] == 200
    assert _get("/v1/health")[0] == 200
    # free-for-now default (2026-08-15): no key -> served, not 401
    s, b = _get(f"/v1/trust/base/{ADDR_CLEAN}")
    assert s == 200 and b["grade"]
    assert b["pricing"] == "free — no key, no signup."
    monkeypatch.setattr(trust_api, "FREE_MODE", False)
    assert _get(f"/v1/trust/base/{ADDR_CLEAN}")[0] == 401       # hard gate
    s, b = _get(f"/v1/trust/base/{ADDR_CLEAN}", "sk_test_abc")
    assert s == 200 and b["grade"] and b["score_version"] == 4
    assert b["verification"]["snapshot_sha256"]                 # provable
    s, b = _get(f"/v1/trust/base/{ADDR_WASH}", "sk_test_abc")
    assert s == 200 and b["score"] <= 49
    assert b["components"]["wash"]["nightly_flag"]
    s, _ = _get(f"/v1/trust/base/0x{'A' * 40}", "sk_test_abc")
    assert s == 200                                             # checksum norm
    assert _get(f"/v1/trust/base/0x{'c' * 40}", "sk_test_abc")[0] == 404
    assert _get("/v1/trust/base/nonsense", "sk_test_abc")[0] == 400
    codes = [_get(f"/v1/trust/base/{ADDR_CLEAN}", "sk_test_abc")[0]
             for _ in range(20)]
    assert 429 in codes                                         # rate limited
    lines = (tmp_path / "usage.jsonl").read_text().strip().splitlines()
    # handled requests are metered; rate-limited (429) requests are NOT (they'd
    # let an unthrottled flood fill the usage file — audit LB-6).
    assert len(lines) >= 6 and any('"key:demo"' in ln for ln in lines)
    assert not any('"status": 429' in ln for ln in lines)       # 429s not metered

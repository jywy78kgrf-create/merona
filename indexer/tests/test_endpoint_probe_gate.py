"""Per-resource probe escalation + endpoint-grade confidence gate.

Born from a real miss: an origin with 297 listed endpoints and 25 paying
customers wore "C, valid_402_rate 0.0" for three weeks because its first
catalog-ordered path wants query params (GET -> 400) and that single path
was the only one ever probed. Covers:

  - escalation: rep answers 400 -> alternate listed paths are tried,
    a valid 402 anywhere makes the day valid; down origins do NOT escalate
  - day-based rates: one valid probe among several makes the DAY valid
  - confidence gate: provisional origins carry grade NULL, mature ones a
    letter; the numeric score is always present
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import instrumentation
import scores
import storage


def _res(origin: str, path: str) -> dict:
    return {"resource": origin + path, "origin": origin,
            "seller_wallet": "0x" + "ab" * 20, "network": "base"}


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self._body = json.dumps(body or {}).encode()

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


VALID_402 = {"x402Version": 1, "accepts": [
    {"payTo": "0x" + "FE" * 20, "network": "base", "asset": "0x" + "11" * 20}]}


class _FakeSession:
    """Scripted per-URL responses; records the probe order. GET-then-POST
    fallback (instrumentation._probe_x402) now means a non-valid GET tries
    the SAME scripted URL again via POST — .post() reuses the same script so
    existing scripts (one entry per URL) still resolve, without changing
    which order/count of *GET* calls (`self.calls`) each test asserts on."""
    def __init__(self, script):
        self.script = script
        self.calls = []
        self.post_calls = []
        self.headers = {}

    def get(self, url, **kw):
        self.calls.append(url)
        r = self.script[url]
        if isinstance(r, Exception):
            raise r
        return r

    def post(self, url, **kw):
        self.post_calls.append(url)
        r = self.script[url]
        if isinstance(r, Exception):
            raise r
        return r


def _run_probe(monkeypatch, tmp_path, script, resources, date="2026-08-05"):
    store = storage.open_store(str(tmp_path / "t.db"))
    fake = _FakeSession(script)
    monkeypatch.setattr(instrumentation.requests, "Session", lambda: fake)
    monkeypatch.setattr(instrumentation, "PROBE_DELAY_S", 0)
    instrumentation.probe_endpoint_liveness(store, date, resources)
    return store, fake


def test_escalates_past_param_requiring_path(monkeypatch, tmp_path):
    o = "https://x402.metrics.example"
    resources = [_res(o, f"/v1/m/{i}") for i in range(297)]
    # first listed path 400s (wants query params); the middle path 402s
    script = {r["resource"]: _Resp(400) for r in resources}
    script[o + "/v1/m/148"] = _Resp(402, VALID_402)
    store, fake = _run_probe(monkeypatch, tmp_path, script, resources)

    # rep first, then the deterministic middle fallback; stop on valid 402
    assert fake.calls == [o + "/v1/m/0", o + "/v1/m/148"]
    rows = store.db.execute(
        "SELECT endpoint_url, valid_402 FROM endpoint_liveness").fetchall()
    assert len(rows) == 2
    assert dict(rows)[o + "/v1/m/148"]
    # the live offer from the escalated probe still feeds the payTo feed
    offers = store.db.execute("SELECT payto FROM live_offers").fetchall()
    assert offers and offers[0][0] == ("0x" + "fe" * 20)


def test_down_origin_does_not_escalate(monkeypatch, tmp_path):
    o = "https://dead.example"
    resources = [_res(o, f"/p{i}") for i in range(5)]
    script = {r["resource"]: ConnectionError("refused") for r in resources}
    store, fake = _run_probe(monkeypatch, tmp_path, script, resources)
    assert fake.calls == [o + "/p0"]          # down is down; one probe only
    rows = store.db.execute(
        "SELECT error FROM endpoint_liveness").fetchall()
    assert len(rows) == 1 and "refused" in rows[0][0]


def test_valid_first_probe_stops_immediately(monkeypatch, tmp_path):
    o = "https://healthy.example"
    resources = [_res(o, f"/p{i}") for i in range(9)]
    script = {r["resource"]: _Resp(402, VALID_402) for r in resources}
    _store, fake = _run_probe(monkeypatch, tmp_path, script, resources)
    assert fake.calls == [o + "/p0"]


def test_overflow_rotates_instead_of_freezing(monkeypatch, tmp_path):
    origins = [f"https://o{i}.example" for i in range(6)]
    resources = [_res(o, "/p") for o in origins]
    script = {r["resource"]: _Resp(402, VALID_402) for r in resources}
    monkeypatch.setattr(instrumentation, "LIVENESS_MAX_ORIGINS", 3)

    probed = {}
    for day in ("2026-08-05", "2026-08-06", "2026-08-07"):
        store, fake = _run_probe(monkeypatch, tmp_path / day, script,
                                 resources, date=day)
        probed[day] = {u.rsplit("/", 1)[0] for u in fake.calls}
        assert len(probed[day]) == 3
    # date-salted rotation: the sampled set must not be identical every night
    assert len(set(map(frozenset, probed.values()))) > 1
    # deterministic per date: same day replans identically
    store2, fake2 = _run_probe(monkeypatch, tmp_path / "re", script,
                               resources, date="2026-08-05")
    assert {u.rsplit("/", 1)[0] for u in fake2.calls} == probed["2026-08-05"]


def test_day_based_rates_and_confidence_gate(tmp_path):
    store = storage.open_store(str(tmp_path / "t.db"))
    o = "https://x402.metrics.example"
    now = "2026-08-05T00:00:00+00:00"

    def liveness(day, url, status, v402):
        store.record_endpoint_liveness(day, {
            "endpoint_url": url, "seller_wallet": None, "network": "base",
            "http_status": status, "latency_ms": 100, "valid_402": v402}, now)

    # young origin: 2 days, each day one 400 then one valid 402 (escalation)
    for day in ("2026-08-04", "2026-08-05"):
        liveness(day, o + "/v1/m/0", 400, False)
        liveness(day, o + "/v1/m/148", 402, True)
    # week-old origin: MIN_LETTER_DAYS days -> provisional letter
    # mature origin: MIN_MATURE_DAYS days -> verified letter
    # (dated backwards from today so every day lands inside the window)
    from datetime import date, timedelta
    for i in range(scores.MIN_LETTER_DAYS):
        d = (date.today() - timedelta(days=i)).isoformat()
        liveness(d, "https://week.example/p", 402, True)
    for i in range(scores.MIN_MATURE_DAYS):
        d = (date.today() - timedelta(days=i)).isoformat()
        liveness(d, "https://mature.example/p", 402, True)

    assert scores.compute_endpoint_scores(store, "2026-08-05") == 3
    rows = {r[0]: r for r in store.db.execute(
        "SELECT origin, grade, provisional, valid_402_rate, uptime_rate, "
        "score FROM endpoint_scores WHERE measured_date='2026-08-05'")}

    young = rows[o]
    # a day with ANY valid probe is a valid day: rate 1.0 despite the 400s
    assert young[3] == 1.0 and young[4] == 1.0
    # tier 1 — unrated: no letter, numeric score present
    assert young[1] is None and bool(young[2]) and young[5] is not None

    # tier 2 — 7 observed days: letter appears, still marked provisional
    week = rows["https://week.example"]
    assert week[1] == "A" and bool(week[2])

    # tier 3 — 14 observed days: verified letter
    mature = rows["https://mature.example"]
    assert mature[1] == "A" and not bool(mature[2])

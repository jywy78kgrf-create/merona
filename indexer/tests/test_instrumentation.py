"""Passive-instrumentation tests. The load-bearing requirement: OBSERVE-ONLY
isolation — a crash in any probe must not touch settlements or stop the others.
Network is stubbed so tests are deterministic and offline."""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instrumentation as I  # noqa: E402
from storage import Store  # noqa: E402

PG_URL = os.environ.get("X402_TEST_DB_URL")


def _seed(store):
    now = int(time.time())

    def row(tx, li, chain, amt, ts):
        return dict(tx_hash=tx, log_index=li, chain=chain, token="0xusdc",
                    payer="0xp", seller="0xs", amount=amt,
                    block_number=1000 + li, block_timestamp=ts)
    store.commit_range("base", 1000, 1001,
                       [row("0xa", 0, "base", 1_000_000, now - 3600)], "t")
    store.commit_solana_batch([
        {**row("s1", 0, "solana", 500_000, now - 7200), "facilitator": "relAlpha"},
        {**row("s2", 1, "solana", 900_000, now - 5400), "facilitator": "relBeta"}])
    return now


def test_hourly_and_facilitator_derive(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    assert I.aggregate_hourly(s, "2026-07-05") >= 2      # base + solana hours
    assert I.compute_facilitator_health(s, "2026-07-05", ["base", "solana"]) >= 3
    # EVM has NO facilitator -> recorded as unattributed superset, with a note
    evm = s.db.execute("SELECT facilitator,derivable_note FROM facilitator_health "
                       "WHERE chain='base'").fetchone()
    assert evm[0] == "_unattributed_evm_superset" and "superset" in evm[1]
    # Solana IS attributed per-facilitator, latency/revert NULL (not derivable)
    sol = s.db.execute("SELECT facilitator,revert_count,median_latency_ms "
                       "FROM facilitator_health WHERE chain='solana' "
                       "AND facilitator='relAlpha'").fetchone()
    assert sol[0] == "relAlpha" and sol[1] is None and sol[2] is None
    s.close()


def test_valid_402_detection():
    class R:
        status_code = 402
        def iter_content(self, n):
            yield b'{"x402Version":1,"accepts":[]}'
    assert I._looks_like_x402_402(R()) is True

    class NotJson:
        status_code = 402
        def iter_content(self, n):
            yield b'<html>pay me</html>'
    assert I._looks_like_x402_402(NotJson()) is False

    class OK:
        status_code = 200
        def iter_content(self, n):
            yield b'{}'
    assert I._looks_like_x402_402(OK()) is False


def test_v2_payment_required_header_dialect_parsed():
    """x402 v2: the challenge travels in a base64 PAYMENT-REQUIRED response
    header, and the JSON body is just a human-readable summary with no
    'accepts' — confirmed live (attest/payprobe.py's parse_offer, commit
    0a46880, observed 2026-08-07). A body-only parser mis-scores these as
    invalid; _parse_402_offers must check the header too."""
    import base64
    import json as _json

    offer = {"x402Version": 2,
             "accepts": [{"payTo": "0xdef", "network": "base",
                          "asset": "USDC"}]}
    hdr_val = base64.b64encode(_json.dumps(offer).encode()).decode()

    class V2Resp:
        status_code = 402
        headers = {"content-type": "text/plain",
                   "PAYMENT-REQUIRED": hdr_val}
        def iter_content(self, n):
            yield b"Payment required. See PAYMENT-REQUIRED header."

    valid, offers = I._parse_402_offers(V2Resp())
    assert valid is True
    assert offers and offers[0]["payto"] == "0xdef"


def test_v1_body_dialect_still_parses_without_header():
    """No PAYMENT-REQUIRED header present -> falls back to the JSON body
    (v1), unchanged behavior."""
    class V1Resp:
        status_code = 402
        headers = {"content-type": "application/json"}
        def iter_content(self, n):
            yield b'{"x402Version":1,"accepts":[{"payTo":"0xaaa",' \
                  b'"network":"base","asset":"USDC"}]}'

    valid, offers = I._parse_402_offers(V1Resp())
    assert valid is True
    assert offers and offers[0]["payto"] == "0xaaa"


def test_isolation_probe_crash_does_not_touch_settlements(tmp_path, monkeypatch):
    """Kill a probe mid-run: settlement rows unchanged, other probes still run,
    nothing propagates."""
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    before = s.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]

    # no network: Bazaar returns nothing (endpoint probes skip)
    monkeypatch.setattr(I, "fetch_bazaar_resources", lambda *a, **k: ([], 0))
    # facilitator_health explodes
    def boom(*a, **k):
        raise RuntimeError("simulated probe crash")
    monkeypatch.setattr(I, "compute_facilitator_health", boom)

    # must not raise
    I.run_instrumentation(s, "2026-07-05", {"fingerprint_version": 1},
                          chains=["base", "solana"])

    after = s.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    assert after == before                      # settlement ledger untouched
    # the crashing probe wrote nothing; the healthy one (hourly) still ran
    assert s.db.execute("SELECT COUNT(*) FROM facilitator_health").fetchone()[0] == 0
    assert s.db.execute("SELECT COUNT(*) FROM hourly_settlements").fetchone()[0] > 0
    # fingerprint still stamped
    assert s.db.execute("SELECT COUNT(*) FROM daily_fingerprint").fetchone()[0] == 1
    s.close()


def test_isolation_runner_never_raises(tmp_path, monkeypatch):
    s = Store(tmp_path / "t.sqlite")
    _seed(s)
    for fn in ("aggregate_hourly", "compute_facilitator_health",
               "fetch_bazaar_resources"):
        def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(I, fn, boom)
    # everything broken -> still returns cleanly, settlements safe (1 base + 2 sol)
    I.run_instrumentation(s, "2026-07-05", {"fingerprint_version": 1}, ["base"])
    assert s.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 3
    s.close()


@pytest.mark.skipif(not PG_URL, reason="X402_TEST_DB_URL not set")
def test_settlement_collectors_postgres():
    """Regression: the settlement-derived collectors must run on Postgres. A
    literal '%' in a parameterized LIKE broke psycopg's placeholder parser and
    failed hourly_settlements + facilitator_health on the live box."""
    import psycopg

    from storage import PostgresStore
    with psycopg.connect(PG_URL, autocommit=True) as c:
        for t in ("settlements", "indexed_ranges", "meta", "solana_cursors",
                  "coverage_ratio", "daily_fingerprint", "hourly_settlements",
                  "facilitator_health", "endpoint_liveness", "domain_intel"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
    s = PostgresStore(PG_URL)
    _seed(s)
    assert I.aggregate_hourly(s, "2026-07-05") >= 2
    assert I.compute_facilitator_health(s, "2026-07-05", ["base", "solana"]) >= 3
    s.close()


def test_domain_change_detection(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.record_domain_intel("2026-07-04", "x.com",
                          {"dns_fingerprint": "1.1.1.1"}, "t")
    assert s.last_domain_fingerprint("x.com", "2026-07-05") == "1.1.1.1"
    s.record_domain_intel("2026-07-05", "x.com",
                          {"dns_fingerprint": "2.2.2.2",
                           "changed_since_last": s.last_domain_fingerprint("x.com", "2026-07-05") != "2.2.2.2"},
                          "t")
    changed = s.db.execute("SELECT changed_since_last FROM domain_intel "
                           "WHERE measured_date='2026-07-05'").fetchone()[0]
    assert changed  # 1.1.1.1 -> 2.2.2.2
    s.close()


def test_origin_probe_plan_is_one_entry_per_origin_not_per_endpoint():
    """Coverage is measured in ORIGINS (hostnames), not endpoints: the whole
    liveness-vs-directory comparison rests on this. A host with many listed
    endpoints/legs must collapse to a single plan entry, so LIVENESS_MAX_ORIGINS
    bounds hosts probed, and one origin's liveness generalizes to its paths."""
    resources = []
    # busy origin: 12 endpoints (like x402.ottoai.services in the wild)
    for i in range(12):
        resources.append({"origin": "https://busy.example",
                          "resource": f"https://busy.example/api/{i}",
                          "seller_wallet": "0x" + "aa" * 20, "network": "base"})
    # a resource listed twice (two accepts legs) must not double-count
    for _ in range(2):
        resources.append({"origin": "https://dup.example",
                          "resource": "https://dup.example/one",
                          "seller_wallet": "0x" + "bb" * 20, "network": "base"})
    resources.append({"origin": "https://solo.example",
                      "resource": "https://solo.example/x",
                      "seller_wallet": "0x" + "cc" * 20, "network": "base"})
    plan = I._origin_probe_plan(resources)
    origins = [o for o, _ in plan]
    assert origins == ["https://busy.example", "https://dup.example",
                       "https://solo.example"]          # 3 origins, not 15
    picks = dict(plan)
    # dedup within an origin: the twice-listed URL appears once
    assert len(picks["https://dup.example"]) == 1
    # busy origin gets rep + fallbacks, bounded by LIVENESS_402_FALLBACKS
    assert len(picks["https://busy.example"]) == 1 + I.LIVENESS_402_FALLBACKS


def test_liveness_cap_covers_the_current_catalog():
    """The cap exists only to bound runtime; it must sit above the real origin
    count (~2,900) so no origin rotates out on a normal night — otherwise the
    free feed's 'no mismatch found' is silence, not evidence, for the remainder
    (2026-08-13: raised 2500 -> 6000 for exactly this)."""
    assert I.LIVENESS_MAX_ORIGINS >= 3000


# --- probe_endpoints_full: per-endpoint sweep, capped per host --------------
# Everything network-shaped is stubbed with a fake requests.Session so these
# run offline and deterministically, same discipline as the module docstring.

class _FakeResp402:
    """Canned valid x402 402 (one accepts leg) — reused across the fakes below
    so tests exercise the exact _parse_402_offers/record_live_offer path."""

    def __init__(self, status=402, payto="0xabc", network="base"):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self._body = (('{"x402Version":1,"accepts":[{"payTo":"%s",'
                       '"network":"%s","asset":"USDC"}]}') % (payto, network)
                      ).encode()

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


class _FakeResp:
    """Arbitrary-status fake response with no valid 402 offer — e.g. the 405
    a POST-only endpoint gives to GET, or a plain dead-endpoint answer."""

    def __init__(self, status=405, body=b'{}'):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self._body = body

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


class _FakeSession:
    """Records every instance created, so tests can assert one Session per
    worker thread (not one shared across the pool). GET answers with a valid
    402 by default (existing per-endpoint-sweep tests rely on this); POST
    also answers with a valid 402 but is expected to go UNCALLED whenever GET
    already validated (see post_calls / test_probe_x402_get_fast_path_*)."""
    created: list = []

    def __init__(self):
        self.headers = {}
        self.post_calls = 0
        _FakeSession.created.append(self)

    def get(self, url, **kw):
        return _FakeResp402()

    def post(self, url, **kw):
        self.post_calls += 1
        return _FakeResp402()


def _resources_for_hosts(host_counts: dict) -> list:
    """{host: n_endpoints} -> flat resource list, one dict per distinct URL."""
    out = []
    for host, n in host_counts.items():
        for i in range(n):
            out.append({"resource": f"https://{host}/p{i}",
                       "seller_wallet": "0x" + "aa" * 20, "network": "base"})
    return out


@pytest.fixture(autouse=True)
def _reset_fake_session():
    _FakeSession.created = []
    yield
    _FakeSession.created = []


def test_per_endpoint_plan_host_under_cap_probes_everything():
    resources = _resources_for_hosts({"small.example": 10})
    plan = I._endpoint_probe_plan(resources, "2026-08-13")
    assert len(plan["small.example"]) == 10


def test_per_endpoint_plan_caps_a_big_host_and_rotates_by_date():
    resources = _resources_for_hosts({"big.example": 3 * I.LIVENESS_PER_HOST_CAP})
    plan_a = I._endpoint_probe_plan(resources, "2026-08-13")
    plan_b = I._endpoint_probe_plan(resources, "2026-08-14")
    assert len(plan_a["big.example"]) == I.LIVENESS_PER_HOST_CAP
    assert len(plan_b["big.example"]) == I.LIVENESS_PER_HOST_CAP
    urls_a = {r["resource"] for r in plan_a["big.example"]}
    urls_b = {r["resource"] for r in plan_b["big.example"]}
    # a different date yields a (mostly) different sampled subset
    assert urls_a != urls_b
    # over enough dates, every endpoint on the host accrues coverage
    seen: set = set()
    for d in range(30):
        p = I._endpoint_probe_plan(resources, f"2026-08-{d + 1:02d}")
        seen |= {r["resource"] for r in p["big.example"]}
    all_urls = {r["resource"] for r in resources}
    assert seen == all_urls


def test_per_endpoint_plan_dedupes_url_listed_as_two_legs():
    dup = [{"resource": "https://d.example/one", "seller_wallet": "0xa",
           "network": "base"},
          {"resource": "https://d.example/one", "seller_wallet": "0xa",
           "network": "polygon"}]
    plan = I._endpoint_probe_plan(dup, "2026-08-13")
    assert len(plan["d.example"]) == 1


def test_probe_endpoints_full_one_session_per_host(tmp_path, monkeypatch):
    """Concurrency correctness without real network: hosts run in parallel,
    each on its OWN Session (not one shared across the pool)."""
    monkeypatch.setattr(I.requests, "Session", _FakeSession)
    s = Store(tmp_path / "t.sqlite")
    resources = _resources_for_hosts({"a.example": 3, "b.example": 2,
                                      "c.example": 4})
    n = I.probe_endpoints_full(s, "2026-08-13", resources)
    assert n == 9                                    # 3 + 2 + 4 distinct URLs
    assert len(_FakeSession.created) == 3             # one Session per host

    # DB writes happened (in the main thread, after the pool completed):
    # every probed URL has exactly one liveness row + one live_offer row.
    live_rows = s.db.execute(
        "SELECT COUNT(*) FROM endpoint_liveness WHERE measured_date=?",
        ("2026-08-13",)).fetchone()[0]
    offer_rows = s.db.execute(
        "SELECT COUNT(*) FROM live_offers WHERE measured_date=?",
        ("2026-08-13",)).fetchone()[0]
    assert live_rows == 9
    assert offer_rows == 9
    valid = s.db.execute(
        "SELECT COUNT(*) FROM endpoint_liveness WHERE valid_402=1"
    ).fetchone()[0]
    assert valid == 9
    s.close()


def test_probe_endpoints_full_host_failure_records_error_and_continues(
        tmp_path, monkeypatch):
    """A host whose probing raises still gets an error row per pending URL,
    and it never aborts the other hosts' results."""

    class _BoomSession:
        def __init__(self):
            self.headers = {}

        def get(self, url, **kw):
            if "boom.example" in url:
                raise ConnectionError("simulated network failure")
            return _FakeResp402()

        def post(self, url, **kw):
            # GET already raised for boom.example, so _probe_x402 retries
            # here too; both verbs failing is what should surface as the
            # ConnectionError row (not an AttributeError from a missing
            # .post). ok.example never gets here (GET already validated).
            if "boom.example" in url:
                raise ConnectionError("simulated network failure")
            return _FakeResp402()

    monkeypatch.setattr(I.requests, "Session", _BoomSession)
    s = Store(tmp_path / "t.sqlite")
    resources = _resources_for_hosts({"boom.example": 3, "ok.example": 2})
    n = I.probe_endpoints_full(s, "2026-08-13", resources)
    assert n == 5                                     # sweep completes fully

    boom_rows = s.db.execute(
        "SELECT error FROM endpoint_liveness WHERE endpoint_url LIKE "
        "'%boom.example%'").fetchall()
    assert len(boom_rows) == 3
    assert all(e[0] and "ConnectionError" in e[0] for e in boom_rows)
    # the healthy host's rows are untouched by the other host's failure
    ok_rows = s.db.execute(
        "SELECT valid_402 FROM endpoint_liveness WHERE endpoint_url LIKE "
        "'%ok.example%'").fetchall()
    assert len(ok_rows) == 2
    assert all(r[0] for r in ok_rows)
    s.close()


def test_probe_endpoints_full_per_url_error_does_not_abort_host(
        tmp_path, monkeypatch):
    """Same isolation, one level down: one bad URL on an otherwise-healthy
    host still yields an error row for that URL and valid rows for the rest
    (matches probe_endpoint_liveness's per-probe try/except discipline)."""

    class _PartialSession:
        def __init__(self):
            self.headers = {}

        def get(self, url, **kw):
            if url.endswith("/p1"):
                raise TimeoutError("simulated timeout")
            return _FakeResp402()

        def post(self, url, **kw):
            # /p1's GET already raised, so _probe_x402 retries with POST;
            # keep it failing the same way so the row still reports a
            # TimeoutError (not an unrelated AttributeError).
            if url.endswith("/p1"):
                raise TimeoutError("simulated timeout")
            return _FakeResp402()

    monkeypatch.setattr(I.requests, "Session", _PartialSession)
    s = Store(tmp_path / "t.sqlite")
    resources = _resources_for_hosts({"mixed.example": 3})
    n = I.probe_endpoints_full(s, "2026-08-13", resources)
    assert n == 3
    bad = s.db.execute("SELECT error FROM endpoint_liveness WHERE "
                       "endpoint_url=?", ("https://mixed.example/p1",)
                       ).fetchone()[0]
    assert bad and "TimeoutError" in bad
    good = s.db.execute("SELECT COUNT(*) FROM endpoint_liveness WHERE "
                        "error IS NULL AND measured_date=?",
                        ("2026-08-13",)).fetchone()[0]
    assert good == 2
    s.close()


def test_probe_endpoints_full_endpoint_max_backstop(monkeypatch):
    """The aggregate LIVENESS_MAX_ENDPOINTS backstop rotate-samples down
    across ALL capped urls (not per host) when many small hosts together
    exceed it, and logs the truncation."""
    monkeypatch.setattr(I, "LIVENESS_MAX_ENDPOINTS", 25)
    resources = _resources_for_hosts({f"h{i}.example": 5 for i in range(10)})
    plan = I._endpoint_probe_plan(resources, "2026-08-13")
    total = sum(len(v) for v in plan.values())
    assert total == 25


# --- _probe_x402: the shared GET-then-POST rescue for POST-only endpoints ---
# Audit fix (2026-08-13): the402 reports ~5,273 of ~14,402 catalog endpoints
# answer ONLY to POST; GET-only probing scored them dead/invalid and their
# live_offers never populated. These exercise _probe_x402 directly (unit
# level) and through both call sites that wire it in (integration level).

class _RoutedSession:
    """Fake Session whose GET/POST behavior is supplied per-call as a
    (response_or_exception) factory, with call counters so tests can assert
    the POST fast-path is skipped when GET already validates."""

    def __init__(self, get_fn, post_fn=None):
        self.headers = {}
        self._get_fn = get_fn
        self._post_fn = post_fn
        self.get_calls = 0
        self.post_calls = 0

    def get(self, url, **kw):
        self.get_calls += 1
        return self._get_fn(url)

    def post(self, url, **kw):
        self.post_calls += 1
        if self._post_fn is None:
            raise AssertionError(f"POST unexpectedly called for {url}")
        return self._post_fn(url)


def _raise(exc):
    def _fn(url):
        raise exc
    return _fn


def test_probe_x402_get_fast_path_skips_post():
    """GET answers with a valid 402 -> POST is NEVER called (one request)."""
    sess = _RoutedSession(get_fn=lambda url: _FakeResp402())
    r, valid, offers, method = I._probe_x402(sess, "https://get-ok.example/x")
    assert valid is True and method == "GET"
    assert offers and offers[0]["payto"] == "0xabc"
    assert sess.get_calls == 1
    assert sess.post_calls == 0


def test_probe_x402_post_only_endpoint_rescued():
    """GET gets a 405 (not a 402); POST rescues it with a valid 402 ->
    valid_402 True, offers captured, method recorded as POST."""
    sess = _RoutedSession(
        get_fn=lambda url: _FakeResp(status=405),
        post_fn=lambda url: _FakeResp402(payto="0xpostonly"))
    r, valid, offers, method = I._probe_x402(sess, "https://post-only.example/x")
    assert valid is True and method == "POST"
    assert offers and offers[0]["payto"] == "0xpostonly"
    assert sess.get_calls == 1 and sess.post_calls == 1


def test_probe_x402_truly_dead_endpoint():
    """GET and POST both answer, neither with a valid 402 -> valid False, no
    offers, no crash; the caller still gets a usable (response, method)."""
    sess = _RoutedSession(
        get_fn=lambda url: _FakeResp(status=404),
        post_fn=lambda url: _FakeResp(status=404))
    r, valid, offers, method = I._probe_x402(sess, "https://dead.example/x")
    assert valid is False
    assert offers == []
    assert r is not None                  # still something to build a row from
    assert sess.get_calls == 1 and sess.post_calls == 1


def test_probe_x402_get_network_error_post_succeeds():
    """GET raises outright; POST succeeds with a valid 402 -> handled, no
    raise, correct (POST) row."""
    sess = _RoutedSession(
        get_fn=_raise(ConnectionError("simulated")),
        post_fn=lambda url: _FakeResp402(payto="0xrescued"))
    r, valid, offers, method = I._probe_x402(sess, "https://flaky-get.example/x")
    assert valid is True and method == "POST"
    assert offers and offers[0]["payto"] == "0xrescued"


def test_probe_x402_post_network_error_get_answered_non402():
    """GET answers (not a valid 402); POST then raises -> handled, no raise,
    falls back to the GET response/verdict (most informative thing we have)."""
    sess = _RoutedSession(
        get_fn=lambda url: _FakeResp(status=403),
        post_fn=_raise(TimeoutError("simulated")))
    r, valid, offers, method = I._probe_x402(sess, "https://flaky-post.example/x")
    assert valid is False and method == "GET"
    assert r.status_code == 403


def test_probe_x402_both_verbs_network_error_raises():
    """Both GET and POST raise -> _probe_x402 itself raises, preserving the
    existing per-probe try/except discipline at the call sites (a network
    error becomes an error row there, never an unhandled crash here)."""
    sess = _RoutedSession(
        get_fn=_raise(ConnectionError("get down")),
        post_fn=_raise(ConnectionError("post down")))
    with pytest.raises(ConnectionError):
        I._probe_x402(sess, "https://both-down.example/x")


# --- integration: both call sites wire _probe_x402 in --------------------

def test_probe_endpoint_liveness_rescues_post_only_endpoint(tmp_path, monkeypatch):
    """probe_endpoint_liveness (origin-level sample) rescues a POST-only
    endpoint via the shared helper: valid_402 True, live_offers populated."""
    sess = _RoutedSession(
        get_fn=lambda url: _FakeResp(status=405),
        post_fn=lambda url: _FakeResp402(payto="0xorigin"))
    monkeypatch.setattr(I.requests, "Session", lambda: sess)
    monkeypatch.setattr(I, "PROBE_DELAY_S", 0)
    s = Store(tmp_path / "t.sqlite")
    resources = [{"origin": "https://post-origin.example",
                 "resource": "https://post-origin.example/pay",
                 "seller_wallet": "0x" + "aa" * 20, "network": "base"}]
    n = I.probe_endpoint_liveness(s, "2026-08-13", resources)
    assert n == 1
    row = s.db.execute(
        "SELECT valid_402 FROM endpoint_liveness WHERE endpoint_url=?",
        ("https://post-origin.example/pay",)).fetchone()
    assert row[0]
    offer = s.db.execute(
        "SELECT payto FROM live_offers WHERE endpoint_url=?",
        ("https://post-origin.example/pay",)).fetchone()
    assert offer[0] == "0xorigin"
    s.close()


def test_probe_endpoints_full_rescues_post_only_endpoint(tmp_path, monkeypatch):
    """probe_endpoints_full / _probe_host_serial (per-endpoint sweep) rescues
    a POST-only endpoint via the same shared helper."""

    class _PostOnlySession:
        def __init__(self):
            self.headers = {}

        def get(self, url, **kw):
            return _FakeResp(status=405)

        def post(self, url, **kw):
            return _FakeResp402(payto="0xsweep")

    monkeypatch.setattr(I.requests, "Session", _PostOnlySession)
    s = Store(tmp_path / "t.sqlite")
    resources = [{"resource": "https://post-sweep.example/pay",
                 "seller_wallet": "0x" + "bb" * 20, "network": "base"}]
    n = I.probe_endpoints_full(s, "2026-08-13", resources)
    assert n == 1
    row = s.db.execute(
        "SELECT valid_402 FROM endpoint_liveness WHERE endpoint_url=?",
        ("https://post-sweep.example/pay",)).fetchone()
    assert row[0]
    offer = s.db.execute(
        "SELECT payto FROM live_offers WHERE endpoint_url=?",
        ("https://post-sweep.example/pay",)).fetchone()
    assert offer[0] == "0xsweep"
    s.close()


def test_probe_endpoints_full_get_fast_path_via_shared_session(tmp_path, monkeypatch):
    """Per-endpoint sweep: GET-answering endpoints never trigger the POST
    fallback (fast path, one request per endpoint) — exercised through the
    real _FakeSession used by the rest of this file's per-endpoint tests."""
    monkeypatch.setattr(I.requests, "Session", _FakeSession)
    s = Store(tmp_path / "t.sqlite")
    resources = _resources_for_hosts({"fast.example": 4})
    n = I.probe_endpoints_full(s, "2026-08-13", resources)
    assert n == 4
    assert all(sess.post_calls == 0 for sess in _FakeSession.created)
    s.close()

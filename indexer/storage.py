"""Storage for the x402 settlement index — SQLite (default) or Postgres.

Two properties protect a dataset that accretes for months, identical on both
backends:

1. IDEMPOTENCY. `settlements` is keyed on (tx_hash, log_index) with an
   insert-or-ignore, so re-processing any block range — after a crash, a retry,
   a reorg re-scan — can never double-count.

2. VISIBLE GAPS. `indexed_ranges` records only ranges we *confirmed complete and
   committed*. Coverage/holes are derived from it, so a missed or failed run is
   a detectable gap, not silent missing data. Nothing is ever assumed indexed.

Schema is versioned in `meta`; a mismatch refuses to run rather than mixing
incompatible rows.

Backends:
- SQLite (default) — the local + test + zero-infra backend. `amount` is INTEGER
  (fine for USDC and realistic payments; a non-USDC amount >= 2^63 fails loud).
- Postgres — set `X402_DB_URL` (or call `open_store(url=...)`). Client-server for
  a serving layer, `amount` is NUMERIC(78,0) (uint256-safe), block/slot columns
  are BIGINT. Same logic + guarantees.

Use `open_store(sqlite_path, url=None)` to pick the backend from the environment;
`Store(path)` still constructs a SQLite store directly (back-compat).
"""

from __future__ import annotations

import json
import os
import sqlite3
from decimal import Decimal
from pathlib import Path

SCHEMA_VERSION = 1


class _StoreBase:
    """Shared SQL logic + guarantees. Subclasses set the connection and the
    dialect knobs (placeholder, amount/int types, upsert syntax)."""

    PH = "?"                 # parameter placeholder
    AMOUNT = "INTEGER"       # settlements.amount column type
    BIGINT = "INTEGER"       # block/slot/timestamp column type
    BOOL = "INTEGER"         # boolean column type (instrumentation flags)

    db: object               # set by subclass __init__ before _init_schema()

    # --- dialect helpers ---------------------------------------------------
    def q(self, sql: str) -> str:
        """Translate `?` placeholders to the backend's style."""
        return sql if self.PH == "?" else sql.replace("?", "%s")

    def _settlement_insert_sql(self, with_facilitator: bool) -> str:
        cols = ("tx_hash,log_index,chain,token,payer,seller,amount,"
                "block_number,block_timestamp")
        n = 9
        if with_facilitator:
            cols += ",facilitator"
            n += 1
        ph = ",".join(["?"] * n)
        if self.PH == "?":   # sqlite
            return f"INSERT OR IGNORE INTO settlements ({cols}) VALUES ({ph})"
        return self.q(f"INSERT INTO settlements ({cols}) VALUES ({ph}) "
                      f"ON CONFLICT (tx_hash,log_index) DO NOTHING")

    def _range_upsert_sql(self) -> str:
        if self.PH == "?":
            return ("INSERT OR REPLACE INTO indexed_ranges "
                    "(chain,from_block,to_block,n_settlements,indexed_at) "
                    "VALUES (?,?,?,?,?)")
        return ("INSERT INTO indexed_ranges "
                "(chain,from_block,to_block,n_settlements,indexed_at) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (chain,from_block,to_block) DO UPDATE SET "
                "n_settlements=excluded.n_settlements, indexed_at=excluded.indexed_at")

    def volume_agg(self, col: str = "amount") -> str:
        # Overflow-safe SUM for snapshot/stats use. SQLite total() returns REAL
        # (safe for 18-decimal token sums); Postgres NUMERIC SUM is exact and
        # can't overflow. `total()` does not exist in Postgres, so pick per backend.
        return f"total({col})" if self.PH == "?" else f"SUM({col})"

    def _vol_sql(self) -> str:
        return f"SELECT COALESCE({self.volume_agg()},0) FROM settlements WHERE chain=?"

    @staticmethod
    def _norm_vol(vol):
        if vol is None:
            return 0
        if isinstance(vol, Decimal):   # Postgres NUMERIC — exact
            return int(vol)
        if isinstance(vol, float):     # SQLite total() — int when it fits exactly
            return int(vol) if abs(vol) < 2**53 and vol == int(vol) else vol
        return vol

    # --- schema ------------------------------------------------------------
    def _schema_statements(self) -> list[str]:
        return [
            f"""CREATE TABLE IF NOT EXISTS settlements (
                  tx_hash        TEXT NOT NULL,
                  log_index      {self.BIGINT} NOT NULL,
                  chain          TEXT NOT NULL,
                  token          TEXT NOT NULL,
                  payer          TEXT NOT NULL,
                  seller         TEXT NOT NULL,
                  amount         {self.AMOUNT} NOT NULL,
                  block_number   {self.BIGINT} NOT NULL,
                  block_timestamp {self.BIGINT},
                  facilitator    TEXT,
                  PRIMARY KEY (tx_hash, log_index)
                )""",
            "CREATE INDEX IF NOT EXISTS ix_settlements_seller ON settlements(seller)",
            "CREATE INDEX IF NOT EXISTS ix_settlements_block ON settlements(block_number)",
            "CREATE INDEX IF NOT EXISTS ix_settlements_ts ON settlements(block_timestamp)",
            # x402-scoping filters `WHERE chain=? AND facilitator IN (...)`; without
            # this the nightly scoped readout full-scans the settlements table.
            "CREATE INDEX IF NOT EXISTS ix_settlements_chain_facilitator "
            "ON settlements(chain, facilitator)",
            # enrichment scans `WHERE chain=? AND facilitator IS NULL` to find the
            # un-enriched backlog — same index serves it.
            # COUNT(DISTINCT seller) per chain (and scoped) is the dashboard's
            # heaviest read; these ordered indexes let it run as an index-only
            # scan instead of a full heap scan, so the live stats don't time out.
            "CREATE INDEX IF NOT EXISTS ix_settlements_chain_seller "
            "ON settlements(chain, seller)",
            "CREATE INDEX IF NOT EXISTS ix_settlements_chain_fac_seller "
            "ON settlements(chain, facilitator, seller)",
            f"""CREATE TABLE IF NOT EXISTS indexed_ranges (
                  chain      TEXT NOT NULL,
                  from_block {self.BIGINT} NOT NULL,
                  to_block   {self.BIGINT} NOT NULL,
                  n_settlements {self.BIGINT} NOT NULL,
                  indexed_at TEXT NOT NULL,
                  PRIMARY KEY (chain, from_block, to_block)
                )""",
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            f"""CREATE TABLE IF NOT EXISTS solana_cursors (
                  relayer        TEXT PRIMARY KEY,
                  last_signature TEXT,
                  last_slot      {self.BIGINT},
                  backfilled_to_start {self.BIGINT} NOT NULL DEFAULT 0,
                  updated_at     TEXT
                )""",
            # Dark-matter canary: daily per-chain per-mechanism ratio of what the
            # indexer ATTRIBUTES to x402 vs the TOTAL on-chain signal (EIP-3009
            # AuthorizationUsed / Permit2 Settled) in the same measured window. A
            # drop is the early warning that a new facilitator/contract launched
            # and our attribution is stale.
            f"""CREATE TABLE IF NOT EXISTS coverage_ratio (
                  measured_date TEXT NOT NULL,
                  chain         TEXT NOT NULL,
                  mechanism     TEXT NOT NULL,
                  window_from   {self.BIGINT} NOT NULL,
                  window_to     {self.BIGINT} NOT NULL,
                  total_events  {self.BIGINT} NOT NULL,
                  attributed    {self.BIGINT} NOT NULL,
                  ratio         REAL,
                  attributed_volume {self.AMOUNT},
                  measured_at   TEXT NOT NULL,
                  PRIMARY KEY (measured_date, chain, mechanism)
                )""",
            # --- passive instrumentation (observe-only; separate from the
            # settlement ledger, joined by measured_date). A failure in any of
            # these never touches settlements/indexed_ranges. Each row is
            # interpretable via daily_fingerprint (same coverage fingerprint as
            # the day's snapshot), joined on measured_date. ------------------
            """CREATE TABLE IF NOT EXISTS daily_fingerprint (
                  measured_date TEXT PRIMARY KEY,
                  fingerprint   TEXT NOT NULL,
                  created_at    TEXT NOT NULL
                )""",
            f"""CREATE TABLE IF NOT EXISTS endpoint_liveness (
                  measured_date TEXT NOT NULL,
                  endpoint_url  TEXT NOT NULL,
                  seller_wallet TEXT,
                  network       TEXT,
                  http_status   {self.BIGINT},
                  latency_ms    {self.BIGINT},
                  valid_402     {self.BOOL},
                  content_type  TEXT,
                  raw_headers   TEXT,
                  error         TEXT,
                  measured_at   TEXT NOT NULL,
                  PRIMARY KEY (measured_date, endpoint_url)
                )""",
            # Live 402 offers observed by the liveness probe: the payTo the
            # endpoint ACTUALLY asks for right now, vs bazaar_census which is
            # what the catalog CLAIMS. The diff between the two is the free
            # public payTo-mismatch feed (indexer/payto_watch.py).
            """CREATE TABLE IF NOT EXISTS live_offers (
                  measured_date TEXT NOT NULL,
                  endpoint_url  TEXT NOT NULL,
                  payto         TEXT NOT NULL,
                  network       TEXT,
                  asset         TEXT,
                  measured_at   TEXT NOT NULL,
                  PRIMARY KEY (measured_date, endpoint_url, payto)
                )""",
            f"""CREATE TABLE IF NOT EXISTS facilitator_health (
                  measured_date  TEXT NOT NULL,
                  chain          TEXT NOT NULL,
                  facilitator    TEXT NOT NULL,
                  settlement_count {self.BIGINT} NOT NULL,
                  volume         {self.AMOUNT},
                  volume_share   REAL,
                  revert_count   {self.BIGINT},
                  median_latency_ms {self.BIGINT},
                  derivable_note TEXT,
                  measured_at    TEXT NOT NULL,
                  PRIMARY KEY (measured_date, chain, facilitator)
                )""",
            f"""CREATE TABLE IF NOT EXISTS hourly_settlements (
                  measured_date TEXT NOT NULL,
                  hour          {self.BIGINT} NOT NULL,
                  chain         TEXT NOT NULL,
                  settlement_count {self.BIGINT} NOT NULL,
                  volume        {self.AMOUNT},
                  PRIMARY KEY (measured_date, hour, chain)
                )""",
            f"""CREATE TABLE IF NOT EXISTS domain_intel (
                  measured_date TEXT NOT NULL,
                  domain        TEXT NOT NULL,
                  whois_created TEXT,
                  cert_issuer   TEXT,
                  cert_age_days {self.BIGINT},
                  dns_fingerprint TEXT,
                  changed_since_last {self.BOOL},
                  error         TEXT,
                  measured_at   TEXT NOT NULL,
                  PRIMARY KEY (measured_date, domain)
                )""",
            # Full nightly Bazaar census — one compact row per listed resource.
            # Doubles as the PRICE BOOK (asked price per resource per day; the
            # price index of the agentic economy) and the churn record
            # (arrivals/departures via date-over-date diff).
            """CREATE TABLE IF NOT EXISTS bazaar_census (
                  measured_date TEXT NOT NULL,
                  resource      TEXT NOT NULL,
                  domain        TEXT,
                  network       TEXT,
                  asset         TEXT,
                  amount_raw    TEXT,
                  amount_usd    REAL,
                  seller_wallet TEXT,
                  service       TEXT,
                  PRIMARY KEY (measured_date, resource)
                )""",
            # Cross-tracker reconciliation — other trackers' headline numbers
            # recorded daily next to ours; divergence is report material, and
            # "unreachable / no API" is itself a recorded fact.
            """CREATE TABLE IF NOT EXISTS tracker_recon (
                  measured_date TEXT NOT NULL,
                  tracker       TEXT NOT NULL,
                  metric        TEXT NOT NULL,
                  value         REAL,
                  note          TEXT,
                  measured_at   TEXT NOT NULL,
                  PRIMARY KEY (measured_date, tracker, metric)
                )""",
            # Endpoint reliability grades — per-origin nightly score from the
            # trailing liveness window (uptime / valid-402 / latency).
            f"""CREATE TABLE IF NOT EXISTS endpoint_scores (
                  measured_date TEXT NOT NULL,
                  origin        TEXT NOT NULL,
                  days_observed {self.BIGINT},
                  probes        {self.BIGINT},
                  uptime_rate   REAL,
                  valid_402_rate REAL,
                  median_latency_ms {self.BIGINT},
                  score         REAL,
                  grade         TEXT,
                  provisional   {self.BOOL},
                  score_version {self.BIGINT},
                  measured_at   TEXT NOT NULL,
                  PRIMARY KEY (measured_date, origin)
                )""",
            # Payment-grounded seller trust scores — computed deterministically
            # from the day's immutable snapshot (publicly hash-anchored), so
            # every score is externally verifiable. Component breakdown kept
            # as JSON for transparency.
            f"""CREATE TABLE IF NOT EXISTS seller_scores (
                  measured_date TEXT NOT NULL,
                  chain         TEXT NOT NULL,
                  seller        TEXT NOT NULL,
                  score         REAL,
                  grade         TEXT,
                  provisional   {self.BOOL},
                  snapshot_date TEXT,
                  tx_count      {self.BIGINT},
                  unique_payers {self.BIGINT},
                  self_pay_ratio REAL,
                  listed        {self.BOOL},
                  components    TEXT,
                  score_version {self.BIGINT},
                  measured_at   TEXT NOT NULL,
                  PRIMARY KEY (measured_date, chain, seller)
                )""",
            "CREATE INDEX IF NOT EXISTS ix_seller_scores_seller "
            "ON seller_scores(seller, measured_date)",
            # PAYER (agent) trust scores — the buy side. `status` is a
            # first-class column, not an error: most agents have too little
            # history to score, and 'no_history' must never be stored as a
            # low score. score is NULL in that case, on purpose.
            f"""CREATE TABLE IF NOT EXISTS payer_scores (
                  measured_date TEXT NOT NULL,
                  chain         TEXT NOT NULL,
                  payer         TEXT NOT NULL,
                  score         REAL,
                  grade         TEXT,
                  status        TEXT,
                  provisional   {self.BOOL},
                  snapshot_date TEXT,
                  tx_count      {self.BIGINT},
                  unique_sellers {self.BIGINT},
                  self_pay_ratio REAL,
                  exclusive_repeat {self.BOOL},
                  components    TEXT,
                  score_version {self.BIGINT},
                  measured_at   TEXT NOT NULL,
                  PRIMARY KEY (measured_date, chain, payer)
                )""",
            "CREATE INDEX IF NOT EXISTS ix_payer_scores_payer "
            "ON payer_scores(payer, measured_date)",
            # Wash/self-dealing signals per top seller (Artemis-style filters:
            # self-pay ratio, on-chain funded-payer lookback, reciprocal pairs).
            f"""CREATE TABLE IF NOT EXISTS wash_signals (
                  measured_date TEXT NOT NULL,
                  chain         TEXT NOT NULL,
                  seller        TEXT NOT NULL,
                  tx_count      {self.BIGINT},
                  unique_payers {self.BIGINT},
                  self_pay_ratio REAL,
                  funded_payers {self.BIGINT},
                  funded_payer_ratio REAL,
                  lookback_ok   {self.BOOL},
                  reciprocal    {self.BOOL},
                  flagged       {self.BOOL},
                  thresholds    TEXT,
                  measured_at   TEXT NOT NULL,
                  PRIMARY KEY (measured_date, chain, seller)
                )""",
            # Wash v3 cycle evidence (indexer/wash_cycles.py): one row per
            # enumerated cycle (length ≤ k) + one 'flagged' row per chain
            # carrying the exact SCC∩balanced set that caps grades and is
            # excluded from clean metrics. Detector params stored per row so
            # every published flag is reproducible against the exact detector.
            f"""CREATE TABLE IF NOT EXISTS wash_cycles (
                  measured_date TEXT NOT NULL,
                  chain         TEXT NOT NULL,
                  cycle_id      TEXT NOT NULL,
                  length        {self.BIGINT},
                  wallets       TEXT,
                  float_usd     REAL,
                  minted_usd    REAL,
                  params        TEXT,
                  measured_at   TEXT NOT NULL,
                  PRIMARY KEY (measured_date, chain, cycle_id)
                )""",
            # The publishable CLEAN numbers: scoped totals minus flagged-seller
            # and self-pay settlements, with the coverage caveat stored inline.
            f"""CREATE TABLE IF NOT EXISTS clean_metrics (
                  measured_date TEXT NOT NULL,
                  chain         TEXT NOT NULL,
                  scoped_settlements {self.BIGINT},
                  scoped_volume_usd  REAL,
                  flagged_sellers    {self.BIGINT},
                  excluded_settlements {self.BIGINT},
                  clean_settlements  {self.BIGINT},
                  clean_volume_usd   REAL,
                  coverage_note      TEXT,
                  measured_at        TEXT NOT NULL,
                  PRIMARY KEY (measured_date, chain)
                )""",
            # Unit economics — sampled median settlement gas fee vs median
            # payment per chain per day ("$0.0001 to move $0.03").
            f"""CREATE TABLE IF NOT EXISTS unit_economics (
                  measured_date TEXT NOT NULL,
                  chain         TEXT NOT NULL,
                  sample_n      {self.BIGINT},
                  median_fee_usd REAL,
                  median_payment_usd REAL,
                  fee_over_payment REAL,
                  native_price_usd REAL,
                  error         TEXT,
                  measured_at   TEXT NOT NULL,
                  PRIMARY KEY (measured_date, chain)
                )""",
            # Per-payer on-chain account kind (indexer/enrich_payer_kinds.py):
            # EOA vs EIP-7702-delegated EOA vs contract (ERC-1271 smart
            # account), keyed by (chain, payer) rather than per-day like the
            # instrumentation tables above — a payer's kind is a property of
            # the ADDRESS, not of a measurement day, though it CAN change
            # (7702 lets a plain EOA gain delegation code later), which is why
            # record_payer_kind upserts rather than inserting a fresh row per
            # check. code_prefix is the first 8 bytes of the code (audit
            # trail), NULL for a plain EOA. Not scoped to Solana — see that
            # module's docstring for why.
            """CREATE TABLE IF NOT EXISTS payer_kinds (
                  chain       TEXT NOT NULL,
                  payer       TEXT NOT NULL,
                  kind        TEXT NOT NULL,
                  code_prefix TEXT,
                  checked_at  TEXT NOT NULL,
                  PRIMARY KEY (chain, payer)
                )""",
        ]

    def _init_schema(self) -> None:
        cur = self.db.cursor()
        for stmt in self._schema_statements():
            cur.execute(stmt)
        row = cur.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            cur.execute(self.q("INSERT INTO meta(key,value) VALUES('schema_version',?)"),
                        (str(SCHEMA_VERSION),))
        elif int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError(
                f"DB schema v{row[0]} != code v{SCHEMA_VERSION}; refusing to run")
        self.db.commit()

    # --- meta --------------------------------------------------------------
    def set_meta(self, key: str, value) -> None:
        self.db.execute(
            self.q("INSERT INTO meta(key,value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value"),
            (key, json.dumps(value) if not isinstance(value, str) else value))
        self.db.commit()

    def get_meta(self, key: str):
        row = self.db.execute(
            self.q("SELECT value FROM meta WHERE key=?"), (key,)).fetchone()
        return row[0] if row else None

    # --- settlements + ranges ---------------------------------------------
    def commit_range(self, chain: str, from_block: int, to_block: int,
                     rows: list[dict], indexed_at: str) -> int:
        """Insert settlements and record the range complete IN ONE TXN, so a
        crash mid-range leaves the range absent from indexed_ranges (a visible
        gap to re-do) rather than half-recorded-as-done."""
        cur = self.db.cursor()
        ins = self._settlement_insert_sql(with_facilitator=False)
        try:
            for r in rows:
                cur.execute(ins, (r["tx_hash"], r["log_index"], r["chain"],
                                  r["token"], r["payer"], r["seller"], r["amount"],
                                  r["block_number"], r["block_timestamp"]))
            cur.execute(self._range_upsert_sql(),
                        (chain, from_block, to_block, len(rows), indexed_at))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return len(rows)

    def covered_frontier(self, chain: str, start_block: int) -> int:
        """Highest block B such that [start_block, B] is fully covered by
        contiguous indexed_ranges. Returns start_block-1 if nothing covered.
        This is the resume point; it will not jump over a hole."""
        ranges = self.db.execute(
            self.q("SELECT from_block,to_block FROM indexed_ranges WHERE chain=? "
                   "ORDER BY from_block"), (chain,)).fetchall()
        frontier = start_block - 1
        for fb, tb in ranges:
            if fb <= frontier + 1:
                frontier = max(frontier, tb)
            elif fb > frontier + 1:
                break  # a gap: stop advancing
        return frontier

    def find_gaps(self, chain: str, start_block: int, end_block: int) -> list[tuple]:
        """Return uncovered [gap_from, gap_to] sub-ranges within
        [start_block, end_block]. Empty list == fully covered."""
        ranges = self.db.execute(
            self.q("SELECT from_block,to_block FROM indexed_ranges WHERE chain=? "
                   "AND to_block>=? AND from_block<=? ORDER BY from_block"),
            (chain, start_block, end_block)).fetchall()
        gaps = []
        cursor = start_block
        for fb, tb in ranges:
            if fb > cursor:
                gaps.append((cursor, min(fb - 1, end_block)))
            cursor = max(cursor, tb + 1)
            if cursor > end_block:
                break
        if cursor <= end_block:
            gaps.append((cursor, end_block))
        return gaps

    # --- solana cursors ----------------------------------------------------
    def get_solana_cursor(self, relayer: str):
        row = self.db.execute(
            self.q("SELECT last_signature, last_slot FROM solana_cursors "
                   "WHERE relayer=?"), (relayer,)).fetchone()
        return tuple(row) if row else (None, None)

    def set_solana_cursor(self, relayer: str, signature: str, slot: int,
                          indexed_at: str) -> None:
        self.db.execute(
            self.q("INSERT INTO solana_cursors(relayer,last_signature,last_slot,updated_at) "
                   "VALUES(?,?,?,?) ON CONFLICT(relayer) DO UPDATE SET "
                   "last_signature=excluded.last_signature, last_slot=excluded.last_slot, "
                   "updated_at=excluded.updated_at"),
            (relayer, signature, slot, indexed_at))
        self.db.commit()

    def commit_solana_batch(self, rows: list[dict]) -> int:
        """Idempotent insert of Solana settlement rows (same table/PK as Base:
        tx_hash=signature, log_index=instruction index). Returns rows inserted."""
        cur = self.db.cursor()
        ins = self._settlement_insert_sql(with_facilitator=True)
        inserted = 0
        try:
            for r in rows:
                cur.execute(ins, (r["tx_hash"], r["log_index"], r["chain"],
                                  r["token"], r["payer"], r["seller"], r["amount"],
                                  r["block_number"], r["block_timestamp"],
                                  r.get("facilitator")))
                inserted += cur.rowcount  # 1 inserted, 0 ignored (dupe) — both backends
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return inserted

    # --- stats -------------------------------------------------------------
    def stats(self, chain: str) -> dict:
        c = self.db.cursor()
        n = c.execute(self.q("SELECT COUNT(*) FROM settlements WHERE chain=?"),
                      (chain,)).fetchone()[0]
        sellers = c.execute(
            self.q("SELECT COUNT(DISTINCT seller) FROM settlements WHERE chain=?"),
            (chain,)).fetchone()[0]
        vol = self._norm_vol(
            c.execute(self.q(self._vol_sql()), (chain,)).fetchone()[0])
        rng = c.execute(
            self.q("SELECT MIN(block_number),MAX(block_number) FROM settlements "
                   "WHERE chain=?"), (chain,)).fetchone()
        return {"settlements": n, "unique_sellers": sellers,
                "volume_base_units": vol, "min_block": rng[0], "max_block": rng[1]}

    def scoped_stats(self, chain: str, relayers) -> dict | None:
        """Stats for the x402-SCOPED subset: settlements whose relayer (the
        `facilitator` column = tx.from) is a known x402 facilitator relayer. This
        is the true x402 metric; `stats()` is the EIP-3009 superset. Returns None
        if the chain has no facilitator registry (not scopable). Rows whose
        relayer isn't yet enriched (facilitator NULL) are excluded — call
        relayer_enriched_fraction() to report coverage alongside."""
        relayers = list(relayers or [])
        if not relayers:
            return None
        inph = ",".join(["?"] * len(relayers))
        c = self.db.cursor()
        n, sellers, payers = c.execute(self.q(
            f"SELECT COUNT(*), COUNT(DISTINCT seller), COUNT(DISTINCT payer) "
            f"FROM settlements WHERE chain=? AND facilitator IN ({inph})"),
            (chain, *relayers)).fetchone()
        vol = self._norm_vol(c.execute(self.q(
            f"SELECT COALESCE({self.volume_agg()},0) FROM settlements "
            f"WHERE chain=? AND facilitator IN ({inph})"),
            (chain, *relayers)).fetchone()[0])
        return {"settlements": n, "unique_sellers": sellers,
                "unique_payers": payers,
                "volume_base_units": vol, "relayer_set_size": len(relayers)}

    def relayer_enriched_fraction(self, chain: str) -> dict:
        """Coverage of relayer enrichment for a chain: how many settlement rows
        have their relayer (facilitator) populated vs total. Scoped stats are
        only complete once this is ~1.0."""
        c = self.db.cursor()
        tot = c.execute(self.q("SELECT COUNT(*) FROM settlements WHERE chain=?"),
                        (chain,)).fetchone()[0]
        enr = c.execute(self.q("SELECT COUNT(*) FROM settlements WHERE chain=? "
                               "AND facilitator IS NOT NULL"), (chain,)).fetchone()[0]
        return {"total": tot, "enriched": enr,
                "fraction": (enr / tot) if tot else 1.0}

    def chain_min_block(self, chain: str):
        """Lowest block/slot indexed for `chain`, or None. Used as the
        coverage-start floor stamp (distinguishes 'not indexed' from 'no data')."""
        row = self.db.execute(
            self.q("SELECT MIN(block_number) FROM settlements WHERE chain=?"),
            (chain,)).fetchone()
        return row[0] if row else None

    # --- coverage canary ---------------------------------------------------
    def record_coverage_ratio(self, measured_date: str, chain: str,
                              mechanism: str, window: tuple, total: int,
                              attributed: int, volume, measured_at: str) -> float:
        """Upsert a daily per-chain per-mechanism coverage ratio; returns ratio."""
        ratio = (attributed / total) if total else None
        self.db.execute(
            self.q("INSERT INTO coverage_ratio(measured_date,chain,mechanism,"
                   "window_from,window_to,total_events,attributed,ratio,"
                   "attributed_volume,measured_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(measured_date,chain,mechanism) DO UPDATE SET "
                   "window_from=excluded.window_from,window_to=excluded.window_to,"
                   "total_events=excluded.total_events,attributed=excluded.attributed,"
                   "ratio=excluded.ratio,attributed_volume=excluded.attributed_volume,"
                   "measured_at=excluded.measured_at"),
            (measured_date, chain, mechanism, window[0], window[1],
             total, attributed, ratio, volume, measured_at))
        self.db.commit()
        return ratio if ratio is not None else float("nan")

    # --- passive instrumentation recorders (observe-only) ------------------
    def set_daily_fingerprint(self, measured_date: str, fingerprint: dict,
                              created_at: str) -> None:
        self.db.execute(
            self.q("INSERT INTO daily_fingerprint(measured_date,fingerprint,"
                   "created_at) VALUES(?,?,?) ON CONFLICT(measured_date) DO "
                   "UPDATE SET fingerprint=excluded.fingerprint"),
            (measured_date, json.dumps(fingerprint), created_at))
        self.db.commit()

    def record_live_offer(self, measured_date: str, endpoint_url: str,
                          r: dict, measured_at: str) -> None:
        """One probe-observed 402 offer (payTo the endpoint asks for LIVE)."""
        self.db.execute(
            self.q("INSERT INTO live_offers(measured_date,endpoint_url,payto,"
                   "network,asset,measured_at) VALUES(?,?,?,?,?,?) "
                   "ON CONFLICT(measured_date,endpoint_url,payto) DO UPDATE SET "
                   "network=excluded.network,asset=excluded.asset,"
                   "measured_at=excluded.measured_at"),
            (measured_date, endpoint_url, r["payto"], r.get("network"),
             r.get("asset"), measured_at))

    def record_endpoint_liveness(self, measured_date: str, r: dict,
                                 measured_at: str) -> None:
        self.db.execute(
            self.q("INSERT INTO endpoint_liveness(measured_date,endpoint_url,"
                   "seller_wallet,network,http_status,latency_ms,valid_402,"
                   "content_type,raw_headers,error,measured_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(measured_date,"
                   "endpoint_url) DO UPDATE SET http_status=excluded.http_status,"
                   "latency_ms=excluded.latency_ms,valid_402=excluded.valid_402,"
                   "content_type=excluded.content_type,raw_headers=excluded.raw_headers,"
                   "error=excluded.error,measured_at=excluded.measured_at"),
            (measured_date, r["endpoint_url"], r.get("seller_wallet"),
             r.get("network"), r.get("http_status"), r.get("latency_ms"),
             r.get("valid_402"), r.get("content_type"),
             json.dumps(r.get("raw_headers")) if r.get("raw_headers") is not None else None,
             r.get("error"), measured_at))
        self.db.commit()

    def record_facilitator_health(self, measured_date: str, chain: str,
                                  facilitator: str, count: int, volume,
                                  volume_share, revert_count, median_latency,
                                  note: str, measured_at: str) -> None:
        self.db.execute(
            self.q("INSERT INTO facilitator_health(measured_date,chain,facilitator,"
                   "settlement_count,volume,volume_share,revert_count,"
                   "median_latency_ms,derivable_note,measured_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(measured_date,chain,"
                   "facilitator) DO UPDATE SET settlement_count=excluded.settlement_count,"
                   "volume=excluded.volume,volume_share=excluded.volume_share,"
                   "revert_count=excluded.revert_count,median_latency_ms=excluded.median_latency_ms,"
                   "derivable_note=excluded.derivable_note,measured_at=excluded.measured_at"),
            (measured_date, chain, facilitator, count, volume, volume_share,
             revert_count, median_latency, note, measured_at))
        self.db.commit()

    def record_hourly_settlement(self, measured_date: str, hour: int,
                                 chain: str, count: int, volume) -> None:
        self.db.execute(
            self.q("INSERT INTO hourly_settlements(measured_date,hour,chain,"
                   "settlement_count,volume) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(measured_date,hour,chain) DO UPDATE SET "
                   "settlement_count=excluded.settlement_count,volume=excluded.volume"),
            (measured_date, hour, chain, count, volume))
        self.db.commit()

    def record_domain_intel(self, measured_date: str, domain: str, r: dict,
                            measured_at: str) -> None:
        self.db.execute(
            self.q("INSERT INTO domain_intel(measured_date,domain,whois_created,"
                   "cert_issuer,cert_age_days,dns_fingerprint,changed_since_last,"
                   "error,measured_at) VALUES(?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(measured_date,domain) DO UPDATE SET "
                   "whois_created=excluded.whois_created,cert_issuer=excluded.cert_issuer,"
                   "cert_age_days=excluded.cert_age_days,dns_fingerprint=excluded.dns_fingerprint,"
                   "changed_since_last=excluded.changed_since_last,error=excluded.error,"
                   "measured_at=excluded.measured_at"),
            (measured_date, domain, r.get("whois_created"), r.get("cert_issuer"),
             r.get("cert_age_days"), r.get("dns_fingerprint"),
             r.get("changed_since_last"), r.get("error"), measured_at))
        self.db.commit()

    def record_bazaar_census_rows(self, measured_date: str, rows: list[dict]) -> int:
        """Batch-insert the day's full Bazaar census (one row per resource).
        Idempotent: re-runs upsert. Single commit — ~25k rows/night."""
        cur = self.db.cursor()
        for r in rows:
            cur.execute(
                self.q("INSERT INTO bazaar_census(measured_date,resource,domain,"
                       "network,asset,amount_raw,amount_usd,seller_wallet,service) "
                       "VALUES(?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(measured_date,resource) DO UPDATE SET "
                       "network=excluded.network,asset=excluded.asset,"
                       "amount_raw=excluded.amount_raw,amount_usd=excluded.amount_usd,"
                       "seller_wallet=excluded.seller_wallet,service=excluded.service"),
                (measured_date, r["resource"], r.get("domain"), r.get("network"),
                 r.get("asset"), r.get("amount_raw"), r.get("amount_usd"),
                 r.get("seller_wallet"), r.get("service")))
        self.db.commit()
        return len(rows)

    def record_tracker_metric(self, measured_date: str, tracker: str,
                              metric: str, value, note, measured_at: str) -> None:
        self.db.execute(
            self.q("INSERT INTO tracker_recon(measured_date,tracker,metric,value,"
                   "note,measured_at) VALUES(?,?,?,?,?,?) "
                   "ON CONFLICT(measured_date,tracker,metric) DO UPDATE SET "
                   "value=excluded.value,note=excluded.note,"
                   "measured_at=excluded.measured_at"),
            (measured_date, tracker, metric, value, note, measured_at))
        self.db.commit()

    def record_unit_economics(self, measured_date: str, chain: str, r: dict,
                              measured_at: str) -> None:
        self.db.execute(
            self.q("INSERT INTO unit_economics(measured_date,chain,sample_n,"
                   "median_fee_usd,median_payment_usd,fee_over_payment,"
                   "native_price_usd,error,measured_at) VALUES(?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(measured_date,chain) DO UPDATE SET "
                   "sample_n=excluded.sample_n,median_fee_usd=excluded.median_fee_usd,"
                   "median_payment_usd=excluded.median_payment_usd,"
                   "fee_over_payment=excluded.fee_over_payment,"
                   "native_price_usd=excluded.native_price_usd,error=excluded.error,"
                   "measured_at=excluded.measured_at"),
            (measured_date, chain, r.get("sample_n"), r.get("median_fee_usd"),
             r.get("median_payment_usd"), r.get("fee_over_payment"),
             r.get("native_price_usd"), r.get("error"), measured_at))
        self.db.commit()

    def record_payer_kind(self, chain: str, payer: str, kind: str,
                          code_prefix: str | None, checked_at: str) -> None:
        """Upsert one payer's on-chain account kind. NB: does NOT commit — the
        enrichment run batches one commit per RPC batch of addresses (see
        enrich_payer_kinds.py), the same shape as enrich_relayers.py's
        per-block-chunk commit, not per-row. ON CONFLICT DO UPDATE (not
        DO NOTHING): a kind can change over time — EIP-7702 lets a
        previously-plain EOA gain delegation code later — so a re-check must
        overwrite the stored kind, not be silently ignored."""
        self.db.execute(
            self.q("INSERT INTO payer_kinds(chain,payer,kind,code_prefix,"
                   "checked_at) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(chain,payer) DO UPDATE SET "
                   "kind=excluded.kind,code_prefix=excluded.code_prefix,"
                   "checked_at=excluded.checked_at"),
            (chain, payer, kind, code_prefix, checked_at))

    def payer_kind_counts(self, chain: str) -> dict:
        """{kind: n} for one chain's checked payers so far — the dashboard
        payer_kinds blob's input (see build_dashboard_metrics)."""
        rows = self.db.execute(
            self.q("SELECT kind, COUNT(*) FROM payer_kinds WHERE chain=? "
                   "GROUP BY kind"), (chain,)).fetchall()
        return {k: int(n) for k, n in rows}

    def record_endpoint_score(self, measured_date: str, origin: str, r: dict,
                              measured_at: str) -> None:
        """NB: does NOT commit — callers batch a whole scoring run per commit."""
        self.db.execute(
            self.q("INSERT INTO endpoint_scores(measured_date,origin,days_observed,"
                   "probes,uptime_rate,valid_402_rate,median_latency_ms,score,"
                   "grade,provisional,score_version,measured_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(measured_date,origin) DO UPDATE SET "
                   "days_observed=excluded.days_observed,probes=excluded.probes,"
                   "uptime_rate=excluded.uptime_rate,valid_402_rate=excluded.valid_402_rate,"
                   "median_latency_ms=excluded.median_latency_ms,score=excluded.score,"
                   "grade=excluded.grade,provisional=excluded.provisional,"
                   "score_version=excluded.score_version,measured_at=excluded.measured_at"),
            (measured_date, origin, r.get("days_observed"), r.get("probes"),
             r.get("uptime_rate"), r.get("valid_402_rate"),
             r.get("median_latency_ms"), r.get("score"), r.get("grade"),
             r.get("provisional"), r.get("score_version"), measured_at))

    def record_seller_score(self, measured_date: str, chain: str, seller: str,
                            r: dict, measured_at: str) -> None:
        """NB: does NOT commit — ~15k rows/night are batched into one commit."""
        self.db.execute(
            self.q("INSERT INTO seller_scores(measured_date,chain,seller,score,"
                   "grade,provisional,snapshot_date,tx_count,unique_payers,"
                   "self_pay_ratio,listed,components,score_version,measured_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(measured_date,chain,seller) DO UPDATE SET "
                   "score=excluded.score,grade=excluded.grade,"
                   "provisional=excluded.provisional,snapshot_date=excluded.snapshot_date,"
                   "tx_count=excluded.tx_count,unique_payers=excluded.unique_payers,"
                   "self_pay_ratio=excluded.self_pay_ratio,listed=excluded.listed,"
                   "components=excluded.components,score_version=excluded.score_version,"
                   "measured_at=excluded.measured_at"),
            (measured_date, chain, seller, r.get("score"), r.get("grade"),
             r.get("provisional"), r.get("snapshot_date"), r.get("tx_count"),
             r.get("unique_payers"), r.get("self_pay_ratio"), r.get("listed"),
             json.dumps(r.get("components") or {}), r.get("score_version"),
             measured_at))

    def record_payer_score(self, measured_date: str, chain: str, payer: str,
                           r: dict, measured_at: str) -> None:
        """NB: does NOT commit — the payer run batches into one commit."""
        self.db.execute(
            self.q("INSERT INTO payer_scores(measured_date,chain,payer,score,"
                   "grade,status,provisional,snapshot_date,tx_count,"
                   "unique_sellers,self_pay_ratio,exclusive_repeat,components,"
                   "score_version,measured_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(measured_date,chain,payer) DO UPDATE SET "
                   "score=excluded.score,grade=excluded.grade,"
                   "status=excluded.status,provisional=excluded.provisional,"
                   "snapshot_date=excluded.snapshot_date,"
                   "tx_count=excluded.tx_count,"
                   "unique_sellers=excluded.unique_sellers,"
                   "self_pay_ratio=excluded.self_pay_ratio,"
                   "exclusive_repeat=excluded.exclusive_repeat,"
                   "components=excluded.components,"
                   "score_version=excluded.score_version,"
                   "measured_at=excluded.measured_at"),
            (measured_date, chain, payer, r.get("score"), r.get("grade"),
             r.get("status"), r.get("provisional"), r.get("snapshot_date"),
             r.get("tx_count"), r.get("unique_sellers"),
             r.get("self_pay_ratio"), r.get("exclusive_repeat"),
             json.dumps(r.get("components") or {}), r.get("score_version"),
             measured_at))

    def record_wash_signal(self, measured_date: str, chain: str, seller: str,
                           r: dict, measured_at: str) -> None:
        """NB: does NOT commit — the wash run batches per chain."""
        self.db.execute(
            self.q("INSERT INTO wash_signals(measured_date,chain,seller,tx_count,"
                   "unique_payers,self_pay_ratio,funded_payers,funded_payer_ratio,"
                   "lookback_ok,reciprocal,flagged,thresholds,measured_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(measured_date,chain,seller) DO UPDATE SET "
                   "tx_count=excluded.tx_count,unique_payers=excluded.unique_payers,"
                   "self_pay_ratio=excluded.self_pay_ratio,"
                   "funded_payers=excluded.funded_payers,"
                   "funded_payer_ratio=excluded.funded_payer_ratio,"
                   "lookback_ok=excluded.lookback_ok,reciprocal=excluded.reciprocal,"
                   "flagged=excluded.flagged,thresholds=excluded.thresholds,"
                   "measured_at=excluded.measured_at"),
            (measured_date, chain, seller, r.get("tx_count"), r.get("unique_payers"),
             r.get("self_pay_ratio"), r.get("funded_payers"),
             r.get("funded_payer_ratio"), r.get("lookback_ok"), r.get("reciprocal"),
             r.get("flagged"), json.dumps(r.get("thresholds") or {}), measured_at))

    def record_wash_cycle(self, measured_date: str, chain: str, cycle_id: str,
                          r: dict, measured_at: str) -> None:
        """NB: does NOT commit — wash_cycles.record() batches per chain."""
        self.db.execute(
            self.q("INSERT INTO wash_cycles(measured_date,chain,cycle_id,"
                   "length,wallets,float_usd,minted_usd,params,measured_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(measured_date,chain,cycle_id) DO UPDATE SET "
                   "length=excluded.length,wallets=excluded.wallets,"
                   "float_usd=excluded.float_usd,minted_usd=excluded.minted_usd,"
                   "params=excluded.params,measured_at=excluded.measured_at"),
            (measured_date, chain, cycle_id, r.get("length"),
             json.dumps(r.get("wallets") or []), r.get("float_usd"),
             r.get("minted_usd"), json.dumps(r.get("params") or {}),
             measured_at))

    def record_clean_metrics(self, measured_date: str, chain: str, r: dict,
                             measured_at: str) -> None:
        self.db.execute(
            self.q("INSERT INTO clean_metrics(measured_date,chain,scoped_settlements,"
                   "scoped_volume_usd,flagged_sellers,excluded_settlements,"
                   "clean_settlements,clean_volume_usd,coverage_note,measured_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(measured_date,chain) DO UPDATE SET "
                   "scoped_settlements=excluded.scoped_settlements,"
                   "scoped_volume_usd=excluded.scoped_volume_usd,"
                   "flagged_sellers=excluded.flagged_sellers,"
                   "excluded_settlements=excluded.excluded_settlements,"
                   "clean_settlements=excluded.clean_settlements,"
                   "clean_volume_usd=excluded.clean_volume_usd,"
                   "coverage_note=excluded.coverage_note,measured_at=excluded.measured_at"),
            (measured_date, chain, r.get("scoped_settlements"),
             r.get("scoped_volume_usd"), r.get("flagged_sellers"),
             r.get("excluded_settlements"), r.get("clean_settlements"),
             r.get("clean_volume_usd"), r.get("coverage_note"), measured_at))
        self.db.commit()

    def last_domain_fingerprint(self, domain: str, before_date: str):
        """Most recent prior dns_fingerprint for a domain (for change detection)."""
        row = self.db.execute(
            self.q("SELECT dns_fingerprint FROM domain_intel WHERE domain=? "
                   "AND measured_date<? AND dns_fingerprint IS NOT NULL "
                   "ORDER BY measured_date DESC LIMIT 1"),
            (domain, before_date)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self.db.close()


class Store(_StoreBase):
    """SQLite backend (default; local + tests + zero-infra Actions)."""

    PH = "?"
    AMOUNT = "INTEGER"
    BIGINT = "INTEGER"
    BOOL = "INTEGER"

    def __init__(self, path, init_schema: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        # a concurrent writer (cron run + manual run) waits rather than erroring
        self.db.execute("PRAGMA busy_timeout=30000")
        if init_schema:
            self._init_schema()


class PostgresStore(_StoreBase):
    """Postgres backend (set X402_DB_URL). uint256-safe NUMERIC amounts; BIGINT
    block/slot columns; a real client-server DB for the serving layer."""

    PH = "%s"
    BOOL = "BOOLEAN"
    AMOUNT = "NUMERIC(78,0)"   # holds any uint256
    BIGINT = "BIGINT"

    def __init__(self, url: str, init_schema: bool = True):
        import psycopg  # lazy: SQLite-only envs (tests/Actions) need no driver
        self.url = url
        self.db = psycopg.connect(url, autocommit=False)
        if init_schema:
            self._init_schema()

    # NOTE: the probe's pg_trgm GIN indexes are deliberately NOT created here.
    # CREATE EXTENSION needs a privilege the writer role may lack, and any
    # failure in _init_schema aborts the whole nightly run. They live in
    # deploy/migrations/ instead (applied by deploy-serving.sh) so a
    # privilege/DDL problem surfaces loudly at deploy time and can never break
    # indexing. See deploy/migrations/2026-07-19-probe-trgm.sql.


def open_store(sqlite_path=None, url: str | None = None,
               init_schema: bool = True):
    """Pick the backend: Postgres if a URL is given or X402_DB_URL is set,
    else SQLite at `sqlite_path`. Serving processes connect with a SELECT-only
    role and MUST pass init_schema=False — the DDL pass needs CREATE, which a
    read-only role rightly lacks (audit LB-4)."""
    url = url or os.environ.get("X402_DB_URL")
    if url:
        return PostgresStore(url, init_schema=init_schema)
    return Store(sqlite_path, init_schema=init_schema)

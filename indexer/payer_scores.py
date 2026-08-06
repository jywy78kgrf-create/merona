"""Payment-grounded PAYER (agent) trust scores — the buy side of m.Score.

WHAT QUESTION THIS ANSWERS. `indexer/scores.py` scores sellers: "is this
endpoint worth paying?" This scores the counterparty on the other side of the
same transaction: "an agent just paid me — is it a real customer or part of a
wash structure?" With x402 the payment has already settled by the time the
seller sees the request, so this is not an authorisation decision. It is a
question about who you are actually doing business with.

THE INVERSION THIS EXISTS TO AVOID. Every agent-reputation design reaches for
payment history as the trust signal: more payments, more trustworthy. Our own
measurements say that ranking is backwards at the top end.

  - Base: the MEDIAN agent made exactly one payment; the top 1% of wallets
    account for 94% of all activity; a single wallet did 7.8% of history.
  - Base: 54% of all dollars are `payer == seller`.
  - Solana: median 2.0 payments/agent, top 1% = 91.7% of settlements, and
    36.8% of volume comes from repeat payers whose only counterparty ever is
    the one seller they pay.

So a wallet with 1,204 x402 payments is not a well-behaved power user by
default — the closest match for that profile in our data is a funded-loop
payer or an exclusive-repeat farm wallet. A history-weighted score hands its
highest rating to exactly the wallets a seller should be most suspicious of.

The fix is that COUNTERPARTY DIVERSITY, not payment count, is the core
component, and paying one seller many times is a PENALTY rather than a
qualification. A wallet paying 30 different sellers is structurally hard to
fake — it would require 30 colluding sellers. A wallet paying one seller 1,204
times requires one.

WHAT WE REFUSE TO DO: SCORE A STRANGER. Most agents have no history at all —
that is the finding, not a data gap. A reputation product that returns `78`
for a wallet it has never seen is inventing the number that matters most. This
scorer returns `no_history` / `insufficient_history` with `score: null` and
grade UNKNOWN, and reports the coverage rate so a caller knows in advance how
often that will happen. An honest UNKNOWN is the product.

AND THE SAME RULE ONE LEVEL UP: A LOW GRADE MUST MEAN "STRUCTURE DETECTED",
NEVER "WE HAVEN'T SEEN MUCH". Every positive component here (tenure, volume,
count, diversity) starts near zero for a young wallet, so a real first-week
customer paying three different sellers accumulates few points and, graded
naively, comes out F — indistinguishable from a self-dealer. That is the
original sin of reputation scoring reproduced exactly. So a grade is issued
only when the evidence supports one: either the wallet clears
PAYER_CONFIDENT_TX settlements, or a PENALTY FIRED — self-payment and
exclusive-repeat are conclusive at any size, because they are statements
about shape rather than about volume. Everything else is `thin_history`,
score null, grade UNKNOWN, with the computed value exposed as
`indicative_score` so nothing is hidden and nothing can be sorted on.

PAYER_SCORE_VERSION history:
  1 — snapshot-derived only: counterparty diversity, rail tenure, volume,
      settlement count; penalties for self-pay and exclusive-repeat
      concentration. Deliberately does NOT include the funding graph
      (was this wallet funded by the seller it pays, before it ever paid?)
      or flagged-counterparty share — both need artefacts that are per-seller
      today (`wash_signals`, `wash_flags_<chain>.csv`) and must be exported
      per-payer first. Those are the v2 components, and they are the two
      strongest signals we have, so a v1 score is a floor on suspicion:
      it can say "this looks like a farm wallet", never "this one is clean".

HONEST LIMITS, to be carried in the API response and never quietly dropped:
  - "Tenure" is RAIL tenure — first to last x402 payment we observed. It is
    not wallet age; we do not read the wallet's first-ever transaction.
  - Scores cover the WITNESSED regime (what this indexer observed live), not
    the backfilled history, because that is what the snapshot attests.
  - A high score is weak evidence. A low score is strong evidence. The
    components are built to detect structure, not to certify virtue.
"""
from __future__ import annotations

import csv
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PAYER_SCORE_VERSION = 1
ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "data" / "indexer" / "snapshots"

# Below this many settlements we decline to score at all. Two payments say
# nothing about a wallet, and a confident-looking number derived from them is
# worse than silence.
MIN_SCOREABLE_TX = int(os.environ.get("PAYER_MIN_TX", "3"))
# Below this many settlements a wallet gets a GRADE only if a penalty fired.
# Positive components all start near zero, so grading a young wallet on them
# would print F for a real first-week customer.
CONFIDENT_TX = int(os.environ.get("PAYER_CONFIDENT_TX", "20"))
# An exclusive-repeat payer — one counterparty ever, this many payments or
# more — is the farm shape. Matches analytics/exclusivity.py's MIN_REPEAT so
# the nightly score and the batch analysis agree on the definition.
EXCLUSIVE_MIN_REPEAT = int(os.environ.get("EXCL_MIN_REPEAT", "5"))
EXCLUSIVE_CAP = 49.0        # exclusive-repeat wallets grade D at best
MIN_MATURE_DAYS = int(os.environ.get("SCORE_MATURE_DAYS", "14"))
USDC_UNITS = 1_000_000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _grade(score: float) -> str:
    return ("A" if score >= 90 else "B" if score >= 75 else
            "C" if score >= 60 else "D" if score >= 40 else "F")


def latest_payer_snapshot():
    """(date, path) of the newest snapshot carrying payer aggregates.

    Snapshots written before payer aggregates existed have no such file and
    are skipped rather than back-filled — snapshots are immutable, and a
    rewritten one would break its own published anchor.
    """
    if not SNAP_DIR.exists():
        return None, None
    dates = sorted(p.name for p in SNAP_DIR.iterdir()
                   if p.is_dir() and (p / "payer_aggregates.csv").exists())
    if not dates:
        return None, None
    return dates[-1], SNAP_DIR / dates[-1] / "payer_aggregates.csv"


def score_payer(tx: int, unique_sellers: int, volume_units: int,
                first_ts: int, last_ts: int, self_pay: int) -> dict:
    """Score one payer from its snapshot aggregates. Pure — no I/O, no DB.

    Components (sum to 100 before penalties):
      counterparty diversity 0-40  log10(unique_sellers); 30 sellers -> max.
                                   THE CORE. One seller scores zero here, and
                                   faking a high value means colluding with
                                   dozens of independent sellers.
      rail tenure            0-20  first-to-last payment span; 90 days -> max
      volume                 0-20  log-scaled USDC; $10k -> max
      settlement count       0-20  log-scaled; 1,000 payments -> max. Capped
                                   low ON PURPOSE — this is the component every
                                   naive design over-weights.
    Penalties:
      self-pay               up to -40  (payer == seller; definitional wash)
      exclusive repeat       -35        (one counterparty ever, >= 5 payments)
    Cap: an exclusive-repeat wallet grades D at best, so it cannot buy its way
    back with volume — which is precisely what a farm wallet has most of.

    Returns a dict with score=None and status='insufficient_history' when the
    wallet is below MIN_SCOREABLE_TX.
    """
    if tx < MIN_SCOREABLE_TX:
        return {"score": None, "grade": "UNKNOWN",
                "status": ("no_history" if tx <= 0 else "insufficient_history"),
                "tx_count": tx, "unique_sellers": unique_sellers,
                "reason": (f"{tx} settlement(s) observed; below the"
                           f" {MIN_SCOREABLE_TX}-payment floor for scoring."
                           f" This is not a low score — it is the absence of"
                           f" evidence, and most agents have no history at"
                           f" all (the median Base agent made one payment).")}

    diversity = (min(40.0, 40 * math.log10(unique_sellers) / 1.5)
                 if unique_sellers > 1 else 0.0)      # 30 sellers -> max
    days_active = max(0.0, (last_ts - first_ts) / 86400)
    tenure = min(20.0, days_active / 90 * 20)
    volume_usd = volume_units / USDC_UNITS
    vol_pts = min(20.0, 20 * math.log10(1 + volume_usd) / 4)   # $10k -> max
    count_pts = min(20.0, 20 * math.log10(1 + tx) / 3)         # 1k tx -> max

    self_ratio = self_pay / tx
    self_pen = min(40.0, 40 * self_ratio * 2)                  # 50% -> max
    exclusive = (unique_sellers <= 1 and tx >= EXCLUSIVE_MIN_REPEAT)
    excl_pen = 35.0 if exclusive else 0.0

    score = max(0.0, min(100.0, round(
        diversity + tenure + vol_pts + count_pts - self_pen - excl_pen, 1)))
    if exclusive:
        score = min(score, EXCLUSIVE_CAP)

    # A penalty is a statement about SHAPE and holds at any size; the positive
    # components are statements about ACCUMULATION and do not. So a thin wallet
    # with nothing against it is UNKNOWN, not F.
    penalised = exclusive or self_pen > 0
    if tx < CONFIDENT_TX and not penalised:
        return {
            "score": None, "grade": "UNKNOWN", "status": "thin_history",
            "indicative_score": score,
            "tx_count": tx, "unique_sellers": unique_sellers,
            "self_pay_ratio": round(self_ratio, 4),
            "exclusive_repeat": False,
            "components": {
                "counterparty_diversity": round(diversity, 1),
                "rail_tenure": round(tenure, 1),
                "volume": round(vol_pts, 1),
                "settlement_count": round(count_pts, 1),
                "self_pay_penalty": 0.0,
                "exclusive_repeat_penalty": 0.0,
            },
            "reason": (f"{tx} settlements is below the {CONFIDENT_TX}-payment"
                       f" bar for a grade, and nothing structural was"
                       f" detected. The low indicative score reflects a short"
                       f" history, NOT misconduct — grading it would print F"
                       f" for an ordinary new customer."),
            "interpretation": "too little history to rate; nothing against it",
        }

    # D and F are RESERVED FOR DETECTED STRUCTURE. A clean wallet just past the
    # confidence bar (say 25 payments, 8 sellers, nothing against it) earns a
    # low score because every positive component measures accumulation — and
    # letting that print D reproduces, one bracket higher, the exact sin the
    # CONFIDENT_TX gate exists to stop. With no penalty fired the letter floors
    # at C: the number still says "small", the letter no longer says "bad".
    grade = _grade(score)
    graded_on_size_alone = not penalised and grade in ("D", "F")
    if graded_on_size_alone:
        grade = "C"

    return {
        "score": score, "grade": grade, "status": "scored",
        "grade_floored": graded_on_size_alone,
        "tx_count": tx, "unique_sellers": unique_sellers,
        "self_pay_ratio": round(self_ratio, 4),
        "exclusive_repeat": exclusive,
        "components": {
            "counterparty_diversity": round(diversity, 1),
            "rail_tenure": round(tenure, 1),
            "volume": round(vol_pts, 1),
            "settlement_count": round(count_pts, 1),
            "self_pay_penalty": round(-self_pen, 1),
            "exclusive_repeat_penalty": round(-excl_pen, 1),
        },
        "interpretation": (
            f"one counterparty ever across {tx} payments — the farm shape;"
            f" capped at D regardless of volume" if exclusive else
            "low score reflects a small, young history with nothing detected"
            " against it — D/F are reserved for structural findings"
            if graded_on_size_alone else
            "pays multiple independent sellers — structurally hard to fake"
            if unique_sellers >= 5 else
            "few counterparties; not conclusive either way"),
    }


def compute_payer_scores(store, date: str) -> int:
    """Score every payer in the latest snapshot carrying payer aggregates."""
    snap_date, csv_path = latest_payer_snapshot()
    if not csv_path:
        print("[payer-scores] no snapshot with payer_aggregates.csv yet —"
              " it lands with the next daily snapshot", file=sys.stderr)
        return 0
    n_snap_days = len([p for p in SNAP_DIR.iterdir() if p.is_dir()])
    provisional = n_snap_days < MIN_MATURE_DAYS

    n = 0
    by_status: dict = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            tx = int(row["tx_count"] or 0)
            if tx <= 0:
                continue
            r = score_payer(
                tx=tx,
                unique_sellers=int(row["unique_sellers"] or 0),
                volume_units=int(row["volume_base_units"] or 0),
                first_ts=int(row["first_ts"] or 0),
                last_ts=int(row["last_ts"] or 0),
                self_pay=int(row["self_pay_tx"] or 0))
            # indicative_score rides inside the components JSON — the table has
            # no column for it, and dropping it here would break the promise
            # that a thin_history wallet's computed number stays visible.
            comp = dict(r.get("components") or {})
            if r.get("indicative_score") is not None:
                comp["indicative_score"] = r["indicative_score"]
            store.record_payer_score(date, row["chain"], row["payer"], {
                "score": r["score"], "grade": r["grade"],
                "status": r["status"], "provisional": provisional,
                "snapshot_date": snap_date, "tx_count": r["tx_count"],
                "unique_sellers": r["unique_sellers"],
                "self_pay_ratio": r.get("self_pay_ratio"),
                "exclusive_repeat": r.get("exclusive_repeat", False),
                "components": comp,
                "score_version": PAYER_SCORE_VERSION}, _utcnow())
            n += 1
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    store.db.commit()
    scored = by_status.get("scored", 0)
    cov = scored / n if n else 0.0
    # the coverage rate is the honest headline of this product: how often a
    # caller gets a grade at all, and why not when they don't
    detail = ", ".join(f"{k}={v:,}" for k, v in sorted(by_status.items()))
    print(f"[payer-scores] {n:,} payers from snapshot {snap_date}: "
          f"graded {cov:.1%} ({detail}) (provisional={provisional})")
    return n

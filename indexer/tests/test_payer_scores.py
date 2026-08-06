"""Fixture tests for the buy-side score (no DB, no network).

The one thing this must get right: a wallet with a huge payment count and ONE
counterparty must not out-rank a wallet with modest activity across many
sellers. Every agent-reputation design reaches for payment history as the
trust signal, and our own data says that ranking is backwards at the top end —
on Base the median agent made one payment, the top 1% of wallets are 94% of
activity, and 54% of dollars are payer == seller.

Run:  python indexer/tests/test_payer_scores.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from payer_scores import (  # noqa: E402
    CONFIDENT_TX, EXCLUSIVE_CAP, MIN_SCOREABLE_TX, score_payer)

DAY = 86400
USDC = 1_000_000


def farm(tx=1204, volume_usd=50_000):
    """One counterparty ever, enormous history — the VOUCH sample response."""
    return score_payer(tx=tx, unique_sellers=1, volume_units=volume_usd * USDC,
                       first_ts=0, last_ts=200 * DAY, self_pay=0)


def diversified(tx=40, sellers=12, volume_usd=400):
    return score_payer(tx=tx, unique_sellers=sellers,
                       volume_units=volume_usd * USDC,
                       first_ts=0, last_ts=120 * DAY, self_pay=0)


def test_the_inversion_is_fixed():
    """THE POINT OF THE WHOLE MODULE. 1,204 payments to one seller must score
    BELOW 40 payments across twelve."""
    f, d = farm(), diversified()
    assert f["score"] < d["score"], (f["score"], d["score"])
    assert f["exclusive_repeat"] is True
    assert d["exclusive_repeat"] is False


def test_volume_cannot_buy_back_the_exclusivity_cap():
    """A farm wallet's one abundant resource is volume. It must not be able to
    out-score its own structure."""
    poor, rich = farm(volume_usd=10), farm(volume_usd=10_000_000)
    assert rich["score"] <= EXCLUSIVE_CAP
    assert poor["score"] <= EXCLUSIVE_CAP
    assert rich["grade"] in ("D", "F")


def test_counterparty_diversity_is_the_dominant_component():
    one = score_payer(tx=50, unique_sellers=1, volume_units=100 * USDC,
                      first_ts=0, last_ts=90 * DAY, self_pay=0)
    many = score_payer(tx=50, unique_sellers=30, volume_units=100 * USDC,
                       first_ts=0, last_ts=90 * DAY, self_pay=0)
    assert one["components"]["counterparty_diversity"] == 0.0
    assert many["components"]["counterparty_diversity"] >= 39.0


def test_a_stranger_is_unknown_not_bad():
    """The failure mode of every agent-reputation product: turning absence of
    evidence into a low score."""
    r = score_payer(tx=0, unique_sellers=0, volume_units=0,
                    first_ts=0, last_ts=0, self_pay=0)
    assert r["score"] is None and r["grade"] == "UNKNOWN"
    assert r["status"] == "no_history"


def test_one_shot_payers_are_not_scored():
    """The median Base agent made exactly one payment — scoring that wallet at
    all would mean scoring most of the population on noise."""
    r = score_payer(tx=1, unique_sellers=1, volume_units=5 * USDC,
                    first_ts=0, last_ts=0, self_pay=0)
    assert r["score"] is None and r["status"] == "insufficient_history"
    # crossing MIN_SCOREABLE_TX buys components, not yet a grade — that is
    # CONFIDENT_TX's job
    r2 = score_payer(tx=MIN_SCOREABLE_TX, unique_sellers=2,
                     volume_units=5 * USDC, first_ts=0, last_ts=DAY,
                     self_pay=0)
    assert r2["status"] == "thin_history" and r2["grade"] == "UNKNOWN"
    r3 = score_payer(tx=CONFIDENT_TX, unique_sellers=6,
                     volume_units=500 * USDC, first_ts=0, last_ts=60 * DAY,
                     self_pay=0)
    assert r3["score"] is not None and r3["status"] == "scored"


def test_self_payment_is_penalised_hard():
    clean = score_payer(tx=100, unique_sellers=8, volume_units=1000 * USDC,
                        first_ts=0, last_ts=90 * DAY, self_pay=0)
    dirty = score_payer(tx=100, unique_sellers=8, volume_units=1000 * USDC,
                        first_ts=0, last_ts=90 * DAY, self_pay=50)
    assert clean["score"] - dirty["score"] >= 39.0


def test_four_payments_to_one_seller_is_not_yet_the_farm_shape():
    """Below EXCLUSIVE_MIN_REPEAT a single-counterparty wallet is just a new
    customer of one vendor — real and common. The penalty must not fire."""
    r = score_payer(tx=4, unique_sellers=1, volume_units=20 * USDC,
                    first_ts=0, last_ts=10 * DAY, self_pay=0)
    assert r["exclusive_repeat"] is False
    assert r["components"]["exclusive_repeat_penalty"] == 0.0


def test_a_real_new_customer_is_never_graded_F():
    """THE SECOND FORM OF THE SAME SIN. Five payments across three sellers is
    an ordinary first-week customer. Every positive component starts near zero,
    so grading it naively prints F — indistinguishable from a self-dealer."""
    r = score_payer(tx=5, unique_sellers=3, volume_units=12 * USDC,
                    first_ts=0, last_ts=6 * DAY, self_pay=0)
    assert r["status"] == "thin_history"
    assert r["score"] is None and r["grade"] == "UNKNOWN"
    assert r["indicative_score"] is not None, "the number must still be shown"


def test_structure_is_conclusive_at_any_size():
    """Penalties describe SHAPE, so they hold below the confidence bar — a
    thin wallet that only ever pays itself is not 'too early to say'."""
    r = score_payer(tx=6, unique_sellers=1, volume_units=10 * USDC,
                    first_ts=0, last_ts=3 * DAY, self_pay=6)
    assert r["status"] == "scored" and r["grade"] == "F"
    excl = score_payer(tx=6, unique_sellers=1, volume_units=10 * USDC,
                       first_ts=0, last_ts=3 * DAY, self_pay=0)
    assert excl["status"] == "scored" and excl["exclusive_repeat"] is True


def test_a_clean_small_wallet_past_the_bar_is_never_D_or_F():
    """THE THIRD FORM OF THE SAME SIN, caught by running the real pipeline:
    25 payments across 8 sellers, zero self-pay, no penalty — pure
    accumulation scoring printed D. D and F are reserved for detected
    structure; with nothing fired, the letter floors at C while the score
    still says 'small'."""
    r = score_payer(tx=25, unique_sellers=8, volume_units=50 * USDC,
                    first_ts=0, last_ts=24 * DAY, self_pay=0)
    assert r["status"] == "scored"
    assert r["grade"] not in ("D", "F"), r
    assert r["grade_floored"] is True
    assert r["score"] < 60          # the number is not inflated by the floor
    assert "reserved for structural findings" in r["interpretation"]


def test_the_floor_never_rescues_a_penalised_wallet():
    """The floor exists for clean wallets only — a farm or self-dealer with
    the same accumulation profile must keep its D/F."""
    f = score_payer(tx=25, unique_sellers=1, volume_units=50 * USDC,
                    first_ts=0, last_ts=24 * DAY, self_pay=0)
    assert f["grade"] in ("D", "F") and not f.get("grade_floored")
    sd = score_payer(tx=25, unique_sellers=8, volume_units=50 * USDC,
                     first_ts=0, last_ts=24 * DAY, self_pay=12)
    assert sd["grade"] in ("D", "F") and not sd.get("grade_floored")


def test_scores_stay_in_range_at_the_extremes():
    hi = score_payer(tx=10**6, unique_sellers=10**4,
                     volume_units=10**9 * USDC, first_ts=0,
                     last_ts=3650 * DAY, self_pay=0)
    lo = score_payer(tx=1000, unique_sellers=1, volume_units=0,
                     first_ts=0, last_ts=0, self_pay=1000)
    assert 0.0 <= lo["score"] <= 100.0
    assert 0.0 <= hi["score"] <= 100.0
    assert hi["grade"] == "A" and lo["grade"] == "F"


def test_interpretation_names_the_shape():
    assert "farm shape" in farm()["interpretation"]
    assert "hard to fake" in diversified()["interpretation"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(dict(globals()).items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nall {len(fns)} payer-score tests passed")

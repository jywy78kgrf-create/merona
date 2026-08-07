"""Scores v4 seller letters: conduct sinks, absence caps, only conduct
earns an F.

Motivating audit (2026-08-06, production Postgres): 131,959 sellers graded F,
of which exactly 201 carried wash evidence — 99.85% of Fs were merely small.
The clean-seller score distribution sat at p25 36 / p50 41 / p90 49 while the
letter map started C at 60, so the letter carried no information: an F shared
by 99.5% of the population stops no payments and hides the 201 real flags.

These tests pin the v4 letter function branch by branch, plus one integration
pass through compute_seller_scores to prove the gate and conduct anchors
survive the real pipeline.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import scores                                            # noqa: E402
from scores import _seller_letter                        # noqa: E402
from storage import Store                                # noqa: E402

CLEAN = dict(self_ratio=0.0, nflag=False, recip=False, hflag=False,
             cflag=False, t2_ok=False, top_share=0.0)
GATED = dict(tx=100, payers=20)                          # comfortably past gate


def L(score, **kw):
    a = {**CLEAN, **GATED, "score": score, **kw}
    return _seller_letter(a["score"], a["tx"], a["payers"], a["self_ratio"],
                          a["nflag"], a["recip"], a["hflag"], a["cflag"],
                          a["t2_ok"], a["top_share"])


# ---- the gate: absence of history is not evidence of misconduct -------------

def test_clean_thin_sellers_are_unrated_not_F():
    for tx, payers in ((1, 1), (3, 2), (24, 50), (500, 4)):
        g, note = L(40, tx=tx, payers=payers)
        assert g is None, (tx, payers, g)
        assert note == "unrated_below_letter_gate"


def test_gate_boundary_is_exact():
    assert L(40, tx=25, payers=5)[0] is not None      # exactly at gate: letter
    assert L(40, tx=24, payers=5)[0] is None
    assert L(40, tx=25, payers=4)[0] is None


# ---- conduct anchors: F/D are earned, and ignore the gate -------------------

def test_live_wash_signals_earn_F_even_below_the_gate():
    for flag in ("nflag", "recip", "cflag"):
        g, note = L(45, tx=2, payers=1, **{flag: True})
        assert g == "F" and note == "conduct_live", flag


def test_extreme_self_pay_is_conduct():
    assert L(45, self_ratio=0.25)[0] == "F"
    assert L(45, self_ratio=0.24)[0] != "F"


def test_history_only_flag_is_D_not_F():
    g, note = L(45, hflag=True)
    assert g == "D" and note == "conduct_history_only"
    # but any live signal outranks the history downgrade
    assert L(45, hflag=True, cflag=True)[0] == "F"


# ---- bands anchored to the observed clean distribution ----------------------

def test_bands_match_calibration():
    # p50=41 must be a C, p90=49 a B — under v3 both were D
    assert L(36)[0] == "C" and L(41)[0] == "C" and L(46.9)[0] == "C"
    assert L(47)[0] == "B" and L(49)[0] == "B"
    g, note = L(35.9)
    assert g == "D" and note == "clean_bottom_band"


def test_A_requires_T2_evidence_not_just_score():
    assert L(60)[0] == "B"                      # high score, no census proof
    assert L(60, t2_ok=True, payers=100)[0] == "A"
    assert L(54.9, t2_ok=True)[0] == "B"        # T2 alone does not gift an A


def test_A_caps_at_B_on_single_payer_concentration():
    # v4.2: BlockRun — 99.6% of 10M settlements from one payer. Nominal
    # breadth is not a customer base; the cap and its reason are recorded.
    g, note = L(70, t2_ok=True, payers=1100, top_share=0.996)
    assert g == "B" and note == "concentration_capped"
    assert L(70, t2_ok=True, payers=1100, top_share=0.89)[0] == "A"


def test_A_requires_a_real_customer_base():
    # v4.1: six lifetime payers is not verified commerce, whatever the score
    assert L(70, t2_ok=True, payers=6)[0] == "B"
    assert L(70, t2_ok=True, payers=49)[0] == "B"
    assert L(70, t2_ok=True, payers=50)[0] == "A"


# ---- integration: the real pipeline applies gate + conduct ------------------

def test_compute_seller_scores_applies_v4(tmp_path, monkeypatch):
    now = int(time.time())
    snap = tmp_path / "snapshots" / "2026-08-06"
    snap.mkdir(parents=True)
    tiny = "0x" + "01" * 20          # 3 tx, clean -> unrated (was F in v3)
    mid = "0x" + "02" * 20           # 5000 tx / 900 payers clean -> letter
    with open(snap / "seller_aggregates.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["chain", "seller", "tx_count",
                                          "unique_payers", "self_pay_tx",
                                          "first_ts", "last_ts"])
        w.writeheader()
        w.writerow(dict(chain="base", seller=tiny, tx_count=3,
                        unique_payers=2, self_pay_tx=0,
                        first_ts=now - 86400, last_ts=now))
        w.writerow(dict(chain="base", seller=mid, tx_count=5000,
                        unique_payers=900, self_pay_tx=0,
                        first_ts=now - 60 * 86400, last_ts=now))
    monkeypatch.setattr(scores, "SNAP_DIR", snap.parent)
    monkeypatch.setattr(scores, "FLAGS_DIR", tmp_path)
    monkeypatch.setattr(scores, "MIN_MATURE_DAYS", 1)
    monkeypatch.setattr(scores, "CENSUS_DIRECTORY", tmp_path / "none.json")
    s = Store(tmp_path / "t.sqlite")
    assert scores.compute_seller_scores(s, "2026-08-06") == 2

    rows = {r[0]: r for r in s.db.execute(
        "SELECT seller, grade, score, components, score_version "
        "FROM seller_scores").fetchall()}
    g_tiny, comp_tiny = rows[tiny][1], json.loads(rows[tiny][3])
    g_mid = rows[mid][1]
    assert rows[tiny][4] == 4 and rows[mid][4] == 4
    assert g_tiny is None                              # the whole point of v4
    assert rows[tiny][2] is not None                   # score still recorded
    assert comp_tiny["letter"] == "unrated_below_letter_gate"
    assert g_mid in ("B", "C")                         # clean + gated = letter
    s.close()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

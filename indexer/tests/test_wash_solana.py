"""Solana wash consumer (indexer/wash_solana.py) — the GATED v2 path.

Covers the two pieces that must be right before Solana is ever promoted into
wash.WASH_CHAINS: parsing the off-rail funding graph, the per-seller classifier,
and the end-to-end clean computation (scope + flag + exclude). Network/RPC is
never touched — the graph is a fixture, the store is SQLite. Asserts case is
preserved (Solana base58 is case-sensitive, unlike EVM)."""
import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wash_solana as W  # noqa: E402
from storage import Store  # noqa: E402

REL = "SoLReLaYeRxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx1"   # a registry relayer (case-sensitive)
S1 = "WashSeLLeR1111111111111111111111111111111"   # funds its own payers -> washed
S2 = "CleanSeLLeR22222222222222222222222222222"    # organic


def _graph_file(tmp_path):
    p = tmp_path / "funding_solana.csv.gz"
    with gzip.open(p, "wt") as f:
        f.write("sender,recipient,amount\n")
        # S1 funds P1,P2,P3 off-rail; a blank + short line must be tolerated
        for pay in ("Pay1", "Pay2", "Pay3"):
            f.write(f"{S1},{pay},1000000\n")
        f.write("\n")
        f.write("garbage\n")
    return p


def test_load_funding_graph_parses_and_preserves_case(tmp_path):
    g = W.load_funding_graph(_graph_file(tmp_path))
    assert g == {S1: {"Pay1", "Pay2", "Pay3"}}
    # the exact (mixed-case) seller key must be present — no lowercasing
    assert S1 in g and S1.lower() not in g


def test_load_funding_graph_tolerates_truncated_tail(tmp_path):
    """funding_solana.py appends a gzip member per resumed run; a killed run
    leaves a truncated final member. The reader must salvage the intact members
    (EOFError before the tear) rather than raise."""
    import io

    def member(text):
        b = io.BytesIO()
        with gzip.GzipFile(fileobj=b, mode="wb") as g:
            g.write(text.encode())
        return b.getvalue()

    m1 = member("sender,recipient,amount\n" + S1 + ",Pay1,1\n" + S1 + ",Pay2,1\n")
    torn = member(S2 + ",Pay9,1\n" + S2 + ",Pay8,1\n")
    m3 = member(REL + ",PayZ,1\n")               # a CLEAN member AFTER the tear
    p = tmp_path / "f.csv.gz"
    # intact | half-written (torn) | intact — the reader must recover m1 AND m3
    p.write_bytes(m1 + torn[: len(torn) // 2] + m3)

    g = W.load_funding_graph(p)                  # must NOT raise
    assert g.get(S1) == {"Pay1", "Pay2"}         # member before the tear
    assert g.get(REL) == {"PayZ"}                # member AFTER the tear recovered


def test_flag_seller_rules():
    payers = {"Pay1", "Pay2", "Pay3"}
    # funded_ratio 1.0 (all payers were funded by the seller) -> flagged
    fl, sr, fr = W.flag_seller(tx_count=3, self_pay=0, payers=payers,
                               funded_recipients=payers, is_recip=False)
    assert fl and fr == 1.0 and sr == 0.0
    # no funding overlap, no self-pay, not reciprocal -> clean
    fl, _, fr = W.flag_seller(tx_count=3, self_pay=0, payers=payers,
                              funded_recipients=set(), is_recip=False)
    assert not fl and fr == 0.0
    # self-pay over threshold alone -> flagged
    fl, sr, _ = W.flag_seller(tx_count=10, self_pay=5, payers=payers,
                              funded_recipients=set(), is_recip=False)
    assert fl and sr == 0.5
    # reciprocal alone -> flagged even with no funding coverage
    fl, _, fr = W.flag_seller(tx_count=3, self_pay=0, payers=payers,
                              funded_recipients=None, is_recip=True)
    assert fl and fr is None


def _seed_solana(store):
    def row(tx, li, seller, payer, amt, blk):
        return dict(tx_hash=tx, log_index=li, chain="solana", token="usdc",
                    payer=payer, seller=seller, amount=amt,
                    block_number=blk, block_timestamp=1730000000 + blk)
    rows = [
        row("t1", 0, S1, "Pay1", 1_000_000, 1001),   # washed seller S1: $3
        row("t2", 0, S1, "Pay2", 1_000_000, 1002),
        row("t3", 0, S1, "Pay3", 1_000_000, 1003),
        row("t4", 0, S2, "Qa",   2_000_000, 1004),   # clean seller S2: $4
        row("t5", 0, S2, "Qb",   2_000_000, 1005),
    ]
    store.commit_range("solana", 1000, 1010, rows, "t")
    # stamp the facilitator (native scope) — all through the registry relayer
    store.db.execute("UPDATE settlements SET facilitator=? WHERE chain='solana'",
                     (REL,))
    store.set_meta("witnessed_start_solana", 0)
    store.db.commit()


def test_compute_clean_excludes_funded_loop(tmp_path, monkeypatch):
    s = Store(tmp_path / "sol.sqlite")
    _seed_solana(s)
    monkeypatch.setattr(W, "solana_relayers", lambda: [REL])
    graph = W.load_funding_graph(_graph_file(tmp_path))

    m = W.compute_clean_solana(s, "2026-07-30", graph, write=False)
    # scoped = all 5 ($7); S1 flagged (funded-loop) -> its 3 setts / $3 removed
    assert m["scoped_settlements"] == 5 and m["scoped_volume_usd"] == 7.0
    assert m["flagged_sellers"] == 1
    assert m["excluded_settlements"] == 3
    assert m["clean_settlements"] == 2 and m["clean_volume_usd"] == 4.0
    # dry-run wrote nothing
    assert s.db.execute("SELECT COUNT(*) FROM clean_metrics").fetchone()[0] == 0
    s.close()


def test_write_flag_publishes_row(tmp_path, monkeypatch):
    s = Store(tmp_path / "sol.sqlite")
    _seed_solana(s)
    monkeypatch.setattr(W, "solana_relayers", lambda: [REL])
    graph = W.load_funding_graph(_graph_file(tmp_path))
    W.compute_clean_solana(s, "2026-07-30", graph, write=True)
    got = s.db.execute("SELECT chain, clean_volume_usd FROM clean_metrics "
                       "WHERE measured_date='2026-07-30'").fetchone()
    assert got == ("solana", 4.0)
    s.close()

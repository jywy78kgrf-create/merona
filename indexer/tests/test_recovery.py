"""Snapshot-anchored bootstrap recovery.

If the working DB is wiped (fresh box, evicted actions/cache) but the durable
git-committed snapshots exist, a fresh bootstrap must RESUME from the last
snapshot head — not re-seed from chain-tip and leave a silent gap.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import daily_snapshot as ds  # noqa: E402
from evm_chains import BASE  # noqa: E402
from storage import Store  # noqa: E402


def test_recovery_anchors_to_snapshot_head(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    # fresh DB, but a committed snapshot recorded base up to block 1000
    start = ds.bootstrap_start_block(store, BASE, end=5_000_000,
                                     snapshot_heads={"base": 1000})
    assert start == 1001                    # resume from snapshot head + 1…
    assert start != 5_000_000 - BASE.bootstrap_lookback_blocks  # …NOT a tip re-seed
    assert int(store.get_meta("start_block")) == 1001
    # already bootstrapped → returns the stored value, ignores new heads
    assert ds.bootstrap_start_block(store, BASE, 9_000_000, {"base": 9999}) == 1001
    store.close()


def test_no_snapshot_seeds_from_tip(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    start = ds.bootstrap_start_block(store, BASE, end=5_000_000, snapshot_heads={})
    assert start == 5_000_000 - BASE.bootstrap_lookback_blocks
    store.close()


def test_last_snapshot_chain_heads(tmp_path):
    d = tmp_path / "2026-07-04"
    d.mkdir()
    (d / "snapshot_meta.json").write_text(json.dumps({
        "base": {"max_block": 123},
        "polygon": {"max_block": 456},
        "solana": {"max_block": None},                 # excluded (no int)
        "permit2_non_usdc": {"base": {"settlements": 1}},  # excluded (no max_block)
    }))
    assert ds.last_snapshot_chain_heads(tmp_path) == {"base": 123, "polygon": 456}
    # newest snapshot wins
    d2 = tmp_path / "2026-07-05"
    d2.mkdir()
    (d2 / "snapshot_meta.json").write_text(json.dumps({"base": {"max_block": 999}}))
    assert ds.last_snapshot_chain_heads(tmp_path) == {"base": 999}
    # no snapshots → empty
    assert ds.last_snapshot_chain_heads(tmp_path / "nope") == {}

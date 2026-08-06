"""Optional enrichment: resolve each settlement's facilitator via tx.from.

The core index anchors on EIP-3009 (gasless/authorized USDC transfers) and is a
~4%-broader, fresher superset of x402scan's facilitator-scoped view. This pass
resolves the relayer (tx.from) for settlements and tags:
  facilitator = <id>  if tx.from is a known relayer  (facilitator-x402)
  facilitator = ''    if resolved but not a known relayer (other gasless pay)
  facilitator = NULL  not yet resolved

One eth_getTransactionByHash per UNIQUE tx (settlements in a batch tx share it),
so cost is per-tx not per-settlement. Idempotent and resumable: only rows with
facilitator IS NULL are fetched. Safe to run incrementally or partially.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chain import ChainClient  # noqa: E402
from storage import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "data" / "indexer" / "relayer_registry.json"
DB_PATH = ROOT / "data" / "indexer" / "base_settlements.sqlite"


def load_relayer_map() -> dict[str, str]:
    """address(lower) -> facilitator id."""
    reg = json.load(open(REGISTRY))
    m: dict[str, str] = {}
    for fid, nets in reg["facilitators"].items():
        for a in nets.get("base", []):
            m[a["address"].lower()] = fid
    return m


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0  # 0 = all
    store = open_store(DB_PATH)
    relayers = load_relayer_map()
    client = ChainClient("https://mainnet.base.org")

    q = ("SELECT DISTINCT tx_hash FROM settlements WHERE facilitator IS NULL")
    if limit:
        q += f" LIMIT {limit}"
    tx_hashes = [r[0] for r in store.db.execute(q).fetchall()]
    print(f"[enrich] {len(tx_hashes)} unresolved txs")

    resolved = 0
    for i, h in enumerate(tx_hashes):
        try:
            tx = client._call("eth_getTransactionByHash", [h])
            frm = (tx.get("from") or "").lower()
        except Exception as e:
            print(f"[enrich] {h[:12]} err {e}")
            continue
        fid = relayers.get(frm, "")
        store.db.execute("UPDATE settlements SET facilitator=? WHERE tx_hash=?",
                         (fid, h))
        resolved += 1
        if (i + 1) % 200 == 0:
            store.db.commit()
            print(f"[enrich] {i+1}/{len(tx_hashes)}", flush=True)
    store.db.commit()

    # report the split
    tot = store.db.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    # NB: `col NOT IN ('', NULL)` is always false in SQL (NULL comparison is
    # unknown) — must test IS NOT NULL AND != '' explicitly.
    facil = store.db.execute(
        "SELECT COUNT(*) FROM settlements "
        "WHERE facilitator IS NOT NULL AND facilitator != ''").fetchone()[0]
    other = store.db.execute(
        "SELECT COUNT(*) FROM settlements WHERE facilitator=''").fetchone()[0]
    unresolved = store.db.execute(
        "SELECT COUNT(*) FROM settlements WHERE facilitator IS NULL").fetchone()[0]
    print(json.dumps({"resolved_txs_this_run": resolved, "settlements_total": tot,
                      "facilitator_x402": facil, "other_gasless": other,
                      "unresolved": unresolved,
                      "facilitator_share": round(facil / (facil + other), 4)
                      if (facil + other) else None}, indent=2))
    store.close()


if __name__ == "__main__":
    main()

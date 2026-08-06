"""Registry parser — address/date pairing under both upstream field orders.

The registry's whole job is "which addresses are facilitator relayers"; the
dates ride along as provenance. A line-oriented walk that carried a pending
date forward silently handed every address its PREDECESSOR's date whenever
upstream wrote `address` before `dateOfFirstTransaction` (which most of the
current facilitator files do), and left the first entry of each block null.
Both orders appear upstream, so the parser reads each entry object whole.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_relayer_registry import parse_facilitator  # noqa: E402

ADDR_FIRST = """
export const obolFacilitator = {
  id: 'obol',
  addresses: {
    [Network.BASE]: [
      {
        address: '0xAAAA000000000000000000000000000000000001',
        tokens: [USDC_BASE_TOKEN],
        dateOfFirstTransaction: new Date('2026-06-07'),
      },
      {
        address: '0xAAAA000000000000000000000000000000000002',
        tokens: [USDC_BASE_TOKEN],
        dateOfFirstTransaction: new Date('2026-06-08'),
      },
    ],
    [Network.SOLANA]: [
      {
        address: 'SoLRelayer1111111111111111111111111111111',
        dateOfFirstTransaction: new Date('2026-06-09'),
      },
    ],
  },
}
"""

DATE_FIRST = """
export const legacyFacilitator = {
  id: 'legacy',
  addresses: {
    [Network.Base]: [
      { dateOfFirstTransaction: new Date('2025-01-01'),
        address: '0xBBBB000000000000000000000000000000000001' },
      { dateOfFirstTransaction: new Date('2025-02-02'),
        address: '0xBBBB000000000000000000000000000000000002' },
    ],
  },
}
"""


def _parse(tmp_path, name, text):
    p = tmp_path / f"{name}.ts"
    p.write_text(text)
    return parse_facilitator(p)


def test_address_before_date_pairs_within_entry(tmp_path):
    fid, addrs = _parse(tmp_path, "obol", ADDR_FIRST)
    assert fid == "obol"
    assert addrs["base"] == [
        {"address": "0xAAAA000000000000000000000000000000000001",
         "first_tx_date": "2026-06-07"},
        {"address": "0xAAAA000000000000000000000000000000000002",
         "first_tx_date": "2026-06-08"},
    ]
    # and the date must not leak across the network boundary
    assert addrs["solana"] == [
        {"address": "SoLRelayer1111111111111111111111111111111",
         "first_tx_date": "2026-06-09"}]


def test_date_before_address_still_pairs(tmp_path):
    _, addrs = _parse(tmp_path, "legacy", DATE_FIRST)
    assert [a["first_tx_date"] for a in addrs["base"]] == ["2025-01-01",
                                                           "2025-02-02"]


def test_undated_address_is_kept_with_null_not_borrowed(tmp_path):
    text = ADDR_FIRST.replace(
        "        dateOfFirstTransaction: new Date('2026-06-08'),\n", "")
    _, addrs = _parse(tmp_path, "obol", text)
    assert addrs["base"][0]["first_tx_date"] == "2026-06-07"
    assert addrs["base"][1]["first_tx_date"] is None  # not 2026-06-07


def test_literals_outside_addresses_block_are_not_relayers(tmp_path):
    text = ("const USDC = { address: '0xDEAD00000000000000000000000000000000dead' }\n"
            + ADDR_FIRST)
    _, addrs = _parse(tmp_path, "obol", text)
    got = {a["address"] for v in addrs.values() for a in v}
    assert not any(a.lower().endswith("dead") for a in got)
    assert len(got) == 3


def test_no_addresses_block_returns_empty(tmp_path):
    _, addrs = _parse(tmp_path, "cfg", "export const cfg = { id: 'cfg' }\n")
    assert addrs == {}

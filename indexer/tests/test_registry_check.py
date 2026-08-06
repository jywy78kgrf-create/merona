"""P1.5 — facilitator registry drift check. Verifies the derive/diff logic
against a synthetic x402scan facilitators tree (no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import registry_check as rc  # noqa: E402

FAC_TS = """
export const foo = {
  id: 'foo',
  addresses: {
    [Network.Base]: [
      { address: '0xAAAA000000000000000000000000000000000001',
        dateOfFirstTransaction: new Date('2025-01-01') },
    ],
    [Network.Solana]: [
      { address: 'SoLRelayer1111111111111111111111111111111' },
    ],
  },
}
"""


def _make_clone(tmp_path, extra_solana=None):
    fac = tmp_path / rc.FAC_SUBPATH
    fac.mkdir(parents=True)
    (fac / "foo.ts").write_text(FAC_TS)
    if extra_solana:
        (fac / "bar.ts").write_text(FAC_TS.replace("foo", "bar").replace(
            "SoLRelayer1111111111111111111111111111111", extra_solana))
    return tmp_path


def test_derive_counts(tmp_path):
    clone = _make_clone(tmp_path / "c")
    d = rc.derive(clone)
    assert d["facilitators"] == {"foo"}
    assert d["base"] == {"0xaaaa000000000000000000000000000000000001"}
    assert d["solana"] == {"SoLRelayer1111111111111111111111111111111"}


def test_drift_detects_new_solana_relayer(tmp_path, monkeypatch, capsys):
    # stored registry = one solana relayer; upstream clone adds a second
    reg = tmp_path / "registry.json"
    reg.write_text('{"source_commit":"pinned","facilitators":["foo"],'
                   '"base_relayers_lower":["0xaaaa000000000000000000000000000000000001"],'
                   '"solana_relayers":["SoLRelayer1111111111111111111111111111111"]}')
    clone = _make_clone(tmp_path / "c",
                        extra_solana="SoLRelayer2222222222222222222222222222222")
    monkeypatch.setattr(rc, "REGISTRY", reg)
    monkeypatch.setattr(rc, "CURATED", tmp_path / "no_curated.json")
    monkeypatch.setattr(sys, "argv", ["registry_check.py", str(clone)])
    rc_exit = rc.main()
    out = capsys.readouterr()
    assert rc_exit == 3  # drift
    assert "SoLRelayer2222" in out.err and "CRITICAL" in out.err


def test_in_sync_is_clean(tmp_path, monkeypatch):
    reg = tmp_path / "registry.json"
    reg.write_text('{"source_commit":"pinned","facilitators":["foo"],'
                   '"base_relayers_lower":["0xaaaa000000000000000000000000000000000001"],'
                   '"solana_relayers":["SoLRelayer1111111111111111111111111111111"]}')
    clone = _make_clone(tmp_path / "c")
    monkeypatch.setattr(rc, "REGISTRY", reg)
    monkeypatch.setattr(rc, "CURATED", tmp_path / "no_curated.json")
    monkeypatch.setattr(sys, "argv", ["registry_check.py", str(clone)])
    assert rc.main() == 0

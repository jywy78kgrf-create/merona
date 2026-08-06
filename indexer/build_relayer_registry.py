"""Extract the facilitator RELAYER address registry from the x402scan source.

Why this exists: the indexer's entire notion of "an x402 settlement" is "a USDC
transfer submitted by a facilitator relayer." If the relayer set is wrong, every
downstream number is silently wrong for as long as the indexer runs. So we parse
the *structured* `addresses: { [Network.X]: [{address, dateOfFirstTransaction}] }`
blocks from each facilitator definition — NOT a blind hex grep, which would pull
in example payTo/receiver addresses and poison the set.

Source: github.com/Merit-Systems/x402scan @ pinned commit (recorded in output).
Run against a local clone path passed as argv[1].

Output: data/indexer/relayer_registry.json
  { facilitator_id: { base: [{address, first_tx_date}], solana: [...] } }
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "data" / "indexer" / "relayer_registry.json"
# Curated facilitators x402scan's source omits (e.g. Polygon Labs' own 24-key
# Facilitator pool). Merged into the generated set; registry_check excludes them
# from the x402scan drift diff since upstream not listing them is expected.
CURATED = REPO_ROOT / "data" / "indexer" / "relayer_registry_curated.json"
# Facilitator SELF-DECLARATIONS (registrar/SELF-DECLARATION.md): signature-
# verified relayer addresses, typically on chains x402scan has no registry for.
# Same shape as curated; the _declaration metadata key carries the signatures.
DECLARED = REPO_ROOT / "data" / "indexer" / "relayer_registry_declared.json"

NETWORK_RE = re.compile(r"\[Network\.(\w+)\]\s*:")
ADDRESS_RE = re.compile(r"address:\s*['\"]([^'\"]+)['\"]")
DATE_RE = re.compile(r"dateOfFirstTransaction:\s*new Date\(['\"]([^'\"]+)['\"]\)")
ID_RE = re.compile(r"id:\s*['\"]([^'\"]+)['\"]")


def _entries(block: str):
    """Yield (offset, text) for each `{...}` object nested directly inside the
    `addresses` block — one per relayer address, regardless of field order."""
    depth, start = 0, None
    for i, ch in enumerate(block):
        if ch == "{":
            depth += 1
            if depth == 2:  # depth 1 is the `addresses` object itself
                start = i
        elif ch == "}":
            if depth == 2 and start is not None:
                yield start, block[start:i + 1]
                start = None
            depth -= 1


def parse_facilitator(path: Path) -> tuple[str, dict]:
    """Return (facilitator_id, {network: [{address, first_tx_date}]}).

    We slice the `addresses:` object out first, so unrelated address literals
    elsewhere in the file (e.g. token constants) can't poison the set. Within it
    we read each entry OBJECT whole and pair the address with the date declared
    beside it. Reading per-object rather than per-line matters: upstream writes
    `address` before `dateOfFirstTransaction`, so a linear walk that carries a
    pending date forward hands every address its predecessor's date and leaves
    the first of each block null. Network is assigned by position — the last
    `[Network.X]:` header preceding the entry.
    """
    text = path.read_text()
    fid_match = ID_RE.search(text)
    facilitator_id = fid_match.group(1) if fid_match else path.stem

    # Restrict to the `addresses: { ... }` object to avoid stray literals.
    addr_start = text.find("addresses:")
    if addr_start == -1:
        return facilitator_id, {}
    # find the matching closing brace for the addresses object
    brace_open = text.find("{", addr_start)
    depth, i = 0, brace_open
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = text[brace_open:i + 1]

    result: dict[str, list] = {}
    headers = [(m.start(), m.group(1).lower()) for m in NETWORK_RE.finditer(block)]
    for _, net in headers:
        result.setdefault(net, [])
    for offset, entry in _entries(block):
        net = None
        for pos, name in headers:
            if pos > offset:
                break
            net = name
        if net is None:
            continue  # an entry before any network header: not ours to claim
        am = ADDRESS_RE.search(entry)
        if not am:
            continue
        dm = DATE_RE.search(entry)
        result[net].append({
            "address": am.group(1),
            "first_tx_date": dm.group(1) if dm else None,
        })
    return facilitator_id, result


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: build_relayer_registry.py <path-to-x402scan-clone>")
        sys.exit(2)
    clone = Path(sys.argv[1])
    fac_dir = clone / "packages/external/facilitators/src/facilitators"
    if not fac_dir.is_dir():
        print(f"facilitators dir not found under {clone}")
        sys.exit(2)

    commit = "unknown"
    head = clone / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref:"):
            commit = (clone / ".git" / ref.split(" ", 1)[1].strip()).read_text().strip()
        else:
            commit = ref
    except Exception:
        pass

    registry: dict[str, dict] = {}
    norm_base: set[str] = set()
    norm_sol: set[str] = set()
    # Per-chain relayer sets, for x402-SCOPING the EVM EIP-3009 superset (a
    # settlement counts as x402 only if tx.from is one of these). EVM addresses
    # lowercased; Solana kept as-is (base58, case-sensitive).
    by_chain: dict[str, set] = {}
    for f in sorted(fac_dir.glob("*.ts")):
        if f.stem == "index":
            continue
        fid, addrs = parse_facilitator(f)
        if not addrs:
            continue
        registry[fid] = addrs
        for net, lst in addrs.items():
            s = by_chain.setdefault(net, set())
            for a in lst:
                s.add(a["address"] if net == "solana" else a["address"].lower())
        for a in addrs.get("base", []):
            norm_base.add(a["address"].lower())
        for a in addrs.get("solana", []):
            norm_sol.add(a["address"])

    # Merge curated facilitators (authoritative sources x402scan doesn't carry)
    # and signature-verified self-declarations (registrar/). Same shape as
    # parsed facilitators; keys starting with "_" (provenance notes, the
    # _declaration signature block) are metadata, not networks. EVM lowercased,
    # Solana kept as-is.
    def _merge_extra(path: Path, ids: list[str]) -> None:
        if not path.exists():
            return
        cj = json.loads(path.read_text())
        for fid, nets in cj.get("facilitators", {}).items():
            clean = {net: lst for net, lst in nets.items()
                     if not net.startswith("_") and isinstance(lst, list)}
            registry[fid] = {**registry.get(fid, {}), **clean}
            ids.append(fid)
            for net, lst in clean.items():
                s = by_chain.setdefault(net, set())
                for a in lst:
                    s.add(a["address"] if net == "solana" else a["address"].lower())
                if net == "base":
                    norm_base.update(a["address"].lower() for a in lst)
                if net == "solana":
                    norm_sol.update(a["address"] for a in lst)

    curated_ids: list[str] = []
    declared_ids: list[str] = []
    _merge_extra(CURATED, curated_ids)
    _merge_extra(DECLARED, declared_ids)

    out = {
        "source": "github.com/Merit-Systems/x402scan",
        "source_commit": commit,
        "note": ("Relayer (tx sender) addresses per facilitator per network, "
                 "parsed from the structured `addresses` blocks. A settlement "
                 "= a USDC transfer whose tx.from is one of these."),
        "facilitators": registry,
        "curated_facilitators": sorted(curated_ids),
        "declared_facilitators": sorted(declared_ids),
        "base_relayers_lower": sorted(norm_base),
        "solana_relayers": sorted(norm_sol),
        # canonical per-chain map used for x402 scoping (supersedes the two
        # legacy keys above, which are kept for back-compat).
        "relayers_by_chain": {k: sorted(v) for k, v in sorted(by_chain.items())},
        "counts": {
            "facilitators": len(registry),
            "base_relayers": len(norm_base),
            "solana_relayers": len(norm_sol),
            "by_chain": {k: len(v) for k, v in sorted(by_chain.items())},
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))
    print(f"wrote {OUT}")
    print(json.dumps(out["counts"], indent=2))
    # sanity: flag any base address that isn't a well-formed 0x40hex
    bad = [a for a in norm_base if not re.fullmatch(r"0x[0-9a-f]{40}", a)]
    if bad:
        print("WARNING malformed base addresses:", bad)


if __name__ == "__main__":
    main()

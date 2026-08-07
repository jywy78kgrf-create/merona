# Trust score revisions

*The scores are deterministic and reproducible per row (snapshot sha +
flags sha + recorded detector params). This file is the loud record of what
changed between score versions and why — published, not buried, same policy
as the clean-volume series.*

## 2026-08-01 — SCORE_VERSION 2 → 3

Trigger: the Polygon payment ring (see `docs/WASH_V3_SPEC.md` and the July
reconciliation report). A depth-3 carousel held a C grade while
manufacturing ~100% of Polygon's clean volume — the score's integrity
inputs were all depth ≤ 2, and the ring's design *earned* points from the
diversity curve (30 Sybil payers) while honest small sellers were floored
at F. Four changes:

1. **Cycle input.** `wash_cycles` (SCC + net-flow balance, depth-unbounded)
   joins the score: −40 penalty and the same D-at-best cap as other wash
   flags. Detector parameters are recorded in every affected row.
2. **`signals_live` per row.** Every score row now records which wash
   inputs actually ran the night it was computed (`wash_signals` date,
   `recip_scan_ok`, cycles date).
3. **Diversity split into breadth (0–25) + loyalty (0–10).** Repeat depth
   (tx per payer, payers ≥ 2) now earns points, correcting v2's inversion:
   small sellers with genuine repeat customers were over-punished while
   Sybil breadth was rewarded.
4. **A-gate.** Score ≥ 90 additionally requires census tier T2+ evidence
   (settled, service-verified origin, `service_directory_v0.json`) for one
   of the seller's listed origins; otherwise the score caps at 89 with the
   gate decision recorded. An A is a claim about verified commerce, not
   about clean-looking flow.

## Disclosure — v2 rows with a silently missing input

The nightly reciprocal self-join outgrew the database statement timeout at
an unknown date before 2026-08-01 and failed silently: on affected nights
every seller scored with `reciprocal=false` regardless of reality, and
nothing in the row recorded the difference between "scanned clean" and
"scan died". The failure was found and fixed on 2026-08-01 (timeout lifted
inside the wash pass + transaction rollback on failure).

Consequences, stated plainly:

- `seller_scores` rows with `score_version = 2` may have been computed
  without the reciprocal signal. The affected night set is not recoverable
  from the data (that is precisely the defect), so the rows are left as the
  historical record rather than speculatively recomputed.
- v3 rows supersede them nightly and carry `signals_live`, so this class of
  silent gap cannot recur unrecorded: `recip_scan_ok` is now written per
  night, and a future scan failure will be visible in every row it touches.

## Endpoint grades v4 (2026-08-05) — day-based rates, probe escalation, letter gate

Found via a real case: an origin listing 297 endpoints — with 25 distinct
payers and ~$52 of genuine on-chain settlements — carried `C,
valid_402_rate 0.0` for three weeks. Cause: the prober GETs one
catalog-ordered path per origin, that origin's first path requires query
parameters (bare GET → 400, never 402), and probe-weighted rates let a
single unrepresentative path zero the 402 signal for the whole origin.
The grade was our artifact, not their defect.

Three changes, versioned as endpoint `score_version = 4`:

- **Probe escalation** (`LIVENESS_402_FALLBACKS`, default 2): when the
  representative path answers but not with a valid 402, up to two more
  listed paths (middle and last of the origin's catalog listing —
  deterministic) are tried seeking one. Down origins do not escalate;
  origins that 402 on the first probe cost exactly what they did before.
- **Day-based rates**: uptime and valid-402 are now the fraction of
  observed *days* on which any probe succeeded, not the fraction of
  probes. Probe-weighted rates would have punished origins for our own
  escalation attempts.
- **Letter tiers** (amended same day): grades are tiered by evidence
  rather than binary. Below `MIN_LETTER_DAYS` (default 7) distinct probe
  days the grade is NULL — "unrated", with the observation count. From 7
  days a letter is published marked **provisional** — a week of nightly
  probes demonstrates active, continuous service. From `MIN_MATURE_DAYS`
  (default 14) the letter is **verified**. The numeric score is always
  computed and recorded, so maturation reveals a grade rather than
  recomputing one. Same UNKNOWN-over-invented rule the agent scores have
  always followed.

v2/v3 endpoint rows remain as the historical record. Publication of grade
surfaces beyond the per-query API gates on this revision.

---

## Suggested action, rule v1 — 2026-08-06

Seller responses (`/v1/trust/{chain}/{address}` and the `trust_score` MCP
tool) now carry a `suggested_action` block:

```json
"suggested_action": {
  "action": "INVESTIGATE",
  "rule_version": 1,
  "because": ["wash signals: cycle_flag", "grade B"],
  "note": "Suggested action for the caller's own funds, derived by a published
           deterministic rule from the evidence in this response. Not a claim
           about the seller."
}
```

**What it is.** A deterministic mapping from evidence already present in the
same response to an action the caller can act on without first writing a
policy that decides what a `B` means. Nothing new is measured; no new data
enters. It is a stated reading of published figures.

**What it is not.** It is not a claim about the seller. Every value is advice
to the caller about the caller's own funds, which is why the strongest
negative is `DECLINE` (do not send *your* money) and not a verdict of guilt.
That keeps the field inside the editorial policy on
[merona.io/editorial](https://merona.io/editorial): recorded facts, never
accusations.

**Rule v1**, first match wins:

| Condition | Action |
|---|---|
| Hand-verified adverse finding on record | `DECLINE` |
| No score for this address in the witnessed index | `INSUFFICIENT_EVIDENCE` |
| Grade D or F on a full observation window | `DECLINE` |
| Any wash signal (`cycle_flag`, `history_flag`, `nightly_flag`), **or** a provisional D/F | `INVESTIGATE` |
| Provisional score, or grade C | `CAUTION` |
| Grade A or B, full window, no wash signals | `PROCEED` |

**`INSUFFICIENT_EVIDENCE` is a first-class answer**, returned on the 404 path
as well as the 200 path. An address we have not observed long enough is not an
address we are calling bad. Collapsing "no data" into "unsafe" is the standard
failure of endpoint-checking services and it penalises every honest newcomer;
the tiering that prevents it is the same evidence gate used for letter grades.

**Provisional D/F resolves to `INVESTIGATE`, not `DECLINE`.** Thin evidence is
not grounds to tell someone to walk away, but a poor early grade is more than
ordinary caution — the caller is pointed at the evidence instead.

**Stability.** `rule_version` ships in every response. Any threshold change
bumps it and gets a dated entry here; `indexer/tests/test_suggested_action.py`
pins each branch and the version together, so a silent drift fails the suite.
Callers who want none of this can ignore the field and read `grade`,
`components` and `adverse_findings` directly — the inputs are all still there.

---

## Seller scores v4 — letters mean conduct, not size — 2026-08-06

**What the audit found.** On 2026-08-06 the production index held 131,959
sellers graded F. Exactly **201** of them carried wash evidence. The other
131,758 — 99.85% — had no adverse evidence of any kind; they were merely
small or new. Zero sellers had ever earned an A, and eight held a B, because
the v3 rubric compresses clean sellers into a ~35–50 score band (calibration:
p25 36 / p50 41 / p75 42 / p90 49 across 2,647 gated clean sellers) while the
letter map started C at 60. The letter carried no information: an F shared by
99.5% of the population stops no payments, and the 201 wallets the F exists
for were indistinguishable from honest newcomers.

**The v4 principle: bad evidence sinks you; absent evidence caps you; only
conduct earns an F.**

Letters only — the numeric score, its components and its penalties are
unchanged and remain comparable across versions.

1. **Letter gate.** A clean seller below **25 settlements or 5 distinct
   payers** publishes **no letter** (grade NULL, `tier: "unrated"` in the
   API). The score is still computed and recorded nightly; crossing the gate
   reveals the letter — the same maturation design as endpoint grades v4.
2. **Conduct anchors, which ignore the gate.** A live wash signal (cycle
   flag, funded-ratio flag, reciprocal pair) or self-pay ≥ 25% of volume
   → **F**. A history-only flag with no live signal → **D**. A wash-flagged
   wallet cannot hide behind being small.
3. **Bands re-anchored to the observed distribution.** B ≥ 47 (≈ top decile
   of clean gated sellers), C ≥ 36 (≈ p25 — the broad clean middle), D below
   (clean bottom band, with the reason recorded in components). **A requires
   score ≥ 55 AND census T2+ evidence** — verified commerce, not
   clean-looking flow. An A is now rare but genuinely reachable; under v3 it
   required a score the rubric cannot produce for real sellers.

**API surface.** Seller responses now carry `tier`
(`unrated | provisional | verified`) mirroring endpoint grades. Unrated
responses state explicitly that the missing letter is an evidence gate, not
an adverse signal, and `suggested_action` resolves them to
`INSUFFICIENT_EVIDENCE` with that reason — never to DECLINE. Hand-verified
adverse findings continue to override any letter downward, never upward.

**Expected effect** (from the calibration distribution): the ~130K unrated
mass leaves the letter population entirely; F shrinks to conduct-flagged
wallets (~200 today); the ~2,600 clean gated sellers distribute roughly
10–15% B, ~60% C, ~25% D; A awaits the first seller with a ≥55 score on a
census-verified origin. Verify against the first post-deploy nightly before
publishing any grade surface.

Pinned by `indexer/tests/test_seller_grade_v4.py` branch by branch; the v3
history stays in the table under `score_version = 3`.


## v4.1 — A requires a customer base — 2026-08-07

First post-v4 cycle minted 58 A's. Cohort audit found several resting on
6–9 lifetime distinct payers (score carried by endpoint/domain points). An
A now additionally requires ≥ 50 distinct payers (`SELLER_A_MIN_PAYERS`);
below the floor the same seller grades B. Rule pinned in
test_seller_grade_v4.py; expected effect ≈ a third of the first cohort
moves A→B, leaving ~40 A's each backed by a real payer base.

## v4.2 — concentration cap on A — 2026-08-07

Cohort audit of the first A's: one seller (a real stock-price API) held an
A on 10.07M settlements of which **99.6% came from a single payer** — 1,100
nominal payers cleared the v4.1 floor while the volume was a monoculture.
The snapshot now records `top_payer_tx` per seller; top-payer share ≥ 90%
(`SELLER_A_MAX_TOP_SHARE`) caps the letter at B with
`letter: concentration_capped` and the share published in components.
Recorded facts, not an accusation: the service is real; the breadth its A
claimed was not.

## Clean-metrics scope — own payTo excluded — 2026-08-07

Not a score change; a conflict-of-interest guard on the clean series,
recorded here under the same publish-loudly policy. merona's own payment
address (`0x31ee6253e34df1ab3a45e00ffa9f5dc2be1040b6`, the published payTo
for the paid trust endpoint) is now excluded from clean metrics **as a
seller**, on the same footing as registry relayer addresses
(`indexer/wash.py`, `X402_OWN_PAYTO`, noted in every `coverage_note`).

Rationale: with on-chain anchor attestations and a paid x402 endpoint of
our own, some inbound settlements will name us as seller. However small,
an index must not count its own receipts inside the number it publishes —
the exclusion makes that structural rather than a promise. The companion
policy (attest/README.md #3) is the outbound half: merona never generates
transactions that resemble commerce toward its own payTo.

Expected effect on the clean series: none today (the address has no
recorded settlements as seller); the guard exists for the day that stops
being true.

## suggested_action rule v2 — paid-probe delivery evidence — 2026-08-07

Trigger: the first delivery sweep (attest/paysweep.py) — merona paid every
discoverable Base seller's cheapest listed endpoint once (502 targets,
$1.72 total). Outcomes, receipt-backed: 150 delivered, 201 no live
paywall, 118 could not complete a sale, 12 **settled the payment on-chain
and then refused the request** (charge-before-validate defects: 400s on
missing params, 405s on method, 500s — each row carries the settlement tx
and the delivered-content hash).

Seller responses now carry a `delivery` field when merona has bought from
the address: `delivery_verified` (dated, with probe URL) or
`charged_unserved` (dated, with settlement tx and HTTP status). Latest
settled receipt wins, so a seller who fixes their endpoint clears the flag
on the next paid probe. Framed as a recorded fact about one request —
never an intent claim — and it stays off-chain per attest/README.md
policy #2 (adverse findings are argued with, not timestamped forever).

Rule change (v1 → v2): `charged_unserved` floors the action at
INVESTIGATE — it lifts PROCEED/CAUTION/INSUFFICIENT_EVIDENCE, never
softens DECLINE, and applies even to unscored addresses (unlike absence
of history, a settled-but-unserved charge is something that happened).
`delivery_verified` adds a dated reason but never upgrades the action —
delivering once is worth recording, not worth overriding the grade.
Pinned in test_suggested_action.py.

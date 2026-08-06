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

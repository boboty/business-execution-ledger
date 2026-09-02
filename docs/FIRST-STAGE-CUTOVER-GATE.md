# FIRST-STAGE CUTOVER GATE

The FINAL first-stage cutover readiness gate for BEL.

This is **not** a new business feature. It answers only one question:

> Is THIS PostgreSQL database, built from THIS private cutover source and
> business-confirmed Cutover Baseline, ready for BEL to be declared the
> System of Record?

The Gate **judges an already-prepared target database**. It never performs
the cutover switch, never mutates a `system_of_record` flag, never demotes
Excel, never invents baseline data, never repairs discrepancies, never
auto-resolves Tasks, and never runs backfill as part of the judgment.

Expected sequence *outside* the Gate:

```
fresh PostgreSQL
    -> alembic upgrade head
    -> approved backfill plan executed
    -> human/business corrections as needed
    -> FIRST-STAGE CUTOVER GATE
    -> PASS
    -> human decision to declare BEL System of Record
```

---

## 1. Frozen Gate contract — the seven mandatory dimensions

Gate PASS requires **ALL** mandatory dimensions PASS. There is no weighted
score, no "mostly ready", and no warning can compensate for a failed
mandatory dimension.

| Dimension | Meaning | PASS requires |
|---|---|---|
| **A. `runtime_schema`** | Runtime / schema | Effective runtime is the canonical **`postgresql+psycopg://`** (PostgreSQL, psycopg3); schema current revision **== Alembic head**. No startup migration, no auto-upgrade. **SQLite → FAIL. Non-canonical PostgreSQL driver (`postgresql://`, `+psycopg2`, `+asyncpg`) → FAIL. Schema mismatch → FAIL.** |
| **B. `cutover_inputs`** | Backfill / baseline readiness | `<period>/backfill-plan.json` **and** `<period>/expected/cutover-baseline.json` are both present. The Cutover Baseline is business-confirmed acceptance material — **never** synthesized, **never** inferred from current BEL data, **never** inferred from raw Excel. Missing → FAIL (`BACKFILL_PLAN_MISSING` / `BASELINE_MISSING`). No silent substitution of another period. |
| **C. `reconciliation`** | Private reconciliation | The canonical `bel.application.cutover_reconciliation.reconcile` against current target PostgreSQL state **and** the business-confirmed `cutover-baseline.json` passes: `reconciliation.passed == True` **and** `unresolved_count == 0`. Any `UNRESOLVED` → FAIL. `MATCH` and `BEL_CORRECTED_LEGACY` are both legitimate final outcomes. Raw legacy Excel equality is **not** required. |
| **D. `work_surfaces`** | Required first-stage surfaces | All five Application projections execute successfully on the same target DB: Contract Business Ledger, Contract 360 (existing canonical path), Period Close Workbench (selected period), Invoice Preparation Workbench, Exception & Task Center (selected period). Canonical traceability retained; no business-state write. A nonzero row is **not** required — empty is truthful. |
| **E. `data_products`** | Required Data Products | All four first-stage Data Products generate on the same target DB, **CSV and XLSX**, each **twice from the same state/filter**, and **byte-identical** across the two runs: Contract Business Ledger, Period Close, Invoice Preparation, Exception & Task. Uses the **existing** canonical builders/serializers — no new export implementation. Nothing saved into the repository. |
| **F. `privacy_boundary`** | Privacy | `BEL_PRIVATE_DATA_ROOT` set and resolving to a real directory **outside the repository**; period is strict `YYYY-MM`; the period directory resolves **inside** the private root with no symlink/path escape. |
| **G. `read_only`** | Read-only judgment | Zero business-state writes: a full-schema fingerprint taken before and after Gate execution is unchanged (no new Fact, TaskException, status/MatchCase transition, allocation, accrual, correction, relationship, or blocker snapshot). The only file writes are the private diagnostic reports under `$BEL_PRIVATE_DATA_ROOT/reports/`. |

## 2. PostgreSQL only — canonical `postgresql+psycopg://` runtime

The real first-stage Gate **must** run against the canonical BEL runtime
URL: `postgresql+psycopg://` (PostgreSQL with the psycopg3 driver) under
`BEL_DATABASE_URL`. SQLite is rejected for the real Gate (SQLite remains a
test-only convenience for the historical `tests/private_acceptance/runner.py`,
which is left untouched and is **not** the final SoR cutover gate).

The Gate verifies its runtime from engine metadata (never the URL's
credentials — no rewrite, no driver fallback):

- effective drivername == `postgresql+psycopg` AND
  `dialect.name == "postgresql"` AND `dialect.driver == "psycopg"`
- schema current revision == Alembic head

The bare `postgresql://` form (defaults to psycopg2),
`postgresql+psycopg2://`, `postgresql+asyncpg://`, and SQLite are all
rejected (`NON_CANONICAL_DATABASE_DRIVER` / `NON_POSTGRESQL_DIALECT`).

No startup migration and no auto-upgrade: **schema mismatch → FAIL** and
**every non-canonical runtime → FAIL**, each reported as a `runtime_schema`
failure with a private diagnostic — never silently fixed.

## 3. Application seam / CLI

- Application seam: `bel.application.first_stage_cutover_gate`
  (`run_first_stage_cutover_gate`) with the neutral result DTO
  `FirstStageCutoverGateResult` — each dimension is exactly `PASS`/`FAIL`,
  plus `reason_codes`, `passed`, `report_written`, and a `diagnostics`
  dict that is **private** (report-only, never printed).
- CLI: `bel cutover gate --period YYYY-MM` (under the existing `cutover`
  command group).

Public stdout is exactly one safe verdict line:

```
FIRST_STAGE_CUTOVER_GATE: PASS
```

or

```
FIRST_STAGE_CUTOVER_GATE: FAIL
```

Nothing else containing private-derived names, ids, counts, amounts,
discrepancies, or paths to sensitive files is ever printed. Full
diagnostics — which may contain private-derived values — are written only
under `$BEL_PRIVATE_DATA_ROOT/reports/`.

`cutover gate` is the one command that bypasses the generic CLI startup
schema rejection. The bypass is **two-level Click-aware**, not a global
disable:

- the ROOT `cli` group enforces the startup schema gate for every
  top-level command **except** the `cutover` group (at the root,
  `ctx.invoked_subcommand` is the first-level command name — `"cutover"`
  — never the nested `"gate"`); and
- the `cutover` group enforces the startup schema gate for every cutover
  subcommand **except** `gate` (there, `ctx.invoked_subcommand` is the
  nested command name).

So an ordinary command and `cutover backfill`/`cutover reconcile` keep the
hard startup schema rejection, while `cutover gate` is never intercepted:
it is strictly read-only, and its whole purpose is to REPORT a schema
mismatch as a FAIL dimension (with the private diagnostic) rather than
being blocked before it can judge. It performs its **own** canonical
runtime + schema-at-head verification and can never PASS a mismatched
schema.

## 4. Private root / period boundary

The Gate reuses the already-hardened private-root / period containment
policy (shared helper module `bel.infrastructure.private_paths`, the same
discipline `tests/private_acceptance/runner.py` enforces):

- `BEL_PRIVATE_DATA_ROOT` must be set (or supplied explicitly).
- The root must resolve to a real directory **outside the repository**.
- The period is a strict `YYYY-MM` identifier — never an arbitrary path
  string. `..`, an absolute path, and a same-looking symlinked period
  directory resolving outside the root are all rejected.
- Required private **input** files are read through a
  **descriptor-anchored** walk (`PrivatePeriodReader` in the shared helper
  module `bel.infrastructure.private_paths`), NOT by pathname reopen:
  once the private root is accepted it is opened ONCE as a directory
  descriptor and the period directory is opened relative to it; every
  input component is opened only relative to an already-open descriptor
  via `dir_fd` with `O_NOFOLLOW`. NO symlink is allowed in ANY component
  (the period directory, an `expected/` directory, or a control file) —
  even one resolving back inside the root — and NO path is reopened by
  name after validation. A `resolve`-then-reopen TOCTOU (an intermediate
  directory renamed/replaced with a symlink or redirected tree) cannot
  redirect the read: the descriptor chain is the authority. Each final
  file must be a **regular file** (`fstat` `S_ISREG`; a FIFO/socket/device
  is rejected by type, never blocked on) under a defensive size ceiling.
  Failures map to `PRIVATE_INPUT_ESCAPE` (symlink / escape),
  `PRIVATE_INPUT_MISSING`, `PRIVATE_INPUT_UNSAFE_TYPE`, and
  `PRIVATE_INPUT_TOO_LARGE`.
- Reports are written only under `$BEL_PRIVATE_DATA_ROOT/reports/` through
  a checked `O_DIRECTORY` + `O_NOFOLLOW` descriptor and a `O_NOFOLLOW`
  final component, so a reports-directory or report-file symlink swap can
  never redirect diagnostics into the repository or elsewhere.

Existing R5 security is never weakened.

## 5. Required cutover inputs

For the selected period the Gate requires at least:

- `<period>/backfill-plan.json`
- `<period>/expected/cutover-baseline.json`

The **Cutover Baseline** is business-confirmed acceptance material. It is
NOT a Fact input, NOT an Evidence source, and NOT something BEL may
generate from its own output. If the baseline is missing → FAIL
(`BASELINE_MISSING`); if the backfill plan is missing → FAIL
(`BACKFILL_PLAN_MISSING`); if either file is a symlink, or any component
on its path is a symlink or resolves outside the private root / inside
the repository → FAIL (`PRIVATE_INPUT_ESCAPE`); a non-regular input type
or an oversized file is likewise FAIL (`PRIVATE_INPUT_UNSAFE_TYPE` /
`PRIVATE_INPUT_TOO_LARGE`). Neither input is synthesized or inferred, and
the external content of an escaping file is never parsed (the baseline
JSON is decoded only from the descriptor-anchored bytes). The Gate never
silently substitutes another period.

## 6. The Gate does not run backfill

Hard boundary: `bel cutover gate` must **not** execute
`run_backfill_plan(...)`, import source data, create Facts, create
TaskExceptions, or apply corrections. The target PostgreSQL database must
already contain the proposed cutover state (via the approved backfill
commands executed *before* the Gate). A gate must not modify the thing it
is judging.

## 7. Private cutover reconciliation

The Gate reuses the canonical
`bel.application.cutover_reconciliation.reconcile` against current TARGET
PostgreSQL state **and** the business-confirmed `cutover-baseline.json`.
There is no second reconciliation implementation.

PASS requires `reconciliation.passed == True` and `unresolved_count == 0`.
Any `UNRESOLVED` → Gate FAIL. `MATCH` and `BEL_CORRECTED_LEGACY` are both
legitimate final reconciliation outcomes per existing semantics. Raw
legacy Excel equality is **not** required.

## 8. Backfill unresolved work

There is no unresolved cutover/backfill work that can coexist with a PASS.
The canonical reconciliation already treats **every OPEN backfill-produced
`TaskException`** (`BackfillIdentityIncomplete` /
`BackfillIdentityAmbiguous` / `BackfillConflict`) as an unconditional
`UNRESOLVED` entry — including one that cannot be mapped to a specific
Contract. The Gate reuses that; it does not duplicate the logic.

The Gate does **not** require the entire Exception Center to be empty.
Ordinary operational unresolved work may legitimately exist while BEL is
the System of Record. The Gate requirement is **unresolved CUTOVER
discrepancy = 0**, not **all business work in BEL = resolved**. SoR cutover
is not "zero tasks anywhere".

## 9. Required business surfaces — same PostgreSQL DB

Against the SAME target PostgreSQL state, the Gate proves these Application
projections are operational:

1. Contract Business Ledger (`get_contract_business_ledger`)
2. Contract 360 (`get_contract_360`) — at least the existing canonical path
   remains operational (exercised on the first deterministic contract when
   one exists; a genuinely empty database is truthful)
3. Period Close Workbench for the selected period
   (`get_period_close_workbench`)
4. Invoice Preparation Workbench (`get_invoice_preparation_workbench`)
5. Exception & Task Center for the selected period
   (`get_unresolved_work_center`)

Existing Application paths are used — business logic is never reproduced
inside the Gate. "Operational" means the projection executes successfully,
canonical traceability is retained, and no business-state write occurs.
The Gate does **not** require each surface to contain a nonzero row; empty
can be truthful. No private counts are published on stdout.

## 10. Required Data Products — same PostgreSQL DB

Against the same target database the Gate verifies all first-stage Data
Products can be generated, CSV **and** XLSX:

1. Contract Business Ledger — CSV, XLSX
2. Period Close — CSV, XLSX
3. Invoice Preparation — CSV, XLSX
4. Exception & Task — CSV, XLSX

Existing canonical builders/serializers are used — no new export
implementation. For each format, the product is generated **twice from the
same state/filter** and the result must be **byte-identical**. For XLSX,
byte identity includes the **package metadata**, not just cell values: all
four first-stage XLSX serializers share one canonical deterministic-XLSX
normalizer (`bel.infrastructure.deterministic_xlsx`) that pins every ZIP
entry `date_time` and both `docProps/core.xml` created/modified fields —
two exports of identical state are byte-identical across any wall-clock /
ZIP-timestamp boundary. Artifacts are never saved into the repository
(temporary memory, or the private reports location when persistence is
needed); the Gate result does not archive every exported byte.

## 11. Read-only Gate

Except for writing the PRIVATE diagnostic report files, Gate execution is
strictly read-only against BEL business state. A before/after full-schema
fingerprint (or equivalent database assertions) proves no new Fact, no new
TaskException, no status transition, no MatchCase transition, no
allocation, no accrual, no correction, no relationship, and no blocker
snapshot. The Gate itself never commits a business mutation.

## 12. Required fact flows

The Gate does **not** invent a new "fact completeness score". Already
implemented first-stage capabilities are the evidence that the required
fact flows are operational: the target database supports/projects the
canonical V1 facts needed by Contract Ledger, Period Close, Invoice
Preparation, and the Exception Center. Absence of a Fact is **not**
automatically a Gate failure — unless the Cutover Baseline /
reconciliation says it is required, or an existing deterministic Gate
prerequisite cannot be evaluated. No guessed completeness.

## 13. First-stage cutover result

The neutral result has explicit dimensions — `runtime_schema`,
`cutover_inputs`, `reconciliation`, `work_surfaces`, `data_products`,
`privacy_boundary`, `read_only` — each `PASS` / `FAIL`. Overall PASS only
if all mandatory dimensions PASS. There is **no** `READY_WITH_WARNINGS`,
no score, no percentage. Warnings may exist separately but cannot override
a FAIL.

A dimension that cannot be evaluated because an earlier mandatory
dimension failed (e.g. `reconciliation` when `cutover_inputs` is missing,
or the DB-dependent dimensions when `runtime_schema` fails on SQLite) is
reported **FAIL**, never a "not run" PASS — there is no "mostly ready".
The blocking dimension's own reason code explains the cascade. (`read_only`
stays PASS when no DB operation ran at all: the Gate made no business
write by construction.)

## 14. No automatic System-of-Record switch

A Gate PASS means **BEL MAY be declared System of Record**. It does not
mean the command itself performs that declaration. The Gate does not
change database state, rename legacy Excel, modify source files, set a
config flag, update ROADMAP automatically, or write "BEL is SoR" into docs
merely because synthetic tests pass. The final SoR declaration is a
human/business acceptance step after the REAL private Gate PASS.

## 15. Private report

The private report contains enough diagnostics to fix failures and may
contain private-derived values because it is written only under
`$BEL_PRIVATE_DATA_ROOT/reports/`. It includes:

- gate period
- candidate application/version/SHA if safely available
- dimension statuses
- failure reason codes
- reconciliation diagnostic (including `unresolved_count`, per-key
  outcomes, and OPEN backfill task keys)
- surface / export failures

The report name is deterministic:
`first-stage-cutover-gate-<YYYY-MM>.json`. Private contents are never
duplicated to stdout or into the repository. The write follows the
hardened path/symlink containment rules (section 4). No generated report is
ever committed.

## 16. Operational runbook

Prerequisites:

1. A **PostgreSQL** runtime database (`BEL_DATABASE_URL` =
   `postgresql+psycopg://...`).
2. Schema at head: `alembic upgrade head`.
3. The approved backfill plan executed for the period (via
   `bel cutover backfill --period YYYY-MM`).
4. Human/business corrections applied as needed (via existing commands).
5. Private cutover acceptance material in place under
   `BEL_PRIVATE_DATA_ROOT`:
   - `<period>/backfill-plan.json`
   - `<period>/expected/cutover-baseline.json` (business-confirmed)

Run:

```bash
BEL_PRIVATE_DATA_ROOT=/path/to/private/data \
BEL_DATABASE_URL=postgresql+psycopg://user:password@host:5432/bel \
bel cutover gate --period YYYY-MM
```

Read the verdict on stdout. Read the full private diagnostics at
`$BEL_PRIVATE_DATA_ROOT/reports/first-stage-cutover-gate-YYYY-MM.json`.

## 17. Human cutover declaration

`FIRST_STAGE_CUTOVER_GATE: PASS` means the database is **ready** to be
declared the System of Record. The declaration itself is a **human /
business acceptance step** performed after the REAL private Gate PASS — it
is never performed by the Gate command.

## 18. Historical private runner vs. this Gate

`tests/private_acceptance/runner.py` is the historical/private scenario
acceptance harness and may continue to use SQLite as a test harness. It is
**not** the final SoR cutover gate, and the final Gate does not inherit its
SQLite-only behavior. Shared safe path/report helpers are extracted
carefully; the two are deliberately distinct.

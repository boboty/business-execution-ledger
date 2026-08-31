# Phase 2D.2 Decisions — Period Close Business Data Product

Judgment calls made while implementing Phase 2D.2. The phase goal, per
its spec, is frozen: given a period, BEL generates a deliverable,
traceable, reproducible Period Close Business Data Product from the
CURRENT authoritative Facts and the existing deterministic close rules.
`docs/DOMAIN.md`, `docs/RULES.md`, and `period_close.py` (the Rule
Engine) are untouched — this phase is Application-layer projection +
serialization + two thin transport layers (Web, CLI), nothing else.

## One Application Data Product path

```
get_period_close_workbench(session, period)      # unchanged (Phase 2C)
    -> build_period_close_data_product(workbench)  # new: neutral DTO
    -> export_period_close_xlsx(product)            # new: XLSX bytes
    -> export_period_close_csv(product)              # new: CSV bytes
```

All in `src/bel/application/period_close_export.py`. Web
(`src/bel/web/routes.py`) and CLI (`src/bel/cli.py`,
`period-close export`) both call exactly this path — neither
re-implements any business computation. This mirrors the Phase 2D.1-R4
Contract Business Ledger export
(`src/bel/application/contract_ledger_export.py`), including the
formula-injection guard convention (`_safe_text`/`_xlsx_cell`, a
dangerous-leading-character gets a literal-text `'` prefix) and the
UTF-8-with-BOM CSV convention.

## Why a flat `PeriodCloseExportRow`, not five separate dataclasses

The spec asks for one unified CSV schema (section 5) reused, sheet by
sheet, in the XLSX. Keeping one row shape with all fields nullable —
rather than five separate per-record-type dataclasses — means the CSV
writer and the six XLSX sheets read the exact same object with no
per-format mapping table to keep in sync. Each XLSX decision sheet then
selects only the columns relevant to it (`_ACCRUAL_REQUIRED_COLUMNS`
etc.) — the spec's "do not force irrelevant columns onto every sheet"
requirement — while the CSV emits every column on every row, leaving
inapplicable fields blank. A field that is genuinely not part of a
decision type (e.g. `source_item_key` on a `CONTRACT_LEVEL_CANDIDATE`
row, which is contract-level by construction) is never fabricated —
`test_csv_unified_long_table_all_record_types` in
`tests/unit/test_period_close_export.py` asserts this stays blank, not
some placeholder.

## `WorkbenchBlocker.counterparty` — a projection extension, not a new rule

`PeriodCloseWorkbench`'s reversal/accrual/candidate/difference rows all
already carried `counterparty` (resolved from the same `contracts` dict
`get_period_close_workbench()` builds internally); `WorkbenchBlocker`
did not. The Data Product's Blocker sheet needs a `counterparty` column
like every other sheet, and the spec explicitly forbids the XLSX/CSV
serializer independently re-querying raw repositories to rebuild a
second trace/label model. The one-field addition to `WorkbenchBlocker` in
`period_close_workbench.py` — populated from the SAME
`_contract_and_item()` helper the other four decision types already use —
is the smallest change that keeps "one Application projection, read once"
true. `WorkbenchBlocker` has exactly one construction site
(`get_period_close_workbench()` itself), so this is a safe additive
change; `tests/web/test_web_period_close.py`'s existing blocker
assertions are unaffected because the Web viewmodel reads it by
attribute, not position.

## Evidence trace vs. blocker context: two different things, two columns

The spec explicitly forbids exporting raw Evidence payloads and forbids
fabricating provenance when none exists, while requiring fact
kind + source document identity + fragment location where available.
`_trace_text()` renders each `FactNode` already composed by
`get_period_close_workbench()`'s `_TraceBuilder` as
`KIND[field=value;...]|doc=<file_name>|loc=<sheet>:<row>` (or a
JSON-encoded `locator_json` for non-Excel fragment kinds), joined with
`->` in the trace's existing deterministic order. This is read-only
rendering of data the Workbench already assembled — no new repository
call, no new judgment. It is used ONLY for decision rows, which have a
genuine Decision -> Fact -> Evidence chain.

`CloseBlocker` has no `FactNode` trace (blockers are diagnostics, not
Decisions with a Fact chain in the same shape). `BlockerContext` is
*current business context used to explain a blocker* — not Evidence
provenance — so it must not be labeled as one. The export row therefore
carries two separate nullable fields: decision rows keep their genuine
`evidence_trace` (with `blocker_context` left `None`), while BLOCKER
rows render the existing `BlockerContext` fields via
`_blocker_context_text()` into `blocker_context` (same field order the
Web page's blocker cards already read, skipping any field that is
`None`/empty rather than emitting a placeholder) with `evidence_trace`
left `None`. No Fact -> Evidence chain is invented for blockers, and the
unified CSV schema and the XLSX `06_Blockers` sheet expose the context
under its own `blocker_context` column.

## XLSX summary sheet: only fields that exist as Facts

`01_Summary` states `period`, `period_end`, and the five decision-type
counts already on `PeriodCloseWorkbench.summary` — the exact same dict
`period_close.py`'s `PeriodClosePreview.summary` produces. No `CLOSED`/
`POSTED`/`APPROVED`-style invented status is ever written (spec 4.1's
HARD rule); `tests/unit/test_period_close_export.py::
test_xlsx_decision_sheets_no_business_status_invented` asserts none of
those three literals appear anywhere in the workbook. There is currently
no Fact that would make such a status meaningful, and inventing one here
would flatten the Fact / Current State / Projected Decision / Blocker
boundary the phase spec explicitly protects.

## No `generated_at` timestamp

Reproducibility per the spec means *semantic* content is identical for
the same Facts + period, not byte-identical files. Since XLSX has
library-level metadata volatility, the acceptance comparison is always
parsed-content equality; a wall-clock `generated_at` field would only
ever add nondeterminism with no demonstrated consumer need, so it is
omitted, per spec section 8's explicit instruction.

## CLI `--format`/`--output` are both required, no stdout binary

`bel period-close export <period> --format {xlsx,csv} --output <path>`
mirrors the spec's frozen contract literally. Neither option has a
default — an accidental omission must fail loudly (Click's own
"missing option" error) rather than silently guessing a format or
writing next to the CLI's cwd. The command never writes the serialized
bytes to stdout; only a one-line confirmation
(`Period Close Data Product written: <path> (<format>)`), matching the
spec's "do not emit binary XLSX to stdout" rule.

## Web routes reuse the page's existing period-validation

`GET /period-close/export.xlsx` and `.../export.csv` both call the same
`_checked_period()` helper the `/period-close` page route already uses
(400 on a malformed period, default-to-latest-known-period when omitted)
— "the same period validation semantics as the existing page" from the
spec, read literally rather than re-implemented.

## Out of scope, reaffirmed

No new close rule, no persisted close result/snapshot/export-history
row, no accounting vocabulary, no schema change. `WorkbenchBlocker`'s
one added field is a projection extension of an existing read-only DTO,
not a new table or a new Rule Engine concept.

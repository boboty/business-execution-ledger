# Phase 2D.2 Acceptance — Period Close Business Data Product

Public acceptance criteria for Phase 2D.2. Builds on
`docs/PHASE2C2-ACCEPTANCE.md` and `docs/PHASE2D1-R0-ACCEPTANCE.md`
(unchanged) — this phase adds one Application Data Product path, two
Web export routes, and one CLI export command, all strictly read-only.

## How to run

```bash
.venv/bin/pytest                                                    # full public suite
.venv/bin/pytest tests/unit/test_period_close_export.py -q          # Data Product DTO + serializers
.venv/bin/pytest tests/web/test_web_period_close_export.py -q       # Web export routes
.venv/bin/pytest tests/integration/test_period_close_export_cli.py -q  # CLI export command
# PostgreSQL gate — destructive tests need the disposable test-database
# contract (tests/pg_disposable.py); BEL_DATABASE_URL alone never runs them:
BEL_TEST_DATABASE_URL=postgresql+psycopg://.../disposable_test_db \
BEL_TEST_DATABASE_DISPOSABLE=1 \
BEL_DATABASE_URL=postgresql+psycopg://... .venv/bin/pytest -q
python tools/privacy_scan.py --staged
python tools/check_migration_immutability.py --staged
```

## Manual / browser acceptance (read-only against real data)

```bash
BEL_DATABASE_URL=postgresql+psycopg://... bel web                   # 127.0.0.1:8000
# open http://127.0.0.1:8000/period-close?period=<period>
# click "下载 XLSX" / "下载 CSV", or:
curl -o period-close-<period>.xlsx "http://127.0.0.1:8000/period-close/export.xlsx?period=<period>"
curl -o period-close-<period>.csv  "http://127.0.0.1:8000/period-close/export.csv?period=<period>"
```

```bash
bel period-close export <period> --format xlsx --output period-close-<period>.xlsx
bel period-close export <period> --format csv  --output period-close-<period>.csv
```

Checklist:

1. The workbook opens in Excel/LibreOffice/openpyxl with exactly six
   sheets, in order: `01_Summary`, `02_Accrual_Required`,
   `03_Prior_Accrual_Reversal`, `04_Actual_Difference`,
   `05_Contract_Level_Candidate`, `06_Blockers`.
2. `01_Summary` states `period` and `period_end` and the five decision
   counts; no invented status (`CLOSED`/`POSTED`/`APPROVED`) appears
   anywhere in the workbook.
3. Every row on every decision sheet carries an `evidence_trace` cell
   that is non-empty whenever the underlying Decision has a genuine
   Fact -> Evidence chain — never a raw Evidence payload dump. Blocker
   rows are different by semantics: `BlockerContext` is current business
   context, not Evidence provenance, so `06_Blockers` and BLOCKER CSV
   rows carry it under `blocker_context`, with `evidence_trace` empty —
   no Fact -> Evidence chain is invented for blockers (none exists).
4. The CSV is one file, one header row, every data row carrying
   `record_type` in
   `{ACCRUAL_REQUIRED, PRIOR_ACCRUAL_REVERSAL, ACTUAL_DIFFERENCE,
   CONTRACT_LEVEL_CANDIDATE, BLOCKER}` — never five files, never a ZIP.
5. Re-running the same export against the same database produces
   semantically identical CSV bytes and identical parsed XLSX sheet
   content (Data Product generation is stateless recomputation, exactly
   like `period_close.py`).
6. Downloading either export changes zero business-table rows (verified
   automatically; may also be spot-checked with `SELECT count(*)` before
   and after against the PostgreSQL runtime).
7. An invalid `period` query param (`export.xlsx?period=not-a-period`)
   returns HTTP 400, matching the existing `/period-close` page's
   validation.

## Automated checks

- **DTO + serializer unit tests**
  (`tests/unit/test_period_close_export.py`) — Preview/Data-Product
  parity (every `PeriodClosePreview` decision has exactly one
  corresponding row, no more, no fewer); exact six-sheet structure and
  order; CSV unified long-table structure with all five record types;
  no invented business status; evidence trace present for decision rows
  (reversals etc.) while blockers carry `blocker_context` with an empty
  `evidence_trace`; missing optional fields stay blank (contract-level
  candidates never fabricate `source_item_key`/`quantity`); export
  functions never write to the database; two independent builds from
  the same session produce byte-identical CSV and identical parsed
  XLSX; an empty database yields a structurally valid, header-only
  export in every category.
- **Web export routes** (`tests/web/test_web_period_close_export.py`) —
  correct `Content-Type` and `Content-Disposition` filename for both
  formats; the page renders both download links; a malformed period is
  a 400; GET on either export route is zero-write; the CSV row count
  matches the Application-layer Data Product built independently from
  the same session (route never recomputes).
- **CLI export command**
  (`tests/integration/test_period_close_export_cli.py`) — real SQLite
  file via `subprocess` (not the in-process pytest session), same
  convention as `test_phase2b_cli.py`; `--format`/`--output` are both
  required (missing either is a non-zero exit); the written XLSX has the
  exact six sheets; the written CSV has all five record types;
  generation does not change `contracts`/`accruals`/`accrual_reversals`
  row counts; stdout never contains XLSX binary content.
- **PostgreSQL gate**
  (`tests/postgres/test_postgres_regression.py::test_period_close_data_product_export_against_postgres`,
  plus the extended
  `tests/integration/test_web_cli_dialect_enforcement.py::test_web_cli_accepts_postgresql_database_url`) —
  the same Data Product build + both serializers run against a real,
  freshly-migrated PostgreSQL schema (not only SQLite), read-only,
  semantically deterministic on rerun; the real HTTP server (`bel web`
  against a real PostgreSQL runtime) answers 200 with the correct
  content type on both export routes, including against an empty
  database.
- **Privacy** — every Phase 2D.2 test seeds its own
  independently-synthetic database (the existing Phase 2B `PO-CLOSE-*`
  fixture, reused unchanged); `python tools/privacy_scan.py` (staged)
  reports zero findings. This document records design principles and
  independently-synthetic examples only — no private-derived count,
  amount, contract number, or blocker detail, and no acceptance outcome
  or conclusion derived from non-public inputs, is recorded here; such
  results live exclusively under `$BEL_PRIVATE_DATA_ROOT/reports/`.
- **Migration immutability** — `python tools/check_migration_immutability.py
  --staged` reports PASS; this phase adds no migration (no schema
  change was required).

## Private real-data acceptance (procedure only)

Run against the private PostgreSQL runtime, per
`docs/PRIVATE-DATA-POLICY.md` — outputs, results, and diagnostics never
enter this repository; they live only under
`$BEL_PRIVATE_DATA_ROOT/reports/`:

```bash
BEL_DATABASE_URL=postgresql+psycopg://... bel period-close export <period> \
    --format xlsx --output "$BEL_PRIVATE_DATA_ROOT/reports/period-close-<period>.xlsx"
BEL_DATABASE_URL=postgresql+psycopg://... bel period-close export <period> \
    --format csv  --output "$BEL_PRIVATE_DATA_ROOT/reports/period-close-<period>.csv"
```

The private run MUST check, and report only scenario-level PASS/FAIL
outside the private root — never record the underlying values or the
conclusions here:

- both files open/parse successfully;
- the XLSX has the six required sheets;
- the CSV parses with the frozen unified schema;
- Preview -> XLSX parity and Preview -> CSV parity (row counts per
  `record_type` match `build_period_close_preview()`'s per-category
  counts exactly);
- every blocker the engine produces is present in `06_Blockers`;
  genuine Evidence trace present on the decision rows whose Facts have
  one, and blocker context present under `blocker_context` (never
  mislabeled as Evidence);
- no business-table row changes before/after generation;
- a second generation is semantically identical to the first.

## Explicitly out of scope (unchanged from the Phase 2D.2 spec)

New close rules, a persisted close snapshot/result/export-history
object, a "close period" mutation, accounting vouchers or vocabulary,
per-format independent business computation, and any 2D.3/2D.4 work
(Agent Runtime, Pi integration, MCP, multi-tenancy, automatic
sales-side matching, new Cutover Baseline generation). See the Phase
2D.2 spec's section 14 for the complete list.

# Phase 1 Decisions

Judgment calls made while implementing Phase 1, per the rule in the
Phase 1 spec: if implementation surfaces a gap in the frozen Phase 0
spec, do not edit [DOMAIN.md](DOMAIN.md)/[RULES.md](RULES.md)/etc. to
fit the code — record it here instead, pending business/architecture
review. **No Phase 0 document was modified during Phase 1.**

## SQLite foreign keys are opt-in per connection (post-review fix)

SQLite silently ignores `FOREIGN KEY` declarations unless
`PRAGMA foreign_keys=ON` is issued on every connection — the initial
implementation declared FKs on every model (`Contract.current_source_fragment_id`,
etc.) but never enabled the pragma, so they were enforced by nothing.
`make_engine()` now registers a `connect` event listener that runs the
pragma on every new DBAPI connection.

That surfaced a second, real bug: SQLAlchemy's automatic flush-order
dependency resolution across mapped classes requires an ORM
`relationship()` between them — with bare `ForeignKey` columns only (no
`relationship()` here, since repositories don't need ORM graph
traversal), a single `session.commit()` was not guaranteed to INSERT
`evidence_fragments` before the `contracts` rows that reference them,
and with the pragma on, that failed immediately with
`FOREIGN KEY constraint failed`. Fixed in
`import_contract_ledger()` by writing all `EvidenceFragment`s first,
calling `session.flush()` explicitly, then writing `Contract`s that
reference the now-persisted fragment ids — an explicit two-pass order
instead of relying on implicit ORM ordering. Covered by
`tests/integration/test_foreign_key_enforcement.py` (asserts an orphan
`current_source_fragment_id` is rejected).

## Technology choices

- **openpyxl, not pandas**, for the Excel adapter. Pandas coerces
  column dtypes on read, which would collapse exactly the "是" vs.
  Excel-date-serial distinction that section 8 of the spec requires
  preserving per-cell. openpyxl with `data_only=True` returns each
  cell's native type (str/int/float/datetime/None) untouched.
- **click** for the CLI (over argparse) — no bearing on the
  Agent/Business-Core boundary either way; picked for subcommand
  ergonomics (`bel contract search`, `bel contract get`, ...).
- **uv** for virtualenv/dependency management (`.venv/`, not committed).
- Amounts parsed as `Decimal(str(cell_value))`, **never** float, at
  every step from raw cell to persisted `NUMERIC(18,2)` column and back,
  with zero drift. `tests/golden/synthetic-v1/` checks this invariant on
  independently constructed data.

## Canonical field defaults not specified by any source column

The ledger has no explicit currency or contract-type column, and no
reliable contract-execution-date column:

- `currency = "CNY"` for every imported Contract. Rationale: `金额` is
  the domestic RMB amount paid to Chinese textile suppliers — the
  workbook has a *separate* `售出金额$` column for the USD-denominated
  export sale price, which is not promoted to any canonical field in
  Phase 1.
- `contract_type = "出口报关购销合同"` for every imported Contract — a
  constant label taken from the sheet's own title row, since every
  business row in this sheet is the same contract category. Not
  inferred per-row from any cell.
- `contract_date = null` for every imported Contract, always. No column
  in this sheet represents "when the contract was signed" (开销项发票日,
  申报印花税日期, 出口日期, 报关日期, etc. are all *other* business
  dates, not contract execution dates). Per spec section 6, this is
  never derived from `contract_no` digits or any other column — a
  `null` result here is the correct, expected output, not a defect.

## `docs/DOMAIN.md` field-list divergence (not resolved, flagged for review)

`DOMAIN.md`'s `Contract` object lists `counterparty_id` (implying an FK
to a normalized counterparty entity) and a `status` field. The Phase 1
spec's own "Canonical Contract" field list (section 5) instead specifies
plain string `counterparty` / `buyer` fields, no `status`, and adds
`current_source_fragment_id` / `created_at` / `updated_at`. This
implementation follows the Phase 1 spec's field list, since it is the
more specific and more recent instruction — but the divergence from
`DOMAIN.md` was never reconciled and `DOMAIN.md` was not edited. A
normalized `Counterparty` entity and a `Contract.status` field are open
questions for a later phase.

## "Business row" definition

A row in `报关出口购销合同` counts as a business row **iff its `合同编码`
cell is non-null and non-blank after stripping whitespace.** This is an
inferred rule (the spec states the target counts but not the exact
predicate) — a trailing sheet region with every cell empty except a
continuing `序号` sequence number is genuinely content-free, not just
numerically past the last business row, so the rule matches rows that
are actually blank.

The rule is deliberately **not** "`外销合同编码` is present" — a business
row can lack that column and must still become a Contract (see
`missing_export_contract_no` in the import result; the synthetic
fixture reproduces this case too).

## Evidence completeness beyond the business-row rule

`EvidenceFragment` rows are created for **every row** in the primary
sheet — the promoted business rows *and* the blank trailing rows —
not only the ones promoted to a Contract. Only Contract *promotion* is
gated by the business-row rule; nothing is excluded from the Evidence
layer itself before that decision is made. This maximizes A02's
evidence-completeness guarantee at negligible cost, rather than
deciding what counts as "not worth keeping" before a fact ever reaches
Evidence.

## Non-primary sheets

`联合运营合同`, `费用合同`, and `印花税25年4月` are recognized only as
entries in the parsed workbook's sheet-name list (surfaced in the CLI's
`sheets: 4` output). No rows from them are read; no `EvidenceFragment`
or domain object is created for them. Per spec section 7, this is
deliberate — not an oversight to "finish later in this phase."

## `raw_data` serialization policy

Every cell value is preserved as its native Python type (`str` / `int`
/ `float` / `None`) with exactly one transformation: `datetime`/`date`
values (which openpyxl returns for genuinely date-formatted cells) are
serialized to ISO-8601 date strings so they're JSON-storable. This is
lossless serialization for storage, not interpretation — a cell holding
a malformed date-like string (e.g. `"205/8/14"`) or a stray numeric
value in a text column is stored as that exact string/number,
untouched. No cell is ever reinterpreted as a different type than
openpyxl reported.

## Idempotency mechanism

A `UNIQUE` index on `evidence_documents.sha256` is the entire
idempotency mechanism. A re-import whose file hash already exists
short-circuits *before* parsing — it creates only a new `import_runs`
audit row (`is_reimport=True`) and zero new
Contracts/ContractItems/Fragments/Documents/Events. `contract_no` has a
non-unique index only (verified by
`tests/integration/test_migration.py`, which inspects the real
Alembic-generated schema, not just the ORM model definitions).

## `BusinessKeyConflict` modeling

Implemented as a single `TaskException` row
(`exception_type="BusinessKeyConflict"`) whose `detail` JSON lists the
conflicting Contract UUIDs — no new table or abstraction. **Phase 1
raises a conflict for every duplicate `contract_no` group
unconditionally**; it does not attempt R004's fuller "do the relevant
business facts actually conflict" judgment — that discrimination
requires a rule engine, which is explicitly out of scope for Phase 1
(see spec section 16) — this should be revisited once R004 has a real
implementation. The synthetic fixture reproduces the shape this
simplification is meant to handle (`tests/golden/synthetic-v1/`).

## `BusinessEvent` granularity

One `CONTRACT_IMPORTED` event per import run (summarizing the whole
run), not one per Contract — one near-identical event per row would
edge toward using `BusinessEvent` as a replay log, which
[V1-SCOPE.md](V1-SCOPE.md) explicitly rules out. One
`BUSINESS_KEY_CONFLICT_DETECTED` event per conflicting `contract_no`
group.

## Statistics that are computed but not persisted as Contract columns

`distinct_sellers`, `distinct_buyers`, `distinct_owners` (对接人),
`distinct_customs_receivers` (接收报关单位), and
`missing_export_contract_no` (外销合同编码) are computed in-memory
during import, from raw row data, purely for the import report. None of
their source columns (对接人, 接收报关单位, 外销合同编码) are part of the
canonical Contract field list in spec section 5, so none of them are
persisted as Contract columns — computing them from `EvidenceFragment`
data on demand would also be possible later without a schema change.

## Failure behavior for a business row with no parseable amount

A business row (non-null `合同编码`) with no `金额` value, or a `金额`
value that cannot parse as `Decimal`, raises `ValueError` and aborts the
whole import rather than silently defaulting to `0`/`null`. This is a
deliberate "fail loudly rather than guess" choice
(A05), not yet a batched per-row error report; if a future ledger
version has genuinely malformed amounts, this will need a softer
per-row-warning path instead of a hard abort.

## Database schema ownership

Schema is owned exclusively by Alembic migrations
(`migrations/versions/`). The CLI never calls
`Base.metadata.create_all()` — `alembic upgrade head` is a documented
prerequisite before first use. Test-layer unit/integration tests build
schema directly from the ORM models on an in-memory SQLite DB for
speed; `tests/integration/test_migration.py` is the one test that runs
the real `alembic upgrade head` as a subprocess against a temp file, to
verify the actual migration (not just the model definitions) produces
the right schema.

## Golden-fixture separation

`tests/golden/synthetic-v1/import-baseline.json` contains independently
constructed Excel-import expectations checked by
`test_import_baseline.py`. The period-close/accrual scenarios G01–G06
remain outside the Phase 1/2A implementation scope (spec section 16).

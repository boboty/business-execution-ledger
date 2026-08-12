# Phase 2C Decisions

Judgment calls made while implementing Phase 2C (Minimal Human Workbench —
月结工作台 + 合同360°). Per the Phase 2C spec, no Phase 0/1/2A/2B document
was modified: `DOMAIN.md`, `RULES.md`, `V1-SCOPE.md`, `ARCHITECTURE.md`,
`GOLDEN-TEST.md`, `PHASE1-DECISIONS.md`, `PHASE2A-*`, and `PHASE2B-*` are
untouched. Anything that would genuinely conflict with the spec is a
`SPEC_CHANGE_REQUEST`, not a silent deviation — none was needed for the
implementation; the one clarification below is recorded here.

## Web layer calls Application Services only

`src/bel/web/routes.py` never touches a repository and never computes a
business number. Every page handler calls `get_period_close_workbench`,
`get_contract_360`, `search_contracts_by_no`, or `allocate_invoice_item`.
The two new application queries (`period_close_workbench.py`,
`contract_360.py`) compose repositories and delegate all close decisions
to the frozen `build_period_close_preview`; the Web layer only maps
display labels. This keeps the single write operation (`allocate_invoice_item`)
byte-identical to the CLI path.

## `PeriodClosePreview` stays the only source of truth

`get_period_close_workbench` returns a `PeriodCloseWorkbench` whose
`preview` attribute is the unchanged `PeriodClosePreview` built by
`build_period_close_preview`. The DTO adds only readable labels
(contract/item names) and the Decision → Fact → EvidenceFragment →
EvidenceDocument trace. A permanent test asserts field-by-field parity:
`[r.decision for r in workbench.reversals] == list(preview.prior_accrual_reversals)`
and so on for every decision list.

## The Contract360 current-period judgment is a filtered subset

Spec section 23 forbids a second Contract-specific close rule set.
`get_contract_360` calls the same `get_period_close_workbench` and then
filters each decision/blocker list by `contract_id`. The same DTO rows
(and the same trace partials) render on both pages.

## Blocker meanings are Presentation; blocker existence is Domain

The four blocker explanations (spec section 8) live in
`src/bel/web/viewmodels.py` as a pure label mapping. The close engine in
`period_close.py` — untouched — decides *whether* a blocker exists. The
Web layer can never suppress or add a blocker; it only renders it.

## Decision Trace carries the fact chain, not raw_data

Each trace node shows the fact fields (period, quantity, estimated cost,
basis, …), then 来源证据 (source type / document / locator), then a
collapsible 技术详情 holding UUIDs and the sha256. `raw_data` is never
rendered in a trace; the Contract360 evidence section shows locator +
metadata only. There is deliberately no file download endpoint anywhere
(`/files` etc. are tested to be absent).

## No inline script / style; everything is local

CSP is `default-src 'self'; script-src 'self'; style-src 'self'` and every
dynamic response carries `Cache-Control: no-store`, `Referrer-Policy:
no-referrer`, `X-Content-Type-Options: nosniff`. Templates contain no
inline `<script>` and no `style="…"` attributes; expand/collapse uses
native `<details>`/`<summary>`. The only JavaScript is a single same-origin
`fetch` POST for the manual allocation, and its handler is in
`static/app.js`. CSS/JS are served from `/static` so pages work offline.

## The allocation select is never preselected

The ContractItem dropdown renders one placeholder option
(`value="" disabled selected`) so a human must explicitly choose. No
name/amount/position-based preselection exists, and `validate_item_allocation`
(the shared service) enforces 11-A/B/C — cross-contract, capacity, and
missing-confirmation all return 400 with zero partial writes (permanent
tests).

## Period dropdown is derived from the data

`list_known_periods` collects YYYY-MM values from Accrual periods,
HistoricalAccrualFact source periods, and Invoice issue dates, newest
first. `/period-close` defaults to the latest known period. This is a
display convenience only — the close engine itself never selects a period.

## `bel web` binds localhost by default

`bel --db /path/bel.db web` serves on `127.0.0.1:8000` and prints
`Business Execution Ledger` + the URL (it never opens a browser). Any
`--host` other than `127.0.0.1` / `::1` / `localhost` prints the spec's
exact warning to stderr before serving.

## Route-level reads run under `no_autoflush`

Every GET handler wraps its whole body in `session.no_autoflush`. This is
what makes the default-period lookup (`list_known_periods`), the contract
search, and the Application Services all strict reads: a pending
(unflushed) object already sitting in the request's session is never
flushed by a GET. A permanent test injects a pending `AccrualModel` into
the route session and asserts `total_changes == 0`, `session.new`
unchanged, and `session.dirty` empty across `/period-close`,
`/contracts/{id}` (both default and explicit period) and
`/contracts/search`.

## The manual allocation rejects non-positive / over-limit amounts

`validate_item_allocation` (the shared guard used by both the CLI and the
Fact Pack import) now rejects `allocated_quantity <= 0`, negative
`allocated_net_amount`, and `allocated_net_amount` beyond the invoice
line's net amount — in addition to the existing cumulative-capacity check.
A negative quantity could otherwise *free* capacity for later allocations;
the new checks close that. The web endpoint surfaces all of these as 400
with zero partial writes.

## Identical manual allocation payloads are duplicates

`allocate_invoice_item` computes the EvidenceDocument sha256 from the
payload and refuses to create a second document when the same bytes are
already recorded (`evidence_documents.sha256` is UNIQUE). A retried
identical POST returns a clean 400 ("duplicate allocation request")
instead of a 500 IntegrityError, and adds no rows. Different payloads on
the same line remain separate allocations governed by capacity.

## Concurrent identical POSTs are controlled, not a 500

The sha256 pre-check is a fast path only; two simultaneous identical
POSTs can both pass it (TOCTOU). The `evidence_documents.sha256` UNIQUE
constraint is the authoritative guard: the service wraps its final
flush/commit and, on an IntegrityError for that constraint, rolls the
session back (zero partial rows) and re-raises the same controlled
"duplicate allocation request" ValueError the pre-check produces. A
permanent test fires two threads at the endpoint across 30 rounds and
asserts exactly one 201 + one 400 per round with exactly
+1 allocation/document/fragment.

## InvoiceItem capacity is enforced atomically under concurrency

`validate_item_allocation`'s read-check-then-insert capacity guard is
racy: two concurrent POSTs with *different* payloads can both read the
same existing allocated quantity, both pass `sum + qty <= line`, and both
commit — oversubscribing the line. The sha256 guard cannot help here
because the payloads differ.

The fix is scoped to the write path, never to reads: the web app exposes
two session factories. Read-only GET pages use the default DEFERRED
engine, so concurrent reads coexist and never touch the SQLite write
lock. The manual-allocation POST uses a dedicated write engine whose
transactions begin with `BEGIN IMMEDIATE` — the write lock is acquired
before any read — so two concurrent allocations on the same InvoiceItem
serialize: the second blocks until the first commits, then re-reads the
fresh allocated quantity and is rejected with a controlled 400 "capacity
exceeded"; the cumulative allocated quantity can never exceed the line.
The CLI stays on the default engine (single-threaded, no allocation
races). A permanent test fires two different 30-unit payloads at a
50-unit line across 10 fresh DBs and asserts one 201 + one 400 with
cumulative quantity == 30, and another test asserts a held read
transaction does not slow down or fail `GET /period-close`.

## Writes carry a server-side same-origin check

Phase 2C's only write endpoint verifies the `Origin` header: a present
Origin must match this server's own scheme+host, otherwise 403. A missing
Origin (non-browser client) is allowed — cross-site browser POSTs are
additionally blocked by the JSON content-type requirement and by the
absent CORS configuration. Permanent tests cover both the 403 and the
matching-Origin 201.

# Phase 2C.1 — SQLite transaction hardening

## One DatabaseRuntime, reads are always DEFERRED

`DatabaseRuntime` owns a single DEFERRED SQLite engine and its session
factory for one database identity. The web app is built from a
`DatabaseRuntime` (constructed from `db_path` or injected), so the
read-only pages and the write API address the SAME database. Reads are
always normal DEFERRED transactions — nothing ever makes a read take the
SQLite write lock, so concurrent GET pages coexist and a long reader
never blocks a writer (WAL).

## BEGIN IMMEDIATE is a WRITE TRANSACTION property, never an Engine property

There is no "write engine" and no engine-level BEGIN IMMEDIATE. The write
lock is acquired per transaction by the single shared boundary:

```
execute_manual_item_allocation(session, ...)     # Web POST + CLI + future Agent
        └→ serialized_write_transaction(session)
               ├→ BEGIN IMMEDIATE                # first statement: write lock before any read
               ├→ allocate_invoice_item(...)     # validation / evidence / allocation / flush
               └→ commit | rollback              # single commit; any failure rolls back cleanly
```

`allocate_invoice_item` deliberately does NOT commit — the boundary owns
the commit, so every manual allocation writer shares ONE transaction
boundary and serializes on the same database-level write lock. This
replaces the earlier two-engine model (removed).

## Web startup rejects `:memory:`

File SQLite is the production/concurrent Web runtime. `bel web --db
:memory:` and `create_app(runtime=DatabaseRuntime(":memory:"))` are both
rejected with an explicit message: `:memory:` has no concurrent Web
guarantee. `:memory:` remains available to sequential domain/application
tests, where reads are plain DEFERRED.

## WAL + busy_timeout, busy errors are controlled

Every connection runs `PRAGMA journal_mode=WAL` (writers never block
readers and vice versa) and `PRAGMA busy_timeout` (default 5s). A
concurrent writer that holds the write lock past the timeout produces a
busy error, which is:
  - web allocation POST → rolled back and answered 503 ("database is
    busy"), never 500 (`is_database_busy`);
  - Close Fact Pack import → rolled back and surfaced as
    `CloseFactPackError` (clean, no partial business state);
  - `bel invoice-item allocate` → clean "database is busy" message.

Any write failure rolls back and leaves no Evidence/Allocation residue
(the shared boundary's rollback is the guarantee; a permanent test
injects an OperationalError at the REAL commit — after evidence/allocation
were flushed — and asserts zero residue plus a healthy next write).

## Contract360 scopes item allocations to the current contract

The frozen Domain allows one Invoice to reference several Contracts
(Invoice ↔ Contract is many-to-many), so an InvoiceItem can carry
allocations owned by another contract's items. `get_contract_360` builds
each InvoiceItem's allocation list by filtering
`allocation.contract_item_id in <current contract's item ids>` — an
allocation owned by another contract must never read as "已关联" on this
contract's page, or the human would lose the manual-allocation form for
work that is genuinely still pending here. The evidence aggregation
follows the same rule. A permanent test confirms: the same Invoice
confirmed to Contracts A and B with the allocation owned by B shows
"未关联" + the allocation form on A and "已关联" without it on B.

## Concurrency acceptance uses temporary FILE SQLite

All committed concurrency tests run against temporary file databases
(`tests/web/test_web_transaction_hardening.py`, 20 rounds) covering:
long reader + POST (201 or 503, never 500); two identical POSTs (at most
one allocation, no orphan Evidence); two different payloads (cumulative
allocation <= line capacity); GET storm + POST (GETs readable, no leaked
lock); POST under a held writer (controlled 503 after busy_timeout, pool
healthy); injected failed commit (rollback, no partial rows, next write
succeeds); injected file runtime identity (GET and POST see the same DB);
fact-pack import vs web writer contention (clean failure, no partial
state); and 20 concurrent read/write repetitions. The local 100-round
stress harness is intentionally kept outside the repository.

## Clarification recorded (no spec conflict)

Spec section 21's Payment table shows 日期/付款/收款/金额/交易对手/确认方式.
The synthetic Contract360 page follows exactly that column set; the bank
reference number is therefore not displayed as a column (it remains in
the evidence locator). This is a reading of the spec, not a deviation —
recorded so the acceptance tests assert the spec's columns.

## UI test data

Every committed UI test (`tests/web/`) runs against the existing
independently-synthetic Phase 2B fixture under `fixtures/synthetic/`. No
private value enters a rendered HTML snapshot, a screenshot, pytest
output, or a commit message. HTML assertions check rule names, Chinese
labels, and synthetic values only.

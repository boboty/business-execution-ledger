# Phase 2C Acceptance

Public acceptance criteria for the Phase 2C Minimal Human Workbench
(月结工作台 + 合同360°). Every committed acceptance check runs against the
independently-synthetic fixture (`fixtures/synthetic/phase2b_close.py`)
via `tests/web/` and the existing golden suite.

## How to run

```bash
.venv/bin/pytest                                   # full public suite (incl. tests/web/)
python tools/privacy_scan.py --tracked             # privacy scan (also runs on commit)
```

## Manual / browser acceptance

```bash
bel --db /path/to/bel.db web                       # 127.0.0.1:8000
# open http://127.0.0.1:8000  (redirects to /period-close)
# open http://127.0.0.1:8000/contracts/search?no=PO-CLOSE-001
```

Checklist:

1. `/` redirects to `/period-close` (302).
2. `GET /period-close?period=2031-03` renders: 5 summary cards, blockers
   first, 历史暂估待红冲, 新增暂估 (Accrual Required), 合同级待补明细
   (尚不能形成正式暂估, visually distinct), 成本差异, and a 查看依据
   trace (Decision → Fact → Evidence) per row. 「重新计算」 is a plain GET.
3. `GET /contracts/{id}?period=2031-03` renders: 合同信息 header,
   合同商品, 发票 (with per-line 已关联/未关联 + 关联合同明细 form),
   付款 (explicit allocations only), 暂估余额 (derived balance), 当前期间
   业务判断, 证据 (locator + metadata, no file URL).
4. Alloc a line: pick a ContractItem explicitly (never pre-selected),
   enter quantity + net amount, submit → 201, page reloads, line shows
   已关联. Invalid business input (cross-contract / over-capacity /
   unconfirmed contract) → 400 with a readable message and no partial
   writes.
5. No page shows an inline `<script>` or `style="…"`; all CSS/JS come from
   `/static`; every response carries `Cache-Control: no-store`,
   `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` and a
   same-origin CSP. There is no `/files`-style endpoint.
6. `bel --db … web --host 0.0.0.0` prints the remote-exposure warning.

## Automated checks (all in `tests/web/`)

- **Web smoke** — `/` → 302 to `/period-close`; `GET /period-close` and
  `GET /contracts/{id}` → 200; bad period → 400; missing contract → 404;
  security headers + CSP on HTML and JSON; static assets served locally;
  no inline script/style; no file-download endpoints.
- **Period Close** — page contains a partial reversal, a new accrual, a
  candidate, a difference, and both blocker types (with Chinese
  explanations); `GET` changes zero DB rows (`total_changes`-style row-count
  snapshot); the Workbench DTO the page renders is field-for-field equal to
  `build_period_close_preview(...)`; every trace node resolves to a
  fragment + document.
- **Contract 360** — shows Contract, ContractItem, Invoice (+item
  allocation state), Payment, Accrual balance (via
  `get_accrual_balance`), Evidence, and the current-period judgment
  filtered to the contract; an unallocated invoice line offers the
  non-preselected allocation form; `GET` changes zero DB rows.
- **Manual allocation API** — success → 201 + one
  `InvoiceItemAllocation` + Evidence row; failures (cross-contract,
  capacity exceeded, missing contract-level confirmation, unknown
  invoice, malformed payload) → 400 with zero partial writes.
- **Gate A regression (permanent)** — default-period GET / contract
  search / Contract360 never autoflush a pending object (`total_changes`
  unchanged, `session.new`/`session.dirty` preserved); a held read
  transaction does not slow down or fail `GET /period-close` (200 fast,
  no write lock); zero/negative quantity, negative net amount, and
  over-line net amount → 400 with no writes; a duplicated identical POST
  → clean 400 (duplicate), never 500, zero rows; two concurrent identical
  POSTs → exactly one 201 + one controlled 400 with no orphaned/duplicate
  rows (30 rounds); two concurrent DIFFERENT payloads (30+30 on a 50
  line) → exactly one 201 + one controlled 400 "capacity exceeded" with
  cumulative quantity never exceeding the line (10 fresh rounds); a
  non-matching `Origin` header → 403, matching Origin → 201.
- **Parity** — on the same synthetic database, the CLI/Application
  preview result equals the Web workbench DTO result.
- **Phase 2C.1 SQLite transaction hardening (tests/web/test_web_transaction_hardening.py)**
  — concurrency acceptance uses temporary FILE SQLite at 20 CI rounds
  (a 100-round local stress harness is kept outside the repository):
  - A long reader + POST → 201 or controlled 503, never 500, reader and
    subsequent connections healthy;
  - B two identical POSTs → at most one allocation, no 500, no orphan
    EvidenceDocument/EvidenceFragment;
  - C two different payloads → cumulative allocation never exceeds the
    line capacity;
  - D GET storm + POST → GETs remain readable, POST succeeds or controlled
    busy, no leaked lock;
  - E POST while a writer holds BEGIN IMMEDIATE → controlled 503 after the
    busy timeout, rollback, pool healthy (next write succeeds);
  - F injected OperationalError at the REAL commit (after evidence and
    allocation are flushed) → the shared boundary rolls back, zero partial
    Evidence/Allocation, next write succeeds;
  - H injected file `DatabaseRuntime` → GET and POST see exactly the same
    DB; `bel web --db :memory:` and `create_app` with a `:memory:` runtime
    are explicitly rejected;
  - I Close Fact Pack import vs web allocation → one waits/fails cleanly
    (CloseFactPackError), no partial business state, retry succeeds;
  - J 20 concurrent read/write repetitions → no database-is-locked residue,
    capacity and no-duplicate invariants preserved.
- **Shared write boundary** — Web POST and `bel invoice-item allocate`
  both run through `execute_manual_item_allocation` →
  `serialized_write_transaction` (BEGIN IMMEDIATE per transaction, single
  commit); `allocate_invoice_item` owns no commit.

## Explicitly out of scope (unchanged)

Business cockpit, 合同业务总账, full exception/task center, login/users/
roles/RBAC, file upload, full Evidence management, month-close
confirmation, real Accrual insert, real Reversal execution, accounting
vouchers, finance-system interfaces, Export, R008–R015, Agent/Pi/
PydanticAI/MCP/Tool API/LLM, and any JS framework or third-party CDN.

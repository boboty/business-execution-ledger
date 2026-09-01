# Phase 2D.3 Acceptance — Invoice Preparation Workbench & Data Product

Public acceptance criteria for Phase 2D.3. Builds on
`docs/PHASE2D2-ACCEPTANCE.md` (unchanged) — this phase adds the
integrated Invoice Preparation Workbench (F0 fact context + the F1
deterministic controls + the F2a presentation) and the Invoice
Preparation Data Product (F2b). Rule provenance is frozen separately in
`docs/PHASE2D3-RULE-FREEZE.md` and is NOT restated here.

## How to run

```bash
.venv/bin/pytest                                                    # full public suite
.venv/bin/pytest tests/integration/test_sales_invoice_preparation.py -q
.venv/bin/pytest tests/integration/test_sales_amount_control_f1f.py -q
.venv/bin/pytest tests/integration/test_supplier_invoice_request_advisories_f1d.py -q
.venv/bin/pytest tests/integration/test_invoice_currency_f1e.py -q
.venv/bin/pytest tests/integration/test_invoice_preparation_workbench.py -q
.venv/bin/pytest tests/web/test_web_invoice_preparation.py -q        # Workbench page + Web exports
.venv/bin/pytest tests/integration/test_invoice_preparation_export.py -q   # Data Product DTO + serializers
.venv/bin/pytest tests/integration/test_invoice_preparation_export_cli.py -q  # CLI export command
python tools/privacy_scan.py --staged
python tools/check_migration_immutability.py --staged
```

## Manual / browser acceptance (read-only against real data)

```bash
BEL_DATABASE_URL=postgresql+psycopg://... bel web                   # 127.0.0.1:8000
# open http://127.0.0.1:8000/invoice-preparation
curl -o invoice-preparation.xlsx "http://127.0.0.1:8000/invoice-preparation/export.xlsx"
curl -o invoice-preparation.csv  "http://127.0.0.1:8000/invoice-preparation/export.csv"
```

```bash
bel invoice-preparation export --format xlsx --output invoice-preparation.xlsx
bel invoice-preparation export --format csv  --output invoice-preparation.csv
```

## Workbench page checklist

1. The page separates 向客户开票 (SalesContract axis) from 向供应商要票
   (procurement Contract axis); the external customer comes only from
   `SalesContract.customer`, and `Contract.buyer` is presented as OUR OWN
   entity, never as a customer.
2. Each scope distinguishes three blocks: 已确认事实 · 核对结果 ·
   提醒/待关注. A comparison outcome is never flattened into an
   eligibility/readiness status; nothing on the page says 可以开票 /
   不允许开票 / 已具备开票资格 / 开票失败.
3. Comparison outcomes use business-facing wording: 金额核对一致 /
   金额存在偏差，建议复核 / 当前信息不足，暂无法核对 / 币种不同，暂不直接
   比较金额 / 对应范围不唯一，暂无法自动核对. Internal enum names
   (MATCH / DEVIATION / NOT_COMPARABLE_* / RULE_CONFLICT /
   INPUTS_PRESENT / the advisory and blocker codes) never appear in the
   page.
4. Confirmed-Fact lists contain only associations whose referenced
   Invoice/Payment Fact exists with the correct direction. A dangling
   association appears ONLY under 提醒/待关注 as 关联记录对应的基础事实缺失,
   never under 已确认发票 / 已确认付款 / 已关联销项发票事实 / 已关联收款事实.
5. Existing unresolved work appears under 提醒/待关注 as 已有待处理事项
   (distinct from 管理提醒, the recomputed F1 advisories such as
   已付款，尚未收到对应进项发票，建议催供应商开票). No Fact is rendered as
   a fabricated zero or a guessed currency.

## Data Product checklist

6. The XLSX has exactly five sheets in order: `01_Summary`,
   `02_Sales_Preparation`, `03_Sales_Attention`, `04_Supplier_Request`,
   `05_Supplier_Attention`.
7. `01_Summary` states scope counts, sales comparison outcome counts,
   supplier comparison counts and attention counts by category — counts
   only, no readiness score.
8. `02_Sales_Preparation` is one row per SalesContract with the IP-S02
   comparison outcome and the three compared amount/currency legs;
   an ambiguous scope is `NOT_COMPARABLE_AMBIGUOUS_SCOPE` (never summed,
   never apportioned), and a missing compared Fact is a blank cell.
9. `04_Supplier_Request` is one row per procurement Contract with the
   reference amount/currency and the existing P02/P05 comparison results
   serialized deterministically (never collapsed into a fake overall
   status).
10. Attention rows (03/05) keep UNRESOLVED_WORK / INCOMPLETE_ASSOCIATION /
    MANAGEMENT_ADVISORY distinguishable; the P09 follow-up is a
    MANAGEMENT_ADVISORY, never overdue / a blocker / a violation.
11. The CSV is one file, one header row, every data row carrying
    `record_type` in `{SALES_PREPARATION, SALES_ATTENTION,
    SUPPLIER_REQUEST, SUPPLIER_ATTENTION}`. Canonical machine codes are
    preserved; business-facing messages accompany them.
12. Re-running the same export against the same database produces
    byte-identical CSV and byte-identical XLSX (deterministic zip
    metadata). Exports cause zero database writes.
13. Text cells beginning with `=`, `+`, `-`, `@`, a tab or a carriage
    return are neutralized (formula-injection guard) in both artifacts.

## Automated checks

Run the focused suites listed above. They enforce the same-source rule
(the export builder accepts the Workbench, never a Session; the Web and
CLI export byte-identical products), the F2a confirmed-Fact boundary,
format (five exact sheet names / unified CSV record_type), determinism,
formula-injection safety, blank-not-zero missing values, and zero-write
for Web XLSX, Web CSV and CLI export. `privacy_scan` and
`check_migration_immutability` must PASS.

## Private real-data acceptance (procedure only)

The focused public suites are independently synthetic. Real-data review
runs the same commands against `$BEL_PRIVATE_DATA_ROOT`-sourced data;
private acceptance output reports scenario ID + PASS/FAIL only
(`docs/PRIVATE-DATA-POLICY.md`). No repository artifact records
source-derived values.

## Explicitly out of scope

- Persisting Decisions, advisories, Tasks, or invoices (F2b is strictly
  read-only; a later phase may turn selected reminders into Tasks).
- Any invoicing/approval workflow, eligibility gate, or "ready to
  invoice" judgment.
- FX conversion, currency inference/defaulting, summing or apportioning
  ambiguous M:N invoices/shipments.
- Tax classification code implementation (IP-P08 stays register-only).
- Any schema/migration change; a schema correction is always a new
  forward migration (`docs/PERSISTENCE-MIGRATION-POLICY.md`).

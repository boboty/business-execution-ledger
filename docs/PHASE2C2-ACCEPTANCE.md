# Phase 2C.2 Acceptance

Public acceptance criteria for Phase 2C.2 (月结决策工作台 + 合同360°
business-language completion). Builds on `docs/PHASE2C-ACCEPTANCE.md`
(unchanged) — every check below is additional or updated wording on the
same two pages, same routes, same single write endpoint.

## How to run

```bash
.venv/bin/pytest                                        # full public suite (incl. tests/web/)
.venv/bin/pytest tests/web/test_web_phase2c2_ui.py -q    # Phase 2C.2 focused
python tools/privacy_scan.py --tracked
python tools/privacy_scan.py --history
python tools/privacy_scan.py --staged
python tools/privacy_scan.py --untracked                # new files not yet `git add`ed
```

## Manual / browser acceptance (read-only against real data)

```bash
bel --db /path/to/bel.db web                             # 127.0.0.1:8000
# open http://127.0.0.1:8000/period-close?period=<period>
```

Checklist (spec section 13):

1. The page reads as a **rehearsal**, not an executed action, at first
   glance: subtitle states "本页面只生成业务判断，不执行红冲、不生成暂估、
   不生成凭证、不修改月结状态"; the reload button reads "重新预演".
2. No row anywhere reads "待红冲" and "已红冲" at the same time — a
   Decision's projected outcome always reads "红冲后：全部冲销" /
   "红冲后：部分冲销", never the bare "已红冲"/"部分红冲".
3. A `ContractItem` with no `product_name` never shows its
   `source_item_key` (e.g. `ITEM-1`) as if it were a product — it shows
   "未提供商品明细", with the technical key demoted into 技术详情/技术信息.
4. Contract-level candidates render grouped by supplier first (合同数 +
   预计成本合计 + collapsed detail), not a flat wall of rows.
5. Each blocker's first screen answers, without expanding anything:
   what's confirmed, why the system won't decide, and what to do next —
   the raw blocker code is present but only inside a collapsed 技术详情.
6. Contract360 visibly separates 当前暂估余额 (Current State) from 本期
   业务判断 · 只读预演 (Projected State / blockers).
7. Every raw technical enum (`AUTO_CONFIRMED`, `HUMAN_CONFIRMED`,
   `EXACT_COUNTERPARTY_AMOUNT_UNIQUE`, blocker codes, `source_item_key`)
   is still traceable somewhere on the page — nothing was deleted, only
   demoted in visual priority.

Everything from `docs/PHASE2C-ACCEPTANCE.md`'s manual checklist (route
responses, allocation flow, security headers, no inline script) still
applies unchanged.

## Automated checks (new/changed in `tests/web/`)

- **Projected vs. Current status wording**
  (`test_web_phase2c2_ui.py::test_a_*`, plus updated assertions in
  `test_web_period_close.py` / `test_web_contract_360.py` /
  `test_web_attack_gates.py`) — "已红冲"/"部分红冲" never appear on
  either page; "红冲后：全部冲销"/"红冲后：部分冲销" appear for this
  period's Decision; a persisted, fully-reversed Accrual legitimately
  renders "已冲销" in Contract360's Current State panel.
- **Missing product evidence**
  (`test_web_phase2c2_ui.py::test_b_*`) — a `ContractItem`/Accrual with
  `product_name = None` renders "未提供商品明细" / "仅合同范围" /
  "当前暂估仅有合同范围证据" as the primary text; its `source_item_key`
  is reachable only inside the `技术信息`/`技术详情` detail (a structural
  check, not just string presence).
- **Candidate supplier grouping**
  (`test_web_phase2c2_ui.py::test_c_*`) — multiple candidates across
  distinct suppliers, including a duplicated `contract_no` under two
  different counterparties: group count, total candidate count, and
  estimated-cost sum all agree exactly with
  `build_period_close_preview(...).contract_level_candidates`; the
  duplicated `contract_no` renders as two distinct rows, never merged.
- **Blocker business presentation**
  (`test_web_phase2c2_ui.py::test_d_*`) —
  `MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE` renders a business
  title, an explicit "无法判断" reason, a "下一步", the fixed
  no-direct-action disclaimer, and a Contract360 link, all without
  expanding anything; the raw blocker code is present but only at a
  string position *after* the "技术详情" marker.
- **Technical enum presentation**
  (updated `test_web_contract_360.py::test_invoice_area_with_manual_allocation_state`)
  — `AUTO_CONFIRMED`/`EXACT_COUNTERPARTY_AMOUNT_UNIQUE` render Chinese
  business labels ("系统确定性匹配"/"交易对手 + 金额唯一匹配") as the
  primary text; the raw literal is still present (technical detail).
- **GET zero-write** (`test_web_phase2c2_ui.py::test_f_*`) — the new
  supplier-grouping and blocker-context composition changes zero DB
  rows, on top of the existing full-page zero-write tests.
- **Preview parity** (`test_web_phase2c2_ui.py::test_g_*`, plus the
  existing `test_web_underlying_dto_matches_application_preview`) — after
  the `item_label: str` -> `item: ContractItem | None` DTO change and the
  new `BlockerContext`, `[row.decision for row in workbench.X] ==
  list(preview.X)` and `[b.blocker for b in workbench.blockers] ==
  list(preview.blockers)` still hold exactly.
- **Manual allocation regression** — `tests/web/test_web_allocation.py`,
  `tests/web/test_web_gate_fixes.py`, and
  `tests/web/test_web_transaction_hardening.py` are unchanged in
  behavior (one wording-only assertion update: "已关联" ->
  "已确认关联"); all still pass, confirming the write path/transaction
  boundary was not touched.
- **Privacy** — every Phase 2C.2 test seeds its own independently-synthetic
  database (`PO-UI-*` / `SupplierUi*`); `python tools/privacy_scan.py`
  (tracked/history/staged/untracked — `--untracked` added in this phase's
  Gate A remediation to cover new files before `git add`) reports zero
  findings. Public documentation (`docs/PHASE2C2-*.md`) records design
  principles and independently-synthetic examples only — it never records
  private-derived counts, combinations, or replay outcomes; those belong
  exclusively under `$BEL_PRIVATE_DATA_ROOT/reports/`.

## Explicitly out of scope (unchanged from Phase 2C, plus)

Everything listed in `docs/PHASE2C-ACCEPTANCE.md`'s out-of-scope section,
plus, specific to 2C.2: a real "confirm reversal scope" write command (a
blocker's next-step is a link + a fixed disclaimer, never a new action
button), CONTRACT-scope `Accrual` records, and a modeled
`UnresolvedBusinessFact` domain object/UI surface (see
`docs/PHASE2C2-DECISIONS.md`'s "Explicitly deferred" section).

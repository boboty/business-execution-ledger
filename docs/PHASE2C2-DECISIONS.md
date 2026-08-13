# Phase 2C.2 Decisions

Judgment calls made while completing the Phase 2C.2 Human Workbench UI
(月结决策工作台 + 合同360° business-language completion). Per the Phase
2C.2 spec, `DOMAIN.md`, `RULES.md`, and `V1-SCOPE.md` are untouched, and
none of `period_close.py` (the Rule Engine) was modified — this phase is
Application query composition and Web presentation only. Anything that
would genuinely conflict with the spec is a `SPEC_CHANGE_REQUEST`; none
was needed.

## The UI's four-layer semantic

Every page must keep these four concepts visually and textually distinct
(spec section 3):

1. **Fact** — already exists in the Ledger (a HistoricalAccrualFact, a
   confirmed Invoice, an InvoiceItemAllocation).
2. **Current State** — the balance derived from persisted Facts as of now
   (Contract360's 当前暂估余额 panel: 未冲销/部分冲销/已冲销 —
   `viewmodels.CURRENT_STATUS_LABELS`).
3. **Decision (Projected State)** — what `PeriodClosePreview` currently
   computes for this period, i.e. what *would* happen if executed
   (红冲后：全部冲销/红冲后：部分冲销 — `viewmodels.PROJECTED_STATUS_LABELS`).
4. **Blocker** — the Rule Engine explicitly declines to decide.

The concrete bug this fixes: before Phase 2C.2, both the persisted
balance and this period's preview reused one `STATUS_LABELS` dict, so a
*preview* reversal could render as "已红冲" — indistinguishable from an
already-executed fact. `PROJECTED_STATUS_LABELS` and
`CURRENT_STATUS_LABELS` are now two separate dicts in
`src/bel/web/viewmodels.py`; the string "已红冲"/"部分红冲" never appears
anywhere in the rendered output — a permanent regression
(`tests/web/test_web_phase2c2_ui.py::test_a_*`) asserts this on both
pages, including that a *persisted, fully-reversed* Accrual legitimately
renders "已冲销" (Current State), never "红冲后：全部冲销" (Projected
State language belongs only to this period's Decision).

## Technical identifiers are secondary presentation

`source_item_key` (e.g. `ITEM-1`), raw blocker type codes
(`MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE`), and raw match/
confirmation enums (`AUTO_CONFIRMED`, `EXACT_COUNTERPARTY_AMOUNT_UNIQUE`)
remain fully traceable, but never as the first thing a business user
reads. Each has a Chinese business label as the primary text
(`CONFIRMATION_TYPE_LABELS`, `MATCH_METHOD_LABELS`,
`viewmodels.BLOCKER_TITLES`) with the raw literal demoted into a
`<details>` "技术详情"/"技术信息" block. This is presentation-only:
`period_close.py` and the Domain enums are untouched, and every raw
value is still asserted reachable by the regression suite (spec 11.E).

## Missing product evidence is a Fact-field rule, not a name pattern

Some `ContractItem` rows genuinely have no `product_name` — the contract
ledger has no item-level columns at all (only the Close Fact Pack can
create a `ContractItem`, and only when a human supplies one), so
`source_item_key` alone (e.g. `ITEM-1`) is a technical scope reference,
never a real product name. `viewmodels.ItemPresentationVM` is the single
shared rule, used identically everywhere a `ContractItem` can appear
(Period Close reversal/accrual/difference/blocker rows, Contract360's
合同范围/商品明细 table, and the 当前暂估余额 panel):

```
product_name present  -> display = product_name
product_name missing  -> display = "未提供商品明细"
                          + evidence_note = "当前暂估仅有合同范围证据"
                          (Contract360 item table: 证据完整性 = "仅合同范围")
```

The rule keys **only** on whether `product_name` is populated — never on
`source_item_key`'s value (no `if key == "ITEM-1"`), so it works
identically for `ITEM-A`, `ITEM-1`, or any future key. This required
changing `period_close_workbench.py`'s `WorkbenchReversal` /
`WorkbenchAccrual` / `WorkbenchDifference` / `WorkbenchBlocker` from a
pre-collapsed `item_label: str` field (which used to fall back to
`source_item_key` — the actual bug: "ITEM-1" could read as a product
name) to `item: ContractItem | None`, letting the Web layer apply
`ItemPresentationVM` uniformly. This is an application query DTO
change (explicitly allowed) — it doesn't touch the Rule Engine, and the
`WorkbenchXxx.decision`/`.blocker` fields (what the parity test compares)
are unchanged.

The one legitimate exception: the manual-allocation `<select>` needs
`source_item_key` as its option value (it's choosing a technical scope,
not displaying a product) — its option *text* still prefers
`product_name` and falls back to `"未提供商品明细（{source_item_key}）"`
rather than a bare key.

## Candidate supplier aggregation is presentation only

`viewmodels.CandidateSupplierGroupVM` / `_group_candidates_by_supplier`
group the *same* `CandidateRowVM` objects `PeriodCloseVM` already builds
from `preview.contract_level_candidates` — grouping never creates a new
Fact or Decision, and the ungrouped per-contract table (with its own
Decision → Fact → Evidence trace) is still rendered inside each group's
`<details>`. Grouping key is `counterparty` text (falling back to "—"),
**not** `contract_no`: `contract_no` is a business key that is
deliberately not unique in the Domain (`ContractModel`'s own field
comment; see `docs/DOMAIN.md` and `docs/RULES.md` R004's
`BusinessKeyConflict` handling), so a duplicated `contract_no` under two
different counterparties must land in two different groups and never be
merged or overwritten. A permanent regression
(`test_c_candidates_group_by_supplier_without_changing_totals`)
constructs this exact case with independently-synthetic data and
asserts: group count, total candidate count, and cost sum all agree with
the raw `preview.contract_level_candidates`, and the duplicated
contract_no's two rows both survive.

## Blocker business context is presentation composed from persisted Facts

`period_close_workbench.BlockerContext` (application layer) attaches
read-only facts to each `WorkbenchBlocker` — historical accrual amount,
current persisted remaining balance, confirmed in-period invoices (keys,
net total, item-line count), existing item-allocation count, and (for
`MISSING_ACCRUAL_BASIS`) the cost-recognition date. Every field is read
back from an already-persisted Fact via the existing repositories
(`AccrualRepository`, `InvoiceAllocationRepository`,
`InvoiceItemAllocationRepository`, ...) — it never re-evaluates whether a
blocker should exist, never picks an allocation, and never invents a
scope decision. `period_close.py` remains the *only* source of blocker
existence; `WorkbenchBlocker.blocker` is still the raw, unmodified
`CloseBlocker` the parity test compares.

The Web layer (`viewmodels.BLOCKER_TITLES`/`BLOCKER_REASONS`/
`BLOCKER_NEXT_STEPS`) turns that context into a business card: title,
known facts, "无法判断" reason, "下一步", and a fixed disclaimer
(`BLOCKER_NO_ACTION_NOTE` — "当前版本尚不支持在此直接确认冲销范围。")
instead of a fake "立即处理" action, because Phase 2C.2 adds no new
write command. The only action is a link to Contract360. The raw
blocker code is still rendered — inside a `技术详情` block, structurally
after the business content (a regression test asserts the code's string
position is *after* the "技术详情" marker, not merely present anywhere
in the page).

## No Rule Engine, Domain, or schema change

`src/bel/application/period_close.py` (R001/R002/R003/R005/R006/R007),
`src/bel/domain/accrual.py` (Accrual/AccrualReversal semantics), and
every Alembic migration are byte-identical to Phase 2C.1. No
`CONTRACT`-scope `Accrual` was introduced (an `AccrualBasisFact` can be
`CONTRACT`-scope, producing a `Contract-Level Candidate` — that was
already true before 2C.2 and is unchanged; a real `Accrual` row is still
always item-scoped). `tests/web/test_web_phase2c2_ui.py::test_g_*`
re-confirms parity: `[b.blocker for b in workbench.blockers] ==
list(preview.blockers)` and `workbench.summary == preview.summary` still
hold after the `item`/`context` DTO changes.

## Explicitly deferred (not implemented in 2C.2)

- **CONTRACT-scope Accrual.** A real formal Accrual stays item-scoped
  only; a contract with no complete `ContractItem` evidence can only
  produce a `Contract-Level Candidate` (never a real Accrual). This
  remains a confirmed future Domain evolution point, out of scope here.
- **UnresolvedBusinessFact.** A business situation can exist where a
  known counterparty and amount cannot be uniquely attributed to one
  `Contract` from available evidence alone. Phase 2C.2 does not invent a
  new domain model or UI surface for this — such a case is never forced
  into the contract-level candidate list, no placeholder contract is
  shown, and no side-channel/private-baseline value is injected into the
  page. It is recorded here as a named future extension point, not
  built. Public documentation records this as a design principle only —
  it does not record whether, how often, or in what combination this
  situation has occurred against any private dataset; that belongs
  exclusively under `$BEL_PRIVATE_DATA_ROOT/reports/`, never in this
  repository.

## Gate A remediation

Two findings from the first Gate A pass were fixed without any Rule
Engine, Domain, or schema change:

1. **Placeholder evidence presentation gap.** The "未提供商品明细" +
   "当前暂估仅有合同范围证据" two-layer rule was applied inconsistently
   — present on some rows, missing on others (Period Close's 成本差异
   table, and every row inside Contract360's 本期业务判断 section).
   Fixed by introducing one shared Jinja macro,
   `partials/_macros.html::item_scope(item)`, and routing every template
   location that shows a `ContractItem`'s scope through it instead of a
   hand-copied conditional — the same failure mode (one spot quietly
   missing the note) cannot recur because there is now exactly one place
   the two-layer text is written. `ContractItemVM` also gained a nested
   `.item` (`ItemPresentationVM`) so the macro applies uniformly there
   too. A permanent regression
   (`test_b_placeholder_evidence_covers_every_required_path`) checks all
   eight required paths independently, by slicing the rendered page
   between section headings so a pass in one section can't paper over a
   miss in another.
2. **Private-derived content in committed documentation.** An earlier
   draft of this document's "Explicitly deferred" and "Candidate supplier
   aggregation" sections stated a specific private record count and
   described a specific private-data combination as an already-confirmed
   real shape. Both were rewritten to state only generic design
   principles and cite public, code-level justification (e.g.
   `ContractModel`'s own "business key, deliberately not unique" field
   comment) instead. Public documentation records design intent and
   independently-synthetic examples only; it never records private
   replay counts, combinations, or outcomes.

`tools/privacy_scan.py` gained a fourth mode, `--untracked` (scans
`git ls-files --others --exclude-standard`), so a new file that hasn't
been `git add`ed yet — exactly how the finding above reached a commit
candidate — is covered before commit, alongside the existing
tracked/history/staged modes. This is the smallest form of the fix: it
reuses `check_path`/`check_file_content` unchanged and adds no new guard
category.

## Test data

Every new Phase 2C.2 test (`tests/web/test_web_phase2c2_ui.py`) builds
its own hand-seeded, independently-synthetic database (`PO-UI-*`,
`SupplierUi*` — invented, never derived from a non-public file), the
same discipline as the existing `tests/web/test_web_attack_gates.py`
Gate B/C scenarios. No private contract number, supplier name, amount,
or quantity from local manual acceptance ever entered a test, a
docstring, or this document.

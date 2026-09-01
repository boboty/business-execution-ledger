"""Web presentation layer for the Phase 2C.2 workbench.

Everything here is display-only: blocker business copy, status labels,
fact field labels, item-evidence presentation, and the Decision -> Fact
-> Evidence trace view models. Jinja templates operate on these view
models only; they never touch repositories or the Rule Engine.

Phase 2C.2 four-layer UI semantic (docs/PHASE2C2-DECISIONS.md):
  Fact ≠ Decision ≠ Projected State ≠ Execution.
A ``PROJECTED_STATUS_LABELS`` value (e.g. "红冲后：全部冲销") describes
what would happen if this period's Decision were executed — it must
never be confused with ``CURRENT_STATUS_LABELS`` (e.g. "已冲销"), which
describes an already-persisted state. Blocker meanings/titles/reasons
are Presentation (spec section 8) — they never change what the close
engine blocks; ``BlockerContext`` (application layer) supplies only
already-persisted Facts, never a new judgment.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from bel.application.contract_360 import (
    Contract360,
    ContractAccrual,
    ContractEvidence,
    ContractInvoice,
    ContractPayment,
)
from bel.application.invoice_preparation_workbench import InvoicePreparationWorkbench
from bel.application.period_close import (
    ITEM_MATCH_REQUIRED_FOR_REVERSAL,
    MISSING_ACCRUAL_BASIS,
    MISSING_CONTRACT_ITEM_EVIDENCE,
    MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE,
    MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE,
)
from bel.application.period_close_workbench import (
    FactNode,
    PeriodCloseWorkbench,
    WorkbenchBlocker,
    WorkbenchCandidate,
    WorkbenchDifference,
    WorkbenchReversal,
)
from bel.application.sales_invoice_preparation import (
    SalesAmountCheckOutcome,
    SalesInvoiceAdvisoryCode,
)
from bel.application.supplier_invoice_request import (
    SupplierRequestAdvisoryCode,
    SupplierRequestBlockerCode,
    SupplierRequestCheckOutcome,
)
from bel.domain.contract import ContractItem

# ---- Blocker business copy (Presentation only — existence/type of a
# blocker is decided exclusively by period_close.py). ----

BLOCKER_TITLES = {
    ITEM_MATCH_REQUIRED_FOR_REVERSAL: "发票已经确认到本合同，但尚未确认对应哪一项合同商品",
    MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE: "存在多笔未冲销的历史暂估，无法判断本次到票归属哪一笔",
    MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE: "发票总额已确认，但明细范围不足以自动冲销",
    MISSING_ACCRUAL_BASIS: "已满足成本确认条件，但缺少可确认的暂估成本依据",
}

# Kept for back-compat with places that want the short one-line meaning
# (e.g. a compact tag) alongside the fuller business card below.
BLOCKER_MEANINGS = {
    ITEM_MATCH_REQUIRED_FOR_REVERSAL: "已确认到票，但尚未确认发票明细对应哪个合同商品",
    MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE: "同一商品存在多笔未结暂估，无法判断此次到票对应哪一笔",
    MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE: "存在多条可用发票明细，无法判断实际成本来源",
    MISSING_ACCRUAL_BASIS: "已满足成本确认条件，但缺少可确认的暂估成本依据",
}

BLOCKER_REASONS = {
    ITEM_MATCH_REQUIRED_FOR_REVERSAL: (
        "发票已经确认对应本合同，但历史暂估是按具体合同商品记录的。现有 Evidence 不能证明这张发票"
        "对应哪一项暂估，因此系统不自动选择，也不按到票顺序猜测。"
    ),
    MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE: (
        "同一项合同商品同时存在多笔尚未冲销的历史暂估。现有到票不能证明本次到票应冲销其中哪一笔，"
        "因此系统不按录入顺序或金额自动选择。"
    ),
    MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE: (
        "历史暂估只有当前可证明的范围，而发票存在多条明细。现有 Evidence 不能证明历史暂估原本如何"
        "拆到这些发票明细，因此系统不自动选择其中一条作为成本来源。"
    ),
    MISSING_ACCRUAL_BASIS: (
        "合同已经满足成本确认条件，但目前没有任何可用的暂估成本依据（合同级或合同商品级），"
        "因此系统无法计算暂估金额。"
    ),
}

BLOCKER_NEXT_STEPS = {
    ITEM_MATCH_REQUIRED_FOR_REVERSAL: "在合同360°中把这张发票的具体明细关联到对应的合同商品。",
    MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE: "明确说明本次到票应归属于哪一笔历史暂估。",
    MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE: "补充原始暂估的商品明细，或明确本次冲销应覆盖发票的哪些明细。",
    MISSING_ACCRUAL_BASIS: "补充该合同的暂估成本依据（合同级或合同商品级）。",
}

BLOCKER_NO_ACTION_NOTE = "当前版本尚不支持在此直接确认冲销范围。"

CANDIDATE_REASON_MEANINGS = {
    MISSING_CONTRACT_ITEM_EVIDENCE: "当前已确认到合同范围，但缺少商品明细证据，暂不能形成正式暂估。",
}
CANDIDATE_STATUS_LABEL = "尚不能形成正式暂估"
CANDIDATE_GROUP_STATUS_LABEL = "待补合同商品明细"

FACT_KIND_LABELS = {
    "HISTORICAL_ACCRUAL": "历史暂估事实",
    "COST_RECOGNITION": "成本确认事实",
    "ACCRUAL_BASIS": "暂估依据事实",
    "MANUAL_ITEM_ALLOCATION": "发票明细关联",
}

FACT_FIELD_LABELS = {
    "period": "期间",
    "quantity": "数量",
    "estimated_cost": "暂估成本",
    "basis": "依据",
    "recognition_date": "确认日期",
    "scope_type": "范围",
    "allocated_quantity": "关联数量",
    "allocated_net_amount": "关联未税金额",
    "invoice_item_line_no": "发票行号",
    "invoice_external_key": "发票号码",
    "issue_date": "开票日期",
}

# Projected State — what would happen if this period's Decision were
# executed. Must never read as an already-executed fact (spec section 3).
PROJECTED_STATUS_LABELS = {
    "REVERSED": "红冲后：全部冲销",
    "PARTIALLY_REVERSED": "红冲后：部分冲销",
    "ACTIVE": "本期不涉及红冲",
}

# Current State — derived from already-persisted Facts (Contract360's
# 暂估余额 area). Legitimately allowed to say "已冲销" because it
# describes what already happened, not a preview.
CURRENT_STATUS_LABELS = {
    "ACTIVE": "未冲销",
    "PARTIALLY_REVERSED": "部分冲销",
    "REVERSED": "已冲销",
}

SCOPE_LABELS = {
    "CONTRACT_ITEM": "合同明细级",
    "CONTRACT": "合同级",
}

EVIDENCE_CATEGORY_LABELS = {
    "CONTRACT": "合同证据",
    "CONTRACT_ITEM": "合同范围 / 商品明细证据",
    "INVOICE": "发票证据",
    "PAYMENT": "付款证据",
    "HISTORICAL_ACCRUAL": "历史暂估证据",
    "ACCRUAL_BASIS": "暂估依据证据",
    "COST_RECOGNITION": "成本确认依据",
    "MANUAL_ITEM_ALLOCATION": "发票明细归属证据",
}

# Technical source_type literal -> business label for the primary Evidence
# table (spec section 7/8). An unknown source_type falls back to the raw
# value rather than being hidden; known values must never show the raw
# literal in the primary row — the raw value stays reachable under 技术信息.
SOURCE_TYPE_LABELS = {
    "contract_ledger_xlsx": "合同台账 Excel",
    "invoice_ledger_xlsx": "发票台账 Excel",
    "cmb_bank_statement_pdf": "银行流水 PDF",
    "close_fact_pack_json": "月结事实包",
}

DIRECTION_LABELS = {
    "PURCHASE": "进项",
    "SALES": "销项",
    "UNKNOWN": "未知",
}

# Technical enums that must appear as a Chinese business label in the
# primary UI, with the raw literal kept only in technical detail (spec
# section 7 / 11.E).
CONFIRMATION_TYPE_LABELS = {
    "AUTO_CONFIRMED": "系统确定性匹配",
    "HUMAN_CONFIRMED": "人工确认",
    "MANUAL_CONFIRMED": "人工确认",
}

MATCH_METHOD_LABELS = {
    "EXACT_COUNTERPARTY_AMOUNT_UNIQUE": "交易对手 + 金额唯一匹配",
}

INVOICE_CONTRACT_MATCH_STATUS_LABEL = "已确认到本合同"

# InvoiceItemScopePresentation (spec section 6/2) — the ladder for an
# InvoiceItem's relationship to the CURRENT contract, kept strictly
# separate from two other confirmation levels: Invoice -> Contract
# (INVOICE_CONTRACT_MATCH_STATUS_LABEL above) and Historical Accrual ->
# InvoiceItem reversal authorization (a Blocker decides that; this ladder
# never implies it). Presentation-only: no Domain enum, no DB field.
# Strength is judged ONLY from an existing Fact — whether an allocation's
# target ContractItem carries real product_name Evidence — never from
# whether the ContractItem object merely exists.
INVOICE_ITEM_SCOPE_UNASSIGNED = "UNASSIGNED"
INVOICE_ITEM_SCOPE_CONTRACT_SCOPE_ASSIGNED = "CONTRACT_SCOPE_ASSIGNED"
INVOICE_ITEM_SCOPE_CONTRACT_ITEM_CONFIRMED = "CONTRACT_ITEM_CONFIRMED"

INVOICE_ITEM_SCOPE_LABELS = {
    INVOICE_ITEM_SCOPE_UNASSIGNED: "尚未归属本合同范围",
    INVOICE_ITEM_SCOPE_CONTRACT_SCOPE_ASSIGNED: "已归属本合同范围",
    INVOICE_ITEM_SCOPE_CONTRACT_ITEM_CONFIRMED: "已确认到合同商品",
}


def _invoice_item_scope_state(allocations: Any, contract_item_by_id: dict) -> str:
    if not allocations:
        return INVOICE_ITEM_SCOPE_UNASSIGNED
    for allocation in allocations:
        target = contract_item_by_id.get(allocation.contract_item_id)
        if target is not None and target.product_name:
            return INVOICE_ITEM_SCOPE_CONTRACT_ITEM_CONFIRMED
    return INVOICE_ITEM_SCOPE_CONTRACT_SCOPE_ASSIGNED


# 未提供商品明细 — the primary business label whenever a ContractItem has
# no product_name. Presence/absence of product_name is the ONLY signal
# used (never a name-pattern check like "== 'ITEM-1'"): spec section 4.3.
NO_PRODUCT_EVIDENCE_LABEL = "未提供商品明细"
NO_PRODUCT_EVIDENCE_NOTE = "当前暂估仅有合同范围证据"
NO_PRODUCT_EVIDENCE_COMPLETENESS = "仅合同范围"


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"{value:,}"
    return str(value)


def _fmt_difference(value: Decimal | None) -> str:
    """0.00 must never render as a bare '0' — spec section 4.7."""
    if value is None:
        return "—"
    if value == 0:
        return f"{value:,.2f} · 无差异"
    return _fmt(value)


def _fragment_locator(fragment) -> str:
    """Locator is presentation: EXCEL_ROW -> sheet/row, otherwise
    locator_json as a flat string. Never raw_data."""
    if fragment is None:
        return "—"
    if fragment.sheet_name:
        row = f"第 {fragment.row_number} 行" if fragment.row_number is not None else ""
        return f"{fragment.sheet_name} {row}".strip()
    if fragment.locator_json:
        return " / ".join(f"{k}={v}" for k, v in fragment.locator_json.items())
    return "—"


class ItemPresentationVM:
    """The single shared rule for presenting a ContractItem's business
    scope, used identically on every page that can show one (Period
    Close, Contract360, Accrual Balance, Decision, Difference, Blocker —
    spec section 4.3). The rule depends ONLY on whether ``product_name``
    is populated, never on the value of ``source_item_key``."""

    def __init__(self, item: ContractItem | None) -> None:
        self.source_item_key = item.source_item_key if item is not None else None
        if item is None:
            self.display = "—"
            self.has_product_evidence: bool | None = None
            self.evidence_note: str | None = None
            self.completeness_label = "—"
        elif item.product_name:
            self.display = item.product_name
            self.has_product_evidence = True
            self.evidence_note = None
            self.completeness_label = "—"
        else:
            self.display = NO_PRODUCT_EVIDENCE_LABEL
            self.has_product_evidence = False
            self.evidence_note = NO_PRODUCT_EVIDENCE_NOTE
            self.completeness_label = NO_PRODUCT_EVIDENCE_COMPLETENESS


class TraceNodeVM:
    def __init__(self, node: FactNode) -> None:
        self.label = FACT_KIND_LABELS.get(node.fact_kind, node.fact_kind)
        self.fields = [
            (FACT_FIELD_LABELS.get(key, key), _fmt(value)) for key, value in node.fields
        ]
        self.source_type = node.document.source_type if node.document is not None else "—"
        self.document = node.document.file_name if node.document is not None else "—"
        self.locator = _fragment_locator(node.fragment)
        self.tech = []
        if node.fragment is not None:
            self.tech.append(("fragment id", str(node.fragment.id)))
            self.tech.append(("创建时间", _fmt(node.fragment.created_at)))
        if node.document is not None:
            self.tech.append(("document id", str(node.document.id)))
            self.tech.append(("sha256", node.document.sha256))


class DecisionTraceVM:
    def __init__(self, panel_id: str, nodes: list[FactNode]) -> None:
        self.panel_id = panel_id
        self.nodes = [TraceNodeVM(n) for n in nodes]


def _trace_vm(panel_id: str, nodes: list[FactNode]) -> DecisionTraceVM:
    return DecisionTraceVM(panel_id, nodes)


class ReversalRowVM:
    def __init__(self, row: WorkbenchReversal, index: int, differences_by_key: dict[tuple[uuid.UUID, uuid.UUID], WorkbenchDifference]) -> None:
        d = row.decision
        self.contract_id = d.contract_id
        self.contract_no = row.contract_no
        self.counterparty = row.counterparty or "—"
        self.item = ItemPresentationVM(row.item)
        self.source_period = d.source_period
        self.arrival_quantity = _fmt(d.reversal_quantity)
        self.reversal_cost = _fmt(d.reversal_estimated_cost)
        self.remaining_quantity = _fmt(d.projected_remaining_quantity)
        self.remaining_cost = _fmt(d.projected_remaining_cost)
        self.status = d.projected_status
        self.status_label = PROJECTED_STATUS_LABELS.get(d.projected_status, d.projected_status)
        diff = differences_by_key.get((d.contract_item_id, d.invoice_item_allocation_id))
        self.raw_difference = diff.decision.difference if diff is not None else None
        self.difference = _fmt_difference(self.raw_difference)
        self.raw_reversal_estimated_cost = d.reversal_estimated_cost
        self.trace = _trace_vm(f"trace-reversal-{index}", list(row.trace))


class AccrualRowVM:
    def __init__(self, row, index: int) -> None:
        d = row.decision
        self.contract_id = d.contract_id
        self.contract_no = row.contract_no
        self.counterparty = row.counterparty or "—"
        self.item = ItemPresentationVM(row.item)
        self.quantity = _fmt(d.quantity)
        self.estimated_cost = _fmt(d.estimated_cost)
        self.raw_estimated_cost = d.estimated_cost
        self.trace = _trace_vm(f"trace-accrual-{index}", list(row.trace))


class CandidateRowVM:
    def __init__(self, row: WorkbenchCandidate, index: int) -> None:
        d = row.decision
        self.contract_id = d.contract_id
        self.contract_no = row.contract_no
        self.counterparty = row.counterparty or "—"
        self.estimated_cost = _fmt(d.estimated_cost)
        self.raw_estimated_cost = d.estimated_cost
        self.missing_info = CANDIDATE_REASON_MEANINGS.get(d.blocking_reason, d.blocking_reason)
        self.blocking_reason = d.blocking_reason
        self.status_label = CANDIDATE_STATUS_LABEL
        self.trace = _trace_vm(f"trace-candidate-{index}", list(row.trace))


class DifferenceRowVM:
    def __init__(self, row: WorkbenchDifference, index: int) -> None:
        d = row.decision
        self.contract_id = d.contract_id
        self.contract_no = row.contract_no
        self.item = ItemPresentationVM(row.item)
        self.actual_net_cost = _fmt(d.actual_net_cost)
        self.reversed_estimated_cost = _fmt(d.reversed_estimated_cost)
        self.raw_difference = d.difference
        self.difference = _fmt_difference(d.difference)
        self.trace = _trace_vm(f"trace-difference-{index}", list(row.trace))


class BlockerRowVM:
    def __init__(self, row: WorkbenchBlocker, index: int) -> None:
        b = row.blocker
        ctx = row.context
        self.type = b.blocker_type
        self.title = BLOCKER_TITLES.get(b.blocker_type, b.blocker_type)
        self.reason = BLOCKER_REASONS.get(b.blocker_type, BLOCKER_MEANINGS.get(b.blocker_type, b.blocker_type))
        self.next_step = BLOCKER_NEXT_STEPS.get(b.blocker_type, "补充相应的业务证据。")
        self.no_action_note = BLOCKER_NO_ACTION_NOTE
        self.meaning = BLOCKER_MEANINGS.get(b.blocker_type, b.blocker_type)
        self.contract_no = row.contract_no or "—"
        self.contract_id = b.contract_id
        self.item = ItemPresentationVM(row.item)
        self.index = index

        known_facts: list[tuple[str, str]] = [("合同", self.contract_no)]
        if ctx.historical_estimated_cost is not None:
            periods = "、".join(ctx.historical_source_periods) or "—"
            known_facts.append((f"历史暂估成本（来源期间 {periods}）", _fmt(ctx.historical_estimated_cost)))
        if ctx.current_remaining_cost is not None:
            known_facts.append(("当前剩余暂估成本", _fmt(ctx.current_remaining_cost)))
        if ctx.confirmed_invoice_keys:
            known_facts.append(("已确认发票", "、".join(ctx.confirmed_invoice_keys)))
            known_facts.append(("已确认发票未税金额合计", _fmt(ctx.confirmed_invoice_net_total)))
            known_facts.append(("发票明细数量", str(ctx.invoice_item_line_count)))
        if ctx.existing_item_allocation_count:
            known_facts.append(("已归属本合同范围的发票明细数量", str(ctx.existing_item_allocation_count)))
        if ctx.cost_recognition_date is not None:
            known_facts.append(("成本确认日期", _fmt(ctx.cost_recognition_date)))
        self.known_facts = known_facts


class SummaryCardVM:
    def __init__(self, key: str, label: str, value: int, secondary: str, anchor: str) -> None:
        self.key = key
        self.label = label
        self.value = value
        self.secondary = secondary
        self.anchor = anchor


def _summary_cards(workbench: PeriodCloseWorkbench) -> list[SummaryCardVM]:
    reversal_cost_total = sum((r.decision.reversal_estimated_cost for r in workbench.reversals), Decimal("0"))
    accrual_cost_total = sum((a.decision.estimated_cost for a in workbench.accruals), Decimal("0"))
    candidate_cost_total = sum((c.decision.estimated_cost for c in workbench.candidates), Decimal("0"))
    difference_total = sum((d.decision.difference for d in workbench.differences), Decimal("0"))
    summary = workbench.summary
    return [
        SummaryCardVM(
            "reversals", "本期拟红冲", summary.get("prior_accrual_reversals", 0),
            f"拟红冲金额合计 {_fmt(reversal_cost_total)}", "reversals-section",
        ),
        SummaryCardVM(
            "accruals", "新增正式暂估", summary.get("new_accrual_requirements", 0),
            f"预计成本合计 {_fmt(accrual_cost_total)}", "accruals-section",
        ),
        SummaryCardVM(
            "candidates", "待补明细候选", summary.get("contract_level_candidates", 0),
            f"预计成本合计 {_fmt(candidate_cost_total)}", "candidates-section",
        ),
        SummaryCardVM(
            "differences", "成本差异", summary.get("accrual_actual_differences", 0),
            f"差异金额合计 {_fmt(difference_total)}", "differences-section",
        ),
        SummaryCardVM(
            "blockers", "阻塞待处理", summary.get("blockers", 0),
            "系统未自动执行", "blockers-section",
        ),
    ]


class CandidateSupplierGroupVM:
    """Presentation-only supplier aggregation of Contract-level Candidate
    rows (spec section 4.4). Never a new Fact or Decision: it groups the
    SAME CandidateRowVM objects the raw list already contains — grouping
    can never change the candidate count or the cost sum, and a
    duplicated contract_no with a different counterparty/contract_id
    stays a distinct row (and can land in a distinct group)."""

    def __init__(self, counterparty: str, rows: list[CandidateRowVM]) -> None:
        self.counterparty = counterparty
        self.rows = rows
        self.contract_count = len(rows)
        self.estimated_cost_total = sum((r.raw_estimated_cost for r in rows), Decimal("0"))
        self.estimated_cost_total_display = _fmt(self.estimated_cost_total)
        self.status_label = CANDIDATE_GROUP_STATUS_LABEL


def _group_candidates_by_supplier(candidates: list[CandidateRowVM]) -> list[CandidateSupplierGroupVM]:
    groups: dict[str, list[CandidateRowVM]] = {}
    for row in candidates:
        groups.setdefault(row.counterparty or "—", []).append(row)
    ordered = sorted(
        groups.items(),
        key=lambda kv: (-sum(r.raw_estimated_cost for r in kv[1]), kv[0]),
    )
    return [CandidateSupplierGroupVM(counterparty, rows) for counterparty, rows in ordered]


class PeriodCloseVM:
    def __init__(self, workbench: PeriodCloseWorkbench) -> None:
        self.period = workbench.period
        self.available_periods = list(workbench.available_periods)
        self.summary = _summary_cards(workbench)
        differences_by_key = {
            (d.decision.contract_item_id, d.decision.invoice_item_allocation_id): d for d in workbench.differences
        }
        self.reversals = [ReversalRowVM(r, i, differences_by_key) for i, r in enumerate(workbench.reversals)]
        self.accruals = [AccrualRowVM(a, i) for i, a in enumerate(workbench.accruals)]
        self.candidates = [CandidateRowVM(c, i) for i, c in enumerate(workbench.candidates)]
        self.candidate_supplier_groups = _group_candidates_by_supplier(self.candidates)
        self.differences = [DifferenceRowVM(d, i) for i, d in enumerate(workbench.differences)]
        self.blockers = [BlockerRowVM(b, i) for i, b in enumerate(workbench.blockers)]


class InvoiceItemVM:
    def __init__(self, item, allocations, contract_item_options, contract_item_by_id: dict) -> None:
        self.line_no = item.line_no
        self.product_name = item.product_name or "—"
        self.specification = item.specification or "—"
        self.quantity = _fmt(item.quantity)
        self.net_amount = _fmt(item.net_amount)
        self.raw_quantity = item.quantity
        self.raw_net_amount = item.net_amount
        self.allocations = allocations
        self.has_allocation = len(allocations) > 0
        self.scope_state = _invoice_item_scope_state(allocations, contract_item_by_id)
        self.allocation_status_label = INVOICE_ITEM_SCOPE_LABELS[self.scope_state]
        self.contract_item_options = contract_item_options


class InvoiceVM:
    def __init__(self, invoice360: ContractInvoice, contract_items) -> None:
        invoice = invoice360.invoice
        self.invoice_no = invoice.external_invoice_key or invoice.invoice_no or "—"
        self.issue_date = _fmt(invoice.issue_date)
        self.direction = invoice.direction
        self.direction_label = DIRECTION_LABELS.get(invoice.direction, invoice.direction)
        self.net_amount = _fmt(invoice.net_amount)
        self.tax_amount = _fmt(invoice.tax_amount)
        self.gross_amount = _fmt(invoice.gross_amount)
        self.match_method = invoice360.allocation.match_method
        self.match_method_label = MATCH_METHOD_LABELS.get(self.match_method, self.match_method)
        self.confirmation_type = invoice360.allocation.confirmation_type
        self.confirmation_type_label = CONFIRMATION_TYPE_LABELS.get(self.confirmation_type, self.confirmation_type)
        self.match_status_label = INVOICE_CONTRACT_MATCH_STATUS_LABEL
        contract_item_options = [
            (
                item.source_item_key,
                item.product_name if item.product_name else f"{NO_PRODUCT_EVIDENCE_LABEL}（{item.source_item_key}）",
            )
            for item in contract_items
            if item.source_item_key
        ]
        contract_item_by_id = {item.id: item for item in contract_items}
        self.items = [
            InvoiceItemVM(i.item, i.allocations, contract_item_options, contract_item_by_id)
            for i in invoice360.items
        ]


class PaymentVM:
    def __init__(self, payment360: ContractPayment) -> None:
        payment = payment360.payment
        self.transaction_date = _fmt(payment.transaction_date)
        self.direction = payment.direction
        self.direction_label = "付款" if payment.direction == "OUT" else ("收款" if payment.direction == "IN" else payment.direction)
        self.amount = _fmt(payment.amount)
        self.counterparty = payment.counterparty or "—"
        self.confirmation_type = payment360.allocation.confirmation_type
        self.confirmation_type_label = CONFIRMATION_TYPE_LABELS.get(self.confirmation_type, self.confirmation_type)
        self.match_method = payment360.allocation.match_method
        self.match_method_label = MATCH_METHOD_LABELS.get(self.match_method, self.match_method)


class AccrualBalanceVM:
    def __init__(self, accrual360: ContractAccrual) -> None:
        view = accrual360.view
        self.item = ItemPresentationVM(accrual360.item)
        self.source_period = accrual360.accrual.period
        self.original_quantity = _fmt(accrual360.accrual.quantity)
        self.original_estimated_cost = _fmt(accrual360.accrual.estimated_cost)
        self.reversed_quantity = _fmt(view.reversed_quantity)
        self.reversed_cost = _fmt(view.reversed_estimated_cost)
        self.remaining_quantity = _fmt(view.remaining_quantity)
        self.remaining_cost = _fmt(view.remaining_estimated_cost)
        self.status = view.projected_status
        # Current State (persisted balance) — "已冲销" is legitimate here,
        # never the Projected State labels used for this period's preview.
        self.status_label = CURRENT_STATUS_LABELS.get(view.projected_status, view.projected_status)


class EvidenceVM:
    def __init__(self, evidence: ContractEvidence) -> None:
        self.category_raw = evidence.category
        self.category = EVIDENCE_CATEGORY_LABELS.get(evidence.category, evidence.category)
        self.label = evidence.label
        self.source_type_raw = evidence.document.source_type
        self.source_type = SOURCE_TYPE_LABELS.get(self.source_type_raw, self.source_type_raw)
        self.locator = _fragment_locator(evidence.fragment)
        self.time = _fmt(evidence.fragment.created_at)
        self.metadata_items = sorted(evidence.fragment.raw_data.items())
        self.tech = [
            ("fragment id", str(evidence.fragment.id)),
            ("document id", str(evidence.document.id)),
            ("sha256", evidence.document.sha256),
            ("category", self.category_raw),
            ("source_type", self.source_type_raw),
        ]


class ContractItemVM:
    def __init__(self, item, accrual_balance: AccrualBalanceVM | None, requirement_item_ids: set) -> None:
        presentation = ItemPresentationVM(item)
        self.item = presentation
        self.source_item_key = item.source_item_key or "—"
        self.product_display = presentation.display
        self.has_product_evidence = presentation.has_product_evidence
        self.evidence_completeness_label = presentation.completeness_label
        self.specification = item.specification or "—"
        self.quantity = _fmt(item.quantity)
        self.unit = item.unit or "—"
        self.unit_price = _fmt(item.unit_price)
        self.gross_amount = _fmt(item.gross_amount)
        self.net_amount = _fmt(item.net_amount)
        self.id = item.id
        if item.id in requirement_item_ids:
            self.status_label = "待暂估"
            self.status_class = "tag-accrual-required"
        elif accrual_balance is not None:
            self.status_label = accrual_balance.status_label
            self.status_class = "tag-status"
        else:
            self.status_label = "无"
            self.status_class = "tag-none"


class ContractDecisionsVM:
    def __init__(self, decisions) -> None:
        self.reversals = [ReversalRowVM(r, i, {}) for i, r in enumerate(decisions.reversals)]
        self.accruals = [AccrualRowVM(a, i) for i, a in enumerate(decisions.accruals)]
        self.candidates = [CandidateRowVM(c, i) for i, c in enumerate(decisions.candidates)]
        self.differences = [DifferenceRowVM(d, i) for i, d in enumerate(decisions.differences)]
        self.blockers = [BlockerRowVM(b, i) for i, b in enumerate(decisions.blockers)]
        self.has_any = bool(self.reversals or self.accruals or self.candidates or self.differences or self.blockers)


UNRESOLVED_WORK_BADGE_LABEL = "有未结事项"
NO_UNRESOLVED_WORK_LABEL = "无"
UNKNOWN_CUSTOMER_LABEL = "客户待补充"

OUTBOUND_INVOICE_STATE_NOTE = "对外开票准备状态将在 2D.3 的 eligibility rule freeze 后提供。"


class LedgerShipmentVM:
    def __init__(self, shipment, item_by_id: dict) -> None:
        self.external_reference = shipment.external_reference or "—"
        self.execution_date = _fmt(shipment.execution_date)
        self.quantity = _fmt(shipment.quantity)
        # Phase 2D.3-F1c — canonical export/customs declaration values;
        # unknown stays unknown, never defaulted.
        self.declared_amount = _fmt(shipment.declared_amount)
        self.declared_currency = shipment.declared_currency or "—"
        self.item = ItemPresentationVM(item_by_id.get(shipment.contract_item_id))


class LedgerProcurementInvoiceVM:
    def __init__(self, entry) -> None:
        invoice = entry.invoice
        self.invoice_no = (invoice.external_invoice_key or invoice.invoice_no or "—") if invoice else "—"
        self.allocated_gross_amount = _fmt(entry.allocation.allocated_gross_amount)
        self.confirmation_type_label = CONFIRMATION_TYPE_LABELS.get(
            entry.allocation.confirmation_type, entry.allocation.confirmation_type
        )


class LedgerOutgoingPaymentVM:
    def __init__(self, entry) -> None:
        payment = entry.payment
        self.bank_reference = (payment.bank_reference or str(payment.id)) if payment else "—"
        self.allocated_amount = _fmt(entry.allocation.allocated_amount)
        self.confirmation_type_label = CONFIRMATION_TYPE_LABELS.get(
            entry.allocation.confirmation_type, entry.allocation.confirmation_type
        )


class LedgerAccrualVM:
    def __init__(self, entry, item_by_id: dict) -> None:
        self.item = ItemPresentationVM(item_by_id.get(entry.contract_item_id))
        self.period = entry.accrual.period
        self.remaining_quantity = _fmt(entry.remaining_quantity)
        self.remaining_estimated_cost = _fmt(entry.remaining_estimated_cost)
        self.reversed_quantity = _fmt(entry.reversed_quantity)
        self.reversed_estimated_cost = _fmt(entry.reversed_estimated_cost)
        # Current persisted state only — legitimately allowed "已冲销".
        self.status_label = CURRENT_STATUS_LABELS.get(entry.projected_status, entry.projected_status)


class LedgerSalesInvoiceAllocationVM:
    def __init__(self, entry) -> None:
        invoice = entry.invoice
        self.invoice_no = (invoice.external_invoice_key or invoice.invoice_no or "—") if invoice else "—"
        self.allocated_gross_amount = _fmt(entry.allocation.allocated_gross_amount)


class LedgerIncomingReceiptAllocationVM:
    def __init__(self, entry) -> None:
        payment = entry.payment
        self.bank_reference = (payment.bank_reference or str(payment.id)) if payment else "—"
        self.allocated_amount = _fmt(entry.allocation.allocated_amount)


class LedgerSalesScopeVM:
    """ONE linked SalesContract's own confirmed facts — never a figure
    attributed to the procurement row it is displayed on (spec section
    13). The same SalesContract may legitimately appear, with the SAME
    values, under more than one procurement row."""

    def __init__(self, scope) -> None:
        sc = scope.sales_contract
        self.sales_contract_id = sc.id
        self.sales_contract_no = sc.sales_contract_no
        self.our_entity = sc.our_entity
        self.customer = sc.customer or UNKNOWN_CUSTOMER_LABEL
        self.customer_known = sc.customer is not None
        self.currency = sc.currency or "—"
        self.gross_amount = _fmt(sc.gross_amount)
        self.contract_date = _fmt(sc.contract_date)
        self.sales_invoice_allocations = [LedgerSalesInvoiceAllocationVM(a) for a in scope.sales_invoice_allocations]
        self.incoming_receipt_allocations = [
            LedgerIncomingReceiptAllocationVM(a) for a in scope.incoming_receipt_allocations
        ]
        self.has_unresolved = scope.has_unresolved


class LedgerRowVM:
    def __init__(self, row) -> None:
        contract = row.contract
        self.contract_id = contract.id
        self.contract_no = contract.contract_no
        self.supplier = contract.counterparty or "—"
        self.our_entity = contract.buyer or "—"
        self.gross_amount = _fmt(contract.gross_amount)
        self.currency = contract.currency
        self.contract_date = _fmt(contract.contract_date)

        item_by_id = {item.id: item for item in row.items}
        self.items = [ItemPresentationVM(item) for item in row.items]
        self.item_count = len(row.items)
        self.shipments = [LedgerShipmentVM(s.shipment, item_by_id) for s in row.shipments]
        self.procurement_invoices = [LedgerProcurementInvoiceVM(i) for i in row.procurement_invoices]
        self.outgoing_payments = [LedgerOutgoingPaymentVM(p) for p in row.outgoing_payments]
        self.accruals = [LedgerAccrualVM(a, item_by_id) for a in row.accruals]
        self.sales_scopes = [LedgerSalesScopeVM(s) for s in row.sales_scopes]
        self.unresolved_summaries = [w.summary for w in row.unresolved_work]
        self.has_unresolved = row.has_unresolved
        self.unresolved_label = UNRESOLVED_WORK_BADGE_LABEL if row.has_unresolved else NO_UNRESOLVED_WORK_LABEL


class LedgerFiltersVM:
    def __init__(self, filters) -> None:
        self.contract_no = filters.contract_no or ""
        self.supplier = filters.supplier or ""
        self.our_entity = filters.our_entity or ""
        self.sales_contract_no = filters.sales_contract_no or ""
        self.customer = filters.customer or ""
        self.has_unresolved = filters.has_unresolved


class ContractBusinessLedgerVM:
    def __init__(self, ledger) -> None:
        self.rows = [LedgerRowVM(r) for r in ledger.rows]
        self.filters = LedgerFiltersVM(ledger.filters)
        self.row_count = len(self.rows)
        self.outbound_invoice_state_note = OUTBOUND_INVOICE_STATE_NOTE


class Contract360VM:
    def __init__(self, dto: Contract360, period: str) -> None:
        contract = dto.contract
        self.contract_no = contract.contract_no
        self.counterparty = contract.counterparty or "—"
        self.buyer = contract.buyer or "—"
        self.gross_amount = _fmt(contract.gross_amount)
        self.currency = contract.currency
        self.contract_date = _fmt(contract.contract_date)
        self.contract_type = contract.contract_type or "—"
        self.contract_id = contract.id
        self.period = period

        accrual_balances = [AccrualBalanceVM(a) for a in dto.accruals]
        accrual_balance_by_item = {
            a.item.id: vm for a, vm in zip(dto.accruals, accrual_balances) if a.item is not None
        }
        requirement_item_ids = {a.decision.contract_item_id for a in dto.decisions.accruals}
        self.items = [
            ContractItemVM(item, accrual_balance_by_item.get(item.id), requirement_item_ids) for item in dto.items
        ]
        self.invoices = [InvoiceVM(i, dto.items) for i in dto.invoices]
        self.payments = [PaymentVM(p) for p in dto.payments]
        self.accruals = accrual_balances
        self.evidence = [EvidenceVM(e) for e in dto.evidence]
        self.decisions = ContractDecisionsVM(dto.decisions)


# ---- Phase 2D.3-F0/F2a: /invoice-preparation — the integrated Invoice
# Preparation Workbench. ----
# Every fact label below describes known data ("已关联…事实", "已确认…",
# "待处理事项"). Deliberately NO label may read as a business judgment or
# workflow eligibility: 应开票 / 可开票 / 已开完 / 应请票 / 尚欠发票 /
# 本次请票 / 可以开票 / 不允许开票 / 已具备开票资格 all require a frozen
# workflow rule that does not exist. F1's frozen rules are presented as
# FACT CONTROL + MANAGEMENT REMINDERS (comparison results + non-blocking
# advisories), never as an approval verdict. Absence of a Fact is
# rendered as factual absence ("暂无已关联销项发票事实"), never as
# "未开票".

INVOICE_PREPARATION_PAGE_NOTE = (
    "本页为开票/请票事实控制与管理工作台：展示已确认事实、可确定性比较的"
    "核对结果与需要关注的管理提醒。它不构成开票资格判定，也不作为开票审批流程。"
)

# Fact-presence labels — "what we know", never "what should happen".
INVOICE_PREP_SALES_INVOICES_LABEL = "已关联销项发票事实"
INVOICE_PREP_SALES_RECEIPTS_LABEL = "已关联收款事实"
INVOICE_PREP_PROCUREMENT_INVOICES_LABEL = "已确认采购发票"
INVOICE_PREP_ITEM_ALLOCATIONS_LABEL = "已确认明细关联"
INVOICE_PREP_OUTGOING_PAYMENTS_LABEL = "已确认付款"
INVOICE_PREP_SHIPMENTS_LABEL = "已确认出货事实"
INVOICE_PREP_ITEMS_LABEL = "当前商品明细"
INVOICE_PREP_UNRESOLVED_LABEL = "待处理事项"
INVOICE_PREP_LINKED_CONTRACTS_LABEL = "已关联采购合同（当前关联）"

NO_LINKED_CONTRACTS_FACT_LABEL = "暂无当前关联采购合同"
NO_SALES_INVOICE_FACT_LABEL = "暂无已关联销项发票事实"
NO_SALES_RECEIPT_FACT_LABEL = "暂无已关联收款事实"
NO_PROCUREMENT_INVOICE_FACT_LABEL = "暂无已确认采购发票"
NO_ITEM_ALLOCATION_FACT_LABEL = "暂无已确认明细关联"
NO_OUTGOING_PAYMENT_FACT_LABEL = "暂无已确认付款"
NO_SHIPMENT_FACT_LABEL = "暂无已确认出货事实"
NO_ITEM_FACT_LABEL = "暂无已确认商品明细"
NO_UNRESOLVED_WORK_LABEL_2 = "无"

INVOICE_PREP_TAB_SALES = "向客户开票"
INVOICE_PREP_TAB_SUPPLIER = "向供应商要票"


class InvoicePrepSalesInvoiceVM:
    def __init__(self, entry) -> None:
        invoice = entry.invoice
        self.invoice_no = (invoice.external_invoice_key or invoice.invoice_no or "—") if invoice else "—"
        self.issue_date = _fmt(invoice.issue_date) if invoice else "—"
        self.gross_amount = _fmt(invoice.gross_amount) if invoice else "—"
        self.allocated_gross_amount = _fmt(entry.allocation.allocated_gross_amount)


class InvoicePrepSalesReceiptVM:
    def __init__(self, entry) -> None:
        payment = entry.payment
        self.bank_reference = (payment.bank_reference or str(payment.id)) if payment else "—"
        self.transaction_date = _fmt(payment.transaction_date) if payment else "—"
        self.amount = _fmt(payment.amount) if payment else "—"
        self.allocated_amount = _fmt(entry.allocation.allocated_amount)


class InvoicePrepLinkedContractVM:
    """One current ProcurementSalesLink + the procurement Contract it
    names. Enumeration only — this edge carries no amount anywhere."""

    def __init__(self, entry) -> None:
        contract = entry.contract
        self.contract_no = contract.contract_no if contract else "—"
        self.supplier = (contract.counterparty or "—") if contract else "—"
        self.confirmation_type_label = CONFIRMATION_TYPE_LABELS.get(
            entry.link.confirmation_type, entry.link.confirmation_type
        )
        self.contract_id = str(entry.link.procurement_contract_id)


class InvoicePrepUnresolvedWorkVM:
    def __init__(self, work) -> None:
        self.summary = work.summary
        self.exception_type = work.exception_type
        self.source = work.source


# ---- Phase 2D.3-F2a: the integrated Invoice Preparation Workbench.
# The comparison/advisory ENUM values from the F1 rule layers are
# translated into business-facing labels HERE, in one presentation layer.
# The template never interprets the raw enums; technical internal names
# (RULE_CONFLICT / INPUTS_PRESENT / NOT_COMPARABLE_* / the advisory and
# blocker codes) never leak into primary business UI. No label reads as
# workflow eligibility: nothing says 可开票 / 不可开票 / 已具备开票资格. ----

SALES_AMOUNT_CONTROL_OUTCOME_LABELS = {
    SalesAmountCheckOutcome.MATCH: "金额核对一致",
    SalesAmountCheckOutcome.DEVIATION: "金额存在偏差，建议复核",
    SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT: "当前信息不足，暂无法核对",
    SalesAmountCheckOutcome.NOT_COMPARABLE_CURRENCY_MISMATCH: "币种不同，暂不直接比较金额",
    SalesAmountCheckOutcome.NOT_COMPARABLE_AMBIGUOUS_SCOPE: "对应范围不唯一，暂无法自动核对",
}

# CSS tag class per comparison outcome — legible at a glance: MATCH is
# neutral-positive, DEVIATION / currency-mismatch is a review signal, an
# unavailable comparison is muted (never a red "blocked").
SALES_AMOUNT_CONTROL_OUTCOME_TAG = {
    SalesAmountCheckOutcome.MATCH: "tag-match",
    SalesAmountCheckOutcome.DEVIATION: "tag-deviation",
    SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT: "tag-unavailable",
    SalesAmountCheckOutcome.NOT_COMPARABLE_CURRENCY_MISMATCH: "tag-deviation",
    SalesAmountCheckOutcome.NOT_COMPARABLE_AMBIGUOUS_SCOPE: "tag-unavailable",
}

SALES_INVOICE_ADVISORY_LABELS = {
    SalesInvoiceAdvisoryCode.SALES_INVOICE_AMOUNT_DEVIATION: "销项发票金额与合同/报关金额存在偏差，建议复核",
    SalesInvoiceAdvisoryCode.SALES_INVOICE_CURRENCY_DEVIATION: "销项发票币种与合同/报关币种不一致，暂不直接比较金额，建议复核",
}

SUPPLIER_AMOUNT_CHECK_OUTCOME_LABELS = {
    SupplierRequestCheckOutcome.MATCH: "已有发票与参考信息一致",
    SupplierRequestCheckOutcome.DEVIATION: "存在金额偏差，建议复核",
    SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT: "当前信息不足或范围无法直接比较",
    SupplierRequestCheckOutcome.NOT_COMPARABLE_CURRENCY_MISMATCH: "币种不同，暂不直接比较金额",
}

SUPPLIER_AMOUNT_CHECK_OUTCOME_TAG = {
    SupplierRequestCheckOutcome.MATCH: "tag-match",
    SupplierRequestCheckOutcome.DEVIATION: "tag-deviation",
    SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT: "tag-unavailable",
    SupplierRequestCheckOutcome.NOT_COMPARABLE_CURRENCY_MISMATCH: "tag-deviation",
}

SUPPLIER_ITEM_NAME_CHECK_OUTCOME_LABELS = {
    SupplierRequestCheckOutcome.MATCH: "商品名称与合同确认名称一致",
    SupplierRequestCheckOutcome.DEVIATION: "商品名称与合同确认名称不一致，建议复核",
    SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT: "当前信息不足，暂无法核对",
}

SUPPLIER_INVOICE_ADVISORY_LABELS = {
    SupplierRequestAdvisoryCode.SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED: (
        "已付款，尚未收到对应进项发票，建议催供应商开票"
    ),
    SupplierRequestAdvisoryCode.PURCHASE_INVOICE_AMOUNT_DEVIATION: "采购发票金额与合同参考金额存在偏差，建议复核",
    SupplierRequestAdvisoryCode.PURCHASE_INVOICE_CURRENCY_DEVIATION: "采购发票币种与合同参考币种不一致，暂不直接比较金额，建议复核",
    SupplierRequestAdvisoryCode.PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION: "商品名称与合同确认名称不一致，建议复核",
    SupplierRequestAdvisoryCode.MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT: "一个采购合同关联多张已确认采购发票，建议复核",
    SupplierRequestAdvisoryCode.PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS: "一张采购发票关联多个采购合同，建议复核",
}

# The sole genuinely-required-data finding on the supplier side — a
# factual statement ("amount not provided"), never an eligibility verdict.
SUPPLIER_REQUEST_BLOCKER_LABELS = {
    SupplierRequestBlockerCode.MISSING_CONTRACT_GROSS_AMOUNT: "合同金额未提供，暂无法确定参考金额",
}

class SalesAmountControlVM:
    """The F1f IP-S02 three-way comparison, presented for review: outcome
    label + the three compared legs (None -> "—", never a fake zero or a
    fabricated value)."""

    def __init__(self, check) -> None:
        self.outcome_label = SALES_AMOUNT_CONTROL_OUTCOME_LABELS.get(check.outcome, check.outcome)
        self.outcome_tag = SALES_AMOUNT_CONTROL_OUTCOME_TAG.get(check.outcome, "tag-unavailable")
        self.contract_amount = _fmt(check.sales_contract_amount)
        self.contract_currency = check.sales_contract_currency or "—"
        self.declared_amount = _fmt(check.declared_amount)
        self.declared_currency = check.declared_currency or "—"
        self.invoice_amount = _fmt(check.sales_invoice_amount)
        self.invoice_currency = check.sales_invoice_currency or "—"


class SalesInvoiceAdvisoryVM:
    """One non-blocking sales review signal — business copy only; the
    internal advisory code never appears in primary UI."""

    def __init__(self, advisory) -> None:
        self.label = SALES_INVOICE_ADVISORY_LABELS.get(advisory.code, advisory.code)


class SupplierAmountCheckVM:
    """The F1e currency-safe P02 amount check, presented for review."""

    def __init__(self, check) -> None:
        self.outcome_label = SUPPLIER_AMOUNT_CHECK_OUTCOME_LABELS.get(check.outcome, check.outcome)
        self.outcome_tag = SUPPLIER_AMOUNT_CHECK_OUTCOME_TAG.get(check.outcome, "tag-unavailable")
        self.contract_amount = _fmt(check.contract_gross_amount)
        self.contract_currency = check.contract_currency or "—"
        self.invoice_amount = _fmt(check.compared_invoice_gross_amount)
        self.invoice_currency = check.compared_invoice_currency or "—"


class SupplierItemNameCheckVM:
    """The P05 product-name comparison, presented for review."""

    def __init__(self, check) -> None:
        self.outcome_label = SUPPLIER_ITEM_NAME_CHECK_OUTCOME_LABELS.get(check.outcome, check.outcome)
        self.outcome_tag = SUPPLIER_AMOUNT_CHECK_OUTCOME_TAG.get(check.outcome, "tag-unavailable")
        self.contract_product_name = check.contract_product_name or "—"
        self.invoice_product_name = check.invoice_product_name or "—"


class SupplierRequestAdvisoryVM:
    """One non-blocking supplier review signal / management reminder —
    business copy only (e.g. 已付款，尚未收到对应进项发票，建议催供应商开票);
    the internal advisory code never appears in primary UI."""

    def __init__(self, advisory) -> None:
        self.label = SUPPLIER_INVOICE_ADVISORY_LABELS.get(advisory.code, advisory.code)


class InvoicePrepSalesScopeVM:
    def __init__(self, scope, decision) -> None:
        sc = scope.sales_contract
        self.sales_contract_id = sc.id
        self.sales_contract_no = sc.sales_contract_no
        self.our_entity = sc.our_entity
        self.customer = sc.customer or UNKNOWN_CUSTOMER_LABEL
        self.customer_known = sc.customer is not None
        self.currency = sc.currency or "—"
        self.gross_amount = _fmt(sc.gross_amount)
        self.contract_date = _fmt(sc.contract_date)
        self.linked_procurement_contracts = [
            InvoicePrepLinkedContractVM(entry) for entry in scope.linked_procurement_contracts
        ]
        self.invoice_allocations = [InvoicePrepSalesInvoiceVM(e) for e in scope.invoice_allocations]
        self.payment_allocations = [InvoicePrepSalesReceiptVM(e) for e in scope.payment_allocations]
        self.unresolved_work = [InvoicePrepUnresolvedWorkVM(w) for w in scope.unresolved_work]
        # F2a — the F1 decision's comparison + advisories, translated once
        # in the presentation layer. The amount check is always present
        # (every sales scope has a SalesContract); a NOT_COMPARABLE
        # outcome is presented as an unavailable comparison, never as a
        # blocker and never as "may not issue invoice".
        self.amount_control = SalesAmountControlVM(decision.amount_check) if decision.amount_check else None
        self.advisories = [SalesInvoiceAdvisoryVM(a) for a in decision.advisories]
        self.has_advisories = bool(self.advisories)


class InvoicePrepShipmentVM:
    def __init__(self, shipment, item_by_id: dict) -> None:
        self.external_reference = shipment.external_reference or "—"
        self.execution_date = _fmt(shipment.execution_date)
        self.quantity = _fmt(shipment.quantity)
        # Phase 2D.3-F1c — canonical export/customs declaration values,
        # exposed as facts only (IP-S02 full three-way comparison is not
        # implemented); unknown stays unknown.
        self.declared_amount = _fmt(shipment.declared_amount)
        self.declared_currency = shipment.declared_currency or "—"
        self.item = ItemPresentationVM(item_by_id.get(shipment.contract_item_id))


class InvoicePrepProcurementInvoiceVM:
    def __init__(self, entry) -> None:
        invoice = entry.invoice
        self.invoice_no = (invoice.external_invoice_key or invoice.invoice_no or "—") if invoice else "—"
        self.issue_date = _fmt(invoice.issue_date) if invoice else "—"
        self.allocated_gross_amount = _fmt(entry.allocation.allocated_gross_amount)
        self.confirmation_type_label = CONFIRMATION_TYPE_LABELS.get(
            entry.allocation.confirmation_type, entry.allocation.confirmation_type
        )


class InvoicePrepItemAllocationVM:
    """One current InvoiceItemAllocation + its InvoiceItem Fact — facts
    only, no remaining quantity/amount concept."""

    def __init__(self, entry) -> None:
        invoice_item = entry.invoice_item
        invoice = entry.invoice
        self.invoice_no = (invoice.external_invoice_key or invoice.invoice_no or "—") if invoice else "—"
        self.line_no = _fmt(invoice_item.line_no) if invoice_item is not None else "—"
        self.allocated_quantity = _fmt(entry.allocation.allocated_quantity)
        self.allocated_net_amount = _fmt(entry.allocation.allocated_net_amount)


class InvoicePrepOutgoingPaymentVM:
    def __init__(self, entry) -> None:
        payment = entry.payment
        self.bank_reference = (payment.bank_reference or str(payment.id)) if payment else "—"
        self.transaction_date = _fmt(payment.transaction_date) if payment else "—"
        self.allocated_amount = _fmt(entry.allocation.allocated_amount)
        self.confirmation_type_label = CONFIRMATION_TYPE_LABELS.get(
            entry.allocation.confirmation_type, entry.allocation.confirmation_type
        )


class InvoicePrepSupplierScopeVM:
    def __init__(self, scope, decision) -> None:
        contract = scope.contract
        self.contract_id = contract.id
        self.contract_no = contract.contract_no
        # Contract.buyer is OUR OWN entity — presented exactly as that,
        # never as a customer (docs/DOMAIN.md).
        self.our_entity = contract.buyer or "—"
        self.supplier = contract.counterparty or "—"
        self.gross_amount = _fmt(contract.gross_amount)
        self.currency = contract.currency
        self.contract_date = _fmt(contract.contract_date)

        item_by_id = {item.id: item for item in scope.items}
        self.items = [ItemPresentationVM(item) for item in scope.items]
        self.shipments = [InvoicePrepShipmentVM(s, item_by_id) for s in scope.shipments]
        self.invoice_allocations = [InvoicePrepProcurementInvoiceVM(e) for e in scope.invoice_allocations]
        self.invoice_item_allocations = [InvoicePrepItemAllocationVM(e) for e in scope.invoice_item_allocations]
        self.payment_allocations = [InvoicePrepOutgoingPaymentVM(e) for e in scope.payment_allocations]
        self.unresolved_work = [InvoicePrepUnresolvedWorkVM(w) for w in scope.unresolved_work]
        # F2a — the F1 decision's reference amount, management-control
        # checks and advisories, translated once in the presentation layer.
        self.expected_invoice_amount = _fmt(decision.expected_purchase_invoice_gross_amount)
        self.expected_invoice_currency = decision.expected_purchase_invoice_currency or "—"
        self.reference_known = decision.expected_purchase_invoice_gross_amount is not None
        self.amount_checks = [SupplierAmountCheckVM(c) for c in decision.amount_checks]
        self.item_name_checks = [SupplierItemNameCheckVM(c) for c in decision.item_name_checks]
        self.advisories = [SupplierRequestAdvisoryVM(a) for a in decision.advisories]
        self.has_advisories = bool(self.advisories)
        # The genuinely-required-data finding, presented factually.
        self.blocker_labels = [
            SUPPLIER_REQUEST_BLOCKER_LABELS.get(b.code, b.code) for b in decision.blockers
        ]


class InvoicePreparationVM:
    """The Phase 2D.3-F2a integrated Workbench projection. Built from the
    ONE read-only Workbench (F0 context + the two F1 reports over the same
    context): each scope VM pairs its F0 fact context with its F1
    decision, so Facts / Comparison / Advisory are never flattened into
    one status. Presentation only — no rule is decided here."""

    def __init__(self, workbench: InvoicePreparationWorkbench) -> None:
        dto = workbench.context
        sales_decision_by_id = {d.sales_contract_id: d for d in workbench.sales_report.decisions}
        supplier_decision_by_id = {d.contract_id: d for d in workbench.supplier_report.decisions}
        self.sales_scopes = [
            InvoicePrepSalesScopeVM(s, sales_decision_by_id[s.sales_contract.id]) for s in dto.sales_scopes
        ]
        self.supplier_scopes = [
            InvoicePrepSupplierScopeVM(s, supplier_decision_by_id[s.contract.id]) for s in dto.supplier_scopes
        ]
        self.sales_scope_count = len(self.sales_scopes)
        self.supplier_scope_count = len(self.supplier_scopes)
        self.page_note = INVOICE_PREPARATION_PAGE_NOTE

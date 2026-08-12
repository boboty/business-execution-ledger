"""Web presentation layer for the Phase 2C workbench.

Everything here is display-only: blocker meanings, status labels, fact
field labels, and the Decision -> Fact -> Evidence trace view models.
Blocker meanings are Presentation (spec section 8) — they never change
what the close engine blocks. Jinja templates operate on these view
models only; they never touch repositories.
"""

from __future__ import annotations

import uuid
from typing import Any

from bel.application.contract_360 import (
    Contract360,
    ContractAccrual,
    ContractEvidence,
    ContractInvoice,
    ContractPayment,
)
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

BLOCKER_MEANINGS = {
    ITEM_MATCH_REQUIRED_FOR_REVERSAL: "已确认到票，但尚未确认发票明细对应哪个合同商品",
    MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE: "同一商品存在多笔未结暂估，无法判断此次到票对应哪一笔",
    MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE: "存在多条可用发票明细，无法判断实际成本来源",
    MISSING_ACCRUAL_BASIS: "已满足成本确认条件，但缺少可确认的暂估成本依据",
}

CANDIDATE_REASON_MEANINGS = {
    MISSING_CONTRACT_ITEM_EVIDENCE: "缺少 ContractItem Evidence，无法定位到具体合同商品",
}

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

STATUS_LABELS = {
    "ACTIVE": "未红冲",
    "PARTIALLY_REVERSED": "部分红冲",
    "REVERSED": "已红冲",
    "CONTRACT_ITEM": "合同明细级",
    "CONTRACT": "合同级",
}

EVIDENCE_CATEGORY_LABELS = {
    "CONTRACT": "合同 Evidence",
    "CONTRACT_ITEM": "合同商品 Evidence",
    "INVOICE": "发票 Evidence",
    "PAYMENT": "付款 Evidence",
    "HISTORICAL_ACCRUAL": "历史暂估事实 Evidence",
    "ACCRUAL_BASIS": "暂估依据 Evidence",
    "COST_RECOGNITION": "成本确认 Evidence",
    "MANUAL_ITEM_ALLOCATION": "人工明细关联 Evidence",
}

DIRECTION_LABELS = {
    "PURCHASE": "进项",
    "SALES": "销项",
    "UNKNOWN": "未知",
}


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"{value:,}"
    return str(value)


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
        self.item_label = row.item_label
        self.source_period = d.source_period
        self.arrival_quantity = _fmt(d.reversal_quantity)
        self.reversal_cost = _fmt(d.reversal_estimated_cost)
        self.remaining_quantity = _fmt(d.projected_remaining_quantity)
        self.remaining_cost = _fmt(d.projected_remaining_cost)
        self.status = d.projected_status
        self.status_label = STATUS_LABELS.get(d.projected_status, d.projected_status)
        diff = differences_by_key.get((d.contract_item_id, d.invoice_item_allocation_id))
        self.difference = _fmt(diff.decision.difference) if diff is not None else "—"
        self.trace = _trace_vm(f"trace-reversal-{index}", list(row.trace))


class AccrualRowVM:
    def __init__(self, row, index: int) -> None:
        d = row.decision
        self.contract_id = d.contract_id
        self.contract_no = row.contract_no
        self.counterparty = row.counterparty or "—"
        self.item_label = row.item_label
        self.quantity = _fmt(d.quantity)
        self.estimated_cost = _fmt(d.estimated_cost)
        self.trace = _trace_vm(f"trace-accrual-{index}", list(row.trace))


class CandidateRowVM:
    def __init__(self, row: WorkbenchCandidate, index: int) -> None:
        d = row.decision
        self.contract_id = d.contract_id
        self.contract_no = row.contract_no
        self.counterparty = row.counterparty or "—"
        self.estimated_cost = _fmt(d.estimated_cost)
        self.missing_info = CANDIDATE_REASON_MEANINGS.get(d.blocking_reason, d.blocking_reason)
        self.trace = _trace_vm(f"trace-candidate-{index}", list(row.trace))


class DifferenceRowVM:
    def __init__(self, row: WorkbenchDifference, index: int) -> None:
        d = row.decision
        self.contract_id = d.contract_id
        self.contract_no = row.contract_no
        self.item_label = row.item_label
        self.actual_net_cost = _fmt(d.actual_net_cost)
        self.reversed_estimated_cost = _fmt(d.reversed_estimated_cost)
        self.difference = _fmt(d.difference)
        self.trace = _trace_vm(f"trace-difference-{index}", list(row.trace))


class BlockerRowVM:
    def __init__(self, row: WorkbenchBlocker, index: int) -> None:
        b = row.blocker
        self.type = b.blocker_type
        self.meaning = BLOCKER_MEANINGS.get(b.blocker_type, b.blocker_type)
        self.contract_no = row.contract_no or "—"
        self.contract_id = b.contract_id
        self.item_label = row.item_label or "—"
        self.accrual_ids = b.accrual_ids
        self.index = index


class SummaryCardVM:
    def __init__(self, key: str, label: str, value: int) -> None:
        self.key = key
        self.label = label
        self.value = value


def _summary_cards(summary: dict[str, int]) -> list[SummaryCardVM]:
    return [
        SummaryCardVM("reversals", "历史暂估待红冲", summary.get("prior_accrual_reversals", 0)),
        SummaryCardVM("accruals", "新增暂估", summary.get("new_accrual_requirements", 0)),
        SummaryCardVM("candidates", "合同级待补明细", summary.get("contract_level_candidates", 0)),
        SummaryCardVM("differences", "成本差异", summary.get("accrual_actual_differences", 0)),
        SummaryCardVM("blockers", "阻塞项", summary.get("blockers", 0)),
    ]


class PeriodCloseVM:
    def __init__(self, workbench: PeriodCloseWorkbench) -> None:
        self.period = workbench.period
        self.available_periods = list(workbench.available_periods)
        self.summary = _summary_cards(workbench.summary)
        differences_by_key = {
            (d.decision.contract_item_id, d.decision.invoice_item_allocation_id): d for d in workbench.differences
        }
        self.reversals = [ReversalRowVM(r, i, differences_by_key) for i, r in enumerate(workbench.reversals)]
        self.accruals = [AccrualRowVM(a, i) for i, a in enumerate(workbench.accruals)]
        self.candidates = [CandidateRowVM(c, i) for i, c in enumerate(workbench.candidates)]
        self.differences = [DifferenceRowVM(d, i) for i, d in enumerate(workbench.differences)]
        self.blockers = [BlockerRowVM(b, i) for i, b in enumerate(workbench.blockers)]


class InvoiceItemVM:
    def __init__(self, item, allocations, contract_item_options) -> None:
        self.line_no = item.line_no
        self.product_name = item.product_name or "—"
        self.specification = item.specification or "—"
        self.quantity = _fmt(item.quantity)
        self.net_amount = _fmt(item.net_amount)
        self.raw_quantity = item.quantity
        self.raw_net_amount = item.net_amount
        self.allocations = allocations
        self.has_allocation = len(allocations) > 0
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
        self.confirmation_type = invoice360.allocation.confirmation_type
        contract_item_options = [
            (item.source_item_key, item.product_name or "—") for item in contract_items if item.source_item_key
        ]
        self.items = [InvoiceItemVM(i.item, i.allocations, contract_item_options) for i in invoice360.items]


class PaymentVM:
    def __init__(self, payment360: ContractPayment) -> None:
        payment = payment360.payment
        self.transaction_date = _fmt(payment.transaction_date)
        self.direction = payment.direction
        self.direction_label = "付款" if payment.direction == "OUT" else ("收款" if payment.direction == "IN" else payment.direction)
        self.amount = _fmt(payment.amount)
        self.counterparty = payment.counterparty or "—"
        self.confirmation_type = payment360.allocation.confirmation_type
        self.match_method = payment360.allocation.match_method


class AccrualBalanceVM:
    def __init__(self, accrual360: ContractAccrual) -> None:
        view = accrual360.view
        self.item_label = accrual360.item.product_name if accrual360.item is not None and accrual360.item.product_name else (accrual360.item.source_item_key if accrual360.item else "—")
        self.source_period = accrual360.accrual.period
        self.original_quantity = _fmt(accrual360.accrual.quantity)
        self.original_estimated_cost = _fmt(accrual360.accrual.estimated_cost)
        self.reversed_quantity = _fmt(view.reversed_quantity)
        self.reversed_cost = _fmt(view.reversed_estimated_cost)
        self.remaining_quantity = _fmt(view.remaining_quantity)
        self.remaining_cost = _fmt(view.remaining_estimated_cost)
        self.status = view.projected_status
        self.status_label = STATUS_LABELS.get(view.projected_status, view.projected_status)


class EvidenceVM:
    def __init__(self, evidence: ContractEvidence) -> None:
        self.category = EVIDENCE_CATEGORY_LABELS.get(evidence.category, evidence.category)
        self.label = evidence.label
        self.source_type = evidence.document.source_type
        self.locator = _fragment_locator(evidence.fragment)
        self.time = _fmt(evidence.fragment.created_at)
        self.metadata_items = sorted(evidence.fragment.raw_data.items())
        self.tech = [
            ("fragment id", str(evidence.fragment.id)),
            ("document id", str(evidence.document.id)),
            ("sha256", evidence.document.sha256),
        ]


class ContractItemVM:
    def __init__(self, item, accrual_balance: AccrualBalanceVM | None, requirement_item_ids: set) -> None:
        self.source_item_key = item.source_item_key or "—"
        self.product_name = item.product_name or "—"
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


class Contract360VM:
    def __init__(self, dto: Contract360) -> None:
        contract = dto.contract
        self.contract_no = contract.contract_no
        self.counterparty = contract.counterparty or "—"
        self.buyer = contract.buyer or "—"
        self.gross_amount = _fmt(contract.gross_amount)
        self.currency = contract.currency
        self.contract_date = _fmt(contract.contract_date)
        self.contract_type = contract.contract_type or "—"
        self.contract_id = contract.id

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

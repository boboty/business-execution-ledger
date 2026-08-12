"""Rule-level tests for the Phase 2B close engine (R001/R002/R003/R005/
R006/R007) plus the section-37 boundary attacks the system must survive:
no full reversal on a partial receipt, no duplicate accrual behind an
open PARTIALLY_REVERSED balance, and no guessed reversal when only the
contract-level match exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from bel.application.period_close import (
    ITEM_MATCH_REQUIRED_FOR_REVERSAL,
    MISSING_ACCRUAL_BASIS,
    MISSING_CONTRACT_ITEM_EVIDENCE,
    MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE,
    MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE,
    build_period_close_preview,
)
from bel.domain.accrual import (
    Accrual,
    AccrualBasisFact,
    AccrualBasisScopeType,
    AccrualReversal,
    AccrualStatus,
    CostRecognitionFact,
    InvoiceItemAllocation,
    get_accrual_balance,
    get_projected_accrual_status,
    is_open_accrual,
)
from bel.domain.contract import Contract, ContractItem
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.domain.matching import (
    AllocationMatchMethod,
    ConfirmationType,
    InvoiceAllocation,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
)
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    AccrualReversalRepository,
    AccrualRepository,
    ContractItemRepository,
    ContractRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    InvoiceAllocationRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
    MatchCaseRepository,
)

CLOSE_PERIOD = "2031-03"
PERIOD_END_DATE = "2031-03-31"
NOW = datetime.now(timezone.utc)


def _make_fragment(session, doc_sha=None):
    if doc_sha is None:
        doc_sha = uuid.uuid4().hex + uuid.uuid4().hex
    doc = EvidenceDocument(id=uuid.uuid4(), file_name="x", sha256=doc_sha, source_type="t", imported_at=NOW)
    evidence_repo = EvidenceRepository(session)
    evidence_repo.add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=doc.id,
        fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None,
        row_number=None,
        locator_json={"section": "test", "index": 0},
        raw_data={},
        created_at=NOW,
    )
    evidence_repo.add_fragment(frag)
    session.flush()
    return frag


def _make_contract(session, fragment_id, counterparty="Supplier", contract_no=None):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no or f"C-{uuid.uuid4().hex[:8]}",
        contract_type=None,
        counterparty=counterparty,
        buyer="Buyer Co",
        gross_amount=Decimal("5000.00"),
        currency="CNY",
        contract_date=None,
        current_source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


def _make_contract_item(session, contract, source_item_key="ITEM-A", quantity="100", fragment_id=None):
    item = ContractItem(
        id=uuid.uuid4(),
        contract_id=contract.id,
        source_item_key=source_item_key,
        sku=None,
        product_name="Widget",
        specification=None,
        quantity=Decimal(quantity) if quantity is not None else None,
        unit="件",
        unit_price=None,
        gross_amount=None,
        tax_rate=None,
        net_amount=None,
        current_source_fragment_id=fragment_id,
        created_at=NOW,
    )
    ContractItemRepository(session).add(item)
    session.flush()
    return item


def _make_invoice_with_item(session, fragment_id, seller, external_key, issue_date, quantity, net_amount, direction=InvoiceDirection.PURCHASE):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=direction,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=external_key,
        external_invoice_key=external_key,
        issue_date=issue_date,
        seller=seller,
        buyer="Buyer Co",
        net_amount=Decimal(net_amount),
        tax_amount=Decimal("0"),
        gross_amount=Decimal(net_amount),
        invoice_status=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    item = InvoiceItem(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        line_no=1,
        product_name="Widget",
        specification=None,
        unit="件",
        quantity=Decimal(quantity),
        unit_price=None,
        net_amount=Decimal(net_amount),
        tax_rate=None,
        tax_amount=Decimal("0"),
        gross_amount=Decimal(net_amount),
        source_fragment_id=fragment_id,
    )
    InvoiceItemRepository(session).add(item)
    session.flush()
    return invoice, item


def _confirm_contract_allocation(session, invoice, contract):
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type="INVOICE",
        subject_id=invoice.id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    InvoiceAllocationRepository(session).add(
        InvoiceAllocation(
            id=uuid.uuid4(),
            invoice_id=invoice.id,
            contract_id=contract.id,
            match_case_id=match_case.id,
            allocated_gross_amount=invoice.gross_amount,
            match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED,
            created_at=NOW,
        )
    )
    session.flush()


def _make_item_allocation(session, invoice_item, contract_item, quantity, net_amount, fragment_id, created_at=NOW):
    allocation = InvoiceItemAllocation(
        id=uuid.uuid4(),
        invoice_item_id=invoice_item.id,
        contract_item_id=contract_item.id,
        allocated_quantity=Decimal(quantity),
        allocated_net_amount=Decimal(net_amount),
        confirmation_type="MANUAL_CONFIRMED",
        source_fragment_id=fragment_id,
        created_at=created_at,
    )
    InvoiceItemAllocationRepository(session).add(allocation)
    session.flush()
    return allocation


def _make_cost_recognition(session, contract, fragment_id, recognition_date="2031-02-28", basis="MANUAL_CONFIRMED"):
    fact = CostRecognitionFact(
        id=uuid.uuid4(),
        contract_id=contract.id,
        recognition_date=datetime.strptime(recognition_date, "%Y-%m-%d").date(),
        basis=basis,
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    CostRecognitionFactRepository(session).add(fact)
    session.flush()
    return fact


def _make_basis(session, contract, scope_type, estimated_cost, fragment_id, contract_item_id=None, quantity=None):
    fact = AccrualBasisFact(
        id=uuid.uuid4(),
        scope_type=scope_type,
        contract_id=contract.id,
        contract_item_id=contract_item_id,
        quantity=Decimal(quantity) if quantity is not None else None,
        estimated_cost=Decimal(estimated_cost),
        basis="MANUAL_CONFIRMED",
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    AccrualBasisFactRepository(session).add(fact)
    session.flush()
    return fact


def _make_accrual(session, contract_item, period="2031-02", quantity="100", estimated_cost="1200.00"):
    accrual = Accrual(
        id=uuid.uuid4(),
        period=period,
        contract_item_id=contract_item.id,
        quantity=Decimal(quantity),
        estimated_cost=Decimal(estimated_cost),
        basis="MANUAL_CONFIRMED",
        status=AccrualStatus.ACTIVE,
        created_from_fact_id=uuid.uuid4(),
        created_at=NOW,
    )
    AccrualRepository(session).add(accrual)
    session.flush()
    return accrual


def _make_reversal(session, accrual, allocation, quantity, estimated_cost, period="2031-03"):
    reversal = AccrualReversal(
        id=uuid.uuid4(),
        accrual_id=accrual.id,
        period=period,
        invoice_item_allocation_id=allocation.id,
        reversed_quantity=Decimal(quantity),
        reversed_estimated_cost=Decimal(estimated_cost),
        created_at=NOW,
    )
    AccrualReversalRepository(session).add(reversal)
    reversals = AccrualReversalRepository(session).list_for_accrual(accrual.id)
    remaining_qty, _, reversed_qty, _ = get_accrual_balance(accrual, reversals)
    AccrualRepository(session).update_status(accrual.id, get_projected_accrual_status(reversed_qty, remaining_qty))
    return reversal


def _preview(session):
    session.flush()
    return build_period_close_preview(session, CLOSE_PERIOD)


def test_r001_prior_accrual_now_invoiced(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierA")
    item = _make_contract_item(db_session, contract, quantity="100")
    accrual = _make_accrual(db_session, item)
    invoice, invoice_item = _make_invoice_with_item(
        db_session, frag.id, "SupplierA", "INV-A", datetime.strptime("2031-03-15", "%Y-%m-%d").date(), "35", "455.00"
    )
    _confirm_contract_allocation(db_session, invoice, contract)
    _make_item_allocation(db_session, invoice_item, item, "35", "455.00", frag.id)
    db_session.flush()

    preview = _preview(db_session)
    assert len(preview.prior_accrual_reversals) == 1
    reversal = preview.prior_accrual_reversals[0]
    assert reversal.accrual_id == accrual.id
    assert reversal.reversal_quantity == Decimal("35")
    assert reversal.reversal_estimated_cost == Decimal("420.00")
    assert reversal.projected_status == AccrualStatus.PARTIALLY_REVERSED


def test_r006_partial_receipt_never_reverses_full_accrual(db_session):
    """Section 37 attack (a): historical qty 100, invoice qty 35. The
    system must reverse only 35 — a 100% reversal here is a BLOCKER."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierB")
    item = _make_contract_item(db_session, contract, quantity="100")
    accrual = _make_accrual(db_session, item, estimated_cost="1200.00")
    invoice, invoice_item = _make_invoice_with_item(
        db_session, frag.id, "SupplierB", "INV-B", datetime.strptime("2031-03-15", "%Y-%m-%d").date(), "35", "455.00"
    )
    _confirm_contract_allocation(db_session, invoice, contract)
    _make_item_allocation(db_session, invoice_item, item, "35", "455.00", frag.id)
    db_session.flush()

    preview = _preview(db_session)
    assert len(preview.prior_accrual_reversals) == 1
    reversal = preview.prior_accrual_reversals[0]
    assert reversal.reversal_quantity == Decimal("35"), "full reversal on a partial receipt is forbidden"
    assert reversal.reversal_estimated_cost == Decimal("420.00")
    assert reversal.projected_remaining_quantity == Decimal("65")
    assert reversal.projected_remaining_cost == Decimal("780.00")
    assert reversal.projected_status == AccrualStatus.PARTIALLY_REVERSED


def test_r006_last_reversal_uses_exact_clear(db_session):
    """Section 18: the final reversal uses remaining_estimated_cost to
    clear exactly — a non-terminating unit cost must not leave 0.01 of
    residue (qty 3 / 10.00 -> unit 3.3333 would give 9.99 otherwise)."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierC")
    item = _make_contract_item(db_session, contract, quantity="3")
    _make_accrual(db_session, item, quantity="3", estimated_cost="10.00")
    invoice, invoice_item = _make_invoice_with_item(
        db_session, frag.id, "SupplierC", "INV-C", datetime.strptime("2031-03-20", "%Y-%m-%d").date(), "3", "9.00"
    )
    _confirm_contract_allocation(db_session, invoice, contract)
    _make_item_allocation(db_session, invoice_item, item, "3", "9.00", frag.id)
    db_session.flush()

    preview = _preview(db_session)
    reversal = preview.prior_accrual_reversals[0]
    assert reversal.reversal_quantity == Decimal("3")
    assert reversal.reversal_estimated_cost == Decimal("10.00"), "final reversal must exact-clear"
    assert reversal.projected_status == AccrualStatus.REVERSED


def test_r005_actual_cost_difference(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierD")
    item = _make_contract_item(db_session, contract, quantity="100")
    _make_accrual(db_session, item, estimated_cost="1200.00")
    invoice, invoice_item = _make_invoice_with_item(
        db_session, frag.id, "SupplierD", "INV-D", datetime.strptime("2031-03-15", "%Y-%m-%d").date(), "35", "455.00"
    )
    _confirm_contract_allocation(db_session, invoice, contract)
    _make_item_allocation(db_session, invoice_item, item, "35", "455.00", frag.id)
    db_session.flush()

    preview = _preview(db_session)
    assert len(preview.accrual_actual_differences) == 1
    difference = preview.accrual_actual_differences[0]
    assert difference.actual_net_cost == Decimal("455.00")
    assert difference.reversed_estimated_cost == Decimal("420.00")
    assert difference.difference == Decimal("35.00")


def test_r002_new_item_level_accrual_required(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierE")
    item = _make_contract_item(db_session, contract, quantity="60")
    _make_cost_recognition(db_session, contract, frag.id)
    _make_basis(db_session, contract, AccrualBasisScopeType.CONTRACT_ITEM, "624.00", frag.id, contract_item_id=item.id, quantity="60")
    db_session.flush()

    preview = _preview(db_session)
    assert len(preview.new_accrual_requirements) == 1
    requirement = preview.new_accrual_requirements[0]
    assert requirement.level == "CONTRACT_ITEM"
    assert requirement.contract_item_id == item.id
    assert requirement.estimated_cost == Decimal("624.00")
    # Preview is a Decision only — it never INSERTs an Accrual.
    assert AccrualRepository(db_session).count() == 0


def test_r003_duplicate_guard_blocks_new_accrual(db_session):
    """Section 37 attack (b): a PARTIALLY_REVERSED accrual with remaining
    balance > 0 must block a new AccrualRequired for the same scope —
    even with cost recognition confirmed and no invoice."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierF")
    item = _make_contract_item(db_session, contract, quantity="100")
    accrual = _make_accrual(db_session, item, estimated_cost="500.00")
    invoice, invoice_item = _make_invoice_with_item(
        db_session, frag.id, "SupplierF", "INV-F", datetime.strptime("2031-02-15", "%Y-%m-%d").date(), "40", "200.00"
    )
    allocation = _make_item_allocation(db_session, invoice_item, item, "40", "200.00", frag.id)
    _make_reversal(db_session, accrual, allocation, "40", "200.00")
    _make_cost_recognition(db_session, contract, frag.id)
    db_session.flush()

    fresh_accrual = AccrualRepository(db_session).get(accrual.id)
    assert fresh_accrual.status == AccrualStatus.PARTIALLY_REVERSED
    preview = _preview(db_session)
    assert len(preview.new_accrual_requirements) == 0, "R003 must block a duplicate accrual"
    assert len(preview.prior_accrual_reversals) == 0, "already-consumed allocation must not re-reverse"


def test_r007_contract_level_candidate_when_item_detail_incomplete(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierG")
    _make_cost_recognition(db_session, contract, frag.id)
    _make_basis(db_session, contract, AccrualBasisScopeType.CONTRACT, "735.00", frag.id)
    db_session.flush()

    preview = _preview(db_session)
    assert len(preview.contract_level_candidates) == 1
    candidate = preview.contract_level_candidates[0]
    assert candidate.level == "CONTRACT"
    assert candidate.estimated_cost == Decimal("735.00")
    assert candidate.blocking_reason == MISSING_CONTRACT_ITEM_EVIDENCE
    assert AccrualRepository(db_session).count() == 0, "R007 forbids INSERTing an item-less Accrual"


def test_no_guessed_reversal_when_item_match_missing(db_session):
    """Section 37 attack (c): invoice confirmed at contract level but no
    item match exists — the system must block with
    ITEM_MATCH_REQUIRED_FOR_REVERSAL and never guess a reversal amount."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierH")
    item = _make_contract_item(db_session, contract, quantity="50")
    _make_accrual(db_session, item, estimated_cost="1000.00")
    invoice, _ = _make_invoice_with_item(
        db_session, frag.id, "SupplierH", "INV-H", datetime.strptime("2031-03-10", "%Y-%m-%d").date(), "50", "950.00"
    )
    _confirm_contract_allocation(db_session, invoice, contract)
    db_session.flush()

    preview = _preview(db_session)
    assert len(preview.prior_accrual_reversals) == 0, "no monetary reversal without an item match"
    assert len(preview.accrual_actual_differences) == 0
    assert any(
        b.blocker_type == ITEM_MATCH_REQUIRED_FOR_REVERSAL and b.contract_item_id == item.id
        for b in preview.blockers
    )


def test_missing_accrual_basis_is_diagnostic_not_decision(db_session):
    """Section 26: with cost recognition confirmed but no basis fact, the
    preview reports a MISSING_ACCRUAL_BASIS blocker — which is NOT the
    PROPOSED R011 EvidenceMissing Decision."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierI")
    _make_cost_recognition(db_session, contract, frag.id)
    db_session.flush()

    preview = _preview(db_session)
    assert len(preview.new_accrual_requirements) == 0
    assert len(preview.contract_level_candidates) == 0
    assert any(b.blocker_type == MISSING_ACCRUAL_BASIS for b in preview.blockers)
    assert not hasattr(preview, "evidence_missing")  # no such Decision type exists


def test_preview_writes_nothing(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierJ")
    item = _make_contract_item(db_session, contract, quantity="100")
    _make_accrual(db_session, item)
    invoice, invoice_item = _make_invoice_with_item(
        db_session, frag.id, "SupplierJ", "INV-J", datetime.strptime("2031-03-15", "%Y-%m-%d").date(), "35", "455.00"
    )
    _confirm_contract_allocation(db_session, invoice, contract)
    _make_item_allocation(db_session, invoice_item, item, "35", "455.00", frag.id)
    db_session.flush()

    counts_before = (
        AccrualRepository(db_session).count(),
        AccrualReversalRepository(db_session).count(),
        InvoiceItemAllocationRepository(db_session).count(),
    )
    preview = _preview(db_session)
    counts_after = (
        AccrualRepository(db_session).count(),
        AccrualReversalRepository(db_session).count(),
        InvoiceItemAllocationRepository(db_session).count(),
    )
    assert counts_before == counts_after
    assert preview.summary["prior_accrual_reversals"] == 1


def test_multiple_open_accruals_same_allocation_blocked(db_session):
    """Codex attack: two open Accruals reference the same ContractItem and
    one InvoiceItemAllocation is available. The engine must NOT FIFO-assign
    the allocation to either accrual — it emits
    MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE and produces no monetary
    reversal at all."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierK")
    item = _make_contract_item(db_session, contract, quantity="100")
    _make_accrual(db_session, item, period="2031-01", estimated_cost="900.00")
    _make_accrual(db_session, item, period="2031-02", estimated_cost="1000.00")
    invoice, invoice_item = _make_invoice_with_item(
        db_session, frag.id, "SupplierK", "INV-K", datetime.strptime("2031-03-15", "%Y-%m-%d").date(), "35", "455.00"
    )
    _confirm_contract_allocation(db_session, invoice, contract)
    _make_item_allocation(db_session, invoice_item, item, "35", "455.00", frag.id)
    db_session.flush()

    preview = _preview(db_session)
    assert len(preview.prior_accrual_reversals) == 0, "no FIFO winner may be chosen between two open accruals"
    assert len(preview.accrual_actual_differences) == 0
    blockers = [b for b in preview.blockers if b.blocker_type == MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE]
    assert len(blockers) == 1
    assert blockers[0].contract_item_id == item.id
    assert len(blockers[0].accrual_ids) == 2


def test_allocation_is_never_double_consumed_by_two_accruals(db_session):
    """Shared consumption: one InvoiceItemAllocation may never be consumed
    by two Accruals. When accrual B's reversal already consumed the whole
    allocation, open accrual A sees zero available — no reversal, and no
    contested-scope blocker (nothing is unclaimed)."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierL")
    item = _make_contract_item(db_session, contract, quantity="100")
    accrual_b = _make_accrual(db_session, item, period="2031-01", estimated_cost="1000.00")
    _make_accrual(db_session, item, period="2031-02", estimated_cost="1200.00")
    invoice, invoice_item = _make_invoice_with_item(
        db_session, frag.id, "SupplierL", "INV-L", datetime.strptime("2031-02-15", "%Y-%m-%d").date(), "40", "520.00"
    )
    _confirm_contract_allocation(db_session, invoice, contract)
    allocation = _make_item_allocation(db_session, invoice_item, item, "40", "520.00", frag.id)
    _make_reversal(db_session, accrual_b, allocation, "40", "400.00")
    db_session.flush()

    preview = _preview(db_session)
    assert len(preview.prior_accrual_reversals) == 0, "the allocation is already fully consumed — nothing to reverse"
    assert not any(
        b.blocker_type == MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE for b in preview.blockers
    ), "no unclaimed quantity means no contested scope"


def test_fully_reversed_sibling_accrual_still_consumes_shared_capacity(db_session):
    """A fully-reversed (closed) sibling Accrual's reversal still consumes
    the allocation's shared capacity — the open accrual must not see the
    same allocation as 'available' again and reverse it a second time."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierL2")
    item = _make_contract_item(db_session, contract, quantity="100")
    closed = _make_accrual(db_session, item, period="2031-01", estimated_cost="1000.00")
    _make_accrual(db_session, item, period="2031-02", estimated_cost="1200.00")
    invoice, invoice_item = _make_invoice_with_item(
        db_session, frag.id, "SupplierL2", "INV-L2", datetime.strptime("2031-02-15", "%Y-%m-%d").date(), "40", "520.00"
    )
    _confirm_contract_allocation(db_session, invoice, contract)
    allocation = _make_item_allocation(db_session, invoice_item, item, "40", "520.00", frag.id)
    _make_reversal(db_session, closed, allocation, "100", "1000.00")
    db_session.flush()

    assert not is_open_accrual(closed, AccrualReversalRepository(db_session).list_for_accrual(closed.id))

    preview = _preview(db_session)
    assert len(preview.prior_accrual_reversals) == 0, (
        "the open accrual must not re-consume an allocation already fully "
        "consumed by a closed sibling's reversal"
    )


def test_sales_invoice_never_drives_reversal(db_session):
    """Purchase invoice gate: a SALES invoice's item allocation must never
    reverse an accrual, and must not raise the item-match blocker either."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierM")
    item = _make_contract_item(db_session, contract, quantity="100")
    _make_accrual(db_session, item, estimated_cost="1200.00")
    invoice, invoice_item = _make_invoice_with_item(
        db_session,
        frag.id,
        "SupplierM",
        "INV-M",
        datetime.strptime("2031-03-15", "%Y-%m-%d").date(),
        "35",
        "455.00",
        direction=InvoiceDirection.SALES,
    )
    _confirm_contract_allocation(db_session, invoice, contract)
    _make_item_allocation(db_session, invoice_item, item, "35", "455.00", frag.id)
    db_session.flush()

    preview = _preview(db_session)
    assert len(preview.prior_accrual_reversals) == 0, "SALES invoices never drive a reversal"
    assert not any(
        b.blocker_type in {ITEM_MATCH_REQUIRED_FOR_REVERSAL, MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE}
        for b in preview.blockers
    )


def test_multiple_item_allocations_require_explicit_scope(db_session):
    """Codex attack: a single open Accrual faces two qualifying
    InvoiceItemAllocations with unclaimed quantity and different unit
    costs. Choosing which allocation supplies the reversed portion's
    actual cost by created_at would silently change the result — the
    engine must block instead. Order-independence is structural: with
    either creation order the outcome is the same blocker and zero
    monetary decisions."""
    from datetime import timedelta

    from bel.infrastructure.persistence.database import make_engine, make_session_factory
    from bel.infrastructure.persistence.models import Base

    def run_case(created_at_a, created_at_b):
        engine = make_engine(":memory:")
        Base.metadata.create_all(engine)
        session = make_session_factory(engine)()
        frag = _make_fragment(session)
        contract = _make_contract(session, frag.id, counterparty="SupplierP")
        item = _make_contract_item(session, contract, quantity="200")
        _make_accrual(session, item, estimated_cost="2400.00")
        invoice_a, invoice_item_a = _make_invoice_with_item(
            session, frag.id, "SupplierP", "INV-PA", datetime.strptime("2031-03-05", "%Y-%m-%d").date(), "35", "455.00"
        )
        invoice_b, invoice_item_b = _make_invoice_with_item(
            session, frag.id, "SupplierP", "INV-PB", datetime.strptime("2031-03-06", "%Y-%m-%d").date(), "40", "480.00"
        )
        _confirm_contract_allocation(session, invoice_a, contract)
        _confirm_contract_allocation(session, invoice_b, contract)
        _make_item_allocation(session, invoice_item_a, item, "35", "455.00", frag.id, created_at=created_at_a)
        _make_item_allocation(session, invoice_item_b, item, "40", "480.00", frag.id, created_at=created_at_b)
        session.flush()

        preview = build_period_close_preview(session, CLOSE_PERIOD)
        session.close()
        assert len(preview.prior_accrual_reversals) == 0, "no monetary reversal without explicit allocation scope"
        assert len(preview.accrual_actual_differences) == 0
        blockers = [
            b for b in preview.blockers if b.blocker_type == MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE
        ]
        assert len(blockers) == 1
        assert blockers[0].contract_item_id == item.id

    base = NOW
    run_case(base, base + timedelta(hours=1))
    run_case(base + timedelta(hours=1), base)


def test_purchase_invoice_blocks_new_accrual(db_session):
    """Section 22 gate, purchase side: a confirmed PURCHASE invoice dated
    by period_end suppresses a new AccrualRequired even before item
    matching exists."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierN2")
    item = _make_contract_item(db_session, contract, quantity="60")
    _make_cost_recognition(db_session, contract, frag.id)
    _make_basis(db_session, contract, AccrualBasisScopeType.CONTRACT_ITEM, "624.00", frag.id, contract_item_id=item.id, quantity="60")
    purchase_invoice, _ = _make_invoice_with_item(
        db_session,
        frag.id,
        "SupplierN2",
        "INV-N2",
        datetime.strptime("2031-03-15", "%Y-%m-%d").date(),
        "60",
        "780.00",
        direction=InvoiceDirection.PURCHASE,
    )
    _confirm_contract_allocation(db_session, purchase_invoice, contract)
    db_session.flush()

    preview = _preview(db_session)
    assert len(preview.new_accrual_requirements) == 0, (
        "a confirmed PURCHASE invoice by period_end means the purchase is "
        "already invoiced — no new accrual may be required"
    )


def test_sales_invoice_does_not_suppress_new_accrual(db_session):
    """Purchase invoice gate: section 22's 'already invoiced' suppression
    applies to PURCHASE invoices only — a SALES invoice must not stop a
    legitimate new AccrualRequired."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierN")
    item = _make_contract_item(db_session, contract, quantity="60")
    _make_cost_recognition(db_session, contract, frag.id)
    _make_basis(db_session, contract, AccrualBasisScopeType.CONTRACT_ITEM, "624.00", frag.id, contract_item_id=item.id, quantity="60")
    sales_invoice, _ = _make_invoice_with_item(
        db_session,
        frag.id,
        "SupplierN",
        "INV-N",
        datetime.strptime("2031-03-15", "%Y-%m-%d").date(),
        "60",
        "780.00",
        direction=InvoiceDirection.SALES,
    )
    _confirm_contract_allocation(db_session, sales_invoice, contract)
    db_session.flush()

    preview = _preview(db_session)
    assert len(preview.new_accrual_requirements) == 1, "a SALES invoice never counts as 'already invoiced'"
    assert preview.new_accrual_requirements[0].estimated_cost == Decimal("624.00")


def test_preview_is_truly_zero_write_total_changes_unchanged(db_session):
    """Strict read-only preview (kept permanently). Attack: a pending
    (unflushed) object sits in the session, the connection's
    total_changes counter is recorded, preview runs, and total_changes
    must be byte-identical — a preview that flushes or writes anything
    increments it."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, counterparty="SupplierO")
    item = _make_contract_item(db_session, contract, quantity="100")
    _make_accrual(db_session, item, estimated_cost="1200.00")
    invoice, invoice_item = _make_invoice_with_item(
        db_session, frag.id, "SupplierO", "INV-O", datetime.strptime("2031-03-15", "%Y-%m-%d").date(), "35", "455.00"
    )
    _confirm_contract_allocation(db_session, invoice, contract)
    _make_item_allocation(db_session, invoice_item, item, "35", "455.00", frag.id)
    db_session.flush()

    # A genuinely pending object: added to the session but never flushed.
    from bel.infrastructure.persistence.models import AccrualModel

    pending_model = AccrualModel(
        id=uuid.uuid4(),
        period="2031-04",
        contract_item_id=item.id,
        quantity=Decimal("50"),
        estimated_cost=Decimal("999.00"),
        basis="MANUAL_CONFIRMED",
        status=AccrualStatus.ACTIVE,
        created_from_fact_id=uuid.uuid4(),
        created_at=NOW,
    )
    db_session.add(pending_model)
    pending_before = set(db_session.new)
    assert len(pending_before) >= 1, "test precondition: a pending object exists"

    conn = db_session.connection()
    total_changes_before = conn.connection.total_changes

    # Deliberately NOT the _preview() helper: it flushes. The engine's own
    # no_autoflush is what's under attack here.
    preview = build_period_close_preview(db_session, CLOSE_PERIOD)

    total_changes_after = conn.connection.total_changes
    assert total_changes_after == total_changes_before, "preview must not write a single row"
    assert pending_before == set(db_session.new), "pending set must be unchanged (no autoflush)"
    assert not db_session.dirty, "no modified objects"
    assert preview.summary["prior_accrual_reversals"] == 1

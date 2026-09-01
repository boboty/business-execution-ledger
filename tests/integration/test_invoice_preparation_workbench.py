"""Phase 2D.3-F2a — the one read-only Invoice Preparation Workbench
application path.

``get_invoice_preparation_workbench`` must compose the F0 factual context
with the two canonical F1 reports over that SAME context — and must NOT
re-derive any rule outcome. The Workbench is application orchestration
only: it never re-implements the IP-S02 comparison, the supplier
P02/P03/P04/P05 comparisons, the P09 follow-up, cardinality handling, or
currency-safe comparison, and it never persists anything.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from bel.application.invoice_preparation import (
    InvoicePreparationContext,
    SalesScopeContext,
    SalesScopeInvoiceAllocation,
    SalesScopeLinkedProcurementContract,
    SupplierScopeContext,
    get_invoice_preparation_context,
)
from bel.application.invoice_preparation_workbench import (
    InvoicePreparationWorkbench,
    get_invoice_preparation_workbench,
    get_invoice_preparation_workbench_from_context,
)
from bel.application.procurement_sales_link import add_procurement_sales_link
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.application.sales_invoice_preparation import (
    SalesAmountCheckOutcome,
    evaluate_sales_invoice_preparation,
)
from bel.application.shipment_facts import create_shipment_fact
from bel.application.supplier_invoice_request import (
    SupplierRequestAdvisoryCode,
    evaluate_supplier_invoice_request,
)
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection
from bel.domain.matching import (
    ConfirmationType,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
    SalesInvoiceAllocation,
    SubjectType,
)
from bel.domain.sales_contract import SalesContract
from bel.domain.shipment import Shipment
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    InvoiceRepository,
    MatchCaseRepository,
    SalesInvoiceAllocationRepository,
)

NOW = datetime.now(timezone.utc)


def _make_fragment(session):
    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    EvidenceRepository(session).add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=doc.id,
        fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None,
        row_number=None,
        locator_json={},
        raw_data={},
        created_at=NOW,
    )
    EvidenceRepository(session).add_fragment(frag)
    session.flush()
    return frag


def _make_contract(session, fragment_id, contract_no, *, gross_amount=Decimal("100.00"), currency="USD"):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no,
        contract_type=None,
        counterparty="Supplier",
        buyer="Our Own Entity",
        gross_amount=gross_amount,
        currency=currency,
        contract_date=date(2026, 1, 1),
        current_source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


def _make_sales_contract(session, fragment_id, sales_contract_no, fields=None):
    return create_sales_contract_fact(
        session,
        our_entity="Our Own Entity",
        sales_contract_no=sales_contract_no,
        fields=fields or {},
        source_fragment_id=fragment_id,
        created_at=NOW,
    ).sales_contract


def _make_sales_invoice(session, fragment_id, *, gross_amount=Decimal("100.00"), currency="USD"):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.SALES,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=f"SINV-WB-{uuid.uuid4().hex[:8]}",
        issue_date=date(2031, 1, 10),
        seller="Our Own Entity",
        buyer="Customer",
        net_amount=gross_amount,
        tax_amount=Decimal("0"),
        gross_amount=gross_amount,
        invoice_status=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
        currency=currency,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    return invoice


def _make_sales_invoice_allocation(session, invoice, sales_contract):
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.INVOICE,
        subject_id=invoice.id,
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
        match_method=MatchMethod.MANUAL_SALES_SCOPE,
        created_at=NOW,
        resolved_at=None,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    SalesInvoiceAllocationRepository(session).add(
        SalesInvoiceAllocation(
            id=uuid.uuid4(),
            invoice_id=invoice.id,
            sales_contract_id=sales_contract.id,
            match_case_id=match_case.id,
            allocated_gross_amount=invoice.gross_amount,
            confirmation_type=ConfirmationType.HUMAN_CONFIRMED,
            created_at=NOW,
        )
    )
    session.flush()


def _link(session, contract, sales_contract, fragment):
    return add_procurement_sales_link(
        session,
        procurement_contract_id=contract.id,
        sales_contract_id=sales_contract.id,
        source_fragment_id=fragment.id,
        confirmation_type="AUTO_CONFIRMED",
        created_at=NOW,
    ).link


def _one_match_scope(session):
    """One SalesContract with a MATCHing 1:1:1 scope and one procurement
    Contract — so both directions have content."""
    frag = _make_fragment(session)
    contract = _make_contract(session, frag.id, "PO-WB-1")
    sales_contract = _make_sales_contract(
        session, frag.id, "SC-WB-1",
        fields={"customer": "Customer WB", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(session, contract, sales_contract, frag)
    create_shipment_fact(
        session,
        contract_id=contract.id,
        external_reference="SHIP-WB-1",
        execution_date=date(2031, 2, 1),
        fields={"declared_amount": Decimal("100.00"), "declared_currency": "USD"},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    invoice = _make_sales_invoice(session, frag.id, gross_amount=Decimal("100.00"), currency="USD")
    _make_sales_invoice_allocation(session, invoice, sales_contract)
    session.flush()
    return frag, contract, sales_contract


def test_workbench_composes_both_directions_from_one_context(db_session):
    """One workbench call composes the F0 context and BOTH F1 reports; the
    reports are the rule layers' own canonical outputs (same values as the
    standalone evaluations), never a re-derivation."""
    _one_match_scope(db_session)
    db_session.commit()

    workbench = get_invoice_preparation_workbench(db_session)

    # The composed context is exactly the F0 context.
    assert workbench.context == get_invoice_preparation_context(db_session)

    # The sales report equals the standalone F1 sales evaluation.
    assert workbench.sales_report == evaluate_sales_invoice_preparation(db_session)
    sales_decision = workbench.sales_report.decisions[0]
    assert sales_decision.amount_check.outcome == SalesAmountCheckOutcome.MATCH

    # The supplier report equals the standalone F1 supplier evaluation,
    # and the two directions come from the SAME underlying context (the
    # linked procurement Contract appears in both).
    assert workbench.supplier_report == evaluate_supplier_invoice_request(db_session)
    assert len(workbench.supplier_report.decisions) == 1
    assert workbench.sales_report.decisions[0].required_inputs[1].source_fact_ids == (
        workbench.supplier_report.decisions[0].contract_id,
    )


def test_workbench_is_strictly_read_only(db_session):
    """The workbench path is a pure read: evaluating it writes nothing and
    leaves the session clean."""
    _one_match_scope(db_session)
    db_session.commit()

    from bel.infrastructure.persistence import models as m

    def _counts():
        counts = {}
        for name in dir(m):
            obj = getattr(m, name)
            if isinstance(obj, type) and hasattr(obj, "__tablename__"):
                counts[obj.__tablename__] = db_session.query(obj).count()
        return counts

    before = _counts()
    get_invoice_preparation_workbench(db_session)
    assert _counts() == before
    assert not db_session.dirty and not db_session.new and not db_session.deleted


def test_workbench_is_pure_over_a_manually_built_context_no_session():
    """The from-context seam is a pure function over F0 DTOs — no session,
    no DB — so the Web page and the future F2b export can compose with it
    identically."""
    sc_id, contract_id, shipment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sales_contract = SalesContract(
        id=sc_id, our_entity="Our Own Entity", sales_contract_no="SC-WB-PURE",
        customer="Customer Pure", currency="USD", gross_amount=Decimal("100.00"),
        contract_date=date(2026, 1, 1), current_source_fragment_id=uuid.uuid4(), created_at=NOW,
    )
    contract = Contract(
        id=contract_id, contract_no="PO-WB-PURE", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=Decimal("100.00"), currency="USD", contract_date=date(2026, 1, 1),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    from bel.domain.procurement_sales_link import ProcurementSalesLink

    link = ProcurementSalesLink(
        id=uuid.uuid4(), procurement_contract_id=contract_id, sales_contract_id=sc_id,
        source_fragment_id=uuid.uuid4(), confirmation_type="AUTO_CONFIRMED", created_at=NOW,
    )
    shipment = Shipment(
        id=shipment_id, contract_id=contract_id, external_reference="SHIP-WB-PURE",
        execution_date=date(2031, 2, 1), contract_item_id=None, quantity=Decimal("1"),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW,
        declared_amount=Decimal("100.00"), declared_currency="USD",
    )
    context = InvoicePreparationContext(
        sales_scopes=(
            SalesScopeContext(
                sales_contract=sales_contract,
                linked_procurement_contracts=(
                    SalesScopeLinkedProcurementContract(link=link, contract=contract),
                ),
                invoice_allocations=(),
                payment_allocations=(),
                unresolved_work=(),
            ),
        ),
        supplier_scopes=(
            SupplierScopeContext(
                contract=contract, items=(), shipments=(shipment,),
                invoice_allocations=(), invoice_item_allocations=(),
                payment_allocations=(), unresolved_work=(),
            ),
        ),
    )

    workbench = get_invoice_preparation_workbench_from_context(context)
    assert isinstance(workbench, InvoicePreparationWorkbench)
    assert workbench.context is context
    sales_decision = workbench.sales_report.decisions[0]
    # No confirmed SALES invoice Fact in the pure context -> the F1 IP-S02
    # comparison (composed, never re-derived) is missing-fact.
    assert sales_decision.amount_check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT


def test_workbench_composes_dangling_allocation_as_not_a_confirmed_fact():
    """A SalesInvoiceAllocation whose Invoice Fact is missing is NOT a
    confirmed Invoice Fact in the composed Workbench either — the F1
    sales layer's confirmed-Fact handling flows through unchanged."""
    sc_id, contract_id, shipment_id, dangling_invoice_id = (uuid.uuid4() for _ in range(4))
    sales_contract = SalesContract(
        id=sc_id, our_entity="Our Own Entity", sales_contract_no="SC-WB-DANGLING",
        customer="Customer D", currency="USD", gross_amount=Decimal("100.00"),
        contract_date=date(2026, 1, 1), current_source_fragment_id=uuid.uuid4(), created_at=NOW,
    )
    contract = Contract(
        id=contract_id, contract_no="PO-WB-DANGLING", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=Decimal("100.00"), currency="USD", contract_date=date(2026, 1, 1),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    from bel.domain.procurement_sales_link import ProcurementSalesLink

    link = ProcurementSalesLink(
        id=uuid.uuid4(), procurement_contract_id=contract_id, sales_contract_id=sc_id,
        source_fragment_id=uuid.uuid4(), confirmation_type="AUTO_CONFIRMED", created_at=NOW,
    )
    shipment = Shipment(
        id=shipment_id, contract_id=contract_id, external_reference="SHIP-WB-DANGLING",
        execution_date=date(2031, 2, 1), contract_item_id=None, quantity=Decimal("1"),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW,
        declared_amount=Decimal("100.00"), declared_currency="USD",
    )
    context = InvoicePreparationContext(
        sales_scopes=(
            SalesScopeContext(
                sales_contract=sales_contract,
                linked_procurement_contracts=(
                    SalesScopeLinkedProcurementContract(link=link, contract=contract),
                ),
                invoice_allocations=(
                    SalesScopeInvoiceAllocation(
                        allocation=SalesInvoiceAllocation(
                            id=uuid.uuid4(), invoice_id=dangling_invoice_id, sales_contract_id=sc_id,
                            match_case_id=uuid.uuid4(), allocated_gross_amount=Decimal("100.00"),
                            confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
                        ),
                        invoice=None,
                    ),
                ),
                payment_allocations=(),
                unresolved_work=(),
            ),
        ),
        supplier_scopes=(
            SupplierScopeContext(
                contract=contract, items=(), shipments=(shipment,),
                invoice_allocations=(), invoice_item_allocations=(),
                payment_allocations=(), unresolved_work=(),
            ),
        ),
    )

    workbench = get_invoice_preparation_workbench_from_context(context)
    sales_decision = workbench.sales_report.decisions[0]
    check = sales_decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert check.sales_invoice_id is None
    # The declaration leg is resolved — only the dangling invoice leg is
    # missing, exactly as the F1 layer decided it.
    assert check.shipment_id == shipment_id


def test_workbench_never_invents_follow_up(db_session):
    """The P09 supplier follow-up comes ONLY from the frozen F1 rule: a
    procurement Contract with a confirmed PURCHASE invoice and an OUT
    payment carries no follow-up advisory in the composed Workbench."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-WB-NOFOLLOW", gross_amount=Decimal("100.00"), currency="USD")
    from bel.domain.matching import AllocationMatchMethod, InvoiceAllocation
    from bel.infrastructure.persistence.repositories import InvoiceAllocationRepository

    purchase_invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.PURCHASE,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key="PINV-WB-NOFOLLOW",
        issue_date=date(2031, 1, 10),
        seller="Supplier",
        buyer="Our Own Entity",
        net_amount=Decimal("100.00"),
        tax_amount=Decimal("0"),
        gross_amount=Decimal("100.00"),
        invoice_status=None,
        source_fragment_id=frag.id,
        created_at=NOW,
        updated_at=NOW,
        currency="USD",
    )
    InvoiceRepository(db_session).add(purchase_invoice)
    db_session.flush()
    match_case = MatchCase(
        id=uuid.uuid4(), subject_type=SubjectType.INVOICE, subject_id=purchase_invoice.id,
        status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001,
        created_at=NOW, resolved_at=NOW,
    )
    MatchCaseRepository(db_session).add(match_case)
    db_session.flush()
    InvoiceAllocationRepository(db_session).add(
        InvoiceAllocation(
            id=uuid.uuid4(), invoice_id=purchase_invoice.id, contract_id=contract.id,
            match_case_id=match_case.id, allocated_gross_amount=Decimal("100.00"),
            match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    )
    db_session.commit()

    workbench = get_invoice_preparation_workbench(db_session)
    supplier_decision = workbench.supplier_report.decisions[0]
    codes = [a.code for a in supplier_decision.advisories]
    assert SupplierRequestAdvisoryCode.SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED not in codes

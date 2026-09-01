"""Phase 2D.3-F0 — Invoice Preparation Context (application layer).

Proves the rule-neutral factual foundation on independently synthetic
data:

- SalesContract is the sales-side primary axis; the external customer
  comes ONLY from ``SalesContract.customer``.
- ``Contract.buyer`` (our own entity) is never presented as an external
  customer — the supplier scope has no customer concept at all.
- Only CURRENT ProcurementSalesLinks are enumerated; the many-to-many
  bridge is enumerated, never apportioned or summed.
- SALES invoices appear only through ``SalesInvoiceAllocation``; IN
  receipts only through ``SalesPaymentAllocation``. On the supplier side
  F0 PRESERVES every current ``InvoiceAllocation`` / ``PaymentAllocation``
  with its resolved Invoice/Payment Fact — missing or wrong-direction
  Facts stay visible as context; whether one is a CONFIRMED PURCHASE
  invoice / OUT payment is decided downstream, never by erasing the
  association here.
- ContractItems / Shipments / InvoiceItemAllocations + InvoiceItem facts
  are exposed as facts only; unknown/None stays unknown.
- No readiness/eligibility/status concept exists anywhere in the DTO
  tree, and the whole read path is strictly read-only.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.invoice_preparation import (
    InvoicePreparationFilters,
    SalesScopeContext,
    SupplierScopeContext,
    get_invoice_preparation_context,
)
from bel.application.procurement_sales_link import add_procurement_sales_link
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.domain.accrual import InvoiceItemAllocation
from bel.domain.contract import Contract, ContractItem
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection
from bel.domain.matching import (
    AllocationMatchMethod,
    ConfirmationType,
    InvoiceAllocation,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
    PaymentAllocation,
    SalesInvoiceAllocation,
    SalesPaymentAllocation,
)
from bel.domain.payment import Payment, PaymentDirection
from bel.domain.procurement_sales_link import ConfirmationType as LinkConfirmationType
from bel.domain.procurement_sales_link import ProcurementSalesLinkCorrection
from bel.domain.shipment import ShipmentRevision, ShipmentRevisionType
from bel.infrastructure.persistence.repositories import (
    ContractItemRepository,
    ContractRepository,
    EvidenceRepository,
    InvoiceAllocationRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
    MatchCaseRepository,
    PaymentAllocationRepository,
    PaymentRepository,
    ProcurementSalesLinkRepository,
    SalesContractRepository,
    SalesInvoiceAllocationRepository,
    SalesPaymentAllocationRepository,
    ShipmentRepository,
)

NOW = datetime.now(timezone.utc)

# Terms that must never appear as a DTO field: they are eligibility /
# preparation judgments reserved for the Phase 2D.3 rule freeze.
BANNED_FIELD_TOKENS = (
    "eligib",
    "ready",
    "remaining",
    "should",
    "outstanding",
    "owed",
    "required",
    "status",
    "due",
    "complete",
    "progress",
    "apportion",
    "ratio",
)


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


def _make_contract(session, fragment_id, contract_no, counterparty="Supplier", buyer="Our Own Entity"):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no,
        contract_type=None,
        counterparty=counterparty,
        buyer=buyer,
        gross_amount=Decimal("1000.00"),
        currency="CNY",
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


def _make_invoice(session, fragment_id, direction, gross_amount=Decimal("100.00"), external_key=None):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=direction,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=external_key or f"INV-{uuid.uuid4().hex[:8]}",
        issue_date=date(2031, 1, 10),
        seller="Seller",
        buyer="Buyer",
        net_amount=gross_amount,
        tax_amount=Decimal("0"),
        gross_amount=gross_amount,
        invoice_status=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    return invoice


def _make_payment(session, fragment_id, direction, amount=Decimal("100.00")):
    payment = Payment(
        id=uuid.uuid4(),
        transaction_date=date(2031, 1, 15),
        direction=direction,
        amount=amount,
        counterparty="Counterparty",
        business_type=None,
        bank_reference=f"REF-{uuid.uuid4().hex[:8]}",
        description=None,
        running_balance=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    PaymentRepository(session).add(payment)
    session.flush()
    return payment


def _make_match_case(session, subject_type, subject_id):
    # A sales allocation can only be written while its case is pending.
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type=subject_type,
        subject_id=subject_id,
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
        match_method=MatchMethod.MANUAL_SALES_SCOPE,
        created_at=NOW,
        resolved_at=None,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    return match_case


def _make_sales_invoice_allocation(session, invoice, sales_contract):
    match_case = _make_match_case(session, "INVOICE", invoice.id)
    allocation = SalesInvoiceAllocation(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        sales_contract_id=sales_contract.id,
        match_case_id=match_case.id,
        allocated_gross_amount=Decimal("50.00"),
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED,
        created_at=NOW,
    )
    SalesInvoiceAllocationRepository(session).add(allocation)
    session.flush()
    return allocation


def _make_sales_payment_allocation(session, payment, sales_contract):
    match_case = _make_match_case(session, "PAYMENT", payment.id)
    allocation = SalesPaymentAllocation(
        id=uuid.uuid4(),
        payment_id=payment.id,
        sales_contract_id=sales_contract.id,
        match_case_id=match_case.id,
        allocated_amount=Decimal("30.00"),
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED,
        created_at=NOW,
    )
    SalesPaymentAllocationRepository(session).add(allocation)
    session.flush()
    return allocation


def _make_procurement_invoice_allocation(session, invoice, contract):
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
    allocation = InvoiceAllocation(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        contract_id=contract.id,
        match_case_id=match_case.id,
        allocated_gross_amount=Decimal("40.00"),
        match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
        confirmation_type=ConfirmationType.AUTO_CONFIRMED,
        created_at=NOW,
    )
    InvoiceAllocationRepository(session).add(allocation)
    session.flush()
    return allocation


def _make_procurement_payment_allocation(session, payment, contract):
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type="PAYMENT",
        subject_id=payment.id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    allocation = PaymentAllocation(
        id=uuid.uuid4(),
        payment_id=payment.id,
        contract_id=contract.id,
        match_case_id=match_case.id,
        allocated_amount=Decimal("20.00"),
        match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
        confirmation_type=ConfirmationType.AUTO_CONFIRMED,
        created_at=NOW,
    )
    PaymentAllocationRepository(session).add(allocation)
    session.flush()
    return allocation


def _make_contract_item(session, contract, fragment_id, source_item_key="ITEM-1", product_name="Widget"):
    item = ContractItem(
        id=uuid.uuid4(),
        contract_id=contract.id,
        source_item_key=source_item_key,
        sku=None,
        product_name=product_name,
        specification=None,
        quantity=Decimal("10"),
        unit=None,
        unit_price=None,
        gross_amount=Decimal("500.00"),
        tax_rate=None,
        net_amount=Decimal("450.00"),
        current_source_fragment_id=fragment_id,
        created_at=NOW,
    )
    ContractItemRepository(session).add(item)
    session.flush()
    return item


def _make_shipment(session, contract, fragment_id, external_reference="SHIP-1"):
    shipment_repo = ShipmentRepository(session)
    anchor_id = uuid.uuid4()
    shipment_repo.create_anchor(
        id=anchor_id,
        contract_id=contract.id,
        external_reference=external_reference,
        execution_date=date(2031, 2, 1),
        created_at=NOW,
    )
    shipment_repo.create_initial_revision(
        ShipmentRevision(
            id=uuid.uuid4(),
            shipment_id=anchor_id,
            revision_type=ShipmentRevisionType.INITIAL,
            contract_item_id=None,
            quantity=Decimal("5"),
            source_fragment_id=fragment_id,
            superseded_by_revision_id=None,
            created_at=NOW,
        )
    )
    session.flush()
    return shipment_repo.get(anchor_id)


def _make_invoice_item_allocation(session, invoice_item, contract_item):
    allocation = InvoiceItemAllocation(
        id=uuid.uuid4(),
        invoice_item_id=invoice_item.id,
        contract_item_id=contract_item.id,
        allocated_quantity=Decimal("2"),
        allocated_net_amount=Decimal("80.00"),
        confirmation_type="MANUAL_CONFIRMED",
        source_fragment_id=_make_fragment(session).id,
        created_at=NOW,
        superseded_by_fact_id=None,
    )
    InvoiceItemAllocationRepository(session).add(allocation)
    session.flush()
    return allocation


# ---------------------------------------------------------------------------
# Primary axes and customer semantics
# ---------------------------------------------------------------------------


def test_sales_contract_is_primary_axis_and_customer_only_from_sales_contract(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-001", counterparty="Supplier X")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-001", fields={"customer": "Customer A"}
    )
    add_procurement_sales_link(
        db_session,
        procurement_contract_id=contract.id,
        sales_contract_id=sales_contract.id,
        source_fragment_id=frag.id,
        confirmation_type=LinkConfirmationType.AUTO_CONFIRMED,
        created_at=NOW,
    )
    db_session.commit()

    ctx = get_invoice_preparation_context(db_session)

    assert len(ctx.sales_scopes) == 1
    scope = ctx.sales_scopes[0]
    assert isinstance(scope, SalesScopeContext)
    # Primary axis is the SalesContract anchor itself.
    assert scope.sales_contract.id == sales_contract.id
    # customer comes ONLY from SalesContract.customer — the procurement
    # Contract's counterparty ("Supplier X") and buyer never appear here.
    assert scope.sales_contract.customer == "Customer A"
    assert scope.sales_contract.our_entity == "Our Own Entity"


def test_contract_buyer_is_our_entity_never_external_customer(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-002", counterparty="Supplier Y", buyer="Our Own Entity")
    db_session.commit()

    ctx = get_invoice_preparation_context(db_session)

    assert len(ctx.supplier_scopes) == 1
    scope = ctx.supplier_scopes[0]
    assert isinstance(scope, SupplierScopeContext)
    # The supplier scope exposes buyer as OUR OWN entity and has no
    # customer field at all — structurally, not just by convention.
    supplier_fields = {f.name for f in dataclasses.fields(SupplierScopeContext)}
    assert "customer" not in supplier_fields
    assert scope.contract.buyer == "Our Own Entity"
    assert scope.contract.counterparty == "Supplier Y"


def test_current_procurement_sales_links_only(db_session):
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, "PO-003")
    c2 = _make_contract(db_session, frag.id, "PO-004")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-002")
    link_repo_args = dict(
        session=db_session,
        sales_contract_id=sales_contract.id,
        source_fragment_id=frag.id,
        confirmation_type=LinkConfirmationType.AUTO_CONFIRMED,
        created_at=NOW,
    )
    link1 = add_procurement_sales_link(procurement_contract_id=c1.id, **link_repo_args).link
    link2 = add_procurement_sales_link(procurement_contract_id=c2.id, **link_repo_args).link
    # Retire link1 at the relationship level — only link2 stays current.
    ProcurementSalesLinkRepository(db_session).add_correction_if_uncorrected(
        ProcurementSalesLinkCorrection(
            id=uuid.uuid4(),
            superseded_link_id=link1.id,
            replacement_link_id=link2.id,
            source_fragment_id=frag.id,
            confirmation_type="HUMAN_CONFIRMED",
            created_at=NOW,
        )
    )
    db_session.commit()

    scope = get_invoice_preparation_context(db_session).sales_scopes[0]
    assert len(scope.linked_procurement_contracts) == 1
    assert scope.linked_procurement_contracts[0].link.id == link2.id
    assert scope.linked_procurement_contracts[0].contract.contract_no == "PO-004"


def test_many_to_many_links_enumerated_never_apportioned(db_session):
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, "PO-005")
    c2 = _make_contract(db_session, frag.id, "PO-006")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-003", fields={"gross_amount": Decimal("900.00")}
    )
    for contract in (c1, c2):
        add_procurement_sales_link(
            db_session,
            procurement_contract_id=contract.id,
            sales_contract_id=sales_contract.id,
            source_fragment_id=frag.id,
            confirmation_type=LinkConfirmationType.AUTO_CONFIRMED,
            created_at=NOW,
        )
    db_session.commit()

    scope = get_invoice_preparation_context(db_session).sales_scopes[0]
    # Both links enumerated as-is.
    assert {entry.contract.id for entry in scope.linked_procurement_contracts} == {c1.id, c2.id}
    # The bridge edge carries no amount/quantity/ratio field at all —
    # enumeration only, nothing to apportion with.
    edge_fields = {f.name for f in dataclasses.fields(type(scope.linked_procurement_contracts[0]))}
    assert edge_fields == {"link", "contract"}
    assert all(
        token not in " ".join(edge_fields) for token in ("amount", "quantity", "ratio", "share")
    )


def test_filters_scope_each_axis(db_session):
    frag = _make_fragment(db_session)
    _make_contract(db_session, frag.id, "PO-007", counterparty="Supplier Alpha")
    _make_contract(db_session, frag.id, "PO-008", counterparty="Supplier Beta")
    _make_sales_contract(db_session, frag.id, "SC-004", fields={"customer": "Customer Alpha"})
    _make_sales_contract(db_session, frag.id, "SC-005", fields={})
    db_session.commit()

    ctx = get_invoice_preparation_context(db_session, InvoicePreparationFilters(supplier="Alpha"))
    assert [s.contract.contract_no for s in ctx.supplier_scopes] == ["PO-007"]
    assert len(ctx.sales_scopes) == 2

    ctx = get_invoice_preparation_context(db_session, InvoicePreparationFilters(customer="Alpha"))
    assert [s.sales_contract.sales_contract_no for s in ctx.sales_scopes] == ["SC-004"]
    assert len(ctx.supplier_scopes) == 2

    # Unknown (None) never matches a present needle.
    ctx = get_invoice_preparation_context(db_session, InvoicePreparationFilters(customer="Gamma"))
    assert ctx.sales_scopes == ()


# ---------------------------------------------------------------------------
# Direction isolation — each Fact appears only through its own allocation path
# ---------------------------------------------------------------------------


def test_sales_invoice_appears_only_through_sales_allocation(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-009")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-006")
    allocated = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, external_key="SINV-ALLOC")
    unallocated = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, external_key="SINV-FREE")
    _make_sales_invoice_allocation(db_session, allocated, sales_contract)
    db_session.commit()

    ctx = get_invoice_preparation_context(db_session)
    scope = ctx.sales_scopes[0]
    assert [e.invoice.external_invoice_key for e in scope.invoice_allocations] == ["SINV-ALLOC"]
    # An unallocated SALES invoice appears nowhere — factual absence.
    all_scope_invoice_ids = {
        e.invoice.id for s in ctx.sales_scopes for e in s.invoice_allocations if e.invoice
    } | {e.invoice.id for s in ctx.supplier_scopes for e in s.invoice_allocations if e.invoice}
    assert unallocated.id not in all_scope_invoice_ids
    # Empty means "no such Fact exists yet" — an empty tuple, never a status.
    other = ctx.supplier_scopes[0]
    assert other.invoice_allocations == ()
    assert not hasattr(other, "invoiced") and not hasattr(other, "invoiced_amount")


def test_in_payment_appears_only_through_sales_payment_allocation(db_session):
    frag = _make_fragment(db_session)
    _make_contract(db_session, frag.id, "PO-010")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-007")
    receipt = _make_payment(db_session, frag.id, PaymentDirection.IN)
    _make_sales_payment_allocation(db_session, receipt, sales_contract)
    db_session.commit()

    ctx = get_invoice_preparation_context(db_session)
    scope = ctx.sales_scopes[0]
    assert [e.payment.id for e in scope.payment_allocations] == [receipt.id]
    assert all(e.payment_allocations == () for e in ctx.supplier_scopes)


def test_purchase_invoice_and_out_payment_only_on_procurement_context(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-011")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-008")
    purchase_invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE)
    out_payment = _make_payment(db_session, frag.id, PaymentDirection.OUT)
    _make_procurement_invoice_allocation(db_session, purchase_invoice, contract)
    _make_procurement_payment_allocation(db_session, out_payment, contract)
    db_session.commit()

    ctx = get_invoice_preparation_context(db_session)
    supplier = ctx.supplier_scopes[0]
    assert [e.invoice.id for e in supplier.invoice_allocations] == [purchase_invoice.id]
    assert [e.payment.id for e in supplier.payment_allocations] == [out_payment.id]
    assert all(s.invoice_allocations == () for s in ctx.sales_scopes)
    assert all(s.payment_allocations == () for s in ctx.sales_scopes)


def test_wrong_direction_subjects_preserved_as_context_never_confirmed(db_session):
    """Final Gate: F0 preserves the association even when the referenced
    Fact is present with the wrong business direction. A SALES invoice
    through the procurement-only InvoiceAllocation, and an IN payment
    through PaymentAllocation, stay visible on the supplier scope with
    their resolved Facts — the confirmed-Fact decision (PURCHASE / OUT) is
    made downstream, never by erasing the association here."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-012")
    sales_invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES)
    in_payment = _make_payment(db_session, frag.id, PaymentDirection.IN)
    _make_procurement_invoice_allocation(db_session, sales_invoice, contract)
    _make_procurement_payment_allocation(db_session, in_payment, contract)
    db_session.commit()

    supplier = get_invoice_preparation_context(db_session).supplier_scopes[0]
    # Both associations are PRESERVED (never silently dropped), with their
    # resolved Facts.
    assert [e.allocation.invoice_id for e in supplier.invoice_allocations] == [sales_invoice.id]
    assert supplier.invoice_allocations[0].invoice.direction == InvoiceDirection.SALES
    assert [e.allocation.payment_id for e in supplier.payment_allocations] == [in_payment.id]
    assert supplier.payment_allocations[0].payment.direction == PaymentDirection.IN


# ---------------------------------------------------------------------------
# Facts only: items, shipments, item allocations
# ---------------------------------------------------------------------------


def test_contract_items_shipments_and_item_allocations_exposed_as_facts(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-013")
    item = _make_contract_item(db_session, contract, frag.id)
    shipment = _make_shipment(db_session, contract, frag.id)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE, external_key="PINV-ITEMS")
    invoice_item = InvoiceItemRepository(db_session).list_for_invoice(invoice.id)  # none yet
    assert invoice_item == []
    # A bare Invoice has no InvoiceItem rows; build one directly for the
    # allocation fact.
    from bel.domain.invoice import InvoiceItem

    invoice_item = InvoiceItem(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        line_no=1,
        product_name="Widget",
        specification=None,
        unit=None,
        quantity=Decimal("2"),
        unit_price=None,
        net_amount=Decimal("80.00"),
        tax_rate=None,
        tax_amount=Decimal("0"),
        gross_amount=Decimal("80.00"),
        source_fragment_id=frag.id,
    )
    InvoiceItemRepository(db_session).add(invoice_item)
    db_session.flush()
    allocation = _make_invoice_item_allocation(db_session, invoice_item, item)
    db_session.commit()

    supplier = get_invoice_preparation_context(db_session).supplier_scopes[0]
    assert [i.id for i in supplier.items] == [item.id]
    assert [s.id for s in supplier.shipments] == [shipment.id]
    assert len(supplier.invoice_item_allocations) == 1
    entry = supplier.invoice_item_allocations[0]
    assert entry.allocation.id == allocation.id
    assert entry.invoice_item.id == invoice_item.id
    assert entry.invoice.external_invoice_key == "PINV-ITEMS"
    # Facts only — no remaining quantity/amount concept anywhere.
    entry_fields = " ".join(f.name for f in dataclasses.fields(type(entry)))
    for token in ("remaining", "should", "required"):
        assert token not in entry_fields


def test_superseded_item_allocation_not_presented(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-014")
    item = _make_contract_item(db_session, contract, frag.id)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE)
    from bel.domain.invoice import InvoiceItem

    invoice_item = InvoiceItem(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        line_no=1,
        product_name="Widget",
        specification=None,
        unit=None,
        quantity=Decimal("1"),
        unit_price=None,
        net_amount=Decimal("10.00"),
        tax_rate=None,
        tax_amount=Decimal("0"),
        gross_amount=Decimal("10.00"),
        source_fragment_id=frag.id,
    )
    InvoiceItemRepository(db_session).add(invoice_item)
    db_session.flush()
    old = _make_invoice_item_allocation(db_session, invoice_item, item)
    new = _make_invoice_item_allocation(db_session, invoice_item, item)
    InvoiceItemAllocationRepository(db_session).mark_superseded(old.id, superseded_by_fact_id=new.id)
    db_session.commit()

    supplier = get_invoice_preparation_context(db_session).supplier_scopes[0]
    assert [e.allocation.id for e in supplier.invoice_item_allocations] == [new.id]


@pytest.mark.parametrize("leaked_direction", [InvoiceDirection.SALES, InvoiceDirection.UNKNOWN])
def test_sales_or_unknown_invoice_item_allocation_preserved_as_context(db_session, leaked_direction):
    """Final Gate (1A): a SALES (or UNKNOWN) Invoice -> InvoiceItem ->
    InvoiceItemAllocation -> PROCUREMENT ContractItem chain is PRESERVED
    in SupplierScopeContext.invoice_item_allocations as the association it
    is, with its resolved parent Invoice. InvoiceItemAllocation itself
    carries no direction, so the parent Invoice is resolved and carried
    as-is; whether it is a CONFIRMED item-name comparison candidate
    (parent PURCHASE) is decided downstream, never by erasing the
    association here. The PURCHASE case is covered by
    test_contract_items_shipments_and_item_allocations_exposed_as_facts."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-016")
    item = _make_contract_item(db_session, contract, frag.id)
    leaked_invoice = _make_invoice(
        db_session, frag.id, leaked_direction, external_key=f"LEAK-{leaked_direction}"
    )
    from bel.domain.invoice import InvoiceItem

    invoice_item = InvoiceItem(
        id=uuid.uuid4(),
        invoice_id=leaked_invoice.id,
        line_no=1,
        product_name="Widget",
        specification=None,
        unit=None,
        quantity=Decimal("3"),
        unit_price=None,
        net_amount=Decimal("30.00"),
        tax_rate=None,
        tax_amount=Decimal("0"),
        gross_amount=Decimal("30.00"),
        source_fragment_id=frag.id,
    )
    InvoiceItemRepository(db_session).add(invoice_item)
    db_session.flush()
    _make_invoice_item_allocation(db_session, invoice_item, item)
    db_session.commit()

    ctx = get_invoice_preparation_context(db_session)
    supplier = ctx.supplier_scopes[0]
    # The misdirected item allocation is PRESERVED with its resolved parent
    # Invoice (never silently dropped) — its direction is a downstream
    # confirmed-Fact decision, not an F0 erasure.
    assert [e.allocation.id for e in supplier.invoice_item_allocations]
    assert supplier.invoice_item_allocations[0].invoice.direction == leaked_direction
    assert supplier.invoice_item_allocations[0].invoice_item is not None
    # The ContractItem itself is untouched.
    assert [i.id for i in supplier.items] == [item.id]
    # The leaked invoice is now VISIBLE as context through the preserved
    # item allocation (never silently dropped) — confirming it was not
    # erased, only that it is not a confirmed PURCHASE parent.
    supplier_invoice_ids = {
        e.invoice.id
        for scope in ctx.supplier_scopes
        for e in scope.invoice_item_allocations
        if e.invoice
    }
    assert leaked_invoice.id in supplier_invoice_ids


# ---------------------------------------------------------------------------
# Unknown stays unknown; no eligibility concept; read-only
# ---------------------------------------------------------------------------


def test_unknown_stays_unknown(db_session):
    frag = _make_fragment(db_session)
    _make_sales_contract(db_session, frag.id, "SC-009", fields={})
    db_session.commit()

    scope = get_invoice_preparation_context(db_session).sales_scopes[0]
    sc = scope.sales_contract
    # None stays None at the DTO level — absence of a Fact, never a
    # negative business assertion and never a fabricated value.
    assert sc.customer is None
    assert sc.currency is None
    assert sc.gross_amount is None
    assert sc.contract_date is None
    assert scope.invoice_allocations == ()
    assert scope.payment_allocations == ()
    assert scope.linked_procurement_contracts == ()


def test_no_readiness_or_eligibility_field_exists_in_dto_tree():
    """Walk every dataclass in the context DTO tree: no field name may
    carry an eligibility / readiness / should-invoice / apportionment
    concept — those require the Phase 2D.3 rule freeze."""
    import bel.application.invoice_preparation as module

    dto_types = [
        obj
        for obj in vars(module).values()
        if dataclasses.is_dataclass(obj) and getattr(obj, "__module__", None) == module.__name__
    ]
    assert {t.__name__ for t in dto_types} >= {
        "InvoicePreparationContext",
        "SalesScopeContext",
        "SupplierScopeContext",
    }
    for dto_type in dto_types:
        for field in dataclasses.fields(dto_type):
            lowered = field.name.lower()
            for token in BANNED_FIELD_TOKENS:
                assert token not in lowered, f"{dto_type.__name__}.{field.name} carries banned concept {token!r}"


def test_strict_read_only(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-015")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-010")
    add_procurement_sales_link(
        db_session,
        procurement_contract_id=contract.id,
        sales_contract_id=sales_contract.id,
        source_fragment_id=frag.id,
        confirmation_type=LinkConfirmationType.AUTO_CONFIRMED,
        created_at=NOW,
    )
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES)
    _make_sales_invoice_allocation(db_session, invoice, sales_contract)
    db_session.commit()

    def _counts():
        from bel.infrastructure.persistence import models as m

        counts = {}
        for name in dir(m):
            obj = getattr(m, name)
            if isinstance(obj, type) and hasattr(obj, "__tablename__"):
                counts[obj.__tablename__] = db_session.query(obj).count()
        return counts

    before = _counts()
    ctx = get_invoice_preparation_context(db_session)
    assert _counts() == before
    assert not db_session.dirty and not db_session.new and not db_session.deleted

    # A pending (unflushed) object in the session must NOT be flushed by
    # the read (no_autoflush).
    from bel.infrastructure.persistence.models import EvidenceDocumentModel

    pending_doc = EvidenceDocumentModel(
        id=uuid.uuid4(), file_name="pending", sha256="pending-" + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    db_session.add(pending_doc)
    get_invoice_preparation_context(db_session)
    assert pending_doc in db_session.new  # still pending — never autoflushed
    db_session.rollback()

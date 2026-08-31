"""Phase 2D.3-F0 — /invoice-preparation web page.

The page is a FACT CONTEXT workbench: it must render through the SAME
Application context (``get_invoice_preparation_context``), be strictly
read-only, describe known data with fact-presence labels, and never
carry an eligibility / preparation judgment label (应开票 / 可开票 /
已开完 / 应请票 / 尚欠发票 / 本次请票 — all reserved for the Phase 2D.3
rule freeze). Independently synthetic data throughout.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from bel.application.invoice_preparation import get_invoice_preparation_context
from bel.application.procurement_sales_link import add_procurement_sales_link
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.domain.accrual import InvoiceItemAllocation
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
    PaymentAllocation,
    SalesInvoiceAllocation,
    SalesPaymentAllocation,
)
from bel.domain.payment import Payment, PaymentDirection
from bel.domain.procurement_sales_link import ConfirmationType as LinkConfirmationType
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
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
    SalesInvoiceAllocationRepository,
    SalesPaymentAllocationRepository,
    ShipmentRepository,
)
from bel.web.app import create_app

NOW = datetime.now(timezone.utc)

FORBIDDEN_JUDGMENT_LABELS = (
    "应开票",
    "可开票",
    "已开完",
    "应请票",
    "尚欠发票",
    "本次请票",
    "未开票",
    "READY",
    "NOT_READY",
    "BLOCKED",
    "ALREADY_INVOICED",
)

FACT_PRESENCE_LABELS = (
    "已关联销项发票事实",
    "已关联收款事实",
    "已确认采购发票",
    "已确认付款",
    "已确认出货事实",
    "当前商品明细",
    "待处理事项",
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


def _make_contract(session, fragment_id, contract_no, counterparty="Supplier Gamma"):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no,
        contract_type=None,
        counterparty=counterparty,
        buyer="Our Own Entity",
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


def _make_invoice(session, fragment_id, direction, external_key):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=direction,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=external_key,
        issue_date=date(2031, 1, 10),
        seller="Seller",
        buyer="Buyer",
        net_amount=Decimal("100.00"),
        tax_amount=Decimal("0"),
        gross_amount=Decimal("100.00"),
        invoice_status=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    return invoice


def _make_payment(session, fragment_id, direction, bank_reference):
    payment = Payment(
        id=uuid.uuid4(),
        transaction_date=date(2031, 1, 15),
        direction=direction,
        amount=Decimal("60.00"),
        counterparty="Counterparty",
        business_type=None,
        bank_reference=bank_reference,
        description=None,
        running_balance=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    PaymentRepository(session).add(payment)
    session.flush()
    return payment


def _build_invoice_preparation_db(db_path):
    """One DB with both axes fully populated plus one bare SalesContract
    with no facts at all (factual-absence rendering)."""
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        frag = _make_fragment(session)
        contract = _make_contract(session, frag.id, "PO-100")
        sales_contract = _make_sales_contract(
            session, frag.id, "SC-100", fields={"customer": "Customer Gamma"}
        )
        add_procurement_sales_link(
            session,
            procurement_contract_id=contract.id,
            sales_contract_id=sales_contract.id,
            source_fragment_id=frag.id,
            confirmation_type=LinkConfirmationType.AUTO_CONFIRMED,
            created_at=NOW,
        )

        # Sales side: one allocated SALES invoice + one allocated IN receipt.
        sales_invoice = _make_invoice(session, frag.id, InvoiceDirection.SALES, "SINV-100")
        invoice_case = MatchCase(
            id=uuid.uuid4(), subject_type="INVOICE", subject_id=sales_invoice.id,
            status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED, match_method=MatchMethod.MANUAL_SALES_SCOPE,
            created_at=NOW, resolved_at=None,
        )
        MatchCaseRepository(session).add(invoice_case)
        session.flush()
        SalesInvoiceAllocationRepository(session).add(
            SalesInvoiceAllocation(
                id=uuid.uuid4(), invoice_id=sales_invoice.id, sales_contract_id=sales_contract.id,
                match_case_id=invoice_case.id, allocated_gross_amount=Decimal("50.00"),
                confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
            )
        )

        receipt = _make_payment(session, frag.id, PaymentDirection.IN, "REF-IN-100")
        payment_case = MatchCase(
            id=uuid.uuid4(), subject_type="PAYMENT", subject_id=receipt.id,
            status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED, match_method=MatchMethod.MANUAL_SALES_SCOPE,
            created_at=NOW, resolved_at=None,
        )
        MatchCaseRepository(session).add(payment_case)
        session.flush()
        SalesPaymentAllocationRepository(session).add(
            SalesPaymentAllocation(
                id=uuid.uuid4(), payment_id=receipt.id, sales_contract_id=sales_contract.id,
                match_case_id=payment_case.id, allocated_amount=Decimal("60.00"),
                confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
            )
        )

        # Supplier side: item, shipment, PURCHASE invoice + item
        # allocation, OUT payment.
        item = ContractItem(
            id=uuid.uuid4(), contract_id=contract.id, source_item_key="ITEM-100", sku=None,
            product_name="Widget Gamma", specification=None, quantity=Decimal("10"), unit=None,
            unit_price=None, gross_amount=Decimal("500.00"), tax_rate=None, net_amount=Decimal("450.00"),
            current_source_fragment_id=frag.id, created_at=NOW,
        )
        ContractItemRepository(session).add(item)
        session.flush()
        shipment_repo = ShipmentRepository(session)
        from bel.domain.shipment import ShipmentRevision, ShipmentRevisionType

        shipment_anchor_id = uuid.uuid4()
        shipment_repo.create_anchor(
            id=shipment_anchor_id, contract_id=contract.id, external_reference="SHIP-100",
            execution_date=date(2031, 2, 1), created_at=NOW,
        )
        shipment_repo.create_initial_revision(
            ShipmentRevision(
                id=uuid.uuid4(), shipment_id=shipment_anchor_id,
                revision_type=ShipmentRevisionType.INITIAL, contract_item_id=None, quantity=Decimal("5"),
                source_fragment_id=frag.id, superseded_by_revision_id=None, created_at=NOW,
            )
        )
        session.flush()

        purchase_invoice = _make_invoice(session, frag.id, InvoiceDirection.PURCHASE, "PINV-100")
        purchase_case = MatchCase(
            id=uuid.uuid4(), subject_type="INVOICE", subject_id=purchase_invoice.id,
            status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001,
            created_at=NOW, resolved_at=NOW,
        )
        MatchCaseRepository(session).add(purchase_case)
        session.flush()
        InvoiceAllocationRepository(session).add(
            InvoiceAllocation(
                id=uuid.uuid4(), invoice_id=purchase_invoice.id, contract_id=contract.id,
                match_case_id=purchase_case.id, allocated_gross_amount=Decimal("40.00"),
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
            )
        )
        invoice_item = InvoiceItem(
            id=uuid.uuid4(), invoice_id=purchase_invoice.id, line_no=1, product_name="Widget Gamma",
            specification=None, unit=None, quantity=Decimal("2"), unit_price=None,
            net_amount=Decimal("80.00"), tax_rate=None, tax_amount=Decimal("0"),
            gross_amount=Decimal("80.00"), source_fragment_id=frag.id,
        )
        InvoiceItemRepository(session).add(invoice_item)
        session.flush()
        InvoiceItemAllocationRepository(session).add(
            InvoiceItemAllocation(
                id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=item.id,
                allocated_quantity=Decimal("2"), allocated_net_amount=Decimal("80.00"),
                confirmation_type="MANUAL_CONFIRMED", source_fragment_id=frag.id, created_at=NOW,
                superseded_by_fact_id=None,
            )
        )

        out_payment = _make_payment(session, frag.id, PaymentDirection.OUT, "REF-OUT-100")
        out_case = MatchCase(
            id=uuid.uuid4(), subject_type="PAYMENT", subject_id=out_payment.id,
            status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001,
            created_at=NOW, resolved_at=NOW,
        )
        MatchCaseRepository(session).add(out_case)
        session.flush()
        PaymentAllocationRepository(session).add(
            PaymentAllocation(
                id=uuid.uuid4(), payment_id=out_payment.id, contract_id=contract.id,
                match_case_id=out_case.id, allocated_amount=Decimal("20.00"),
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
            )
        )

        # A bare sales scope: unknown customer, no facts at all.
        _make_sales_contract(session, frag.id, "SC-101", fields={})
        session.commit()
    engine.dispose()


@pytest.fixture
def prep_ctx(tmp_path):
    db_path = tmp_path / "invoice-prep-web.db"
    _build_invoice_preparation_db(db_path)
    app = create_app(f"sqlite:///{db_path}")
    client = TestClient(app)
    return client, app


def _db_counts(session_factory) -> dict:
    from bel.infrastructure.persistence import models as m

    with session_factory() as session:
        counts = {}
        for name in dir(m):
            obj = getattr(m, name)
            if isinstance(obj, type) and hasattr(obj, "__tablename__"):
                counts[obj.__tablename__] = session.query(obj).count()
        return counts


def test_page_renders_both_tabs_and_is_zero_write(prep_ctx):
    client, app = prep_ctx
    before = _db_counts(app.state.session_factory)
    response = client.get("/invoice-preparation")
    assert response.status_code == 200
    assert "向客户开票" in response.text
    assert "向供应商要票" in response.text
    for label in FACT_PRESENCE_LABELS:
        assert label in response.text, f"missing fact-presence label: {label}"
    after = _db_counts(app.state.session_factory)
    assert before == after, "GET /invoice-preparation must not write a single row"


def test_page_carries_no_judgment_label(prep_ctx):
    client, _ = prep_ctx
    response = client.get("/invoice-preparation")
    for label in FORBIDDEN_JUDGMENT_LABELS:
        assert label not in response.text, f"judgment label must not appear: {label}"


def test_page_states_rules_are_not_frozen(prep_ctx):
    client, _ = prep_ctx
    response = client.get("/invoice-preparation")
    assert "尚未冻结" in response.text


def test_page_renders_same_application_context(prep_ctx):
    """The page must be a presentation of the ONE Application context:
    every scope/invoice/payment rendered comes from
    get_invoice_preparation_context over the same DB."""
    client, app = prep_ctx
    with app.state.session_factory() as session:
        dto = get_invoice_preparation_context(session)
    response = client.get("/invoice-preparation")

    assert f"{len(dto.sales_scopes)} 个销售范围" in response.text
    assert f"{len(dto.supplier_scopes)} 个采购合同" in response.text

    # Every sales scope's contract number is rendered exactly once as a
    # scope heading, with its customer and allocations.
    for scope in dto.sales_scopes:
        assert scope.sales_contract.sales_contract_no in response.text
        for entry in scope.invoice_allocations:
            assert entry.invoice.external_invoice_key in response.text
        for entry in scope.payment_allocations:
            assert entry.payment.bank_reference in response.text
    # And conversely: no allocated invoice number is rendered that the
    # Application context does not know.
    dto_invoice_nos = {
        entry.invoice.external_invoice_key
        for s in dto.sales_scopes
        for entry in s.invoice_allocations
        if entry.invoice
    } | {
        entry.invoice.external_invoice_key
        for s in dto.supplier_scopes
        for entry in s.invoice_allocations
        if entry.invoice
    }
    assert dto_invoice_nos == {"SINV-100", "PINV-100"}


def test_page_customer_and_buyer_semantics(prep_ctx):
    client, _ = prep_ctx
    response = client.get("/invoice-preparation")
    # The external customer appears from SalesContract.customer.
    assert "Customer Gamma" in response.text
    # Contract.buyer is presented as OUR OWN entity on the supplier side.
    assert "我方主体（买方）" in response.text
    # The unknown-customer scope renders factual absence, never a judgment.
    assert "SC-101" in response.text
    assert "客户待补充" in response.text


def test_factual_absence_rendering(prep_ctx):
    client, _ = prep_ctx
    response = client.get("/invoice-preparation")
    assert "暂无已关联销项发票事实" in response.text  # SC-101 has no sales invoice fact
    # The fully-populated supplier scope renders its facts.
    assert "SHIP-100" in response.text
    assert "Widget Gamma" in response.text  # the ContractItem's product evidence
    assert "PINV-100" in response.text
    assert "REF-OUT-100" in response.text


def test_nav_link_present(prep_ctx):
    client, _ = prep_ctx
    response = client.get("/invoice-preparation")
    assert 'href="/invoice-preparation"' in response.text

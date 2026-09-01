"""Phase 2D.3-F0/F2a — /invoice-preparation Web Workbench.

F0: the page renders through the SAME Application context
(``get_invoice_preparation_context``), is strictly read-only, describes
known data with fact-presence labels, and never carries an eligibility /
preparation judgment label (应开票 / 可开票 / 已开完 / 应请票 / 尚欠发票 /
本次请票 / 可以开票 / 不允许开票 / 已具备开票资格 — all reserved for a
future frozen workflow rule that does not exist).

F2a: the page becomes the integrated Invoice Preparation Workbench via
the ONE read-only Application path
(``get_invoice_preparation_workbench``). It clearly separates 向客户开票
(SalesContract axis) from 向供应商要票 (procurement Contract axis), and
per scope distinguishes 已确认事实 / 核对结果 / 提醒·待关注. The F1
comparison/advisory outcomes are translated to business-facing Chinese
labels in the viewmodel layer; internal enum names (MATCH, DEVIATION,
NOT_COMPARABLE_*, RULE_CONFLICT, INPUTS_PRESENT, the advisory/blocker
codes) never appear in primary UI. Independently synthetic data
throughout.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from bel.application.invoice_preparation import (
    InvoicePreparationContext,
    SalesScopeContext,
    SalesScopeInvoiceAllocation,
    SalesScopeLinkedProcurementContract,
    SalesScopePaymentAllocation,
    SupplierScopeContext,
    SupplierScopeInvoiceAllocation,
    SupplierScopePaymentAllocation,
    get_invoice_preparation_context,
)
from bel.application.invoice_preparation_workbench import get_invoice_preparation_workbench_from_context
from bel.application.procurement_sales_link import add_procurement_sales_link
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.application.shipment_facts import create_shipment_fact
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
from bel.domain.procurement_sales_link import (
    ConfirmationType as LinkConfirmationType,
    ProcurementSalesLink,
)
from bel.domain.sales_contract import SalesContract
from bel.domain.shipment import Shipment
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
from bel.web.viewmodels import InvoicePreparationVM

NOW = datetime.now(timezone.utc)

FORBIDDEN_JUDGMENT_LABELS = (
    "应开票",
    "可开票",
    "已开完",
    "应请票",
    "尚欠发票",
    "本次请票",
    "未开票",
    "可以开票",
    "不允许开票",
    "已具备开票资格",
    "开票失败",
    "READY",
    "NOT_READY",
    "BLOCKED",
    "ALREADY_INVOICED",
    # F2a: internal F1 enum names must never appear in primary business UI
    # — the viewmodel translates them to Chinese.
    "RULE_CONFLICT",
    "INPUTS_PRESENT",
    "INSUFFICIENT_FACTS",
    "PREPARATION_AMOUNT_DETERMINABLE",
    "MISSING_CONTRACT_GROSS_AMOUNT",
    "MATCH",
    "DEVIATION",
    "NOT_COMPARABLE_MISSING_FACT",
    "NOT_COMPARABLE_CURRENCY_MISMATCH",
    "NOT_COMPARABLE_AMBIGUOUS_SCOPE",
    "PURCHASE_INVOICE_AMOUNT_DEVIATION",
    "PURCHASE_INVOICE_CURRENCY_DEVIATION",
    "PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION",
    "MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT",
    "PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS",
    "SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED",
    "SALES_INVOICE_AMOUNT_DEVIATION",
    "SALES_INVOICE_CURRENCY_DEVIATION",
)

FACT_PRESENCE_LABELS = (
    "已关联销项发票事实",
    "已关联收款事实",
    "已确认采购发票",
    "已确认付款",
    "已确认出货事实",
    "当前商品明细",
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


def test_page_states_it_is_fact_control_not_approval(prep_ctx):
    client, _ = prep_ctx
    response = client.get("/invoice-preparation")
    # F2a: the page is an integrated Workbench — fact control + management
    # reminders, explicitly NOT an eligibility verdict or approval flow.
    assert "不构成开票资格判定" in response.text
    assert "不作为开票审批流程" in response.text


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


# ---------------------------------------------------------------------------
# Phase 2D.3-F2a — the integrated Workbench over a richer scenario set.
# Each scope exercises one distinct comparison/advisory outcome; the page
# must present them as business-facing Chinese labels (never the internal
# enum names) and as three structurally distinct blocks per scope
# (已确认事实 / 核对结果 / 提醒·待关注).
# ---------------------------------------------------------------------------

F2A_SALES_MATCH_LABEL = "金额核对一致"
F2A_SALES_DEVIATION_LABEL = "金额存在偏差，建议复核"
F2A_SALES_MISSING_LABEL = "当前信息不足，暂无法核对"
F2A_SALES_AMBIGUOUS_LABEL = "对应范围不唯一，暂无法自动核对"
F2A_SUPPLIER_FOLLOW_UP_LABEL = "已付款，尚未收到对应进项发票，建议催供应商开票"


def _make_contract_f2a(session, fragment_id, contract_no, *, gross_amount, currency):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no,
        contract_type=None,
        counterparty="Supplier F2A",
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


def _make_shipment_fact(session, contract, fragment_id, external_reference, declared_amount, declared_currency):
    create_shipment_fact(
        session,
        contract_id=contract.id,
        external_reference=external_reference,
        execution_date=date(2031, 2, 1),
        fields={"quantity": Decimal("10"), "declared_amount": declared_amount, "declared_currency": declared_currency},
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    session.flush()


def _make_sales_invoice_alloc(session, fragment_id, sales_contract, *, gross_amount, currency):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.SALES,
        invoice_type=None, invoice_no=None, digital_invoice_no=None,
        external_invoice_key=f"SINV-F2A-{uuid.uuid4().hex[:8]}",
        issue_date=date(2031, 1, 10),
        seller="Our Own Entity", buyer="Customer",
        net_amount=gross_amount, tax_amount=Decimal("0"), gross_amount=gross_amount,
        invoice_status=None, source_fragment_id=fragment_id, created_at=NOW, updated_at=NOW,
        currency=currency,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    match_case = MatchCase(
        id=uuid.uuid4(), subject_type="INVOICE", subject_id=invoice.id,
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED, match_method=MatchMethod.MANUAL_SALES_SCOPE,
        created_at=NOW, resolved_at=None,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    SalesInvoiceAllocationRepository(session).add(
        SalesInvoiceAllocation(
            id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sales_contract.id,
            match_case_id=match_case.id, allocated_gross_amount=gross_amount,
            confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
        )
    )
    session.flush()
    return invoice


def _make_purchase_invoice_alloc(session, fragment_id, contract, *, gross_amount, currency,
                                 tax_rate=None, product_name=None):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.PURCHASE,
        invoice_type=None, invoice_no=None, digital_invoice_no=None,
        external_invoice_key=f"PINV-F2A-{uuid.uuid4().hex[:8]}",
        issue_date=date(2031, 1, 10),
        seller="Supplier F2A", buyer="Our Own Entity",
        net_amount=gross_amount, tax_amount=Decimal("0"), gross_amount=gross_amount,
        invoice_status=None, source_fragment_id=fragment_id, created_at=NOW, updated_at=NOW,
        currency=currency,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    match_case = MatchCase(
        id=uuid.uuid4(), subject_type="INVOICE", subject_id=invoice.id,
        status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001,
        created_at=NOW, resolved_at=NOW,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    InvoiceAllocationRepository(session).add(
        InvoiceAllocation(
            id=uuid.uuid4(), invoice_id=invoice.id, contract_id=contract.id,
            match_case_id=match_case.id, allocated_gross_amount=gross_amount,
            match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    )
    if product_name is not None or tax_rate is not None:
        invoice_item = InvoiceItem(
            id=uuid.uuid4(), invoice_id=invoice.id, line_no=1,
            product_name=product_name, specification=None, unit=None,
            quantity=Decimal("1"), unit_price=None, net_amount=gross_amount,
            tax_rate=tax_rate, tax_amount=Decimal("0"), gross_amount=gross_amount,
            source_fragment_id=fragment_id,
        )
        InvoiceItemRepository(session).add(invoice_item)
        session.flush()
        return invoice, invoice_item
    session.flush()
    return invoice, None


def _make_contract_item(session, fragment_id, contract, *, product_name):
    item = ContractItem(
        id=uuid.uuid4(), contract_id=contract.id, source_item_key=f"ITEM-F2A-{uuid.uuid4().hex[:8]}",
        sku=None, product_name=product_name, specification=None, quantity=Decimal("1"), unit=None,
        unit_price=None, gross_amount=Decimal("0"), tax_rate=None, net_amount=Decimal("0"),
        current_source_fragment_id=fragment_id, created_at=NOW,
    )
    ContractItemRepository(session).add(item)
    session.flush()
    return item


def _make_invoice_item_alloc(session, fragment_id, invoice_item, contract_item):
    InvoiceItemAllocationRepository(session).add(
        InvoiceItemAllocation(
            id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=contract_item.id,
            allocated_quantity=Decimal("1"), allocated_net_amount=invoice_item.net_amount,
            confirmation_type="MANUAL_CONFIRMED", source_fragment_id=fragment_id, created_at=NOW,
            superseded_by_fact_id=None,
        )
    )
    session.flush()


def _make_out_payment_alloc(session, fragment_id, contract, bank_reference):
    payment = Payment(
        id=uuid.uuid4(), transaction_date=date(2031, 1, 15), direction=PaymentDirection.OUT,
        amount=Decimal("50.00"), counterparty="Supplier F2A", business_type=None,
        bank_reference=bank_reference, description=None, running_balance=None,
        source_fragment_id=fragment_id, created_at=NOW,
    )
    PaymentRepository(session).add(payment)
    session.flush()
    match_case = MatchCase(
        id=uuid.uuid4(), subject_type="PAYMENT", subject_id=payment.id,
        status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001,
        created_at=NOW, resolved_at=NOW,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    PaymentAllocationRepository(session).add(
        PaymentAllocation(
            id=uuid.uuid4(), payment_id=payment.id, contract_id=contract.id,
            match_case_id=match_case.id, allocated_amount=Decimal("50.00"),
            match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    )
    session.flush()
    return payment


def _build_workbench_db(db_path):
    """One DB exercising the F2a scenarios: sales MATCH / DEVIATION /
    ambiguous / missing-invoice, and supplier follow-up / amount-deviation /
    product-deviation / cardinality (with an OUT payment and a tax_rate
    that must never surface as a warning)."""
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        frag = _make_fragment(session)

        # SC-MTCH: 1:1:1 MATCH (contract / declared / invoice = 100 USD).
        match_contract = _make_contract_f2a(session, frag.id, "PO-MTCH", gross_amount=Decimal("100.00"), currency="USD")
        match_sc = _make_sales_contract(
            session, frag.id, "SC-MTCH",
            fields={"customer": "Customer Match", "gross_amount": Decimal("100.00"), "currency": "USD"},
        )
        add_procurement_sales_link(session, procurement_contract_id=match_contract.id, sales_contract_id=match_sc.id,
                                   source_fragment_id=frag.id, confirmation_type=LinkConfirmationType.AUTO_CONFIRMED, created_at=NOW)
        _make_shipment_fact(session, match_contract, frag.id, "SHIP-MTCH", Decimal("100.00"), "USD")
        _make_sales_invoice_alloc(session, frag.id, match_sc, gross_amount=Decimal("100.00"), currency="USD")

        # SC-DEV: invoice gross deviates (90 vs 100 USD).
        dev_contract = _make_contract_f2a(session, frag.id, "PO-DEV", gross_amount=Decimal("100.00"), currency="USD")
        dev_sc = _make_sales_contract(
            session, frag.id, "SC-DEV",
            fields={"customer": "Customer Dev", "gross_amount": Decimal("100.00"), "currency": "USD"},
        )
        add_procurement_sales_link(session, procurement_contract_id=dev_contract.id, sales_contract_id=dev_sc.id,
                                   source_fragment_id=frag.id, confirmation_type=LinkConfirmationType.AUTO_CONFIRMED, created_at=NOW)
        _make_shipment_fact(session, dev_contract, frag.id, "SHIP-DEV", Decimal("100.00"), "USD")
        _make_sales_invoice_alloc(session, frag.id, dev_sc, gross_amount=Decimal("90.00"), currency="USD")

        # SC-AMB: multiple current links -> ambiguous scope.
        amb_c1 = _make_contract_f2a(session, frag.id, "PO-AMB-1", gross_amount=Decimal("100.00"), currency="USD")
        amb_c2 = _make_contract_f2a(session, frag.id, "PO-AMB-2", gross_amount=Decimal("100.00"), currency="USD")
        amb_sc = _make_sales_contract(
            session, frag.id, "SC-AMB",
            fields={"customer": "Customer Amb", "gross_amount": Decimal("100.00"), "currency": "USD"},
        )
        add_procurement_sales_link(session, procurement_contract_id=amb_c1.id, sales_contract_id=amb_sc.id,
                                   source_fragment_id=frag.id, confirmation_type=LinkConfirmationType.AUTO_CONFIRMED, created_at=NOW)
        add_procurement_sales_link(session, procurement_contract_id=amb_c2.id, sales_contract_id=amb_sc.id,
                                   source_fragment_id=frag.id, confirmation_type=LinkConfirmationType.AUTO_CONFIRMED, created_at=NOW)
        _make_shipment_fact(session, amb_c1, frag.id, "SHIP-AMB-1", Decimal("100.00"), "USD")
        _make_shipment_fact(session, amb_c2, frag.id, "SHIP-AMB-2", Decimal("100.00"), "USD")
        _make_sales_invoice_alloc(session, frag.id, amb_sc, gross_amount=Decimal("100.00"), currency="USD")

        # SC-NOINV: linked + shipment but NO confirmed SALES invoice.
        noinv_contract = _make_contract_f2a(session, frag.id, "PO-NOINV", gross_amount=Decimal("100.00"), currency="USD")
        noinv_sc = _make_sales_contract(
            session, frag.id, "SC-NOINV",
            fields={"customer": "Customer NoInv", "gross_amount": Decimal("100.00"), "currency": "USD"},
        )
        add_procurement_sales_link(session, procurement_contract_id=noinv_contract.id, sales_contract_id=noinv_sc.id,
                                   source_fragment_id=frag.id, confirmation_type=LinkConfirmationType.AUTO_CONFIRMED, created_at=NOW)
        _make_shipment_fact(session, noinv_contract, frag.id, "SHIP-NOINV", Decimal("100.00"), "USD")

        # PO-FOLLOW: paid but NO purchase invoice -> P09 follow-up.
        follow_contract = _make_contract_f2a(session, frag.id, "PO-FOLLOW", gross_amount=Decimal("100.00"), currency="USD")
        _make_out_payment_alloc(session, frag.id, follow_contract, "REF-FOLLOW-OUT")

        # PO-AMT: amount deviation (90 vs 100), invoice_item tax_rate +
        # matching product name (tax_rate must stay CONTEXT only).
        amt_contract = _make_contract_f2a(session, frag.id, "PO-AMT", gross_amount=Decimal("100.00"), currency="USD")
        amt_item = _make_contract_item(session, frag.id, amt_contract, product_name="Widget Amt")
        _, amt_invoice_item = _make_purchase_invoice_alloc(
            session, frag.id, amt_contract, gross_amount=Decimal("90.00"), currency="USD",
            tax_rate=Decimal("13.00"), product_name="Widget Amt",
        )
        _make_invoice_item_alloc(session, frag.id, amt_invoice_item, amt_item)

        # PO-PROD: amount MATCH but product name deviates.
        prod_contract = _make_contract_f2a(session, frag.id, "PO-PROD", gross_amount=Decimal("100.00"), currency="USD")
        prod_item = _make_contract_item(session, frag.id, prod_contract, product_name="Widget Prod")
        _, prod_invoice_item = _make_purchase_invoice_alloc(
            session, frag.id, prod_contract, gross_amount=Decimal("100.00"), currency="USD",
            product_name="Widget Prod X",
        )
        _make_invoice_item_alloc(session, frag.id, prod_invoice_item, prod_item)

        # PO-MULTI: two purchase invoices + OUT payment -> cardinality
        # advisory; the payment with invoices must NOT be a follow-up.
        multi_contract = _make_contract_f2a(session, frag.id, "PO-MULTI", gross_amount=Decimal("100.00"), currency="USD")
        _make_purchase_invoice_alloc(session, frag.id, multi_contract, gross_amount=Decimal("60.00"), currency="USD")
        _make_purchase_invoice_alloc(session, frag.id, multi_contract, gross_amount=Decimal("40.00"), currency="USD")
        _make_out_payment_alloc(session, frag.id, multi_contract, "REF-MULTI-OUT")

        session.commit()
    engine.dispose()


@pytest.fixture
def workbench_ctx(tmp_path):
    db_path = tmp_path / "invoice-prep-workbench.db"
    _build_workbench_db(db_path)
    app = create_app(f"sqlite:///{db_path}")
    client = TestClient(app)
    return client, app


def test_f2a_renders_sales_match(workbench_ctx):
    client, _ = workbench_ctx
    response = client.get("/invoice-preparation")
    assert "SC-MTCH" in response.text
    assert F2A_SALES_MATCH_LABEL in response.text


def test_f2a_renders_sales_deviation_and_advisory(workbench_ctx):
    client, _ = workbench_ctx
    response = client.get("/invoice-preparation")
    assert "SC-DEV" in response.text
    assert F2A_SALES_DEVIATION_LABEL in response.text
    assert "销项发票金额与合同/报关金额存在偏差，建议复核" in response.text


def test_f2a_renders_sales_not_comparable_missing_and_ambiguous(workbench_ctx):
    client, _ = workbench_ctx
    response = client.get("/invoice-preparation")
    # SC-NOINV -> missing fact; SC-AMB -> ambiguous scope.
    assert F2A_SALES_MISSING_LABEL in response.text
    assert F2A_SALES_AMBIGUOUS_LABEL in response.text


def test_f2a_renders_supplier_follow_up_reminder(workbench_ctx):
    client, _ = workbench_ctx
    response = client.get("/invoice-preparation")
    assert "PO-FOLLOW" in response.text
    assert F2A_SUPPLIER_FOLLOW_UP_LABEL in response.text


def test_f2a_renders_supplier_amount_product_cardinality_advisories(workbench_ctx):
    client, _ = workbench_ctx
    response = client.get("/invoice-preparation")
    # PO-AMT (amount), PO-PROD (product name), PO-MULTI (cardinality).
    assert "采购发票金额与合同参考金额存在偏差，建议复核" in response.text
    assert "商品名称与合同确认名称不一致，建议复核" in response.text
    assert "一个采购合同关联多张已确认采购发票，建议复核" in response.text


def test_f2a_payment_alone_is_not_a_warning(workbench_ctx):
    """Only PO-FOLLOW (paid + NO invoice) carries the follow-up reminder:
    PO-MULTI has an OUT payment AND confirmed invoices, so its payment is
    presented as a fact with no follow-up warning — the payment alone
    never surfaces as a reminder."""
    client, _ = workbench_ctx
    response = client.get("/invoice-preparation")
    assert response.text.count(F2A_SUPPLIER_FOLLOW_UP_LABEL) == 1
    # PO-MULTI's payment is still rendered as a fact.
    assert "REF-MULTI-OUT" in response.text


def test_f2a_tax_rate_alone_is_not_a_warning(workbench_ctx):
    """An existing InvoiceItem tax_rate is CONTEXT only (IP-P06): PO-AMT
    carries a tax_rate Fact but the page surfaces no tax-rate content or
    warning (no invented field, no tax advisory)."""
    client, _ = workbench_ctx
    response = client.get("/invoice-preparation")
    assert "税率" not in response.text


def test_f2a_no_fake_zero_or_default_for_missing_facts(workbench_ctx):
    """SC-NOINV has no confirmed SALES invoice: the comparison renders the
    missing invoice leg as '—' (factual absence), never a fake 0 or a
    manufactured currency."""
    client, _ = workbench_ctx
    response = client.get("/invoice-preparation")
    assert "已确认销项发票金额：— —" in response.text
    # The resolved legs are still shown for the "why".
    assert "报关申报金额：100.00 USD" in response.text


def test_f2a_workbench_carries_no_judgment_or_internal_enum(workbench_ctx):
    client, _ = workbench_ctx
    response = client.get("/invoice-preparation")
    for label in FORBIDDEN_JUDGMENT_LABELS:
        assert label not in response.text, f"judgment/internal-enum text must not appear: {label}"


def test_f2a_workbench_is_read_only(workbench_ctx):
    client, app = workbench_ctx
    before = _db_counts(app.state.session_factory)
    response = client.get("/invoice-preparation")
    assert response.status_code == 200
    assert _db_counts(app.state.session_factory) == before


def test_f2a_incomplete_allocation_does_not_become_confirmed_fact():
    """A SalesInvoiceAllocation whose Invoice Fact is missing is NOT a
    confirmed Invoice Fact in the presentation layer either: the VM's
    amount control shows the invoice leg as unavailable, and the allocation
    renders with '—' rather than a fabricated invoice number. (Pure-context
    — the repositories cannot persist a dangling allocation, and the F0
    context defensively yields invoice=None.)"""
    sc_id, contract_id, shipment_id, dangling_invoice_id = (uuid.uuid4() for _ in range(4))
    sales_contract = SalesContract(
        id=sc_id, our_entity="Our Own Entity", sales_contract_no="SC-DANGLING",
        customer="Customer D", currency="USD", gross_amount=Decimal("100.00"),
        contract_date=date(2026, 1, 1), current_source_fragment_id=uuid.uuid4(), created_at=NOW,
    )
    contract = Contract(
        id=contract_id, contract_no="PO-DANGLING", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=Decimal("100.00"), currency="USD", contract_date=date(2026, 1, 1),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    link = ProcurementSalesLink(
        id=uuid.uuid4(), procurement_contract_id=contract_id, sales_contract_id=sc_id,
        source_fragment_id=uuid.uuid4(), confirmation_type="AUTO_CONFIRMED", created_at=NOW,
    )
    shipment = Shipment(
        id=shipment_id, contract_id=contract_id, external_reference="SHIP-DANGLING",
        execution_date=date(2031, 2, 1), contract_item_id=None, quantity=Decimal("1"),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW,
        declared_amount=Decimal("100.00"), declared_currency="USD",
    )
    context = InvoicePreparationContext(
        sales_scopes=(
            SalesScopeContext(
                sales_contract=sales_contract,
                linked_procurement_contracts=(SalesScopeLinkedProcurementContract(link=link, contract=contract),),
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
    vm = InvoicePreparationVM(get_invoice_preparation_workbench_from_context(context))
    scope = vm.sales_scopes[0]
    # The dangling allocation is NOT a confirmed Invoice Fact: it must NOT
    # appear in the confirmed sales-invoice list, and the comparison treats
    # the invoice leg as unavailable (business label). The association
    # itself stays visible only under the attention wording.
    assert scope.confirmed_invoice_allocations == []
    assert len(scope.incomplete_allocations) == 1
    assert scope.incomplete_allocations[0].kind_label == "销项发票关联"
    assert scope.amount_control.outcome_label == F2A_SALES_MISSING_LABEL
    assert scope.amount_control.invoice_amount == "—"
    assert scope.has_advisories is False


def test_f2a_dangling_sales_receipt_is_not_a_confirmed_fact():
    """A SalesPaymentAllocation whose Payment Fact is missing is NOT a
    confirmed IN receipt: it must not appear under 已关联收款事实, and stays
    visible only under the attention wording."""
    sc_id, dangling_payment_id = uuid.uuid4(), uuid.uuid4()
    sales_contract = SalesContract(
        id=sc_id, our_entity="Our Own Entity", sales_contract_no="SC-DANGLING-REC",
        customer="Customer D", currency="USD", gross_amount=Decimal("100.00"),
        contract_date=date(2026, 1, 1), current_source_fragment_id=uuid.uuid4(), created_at=NOW,
    )
    context = InvoicePreparationContext(
        sales_scopes=(
            SalesScopeContext(
                sales_contract=sales_contract,
                linked_procurement_contracts=(),
                invoice_allocations=(),
                payment_allocations=(
                    SalesScopePaymentAllocation(
                        allocation=SalesPaymentAllocation(
                            id=uuid.uuid4(), payment_id=dangling_payment_id, sales_contract_id=sc_id,
                            match_case_id=uuid.uuid4(), allocated_amount=Decimal("60.00"),
                            confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
                        ),
                        payment=None,
                    ),
                ),
                unresolved_work=(),
            ),
        ),
        supplier_scopes=(),
    )
    vm = InvoicePreparationVM(get_invoice_preparation_workbench_from_context(context))
    scope = vm.sales_scopes[0]
    assert scope.confirmed_receipt_allocations == []
    assert len(scope.incomplete_allocations) == 1
    assert scope.incomplete_allocations[0].kind_label == "收款关联"


def test_f2a_dangling_purchase_invoice_allocation_is_not_a_confirmed_fact():
    """An InvoiceAllocation whose Invoice Fact is missing is NOT a confirmed
    PURCHASE invoice: it must not appear under 已确认采购发票, and stays
    visible only under the attention wording."""
    contract_id, dangling_invoice_id = uuid.uuid4(), uuid.uuid4()
    contract = Contract(
        id=contract_id, contract_no="PO-DANGLING-PINV", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=Decimal("100.00"), currency="USD", contract_date=date(2026, 1, 1),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    context = InvoicePreparationContext(
        sales_scopes=(),
        supplier_scopes=(
            SupplierScopeContext(
                contract=contract, items=(), shipments=(),
                invoice_allocations=(
                    SupplierScopeInvoiceAllocation(
                        allocation=InvoiceAllocation(
                            id=uuid.uuid4(), invoice_id=dangling_invoice_id, contract_id=contract_id,
                            match_case_id=uuid.uuid4(), allocated_gross_amount=Decimal("100.00"),
                            match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                            confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
                        ),
                        invoice=None,
                    ),
                ),
                invoice_item_allocations=(), payment_allocations=(), unresolved_work=(),
            ),
        ),
    )
    vm = InvoicePreparationVM(get_invoice_preparation_workbench_from_context(context))
    scope = vm.supplier_scopes[0]
    assert scope.confirmed_invoice_allocations == []
    assert len(scope.incomplete_allocations) == 1
    assert scope.incomplete_allocations[0].kind_label == "采购发票关联"


def test_f2a_dangling_out_payment_allocation_is_not_a_confirmed_fact():
    """A PaymentAllocation whose Payment Fact is missing is NOT a confirmed
    OUT payment: it must not appear under 已确认付款, and stays visible only
    under the attention wording."""
    contract_id, dangling_payment_id = uuid.uuid4(), uuid.uuid4()
    contract = Contract(
        id=contract_id, contract_no="PO-DANGLING-OUT", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=Decimal("100.00"), currency="USD", contract_date=date(2026, 1, 1),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    context = InvoicePreparationContext(
        sales_scopes=(),
        supplier_scopes=(
            SupplierScopeContext(
                contract=contract, items=(), shipments=(),
                invoice_allocations=(),
                invoice_item_allocations=(),
                payment_allocations=(
                    SupplierScopePaymentAllocation(
                        allocation=PaymentAllocation(
                            id=uuid.uuid4(), payment_id=dangling_payment_id, contract_id=contract_id,
                            match_case_id=uuid.uuid4(), allocated_amount=Decimal("50.00"),
                            match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                            confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
                        ),
                        payment=None,
                    ),
                ),
                unresolved_work=(),
            ),
        ),
    )
    vm = InvoicePreparationVM(get_invoice_preparation_workbench_from_context(context))
    scope = vm.supplier_scopes[0]
    assert scope.confirmed_payment_allocations == []
    assert len(scope.incomplete_allocations) == 1
    assert scope.incomplete_allocations[0].kind_label == "付款关联"


def test_unresolved_work_is_under_attention_not_facts(prep_ctx):
    """SC-101 carries an OPEN SalesContractCustomerUnresolved task: it must
    appear under 提醒/待关注 as 已有待处理事项, and the old bare 待处理事项
    heading must no longer appear inside 已确认事实 (unresolved work is
    existing Task/Exception context, not a Business Fact)."""
    client, _ = prep_ctx
    response = client.get("/invoice-preparation")
    assert "已有待处理事项" in response.text
    assert '<p class="wb-sub">待处理事项</p>' not in response.text
    assert '<h4 class="wb-block-title">提醒 / 待关注</h4>' in response.text


# ---------------------------------------------------------------------------
# Phase 2D.3-F2b — Web Data Product endpoints share the Workbench source.
# ---------------------------------------------------------------------------


def test_f2b_export_xlsx_content_type_and_filename(workbench_ctx):
    client, _ = workbench_ctx
    response = client.get("/invoice-preparation/export.xlsx")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "filename=invoice-preparation.xlsx" in response.headers["content-disposition"]
    import openpyxl
    import io

    wb = openpyxl.load_workbook(io.BytesIO(response.content))
    assert wb.sheetnames == ["01_Summary", "02_Sales_Preparation", "03_Sales_Attention", "04_Supplier_Request", "05_Supplier_Attention"]


def test_f2b_export_csv_content_type_and_filename(workbench_ctx):
    client, _ = workbench_ctx
    response = client.get("/invoice-preparation/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "filename=invoice-preparation.csv" in response.headers["content-disposition"]
    assert b"record_type" in response.content


def test_f2b_web_exports_share_the_workbench_source(workbench_ctx):
    """The HTML page, the XLSX export and the CSV export all originate
    from the SAME InvoicePreparationWorkbench: the exports serialize the
    same product the builder produces from that workbench."""
    from bel.application.invoice_preparation_export import (
        build_invoice_preparation_data_product,
        export_invoice_preparation_csv,
        export_invoice_preparation_xlsx,
    )

    client, app = workbench_ctx
    from bel.application.invoice_preparation_workbench import get_invoice_preparation_workbench

    with app.state.session_factory() as session:
        product = build_invoice_preparation_data_product(get_invoice_preparation_workbench(session))
    expected_csv = export_invoice_preparation_csv(product)
    expected_xlsx = export_invoice_preparation_xlsx(product)

    assert client.get("/invoice-preparation/export.csv").content == expected_csv
    assert client.get("/invoice-preparation/export.xlsx").content == expected_xlsx
    # The page still renders from that same workbench (same scope count).
    page = client.get("/invoice-preparation")
    assert f"{len(product.sales_preparation)} 个销售范围" in page.text


def test_f2b_web_exports_are_zero_write(workbench_ctx):
    client, app = workbench_ctx
    before = _db_counts(app.state.session_factory)
    client.get("/invoice-preparation/export.xlsx")
    client.get("/invoice-preparation/export.csv")
    assert _db_counts(app.state.session_factory) == before

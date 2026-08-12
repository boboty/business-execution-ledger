"""Manual InvoiceItem allocation API tests.

The endpoint must reuse the SAME Application Service as the CLI
(``allocate_invoice_item``): confirmed contract scope, capacity,
Evidence creation, single commit. Success -> 201; every failure ->
400 with a readable business error and ZERO partial writes.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

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
)

NOW = datetime.now(timezone.utc)


def _count_docs(session) -> int:
    from bel.infrastructure.persistence.models import EvidenceDocumentModel

    return session.query(EvidenceDocumentModel).count()


def _build_minimal_db(tmp_path, *, confirm: bool) -> tuple[TestClient, str, object]:
    """A tiny DB: one contract with ITEM-A, one invoice with line 1.
    ``confirm`` controls whether the contract-level InvoiceAllocation
    exists — the section-11-A guard under test."""
    from bel.web.app import create_app

    db_path = tmp_path / f"alloc-{uuid.uuid4().hex[:8]}.db"
    engine = make_engine(str(db_path))
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        ev = EvidenceRepository(session)
        doc = EvidenceDocument(
            id=uuid.uuid4(), file_name="synthetic.xlsx", sha256=uuid.uuid4().hex,
            source_type="synthetic", imported_at=NOW,
        )
        ev.add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.EXCEL_ROW,
            sheet_name="s1", row_number=1, locator_json=None, raw_data={}, created_at=NOW,
        )
        ev.add_fragment(frag)
        session.flush()

        contract = Contract(
            id=uuid.uuid4(), contract_no="PO-WEB-001", contract_type=None, counterparty="SupplierWeb",
            buyer="BuyerWeb", gross_amount=Decimal("1000.00"), currency="CNY", contract_date=None,
            current_source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
        )
        ContractRepository(session).add(contract)
        session.flush()
        item = ContractItem(
            id=uuid.uuid4(), contract_id=contract.id, source_item_key="ITEM-A", sku=None,
            product_name="Web Widget", specification=None, quantity=Decimal("100"), unit="件",
            unit_price=None, gross_amount=None, tax_rate=None, net_amount=None,
            current_source_fragment_id=frag.id, created_at=NOW,
        )
        ContractItemRepository(session).add(item)
        session.flush()

        invoice = Invoice(
            id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
            digital_invoice_no="DIGITAL-WEB-001", external_invoice_key="DIGITAL-WEB-001",
            issue_date=date(2031, 3, 15), seller="SupplierWeb", buyer="BuyerWeb",
            net_amount=Decimal("1000.00"), tax_amount=Decimal("0"), gross_amount=Decimal("1000.00"),
            invoice_status=None, source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
        )
        InvoiceRepository(session).add(invoice)
        session.flush()
        invoice_item = InvoiceItem(
            id=uuid.uuid4(), invoice_id=invoice.id, line_no=1, product_name="Web Widget",
            specification=None, unit="件", quantity=Decimal("50"), unit_price=None,
            net_amount=Decimal("1000.00"), tax_rate=None, tax_amount=Decimal("0"),
            gross_amount=Decimal("1000.00"), source_fragment_id=frag.id,
        )
        InvoiceItemRepository(session).add(invoice_item)
        session.flush()

        if confirm:
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
                    match_case_id=match_case.id, allocated_gross_amount=invoice.gross_amount,
                    match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                    confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
                )
            )
        session.commit()
        contract_id = str(contract.id)

    app = create_app(str(db_path))
    return TestClient(app), contract_id, app


def _alloc_count(session_factory) -> int:
    with session_factory() as session:
        return InvoiceItemAllocationRepository(session).count()


def test_success_201_creates_allocation_and_evidence(web_client_factory):
    client, app = web_client_factory()
    factory = app.state.session_factory
    with factory() as session:
        invoice = InvoiceRepository(session).find_by_external_key("DIGITAL-CLOSE-006")
        contract = next(c for c in ContractRepository(session).list_all() if c.contract_no == "PO-CLOSE-006")
        contract_id = str(contract.id)
        before_alloc = InvoiceItemAllocationRepository(session).count()
        before_evidence = _count_docs(session)

    response = client.post(
        "/api/invoice-item-allocations",
        json={
            "invoice_external_key": "DIGITAL-CLOSE-006",
            "line_no": 1,
            "contract_id": contract_id,
            "source_item_key": "ITEM-A",
            "quantity": "50",
            "net_amount": "950.00",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert "id" in body
    uuid.UUID(body["id"])

    with factory() as session:
        assert InvoiceItemAllocationRepository(session).count() == before_alloc + 1
        assert _count_docs(session) == before_evidence + 1
        allocation = InvoiceItemAllocationRepository(session).list_all()[-1]
        invoice_item = InvoiceItemRepository(session).list_for_invoice(invoice.id)[0]
        assert allocation.invoice_item_id == invoice_item.id
        assert allocation.allocated_quantity == Decimal("50")
        assert allocation.allocated_net_amount == Decimal("950.00")
        assert allocation.source_fragment_id is not None
        assert allocation.confirmation_type == "MANUAL_CONFIRMED"


def test_cross_contract_400_no_writes(web_client_factory):
    client, app = web_client_factory()
    factory = app.state.session_factory
    with factory() as session:
        target = next(c for c in ContractRepository(session).list_all() if c.contract_no == "PO-CLOSE-002")
        before_alloc = InvoiceItemAllocationRepository(session).count()
        before_evidence = _count_docs(session)

    response = client.post(
        "/api/invoice-item-allocations",
        json={
            "invoice_external_key": "DIGITAL-CLOSE-001",  # confirmed to PO-CLOSE-001
            "line_no": 1,
            "contract_id": str(target.id),  # ... but we target PO-CLOSE-002
            "source_item_key": "ITEM-A",
            "quantity": "35",
            "net_amount": "455.00",
        },
    )
    assert response.status_code == 400
    assert "contract" in response.json()["detail"].lower()

    with factory() as session:
        assert InvoiceItemAllocationRepository(session).count() == before_alloc, "cross-contract must write nothing"
        assert _count_docs(session) == before_evidence, "cross-contract must write no evidence"


def test_capacity_exceeded_400_no_writes(web_client_factory):
    client, app = web_client_factory()
    factory = app.state.session_factory
    with factory() as session:
        contract = next(c for c in ContractRepository(session).list_all() if c.contract_no == "PO-CLOSE-001")
        before_alloc = InvoiceItemAllocationRepository(session).count()
        before_evidence = _count_docs(session)

    # DIGITAL-CLOSE-001 line 1 has quantity 35 and is already fully allocated (35).
    response = client.post(
        "/api/invoice-item-allocations",
        json={
            "invoice_external_key": "DIGITAL-CLOSE-001",
            "line_no": 1,
            "contract_id": str(contract.id),
            "source_item_key": "ITEM-A",
            "quantity": "10",
            "net_amount": "130.00",
        },
    )
    assert response.status_code == 400
    assert "capacity" in response.json()["detail"].lower()

    with factory() as session:
        assert InvoiceItemAllocationRepository(session).count() == before_alloc
        assert _count_docs(session) == before_evidence


def test_missing_contract_level_confirmation_400_no_writes(tmp_path):
    client, contract_id, app = _build_minimal_db(tmp_path, confirm=False)
    factory = app.state.session_factory
    before_alloc = _alloc_count(factory)

    response = client.post(
        "/api/invoice-item-allocations",
        json={
            "invoice_external_key": "DIGITAL-WEB-001",
            "line_no": 1,
            "contract_id": contract_id,
            "source_item_key": "ITEM-A",
            "quantity": "10",
            "net_amount": "200.00",
        },
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "11-A" in detail or "CONFIRMED" in detail
    assert _alloc_count(factory) == before_alloc


def test_success_requires_confirmed_contract_scope(tmp_path):
    client, contract_id, app = _build_minimal_db(tmp_path, confirm=True)
    factory = app.state.session_factory
    before_alloc = _alloc_count(factory)

    response = client.post(
        "/api/invoice-item-allocations",
        json={
            "invoice_external_key": "DIGITAL-WEB-001",
            "line_no": 1,
            "contract_id": contract_id,
            "source_item_key": "ITEM-A",
            "quantity": "10",
            "net_amount": "200.00",
        },
    )
    assert response.status_code == 201
    assert _alloc_count(factory) == before_alloc + 1


def test_invalid_payload_400(web_client):
    response = web_client.post("/api/invoice-item-allocations", json={})
    assert response.status_code == 400
    assert "invoice_external_key" in response.json()["detail"]

    response = web_client.post(
        "/api/invoice-item-allocations",
        json={
            "invoice_external_key": "DIGITAL-CLOSE-006",
            "line_no": 1,
            "contract_id": "not-a-uuid",
            "source_item_key": "ITEM-A",
            "quantity": "50",
            "net_amount": "950.00",
        },
    )
    assert response.status_code == 400


def test_unknown_invoice_400(web_client):
    response = web_client.post(
        "/api/invoice-item-allocations",
        json={
            "invoice_external_key": "DIGITAL-UNKNOWN-999",
            "line_no": 1,
            "contract_id": str(uuid.uuid4()),
            "source_item_key": "ITEM-A",
            "quantity": "1",
            "net_amount": "1.00",
        },
    )
    assert response.status_code == 400
    assert "not found" in response.json()["detail"]


def test_allocation_never_guesses_contract_item(web_client_factory):
    """Codex gate D: the page's allocation select must never default to a
    contract item — even when two items have similar names/amounts. The
    page renders a placeholder option that is disabled and selected, so
    no item is ever pre-chosen."""
    client, app = web_client_factory()
    from bel.infrastructure.persistence.repositories import ContractRepository

    with app.state.session_factory() as session:
        contract = next(c for c in ContractRepository(session).list_all() if c.contract_no == "PO-CLOSE-006")
    html = client.get(f"/contracts/{contract.id}?period=2031-03").text
    form_start = html.find("data-allocation-submit")
    assert form_start != -1
    select_open = html.find("<select", form_start)
    select_close = html.find("</select>", select_open)
    select = html[select_open : select_close + len("</select>")]
    assert 'value="" disabled selected' in select
    assert 'option value="ITEM-A"' in select

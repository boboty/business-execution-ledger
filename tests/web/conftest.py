"""Web test fixtures — every committed UI test runs against the existing
independently-synthetic Phase 2B fixture (docs/PRIVATE-DATA-POLICY.md).
No private data ever enters a rendered page, a screenshot, pytest
output, or this repository.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from bel.application.import_close_facts import import_close_facts
from bel.application.import_contract_ledger import import_contract_ledger
from bel.application.import_invoices import import_invoices
from bel.domain.invoice import InvoiceDirection
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from fixtures.synthetic import scenarios
from fixtures.synthetic.phase2b_close import (
    CLOSE_FACT_PACK,
    CLOSE_PERIOD,
    PHASE2B_CONTRACT_HEADERS,
    PHASE2B_CONTRACT_ROWS,
    PHASE2B_INVOICE_ROWS,
)
from tests.conftest import write_invoice_workbook, write_ledger_workbook

from bel.web.app import create_app  # noqa: E402


def _confirm_contract_allocations(session: Session) -> None:
    """Phase 2A M001 output, constructed directly (the partial-receipt
    invoices never match contract gross by amount)."""
    from bel.domain.matching import (
        AllocationMatchMethod,
        ConfirmationType,
        InvoiceAllocation,
        MatchCase,
        MatchCaseStatus,
        MatchMethod,
    )
    from bel.infrastructure.persistence.repositories import (
        ContractRepository,
        InvoiceAllocationRepository,
        InvoiceRepository,
        MatchCaseRepository,
    )

    now = datetime.now(timezone.utc)
    for external_key, contract_no in [
        ("DIGITAL-CLOSE-001", "PO-CLOSE-001"),
        ("DIGITAL-CLOSE-002", "PO-CLOSE-002"),
        ("DIGITAL-CLOSE-005", "PO-CLOSE-005"),
        ("DIGITAL-CLOSE-006", "PO-CLOSE-006"),
    ]:
        invoice = InvoiceRepository(session).find_by_external_key(external_key)
        contract = next(c for c in ContractRepository(session).list_all() if c.contract_no == contract_no)
        match_case = MatchCase(
            id=uuid.uuid4(),
            subject_type="INVOICE",
            subject_id=invoice.id,
            status=MatchCaseStatus.AUTO_CONFIRMED,
            match_method=MatchMethod.M001,
            created_at=now,
            resolved_at=now,
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
                created_at=now,
            )
        )


def _add_payment_for_contract(session: Session, contract_no: str = "PO-CLOSE-001") -> None:
    """One explicitly-allocated Payment for the Contract360 payment area."""
    from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
    from bel.domain.matching import (
        AllocationMatchMethod,
        ConfirmationType,
        MatchCase,
        MatchCaseStatus,
        MatchMethod,
        PaymentAllocation,
    )
    from bel.domain.payment import Payment
    from bel.infrastructure.persistence.repositories import (
        ContractRepository,
        EvidenceRepository,
        MatchCaseRepository,
        PaymentAllocationRepository,
        PaymentRepository,
    )

    now = datetime.now(timezone.utc)
    contract = next(c for c in ContractRepository(session).list_all() if c.contract_no == contract_no)

    document = EvidenceDocument(
        id=uuid.uuid4(), file_name="bank-statement.pdf", sha256=uuid.uuid4().hex, source_type="bank_statement", imported_at=now
    )
    EvidenceRepository(session).add_document(document)
    fragment = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=document.id,
        fragment_kind=FragmentKind.PDF_TRANSACTION,
        sheet_name=None,
        row_number=None,
        locator_json={"page": 1, "index": 0},
        raw_data={},
        created_at=now,
    )
    EvidenceRepository(session).add_fragment(fragment)
    session.flush()

    payment = Payment(
        id=uuid.uuid4(),
        transaction_date=date(2031, 3, 20),
        direction="OUT",
        amount=Decimal("455.00"),
        counterparty="SupplierCloseAlpha",
        business_type="采购款",
        bank_reference="REF-CLOSE-001",
        description=None,
        running_balance=None,
        source_fragment_id=fragment.id,
        created_at=now,
    )
    PaymentRepository(session).add(payment)
    session.flush()

    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type="PAYMENT",
        subject_id=payment.id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=now,
        resolved_at=now,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    PaymentAllocationRepository(session).add(
        PaymentAllocation(
            id=uuid.uuid4(),
            payment_id=payment.id,
            contract_id=contract.id,
            match_case_id=match_case.id,
            allocated_amount=payment.amount,
            match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED,
            created_at=now,
        )
    )
    session.commit()


def build_phase2b_db(db_path: Path, tmp_path: Path, *, with_payment: bool = True) -> None:
    """Create a migrated-shape DB at *db_path* seeded with the synthetic
    Phase 2B fixture (contracts, invoices, confirmations, close facts).
    Schema is created from the ORM models (same fidelity guarantee as the
    rest of the suite; Alembic shape is covered by test_migration.py)."""
    contracts_xlsx = tmp_path / "phase2b-contracts.xlsx"
    invoices_xlsx = tmp_path / "phase2b-invoices.xlsx"
    facts_json = tmp_path / "phase2b-close-facts.json"
    write_ledger_workbook(contracts_xlsx, PHASE2B_CONTRACT_HEADERS, PHASE2B_CONTRACT_ROWS)
    write_invoice_workbook(invoices_xlsx, scenarios.BUYER, PHASE2B_INVOICE_ROWS)
    facts_json.write_text(json.dumps(CLOSE_FACT_PACK), encoding="utf-8")

    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        import_contract_ledger(session, contracts_xlsx)
        import_invoices(session, invoices_xlsx, InvoiceDirection.PURCHASE)
        _confirm_contract_allocations(session)
        session.commit()
        import_close_facts(session, facts_json)
        if with_payment:
            _add_payment_for_contract(session)


@pytest.fixture
def web_app(tmp_path):
    """Build a fresh synthetic DB and return a create_app callable bound
    to it. Each call yields a NEW database (function-scoped)."""
    def _make(db_path: Path | None = None, *, with_payment: bool = True):
        path = db_path or tmp_path / f"web-{uuid.uuid4().hex[:8]}.db"
        build_phase2b_db(path, tmp_path, with_payment=with_payment)
        return create_app(f"sqlite:///{path}")

    return _make


@pytest.fixture
def web_ctx(web_app):
    """One synthetic DB shared by the page client, the application layer
    cross-check, and the contract-id lookup — so tests can correlate the
    web output with the underlying DTO."""
    from types import SimpleNamespace

    from bel.infrastructure.persistence.repositories import ContractRepository

    app = web_app()
    client = TestClient(app)
    with app.state.session_factory() as session:
        contract_id_by_no = {c.contract_no: str(c.id) for c in ContractRepository(session).list_all()}
    return SimpleNamespace(client=client, app=app, contract_id_by_no=contract_id_by_no)


@pytest.fixture
def web_client(web_ctx):
    return web_ctx.client


@pytest.fixture
def contract_id_by_no(web_ctx):
    return web_ctx.contract_id_by_no


@pytest.fixture
def app_for_client(web_ctx):
    """(client, app) sharing the SAME synthetic database — used to read
    the DB through the application layer and cross-check web output."""
    return web_ctx.client, web_ctx.app


@pytest.fixture
def web_client_factory(web_app):
    def _make(with_payment: bool = True):
        app = web_app(with_payment=with_payment)
        return TestClient(app), app

    return _make


CLOSE_PERIOD_FIXTURE = CLOSE_PERIOD

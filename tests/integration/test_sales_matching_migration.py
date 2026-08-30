"""Automated migration tests for a1b2c3d4e5f6_sales_allocation (Phase
2D.1-R3b). No pre-existing data for any of the three new tables — none
of these objects existed before this round. `match_cases` itself is
untouched (no new column, no new constraint — see the migration's
module docstring for why an earlier draft's uniqueness constraint was
reverted). What this migration MUST preserve is every existing table
(contracts, sales_contracts, procurement_sales_links, shipments,
invoice_allocations, payment_allocations, match_cases, ...) completely
untouched.

Covers:
- fresh DB -> head (sales_invoice_allocations / sales_payment_allocations
  / sales_match_candidates tables + constraints exist)
- R3a-populated baseline (f1a2b3c4d5e6) WITH Contract, SalesContract,
  ProcurementSalesLink, Shipment, and existing procurement
  InvoiceAllocation/PaymentAllocation -> head: all preserved, counts
  unchanged, no sales allocation auto-generated
- head -> R3a baseline (downgrade): the three new tables dropped, all
  pre-existing rows untouched
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine

from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    InvoiceAllocationRepository,
    InvoiceRepository,
    PaymentAllocationRepository,
    PaymentRepository,
    ProcurementSalesLinkRepository,
    SalesContractRepository,
    SalesInvoiceAllocationRepository,
    SalesPaymentAllocationRepository,
    ShipmentRepository,
)

REPO_ROOT = Path(__file__).parent.parent.parent
R3A_REVISION = "f1a2b3c4d5e6"
HEAD_REVISION = "a1b2c3d4e5f6"


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )


def test_fresh_database_upgrades_to_head(tmp_path):
    db_path = tmp_path / "fresh.db"
    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    con = create_engine(f"sqlite:///{db_path}").connect()
    tables = {row[0] for row in con.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert {"sales_invoice_allocations", "sales_payment_allocations", "sales_match_candidates"} <= tables

    invoice_columns = {row[1] for row in con.execute(sa.text("PRAGMA table_info(sales_invoice_allocations)"))}
    assert {
        "id", "invoice_id", "sales_contract_id", "match_case_id", "allocated_gross_amount",
        "confirmation_type", "created_at",
    } <= invoice_columns
    payment_columns = {row[1] for row in con.execute(sa.text("PRAGMA table_info(sales_payment_allocations)"))}
    assert {
        "id", "payment_id", "sales_contract_id", "match_case_id", "allocated_amount",
        "confirmation_type", "created_at",
    } <= payment_columns
    candidate_columns = {row[1] for row in con.execute(sa.text("PRAGMA table_info(sales_match_candidates)"))}
    assert {"id", "match_case_id", "sales_contract_id", "created_at"} <= candidate_columns

    # match_cases is untouched — no new uniqueness constraint beyond the
    # primary key's own implicit index.
    match_case_unique_indexes = [
        row[1] for row in con.execute(sa.text("PRAGMA index_list(match_cases)")) if row[2] and row[3] != "pk"
    ]
    assert match_case_unique_indexes == []
    con.close()


def test_r3a_populated_baseline_upgrades_to_head_untouched(tmp_path):
    db_path = tmp_path / "r3a-baseline.db"
    assert _alembic(db_path, "upgrade", R3A_REVISION).returncode == 0

    from bel.application.procurement_sales_link import add_procurement_sales_link
    from bel.application.sales_contract_facts import create_sales_contract_fact
    from bel.application.shipment_facts import create_shipment_fact
    from bel.domain.contract import Contract
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
        SubjectType,
    )
    from bel.domain.payment import Payment, PaymentDirection
    from bel.domain.procurement_sales_link import ConfirmationType as LinkConfirmationType
    from bel.infrastructure.persistence.repositories import EvidenceRepository, MatchCaseRepository

    now = datetime(2026, 1, 1)
    engine = make_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="f", sha256="9" * 64, source_type="t", imported_at=now)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None, row_number=None, locator_json={}, raw_data={}, created_at=now,
        )
        EvidenceRepository(session).add_fragment(frag)
        session.flush()

        contract = Contract(
            id=uuid.uuid4(), contract_no="C-MIGRATION-R3B", contract_type=None, counterparty="Supplier",
            buyer="Buyer", gross_amount=Decimal("1000.00"), currency="CNY", contract_date=None,
            current_source_fragment_id=frag.id, created_at=now, updated_at=now,
        )
        ContractRepository(session).add(contract)
        session.flush()

        sales_contract = create_sales_contract_fact(
            session, our_entity="Entity A", sales_contract_no="SC-MIGRATION-R3B", fields={},
            source_fragment_id=frag.id, created_at=now,
        ).sales_contract

        add_procurement_sales_link(
            session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=frag.id, confirmation_type=LinkConfirmationType.AUTO_CONFIRMED, created_at=now,
        )

        shipment = create_shipment_fact(
            session, contract_id=contract.id, external_reference="EXP-MIGRATION-R3B", execution_date=date(2031, 3, 1),
            fields={"quantity": Decimal("10")}, source_fragment_id=frag.id, created_at=now,
        ).shipment

        invoice = Invoice(
            id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no="INV-R3B",
            digital_invoice_no=None, external_invoice_key="INV-R3B", issue_date=date(2031, 1, 1),
            seller="Supplier", buyer="Buyer", net_amount=Decimal("500.00"), tax_amount=Decimal("0"),
            gross_amount=Decimal("500.00"), invoice_status=None, source_fragment_id=frag.id, created_at=now, updated_at=now,
        )
        InvoiceRepository(session).add(invoice)
        session.flush()
        match_case = MatchCase(
            id=uuid.uuid4(), subject_type=SubjectType.INVOICE, subject_id=invoice.id,
            status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001, created_at=now, resolved_at=now,
        )
        MatchCaseRepository(session).add(match_case)
        session.flush()
        InvoiceAllocationRepository(session).add(
            InvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, contract_id=contract.id, match_case_id=match_case.id,
                allocated_gross_amount=Decimal("500.00"), match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=now,
            )
        )

        payment = Payment(
            id=uuid.uuid4(), transaction_date=date(2031, 1, 1), direction=PaymentDirection.OUT,
            amount=Decimal("300.00"), counterparty="Supplier", business_type=None, bank_reference="REF-R3B",
            description=None, running_balance=None, source_fragment_id=frag.id, created_at=now,
        )
        PaymentRepository(session).add(payment)
        session.flush()
        payment_match_case = MatchCase(
            id=uuid.uuid4(), subject_type=SubjectType.PAYMENT, subject_id=payment.id,
            status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001, created_at=now, resolved_at=now,
        )
        MatchCaseRepository(session).add(payment_match_case)
        session.flush()
        PaymentAllocationRepository(session).add(
            PaymentAllocation(
                id=uuid.uuid4(), payment_id=payment.id, contract_id=contract.id, match_case_id=payment_match_case.id,
                allocated_amount=Decimal("300.00"), match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=now,
            )
        )
        session.commit()
        ids = {
            "contract_id": contract.id, "sales_contract_id": sales_contract.id, "shipment_id": shipment.id,
            "invoice_id": invoice.id, "payment_id": payment.id,
        }

    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    engine = make_engine(str(db_path))
    with make_session_factory(engine)() as session:
        assert ContractRepository(session).get(ids["contract_id"]) is not None
        assert SalesContractRepository(session).get(ids["sales_contract_id"]) is not None
        assert ShipmentRepository(session).get(ids["shipment_id"]) is not None
        assert ProcurementSalesLinkRepository(session).get_current_link(ids["contract_id"], ids["sales_contract_id"]) is not None

        assert PaymentRepository(session).get(ids["payment_id"]) is not None
        payment_allocations = PaymentAllocationRepository(session).list_for_contract(ids["contract_id"])
        assert len(payment_allocations) == 1
        assert payment_allocations[0].allocated_amount == Decimal("300.00")

        invoice_allocations = InvoiceAllocationRepository(session).list_for_contract(ids["contract_id"])
        assert len(invoice_allocations) == 1
        assert invoice_allocations[0].allocated_gross_amount == Decimal("500.00")

        # No sales allocation is ever fabricated by the migration itself.
        assert SalesInvoiceAllocationRepository(session).list_for_invoice(ids["invoice_id"]) == []
        assert SalesInvoiceAllocationRepository(session).list_for_sales_contract(ids["sales_contract_id"]) == []
        assert SalesPaymentAllocationRepository(session).list_for_sales_contract(ids["sales_contract_id"]) == []


def test_downgrade_drops_sales_allocation_tables_but_preserves_existing_data(tmp_path):
    db_path = tmp_path / "downgrade.db"
    assert _alembic(db_path, "upgrade", "head").returncode == 0

    from bel.application.sales_contract_facts import create_sales_contract_fact
    from bel.application.sales_matching import confirm_sales_invoice_match, propose_sales_invoice_match
    from bel.domain.contract import Contract
    from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
    from bel.domain.invoice import Invoice, InvoiceDirection
    from bel.domain.matching import AllocationMatchMethod, ConfirmationType, MatchCase, MatchCaseStatus, MatchMethod, PaymentAllocation, SubjectType
    from bel.domain.payment import Payment, PaymentDirection
    from bel.infrastructure.persistence.repositories import EvidenceRepository, MatchCaseRepository

    now = datetime(2026, 1, 1)
    engine = make_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="f", sha256="8" * 64, source_type="t", imported_at=now)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None, row_number=None, locator_json={}, raw_data={}, created_at=now,
        )
        EvidenceRepository(session).add_fragment(frag)
        session.flush()
        contract = Contract(
            id=uuid.uuid4(), contract_no="C-DOWNGRADE-R3B", contract_type=None, counterparty="Supplier",
            buyer="Buyer", gross_amount=Decimal("1"), currency="CNY", contract_date=None,
            current_source_fragment_id=frag.id, created_at=now, updated_at=now,
        )
        ContractRepository(session).add(contract)
        session.flush()
        sales_contract = create_sales_contract_fact(
            session, our_entity="Entity A", sales_contract_no="SC-DOWNGRADE-R3B", fields={},
            source_fragment_id=frag.id, created_at=now,
        ).sales_contract
        invoice = Invoice(
            id=uuid.uuid4(), direction=InvoiceDirection.SALES, invoice_type=None, invoice_no="INV-DOWNGRADE",
            digital_invoice_no=None, external_invoice_key="INV-DOWNGRADE", issue_date=date(2031, 1, 1),
            seller="Us", buyer="Customer", net_amount=Decimal("100.00"), tax_amount=Decimal("0"),
            gross_amount=Decimal("100.00"), invoice_status=None, source_fragment_id=frag.id, created_at=now, updated_at=now,
        )
        InvoiceRepository(session).add(invoice)
        session.commit()

        proposal = propose_sales_invoice_match(
            session, invoice_id=invoice.id, sales_contract_ids=[sales_contract.id], created_at=now
        )
        session.commit()
        confirm_sales_invoice_match(
            session, match_case_id=proposal.match_case.id, allocations=[(sales_contract.id, Decimal("100.00"))], created_at=now
        )

        # A pre-existing procurement OUT Payment/PaymentAllocation must
        # also survive this downgrade untouched (Gate fix round #1
        # WARNING: previously only verified via an un-persisted ad-hoc
        # check, not the automated migration test).
        out_payment = Payment(
            id=uuid.uuid4(), transaction_date=date(2031, 1, 1), direction=PaymentDirection.OUT,
            amount=Decimal("250.00"), counterparty="Supplier", business_type=None, bank_reference="REF-DOWNGRADE",
            description=None, running_balance=None, source_fragment_id=frag.id, created_at=now,
        )
        PaymentRepository(session).add(out_payment)
        session.flush()
        payment_match_case = MatchCase(
            id=uuid.uuid4(), subject_type=SubjectType.PAYMENT, subject_id=out_payment.id,
            status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001, created_at=now, resolved_at=now,
        )
        MatchCaseRepository(session).add(payment_match_case)
        session.flush()
        PaymentAllocationRepository(session).add(
            PaymentAllocation(
                id=uuid.uuid4(), payment_id=out_payment.id, contract_id=contract.id, match_case_id=payment_match_case.id,
                allocated_amount=Decimal("250.00"), match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=now,
            )
        )
        session.commit()
        contract_id, sales_contract_id, out_payment_id = contract.id, sales_contract.id, out_payment.id

    result = _alembic(db_path, "downgrade", R3A_REVISION)
    assert result.returncode == 0, result.stderr

    con = create_engine(f"sqlite:///{db_path}").connect()
    tables = {row[0] for row in con.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "sales_invoice_allocations" not in tables
    assert "sales_payment_allocations" not in tables
    assert "sales_match_candidates" not in tables

    payment_row = con.execute(
        sa.text("SELECT direction, amount FROM payments WHERE id = :id"), {"id": out_payment_id.hex}
    ).fetchone()
    assert payment_row is not None
    assert payment_row.direction == "OUT"
    assert Decimal(str(payment_row.amount)) == Decimal("250.00")
    allocation_row = con.execute(
        sa.text("SELECT allocated_amount FROM payment_allocations WHERE payment_id = :id"), {"id": out_payment_id.hex}
    ).fetchone()
    assert allocation_row is not None
    assert Decimal(str(allocation_row.allocated_amount)) == Decimal("250.00")

    contract_row = con.execute(
        sa.text("SELECT contract_no FROM contracts WHERE id = :id"), {"id": contract_id.hex}
    ).fetchone()
    sales_contract_row = con.execute(
        sa.text("SELECT sales_contract_no FROM sales_contracts WHERE id = :id"), {"id": sales_contract_id.hex}
    ).fetchone()
    con.close()
    assert contract_row is not None
    assert contract_row.contract_no == "C-DOWNGRADE-R3B"
    assert sales_contract_row is not None
    assert sales_contract_row.sales_contract_no == "SC-DOWNGRADE-R3B"

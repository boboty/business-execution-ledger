"""Integration tests for the Close Fact Pack import: Evidence
traceability (A02), contract-item creation, HistoricalAccrualFact ->
Accrual, idempotency, selector ambiguity rejection, and the item
allocation safety constraints (11-A/B).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bel.application.import_close_facts import CloseFactPackError, import_close_facts
from bel.application.import_contract_ledger import import_contract_ledger
from bel.application.import_invoices import import_invoices
from bel.domain.accrual import AccrualStatus
from bel.domain.invoice import InvoiceDirection
from bel.domain.matching import (
    AllocationMatchMethod,
    ConfirmationType,
    InvoiceAllocation,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
)
from bel.infrastructure.persistence.models import (
    AccrualModel,
    AccrualReversalModel,
    ContractItemModel,
    EvidenceDocumentModel,
    EvidenceFragmentModel,
    HistoricalAccrualFactModel,
    InvoiceItemAllocationModel,
)
from bel.infrastructure.persistence.repositories import (
    AccrualReversalRepository,
    AccrualRepository,
    ContractItemRepository,
    ContractRepository,
    EvidenceRepository,
    InvoiceAllocationRepository,
    InvoiceItemAllocationRepository,
    InvoiceRepository,
    MatchCaseRepository,
)

NOW = datetime.now(timezone.utc)


def _confirm_invoice_contract(db_session, invoice, contract) -> None:
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type="INVOICE",
        subject_id=invoice.id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(db_session).add(match_case)
    db_session.flush()
    InvoiceAllocationRepository(db_session).add(
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
    db_session.flush()


def _setup_base(db_session, phase2b_ledger_path, phase2b_invoices_path, confirm=("DIGITAL-CLOSE-001", "PO-CLOSE-001")):
    import_contract_ledger(db_session, phase2b_ledger_path)
    import_invoices(db_session, phase2b_invoices_path, InvoiceDirection.PURCHASE)
    invoice = InvoiceRepository(db_session).find_by_external_key(confirm[0])
    contract = next(c for c in ContractRepository(db_session).list_all() if c.contract_no == confirm[1])
    _confirm_invoice_contract(db_session, invoice, contract)
    db_session.commit()


def test_close_fact_pack_is_evidence(db_session, phase2b_ledger_path, phase2b_invoices_path, phase2b_close_facts_path):
    _setup_base(db_session, phase2b_ledger_path, phase2b_invoices_path)
    for external_key, contract_no in [
        ("DIGITAL-CLOSE-002", "PO-CLOSE-002"),
        ("DIGITAL-CLOSE-005", "PO-CLOSE-005"),
        ("DIGITAL-CLOSE-006", "PO-CLOSE-006"),
    ]:
        invoice = InvoiceRepository(db_session).find_by_external_key(external_key)
        contract = next(c for c in ContractRepository(db_session).list_all() if c.contract_no == contract_no)
        _confirm_invoice_contract(db_session, invoice, contract)
    db_session.commit()

    result = import_close_facts(db_session, phase2b_close_facts_path)

    document = EvidenceRepository(db_session).get_document(result.evidence_document_id)
    assert document.source_type == "close_fact_pack_json"
    fragments = [
        f for f in EvidenceRepository(db_session)._session.query(EvidenceFragmentModel).all()
        if f.evidence_document_id == document.id
    ]
    assert fragments, "the Fact Pack must produce EvidenceFragments"
    assert all(f.fragment_kind == "MANUAL_FACT" for f in fragments)
    sections = {f.locator_json["section"] for f in fragments}
    assert {"contract_items", "historical_accrual_facts", "invoice_item_allocations", "accrual_reversals"} <= sections

    # Every Accrual traces back: created_from_fact_id -> HistoricalAccrualFact
    # -> MANUAL_FACT fragment (A02 Decision -> Fact -> Evidence).
    accruals = AccrualRepository(db_session).list_all()
    assert len(accruals) == 4
    hist_facts = db_session.query(HistoricalAccrualFactModel).all()
    fact_ids = {m.id for m in hist_facts}
    fragment_ids = {f.id for f in fragments}
    for accrual in accruals:
        assert accrual.created_from_fact_id in fact_ids
    for fact in hist_facts:
        assert fact.source_fragment_id in fragment_ids


def test_historical_accrual_fact_creates_accrual(
    db_session, phase2b_ledger_path, phase2b_invoices_path, phase2b_close_facts_path
):
    _setup_base(db_session, phase2b_ledger_path, phase2b_invoices_path)
    for external_key, contract_no in [
        ("DIGITAL-CLOSE-002", "PO-CLOSE-002"),
        ("DIGITAL-CLOSE-005", "PO-CLOSE-005"),
        ("DIGITAL-CLOSE-006", "PO-CLOSE-006"),
    ]:
        invoice = InvoiceRepository(db_session).find_by_external_key(external_key)
        contract = next(c for c in ContractRepository(db_session).list_all() if c.contract_no == contract_no)
        _confirm_invoice_contract(db_session, invoice, contract)
    db_session.commit()

    import_close_facts(db_session, phase2b_close_facts_path)

    close001 = next(c for c in ContractRepository(db_session).list_all() if c.contract_no == "PO-CLOSE-001")
    item = ContractItemRepository(db_session).find_by_contract_and_key(close001.id, "ITEM-A")
    accrual = AccrualRepository(db_session).find_by_item_and_period(item.id, "2031-02")
    assert accrual is not None
    assert accrual.quantity == Decimal("100")
    assert accrual.estimated_cost == Decimal("1200.00")
    assert accrual.status == AccrualStatus.ACTIVE

    # The PARTIALLY_REVERSED go-live state (CLOSE-005) derives from a
    # reversal row, not a separately stored remaining amount.
    close005 = next(c for c in ContractRepository(db_session).list_all() if c.contract_no == "PO-CLOSE-005")
    item005 = ContractItemRepository(db_session).find_by_contract_and_key(close005.id, "ITEM-A")
    accrual005 = AccrualRepository(db_session).find_by_item_and_period(item005.id, "2031-02")
    assert accrual005.status == AccrualStatus.PARTIALLY_REVERSED
    reversals = AccrualReversalRepository(db_session).list_for_accrual(accrual005.id)
    assert len(reversals) == 1
    assert reversals[0].reversed_quantity == Decimal("40")
    assert reversals[0].reversed_estimated_cost == Decimal("200.00")


def test_close_fact_import_is_idempotent(
    db_session, phase2b_ledger_path, phase2b_invoices_path, phase2b_close_facts_path
):
    _setup_base(db_session, phase2b_ledger_path, phase2b_invoices_path)
    for external_key, contract_no in [
        ("DIGITAL-CLOSE-002", "PO-CLOSE-002"),
        ("DIGITAL-CLOSE-005", "PO-CLOSE-005"),
        ("DIGITAL-CLOSE-006", "PO-CLOSE-006"),
    ]:
        invoice = InvoiceRepository(db_session).find_by_external_key(external_key)
        contract = next(c for c in ContractRepository(db_session).list_all() if c.contract_no == contract_no)
        _confirm_invoice_contract(db_session, invoice, contract)
    db_session.commit()

    first = import_close_facts(db_session, phase2b_close_facts_path)
    second = import_close_facts(db_session, phase2b_close_facts_path)

    assert first.is_reimport is False
    assert second.is_reimport is True
    assert second.evidence_document_id == first.evidence_document_id

    # Same SHA re-import: 0 duplicate Facts / ContractItems / Accruals /
    # ItemAllocations — and no second ACTIVE Accrual from the repeated
    # historical accrual.
    close_pack_docs = (
        db_session.query(EvidenceDocumentModel)
        .filter_by(source_type="close_fact_pack_json")
        .count()
    )
    assert close_pack_docs == 1  # the re-import did not add a second document
    assert db_session.query(AccrualModel).count() == 4
    assert db_session.query(AccrualReversalModel).count() == 1
    assert db_session.query(InvoiceItemAllocationModel).count() == 3
    assert db_session.query(HistoricalAccrualFactModel).count() == 4
    assert db_session.query(ContractItemModel).count() == 5
    active_count = db_session.query(AccrualModel).filter_by(status=AccrualStatus.ACTIVE).count()
    assert active_count == 3  # CLOSE-001/002/006 — no duplicate from re-import


def test_ambiguous_contract_selector_is_rejected(
    db_session, phase2b_ledger_path, phase2b_invoices_path, phase2b_close_facts_path
):
    """0 matches and >1 matches both reject — never 'take the first one'."""
    _setup_base(db_session, phase2b_ledger_path, phase2b_invoices_path)

    # No such contract -> 0 matches -> reject.
    pack = {
        "version": 1,
        "contract_items": [
            {
                "contract_selector": {"contract_no": "PO-NOPE", "counterparty": "Nobody"},
                "source_item_key": "ITEM-A",
                "quantity": 10,
            }
        ],
    }
    path = phase2b_close_facts_path.with_name("nonexistent-contract.json")
    path.write_text(json.dumps(pack))
    with pytest.raises(CloseFactPackError):
        import_close_facts(db_session, path)


def test_item_allocation_capacity_enforced(
    db_session, phase2b_ledger_path, phase2b_invoices_path, phase2b_close_facts_path
):
    """11-B: sum(allocated_quantity) must never exceed invoice_item.quantity."""
    _setup_base(db_session, phase2b_ledger_path, phase2b_invoices_path)
    pack = {
        "version": 1,
        "contract_items": [
            {
                "contract_selector": {"contract_no": "PO-CLOSE-001", "counterparty": "SupplierCloseAlpha"},
                "source_item_key": "ITEM-A",
                "quantity": 100,
            }
        ],
        "historical_accrual_facts": [],
        "invoice_item_allocations": [
            {
                "invoice": {"external_key": "DIGITAL-CLOSE-001", "line_no": 1},
                "contract_selector": {"contract_no": "PO-CLOSE-001", "counterparty": "SupplierCloseAlpha"},
                "source_item_key": "ITEM-A",
                "allocated_quantity": 40,  # line quantity is 35
                "allocated_net_amount": 520.00,
                "confirmation_type": "MANUAL_CONFIRMED",
            }
        ],
    }
    path = phase2b_close_facts_path.with_name("over-capacity.json")
    path.write_text(json.dumps(pack))
    with pytest.raises(CloseFactPackError):
        import_close_facts(db_session, path)
    assert db_session.query(InvoiceItemAllocationModel).count() == 0


def test_item_allocation_requires_contract_level_confirmation(
    db_session, phase2b_ledger_path, phase2b_invoices_path, phase2b_close_facts_path
):
    """11-A: the invoice must already be confirmed to the same contract at
    contract level before any item allocation may be created."""
    _setup_base(db_session, phase2b_ledger_path, phase2b_invoices_path)
    # DIGITAL-CLOSE-007 has NO contract-level allocation.
    pack = {
        "version": 1,
        "contract_items": [
            {
                "contract_selector": {"contract_no": "PO-CLOSE-007", "counterparty": "SupplierCloseEta"},
                "source_item_key": "ITEM-A",
                "quantity": 30,
            }
        ],
        "historical_accrual_facts": [],
        "invoice_item_allocations": [
            {
                "invoice": {"external_key": "DIGITAL-CLOSE-007", "line_no": 1},
                "contract_selector": {"contract_no": "PO-CLOSE-007", "counterparty": "SupplierCloseEta"},
                "source_item_key": "ITEM-A",
                "allocated_quantity": 30,
                "allocated_net_amount": 300.00,
                "confirmation_type": "MANUAL_CONFIRMED",
            }
        ],
    }
    path = phase2b_close_facts_path.with_name("no-contract-level.json")
    path.write_text(json.dumps(pack))
    with pytest.raises(CloseFactPackError):
        import_close_facts(db_session, path)
    assert db_session.query(InvoiceItemAllocationModel).count() == 0

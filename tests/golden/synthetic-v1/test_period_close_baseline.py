"""Golden full-pipeline Phase 2B baseline — the public counterpart of the
private P2B_* scenarios. Exercises S2B-01..S2B-08 plus the preview
no-write invariants. See docs/PHASE2B-ACCEPTANCE.md.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import sqlalchemy

from bel.application.import_close_facts import import_close_facts
from bel.application.import_contract_ledger import import_contract_ledger
from bel.application.import_invoices import import_invoices
from bel.application.period_close import build_period_close_preview
from bel.domain.accrual import get_accrual_balance
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
    BusinessEventModel,
    InvoiceAllocationModel,
    InvoiceItemAllocationModel,
)
from bel.infrastructure.persistence.repositories import (
    AccrualReversalRepository,
    AccrualRepository,
    ContractItemRepository,
    ContractRepository,
    InvoiceAllocationRepository,
    InvoiceRepository,
    MatchCaseRepository,
)
from fixtures.synthetic.phase2b_close import CLOSE_PERIOD

BASELINE_PATH = Path(__file__).parent / "period-close-baseline.json"


def _confirm_invoice_contract(db_session, invoice, contract) -> None:
    """Phase 2A M001 output, constructed directly: the S2B invoice amounts
    intentionally differ from contract gross (partial receipt), so the
    exact-amount M001 rule would not fire here. See docs/PHASE2B-DECISIONS.md."""
    now = datetime.now(timezone.utc)
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type="INVOICE",
        subject_id=invoice.id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=now,
        resolved_at=now,
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
            created_at=now,
        )
    )


def _no_by_id(db_session):
    return {c.id: c.contract_no for c in ContractRepository(db_session).list_all()}


def _item_key_by_id(db_session):
    return {i.id: i.source_item_key for i in ContractItemRepository(db_session).list_all()}


def test_phase2b_period_close_matches_baseline(
    db_session, phase2b_ledger_path, phase2b_invoices_path, phase2b_close_facts_path
):
    baseline = json.loads(BASELINE_PATH.read_text())

    import_contract_ledger(db_session, phase2b_ledger_path)
    import_invoices(db_session, phase2b_invoices_path, InvoiceDirection.PURCHASE)

    # Contract-level confirmations must exist BEFORE the fact pack's item
    # allocations (section 11-A).
    for external_key, contract_no in [
        ("DIGITAL-CLOSE-001", "PO-CLOSE-001"),
        ("DIGITAL-CLOSE-002", "PO-CLOSE-002"),
        ("DIGITAL-CLOSE-005", "PO-CLOSE-005"),
        ("DIGITAL-CLOSE-006", "PO-CLOSE-006"),
    ]:
        invoice = InvoiceRepository(db_session).find_by_external_key(external_key)
        contract = next(c for c in ContractRepository(db_session).list_all() if c.contract_no == contract_no)
        _confirm_invoice_contract(db_session, invoice, contract)
    db_session.commit()

    import_close_facts(db_session, phase2b_close_facts_path)

    no_by_id = _no_by_id(db_session)
    item_key_by_id = _item_key_by_id(db_session)

    # ---- Preview is a pure function: nothing may change. ----
    def _counts():
        return {
            "accruals": db_session.query(AccrualModel).count(),
            "accrual_reversals": db_session.query(AccrualReversalModel).count(),
            "invoice_allocations": db_session.query(InvoiceAllocationModel).count(),
            "invoice_item_allocations": db_session.query(InvoiceItemAllocationModel).count(),
            "business_events": db_session.query(BusinessEventModel).count(),
        }

    before = _counts()
    preview = build_period_close_preview(db_session, CLOSE_PERIOD)
    after = _counts()
    assert before == after == {
        "accruals": baseline["no_write_invariants"]["accruals"],
        "accrual_reversals": baseline["no_write_invariants"]["accrual_reversals"],
        "invoice_allocations": baseline["no_write_invariants"]["invoice_allocations"],
        "invoice_item_allocations": baseline["no_write_invariants"]["invoice_item_allocations"],
        "business_events": 2,
    }

    # No Voucher/AccountingEntry/TaxEntry tables exist at all.
    inspector = sqlalchemy.inspect(db_session.get_bind())
    table_names = set(inspector.get_table_names())
    assert {"vouchers", "accounting_entries", "tax_entries"}.isdisjoint(table_names)

    # ---- S2B-01 / S2B-02: prior accrual reversals (R001+R006). ----
    reversals = {no_by_id[r.contract_id]: r for r in preview.prior_accrual_reversals}
    for contract_no, expected in baseline["prior_accrual_reversals"].items():
        r = reversals[contract_no]
        assert item_key_by_id[r.contract_item_id] == expected["item_key"]
        assert r.reversal_quantity == Decimal(expected["reversal_quantity"])
        assert r.reversal_estimated_cost == Decimal(expected["reversal_estimated_cost"])
        assert r.projected_remaining_quantity == Decimal(expected["projected_remaining_quantity"])
        assert r.projected_remaining_cost == Decimal(expected["projected_remaining_cost"])
        assert r.projected_status == expected["projected_status"]
    assert len(reversals) == 2

    # ---- S2B-03: prior-period reversal cross-check. ----
    total_reversal_cost = sum(r.reversal_estimated_cost for r in preview.prior_accrual_reversals)
    assert total_reversal_cost == Decimal(baseline["prior_period_reversal_cross_check"]["expected"])

    # ---- R005: accrual actual differences. ----
    differences = {no_by_id[d.contract_id]: d for d in preview.accrual_actual_differences}
    for contract_no, expected in baseline["accrual_actual_differences"].items():
        d = differences[contract_no]
        assert d.actual_net_cost == Decimal(expected["actual_net_cost"])
        assert d.reversed_estimated_cost == Decimal(expected["reversed_estimated_cost"])
        assert d.difference == Decimal(expected["difference"])

    # ---- S2B-04: new item-level accrual (R002). ----
    requirements = {no_by_id[a.contract_id]: a for a in preview.new_accrual_requirements}
    for contract_no, expected in baseline["new_accrual_requirements"].items():
        a = requirements[contract_no]
        assert a.level == expected["level"]
        assert item_key_by_id[a.contract_item_id] == expected["item_key"]
        assert a.quantity == Decimal(expected["quantity"])
        assert a.estimated_cost == Decimal(expected["estimated_cost"])

    # ---- S2B-05 + S2B-08-run1: contract-level candidates (R007). ----
    candidates = {no_by_id[c.contract_id]: c for c in preview.contract_level_candidates}
    for contract_no, expected in baseline["contract_level_candidates"].items():
        c = candidates[contract_no]
        assert c.level == "CONTRACT"
        assert c.estimated_cost == Decimal(expected["estimated_cost"])
        assert c.blocking_reason == expected["blocking_reason"]

    # ---- S2B-06: duplicate guard. ----
    close005 = next(c for c in ContractRepository(db_session).list_all() if c.contract_no == "PO-CLOSE-005")
    assert close005.id not in requirements
    assert close005.id not in reversals
    accrual = next(
        a
        for a in AccrualRepository(db_session).list_all()
        if a.contract_item_id in {i.id for i in ContractItemRepository(db_session).list_for_contract(close005.id)}
    )
    reversals_of = AccrualReversalRepository(db_session).list_for_accrual(accrual.id)
    remaining_qty, remaining_cost, _, _ = get_accrual_balance(accrual, reversals_of)
    assert remaining_qty == Decimal(baseline["duplicate_guard"]["PO-CLOSE-005"]["remaining_quantity"])
    assert remaining_cost == Decimal(baseline["duplicate_guard"]["PO-CLOSE-005"]["remaining_estimated_cost"])
    assert accrual.status == baseline["duplicate_guard"]["PO-CLOSE-005"]["accrual_status"]

    # ---- S2B-07: item-match blocker, no guessed reversal amount. ----
    blockers = {no_by_id[b.contract_id]: b for b in preview.blockers}
    assert blockers["PO-CLOSE-006"].blocker_type == baseline["item_match_blocker"]["PO-CLOSE-006"]["blocker"]
    assert "PO-CLOSE-006" not in reversals

    # ---- S2B-08 run 1: candidate present. ----
    assert "PO-CLOSE-007" in candidates

    # ---- MISSING_ACCRUAL_BASIS diagnostic blocker (not R011). ----
    assert blockers["PO-CLOSE-008"].blocker_type == baseline["missing_basis_blocker"]["PO-CLOSE-008"]["blocker"]

    # ---- summary. ----
    assert preview.summary == baseline["summary"]


def test_fresh_recompute_candidate_disappears(
    db_session, phase2b_ledger_path, phase2b_invoices_path, phase2b_recompute_facts_path
):
    """S2B-08: a contract-level candidate that exists while the contract
    has no invoice by period_end must vanish once a confirmed invoice
    arrives — without any stale Decision row ever being deleted, because
    Period Close never persists Decisions (stateless recomputation, NOT
    R015)."""
    import_contract_ledger(db_session, phase2b_ledger_path)
    import_invoices(db_session, phase2b_invoices_path, InvoiceDirection.PURCHASE)
    import_close_facts(db_session, phase2b_recompute_facts_path)

    no_by_id = _no_by_id(db_session)

    def _candidate_contract_nos():
        preview = build_period_close_preview(db_session, CLOSE_PERIOD)
        return {no_by_id[c.contract_id] for c in preview.contract_level_candidates}

    run1 = _candidate_contract_nos()
    assert "PO-CLOSE-007" in run1

    invoice = InvoiceRepository(db_session).find_by_external_key("DIGITAL-CLOSE-007")
    contract = next(c for c in ContractRepository(db_session).list_all() if c.contract_no == "PO-CLOSE-007")
    _confirm_invoice_contract(db_session, invoice, contract)
    db_session.commit()

    run2 = _candidate_contract_nos()
    assert "PO-CLOSE-007" not in run2

    # No stale Decision rows exist — nothing was ever persisted, so there
    # is nothing to delete.
    assert db_session.query(AccrualModel).count() == 0
    assert db_session.query(AccrualReversalModel).count() == 0
    assert db_session.query(InvoiceItemAllocationModel).count() == 0

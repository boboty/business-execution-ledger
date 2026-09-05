"""Golden full-pipeline matching baseline (import all three sources,
then run M001) against the synthetic fixtures — the public counterpart
of the private acceptance suite's P2A_MATCHING scenario. See
docs/PRIVATE-DATA-POLICY.md.
"""

import json
from decimal import Decimal
from pathlib import Path

from bel.application.import_bank import import_bank_statement
from bel.application.import_contract_ledger import import_contract_ledger
from bel.application.import_invoices import import_invoices
from bel.application.matching import match_invoices, match_payments
from bel.domain.invoice import InvoiceDirection
from bel.domain.matching import AllocationMatchMethod
from bel.infrastructure.persistence.models import (
    InvoiceAllocationModel,
    MatchCaseModel,
    PaymentAllocationModel,
)

BASELINE_PATH = Path(__file__).parent / "matching-baseline.json"


def test_synthetic_matching_run_matches_baseline(
    db_session, synthetic_ledger_path, synthetic_invoices_path, synthetic_bank_pdf_path
):
    baseline = json.loads(BASELINE_PATH.read_text())

    import_contract_ledger(db_session, synthetic_ledger_path)
    import_invoices(db_session, synthetic_invoices_path, InvoiceDirection.PURCHASE)
    import_bank_statement(db_session, synthetic_bank_pdf_path, "cmb")

    inv_summary = match_invoices(db_session)
    pay_summary = match_payments(db_session)

    inv_baseline = baseline["invoice_matching"]
    # Explicit, non-derived assertions — see docs/PHASE2A-DECISIONS.md.
    assert inv_summary.eligible_total == inv_baseline["eligible_total"]
    assert inv_summary.auto_confirmed == inv_baseline["auto_confirmed"]
    assert inv_summary.human_confirmation_required == inv_baseline["human_confirmation_required"]
    assert inv_summary.unmatched == inv_baseline["unmatched_within_eligible"]
    assert inv_summary.capacity_exceeded == inv_baseline["capacity_exceeded"]

    pay_baseline = baseline["payment_matching"]
    assert pay_summary.eligible_total == pay_baseline["eligible_total"]
    assert pay_summary.auto_confirmed == pay_baseline["auto_confirmed"]
    assert pay_summary.human_confirmation_required == pay_baseline["human_confirmation_required"]
    assert pay_summary.unmatched == pay_baseline["unmatched_within_eligible"]
    assert pay_summary.capacity_exceeded == pay_baseline["capacity_exceeded"]

    # Out-of-scope subjects must never become a MatchCase at all — see
    # spec section 14's explicit ContractNotFound-noise warning.
    invoice_match_case_count = db_session.query(MatchCaseModel).filter_by(subject_type="INVOICE").count()
    payment_match_case_count = db_session.query(MatchCaseModel).filter_by(subject_type="PAYMENT").count()
    assert invoice_match_case_count == inv_baseline["eligible_total"]
    assert payment_match_case_count == pay_baseline["eligible_total"]

    # The independently synthetic complete 2x2 cohorts now exercise the
    # equivalent-permutation convention, never the chronological method.
    equivalent_amounts = {Decimal(a) for a in baseline["equivalent_canonical_amount_clusters"]}
    invoice_methods = {
        a.match_method for a in db_session.query(InvoiceAllocationModel).all()
        if a.allocated_gross_amount in equivalent_amounts
    }
    expected_method = AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_EQUIVALENT_CANONICAL
    assert invoice_methods == {expected_method}
    # This older synthetic PDF intentionally lacks a complete Payment
    # business identity, so its analogous cohort remains HCR.
    assert not {
        a.match_method for a in db_session.query(PaymentAllocationModel).all()
        if a.allocated_amount in equivalent_amounts
    }

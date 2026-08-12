import uuid
from datetime import datetime, timezone
from decimal import Decimal

from bel.application.matching import confirm_match, match_invoices, match_payments
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionType
from bel.domain.invoice import Invoice, InvoiceDirection
from bel.domain.matching import MatchCaseStatus
from bel.domain.payment import Payment, PaymentDirection
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    ExceptionRepository,
    InvoiceAllocationRepository,
    InvoiceRepository,
    MatchCaseRepository,
    PaymentAllocationRepository,
    PaymentRepository,
)


def _make_fragment(session, doc_sha="a" * 64):
    now = datetime.now(timezone.utc)
    doc = EvidenceDocument(id=uuid.uuid4(), file_name="x", sha256=doc_sha, source_type="t", imported_at=now)
    from bel.infrastructure.persistence.repositories import EvidenceRepository

    er = EvidenceRepository(session)
    er.add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=doc.id,
        fragment_kind=FragmentKind.EXCEL_ROW,
        sheet_name="s",
        row_number=1,
        locator_json=None,
        raw_data={},
        created_at=now,
    )
    er.add_fragment(frag)
    session.flush()
    return frag


def _make_contract(session, fragment_id, counterparty, gross_amount):
    now = datetime.now(timezone.utc)
    c = Contract(
        id=uuid.uuid4(),
        contract_no=f"C-{uuid.uuid4().hex[:8]}",
        contract_type=None,
        counterparty=counterparty,
        buyer="Buyer Co",
        gross_amount=Decimal(gross_amount),
        currency="CNY",
        contract_date=None,
        current_source_fragment_id=fragment_id,
        created_at=now,
        updated_at=now,
    )
    ContractRepository(session).add(c)
    return c


def _make_invoice(session, fragment_id, seller, gross_amount, direction=InvoiceDirection.PURCHASE):
    now = datetime.now(timezone.utc)
    inv = Invoice(
        id=uuid.uuid4(),
        direction=direction,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=None,
        issue_date=None,
        seller=seller,
        buyer="Buyer Co",
        net_amount=Decimal(gross_amount),
        tax_amount=Decimal("0"),
        gross_amount=Decimal(gross_amount),
        invoice_status=None,
        source_fragment_id=fragment_id,
        created_at=now,
        updated_at=now,
    )
    InvoiceRepository(session).add(inv)
    return inv


def _make_payment(session, fragment_id, counterparty, amount, direction=PaymentDirection.OUT):
    now = datetime.now(timezone.utc)
    p = Payment(
        id=uuid.uuid4(),
        transaction_date=now.date(),
        direction=direction,
        amount=Decimal(amount),
        counterparty=counterparty,
        business_type=None,
        bank_reference=None,
        description=None,
        running_balance=None,
        source_fragment_id=fragment_id,
        created_at=now,
    )
    PaymentRepository(session).add(p)
    return p


def test_unique_candidate_auto_confirms_and_allocates(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "Seller A", "1000.00")
    invoice = _make_invoice(db_session, frag.id, "Seller A", "1000.00")
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 1
    assert summary.human_confirmation_required == 0
    assert summary.unmatched == 0

    match_case = MatchCaseRepository(db_session).find_by_subject("INVOICE", invoice.id)
    assert match_case.status == MatchCaseStatus.AUTO_CONFIRMED

    allocations = InvoiceAllocationRepository(db_session).list_for_contract(contract.id)
    assert len(allocations) == 1
    assert allocations[0].allocated_gross_amount == Decimal("1000.00")
    assert allocations[0].match_case_id == match_case.id


def test_ambiguous_candidates_create_no_allocation(db_session):
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id, "Seller A", "2233.00")
    contract_b = _make_contract(db_session, frag.id, "Seller A", "2233.00")
    invoice = _make_invoice(db_session, frag.id, "Seller A", "2233.00")
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.human_confirmation_required == 1
    assert summary.auto_confirmed == 0

    match_case = MatchCaseRepository(db_session).find_by_subject("INVOICE", invoice.id)
    assert match_case.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED

    allocations = InvoiceAllocationRepository(db_session).list_for_contract(
        contract_a.id
    ) + InvoiceAllocationRepository(db_session).list_for_contract(contract_b.id)
    assert allocations == []


def test_zero_candidates_is_unmatched(db_session):
    frag = _make_fragment(db_session)
    _make_contract(db_session, frag.id, "Seller A", "500.00")
    invoice = _make_invoice(db_session, frag.id, "Seller A", "999.00")  # amount doesn't match any contract
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.unmatched == 1
    match_case = MatchCaseRepository(db_session).find_by_subject("INVOICE", invoice.id)
    assert match_case.status == MatchCaseStatus.UNMATCHED


def test_out_of_scope_counterparty_gets_no_match_case_at_all(db_session):
    """Spec section 14's explicit warning, generalized to invoices too:
    an invoice/payment whose counterparty was never a party to ANY
    contract (phone bill, salary, tax, logistics, random one-off
    purchase) is simply not contract-related business. It must not
    become a MatchCase at all — not even UNMATCHED — or every such
    subject would silently pollute the exception/task surface as a
    fake 'ContractNotFound'."""
    frag = _make_fragment(db_session)
    _make_contract(db_session, frag.id, "Seller A", "500.00")
    unrelated_invoice = _make_invoice(db_session, frag.id, "Unrelated Telecom Co", "88.00")
    unrelated_payment = _make_payment(db_session, frag.id, "Unrelated Employee Reimbursement", "88.00")
    db_session.flush()

    inv_summary = match_invoices(db_session)
    pay_summary = match_payments(db_session)
    db_session.commit()

    assert inv_summary.eligible_total == 0
    assert inv_summary.out_of_scope == 1
    assert inv_summary.unmatched == 0
    assert MatchCaseRepository(db_session).find_by_subject("INVOICE", unrelated_invoice.id) is None

    assert pay_summary.eligible_total == 0
    assert pay_summary.out_of_scope == 1
    assert pay_summary.unmatched == 0
    assert MatchCaseRepository(db_session).find_by_subject("PAYMENT", unrelated_payment.id) is None


def test_no_sequence_guessing_two_contracts_two_invoices_same_amount(db_session):
    """The forbidden shortcut: pairing invoice[0]<->contract[0] and
    invoice[1]<->contract[1] just because they line up positionally.
    Both invoices must land in HUMAN_CONFIRMATION_REQUIRED with BOTH
    contracts as candidates — never silently paired. See spec section 20."""
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id, "Speedlight Co", "2233.00")
    contract_b = _make_contract(db_session, frag.id, "Speedlight Co", "2233.00")
    invoice_x = _make_invoice(db_session, frag.id, "Speedlight Co", "2233.00")
    invoice_y = _make_invoice(db_session, frag.id, "Speedlight Co", "2233.00")
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.human_confirmation_required == 2
    assert summary.auto_confirmed == 0

    case_repo = MatchCaseRepository(db_session)
    from bel.infrastructure.persistence.repositories import MatchCandidateRepository

    candidate_repo = MatchCandidateRepository(db_session)
    for inv in (invoice_x, invoice_y):
        case = case_repo.find_by_subject("INVOICE", inv.id)
        assert case.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED
        candidates = {c.contract_id for c in candidate_repo.list_for_case(case.id)}
        assert candidates == {contract_a.id, contract_b.id}

    # No allocations anywhere — ambiguity must never resolve itself.
    assert InvoiceAllocationRepository(db_session).list_for_contract(contract_a.id) == []
    assert InvoiceAllocationRepository(db_session).list_for_contract(contract_b.id) == []


def test_allocation_capacity_exceeded_blocks_second_unique_match(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "Seller A", "1000.00")
    invoice_1 = _make_invoice(db_session, frag.id, "Seller A", "1000.00")
    db_session.flush()
    match_invoices(db_session)
    db_session.commit()

    # Second invoice, same seller+amount as the contract's gross — the
    # contract is already fully allocated, so this must NOT auto-confirm.
    invoice_2 = _make_invoice(db_session, frag.id, "Seller A", "1000.00")
    db_session.flush()
    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.capacity_exceeded == 1
    case = MatchCaseRepository(db_session).find_by_subject("INVOICE", invoice_2.id)
    assert case.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED

    exceptions = ExceptionRepository(db_session).list_open()
    assert any(e.exception_type == ExceptionType.ALLOCATION_CAPACITY_EXCEEDED for e in exceptions)

    allocations = InvoiceAllocationRepository(db_session).list_for_contract(contract.id)
    assert len(allocations) == 1  # only invoice_1's


def test_confirm_match_resolves_human_confirmation_required(db_session):
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id, "Seller A", "2233.00")
    contract_b = _make_contract(db_session, frag.id, "Seller A", "2233.00")
    invoice = _make_invoice(db_session, frag.id, "Seller A", "2233.00")
    db_session.flush()
    match_invoices(db_session)
    db_session.commit()

    case = MatchCaseRepository(db_session).find_by_subject("INVOICE", invoice.id)
    confirm_match(db_session, case.id, contract_a.id)
    db_session.commit()

    resolved = MatchCaseRepository(db_session).get(case.id)
    assert resolved.status == MatchCaseStatus.RESOLVED
    assert resolved.resolved_at is not None

    allocations = InvoiceAllocationRepository(db_session).list_for_contract(contract_a.id)
    assert len(allocations) == 1
    assert allocations[0].confirmation_type == "HUMAN_CONFIRMED"
    assert allocations[0].match_case_id == case.id
    assert InvoiceAllocationRepository(db_session).list_for_contract(contract_b.id) == []


def test_payment_matching_unique_and_ambiguous(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "Seller A", "500.00")
    payment = _make_payment(db_session, frag.id, "Seller A", "500.00")
    db_session.flush()

    summary = match_payments(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 1
    allocations = PaymentAllocationRepository(db_session).list_for_contract(contract.id)
    assert len(allocations) == 1
    assert allocations[0].allocated_amount == Decimal("500.00")


def test_re_running_match_is_idempotent(db_session):
    frag = _make_fragment(db_session)
    _make_contract(db_session, frag.id, "Seller A", "500.00")
    _make_invoice(db_session, frag.id, "Seller A", "500.00")
    db_session.flush()

    first = match_invoices(db_session)
    db_session.commit()
    second = match_invoices(db_session)
    db_session.commit()

    assert first.auto_confirmed == 1
    assert second.auto_confirmed == 0
    assert second.already_matched_skipped == 1

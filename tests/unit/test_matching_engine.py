import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from bel.application.matching import confirm_match, match_invoices, match_payments
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionType
from bel.domain.invoice import Invoice, InvoiceDirection
from bel.domain.matching import AllocationMatchMethod, MatchCaseStatus
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


def _make_contract(
    session, fragment_id, counterparty, gross_amount, *, contract_no=None, contract_date=None, contract_id=None
):
    now = datetime.now(timezone.utc)
    c = Contract(
        id=contract_id or uuid.uuid4(),
        contract_no=contract_no or f"C-{uuid.uuid4().hex[:8]}",
        contract_type=None,
        counterparty=counterparty,
        buyer="Buyer Co",
        gross_amount=Decimal(gross_amount),
        currency="CNY",
        contract_date=contract_date,
        current_source_fragment_id=fragment_id,
        created_at=now,
        updated_at=now,
    )
    ContractRepository(session).add(c)
    return c


def _make_invoice(
    session, fragment_id, seller, gross_amount, *, direction=InvoiceDirection.PURCHASE, issue_date=None,
    external_invoice_key=None, digital_invoice_no=None, invoice_no=None, invoice_id=None,
):
    now = datetime.now(timezone.utc)
    inv = Invoice(
        id=invoice_id or uuid.uuid4(),
        direction=direction,
        invoice_type=None,
        invoice_no=invoice_no,
        digital_invoice_no=digital_invoice_no,
        external_invoice_key=external_invoice_key,
        issue_date=issue_date,
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


def _make_payment(
    session, fragment_id, counterparty, amount, *, direction=PaymentDirection.OUT, transaction_date=None,
    bank_reference=None,
):
    now = datetime.now(timezone.utc)
    p = Payment(
        id=uuid.uuid4(),
        transaction_date=transaction_date if transaction_date is not None else now.date(),
        direction=direction,
        amount=Decimal(amount),
        counterparty=counterparty,
        business_type=None,
        bank_reference=bank_reference,
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


def test_equivalent_candidates_allocate_chronologically(db_session):
    """Two exactly-equivalent Contracts (same seller + same amount,
    different dates) and ONE subject: the confirmed chronological rule
    allocates to the EARLIEST candidate with capacity — AUTO_CONFIRMED,
    method EXACT_COUNTERPARTY_AMOUNT_CHRONOLOGICAL, no HCR."""
    frag = _make_fragment(db_session)
    k1 = _make_contract(db_session, frag.id, "Seller A", "2233.00", contract_no="K1", contract_date=date(2026, 8, 1))
    k2 = _make_contract(db_session, frag.id, "Seller A", "2233.00", contract_no="K2", contract_date=date(2026, 8, 5))
    invoice = _make_invoice(db_session, frag.id, "Seller A", "2233.00", issue_date=date(2026, 8, 10))
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 1
    assert summary.human_confirmation_required == 0

    match_case = MatchCaseRepository(db_session).find_by_subject("INVOICE", invoice.id)
    assert match_case.status == MatchCaseStatus.AUTO_CONFIRMED

    a_alloc = InvoiceAllocationRepository(db_session).list_for_contract(k1.id)
    assert len(a_alloc) == 1
    assert a_alloc[0].match_method == AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_CHRONOLOGICAL
    assert InvoiceAllocationRepository(db_session).list_for_contract(k2.id) == []


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


def test_two_contracts_two_invoices_chronological_allocation(db_session):
    """Two exactly-equivalent Contracts and two matching Invoices, inserted
    in the OPPOSITE business order: the confirmed chronological rule pairs
    them deterministically by business date (issue_date ASC), NOT by
    insertion/row order — both AUTO_CONFIRMED, no HCR."""
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id, "Speedlight Co", "2233.00", contract_no="KA",
                                contract_date=date(2026, 8, 1))
    contract_b = _make_contract(db_session, frag.id, "Speedlight Co", "2233.00", contract_no="KB",
                                contract_date=date(2026, 8, 5))
    # Inserted later business-date first on purpose.
    invoice_x = _make_invoice(db_session, frag.id, "Speedlight Co", "2233.00", issue_date=date(2026, 8, 20),
                              external_invoice_key="IX")
    invoice_y = _make_invoice(db_session, frag.id, "Speedlight Co", "2233.00", issue_date=date(2026, 8, 10),
                              external_invoice_key="IY")
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 2
    assert summary.human_confirmation_required == 0

    a_alloc = InvoiceAllocationRepository(db_session).list_for_contract(contract_a.id)
    b_alloc = InvoiceAllocationRepository(db_session).list_for_contract(contract_b.id)
    assert [a.invoice_id for a in a_alloc] == [invoice_y.id]  # 8/10 invoice -> earliest contract
    assert [b.invoice_id for b in b_alloc] == [invoice_x.id]  # 8/20 invoice -> next contract
    for alloc in a_alloc + b_alloc:
        assert alloc.match_method == AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_CHRONOLOGICAL


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


def test_confirm_match_resolves_capacity_blocked_human_confirmation_required(db_session):
    """A capacity-blocked subject becomes HUMAN_CONFIRMATION_REQUIRED (the
    only remaining automatic outcome for an exact-amount world). A human may
    still confirm it onto a DIFFERENT contract that has capacity — the same
    guard applies and never over-allocates."""
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id, "Seller A", "1000.00")  # exact candidate, consumed below
    contract_b = _make_contract(db_session, frag.id, "Seller A", "1500.00")  # not an exact candidate, has capacity
    invoice_1 = _make_invoice(db_session, frag.id, "Seller A", "1000.00")
    db_session.flush()
    match_invoices(db_session)
    db_session.commit()

    invoice_2 = _make_invoice(db_session, frag.id, "Seller A", "1000.00")
    db_session.flush()
    summary = match_invoices(db_session)
    db_session.commit()
    assert summary.capacity_exceeded == 1

    case = MatchCaseRepository(db_session).find_by_subject("INVOICE", invoice_2.id)
    assert case.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED

    confirm_match(db_session, case.id, contract_b.id)
    db_session.commit()

    resolved = MatchCaseRepository(db_session).get(case.id)
    assert resolved.status == MatchCaseStatus.RESOLVED
    assert resolved.resolved_at is not None

    b_alloc = InvoiceAllocationRepository(db_session).list_for_contract(contract_b.id)
    assert len(b_alloc) == 1
    assert b_alloc[0].confirmation_type == "HUMAN_CONFIRMED"
    assert b_alloc[0].match_case_id == case.id

    # Re-running after a human decision must not reassign or duplicate.
    again = match_invoices(db_session)
    db_session.commit()
    assert again.auto_confirmed == 0
    assert again.already_matched_skipped == 2  # invoice_1 (auto) + invoice_2 (resolved HCR)
    assert len(InvoiceAllocationRepository(db_session).list_for_contract(contract_b.id)) == 1
    assert len(InvoiceAllocationRepository(db_session).list_for_contract(contract_a.id)) == 1  # invoice_1 only


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
    # A genuinely unique candidate is UNIQUE, never labelled chronological.
    assert allocations[0].match_method == AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE


def test_three_contracts_three_payments_chronological(db_session):
    """The confirmed business example: 8/01, 8/05, 8/24 Contracts (3000
    each) and 8/20, 8/20, 8/28 Payments (3000 each) allocate
    deterministically 1->1, 2->2, 3->3 with NO human confirmation."""
    frag = _make_fragment(db_session)
    k1 = _make_contract(db_session, frag.id, "PM Co", "3000.00", contract_no="PM1", contract_date=date(2026, 8, 1))
    k2 = _make_contract(db_session, frag.id, "PM Co", "3000.00", contract_no="PM2", contract_date=date(2026, 8, 5))
    k3 = _make_contract(db_session, frag.id, "PM Co", "3000.00", contract_no="PM3", contract_date=date(2026, 8, 24))
    p1 = _make_payment(db_session, frag.id, "PM Co", "3000.00", transaction_date=date(2026, 8, 20), bank_reference="P-1")
    p2 = _make_payment(db_session, frag.id, "PM Co", "3000.00", transaction_date=date(2026, 8, 20), bank_reference="P-2")
    p3 = _make_payment(db_session, frag.id, "PM Co", "3000.00", transaction_date=date(2026, 8, 28), bank_reference="P-3")
    db_session.flush()

    summary = match_payments(db_session)
    db_session.commit()
    assert summary.auto_confirmed == 3
    assert summary.human_confirmation_required == 0
    assert summary.capacity_exceeded == 0

    payment_repo = PaymentRepository(db_session)

    def payment_refs(contract):
        return sorted(
            payment_repo.get(a.payment_id).bank_reference
            for a in PaymentAllocationRepository(db_session).list_for_contract(contract.id)
        )

    assert payment_refs(k1) == ["P-1"]
    assert payment_refs(k2) == ["P-2"]
    assert payment_refs(k3) == ["P-3"]
    for contract in (k1, k2, k3):
        for a in PaymentAllocationRepository(db_session).list_for_contract(contract.id):
            assert a.match_method == AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_CHRONOLOGICAL

    # Idempotency: a second pass changes nothing and reassigns nothing.
    second = match_payments(db_session)
    db_session.commit()
    assert second.auto_confirmed == 0
    assert second.already_matched_skipped == 3
    assert payment_refs(k1) == ["P-1"]
    assert payment_refs(k2) == ["P-2"]
    assert payment_refs(k3) == ["P-3"]


def test_capacity_advance_then_exhaustion_multi_candidate(db_session):
    """Once an earlier Contract is consumed, the next chronological subject
    advances to the next available candidate; when every candidate is
    consumed the subject is NOT silently over-allocated — it stays HCR with
    the capacity protection."""
    frag = _make_fragment(db_session)
    k1 = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_no="CA", contract_date=date(2026, 8, 1))
    k2 = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_no="CB", contract_date=date(2026, 8, 5))
    p1 = _make_payment(db_session, frag.id, "Seller A", "1000.00", transaction_date=date(2026, 8, 10), bank_reference="PA")
    p2 = _make_payment(db_session, frag.id, "Seller A", "1000.00", transaction_date=date(2026, 8, 20), bank_reference="PB")
    p3 = _make_payment(db_session, frag.id, "Seller A", "1000.00", transaction_date=date(2026, 8, 30), bank_reference="PC")
    db_session.flush()

    summary = match_payments(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 2  # p1->k1, p2->k2 (advance, no over-alloc)
    assert summary.human_confirmation_required == 0
    assert summary.capacity_exceeded == 1  # p3, capacity exhausted -> HCR via capacity protection
    assert summary.unmatched == 0

    payment_repo = PaymentRepository(db_session)
    assert payment_repo.get(PaymentAllocationRepository(db_session).list_for_contract(k1.id)[0].payment_id).bank_reference == "PA"
    assert payment_repo.get(PaymentAllocationRepository(db_session).list_for_contract(k2.id)[0].payment_id).bank_reference == "PB"

    p3_case = MatchCaseRepository(db_session).find_by_subject("PAYMENT", p3.id)
    assert p3_case.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED
    assert PaymentAllocationRepository(db_session).list_for_contract(k1.id)  # only p1
    assert PaymentAllocationRepository(db_session).list_for_contract(k2.id)  # only p2
    exceptions = ExceptionRepository(db_session).list_open()
    assert any(e.exception_type == ExceptionType.ALLOCATION_CAPACITY_EXCEEDED for e in exceptions)


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


# ---------------------------------------------------------------------------
# Repair #1 — dates are required ONLY when chronology must choose between
# multiple valid candidates; unique/effectively-unique matches proceed with
# missing dates; same-date ties use real business keys before UUID.
# ---------------------------------------------------------------------------


def test_missing_contract_date_among_multiple_candidates_is_hcr(db_session):
    """Two viable equivalent Contracts, one with a NULL contract_date:
    chronology is unavailable -> HCR, no allocation (NULL is never sorted
    as earliest/latest)."""
    frag = _make_fragment(db_session)
    dated = _make_contract(db_session, frag.id, "Seller A", "1580.00", contract_no="D1",
                           contract_date=date(2026, 8, 1))
    undated = _make_contract(db_session, frag.id, "Seller A", "1580.00", contract_no="U1", contract_date=None)
    invoice = _make_invoice(db_session, frag.id, "Seller A", "1580.00", issue_date=date(2026, 8, 10))
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 0
    assert summary.human_confirmation_required == 1
    case = MatchCaseRepository(db_session).find_by_subject("INVOICE", invoice.id)
    assert case.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED
    assert InvoiceAllocationRepository(db_session).list_for_contract(dated.id) == []
    assert InvoiceAllocationRepository(db_session).list_for_contract(undated.id) == []


def test_unique_candidate_with_missing_contract_date_auto_confirms(db_session):
    """A unique candidate needs no chronology — a NULL contract_date never
    blocks an otherwise deterministic match."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_date=None)
    invoice = _make_invoice(db_session, frag.id, "Seller A", "1000.00", issue_date=date(2026, 8, 10))
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 1
    assert summary.human_confirmation_required == 0
    case = MatchCaseRepository(db_session).find_by_subject("INVOICE", invoice.id)
    assert case.status == MatchCaseStatus.AUTO_CONFIRMED
    allocs = InvoiceAllocationRepository(db_session).list_for_contract(contract.id)
    assert len(allocs) == 1
    assert allocs[0].match_method == AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE


def test_missing_invoice_date_with_multiple_candidates_is_hcr(db_session):
    """Contracts are dated but the Invoice has no issue_date and subject
    chronology is required: no fabricated subject order -> HCR."""
    frag = _make_fragment(db_session)
    _make_contract(db_session, frag.id, "Seller A", "1580.00", contract_no="D1", contract_date=date(2026, 8, 1))
    _make_contract(db_session, frag.id, "Seller A", "1580.00", contract_no="D2", contract_date=date(2026, 8, 5))
    invoice = _make_invoice(db_session, frag.id, "Seller A", "1580.00", issue_date=None)
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 0
    assert summary.human_confirmation_required == 1
    case = MatchCaseRepository(db_session).find_by_subject("INVOICE", invoice.id)
    assert case.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED


def test_missing_invoice_date_effectively_unique_allocates(db_session):
    """A missing issue_date is only a problem when chronology is needed; an
    effectively unique correspondence allocates normally."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_date=date(2026, 8, 1))
    invoice = _make_invoice(db_session, frag.id, "Seller A", "1000.00", issue_date=None)
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 1
    assert summary.human_confirmation_required == 0
    assert InvoiceAllocationRepository(db_session).list_for_contract(contract.id)


def test_same_contract_date_tie_business_key_beats_uuid(db_session):
    """Two Contracts share contract_date; the UUID-min Contract (CB) is
    deliberately the one that WOULD win by UUID order, yet the earlier
    contract_no (CA) is chosen first — business/source key precedes UUID.
    Invoice order likewise follows external_invoice_key (INV-1 before
    INV-2) although INV-2's UUID is lower."""
    frag = _make_fragment(db_session)
    ca = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_no="CA-01",
                        contract_date=date(2026, 8, 1),
                        contract_id=uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"))
    cb = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_no="CB-02",
                        contract_date=date(2026, 8, 1),
                        contract_id=uuid.UUID("0a0a0a0a-0a0a-0a0a-0a0a-0a0a0a0a0a0b"))
    ia = _make_invoice(db_session, frag.id, "Seller A", "1000.00", issue_date=date(2026, 8, 5),
                       external_invoice_key="INV-1",
                       invoice_id=uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"))
    ib = _make_invoice(db_session, frag.id, "Seller A", "1000.00", issue_date=date(2026, 8, 5),
                       external_invoice_key="INV-2",
                       invoice_id=uuid.UUID("0c0c0c0c-0c0c-0c0c-0c0c-0c0c0c0c0c0d"))
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()
    assert summary.auto_confirmed == 2
    assert summary.human_confirmation_required == 0

    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(ca.id)] == [ia.id]
    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(cb.id)] == [ib.id]


def test_same_invoice_issue_date_tie_business_key_beats_uuid(db_session):
    """Same issue_date, distinct contract dates: the invoice with the lower
    external_invoice_key is processed first and takes the EARLIER Contract,
    even though its UUID is the HIGHER one (a UUID-first order would give
    the earlier Contract to the other invoice)."""
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_no="C1",
                        contract_date=date(2026, 8, 1),
                        contract_id=uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"))
    c2 = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_no="C2",
                        contract_date=date(2026, 8, 5),
                        contract_id=uuid.UUID("0a0a0a0a-0a0a-0a0a-0a0a-0a0a0a0a0a0b"))
    ia = _make_invoice(db_session, frag.id, "Seller A", "1000.00", issue_date=date(2026, 8, 5),
                       external_invoice_key="INV-2",
                       invoice_id=uuid.UUID("0c0c0c0c-0c0c-0c0c-0c0c-0c0c0c0c0c0d"))
    ib = _make_invoice(db_session, frag.id, "Seller A", "1000.00", issue_date=date(2026, 8, 5),
                       external_invoice_key="INV-1",
                       invoice_id=uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"))
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()
    assert summary.auto_confirmed == 2
    assert summary.human_confirmation_required == 0

    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(c1.id)] == [ib.id]
    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(c2.id)] == [ia.id]


# ---------------------------------------------------------------------------
# Repair #2 — a missing-date subject must never see "effective uniqueness"
# that was itself CREATED by another unresolved subject's chronological
# allocation earlier in this same run. Two or more unresolved subjects
# sharing a normalized counterparty + exact amount share the same static
# candidate Contract pool and are therefore a competing cohort: chronological
# fallback runs for the whole cohort only when every member has a real date
# AND every Contract it could still reach (pre-run) has a real contract_date.
# ---------------------------------------------------------------------------


def test_mixed_dated_undated_invoices_multiple_contracts_is_hcr(db_session):
    """Two equivalent dated Contracts and two competing unresolved Invoices,
    one dated and one undated, same counterparty+amount: the undated
    Invoice's relative order against the dated one is unknown, so the dated
    Invoice must NOT auto-consume a Contract first and leave the undated
    Invoice an 'effectively unique' leftover — both stay
    HUMAN_CONFIRMATION_REQUIRED, zero Allocations."""
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, "Seller A", "3000.00", contract_no="C1", contract_date=date(2026, 8, 1))
    c2 = _make_contract(db_session, frag.id, "Seller A", "3000.00", contract_no="C2", contract_date=date(2026, 8, 5))
    i1 = _make_invoice(db_session, frag.id, "Seller A", "3000.00", issue_date=date(2026, 8, 20), external_invoice_key="I1")
    i2 = _make_invoice(db_session, frag.id, "Seller A", "3000.00", issue_date=None, external_invoice_key="I2")
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 0
    assert summary.human_confirmation_required == 2
    assert MatchCaseRepository(db_session).find_by_subject("INVOICE", i1.id).status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED
    assert MatchCaseRepository(db_session).find_by_subject("INVOICE", i2.id).status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED
    assert InvoiceAllocationRepository(db_session).list_for_contract(c1.id) == []
    assert InvoiceAllocationRepository(db_session).list_for_contract(c2.id) == []


def test_mixed_dated_undated_invoices_single_contract_capacity_is_hcr(db_session):
    """One Contract, two competing unresolved Invoices (one dated, one
    undated) for its exact amount: only one could ever be allocated, but
    sorting the undated Invoice last must not manufacture an arbitrary
    winner — both stay HUMAN_CONFIRMATION_REQUIRED, zero Allocations."""
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, "Seller A", "3000.00", contract_date=date(2026, 8, 1))
    i1 = _make_invoice(db_session, frag.id, "Seller A", "3000.00", issue_date=date(2026, 8, 20), external_invoice_key="I1")
    i2 = _make_invoice(db_session, frag.id, "Seller A", "3000.00", issue_date=None, external_invoice_key="I2")
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 0
    assert summary.human_confirmation_required == 2
    assert InvoiceAllocationRepository(db_session).list_for_contract(c1.id) == []


def test_all_dated_cohort_allocates_chronologically(db_session):
    """Repair #2's fully-dated control case: two dated equivalent Contracts
    and two dated competing Invoices — chronological allocation proceeds
    exactly as before, both AUTO_CONFIRMED, zero HCR."""
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, "Seller A", "3000.00", contract_no="C1", contract_date=date(2026, 8, 1))
    c2 = _make_contract(db_session, frag.id, "Seller A", "3000.00", contract_no="C2", contract_date=date(2026, 8, 5))
    i1 = _make_invoice(db_session, frag.id, "Seller A", "3000.00", issue_date=date(2026, 8, 20), external_invoice_key="I1")
    i2 = _make_invoice(db_session, frag.id, "Seller A", "3000.00", issue_date=date(2026, 8, 28), external_invoice_key="I2")
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 2
    assert summary.human_confirmation_required == 0
    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(c1.id)] == [i1.id]
    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(c2.id)] == [i2.id]


def test_single_undated_invoice_unique_contract_auto_confirms(db_session):
    """No competition (only one unresolved Invoice for this
    counterparty+amount): a missing issue_date never blocks the
    deterministic unique match."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "Seller A", "3000.00", contract_date=date(2026, 8, 1))
    invoice = _make_invoice(db_session, frag.id, "Seller A", "3000.00", issue_date=None)
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 1
    assert summary.human_confirmation_required == 0
    assert InvoiceAllocationRepository(db_session).list_for_contract(contract.id)


def test_single_undated_invoice_preexisting_capacity_makes_one_contract_viable(db_session):
    """Two static candidate Contracts, but one was already fully consumed by
    a prior authoritative match BEFORE this run — the remaining Contract is
    genuinely, independently unique regardless of the current (undated)
    Invoice's date, since nothing in THIS run created that narrowing."""
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, "Seller A", "3000.00", contract_no="C1", contract_date=date(2026, 8, 1))
    c2 = _make_contract(db_session, frag.id, "Seller A", "3000.00", contract_no="C2", contract_date=date(2026, 8, 5))
    prior_invoice = _make_invoice(db_session, frag.id, "Seller A", "3000.00", issue_date=date(2026, 7, 1))
    db_session.flush()
    match_invoices(db_session)  # prior run consumes c1 (earliest dated) via prior_invoice
    db_session.commit()
    assert InvoiceAllocationRepository(db_session).list_for_contract(c1.id)

    new_invoice = _make_invoice(db_session, frag.id, "Seller A", "3000.00", issue_date=None)
    db_session.flush()
    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 1
    assert summary.human_confirmation_required == 0
    case = MatchCaseRepository(db_session).find_by_subject("INVOICE", new_invoice.id)
    assert case.status == MatchCaseStatus.AUTO_CONFIRMED
    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(c2.id)] == [new_invoice.id]


def test_undated_contract_in_competing_set_is_hcr(db_session):
    """Two dated competing Invoices (same counterparty+amount, a genuine
    cohort) whose static candidate Contracts include one with no
    contract_date: Contract chronology is undefined, so no partial
    dated-Contract-first allocation happens for either Invoice — both stay
    HUMAN_CONFIRMATION_REQUIRED, zero Allocations."""
    frag = _make_fragment(db_session)
    dated_contract = _make_contract(db_session, frag.id, "Seller A", "3000.00", contract_no="D1",
                                    contract_date=date(2026, 8, 1))
    undated_contract = _make_contract(db_session, frag.id, "Seller A", "3000.00", contract_no="U1", contract_date=None)
    i1 = _make_invoice(db_session, frag.id, "Seller A", "3000.00", issue_date=date(2026, 8, 10), external_invoice_key="I1")
    i2 = _make_invoice(db_session, frag.id, "Seller A", "3000.00", issue_date=date(2026, 8, 20), external_invoice_key="I2")
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()

    assert summary.auto_confirmed == 0
    assert summary.human_confirmation_required == 2
    assert InvoiceAllocationRepository(db_session).list_for_contract(dated_contract.id) == []
    assert InvoiceAllocationRepository(db_session).list_for_contract(undated_contract.id) == []


# ---------------------------------------------------------------------------
# Final small repair — PURCHASE Invoice same-date tie-break must fall back
# through the full business/source identifier chain (external_invoice_key ->
# digital_invoice_no -> invoice_no) before ever reaching UUID, and a blank/
# whitespace-only value in that chain must not count as usable.
# ---------------------------------------------------------------------------


def test_missing_external_key_falls_back_to_digital_invoice_no(db_session):
    """external_invoice_key is absent on both invoices: digital_invoice_no
    determines the tie-break order, not UUID. UUIDs are deliberately
    constructed in the OPPOSITE order from the expected digital_invoice_no
    order (ia's UUID sorts before ib's) to prove UUID is not driving the
    result — if it were, ia would win the earlier Contract instead of ib."""
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_no="C1",
                        contract_date=date(2026, 8, 1),
                        contract_id=uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"))
    c2 = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_no="C2",
                        contract_date=date(2026, 8, 5),
                        contract_id=uuid.UUID("0a0a0a0a-0a0a-0a0a-0a0a-0a0a0a0a0a0b"))
    ia = _make_invoice(db_session, frag.id, "Seller A", "1000.00", issue_date=date(2026, 8, 5),
                       digital_invoice_no="D-2",
                       invoice_id=uuid.UUID("0c0c0c0c-0c0c-0c0c-0c0c-0c0c0c0c0c0d"))
    ib = _make_invoice(db_session, frag.id, "Seller A", "1000.00", issue_date=date(2026, 8, 5),
                       digital_invoice_no="D-1",
                       invoice_id=uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"))
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()
    assert summary.auto_confirmed == 2
    assert summary.human_confirmation_required == 0

    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(c1.id)] == [ib.id]
    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(c2.id)] == [ia.id]


def test_missing_external_and_digital_falls_back_to_invoice_no(db_session):
    """external_invoice_key and digital_invoice_no are both absent:
    invoice_no determines the tie-break order, not UUID. UUIDs are
    deliberately constructed in the OPPOSITE order from the expected
    invoice_no order (ia's UUID sorts before ib's) to prove UUID is not
    driving the result — if it were, ia would win the earlier Contract
    instead of ib."""
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_no="C1",
                        contract_date=date(2026, 8, 1),
                        contract_id=uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"))
    c2 = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_no="C2",
                        contract_date=date(2026, 8, 5),
                        contract_id=uuid.UUID("0a0a0a0a-0a0a-0a0a-0a0a-0a0a0a0a0a0b"))
    ia = _make_invoice(db_session, frag.id, "Seller A", "1000.00", issue_date=date(2026, 8, 5),
                       invoice_no="N-2",
                       invoice_id=uuid.UUID("0c0c0c0c-0c0c-0c0c-0c0c-0c0c0c0c0c0d"))
    ib = _make_invoice(db_session, frag.id, "Seller A", "1000.00", issue_date=date(2026, 8, 5),
                       invoice_no="N-1",
                       invoice_id=uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"))
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()
    assert summary.auto_confirmed == 2
    assert summary.human_confirmation_required == 0

    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(c1.id)] == [ib.id]
    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(c2.id)] == [ia.id]


def test_blank_business_keys_are_not_usable_and_fall_through(db_session):
    """A present-but-blank/whitespace-only external_invoice_key or
    digital_invoice_no must not count as a usable business key — it falls
    through to the next key in the chain exactly like NULL would. UUIDs are
    again constructed in the opposite order from the expected invoice_no
    order to prove neither UUID nor the blank fields are driving the
    result."""
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_no="C1",
                        contract_date=date(2026, 8, 1),
                        contract_id=uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"))
    c2 = _make_contract(db_session, frag.id, "Seller A", "1000.00", contract_no="C2",
                        contract_date=date(2026, 8, 5),
                        contract_id=uuid.UUID("0a0a0a0a-0a0a-0a0a-0a0a-0a0a0a0a0a0b"))
    ia = _make_invoice(db_session, frag.id, "Seller A", "1000.00", issue_date=date(2026, 8, 5),
                       external_invoice_key="   ", digital_invoice_no="", invoice_no="N-2",
                       invoice_id=uuid.UUID("0c0c0c0c-0c0c-0c0c-0c0c-0c0c0c0c0c0d"))
    ib = _make_invoice(db_session, frag.id, "Seller A", "1000.00", issue_date=date(2026, 8, 5),
                       external_invoice_key="", digital_invoice_no="   ", invoice_no="N-1",
                       invoice_id=uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"))
    db_session.flush()

    summary = match_invoices(db_session)
    db_session.commit()
    assert summary.auto_confirmed == 2
    assert summary.human_confirmation_required == 0

    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(c1.id)] == [ib.id]
    assert [a.invoice_id for a in InvoiceAllocationRepository(db_session).list_for_contract(c2.id)] == [ia.id]

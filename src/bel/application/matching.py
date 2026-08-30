"""M001 — Exact Counterparty + Exact Gross Amount.

Eligibility gate first (spec sections 9/14): a subject only enters
matching at all if its (normalized) counterparty is a party to some
contract — amount plays no role in eligibility. Subjects that fail this
(phone bills, salaries, tax, logistics, any one-off purchase unrelated
to a procurement contract) never become a MatchCase of any kind,
including UNMATCHED — see `_is_eligible` and docs/PHASE2A-DECISIONS.md
for why an earlier version that skipped this produced misleading-looking
correct totals while quietly generating UNMATCHED noise for everything
out of scope.

Two-phase by construction, so sequence-guessing is structurally
impossible (spec section 20):

  Pass 1 computes every subject's candidate Contract set as a pure
  function of a single static snapshot of contracts. No subject's
  evaluation can see or affect another subject's candidates, and
  nothing is written yet — so there is no way for import order, Excel
  row order, or bank statement order to influence who is "ambiguous"
  versus "unique."

  Pass 2 turns each subject's (already-fixed) candidate set into a
  MatchCase, and only allocates for genuinely unique candidates — after
  a capacity check. The one place processing order can still matter is
  concurrent capacity exhaustion (two unique-candidate subjects racing
  for the same contract's remaining capacity) — that is a real resource
  constraint, not a guess, and the loser gets an auditable
  ALLOCATION_CAPACITY_EXCEEDED exception rather than a silent wrong
  allocation. See docs/PHASE2A-DECISIONS.md.

match_invoices() and match_payments() share one core loop
(_run_match_pass) parameterized by the handful of things that actually
differ between an Invoice and a Payment subject — not a generic rule
engine, just avoiding two near-identical 150-line copies of the same
M001 mechanics.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Sequence

from sqlalchemy.orm import Session

from bel.domain.contract import Contract
from bel.domain.event import BusinessEvent, BusinessEventType
from bel.domain.exception import ExceptionStatus, ExceptionType, TaskException
from bel.domain.invoice import InvoiceDirection
from bel.domain.matching import (
    AllocationMatchMethod,
    ConfirmationType,
    InvoiceAllocation,
    MatchCandidate,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
    PaymentAllocation,
    SubjectType,
)
from bel.domain.normalize import normalize_counterparty
from bel.domain.payment import PaymentDirection
from bel.infrastructure.persistence.database import acquire_serialization_lock
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EventRepository,
    ExceptionRepository,
    InvoiceAllocationRepository,
    InvoiceRepository,
    MatchCandidateRepository,
    MatchCaseRepository,
    PaymentAllocationRepository,
    PaymentRepository,
)


@dataclass
class MatchRunSummary:
    eligible_total: int = 0
    out_of_scope: int = 0
    auto_confirmed: int = 0
    human_confirmation_required: int = 0
    unmatched: int = 0
    capacity_exceeded: int = 0
    already_matched_skipped: int = 0
    subject_ids: list[uuid.UUID] = field(default_factory=list)


def _is_eligible(subject_counterparty: str | None, contract_counterparties: set[str]) -> bool:
    """Eligibility (spec sections 9/14) is defined by counterparty
    identity ALONE — never by amount. An invoice/payment whose
    counterparty was never a party to any of our contracts is simply not
    contract-related business (phone bills, salaries, tax, logistics —
    spec section 14's explicit ContractNotFound-noise warning) and must
    never become a MatchCase at all, not even UNMATCHED. Only among
    counterparty-eligible subjects does "0 amount-matching candidates"
    mean anything (see docs/PHASE2A-DECISIONS.md)."""
    normalized = normalize_counterparty(subject_counterparty)
    return normalized is not None and normalized in contract_counterparties


def _find_candidate_contract_ids(
    subject_amount: Decimal, subject_counterparty: str | None, contracts: Sequence[Contract]
) -> list[uuid.UUID]:
    """Pure: reads only its arguments, mutates nothing. Called once per
    eligible subject in Pass 1, always against the same `contracts`
    snapshot."""
    normalized_subject = normalize_counterparty(subject_counterparty)
    if normalized_subject is None:
        return []
    return [
        c.id
        for c in contracts
        if normalize_counterparty(c.counterparty) == normalized_subject and c.gross_amount == subject_amount
    ]


def _run_match_pass(
    *,
    session: Session,
    subject_type: str,
    subjects: Sequence,
    get_amount: Callable[[object], Decimal],
    get_counterparty: Callable[[object], str | None],
    contracts: Sequence[Contract],
    match_case_repo: MatchCaseRepository,
    candidate_repo: MatchCandidateRepository,
    exception_repo: ExceptionRepository,
    event_repo: EventRepository,
    sum_confirmed_for_contract: Callable[[uuid.UUID], Decimal],
    create_allocation: Callable[[object, uuid.UUID, uuid.UUID, datetime], None],
    auto_confirmed_event_type: str,
    now: datetime,
) -> MatchRunSummary:
    # Eligibility gate (spec sections 9/14): counterparty membership only,
    # checked before anything else. Subjects that fail this never become
    # a MatchCase — see _is_eligible's docstring.
    contract_counterparties = {
        n for c in contracts if (n := normalize_counterparty(c.counterparty)) is not None
    }
    eligible_subjects = [s for s in subjects if _is_eligible(get_counterparty(s), contract_counterparties)]

    summary = MatchRunSummary()
    summary.eligible_total = len(eligible_subjects)
    summary.out_of_scope = len(subjects) - len(eligible_subjects)

    # Pass 1: pure candidate computation for every ELIGIBLE subject, no writes.
    candidates_by_subject = {
        s.id: _find_candidate_contract_ids(get_amount(s), get_counterparty(s), contracts) for s in eligible_subjects
    }

    # Pass 2: turn each (already-fixed) candidate set into outcomes.
    for subject in eligible_subjects:
        if match_case_repo.find_by_subject(subject_type, subject.id) is not None:
            summary.already_matched_skipped += 1
            continue

        candidates = candidates_by_subject[subject.id]
        match_case_id = uuid.uuid4()

        if len(candidates) == 0:
            match_case_repo.add(
                MatchCase(
                    id=match_case_id,
                    subject_type=subject_type,
                    subject_id=subject.id,
                    status=MatchCaseStatus.UNMATCHED,
                    match_method=MatchMethod.M001,
                    created_at=now,
                    resolved_at=None,
                )
            )
            # No dependent rows this branch — nothing else references
            # match_case_id here, so no flush needed.
            summary.unmatched += 1
            summary.subject_ids.append(subject.id)
            continue

        if len(candidates) > 1:
            match_case_repo.add(
                MatchCase(
                    id=match_case_id,
                    subject_type=subject_type,
                    subject_id=subject.id,
                    status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
                    match_method=MatchMethod.M001,
                    created_at=now,
                    resolved_at=None,
                )
            )
            # MatchCandidate rows below reference match_case_id — flush
            # first so the FK is satisfied. Same lesson as Phase 1's
            # fragment/contract ordering (no relationship() -> no
            # automatic cross-table insert ordering).
            session.flush()
            for contract_id in candidates:
                candidate_repo.add(
                    MatchCandidate(id=uuid.uuid4(), match_case_id=match_case_id, contract_id=contract_id, created_at=now)
                )
            event_repo.add(
                BusinessEvent(
                    id=uuid.uuid4(),
                    event_type=BusinessEventType.MATCH_HUMAN_CONFIRMATION_REQUIRED,
                    occurred_at=now,
                    payload={
                        "subject_type": subject_type,
                        "subject_id": str(subject.id),
                        "candidate_contract_ids": [str(c) for c in candidates],
                    },
                )
            )
            summary.human_confirmation_required += 1
            summary.subject_ids.append(subject.id)
            continue

        # Exactly one candidate.
        contract_id = candidates[0]
        contract = next(c for c in contracts if c.id == contract_id)
        amount = get_amount(subject)
        already_allocated = sum_confirmed_for_contract(contract_id)

        if already_allocated + amount > contract.gross_amount:
            match_case_repo.add(
                MatchCase(
                    id=match_case_id,
                    subject_type=subject_type,
                    subject_id=subject.id,
                    status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
                    match_method=MatchMethod.M001,
                    created_at=now,
                    resolved_at=None,
                )
            )
            session.flush()
            candidate_repo.add(
                MatchCandidate(id=uuid.uuid4(), match_case_id=match_case_id, contract_id=contract_id, created_at=now)
            )
            exception_repo.add(
                TaskException(
                    id=uuid.uuid4(),
                    exception_type=ExceptionType.ALLOCATION_CAPACITY_EXCEEDED,
                    status=ExceptionStatus.OPEN,
                    summary=(
                        f"{subject_type} {subject.id} would push confirmed allocations for contract "
                        f"{contract_id} to {already_allocated + amount}, exceeding gross_amount {contract.gross_amount}"
                    ),
                    detail={
                        "subject_type": subject_type,
                        "subject_id": str(subject.id),
                        "contract_id": str(contract_id),
                        "already_allocated": str(already_allocated),
                        "attempted_amount": str(amount),
                        "contract_gross_amount": str(contract.gross_amount),
                    },
                    created_at=now,
                )
            )
            event_repo.add(
                BusinessEvent(
                    id=uuid.uuid4(),
                    event_type=BusinessEventType.ALLOCATION_CAPACITY_EXCEEDED,
                    occurred_at=now,
                    payload={"subject_type": subject_type, "subject_id": str(subject.id), "contract_id": str(contract_id)},
                )
            )
            summary.capacity_exceeded += 1
            summary.subject_ids.append(subject.id)
            continue

        match_case_repo.add(
            MatchCase(
                id=match_case_id,
                subject_type=subject_type,
                subject_id=subject.id,
                status=MatchCaseStatus.AUTO_CONFIRMED,
                match_method=MatchMethod.M001,
                created_at=now,
                resolved_at=now,
            )
        )
        session.flush()
        candidate_repo.add(
            MatchCandidate(id=uuid.uuid4(), match_case_id=match_case_id, contract_id=contract_id, created_at=now)
        )
        create_allocation(subject, contract_id, match_case_id, now)
        event_repo.add(
            BusinessEvent(
                id=uuid.uuid4(),
                event_type=auto_confirmed_event_type,
                occurred_at=now,
                payload={"subject_id": str(subject.id), "contract_id": str(contract_id), "amount": str(amount)},
            )
        )
        summary.auto_confirmed += 1
        summary.subject_ids.append(subject.id)

    session.commit()
    return summary


def match_invoices(session: Session) -> MatchRunSummary:
    """Phase 2D.1-P: acquire_serialization_lock() is the FIRST action,
    before any read — this is a production read-check-write batch writer
    (read current MatchCase state, read allocation capacity, decide,
    write MatchCase + allocation), and SQLite's implicit whole-database
    write lock always serialized it against every other writer
    (including a concurrent confirm_match / another match pass)
    for free. PostgreSQL's weaker default isolation has no such implicit
    guarantee, so this now takes the same shared advisory lock every
    other manual/batch writer takes, closing the same race class."""
    acquire_serialization_lock(session)
    now = datetime.now(timezone.utc)
    contract_repo = ContractRepository(session)
    invoice_repo = InvoiceRepository(session)
    match_case_repo = MatchCaseRepository(session)
    candidate_repo = MatchCandidateRepository(session)
    allocation_repo = InvoiceAllocationRepository(session)
    exception_repo = ExceptionRepository(session)
    event_repo = EventRepository(session)

    contracts = contract_repo.list_all()
    eligible = [inv for inv in invoice_repo.list_all() if inv.direction == InvoiceDirection.PURCHASE]
    # Deterministic, business-meaningless order — see docs/PHASE2A-DECISIONS.md.
    eligible.sort(key=lambda inv: str(inv.id))

    def create_allocation(invoice, contract_id: uuid.UUID, match_case_id: uuid.UUID, now: datetime) -> None:
        allocation_repo.add(
            InvoiceAllocation(
                id=uuid.uuid4(),
                invoice_id=invoice.id,
                contract_id=contract_id,
                match_case_id=match_case_id,
                allocated_gross_amount=invoice.gross_amount,
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED,
                created_at=now,
            )
        )

    return _run_match_pass(
        session=session,
        subject_type=SubjectType.INVOICE,
        subjects=eligible,
        get_amount=lambda inv: inv.gross_amount,
        get_counterparty=lambda inv: inv.seller,
        contracts=contracts,
        match_case_repo=match_case_repo,
        candidate_repo=candidate_repo,
        exception_repo=exception_repo,
        event_repo=event_repo,
        sum_confirmed_for_contract=allocation_repo.sum_confirmed_for_contract,
        create_allocation=create_allocation,
        auto_confirmed_event_type=BusinessEventType.INVOICE_MATCH_AUTO_CONFIRMED,
        now=now,
    )


def match_payments(session: Session) -> MatchRunSummary:
    """The OUT-payment twin of match_invoices — see its docstring for
    why acquire_serialization_lock() is the first action here too."""
    acquire_serialization_lock(session)
    now = datetime.now(timezone.utc)
    contract_repo = ContractRepository(session)
    payment_repo = PaymentRepository(session)
    match_case_repo = MatchCaseRepository(session)
    candidate_repo = MatchCandidateRepository(session)
    allocation_repo = PaymentAllocationRepository(session)
    exception_repo = ExceptionRepository(session)
    event_repo = EventRepository(session)

    contracts = contract_repo.list_all()
    eligible = [p for p in payment_repo.list_all() if p.direction == PaymentDirection.OUT]
    eligible.sort(key=lambda p: str(p.id))

    def create_allocation(payment, contract_id: uuid.UUID, match_case_id: uuid.UUID, now: datetime) -> None:
        allocation_repo.add(
            PaymentAllocation(
                id=uuid.uuid4(),
                payment_id=payment.id,
                contract_id=contract_id,
                match_case_id=match_case_id,
                allocated_amount=payment.amount,
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED,
                created_at=now,
            )
        )

    return _run_match_pass(
        session=session,
        subject_type=SubjectType.PAYMENT,
        subjects=eligible,
        get_amount=lambda p: p.amount,
        get_counterparty=lambda p: p.counterparty,
        contracts=contracts,
        match_case_repo=match_case_repo,
        candidate_repo=candidate_repo,
        exception_repo=exception_repo,
        event_repo=event_repo,
        sum_confirmed_for_contract=allocation_repo.sum_confirmed_for_contract,
        create_allocation=create_allocation,
        auto_confirmed_event_type=BusinessEventType.PAYMENT_MATCH_AUTO_CONFIRMED,
        now=now,
    )


def confirm_match(session: Session, match_case_id: uuid.UUID, contract_id: uuid.UUID) -> None:
    """Human confirmation for a HUMAN_CONFIRMATION_REQUIRED MatchCase.
    contract_id need not be one of the pre-computed MatchCandidates — a
    human may know something M001 can't (spec section 26) — but the same
    capacity guard from the automated pass still applies: this CLI is
    not a bypass for over-allocating a contract.

    Phase 2D.1-P: the capacity check below and the allocation write have
    no DB-level backstop of their own (unlike the sales-leg twin, which
    folds its capacity check into the atomic INSERT itself), so under
    PostgreSQL's weaker default isolation this needs
    ``acquire_serialization_lock`` to stay race-free, exactly as it
    always implicitly was under SQLite's whole-database write lock."""
    acquire_serialization_lock(session)
    now = datetime.now(timezone.utc)
    match_case_repo = MatchCaseRepository(session)
    contract_repo = ContractRepository(session)
    event_repo = EventRepository(session)

    match_case = match_case_repo.get(match_case_id)
    if match_case is None:
        raise ValueError(f"MatchCase {match_case_id} not found")
    if match_case.status != MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED:
        raise ValueError(f"MatchCase {match_case_id} is {match_case.status}, not HUMAN_CONFIRMATION_REQUIRED")

    contract = contract_repo.get(contract_id)
    if contract is None:
        raise ValueError(f"Contract {contract_id} not found")

    if match_case.subject_type == SubjectType.INVOICE:
        invoice_repo = InvoiceRepository(session)
        allocation_repo = InvoiceAllocationRepository(session)
        invoice = invoice_repo.get(match_case.subject_id)
        if invoice is None:
            raise ValueError(f"Invoice {match_case.subject_id} not found")
        # Phase 2D.1-R3b, docs/PHASE2D1-R0-DECISIONS.md section 2.7 Gate
        # G5 guard #1 (HARD): this function has no leg/direction check of
        # its own — left as is, confirming a SALES invoice's MatchCase
        # here would attribute it to a procurement Contract, the exact
        # outcome the sales/procurement physical separation forbids. A
        # defensive rejection only; M001 semantics are unchanged.
        if invoice.direction != InvoiceDirection.PURCHASE:
            raise ValueError(
                f"MatchCase {match_case_id} is for a {invoice.direction} invoice — procurement confirm_match "
                "only accepts PURCHASE invoices; use bel.application.sales_matching.confirm_sales_invoice_match"
            )
        amount = invoice.gross_amount
        already_allocated = allocation_repo.sum_confirmed_for_contract(contract_id)
        if already_allocated + amount > contract.gross_amount:
            raise ValueError(
                f"Confirming would push confirmed allocations for contract {contract_id} to "
                f"{already_allocated + amount}, exceeding gross_amount {contract.gross_amount}"
            )
        allocation_repo.add(
            InvoiceAllocation(
                id=uuid.uuid4(),
                invoice_id=invoice.id,
                contract_id=contract_id,
                match_case_id=match_case_id,
                allocated_gross_amount=amount,
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.HUMAN_CONFIRMED,
                created_at=now,
            )
        )
    elif match_case.subject_type == SubjectType.PAYMENT:
        payment_repo = PaymentRepository(session)
        allocation_repo = PaymentAllocationRepository(session)
        payment = payment_repo.get(match_case.subject_id)
        if payment is None:
            raise ValueError(f"Payment {match_case.subject_id} not found")
        # Same Gate G5 guard #1 as the INVOICE branch above, for the OUT/IN direction.
        if payment.direction != PaymentDirection.OUT:
            raise ValueError(
                f"MatchCase {match_case_id} is for a {payment.direction} payment — procurement confirm_match "
                "only accepts OUT payments; use bel.application.sales_matching.confirm_sales_payment_match"
            )
        amount = payment.amount
        already_allocated = allocation_repo.sum_confirmed_for_contract(contract_id)
        if already_allocated + amount > contract.gross_amount:
            raise ValueError(
                f"Confirming would push confirmed allocations for contract {contract_id} to "
                f"{already_allocated + amount}, exceeding gross_amount {contract.gross_amount}"
            )
        allocation_repo.add(
            PaymentAllocation(
                id=uuid.uuid4(),
                payment_id=payment.id,
                contract_id=contract_id,
                match_case_id=match_case_id,
                allocated_amount=amount,
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.HUMAN_CONFIRMED,
                created_at=now,
            )
        )
    else:
        raise ValueError(f"Unknown subject_type {match_case.subject_type!r}")

    match_case_repo.update_status(match_case_id, MatchCaseStatus.RESOLVED, resolved_at=now)
    event_repo.add(
        BusinessEvent(
            id=uuid.uuid4(),
            event_type=BusinessEventType.MATCH_HUMAN_CONFIRMED,
            occurred_at=now,
            payload={"match_case_id": str(match_case_id), "contract_id": str(contract_id)},
        )
    )

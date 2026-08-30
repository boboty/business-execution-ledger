"""Sales-side manual matching (Phase 2D.1-R3b,
docs/PHASE2D1-R0-DECISIONS.md sections 2.5-2.7).

Establishes the sales leg's ONLY match mechanism this round: an explicit
human proposal naming candidate `SalesContract`s, followed by an explicit
human confirmation with explicit per-target amounts. `MatchCase` is
reused unchanged in shape (2.7's "MatchCase reuse") — this module adds
no field, no FK, no new status to it. `SalesMatchCandidate`,
`SalesInvoiceAllocation`, and `SalesPaymentAllocation` are the sales-leg's
own objects, physically separate from `MatchCandidate`/`InvoiceAllocation`/
`PaymentAllocation`, which stay completely untouched by this module.

    SALES Invoice / IN Payment
    + candidate SalesContract id(s), explicitly supplied by the caller
          |
          v
    propose_sales_invoice_match / propose_sales_payment_match
          |
          v
    MatchCase(HUMAN_CONFIRMATION_REQUIRED, MANUAL_SALES_SCOPE)
    + real SalesMatchCandidate rows
          |
          v
    confirm_sales_invoice_match / confirm_sales_payment_match
    (explicit (sales_contract_id, amount) pairs, one submission covers
    the WHOLE confirmation — never a per-target call that could leave a
    case ambiguously half-confirmed)
          |
          v
    SalesInvoiceAllocation / SalesPaymentAllocation rows (HUMAN_CONFIRMED)
    + MatchCase -> RESOLVED, resolved_at set
          |
          v
    query / read model

Frozen semantics this module implements
(docs/PHASE2D1-R0-DECISIONS.md sections 2.5-2.7):

- No automatic sales matching algorithm of any kind — no counterparty
  match, no amount match, no M001 reuse, no `ProcurementSalesLink`- or
  `Shipment`-based auto-targeting. Every candidate and every allocation
  amount is supplied explicitly by the caller.
- `confirmation_type` is `HUMAN_CONFIRMED` only this round — enforced
  both here and by a DB CHECK constraint on both allocation tables.
- One subject may allocate across several `SalesContract`s in ONE
  confirmation submission (never a design that forces "one target then
  permanently closed").
- No apportionment: amounts are never computed by dividing anything by
  anything — only validated (positive, not exceeding the subject's own
  remaining amount) and stored exactly as given.
- No invented completion state: this module tracks only "does the sum
  of confirmed allocations exceed the subject's amount" (rejected) — it
  never introduces a PARTIALLY_MATCHED/FULLY_MATCHED judgment. A
  confirmed case whose allocations sum to less than the subject's full
  amount is simply RESOLVED, same as one that sums to the full amount.
- Direction is authoritative, never guessed from `match_method` or
  anything else: `propose_sales_invoice_match` requires
  `Invoice.direction == SALES`; `propose_sales_payment_match` requires
  `Payment.direction == IN`. The confirm functions re-check the SAME
  thing defensively, independent of whatever created the MatchCase.
- Idempotent proposal and confirmation: exact replay (same candidate
  set / same allocation set) is a no-op; a different payload against an
  already-decided MatchCase is a rejected conflict, never a silent
  second authoritative write.
- Concurrency: `MatchCaseRepository.add_if_no_case_for_subject` and
  `.resolve_if_pending` are single atomic conditional SQL statements
  (see their docstrings) — never a separate check-then-write — so two
  concurrent proposals or confirmations for the same subject/case can
  never both succeed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from bel.domain.invoice import InvoiceDirection
from bel.domain.matching import (
    ConfirmationType,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
    SalesInvoiceAllocation,
    SalesMatchCandidate,
    SalesPaymentAllocation,
    SubjectType,
    validate_storable_amount,
)
from bel.domain.payment import PaymentDirection
from bel.infrastructure.persistence.database import is_database_busy
from bel.infrastructure.persistence.repositories import (
    InvoiceRepository,
    MatchCaseNotPendingError,
    MatchCaseRepository,
    PaymentRepository,
    SalesContractRepository,
    SalesInvoiceAllocationRepository,
    SalesMatchCandidateRepository,
    SalesPaymentAllocationRepository,
)


class SalesMatchError(ValueError):
    """A rejected sales-match operation — missing subject/SalesContract,
    wrong direction, an unmet MatchCase precondition, or an invalid
    allocation amount. Surfaces as an explicit failure, never a silent
    partial write."""


class SalesMatchConflict(SalesMatchError):
    """An explicit-intent conflict the system will not guess through: a
    proposal or confirmation whose payload differs from an
    already-decided one, or a race lost to a concurrent proposal/
    confirmation for the same subject/case. A human must resolve this,
    never an inferred merge or blind retry."""


@dataclass(frozen=True)
class SalesMatchProposalResult:
    match_case: MatchCase
    created: bool = False
    replay: bool = False


@dataclass(frozen=True)
class SalesMatchConfirmationResult:
    match_case: MatchCase
    allocations: list
    created: bool = False
    replay: bool = False


class _ConfirmationRaceLost(Exception):
    """Internal control-flow signal only — never escapes this module."""


def _validate_candidate_ids(session: Session, sales_contract_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    if not sales_contract_ids:
        raise SalesMatchError("at least one candidate SalesContract is required")
    if len(set(sales_contract_ids)) != len(sales_contract_ids):
        raise SalesMatchError("duplicate candidate SalesContract in one proposal")
    sc_repo = SalesContractRepository(session)
    for sc_id in sales_contract_ids:
        if sc_repo.get(sc_id) is None:
            raise SalesMatchError(f"SalesContract {sc_id} not found")
    return list(sales_contract_ids)


def _validate_allocation_pairs(
    session: Session, allocations: Sequence[tuple[uuid.UUID, Decimal]]
) -> list[tuple[uuid.UUID, Decimal]]:
    if not allocations:
        raise SalesMatchError("at least one allocation is required")
    targets = [sc_id for sc_id, _ in allocations]
    if len(set(targets)) != len(targets):
        raise SalesMatchError("duplicate target SalesContract in one confirmation")
    sc_repo = SalesContractRepository(session)
    pairs: list[tuple[uuid.UUID, Decimal]] = []
    for sc_id, amount in allocations:
        if sc_repo.get(sc_id) is None:
            raise SalesMatchError(f"SalesContract {sc_id} not found")
        try:
            validate_storable_amount(amount)
        except ValueError as exc:
            raise SalesMatchError(f"allocation amount for SalesContract {sc_id}: {exc}") from exc
        pairs.append((sc_id, amount))
    return pairs


def _replay_or_conflict_proposal(
    existing_case: MatchCase, candidate_repo: SalesMatchCandidateRepository, sales_contract_ids: Sequence[uuid.UUID]
) -> SalesMatchProposalResult:
    existing_candidates = {c.sales_contract_id for c in candidate_repo.list_for_case(existing_case.id)}
    if existing_candidates == set(sales_contract_ids):
        return SalesMatchProposalResult(match_case=existing_case, created=False, replay=True)
    raise SalesMatchConflict(
        f"MatchCase {existing_case.id} already exists for this subject with a different candidate set — "
        "propose never silently appends a second authoritative candidate set"
    )


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------


def propose_sales_invoice_match(
    session: Session, *, invoice_id: uuid.UUID, sales_contract_ids: Sequence[uuid.UUID], created_at: datetime
) -> SalesMatchProposalResult:
    """Precondition: `invoice_id` names a `SALES` invoice with no
    existing MatchCase (or an identical prior proposal — idempotent
    replay). Never computes candidates itself — `sales_contract_ids` is
    caller-supplied, exactly as docs/PHASE2D1-R0-DECISIONS.md section 2.7
    requires ("no automatic sales matching algorithm")."""
    invoice = InvoiceRepository(session).get(invoice_id)
    if invoice is None:
        raise SalesMatchError(f"Invoice {invoice_id} not found")
    if invoice.direction != InvoiceDirection.SALES:
        raise SalesMatchError(
            f"Invoice {invoice_id} has direction {invoice.direction!r} — only SALES invoices may enter a "
            "sales MatchCase; use the procurement match path for PURCHASE invoices"
        )
    _validate_candidate_ids(session, sales_contract_ids)

    match_case_repo = MatchCaseRepository(session)
    candidate_repo = SalesMatchCandidateRepository(session)
    existing = match_case_repo.find_by_subject(SubjectType.INVOICE, invoice_id)
    if existing is not None:
        return _replay_or_conflict_proposal(existing, candidate_repo, sales_contract_ids)

    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.INVOICE,
        subject_id=invoice_id,
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
        match_method=MatchMethod.MANUAL_SALES_SCOPE,
        created_at=created_at,
        resolved_at=None,
    )
    try:
        inserted = match_case_repo.add_if_no_case_for_subject(match_case)
    except OperationalError as exc:
        if is_database_busy(exc):
            raise SalesMatchConflict(
                f"Invoice {invoice_id} could not be proposed due to concurrent database contention — retry"
            ) from exc
        raise
    if not inserted:
        # Lost a race against a concurrent proposal for the SAME subject.
        existing = match_case_repo.find_by_subject(SubjectType.INVOICE, invoice_id)
        assert existing is not None
        return _replay_or_conflict_proposal(existing, candidate_repo, sales_contract_ids)

    persisted = match_case_repo.get(match_case.id)
    assert persisted is not None
    for sc_id in sales_contract_ids:
        candidate_repo.add(
            SalesMatchCandidate(id=uuid.uuid4(), match_case_id=persisted.id, sales_contract_id=sc_id, created_at=created_at)
        )
    return SalesMatchProposalResult(match_case=persisted, created=True)


def propose_sales_payment_match(
    session: Session, *, payment_id: uuid.UUID, sales_contract_ids: Sequence[uuid.UUID], created_at: datetime
) -> SalesMatchProposalResult:
    """The `IN` receipt twin of `propose_sales_invoice_match`."""
    payment = PaymentRepository(session).get(payment_id)
    if payment is None:
        raise SalesMatchError(f"Payment {payment_id} not found")
    if payment.direction != PaymentDirection.IN:
        raise SalesMatchError(
            f"Payment {payment_id} has direction {payment.direction!r} — only IN payments may enter a "
            "sales MatchCase; use the procurement match path for OUT payments"
        )
    _validate_candidate_ids(session, sales_contract_ids)

    match_case_repo = MatchCaseRepository(session)
    candidate_repo = SalesMatchCandidateRepository(session)
    existing = match_case_repo.find_by_subject(SubjectType.PAYMENT, payment_id)
    if existing is not None:
        return _replay_or_conflict_proposal(existing, candidate_repo, sales_contract_ids)

    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.PAYMENT,
        subject_id=payment_id,
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
        match_method=MatchMethod.MANUAL_SALES_SCOPE,
        created_at=created_at,
        resolved_at=None,
    )
    try:
        inserted = match_case_repo.add_if_no_case_for_subject(match_case)
    except OperationalError as exc:
        if is_database_busy(exc):
            raise SalesMatchConflict(
                f"Payment {payment_id} could not be proposed due to concurrent database contention — retry"
            ) from exc
        raise
    if not inserted:
        existing = match_case_repo.find_by_subject(SubjectType.PAYMENT, payment_id)
        assert existing is not None
        return _replay_or_conflict_proposal(existing, candidate_repo, sales_contract_ids)

    persisted = match_case_repo.get(match_case.id)
    assert persisted is not None
    for sc_id in sales_contract_ids:
        candidate_repo.add(
            SalesMatchCandidate(id=uuid.uuid4(), match_case_id=persisted.id, sales_contract_id=sc_id, created_at=created_at)
        )
    return SalesMatchProposalResult(match_case=persisted, created=True)


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def confirm_sales_invoice_match(
    session: Session,
    *,
    match_case_id: uuid.UUID,
    allocations: Sequence[tuple[uuid.UUID, Decimal]],
    created_at: datetime,
) -> SalesMatchConfirmationResult:
    """One call covers the WHOLE confirmation — `allocations` is the
    complete `[(sales_contract_id, amount), ...]` set for this subject,
    never a single-target call that would structurally prevent
    allocating across several `SalesContract`s. Validates every target
    and amount BEFORE writing anything; writes every
    `SalesInvoiceAllocation` and resolves the `MatchCase` atomically
    (via a SAVEPOINT — see the race-handling below), never leaving a
    partial allocation set behind."""
    match_case_repo = MatchCaseRepository(session)
    match_case = match_case_repo.get(match_case_id)
    if match_case is None:
        raise SalesMatchError(f"MatchCase {match_case_id} not found")
    if match_case.subject_type != SubjectType.INVOICE:
        raise SalesMatchError(f"MatchCase {match_case_id} subject_type is {match_case.subject_type}, not INVOICE")

    invoice = InvoiceRepository(session).get(match_case.subject_id)
    if invoice is None:
        raise SalesMatchError(f"Invoice {match_case.subject_id} not found")
    if invoice.direction != InvoiceDirection.SALES:
        raise SalesMatchError(
            f"Invoice {invoice.id} has direction {invoice.direction!r} — the sales confirmation path only "
            "accepts SALES invoices"
        )

    pairs = _validate_allocation_pairs(session, allocations)
    allocation_repo = SalesInvoiceAllocationRepository(session)

    if match_case.status == MatchCaseStatus.RESOLVED:
        existing = [a for a in allocation_repo.list_for_invoice(invoice.id) if a.match_case_id == match_case_id]
        existing_pairs = {(a.sales_contract_id, a.allocated_gross_amount) for a in existing}
        if existing_pairs == set(pairs):
            return SalesMatchConfirmationResult(match_case=match_case, allocations=existing, replay=True)
        raise SalesMatchConflict(
            f"MatchCase {match_case_id} is already RESOLVED with a different allocation set — never silently "
            "appends a second authoritative allocation set"
        )
    if match_case.status != MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED:
        raise SalesMatchError(f"MatchCase {match_case_id} is {match_case.status}, not HUMAN_CONFIRMATION_REQUIRED")

    total = sum((amount for _, amount in pairs), Decimal("0"))
    already_allocated = allocation_repo.sum_for_invoice(invoice.id)
    if already_allocated + total > invoice.gross_amount:
        raise SalesMatchError(
            f"allocations totalling {already_allocated + total} would exceed Invoice {invoice.id}'s "
            f"gross_amount {invoice.gross_amount}"
        )

    nested = session.begin_nested()
    try:
        written = []
        for sc_id, amount in pairs:
            allocation = SalesInvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, sales_contract_id=sc_id, match_case_id=match_case_id,
                allocated_gross_amount=amount, confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=created_at,
            )
            allocation_repo.add(allocation)
            written.append(allocation)
        if not match_case_repo.resolve_if_pending(match_case_id, resolved_at=created_at):
            raise _ConfirmationRaceLost()
        nested.commit()
    except _ConfirmationRaceLost:
        nested.rollback()
        raise SalesMatchConflict(f"MatchCase {match_case_id} was confirmed concurrently — refusing to write a second confirmation")
    except MatchCaseNotPendingError as exc:
        # Every OTHER precondition (direction, existence, amount,
        # correspondence, capacity) was already validated above, moments
        # ago — the only thing that can still change between then and
        # this write is a concurrent session resolving this SAME
        # MatchCase first, which `allocation_repo.add()`'s own
        # authoritative status check (BLOCKER 1) surfaces as this SPECIFIC
        # exception type — never a bare `ValueError`, so a genuine bug
        # (e.g. a capacity or amount check this function's own
        # up-front validation should have already caught) is never
        # silently reclassified as "just a race" instead of surfacing
        # honestly.
        nested.rollback()
        raise SalesMatchConflict(
            f"MatchCase {match_case_id} was confirmed concurrently — refusing to write a second confirmation"
        ) from exc
    except OperationalError as exc:
        # A genuine SQLite write-lock timeout under real concurrent
        # sessions (busy_timeout exceeded) is likewise a legitimate
        # "someone else is writing this same data right now" outcome —
        # rolled back cleanly and surfaced the same way, never left as a
        # raw OperationalError the caller of this module never expected.
        nested.rollback()
        if is_database_busy(exc):
            raise SalesMatchConflict(
                f"MatchCase {match_case_id} could not be confirmed due to concurrent database contention — retry"
            ) from exc
        raise
    except ValueError as exc:
        # Any OTHER repository-level rejection (should not normally be
        # reachable, since this function's own up-front validation
        # already checked direction/existence/amount/capacity) — surfaced
        # honestly as a business error, never mislabeled as a conflict.
        nested.rollback()
        raise SalesMatchError(str(exc)) from exc

    resolved_case = match_case_repo.get(match_case_id)
    assert resolved_case is not None
    return SalesMatchConfirmationResult(match_case=resolved_case, allocations=written, created=True)


def confirm_sales_payment_match(
    session: Session,
    *,
    match_case_id: uuid.UUID,
    allocations: Sequence[tuple[uuid.UUID, Decimal]],
    created_at: datetime,
) -> SalesMatchConfirmationResult:
    """The `IN` receipt twin of `confirm_sales_invoice_match`."""
    match_case_repo = MatchCaseRepository(session)
    match_case = match_case_repo.get(match_case_id)
    if match_case is None:
        raise SalesMatchError(f"MatchCase {match_case_id} not found")
    if match_case.subject_type != SubjectType.PAYMENT:
        raise SalesMatchError(f"MatchCase {match_case_id} subject_type is {match_case.subject_type}, not PAYMENT")

    payment = PaymentRepository(session).get(match_case.subject_id)
    if payment is None:
        raise SalesMatchError(f"Payment {match_case.subject_id} not found")
    if payment.direction != PaymentDirection.IN:
        raise SalesMatchError(
            f"Payment {payment.id} has direction {payment.direction!r} — the sales confirmation path only "
            "accepts IN payments"
        )

    pairs = _validate_allocation_pairs(session, allocations)
    allocation_repo = SalesPaymentAllocationRepository(session)

    if match_case.status == MatchCaseStatus.RESOLVED:
        existing = [a for a in allocation_repo.list_for_payment(payment.id) if a.match_case_id == match_case_id]
        existing_pairs = {(a.sales_contract_id, a.allocated_amount) for a in existing}
        if existing_pairs == set(pairs):
            return SalesMatchConfirmationResult(match_case=match_case, allocations=existing, replay=True)
        raise SalesMatchConflict(
            f"MatchCase {match_case_id} is already RESOLVED with a different allocation set"
        )
    if match_case.status != MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED:
        raise SalesMatchError(f"MatchCase {match_case_id} is {match_case.status}, not HUMAN_CONFIRMATION_REQUIRED")

    total = sum((amount for _, amount in pairs), Decimal("0"))
    already_allocated = allocation_repo.sum_for_payment(payment.id)
    if already_allocated + total > payment.amount:
        raise SalesMatchError(
            f"allocations totalling {already_allocated + total} would exceed Payment {payment.id}'s amount "
            f"{payment.amount}"
        )

    nested = session.begin_nested()
    try:
        written = []
        for sc_id, amount in pairs:
            allocation = SalesPaymentAllocation(
                id=uuid.uuid4(), payment_id=payment.id, sales_contract_id=sc_id, match_case_id=match_case_id,
                allocated_amount=amount, confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=created_at,
            )
            allocation_repo.add(allocation)
            written.append(allocation)
        if not match_case_repo.resolve_if_pending(match_case_id, resolved_at=created_at):
            raise _ConfirmationRaceLost()
        nested.commit()
    except _ConfirmationRaceLost:
        nested.rollback()
        raise SalesMatchConflict(f"MatchCase {match_case_id} was confirmed concurrently — refusing to write a second confirmation")
    except MatchCaseNotPendingError as exc:
        nested.rollback()
        raise SalesMatchConflict(
            f"MatchCase {match_case_id} was confirmed concurrently — refusing to write a second confirmation"
        ) from exc
    except OperationalError as exc:
        nested.rollback()
        if is_database_busy(exc):
            raise SalesMatchConflict(
                f"MatchCase {match_case_id} could not be confirmed due to concurrent database contention — retry"
            ) from exc
        raise
    except ValueError as exc:
        nested.rollback()
        raise SalesMatchError(str(exc)) from exc

    resolved_case = match_case_repo.get(match_case_id)
    assert resolved_case is not None
    return SalesMatchConfirmationResult(match_case=resolved_case, allocations=written, created=True)


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


def list_sales_match_cases(session: Session, status: str | None = None) -> list[MatchCase]:
    """docs/PHASE2D1-R0-DECISIONS.md section 2.7's Gate G5 guard #2:
    leg-agnostic listing must not present a sales case as
    procurement-confirmable, and symmetrically the sales listing must
    never surface a procurement case. Filters by the SUBJECT's own
    `direction` field — never by `match_method` name-guessing."""
    match_case_repo = MatchCaseRepository(session)
    cases = match_case_repo.list_by_status(status) if status else match_case_repo.list_all()
    invoice_repo = InvoiceRepository(session)
    payment_repo = PaymentRepository(session)
    result = []
    for case in cases:
        if case.subject_type == SubjectType.INVOICE:
            invoice = invoice_repo.get(case.subject_id)
            if invoice is not None and invoice.direction == InvoiceDirection.SALES:
                result.append(case)
        elif case.subject_type == SubjectType.PAYMENT:
            payment = payment_repo.get(case.subject_id)
            if payment is not None and payment.direction == PaymentDirection.IN:
                result.append(case)
    return result


def list_sales_match_candidates(session: Session, match_case_id: uuid.UUID) -> list[SalesMatchCandidate]:
    return SalesMatchCandidateRepository(session).list_for_case(match_case_id)


def list_sales_invoice_allocations_for_invoice(session: Session, invoice_id: uuid.UUID) -> list[SalesInvoiceAllocation]:
    return SalesInvoiceAllocationRepository(session).list_for_invoice(invoice_id)


def list_sales_invoice_allocations_for_sales_contract(
    session: Session, sales_contract_id: uuid.UUID
) -> list[SalesInvoiceAllocation]:
    return SalesInvoiceAllocationRepository(session).list_for_sales_contract(sales_contract_id)


def list_sales_payment_allocations_for_payment(session: Session, payment_id: uuid.UUID) -> list[SalesPaymentAllocation]:
    return SalesPaymentAllocationRepository(session).list_for_payment(payment_id)


def list_sales_payment_allocations_for_sales_contract(
    session: Session, sales_contract_id: uuid.UUID
) -> list[SalesPaymentAllocation]:
    return SalesPaymentAllocationRepository(session).list_for_sales_contract(sales_contract_id)

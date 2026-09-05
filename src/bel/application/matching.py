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

Business-owner-confirmed chronological allocation (supersedes the
former "no sequence guessing / multiple candidates => HCR" rule — see
docs/PHASE2A-DECISIONS.md):

  Explicit/authoritative decisions come first: a subject that already
  has a MatchCase of any status (AUTO_CONFIRMED, RESOLVED human
  decision, HCR, ...) is never reconsidered or reassigned.

  Otherwise, candidate Contracts are considered in business
  chronological order (contract_date ASC; deterministic stable tie-break
  within a date), and eligible subjects are processed in their own
  business chronological order (invoice issue_date ASC / payment
  transaction_date ASC; same deterministic tie-break). For a subject
  with several EXACTLY equivalent candidates (same counterparty + same
  amount), BEL allocates it to the EARLIEST candidate that still has
  sufficient remaining capacity, marks the allocation with
  AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_CHRONOLOGICAL and
  AUTO_CONFIRMs the MatchCase. AUTO_CONFIRMED here means BEL
  deterministically applied the confirmed business rule — NOT that source
  Evidence explicitly proved that one-to-one historical relationship.

  The candidate snapshot is still computed as a pure function of a single
  static contract snapshot (spec section 20's two-phase shape): what
  changed is the DECISION for multiple equivalent candidates, not how a
  candidate set is built. The capacity guard is never weakened: if no
  candidate has sufficient remaining capacity, the subject is NOT
  allocated — it goes to the existing HUMAN_CONFIRMATION_REQUIRED +
  ALLOCATION_CAPACITY_EXCEEDED protection path, never a silent
  over-allocation.

  "Effective uniqueness" must be established independently of any
  chronological allocation created in the SAME unresolved cohort this
  run — never manufactured by this run's own processing order. A missing
  date sorted last is not evidence of anything: if two or more unresolved
  subjects share the same normalized counterparty + exact amount (so they
  share the same static candidate Contract pool), letting a dated member
  consume capacity first and then treating the narrowed leftover as
  "unique" for an undated sibling would fabricate a chronology the data
  never established. `_run_match_pass` therefore snapshots each such
  cohort BEFORE any allocation: if every competing subject has a real
  business date AND every Contract still able to accept one of them
  (by pre-existing, pre-run authoritative capacity alone) has a real
  contract_date, the cohort's chronology is well-defined and normal
  greedy allocation proceeds; otherwise NONE of the cohort's members are
  chronologically allocated this run — not even the dated ones — and each
  goes to HUMAN_CONFIRMATION_REQUIRED with the full static candidate list.
  A single UNRESOLVED subject is never subject to this cohort check (its
  uniqueness, or lack of it, cannot have been manufactured by a sibling),
  and capacity already consumed by a pre-existing authoritative decision
  (from a prior run, or a human confirmation) can still make a cohort — or
  a lone subject — genuinely, independently unique.

match_invoices() and match_payments() share one core loop
(_run_match_pass) parameterized by the handful of things that actually
differ between an Invoice and a Payment subject — not a generic rule
engine, just avoiding two near-identical 150-line copies of the same
M001 mechanics.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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


def normalized_contract_counterparties(contracts: Sequence[Contract]) -> set[str]:
    """The canonical eligibility counterparty set for the current
    procurement scope: the normalized counterparties of every Contract.
    M001 eligibility (``_is_eligible``) is membership in this set, and the
    first-stage cutover Payment-scope filter reuses the SAME function, so
    matching and cutover backfill can never drift on what a
    "contract-related" bank counterparty is."""
    return {n for c in contracts if (n := normalize_counterparty(c.counterparty)) is not None}


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


def _contract_chronological_key(contract: Contract) -> tuple:
    """A STABLE snapshot/enumeration order only (candidate lists are built
    from this snapshot): dated Contracts in contract_date ASC order, then
    undated ones, then contract_no, then UUID. This is NOT the
    chronological decision — `_run_match_pass` only ever chooses the
    earliest Contract when EVERY competing candidate has a real
    contract_date; an undated Contract among multiple valid candidates
    forces HUMAN_CONFIRMATION_REQUIRED and is never treated as
    "earliest/latest" by technical ordering."""
    return (
        0 if contract.contract_date is not None else 1,
        contract.contract_date if contract.contract_date is not None else date.min,
        contract.contract_no or "",
        contract.id,
    )


def _first_usable_key(*values: str | None) -> str | None:
    """First non-blank value in priority order, or None if every value is
    missing/blank. Whitespace-only values do not count as usable — a
    stale-but-present empty string must fall through to the next key in the
    chain exactly like a NULL would."""
    for value in values:
        if value is not None and value.strip():
            return value
    return None


def _invoice_chronological_key(invoice) -> tuple:
    """PURCHASE Invoice enumeration/processing order: issue_date ASC (undated
    last), then a stable business/source identifier — external_invoice_key,
    then digital_invoice_no, then invoice_no — and UUID only as the final
    fallback when none of those three are usable. This is an enumeration
    order only, not a chronological DECISION (see _run_match_pass); it just
    needs to be deterministic and never technical-order-as-if-it-were-a-date."""
    business_key = _first_usable_key(invoice.external_invoice_key, invoice.digital_invoice_no, invoice.invoice_no)
    return (
        0 if invoice.issue_date is not None else 1,
        invoice.issue_date if invoice.issue_date is not None else date.min,
        0 if business_key is not None else 1,
        business_key or "",
        invoice.id,
    )


def _run_match_pass(
    *,
    session: Session,
    subject_type: str,
    subjects: Sequence,
    get_amount: Callable[[object], Decimal],
    get_counterparty: Callable[[object], str | None],
    get_subject_date: Callable[[object], date | None],
    contracts: Sequence[Contract],
    match_case_repo: MatchCaseRepository,
    candidate_repo: MatchCandidateRepository,
    exception_repo: ExceptionRepository,
    event_repo: EventRepository,
    sum_confirmed_for_contract: Callable[[uuid.UUID], Decimal],
    create_allocation: Callable[[object, uuid.UUID, uuid.UUID, str, datetime], None],
    auto_confirmed_event_type: str,
    now: datetime,
) -> MatchRunSummary:
    """See the module docstring — the callers hand ``subjects`` already in
    business chronological order and ``contracts`` already in contract
    chronological order, so ``candidates`` (computed from that snapshot)
    are listed earliest-first and the greedy earliest-with-capacity choice
    below is the confirmed deterministic rule."""
    # Eligibility gate (spec sections 9/14): counterparty membership only,
    # checked before anything else. Subjects that fail this never become
    # a MatchCase — see _is_eligible's docstring.
    contract_counterparties = normalized_contract_counterparties(contracts)
    eligible_subjects = [s for s in subjects if _is_eligible(get_counterparty(s), contract_counterparties)]

    summary = MatchRunSummary()
    summary.eligible_total = len(eligible_subjects)
    summary.out_of_scope = len(subjects) - len(eligible_subjects)

    # Pass 1: pure candidate computation for every ELIGIBLE subject, no
    # writes. Candidate order == the chronological `contracts` snapshot
    # order the caller supplied.
    candidates_by_subject = {
        s.id: _find_candidate_contract_ids(get_amount(s), get_counterparty(s), contracts) for s in eligible_subjects
    }
    contract_by_id = {c.id: c for c in contracts}

    # Remaining capacity per candidate contract, seeded lazily from the DB
    # (authoritative existing allocations — human-confirmed included) and
    # decremented in-memory as THIS pass allocates, so the greedy choice is
    # deterministic and never reads a half-committed sibling.
    remaining_capacity: dict[uuid.UUID, Decimal] = {}

    def _remaining(cid: uuid.UUID) -> Decimal:
        if cid not in remaining_capacity:
            contract = contract_by_id[cid]
            remaining_capacity[cid] = contract.gross_amount - sum_confirmed_for_contract(cid)
        return remaining_capacity[cid]

    def _add_hcr_candidates(contract_ids: Sequence[uuid.UUID], match_case_id: uuid.UUID) -> None:
        # MatchCandidate rows reference match_case_id — flush first so the
        # FK is satisfied (no relationship() -> no automatic insert
        # ordering). Same lesson as every other writer in this module.
        session.flush()
        for cid in contract_ids:
            candidate_repo.add(
                MatchCandidate(id=uuid.uuid4(), match_case_id=match_case_id, contract_id=cid, created_at=now)
            )

    # Which eligible subjects are still unresolved (no existing MatchCase of
    # any status)? Explicit/authoritative decisions and their capacity
    # effects are preserved untouched (already reflected in
    # sum_confirmed_for_contract / _remaining above) and are never
    # reconsidered here.
    unresolved_subjects: list = []
    for subject in eligible_subjects:
        if match_case_repo.find_by_subject(subject_type, subject.id) is not None:
            summary.already_matched_skipped += 1
            continue
        unresolved_subjects.append(subject)

    # Static cohort snapshot (docs/PHASE2A-DECISIONS.md): unresolved
    # subjects sharing the same normalized counterparty + exact amount
    # share the exact same static candidate Contract pool (a candidate also
    # requires contract.gross_amount == subject amount), so they are the
    # unit of competition for shared capacity. A cohort with 2+ members can
    # only run chronological fallback allocation THIS pass when it is well
    # -defined for every member: every competing subject has a real
    # business date AND every Contract that could still accept one of them
    # BEFORE this run (i.e. by pre-existing authoritative capacity alone)
    # has a real contract_date. Otherwise NONE of the cohort's members are
    # chronologically allocated this run — not even the dated ones —
    # because letting dated members consume capacity first would let a
    # later (possibly undated) member see a narrowed, "effectively unique"
    # candidate set that was itself CREATED by this run's own undefined
    # ordering. That is not real effective uniqueness (see the module
    # docstring); it is the manufactured-chronology bug this guards
    # against. Capacity already consumed by pre-existing authoritative
    # decisions is unaffected by this check — it can still make a cohort
    # (or a lone subject) genuinely, independently unique.
    cohorts: dict[tuple, list] = {}
    for subject in unresolved_subjects:
        cohort_key = (normalize_counterparty(get_counterparty(subject)), get_amount(subject))
        cohorts.setdefault(cohort_key, []).append(subject)

    blocked_subject_ids: set[uuid.UUID] = set()
    for members in cohorts.values():
        if len(members) < 2:
            # No competition — a lone subject's uniqueness (or lack of it)
            # cannot have been manufactured by this run's own ordering.
            continue
        candidates = candidates_by_subject[members[0].id]  # identical for every member of this cohort
        if not candidates:
            continue
        cohort_amount = get_amount(members[0])
        initially_viable = [cid for cid in candidates if cohort_amount <= _remaining(cid)]
        if not initially_viable:
            continue
        subjects_dated = all(get_subject_date(s) is not None for s in members)
        contracts_dated = all(contract_by_id[cid].contract_date is not None for cid in initially_viable)
        if not (subjects_dated and contracts_dated):
            blocked_subject_ids.update(s.id for s in members)

    # Pass 2: process unresolved subjects in business chronological order.
    # Chronology is only ever REQUIRED to choose between more than one valid
    # candidate — it is never fabricated from a NULL date (see the module
    # docstring and docs/PHASE2A-DECISIONS.md). A subject in
    # blocked_subject_ids belongs to a cohort whose chronological order is
    # undefined this run: it goes straight to HUMAN_CONFIRMATION_REQUIRED
    # with its full static candidate list, consuming no capacity.
    for subject in unresolved_subjects:
        candidates = candidates_by_subject[subject.id]
        amount = get_amount(subject)
        match_case_id = uuid.uuid4()

        if len(candidates) == 0:
            # Eligible (counterparty is a contract party) but no exact
            # counterparty+amount Contract correspondence.
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
            summary.unmatched += 1
            summary.subject_ids.append(subject.id)
            continue

        if subject.id in blocked_subject_ids:
            # This subject's cohort has 2+ unresolved competing members and
            # an undefined chronological order this run (see the cohort
            # snapshot above) — never let it fall through to the dynamic
            # viable-candidate computation below, which would consume
            # capacity and could manufacture "effective uniqueness" for a
            # sibling processed later in this same pass. Goes straight to
            # the undefined-chronology HCR outcome; no capacity is touched.
            chosen_contract_id = None
        else:
            # Viable = exact candidates that can still accept this subject.
            viable = [cid for cid in candidates if amount <= _remaining(cid)]

            if not viable:
                # No candidate has sufficient remaining capacity — the
                # deterministic rule cannot allocate. Preserve the existing
                # protection: HCR + candidates + capacity exception. NEVER a
                # silent over-allocation.
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
                _add_hcr_candidates(candidates, match_case_id)
                first = contract_by_id[candidates[0]]
                already_allocated = first.gross_amount - _remaining(candidates[0])
                exception_repo.add(
                    TaskException(
                        id=uuid.uuid4(),
                        exception_type=ExceptionType.ALLOCATION_CAPACITY_EXCEEDED,
                        status=ExceptionStatus.OPEN,
                        summary=(
                            f"{subject_type} {subject.id}: no candidate has sufficient remaining capacity for {amount}"
                        ),
                        detail={
                            "subject_type": subject_type,
                            "subject_id": str(subject.id),
                            "contract_id": str(candidates[0]),
                            "already_allocated": str(already_allocated),
                            "attempted_amount": str(amount),
                            "contract_gross_amount": str(first.gross_amount),
                        },
                        created_at=now,
                    )
                )
                event_repo.add(
                    BusinessEvent(
                        id=uuid.uuid4(),
                        event_type=BusinessEventType.ALLOCATION_CAPACITY_EXCEEDED,
                        occurred_at=now,
                        payload={"subject_type": subject_type, "subject_id": str(subject.id), "contract_id": str(candidates[0])},
                    )
                )
                summary.capacity_exceeded += 1
                summary.subject_ids.append(subject.id)
                continue

            # UNIQUE / EFFECTIVELY UNIQUE: chronology is not needed when no
            # real choice remains — a missing contract_date / issue_date
            # never blocks an otherwise deterministic single-candidate
            # allocation.
            if len(viable) == 1:
                chosen_contract_id = viable[0]
            else:
                # MULTIPLE valid candidates => chronological fallback is the
                # decision. Only allocate if chronology is genuinely
                # business-defined for every competing alternative; otherwise
                # leave the case to human review (never sort NULL first/last,
                # never substitute created_at / import order / UUID).
                if any(contract_by_id[cid].contract_date is None for cid in viable):
                    chosen_contract_id = None
                elif get_subject_date(subject) is None:
                    chosen_contract_id = None
                else:
                    chosen_contract_id = min(
                        viable,
                        key=lambda cid: (
                            contract_by_id[cid].contract_date,
                            contract_by_id[cid].contract_no or "",
                            cid,
                        ),
                    )

        if chosen_contract_id is None:
            # Chronology unavailable (a competing Contract has no
            # contract_date, or this subject has no business date to be
            # sequenced by) — HCR with all candidates; nothing allocated.
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
            _add_hcr_candidates(candidates, match_case_id)
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

        remaining_capacity[chosen_contract_id] -= amount
        # A multi-candidate decision is chronological allocation, never
        # mislabelled as a unique-candidate decision.
        match_method = (
            AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_CHRONOLOGICAL
            if len(candidates) > 1
            else AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE
        )
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
            MatchCandidate(id=uuid.uuid4(), match_case_id=match_case_id, contract_id=chosen_contract_id, created_at=now)
        )
        create_allocation(subject, chosen_contract_id, match_case_id, match_method, now)
        event_repo.add(
            BusinessEvent(
                id=uuid.uuid4(),
                event_type=auto_confirmed_event_type,
                occurred_at=now,
                payload={"subject_id": str(subject.id), "contract_id": str(chosen_contract_id), "amount": str(amount)},
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
    contracts.sort(key=_contract_chronological_key)
    eligible = [inv for inv in invoice_repo.list_all() if inv.direction == InvoiceDirection.PURCHASE]
    # Dated subjects in issue_date ASC order for greedy processing, undated
    # subjects after (enumeration only). An undated Invoice is never
    # auto-allocated when it would need subject chronology — see
    # _run_match_pass. Within the same issue_date a stable business/source
    # identifier comes before UUID — see _invoice_chronological_key.
    eligible.sort(key=_invoice_chronological_key)

    def create_allocation(
        invoice, contract_id: uuid.UUID, match_case_id: uuid.UUID, match_method: str, now: datetime
    ) -> None:
        allocation_repo.add(
            InvoiceAllocation(
                id=uuid.uuid4(),
                invoice_id=invoice.id,
                contract_id=contract_id,
                match_case_id=match_case_id,
                allocated_gross_amount=invoice.gross_amount,
                match_method=match_method,
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
        get_subject_date=lambda inv: inv.issue_date,
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
    contracts.sort(key=_contract_chronological_key)
    eligible = [p for p in payment_repo.list_all() if p.direction == PaymentDirection.OUT]
    # transaction_date ASC (domain-required); same-date payments tie-break
    # on the bank reference (a real source component) before UUID.
    eligible.sort(
        key=lambda p: (
            0 if p.transaction_date is not None else 1,
            p.transaction_date if p.transaction_date is not None else date.min,
            p.bank_reference or "",
            p.id,
        )
    )

    def create_allocation(
        payment, contract_id: uuid.UUID, match_case_id: uuid.UUID, match_method: str, now: datetime
    ) -> None:
        allocation_repo.add(
            PaymentAllocation(
                id=uuid.uuid4(),
                payment_id=payment.id,
                contract_id=contract_id,
                match_case_id=match_case_id,
                allocated_amount=payment.amount,
                match_method=match_method,
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
        get_subject_date=lambda p: p.transaction_date,
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

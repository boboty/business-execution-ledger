"""SalesContract Fact Maintenance (Phase 2D.1-R3a, Slice 1).

Establishes the everyday business intake path for `SalesContract` — the
sales-side twin of `Contract` (docs/DOMAIN.md, docs/PHASE2D1-R0-DECISIONS.md
section 2.2), the only place an external sales customer is expressed.
This module reuses, deliberately and explicitly, the SAME pattern
`bel.application.contract_item_facts` / `bel.application.shipment_facts`
established and had validated across the Phase 2D.1-R1/R2 Codex fix
rounds — not a generic Fact revision engine, a third copy of the same
three explicit business intents:

    sales-side Evidence
          |
          v
    create_sales_contract_fact       — a SalesContract did not exist before
    supplement_sales_contract_fact   — a previously-unknown field becomes known
    correct_sales_contract_fact      — a previously-asserted value was wrong
          |
          v
    SalesContractRevision (anchor + revision model, docs/PHASE2D1-R0-DECISIONS.md 1.3)
          |
          v
    query / read model (get, find_by_identity, list, history)

Frozen semantics this module implements (docs/PHASE2D1-R0-DECISIONS.md):

- 2.1/2.3 — `Contract.buyer` is our own entity and is NEVER used as a
  sales customer key; neither is a sales-scope reference number, a
  customs-receiving party, nor `Contract.counterparty`. This module has
  no code path that reads any `Contract` field at all — the ONLY
  customer source is the `customer` value the caller passes, which must
  itself originate from sales-side Evidence (an application-layer
  discipline this module cannot enforce mechanically, only refuse to
  make easier: there is no `contract_id` parameter anywhere here).
- 2.2 — `our_entity` and `sales_contract_no` are the frozen business
  identity (4.4) and therefore live on the anchor, immutable after
  creation — exactly like ContractItem's `(contract_id, source_item_key)`
  and Shipment's `(contract_id, external_reference, execution_date)`.
  `customer`, `currency`, `gross_amount`, `contract_date` are the
  correctable business values, living on `SalesContractRevision`.
- 2.3 — a SalesContract may legitimately exist with `customer = NULL`.
  Creating one raises a persisted, idempotent
  `SALES_CONTRACT_CUSTOMER_UNRESOLVED` Task; the Task resolves
  (`ExceptionStatus.RESOLVED`) the moment a `SUPPLEMENT` fills in
  `customer` — reusing the SUPPLEMENT machinery already frozen for this
  purpose, never a second mechanism.
- 4.4 — business identity is `(our_entity, sales_contract_no)`.
  `sales_contract_no` and/or `our_entity` missing -> NO canonical anchor
  may ever be created (unlike Shipment's nullable `external_reference`,
  there is no confirmation override for this — the identity is genuinely
  required). Evidence is preserved and a persisted, idempotent
  `SALES_CONTRACT_IDENTITY_INCOMPLETE` Task is raised instead.
  Conflicting business facts under one identity -> `BusinessKeyConflict`
  (the R004 pattern, reused by exception_type rather than inventing a
  parallel constant) persisted idempotently; the existing anchor/revision
  is left completely unchanged.

Idempotent replay requires ALL of same business identity/anchor + same
Evidence fragment + same revision intent (revision_type) + same asserted
field/value content — never merely a fragment-id match.
`asserted_field_names` is captured verbatim at write time (never
reconstructed by diffing against the predecessor).

What this module deliberately does NOT do: it does not let a caller
change `our_entity` or `sales_contract_no` (re-identification is out of
scope, exactly like ContractItem/Shipment's identity fields — simply
never members of `SALES_CONTRACT_FACT_FIELDS`, so supplement/correct
reject them outright as unknown fields); it does not create a
`ProcurementSalesLink` (that is Slice 2's job entirely — this module has
no knowledge of procurement `Contract` at all); it does not implement
`SalesInvoiceAllocation`/`SalesPaymentAllocation`/`SalesMatchCandidate`
(R3b); and it does not add a generic Fact revision engine — the three
functions below (plus their `execute_*` wrappers) are the whole surface.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionStatus, ExceptionType, TaskException
from bel.domain.sales_contract import (
    SALES_CONTRACT_FACT_FIELDS,
    SalesContract,
    SalesContractRevision,
    SalesContractRevisionType,
)
from bel.infrastructure.persistence.database import serialized_write_transaction
from bel.infrastructure.persistence.repositories import EvidenceRepository, ExceptionRepository, SalesContractRepository


class SalesContractFactError(ValueError):
    """A rejected SalesContract Fact operation — missing/unresolvable
    Evidence, or an unknown field name. Surfaces as an explicit failure,
    never a silent partial write."""


class SalesContractFactConflict(SalesContractFactError):
    """An explicit-intent conflict the system will not guess through:
    a supplement targeting an already-known, different value; a
    correction targeting a field with no existing value; a
    supplement/correction that no longer targets the current revision;
    or a create whose incoming assertion conflicts with an existing
    anchor's. A human must resolve this, never an inferred merge."""


class SalesContractIdentityIncomplete(SalesContractFactError):
    """Raised when `create_sales_contract_fact` is called with a missing
    `our_entity` and/or `sales_contract_no`. Per
    docs/PHASE2D1-R0-DECISIONS.md section 4.4 ("NO canonical anchor may
    be created"), this is unconditional — unlike Shipment's nullable
    `external_reference`, there is no confirmation override. Evidence is
    preserved and a persisted `SALES_CONTRACT_IDENTITY_INCOMPLETE` Task
    is raised, idempotently keyed by `source_fragment_id`."""


@dataclass(frozen=True)
class SalesContractFactResult:
    """Mirrors `ShipmentFactResult`/`ContractItemFactResult` — see their
    docstrings for the full rationale. `created` / `revision_written` /
    `replay` / `corroborating` are independent flags because "nothing new
    was written" is not one outcome."""

    sales_contract: SalesContract
    created: bool = False
    revision_written: bool = True
    replay: bool = False
    corroborating: bool = False


def _validate_fields(fields: dict[str, Any]) -> None:
    unknown = sorted(set(fields) - set(SALES_CONTRACT_FACT_FIELDS))
    if unknown:
        raise SalesContractFactError(f"unknown SalesContract field(s): {unknown}")
    # Gate 2D.1-R3a Slice 1 fix round, BLOCKER 2: `None` is never a valid
    # asserted value — a caller who wants a field left unset must omit it
    # from `fields` entirely (exactly what the CLI's
    # `_sales_contract_fields_from_options` already does). Without this
    # guard, `supplement_sales_contract_fact(fields={"customer": None})`
    # would write a no-op revision AND incorrectly resolve the
    # unresolved-customer Task even though `customer` never actually
    # became known.
    none_valued = sorted(key for key, value in fields.items() if value is None)
    if none_valued:
        raise SalesContractFactError(
            f"field(s) {none_valued} were passed as None — omit a field entirely if it is not being "
            "asserted this call; None is never a valid asserted value"
        )


def _revision_values(revision: SalesContractRevision) -> dict[str, Any]:
    return {field: getattr(revision, field) for field in SALES_CONTRACT_FACT_FIELDS}


def _normalized(fields: dict[str, Any]) -> dict[str, Any]:
    return {field: fields.get(field) for field in SALES_CONTRACT_FACT_FIELDS}


def _require_fragment(session: Session, source_fragment_id: uuid.UUID) -> None:
    if EvidenceRepository(session).get_fragment(source_fragment_id) is None:
        raise SalesContractFactError(f"EvidenceFragment {source_fragment_id} not found")


def _asserted_fields(session: Session, revision: SalesContractRevision) -> dict[str, Any]:
    """Mirrors `contract_item_facts._asserted_fields` /
    `shipment_facts._asserted_fields` exactly — reads the persisted
    `asserted_field_names` (there is no legacy SalesContract data with
    `asserted_field_names = NULL` to fall back for, since this is a
    brand-new object; the fallback path exists only for structural
    parity with the shared pattern, never expected to trigger in
    practice)."""
    if revision.asserted_field_names is not None:
        values = _revision_values(revision)
        return {field: values[field] for field in revision.asserted_field_names}

    predecessor = SalesContractRepository(session).find_predecessor(revision.id)
    values = _revision_values(revision)
    if predecessor is None:
        return {field: value for field, value in values.items() if value is not None}
    predecessor_values = _revision_values(predecessor)
    return {field: value for field, value in values.items() if value != predecessor_values[field]}


def _find_identity_incomplete_task(session: Session, source_fragment_id: uuid.UUID) -> TaskException | None:
    """Idempotency check: the SAME Evidence fragment resubmitted with a
    missing identity component must not raise a second
    `SALES_CONTRACT_IDENTITY_INCOMPLETE` Task."""
    for task in ExceptionRepository(session).list_open():
        if (
            task.exception_type == ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE
            and task.detail.get("source_fragment_id") == str(source_fragment_id)
        ):
            return task
    return None


def _find_business_key_conflict_task(
    session: Session, *, sales_contract_id: uuid.UUID, conflicting_source_fragment_id: uuid.UUID
) -> TaskException | None:
    """Idempotency check, scoped by OUR OWN detail keys
    (`sales_contract_id`) so this never matches an unrelated procurement
    `BusinessKeyConflict` Task (which carries `contract_no`/`contract_ids`
    instead — see import_contract_ledger.py). The SAME conflicting
    Evidence fragment resubmitted against the SAME existing anchor must
    not raise a second Task."""
    for task in ExceptionRepository(session).list_open():
        if (
            task.exception_type == ExceptionType.BUSINESS_KEY_CONFLICT
            and task.detail.get("sales_contract_id") == str(sales_contract_id)
            and task.detail.get("conflicting_source_fragment_id") == str(conflicting_source_fragment_id)
        ):
            return task
    return None


def _flag_unresolved_customer(session: Session, sales_contract_id: uuid.UUID, created_at: datetime) -> None:
    """docs/PHASE2D1-R0-DECISIONS.md section 2.3: a SalesContract created
    with `customer = NULL` gets a persisted, idempotent unresolved-customer
    Task. Idempotent by `sales_contract_id` — an anchor can only be
    created once, so this is defence-in-depth consistent with the
    established pattern, not something expected to fire twice in
    practice."""
    for task in ExceptionRepository(session).list_open():
        if (
            task.exception_type == ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED
            and task.detail.get("sales_contract_id") == str(sales_contract_id)
        ):
            return
    ExceptionRepository(session).add(
        TaskException(
            id=uuid.uuid4(),
            exception_type=ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED,
            status=ExceptionStatus.OPEN,
            summary=f"SalesContract {sales_contract_id} has no customer",
            detail={"sales_contract_id": str(sales_contract_id)},
            created_at=created_at,
        )
    )
    session.flush()


def _resolve_unresolved_customer_task(session: Session, sales_contract_id: uuid.UUID) -> None:
    """Closes the loop (docs/V1-SCOPE.md section 5.2: "Task resolves")
    once a SUPPLEMENT fills in `customer`. Reuses the existing
    `TaskException.status` field via `ExceptionRepository.update_status`
    — no new Task lifecycle machinery."""
    for task in ExceptionRepository(session).list_open():
        if (
            task.exception_type == ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED
            and task.detail.get("sales_contract_id") == str(sales_contract_id)
        ):
            ExceptionRepository(session).update_status(task.id, ExceptionStatus.RESOLVED)


# ---------------------------------------------------------------------------
# Core commands — session-transaction-agnostic, per contract_item_facts.py's
# split: these never commit. execute_* below owns the CLI/Web transaction.
# ---------------------------------------------------------------------------


def create_sales_contract_fact(
    session: Session,
    *,
    our_entity: str | None,
    sales_contract_no: str | None,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> SalesContractFactResult:
    """Case A (docs/PHASE2D1-R0-DECISIONS.md 1.1): a SalesContract did not
    exist before. `fields` may include `customer`, `currency`,
    `gross_amount`, `contract_date` — all optional.

    If `our_entity` or `sales_contract_no` is missing, NO anchor is ever
    created (section 4.4: "NO canonical anchor may be created") — this
    raises `SalesContractIdentityIncomplete` after persisting a Task,
    idempotently keyed by `source_fragment_id`.

    Otherwise, a duplicate `(our_entity, sales_contract_no)` is resolved
    the same four-outcome way validated for ContractItem/Shipment:

    1. Same Evidence fragment, same assertion -> EXACT REPLAY
       (`replay=True`). Nothing new is written.
    2. Different Evidence fragment, same assertion -> CORROBORATING
       (`corroborating=True`). Not a replay, not a conflict, not a
       second INITIAL revision.
    3. Different Evidence fragment, conflicting assertion -> the
       existing anchor/revision is left completely unchanged, a
       persisted `BUSINESS_KEY_CONFLICT` Task is raised (idempotently
       keyed by `(sales_contract_id, conflicting source_fragment_id)`),
       and `SalesContractFactConflict` is raised to the caller.
    4. Same Evidence fragment, DIFFERENT assertion -> also
       `SalesContractFactConflict` (no persisted Task — a malformed
       replay of the SAME artifact, not new conflicting Evidence).

    A genuinely new anchor with `customer = None` also raises a
    persisted, idempotent `SALES_CONTRACT_CUSTOMER_UNRESOLVED` Task
    (section 2.3)."""
    _validate_fields(fields)
    _require_fragment(session, source_fragment_id)

    repo = SalesContractRepository(session)
    incoming_assertion = {key: value for key, value in fields.items() if value is not None}

    if not our_entity or not sales_contract_no:
        existing_task = _find_identity_incomplete_task(session, source_fragment_id)
        if existing_task is None:
            # Gate 2D.1-R3a Slice 1 fix round, BLOCKER 1: a persisted Task
            # must never carry the asserted entity name, contract number,
            # or any other field value — only safe identifiers (the
            # Evidence fragment id) and WHICH identity fields are missing
            # (booleans, not their would-be values, since a missing
            # component has no value to leak in the first place — the
            # concern is the CO-PRESENT `fields`/`our_entity`/
            # `sales_contract_no` payload previously stored here).
            ExceptionRepository(session).add(
                TaskException(
                    id=uuid.uuid4(),
                    exception_type=ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE,
                    status=ExceptionStatus.OPEN,
                    summary="SalesContract create is missing our_entity and/or sales_contract_no",
                    detail={
                        "source_fragment_id": str(source_fragment_id),
                        "missing_our_entity": not bool(our_entity),
                        "missing_sales_contract_no": not bool(sales_contract_no),
                    },
                    created_at=created_at,
                )
            )
            session.flush()
        raise SalesContractIdentityIncomplete(
            "SalesContract create is missing our_entity and/or sales_contract_no — "
            "docs/PHASE2D1-R0-DECISIONS.md section 4.4: NO canonical anchor may be created. "
            "Evidence preserved, a Task has been raised; resolve manually with a complete identity."
        )

    existing = repo.find_by_identity(our_entity, sales_contract_no)
    if existing is not None:
        initial_revision = repo.get_initial_revision(existing.id)
        assert initial_revision is not None
        existing_assertion = _asserted_fields(session, initial_revision)
        same_fragment = initial_revision.source_fragment_id == source_fragment_id
        if incoming_assertion == existing_assertion:
            return SalesContractFactResult(
                sales_contract=existing,
                created=False,
                revision_written=False,
                replay=same_fragment,
                corroborating=not same_fragment,
            )
        if same_fragment:
            raise SalesContractFactConflict(
                f"SalesContract {existing.id}: source_fragment_id {source_fragment_id} was already used to "
                f"assert {existing_assertion!r} as the INITIAL revision — this call asserts "
                f"{incoming_assertion!r}, a different content under the SAME Evidence, which cannot both be true"
            )
        existing_conflict_task = _find_business_key_conflict_task(
            session, sales_contract_id=existing.id, conflicting_source_fragment_id=source_fragment_id
        )
        if existing_conflict_task is None:
            # Gate 2D.1-R3a Slice 1 fix round, BLOCKER 1: no entity name,
            # contract number, or asserted VALUE (customer name,
            # currency, amount, date) may be persisted into Task.detail —
            # only safe identifiers (anchor id, fragment ids) and WHICH
            # field names disagree, never what they disagree TO.
            conflicting_fields = sorted(
                key
                for key in set(existing_assertion) | set(incoming_assertion)
                if existing_assertion.get(key) != incoming_assertion.get(key)
            )
            ExceptionRepository(session).add(
                TaskException(
                    id=uuid.uuid4(),
                    exception_type=ExceptionType.BUSINESS_KEY_CONFLICT,
                    status=ExceptionStatus.OPEN,
                    summary=f"SalesContract {existing.id} has conflicting Evidence under the same business identity",
                    detail={
                        "sales_contract_id": str(existing.id),
                        "existing_source_fragment_id": str(initial_revision.source_fragment_id),
                        "conflicting_source_fragment_id": str(source_fragment_id),
                        "conflicting_fields": conflicting_fields,
                    },
                    created_at=created_at,
                )
            )
            session.flush()
        raise SalesContractFactConflict(
            f"SalesContract {existing.id} already asserts {existing_assertion!r}; Evidence "
            f"{source_fragment_id} asserts {incoming_assertion!r} instead — conflicting values under "
            "different Evidence require an explicit supplement (if previously unknown) or correction "
            "(if previously wrong); create never guesses which. A BusinessKeyConflict Task has been "
            "raised; the existing anchor is unchanged."
        )

    anchor_id = uuid.uuid4()
    repo.create_anchor(id=anchor_id, our_entity=our_entity, sales_contract_no=sales_contract_no, created_at=created_at)
    repo.create_initial_revision(
        SalesContractRevision(
            id=uuid.uuid4(),
            sales_contract_id=anchor_id,
            revision_type=SalesContractRevisionType.INITIAL,
            source_fragment_id=source_fragment_id,
            superseded_by_revision_id=None,
            created_at=created_at,
            asserted_field_names=sorted(key for key, value in fields.items() if value is not None),
            **_normalized(fields),
        )
    )
    session.flush()
    created_sales_contract = repo.get(anchor_id)
    assert created_sales_contract is not None
    if created_sales_contract.customer is None:
        _flag_unresolved_customer(session, anchor_id, created_at)
    return SalesContractFactResult(sales_contract=created_sales_contract, created=True, revision_written=True)


def supplement_sales_contract_fact(
    session: Session,
    *,
    sales_contract_id: uuid.UUID,
    based_on_revision_id: uuid.UUID,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> SalesContractFactResult:
    """Case B-supplement: a previously-unknown attribute (`customer`,
    `currency`, `gross_amount`, `contract_date`) becomes known. Every key
    in `fields` must currently be NULL on the current revision —
    supplementing a field that already holds a DIFFERENT value is
    rejected (`SalesContractFactConflict`: that is a correction).
    Resupplying the SAME value is accepted as harmless.

    When `customer` is supplemented, the anchor's open
    `SALES_CONTRACT_CUSTOMER_UNRESOLVED` Task (if any) is marked
    RESOLVED (section 2.3's closed loop) — never on a replay, since
    nothing new was asserted then."""
    result = _apply_revision(
        session,
        sales_contract_id=sales_contract_id,
        based_on_revision_id=based_on_revision_id,
        fields=fields,
        source_fragment_id=source_fragment_id,
        created_at=created_at,
        revision_type=SalesContractRevisionType.SUPPLEMENT,
    )
    # `_validate_fields` now rejects `fields={"customer": None}` outright
    # (Gate 2D.1-R3a Slice 1 fix round, BLOCKER 2), so `result.sales_contract.customer`
    # cannot be None here from THIS call alone — the `is not None` check is
    # deliberate defence-in-depth against ever resolving the Task while the
    # authoritative projection still shows no customer.
    if result.revision_written and "customer" in fields and result.sales_contract.customer is not None:
        _resolve_unresolved_customer_task(session, sales_contract_id)
    return result


def correct_sales_contract_fact(
    session: Session,
    *,
    sales_contract_id: uuid.UUID,
    based_on_revision_id: uuid.UUID,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> SalesContractFactResult:
    """Case B-correction: a previously-asserted value was wrong. Every
    key in `fields` must currently hold a NON-NULL value on the current
    revision — correcting a field with no existing value is rejected
    (`SalesContractFactConflict`: that is a supplement). Since
    correction never operates on a NULL field, it can never be the path
    that fills in `customer` for the first time — that is always
    `supplement_sales_contract_fact`'s job, and only that path resolves
    the unresolved-customer Task."""
    return _apply_revision(
        session,
        sales_contract_id=sales_contract_id,
        based_on_revision_id=based_on_revision_id,
        fields=fields,
        source_fragment_id=source_fragment_id,
        created_at=created_at,
        revision_type=SalesContractRevisionType.CORRECTION,
    )


def get_sales_contract(session: Session, sales_contract_id: uuid.UUID) -> SalesContract | None:
    """Current authoritative state, for the CLI's `sales-contract show`
    and any future Web equivalent."""
    return SalesContractRepository(session).get(sales_contract_id)


def find_sales_contract_by_identity(session: Session, our_entity: str, sales_contract_no: str) -> SalesContract | None:
    """Resolves the frozen business identity directly — for the CLI and
    any caller (e.g. a future ProcurementSalesLink Slice 2) that has the
    identity but not the anchor id."""
    return SalesContractRepository(session).find_by_identity(our_entity, sales_contract_no)


def list_sales_contracts(session: Session) -> list[SalesContract]:
    """Every SalesContract, deterministic order — for the CLI's
    `sales-contract list` and any future Web equivalent."""
    return SalesContractRepository(session).list_all()


def get_sales_contract_history(session: Session, sales_contract_id: uuid.UUID) -> list[SalesContractRevision]:
    """Full audit trail for the CLI's `sales-contract history` and any
    future Web equivalent — every revision ever asserted, oldest first."""
    return SalesContractRepository(session).list_revisions(sales_contract_id)


def _apply_revision(
    session: Session,
    *,
    sales_contract_id: uuid.UUID,
    based_on_revision_id: uuid.UUID,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
    revision_type: str,
) -> SalesContractFactResult:
    _validate_fields(fields)
    if not fields:
        raise SalesContractFactError(f"{revision_type.lower()} requires at least one field")

    repo = SalesContractRepository(session)
    current_sales_contract = repo.get(sales_contract_id)
    if current_sales_contract is None:
        raise SalesContractFactError(f"SalesContract {sales_contract_id} not found")
    _require_fragment(session, source_fragment_id)

    # Reuse/replay check FIRST, but a fragment hit is never automatically a
    # replay: the SAME Evidence fragment reused for a DIFFERENT
    # revision_type, or for the SAME type asserting DIFFERENT field/value
    # content, is a conflict — never a silent no-op.
    reused_revision = repo.find_revision_by_fragment(sales_contract_id, source_fragment_id)
    if reused_revision is not None:
        if reused_revision.revision_type != revision_type:
            raise SalesContractFactConflict(
                f"SalesContract {sales_contract_id}: source_fragment_id {source_fragment_id} was already "
                f"used for a {reused_revision.revision_type}; this call asks for {revision_type} — a "
                "different intent under the SAME Evidence is never inferred"
            )
        reused_assertion = _asserted_fields(session, reused_revision)
        if reused_assertion != fields:
            raise SalesContractFactConflict(
                f"SalesContract {sales_contract_id}: source_fragment_id {source_fragment_id} already "
                f"asserted {reused_assertion!r} as a {revision_type}; this call asserts {fields!r} — a "
                "different content under the SAME Evidence, which cannot both be true"
            )
        # Exact replay: same fragment, same intent, same content.
        return SalesContractFactResult(
            sales_contract=current_sales_contract, created=False, revision_written=False, replay=True
        )

    current_revision = repo.get_current_revision(sales_contract_id)
    assert current_revision is not None
    if current_revision.id != based_on_revision_id:
        raise SalesContractFactConflict(
            f"SalesContract {sales_contract_id}: {revision_type.lower()} targets revision "
            f"{based_on_revision_id}, but the current revision is {current_revision.id} — refusing to "
            "guess which one was meant"
        )

    current_values = _revision_values(current_revision)
    if revision_type == SalesContractRevisionType.SUPPLEMENT:
        for key, value in fields.items():
            existing_value = current_values[key]
            if existing_value is not None and existing_value != value:
                raise SalesContractFactConflict(
                    f"SalesContract {sales_contract_id}: field {key!r} is already known as "
                    f"{existing_value!r} — use correction, not supplement"
                )
    else:  # CORRECTION
        for key in fields:
            if current_values[key] is None:
                raise SalesContractFactConflict(
                    f"SalesContract {sales_contract_id}: field {key!r} has no existing value to "
                    "correct — use supplement"
                )

    merged = dict(current_values)
    merged.update(fields)
    new_revision = SalesContractRevision(
        id=uuid.uuid4(),
        sales_contract_id=sales_contract_id,
        revision_type=revision_type,
        source_fragment_id=source_fragment_id,
        superseded_by_revision_id=None,
        created_at=created_at,
        asserted_field_names=sorted(fields.keys()),
        **merged,
    )
    # Atomic conditional retire-then-insert — see
    # SalesContractRepository.append_revision_against_current: if
    # based_on_revision_id was superseded by someone else between our
    # read above and this call, this writes NOTHING and returns False,
    # which we surface as a conflict rather than ever installing a
    # second current revision.
    retired = repo.append_revision_against_current(new_revision, based_on_revision_id=based_on_revision_id)
    if not retired:
        raise SalesContractFactConflict(
            f"SalesContract {sales_contract_id}: {revision_type.lower()} target revision "
            f"{based_on_revision_id} was superseded concurrently — refusing to create a second "
            "current revision"
        )
    session.flush()
    updated_sales_contract = repo.get(sales_contract_id)
    assert updated_sales_contract is not None
    return SalesContractFactResult(sales_contract=updated_sales_contract, created=False, revision_written=True)


# ---------------------------------------------------------------------------
# execute_* — the human/CLI/Web entry points. Each builds its own
# MANUAL_FACT Evidence (a human confirmation IS Evidence, per DOMAIN.md)
# with the same sha256 payload-dedup pattern as contract_item_facts.py /
# shipment_facts.py, then runs the matching core command inside the
# shared serialized write boundary (database.py).
# ---------------------------------------------------------------------------


def _json_safe(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, (Decimal, date)) else v) for k, v in fields.items()}


def _find_or_build_manual_fragment(
    session: Session, *, raw_data: dict[str, Any], source_type: str, locator: dict[str, Any], now: datetime
) -> EvidenceFragment:
    payload = json.dumps(raw_data, sort_keys=True).encode("utf-8")
    sha256 = hashlib.sha256(payload).hexdigest()
    evidence_repo = EvidenceRepository(session)
    existing_document = evidence_repo.find_document_by_sha256(sha256)
    if existing_document is not None:
        existing_fragment = evidence_repo.find_fragment_by_document(existing_document.id)
        if existing_fragment is not None:
            return existing_fragment
    document = EvidenceDocument(
        id=uuid.uuid4(),
        file_name=f"manual-sales-contract-fact-{now.isoformat()}.json",
        sha256=sha256,
        source_type=source_type,
        imported_at=now,
    )
    evidence_repo.add_document(document)
    fragment = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=document.id,
        fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None,
        row_number=None,
        locator_json=locator,
        raw_data=raw_data,
        created_at=now,
    )
    evidence_repo.add_fragment(fragment)
    session.flush()
    return fragment


def execute_create_sales_contract_fact(
    session: Session,
    *,
    our_entity: str | None,
    sales_contract_no: str | None,
    fields: dict[str, Any],
) -> SalesContractFactResult:
    """`serialized_write_transaction` rolls back on ANY exception — which
    would silently discard the `SALES_CONTRACT_IDENTITY_INCOMPLETE` /
    `BUSINESS_KEY_CONFLICT` Task `create_sales_contract_fact` may have
    already flushed before raising (same lesson as
    `shipment_facts.execute_create_shipment_fact`'s Codex fix round: a
    rejected create must still leave a durable, persisted Task).
    `SalesContractFactError` is therefore caught INSIDE the transaction
    and re-raised only AFTER it commits."""
    pending_error: SalesContractFactError | None = None
    result: SalesContractFactResult | None = None
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "sales-contract-create",
            "our_entity": our_entity,
            "sales_contract_no": sales_contract_no,
            "fields": _json_safe(fields),
        }
        fragment = _find_or_build_manual_fragment(
            session,
            raw_data=raw_data,
            source_type="manual_sales_contract_fact",
            locator={"command": "sales-contract-create"},
            now=now,
        )
        try:
            result = create_sales_contract_fact(
                session,
                our_entity=our_entity,
                sales_contract_no=sales_contract_no,
                fields=fields,
                source_fragment_id=fragment.id,
                created_at=now,
            )
        except SalesContractFactError as exc:
            pending_error = exc
    if pending_error is not None:
        raise pending_error
    assert result is not None
    return result


def execute_supplement_sales_contract_fact(
    session: Session, *, sales_contract_id: uuid.UUID, based_on_revision_id: uuid.UUID, fields: dict[str, Any]
) -> SalesContractFactResult:
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "sales-contract-supplement",
            "sales_contract_id": str(sales_contract_id),
            "based_on_revision_id": str(based_on_revision_id),
            "fields": _json_safe(fields),
        }
        fragment = _find_or_build_manual_fragment(
            session,
            raw_data=raw_data,
            source_type="manual_sales_contract_fact",
            locator={"command": "sales-contract-supplement"},
            now=now,
        )
        return supplement_sales_contract_fact(
            session,
            sales_contract_id=sales_contract_id,
            based_on_revision_id=based_on_revision_id,
            fields=fields,
            source_fragment_id=fragment.id,
            created_at=now,
        )


def execute_correct_sales_contract_fact(
    session: Session, *, sales_contract_id: uuid.UUID, based_on_revision_id: uuid.UUID, fields: dict[str, Any]
) -> SalesContractFactResult:
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "sales-contract-correct",
            "sales_contract_id": str(sales_contract_id),
            "based_on_revision_id": str(based_on_revision_id),
            "fields": _json_safe(fields),
        }
        fragment = _find_or_build_manual_fragment(
            session,
            raw_data=raw_data,
            source_type="manual_sales_contract_fact",
            locator={"command": "sales-contract-correct"},
            now=now,
        )
        return correct_sales_contract_fact(
            session,
            sales_contract_id=sales_contract_id,
            based_on_revision_id=based_on_revision_id,
            fields=fields,
            source_fragment_id=fragment.id,
            created_at=now,
        )

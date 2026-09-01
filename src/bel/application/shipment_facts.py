"""Shipment Fact Maintenance (Phase 2D.1-R2).

Establishes the everyday business intake path for `Shipment` — before
this round, no `Shipment`/`Export` domain object, persistence model,
adapter, or matching pipeline existed anywhere in the codebase
(docs/V1-SCOPE.md section 2.3). This module reuses, deliberately and
explicitly, the SAME pattern `bel.application.contract_item_facts`
established and had validated across three Phase 2D.1-R1 Codex fix
rounds — not a generic Fact revision engine, a second copy of the same
three explicit business intents:

    human-supplied / confirmed Evidence
          |
          v
    create_shipment_fact       — a Shipment did not exist before
    supplement_shipment_fact   — a previously-unknown field becomes known
    correct_shipment_fact      — a previously-asserted value was wrong
          |
          v
    ShipmentRevision (anchor + revision model, docs/PHASE2D1-R0-DECISIONS.md 1.3)
          |
          v
    query / read model (get_shipment, list_shipments_for_contract, history)

Frozen semantics this module implements (docs/PHASE2D1-R0-DECISIONS.md):

- 3.1/3.2 — one object, one name (`Shipment`), the frozen minimal field
  list. `contract_id`, `external_reference` and `execution_date` are the
  frozen business identity (4.4) and therefore live on the anchor,
  immutable after creation — this module never lets a caller change them
  (re-identification is out of scope, exactly like ContractItem's
  `(contract_id, source_item_key)` in R1). `contract_item_id`,
  `quantity`, and — since Phase 2D.3-F1c —
  `declared_amount`/`declared_currency` (the canonical export/customs
  declaration values, docs/PHASE2D3-RULE-FREEZE.md IP-S02) are the
  correctable business values, living on `ShipmentRevision`.
- 3.3 — a Shipment names exactly one procurement `Contract`. A shipment
  genuinely spanning contracts is an explicit unresolved case out of R2's
  scope (never a silent split) — this module simply does not offer an
  API to name more than one.
- 3.4 — `CostRecognitionFact.shipment_id` is a provenance reference to
  the Shipment anchor, set only by the caller (import_close_facts.py) via
  explicit resolution of the Shipment's frozen identity — this module
  never creates or looks up a CostRecognitionFact, and creating a
  Shipment here never creates one either.
- 3.5 — this module never creates a `ProcurementSalesLink`, and carries
  no sales-side reference; R3a's job, not R2's.
- 3.6 — this module makes no invoicing-eligibility judgment and treats
  `quantity` as nothing more than a stored, correctable value.
- 4.4 — business identity is `(contract_id, external_reference,
  execution_date)`. `external_reference` may be `None`
  ("identity incomplete" — 4.4): when it is, `create_shipment_fact` never
  attempts an identity lookup at all (there is no reliable key to guess
  against) and unconditionally creates a new anchor. When it is provided,
  a duplicate identity is resolved the same four-outcome way R1 validated
  for ContractItem (replay / corroborating / two flavours of conflict) —
  see `create_shipment_fact`'s docstring.

Idempotent replay requires ALL of same business identity/anchor + same
Evidence fragment + same revision intent (revision_type) + same asserted
field/value content — never merely a fragment-id match.
`asserted_field_names` is captured verbatim at write time (never
reconstructed by diffing against the predecessor, which cannot tell "not
asserted" apart from "re-asserted with its already-current value").

What this module deliberately does NOT do: it does not let a caller
change `contract_id`, `external_reference` or `execution_date`; it does
not create a `ProcurementSalesLink`; it does not create a
`CostRecognitionFact`; it does not treat `quantity` as invoiceable
quantity; it does not add a generic Fact revision engine — the three
functions below (plus their `execute_*` wrappers) are the whole surface.

Phase 2D.3-F1c boundaries, unchanged by the two new fields: this module
never applies FX conversion, never defaults a missing currency, and never
substitutes `quantity`, `Contract.gross_amount` or
`SalesContract.gross_amount` as a declaration amount — `declared_amount`
must come from actual export/customs declaration Evidence or an explicit
human-confirmed Fact. An amount without a currency remains a representable
incomplete Fact (the values are stored independently); full three-way
IP-S02 comparison is NOT implemented here.
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
from bel.domain.shipment import SHIPMENT_FACT_FIELDS, Shipment, ShipmentRevision, ShipmentRevisionType
from bel.infrastructure.persistence.database import serialized_write_transaction
from bel.infrastructure.persistence.repositories import (
    ContractItemRepository,
    ContractRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    ExceptionRepository,
    ShipmentRepository,
)


class ShipmentFactError(ValueError):
    """A rejected Shipment Fact operation — missing anchor/contract,
    missing or unresolvable Evidence, an unresolvable `contract_item_id`,
    or an unknown field name. Surfaces as an explicit failure, never a
    silent partial write."""


class ShipmentFactConflict(ShipmentFactError):
    """An explicit-intent conflict the system will not guess through:
    a supplement targeting an already-known, different value; a
    correction targeting a field with no existing value; a
    supplement/correction that no longer targets the current revision;
    or a create whose incoming assertion conflicts with an existing
    anchor's. A human must resolve this, never an inferred merge."""


class ShipmentIdentityIncomplete(ShipmentFactError):
    """Raised when `create_shipment_fact` is called with no
    `external_reference` and `identity_confirmed=False` (Phase 2D.1-R2
    Codex fix round, BLOCKER 1). Per docs/PHASE2D1-R0-DECISIONS.md
    section 4.4 ("external_reference null -> identity incomplete ->
    requires human confirmation"), NO Shipment anchor is created in this
    case — the Evidence is preserved (it was already recorded as an
    `EvidenceFragment` before this call) and a persisted
    `SHIPMENT_IDENTITY_INCOMPLETE` Task is raised, idempotently keyed by
    `source_fragment_id`, so a replay of the same submission never
    creates a second Task. Only an explicit resubmission with
    `identity_confirmed=True` creates the anchor."""


@dataclass(frozen=True)
class ShipmentFactResult:
    """Mirrors `ContractItemFactResult` — see its docstring for the full
    rationale. `created` / `revision_written` / `replay` / `corroborating`
    are independent flags because "nothing new was written" is not one
    outcome."""

    shipment: Shipment
    created: bool = False
    revision_written: bool = True
    replay: bool = False
    corroborating: bool = False


def _validate_fields(fields: dict[str, Any]) -> None:
    unknown = sorted(set(fields) - set(SHIPMENT_FACT_FIELDS))
    if unknown:
        raise ShipmentFactError(f"unknown Shipment field(s): {unknown}")


def _revision_values(revision: ShipmentRevision) -> dict[str, Any]:
    return {field: getattr(revision, field) for field in SHIPMENT_FACT_FIELDS}


def _normalized(fields: dict[str, Any]) -> dict[str, Any]:
    return {field: fields.get(field) for field in SHIPMENT_FACT_FIELDS}


def _require_fragment(session: Session, source_fragment_id: uuid.UUID) -> None:
    if EvidenceRepository(session).get_fragment(source_fragment_id) is None:
        raise ShipmentFactError(f"EvidenceFragment {source_fragment_id} not found")


def _require_valid_contract_item(session: Session, contract_id: uuid.UUID, contract_item_id: uuid.UUID) -> None:
    """`contract_item_id` (section 3.2: "item scope where known") is an
    identity reference to an EXISTING ContractItem anchor, and it must
    genuinely belong to this Shipment's contract — never a different
    contract's item, which would silently misattribute item-level scope
    (docs/PHASE2D1-R0-DECISIONS.md section 3.3's item-level judgments)."""
    contract_item = ContractItemRepository(session).get(contract_item_id)
    if contract_item is None:
        raise ShipmentFactError(f"ContractItem {contract_item_id} not found")
    if contract_item.contract_id != contract_id:
        raise ShipmentFactError(
            f"ContractItem {contract_item_id} belongs to contract {contract_item.contract_id}, not {contract_id}"
        )


def _asserted_fields(session: Session, revision: ShipmentRevision) -> dict[str, Any]:
    """Mirrors `contract_item_facts._asserted_fields` exactly — see its
    docstring for the full rationale on why this must read the persisted
    `asserted_field_names` rather than reconstruct it by diffing against
    the predecessor."""
    if revision.asserted_field_names is not None:
        values = _revision_values(revision)
        return {field: values[field] for field in revision.asserted_field_names}

    predecessor = ShipmentRepository(session).find_predecessor(revision.id)
    values = _revision_values(revision)
    if predecessor is None:
        return {field: value for field, value in values.items() if value is not None}
    predecessor_values = _revision_values(predecessor)
    return {field: value for field, value in values.items() if value != predecessor_values[field]}


def _find_identity_incomplete_task(session: Session, source_fragment_id: uuid.UUID) -> TaskException | None:
    """Idempotency check for BLOCKER 1: the SAME Evidence fragment
    resubmitted with no `external_reference` must not raise a second
    `SHIPMENT_IDENTITY_INCOMPLETE` Task."""
    for task in ExceptionRepository(session).list_open():
        if (
            task.exception_type == ExceptionType.SHIPMENT_IDENTITY_INCOMPLETE
            and task.detail.get("source_fragment_id") == str(source_fragment_id)
        ):
            return task
    return None


def _find_identity_conflict_task(
    session: Session, *, shipment_id: uuid.UUID, conflicting_source_fragment_id: uuid.UUID
) -> TaskException | None:
    """Idempotency check for BLOCKER 2: the SAME conflicting Evidence
    fragment resubmitted against the SAME existing anchor must not raise
    a second `SHIPMENT_IDENTITY_CONFLICT` Task."""
    for task in ExceptionRepository(session).list_open():
        if (
            task.exception_type == ExceptionType.SHIPMENT_IDENTITY_CONFLICT
            and task.detail.get("shipment_id") == str(shipment_id)
            and task.detail.get("conflicting_source_fragment_id") == str(conflicting_source_fragment_id)
        ):
            return task
    return None


# ---------------------------------------------------------------------------
# Core commands — session-transaction-agnostic, per contract_item_facts.py's
# split: these never commit. execute_* below owns the CLI/Web transaction.
# ---------------------------------------------------------------------------


def create_shipment_fact(
    session: Session,
    *,
    contract_id: uuid.UUID,
    external_reference: str | None,
    execution_date: date,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
    identity_confirmed: bool = False,
) -> ShipmentFactResult:
    """Case A (docs/PHASE2D1-R0-DECISIONS.md 1.1): a Shipment did not
    exist before. `fields` may include `contract_item_id`, `quantity`,
    and (Phase 2D.3-F1c) `declared_amount` / `declared_currency` — all
    optional per section 3.2. The declaration fields, like every other
    value, require Evidence: they are never inferred from the contract
    or from `quantity`.

    When `external_reference` is `None`, the business identity is
    "incomplete" (section 4.4: "requires human confirmation") — Phase
    2D.1-R2 Codex fix round, BLOCKER 1. NO anchor is created unless the
    caller explicitly passes `identity_confirmed=True`:

    - `identity_confirmed=False` (the default): the Evidence is
      preserved (it already exists as an `EvidenceFragment`) and a
      persisted `SHIPMENT_IDENTITY_INCOMPLETE` Task is raised,
      idempotently keyed by `source_fragment_id` — replaying the same
      submission never creates a second Task. Raises
      `ShipmentIdentityIncomplete`.
    - `identity_confirmed=True`: a human has explicitly confirmed this
      is a genuinely new Shipment despite the incomplete identity. This
      looks up EVERY revision anywhere that already used this exact
      Evidence fragment (`find_revisions_by_fragment_id`, global — there
      is no anchor to scope it by yet) and verifies each candidate's
      anchor `contract_id`/`external_reference`/`execution_date`,
      `revision_type == INITIAL`, and asserted content against THIS
      call, never trusting an arbitrary first match (Phase 2D.1-R2
      second Codex fix round: a bare `scalar()` lookup could silently
      return a DIFFERENT contract's anchor if the fragment id were ever
      reused). Exactly one full match -> EXACT REPLAY (`replay=True`).
      Zero matches -> a genuinely new anchor. More than one match, or a
      candidate that matches on fragment but disagrees on contract,
      date, or content, is never a guess: it raises
      `ShipmentFactConflict` and creates nothing.

    When `external_reference` is provided, a duplicate
    `(contract_id, external_reference, execution_date)` is resolved
    exactly like ContractItem's create (docs/PHASE2D1-R0-DECISIONS.md
    section 4.4, generalised per the Phase 2D.1-R1 Codex fix round):

    1. Same Evidence fragment, same assertion -> EXACT REPLAY
       (`replay=True`). Nothing new is written.
    2. Different Evidence fragment, same assertion -> CORROBORATING
       (`corroborating=True`). Not a replay, not a conflict, not a
       second INITIAL revision.
    3. Different Evidence fragment, conflicting assertion -> the
       existing anchor/revision is left completely unchanged, a
       persisted `SHIPMENT_IDENTITY_CONFLICT` Task is raised (Phase
       2D.1-R2 Codex fix round, BLOCKER 2 — section 4.4's "Same key,
       different Evidence -> Task"; idempotently keyed by
       `(shipment_id, conflicting source_fragment_id)`), and
       `ShipmentFactConflict` is raised to the caller. `create` never
       decides whether the caller meant supplement or correction.
    4. Same Evidence fragment, DIFFERENT assertion -> also
       `ShipmentFactConflict` (no persisted Task — this is a malformed
       replay of the SAME artifact, not a new piece of conflicting
       Evidence) — the same immutable artifact cannot be asked to mean
       two different things."""
    _validate_fields(fields)
    if ContractRepository(session).get(contract_id) is None:
        raise ShipmentFactError(f"Contract {contract_id} not found")
    if fields.get("contract_item_id") is not None:
        _require_valid_contract_item(session, contract_id, fields["contract_item_id"])
    _require_fragment(session, source_fragment_id)

    shipment_repo = ShipmentRepository(session)
    incoming_assertion = {key: value for key, value in fields.items() if value is not None}

    if external_reference is None:
        if not identity_confirmed:
            existing_task = _find_identity_incomplete_task(session, source_fragment_id)
            if existing_task is None:
                ExceptionRepository(session).add(
                    TaskException(
                        id=uuid.uuid4(),
                        exception_type=ExceptionType.SHIPMENT_IDENTITY_INCOMPLETE,
                        status=ExceptionStatus.OPEN,
                        summary=(
                            f"Shipment create for contract {contract_id} on {execution_date} has no "
                            "external_reference — identity incomplete, requires human confirmation"
                        ),
                        detail={
                            "contract_id": str(contract_id),
                            "execution_date": execution_date.isoformat(),
                            "source_fragment_id": str(source_fragment_id),
                            "fields": {k: str(v) for k, v in incoming_assertion.items()},
                        },
                        created_at=created_at,
                    )
                )
                session.flush()
            raise ShipmentIdentityIncomplete(
                f"Shipment create for contract {contract_id} on {execution_date} has no external_reference — "
                "identity incomplete (docs/PHASE2D1-R0-DECISIONS.md section 4.4): Evidence preserved, a Task "
                "has been raised, and no Shipment anchor was created. Resubmit with identity_confirmed=True "
                "after explicit human confirmation."
            )
        # identity_confirmed=True: no reliable business key exists, so
        # the only safe dedup is an EXACT match on every dimension —
        # never an arbitrary first hit on fragment id alone (Phase
        # 2D.1-R2 second Codex fix round, cross-contract misattribution).
        candidates = shipment_repo.find_revisions_by_fragment_id(source_fragment_id)
        if candidates:
            exact_matches = []
            for candidate in candidates:
                candidate_shipment = shipment_repo.get(candidate.shipment_id)
                if (
                    candidate_shipment is not None
                    and candidate.revision_type == ShipmentRevisionType.INITIAL
                    and candidate_shipment.contract_id == contract_id
                    and candidate_shipment.external_reference is None
                    and candidate_shipment.execution_date == execution_date
                    and _asserted_fields(session, candidate) == incoming_assertion
                ):
                    exact_matches.append(candidate_shipment)
            if len(exact_matches) == 1:
                return ShipmentFactResult(
                    shipment=exact_matches[0], created=False, revision_written=False, replay=True
                )
            # Zero exact matches among ≥1 candidates (fragment already
            # tied to a different contract/date/content) or more than one
            # ambiguous match — never guess which, if any, is "the same".
            raise ShipmentFactConflict(
                f"EvidenceFragment {source_fragment_id} is already associated with "
                f"{len(candidates)} Shipment revision(s) that do not unambiguously match this create "
                f"(contract={contract_id}, execution_date={execution_date}, fields={incoming_assertion!r}) — "
                "refusing to guess whether this is a replay; resolve manually before resubmitting"
            )
    else:
        existing = shipment_repo.find_by_identity(contract_id, external_reference, execution_date)
        if existing is not None:
            initial_revision = shipment_repo.get_initial_revision(existing.id)
            assert initial_revision is not None
            existing_assertion = _asserted_fields(session, initial_revision)
            same_fragment = initial_revision.source_fragment_id == source_fragment_id
            if incoming_assertion == existing_assertion:
                return ShipmentFactResult(
                    shipment=existing,
                    created=False,
                    revision_written=False,
                    replay=same_fragment,
                    corroborating=not same_fragment,
                )
            if same_fragment:
                raise ShipmentFactConflict(
                    f"Shipment {existing.id}: source_fragment_id {source_fragment_id} was already used to "
                    f"assert {existing_assertion!r} as the INITIAL revision — this call asserts "
                    f"{incoming_assertion!r}, a different content under the SAME Evidence, which cannot both be true"
                )
            existing_conflict_task = _find_identity_conflict_task(
                session, shipment_id=existing.id, conflicting_source_fragment_id=source_fragment_id
            )
            if existing_conflict_task is None:
                ExceptionRepository(session).add(
                    TaskException(
                        id=uuid.uuid4(),
                        exception_type=ExceptionType.SHIPMENT_IDENTITY_CONFLICT,
                        status=ExceptionStatus.OPEN,
                        summary=f"Shipment {existing.id} has conflicting Evidence under the same business identity",
                        detail={
                            "shipment_id": str(existing.id),
                            "existing_source_fragment_id": str(initial_revision.source_fragment_id),
                            "existing_assertion": {k: str(v) for k, v in existing_assertion.items()},
                            "conflicting_source_fragment_id": str(source_fragment_id),
                            "conflicting_assertion": {k: str(v) for k, v in incoming_assertion.items()},
                        },
                        created_at=created_at,
                    )
                )
                session.flush()
            raise ShipmentFactConflict(
                f"Shipment {existing.id} already asserts {existing_assertion!r}; Evidence "
                f"{source_fragment_id} asserts {incoming_assertion!r} instead — conflicting values under "
                "different Evidence require an explicit supplement (if previously unknown) or correction "
                "(if previously wrong); create never guesses which. A SHIPMENT_IDENTITY_CONFLICT Task has "
                "been raised; the existing anchor is unchanged."
            )

    anchor_id = uuid.uuid4()
    shipment_repo.create_anchor(
        id=anchor_id,
        contract_id=contract_id,
        external_reference=external_reference,
        execution_date=execution_date,
        created_at=created_at,
    )
    shipment_repo.create_initial_revision(
        ShipmentRevision(
            id=uuid.uuid4(),
            shipment_id=anchor_id,
            revision_type=ShipmentRevisionType.INITIAL,
            source_fragment_id=source_fragment_id,
            superseded_by_revision_id=None,
            created_at=created_at,
            asserted_field_names=sorted(key for key, value in fields.items() if value is not None),
            **_normalized(fields),
        )
    )
    session.flush()
    created_shipment = shipment_repo.get(anchor_id)
    assert created_shipment is not None
    return ShipmentFactResult(shipment=created_shipment, created=True, revision_written=True)


def supplement_shipment_fact(
    session: Session,
    *,
    shipment_id: uuid.UUID,
    based_on_revision_id: uuid.UUID,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> ShipmentFactResult:
    """Case B-supplement: a previously-unknown attribute
    (`contract_item_id`, `quantity`, or Phase 2D.3-F1c
    `declared_amount` / `declared_currency`) becomes known. Every key in
    `fields` must currently be NULL on the current revision — supplementing
    a field that already holds a DIFFERENT value is rejected
    (`ShipmentFactConflict`: that is a correction). Resupplying the SAME
    value is accepted as harmless."""
    return _apply_revision(
        session,
        shipment_id=shipment_id,
        based_on_revision_id=based_on_revision_id,
        fields=fields,
        source_fragment_id=source_fragment_id,
        created_at=created_at,
        revision_type=ShipmentRevisionType.SUPPLEMENT,
    )


def correct_shipment_fact(
    session: Session,
    *,
    shipment_id: uuid.UUID,
    based_on_revision_id: uuid.UUID,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> ShipmentFactResult:
    """Case B-correction: a previously-asserted value was wrong. Every key
    in `fields` must currently hold a NON-NULL value on the current
    revision — correcting a field with no existing value is rejected
    (`ShipmentFactConflict`: that is a supplement).

    `CostRecognitionFact.shipment_id` names the Shipment ANCHOR, never a
    specific revision (section 3.4) — so a correction never invalidates
    that reference by itself. What it CAN invalidate is the business
    assumption a human made when they cited this shipment: if a
    persisted `CostRecognitionFact` already names this anchor when one
    of its revisions is superseded by a correction, a
    `SHIPMENT_FACT_SUPERSEDED` Task is raised naming both revisions and
    every such fact (docs/PHASE2D1-R0-DECISIONS.md section 1.5) — the
    fact's `shipment_id` is never edited or re-pointed, and nothing is
    silently rewritten or reversed."""
    result = _apply_revision(
        session,
        shipment_id=shipment_id,
        based_on_revision_id=based_on_revision_id,
        fields=fields,
        source_fragment_id=source_fragment_id,
        created_at=created_at,
        revision_type=ShipmentRevisionType.CORRECTION,
    )
    if result.revision_written:
        _flag_dependents_on_correction(
            session,
            shipment_id=shipment_id,
            superseded_revision_id=based_on_revision_id,
            superseding_revision_id=_current_revision_id(session, shipment_id),
            created_at=created_at,
        )
    return result


def get_shipment(session: Session, shipment_id: uuid.UUID) -> Shipment | None:
    """Current authoritative state, for the CLI's `shipment show` and any
    future Web equivalent."""
    return ShipmentRepository(session).get(shipment_id)


def list_shipments_for_contract(session: Session, contract_id: uuid.UUID) -> list[Shipment]:
    """One `Contract` -> many `Shipment`s (docs/PHASE2D1-R0-DECISIONS.md
    section 3.3) — deterministic order (created_at, id)."""
    return ShipmentRepository(session).list_for_contract(contract_id)


def get_shipment_history(session: Session, shipment_id: uuid.UUID) -> list[ShipmentRevision]:
    """Full audit trail for the CLI's `shipment history` and any future
    Web equivalent — every revision ever asserted, oldest first."""
    return ShipmentRepository(session).list_revisions(shipment_id)


def _current_revision_id(session: Session, shipment_id: uuid.UUID) -> uuid.UUID:
    current = ShipmentRepository(session).get_current_revision(shipment_id)
    assert current is not None
    return current.id


def _apply_revision(
    session: Session,
    *,
    shipment_id: uuid.UUID,
    based_on_revision_id: uuid.UUID,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
    revision_type: str,
) -> ShipmentFactResult:
    _validate_fields(fields)
    if not fields:
        raise ShipmentFactError(f"{revision_type.lower()} requires at least one field")

    shipment_repo = ShipmentRepository(session)
    current_shipment = shipment_repo.get(shipment_id)
    if current_shipment is None:
        raise ShipmentFactError(f"Shipment {shipment_id} not found")
    if fields.get("contract_item_id") is not None:
        _require_valid_contract_item(session, current_shipment.contract_id, fields["contract_item_id"])
    _require_fragment(session, source_fragment_id)

    # Reuse/replay check FIRST, but a fragment hit is never automatically a
    # replay: the SAME Evidence fragment reused for a DIFFERENT
    # revision_type, or for the SAME type asserting DIFFERENT field/value
    # content, is a conflict — never a silent no-op.
    reused_revision = shipment_repo.find_revision_by_fragment(shipment_id, source_fragment_id)
    if reused_revision is not None:
        if reused_revision.revision_type != revision_type:
            raise ShipmentFactConflict(
                f"Shipment {shipment_id}: source_fragment_id {source_fragment_id} was already used for a "
                f"{reused_revision.revision_type}; this call asks for {revision_type} — a different intent "
                "under the SAME Evidence is never inferred"
            )
        reused_assertion = _asserted_fields(session, reused_revision)
        if reused_assertion != fields:
            raise ShipmentFactConflict(
                f"Shipment {shipment_id}: source_fragment_id {source_fragment_id} already asserted "
                f"{reused_assertion!r} as a {revision_type}; this call asserts {fields!r} — a different "
                "content under the SAME Evidence, which cannot both be true"
            )
        # Exact replay: same fragment, same intent, same content.
        return ShipmentFactResult(shipment=current_shipment, created=False, revision_written=False, replay=True)

    current_revision = shipment_repo.get_current_revision(shipment_id)
    assert current_revision is not None
    if current_revision.id != based_on_revision_id:
        raise ShipmentFactConflict(
            f"Shipment {shipment_id}: {revision_type.lower()} targets revision {based_on_revision_id}, but "
            f"the current revision is {current_revision.id} — refusing to guess which one was meant"
        )

    current_values = _revision_values(current_revision)
    if revision_type == ShipmentRevisionType.SUPPLEMENT:
        for key, value in fields.items():
            existing_value = current_values[key]
            if existing_value is not None and existing_value != value:
                raise ShipmentFactConflict(
                    f"Shipment {shipment_id}: field {key!r} is already known as {existing_value!r} — use "
                    "correction, not supplement"
                )
    else:  # CORRECTION
        for key in fields:
            if current_values[key] is None:
                raise ShipmentFactConflict(
                    f"Shipment {shipment_id}: field {key!r} has no existing value to correct — use supplement"
                )

    merged = dict(current_values)
    merged.update(fields)
    new_revision = ShipmentRevision(
        id=uuid.uuid4(),
        shipment_id=shipment_id,
        revision_type=revision_type,
        source_fragment_id=source_fragment_id,
        superseded_by_revision_id=None,
        created_at=created_at,
        asserted_field_names=sorted(fields.keys()),
        **merged,
    )
    # Atomic conditional retire-then-insert — see
    # ShipmentRepository.append_revision_against_current: if
    # based_on_revision_id was superseded by someone else between our
    # read above and this call, this writes NOTHING and returns False,
    # which we surface as a conflict rather than ever installing a
    # second current revision.
    retired = shipment_repo.append_revision_against_current(new_revision, based_on_revision_id=based_on_revision_id)
    if not retired:
        raise ShipmentFactConflict(
            f"Shipment {shipment_id}: {revision_type.lower()} target revision {based_on_revision_id} was "
            "superseded concurrently — refusing to create a second current revision"
        )
    session.flush()
    updated_shipment = shipment_repo.get(shipment_id)
    assert updated_shipment is not None
    return ShipmentFactResult(shipment=updated_shipment, created=False, revision_written=True)


def _flag_dependents_on_correction(
    session: Session,
    *,
    shipment_id: uuid.UUID,
    superseded_revision_id: uuid.UUID,
    superseding_revision_id: uuid.UUID,
    created_at: datetime,
) -> None:
    facts = CostRecognitionFactRepository(session).list_for_shipment(shipment_id)
    if not facts:
        return

    ExceptionRepository(session).add(
        TaskException(
            id=uuid.uuid4(),
            exception_type=ExceptionType.SHIPMENT_FACT_SUPERSEDED,
            status=ExceptionStatus.OPEN,
            summary=f"Shipment {shipment_id} corrected while a CostRecognitionFact references it",
            detail={
                "shipment_id": str(shipment_id),
                "superseded_revision_id": str(superseded_revision_id),
                "superseding_revision_id": str(superseding_revision_id),
                "dependents": {"cost_recognition_facts": [str(f.id) for f in facts]},
            },
            created_at=created_at,
        )
    )


# ---------------------------------------------------------------------------
# execute_* — the human/CLI/Web entry points. Each builds its own
# MANUAL_FACT Evidence (a human confirmation IS Evidence, per DOMAIN.md)
# with the same sha256 payload-dedup pattern as contract_item_facts.py,
# then runs the matching core command inside the shared serialized write
# boundary (database.py).
# ---------------------------------------------------------------------------


def _json_safe(fields: dict[str, Any]) -> dict[str, Any]:
    """Unlike ContractItem's fields (all Decimal/str), Shipment's
    `contract_item_id` is a UUID — also needs stringifying for the JSON
    `raw_data` column."""
    return {k: (str(v) if isinstance(v, (Decimal, uuid.UUID)) else v) for k, v in fields.items()}


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
        file_name=f"manual-shipment-fact-{now.isoformat()}.json",
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


def execute_create_shipment_fact(
    session: Session,
    *,
    contract_id: uuid.UUID,
    external_reference: str | None,
    execution_date: date,
    fields: dict[str, Any],
    identity_confirmed: bool = False,
) -> ShipmentFactResult:
    """`serialized_write_transaction` rolls back on ANY exception — which
    would silently discard the `SHIPMENT_IDENTITY_INCOMPLETE` /
    `SHIPMENT_IDENTITY_CONFLICT` Task `create_shipment_fact` may have
    already flushed before raising (Phase 2D.1-R2 Codex fix round,
    BLOCKERs 1-2: a rejected create must still leave a durable, persisted
    Task — a raised exception alone is not enough). ``ShipmentFactError``
    is therefore caught INSIDE the transaction and re-raised only AFTER
    it commits, so the Task (and nothing else — the anchor/revision
    writes those two branches take are never reached) survives exactly
    as `create_shipment_fact` left it."""
    pending_error: ShipmentFactError | None = None
    result: ShipmentFactResult | None = None
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        # identity_confirmed is deliberately NOT part of the hashed
        # payload: the first (unconfirmed) submission and a later
        # resubmission with identity_confirmed=True for the SAME
        # human-supplied facts must resolve to the SAME EvidenceFragment,
        # which is what lets create_shipment_fact's
        # find_revisions_by_fragment_id recognise the confirmed create as
        # continuing that same Evidence rather than a new artifact.
        raw_data = {
            "command": "shipment-create",
            "contract_id": str(contract_id),
            "external_reference": external_reference,
            "execution_date": execution_date.isoformat(),
            "fields": _json_safe(fields),
        }
        fragment = _find_or_build_manual_fragment(
            session,
            raw_data=raw_data,
            source_type="manual_shipment_fact",
            locator={"command": "shipment-create"},
            now=now,
        )
        try:
            result = create_shipment_fact(
                session,
                contract_id=contract_id,
                external_reference=external_reference,
                execution_date=execution_date,
                fields=fields,
                source_fragment_id=fragment.id,
                identity_confirmed=identity_confirmed,
                created_at=now,
            )
        except ShipmentFactError as exc:
            pending_error = exc
    if pending_error is not None:
        raise pending_error
    assert result is not None
    return result


def execute_supplement_shipment_fact(
    session: Session, *, shipment_id: uuid.UUID, based_on_revision_id: uuid.UUID, fields: dict[str, Any]
) -> ShipmentFactResult:
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "shipment-supplement",
            "shipment_id": str(shipment_id),
            "based_on_revision_id": str(based_on_revision_id),
            "fields": _json_safe(fields),
        }
        fragment = _find_or_build_manual_fragment(
            session,
            raw_data=raw_data,
            source_type="manual_shipment_fact",
            locator={"command": "shipment-supplement"},
            now=now,
        )
        return supplement_shipment_fact(
            session,
            shipment_id=shipment_id,
            based_on_revision_id=based_on_revision_id,
            fields=fields,
            source_fragment_id=fragment.id,
            created_at=now,
        )


def execute_correct_shipment_fact(
    session: Session, *, shipment_id: uuid.UUID, based_on_revision_id: uuid.UUID, fields: dict[str, Any]
) -> ShipmentFactResult:
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "shipment-correct",
            "shipment_id": str(shipment_id),
            "based_on_revision_id": str(based_on_revision_id),
            "fields": _json_safe(fields),
        }
        fragment = _find_or_build_manual_fragment(
            session,
            raw_data=raw_data,
            source_type="manual_shipment_fact",
            locator={"command": "shipment-correct"},
            now=now,
        )
        return correct_shipment_fact(
            session,
            shipment_id=shipment_id,
            based_on_revision_id=based_on_revision_id,
            fields=fields,
            source_fragment_id=fragment.id,
            created_at=now,
        )

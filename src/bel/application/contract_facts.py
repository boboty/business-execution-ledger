"""Contract Fact Maintenance (Phase 2D.1-R5 pre-flight debt closure).

Mirrors ``bel.application.contract_item_facts`` structurally — the same
three explicit business intents, the same anchor+revision model, the
same Evidence-required invariant — but adapted to Contract's identity,
which (unlike ContractItem's ``(contract_id, source_item_key)``) is
**not** database-unique: ``(contract_no, counterparty)`` may legitimately
collide (docs/PHASE2D1-R0-DECISIONS.md section 4.4 — resolved by Task,
never by schema prohibition). ``create_contract_fact`` therefore has a
FIFTH outcome ContractItem's create does not: more than one existing
anchor already shares the identity, which is never guessed through —
``ContractFactAmbiguous`` is raised and the caller (R5 backfill) turns
that into a Task.

``contract_no``/``counterparty`` are identity-bearing and therefore
excluded from ``CONTRACT_FACT_FIELDS`` — supplement/correct can never
touch them, which is what makes RE-IDENTIFICATION (changing either)
structurally impossible through the ordinary Fact-maintenance path,
exactly as section 4.4 requires ("always produces a Task, never a plain
correction"). There is deliberately no "re-identify an anchor" function
anywhere in this codebase.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from bel.domain.contract import CONTRACT_FACT_FIELDS, Contract, ContractRevision, ContractRevisionType
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.infrastructure.persistence.database import serialized_write_transaction
from bel.infrastructure.persistence.repositories import ContractRepository, EvidenceRepository


class ContractFactError(ValueError):
    """A rejected Contract Fact operation — missing anchor, missing or
    unresolvable Evidence, or an unknown field name."""


class ContractFactConflict(ContractFactError):
    """An explicit-intent conflict the system will not guess through —
    see ContractItemFactConflict's docstring for the general shape."""


class ContractFactAmbiguous(ContractFactError):
    """More than one existing Contract anchor already shares the
    ``(contract_no, counterparty)`` identity — R0 explicitly permits this
    (duplicates are expected, never merged), so ``create_contract_fact``
    never picks one; the caller must resolve the ambiguity explicitly
    (docs/PHASE2D1-R0-DECISIONS.md section 4.4's "Task, never a guess")."""


@dataclass(frozen=True)
class ContractFactResult:
    """See ContractItemFactResult's docstring for the full rationale
    behind four independent outcome flags."""

    contract: Contract
    created: bool = False
    revision_written: bool = True
    replay: bool = False
    corroborating: bool = False


def _validate_fields(fields: dict[str, Any]) -> None:
    unknown = sorted(set(fields) - set(CONTRACT_FACT_FIELDS))
    if unknown:
        raise ContractFactError(f"unknown Contract field(s): {unknown}")


def _revision_values(revision: ContractRevision) -> dict[str, Any]:
    return {field: getattr(revision, field) for field in CONTRACT_FACT_FIELDS}


def _normalized(fields: dict[str, Any]) -> dict[str, Any]:
    return {field: fields.get(field) for field in CONTRACT_FACT_FIELDS}


def _require_fragment(session: Session, source_fragment_id: uuid.UUID) -> None:
    if EvidenceRepository(session).get_fragment(source_fragment_id) is None:
        raise ContractFactError(f"EvidenceFragment {source_fragment_id} not found")


def _asserted_fields(session: Session, revision: ContractRevision) -> dict[str, Any]:
    """See ``bel.application.contract_item_facts._asserted_fields`` — the
    identical reasoning applies verbatim."""
    if revision.asserted_field_names is not None:
        values = _revision_values(revision)
        return {field: values[field] for field in revision.asserted_field_names}

    predecessor = ContractRepository(session).find_predecessor(revision.id)
    values = _revision_values(revision)
    if predecessor is None:
        return {field: value for field, value in values.items() if value is not None}
    predecessor_values = _revision_values(predecessor)
    return {field: value for field, value in values.items() if value != predecessor_values[field]}


# ---------------------------------------------------------------------------
# Core commands — session-transaction-agnostic. cutover_backfill.py calls
# these directly inside its own transaction; execute_* below owns the
# CLI/Web transaction for any future direct caller.
# ---------------------------------------------------------------------------


def create_contract_fact(
    session: Session,
    *,
    contract_no: str,
    counterparty: str | None,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> ContractFactResult:
    """Case A: a Contract did not exist before. ``fields`` must include
    ``gross_amount`` and ``currency`` — unlike every other revisioned
    field, both stay NOT NULL at the schema level (matching Contract's
    pre-R5 strictness; this is not a new rule).

    Five outcomes, decided by comparing the incoming assertion against
    each EXISTING anchor sharing the identity:

    0. No existing anchor -> a genuinely new one (``created=True``).
    1. Exactly one existing anchor, same content -> replay/corroborating
       (identical logic to ``create_contract_item_fact``).
    2. Exactly one existing anchor, different content -> ``ContractFactConflict``.
    3. MORE THAN ONE existing anchor already shares the identity ->
       ``ContractFactAmbiguous`` — R0 explicitly permits duplicate
       ``(contract_no, counterparty)`` identity, so this function never
       guesses which one the caller meant; the caller must resolve the
       ambiguity itself (R5 backfill turns this into a Task)."""
    if not contract_no:
        raise ContractFactError("contract_no is required")
    _validate_fields(fields)
    if fields.get("gross_amount") is None or fields.get("currency") is None:
        raise ContractFactError("gross_amount and currency are required to create a Contract")
    _require_fragment(session, source_fragment_id)

    contract_repo = ContractRepository(session)
    existing_matches = contract_repo.find_by_identity(contract_no, counterparty)
    if len(existing_matches) > 1:
        raise ContractFactAmbiguous(
            f"identity (contract_no={contract_no!r}, counterparty={counterparty!r}) already matches "
            f"{len(existing_matches)} existing Contract anchors — refusing to guess which one this Evidence "
            "concerns"
        )
    if existing_matches:
        existing = existing_matches[0]
        initial_revision = contract_repo.get_initial_revision(existing.id)
        assert initial_revision is not None
        existing_assertion = _asserted_fields(session, initial_revision)
        incoming_assertion = {key: value for key, value in fields.items() if value is not None}
        same_fragment = initial_revision.source_fragment_id == source_fragment_id
        if incoming_assertion == existing_assertion:
            return ContractFactResult(
                contract=existing, created=False, revision_written=False, replay=same_fragment,
                corroborating=not same_fragment,
            )
        if same_fragment:
            raise ContractFactConflict(
                f"Contract {existing.id}: source_fragment_id {source_fragment_id} was already used to assert "
                f"{existing_assertion!r} as the INITIAL revision — this call asserts {incoming_assertion!r}, a "
                "different content under the SAME Evidence, which cannot both be true"
            )
        raise ContractFactConflict(
            f"Contract {existing.id} already asserts {existing_assertion!r}; Evidence {source_fragment_id} "
            f"asserts {incoming_assertion!r} instead — conflicting values under different Evidence require an "
            "explicit supplement (if previously unknown) or correction (if previously wrong); create never "
            "guesses which"
        )

    anchor_id = uuid.uuid4()
    contract_repo.create_anchor(id=anchor_id, contract_no=contract_no, counterparty=counterparty, created_at=created_at)
    contract_repo.create_initial_revision(
        ContractRevision(
            id=uuid.uuid4(),
            contract_id=anchor_id,
            revision_type=ContractRevisionType.INITIAL,
            source_fragment_id=source_fragment_id,
            superseded_by_revision_id=None,
            created_at=created_at,
            asserted_field_names=sorted(key for key, value in fields.items() if value is not None),
            **_normalized(fields),
        )
    )
    session.flush()
    created_contract = contract_repo.get(anchor_id)
    assert created_contract is not None
    return ContractFactResult(contract=created_contract, created=True, revision_written=True)


def supplement_contract_fact(
    session: Session,
    *,
    contract_id: uuid.UUID,
    based_on_revision_id: uuid.UUID,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> ContractFactResult:
    """Case B-supplement — identical semantics to
    ``supplement_contract_item_fact``. ``fields`` may never contain
    ``contract_no``/``counterparty`` (not in ``CONTRACT_FACT_FIELDS`` —
    see this module's docstring)."""
    return _apply_revision(
        session, contract_id=contract_id, based_on_revision_id=based_on_revision_id, fields=fields,
        source_fragment_id=source_fragment_id, created_at=created_at, revision_type=ContractRevisionType.SUPPLEMENT,
    )


def correct_contract_fact(
    session: Session,
    *,
    contract_id: uuid.UUID,
    based_on_revision_id: uuid.UUID,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> ContractFactResult:
    """Case B-correction — identical semantics to
    ``correct_contract_item_fact``."""
    return _apply_revision(
        session, contract_id=contract_id, based_on_revision_id=based_on_revision_id, fields=fields,
        source_fragment_id=source_fragment_id, created_at=created_at, revision_type=ContractRevisionType.CORRECTION,
    )


def get_contract(session: Session, contract_id: uuid.UUID) -> Contract | None:
    return ContractRepository(session).get(contract_id)


def get_contract_history(session: Session, contract_id: uuid.UUID) -> list[ContractRevision]:
    return ContractRepository(session).list_revisions(contract_id)


def _apply_revision(
    session: Session,
    *,
    contract_id: uuid.UUID,
    based_on_revision_id: uuid.UUID,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
    revision_type: str,
) -> ContractFactResult:
    _validate_fields(fields)
    if not fields:
        raise ContractFactError(f"{revision_type.lower()} requires at least one field")

    contract_repo = ContractRepository(session)
    current_contract = contract_repo.get(contract_id)
    if current_contract is None:
        raise ContractFactError(f"Contract {contract_id} not found")
    _require_fragment(session, source_fragment_id)

    reused_revision = contract_repo.find_revision_by_fragment(contract_id, source_fragment_id)
    if reused_revision is not None:
        if reused_revision.revision_type != revision_type:
            raise ContractFactConflict(
                f"Contract {contract_id}: source_fragment_id {source_fragment_id} was already used for a "
                f"{reused_revision.revision_type}; this call asks for {revision_type} — a different intent "
                "under the SAME Evidence is never inferred"
            )
        reused_assertion = _asserted_fields(session, reused_revision)
        if reused_assertion != fields:
            raise ContractFactConflict(
                f"Contract {contract_id}: source_fragment_id {source_fragment_id} already asserted "
                f"{reused_assertion!r} as a {revision_type}; this call asserts {fields!r} — a different content "
                "under the SAME Evidence, which cannot both be true"
            )
        return ContractFactResult(contract=current_contract, created=False, revision_written=False, replay=True)

    current_revision = contract_repo.get_current_revision(contract_id)
    assert current_revision is not None
    if current_revision.id != based_on_revision_id:
        raise ContractFactConflict(
            f"Contract {contract_id}: {revision_type.lower()} targets revision {based_on_revision_id}, but the "
            f"current revision is {current_revision.id} — refusing to guess which one was meant"
        )

    current_values = _revision_values(current_revision)
    if revision_type == ContractRevisionType.SUPPLEMENT:
        for key, value in fields.items():
            existing_value = current_values[key]
            if existing_value is not None and existing_value != value:
                raise ContractFactConflict(
                    f"Contract {contract_id}: field {key!r} is already known as {existing_value!r} — use "
                    "correction, not supplement"
                )
    else:  # CORRECTION
        for key in fields:
            if current_values[key] is None:
                raise ContractFactConflict(
                    f"Contract {contract_id}: field {key!r} has no existing value to correct — use supplement"
                )

    merged = dict(current_values)
    merged.update(fields)
    new_revision = ContractRevision(
        id=uuid.uuid4(),
        contract_id=contract_id,
        revision_type=revision_type,
        source_fragment_id=source_fragment_id,
        superseded_by_revision_id=None,
        created_at=created_at,
        asserted_field_names=sorted(fields.keys()),
        **merged,
    )
    retired = contract_repo.append_revision_against_current(new_revision, based_on_revision_id=based_on_revision_id)
    if not retired:
        raise ContractFactConflict(
            f"Contract {contract_id}: {revision_type.lower()} target revision {based_on_revision_id} was "
            "superseded concurrently — refusing to create a second current revision"
        )
    session.flush()
    updated_contract = contract_repo.get(contract_id)
    assert updated_contract is not None
    return ContractFactResult(contract=updated_contract, created=False, revision_written=True)


# ---------------------------------------------------------------------------
# execute_* — human/CLI/Web entry points, mirroring
# contract_item_facts.execute_*.
# ---------------------------------------------------------------------------


def _json_safe(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in fields.items()}


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
        id=uuid.uuid4(), file_name=f"manual-contract-fact-{now.isoformat()}.json", sha256=sha256,
        source_type=source_type, imported_at=now,
    )
    evidence_repo.add_document(document)
    fragment = EvidenceFragment(
        id=uuid.uuid4(), evidence_document_id=document.id, fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None, row_number=None, locator_json=locator, raw_data=raw_data, created_at=now,
    )
    evidence_repo.add_fragment(fragment)
    session.flush()
    return fragment


def execute_create_contract_fact(
    session: Session, *, contract_no: str, counterparty: str | None, fields: dict[str, Any]
) -> ContractFactResult:
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "contract-create", "contract_no": contract_no, "counterparty": counterparty,
            "fields": _json_safe(fields),
        }
        fragment = _find_or_build_manual_fragment(
            session, raw_data=raw_data, source_type="manual_contract_fact",
            locator={"command": "contract-create"}, now=now,
        )
        return create_contract_fact(
            session, contract_no=contract_no, counterparty=counterparty, fields=fields,
            source_fragment_id=fragment.id, created_at=now,
        )


def execute_supplement_contract_fact(
    session: Session, *, contract_id: uuid.UUID, based_on_revision_id: uuid.UUID, fields: dict[str, Any]
) -> ContractFactResult:
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "contract-supplement", "contract_id": str(contract_id),
            "based_on_revision_id": str(based_on_revision_id), "fields": _json_safe(fields),
        }
        fragment = _find_or_build_manual_fragment(
            session, raw_data=raw_data, source_type="manual_contract_fact",
            locator={"command": "contract-supplement"}, now=now,
        )
        return supplement_contract_fact(
            session, contract_id=contract_id, based_on_revision_id=based_on_revision_id, fields=fields,
            source_fragment_id=fragment.id, created_at=now,
        )


def execute_correct_contract_fact(
    session: Session, *, contract_id: uuid.UUID, based_on_revision_id: uuid.UUID, fields: dict[str, Any]
) -> ContractFactResult:
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "contract-correct", "contract_id": str(contract_id),
            "based_on_revision_id": str(based_on_revision_id), "fields": _json_safe(fields),
        }
        fragment = _find_or_build_manual_fragment(
            session, raw_data=raw_data, source_type="manual_contract_fact",
            locator={"command": "contract-correct"}, now=now,
        )
        return correct_contract_fact(
            session, contract_id=contract_id, based_on_revision_id=based_on_revision_id, fields=fields,
            source_fragment_id=fragment.id, created_at=now,
        )

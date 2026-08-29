"""ContractItem Fact Maintenance (Phase 2D.1-R1).

Establishes the everyday business intake path for ``ContractItem`` —
today the ONLY code path that creates one is the human-authored Close
Fact Pack (docs/V1-SCOPE.md section 2.2). This module is that path,
generalised into three explicit business intents, never an
undifferentiated ``update_contract_item(fields={...})``:

    human-supplied / confirmed Evidence
          |
          v
    create_contract_item_fact       — a ContractItem did not exist before
    supplement_contract_item_fact   — a previously-unknown field becomes known
    correct_contract_item_fact      — a previously-asserted value was wrong
          |
          v
    ContractItemRevision (anchor + revision model, docs/PHASE2D1-R0-DECISIONS.md 1.3)
          |
          v
    existing deterministic business logic (period_close.py, ...) reads the
    current revision through ContractItemRepository — unchanged, per the
    frozen assembly-seam requirement.

Frozen semantics this module implements (docs/PHASE2D1-R0-DECISIONS.md):

- 1.1 — the three cases never share one operation. Which one applies is
  the caller's explicit intent, never inferred from the shape of the
  data (section 12/13 of the R1 brief: "no guessing").
- 1.2 — every revision requires Evidence. ``execute_*`` wrappers build a
  MANUAL_FACT EvidenceDocument/Fragment for a human-confirmed input, with
  the same sha256 payload-dedup pattern as allocate_invoice_item.py; a
  caller that already has a fragment (e.g. an import pipeline building
  its own Evidence pass first, like import_close_facts.py) passes it
  straight through the ``*_fact`` core functions instead.
- 1.3 — identity anchor + revisions; no is_current sprawl (resolved once,
  in ContractItemRepository); revision_type is INITIAL | SUPPLEMENT |
  CORRECTION.
- 1.5 — recomputation after correction never silently rewrites a derived
  record. A CORRECTION that supersedes a revision persisted derived
  records still identity-reference raises a
  ``ExceptionType.CONTRACT_ITEM_FACT_SUPERSEDED`` Task naming both
  revisions and every affected record; nothing is edited or reversed
  automatically.
- 4.4 — business identity is ``(contract_id, source_item_key)``. A
  duplicate create is NEVER an unconditional "resolve to the existing
  anchor" (that was a Phase 2D.1-R1 Codex-fix BLOCKER): it is decided by
  comparing the incoming assertion against the existing INITIAL
  revision's own asserted content — see ``create_contract_item_fact``'s
  docstring for the four resulting outcomes (replay / corroborating /
  two flavours of conflict).

Idempotent replay (section 12 of the R1 brief) requires ALL of same
business identity/anchor + same Evidence fragment + same revision intent
(revision_type) + same asserted field/value content — not merely a
fragment-id match (a second Phase 2D.1-R1 Codex-fix BLOCKER: the same
fragment reused for a different intent, e.g. SUPPLEMENT then CORRECTION,
or for different field values, is a conflict, never a silent no-op).
``_asserted_fields`` reconstructs what a stored revision actually
asserted (as opposed to values merely carried forward from its
predecessor) so this comparison needs no separate assertion-metadata
column. A create-time "same identity, different Evidence, same
assertion" is corroborating Evidence, not a replay — see
``ContractItemFactResult.corroborating``.

What this module deliberately does NOT do, per the R1 brief: it does not
let a caller change ``contract_id`` or ``source_item_key`` (identity
re-identification is out of scope for R1 — docs/PHASE2D1-R0-DECISIONS.md
section 4.4 requires that to always produce a Task, which is R5/backfill
territory) and it does not add a generic Fact revision engine — the three
functions below are the whole surface.
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

from bel.domain.contract import (
    CONTRACT_ITEM_FACT_FIELDS,
    ContractItem,
    ContractItemRevision,
    ContractItemRevisionType,
)
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionStatus, ExceptionType, TaskException
from bel.infrastructure.persistence.database import serialized_write_transaction
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    AccrualRepository,
    ContractItemRepository,
    ContractRepository,
    EvidenceRepository,
    ExceptionRepository,
    HistoricalAccrualFactRepository,
    InvoiceItemAllocationRepository,
)


class ContractItemFactError(ValueError):
    """A rejected ContractItem Fact operation — missing anchor/contract,
    missing or unresolvable Evidence, or an unknown field name. Surfaces
    as an explicit failure, exactly like ``_ContractResolver``'s
    "rejecting, not guessing" precedent in import_close_facts.py — never
    a silent partial write."""


class ContractItemFactConflict(ContractItemFactError):
    """An explicit-intent conflict the system will not guess through:
    a supplement targeting an already-known, different value; a
    correction targeting a field with no existing value; or a
    supplement/correction that no longer targets the current revision.
    See docs/PHASE2D1-R0-DECISIONS.md sections 1.1 and 1.5 — a human
    must resolve this, never an inferred merge."""


@dataclass(frozen=True)
class ContractItemFactResult:
    """Four independent flags, because "nothing new was written" is not
    one outcome (Phase 2D.1-R1 Codex fix round, BLOCKER 1):

    - ``created`` — a brand-new anchor was written.
    - ``revision_written`` — a brand-new revision row was written
      (True for ``created``, True for a successful supplement/correct,
      False for every idempotent/no-op outcome below).
    - ``replay`` — an EXACT replay: same business identity/anchor, same
      Evidence fragment, same revision intent, same asserted content.
      Nothing new was written; the ORIGINAL revision already says this.
    - ``corroborating`` — CREATE-only: a DIFFERENT Evidence fragment
      asserting the SAME content as the existing INITIAL revision. Not a
      replay (the fragment differs), not a conflict (the content
      agrees), and not a second INITIAL revision — the existing anchor
      is returned unchanged, and the new fragment stays on record as its
      own independent, immutable piece of corroborating Evidence."""

    item: ContractItem
    created: bool = False
    revision_written: bool = True
    replay: bool = False
    corroborating: bool = False


def _validate_fields(fields: dict[str, Any]) -> None:
    unknown = sorted(set(fields) - set(CONTRACT_ITEM_FACT_FIELDS))
    if unknown:
        raise ContractItemFactError(f"unknown ContractItem field(s): {unknown}")


def _revision_values(revision: ContractItemRevision) -> dict[str, Any]:
    return {field: getattr(revision, field) for field in CONTRACT_ITEM_FACT_FIELDS}


def _normalized(fields: dict[str, Any]) -> dict[str, Any]:
    return {field: fields.get(field) for field in CONTRACT_ITEM_FACT_FIELDS}


def _require_fragment(session: Session, source_fragment_id: uuid.UUID) -> None:
    if EvidenceRepository(session).get_fragment(source_fragment_id) is None:
        raise ContractItemFactError(f"EvidenceFragment {source_fragment_id} not found")


def _asserted_fields(session: Session, revision: ContractItemRevision) -> dict[str, Any]:
    """The exact field/value set THIS revision actually asserted, as
    opposed to values merely carried forward unchanged from its
    predecessor.

    Phase 2D.1-R1 Codex fix round #2: this MUST read the persisted
    ``asserted_field_names`` (captured verbatim by ``create_contract_item_fact``
    / ``_apply_revision`` at write time) rather than reconstruct it by
    diffing against the predecessor. Diffing is lossy: a caller is
    entitled to re-assert a field that ALREADY holds the value being
    supplied (docs/PHASE2D1-R0-DECISIONS.md 1.1's "resupplying the same
    value is harmless") in the SAME call that also supplies a genuinely
    new field, and the stored snapshot of that revision then looks
    IDENTICAL whether or not the unchanged field was explicitly named —
    a diff can never tell those two cases apart, which silently dropped
    the unchanged field from the reconstructed assertion and broke exact
    replay for that call.

    ``asserted_field_names`` is ``None`` only for revisions with no
    captured intent — legacy data carried forward by the migration, and
    the ``ContractItemRepository.add()`` test convenience — for which we
    fall back to a best-effort reconstruction: an INITIAL revision's own
    non-NULL fields (there is no predecessor to lose information
    against), or a non-INITIAL revision's diff against its predecessor
    (imperfect for the same reason above, but strictly better than
    nothing for data no command ever wrote through the new API)."""
    if revision.asserted_field_names is not None:
        values = _revision_values(revision)
        return {field: values[field] for field in revision.asserted_field_names}

    predecessor = ContractItemRepository(session).find_predecessor(revision.id)
    values = _revision_values(revision)
    if predecessor is None:
        return {field: value for field, value in values.items() if value is not None}
    predecessor_values = _revision_values(predecessor)
    return {field: value for field, value in values.items() if value != predecessor_values[field]}


# ---------------------------------------------------------------------------
# Core commands — session-transaction-agnostic, per allocate_invoice_item.py's
# split: these never commit. import_close_facts.py calls create_* directly
# inside its own transaction; execute_* below owns the CLI/Web transaction.
# ---------------------------------------------------------------------------


def create_contract_item_fact(
    session: Session,
    *,
    contract_id: uuid.UUID,
    source_item_key: str,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> ContractItemFactResult:
    """Case A (docs/PHASE2D1-R0-DECISIONS.md 1.1): a ContractItem did not
    exist before. ``fields`` may be a partial subset — R007 permits a
    contract/supplier/amount-only assertion with product/quantity still
    unknown.

    A duplicate ``(contract_id, source_item_key)`` is NEVER a blind
    "return existing anchor" (Phase 2D.1-R1 Codex fix round, BLOCKER 1).
    Four outcomes, decided by comparing the incoming assertion (the
    non-NULL entries of ``fields``) against the existing INITIAL
    revision's own asserted content:

    1. Same Evidence fragment, same assertion -> EXACT REPLAY
       (``replay=True``). Nothing new is written.
    2. Different Evidence fragment, same assertion -> CORROBORATING
       (``corroborating=True``). Not a replay, not a conflict, not a
       second INITIAL revision — the new fragment simply stands as its
       own independent, immutable corroborating Evidence.
    3. Different Evidence fragment, conflicting assertion ->
       ``ContractItemFactConflict``. ``create`` never decides whether the
       caller meant supplement or correction.
    4. Same Evidence fragment, DIFFERENT assertion -> also
       ``ContractItemFactConflict`` — the same immutable artifact cannot
       be asked to mean two different things.

    Only when no anchor exists yet does this create a genuinely new one
    (``created=True``)."""
    if not source_item_key:
        raise ContractItemFactError("source_item_key is required")
    _validate_fields(fields)
    if ContractRepository(session).get(contract_id) is None:
        raise ContractItemFactError(f"Contract {contract_id} not found")
    _require_fragment(session, source_fragment_id)

    item_repo = ContractItemRepository(session)
    existing = item_repo.find_by_contract_and_key(contract_id, source_item_key)
    if existing is not None:
        initial_revision = item_repo.get_initial_revision(existing.id)
        assert initial_revision is not None
        existing_assertion = _asserted_fields(session, initial_revision)
        incoming_assertion = {key: value for key, value in fields.items() if value is not None}
        same_fragment = initial_revision.source_fragment_id == source_fragment_id
        if incoming_assertion == existing_assertion:
            return ContractItemFactResult(
                item=existing, created=False, revision_written=False, replay=same_fragment, corroborating=not same_fragment
            )
        if same_fragment:
            raise ContractItemFactConflict(
                f"ContractItem {existing.id}: source_fragment_id {source_fragment_id} was already used to "
                f"assert {existing_assertion!r} as the INITIAL revision — this call asserts "
                f"{incoming_assertion!r}, a different content under the SAME Evidence, which cannot both be true"
            )
        raise ContractItemFactConflict(
            f"ContractItem {existing.id} already asserts {existing_assertion!r}; Evidence "
            f"{source_fragment_id} asserts {incoming_assertion!r} instead — conflicting values under "
            "different Evidence require an explicit supplement (if previously unknown) or correction "
            "(if previously wrong); create never guesses which"
        )

    anchor_id = uuid.uuid4()
    item_repo.create_anchor(id=anchor_id, contract_id=contract_id, source_item_key=source_item_key, created_at=created_at)
    item_repo.create_initial_revision(
        ContractItemRevision(
            id=uuid.uuid4(),
            contract_item_id=anchor_id,
            revision_type=ContractItemRevisionType.INITIAL,
            source_fragment_id=source_fragment_id,
            superseded_by_revision_id=None,
            created_at=created_at,
            asserted_field_names=sorted(key for key, value in fields.items() if value is not None),
            **_normalized(fields),
        )
    )
    session.flush()
    created_item = item_repo.get(anchor_id)
    assert created_item is not None
    return ContractItemFactResult(item=created_item, created=True, revision_written=True)


def supplement_contract_item_fact(
    session: Session,
    *,
    contract_item_id: uuid.UUID,
    based_on_revision_id: uuid.UUID,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> ContractItemFactResult:
    """Case B-supplement: a previously-unknown attribute becomes known.
    Every key in ``fields`` must currently be NULL on the current
    revision — supplementing a field that already holds a DIFFERENT
    value is rejected (``ContractItemFactConflict``: that is a
    correction, and this function does not guess the caller meant one).
    Resupplying the SAME value is accepted as harmless."""
    return _apply_revision(
        session,
        contract_item_id=contract_item_id,
        based_on_revision_id=based_on_revision_id,
        fields=fields,
        source_fragment_id=source_fragment_id,
        created_at=created_at,
        revision_type=ContractItemRevisionType.SUPPLEMENT,
    )


def correct_contract_item_fact(
    session: Session,
    *,
    contract_item_id: uuid.UUID,
    based_on_revision_id: uuid.UUID,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> ContractItemFactResult:
    """Case B-correction: a previously-asserted value was wrong. Every
    key in ``fields`` must currently hold a NON-NULL value on the current
    revision — correcting a field with no existing value is rejected
    (``ContractItemFactConflict``: that is a supplement).

    When the correction supersedes a revision that persisted derived
    records (InvoiceItemAllocation, Accrual, AccrualBasisFact,
    HistoricalAccrualFact) still identity-reference, a
    ``CONTRACT_ITEM_FACT_SUPERSEDED`` Task is raised naming both
    revisions and every affected record (docs/PHASE2D1-R0-DECISIONS.md
    section 1.5) — nothing is silently rewritten or reversed."""
    result = _apply_revision(
        session,
        contract_item_id=contract_item_id,
        based_on_revision_id=based_on_revision_id,
        fields=fields,
        source_fragment_id=source_fragment_id,
        created_at=created_at,
        revision_type=ContractItemRevisionType.CORRECTION,
    )
    if result.revision_written:
        _flag_dependents_on_correction(
            session,
            contract_item_id=contract_item_id,
            superseded_revision_id=based_on_revision_id,
            superseding_revision_id=_current_revision_id(session, contract_item_id),
            created_at=created_at,
        )
    return result


def get_contract_item(session: Session, contract_item_id: uuid.UUID) -> ContractItem | None:
    """Current authoritative state, for the CLI's ``contract-item show``
    and any future Web equivalent."""
    return ContractItemRepository(session).get(contract_item_id)


def get_contract_item_history(session: Session, contract_item_id: uuid.UUID) -> list[ContractItemRevision]:
    """Full audit trail for the CLI's ``contract-item history`` and any
    future Web equivalent — every revision ever asserted, oldest first,
    each still carrying its own ``source_fragment_id`` and
    ``superseded_by_revision_id``."""
    return ContractItemRepository(session).list_revisions(contract_item_id)


def _current_revision_id(session: Session, contract_item_id: uuid.UUID) -> uuid.UUID:
    current = ContractItemRepository(session).get_current_revision(contract_item_id)
    assert current is not None
    return current.id


def _apply_revision(
    session: Session,
    *,
    contract_item_id: uuid.UUID,
    based_on_revision_id: uuid.UUID,
    fields: dict[str, Any],
    source_fragment_id: uuid.UUID,
    created_at: datetime,
    revision_type: str,
) -> ContractItemFactResult:
    _validate_fields(fields)
    if not fields:
        raise ContractItemFactError(f"{revision_type.lower()} requires at least one field")

    item_repo = ContractItemRepository(session)
    current_item = item_repo.get(contract_item_id)
    if current_item is None:
        raise ContractItemFactError(f"ContractItem {contract_item_id} not found")
    _require_fragment(session, source_fragment_id)

    # Reuse/replay check FIRST, but a fragment hit is never automatically a
    # replay (Phase 2D.1-R1 Codex fix round, BLOCKER 2): the SAME Evidence
    # fragment reused for a DIFFERENT revision_type, or for the SAME type
    # asserting DIFFERENT field/value content, is a conflict — not a
    # silent no-op that swallows the second call.
    reused_revision = item_repo.find_revision_by_fragment(contract_item_id, source_fragment_id)
    if reused_revision is not None:
        if reused_revision.revision_type != revision_type:
            raise ContractItemFactConflict(
                f"ContractItem {contract_item_id}: source_fragment_id {source_fragment_id} was already "
                f"used for a {reused_revision.revision_type}; this call asks for {revision_type} — a "
                "different intent under the SAME Evidence is never inferred"
            )
        reused_assertion = _asserted_fields(session, reused_revision)
        if reused_assertion != fields:
            raise ContractItemFactConflict(
                f"ContractItem {contract_item_id}: source_fragment_id {source_fragment_id} already "
                f"asserted {reused_assertion!r} as a {revision_type}; this call asserts {fields!r} — a "
                "different content under the SAME Evidence, which cannot both be true"
            )
        # Exact replay: same fragment, same intent, same content.
        return ContractItemFactResult(item=current_item, created=False, revision_written=False, replay=True)

    current_revision = item_repo.get_current_revision(contract_item_id)
    assert current_revision is not None
    if current_revision.id != based_on_revision_id:
        raise ContractItemFactConflict(
            f"ContractItem {contract_item_id}: {revision_type.lower()} targets revision "
            f"{based_on_revision_id}, but the current revision is {current_revision.id} — "
            "refusing to guess which one was meant"
        )

    current_values = _revision_values(current_revision)
    if revision_type == ContractItemRevisionType.SUPPLEMENT:
        for key, value in fields.items():
            existing_value = current_values[key]
            if existing_value is not None and existing_value != value:
                raise ContractItemFactConflict(
                    f"ContractItem {contract_item_id}: field {key!r} is already known as "
                    f"{existing_value!r} — use correction, not supplement"
                )
    else:  # CORRECTION
        for key in fields:
            if current_values[key] is None:
                raise ContractItemFactConflict(
                    f"ContractItem {contract_item_id}: field {key!r} has no existing value to "
                    "correct — use supplement"
                )

    merged = dict(current_values)
    merged.update(fields)
    new_revision = ContractItemRevision(
        id=uuid.uuid4(),
        contract_item_id=contract_item_id,
        revision_type=revision_type,
        source_fragment_id=source_fragment_id,
        superseded_by_revision_id=None,
        created_at=created_at,
        asserted_field_names=sorted(fields.keys()),
        **merged,
    )
    # Atomic conditional retire-then-insert (Phase 2D.1-R1 Codex fix round,
    # BLOCKERs 3-4): if based_on_revision_id was superseded by someone else
    # between our read above and this call — a genuine race between two
    # independent sessions — this writes NOTHING and returns False, which
    # we surface as a conflict rather than ever installing a second current
    # revision. The uq_contract_item_revisions_one_current partial unique
    # index is the DB-level backstop behind this same guarantee.
    retired = item_repo.append_revision_against_current(new_revision, based_on_revision_id=based_on_revision_id)
    if not retired:
        raise ContractItemFactConflict(
            f"ContractItem {contract_item_id}: {revision_type.lower()} target revision "
            f"{based_on_revision_id} was superseded concurrently — refusing to create a second "
            "current revision"
        )
    session.flush()
    updated_item = item_repo.get(contract_item_id)
    assert updated_item is not None
    return ContractItemFactResult(item=updated_item, created=False, revision_written=True)


def _flag_dependents_on_correction(
    session: Session,
    *,
    contract_item_id: uuid.UUID,
    superseded_revision_id: uuid.UUID,
    superseding_revision_id: uuid.UUID,
    created_at: datetime,
) -> None:
    dependents: dict[str, list[str]] = {}
    allocations = InvoiceItemAllocationRepository(session).list_for_contract_item(contract_item_id)
    if allocations:
        dependents["invoice_item_allocations"] = [str(a.id) for a in allocations]
    accruals = AccrualRepository(session).list_for_contract_item(contract_item_id)
    if accruals:
        dependents["accruals"] = [str(a.id) for a in accruals]
    basis_facts = AccrualBasisFactRepository(session).list_for_contract_item(contract_item_id)
    if basis_facts:
        dependents["accrual_basis_facts"] = [str(f.id) for f in basis_facts]
    historical_facts = HistoricalAccrualFactRepository(session).list_for_contract_item(contract_item_id)
    if historical_facts:
        dependents["historical_accrual_facts"] = [str(f.id) for f in historical_facts]

    if not dependents:
        return

    ExceptionRepository(session).add(
        TaskException(
            id=uuid.uuid4(),
            exception_type=ExceptionType.CONTRACT_ITEM_FACT_SUPERSEDED,
            status=ExceptionStatus.OPEN,
            summary=f"ContractItem {contract_item_id} corrected while derived records reference it",
            detail={
                "contract_item_id": str(contract_item_id),
                "superseded_revision_id": str(superseded_revision_id),
                "superseding_revision_id": str(superseding_revision_id),
                "dependents": dependents,
            },
            created_at=created_at,
        )
    )


# ---------------------------------------------------------------------------
# execute_* — the human/CLI/Web entry points. Each builds its own
# MANUAL_FACT Evidence (a human confirmation IS Evidence, per DOMAIN.md)
# with the same sha256 payload-dedup pattern as allocate_invoice_item.py,
# then runs the matching core command inside the shared serialized write
# boundary (database.py) — the SAME boundary every manual writer in this
# codebase shares.
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
        id=uuid.uuid4(),
        file_name=f"manual-contract-item-fact-{now.isoformat()}.json",
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


def execute_create_contract_item_fact(
    session: Session, *, contract_id: uuid.UUID, source_item_key: str, fields: dict[str, Any]
) -> ContractItemFactResult:
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "contract-item-create",
            "contract_id": str(contract_id),
            "source_item_key": source_item_key,
            "fields": _json_safe(fields),
        }
        fragment = _find_or_build_manual_fragment(
            session,
            raw_data=raw_data,
            source_type="manual_contract_item_fact",
            locator={"command": "contract-item-create"},
            now=now,
        )
        return create_contract_item_fact(
            session,
            contract_id=contract_id,
            source_item_key=source_item_key,
            fields=fields,
            source_fragment_id=fragment.id,
            created_at=now,
        )


def execute_supplement_contract_item_fact(
    session: Session, *, contract_item_id: uuid.UUID, based_on_revision_id: uuid.UUID, fields: dict[str, Any]
) -> ContractItemFactResult:
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "contract-item-supplement",
            "contract_item_id": str(contract_item_id),
            "based_on_revision_id": str(based_on_revision_id),
            "fields": _json_safe(fields),
        }
        fragment = _find_or_build_manual_fragment(
            session,
            raw_data=raw_data,
            source_type="manual_contract_item_fact",
            locator={"command": "contract-item-supplement"},
            now=now,
        )
        return supplement_contract_item_fact(
            session,
            contract_item_id=contract_item_id,
            based_on_revision_id=based_on_revision_id,
            fields=fields,
            source_fragment_id=fragment.id,
            created_at=now,
        )


def execute_correct_contract_item_fact(
    session: Session, *, contract_item_id: uuid.UUID, based_on_revision_id: uuid.UUID, fields: dict[str, Any]
) -> ContractItemFactResult:
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "contract-item-correct",
            "contract_item_id": str(contract_item_id),
            "based_on_revision_id": str(based_on_revision_id),
            "fields": _json_safe(fields),
        }
        fragment = _find_or_build_manual_fragment(
            session,
            raw_data=raw_data,
            source_type="manual_contract_item_fact",
            locator={"command": "contract-item-correct"},
            now=now,
        )
        return correct_contract_item_fact(
            session,
            contract_item_id=contract_item_id,
            based_on_revision_id=based_on_revision_id,
            fields=fields,
            source_fragment_id=fragment.id,
            created_at=now,
        )

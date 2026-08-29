"""ProcurementSalesLink Fact Maintenance (Phase 2D.1-R3a Slice 2).

Establishes the canonical procurement/sales bridge
(docs/PHASE2D1-R0-DECISIONS.md section 2.4) as three explicit,
never-inferred business intents:

    Evidence (procurement ledger row, or manual confirmation)
          |
          v
    add_procurement_sales_link          — the business key has never existed
    correct_procurement_sales_link      — retire the CURRENT episode (pure
                                           invalidation or replacement)
    reestablish_procurement_sales_link  — the business key is retired,
                                           none current; new Evidence +
                                           explicit HUMAN_CONFIRMED
          |
          v
    ProcurementSalesLink / ProcurementSalesLinkCorrection
    (append-only Facts, never mutated — see domain.procurement_sales_link)
          |
          v
    query / read model (current link, episode history, linked scopes)

This is deliberately NOT a fourth copy of the anchor+revision engine
(ContractItem/Shipment/SalesContract): a link has no field to supplement
in place. The whole Fact is "this relationship is confirmed", asserted
once per episode; correction operates at the relationship level by
retiring the whole episode, never by editing one.

Frozen semantics this module implements
(docs/PHASE2D1-R0-DECISIONS.md section 2.4):

- Two-layer identity: relationship business key
  `(procurement_contract_id, sales_contract_id)` vs. one row per
  confirmed assertion episode. At most one CURRENT episode per business
  key; history may hold several.
- The three creation actions are explicit and never inferred from data
  shape: `ADD` (no current, no retired), `CORRECT`/`INVALIDATE` (targets
  a current episode), `REESTABLISH` (retired exists, none current — new
  Evidence + explicit `HUMAN_CONFIRMED`).
- `REESTABLISH` is not resurrection: it always writes a NEW episode; the
  retired one stays retired forever, unmutated.
- Replay protection rests on PER-EPISODE provenance: the same
  `source_fragment_id` for the same business key is idempotent; a
  fragment that created a now-retired episode can never, by replay,
  create a current one — only new Evidence + explicit `REESTABLISH`
  intent can.
- `current(link) ⟺ no ProcurementSalesLinkCorrection names it as
  superseded_link_id` — resolved in exactly one place,
  `ProcurementSalesLinkRepository` (see that class's docstring for the
  storage-level backstop). No code here ever inspects `created_at` to
  decide which episode is newer.
- Additive vs. corrective is a human/caller determination, never
  inferred: because the bridge is many-to-many, a second current link
  from one procurement Contract to a DIFFERENT SalesContract is
  legitimate, not a conflict — it produces a
  `PROCUREMENT_SALES_LINK_MULTIPLE_SCOPES` ambiguity Task (attribution is
  undecidable from the link alone) but is never rejected and never
  auto-apportioned.
- A correction's replacement, if any, is resolved (not guessed): if the
  replacement business key already has a current episode, the correction
  references it; otherwise a new confirmed replacement episode is
  created in the SAME transaction as the correction (never observably
  dual-current, never orphaned if the correction itself then loses a
  race — see `correct_procurement_sales_link`'s use of a SAVEPOINT).

What this module deliberately does NOT do: implement `SalesInvoiceAllocation`
/ `SalesPaymentAllocation` / `SalesMatchCandidate` / any amount-based sales
matching (R3b); apportion any amount or quantity across the bridge; let a
`Shipment` create a link automatically (3.6); let `ContractItem`
participate (V1 excludes it entirely); or add a `status`/`is_current`/
`superseded_by_link_id` field anywhere — see domain.procurement_sales_link
for why that would create a second, competing source of truth for
"current".
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionStatus, ExceptionType, TaskException
from bel.domain.procurement_sales_link import ConfirmationType, ProcurementSalesLink, ProcurementSalesLinkCorrection
from bel.infrastructure.persistence.database import serialized_write_transaction
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    ExceptionRepository,
    ProcurementSalesLinkRepository,
    SalesContractRepository,
)


class ProcurementSalesLinkFactError(ValueError):
    """A rejected ProcurementSalesLink operation — missing/unresolvable
    endpoint or Evidence, an unknown `confirmation_type`, an action whose
    precondition is not met (e.g. `ADD` where retired history exists), or
    a correction attempted with `confirmation_type != HUMAN_CONFIRMED`."""


class ProcurementSalesLinkFactConflict(ProcurementSalesLinkFactError):
    """An explicit-intent conflict the system will not guess through: a
    correction targeting an already-retired or already-corrected episode,
    or a race lost to a concurrent write for the same business key /
    `superseded_link_id`. A human must resolve this, never an inferred
    merge or retry."""


@dataclass(frozen=True)
class ProcurementSalesLinkResult:
    """`created` / `replay` / `corroborating` are independent flags —
    "nothing new was written" is not one outcome (mirrors
    `SalesContractFactResult`'s rationale)."""

    link: ProcurementSalesLink
    created: bool = False
    replay: bool = False
    corroborating: bool = False


@dataclass(frozen=True)
class ProcurementSalesLinkCorrectionResult:
    correction: ProcurementSalesLinkCorrection
    replacement_link: ProcurementSalesLink | None
    created: bool = False
    replay: bool = False


@dataclass(frozen=True)
class EpisodeHistoryEntry:
    """One row of a relationship business key's full audit trail — for
    the CLI's `sales-link history` and any future Web equivalent."""

    episode: ProcurementSalesLink
    current: bool
    correction: ProcurementSalesLinkCorrection | None


def _validate_endpoints(procurement_contract_id: uuid.UUID | None, sales_contract_id: uuid.UUID | None) -> None:
    if procurement_contract_id is None or sales_contract_id is None:
        raise ProcurementSalesLinkFactError(
            "both procurement_contract_id and sales_contract_id are required — neither end of the "
            "relationship business key may be empty"
        )


def _validate_confirmation_type(confirmation_type: str) -> None:
    if confirmation_type not in (ConfirmationType.AUTO_CONFIRMED, ConfirmationType.HUMAN_CONFIRMED):
        raise ProcurementSalesLinkFactError(f"unknown confirmation_type: {confirmation_type!r}")


def _require_procurement_contract(session: Session, procurement_contract_id: uuid.UUID) -> None:
    if ContractRepository(session).get(procurement_contract_id) is None:
        raise ProcurementSalesLinkFactError(f"Contract {procurement_contract_id} not found")


def _require_sales_contract(session: Session, sales_contract_id: uuid.UUID) -> None:
    if SalesContractRepository(session).get(sales_contract_id) is None:
        raise ProcurementSalesLinkFactError(f"SalesContract {sales_contract_id} not found")


def _require_fragment(session: Session, source_fragment_id: uuid.UUID) -> None:
    if EvidenceRepository(session).get_fragment(source_fragment_id) is None:
        raise ProcurementSalesLinkFactError(f"EvidenceFragment {source_fragment_id} not found")


def _flag_multiple_sales_scopes_if_ambiguous(
    session: Session, repo: ProcurementSalesLinkRepository, procurement_contract_id: uuid.UUID, created_at: datetime
) -> None:
    """docs/PHASE2D1-R0-DECISIONS.md section 2.4's cardinality table: one
    procurement Contract with more than one current link is legitimate,
    but attribution is undecidable from the link alone — surface a Task,
    never choose. Idempotent per `procurement_contract_id` alone (not the
    exact scope set) so a THIRD scope later added does not pile up a
    second Task — a human resolving the ambiguity consults the read
    model for the current set, not a frozen snapshot in the Task
    detail."""
    current_links = repo.list_current_links_for_procurement_contract(procurement_contract_id)
    if len(current_links) <= 1:
        return
    for task in ExceptionRepository(session).list_open():
        if (
            task.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_MULTIPLE_SCOPES
            and task.detail.get("procurement_contract_id") == str(procurement_contract_id)
        ):
            return
    sales_contract_ids = sorted(str(link.sales_contract_id) for link in current_links)
    ExceptionRepository(session).add(
        TaskException(
            id=uuid.uuid4(),
            exception_type=ExceptionType.PROCUREMENT_SALES_LINK_MULTIPLE_SCOPES,
            status=ExceptionStatus.OPEN,
            summary=f"Procurement Contract {procurement_contract_id} has multiple current sales scopes",
            detail={"procurement_contract_id": str(procurement_contract_id), "sales_contract_ids": sales_contract_ids},
            created_at=created_at,
        )
    )
    session.flush()


def _flag_correction_conflict(
    session: Session, superseded_link_id: uuid.UUID, conflicting_source_fragment_id: uuid.UUID, created_at: datetime
) -> None:
    for task in ExceptionRepository(session).list_open():
        if (
            task.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_CORRECTION_CONFLICT
            and task.detail.get("superseded_link_id") == str(superseded_link_id)
            and task.detail.get("conflicting_source_fragment_id") == str(conflicting_source_fragment_id)
        ):
            return
    ExceptionRepository(session).add(
        TaskException(
            id=uuid.uuid4(),
            exception_type=ExceptionType.PROCUREMENT_SALES_LINK_CORRECTION_CONFLICT,
            status=ExceptionStatus.OPEN,
            summary=f"ProcurementSalesLink {superseded_link_id} has a conflicting correction attempt",
            detail={
                "superseded_link_id": str(superseded_link_id),
                "conflicting_source_fragment_id": str(conflicting_source_fragment_id),
            },
            created_at=created_at,
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# Core commands — session-transaction-agnostic, per the established
# split (contract_item_facts.py / shipment_facts.py / sales_contract_facts.py):
# these never commit. execute_* below owns the CLI/Web transaction.
# ---------------------------------------------------------------------------


def add_procurement_sales_link(
    session: Session,
    *,
    procurement_contract_id: uuid.UUID | None,
    sales_contract_id: uuid.UUID | None,
    source_fragment_id: uuid.UUID,
    confirmation_type: str,
    created_at: datetime,
) -> ProcurementSalesLinkResult:
    """`ADD` (docs/PHASE2D1-R0-DECISIONS.md section 2.4): the business
    key has no current AND no retired episode. If ANY episode has ever
    existed for this business key (`ADD`'s precondition is not met),
    this raises `ProcurementSalesLinkFactError` unconditionally and
    directs the caller to `REESTABLISH` — this is also what makes
    replaying an OLD fragment that created a now-retired episode safe:
    `ADD` never resurrects, because it never even considers the
    fragment, only whether history exists at all.

    If a current episode already exists for this business key: the SAME
    fragment is an exact replay (idempotent, nothing written); a
    DIFFERENT fragment is corroborating Evidence for the SAME already-true
    relationship (also nothing written, no conflict — a link asserts only
    "this relationship exists", so there is no content within one pair
    to disagree about)."""
    _validate_endpoints(procurement_contract_id, sales_contract_id)
    _validate_confirmation_type(confirmation_type)
    _require_procurement_contract(session, procurement_contract_id)
    _require_sales_contract(session, sales_contract_id)
    _require_fragment(session, source_fragment_id)

    repo = ProcurementSalesLinkRepository(session)
    current = repo.get_current_link(procurement_contract_id, sales_contract_id)
    if current is not None:
        if current.source_fragment_id == source_fragment_id:
            return ProcurementSalesLinkResult(link=current, replay=True)
        return ProcurementSalesLinkResult(link=current, corroborating=True)

    episodes = repo.list_episodes(procurement_contract_id, sales_contract_id)
    if episodes:
        raise ProcurementSalesLinkFactError(
            f"relationship ({procurement_contract_id}, {sales_contract_id}) has retired history but no current "
            "episode — ADD cannot create here; use REESTABLISH with new Evidence and an explicit "
            "HUMAN_CONFIRMED re-establishment"
        )

    link = ProcurementSalesLink(
        id=uuid.uuid4(),
        procurement_contract_id=procurement_contract_id,
        sales_contract_id=sales_contract_id,
        source_fragment_id=source_fragment_id,
        confirmation_type=confirmation_type,
        created_at=created_at,
    )
    if not repo.insert_episode_if_no_current(link):
        # Lost a race against a concurrent ADD/REESTABLISH for the SAME
        # business key. Re-resolve deterministically against whoever won
        # rather than raising blindly — the winner is now authoritative.
        winner = repo.get_current_link(procurement_contract_id, sales_contract_id)
        assert winner is not None
        if winner.source_fragment_id == source_fragment_id:
            return ProcurementSalesLinkResult(link=winner, replay=True)
        return ProcurementSalesLinkResult(link=winner, corroborating=True)

    _flag_multiple_sales_scopes_if_ambiguous(session, repo, procurement_contract_id, created_at)
    # Re-fetch rather than return the pre-insert Python object: SQLite has
    # no native timezone storage, so a round-tripped `created_at` compares
    # unequal to the tz-aware value the caller originally passed in —
    # every later read (get_current_procurement_sales_link, history, ...)
    # already reads the round-tripped form, so the CREATE result must too.
    persisted = repo.get(link.id)
    assert persisted is not None
    return ProcurementSalesLinkResult(link=persisted, created=True)


def record_unconfirmed_procurement_sales_link(
    session: Session,
    *,
    procurement_contract_id: uuid.UUID,
    sales_contract_id: uuid.UUID,
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> bool:
    """Evidence merely SUGGESTS a pairing — no confirmation action has
    been taken. NEVER creates a `ProcurementSalesLink` row (section 2.4:
    "a link exists only for a confirmed relationship"); persists an
    idempotent `PROCUREMENT_SALES_LINK_UNCONFIRMED` Task instead. Returns
    `True` if a new Task was written, `False` if an identical one
    (same candidate pair, same Evidence) already existed."""
    _validate_endpoints(procurement_contract_id, sales_contract_id)
    _require_fragment(session, source_fragment_id)
    for task in ExceptionRepository(session).list_open():
        if (
            task.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_UNCONFIRMED
            and task.detail.get("procurement_contract_id") == str(procurement_contract_id)
            and task.detail.get("sales_contract_id") == str(sales_contract_id)
            and task.detail.get("source_fragment_id") == str(source_fragment_id)
        ):
            return False
    ExceptionRepository(session).add(
        TaskException(
            id=uuid.uuid4(),
            exception_type=ExceptionType.PROCUREMENT_SALES_LINK_UNCONFIRMED,
            status=ExceptionStatus.OPEN,
            summary="Evidence suggests a procurement/sales relationship that is not yet confirmed",
            detail={
                "procurement_contract_id": str(procurement_contract_id),
                "sales_contract_id": str(sales_contract_id),
                "source_fragment_id": str(source_fragment_id),
            },
            created_at=created_at,
        )
    )
    session.flush()
    return True


class _CorrectionRaceLost(Exception):
    """Internal control-flow signal only — never escapes
    `correct_procurement_sales_link`."""


def _same_replacement_business_key(
    repo: ProcurementSalesLinkRepository,
    existing_correction: ProcurementSalesLinkCorrection,
    replacement_procurement_contract_id: uuid.UUID | None,
    replacement_sales_contract_id: uuid.UUID | None,
) -> bool:
    if existing_correction.replacement_link_id is None:
        return replacement_procurement_contract_id is None and replacement_sales_contract_id is None
    if replacement_procurement_contract_id is None or replacement_sales_contract_id is None:
        return False
    existing_replacement = repo.get(existing_correction.replacement_link_id)
    return (
        existing_replacement is not None
        and existing_replacement.procurement_contract_id == replacement_procurement_contract_id
        and existing_replacement.sales_contract_id == replacement_sales_contract_id
    )


def correct_procurement_sales_link(
    session: Session,
    *,
    superseded_link_id: uuid.UUID,
    source_fragment_id: uuid.UUID,
    confirmation_type: str,
    created_at: datetime,
    replacement_procurement_contract_id: uuid.UUID | None = None,
    replacement_sales_contract_id: uuid.UUID | None = None,
) -> ProcurementSalesLinkCorrectionResult:
    """`CORRECT` / `INVALIDATE` (docs/PHASE2D1-R0-DECISIONS.md section
    2.4): targets a CURRENT episode only. `replacement_procurement_contract_id`
    and `replacement_sales_contract_id` both `None` -> pure invalidation
    (no replacement); both provided -> replacement, resolved atomically:
    if the replacement business key already has a current episode, the
    correction references it; otherwise a new confirmed replacement
    episode is created in the SAME transaction as the correction.

    V1-frozen: `confirmation_type` must be `HUMAN_CONFIRMED` —
    corrective Evidence alone never changes the authoritative
    relationship.

    Uses a SAVEPOINT (`session.begin_nested()`) around
    "resolve-replacement + write-correction" so that if the correction
    insert itself loses a race (someone else corrected the SAME episode
    concurrently), any replacement episode this call just created is
    rolled back to the savepoint rather than left as an orphaned current
    episode with no correction ever pointing at it — the exact
    "no observable A→X current AND A→Y newly-created replacement current
    without correction" state the frozen text forbids."""
    if confirmation_type != ConfirmationType.HUMAN_CONFIRMED:
        raise ProcurementSalesLinkFactError(
            "a correction must be HUMAN_CONFIRMED — corrective Evidence alone never changes the authoritative "
            "relationship (docs/PHASE2D1-R0-DECISIONS.md section 2.4)"
        )
    _require_fragment(session, source_fragment_id)
    has_replacement_procurement = replacement_procurement_contract_id is not None
    has_replacement_sales = replacement_sales_contract_id is not None
    if has_replacement_procurement != has_replacement_sales:
        raise ProcurementSalesLinkFactError(
            "a replacement business key requires BOTH replacement_procurement_contract_id and "
            "replacement_sales_contract_id, or neither for pure invalidation"
        )

    repo = ProcurementSalesLinkRepository(session)
    target = repo.get(superseded_link_id)
    if target is None:
        raise ProcurementSalesLinkFactError(f"ProcurementSalesLink {superseded_link_id} not found")

    existing_correction = repo.get_correction_for_superseded(superseded_link_id)
    if existing_correction is not None:
        if existing_correction.source_fragment_id == source_fragment_id and _same_replacement_business_key(
            repo, existing_correction, replacement_procurement_contract_id, replacement_sales_contract_id
        ):
            replacement_link = (
                repo.get(existing_correction.replacement_link_id) if existing_correction.replacement_link_id else None
            )
            return ProcurementSalesLinkCorrectionResult(
                correction=existing_correction, replacement_link=replacement_link, replay=True
            )
        _flag_correction_conflict(session, superseded_link_id, source_fragment_id, created_at)
        raise ProcurementSalesLinkFactConflict(
            f"ProcurementSalesLink {superseded_link_id} was already corrected by {existing_correction.id} — a "
            "second, different correction is rejected; lineage cannot fork"
        )

    if not repo.is_current(superseded_link_id):
        raise ProcurementSalesLinkFactConflict(
            f"ProcurementSalesLink {superseded_link_id} is already retired — only a CURRENT episode may be "
            "corrected; a retired episode is final"
        )

    if has_replacement_procurement:
        _require_procurement_contract(session, replacement_procurement_contract_id)
        _require_sales_contract(session, replacement_sales_contract_id)

    nested = session.begin_nested()
    try:
        replacement_link: ProcurementSalesLink | None = None
        if has_replacement_procurement:
            replacement_link = repo.get_current_link(replacement_procurement_contract_id, replacement_sales_contract_id)
            if replacement_link is None:
                candidate = ProcurementSalesLink(
                    id=uuid.uuid4(),
                    procurement_contract_id=replacement_procurement_contract_id,
                    sales_contract_id=replacement_sales_contract_id,
                    source_fragment_id=source_fragment_id,
                    confirmation_type=confirmation_type,
                    created_at=created_at,
                )
                if repo.insert_episode_if_no_current(candidate):
                    replacement_link = repo.get(candidate.id)  # re-fetch: see add_procurement_sales_link's note
                    assert replacement_link is not None
                else:
                    replacement_link = repo.get_current_link(
                        replacement_procurement_contract_id, replacement_sales_contract_id
                    )
                    assert replacement_link is not None

        correction = ProcurementSalesLinkCorrection(
            id=uuid.uuid4(),
            superseded_link_id=superseded_link_id,
            replacement_link_id=replacement_link.id if replacement_link is not None else None,
            source_fragment_id=source_fragment_id,
            confirmation_type=confirmation_type,
            created_at=created_at,
        )
        if not repo.add_correction_if_uncorrected(correction):
            raise _CorrectionRaceLost()
        nested.commit()
        correction = repo.get_correction_for_superseded(superseded_link_id)  # re-fetch, same tz-round-trip reason
        assert correction is not None
    except _CorrectionRaceLost:
        nested.rollback()
        _flag_correction_conflict(session, superseded_link_id, source_fragment_id, created_at)
        raise ProcurementSalesLinkFactConflict(
            f"ProcurementSalesLink {superseded_link_id} was corrected concurrently — refusing to create a "
            "second correction"
        )
    return ProcurementSalesLinkCorrectionResult(correction=correction, replacement_link=replacement_link, created=True)


def reestablish_procurement_sales_link(
    session: Session,
    *,
    procurement_contract_id: uuid.UUID | None,
    sales_contract_id: uuid.UUID | None,
    source_fragment_id: uuid.UUID,
    confirmation_type: str,
    created_at: datetime,
) -> ProcurementSalesLinkResult:
    """`REESTABLISH` (docs/PHASE2D1-R0-DECISIONS.md section 2.4): the
    business key has a RETIRED episode and NO current one. Always
    `HUMAN_CONFIRMED` and always requires genuinely NEW Evidence — never
    resurrects the old row; always writes a NEW episode with its own id
    and provenance."""
    if confirmation_type != ConfirmationType.HUMAN_CONFIRMED:
        raise ProcurementSalesLinkFactError(
            "REESTABLISH requires explicit HUMAN_CONFIRMED — never AUTO_CONFIRMED "
            "(docs/PHASE2D1-R0-DECISIONS.md section 2.4)"
        )
    _validate_endpoints(procurement_contract_id, sales_contract_id)
    _require_procurement_contract(session, procurement_contract_id)
    _require_sales_contract(session, sales_contract_id)
    _require_fragment(session, source_fragment_id)

    repo = ProcurementSalesLinkRepository(session)
    episodes = repo.list_episodes(procurement_contract_id, sales_contract_id)
    if not episodes:
        raise ProcurementSalesLinkFactError(
            f"relationship ({procurement_contract_id}, {sales_contract_id}) has no history at all — REESTABLISH "
            "requires a retired episode to exist; use ADD for a genuinely new relationship"
        )
    if repo.get_current_link(procurement_contract_id, sales_contract_id) is not None:
        raise ProcurementSalesLinkFactError(
            f"relationship ({procurement_contract_id}, {sales_contract_id}) already has a current episode — "
            "REESTABLISH only applies when none is current"
        )
    # Historical-Evidence replay guard (FROZEN, HARD, docs/PHASE2D1-R0-DECISIONS.md
    # section 2.4): a fragment that already played ANY evidentiary role in
    # this business key's history — whether it originally ASSERTED one of
    # the (now retired) episodes, or was the Evidence for the CORRECTION
    # that retired one — can NEVER, by being reprocessed, create a current
    # episode. Only genuinely NEW Evidence may. Checking only episodes'
    # own `source_fragment_id` would miss the correction's fragment,
    # which is equally "historical Evidence for this business key".
    historical_fragment_ids = {episode.source_fragment_id for episode in episodes}
    for episode in episodes:
        correction = repo.get_correction_for_superseded(episode.id)
        if correction is not None:
            historical_fragment_ids.add(correction.source_fragment_id)
    if source_fragment_id in historical_fragment_ids:
        raise ProcurementSalesLinkFactError(
            "REESTABLISH requires NEW Evidence — this source_fragment_id already played an evidentiary role "
            "in this business key's history (an episode assertion or a correction); replaying historical "
            "Evidence never produces a current episode"
        )

    link = ProcurementSalesLink(
        id=uuid.uuid4(),
        procurement_contract_id=procurement_contract_id,
        sales_contract_id=sales_contract_id,
        source_fragment_id=source_fragment_id,
        confirmation_type=confirmation_type,
        created_at=created_at,
    )
    if not repo.insert_episode_if_no_current(link):
        raise ProcurementSalesLinkFactConflict(
            f"relationship ({procurement_contract_id}, {sales_contract_id}) gained a current episode "
            "concurrently — refusing to create a second one"
        )
    _flag_multiple_sales_scopes_if_ambiguous(session, repo, procurement_contract_id, created_at)
    persisted = repo.get(link.id)
    assert persisted is not None
    return ProcurementSalesLinkResult(link=persisted, created=True)


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


def get_procurement_sales_link(session: Session, link_id: uuid.UUID) -> ProcurementSalesLink | None:
    return ProcurementSalesLinkRepository(session).get(link_id)


def get_current_procurement_sales_link(
    session: Session, procurement_contract_id: uuid.UUID, sales_contract_id: uuid.UUID
) -> ProcurementSalesLink | None:
    return ProcurementSalesLinkRepository(session).get_current_link(procurement_contract_id, sales_contract_id)


def list_procurement_sales_link_episodes(
    session: Session, procurement_contract_id: uuid.UUID, sales_contract_id: uuid.UUID
) -> list[ProcurementSalesLink]:
    return ProcurementSalesLinkRepository(session).list_episodes(procurement_contract_id, sales_contract_id)


def list_current_links_for_procurement_contract(
    session: Session, procurement_contract_id: uuid.UUID
) -> list[ProcurementSalesLink]:
    """Linked sales scopes for a procurement Contract — enumerated only,
    never summed/aggregated across the bridge (docs/PHASE2D1-R0-DECISIONS.md
    section 2.4: "No cross-bridge apportionment in V1")."""
    return ProcurementSalesLinkRepository(session).list_current_links_for_procurement_contract(
        procurement_contract_id
    )


def list_current_links_for_sales_contract(session: Session, sales_contract_id: uuid.UUID) -> list[ProcurementSalesLink]:
    return ProcurementSalesLinkRepository(session).list_current_links_for_sales_contract(sales_contract_id)


def get_relationship_history(
    session: Session, procurement_contract_id: uuid.UUID, sales_contract_id: uuid.UUID
) -> list[EpisodeHistoryEntry]:
    """Full audit trail for one business key — every episode ever
    asserted, oldest first, each annotated with its derived current/
    retired state and its correction record if any."""
    repo = ProcurementSalesLinkRepository(session)
    episodes = repo.list_episodes(procurement_contract_id, sales_contract_id)
    history = []
    for episode in episodes:
        correction = repo.get_correction_for_superseded(episode.id)
        history.append(EpisodeHistoryEntry(episode=episode, current=(correction is None), correction=correction))
    return history


# ---------------------------------------------------------------------------
# execute_* — the human/CLI/Web entry points. Each builds its own
# MANUAL_FACT Evidence (a human confirmation IS Evidence, per DOMAIN.md)
# with the same sha256 payload-dedup pattern as contract_item_facts.py /
# shipment_facts.py / sales_contract_facts.py, then runs the matching
# core command inside the shared serialized write boundary (database.py).
# ---------------------------------------------------------------------------


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
        file_name=f"manual-procurement-sales-link-{now.isoformat()}.json",
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


def execute_add_procurement_sales_link(
    session: Session,
    *,
    procurement_contract_id: uuid.UUID,
    sales_contract_id: uuid.UUID,
    confirmation_type: str = ConfirmationType.HUMAN_CONFIRMED,
) -> ProcurementSalesLinkResult:
    """`serialized_write_transaction` rolls back on ANY exception — which
    would silently discard a persisted Task the core command may have
    already flushed before raising (same lesson as
    `sales_contract_facts.execute_create_sales_contract_fact`'s Gate fix
    round). `ProcurementSalesLinkFactError` is therefore caught INSIDE
    the transaction and re-raised only AFTER it commits."""
    pending_error: ProcurementSalesLinkFactError | None = None
    result: ProcurementSalesLinkResult | None = None
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "sales-link-add",
            "procurement_contract_id": str(procurement_contract_id),
            "sales_contract_id": str(sales_contract_id),
            "confirmation_type": confirmation_type,
        }
        fragment = _find_or_build_manual_fragment(
            session,
            raw_data=raw_data,
            source_type="manual_procurement_sales_link",
            locator={"command": "sales-link-add"},
            now=now,
        )
        try:
            result = add_procurement_sales_link(
                session,
                procurement_contract_id=procurement_contract_id,
                sales_contract_id=sales_contract_id,
                source_fragment_id=fragment.id,
                confirmation_type=confirmation_type,
                created_at=now,
            )
        except ProcurementSalesLinkFactError as exc:
            pending_error = exc
    if pending_error is not None:
        raise pending_error
    assert result is not None
    return result


def execute_correct_procurement_sales_link(
    session: Session,
    *,
    superseded_link_id: uuid.UUID,
    replacement_procurement_contract_id: uuid.UUID | None = None,
    replacement_sales_contract_id: uuid.UUID | None = None,
) -> ProcurementSalesLinkCorrectionResult:
    pending_error: ProcurementSalesLinkFactError | None = None
    result: ProcurementSalesLinkCorrectionResult | None = None
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "sales-link-correct",
            "superseded_link_id": str(superseded_link_id),
            "replacement_procurement_contract_id": str(replacement_procurement_contract_id)
            if replacement_procurement_contract_id
            else None,
            "replacement_sales_contract_id": str(replacement_sales_contract_id)
            if replacement_sales_contract_id
            else None,
        }
        fragment = _find_or_build_manual_fragment(
            session,
            raw_data=raw_data,
            source_type="manual_procurement_sales_link",
            locator={"command": "sales-link-correct"},
            now=now,
        )
        try:
            result = correct_procurement_sales_link(
                session,
                superseded_link_id=superseded_link_id,
                source_fragment_id=fragment.id,
                confirmation_type=ConfirmationType.HUMAN_CONFIRMED,
                created_at=now,
                replacement_procurement_contract_id=replacement_procurement_contract_id,
                replacement_sales_contract_id=replacement_sales_contract_id,
            )
        except ProcurementSalesLinkFactError as exc:
            pending_error = exc
    if pending_error is not None:
        raise pending_error
    assert result is not None
    return result


def execute_reestablish_procurement_sales_link(
    session: Session, *, procurement_contract_id: uuid.UUID, sales_contract_id: uuid.UUID
) -> ProcurementSalesLinkResult:
    pending_error: ProcurementSalesLinkFactError | None = None
    result: ProcurementSalesLinkResult | None = None
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": "sales-link-reestablish",
            "procurement_contract_id": str(procurement_contract_id),
            "sales_contract_id": str(sales_contract_id),
        }
        fragment = _find_or_build_manual_fragment(
            session,
            raw_data=raw_data,
            source_type="manual_procurement_sales_link",
            locator={"command": "sales-link-reestablish"},
            now=now,
        )
        try:
            result = reestablish_procurement_sales_link(
                session,
                procurement_contract_id=procurement_contract_id,
                sales_contract_id=sales_contract_id,
                source_fragment_id=fragment.id,
                confirmation_type=ConfirmationType.HUMAN_CONFIRMED,
                created_at=now,
            )
        except ProcurementSalesLinkFactError as exc:
            pending_error = exc
    if pending_error is not None:
        raise pending_error
    assert result is not None
    return result

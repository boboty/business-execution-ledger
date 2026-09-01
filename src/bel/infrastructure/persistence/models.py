from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class EvidenceDocumentModel(Base):
    __tablename__ = "evidence_documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    file_name: Mapped[str] = mapped_column(String, nullable=False)
    # UNIQUE is the idempotency mechanism: re-importing the same bytes
    # must not create a second EvidenceDocument. See docs/PHASE1-DECISIONS.md.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class EvidenceFragmentModel(Base):
    """fragment_kind distinguishes locator shape: EXCEL_ROW uses
    sheet_name/row_number (kept for Phase 1 backward compatibility),
    PDF_TRANSACTION and any future kind use locator_json instead. See
    docs/PHASE2A-DECISIONS.md."""

    __tablename__ = "evidence_fragments"
    __table_args__ = (
        UniqueConstraint("evidence_document_id", "sheet_name", "row_number", name="uq_fragment_location"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    evidence_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence_documents.id"), nullable=False, index=True
    )
    fragment_kind: Mapped[str] = mapped_column(String, nullable=False, default="EXCEL_ROW")
    sheet_name: Mapped[str | None] = mapped_column(String, nullable=True)
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    locator_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ContractModel(Base):
    """The stable identity anchor (docs/PHASE2D1-R0-DECISIONS.md section
    1.3 / 4.4), closing the Phase 2D.1-R5 pre-flight debt: Contract now
    uses the SAME anchor + current-revision pattern as ContractItem /
    Shipment / SalesContract. Business identity is
    ``(contract_no, counterparty)`` — deliberately NOT a unique
    constraint. Duplicates are expected and produce a Task/BusinessKeyConflict
    instead of a DB error (R0 explicitly permits ambiguity; schema-level
    prohibition would "fix" it by forbidding a legitimate business
    state). Business values live on ContractRevisionModel instead — this
    anchor deliberately carries none besides the identity pair, so there
    is nothing here to get out of sync with the current revision."""

    __tablename__ = "contracts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    contract_no: Mapped[str] = mapped_column(String, nullable=False, index=True)
    counterparty: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ContractRevisionModel(Base):
    """Versioned assertions about a Contract anchor — the same shape as
    ContractItemRevisionModel (docs/PHASE2D1-R0-DECISIONS.md section
    1.3). The current revision is the one row per anchor with
    ``superseded_by_revision_id IS NULL``, resolved only in
    ContractRepository, never re-derived elsewhere."""

    __tablename__ = "contract_revisions"
    __table_args__ = (
        Index(
            "uq_contract_revisions_one_current",
            "contract_id",
            unique=True,
            sqlite_where=text("superseded_by_revision_id IS NULL"),
            postgresql_where=text("superseded_by_revision_id IS NULL"),
        ),
        Index(
            "uq_contract_revisions_one_initial",
            "contract_id",
            unique=True,
            sqlite_where=text("revision_type = 'INITIAL'"),
            postgresql_where=text("revision_type = 'INITIAL'"),
        ),
        CheckConstraint(
            "revision_type IN ('INITIAL', 'SUPPLEMENT', 'CORRECTION')",
            name="ck_contract_revisions_revision_type",
        ),
        # Phase 2D.1-R5 gate fix: the pre-R5 invariant "Contract.gross_amount
        # and Contract.currency are always non-NULL" must hold for the
        # CURRENT revision — schema-level backstop behind
        # bel.application.contract_facts's own guard (which rejects
        # asserting None for either field at the application layer).
        # Evaluated at INSERT time: a brand-new revision always has
        # superseded_by_revision_id IS NULL, so the OR never lets a
        # newly-written current row through with either NULL; a row
        # LATER retired (superseded_by_revision_id set on UPDATE) keeps
        # whatever non-NULL values it was written with — this constraint
        # only ever fires on that row's own gross_amount/currency
        # columns, which nothing here re-writes.
        CheckConstraint(
            "superseded_by_revision_id IS NOT NULL OR (gross_amount IS NOT NULL AND currency IS NOT NULL)",
            name="ck_contract_revisions_current_requires_amount_currency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.id"), nullable=False, index=True)
    revision_type: Mapped[str] = mapped_column(String, nullable=False)  # INITIAL / SUPPLEMENT / CORRECTION
    contract_type: Mapped[str | None] = mapped_column(String, nullable=True)
    buyer: Mapped[str | None] = mapped_column(String, nullable=True)
    gross_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    contract_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Provenance reference — the exact Evidence for THIS revision. Never
    # re-pointed. Nullable at the schema level for the same reason every
    # other revision table's source_fragment_id is (no DB-level Evidence
    # enforcement); bel.application.contract_facts requires a real
    # fragment for every revision it writes.
    source_fragment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=True)
    superseded_by_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contract_revisions.id", deferrable=True, initially="DEFERRED"), nullable=True, index=True
    )
    # The exact field names the writing command actually asserted, for
    # exact-replay comparison. See ContractItemRevisionModel's docstring.
    asserted_field_names: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ContractItemModel(Base):
    """The stable identity anchor (docs/PHASE2D1-R0-DECISIONS.md section
    1.3). All foreign keys in the system point at this row's id, which
    is never superseded and never deleted. Business values live on
    ContractItemRevisionModel instead — this anchor deliberately carries
    none, so there is nothing here to get out of sync with the current
    revision."""

    __tablename__ = "contract_items"
    __table_args__ = (
        # source_item_key is an implementation-level stable reference for
        # Fact Pack selectors — NOT a global business key and not a SKU.
        # Unique per contract only. See docs/PHASE2B-DECISIONS.md.
        UniqueConstraint("contract_id", "source_item_key", name="uq_contract_item_source_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.id"), nullable=False, index=True)
    source_item_key: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ContractItemRevisionModel(Base):
    """Versioned assertions about a ContractItem anchor
    (docs/PHASE2D1-R0-DECISIONS.md section 1.3). The current revision is
    the one row per anchor with ``superseded_by_revision_id IS NULL`` —
    resolved only in the repository layer (ContractItemRepository), never
    re-derived by a rule or an application service.

    ``uq_contract_item_revisions_one_current`` is a database-level
    partial unique index enforcing "at most one current revision per
    anchor" — the backstop behind ContractItemRepository's own
    conditional-retire-then-insert primitives
    (``append_revision_against_current``), per the Phase 2D.1-R1 Codex
    fix round (BLOCKER 4). Application logic already prevents two
    current rows under normal operation; this index is what makes a
    violation impossible even under a race or a future direct writer,
    rather than merely undocumented.

    ``uq_contract_item_revisions_one_initial`` solves a DIFFERENT
    problem (Phase 2D.1-R1 Codex fix round #3, FIX 3B): "at most one
    current revision" does not by itself stop a second INITIAL revision
    from existing on the same anchor. It deliberately does NOT enforce
    "an anchor must always have an INITIAL" — no trigger for that exists
    or is intended here.

    ``ck_contract_item_revisions_revision_type`` closes the same gap
    from the other side (FIX 3A): the closed set of legal
    ``revision_type`` values is enforced at the database level, not only
    by ContractItemRepository's own validation — a raw INSERT or an ORM
    bypass cannot write an unrecognised value either."""

    __tablename__ = "contract_item_revisions"
    __table_args__ = (
        Index(
            "uq_contract_item_revisions_one_current",
            "contract_item_id",
            unique=True,
            sqlite_where=text("superseded_by_revision_id IS NULL"),
            postgresql_where=text("superseded_by_revision_id IS NULL"),
        ),
        Index(
            "uq_contract_item_revisions_one_initial",
            "contract_item_id",
            unique=True,
            sqlite_where=text("revision_type = 'INITIAL'"),
            postgresql_where=text("revision_type = 'INITIAL'"),
        ),
        CheckConstraint(
            "revision_type IN ('INITIAL', 'SUPPLEMENT', 'CORRECTION')",
            name="ck_contract_item_revisions_revision_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    contract_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contract_items.id"), nullable=False, index=True)
    revision_type: Mapped[str] = mapped_column(String, nullable=False)  # INITIAL / SUPPLEMENT / CORRECTION
    sku: Mapped[str | None] = mapped_column(String, nullable=True)
    product_name: Mapped[str | None] = mapped_column(String, nullable=True)
    specification: Mapped[str | None] = mapped_column(String, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    gross_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    net_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    # Provenance reference — the exact Evidence for THIS revision. Never
    # re-pointed. See docs/PHASE2D1-R0-DECISIONS.md section 1.3.
    # Nullable at the schema level for the same reason the pre-R1
    # current_source_fragment_id was nullable (no DB-level Evidence
    # enforcement, section 1.2); bel.application.contract_item_facts
    # requires a real fragment for every revision it writes.
    source_fragment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=True)
    superseded_by_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contract_item_revisions.id", deferrable=True, initially="DEFERRED"), nullable=True, index=True
    )
    # The exact field names the writing command actually asserted,
    # captured verbatim (Phase 2D.1-R1 Codex fix round #2) — NOT
    # reconstructed later by diffing against the predecessor, which
    # cannot distinguish "not asserted" from "re-asserted with its
    # already-current value". NULL for revisions with no captured
    # intent: pre-R1 data carried forward by the migration, and the
    # ContractItemRepository.add() test convenience.
    asserted_field_names: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ShipmentModel(Base):
    """The stable identity anchor for Shipment (Phase 2D.1-R2), same
    pattern as ContractItemModel. Carries the frozen business identity
    (docs/PHASE2D1-R0-DECISIONS.md section 4.4:
    ``(contract_id, external_reference, execution_date)``) — these three
    fields are immutable after creation; correcting one is
    re-identification and out of R2's scope (section 4.4's "always
    produces a Task", deferred to R5), exactly like ContractItem's
    ``(contract_id, source_item_key)``. All other business values
    (``contract_item_id``, ``quantity``) live on ShipmentRevisionModel
    instead.

    ``external_reference`` is nullable (section 3.2: "optional"). SQLite
    (like standard SQL) treats NULL as distinct from any other NULL under
    a UNIQUE constraint, so two Shipments sharing
    ``(contract_id, execution_date)`` with no ``external_reference`` never
    collide — matching section 4.4's "external_reference null -> identity
    incomplete -> requires human confirmation": there is no reliable key
    to auto-dedupe against, so none is attempted (bel.application.
    shipment_facts never looks up by identity when external_reference is
    None; every such create is unconditionally new)."""

    __tablename__ = "shipments"
    __table_args__ = (
        UniqueConstraint(
            "contract_id", "external_reference", "execution_date", name="uq_shipment_business_identity"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.id"), nullable=False, index=True)
    external_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    execution_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ShipmentRevisionModel(Base):
    """Versioned assertions about a Shipment anchor — same model as
    ContractItemRevisionModel (docs/PHASE2D1-R0-DECISIONS.md section
    1.3), including the same three DB-level backstops closed in the
    Phase 2D.1-R1 Codex fix rounds: ``uq_shipment_revisions_one_current``
    (at most one current revision per anchor),
    ``uq_shipment_revisions_one_initial`` (at most one INITIAL revision
    per anchor — a separate invariant from "one current"), and
    ``ck_shipment_revisions_revision_type`` (the closed
    INITIAL/SUPPLEMENT/CORRECTION set, enforced even against a raw
    INSERT or an ORM bypass).

    ``source_fragment_id`` is NOT NULL here — unlike
    ContractItemRevisionModel, there is no pre-R2 legacy Shipment data to
    accommodate, so section 3.2's "source_fragment_id required — Evidence
    trace, never nullable" is enforced as a real schema constraint, not
    only at the application layer."""

    __tablename__ = "shipment_revisions"
    __table_args__ = (
        Index(
            "uq_shipment_revisions_one_current",
            "shipment_id",
            unique=True,
            sqlite_where=text("superseded_by_revision_id IS NULL"),
            postgresql_where=text("superseded_by_revision_id IS NULL"),
        ),
        Index(
            "uq_shipment_revisions_one_initial",
            "shipment_id",
            unique=True,
            sqlite_where=text("revision_type = 'INITIAL'"),
            postgresql_where=text("revision_type = 'INITIAL'"),
        ),
        CheckConstraint(
            "revision_type IN ('INITIAL', 'SUPPLEMENT', 'CORRECTION')",
            name="ck_shipment_revisions_revision_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    shipment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shipments.id"), nullable=False, index=True)
    revision_type: Mapped[str] = mapped_column(String, nullable=False)  # INITIAL / SUPPLEMENT / CORRECTION
    contract_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contract_items.id"), nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    # Phase 2D.3-F1c — canonical export/customs declaration values
    # (docs/PHASE2D3-RULE-FREEZE.md IP-S02). Nullable: a pre-F1c Shipment
    # row has no declaration Evidence and stays NULL; amount known with
    # currency unknown remains a representable incomplete Fact. Never
    # derived from quantity / contract amounts at write time.
    declared_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    declared_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Provenance reference — the exact Evidence for THIS revision. Never
    # re-pointed. See docs/PHASE2D1-R0-DECISIONS.md sections 1.3 and 3.2.
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=False)
    superseded_by_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("shipment_revisions.id", deferrable=True, initially="DEFERRED"), nullable=True, index=True
    )
    # The exact field names the writing command actually asserted,
    # captured verbatim — see ContractItemRevisionModel's docstring for
    # why this must never be reconstructed later by diffing against the
    # predecessor.
    asserted_field_names: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SalesContractModel(Base):
    """The stable identity anchor for SalesContract (Phase 2D.1-R3a
    Slice 1), same pattern as ContractItemModel/ShipmentModel. Carries
    the frozen business identity (docs/PHASE2D1-R0-DECISIONS.md section
    4.4: ``(our_entity, sales_contract_no)``) — both fields are immutable
    after creation; correcting one is re-identification and out of this
    round's scope, exactly like ContractItem's ``(contract_id,
    source_item_key)`` and Shipment's ``(contract_id, external_reference,
    execution_date)``. Unlike Shipment's identity, BOTH components here
    are mandatory: section 4.4 requires "NO canonical anchor may be
    created" if either is missing — there is no confirmed-anchor-with-
    incomplete-identity case for SalesContract the way there is for
    Shipment's nullable ``external_reference``.

    ``customer`` and every other business value (``currency``,
    ``gross_amount``, ``contract_date``) live on
    SalesContractRevisionModel instead — this anchor deliberately carries
    none, matching ContractItem/Shipment's separation of identity from
    asserted value."""

    __tablename__ = "sales_contracts"
    __table_args__ = (
        UniqueConstraint("our_entity", "sales_contract_no", name="uq_sales_contract_business_identity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    our_entity: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sales_contract_no: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SalesContractRevisionModel(Base):
    """Versioned assertions about a SalesContract anchor — same model as
    ContractItemRevisionModel/ShipmentRevisionModel
    (docs/PHASE2D1-R0-DECISIONS.md section 1.3), including the same three
    DB-level backstops closed in the Phase 2D.1-R1/R2 Codex fix rounds:
    ``uq_sales_contract_revisions_one_current`` (at most one current
    revision per anchor), ``uq_sales_contract_revisions_one_initial`` (at
    most one INITIAL revision per anchor — a separate invariant from "one
    current"), and ``ck_sales_contract_revisions_revision_type`` (the
    closed INITIAL/SUPPLEMENT/CORRECTION set, enforced even against a raw
    INSERT or an ORM bypass).

    ``source_fragment_id`` is NOT NULL here — like ShipmentRevisionModel,
    there is no pre-R3a legacy SalesContract data to accommodate."""

    __tablename__ = "sales_contract_revisions"
    __table_args__ = (
        Index(
            "uq_sales_contract_revisions_one_current",
            "sales_contract_id",
            unique=True,
            sqlite_where=text("superseded_by_revision_id IS NULL"),
            postgresql_where=text("superseded_by_revision_id IS NULL"),
        ),
        Index(
            "uq_sales_contract_revisions_one_initial",
            "sales_contract_id",
            unique=True,
            sqlite_where=text("revision_type = 'INITIAL'"),
            postgresql_where=text("revision_type = 'INITIAL'"),
        ),
        CheckConstraint(
            "revision_type IN ('INITIAL', 'SUPPLEMENT', 'CORRECTION')",
            name="ck_sales_contract_revisions_revision_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sales_contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales_contracts.id"), nullable=False, index=True)
    revision_type: Mapped[str] = mapped_column(String, nullable=False)  # INITIAL / SUPPLEMENT / CORRECTION
    # The ONLY place an external sales customer is expressed
    # (docs/DOMAIN.md, docs/PHASE2D1-R0-DECISIONS.md section 2.2).
    # Nullable: a scope may be known before its customer is (section 2.3).
    customer: Mapped[str | None] = mapped_column(String, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    gross_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    contract_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Provenance reference — the exact Evidence for THIS revision. Never
    # re-pointed. See docs/PHASE2D1-R0-DECISIONS.md sections 1.3 and 2.2.
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=False)
    superseded_by_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sales_contract_revisions.id", deferrable=True, initially="DEFERRED"), nullable=True, index=True
    )
    # The exact field names the writing command actually asserted,
    # captured verbatim — see ContractItemRevisionModel's docstring for
    # why this must never be reconstructed later by diffing against the
    # predecessor.
    asserted_field_names: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ProcurementSalesLinkModel(Base):
    """One confirmed assertion episode (Phase 2D.1-R3a Slice 2,
    docs/PHASE2D1-R0-DECISIONS.md section 2.4). Deliberately NOT an
    anchor+revision Fact: there is no field to supplement or correct in
    place — a link row is immutable from the moment it is written.
    Correction happens at the relationship level via
    `ProcurementSalesLinkCorrectionModel`, never by editing this row.

    No `UniqueConstraint` on `(procurement_contract_id, sales_contract_id)`
    — that would make `REESTABLISH` impossible, since a business key may
    legitimately accumulate several episodes over time (at most one
    current). The one-current-per-business-key invariant instead lives
    in two places, deliberately redundant:

    1. `ProcurementSalesLinkRepository.insert_episode_if_no_current` —
       a single atomic `INSERT ... SELECT ... WHERE NOT EXISTS (...)`
       statement (never a separate check-then-insert) that only
       succeeds when no un-superseded episode already exists for the
       target business key.
    2. `trg_procurement_sales_links_one_current` (registered below via
       `event.listen`, defined in the R3a Slice 2 migration) — a genuine
       storage-level backstop that aborts ANY insert (including an ORM
       bypass that skips the repository entirely) that would create a
       second current episode for a business key, evaluated against the
       SAME `ProcurementSalesLinkCorrectionModel` lineage that defines
       `current()` everywhere else. No plain (partial) unique index can
       express this predicate — it depends on a different table."""

    __tablename__ = "procurement_sales_links"
    __table_args__ = (
        CheckConstraint(
            "confirmation_type IN ('AUTO_CONFIRMED', 'HUMAN_CONFIRMED')",
            name="ck_procurement_sales_links_confirmation_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    procurement_contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.id"), nullable=False, index=True)
    sales_contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales_contracts.id"), nullable=False, index=True)
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=False)
    confirmation_type: Mapped[str] = mapped_column(String, nullable=False)  # AUTO_CONFIRMED / HUMAN_CONFIRMED
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ProcurementSalesLinkCorrectionModel(Base):
    """An append-only correction Fact retiring exactly one assertion
    episode (docs/PHASE2D1-R0-DECISIONS.md section 2.4). Never updated
    after insert; `superseded_link_id` is UNIQUE so a correction chain
    can never fork (an episode may be superseded at most once)."""

    __tablename__ = "procurement_sales_link_corrections"
    __table_args__ = (
        CheckConstraint(
            "confirmation_type = 'HUMAN_CONFIRMED'", name="ck_procurement_sales_link_corrections_confirmation_type"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    superseded_link_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("procurement_sales_links.id"), nullable=False, unique=True
    )
    replacement_link_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("procurement_sales_links.id"), nullable=True
    )
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=False)
    # V1-frozen to HUMAN_CONFIRMED only (docs/PHASE2D1-R0-DECISIONS.md:
    # "a V1 correction record is therefore always HUMAN_CONFIRMED") — the
    # CHECK constraint above enforces this even against an ORM bypass;
    # the column itself is a plain String so both models can share the
    # same ConfirmationType vocabulary conceptually.
    confirmation_type: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# Storage-level "at most one current episode per relationship business
# key" backstop — see ProcurementSalesLinkModel's docstring. This cannot
# be a declarative Index/CheckConstraint (SQLite partial indexes and
# CHECK constraints cannot reference another table), so it is a trigger,
# registered via SQLAlchemy DDL events so `Base.metadata.create_all()`
# (used by in-memory test fixtures) creates it identically to
# `alembic upgrade head`. Attached to the CORRECTIONS table's create/drop
# events since SQLAlchemy creates tables in FK-dependency order (links
# before corrections) and drops them in reverse — this guarantees both
# tables already exist when the trigger is created, and the trigger is
# dropped before either table goes away.
#
# Two dialect-scoped bodies, not one literal translation (Phase 2D.1-P):
#
# SQLite (unchanged since Phase 2D.1-R3a Slice 2): the raw `WHEN EXISTS
# (...) ... RAISE(ABORT, ...)` trigger is safe as-is because SQLite has
# no concurrent writer to race against in the first place (the whole
# database has one write lock).
#
# PostgreSQL has no such implicit guarantee under READ COMMITTED — two
# concurrent INSERTs for the SAME (procurement_contract_id,
# sales_contract_id) could each evaluate the "no current row" EXISTS
# check before either commits and both pass, even inside a BEFORE INSERT
# trigger, including from a write that bypasses
# `serialized_write_transaction` entirely (a raw ORM `session.add` or a
# direct INSERT from another connection). The PostgreSQL trigger function
# therefore takes a business-key-scoped `pg_advisory_xact_lock` as its
# FIRST action, before the existence check — transaction-scoped, released
# automatically on commit/rollback, and blocking a second concurrent
# insert for the same key until the first resolves, at which point the
# EXISTS check correctly observes the committed outcome (or its absence).
# Inserts for DIFFERENT business keys never contend. This lock is
# independent of, and not expected to collide with, the single global key
# `serialized_write_transaction` uses at the application layer (see
# `database.py`'s `_WRITE_LOCK_KEY`) — different key space, and even a
# collision would only cost extra (harmless) waiting.
_ONE_CURRENT_LINK_TRIGGER_SQLITE = """
CREATE TRIGGER trg_procurement_sales_links_one_current
BEFORE INSERT ON procurement_sales_links
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM procurement_sales_links existing
    WHERE existing.procurement_contract_id = NEW.procurement_contract_id
      AND existing.sales_contract_id = NEW.sales_contract_id
      AND NOT EXISTS (
          SELECT 1 FROM procurement_sales_link_corrections c
          WHERE c.superseded_link_id = existing.id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'one current assertion episode per relationship business key');
END;
"""

_ONE_CURRENT_LINK_TRIGGER_POSTGRESQL = """
CREATE FUNCTION trg_procurement_sales_links_one_current_fn() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(NEW.procurement_contract_id::text || ':' || NEW.sales_contract_id::text, 0)
    );
    IF EXISTS (
        SELECT 1 FROM procurement_sales_links existing
        WHERE existing.procurement_contract_id = NEW.procurement_contract_id
          AND existing.sales_contract_id = NEW.sales_contract_id
          AND NOT EXISTS (
              SELECT 1 FROM procurement_sales_link_corrections c
              WHERE c.superseded_link_id = existing.id
          )
    ) THEN
        RAISE EXCEPTION 'one current assertion episode per relationship business key';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_procurement_sales_links_one_current
BEFORE INSERT ON procurement_sales_links
FOR EACH ROW
EXECUTE FUNCTION trg_procurement_sales_links_one_current_fn();
"""

event.listen(
    ProcurementSalesLinkCorrectionModel.__table__,
    "after_create",
    DDL(_ONE_CURRENT_LINK_TRIGGER_SQLITE).execute_if(dialect="sqlite"),
)
event.listen(
    ProcurementSalesLinkCorrectionModel.__table__,
    "after_create",
    DDL(_ONE_CURRENT_LINK_TRIGGER_POSTGRESQL).execute_if(dialect="postgresql"),
)
event.listen(
    ProcurementSalesLinkCorrectionModel.__table__,
    "before_drop",
    DDL("DROP TRIGGER IF EXISTS trg_procurement_sales_links_one_current").execute_if(dialect="sqlite"),
)
event.listen(
    ProcurementSalesLinkCorrectionModel.__table__,
    "before_drop",
    DDL(
        "DROP TRIGGER IF EXISTS trg_procurement_sales_links_one_current ON procurement_sales_links; "
        "DROP FUNCTION IF EXISTS trg_procurement_sales_links_one_current_fn()"
    ).execute_if(dialect="postgresql"),
)


class BusinessEventModel(Base):
    __tablename__ = "business_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class TaskExceptionModel(Base):
    __tablename__ = "task_exceptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    exception_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    summary: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ImportRunModel(Base):
    """Audit trail only — not a replay/event-sourcing mechanism.
    See docs/V1-SCOPE.md non-goals."""

    __tablename__ = "import_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    evidence_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence_documents.id"), nullable=False, index=True
    )
    file_name: Mapped[str] = mapped_column(String, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_reimport: Mapped[bool] = mapped_column(nullable=False)
    contracts_created_count: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_items_created_count: Mapped[int] = mapped_column(Integer, nullable=False)
    business_key_conflicts_detected_count: Mapped[int] = mapped_column(Integer, nullable=False)


class InvoiceModel(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    direction: Mapped[str] = mapped_column(String, nullable=False)  # PURCHASE / SALES / UNKNOWN
    invoice_type: Mapped[str | None] = mapped_column(String, nullable=True)
    invoice_no: Mapped[str | None] = mapped_column(String, nullable=True)
    digital_invoice_no: Mapped[str | None] = mapped_column(String, nullable=True)
    # external_invoice_key: digital_invoice_no when present (today, always).
    # UNIQUE + nullable lets a future invoice_code+invoice_no fallback
    # coexist without a schema change. See docs/PHASE2A-DECISIONS.md.
    external_invoice_key: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, index=True)
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    seller: Mapped[str | None] = mapped_column(String, nullable=True)
    buyer: Mapped[str | None] = mapped_column(String, nullable=True)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    invoice_status: Mapped[str | None] = mapped_column(String, nullable=True)
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Phase 2D.3-F1e — canonical Invoice currency (docs/PHASE2D3-RULE-FREEZE.md
    # IP-P02). Evidence-derived only, never defaulted/inferred/FX-converted,
    # and NOT part of invoice identity (external_invoice_key unchanged).
    # Nullable: a pre-F1e Invoice row has no explicit currency and stays
    # NULL (no backfill); a source with no stated currency imports NULL.
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)


class InvoiceItemModel(Base):
    __tablename__ = "invoice_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), nullable=False, index=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_name: Mapped[str | None] = mapped_column(String, nullable=True)
    specification: Mapped[str | None] = mapped_column(String, nullable=True)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=False)


class PaymentModel(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)  # IN / OUT
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)  # always positive
    counterparty: Mapped[str | None] = mapped_column(String, nullable=True)
    business_type: Mapped[str | None] = mapped_column(String, nullable=True)
    bank_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    running_balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Phase 2D.1-R5 pre-flight debt — see Payment.source_account_id in
    # bel.domain.payment. Nullable: no value is fabricated for
    # pre-existing rows.
    source_account_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)


class InvoiceAllocationModel(Base):
    """Many-to-many by construction: an Invoice can have allocations
    against more than one Contract, and vice versa. Phase 2A never
    auto-splits — every row here comes from a unique-candidate M001
    match or a future manual confirmation. See docs/RULES.md-adjacent
    docs/PHASE2A-DECISIONS.md for the M001 rule."""

    __tablename__ = "invoice_allocations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), nullable=False, index=True)
    contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.id"), nullable=False, index=True)
    # Beyond the spec's minimum field list ("at least"): traces every
    # Allocation back to the MatchCase that authorized it, per section 27's
    # Allocation -> MatchCase -> Fact -> Evidence chain. See PHASE2A-DECISIONS.md.
    match_case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("match_cases.id"), nullable=False, index=True)
    allocated_gross_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    match_method: Mapped[str] = mapped_column(String, nullable=False)
    confirmation_type: Mapped[str] = mapped_column(String, nullable=False)  # AUTO_CONFIRMED / HUMAN_CONFIRMED
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PaymentAllocationModel(Base):
    __tablename__ = "payment_allocations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), nullable=False, index=True)
    contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.id"), nullable=False, index=True)
    match_case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("match_cases.id"), nullable=False, index=True)
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    match_method: Mapped[str] = mapped_column(String, nullable=False)
    confirmation_type: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MatchCaseModel(Base):
    """subject_id is polymorphic (points at invoices.id or payments.id
    per subject_type) so it is intentionally not an FK — a single column
    can't target two tables without a generic-association framework,
    which Phase 2A explicitly avoids building.

    Deliberately NO `UniqueConstraint` on `(subject_type, subject_id)`:
    an earlier Phase 2D.1-R3b draft added one, reasoning it only
    formalised an assumed invariant — but
    `tests/web/test_web_contract_360.py::test_contract360_item_allocation_is_scoped_to_own_contract`
    proves the procurement leg already relies on ONE Invoice legitimately
    producing TWO separate `MatchCase` rows (one per Contract it is
    confirmed against — "Domain: Invoice <-> Contract is many-to-many").
    A blanket constraint would have broken that existing, tested
    behaviour. The sales leg's OWN concurrency safety (Phase 2D.1-R3b
    section 27) comes instead from `MatchCaseRepository
    .add_if_no_case_for_subject`'s atomic conditional insert — see its
    docstring — which needs no DB-level constraint change here."""

    __tablename__ = "match_cases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subject_type: Mapped[str] = mapped_column(String, nullable=False, index=True)  # INVOICE / PAYMENT
    subject_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    match_method: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MatchCandidateModel(Base):
    """Real rows, not a JSON blob on MatchCase — candidates are formal
    data, per spec section 25."""

    __tablename__ = "match_candidates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    match_case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("match_cases.id"), nullable=False, index=True)
    contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SalesInvoiceAllocationModel(Base):
    """The sales-side twin of `InvoiceAllocationModel`
    (docs/PHASE2D1-R0-DECISIONS.md section 2.7) — physically separate,
    targeting `sales_contracts.id` instead of `contracts.id`. No
    `contract_id` column anywhere on this table: a SALES invoice cannot
    be attributed to a procurement Contract even by mistake, structurally."""

    __tablename__ = "sales_invoice_allocations"
    __table_args__ = (
        CheckConstraint(
            "confirmation_type = 'HUMAN_CONFIRMED'", name="ck_sales_invoice_allocations_confirmation_type"
        ),
        # Gate 2D.1-R3b fix round, BLOCKER 1: a DB-level backstop against
        # a negative/zero amount even via a raw ORM bypass of
        # SalesInvoiceAllocationRepository.add's own validation.
        CheckConstraint("allocated_gross_amount > 0", name="ck_sales_invoice_allocations_positive_amount"),
        # Gate 2D.1-R3b fix round #2, BLOCKER 2: a coarse DB-level
        # backstop bounding NUMERIC(18,2) to at most 16 integer digits
        # (10**16) — NOT the authoritative precision guard (a plain SQL
        # CHECK cannot express the actual float-round-trip condition
        # bel.domain.matching.validate_storable_amount enforces), just a
        # sanity bound against wildly out-of-range values via ORM bypass.
        CheckConstraint(
            f"allocated_gross_amount < {10 ** 16}", name="ck_sales_invoice_allocations_max_amount"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), nullable=False, index=True)
    sales_contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales_contracts.id"), nullable=False, index=True)
    match_case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("match_cases.id"), nullable=False, index=True)
    allocated_gross_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    # R3b's first version is HUMAN_CONFIRMED only (docs/PHASE2D1-R0-DECISIONS.md
    # section 2.7); the CHECK constraint enforces this even against an
    # ORM bypass.
    confirmation_type: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SalesPaymentAllocationModel(Base):
    """The sales-side twin of `PaymentAllocationModel`
    (docs/PHASE2D1-R0-DECISIONS.md section 2.7). No `contract_id`
    column: an `IN` receipt cannot be attributed to a procurement
    Contract even by mistake, structurally."""

    __tablename__ = "sales_payment_allocations"
    __table_args__ = (
        CheckConstraint(
            "confirmation_type = 'HUMAN_CONFIRMED'", name="ck_sales_payment_allocations_confirmation_type"
        ),
        CheckConstraint("allocated_amount > 0", name="ck_sales_payment_allocations_positive_amount"),
        CheckConstraint(f"allocated_amount < {10 ** 16}", name="ck_sales_payment_allocations_max_amount"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), nullable=False, index=True)
    sales_contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales_contracts.id"), nullable=False, index=True)
    match_case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("match_cases.id"), nullable=False, index=True)
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    confirmation_type: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SalesMatchCandidateModel(Base):
    """A human-confirmation candidate for a sales-leg `MatchCase`
    (docs/PHASE2D1-R0-DECISIONS.md section 2.7's `MatchCase` reuse) — a
    separate object from `MatchCandidateModel`, never a generalisation of
    it (that model's `contract_id` is a hard FK to `contracts.id`).
    `uq_sales_match_candidates_case_target` prevents the same
    SalesContract being proposed twice as a candidate for one case."""

    __tablename__ = "sales_match_candidates"
    __table_args__ = (
        UniqueConstraint(
            "match_case_id", "sales_contract_id", name="uq_sales_match_candidates_case_target"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    match_case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("match_cases.id"), nullable=False, index=True)
    sales_contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales_contracts.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvoiceItemAllocationModel(Base):
    """ContractItem ↔ InvoiceItem confirmed relationship — the
    R006 partial-receipt primitive. Created only via MANUAL_CONFIRMED or
    a Fact Pack; Phase 2B adds no automatic item matching. See
    docs/PHASE2B-DECISIONS.md and spec sections 10-11."""

    __tablename__ = "invoice_item_allocations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice_items.id"), nullable=False, index=True)
    contract_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contract_items.id"), nullable=False, index=True)
    allocated_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    allocated_net_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    confirmation_type: Mapped[str] = mapped_column(String, nullable=False)
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Phase 2D.1-R5 pre-flight debt (docs/PHASE2D1-R0-DECISIONS.md
    # section 21): whole-fact supersession lineage pointer. NULL for a
    # current fact; set exactly once via an atomic conditional UPDATE,
    # never re-pointed. "Current" repository reads exclude any row with
    # this set.
    superseded_by_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invoice_item_allocations.id"), nullable=True, index=True
    )


class CostRecognitionFactModel(Base):
    __tablename__ = "cost_recognition_facts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.id"), nullable=False, index=True)
    recognition_date: Mapped[date] = mapped_column(Date, nullable=False)
    basis: Mapped[str] = mapped_column(String, nullable=False)
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=False)
    # Provenance reference (Phase 2D.1-R2, docs/PHASE2D1-R0-DECISIONS.md
    # section 3.4) — names the Shipment anchor that evidenced this cost
    # recognition. Nullable: pre-R2 facts have none, and not every basis
    # is shipment-evidenced. Never re-pointed once set.
    shipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("shipments.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # See InvoiceItemAllocationModel.superseded_by_fact_id.
    superseded_by_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cost_recognition_facts.id"), nullable=True, index=True
    )


class AccrualBasisFactModel(Base):
    __tablename__ = "accrual_basis_facts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)  # CONTRACT / CONTRACT_ITEM
    contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.id"), nullable=False, index=True)
    contract_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contract_items.id"), nullable=True, index=True
    )
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    basis: Mapped[str] = mapped_column(String, nullable=False)
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # See InvoiceItemAllocationModel.superseded_by_fact_id.
    superseded_by_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accrual_basis_facts.id"), nullable=True, index=True
    )


class HistoricalAccrualFactModel(Base):
    __tablename__ = "historical_accrual_facts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_period: Mapped[str] = mapped_column(String, nullable=False, index=True)
    contract_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contract_items.id"), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    basis: Mapped[str] = mapped_column(String, nullable=False)
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_fragments.id"), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # See InvoiceItemAllocationModel.superseded_by_fact_id.
    superseded_by_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("historical_accrual_facts.id"), nullable=True, index=True
    )


class AccrualModel(Base):
    __tablename__ = "accruals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    period: Mapped[str] = mapped_column(String, nullable=False, index=True)
    contract_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contract_items.id"), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    basis: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # created_from_fact_id is intentionally NOT an FK: Phase 2B sources
    # accruals only from HistoricalAccrualFact, but future self-generated
    # accruals extend the source set (spec section 7).
    created_from_fact_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AccrualReversalModel(Base):
    __tablename__ = "accrual_reversals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    accrual_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accruals.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String, nullable=False)
    invoice_item_allocation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_item_allocations.id"), nullable=False, index=True
    )
    reversed_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    reversed_estimated_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

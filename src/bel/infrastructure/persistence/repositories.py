from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, literal, select, text, update
from sqlalchemy.orm import Session

from bel.domain.accrual import (
    Accrual,
    AccrualBasisFact,
    AccrualReversal,
    CostRecognitionFact,
    HistoricalAccrualFact,
    InvoiceItemAllocation,
)
from bel.domain.contract import (
    CONTRACT_ITEM_FACT_FIELDS,
    Contract,
    ContractItem,
    ContractItemRevision,
    ContractItemRevisionType,
    ContractRevision,
    ContractRevisionType,
)
from bel.domain.event import BusinessEvent
from bel.domain.evidence import EvidenceDocument, EvidenceFragment
from bel.domain.exception import TaskException
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.domain.matching import (
    ConfirmationType,
    InvoiceAllocation,
    MatchCandidate,
    MatchCase,
    MatchCaseStatus,
    PaymentAllocation,
    SalesInvoiceAllocation,
    SalesMatchCandidate,
    SalesPaymentAllocation,
    SubjectType,
    validate_storable_amount,
)
from bel.domain.payment import Payment, PaymentDirection
from bel.domain.procurement_sales_link import ProcurementSalesLink, ProcurementSalesLinkCorrection
from bel.domain.sales_contract import SalesContract, SalesContractRevision, SalesContractRevisionType
from bel.domain.shipment import Shipment, ShipmentRevision, ShipmentRevisionType
from bel.infrastructure.persistence.models import (
    AccrualBasisFactModel,
    AccrualModel,
    AccrualReversalModel,
    BusinessEventModel,
    ContractItemModel,
    ContractItemRevisionModel,
    ContractModel,
    ContractRevisionModel,
    CostRecognitionFactModel,
    EvidenceDocumentModel,
    EvidenceFragmentModel,
    HistoricalAccrualFactModel,
    ImportRunModel,
    InvoiceAllocationModel,
    InvoiceItemAllocationModel,
    InvoiceItemModel,
    InvoiceModel,
    MatchCandidateModel,
    MatchCaseModel,
    PaymentAllocationModel,
    PaymentModel,
    ProcurementSalesLinkCorrectionModel,
    ProcurementSalesLinkModel,
    SalesContractModel,
    SalesContractRevisionModel,
    SalesInvoiceAllocationModel,
    SalesMatchCandidateModel,
    SalesPaymentAllocationModel,
    ShipmentModel,
    ShipmentRevisionModel,
    TaskExceptionModel,
)


def _defer_fk_checks(session: Session) -> None:
    """Defer FK constraint checking to this transaction's commit —
    dialect-dispatched. Needed by every ``append_revision_against_current``
    below: the retiring UPDATE momentarily points a row's
    ``superseded_by_revision_id`` at the new revision's id before that row
    is inserted (retire-then-insert is the order the partial unique index
    requires), which an immediately-checked FK would otherwise reject.

    SQLite: ``PRAGMA defer_foreign_keys = ON`` defers ALL FK checks to
    commit regardless of how the constraint was declared, and resets
    automatically at the end of every transaction — set fresh here rather
    than once at connect time (unchanged since Phase 2C.1).

    PostgreSQL: constraint-level deferrability is a DDL-time property —
    ``SET CONSTRAINTS ALL DEFERRED`` only has an effect on constraints the
    schema already declared ``DEFERRABLE`` (see the 4 self-referential
    ``superseded_by_revision_id`` FKs in models.py / the PostgreSQL
    baseline migration, the only columns this is called for)."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    else:
        session.execute(text("PRAGMA defer_foreign_keys = ON"))


def _document_to_domain(m: EvidenceDocumentModel) -> EvidenceDocument:
    return EvidenceDocument(
        id=m.id, file_name=m.file_name, sha256=m.sha256, source_type=m.source_type, imported_at=m.imported_at
    )


def _fragment_to_domain(m: EvidenceFragmentModel) -> EvidenceFragment:
    return EvidenceFragment(
        id=m.id,
        evidence_document_id=m.evidence_document_id,
        fragment_kind=m.fragment_kind,
        sheet_name=m.sheet_name,
        row_number=m.row_number,
        locator_json=m.locator_json,
        raw_data=m.raw_data,
        created_at=m.created_at,
    )


def _contract_revision_to_domain(m: ContractRevisionModel) -> ContractRevision:
    return ContractRevision(
        id=m.id,
        contract_id=m.contract_id,
        revision_type=m.revision_type,
        contract_type=m.contract_type,
        buyer=m.buyer,
        gross_amount=m.gross_amount,
        currency=m.currency,
        contract_date=m.contract_date,
        source_fragment_id=m.source_fragment_id,
        superseded_by_revision_id=m.superseded_by_revision_id,
        created_at=m.created_at,
        asserted_field_names=m.asserted_field_names,
    )


def _assemble_contract(anchor: ContractModel, current_revision: ContractRevisionModel) -> Contract:
    """The anchor + current-revision join, in ONE place
    (docs/PHASE2D1-R0-DECISIONS.md section 1.3: "current-revision
    resolution is defined once, in the repository layer"). Returns the
    Contract dataclass of exactly its pre-R5 shape, so every existing
    consumer (contract_360.py, contract_business_ledger.py,
    matching.py, period_close.py, ...) is unaffected. ``updated_at`` is
    derived as the current revision's own ``created_at`` — there is no
    separate mutable "last updated" column to fall out of sync."""
    return Contract(
        id=anchor.id,
        contract_no=anchor.contract_no,
        contract_type=current_revision.contract_type,
        counterparty=anchor.counterparty,
        buyer=current_revision.buyer,
        gross_amount=current_revision.gross_amount,
        currency=current_revision.currency,
        contract_date=current_revision.contract_date,
        current_source_fragment_id=current_revision.source_fragment_id,
        created_at=anchor.created_at,
        updated_at=current_revision.created_at,
    )


def _contract_item_revision_to_domain(m: ContractItemRevisionModel) -> ContractItemRevision:
    return ContractItemRevision(
        id=m.id,
        contract_item_id=m.contract_item_id,
        revision_type=m.revision_type,
        sku=m.sku,
        product_name=m.product_name,
        specification=m.specification,
        quantity=m.quantity,
        unit=m.unit,
        unit_price=m.unit_price,
        gross_amount=m.gross_amount,
        tax_rate=m.tax_rate,
        net_amount=m.net_amount,
        source_fragment_id=m.source_fragment_id,
        superseded_by_revision_id=m.superseded_by_revision_id,
        created_at=m.created_at,
        asserted_field_names=m.asserted_field_names,
    )


def _assemble_contract_item(anchor: ContractItemModel, current_revision: ContractItemRevisionModel) -> ContractItem:
    """The anchor + current-revision join, in ONE place, per
    docs/PHASE2D1-R0-DECISIONS.md section 1.3 ("current-revision
    resolution is defined once, in the repository layer"). Returns the
    ContractItem dataclass of exactly its pre-R1 shape, so every existing
    consumer (period_close.py, contract_360.py, ...) is unaffected.
    ``current_source_fragment_id`` now resolves to the CURRENT revision's
    Evidence rather than the pre-R1 field of the same name, which was
    documented as never updated after creation."""
    return ContractItem(
        id=anchor.id,
        contract_id=anchor.contract_id,
        source_item_key=anchor.source_item_key,
        sku=current_revision.sku,
        product_name=current_revision.product_name,
        specification=current_revision.specification,
        quantity=current_revision.quantity,
        unit=current_revision.unit,
        unit_price=current_revision.unit_price,
        gross_amount=current_revision.gross_amount,
        tax_rate=current_revision.tax_rate,
        net_amount=current_revision.net_amount,
        current_source_fragment_id=current_revision.source_fragment_id,
        created_at=anchor.created_at,
    )


def _shipment_revision_to_domain(m: ShipmentRevisionModel) -> ShipmentRevision:
    return ShipmentRevision(
        id=m.id,
        shipment_id=m.shipment_id,
        revision_type=m.revision_type,
        contract_item_id=m.contract_item_id,
        quantity=m.quantity,
        declared_amount=m.declared_amount,
        declared_currency=m.declared_currency,
        source_fragment_id=m.source_fragment_id,
        superseded_by_revision_id=m.superseded_by_revision_id,
        created_at=m.created_at,
        asserted_field_names=m.asserted_field_names,
    )


def _assemble_shipment(anchor: ShipmentModel, current_revision: ShipmentRevisionModel) -> Shipment:
    """The anchor + current-revision join, in ONE place, mirroring
    ``_assemble_contract_item``. See docs/PHASE2D1-R0-DECISIONS.md
    section 1.3 — current-revision resolution is defined once, in the
    repository layer. Phase 2D.3-F1c: the declaration values resolve
    through the current revision exactly like ``quantity``."""
    return Shipment(
        id=anchor.id,
        contract_id=anchor.contract_id,
        external_reference=anchor.external_reference,
        execution_date=anchor.execution_date,
        contract_item_id=current_revision.contract_item_id,
        quantity=current_revision.quantity,
        declared_amount=current_revision.declared_amount,
        declared_currency=current_revision.declared_currency,
        current_source_fragment_id=current_revision.source_fragment_id,
        created_at=anchor.created_at,
    )


def _sales_contract_revision_to_domain(m: SalesContractRevisionModel) -> SalesContractRevision:
    return SalesContractRevision(
        id=m.id,
        sales_contract_id=m.sales_contract_id,
        revision_type=m.revision_type,
        customer=m.customer,
        currency=m.currency,
        gross_amount=m.gross_amount,
        contract_date=m.contract_date,
        source_fragment_id=m.source_fragment_id,
        superseded_by_revision_id=m.superseded_by_revision_id,
        created_at=m.created_at,
        asserted_field_names=m.asserted_field_names,
    )


def _assemble_sales_contract(anchor: SalesContractModel, current_revision: SalesContractRevisionModel) -> SalesContract:
    """The anchor + current-revision join, in ONE place, mirroring
    ``_assemble_shipment``/``_assemble_contract_item``. See
    docs/PHASE2D1-R0-DECISIONS.md section 1.3 — current-revision
    resolution is defined once, in the repository layer."""
    return SalesContract(
        id=anchor.id,
        our_entity=anchor.our_entity,
        sales_contract_no=anchor.sales_contract_no,
        customer=current_revision.customer,
        currency=current_revision.currency,
        gross_amount=current_revision.gross_amount,
        contract_date=current_revision.contract_date,
        current_source_fragment_id=current_revision.source_fragment_id,
        created_at=anchor.created_at,
    )


def _exception_to_domain(m: TaskExceptionModel) -> TaskException:
    return TaskException(
        id=m.id,
        exception_type=m.exception_type,
        status=m.status,
        summary=m.summary,
        detail=m.detail,
        created_at=m.created_at,
    )


def _invoice_to_domain(m: InvoiceModel) -> Invoice:
    return Invoice(
        id=m.id,
        direction=m.direction,
        invoice_type=m.invoice_type,
        invoice_no=m.invoice_no,
        digital_invoice_no=m.digital_invoice_no,
        external_invoice_key=m.external_invoice_key,
        issue_date=m.issue_date,
        seller=m.seller,
        buyer=m.buyer,
        net_amount=m.net_amount,
        tax_amount=m.tax_amount,
        gross_amount=m.gross_amount,
        invoice_status=m.invoice_status,
        source_fragment_id=m.source_fragment_id,
        created_at=m.created_at,
        updated_at=m.updated_at,
        currency=m.currency,
    )


def _invoice_item_to_domain(m: InvoiceItemModel) -> InvoiceItem:
    return InvoiceItem(
        id=m.id,
        invoice_id=m.invoice_id,
        line_no=m.line_no,
        product_name=m.product_name,
        specification=m.specification,
        unit=m.unit,
        quantity=m.quantity,
        unit_price=m.unit_price,
        net_amount=m.net_amount,
        tax_rate=m.tax_rate,
        tax_amount=m.tax_amount,
        gross_amount=m.gross_amount,
        source_fragment_id=m.source_fragment_id,
    )


def _payment_to_domain(m: PaymentModel) -> Payment:
    return Payment(
        id=m.id,
        transaction_date=m.transaction_date,
        direction=m.direction,
        amount=m.amount,
        counterparty=m.counterparty,
        business_type=m.business_type,
        bank_reference=m.bank_reference,
        description=m.description,
        running_balance=m.running_balance,
        source_fragment_id=m.source_fragment_id,
        created_at=m.created_at,
        source_account_id=m.source_account_id,
    )


def _match_case_to_domain(m: MatchCaseModel) -> MatchCase:
    return MatchCase(
        id=m.id,
        subject_type=m.subject_type,
        subject_id=m.subject_id,
        status=m.status,
        match_method=m.match_method,
        created_at=m.created_at,
        resolved_at=m.resolved_at,
    )


def _match_candidate_to_domain(m: MatchCandidateModel) -> MatchCandidate:
    return MatchCandidate(id=m.id, match_case_id=m.match_case_id, contract_id=m.contract_id, created_at=m.created_at)


def _invoice_allocation_to_domain(m: InvoiceAllocationModel) -> InvoiceAllocation:
    return InvoiceAllocation(
        id=m.id,
        invoice_id=m.invoice_id,
        contract_id=m.contract_id,
        match_case_id=m.match_case_id,
        allocated_gross_amount=m.allocated_gross_amount,
        match_method=m.match_method,
        confirmation_type=m.confirmation_type,
        created_at=m.created_at,
    )


def _payment_allocation_to_domain(m: PaymentAllocationModel) -> PaymentAllocation:
    return PaymentAllocation(
        id=m.id,
        payment_id=m.payment_id,
        contract_id=m.contract_id,
        match_case_id=m.match_case_id,
        allocated_amount=m.allocated_amount,
        match_method=m.match_method,
        confirmation_type=m.confirmation_type,
        created_at=m.created_at,
    )


class EvidenceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find_document_by_sha256(self, sha256: str) -> EvidenceDocument | None:
        m = self._session.scalar(select(EvidenceDocumentModel).where(EvidenceDocumentModel.sha256 == sha256))
        return _document_to_domain(m) if m else None

    def get_document(self, document_id: uuid.UUID) -> EvidenceDocument | None:
        m = self._session.get(EvidenceDocumentModel, document_id)
        return _document_to_domain(m) if m else None

    def add_document(self, document: EvidenceDocument) -> None:
        self._session.add(
            EvidenceDocumentModel(
                id=document.id,
                file_name=document.file_name,
                sha256=document.sha256,
                source_type=document.source_type,
                imported_at=document.imported_at,
            )
        )

    def add_fragment(self, fragment: EvidenceFragment) -> None:
        self._session.add(
            EvidenceFragmentModel(
                id=fragment.id,
                evidence_document_id=fragment.evidence_document_id,
                fragment_kind=fragment.fragment_kind,
                sheet_name=fragment.sheet_name,
                row_number=fragment.row_number,
                locator_json=fragment.locator_json,
                raw_data=fragment.raw_data,
                created_at=fragment.created_at,
            )
        )

    def get_fragment(self, fragment_id: uuid.UUID) -> EvidenceFragment | None:
        m = self._session.get(EvidenceFragmentModel, fragment_id)
        return _fragment_to_domain(m) if m else None

    def find_fragment_by_document(self, document_id: uuid.UUID) -> EvidenceFragment | None:
        """For the manual-Fact 1-document-to-1-fragment pattern shared by
        allocate_invoice_item.py and contract_item_facts.py: after a
        sha256 document-dedup hit, look up the fragment that document
        already carries so a replay reuses it instead of writing a
        second one."""
        m = self._session.scalar(
            select(EvidenceFragmentModel).where(EvidenceFragmentModel.evidence_document_id == document_id)
        )
        return _fragment_to_domain(m) if m else None


class ContractRepository:
    """Anchor + current-revision assembly (docs/PHASE2D1-R0-DECISIONS.md
    section 1.3/4.4), the same pattern as ContractItemRepository. ``get``
    / ``find_by_contract_no`` / ``find_by_identity`` / ``list_all`` join
    the anchor to its current (un-superseded) revision and return the
    pre-R5-shaped ``Contract`` dataclass — every existing consumer
    (contract_360.py, contract_business_ledger.py, matching.py,
    period_close.py, import_contract_ledger.py, ...) is unaffected.

    Business identity is ``(contract_no, counterparty)`` —
    deliberately NOT a unique constraint (R0 explicitly permits
    ambiguity, resolved by Task, never by schema prohibition), so
    ``find_by_identity`` returns a list like ``find_by_contract_no``
    already does."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _current_revision_join(self):
        return select(ContractModel, ContractRevisionModel).join(
            ContractRevisionModel,
            (ContractRevisionModel.contract_id == ContractModel.id)
            & (ContractRevisionModel.superseded_by_revision_id.is_(None)),
        )

    def add(self, contract: Contract) -> None:
        """Back-compat convenience over create_anchor + create_initial_revision
        — see ContractItemRepository.add()'s docstring for the full
        rationale; the same Evidence-required invariant applies."""
        if contract.current_source_fragment_id is None:
            raise ValueError(
                "ContractRepository.add() requires a real current_source_fragment_id — "
                "create a synthetic EvidenceFragment first; NULL provenance is only ever "
                "tolerated for legacy pre-R5 data carried forward by the migration"
            )
        self.create_anchor(
            id=contract.id, contract_no=contract.contract_no, counterparty=contract.counterparty,
            created_at=contract.created_at,
        )
        self.create_initial_revision(
            ContractRevision(
                id=uuid.uuid4(),
                contract_id=contract.id,
                revision_type=ContractRevisionType.INITIAL,
                contract_type=contract.contract_type,
                buyer=contract.buyer,
                gross_amount=contract.gross_amount,
                currency=contract.currency,
                contract_date=contract.contract_date,
                source_fragment_id=contract.current_source_fragment_id,
                superseded_by_revision_id=None,
                created_at=contract.updated_at,
            )
        )

    def create_anchor(self, *, id: uuid.UUID, contract_no: str, counterparty: str | None, created_at) -> None:
        self._session.add(ContractModel(id=id, contract_no=contract_no, counterparty=counterparty, created_at=created_at))

    def create_initial_revision(self, revision: ContractRevision) -> None:
        if revision.source_fragment_id is None:
            raise ValueError("ContractRevision.source_fragment_id is required for a new revision")
        if revision.revision_type != ContractRevisionType.INITIAL:
            raise ValueError("create_initial_revision only accepts revision_type=INITIAL")
        if revision.superseded_by_revision_id is not None:
            raise ValueError("a newly created current revision cannot already be superseded")
        self._session.add(self._revision_model(revision))

    def append_revision_against_current(self, revision: ContractRevision, *, based_on_revision_id: uuid.UUID) -> bool:
        """Atomic conditional retire-then-insert — identical pattern to
        ContractItemRepository.append_revision_against_current(). Returns
        False (writes nothing) if ``based_on_revision_id`` is not the
        anchor's current revision; the caller must treat that as a
        conflict, never retry blindly."""
        if revision.source_fragment_id is None:
            raise ValueError("ContractRevision.source_fragment_id is required for a new revision")
        if revision.superseded_by_revision_id is not None:
            raise ValueError("a newly created current revision cannot already be superseded")
        if revision.revision_type not in (ContractRevisionType.SUPPLEMENT, ContractRevisionType.CORRECTION):
            raise ValueError(
                "append_revision_against_current only accepts revision_type SUPPLEMENT or CORRECTION, got "
                f"{revision.revision_type!r}"
            )
        _defer_fk_checks(self._session)
        result = self._session.execute(
            update(ContractRevisionModel)
            .where(
                ContractRevisionModel.id == based_on_revision_id,
                ContractRevisionModel.contract_id == revision.contract_id,
                ContractRevisionModel.superseded_by_revision_id.is_(None),
            )
            .values(superseded_by_revision_id=revision.id)
        )
        if result.rowcount != 1:
            return False
        self._session.add(self._revision_model(revision))
        self._session.flush()
        return True

    def _revision_model(self, revision: ContractRevision) -> ContractRevisionModel:
        return ContractRevisionModel(
            id=revision.id,
            contract_id=revision.contract_id,
            revision_type=revision.revision_type,
            contract_type=revision.contract_type,
            buyer=revision.buyer,
            gross_amount=revision.gross_amount,
            currency=revision.currency,
            contract_date=revision.contract_date,
            source_fragment_id=revision.source_fragment_id,
            superseded_by_revision_id=revision.superseded_by_revision_id,
            created_at=revision.created_at,
            asserted_field_names=revision.asserted_field_names,
        )

    def get_current_revision(self, contract_id: uuid.UUID) -> ContractRevision | None:
        m = self._session.scalar(
            select(ContractRevisionModel).where(
                ContractRevisionModel.contract_id == contract_id,
                ContractRevisionModel.superseded_by_revision_id.is_(None),
            )
        )
        return _contract_revision_to_domain(m) if m else None

    def get_initial_revision(self, contract_id: uuid.UUID) -> ContractRevision | None:
        m = self._session.scalar(
            select(ContractRevisionModel).where(
                ContractRevisionModel.contract_id == contract_id,
                ContractRevisionModel.revision_type == ContractRevisionType.INITIAL,
            )
        )
        return _contract_revision_to_domain(m) if m else None

    def find_predecessor(self, revision_id: uuid.UUID) -> ContractRevision | None:
        m = self._session.scalar(
            select(ContractRevisionModel).where(ContractRevisionModel.superseded_by_revision_id == revision_id)
        )
        return _contract_revision_to_domain(m) if m else None

    def find_revision_by_fragment(self, contract_id: uuid.UUID, source_fragment_id: uuid.UUID) -> ContractRevision | None:
        m = self._session.scalar(
            select(ContractRevisionModel).where(
                ContractRevisionModel.contract_id == contract_id,
                ContractRevisionModel.source_fragment_id == source_fragment_id,
            )
        )
        return _contract_revision_to_domain(m) if m else None

    def list_revisions(self, contract_id: uuid.UUID) -> list[ContractRevision]:
        """Full audit history, oldest first — walked along the
        append-only ``superseded_by_revision_id`` chain, NOT sorted by
        ``created_at`` (two revisions in one transaction can share a
        timestamp)."""
        rows = {
            m.id: m
            for m in self._session.scalars(
                select(ContractRevisionModel).where(ContractRevisionModel.contract_id == contract_id)
            )
        }
        if not rows:
            return []
        current = next(m for m in rows.values() if m.revision_type == ContractRevisionType.INITIAL)
        ordered = [current]
        while current.superseded_by_revision_id is not None:
            current = rows[current.superseded_by_revision_id]
            ordered.append(current)
        return [_contract_revision_to_domain(m) for m in ordered]

    def get(self, contract_id: uuid.UUID) -> Contract | None:
        row = self._session.execute(self._current_revision_join().where(ContractModel.id == contract_id)).first()
        return _assemble_contract(*row) if row else None

    def find_by_contract_no(self, contract_no: str) -> list[Contract]:
        rows = self._session.execute(self._current_revision_join().where(ContractModel.contract_no == contract_no))
        return [_assemble_contract(anchor, rev) for anchor, rev in rows]

    def find_by_identity(self, contract_no: str, counterparty: str | None) -> list[Contract]:
        """The (contract_no, counterparty) identity lookup R5 backfill
        uses to resolve 0 / 1 / many existing anchors — never assumed
        unique (docs/PHASE2D1-R0-DECISIONS.md section 4.4)."""
        rows = self._session.execute(
            self._current_revision_join().where(
                ContractModel.contract_no == contract_no, ContractModel.counterparty == counterparty
            )
        )
        return [_assemble_contract(anchor, rev) for anchor, rev in rows]

    def list_all(self) -> list[Contract]:
        rows = self._session.execute(self._current_revision_join())
        return [_assemble_contract(anchor, rev) for anchor, rev in rows]

    def count(self) -> int:
        return len(self._session.scalars(select(ContractModel.id)).all())


class ContractItemRepository:
    """Anchor + current-revision assembly (docs/PHASE2D1-R0-DECISIONS.md
    section 1.3). ``get`` / ``find_by_contract_and_key`` / ``list_*``
    join the anchor to its current (un-superseded) revision and return
    the pre-R1-shaped ``ContractItem`` dataclass — the single seam every
    consumer (period_close.py, contract_360.py, import_close_facts.py,
    allocate_invoice_item.py) reads through, unaware anything changed.

    Anchor and revision writes (``create_anchor`` /
    ``create_initial_revision`` / ``append_revision_against_current``)
    are the primitives ``bel.application.contract_item_facts`` composes
    into the three Fact-maintenance operations (create / supplement /
    correct). There is no unchecked "just insert a revision row"
    primitive — every write path either creates the one INITIAL
    revision an anchor is allowed, or atomically retires the current
    revision before installing its replacement, so a caller cannot
    silently create a second current revision or a NULL-provenance Fact
    (Phase 2D.1-R1 Codex fix round, BLOCKER 3). This repository never
    guesses which revision is current by timestamp — only
    ``superseded_by_revision_id IS NULL`` decides that, in the query
    below and nowhere else, backed by the
    ``uq_contract_item_revisions_one_current`` partial unique index
    (BLOCKER 4)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _current_revision_join(self):
        return select(ContractItemModel, ContractItemRevisionModel).join(
            ContractItemRevisionModel,
            (ContractItemRevisionModel.contract_item_id == ContractItemModel.id)
            & (ContractItemRevisionModel.superseded_by_revision_id.is_(None)),
        )

    def add(self, item: ContractItem) -> None:
        """Back-compat convenience over create_anchor + create_initial_revision:
        builds the anchor and one INITIAL revision from a fully-populated
        ContractItem in a single call, for callers (tests, simple
        fixtures) that don't need the explicit create/supplement/correct
        Fact-maintenance commands in bel.application.contract_item_facts.
        Not a second write mechanism — it composes the same safe
        primitives every other writer uses, so it is subject to the same
        Evidence-required invariant (Phase 2D.1-R1 Codex fix round,
        BLOCKER 3): ``item.current_source_fragment_id`` must be a real
        fragment id. A fixture that has no Evidence to cite must create
        one first (see the ``_make_fragment``-style helpers throughout
        tests/) — it is never acceptable to fabricate a NULL-provenance
        Fact just because a test finds that convenient."""
        if item.current_source_fragment_id is None:
            raise ValueError(
                "ContractItemRepository.add() requires a real current_source_fragment_id — "
                "create a synthetic EvidenceFragment first; NULL provenance is only ever "
                "tolerated for legacy pre-R1 data carried forward by the migration"
            )
        self.create_anchor(
            id=item.id, contract_id=item.contract_id, source_item_key=item.source_item_key, created_at=item.created_at
        )
        self.create_initial_revision(
            ContractItemRevision(
                id=uuid.uuid4(),
                contract_item_id=item.id,
                revision_type=ContractItemRevisionType.INITIAL,
                sku=item.sku,
                product_name=item.product_name,
                specification=item.specification,
                quantity=item.quantity,
                unit=item.unit,
                unit_price=item.unit_price,
                gross_amount=item.gross_amount,
                tax_rate=item.tax_rate,
                net_amount=item.net_amount,
                source_fragment_id=item.current_source_fragment_id,
                superseded_by_revision_id=None,
                created_at=item.created_at,
            )
        )

    def create_anchor(self, *, id: uuid.UUID, contract_id: uuid.UUID, source_item_key: str, created_at) -> None:
        """Creates the bare identity anchor only — no business values, no
        revision. Callers (bel.application.contract_item_facts) always
        pair this with an immediate ``create_initial_revision`` call,
        inside the same transaction."""
        self._session.add(
            ContractItemModel(id=id, contract_id=contract_id, source_item_key=source_item_key, created_at=created_at)
        )

    def create_initial_revision(self, revision: ContractItemRevision) -> None:
        """The ONLY way to write an anchor's first (INITIAL) revision
        through the normal repository API (Phase 2D.1-R1 Codex fix
        round, BLOCKER 3). Requires real Evidence: a NEW canonical
        revision created through this repository must never carry
        ``source_fragment_id=None``. The one place a NULL provenance
        reference legitimately exists — a row carried forward from
        pre-R1 data whose original provenance was already unknown —
        is written directly by the
        db1c3258569e_contract_item_fact_revisions migration's raw SQL,
        which does not go through this repository at all."""
        if revision.source_fragment_id is None:
            raise ValueError("ContractItemRevision.source_fragment_id is required for a new revision")
        if revision.revision_type != ContractItemRevisionType.INITIAL:
            raise ValueError("create_initial_revision only accepts revision_type=INITIAL")
        if revision.superseded_by_revision_id is not None:
            raise ValueError("a newly created current revision cannot already be superseded")
        self._session.add(self._revision_model(revision))

    def append_revision_against_current(self, revision: ContractItemRevision, *, based_on_revision_id: uuid.UUID) -> bool:
        """The ONLY way to append a SUPPLEMENT/CORRECTION revision
        through the normal repository API (Phase 2D.1-R1 Codex fix
        round, BLOCKERs 2-4). Atomically retires ``based_on_revision_id``
        with a single conditional ``UPDATE ... WHERE id = :based_on AND
        contract_item_id = :anchor AND superseded_by_revision_id IS NULL``
        and only inserts ``revision`` as the new current row if EXACTLY
        one row was retired.

        Only ``SUPPLEMENT`` and ``CORRECTION`` may ever be appended this
        way (Phase 2D.1-R1 Codex fix round #3, FIX 1) — an ``INITIAL`` or
        any unrecognised ``revision_type`` is a caller bug, not a
        conflict, and raises ``ValueError`` immediately, before any
        UPDATE or INSERT runs. Confusing that with a CAS failure would
        let a malformed call silently do nothing instead of surfacing
        the bug.

        The anchor-ownership check (``contract_item_id ==
        revision.contract_item_id``) is folded into the SAME conditional
        UPDATE as the current-revision check (FIX 2), not a separate
        SELECT beforehand — a check-then-act split would reopen exactly
        the race this method exists to close, just on ownership instead
        of currency. Returns ``False`` — having written NOTHING — for
        any of: ``based_on_revision_id`` does not exist, is no longer
        current (someone else already superseded it), or does not
        belong to ``revision.contract_item_id`` (a cross-anchor append
        attempt). The caller MUST treat ``False`` as a conflict and
        never retry blindly; it must never fall back to inserting a
        second current row. The ``uq_contract_item_revisions_one_current``
        partial unique index is the DB-level backstop behind the
        current-revision half of this guarantee, not a substitute for
        it — the conditional UPDATE is what actually closes the
        check-then-act race between two independent sessions."""
        if revision.source_fragment_id is None:
            raise ValueError("ContractItemRevision.source_fragment_id is required for a new revision")
        if revision.superseded_by_revision_id is not None:
            raise ValueError("a newly created current revision cannot already be superseded")
        if revision.revision_type not in (ContractItemRevisionType.SUPPLEMENT, ContractItemRevisionType.CORRECTION):
            raise ValueError(
                "append_revision_against_current only accepts revision_type SUPPLEMENT or CORRECTION, got "
                f"{revision.revision_type!r} — INITIAL must go through create_initial_revision, and no other "
                "value is a legal revision_type"
            )
        # The old row's superseded_by_revision_id must point at `revision.id`
        # before `revision` itself is inserted, and the new row must be
        # inserted while it is still the anchor's ONLY current row — the
        # retire-then-insert order the partial unique index requires. That
        # makes the retiring UPDATE momentarily reference a row that does
        # not exist yet, which SQLite's (session-scoped, connect-time)
        # `PRAGMA foreign_keys=ON` would otherwise reject outright.
        # `defer_foreign_keys` defers the REFERENTIAL check to this
        # transaction's commit — by which point the insert below has
        # happened — without weakening the FK constraint itself; SQLite
        # resets it automatically at the end of every transaction, so it
        # is set fresh here rather than once at connect time.
        _defer_fk_checks(self._session)
        result = self._session.execute(
            update(ContractItemRevisionModel)
            .where(
                ContractItemRevisionModel.id == based_on_revision_id,
                ContractItemRevisionModel.contract_item_id == revision.contract_item_id,
                ContractItemRevisionModel.superseded_by_revision_id.is_(None),
            )
            .values(superseded_by_revision_id=revision.id)
        )
        if result.rowcount != 1:
            return False
        self._session.add(self._revision_model(revision))
        self._session.flush()
        return True

    def _revision_model(self, revision: ContractItemRevision) -> ContractItemRevisionModel:
        return ContractItemRevisionModel(
            id=revision.id,
            contract_item_id=revision.contract_item_id,
            revision_type=revision.revision_type,
            sku=revision.sku,
            product_name=revision.product_name,
            specification=revision.specification,
            quantity=revision.quantity,
            unit=revision.unit,
            unit_price=revision.unit_price,
            gross_amount=revision.gross_amount,
            tax_rate=revision.tax_rate,
            net_amount=revision.net_amount,
            source_fragment_id=revision.source_fragment_id,
            superseded_by_revision_id=revision.superseded_by_revision_id,
            created_at=revision.created_at,
            asserted_field_names=revision.asserted_field_names,
        )

    def get_current_revision(self, contract_item_id: uuid.UUID) -> ContractItemRevision | None:
        m = self._session.scalar(
            select(ContractItemRevisionModel).where(
                ContractItemRevisionModel.contract_item_id == contract_item_id,
                ContractItemRevisionModel.superseded_by_revision_id.is_(None),
            )
        )
        return _contract_item_revision_to_domain(m) if m else None

    def get_initial_revision(self, contract_item_id: uuid.UUID) -> ContractItemRevision | None:
        m = self._session.scalar(
            select(ContractItemRevisionModel).where(
                ContractItemRevisionModel.contract_item_id == contract_item_id,
                ContractItemRevisionModel.revision_type == ContractItemRevisionType.INITIAL,
            )
        )
        return _contract_item_revision_to_domain(m) if m else None

    def find_predecessor(self, revision_id: uuid.UUID) -> ContractItemRevision | None:
        """The revision that ``revision_id`` superseded, if any — the
        reverse direction of ``superseded_by_revision_id``. Used by
        ``bel.application.contract_item_facts._asserted_fields`` to
        compute exactly which fields a revision actually asserted,
        without a separate assertion-metadata column."""
        m = self._session.scalar(
            select(ContractItemRevisionModel).where(ContractItemRevisionModel.superseded_by_revision_id == revision_id)
        )
        return _contract_item_revision_to_domain(m) if m else None

    def find_revision_by_fragment(
        self, contract_item_id: uuid.UUID, source_fragment_id: uuid.UUID
    ) -> ContractItemRevision | None:
        """The replay/reuse lookup: the same Evidence fragment already
        asserted SOMETHING against this anchor. Callers must not treat a
        hit as an automatic replay — see
        ``bel.application.contract_item_facts._apply_revision``, which
        compares ``revision_type`` and the actual asserted field/value
        content before deciding replay vs. conflict (Phase 2D.1-R1 Codex
        fix round, BLOCKER 2)."""
        m = self._session.scalar(
            select(ContractItemRevisionModel).where(
                ContractItemRevisionModel.contract_item_id == contract_item_id,
                ContractItemRevisionModel.source_fragment_id == source_fragment_id,
            )
        )
        return _contract_item_revision_to_domain(m) if m else None

    def list_revisions(self, contract_item_id: uuid.UUID) -> list[ContractItemRevision]:
        """Full audit history, oldest first — walked along the
        append-only ``superseded_by_revision_id`` chain from the INITIAL
        revision forward, NOT sorted by ``created_at`` (Phase 2D.1-R1
        Codex fix round, WARNING: two revisions written in the same
        transaction can share an identical timestamp, which made
        ``ORDER BY created_at`` alone non-deterministic). The chain
        itself is the model's own intrinsic, always-unambiguous order."""
        rows = {
            m.id: m
            for m in self._session.scalars(
                select(ContractItemRevisionModel).where(ContractItemRevisionModel.contract_item_id == contract_item_id)
            )
        }
        if not rows:
            return []
        current = next(m for m in rows.values() if m.revision_type == ContractItemRevisionType.INITIAL)
        ordered = [current]
        while current.superseded_by_revision_id is not None:
            current = rows[current.superseded_by_revision_id]
            ordered.append(current)
        return [_contract_item_revision_to_domain(m) for m in ordered]

    def get(self, item_id: uuid.UUID) -> ContractItem | None:
        row = self._session.execute(self._current_revision_join().where(ContractItemModel.id == item_id)).first()
        return _assemble_contract_item(*row) if row else None

    def find_by_contract_and_key(self, contract_id: uuid.UUID, source_item_key: str) -> ContractItem | None:
        row = self._session.execute(
            self._current_revision_join().where(
                ContractItemModel.contract_id == contract_id, ContractItemModel.source_item_key == source_item_key
            )
        ).first()
        return _assemble_contract_item(*row) if row else None

    def list_for_contract(self, contract_id: uuid.UUID) -> list[ContractItem]:
        rows = self._session.execute(
            self._current_revision_join()
            .where(ContractItemModel.contract_id == contract_id)
            .order_by(ContractItemModel.created_at)
        )
        return [_assemble_contract_item(anchor, rev) for anchor, rev in rows]

    def list_all(self) -> list[ContractItem]:
        rows = self._session.execute(self._current_revision_join())
        return [_assemble_contract_item(anchor, rev) for anchor, rev in rows]

    def count(self) -> int:
        return len(self._session.scalars(select(ContractItemModel.id)).all())


class ShipmentRepository:
    """Anchor + current-revision assembly for Shipment (Phase 2D.1-R2),
    the same pattern as ContractItemRepository — reused deliberately,
    not abstracted into a shared generic engine. ``get`` /
    ``find_by_identity`` / ``list_*`` join the anchor to its current
    (un-superseded) revision and return the assembled ``Shipment``
    dataclass.

    Anchor and revision writes (``create_anchor`` /
    ``create_initial_revision`` / ``append_revision_against_current``)
    are the primitives ``bel.application.shipment_facts`` composes into
    the three Fact-maintenance operations (create / supplement /
    correct). There is no unchecked "just insert a revision row"
    primitive, mirroring every structural invariant closed in the Phase
    2D.1-R1 Codex fix rounds: Evidence required for every new revision,
    at most one INITIAL and one current revision per anchor (DB-backed),
    the closed revision_type set (DB-backed), and an anchor-scoped
    conditional-retire-then-insert CAS that cannot cross anchors."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _current_revision_join(self):
        return select(ShipmentModel, ShipmentRevisionModel).join(
            ShipmentRevisionModel,
            (ShipmentRevisionModel.shipment_id == ShipmentModel.id)
            & (ShipmentRevisionModel.superseded_by_revision_id.is_(None)),
        )

    def create_anchor(
        self, *, id: uuid.UUID, contract_id: uuid.UUID, external_reference: str | None, execution_date, created_at
    ) -> None:
        """Creates the bare identity anchor only — no business values, no
        revision. Callers (bel.application.shipment_facts) always pair
        this with an immediate ``create_initial_revision`` call, inside
        the same transaction."""
        self._session.add(
            ShipmentModel(
                id=id,
                contract_id=contract_id,
                external_reference=external_reference,
                execution_date=execution_date,
                created_at=created_at,
            )
        )

    def create_initial_revision(self, revision: ShipmentRevision) -> None:
        """The ONLY way to write an anchor's first (INITIAL) revision
        through the normal repository API. Requires real Evidence: a new
        canonical revision must never carry ``source_fragment_id=None``
        (there is no pre-R2 legacy Shipment data to accommodate a
        looser rule for, unlike ContractItem)."""
        if revision.source_fragment_id is None:
            raise ValueError("ShipmentRevision.source_fragment_id is required for a new revision")
        if revision.revision_type != ShipmentRevisionType.INITIAL:
            raise ValueError("create_initial_revision only accepts revision_type=INITIAL")
        if revision.superseded_by_revision_id is not None:
            raise ValueError("a newly created current revision cannot already be superseded")
        self._session.add(self._revision_model(revision))

    def append_revision_against_current(self, revision: ShipmentRevision, *, based_on_revision_id: uuid.UUID) -> bool:
        """The ONLY way to append a SUPPLEMENT/CORRECTION revision
        through the normal repository API. Atomically retires
        ``based_on_revision_id`` with a single conditional
        ``UPDATE ... WHERE id = :based_on AND shipment_id = :anchor AND
        superseded_by_revision_id IS NULL`` and only inserts ``revision``
        as the new current row if EXACTLY one row was retired.

        Only ``SUPPLEMENT`` and ``CORRECTION`` may ever be appended this
        way — an ``INITIAL`` or any unrecognised ``revision_type`` is a
        caller bug, not a conflict, and raises ``ValueError`` immediately,
        before any UPDATE or INSERT runs.

        The anchor-ownership check (``shipment_id ==
        revision.shipment_id``) is folded into the SAME conditional
        UPDATE as the current-revision check, not a separate SELECT
        beforehand — a check-then-act split would reopen the exact race
        this method exists to close, just on ownership instead of
        currency. Returns ``False`` — having written NOTHING — for any
        of: ``based_on_revision_id`` does not exist, is no longer current
        (someone else already superseded it), or does not belong to
        ``revision.shipment_id`` (a cross-anchor append attempt). The
        caller MUST treat ``False`` as a conflict and never retry
        blindly. The ``uq_shipment_revisions_one_current`` partial unique
        index is the DB-level backstop behind the current-revision half
        of this guarantee, not a substitute for it."""
        if revision.source_fragment_id is None:
            raise ValueError("ShipmentRevision.source_fragment_id is required for a new revision")
        if revision.superseded_by_revision_id is not None:
            raise ValueError("a newly created current revision cannot already be superseded")
        if revision.revision_type not in (ShipmentRevisionType.SUPPLEMENT, ShipmentRevisionType.CORRECTION):
            raise ValueError(
                "append_revision_against_current only accepts revision_type SUPPLEMENT or CORRECTION, got "
                f"{revision.revision_type!r} — INITIAL must go through create_initial_revision, and no other "
                "value is a legal revision_type"
            )
        # See ContractItemRepository.append_revision_against_current for
        # why defer_foreign_keys is required here: the retiring UPDATE
        # must point the old row at `revision.id` before `revision`
        # itself is inserted, and SQLite's connect-time
        # `PRAGMA foreign_keys=ON` would otherwise reject a reference to
        # a row that does not exist yet.
        _defer_fk_checks(self._session)
        result = self._session.execute(
            update(ShipmentRevisionModel)
            .where(
                ShipmentRevisionModel.id == based_on_revision_id,
                ShipmentRevisionModel.shipment_id == revision.shipment_id,
                ShipmentRevisionModel.superseded_by_revision_id.is_(None),
            )
            .values(superseded_by_revision_id=revision.id)
        )
        if result.rowcount != 1:
            return False
        self._session.add(self._revision_model(revision))
        self._session.flush()
        return True

    def _revision_model(self, revision: ShipmentRevision) -> ShipmentRevisionModel:
        return ShipmentRevisionModel(
            id=revision.id,
            shipment_id=revision.shipment_id,
            revision_type=revision.revision_type,
            contract_item_id=revision.contract_item_id,
            quantity=revision.quantity,
            declared_amount=revision.declared_amount,
            declared_currency=revision.declared_currency,
            source_fragment_id=revision.source_fragment_id,
            superseded_by_revision_id=revision.superseded_by_revision_id,
            created_at=revision.created_at,
            asserted_field_names=revision.asserted_field_names,
        )

    def get_current_revision(self, shipment_id: uuid.UUID) -> ShipmentRevision | None:
        m = self._session.scalar(
            select(ShipmentRevisionModel).where(
                ShipmentRevisionModel.shipment_id == shipment_id,
                ShipmentRevisionModel.superseded_by_revision_id.is_(None),
            )
        )
        return _shipment_revision_to_domain(m) if m else None

    def get_initial_revision(self, shipment_id: uuid.UUID) -> ShipmentRevision | None:
        m = self._session.scalar(
            select(ShipmentRevisionModel).where(
                ShipmentRevisionModel.shipment_id == shipment_id,
                ShipmentRevisionModel.revision_type == ShipmentRevisionType.INITIAL,
            )
        )
        return _shipment_revision_to_domain(m) if m else None

    def find_predecessor(self, revision_id: uuid.UUID) -> ShipmentRevision | None:
        m = self._session.scalar(
            select(ShipmentRevisionModel).where(ShipmentRevisionModel.superseded_by_revision_id == revision_id)
        )
        return _shipment_revision_to_domain(m) if m else None

    def find_revision_by_fragment(self, shipment_id: uuid.UUID, source_fragment_id: uuid.UUID) -> ShipmentRevision | None:
        """The replay/reuse lookup: the same Evidence fragment already
        asserted SOMETHING against this anchor. Callers must not treat a
        hit as an automatic replay — see
        ``bel.application.shipment_facts._apply_revision``, which
        compares ``revision_type`` and the actual asserted field/value
        content before deciding replay vs. conflict."""
        m = self._session.scalar(
            select(ShipmentRevisionModel).where(
                ShipmentRevisionModel.shipment_id == shipment_id,
                ShipmentRevisionModel.source_fragment_id == source_fragment_id,
            )
        )
        return _shipment_revision_to_domain(m) if m else None

    def find_revisions_by_fragment_id(self, source_fragment_id: uuid.UUID) -> list[ShipmentRevision]:
        """The GLOBAL (not anchor-scoped) lookup — for the one case where
        no business identity exists to scope a lookup by: a confirmed
        create with no ``external_reference`` (Phase 2D.1-R2 Codex fix
        round, BLOCKER 1). There is no ``(contract_id, external_reference,
        execution_date)`` to look this up by, so this searches
        ``shipment_revisions`` directly by fragment alone.

        Returns EVERY match, never just one (Phase 2D.1-R2 second Codex
        fix round): an earlier version used ``session.scalar()``, which
        silently returns an arbitrary first row when more than one
        revision happens to share a ``source_fragment_id`` — risking a
        cross-contract misattribution if the SAME fragment id is ever
        reused for an unrelated Shipment. The caller
        (``bel.application.shipment_facts.create_shipment_fact``) must
        verify every returned candidate's anchor identity, revision type
        and asserted content before treating any of them as a genuine
        replay, and must reject rather than guess when zero or more than
        one candidate is an exact match."""
        rows = self._session.scalars(
            select(ShipmentRevisionModel).where(ShipmentRevisionModel.source_fragment_id == source_fragment_id)
        )
        return [_shipment_revision_to_domain(m) for m in rows]

    def list_revisions(self, shipment_id: uuid.UUID) -> list[ShipmentRevision]:
        """Full audit history, oldest first — walked along the
        append-only ``superseded_by_revision_id`` chain from the INITIAL
        revision forward, NOT sorted by ``created_at`` (two revisions
        written in the same transaction can share an identical
        timestamp). The chain itself is the model's own intrinsic,
        always-unambiguous order."""
        rows = {
            m.id: m
            for m in self._session.scalars(
                select(ShipmentRevisionModel).where(ShipmentRevisionModel.shipment_id == shipment_id)
            )
        }
        if not rows:
            return []
        current = next(m for m in rows.values() if m.revision_type == ShipmentRevisionType.INITIAL)
        ordered = [current]
        while current.superseded_by_revision_id is not None:
            current = rows[current.superseded_by_revision_id]
            ordered.append(current)
        return [_shipment_revision_to_domain(m) for m in ordered]

    def get(self, shipment_id: uuid.UUID) -> Shipment | None:
        row = self._session.execute(self._current_revision_join().where(ShipmentModel.id == shipment_id)).first()
        return _assemble_shipment(*row) if row else None

    def find_by_identity(
        self, contract_id: uuid.UUID, external_reference: str, execution_date
    ) -> Shipment | None:
        """Resolves the frozen business identity
        (docs/PHASE2D1-R0-DECISIONS.md section 4.4:
        ``(contract_id, external_reference, execution_date)``).
        ``external_reference`` must be a real (non-None) value here —
        callers must never look up by identity when it is None (section
        4.4's "identity incomplete"); see
        ``bel.application.shipment_facts.create_shipment_fact``, which
        skips this lookup entirely in that case rather than passing
        None through to an ``IS NULL`` match that would collide
        unrelated Shipments."""
        row = self._session.execute(
            self._current_revision_join().where(
                ShipmentModel.contract_id == contract_id,
                ShipmentModel.external_reference == external_reference,
                ShipmentModel.execution_date == execution_date,
            )
        ).first()
        return _assemble_shipment(*row) if row else None

    def list_for_contract(self, contract_id: uuid.UUID) -> list[Shipment]:
        rows = self._session.execute(
            self._current_revision_join()
            .where(ShipmentModel.contract_id == contract_id)
            .order_by(ShipmentModel.created_at, ShipmentModel.id)
        )
        return [_assemble_shipment(anchor, rev) for anchor, rev in rows]

    def list_all(self) -> list[Shipment]:
        rows = self._session.execute(self._current_revision_join())
        return [_assemble_shipment(anchor, rev) for anchor, rev in rows]

    def count(self) -> int:
        return len(self._session.scalars(select(ShipmentModel.id)).all())


class SalesContractRepository:
    """Anchor + current-revision assembly for SalesContract (Phase
    2D.1-R3a Slice 1), the same pattern as
    ContractItemRepository/ShipmentRepository — reused deliberately, not
    abstracted into a shared generic engine. ``get`` / ``find_by_identity``
    / ``list_*`` join the anchor to its current (un-superseded) revision
    and return the assembled ``SalesContract`` dataclass.

    Anchor and revision writes (``create_anchor`` /
    ``create_initial_revision`` / ``append_revision_against_current``)
    are the primitives ``bel.application.sales_contract_facts`` composes
    into the three Fact-maintenance operations (create / supplement /
    correct). There is no unchecked "just insert a revision row"
    primitive, mirroring every structural invariant closed in the Phase
    2D.1-R1/R2 Codex fix rounds: Evidence required for every new
    revision, at most one INITIAL and one current revision per anchor
    (DB-backed), the closed revision_type set (DB-backed), and an
    anchor-scoped conditional-retire-then-insert CAS that cannot cross
    anchors.

    Unlike ShipmentRepository, there is no global (non-anchor-scoped)
    fragment lookup here: SalesContract's frozen identity null policy
    (section 4.4) never permits creating an anchor with an incomplete
    identity in the first place ("NO canonical anchor may be created"),
    so the class of cross-anchor-misattribution risk that required
    Shipment's candidate-filtering fix cannot arise for SalesContract."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _current_revision_join(self):
        return select(SalesContractModel, SalesContractRevisionModel).join(
            SalesContractRevisionModel,
            (SalesContractRevisionModel.sales_contract_id == SalesContractModel.id)
            & (SalesContractRevisionModel.superseded_by_revision_id.is_(None)),
        )

    def create_anchor(self, *, id: uuid.UUID, our_entity: str, sales_contract_no: str, created_at) -> None:
        """Creates the bare identity anchor only — no business values, no
        revision. Callers (bel.application.sales_contract_facts) always
        pair this with an immediate ``create_initial_revision`` call,
        inside the same transaction."""
        self._session.add(
            SalesContractModel(id=id, our_entity=our_entity, sales_contract_no=sales_contract_no, created_at=created_at)
        )

    def create_initial_revision(self, revision: SalesContractRevision) -> None:
        """The ONLY way to write an anchor's first (INITIAL) revision
        through the normal repository API. Requires real Evidence: a new
        canonical revision must never carry ``source_fragment_id=None``."""
        if revision.source_fragment_id is None:
            raise ValueError("SalesContractRevision.source_fragment_id is required for a new revision")
        if revision.revision_type != SalesContractRevisionType.INITIAL:
            raise ValueError("create_initial_revision only accepts revision_type=INITIAL")
        if revision.superseded_by_revision_id is not None:
            raise ValueError("a newly created current revision cannot already be superseded")
        self._session.add(self._revision_model(revision))

    def append_revision_against_current(
        self, revision: SalesContractRevision, *, based_on_revision_id: uuid.UUID
    ) -> bool:
        """The ONLY way to append a SUPPLEMENT/CORRECTION revision
        through the normal repository API. Atomically retires
        ``based_on_revision_id`` with a single conditional
        ``UPDATE ... WHERE id = :based_on AND sales_contract_id = :anchor
        AND superseded_by_revision_id IS NULL`` and only inserts
        ``revision`` as the new current row if EXACTLY one row was
        retired.

        Only ``SUPPLEMENT`` and ``CORRECTION`` may ever be appended this
        way — an ``INITIAL`` or any unrecognised ``revision_type`` is a
        caller bug, not a conflict, and raises ``ValueError``
        immediately, before any UPDATE or INSERT runs.

        The anchor-ownership check (``sales_contract_id ==
        revision.sales_contract_id``) is folded into the SAME conditional
        UPDATE as the current-revision check, not a separate SELECT
        beforehand. Returns ``False`` — having written NOTHING — for any
        of: ``based_on_revision_id`` does not exist, is no longer current,
        or does not belong to ``revision.sales_contract_id`` (a
        cross-anchor append attempt). The caller MUST treat ``False`` as
        a conflict and never retry blindly."""
        if revision.source_fragment_id is None:
            raise ValueError("SalesContractRevision.source_fragment_id is required for a new revision")
        if revision.superseded_by_revision_id is not None:
            raise ValueError("a newly created current revision cannot already be superseded")
        if revision.revision_type not in (SalesContractRevisionType.SUPPLEMENT, SalesContractRevisionType.CORRECTION):
            raise ValueError(
                "append_revision_against_current only accepts revision_type SUPPLEMENT or CORRECTION, got "
                f"{revision.revision_type!r} — INITIAL must go through create_initial_revision, and no other "
                "value is a legal revision_type"
            )
        # See ContractItemRepository.append_revision_against_current for
        # why defer_foreign_keys is required here: the retiring UPDATE
        # must point the old row at `revision.id` before `revision`
        # itself is inserted, and SQLite's connect-time
        # `PRAGMA foreign_keys=ON` would otherwise reject a reference to
        # a row that does not exist yet.
        _defer_fk_checks(self._session)
        result = self._session.execute(
            update(SalesContractRevisionModel)
            .where(
                SalesContractRevisionModel.id == based_on_revision_id,
                SalesContractRevisionModel.sales_contract_id == revision.sales_contract_id,
                SalesContractRevisionModel.superseded_by_revision_id.is_(None),
            )
            .values(superseded_by_revision_id=revision.id)
        )
        if result.rowcount != 1:
            return False
        self._session.add(self._revision_model(revision))
        self._session.flush()
        return True

    def _revision_model(self, revision: SalesContractRevision) -> SalesContractRevisionModel:
        return SalesContractRevisionModel(
            id=revision.id,
            sales_contract_id=revision.sales_contract_id,
            revision_type=revision.revision_type,
            customer=revision.customer,
            currency=revision.currency,
            gross_amount=revision.gross_amount,
            contract_date=revision.contract_date,
            source_fragment_id=revision.source_fragment_id,
            superseded_by_revision_id=revision.superseded_by_revision_id,
            created_at=revision.created_at,
            asserted_field_names=revision.asserted_field_names,
        )

    def get_current_revision(self, sales_contract_id: uuid.UUID) -> SalesContractRevision | None:
        m = self._session.scalar(
            select(SalesContractRevisionModel).where(
                SalesContractRevisionModel.sales_contract_id == sales_contract_id,
                SalesContractRevisionModel.superseded_by_revision_id.is_(None),
            )
        )
        return _sales_contract_revision_to_domain(m) if m else None

    def get_initial_revision(self, sales_contract_id: uuid.UUID) -> SalesContractRevision | None:
        m = self._session.scalar(
            select(SalesContractRevisionModel).where(
                SalesContractRevisionModel.sales_contract_id == sales_contract_id,
                SalesContractRevisionModel.revision_type == SalesContractRevisionType.INITIAL,
            )
        )
        return _sales_contract_revision_to_domain(m) if m else None

    def find_predecessor(self, revision_id: uuid.UUID) -> SalesContractRevision | None:
        m = self._session.scalar(
            select(SalesContractRevisionModel).where(SalesContractRevisionModel.superseded_by_revision_id == revision_id)
        )
        return _sales_contract_revision_to_domain(m) if m else None

    def find_revision_by_fragment(
        self, sales_contract_id: uuid.UUID, source_fragment_id: uuid.UUID
    ) -> SalesContractRevision | None:
        """The replay/reuse lookup: the same Evidence fragment already
        asserted SOMETHING against this anchor. Callers must not treat a
        hit as an automatic replay — see
        ``bel.application.sales_contract_facts._apply_revision``, which
        compares ``revision_type`` and the actual asserted field/value
        content before deciding replay vs. conflict."""
        m = self._session.scalar(
            select(SalesContractRevisionModel).where(
                SalesContractRevisionModel.sales_contract_id == sales_contract_id,
                SalesContractRevisionModel.source_fragment_id == source_fragment_id,
            )
        )
        return _sales_contract_revision_to_domain(m) if m else None

    def list_revisions(self, sales_contract_id: uuid.UUID) -> list[SalesContractRevision]:
        """Full audit history, oldest first — walked along the
        append-only ``superseded_by_revision_id`` chain from the INITIAL
        revision forward, NOT sorted by ``created_at``."""
        rows = {
            m.id: m
            for m in self._session.scalars(
                select(SalesContractRevisionModel).where(SalesContractRevisionModel.sales_contract_id == sales_contract_id)
            )
        }
        if not rows:
            return []
        current = next(m for m in rows.values() if m.revision_type == SalesContractRevisionType.INITIAL)
        ordered = [current]
        while current.superseded_by_revision_id is not None:
            current = rows[current.superseded_by_revision_id]
            ordered.append(current)
        return [_sales_contract_revision_to_domain(m) for m in ordered]

    def get(self, sales_contract_id: uuid.UUID) -> SalesContract | None:
        row = self._session.execute(
            self._current_revision_join().where(SalesContractModel.id == sales_contract_id)
        ).first()
        return _assemble_sales_contract(*row) if row else None

    def find_by_identity(self, our_entity: str, sales_contract_no: str) -> SalesContract | None:
        """Resolves the frozen business identity
        (docs/PHASE2D1-R0-DECISIONS.md section 4.4:
        ``(our_entity, sales_contract_no)``)."""
        row = self._session.execute(
            self._current_revision_join().where(
                SalesContractModel.our_entity == our_entity,
                SalesContractModel.sales_contract_no == sales_contract_no,
            )
        ).first()
        return _assemble_sales_contract(*row) if row else None

    def list_all(self) -> list[SalesContract]:
        rows = self._session.execute(
            self._current_revision_join().order_by(SalesContractModel.created_at, SalesContractModel.id)
        )
        return [_assemble_sales_contract(anchor, rev) for anchor, rev in rows]

    def count(self) -> int:
        return len(self._session.scalars(select(SalesContractModel.id)).all())


def _link_to_domain(m: ProcurementSalesLinkModel) -> ProcurementSalesLink:
    return ProcurementSalesLink(
        id=m.id,
        procurement_contract_id=m.procurement_contract_id,
        sales_contract_id=m.sales_contract_id,
        source_fragment_id=m.source_fragment_id,
        confirmation_type=m.confirmation_type,
        created_at=m.created_at,
    )


def _correction_to_domain(m: ProcurementSalesLinkCorrectionModel) -> ProcurementSalesLinkCorrection:
    return ProcurementSalesLinkCorrection(
        id=m.id,
        superseded_link_id=m.superseded_link_id,
        replacement_link_id=m.replacement_link_id,
        source_fragment_id=m.source_fragment_id,
        confirmation_type=m.confirmation_type,
        created_at=m.created_at,
    )


class ProcurementSalesLinkRepository:
    """Anchor-free Fact storage for `ProcurementSalesLink` /
    `ProcurementSalesLinkCorrection` (Phase 2D.1-R3a Slice 2,
    docs/PHASE2D1-R0-DECISIONS.md section 2.4). Unlike every other
    repository in this module, there is no anchor+revision assembly here
    — a link row IS the Fact, in full, forever; ``current()`` is a
    computed predicate over the corrections table, resolved in exactly
    ONE shared place (this class), exactly as
    ``get_accrual_balance``/``is_open_accrual`` are the shared predicates
    for accrual state.

    ``insert_episode_if_no_current`` / ``add_correction_if_uncorrected``
    are the ONLY write primitives, and both are single atomic
    ``INSERT ... SELECT ... WHERE NOT EXISTS (...)`` statements — never a
    separate "check current, then insert" round trip, which is exactly
    the race ``if repo.find_current() is None: insert()`` would lose
    under two concurrent sessions. The `NOT EXISTS` predicate is
    evaluated by SQLite as part of the SAME statement that performs the
    write, so two competing inserts for the same business key (or the
    same ``superseded_link_id``) can never both succeed — SQLite executes
    one write statement at a time. The
    ``trg_procurement_sales_links_one_current`` trigger (declared in
    models.py) is a second, independent backstop against the same
    invariant for any write that bypasses this repository entirely (a
    raw ORM ``session.add``); ``superseded_link_id UNIQUE`` on the
    corrections table plays the identical backstop role for corrections.

    No method here ever inspects ``created_at`` to decide which episode
    is current — that is exclusively a function of whether a correction
    names it as ``superseded_link_id``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, link_id: uuid.UUID) -> ProcurementSalesLink | None:
        m = self._session.get(ProcurementSalesLinkModel, link_id)
        return _link_to_domain(m) if m else None

    def is_current(self, link_id: uuid.UUID) -> bool:
        """The ONE shared current-predicate
        (docs/PHASE2D1-R0-DECISIONS.md: ``current(link) ⟺ no correction
        record names it as superseded_link_id``)."""
        corrected = self._session.scalar(
            select(ProcurementSalesLinkCorrectionModel.id).where(
                ProcurementSalesLinkCorrectionModel.superseded_link_id == link_id
            )
        )
        return corrected is None

    def get_current_link(
        self, procurement_contract_id: uuid.UUID, sales_contract_id: uuid.UUID
    ) -> ProcurementSalesLink | None:
        """The current episode for a relationship business key, if any.
        ``.scalar()`` deliberately raises if the storage-level invariant
        were ever somehow violated (more than one un-superseded episode
        for the same key) rather than silently picking one — that would
        be a bug worth surfacing loudly, never guessed past."""
        corrected_ids = select(ProcurementSalesLinkCorrectionModel.superseded_link_id)
        m = self._session.scalar(
            select(ProcurementSalesLinkModel).where(
                ProcurementSalesLinkModel.procurement_contract_id == procurement_contract_id,
                ProcurementSalesLinkModel.sales_contract_id == sales_contract_id,
                ProcurementSalesLinkModel.id.not_in(corrected_ids),
            )
        )
        return _link_to_domain(m) if m else None

    def list_episodes(
        self, procurement_contract_id: uuid.UUID, sales_contract_id: uuid.UUID
    ) -> list[ProcurementSalesLink]:
        """Every episode (current and retired) ever asserted for this
        business key — the full audit trail. Ordered by
        ``(created_at, id)`` purely for deterministic DISPLAY; this
        ordering is never used to decide which episode is current."""
        rows = self._session.scalars(
            select(ProcurementSalesLinkModel)
            .where(
                ProcurementSalesLinkModel.procurement_contract_id == procurement_contract_id,
                ProcurementSalesLinkModel.sales_contract_id == sales_contract_id,
            )
            .order_by(ProcurementSalesLinkModel.created_at, ProcurementSalesLinkModel.id)
        )
        return [_link_to_domain(m) for m in rows]

    def list_current_links_for_procurement_contract(
        self, procurement_contract_id: uuid.UUID
    ) -> list[ProcurementSalesLink]:
        corrected_ids = select(ProcurementSalesLinkCorrectionModel.superseded_link_id)
        rows = self._session.scalars(
            select(ProcurementSalesLinkModel)
            .where(
                ProcurementSalesLinkModel.procurement_contract_id == procurement_contract_id,
                ProcurementSalesLinkModel.id.not_in(corrected_ids),
            )
            .order_by(ProcurementSalesLinkModel.created_at, ProcurementSalesLinkModel.id)
        )
        return [_link_to_domain(m) for m in rows]

    def list_current_links_for_sales_contract(self, sales_contract_id: uuid.UUID) -> list[ProcurementSalesLink]:
        corrected_ids = select(ProcurementSalesLinkCorrectionModel.superseded_link_id)
        rows = self._session.scalars(
            select(ProcurementSalesLinkModel)
            .where(
                ProcurementSalesLinkModel.sales_contract_id == sales_contract_id,
                ProcurementSalesLinkModel.id.not_in(corrected_ids),
            )
            .order_by(ProcurementSalesLinkModel.created_at, ProcurementSalesLinkModel.id)
        )
        return [_link_to_domain(m) for m in rows]

    def insert_episode_if_no_current(self, link: ProcurementSalesLink) -> bool:
        """The ONLY way to create a new assertion episode. Succeeds
        (returns ``True``) only if no un-superseded episode already
        exists for ``(link.procurement_contract_id,
        link.sales_contract_id)`` — evaluated as part of the SAME
        statement, so this is the storage-level primitive both `ADD` and
        `REESTABLISH` share; they differ only in the application-layer
        precondition (whether history exists) checked before calling
        this, never in the write itself."""
        links = ProcurementSalesLinkModel.__table__
        corrections = ProcurementSalesLinkCorrectionModel.__table__
        existing = links.alias("existing")
        blocking_current = (
            select(existing.c.id)
            .where(
                existing.c.procurement_contract_id == link.procurement_contract_id,
                existing.c.sales_contract_id == link.sales_contract_id,
            )
            .where(~select(corrections.c.id).where(corrections.c.superseded_link_id == existing.c.id).exists())
        )
        select_values = select(
            literal(link.id, type_=links.c.id.type),
            literal(link.procurement_contract_id, type_=links.c.procurement_contract_id.type),
            literal(link.sales_contract_id, type_=links.c.sales_contract_id.type),
            literal(link.source_fragment_id, type_=links.c.source_fragment_id.type),
            literal(link.confirmation_type, type_=links.c.confirmation_type.type),
            literal(link.created_at, type_=links.c.created_at.type),
        ).where(~blocking_current.exists())
        stmt = links.insert().from_select(
            ["id", "procurement_contract_id", "sales_contract_id", "source_fragment_id", "confirmation_type", "created_at"],
            select_values,
        ).returning(links.c.id)
        # Never trust result.rowcount for INSERT...FROM SELECT — confirmed
        # empirically that SQLAlchemy's PostgreSQL/psycopg dialect reports
        # -1 (unsupported) for this statement shape even when a row WAS
        # inserted, which would silently make every caller of this method
        # believe it always lost the race. `.returning(...)` + checking
        # whether a row came back is portable across SQLite and PostgreSQL.
        inserted = self._session.execute(stmt).fetchone() is not None
        self._session.flush()
        return inserted

    def get_correction_for_superseded(self, superseded_link_id: uuid.UUID) -> ProcurementSalesLinkCorrection | None:
        m = self._session.scalar(
            select(ProcurementSalesLinkCorrectionModel).where(
                ProcurementSalesLinkCorrectionModel.superseded_link_id == superseded_link_id
            )
        )
        return _correction_to_domain(m) if m else None

    def add_correction_if_uncorrected(self, correction: ProcurementSalesLinkCorrection) -> bool:
        """The ONLY way to write a correction. Succeeds only if no
        correction already names ``correction.superseded_link_id`` —
        evaluated atomically as part of the SAME insert statement, the
        same technique as ``insert_episode_if_no_current``. This is what
        makes ``superseded_link_id`` semantically unique even under
        concurrent competing corrections, independent of the DB-level
        ``UNIQUE`` constraint on that column (which remains as a second,
        ORM-bypass-proof backstop)."""
        corrections = ProcurementSalesLinkCorrectionModel.__table__
        existing = corrections.alias("existing")
        already_corrected = select(existing.c.id).where(existing.c.superseded_link_id == correction.superseded_link_id)
        select_values = select(
            literal(correction.id, type_=corrections.c.id.type),
            literal(correction.superseded_link_id, type_=corrections.c.superseded_link_id.type),
            literal(correction.replacement_link_id, type_=corrections.c.replacement_link_id.type),
            literal(correction.source_fragment_id, type_=corrections.c.source_fragment_id.type),
            literal(correction.confirmation_type, type_=corrections.c.confirmation_type.type),
            literal(correction.created_at, type_=corrections.c.created_at.type),
        ).where(~already_corrected.exists())
        stmt = corrections.insert().from_select(
            [
                "id", "superseded_link_id", "replacement_link_id", "source_fragment_id", "confirmation_type",
                "created_at",
            ],
            select_values,
        ).returning(corrections.c.id)
        # See insert_episode_if_no_current's comment — never trust
        # result.rowcount for INSERT...FROM SELECT.
        inserted = self._session.execute(stmt).fetchone() is not None
        self._session.flush()
        return inserted

    def count(self) -> int:
        return len(self._session.scalars(select(ProcurementSalesLinkModel.id)).all())


class ExceptionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, exception: TaskException) -> None:
        self._session.add(
            TaskExceptionModel(
                id=exception.id,
                exception_type=exception.exception_type,
                status=exception.status,
                summary=exception.summary,
                detail=exception.detail,
                created_at=exception.created_at,
            )
        )

    def list_all(self) -> list[TaskException]:
        rows = self._session.scalars(select(TaskExceptionModel))
        return [_exception_to_domain(m) for m in rows]

    def list_open(self) -> list[TaskException]:
        rows = self._session.scalars(select(TaskExceptionModel).where(TaskExceptionModel.status == "OPEN"))
        return [_exception_to_domain(m) for m in rows]

    def update_status(self, exception_id: uuid.UUID, status: str) -> None:
        """Minimal status transition (e.g. OPEN -> RESOLVED), mirroring
        AccrualRepository.update_status / MatchCaseRepository.update_status
        — not a workflow engine, just the existing TaskException.status
        field the domain model already has. Used by
        bel.application.sales_contract_facts to close an
        unresolved-customer Task once a SUPPLEMENT fills in the customer
        (docs/V1-SCOPE.md section 5.2's closed loop: "Task resolves")."""
        m = self._session.get(TaskExceptionModel, exception_id)
        if m is None:
            raise KeyError(f"TaskException {exception_id} not found")
        m.status = status


class EventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, event: BusinessEvent) -> None:
        self._session.add(
            BusinessEventModel(
                id=event.id,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                payload=event.payload,
            )
        )


class ImportRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        run_id: uuid.UUID,
        evidence_document_id: uuid.UUID,
        file_name: str,
        sha256: str,
        started_at: datetime,
        completed_at: datetime,
        is_reimport: bool,
        contracts_created_count: int,
        contract_items_created_count: int,
        business_key_conflicts_detected_count: int,
    ) -> None:
        self._session.add(
            ImportRunModel(
                id=run_id,
                evidence_document_id=evidence_document_id,
                file_name=file_name,
                sha256=sha256,
                started_at=started_at,
                completed_at=completed_at,
                is_reimport=is_reimport,
                contracts_created_count=contracts_created_count,
                contract_items_created_count=contract_items_created_count,
                business_key_conflicts_detected_count=business_key_conflicts_detected_count,
            )
        )


class InvoiceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, invoice: Invoice) -> None:
        self._session.add(
            InvoiceModel(
                id=invoice.id,
                direction=invoice.direction,
                invoice_type=invoice.invoice_type,
                invoice_no=invoice.invoice_no,
                digital_invoice_no=invoice.digital_invoice_no,
                external_invoice_key=invoice.external_invoice_key,
                issue_date=invoice.issue_date,
                seller=invoice.seller,
                buyer=invoice.buyer,
                net_amount=invoice.net_amount,
                tax_amount=invoice.tax_amount,
                gross_amount=invoice.gross_amount,
                invoice_status=invoice.invoice_status,
                source_fragment_id=invoice.source_fragment_id,
                created_at=invoice.created_at,
                updated_at=invoice.updated_at,
                currency=invoice.currency,
            )
        )

    def get(self, invoice_id: uuid.UUID) -> Invoice | None:
        m = self._session.get(InvoiceModel, invoice_id)
        return _invoice_to_domain(m) if m else None

    def find_by_external_key(self, external_invoice_key: str) -> Invoice | None:
        m = self._session.scalar(select(InvoiceModel).where(InvoiceModel.external_invoice_key == external_invoice_key))
        return _invoice_to_domain(m) if m else None

    def list_all(self) -> list[Invoice]:
        rows = self._session.scalars(select(InvoiceModel))
        return [_invoice_to_domain(m) for m in rows]

    def count(self) -> int:
        return len(self._session.scalars(select(InvoiceModel.id)).all())


class InvoiceItemRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, item: InvoiceItem) -> None:
        self._session.add(
            InvoiceItemModel(
                id=item.id,
                invoice_id=item.invoice_id,
                line_no=item.line_no,
                product_name=item.product_name,
                specification=item.specification,
                unit=item.unit,
                quantity=item.quantity,
                unit_price=item.unit_price,
                net_amount=item.net_amount,
                tax_rate=item.tax_rate,
                tax_amount=item.tax_amount,
                gross_amount=item.gross_amount,
                source_fragment_id=item.source_fragment_id,
            )
        )

    def get(self, item_id: uuid.UUID) -> InvoiceItem | None:
        m = self._session.get(InvoiceItemModel, item_id)
        return _invoice_item_to_domain(m) if m else None

    def list_for_invoice(self, invoice_id: uuid.UUID) -> list[InvoiceItem]:
        rows = self._session.scalars(select(InvoiceItemModel).where(InvoiceItemModel.invoice_id == invoice_id))
        return [_invoice_item_to_domain(m) for m in rows]

    def list_all(self) -> list[InvoiceItem]:
        rows = self._session.scalars(select(InvoiceItemModel))
        return [_invoice_item_to_domain(m) for m in rows]

    def count(self) -> int:
        return len(self._session.scalars(select(InvoiceItemModel.id)).all())


class PaymentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, payment: Payment) -> None:
        self._session.add(
            PaymentModel(
                id=payment.id,
                transaction_date=payment.transaction_date,
                direction=payment.direction,
                amount=payment.amount,
                counterparty=payment.counterparty,
                business_type=payment.business_type,
                bank_reference=payment.bank_reference,
                description=payment.description,
                running_balance=payment.running_balance,
                source_fragment_id=payment.source_fragment_id,
                created_at=payment.created_at,
                source_account_id=payment.source_account_id,
            )
        )

    def get(self, payment_id: uuid.UUID) -> Payment | None:
        m = self._session.get(PaymentModel, payment_id)
        return _payment_to_domain(m) if m else None

    def list_all(self) -> list[Payment]:
        rows = self._session.scalars(select(PaymentModel))
        return [_payment_to_domain(m) for m in rows]

    def find_by_identity(
        self,
        *,
        source_account_id: str,
        transaction_date,
        direction: str,
        amount: Decimal,
        bank_reference: str,
    ) -> list[Payment]:
        """Phase 2D.1-R5's robust business identity
        (docs/PHASE2D1-R0-DECISIONS.md section 4.4): the composite that
        actually separates two genuinely different transactions on
        different accounts. Returns a list — not assumed unique at the
        DB level, mirroring Contract's identity lookup; the caller (R5
        backfill) treats more than one match as ambiguous, never a
        first() guess."""
        rows = self._session.scalars(
            select(PaymentModel).where(
                PaymentModel.source_account_id == source_account_id,
                PaymentModel.transaction_date == transaction_date,
                PaymentModel.direction == direction,
                PaymentModel.amount == amount,
                PaymentModel.bank_reference == bank_reference,
            )
        )
        return [_payment_to_domain(m) for m in rows]

    def count(self) -> int:
        return len(self._session.scalars(select(PaymentModel.id)).all())


class MatchCaseRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, match_case: MatchCase) -> None:
        self._session.add(
            MatchCaseModel(
                id=match_case.id,
                subject_type=match_case.subject_type,
                subject_id=match_case.subject_id,
                status=match_case.status,
                match_method=match_case.match_method,
                created_at=match_case.created_at,
                resolved_at=match_case.resolved_at,
            )
        )

    def get(self, match_case_id: uuid.UUID) -> MatchCase | None:
        m = self._session.get(MatchCaseModel, match_case_id)
        return _match_case_to_domain(m) if m else None

    def find_by_subject(self, subject_type: str, subject_id: uuid.UUID) -> MatchCase | None:
        m = self._session.scalar(
            select(MatchCaseModel).where(
                MatchCaseModel.subject_type == subject_type, MatchCaseModel.subject_id == subject_id
            )
        )
        return _match_case_to_domain(m) if m else None

    def update_status(self, match_case_id: uuid.UUID, status: str, resolved_at: datetime | None) -> None:
        m = self._session.get(MatchCaseModel, match_case_id)
        if m is None:
            raise KeyError(f"MatchCase {match_case_id} not found")
        m.status = status
        m.resolved_at = resolved_at

    def add_if_no_case_for_subject(self, match_case: MatchCase) -> bool:
        """Phase 2D.1-R3b: the atomic counterpart to `add()` — a single
        `INSERT ... SELECT ... WHERE NOT EXISTS (...)` statement, never a
        separate `find_by_subject` check followed by `add()`, which two
        concurrent sessions proposing a match for the SAME subject could
        both pass. M001's own `_run_match_pass` still uses the older
        check-then-`add()` pattern (unchanged, out of this round's
        scope — it is a single-writer batch pass with no concurrent
        caller, and — see `MatchCaseModel`'s docstring — the procurement
        leg legitimately allows more than one `MatchCase` per subject
        across different Contracts, so a `(subject_type, subject_id)` DB
        constraint is deliberately NOT added here). This method exists
        for the sales leg's newly-concurrent manual proposal path
        (`bel.application.sales_matching`), where exactly one `MatchCase`
        per subject IS the intended design (multi-target allocation
        happens via several `SalesInvoiceAllocation`/`SalesPaymentAllocation`
        rows under that ONE case, never via several cases). Returns
        `False` — having written nothing — if a MatchCase for this
        subject already exists."""
        table = MatchCaseModel.__table__
        existing = table.alias("existing")
        blocking = select(existing.c.id).where(
            existing.c.subject_type == match_case.subject_type, existing.c.subject_id == match_case.subject_id
        )
        select_values = select(
            literal(match_case.id, type_=table.c.id.type),
            literal(match_case.subject_type, type_=table.c.subject_type.type),
            literal(match_case.subject_id, type_=table.c.subject_id.type),
            literal(match_case.status, type_=table.c.status.type),
            literal(match_case.match_method, type_=table.c.match_method.type),
            literal(match_case.created_at, type_=table.c.created_at.type),
            literal(match_case.resolved_at, type_=table.c.resolved_at.type),
        ).where(~blocking.exists())
        stmt = table.insert().from_select(
            ["id", "subject_type", "subject_id", "status", "match_method", "created_at", "resolved_at"],
            select_values,
        ).returning(table.c.id)
        # Never trust result.rowcount for INSERT...FROM SELECT — see
        # ProcurementSalesLinkRepository.insert_episode_if_no_current's
        # comment for why.
        inserted = self._session.execute(stmt).fetchone() is not None
        self._session.flush()
        return inserted

    def resolve_if_pending(self, match_case_id: uuid.UUID, *, resolved_at: datetime) -> bool:
        """Phase 2D.1-R3b: the atomic conditional counterpart to
        `update_status()` — a single `UPDATE ... WHERE status =
        'HUMAN_CONFIRMATION_REQUIRED'` (never a separate read-then-write),
        so two sessions concurrently confirming the SAME MatchCase can
        never both succeed. Returns `False` — having written nothing —
        if the case was not `HUMAN_CONFIRMATION_REQUIRED` at the moment
        this statement executed (already resolved by a concurrent
        confirmation, or never eligible in the first place)."""
        result = self._session.execute(
            update(MatchCaseModel)
            .where(
                MatchCaseModel.id == match_case_id,
                MatchCaseModel.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
            )
            .values(status=MatchCaseStatus.RESOLVED, resolved_at=resolved_at)
        )
        self._session.flush()
        return result.rowcount == 1

    def list_all(self) -> list[MatchCase]:
        rows = self._session.scalars(select(MatchCaseModel))
        return [_match_case_to_domain(m) for m in rows]

    def list_by_status(self, status: str) -> list[MatchCase]:
        rows = self._session.scalars(select(MatchCaseModel).where(MatchCaseModel.status == status))
        return [_match_case_to_domain(m) for m in rows]


class MatchCandidateRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, candidate: MatchCandidate) -> None:
        self._session.add(
            MatchCandidateModel(
                id=candidate.id,
                match_case_id=candidate.match_case_id,
                contract_id=candidate.contract_id,
                created_at=candidate.created_at,
            )
        )

    def list_for_case(self, match_case_id: uuid.UUID) -> list[MatchCandidate]:
        rows = self._session.scalars(select(MatchCandidateModel).where(MatchCandidateModel.match_case_id == match_case_id))
        return [_match_candidate_to_domain(m) for m in rows]


def _sales_match_candidate_to_domain(m: SalesMatchCandidateModel) -> SalesMatchCandidate:
    return SalesMatchCandidate(
        id=m.id, match_case_id=m.match_case_id, sales_contract_id=m.sales_contract_id, created_at=m.created_at
    )


class SalesMatchCandidateRepository:
    """The sales-side twin of `MatchCandidateRepository` (Phase 2D.1-R3b,
    docs/PHASE2D1-R0-DECISIONS.md section 2.7) — never a generalisation
    of it. `uq_sales_match_candidates_case_target` (declared on the
    model) makes proposing the SAME SalesContract twice for one case a
    harmless no-op at the DB level; `add_if_new` below is the
    application-facing idempotent wrapper."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, candidate: SalesMatchCandidate) -> None:
        self._session.add(
            SalesMatchCandidateModel(
                id=candidate.id,
                match_case_id=candidate.match_case_id,
                sales_contract_id=candidate.sales_contract_id,
                created_at=candidate.created_at,
            )
        )

    def list_for_case(self, match_case_id: uuid.UUID) -> list[SalesMatchCandidate]:
        rows = self._session.scalars(
            select(SalesMatchCandidateModel).where(SalesMatchCandidateModel.match_case_id == match_case_id)
        )
        return [_sales_match_candidate_to_domain(m) for m in rows]


class InvoiceAllocationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, allocation: InvoiceAllocation) -> None:
        self._session.add(
            InvoiceAllocationModel(
                id=allocation.id,
                invoice_id=allocation.invoice_id,
                contract_id=allocation.contract_id,
                match_case_id=allocation.match_case_id,
                allocated_gross_amount=allocation.allocated_gross_amount,
                match_method=allocation.match_method,
                confirmation_type=allocation.confirmation_type,
                created_at=allocation.created_at,
            )
        )

    def list_for_contract(self, contract_id: uuid.UUID) -> list[InvoiceAllocation]:
        rows = self._session.scalars(select(InvoiceAllocationModel).where(InvoiceAllocationModel.contract_id == contract_id))
        return [_invoice_allocation_to_domain(m) for m in rows]

    def list_all(self) -> list[InvoiceAllocation]:
        rows = self._session.scalars(select(InvoiceAllocationModel))
        return [_invoice_allocation_to_domain(m) for m in rows]

    def sum_confirmed_for_contract(self, contract_id: uuid.UUID) -> Decimal:
        rows = self.list_for_contract(contract_id)
        return sum((a.allocated_gross_amount for a in rows), Decimal("0"))


class PaymentAllocationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, allocation: PaymentAllocation) -> None:
        self._session.add(
            PaymentAllocationModel(
                id=allocation.id,
                payment_id=allocation.payment_id,
                contract_id=allocation.contract_id,
                match_case_id=allocation.match_case_id,
                allocated_amount=allocation.allocated_amount,
                match_method=allocation.match_method,
                confirmation_type=allocation.confirmation_type,
                created_at=allocation.created_at,
            )
        )

    def list_for_contract(self, contract_id: uuid.UUID) -> list[PaymentAllocation]:
        rows = self._session.scalars(select(PaymentAllocationModel).where(PaymentAllocationModel.contract_id == contract_id))
        return [_payment_allocation_to_domain(m) for m in rows]

    def sum_confirmed_for_contract(self, contract_id: uuid.UUID) -> Decimal:
        rows = self.list_for_contract(contract_id)
        return sum((a.allocated_amount for a in rows), Decimal("0"))


def _sales_invoice_allocation_to_domain(m: SalesInvoiceAllocationModel) -> SalesInvoiceAllocation:
    return SalesInvoiceAllocation(
        id=m.id,
        invoice_id=m.invoice_id,
        sales_contract_id=m.sales_contract_id,
        match_case_id=m.match_case_id,
        allocated_gross_amount=m.allocated_gross_amount,
        confirmation_type=m.confirmation_type,
        created_at=m.created_at,
    )


class MatchCaseNotPendingError(ValueError):
    """Raised by Sales*AllocationRepository.add() ONLY for the
    MatchCase-status check — distinguished from every other ValueError
    this method raises so a caller (specifically
    bel.application.sales_matching) can treat ONLY this as "a concurrent
    session got here first" and translate it to a retryable conflict,
    never silently reclassifying a genuine bug (wrong direction, bad
    amount, capacity exceeded) as mere bad timing."""


class SalesInvoiceAllocationRepository:
    """The sales-side twin of `InvoiceAllocationRepository` (Phase
    2D.1-R3b, docs/PHASE2D1-R0-DECISIONS.md section 2.7) — never a
    generalisation of it. No `list_for_contract` exists here on purpose:
    this table has no `contract_id` at all.

    `add()` is the ONLY write primitive and IS the authoritative
    boundary — Gate 2D.1-R3b fix round, BLOCKER 1 (round 1) and BLOCKER 1
    (round 2): the application layer's checks (direction, MatchCase
    status, amount, subject-level capacity) do not close this off, since
    this repository is itself public and callable directly, bypassing
    `bel.application.sales_matching` entirely. A caller who did that
    could otherwise write a PURCHASE invoice, a non-existent/mismatched/
    already-resolved MatchCase, a negative/oversized amount, or an
    allocation that pushes the subject's confirmed total past its own
    amount, straight past every application-level guard. So the SAME
    checks are enforced here, not only there — including the capacity
    check, computed fresh against whatever is already committed (or
    already `add()`-ed earlier in the SAME transaction — SQLAlchemy
    autoflushes pending inserts before a `SELECT`, so sequential
    multi-target `add()` calls within one confirmation correctly
    accumulate)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, allocation: SalesInvoiceAllocation) -> None:
        invoice_row = self._session.get(InvoiceModel, allocation.invoice_id)
        if invoice_row is None:
            raise ValueError(f"Invoice {allocation.invoice_id} not found")
        if invoice_row.direction != InvoiceDirection.SALES:
            raise ValueError(
                f"Invoice {allocation.invoice_id} has direction {invoice_row.direction!r} — "
                "SalesInvoiceAllocation requires a SALES invoice"
            )
        match_case_row = self._session.get(MatchCaseModel, allocation.match_case_id)
        if match_case_row is None:
            raise ValueError(f"MatchCase {allocation.match_case_id} not found")
        if match_case_row.subject_type != SubjectType.INVOICE or match_case_row.subject_id != allocation.invoice_id:
            raise ValueError(
                f"MatchCase {allocation.match_case_id} does not correspond to Invoice {allocation.invoice_id}"
            )
        if match_case_row.status != MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED:
            # Non-authoritative fast-fail only — see the atomic INSERT's
            # own WHERE clause below, which is what actually enforces
            # this against a stale identity-map read or a cross-session
            # race (Gate 2D.1-R3b fix round #5, BLOCKER).
            raise MatchCaseNotPendingError(
                f"MatchCase {allocation.match_case_id} is {match_case_row.status}, not "
                "HUMAN_CONFIRMATION_REQUIRED — an allocation can only be written while its case is pending"
            )
        if allocation.confirmation_type != ConfirmationType.HUMAN_CONFIRMED:
            raise ValueError("SalesInvoiceAllocation.confirmation_type must be HUMAN_CONFIRMED")
        validate_storable_amount(allocation.allocated_gross_amount)
        # Gate 2D.1-R3b fix round #5, BLOCKER: round #4 made the CAPACITY
        # check atomic, but the MatchCase HUMAN_CONFIRMATION_REQUIRED
        # check above was still a plain `session.get()` read-then-write —
        # vulnerable both to a stale identity-map entry
        # (sessionmaker(expire_on_commit=False) can leave an old status
        # cached) and to a genuine cross-session race (another session
        # resolves the SAME case between this read and the write below).
        # Fixed by folding MatchCase eligibility (id, subject_type,
        # subject_id, status) into the SAME atomic INSERT ... SELECT ...
        # WHERE as the capacity check, via an EXISTS subquery — so BOTH
        # conditions are evaluated by SQLite as part of ONE statement,
        # against whatever is genuinely current at execution time, never
        # against anything read moments earlier by this session.
        self._session.flush()  # same-session/no_autoflush safety, as before — now a defence layer, not the guarantee
        table = SalesInvoiceAllocationModel.__table__
        match_case_table = MatchCaseModel.__table__
        amount_type = table.c.allocated_gross_amount.type
        current_sum = func.coalesce(func.sum(table.c.allocated_gross_amount), literal(Decimal("0"), type_=amount_type))
        current_sum_subquery = select(current_sum).where(table.c.invoice_id == allocation.invoice_id).scalar_subquery()
        new_amount = literal(allocation.allocated_gross_amount, type_=amount_type)
        gross_amount = literal(invoice_row.gross_amount, type_=amount_type)
        capacity_ok = (current_sum_subquery + new_amount) <= gross_amount
        match_case_eligible = (
            select(literal(1))
            .where(
                match_case_table.c.id == allocation.match_case_id,
                match_case_table.c.subject_type == SubjectType.INVOICE,
                match_case_table.c.subject_id == allocation.invoice_id,
                match_case_table.c.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
            )
            .exists()
        )
        select_values = select(
            literal(allocation.id, type_=table.c.id.type),
            literal(allocation.invoice_id, type_=table.c.invoice_id.type),
            literal(allocation.sales_contract_id, type_=table.c.sales_contract_id.type),
            literal(allocation.match_case_id, type_=table.c.match_case_id.type),
            new_amount,
            literal(allocation.confirmation_type, type_=table.c.confirmation_type.type),
            literal(allocation.created_at, type_=table.c.created_at.type),
        ).where(match_case_eligible, capacity_ok)
        stmt = table.insert().from_select(
            ["id", "invoice_id", "sales_contract_id", "match_case_id", "allocated_gross_amount", "confirmation_type", "created_at"],
            select_values,
        ).returning(table.c.id)
        # Never trust result.rowcount for INSERT...FROM SELECT — see
        # ProcurementSalesLinkRepository.insert_episode_if_no_current's
        # comment for why.
        inserted = self._session.execute(stmt).fetchone() is not None
        if not inserted:
            # Either a genuine cross-session race was lost, or the
            # pre-existing state already made this ineligible — diagnose
            # with a FRESH, identity-map-bypassing column read (never the
            # possibly-stale `match_case_row` loaded above) so the error
            # message is never wrong about which condition actually
            # failed. The atomic statement above is what actually
            # enforced the invariant; this is purely diagnostic.
            fresh_status = self._session.execute(
                select(match_case_table.c.status).where(match_case_table.c.id == allocation.match_case_id)
            ).scalar_one_or_none()
            if fresh_status != MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED:
                raise MatchCaseNotPendingError(
                    f"MatchCase {allocation.match_case_id} is {fresh_status}, not "
                    "HUMAN_CONFIRMATION_REQUIRED — an allocation can only be written while its case is pending"
                )
            already_allocated = self.sum_for_invoice(allocation.invoice_id)
            raise ValueError(
                f"allocations for Invoice {allocation.invoice_id} totalling "
                f"{already_allocated + allocation.allocated_gross_amount} would exceed its gross_amount "
                f"{invoice_row.gross_amount}"
            )
        self._session.flush()

    def list_for_invoice(self, invoice_id: uuid.UUID) -> list[SalesInvoiceAllocation]:
        rows = self._session.scalars(
            select(SalesInvoiceAllocationModel).where(SalesInvoiceAllocationModel.invoice_id == invoice_id)
        )
        return [_sales_invoice_allocation_to_domain(m) for m in rows]

    def list_for_sales_contract(self, sales_contract_id: uuid.UUID) -> list[SalesInvoiceAllocation]:
        rows = self._session.scalars(
            select(SalesInvoiceAllocationModel).where(
                SalesInvoiceAllocationModel.sales_contract_id == sales_contract_id
            )
        )
        return [_sales_invoice_allocation_to_domain(m) for m in rows]

    def sum_for_invoice(self, invoice_id: uuid.UUID) -> Decimal:
        return sum((a.allocated_gross_amount for a in self.list_for_invoice(invoice_id)), Decimal("0"))


def _sales_payment_allocation_to_domain(m: SalesPaymentAllocationModel) -> SalesPaymentAllocation:
    return SalesPaymentAllocation(
        id=m.id,
        payment_id=m.payment_id,
        sales_contract_id=m.sales_contract_id,
        match_case_id=m.match_case_id,
        allocated_amount=m.allocated_amount,
        confirmation_type=m.confirmation_type,
        created_at=m.created_at,
    )


class SalesPaymentAllocationRepository:
    """The sales-side twin of `PaymentAllocationRepository` (Phase
    2D.1-R3b, docs/PHASE2D1-R0-DECISIONS.md section 2.7). No
    `contract_id` on this table.

    `add()` is the ONLY write primitive and IS the authoritative
    boundary — see `SalesInvoiceAllocationRepository.add`'s docstring
    for why the same checks belong here too, not only in the
    application layer."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, allocation: SalesPaymentAllocation) -> None:
        payment_row = self._session.get(PaymentModel, allocation.payment_id)
        if payment_row is None:
            raise ValueError(f"Payment {allocation.payment_id} not found")
        if payment_row.direction != PaymentDirection.IN:
            raise ValueError(
                f"Payment {allocation.payment_id} has direction {payment_row.direction!r} — "
                "SalesPaymentAllocation requires an IN payment"
            )
        match_case_row = self._session.get(MatchCaseModel, allocation.match_case_id)
        if match_case_row is None:
            raise ValueError(f"MatchCase {allocation.match_case_id} not found")
        if match_case_row.subject_type != SubjectType.PAYMENT or match_case_row.subject_id != allocation.payment_id:
            raise ValueError(
                f"MatchCase {allocation.match_case_id} does not correspond to Payment {allocation.payment_id}"
            )
        if match_case_row.status != MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED:
            # Non-authoritative fast-fail only — see the atomic INSERT's
            # own WHERE clause below.
            raise MatchCaseNotPendingError(
                f"MatchCase {allocation.match_case_id} is {match_case_row.status}, not "
                "HUMAN_CONFIRMATION_REQUIRED — an allocation can only be written while its case is pending"
            )
        if allocation.confirmation_type != ConfirmationType.HUMAN_CONFIRMED:
            raise ValueError("SalesPaymentAllocation.confirmation_type must be HUMAN_CONFIRMED")
        validate_storable_amount(allocation.allocated_amount)
        # Gate 2D.1-R3b fix round #5, BLOCKER: see the identical
        # MatchCase-eligibility-folded-into-the-atomic-INSERT note in
        # SalesInvoiceAllocationRepository.add — a read-then-write status
        # check is vulnerable to both a stale identity-map entry and a
        # genuine cross-session race.
        self._session.flush()  # same-session/no_autoflush safety, as before — now a defence layer, not the guarantee
        table = SalesPaymentAllocationModel.__table__
        match_case_table = MatchCaseModel.__table__
        amount_type = table.c.allocated_amount.type
        current_sum = func.coalesce(func.sum(table.c.allocated_amount), literal(Decimal("0"), type_=amount_type))
        current_sum_subquery = select(current_sum).where(table.c.payment_id == allocation.payment_id).scalar_subquery()
        new_amount = literal(allocation.allocated_amount, type_=amount_type)
        payment_amount = literal(payment_row.amount, type_=amount_type)
        capacity_ok = (current_sum_subquery + new_amount) <= payment_amount
        match_case_eligible = (
            select(literal(1))
            .where(
                match_case_table.c.id == allocation.match_case_id,
                match_case_table.c.subject_type == SubjectType.PAYMENT,
                match_case_table.c.subject_id == allocation.payment_id,
                match_case_table.c.status == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
            )
            .exists()
        )
        select_values = select(
            literal(allocation.id, type_=table.c.id.type),
            literal(allocation.payment_id, type_=table.c.payment_id.type),
            literal(allocation.sales_contract_id, type_=table.c.sales_contract_id.type),
            literal(allocation.match_case_id, type_=table.c.match_case_id.type),
            new_amount,
            literal(allocation.confirmation_type, type_=table.c.confirmation_type.type),
            literal(allocation.created_at, type_=table.c.created_at.type),
        ).where(match_case_eligible, capacity_ok)
        stmt = table.insert().from_select(
            ["id", "payment_id", "sales_contract_id", "match_case_id", "allocated_amount", "confirmation_type", "created_at"],
            select_values,
        ).returning(table.c.id)
        # Never trust result.rowcount for INSERT...FROM SELECT — see
        # ProcurementSalesLinkRepository.insert_episode_if_no_current's
        # comment for why.
        inserted = self._session.execute(stmt).fetchone() is not None
        if not inserted:
            # Diagnose with a FRESH, identity-map-bypassing column read —
            # see the identical note in SalesInvoiceAllocationRepository.add.
            fresh_status = self._session.execute(
                select(match_case_table.c.status).where(match_case_table.c.id == allocation.match_case_id)
            ).scalar_one_or_none()
            if fresh_status != MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED:
                raise MatchCaseNotPendingError(
                    f"MatchCase {allocation.match_case_id} is {fresh_status}, not "
                    "HUMAN_CONFIRMATION_REQUIRED — an allocation can only be written while its case is pending"
                )
            already_allocated = self.sum_for_payment(allocation.payment_id)
            raise ValueError(
                f"allocations for Payment {allocation.payment_id} totalling "
                f"{already_allocated + allocation.allocated_amount} would exceed its amount {payment_row.amount}"
            )
        self._session.flush()

    def list_for_payment(self, payment_id: uuid.UUID) -> list[SalesPaymentAllocation]:
        rows = self._session.scalars(
            select(SalesPaymentAllocationModel).where(SalesPaymentAllocationModel.payment_id == payment_id)
        )
        return [_sales_payment_allocation_to_domain(m) for m in rows]

    def list_for_sales_contract(self, sales_contract_id: uuid.UUID) -> list[SalesPaymentAllocation]:
        rows = self._session.scalars(
            select(SalesPaymentAllocationModel).where(
                SalesPaymentAllocationModel.sales_contract_id == sales_contract_id
            )
        )
        return [_sales_payment_allocation_to_domain(m) for m in rows]

    def sum_for_payment(self, payment_id: uuid.UUID) -> Decimal:
        return sum((a.allocated_amount for a in self.list_for_payment(payment_id)), Decimal("0"))


def _invoice_item_allocation_to_domain(m: InvoiceItemAllocationModel) -> InvoiceItemAllocation:
    return InvoiceItemAllocation(
        id=m.id,
        invoice_item_id=m.invoice_item_id,
        contract_item_id=m.contract_item_id,
        allocated_quantity=m.allocated_quantity,
        allocated_net_amount=m.allocated_net_amount,
        confirmation_type=m.confirmation_type,
        source_fragment_id=m.source_fragment_id,
        created_at=m.created_at,
        superseded_by_fact_id=m.superseded_by_fact_id,
    )


def _cost_recognition_fact_to_domain(m: CostRecognitionFactModel) -> CostRecognitionFact:
    return CostRecognitionFact(
        id=m.id,
        contract_id=m.contract_id,
        recognition_date=m.recognition_date,
        basis=m.basis,
        source_fragment_id=m.source_fragment_id,
        created_at=m.created_at,
        shipment_id=m.shipment_id,
        superseded_by_fact_id=m.superseded_by_fact_id,
    )


def _accrual_basis_fact_to_domain(m: AccrualBasisFactModel) -> AccrualBasisFact:
    return AccrualBasisFact(
        id=m.id,
        scope_type=m.scope_type,
        contract_id=m.contract_id,
        contract_item_id=m.contract_item_id,
        quantity=m.quantity,
        estimated_cost=m.estimated_cost,
        basis=m.basis,
        source_fragment_id=m.source_fragment_id,
        created_at=m.created_at,
        superseded_by_fact_id=m.superseded_by_fact_id,
    )


def _historical_accrual_fact_to_domain(m: HistoricalAccrualFactModel) -> HistoricalAccrualFact:
    return HistoricalAccrualFact(
        id=m.id,
        source_period=m.source_period,
        contract_item_id=m.contract_item_id,
        quantity=m.quantity,
        estimated_cost=m.estimated_cost,
        basis=m.basis,
        source_fragment_id=m.source_fragment_id,
        confirmed_at=m.confirmed_at,
        superseded_by_fact_id=m.superseded_by_fact_id,
    )


def _accrual_to_domain(m: AccrualModel) -> Accrual:
    return Accrual(
        id=m.id,
        period=m.period,
        contract_item_id=m.contract_item_id,
        quantity=m.quantity,
        estimated_cost=m.estimated_cost,
        basis=m.basis,
        status=m.status,
        created_from_fact_id=m.created_from_fact_id,
        created_at=m.created_at,
    )


def _accrual_reversal_to_domain(m: AccrualReversalModel) -> AccrualReversal:
    return AccrualReversal(
        id=m.id,
        accrual_id=m.accrual_id,
        period=m.period,
        invoice_item_allocation_id=m.invoice_item_allocation_id,
        reversed_quantity=m.reversed_quantity,
        reversed_estimated_cost=m.reversed_estimated_cost,
        created_at=m.created_at,
    )


class InvoiceItemAllocationRepository:
    """docs/PHASE2D1-R0-DECISIONS.md section 21 (Phase 2D.1-R5 pre-flight
    debt): whole-fact supersession. Every "current" read method excludes
    a row whose ``superseded_by_fact_id`` is set — for today's data,
    where nothing has ever been superseded, this filters nothing and
    changes no existing behaviour. ``mark_superseded`` is the ONLY write
    path that sets it, via a single atomic conditional UPDATE (the same
    CAS shape as ContractItemRepository.append_revision_against_current)
    so two concurrent supersession attempts against the same fact can
    never both succeed."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, allocation: InvoiceItemAllocation) -> None:
        self._session.add(
            InvoiceItemAllocationModel(
                id=allocation.id,
                invoice_item_id=allocation.invoice_item_id,
                contract_item_id=allocation.contract_item_id,
                allocated_quantity=allocation.allocated_quantity,
                allocated_net_amount=allocation.allocated_net_amount,
                confirmation_type=allocation.confirmation_type,
                source_fragment_id=allocation.source_fragment_id,
                created_at=allocation.created_at,
            )
        )

    def get(self, allocation_id: uuid.UUID) -> InvoiceItemAllocation | None:
        m = self._session.get(InvoiceItemAllocationModel, allocation_id)
        return _invoice_item_allocation_to_domain(m) if m else None

    def list_for_contract_item(self, contract_item_id: uuid.UUID) -> list[InvoiceItemAllocation]:
        rows = self._session.scalars(
            select(InvoiceItemAllocationModel).where(
                InvoiceItemAllocationModel.contract_item_id == contract_item_id,
                InvoiceItemAllocationModel.superseded_by_fact_id.is_(None),
            )
        )
        return [_invoice_item_allocation_to_domain(m) for m in rows]

    def list_for_invoice_item(self, invoice_item_id: uuid.UUID) -> list[InvoiceItemAllocation]:
        rows = self._session.scalars(
            select(InvoiceItemAllocationModel).where(
                InvoiceItemAllocationModel.invoice_item_id == invoice_item_id,
                InvoiceItemAllocationModel.superseded_by_fact_id.is_(None),
            )
        )
        return [_invoice_item_allocation_to_domain(m) for m in rows]

    def list_all(self) -> list[InvoiceItemAllocation]:
        rows = self._session.scalars(
            select(InvoiceItemAllocationModel).where(InvoiceItemAllocationModel.superseded_by_fact_id.is_(None))
        )
        return [_invoice_item_allocation_to_domain(m) for m in rows]

    def list_all_including_superseded(self) -> list[InvoiceItemAllocation]:
        """Full audit view — used by reconciliation/history tooling, never
        by the Rule Engine."""
        rows = self._session.scalars(select(InvoiceItemAllocationModel))
        return [_invoice_item_allocation_to_domain(m) for m in rows]

    def find(self, invoice_item_id: uuid.UUID, contract_item_id: uuid.UUID) -> InvoiceItemAllocation | None:
        m = self._session.scalar(
            select(InvoiceItemAllocationModel).where(
                InvoiceItemAllocationModel.invoice_item_id == invoice_item_id,
                InvoiceItemAllocationModel.contract_item_id == contract_item_id,
                InvoiceItemAllocationModel.superseded_by_fact_id.is_(None),
            )
        )
        return _invoice_item_allocation_to_domain(m) if m else None

    def mark_superseded(self, fact_id: uuid.UUID, *, superseded_by_fact_id: uuid.UUID) -> bool:
        result = self._session.execute(
            update(InvoiceItemAllocationModel)
            .where(
                InvoiceItemAllocationModel.id == fact_id,
                InvoiceItemAllocationModel.superseded_by_fact_id.is_(None),
            )
            .values(superseded_by_fact_id=superseded_by_fact_id)
        )
        return result.rowcount == 1

    def sum_allocated_quantity_for_invoice_item(self, invoice_item_id: uuid.UUID) -> Decimal:
        rows = self.list_for_invoice_item(invoice_item_id)
        return sum((a.allocated_quantity for a in rows), Decimal("0"))

    def count(self) -> int:
        return len(self._session.scalars(select(InvoiceItemAllocationModel.id)).all())


class CostRecognitionFactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, fact: CostRecognitionFact) -> None:
        self._session.add(
            CostRecognitionFactModel(
                id=fact.id,
                contract_id=fact.contract_id,
                recognition_date=fact.recognition_date,
                basis=fact.basis,
                source_fragment_id=fact.source_fragment_id,
                created_at=fact.created_at,
                shipment_id=fact.shipment_id,
            )
        )

    def get(self, fact_id: uuid.UUID) -> CostRecognitionFact | None:
        m = self._session.get(CostRecognitionFactModel, fact_id)
        return _cost_recognition_fact_to_domain(m) if m else None

    def list_for_shipment(self, shipment_id: uuid.UUID) -> list[CostRecognitionFact]:
        rows = self._session.scalars(
            select(CostRecognitionFactModel).where(
                CostRecognitionFactModel.shipment_id == shipment_id,
                CostRecognitionFactModel.superseded_by_fact_id.is_(None),
            )
        )
        return [_cost_recognition_fact_to_domain(m) for m in rows]

    def list_all(self) -> list[CostRecognitionFact]:
        rows = self._session.scalars(
            select(CostRecognitionFactModel).where(CostRecognitionFactModel.superseded_by_fact_id.is_(None))
        )
        return [_cost_recognition_fact_to_domain(m) for m in rows]

    def list_all_including_superseded(self) -> list[CostRecognitionFact]:
        rows = self._session.scalars(select(CostRecognitionFactModel))
        return [_cost_recognition_fact_to_domain(m) for m in rows]

    def find_duplicate(self, contract_id: uuid.UUID, recognition_date, basis: str) -> CostRecognitionFact | None:
        m = self._session.scalar(
            select(CostRecognitionFactModel).where(
                CostRecognitionFactModel.contract_id == contract_id,
                CostRecognitionFactModel.recognition_date == recognition_date,
                CostRecognitionFactModel.basis == basis,
            )
        )
        return _cost_recognition_fact_to_domain(m) if m else None

    def mark_superseded(self, fact_id: uuid.UUID, *, superseded_by_fact_id: uuid.UUID) -> bool:
        result = self._session.execute(
            update(CostRecognitionFactModel)
            .where(CostRecognitionFactModel.id == fact_id, CostRecognitionFactModel.superseded_by_fact_id.is_(None))
            .values(superseded_by_fact_id=superseded_by_fact_id)
        )
        return result.rowcount == 1

    def count(self) -> int:
        return len(self._session.scalars(select(CostRecognitionFactModel.id)).all())


class AccrualBasisFactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, fact: AccrualBasisFact) -> None:
        self._session.add(
            AccrualBasisFactModel(
                id=fact.id,
                scope_type=fact.scope_type,
                contract_id=fact.contract_id,
                contract_item_id=fact.contract_item_id,
                quantity=fact.quantity,
                estimated_cost=fact.estimated_cost,
                basis=fact.basis,
                source_fragment_id=fact.source_fragment_id,
                created_at=fact.created_at,
            )
        )

    def get(self, fact_id: uuid.UUID) -> AccrualBasisFact | None:
        m = self._session.get(AccrualBasisFactModel, fact_id)
        return _accrual_basis_fact_to_domain(m) if m else None

    def list_all(self) -> list[AccrualBasisFact]:
        rows = self._session.scalars(
            select(AccrualBasisFactModel).where(AccrualBasisFactModel.superseded_by_fact_id.is_(None))
        )
        return [_accrual_basis_fact_to_domain(m) for m in rows]

    def list_all_including_superseded(self) -> list[AccrualBasisFact]:
        rows = self._session.scalars(select(AccrualBasisFactModel))
        return [_accrual_basis_fact_to_domain(m) for m in rows]

    def list_for_contract_item(self, contract_item_id: uuid.UUID) -> list[AccrualBasisFact]:
        rows = self._session.scalars(
            select(AccrualBasisFactModel).where(
                AccrualBasisFactModel.contract_item_id == contract_item_id,
                AccrualBasisFactModel.superseded_by_fact_id.is_(None),
            )
        )
        return [_accrual_basis_fact_to_domain(m) for m in rows]

    def find_duplicate(
        self,
        contract_id: uuid.UUID,
        scope_type: str,
        contract_item_id: uuid.UUID | None,
        estimated_cost: Decimal,
        basis: str,
    ) -> AccrualBasisFact | None:
        query = select(AccrualBasisFactModel).where(
            AccrualBasisFactModel.contract_id == contract_id,
            AccrualBasisFactModel.scope_type == scope_type,
            AccrualBasisFactModel.contract_item_id == contract_item_id,
            AccrualBasisFactModel.estimated_cost == estimated_cost,
            AccrualBasisFactModel.basis == basis,
        )
        m = self._session.scalar(query)
        return _accrual_basis_fact_to_domain(m) if m else None

    def mark_superseded(self, fact_id: uuid.UUID, *, superseded_by_fact_id: uuid.UUID) -> bool:
        result = self._session.execute(
            update(AccrualBasisFactModel)
            .where(AccrualBasisFactModel.id == fact_id, AccrualBasisFactModel.superseded_by_fact_id.is_(None))
            .values(superseded_by_fact_id=superseded_by_fact_id)
        )
        return result.rowcount == 1

    def count(self) -> int:
        return len(self._session.scalars(select(AccrualBasisFactModel.id)).all())


class HistoricalAccrualFactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, fact: HistoricalAccrualFact) -> None:
        self._session.add(
            HistoricalAccrualFactModel(
                id=fact.id,
                source_period=fact.source_period,
                contract_item_id=fact.contract_item_id,
                quantity=fact.quantity,
                estimated_cost=fact.estimated_cost,
                basis=fact.basis,
                source_fragment_id=fact.source_fragment_id,
                confirmed_at=fact.confirmed_at,
            )
        )

    def get(self, fact_id: uuid.UUID) -> HistoricalAccrualFact | None:
        m = self._session.get(HistoricalAccrualFactModel, fact_id)
        return _historical_accrual_fact_to_domain(m) if m else None

    def list_all(self) -> list[HistoricalAccrualFact]:
        rows = self._session.scalars(
            select(HistoricalAccrualFactModel).where(HistoricalAccrualFactModel.superseded_by_fact_id.is_(None))
        )
        return [_historical_accrual_fact_to_domain(m) for m in rows]

    def list_all_including_superseded(self) -> list[HistoricalAccrualFact]:
        rows = self._session.scalars(select(HistoricalAccrualFactModel))
        return [_historical_accrual_fact_to_domain(m) for m in rows]

    def list_for_contract_item(self, contract_item_id: uuid.UUID) -> list[HistoricalAccrualFact]:
        rows = self._session.scalars(
            select(HistoricalAccrualFactModel).where(
                HistoricalAccrualFactModel.contract_item_id == contract_item_id,
                HistoricalAccrualFactModel.superseded_by_fact_id.is_(None),
            )
        )
        return [_historical_accrual_fact_to_domain(m) for m in rows]

    def find_duplicate(
        self,
        contract_item_id: uuid.UUID,
        source_period: str,
        quantity: Decimal,
        estimated_cost: Decimal,
        basis: str,
    ) -> HistoricalAccrualFact | None:
        m = self._session.scalar(
            select(HistoricalAccrualFactModel).where(
                HistoricalAccrualFactModel.contract_item_id == contract_item_id,
                HistoricalAccrualFactModel.source_period == source_period,
                HistoricalAccrualFactModel.quantity == quantity,
                HistoricalAccrualFactModel.estimated_cost == estimated_cost,
                HistoricalAccrualFactModel.basis == basis,
            )
        )
        return _historical_accrual_fact_to_domain(m) if m else None

    def mark_superseded(self, fact_id: uuid.UUID, *, superseded_by_fact_id: uuid.UUID) -> bool:
        result = self._session.execute(
            update(HistoricalAccrualFactModel)
            .where(
                HistoricalAccrualFactModel.id == fact_id, HistoricalAccrualFactModel.superseded_by_fact_id.is_(None)
            )
            .values(superseded_by_fact_id=superseded_by_fact_id)
        )
        return result.rowcount == 1

    def count(self) -> int:
        return len(self._session.scalars(select(HistoricalAccrualFactModel.id)).all())


class AccrualRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, accrual: Accrual) -> None:
        self._session.add(
            AccrualModel(
                id=accrual.id,
                period=accrual.period,
                contract_item_id=accrual.contract_item_id,
                quantity=accrual.quantity,
                estimated_cost=accrual.estimated_cost,
                basis=accrual.basis,
                status=accrual.status,
                created_from_fact_id=accrual.created_from_fact_id,
                created_at=accrual.created_at,
            )
        )

    def get(self, accrual_id: uuid.UUID) -> Accrual | None:
        m = self._session.get(AccrualModel, accrual_id)
        return _accrual_to_domain(m) if m else None

    def find_by_item_and_period(self, contract_item_id: uuid.UUID, period: str) -> Accrual | None:
        m = self._session.scalar(
            select(AccrualModel).where(
                AccrualModel.contract_item_id == contract_item_id, AccrualModel.period == period
            )
        )
        return _accrual_to_domain(m) if m else None

    def list_all(self) -> list[Accrual]:
        rows = self._session.scalars(select(AccrualModel).order_by(AccrualModel.created_at))
        return [_accrual_to_domain(m) for m in rows]

    def list_for_period(self, period: str) -> list[Accrual]:
        rows = self._session.scalars(select(AccrualModel).where(AccrualModel.period == period))
        return [_accrual_to_domain(m) for m in rows]

    def list_for_contract_item(self, contract_item_id: uuid.UUID) -> list[Accrual]:
        rows = self._session.scalars(select(AccrualModel).where(AccrualModel.contract_item_id == contract_item_id))
        return [_accrual_to_domain(m) for m in rows]

    def count(self) -> int:
        return len(self._session.scalars(select(AccrualModel.id)).all())

    def update_status(self, accrual_id: uuid.UUID, status: str) -> None:
        m = self._session.get(AccrualModel, accrual_id)
        if m is None:
            raise KeyError(f"Accrual {accrual_id} not found")
        m.status = status


class AccrualReversalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, reversal: AccrualReversal) -> None:
        self._session.add(
            AccrualReversalModel(
                id=reversal.id,
                accrual_id=reversal.accrual_id,
                period=reversal.period,
                invoice_item_allocation_id=reversal.invoice_item_allocation_id,
                reversed_quantity=reversal.reversed_quantity,
                reversed_estimated_cost=reversal.reversed_estimated_cost,
                created_at=reversal.created_at,
            )
        )

    def list_for_accrual(self, accrual_id: uuid.UUID) -> list[AccrualReversal]:
        rows = self._session.scalars(
            select(AccrualReversalModel).where(AccrualReversalModel.accrual_id == accrual_id)
        )
        return [_accrual_reversal_to_domain(m) for m in rows]

    def find_by_allocation(self, accrual_id: uuid.UUID, invoice_item_allocation_id: uuid.UUID) -> AccrualReversal | None:
        m = self._session.scalar(
            select(AccrualReversalModel).where(
                AccrualReversalModel.accrual_id == accrual_id,
                AccrualReversalModel.invoice_item_allocation_id == invoice_item_allocation_id,
            )
        )
        return _accrual_reversal_to_domain(m) if m else None

    def list_all(self) -> list[AccrualReversal]:
        rows = self._session.scalars(select(AccrualReversalModel))
        return [_accrual_reversal_to_domain(m) for m in rows]

    def count(self) -> int:
        return len(self._session.scalars(select(AccrualReversalModel.id)).all())

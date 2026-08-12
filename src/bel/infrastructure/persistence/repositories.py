from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bel.domain.contract import Contract, ContractItem
from bel.domain.event import BusinessEvent
from bel.domain.evidence import EvidenceDocument, EvidenceFragment
from bel.domain.exception import TaskException
from bel.domain.invoice import Invoice, InvoiceItem
from bel.domain.matching import InvoiceAllocation, MatchCandidate, MatchCase, PaymentAllocation
from bel.domain.payment import Payment
from bel.infrastructure.persistence.models import (
    BusinessEventModel,
    ContractItemModel,
    ContractModel,
    EvidenceDocumentModel,
    EvidenceFragmentModel,
    ImportRunModel,
    InvoiceAllocationModel,
    InvoiceItemModel,
    InvoiceModel,
    MatchCandidateModel,
    MatchCaseModel,
    PaymentAllocationModel,
    PaymentModel,
    TaskExceptionModel,
)


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


def _contract_to_domain(m: ContractModel) -> Contract:
    return Contract(
        id=m.id,
        contract_no=m.contract_no,
        contract_type=m.contract_type,
        counterparty=m.counterparty,
        buyer=m.buyer,
        gross_amount=m.gross_amount,
        currency=m.currency,
        contract_date=m.contract_date,
        current_source_fragment_id=m.current_source_fragment_id,
        created_at=m.created_at,
        updated_at=m.updated_at,
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


class ContractRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, contract: Contract) -> None:
        self._session.add(
            ContractModel(
                id=contract.id,
                contract_no=contract.contract_no,
                contract_type=contract.contract_type,
                counterparty=contract.counterparty,
                buyer=contract.buyer,
                gross_amount=contract.gross_amount,
                currency=contract.currency,
                contract_date=contract.contract_date,
                current_source_fragment_id=contract.current_source_fragment_id,
                created_at=contract.created_at,
                updated_at=contract.updated_at,
            )
        )

    def get(self, contract_id: uuid.UUID) -> Contract | None:
        m = self._session.get(ContractModel, contract_id)
        return _contract_to_domain(m) if m else None

    def find_by_contract_no(self, contract_no: str) -> list[Contract]:
        rows = self._session.scalars(select(ContractModel).where(ContractModel.contract_no == contract_no))
        return [_contract_to_domain(m) for m in rows]

    def list_all(self) -> list[Contract]:
        rows = self._session.scalars(select(ContractModel))
        return [_contract_to_domain(m) for m in rows]

    def count(self) -> int:
        return len(self._session.scalars(select(ContractModel.id)).all())


class ContractItemRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, item: ContractItem) -> None:
        self._session.add(
            ContractItemModel(
                id=item.id,
                contract_id=item.contract_id,
                sku=item.sku,
                product_name=item.product_name,
                specification=item.specification,
                quantity=item.quantity,
                unit=item.unit,
                unit_price=item.unit_price,
                gross_amount=item.gross_amount,
                tax_rate=item.tax_rate,
                net_amount=item.net_amount,
                current_source_fragment_id=item.current_source_fragment_id,
                created_at=item.created_at,
            )
        )

    def count(self) -> int:
        return len(self._session.scalars(select(ContractItemModel.id)).all())


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

    def list_for_invoice(self, invoice_id: uuid.UUID) -> list[InvoiceItem]:
        rows = self._session.scalars(select(InvoiceItemModel).where(InvoiceItemModel.invoice_id == invoice_id))
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
            )
        )

    def get(self, payment_id: uuid.UUID) -> Payment | None:
        m = self._session.get(PaymentModel, payment_id)
        return _payment_to_domain(m) if m else None

    def list_all(self) -> list[Payment]:
        rows = self._session.scalars(select(PaymentModel))
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

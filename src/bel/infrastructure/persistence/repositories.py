from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bel.domain.accrual import (
    Accrual,
    AccrualBasisFact,
    AccrualReversal,
    CostRecognitionFact,
    HistoricalAccrualFact,
    InvoiceItemAllocation,
)
from bel.domain.contract import Contract, ContractItem
from bel.domain.event import BusinessEvent
from bel.domain.evidence import EvidenceDocument, EvidenceFragment
from bel.domain.exception import TaskException
from bel.domain.invoice import Invoice, InvoiceItem
from bel.domain.matching import InvoiceAllocation, MatchCandidate, MatchCase, PaymentAllocation
from bel.domain.payment import Payment
from bel.infrastructure.persistence.models import (
    AccrualBasisFactModel,
    AccrualModel,
    AccrualReversalModel,
    BusinessEventModel,
    ContractItemModel,
    ContractModel,
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


def _contract_item_to_domain(m: ContractItemModel) -> ContractItem:
    return ContractItem(
        id=m.id,
        contract_id=m.contract_id,
        source_item_key=m.source_item_key,
        sku=m.sku,
        product_name=m.product_name,
        specification=m.specification,
        quantity=m.quantity,
        unit=m.unit,
        unit_price=m.unit_price,
        gross_amount=m.gross_amount,
        tax_rate=m.tax_rate,
        net_amount=m.net_amount,
        current_source_fragment_id=m.current_source_fragment_id,
        created_at=m.created_at,
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
                source_item_key=item.source_item_key,
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

    def get(self, item_id: uuid.UUID) -> ContractItem | None:
        m = self._session.get(ContractItemModel, item_id)
        return _contract_item_to_domain(m) if m else None

    def find_by_contract_and_key(self, contract_id: uuid.UUID, source_item_key: str) -> ContractItem | None:
        m = self._session.scalar(
            select(ContractItemModel).where(
                ContractItemModel.contract_id == contract_id, ContractItemModel.source_item_key == source_item_key
            )
        )
        return _contract_item_to_domain(m) if m else None

    def list_for_contract(self, contract_id: uuid.UUID) -> list[ContractItem]:
        rows = self._session.scalars(
            select(ContractItemModel).where(ContractItemModel.contract_id == contract_id).order_by(ContractItemModel.created_at)
        )
        return [_contract_item_to_domain(m) for m in rows]

    def list_all(self) -> list[ContractItem]:
        rows = self._session.scalars(select(ContractItemModel))
        return [_contract_item_to_domain(m) for m in rows]

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
    )


def _cost_recognition_fact_to_domain(m: CostRecognitionFactModel) -> CostRecognitionFact:
    return CostRecognitionFact(
        id=m.id,
        contract_id=m.contract_id,
        recognition_date=m.recognition_date,
        basis=m.basis,
        source_fragment_id=m.source_fragment_id,
        created_at=m.created_at,
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
            select(InvoiceItemAllocationModel).where(InvoiceItemAllocationModel.contract_item_id == contract_item_id)
        )
        return [_invoice_item_allocation_to_domain(m) for m in rows]

    def list_for_invoice_item(self, invoice_item_id: uuid.UUID) -> list[InvoiceItemAllocation]:
        rows = self._session.scalars(
            select(InvoiceItemAllocationModel).where(InvoiceItemAllocationModel.invoice_item_id == invoice_item_id)
        )
        return [_invoice_item_allocation_to_domain(m) for m in rows]

    def list_all(self) -> list[InvoiceItemAllocation]:
        rows = self._session.scalars(select(InvoiceItemAllocationModel))
        return [_invoice_item_allocation_to_domain(m) for m in rows]

    def find(self, invoice_item_id: uuid.UUID, contract_item_id: uuid.UUID) -> InvoiceItemAllocation | None:
        m = self._session.scalar(
            select(InvoiceItemAllocationModel).where(
                InvoiceItemAllocationModel.invoice_item_id == invoice_item_id,
                InvoiceItemAllocationModel.contract_item_id == contract_item_id,
            )
        )
        return _invoice_item_allocation_to_domain(m) if m else None

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
            )
        )

    def list_all(self) -> list[CostRecognitionFact]:
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

    def list_all(self) -> list[AccrualBasisFact]:
        rows = self._session.scalars(select(AccrualBasisFactModel))
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

    def list_all(self) -> list[HistoricalAccrualFact]:
        rows = self._session.scalars(select(HistoricalAccrualFactModel))
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

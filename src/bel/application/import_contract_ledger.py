from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from bel.adapters.excel.contract_ledger import compute_sha256, parse_contract_ledger
from bel.domain.contract import Contract
from bel.domain.event import BusinessEvent, BusinessEventType
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionStatus, ExceptionType, TaskException
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EventRepository,
    EvidenceRepository,
    ExceptionRepository,
    ImportRunRepository,
)

SOURCE_TYPE = "contract_ledger_xlsx"
# Every business row in this sheet is the same contract category — see
# docs/PHASE1-DECISIONS.md for why this is a constant, not inferred.
CONTRACT_TYPE = "出口报关购销合同"
DEFAULT_CURRENCY = "CNY"


@dataclass
class BusinessKeyConflictSummary:
    contract_no: str
    contract_ids: list[uuid.UUID]


@dataclass
class ImportResult:
    evidence_document_id: uuid.UUID
    file_name: str
    sha256: str
    is_reimport: bool
    sheets: list[str]
    primary_sheet: str
    primary_sheet_columns: int
    business_rows: int
    blank_trailing_rows: int
    contracts_created: int
    contract_items_created: int
    gross_amount_total: Decimal
    business_key_conflicts: list[BusinessKeyConflictSummary]
    distinct_sellers: int
    distinct_buyers: int
    distinct_owners: int
    distinct_customs_receivers: int
    missing_export_contract_no: int


def import_contract_ledger(session: Session, file_path: Path) -> ImportResult:
    """Import a contract ledger workbook.

    Idempotent on file content: re-importing bytes that hash to an
    EvidenceDocument already on file creates only an audit ImportRun row
    and zero new business facts — see docs/PHASE1-DECISIONS.md.
    """
    now = datetime.now(timezone.utc)
    sha256 = compute_sha256(file_path)

    evidence_repo = EvidenceRepository(session)
    contract_repo = ContractRepository(session)
    exception_repo = ExceptionRepository(session)
    event_repo = EventRepository(session)
    import_run_repo = ImportRunRepository(session)

    existing_document = evidence_repo.find_document_by_sha256(sha256)
    if existing_document is not None:
        import_run_repo.add(
            run_id=uuid.uuid4(),
            evidence_document_id=existing_document.id,
            file_name=file_path.name,
            sha256=sha256,
            started_at=now,
            completed_at=now,
            is_reimport=True,
            contracts_created_count=0,
            contract_items_created_count=0,
            business_key_conflicts_detected_count=0,
        )
        session.commit()
        return ImportResult(
            evidence_document_id=existing_document.id,
            file_name=file_path.name,
            sha256=sha256,
            is_reimport=True,
            sheets=[],
            primary_sheet="",
            primary_sheet_columns=0,
            business_rows=0,
            blank_trailing_rows=0,
            contracts_created=0,
            contract_items_created=0,
            gross_amount_total=Decimal("0"),
            business_key_conflicts=[],
            distinct_sellers=0,
            distinct_buyers=0,
            distinct_owners=0,
            distinct_customs_receivers=0,
            missing_export_contract_no=0,
        )

    parsed = parse_contract_ledger(file_path)

    document = EvidenceDocument(
        id=uuid.uuid4(), file_name=file_path.name, sha256=sha256, source_type=SOURCE_TYPE, imported_at=now
    )
    evidence_repo.add_document(document)

    # Two passes, with an explicit flush between them. SQLAlchemy only
    # auto-orders INSERTs across mapped classes that share an ORM
    # relationship() — with bare ForeignKey columns (no relationship()
    # here, by design: repositories don't need ORM graph traversal) it
    # does NOT guarantee fragments are inserted before the contracts
    # that reference them. With PRAGMA foreign_keys=ON (see database.py)
    # that would fail immediately. Flushing fragments first makes the
    # dependency explicit instead of relying on implicit ordering.
    row_fragment_ids: dict[int, uuid.UUID] = {}
    for row in parsed.rows:
        fragment = EvidenceFragment(
            id=uuid.uuid4(),
            evidence_document_id=document.id,
            fragment_kind=FragmentKind.EXCEL_ROW,
            sheet_name=row.sheet_name,
            row_number=row.row_number,
            locator_json=None,
            raw_data=row.raw_data,
            created_at=now,
        )
        evidence_repo.add_fragment(fragment)
        row_fragment_ids[row.row_number] = fragment.id

    session.flush()

    contract_ids_by_no: dict[str, list[uuid.UUID]] = {}
    gross_amount_total = Decimal("0")

    for row in parsed.business_rows:
        contract = Contract(
            id=uuid.uuid4(),
            contract_no=row.contract_no,
            contract_type=CONTRACT_TYPE,
            counterparty=row.counterparty,
            buyer=row.buyer,
            gross_amount=row.gross_amount,
            currency=DEFAULT_CURRENCY,
            contract_date=None,
            current_source_fragment_id=row_fragment_ids[row.row_number],
            created_at=now,
            updated_at=now,
        )
        contract_repo.add(contract)
        contract_ids_by_no.setdefault(row.contract_no, []).append(contract.id)
        gross_amount_total += row.gross_amount

    conflicts: list[BusinessKeyConflictSummary] = []
    for contract_no, ids in contract_ids_by_no.items():
        if len(ids) <= 1:
            continue
        conflicts.append(BusinessKeyConflictSummary(contract_no=contract_no, contract_ids=ids))
        exception_repo.add(
            TaskException(
                id=uuid.uuid4(),
                exception_type=ExceptionType.BUSINESS_KEY_CONFLICT,
                status=ExceptionStatus.OPEN,
                summary=f"contract_no {contract_no!r} maps to {len(ids)} conflicting Contract records",
                detail={"contract_no": contract_no, "contract_ids": [str(i) for i in ids]},
                created_at=now,
            )
        )
        event_repo.add(
            BusinessEvent(
                id=uuid.uuid4(),
                event_type=BusinessEventType.BUSINESS_KEY_CONFLICT_DETECTED,
                occurred_at=now,
                payload={"contract_no": contract_no, "contract_ids": [str(i) for i in ids]},
            )
        )

    business_rows = parsed.business_rows
    distinct_sellers = len({r.counterparty for r in business_rows if r.counterparty})
    distinct_buyers = len({r.buyer for r in business_rows if r.buyer})
    distinct_owners = len({r.raw_data.get("对接人") for r in business_rows if r.raw_data.get("对接人")})
    distinct_customs_receivers = len(
        {r.raw_data.get("接收报关单位") for r in business_rows if r.raw_data.get("接收报关单位")}
    )
    missing_export_contract_no = len([r for r in business_rows if not r.raw_data.get("外销合同编码")])

    event_repo.add(
        BusinessEvent(
            id=uuid.uuid4(),
            event_type=BusinessEventType.CONTRACT_IMPORTED,
            occurred_at=now,
            payload={
                "evidence_document_id": str(document.id),
                "contracts_created": len(business_rows),
                "gross_amount_total": str(gross_amount_total),
            },
        )
    )

    import_run_repo.add(
        run_id=uuid.uuid4(),
        evidence_document_id=document.id,
        file_name=file_path.name,
        sha256=sha256,
        started_at=now,
        completed_at=now,
        is_reimport=False,
        contracts_created_count=len(business_rows),
        contract_items_created_count=0,
        business_key_conflicts_detected_count=len(conflicts),
    )

    session.commit()

    return ImportResult(
        evidence_document_id=document.id,
        file_name=file_path.name,
        sha256=sha256,
        is_reimport=False,
        sheets=parsed.sheet_names,
        primary_sheet=parsed.primary_sheet,
        primary_sheet_columns=parsed.primary_sheet_columns,
        business_rows=len(business_rows),
        blank_trailing_rows=len(parsed.blank_trailing_rows),
        contracts_created=len(business_rows),
        contract_items_created=0,
        gross_amount_total=gross_amount_total,
        business_key_conflicts=conflicts,
        distinct_sellers=distinct_sellers,
        distinct_buyers=distinct_buyers,
        distinct_owners=distinct_owners,
        distinct_customs_receivers=distinct_customs_receivers,
        missing_export_contract_no=missing_export_contract_no,
    )

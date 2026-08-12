from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class EvidenceDocument:
    """The original workbook a set of business facts was extracted from."""

    id: UUID
    file_name: str
    sha256: str
    source_type: str
    imported_at: datetime


class FragmentKind:
    """EXCEL_ROW uses sheet_name/row_number (Phase 1, kept for backward
    compatibility). PDF_TRANSACTION and any future kind use locator_json
    instead. See docs/PHASE2A-DECISIONS.md."""

    EXCEL_ROW = "EXCEL_ROW"
    PDF_TRANSACTION = "PDF_TRANSACTION"
    # Close Fact Pack entries and manual CLI confirmations — locator is a
    # {"section": ..., "index": ...} pair. See docs/PHASE2B-DECISIONS.md.
    MANUAL_FACT = "MANUAL_FACT"


@dataclass(frozen=True)
class EvidenceFragment:
    """One raw, unmodified evidence unit (an Excel row or a PDF
    transaction). raw_data is never overwritten."""

    id: UUID
    evidence_document_id: UUID
    fragment_kind: str
    sheet_name: str | None
    row_number: int | None
    locator_json: dict[str, Any] | None
    raw_data: dict[str, Any]
    created_at: datetime

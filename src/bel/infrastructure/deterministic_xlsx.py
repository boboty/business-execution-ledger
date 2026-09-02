"""Canonical deterministic XLSX byte normalization.

openpyxl stamps wall-clock time into an XLSX package in two places that
survive into the saved bytes and break byte identity across a real time
boundary:

- every ZIP entry's ``date_time`` (written near the current time), and
- ``docProps/core.xml`` ``dcterms:created`` / ``dcterms:modified``.

The FIRST-STAGE CUTOVER GATE requires byte-identical exports of the same
state/filter (docs/FIRST-STAGE-CUTOVER-GATE.md section 10), and all four
first-stage XLSX Data Products (Contract Business Ledger, Period Close,
Invoice Preparation, Exception & Task) share one serialization
discipline, so there is exactly ONE canonical normalizer here instead of
per-exporter copies.

This is pure serialization infrastructure: workbook content, sheet
order/names, cell types, styles and formula-injection guards are
preserved verbatim — every ZIP entry passes through unchanged except the
fixed package-metadata timestamps (entry ``date_time`` and core.xml
created/modified). No current time, no generated_at, no random id.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime

# openpyxl writes these byte-identical for identical state once the
# package metadata is pinned. Fixed timestamp chosen arbitrarily (pre-epoch
# would break some readers) and shared by every Data Product.
FIXED_XLSX_DATETIME = datetime(1980, 1, 1, 0, 0, 0)
_FIXED_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_FIXED_TIMESTAMP_ISO = "1980-01-01T00:00:00Z"

# Both timestamp elements of docProps/core.xml. openpyxl honors
# ``properties.created`` at save but ALWAYS overwrites ``properties.modified``
# with the current time (writer/excel.py), so the modified element must be
# re-pinned after save; the created element is re-pinned too so a forgotten
# ``set_fixed_workbook_properties`` call can never leak wall-clock time.
_CORE_TIMESTAMP_RE = re.compile(r"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:(?:created|modified)>)")


def set_fixed_workbook_properties(wb) -> None:
    """Pin ``Workbook`` docProps timestamps to the fixed datetime before
    save. ``created`` is honored by openpyxl at save; ``modified`` is
    overwritten at save and re-pinned by ``deterministic_xlsx_bytes``."""
    wb.properties.created = FIXED_XLSX_DATETIME
    wb.properties.modified = FIXED_XLSX_DATETIME


def deterministic_xlsx_bytes(content: bytes) -> bytes:
    """Normalize an openpyxl-saved XLSX package to byte-identical form.

    Re-opens the XLSX ZIP, rewrites every entry with a fixed ``date_time``,
    and pins both core.xml timestamp fields to the fixed ISO timestamp.
    Compression, entry order, content and cell data are preserved. Two
    exports of identical state are byte-identical regardless of wall-clock
    time or the moment the exporter ran.
    """
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content), "r") as source, zipfile.ZipFile(
        out, "w", compression=zipfile.ZIP_DEFLATED
    ) as destination:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "docProps/core.xml":
                text = data.decode("utf-8")
                text = _CORE_TIMESTAMP_RE.sub(
                    lambda m: m.group(1) + _FIXED_TIMESTAMP_ISO + m.group(2), text
                )
                data = text.encode("utf-8")
            info.date_time = _FIXED_ZIP_DATE_TIME
            destination.writestr(info, data)
    return out.getvalue()

"""Shared XLSX byte-determinism assertions for the four first-stage Data
Product exporters. A plain helper module (pytest never collects it — the
name matches no test pattern), importable as ``tests.xlsx_assertions`` via
the repo's PEP-420 namespace package.

Two guarantees the FIRST-STAGE CUTOVER GATE needs from every XLSX export:

1. byte identity across a REAL wall-clock / ZIP-timestamp boundary, and
2. a fixed package: every ZIP entry ``date_time`` and both
   ``docProps/core.xml`` timestamp fields pinned (no current time, no
   generated_at).
"""

from __future__ import annotations

import re
import time
import zipfile
from io import BytesIO
from typing import Any, Callable

FIXED_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
FIXED_CORE_TIMESTAMP = "1980-01-01T00:00:00Z"

_CORE_TS_RE = re.compile(r"<dcterms:(?:created|modified)[^>]*>([^<]*)</dcterms:(?:created|modified)>")


def assert_xlsx_package_metadata_fixed(xlsx_bytes: bytes) -> None:
    """Inspect the produced XLSX ZIP: every entry's ``date_time`` is the
    fixed value and ``docProps/core.xml`` created/modified are the fixed
    ISO timestamp — the export carries no wall-clock metadata at all."""
    with zipfile.ZipFile(BytesIO(xlsx_bytes), "r") as zf:
        infos = zf.infolist()
        assert infos, "empty XLSX package"
        for info in infos:
            assert info.date_time == FIXED_ZIP_DATE_TIME, f"unpinned entry timestamp: {info.filename}"
        core_xml = zf.read("docProps/core.xml").decode("utf-8")
    matches = list(_CORE_TS_RE.finditer(core_xml))
    assert matches, "core.xml has no dcterms:created/modified elements"
    for match in matches:
        assert match.group(1) == FIXED_CORE_TIMESTAMP, "core.xml timestamp not pinned"


def assert_xlsx_exporter_deterministic(exporter_fn: Callable[[Any], bytes], product: Any) -> None:
    """Export *product* twice across a real wall-clock second boundary,
    require byte identity, and verify the produced package metadata is
    fully pinned."""
    first = exporter_fn(product)
    time.sleep(1.2)  # cross at least one whole wall-clock second
    second = exporter_fn(product)
    assert first == second, "XLSX export is NOT byte-identical across a wall-clock boundary"
    assert_xlsx_package_metadata_fixed(first)

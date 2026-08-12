"""Golden invoice-import baseline against the synthetic invoice fixture
— the public counterpart of the private acceptance suite's
P2A_INVOICE_IMPORT scenario. See docs/PRIVATE-DATA-POLICY.md.
"""

import json
from decimal import Decimal
from pathlib import Path

from bel.application.import_invoices import import_invoices
from bel.domain.invoice import InvoiceDirection

BASELINE_PATH = Path(__file__).parent / "invoice-import-baseline.json"


def test_synthetic_invoice_import_matches_baseline(db_session, synthetic_invoices_path):
    baseline = json.loads(BASELINE_PATH.read_text())

    result = import_invoices(db_session, synthetic_invoices_path, InvoiceDirection.PURCHASE)

    assert result.is_reimport is False
    assert result.invoices_created == baseline["invoices_created"]
    assert result.invoice_items_created == baseline["invoice_items_created"]
    assert result.net_amount_total == Decimal(baseline["net_amount_total"])
    assert result.tax_amount_total == Decimal(baseline["tax_amount_total"])
    assert result.gross_amount_total == Decimal(baseline["gross_amount_total"])
    assert result.net_amount_total + result.tax_amount_total == result.gross_amount_total


def test_synthetic_invoice_reimport_is_idempotent(db_session, synthetic_invoices_path):
    baseline = json.loads(BASELINE_PATH.read_text())

    first = import_invoices(db_session, synthetic_invoices_path, InvoiceDirection.PURCHASE)
    second = import_invoices(db_session, synthetic_invoices_path, InvoiceDirection.PURCHASE)

    assert first.invoices_created == baseline["invoices_created"]
    assert second.is_reimport is True
    assert second.invoices_created == 0
    assert second.evidence_document_id == first.evidence_document_id

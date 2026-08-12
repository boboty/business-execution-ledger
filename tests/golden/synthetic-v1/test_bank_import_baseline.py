"""Golden bank-import baseline against the synthetic CMB-shaped PDF
fixture (fixtures/synthetic/bank_pdf.py) — the public counterpart of
the private acceptance suite's P2A_PAYMENT_IMPORT scenario. See
docs/PRIVATE-DATA-POLICY.md.
"""

import json
from decimal import Decimal
from pathlib import Path

from bel.application.import_bank import import_bank_statement

BASELINE_PATH = Path(__file__).parent / "bank-import-baseline.json"


def test_synthetic_bank_import_matches_baseline(db_session, synthetic_bank_pdf_path):
    baseline = json.loads(BASELINE_PATH.read_text())

    result = import_bank_statement(db_session, synthetic_bank_pdf_path, "cmb")

    assert result.is_reimport is False
    assert result.payments_created == baseline["payments_created"]
    assert result.opening_balance == Decimal(baseline["opening_balance"])
    assert result.total_in == Decimal(baseline["total_in"])
    assert result.total_out == Decimal(baseline["total_out"])
    assert result.closing_balance == Decimal(baseline["closing_balance"])
    # Reconciliation identity — see spec section 13.
    assert result.opening_balance + result.total_in - result.total_out == result.closing_balance


def test_synthetic_bank_reimport_is_idempotent(db_session, synthetic_bank_pdf_path):
    baseline = json.loads(BASELINE_PATH.read_text())

    first = import_bank_statement(db_session, synthetic_bank_pdf_path, "cmb")
    second = import_bank_statement(db_session, synthetic_bank_pdf_path, "cmb")

    assert first.payments_created == baseline["payments_created"]
    assert second.is_reimport is True
    assert second.payments_created == 0
    assert second.evidence_document_id == first.evidence_document_id

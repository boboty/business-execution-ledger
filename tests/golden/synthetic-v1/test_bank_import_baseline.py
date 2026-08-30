"""Golden bank-import baseline against the synthetic CMB-shaped PDF
fixture (fixtures/synthetic/bank_pdf.py) — the public counterpart of
the private acceptance suite's P2A_PAYMENT_IMPORT scenario. See
docs/PRIVATE-DATA-POLICY.md.
"""

import json
from decimal import Decimal
from pathlib import Path

from bel.application.import_bank import import_bank_statement
from bel.infrastructure.persistence.repositories import PaymentRepository

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


def test_ordinary_import_without_source_account_id_is_unchanged(db_session, synthetic_bank_pdf_path):
    """Backward compatibility regression (R5 round 2): the ordinary
    intake with no ``source_account_id`` behaves exactly as it did
    before the seam existed — same counts, same balances, NULL account
    on every Payment, and no extra rows."""
    baseline = json.loads(BASELINE_PATH.read_text())

    result = import_bank_statement(db_session, synthetic_bank_pdf_path, "cmb")

    assert result.source_account_id is None
    assert result.payments_created == baseline["payments_created"]
    assert result.total_in == Decimal(baseline["total_in"])
    payments = PaymentRepository(db_session).list_all()
    assert len(payments) == baseline["payments_created"]
    assert {p.source_account_id for p in payments} == {None}


def test_ordinary_import_records_supplied_source_account_id(db_session, synthetic_bank_pdf_path):
    """The R4/R5 Payment business identity seam on the ordinary intake:
    a caller-supplied source account is recorded on every Payment this
    import creates, and changes nothing else about the import."""
    baseline = json.loads(BASELINE_PATH.read_text())

    result = import_bank_statement(
        db_session, synthetic_bank_pdf_path, "cmb", source_account_id="ACC-SYNTHETIC"
    )

    assert result.source_account_id == "ACC-SYNTHETIC"
    assert result.payments_created == baseline["payments_created"]
    assert result.total_out == Decimal(baseline["total_out"])
    assert result.opening_balance == Decimal(baseline["opening_balance"])
    payments = PaymentRepository(db_session).list_all()
    assert len(payments) == baseline["payments_created"]
    assert {p.source_account_id for p in payments} == {"ACC-SYNTHETIC"}
    # Never inferred from the file, its name, or the profile.
    assert {p.bank_reference for p in payments} != {"ACC-SYNTHETIC"}

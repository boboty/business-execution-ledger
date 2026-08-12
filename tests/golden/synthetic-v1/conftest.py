from __future__ import annotations

from pathlib import Path

import pytest

from fixtures.synthetic import scenarios
from fixtures.synthetic.bank_pdf import build_cmb_bank_statement_pdf


@pytest.fixture
def synthetic_ledger_path(ledger_workbook_factory) -> Path:
    return ledger_workbook_factory(
        scenarios.CONTRACT_HEADERS, scenarios.CONTRACT_ROWS, filename="synthetic-contracts.xlsx"
    )


@pytest.fixture
def synthetic_invoices_path(invoice_workbook_factory) -> Path:
    return invoice_workbook_factory(
        scenarios.INVOICE_ROWS, buyer=scenarios.BUYER, filename="synthetic-invoices.xlsx"
    )


@pytest.fixture
def synthetic_bank_pdf_path(tmp_path) -> Path:
    path = tmp_path / "synthetic-bank.pdf"
    build_cmb_bank_statement_pdf(path, scenarios.OPENING_BALANCE, scenarios.PAYMENT_TRANSACTIONS)
    return path

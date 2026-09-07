"""Tiny, dependency-neutral presentation formatters.

Structured Application data stays structured (see
``bel.application.sales_invoice_preparation.SalesInvoiceNoteData``);
presentation owns wording. This module holds ONLY the formatting
functions that must produce an IDENTICAL string across presentation
surfaces (Web, CLI, CSV, XLSX) — it depends on neither
``bel.web`` nor ``bel.application.invoice_preparation_export``, and
neither of those depends on the other through here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bel.application.sales_invoice_preparation import SalesInvoiceNoteData


def format_sales_invoice_note(data: "SalesInvoiceNoteData") -> str:
    """The downstream-facing note text for a sales invoice's FX
    preparation, e.g. ``USD 125.00；汇率 7.2000``. The underlying values
    (USD amount, rate, date, expected CNY) stay structured Application
    data; this is the ONE place they are rendered as text."""
    return f"USD {data.contract_usd_amount:.2f}；汇率 {data.applicable_fx_rate:.4f}"

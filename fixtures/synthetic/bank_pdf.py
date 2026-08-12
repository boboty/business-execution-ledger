"""Builds a synthetic CMB-statement-shaped PDF for the public test suite.

Implements only the public parser contract that
src/bel/adapters/pdf/cmb_bank_statement.py depends on (date tokens
in the left margin, two decimal tokens per transaction row, a
上页余额:/期末余额: header pair, a 第N页/共M页 footer) — it does not
attempt to replicate any source document. All transaction data is
independently invented. The parser itself is never modified to fit this
fixture.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

_FONT_NAME = "STSong-Light"
if _FONT_NAME not in pdfmetrics.getRegisteredFontNames():
    pdfmetrics.registerFont(UnicodeCIDFont(_FONT_NAME))

_PAGE_W, _PAGE_H = A4
_TABLE_TOP0 = 140  # first transaction row's "top" (must stay > adapter's TABLE_TOP=118)
_ROW_HEIGHT = 25
_X_DATE, _X_BIZ, _X_REF, _X_DESC, _X_AMT, _X_BAL, _X_CP = 30, 90, 160, 230, 380, 440, 500


def build_cmb_bank_statement_pdf(
    path: Path,
    opening_balance: str,
    transactions: list[tuple[str, str, str, str, str, str]],
) -> str:
    """transactions: (date YYYYMMDD, business_type, bank_reference,
    description, counterparty, out_amount) tuples, in statement order.
    Returns the closing balance (str) so callers/tests don't have to
    duplicate the running-balance arithmetic."""
    balance = Decimal(opening_balance)

    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont(_FONT_NAME, 9)

    c.drawString(40, _PAGE_H - 30, "CMB Statement (synthetic fixture — no real account data)")
    c.drawString(40, _PAGE_H - 45, "Account:SYN-0000-0000")
    c.drawString(40, _PAGE_H - 60, f"上页余额:{opening_balance}")

    rows = []
    for date, biz_type, ref, desc, counterparty, out_amount in transactions:
        balance -= Decimal(out_amount)
        rows.append((date, biz_type, ref, desc, f"-{out_amount}", str(balance), counterparty))
    closing_balance = str(balance)

    c.drawString(220, _PAGE_H - 60, f"期末余额:{closing_balance}")

    for i, (date, biz, ref, desc, signed_amount, running_balance, counterparty) in enumerate(rows):
        top = _TABLE_TOP0 + i * _ROW_HEIGHT
        y = _PAGE_H - top
        c.drawString(_X_DATE, y, date)
        c.drawString(_X_BIZ, y, biz)
        c.drawString(_X_REF, y, ref)
        c.drawString(_X_DESC, y, desc)
        c.drawString(_X_AMT, y, signed_amount)
        c.drawString(_X_BAL, y, running_balance)
        c.drawString(_X_CP, y, counterparty)

    c.drawString(250, 20, "第1页/共1页(synthetic fixture)")
    c.showPage()
    c.save()

    return closing_balance

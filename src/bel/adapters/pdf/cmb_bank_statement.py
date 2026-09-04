"""CMB (招商银行) bank statement PDF adapter — deterministic text-layer
parsing, no OCR.

The supported PDF shape has a text layer but no drawn table grid (pdfplumber's
table-finder returns nothing), and column x-positions are NOT stable
across pages (they appear to auto-size per page). So this parser does
not use fixed pixel columns for amount/balance/counterparty. Instead:

  1. Every word on a page is assigned to a transaction "band" by
     vertical position: band anchors are 8-digit date tokens in the
     left margin, and band boundaries are the midpoints between
     consecutive anchors. This is what correctly reassembles a
     counterparty name that wraps onto a line ABOVE its own date
     anchor as well as below it (see docs/PHASE2A-DECISIONS.md) — plain
     "next date starts a new row"
     logic would truncate those names.
  2. Within a band, the two purely-numeric decimal tokens
     (`-?[\\d,]+\\.\\d{2}`) are the amount and balance, left one first —
     found by pattern, not by x-position, so a page's column drift
     never matters.
  3. Three fixed columns precede the amount, distinguished by their
     left-edge x0 band (the statement auto-sizes column x-positions per
     page, but the column ORDER — 业务类型 < 票据号 < 摘要 — and their
     relative x-gaps are stable): the leftmost column is business_type;
     the 票据号 (Bill No.) column carries the bank's own transaction
     reference (numeric or alphanumeric, possibly wrapping onto a second
     line within the column); the 摘要 column is the description.
     Everything after the balance is the counterparty.
  4. Page footers ("第N页/共M页 ...") are excluded by finding that
     marker's own vertical position and treating it as the bottom
     boundary of the page's last band — otherwise footer text bleeds
     into the last transaction.

Nothing here infers a transaction's identity from "same date + same
amount" — see spec section 12. Each transaction is whatever the PDF's
own row layout says it is.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pdfplumber

DATE_RE = re.compile(r"^\d{8}$")
MONEY_RE = re.compile(r"^-?[\d,]+\.\d{2}$")
FOOTER_RE = re.compile(r"^第\d+页")
DATE_X_RANGE = (20, 66)
# 票据号 (Bill No.) data column — its left-edge x0 band sits strictly
# between the 业务类型 column (business type, leftmost) and the 摘要 column
# (description). The statement auto-sizes column x-positions per page (the
# 票据号 header label drifts from x0=127 to x0=141 across pages), but the
# DATA's own left edge stays in a narrow band (~113-134) and always clears
# the business-type column (<=~81) on the left and the description column
# (>=~167) on the right. This band — the statement's own column layout, not
# a "looks like a number" content heuristic — is what identifies the
# reference.
BILL_NO_X_RANGE = (95.0, 160.0)
TABLE_TOP = 118  # below the repeated account-info header block on every page

RAW_DATE_KEY = "日期"
RAW_BUSINESS_TYPE_KEY = "业务类型"
RAW_BILL_NO_KEY = "票据号"
RAW_DESCRIPTION_KEY = "摘要"
RAW_AMOUNT_KEY = "借方/贷方金额"
RAW_BALANCE_KEY = "余额"
RAW_COUNTERPARTY_KEY = "对手户名"


def _parse_amount(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _parse_date(text: str) -> date:
    return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))


@dataclass
class ParsedBankTransaction:
    page_index: int
    transaction_index: int  # 0-based, sequential across the whole document
    raw_data: dict[str, Any]
    transaction_date: date
    business_type: str | None
    bank_reference: str | None
    description: str | None
    signed_amount: Decimal
    running_balance: Decimal
    counterparty: str | None


@dataclass
class ParsedBankStatement:
    transactions: list[ParsedBankTransaction]
    opening_balance: Decimal | None
    closing_balance: Decimal | None


def _words_by_band(page) -> list[list[dict]]:
    words = [w for w in page.extract_words(use_text_flow=False, keep_blank_chars=False) if w["top"] >= TABLE_TOP]
    footer_tops = [w["top"] for w in words if FOOTER_RE.match(w["text"])]
    footer_top = min(footer_tops) if footer_tops else page.height
    words = [w for w in words if w["top"] < footer_top - 2]

    anchors = sorted(
        w["top"] for w in words if DATE_X_RANGE[0] <= w["x0"] < DATE_X_RANGE[1] and DATE_RE.match(w["text"])
    )
    if not anchors:
        return []
    midpoints = [(anchors[i] + anchors[i + 1]) / 2 for i in range(len(anchors) - 1)]

    bands: list[list[dict]] = [[] for _ in anchors]
    for w in words:
        idx = bisect.bisect_right(midpoints, w["top"])
        if idx >= len(bands):
            idx = len(bands) - 1
        bands[idx].append(w)
    return bands


def _parse_band(band: list[dict]) -> dict[str, Any]:
    band_sorted = sorted(band, key=lambda w: (w["x0"], w["top"]))
    date_words = [w for w in band_sorted if DATE_X_RANGE[0] <= w["x0"] < DATE_X_RANGE[1] and DATE_RE.match(w["text"])]
    money_words = sorted((w for w in band_sorted if MONEY_RE.match(w["text"])), key=lambda w: w["x0"])
    rest = [w for w in band_sorted if w not in date_words and w not in money_words]

    if len(date_words) != 1:
        raise ValueError(f"expected exactly 1 date token in band, got {len(date_words)}: {[w['text'] for w in date_words]}")
    if len(money_words) != 2:
        raise ValueError(
            f"expected exactly 2 money tokens (amount, balance) in band, got {len(money_words)}: "
            f"{[w['text'] for w in money_words]}"
        )

    amount_w, balance_w = money_words
    before = [w for w in rest if w["x0"] < amount_w["x0"]]
    after = sorted((w for w in rest if w["x0"] > balance_w["x0"]), key=lambda w: (w["top"], w["x0"]))

    def _read_column(x_min: float, x_max: float) -> str | None:
        """Concatenate one left-edge x0 column in top-first reading order
        (a wrapped reference/description keeps its natural line order —
        never re-sorted by x0 across the wrap)."""
        tokens = sorted(
            (w for w in before if x_min <= w["x0"] < x_max), key=lambda w: (w["top"], w["x0"])
        )
        return "".join(w["text"] for w in tokens) or None

    business_type = _read_column(0.0, BILL_NO_X_RANGE[0])
    bank_reference = _read_column(BILL_NO_X_RANGE[0], BILL_NO_X_RANGE[1])
    description = _read_column(BILL_NO_X_RANGE[1], float("inf"))
    counterparty = "".join(w["text"] for w in after) or None

    return {
        "date_text": date_words[0]["text"],
        "business_type": business_type,
        "bank_reference": bank_reference,
        "description": description,
        "amount_text": amount_w["text"],
        "balance_text": balance_w["text"],
        "counterparty": counterparty,
    }


def parse_cmb_bank_statement(path: Path) -> ParsedBankStatement:
    transactions: list[ParsedBankTransaction] = []
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    transaction_index = 0

    with pdfplumber.open(path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            all_words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            if opening_balance is None:
                for w in all_words:
                    if w["text"].startswith("上页余额:"):
                        opening_balance = _parse_amount(w["text"].split(":", 1)[1])
                        break
            for w in all_words:
                if w["text"].startswith("期末余额:"):
                    closing_balance = _parse_amount(w["text"].split(":", 1)[1])

            for band in _words_by_band(page):
                parsed = _parse_band(band)
                raw_data = {
                    RAW_DATE_KEY: parsed["date_text"],
                    RAW_BUSINESS_TYPE_KEY: parsed["business_type"],
                    RAW_BILL_NO_KEY: parsed["bank_reference"],
                    RAW_DESCRIPTION_KEY: parsed["description"],
                    # Bank's own signed representation, preserved verbatim —
                    # never overwritten by the positive-amount+direction
                    # canonical form. See spec section 11.
                    RAW_AMOUNT_KEY: parsed["amount_text"],
                    RAW_BALANCE_KEY: parsed["balance_text"],
                    RAW_COUNTERPARTY_KEY: parsed["counterparty"],
                }
                transactions.append(
                    ParsedBankTransaction(
                        page_index=page_index,
                        transaction_index=transaction_index,
                        raw_data=raw_data,
                        transaction_date=_parse_date(parsed["date_text"]),
                        business_type=parsed["business_type"],
                        bank_reference=parsed["bank_reference"],
                        description=parsed["description"],
                        signed_amount=_parse_amount(parsed["amount_text"]),
                        running_balance=_parse_amount(parsed["balance_text"]),
                        counterparty=parsed["counterparty"],
                    )
                )
                transaction_index += 1

    return ParsedBankStatement(transactions=transactions, opening_balance=opening_balance, closing_balance=closing_balance)

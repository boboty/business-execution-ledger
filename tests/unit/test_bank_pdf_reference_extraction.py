"""Regression matrix for CMB bank-reference extraction (parser-repair round).

The CMB statement carries the bank's own transaction reference in a
dedicated ``票据号`` (Bill No.) column whose data sits in a narrow left-edge
x0 band strictly between ``业务类型`` (business type) and ``摘要``
(description). The previous parser guessed the reference as "the lone
long-digit token immediately after business_type in top-first order" — which
missed genuine references that (a) sit on a slightly different sub-line than
the business type, (b) are alphanumeric, or (c) wrap across two tokens inside
the column.

These tests pin the column-semantics rule (``BILL_NO_X_RANGE``) using ONLY
synthetic word positions — never a real private value. They mirror the real
CMB layout's column x0 positions observed in the source (date ~33, business
type ~70, bill number ~113-134, description ~167-257, amount ~383, balance
~429, counterparty ~480+), including the ~1px sub-line offset that broke the
old top-first ordering.
"""

import pytest

from bel.adapters.pdf.cmb_bank_statement import _parse_band, _words_by_band, BILL_NO_X_RANGE

DATE = "20260706"
BUSINESS_TYPE = "对公转账出"
DESCRIPTION = "采购款"
AMOUNT = "-2233.00"
BALANCE = "10000.00"
COUNTERPARTY = "SellerA"


def _word(text: str, x0: float, top: float) -> dict:
    return {"text": text, "x0": x0, "x1": x0 + len(text) * 6, "top": top}


class _FakePage:
    def __init__(self, words: list[dict], height: float = 800):
        self._words = words
        self.height = height

    def extract_words(self, use_text_flow=False, keep_blank_chars=False):
        return self._words


def _row_words(
    *,
    top: float = 130.0,
    bill_no: str | None = None,
    bill_top: float | None = None,
    description: str | None = DESCRIPTION,
    business_type: str = BUSINESS_TYPE,
) -> list[dict]:
    """One synthetic transaction row in the real CMB column layout.

    The date/amount/balance and the bill number sit on ``top + 1`` (the real
    statement renders the numeric columns on a ~1px lower sub-line than the
    CJK business-type/description columns). ``bill_top`` overrides the bill
    number's line (default: the numeric sub-line, which is the real gap).
    """
    row = [
        _word(DATE, 33, top + 1),
        _word(business_type, 70, top),
    ]
    if bill_no is not None:
        row.append(_word(bill_no, 120, top + 1 if bill_top is None else bill_top))
    if description is not None:
        row.append(_word(description, 253, top))
    row.append(_word(AMOUNT, 383, top + 1))
    row.append(_word(BALANCE, 429, top + 1))
    row.append(_word(COUNTERPARTY, 480, top))
    return row


def _parse(words: list[dict]) -> dict:
    return _parse_band(words)


# --- A. existing supported layout: pure-numeric reference, same line --------

def test_numeric_reference_same_line_still_captured():
    # The old parser already handled this (bill number directly after
    # business type on the same top). The column rule must keep it working.
    parsed = _parse(_row_words(bill_no="800000001", bill_top=130.0))
    assert parsed["business_type"] == BUSINESS_TYPE
    assert parsed["bank_reference"] == "800000001"
    assert parsed["description"] == DESCRIPTION


# --- B. newly-observed layout: reference on the numeric sub-line ------------

def test_numeric_reference_on_subline_is_captured():
    # The real gap: the bill number renders on the ~1px lower numeric
    # sub-line, so a top-first sort reordered it AFTER the description.
    parsed = _parse(_row_words(bill_no="800000001"))
    assert parsed["business_type"] == BUSINESS_TYPE
    assert parsed["bank_reference"] == "800000001"
    assert parsed["description"] == DESCRIPTION


def test_alphanumeric_reference_is_captured():
    # Some rows carry an alphanumeric bank reference in the same 票据号
    # column — the column position, not a "pure digits" content guess, is
    # what identifies it. Value is fully synthetic.
    parsed = _parse(_row_words(bill_no="SYNTH-ALNUM-REF-0001", business_type="代发费用"))
    assert parsed["business_type"] == "代发费用"
    assert parsed["bank_reference"] == "SYNTH-ALNUM-REF-0001"


# --- C. no reference in source -> None --------------------------------------

def test_no_reference_yields_none():
    parsed = _parse(_row_words(bill_no=None))
    assert parsed["bank_reference"] is None
    assert parsed["description"] == DESCRIPTION


# --- D. description-only content must NOT become bank_reference ------------

def test_description_identifierish_content_stays_description():
    # A description that merely "looks like" an identifier, but sits in the
    # description column (x0 >= BILL_NO_X_RANGE end), is description — never
    # promoted to bank_reference. Value is fully synthetic.
    parsed = _parse(_row_words(bill_no=None, description="DESC-ID-12345:实时缴税"))
    assert parsed["bank_reference"] is None
    assert parsed["description"] == "DESC-ID-12345:实时缴税"


def test_description_numeric_looking_stays_description():
    # Even a purely-numeric token in the DESCRIPTION column is not a
    # reference — the column decides, not the digit content.
    parsed = _parse(_row_words(bill_no=None, description="12345678"))
    assert parsed["bank_reference"] is None
    assert parsed["description"] == "12345678"


# --- E. leading-zero reference preserved exactly ---------------------------

def test_leading_zero_reference_preserved_exactly():
    parsed = _parse(_row_words(bill_no="007245"))
    assert parsed["bank_reference"] == "007245"  # never numeric-cast / stripped


# --- F. wrapped / multi-token reference inside the column ------------------

def test_wrapped_reference_concatenated_in_line_order():
    # A long reference wraps to a second line INSIDE the 票据号 column (both
    # fragments in the column x-band, on different tops). They concatenate in
    # top-first reading order, preserving the full source identifier. Values
    # are fully synthetic.
    words = [
        _word(DATE, 33, 131),
        _word(BUSINESS_TYPE, 70, 130),
        _word("1234", 113, 129),
        _word("5678", 132, 137),
        _word(DESCRIPTION, 253, 130),
        _word(AMOUNT, 383, 131),
        _word(BALANCE, 429, 131),
        _word(COUNTERPARTY, 480, 130),
    ]
    parsed = _parse(words)
    assert parsed["bank_reference"] == "12345678"
    assert parsed["description"] == DESCRIPTION


# --- G. same date + same amount never derives identity from similarity -----

def test_same_date_amount_never_derives_identity():
    # Two transactions identical in date/amount/counterparty differ ONLY by
    # reference — the reference must survive as the distinguishing identity;
    # it is never synthesised from the shared date/amount.
    words = _row_words(bill_no="800000001", top=130.0) + _row_words(bill_no="800000002", top=150.0)
    page = _FakePage(words)
    bands = _words_by_band(page)
    assert len(bands) == 2
    parsed = [_parse_band(b) for b in bands]
    assert [p["bank_reference"] for p in parsed] == ["800000001", "800000002"]
    assert [p["amount_text"] for p in parsed] == [AMOUNT, AMOUNT]
    assert parsed[0]["bank_reference"] != parsed[1]["bank_reference"]

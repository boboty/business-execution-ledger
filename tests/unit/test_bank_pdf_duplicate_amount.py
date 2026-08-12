"""Unit-tests the CMB adapter's row-banding/parsing core directly with a
fake pdfplumber Page (no PDF file needed) — see spec section 30's
"Payment duplicate amount" fixture: same date, same counterparty, same
amount, different bank reference must stay two distinct transactions.
This is deliberately independent of fixtures/synthetic/bank_pdf.py (the
full-pipeline golden fixture) — testing _words_by_band/_parse_band
directly exercises exactly the band-splitting logic this guarantee
depends on, using only synthetic word positions.
"""

from decimal import Decimal

from bel.adapters.pdf.cmb_bank_statement import _parse_band, _words_by_band


def _word(text: str, x0: float, top: float) -> dict:
    return {"text": text, "x0": x0, "x1": x0 + len(text) * 6, "top": top}


class _FakePage:
    def __init__(self, words: list[dict], height: float = 800):
        self._words = words
        self.height = height

    def extract_words(self, use_text_flow=False, keep_blank_chars=False):
        return self._words


def _transaction_words(top: float, date: str, counterparty: str, amount: str, balance: str, bill_no: str) -> list[dict]:
    return [
        _word(date, 30, top),
        _word("对公转账出", 70, top),
        _word(bill_no, 120, top),
        _word("采购款", 200, top),
        _word(amount, 380, top),
        _word(balance, 435, top),
        _word(counterparty, 480, top),
    ]


def test_same_date_counterparty_amount_different_reference_stays_two_transactions():
    # Bank references are pure digit strings — matching that shape here,
    # not an arbitrary label.
    words = _transaction_words(130, "20260706", "Seller A", "-2233.00", "10000.00", "800000001") + _transaction_words(
        150, "20260706", "Seller A", "-2233.00", "7767.00", "800000002"
    )
    page = _FakePage(words)

    bands = _words_by_band(page)
    assert len(bands) == 2

    parsed = [_parse_band(b) for b in bands]
    assert [p["date_text"] for p in parsed] == ["20260706", "20260706"]
    assert [p["counterparty"] for p in parsed] == ["Seller A", "Seller A"]
    assert [p["amount_text"] for p in parsed] == ["-2233.00", "-2233.00"]
    # The only thing that distinguishes them is bank_reference — and that
    # must be preserved, not collapsed into a single transaction.
    assert [p["bank_reference"] for p in parsed] == ["800000001", "800000002"]
    assert parsed[0]["balance_text"] != parsed[1]["balance_text"]

"""Independently-constructed synthetic business data for the public test
suite (docs/PRIVATE-DATA-POLICY.md — public fixtures must be
independently synthetic, not derived from real files). Every entity
name, contract/invoice number, date, and amount here is invented. The
*shape* of the data (a duplicate business key, ambiguous
same-amount matches, an out-of-scope counterparty, an unmatched
subject, a red/negative invoice) is derived from the public rule
scenarios exercised by the suite.

Scenario map (see tests/golden/synthetic-v1/):
  Contracts: 8 rows, one duplicate contract_no (PO-SYN-004, two
    counterparties -> BusinessKeyConflict), one missing 外销合同编码.
  Invoices: 6 groups / 7 item rows. Supplier Alpha is a 2-item invoice
    (unique match). Supplier Zeta appears on two separate invoices at
    the same amount as two same-amount contracts (complete equivalent
    permutation cohort x2).
    Supplier Eta has one normal invoice (unique match) and one
    negative/red invoice at an amount no contract has (unmatched).
    "UnrelatedServicesCo" is not a party to any contract (out of
    scope).
  Payments (bank statement): 7 OUT transactions. Supplier Beta/Delta/
    Gamma are unique matches; Supplier Zeta appears twice in the complete
    equivalent-permutation cohort; "UnrelatedLogisticsCo" is out of scope;
    Supplier Eta's payment amount matches no contract (unmatched).
"""

from __future__ import annotations

BUYER = "BuyerSyntheticCo"

# Single-token names throughout (contracts, invoices, AND the bank PDF):
# the CMB PDF adapter joins a band's trailing words with "".join with no
# separator — a name with an embedded space would be split
# into two pdfplumber word tokens and rejoin *without* the space, breaking
# the exact-string match against the contract's counterparty.
CONTRACT_HEADERS = ["序号", "合同编码", "卖方", "买方", "金额", "外销合同编码"]

CONTRACT_ROWS = [
    [1, "PO-SYN-001", "SupplierAlpha", BUYER, 1250.00, "EXP-SYN-001"],
    [2, "PO-SYN-002", "SupplierBeta", BUYER, 2480.00, "EXP-SYN-002"],
    [3, "PO-SYN-003", "SupplierGamma", BUYER, 3765.50, "EXP-SYN-003"],
    [4, "PO-SYN-004", "SupplierDelta", BUYER, 1900.00, "EXP-SYN-004"],
    [5, "PO-SYN-004", "SupplierEpsilon", BUYER, 2100.00, "EXP-SYN-004B"],  # duplicate contract_no with row 4
    [6, "PO-SYN-005", "SupplierZeta", BUYER, 1580.00, "EXP-SYN-005"],
    [7, "PO-SYN-006", "SupplierZeta", BUYER, 1580.00, None],  # missing export contract no
    [8, "PO-SYN-007", "SupplierEta", BUYER, 990.25, "EXP-SYN-007"],
]

# Column order matches tests/conftest.py INVOICE_HEADERS.
INVOICE_ROWS = [
    # Invoice 1: Supplier Alpha, 2 items, unique match to PO-SYN-001 (1250.00)
    ["Tmpl", " ", "数电票（普通发票）", "2026-07-03", None, "DIGITAL-SYN-001", "SupplierAlpha",
     "Widget A", None, "件", 8, "100.00", 800.00, None, None, 800.00, "正常", 1250.00, 0, 1250.00],
    [None, None, None, None, None, None, None,
     "Widget A Accessory", None, "件", 5, "90.00", 450.00, None, None, 450.00, None, None, None, None],
    # Invoice 2: Supplier Zeta, complete equivalent 2x2 cohort
    ["Tmpl", " ", "数电票（普通发票）", "2026-07-06", None, "DIGITAL-SYN-002", "SupplierZeta",
     "Product Z", None, "件", 10, "158.00", 1580.00, None, None, 1580.00, "正常", 1580.00, 0, 1580.00],
    # Invoice 3: Supplier Zeta, second subject in the equivalent cohort
    ["Tmpl", " ", "数电票（普通发票）", "2026-07-07", None, "DIGITAL-SYN-003", "SupplierZeta",
     "Product Z", None, "件", 10, "158.00", 1580.00, None, None, 1580.00, "正常", 1580.00, 0, 1580.00],
    # Invoice 4: Supplier Eta, unique match to PO-SYN-007 (990.25)
    ["Tmpl", " ", "数电票（普通发票）", "2026-07-09", None, "DIGITAL-SYN-004", "SupplierEta",
     "Product E", None, "件", 5, "198.05", 990.25, None, None, 990.25, "正常", 990.25, 0, 990.25],
    # Invoice 5: Supplier Eta, red/negative invoice — eligible counterparty but no contract at this amount (unmatched)
    ["Tmpl", " ", "数电票（普通发票）", "2026-07-11", None, "DIGITAL-SYN-005", "SupplierEta",
     "Product E Refund", None, "件", -1, "75.00", -75.00, None, None, -75.00, "正常", -75.00, 0, -75.00],
    # Invoice 6: out-of-scope counterparty (never a contract party)
    ["Tmpl", " ", "数电票（普通发票）", "2026-07-13", None, "DIGITAL-SYN-006", "UnrelatedServicesCo",
     "Misc Service", None, "次", 1, "500.00", 500.00, None, None, 500.00, "正常", 500.00, 0, 500.00],
]

OPENING_BALANCE = "50000.00"

# (date, business_type, bank_reference, description, counterparty, out_amount)
# All OUT transactions; running balance is computed by the PDF builder.
PAYMENT_TRANSACTIONS = [
    ("20260705", "对公转账", "100001", "采购款", "SupplierBeta", "2480.00"),
    ("20260708", "对公转账", "100002", "采购款", "SupplierDelta", "1900.00"),
    ("20260710", "对公转账", "100003", "采购款", "SupplierZeta", "1580.00"),
    ("20260712", "对公转账", "100004", "采购款", "SupplierZeta", "1580.00"),
    ("20260715", "对公转账", "100005", "采购款", "SupplierGamma", "3765.50"),
    ("20260718", "对公转账", "100006", "杂项支出", "UnrelatedLogisticsCo", "300.00"),
    ("20260722", "对公转账", "100007", "采购款", "SupplierEta", "500.00"),
]

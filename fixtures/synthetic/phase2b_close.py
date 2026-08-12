"""Independently-constructed synthetic Phase 2B data (docs/PRIVATE-DATA-POLICY.md).

Every contract, invoice, amount, date and item key here is invented to
exercise the CONFIRMED Phase 2B rules (R001/R002/R003/R005/R006/R007) and
the golden S2B-01..S2B-08 scenarios — never derived from a non-public
file. The *shape* (a partial receipt, a full reversal, a contract-level
candidate, a duplicate-accrual guard, an item-match blocker, a fresh
recompute) comes from the rule scenarios themselves.

Scenario map (see tests/golden/synthetic-v1/test_period_close_baseline.py):
  PO-CLOSE-001  S2B-01 partial reversal  (historical 100/1200.00, invoiced 35/455.00)
  PO-CLOSE-002  S2B-02 full reversal     (historical 40/880.00, invoiced 40/840.00)
  PO-CLOSE-003  S2B-04 new item-level accrual (basis 624.00)
  PO-CLOSE-004  S2B-05 contract-level candidate (basis 735.00)
  PO-CLOSE-005  S2B-06 duplicate guard   (PARTIALLY_REVERSED 500.00, reversed 200.00)
  PO-CLOSE-006  S2B-07 item-match blocker (contract-level invoice, no item match)
  PO-CLOSE-007  S2B-08 fresh recompute   (candidate that must vanish after invoice)
  PO-CLOSE-008  MISSING_ACCRUAL_BASIS diagnostic blocker
"""

from __future__ import annotations

import json
from pathlib import Path

from fixtures.synthetic.scenarios import BUYER, CONTRACT_HEADERS

PHASE2B_CONTRACT_HEADERS = CONTRACT_HEADERS

PHASE2B_CONTRACT_ROWS = [
    [1, "PO-CLOSE-001", "SupplierCloseAlpha", BUYER, 1300.00, "EXP-CLOSE-001"],
    [2, "PO-CLOSE-002", "SupplierCloseBeta", BUYER, 880.00, "EXP-CLOSE-002"],
    [3, "PO-CLOSE-003", "SupplierCloseGamma", BUYER, 624.00, "EXP-CLOSE-003"],
    [4, "PO-CLOSE-004", "SupplierCloseDelta", BUYER, 735.00, "EXP-CLOSE-004"],
    [5, "PO-CLOSE-005", "SupplierCloseEpsilon", BUYER, 500.00, "EXP-CLOSE-005"],
    [6, "PO-CLOSE-006", "SupplierCloseZeta", BUYER, 1000.00, "EXP-CLOSE-006"],
    [7, "PO-CLOSE-007", "SupplierCloseEta", BUYER, 900.00, "EXP-CLOSE-007"],
    [8, "PO-CLOSE-008", "SupplierCloseTheta", BUYER, 300.00, "EXP-CLOSE-008"],
]

# Column order matches tests/conftest.py INVOICE_HEADERS.
PHASE2B_INVOICE_ROWS = [
    # Invoice for PO-CLOSE-001 — S2B-01 partial receipt (35 of 100, net 455.00)
    ["Tmpl", " ", "数电票（普通发票）", "2031-03-15", None, "DIGITAL-CLOSE-001", "SupplierCloseAlpha",
     "Alpha Widget", None, "件", 35, "13.00", 455.00, None, None, 455.00, "正常", 455.00, 0, 455.00],
    # Invoice for PO-CLOSE-002 — S2B-02 full receipt (40 of 40, net 840.00)
    ["Tmpl", " ", "数电票（普通发票）", "2031-03-18", None, "DIGITAL-CLOSE-002", "SupplierCloseBeta",
     "Beta Widget", None, "件", 40, "21.00", 840.00, None, None, 840.00, "正常", 840.00, 0, 840.00],
    # Invoice for PO-CLOSE-005 — drove the go-live partial reversal (40, net 200.00)
    ["Tmpl", " ", "数电票（普通发票）", "2031-02-15", None, "DIGITAL-CLOSE-005", "SupplierCloseEpsilon",
     "Epsilon Widget", None, "件", 40, "5.00", 200.00, None, None, 200.00, "正常", 200.00, 0, 200.00],
    # Invoice for PO-CLOSE-006 — S2B-07 contract-level match only (no item match)
    ["Tmpl", " ", "数电票（普通发票）", "2031-03-10", None, "DIGITAL-CLOSE-006", "SupplierCloseZeta",
     "Zeta Widget", None, "件", 50, "19.00", 950.00, None, None, 950.00, "正常", 950.00, 0, 950.00],
    # Invoice for PO-CLOSE-007 — S2B-08 added after run 1
    ["Tmpl", " ", "数电票（普通发票）", "2031-03-20", None, "DIGITAL-CLOSE-007", "SupplierCloseEta",
     "Eta Widget", None, "件", 30, "10.00", 300.00, None, None, 300.00, "正常", 300.00, 0, 300.00],
]

HISTORICAL_PERIOD = "2031-02"
CLOSE_PERIOD = "2031-03"

CLOSE_FACT_PACK = {
    "version": 1,
    "contract_items": [
        {
            "contract_selector": {"contract_no": "PO-CLOSE-001", "counterparty": "SupplierCloseAlpha"},
            "source_item_key": "ITEM-A",
            "product_name": "Alpha Widget",
            "quantity": 100,
            "unit": "件",
        },
        {
            "contract_selector": {"contract_no": "PO-CLOSE-002", "counterparty": "SupplierCloseBeta"},
            "source_item_key": "ITEM-A",
            "product_name": "Beta Widget",
            "quantity": 40,
            "unit": "件",
        },
        {
            "contract_selector": {"contract_no": "PO-CLOSE-003", "counterparty": "SupplierCloseGamma"},
            "source_item_key": "ITEM-A",
            "product_name": "Gamma Widget",
            "quantity": 60,
            "unit": "件",
        },
        {
            "contract_selector": {"contract_no": "PO-CLOSE-005", "counterparty": "SupplierCloseEpsilon"},
            "source_item_key": "ITEM-A",
            "product_name": "Epsilon Widget",
            "quantity": 100,
            "unit": "件",
        },
        {
            "contract_selector": {"contract_no": "PO-CLOSE-006", "counterparty": "SupplierCloseZeta"},
            "source_item_key": "ITEM-A",
            "product_name": "Zeta Widget",
            "quantity": 50,
            "unit": "件",
        },
    ],
    "cost_recognition_facts": [
        {
            "contract_selector": {"contract_no": "PO-CLOSE-003", "counterparty": "SupplierCloseGamma"},
            "recognition_date": "2031-02-28",
            "basis": "MANUAL_CONFIRMED",
        },
        {
            "contract_selector": {"contract_no": "PO-CLOSE-004", "counterparty": "SupplierCloseDelta"},
            "recognition_date": "2031-02-28",
            "basis": "MANUAL_CONFIRMED",
        },
        {
            "contract_selector": {"contract_no": "PO-CLOSE-005", "counterparty": "SupplierCloseEpsilon"},
            "recognition_date": "2031-02-28",
            "basis": "MANUAL_CONFIRMED",
        },
        {
            "contract_selector": {"contract_no": "PO-CLOSE-007", "counterparty": "SupplierCloseEta"},
            "recognition_date": "2031-02-28",
            "basis": "MANUAL_CONFIRMED",
        },
        {
            "contract_selector": {"contract_no": "PO-CLOSE-008", "counterparty": "SupplierCloseTheta"},
            "recognition_date": "2031-02-28",
            "basis": "MANUAL_CONFIRMED",
        },
    ],
    "accrual_basis_facts": [
        {
            "scope_type": "CONTRACT_ITEM",
            "contract_selector": {"contract_no": "PO-CLOSE-003", "counterparty": "SupplierCloseGamma"},
            "source_item_key": "ITEM-A",
            "quantity": 60,
            "estimated_cost": 624.00,
            "basis": "MANUAL_CONFIRMED",
        },
        {
            "scope_type": "CONTRACT",
            "contract_selector": {"contract_no": "PO-CLOSE-004", "counterparty": "SupplierCloseDelta"},
            "estimated_cost": 735.00,
            "basis": "MANUAL_CONFIRMED",
        },
        {
            "scope_type": "CONTRACT",
            "contract_selector": {"contract_no": "PO-CLOSE-007", "counterparty": "SupplierCloseEta"},
            "estimated_cost": 900.00,
            "basis": "MANUAL_CONFIRMED",
        },
    ],
    "historical_accrual_facts": [
        {
            "source_period": HISTORICAL_PERIOD,
            "contract_selector": {"contract_no": "PO-CLOSE-001", "counterparty": "SupplierCloseAlpha"},
            "source_item_key": "ITEM-A",
            "quantity": 100,
            "estimated_cost": 1200.00,
            "basis": "MANUAL_CONFIRMED",
            "confirmed_at": "2031-02-28",
        },
        {
            "source_period": HISTORICAL_PERIOD,
            "contract_selector": {"contract_no": "PO-CLOSE-002", "counterparty": "SupplierCloseBeta"},
            "source_item_key": "ITEM-A",
            "quantity": 40,
            "estimated_cost": 880.00,
            "basis": "MANUAL_CONFIRMED",
            "confirmed_at": "2031-02-28",
        },
        {
            "source_period": HISTORICAL_PERIOD,
            "contract_selector": {"contract_no": "PO-CLOSE-005", "counterparty": "SupplierCloseEpsilon"},
            "source_item_key": "ITEM-A",
            "quantity": 100,
            "estimated_cost": 500.00,
            "basis": "MANUAL_CONFIRMED",
            "confirmed_at": "2031-02-28",
        },
        {
            "source_period": HISTORICAL_PERIOD,
            "contract_selector": {"contract_no": "PO-CLOSE-006", "counterparty": "SupplierCloseZeta"},
            "source_item_key": "ITEM-A",
            "quantity": 50,
            "estimated_cost": 1000.00,
            "basis": "MANUAL_CONFIRMED",
            "confirmed_at": "2031-02-28",
        },
    ],
    "invoice_item_allocations": [
        {
            "invoice": {"external_key": "DIGITAL-CLOSE-001", "line_no": 1},
            "contract_selector": {"contract_no": "PO-CLOSE-001", "counterparty": "SupplierCloseAlpha"},
            "source_item_key": "ITEM-A",
            "allocated_quantity": 35,
            "allocated_net_amount": 455.00,
            "confirmation_type": "MANUAL_CONFIRMED",
        },
        {
            "invoice": {"external_key": "DIGITAL-CLOSE-002", "line_no": 1},
            "contract_selector": {"contract_no": "PO-CLOSE-002", "counterparty": "SupplierCloseBeta"},
            "source_item_key": "ITEM-A",
            "allocated_quantity": 40,
            "allocated_net_amount": 840.00,
            "confirmation_type": "MANUAL_CONFIRMED",
        },
        {
            "invoice": {"external_key": "DIGITAL-CLOSE-005", "line_no": 1},
            "contract_selector": {"contract_no": "PO-CLOSE-005", "counterparty": "SupplierCloseEpsilon"},
            "source_item_key": "ITEM-A",
            "allocated_quantity": 40,
            "allocated_net_amount": 200.00,
            "confirmation_type": "MANUAL_CONFIRMED",
        },
    ],
    "accrual_reversals": [
        {
            "period": HISTORICAL_PERIOD,
            "contract_selector": {"contract_no": "PO-CLOSE-005", "counterparty": "SupplierCloseEpsilon"},
            "source_item_key": "ITEM-A",
            "accrual_source_period": HISTORICAL_PERIOD,
            "invoice": {"external_key": "DIGITAL-CLOSE-005", "line_no": 1},
            "reversed_quantity": 40,
            "reversed_estimated_cost": 200.00,
        }
    ],
}


def write_phase2b_close_facts(path: Path) -> Path:
    """Write the synthetic Close Fact Pack to *path* and return it."""
    path.write_text(json.dumps(CLOSE_FACT_PACK, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_recompute_fact_pack(path: Path) -> Path:
    """A narrow pack for the S2B-08 fresh-recompute scenario: only
    PO-CLOSE-007 gets cost recognition + a contract-level basis."""
    pack = {
        "version": 1,
        "contract_items": [],
        "cost_recognition_facts": [
            {
                "contract_selector": {"contract_no": "PO-CLOSE-007", "counterparty": "SupplierCloseEta"},
                "recognition_date": "2031-02-28",
                "basis": "MANUAL_CONFIRMED",
            }
        ],
        "accrual_basis_facts": [
            {
                "scope_type": "CONTRACT",
                "contract_selector": {"contract_no": "PO-CLOSE-007", "counterparty": "SupplierCloseEta"},
                "estimated_cost": 900.00,
                "basis": "MANUAL_CONFIRMED",
            }
        ],
        "historical_accrual_facts": [],
        "invoice_item_allocations": [],
        "accrual_reversals": [],
    }
    path.write_text(json.dumps(pack, indent=2, ensure_ascii=False), encoding="utf-8")
    return path

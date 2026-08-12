"""Golden import baseline against the synthetic contract ledger fixture
(fixtures/synthetic/scenarios.py) — the public counterpart of the
private acceptance suite's P1_IMPORT scenario. See
docs/PRIVATE-DATA-POLICY.md.
"""

import json
from decimal import Decimal
from pathlib import Path

from bel.application.import_contract_ledger import import_contract_ledger

BASELINE_PATH = Path(__file__).parent / "import-baseline.json"


def test_synthetic_ledger_import_matches_baseline(db_session, synthetic_ledger_path):
    baseline = json.loads(BASELINE_PATH.read_text())

    result = import_contract_ledger(db_session, synthetic_ledger_path)

    assert result.is_reimport is False
    assert len(result.sheets) == baseline["sheets_detected"]
    assert result.primary_sheet == baseline["primary_sheet"]
    assert result.primary_sheet_columns == baseline["columns"]
    assert result.business_rows == baseline["business_rows"]
    assert result.blank_trailing_rows == baseline["blank_trailing_rows"]
    assert result.contracts_created == baseline["contracts_created"]
    assert result.contract_items_created == baseline["contract_items_created"]
    assert result.distinct_sellers == baseline["distinct_sellers"]
    assert result.distinct_buyers == baseline["distinct_buyers"]
    assert result.distinct_owners == baseline["distinct_owners"]
    assert result.distinct_customs_receivers == baseline["distinct_customs_receivers"]
    assert result.missing_export_contract_no == baseline["missing_export_contract_no"]
    assert len(result.business_key_conflicts) == baseline["business_key_conflicts"]

    # Decimal, not float — see docs/PHASE1-DECISIONS.md.
    assert result.gross_amount_total == Decimal(baseline["gross_amount_total"])

    contract_nos_in_conflict = {c.contract_no for c in result.business_key_conflicts}
    assert len(contract_nos_in_conflict) == baseline["duplicate_contract_no_groups"]


def test_synthetic_ledger_reimport_is_idempotent(db_session, synthetic_ledger_path):
    baseline = json.loads(BASELINE_PATH.read_text())

    first = import_contract_ledger(db_session, synthetic_ledger_path)
    second = import_contract_ledger(db_session, synthetic_ledger_path)

    assert first.contracts_created == baseline["contracts_created"]
    assert second.is_reimport is True
    assert second.contracts_created == 0
    assert second.evidence_document_id == first.evidence_document_id

"""Independent Core Completion checks over a forward-migrated PostgreSQL DB.

All evidence and business values are independently synthetic.
"""
from datetime import date
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from bel.application.contract_item_facts import create_contract_item_fact
from bel.application.invoice_preparation import ApplicableFxRateEvidence
from bel.application.invoice_preparation_workbench import get_invoice_preparation_workbench
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.infrastructure.persistence.database import DatabaseRuntime
from tests.integration.test_contract_item_facts import _make_contract, _make_fragment, NOW

pytestmark = pytest.mark.postgres


@pytest.fixture
def core_pg(postgres_url, monkeypatch):
    monkeypatch.setenv("BEL_DATABASE_URL", postgres_url)
    engine = create_engine(postgres_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()
    command.upgrade(Config("alembic.ini"), "head")
    runtime = DatabaseRuntime(postgres_url)
    yield runtime
    runtime.engine.dispose()


def test_forward_migration_and_preparation_roundtrip(core_pg):
    with core_pg.session_factory() as session:
        fragment = _make_fragment(session)
        procurement = _make_contract(session, fragment.id)
        create_contract_item_fact(
            session, contract_id=procurement.id, source_item_key="gate-line",
            fields={"product_name": "Synthetic widget", "quantity": Decimal("12"),
                    "unit": "piece", "tax_classification_code": "SYNTHETIC-CODE"},
            source_fragment_id=fragment.id, created_at=NOW,
        )
        create_sales_contract_fact(
            session, our_entity="Synthetic exporter", sales_contract_no="GATE-SALES",
            fields={"customer": "Synthetic customer", "currency": "USD",
                    "gross_amount": Decimal("125"), "quantity": Decimal("9"), "unit": "piece"},
            source_fragment_id=fragment.id, created_at=NOW,
        )
        session.commit()
        fx_id = fragment.id
    # Fresh session proves persisted fields, not dataclass-only propagation.
    with core_pg.session_factory() as session:
        fx = ApplicableFxRateEvidence("SAFE", date(2030, 8, 30), Decimal("7.20"), fx_id)
        workbench = get_invoice_preparation_workbench(
            session, invoice_month=date(2030, 9, 1), fx_rate_evidence=(fx,)
        )
        purchase = workbench.supplier_report.decisions[0]
        item = purchase.item_preparations[0]
        assert item.expected_purchase_invoice_quantity == Decimal("12")
        assert item.tax_classification_code == "SYNTHETIC-CODE"
        assert item.tax_classification_code_status == "CONFIRMED"
        sales = workbench.sales_report.decisions[0]
        assert sales.expected_quantity == Decimal("9")
        assert sales.invoice_note_data.expected_invoice_cny == Decimal("900.00")
        assert sales.invoice_note_data.applicable_fx_date == date(2030, 8, 30)
        assert sales.amount_check.fx_provenance_fragment_id == fx_id
        assert get_invoice_preparation_workbench(
            session, invoice_month=date(2030, 9, 1), fx_rate_evidence=(fx,)
        ) == workbench


def test_previous_head_upgrade_downgrade_and_drift(core_pg):
    config = Config("alembic.ini")
    command.downgrade(config, "6aa25aa4e81f")
    assert "quantity" not in {c["name"] for c in inspect(core_pg.engine).get_columns("sales_contract_revisions")}
    command.upgrade(config, "head")
    assert {"quantity", "unit"} <= {c["name"] for c in inspect(core_pg.engine).get_columns("sales_contract_revisions")}
    assert "tax_classification_code" in {c["name"] for c in inspect(core_pg.engine).get_columns("contract_item_revisions")}
    command.check(config)

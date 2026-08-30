"""Automated migration tests for
f1a2b3c4d5e6_procurement_sales_link (Phase 2D.1-R3a Slice 2). Like the
Slice 1 SalesContract migration, there is no pre-existing
ProcurementSalesLink data anywhere (this object did not exist before
this round) — so this migration has nothing of its own to migrate. What
it MUST preserve is every existing table (contracts, sales_contracts,
shipments, cost_recognition_facts, ...) completely untouched.

Covers:
- fresh DB -> head (procurement_sales_links / procurement_sales_link_corrections
  tables + constraints + the one-current trigger exist)
- Slice 1 baseline (7393fdb9c4d2) WITH a pre-existing Contract and
  SalesContract -> head: both survive unchanged; no link is fabricated
- head -> Slice 1 baseline (downgrade): the new tables and trigger are
  dropped, the pre-existing Contract/SalesContract rows untouched
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine

from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    ProcurementSalesLinkRepository,
    SalesContractRepository,
)

REPO_ROOT = Path(__file__).parent.parent.parent
SLICE1_REVISION = "7393fdb9c4d2"
HEAD_REVISION = "f1a2b3c4d5e6"


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )


def test_fresh_database_upgrades_to_head(tmp_path):
    db_path = tmp_path / "fresh.db"
    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    con = create_engine(f"sqlite:///{db_path}").connect()
    tables = {row[0] for row in con.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert {"procurement_sales_links", "procurement_sales_link_corrections"} <= tables

    link_columns = {row[1] for row in con.execute(sa.text("PRAGMA table_info(procurement_sales_links)"))}
    assert {
        "id", "procurement_contract_id", "sales_contract_id", "source_fragment_id", "confirmation_type", "created_at",
    } <= link_columns
    correction_columns = {row[1] for row in con.execute(sa.text("PRAGMA table_info(procurement_sales_link_corrections)"))}
    assert {
        "id", "superseded_link_id", "replacement_link_id", "source_fragment_id", "confirmation_type", "created_at",
    } <= correction_columns

    unique_indexes = [row[1] for row in con.execute(sa.text("PRAGMA index_list(procurement_sales_link_corrections)")) if row[2]]
    unique_columns = {
        col_row[2]
        for idx_name in unique_indexes
        for col_row in con.execute(sa.text(f"PRAGMA index_info('{idx_name}')"))
    }
    assert "superseded_link_id" in unique_columns

    triggers = {row[0] for row in con.execute(sa.text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    assert "trg_procurement_sales_links_one_current" in triggers
    con.close()


def test_slice1_baseline_with_contract_and_sales_contract_upgrades_to_head_untouched(tmp_path):
    db_path = tmp_path / "slice1-baseline.db"
    assert _alembic(db_path, "upgrade", SLICE1_REVISION).returncode == 0

    from bel.application.sales_contract_facts import create_sales_contract_fact
    from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
    from bel.infrastructure.persistence.repositories import EvidenceRepository

    now = datetime(2026, 1, 1)
    engine = make_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="f", sha256="e" * 64, source_type="t", imported_at=now)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None, row_number=None, locator_json={}, raw_data={}, created_at=now,
        )
        EvidenceRepository(session).add_fragment(frag)
        session.flush()
        # SLICE1_REVISION predates the Phase 2D.1-R5 Contract
        # anchor+revision migration — contracts is still the OLD flat
        # shape at this baseline, so it must be seeded with raw SQL, not
        # the current (head-schema) ContractRepository.
        contract_id = uuid.uuid4()
        session.execute(
            sa.text(
                "INSERT INTO contracts (id, contract_no, contract_type, counterparty, buyer, gross_amount, "
                "currency, contract_date, current_source_fragment_id, created_at, updated_at) VALUES "
                "(:id, :contract_no, :contract_type, :counterparty, :buyer, :gross_amount, :currency, "
                ":contract_date, :current_source_fragment_id, :created_at, :updated_at)"
            ),
            {
                "id": contract_id.hex, "contract_no": "C-MIGRATION-PSL-1", "contract_type": None,
                "counterparty": "Supplier", "buyer": "Buyer", "gross_amount": "100.00", "currency": "CNY",
                "contract_date": None, "current_source_fragment_id": frag.id.hex, "created_at": now,
                "updated_at": now,
            },
        )
        session.flush()
        sales_contract = create_sales_contract_fact(
            session, our_entity="Entity A", sales_contract_no="SC-MIGRATION-PSL", fields={"customer": "Customer Co"},
            source_fragment_id=frag.id, created_at=now,
        ).sales_contract
        session.commit()
        sales_contract_id = sales_contract.id

    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    engine = make_engine(str(db_path))
    with make_session_factory(engine)() as session:
        assert ContractRepository(session).get(contract_id) is not None
        preserved_sc = SalesContractRepository(session).get(sales_contract_id)
        assert preserved_sc is not None
        assert preserved_sc.customer == "Customer Co"

        # No link is ever fabricated by the migration itself.
        assert ProcurementSalesLinkRepository(session).list_episodes(contract_id, sales_contract_id) == []


def test_downgrade_drops_link_tables_and_trigger_but_preserves_existing_data(tmp_path):
    db_path = tmp_path / "downgrade.db"
    assert _alembic(db_path, "upgrade", "head").returncode == 0

    from bel.application.procurement_sales_link import add_procurement_sales_link
    from bel.application.sales_contract_facts import create_sales_contract_fact
    from bel.domain.contract import Contract
    from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
    from bel.domain.procurement_sales_link import ConfirmationType
    from bel.infrastructure.persistence.repositories import EvidenceRepository

    now = datetime(2026, 1, 1)
    engine = make_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="f", sha256="f" * 64, source_type="t", imported_at=now)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None, row_number=None, locator_json={}, raw_data={}, created_at=now,
        )
        EvidenceRepository(session).add_fragment(frag)
        session.flush()
        contract = Contract(
            id=uuid.uuid4(), contract_no="C-DOWNGRADE-PSL", contract_type=None, counterparty="Supplier",
            buyer="Buyer", gross_amount=Decimal("1"), currency="CNY", contract_date=None,
            current_source_fragment_id=frag.id, created_at=now, updated_at=now,
        )
        ContractRepository(session).add(contract)
        session.flush()
        sales_contract = create_sales_contract_fact(
            session, our_entity="Entity A", sales_contract_no="SC-DOWNGRADE-PSL", fields={},
            source_fragment_id=frag.id, created_at=now,
        ).sales_contract
        session.commit()

        add_procurement_sales_link(
            session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
            source_fragment_id=frag.id, confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=now,
        )
        session.commit()
        contract_id, sales_contract_id = contract.id, sales_contract.id

    result = _alembic(db_path, "downgrade", SLICE1_REVISION)
    assert result.returncode == 0, result.stderr

    con = create_engine(f"sqlite:///{db_path}").connect()
    tables = {row[0] for row in con.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "procurement_sales_links" not in tables
    assert "procurement_sales_link_corrections" not in tables
    triggers = {row[0] for row in con.execute(sa.text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    assert "trg_procurement_sales_links_one_current" not in triggers

    contract_row = con.execute(
        sa.text("SELECT contract_no FROM contracts WHERE id = :id"), {"id": contract_id.hex}
    ).fetchone()
    sales_contract_row = con.execute(
        sa.text("SELECT sales_contract_no FROM sales_contracts WHERE id = :id"), {"id": sales_contract_id.hex}
    ).fetchone()
    con.close()
    assert contract_row is not None
    assert contract_row.contract_no == "C-DOWNGRADE-PSL"  # pre-existing Contract survives untouched
    assert sales_contract_row is not None
    assert sales_contract_row.sales_contract_no == "SC-DOWNGRADE-PSL"

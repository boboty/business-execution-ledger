"""Automated migration tests for
7393fdb9c4d2_sales_contract_foundation (Phase 2D.1-R3a Slice 1). Like
the R2 Shipment migration, there is no pre-existing SalesContract data
anywhere (this object did not exist before this round) — so this
migration has nothing of its own to migrate. What it MUST preserve is
every existing table (contracts, contract_items, shipments,
cost_recognition_facts, ...) completely untouched.

Covers:
- fresh DB -> head (sales_contracts/sales_contract_revisions tables +
  constraints exist)
- R2 baseline (147d94b436e0) WITH a pre-existing Shipment -> head: the
  Shipment survives unchanged; no SalesContract is fabricated for it
- head -> R2 baseline (downgrade): sales_contracts/sales_contract_revisions
  dropped, the pre-existing Shipment/Contract rows untouched
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.repositories import ContractRepository, SalesContractRepository, ShipmentRepository

REPO_ROOT = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.skip(
    reason="Tests the pre-2D.1-P SQLite migration chain (migrations/versions/), which is frozen "
    "legacy history and no longer wired into active Alembic tooling as of the Phase 2D.1-P "
    "PostgreSQL rebaseline — see docs/PERSISTENCE-MIGRATION-POLICY.md."
)
R2_REVISION = "147d94b436e0"
HEAD_REVISION = "7393fdb9c4d2"


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
    assert {"sales_contracts", "sales_contract_revisions"} <= tables

    columns = {row[1] for row in con.execute(sa.text("PRAGMA table_info(sales_contracts)"))}
    assert {"id", "our_entity", "sales_contract_no", "created_at"} <= columns
    revision_columns = {row[1] for row in con.execute(sa.text("PRAGMA table_info(sales_contract_revisions)"))}
    assert {
        "id", "sales_contract_id", "revision_type", "customer", "currency", "gross_amount",
        "contract_date", "source_fragment_id", "superseded_by_revision_id", "asserted_field_names", "created_at",
    } <= revision_columns

    indexes = {row[1] for row in con.execute(sa.text("PRAGMA index_list(sales_contract_revisions)"))}
    assert "uq_sales_contract_revisions_one_current" in indexes
    assert "uq_sales_contract_revisions_one_initial" in indexes
    con.close()


def test_r2_baseline_with_shipment_upgrades_to_head_untouched(tmp_path):
    db_path = tmp_path / "r2-baseline.db"
    assert _alembic(db_path, "upgrade", R2_REVISION).returncode == 0

    from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
    from bel.infrastructure.persistence.repositories import EvidenceRepository

    now = datetime(2026, 1, 1)
    engine = make_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="f", sha256="c" * 64, source_type="t", imported_at=now)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None, row_number=None, locator_json={}, raw_data={}, created_at=now,
        )
        EvidenceRepository(session).add_fragment(frag)
        session.flush()
        # R2_REVISION predates the Phase 2D.1-R5 Contract anchor+revision
        # migration — contracts is still the OLD flat shape at this
        # baseline, so it must be seeded with raw SQL, not the current
        # (head-schema) ContractRepository.
        contract_id = uuid.uuid4()
        session.execute(
            sa.text(
                "INSERT INTO contracts (id, contract_no, contract_type, counterparty, buyer, gross_amount, "
                "currency, contract_date, current_source_fragment_id, created_at, updated_at) VALUES "
                "(:id, :contract_no, :contract_type, :counterparty, :buyer, :gross_amount, :currency, "
                ":contract_date, :current_source_fragment_id, :created_at, :updated_at)"
            ),
            {
                "id": contract_id.hex, "contract_no": "C-MIGRATION-SC-1", "contract_type": None,
                "counterparty": "Supplier", "buyer": "Buyer", "gross_amount": "100.00", "currency": "CNY",
                "contract_date": None, "current_source_fragment_id": frag.id.hex, "created_at": now,
                "updated_at": now,
            },
        )
        session.flush()
        # create_shipment_fact (current code) reads Contract through
        # ContractRepository, which now assumes the HEAD schema — at this
        # R2 baseline it does not exist yet, so the Shipment anchor +
        # INITIAL revision are seeded with raw SQL too, matching
        # ShipmentModel/ShipmentRevisionModel's shape (unchanged since R2).
        shipment_id = uuid.uuid4()
        session.execute(
            sa.text(
                "INSERT INTO shipments (id, contract_id, external_reference, execution_date, created_at) "
                "VALUES (:id, :contract_id, :external_reference, :execution_date, :created_at)"
            ),
            {
                "id": shipment_id.hex, "contract_id": contract_id.hex, "external_reference": "EXP-MIGRATION-SC",
                "execution_date": date(2031, 3, 1), "created_at": now,
            },
        )
        session.execute(
            sa.text(
                "INSERT INTO shipment_revisions (id, shipment_id, revision_type, contract_item_id, quantity, "
                "source_fragment_id, superseded_by_revision_id, asserted_field_names, created_at) VALUES "
                "(:id, :shipment_id, 'INITIAL', NULL, :quantity, :source_fragment_id, NULL, NULL, :created_at)"
            ),
            {
                "id": uuid.uuid4().hex, "shipment_id": shipment_id.hex, "quantity": "10.0000",
                "source_fragment_id": frag.id.hex, "created_at": now,
            },
        )
        session.commit()

    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    engine = make_engine(str(db_path))
    with make_session_factory(engine)() as session:
        assert ContractRepository(session).get(contract_id) is not None
        preserved_shipment = ShipmentRepository(session).get(shipment_id)
        assert preserved_shipment is not None
        assert preserved_shipment.quantity == Decimal("10")

        # No SalesContract is ever fabricated by the migration itself.
        assert SalesContractRepository(session).list_all() == []


def test_downgrade_drops_sales_contract_tables_but_preserves_existing_data(tmp_path):
    db_path = tmp_path / "downgrade.db"
    assert _alembic(db_path, "upgrade", "head").returncode == 0

    from bel.application.sales_contract_facts import create_sales_contract_fact
    from bel.domain.contract import Contract
    from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
    from bel.infrastructure.persistence.repositories import EvidenceRepository

    now = datetime(2026, 1, 1)
    engine = make_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="f", sha256="d" * 64, source_type="t", imported_at=now)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None, row_number=None, locator_json={}, raw_data={}, created_at=now,
        )
        EvidenceRepository(session).add_fragment(frag)
        session.flush()
        contract = Contract(
            id=uuid.uuid4(), contract_no="C-DOWNGRADE-SC", contract_type=None, counterparty="Supplier",
            buyer="Buyer", gross_amount=Decimal("1"), currency="CNY", contract_date=None,
            current_source_fragment_id=frag.id, created_at=now, updated_at=now,
        )
        ContractRepository(session).add(contract)
        session.flush()

        create_sales_contract_fact(
            session, our_entity="Entity A", sales_contract_no="SC-DOWNGRADE", fields={"customer": "Customer Co"},
            source_fragment_id=frag.id, created_at=now,
        )
        session.commit()
        contract_id = contract.id

    result = _alembic(db_path, "downgrade", R2_REVISION)
    assert result.returncode == 0, result.stderr

    con = create_engine(f"sqlite:///{db_path}").connect()
    tables = {row[0] for row in con.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "sales_contracts" not in tables
    assert "sales_contract_revisions" not in tables
    row = con.execute(
        sa.text("SELECT contract_no, buyer FROM contracts WHERE id = :id"), {"id": contract_id.hex}
    ).fetchone()
    con.close()
    assert row is not None
    assert row.contract_no == "C-DOWNGRADE-SC"  # the pre-existing Contract survives untouched

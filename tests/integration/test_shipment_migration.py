"""Automated migration tests for
147d94b436e0_shipment_minimum_vertical_slice (Phase 2D.1-R2). Unlike the
R1 ContractItem migration, there is no pre-existing Shipment data
anywhere (V1-SCOPE.md section 2.3: no Shipment/Export implementation
existed before this round) — so this migration has nothing of its own to
migrate. What it MUST preserve is existing `cost_recognition_facts` data
untouched, adding only a NULL `shipment_id` to every pre-existing row.

Covers:
- fresh DB -> head (shipments/shipment_revisions tables + constraints exist)
- R1 baseline (db1c3258569e) WITH a pre-existing CostRecognitionFact ->
  head: the fact survives unchanged, with shipment_id = NULL (never
  fabricated)
- head -> R1 baseline (downgrade): shipment_id column removed, the
  CostRecognitionFact row itself untouched; shipments/shipment_revisions
  dropped
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
from bel.infrastructure.persistence.repositories import ContractRepository, CostRecognitionFactRepository, ShipmentRepository

REPO_ROOT = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.skip(
    reason="Tests the pre-2D.1-P SQLite migration chain (migrations/versions/), which is frozen "
    "legacy history and no longer wired into active Alembic tooling as of the Phase 2D.1-P "
    "PostgreSQL rebaseline — see docs/PERSISTENCE-MIGRATION-POLICY.md."
)
R1_REVISION = "db1c3258569e"
HEAD_REVISION = "147d94b436e0"


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )


def _r1_tables(meta: sa.MetaData) -> dict[str, sa.Table]:
    """The R1-era shape, declared with real sa.Uuid()/sa.DateTime() types
    (NOT raw sqlite3) so inserted values round-trip through SQLAlchemy's
    bind/result processors exactly as production code would have written
    them."""
    doc = sa.Table(
        "evidence_documents", meta,
        sa.Column("id", sa.Uuid()), sa.Column("file_name", sa.String()), sa.Column("sha256", sa.String()),
        sa.Column("source_type", sa.String()), sa.Column("imported_at", sa.DateTime()),
    )
    frag = sa.Table(
        "evidence_fragments", meta,
        sa.Column("id", sa.Uuid()), sa.Column("evidence_document_id", sa.Uuid()),
        sa.Column("fragment_kind", sa.String()), sa.Column("sheet_name", sa.String()),
        sa.Column("row_number", sa.Integer()), sa.Column("locator_json", sa.JSON()),
        sa.Column("raw_data", sa.JSON()), sa.Column("created_at", sa.DateTime()),
    )
    contracts = sa.Table(
        "contracts", meta,
        sa.Column("id", sa.Uuid()), sa.Column("contract_no", sa.String()), sa.Column("contract_type", sa.String()),
        sa.Column("counterparty", sa.String()), sa.Column("buyer", sa.String()),
        sa.Column("gross_amount", sa.Numeric(18, 2)), sa.Column("currency", sa.String()),
        sa.Column("contract_date", sa.Date()), sa.Column("current_source_fragment_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime()), sa.Column("updated_at", sa.DateTime()),
    )
    cost_recognition_facts = sa.Table(
        "cost_recognition_facts", meta,
        sa.Column("id", sa.Uuid()), sa.Column("contract_id", sa.Uuid()), sa.Column("recognition_date", sa.Date()),
        sa.Column("basis", sa.String()), sa.Column("source_fragment_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime()),
    )
    return {
        "evidence_documents": doc, "evidence_fragments": frag, "contracts": contracts,
        "cost_recognition_facts": cost_recognition_facts,
    }


def _seed_r1_data(db_path: Path) -> dict:
    engine = create_engine(f"sqlite:///{db_path}")
    meta = sa.MetaData()
    tables = _r1_tables(meta)
    now = datetime(2026, 1, 1)

    doc_id, frag_id, contract_id, fact_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    with engine.begin() as conn:
        conn.execute(tables["evidence_documents"].insert().values(
            id=doc_id, file_name="f.json", sha256="a" * 64, source_type="test", imported_at=now
        ))
        conn.execute(tables["evidence_fragments"].insert().values(
            id=frag_id, evidence_document_id=doc_id, fragment_kind="MANUAL_FACT", sheet_name=None,
            row_number=None, locator_json={}, raw_data={}, created_at=now,
        ))
        conn.execute(tables["contracts"].insert().values(
            id=contract_id, contract_no="C-MIGRATION-SHIP-1", contract_type=None, counterparty="Supplier",
            buyer="Buyer", gross_amount=Decimal("100.00"), currency="CNY", contract_date=None,
            current_source_fragment_id=frag_id, created_at=now, updated_at=now,
        ))
        conn.execute(tables["cost_recognition_facts"].insert().values(
            id=fact_id, contract_id=contract_id, recognition_date=date(2031, 3, 1), basis="MANUAL_CONFIRMED",
            source_fragment_id=frag_id, created_at=now,
        ))
    return {"contract_id": contract_id, "fact_id": fact_id, "frag_id": frag_id}


def test_fresh_database_upgrades_to_head(tmp_path):
    db_path = tmp_path / "fresh.db"
    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    con = create_engine(f"sqlite:///{db_path}").connect()
    tables = {row[0] for row in con.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
    con.close()
    assert {"shipments", "shipment_revisions"} <= tables


def test_r1_baseline_with_cost_recognition_fact_upgrades_to_head(tmp_path):
    db_path = tmp_path / "r1-baseline.db"
    assert _alembic(db_path, "upgrade", R1_REVISION).returncode == 0
    ids = _seed_r1_data(db_path)

    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    engine = make_engine(str(db_path))
    with make_session_factory(engine)() as session:
        facts = CostRecognitionFactRepository(session).list_all()
        fact = [f for f in facts if f.id == ids["fact_id"]][0]
        assert fact.contract_id == ids["contract_id"]
        assert fact.basis == "MANUAL_CONFIRMED"
        assert fact.source_fragment_id == ids["frag_id"]
        assert fact.shipment_id is None  # never fabricated

        assert ShipmentRepository(session).list_all() == []
        assert ContractRepository(session).get(ids["contract_id"]) is not None


def test_downgrade_removes_shipment_id_but_preserves_cost_recognition_fact(tmp_path):
    db_path = tmp_path / "downgrade.db"
    assert _alembic(db_path, "upgrade", "head").returncode == 0

    from bel.application.shipment_facts import create_shipment_fact
    from bel.domain.accrual import CostRecognitionFact
    from bel.domain.contract import Contract
    from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
    from bel.infrastructure.persistence.repositories import EvidenceRepository

    now = datetime(2026, 1, 1)
    engine = make_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="f", sha256="b" * 64, source_type="t", imported_at=now)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None, row_number=None, locator_json={}, raw_data={}, created_at=now,
        )
        EvidenceRepository(session).add_fragment(frag)
        session.flush()
        contract = Contract(
            id=uuid.uuid4(), contract_no="C-DOWNGRADE-SHIP", contract_type=None, counterparty="Supplier",
            buyer="Buyer", gross_amount=Decimal("1"), currency="CNY", contract_date=None,
            current_source_fragment_id=frag.id, created_at=now, updated_at=now,
        )
        ContractRepository(session).add(contract)
        session.flush()

        shipment = create_shipment_fact(
            session, contract_id=contract.id, external_reference="EXP-DOWNGRADE",
            execution_date=date(2031, 3, 1), fields={"quantity": Decimal("10")},
            source_fragment_id=frag.id, created_at=now,
        ).shipment
        session.commit()

        fact_id = uuid.uuid4()
        CostRecognitionFactRepository(session).add(
            CostRecognitionFact(
                id=fact_id, contract_id=contract.id, recognition_date=date(2031, 3, 1), basis="MANUAL_CONFIRMED",
                source_fragment_id=frag.id, created_at=now, shipment_id=shipment.id,
            )
        )
        session.commit()

    result = _alembic(db_path, "downgrade", R1_REVISION)
    assert result.returncode == 0, result.stderr

    con = create_engine(f"sqlite:///{db_path}").connect()
    columns = {row[1] for row in con.execute(sa.text("PRAGMA table_info(cost_recognition_facts)"))}
    assert "shipment_id" not in columns
    row = con.execute(
        sa.text("SELECT contract_id, basis FROM cost_recognition_facts WHERE id = :id"), {"id": fact_id.hex}
    ).fetchone()
    tables = {r[0] for r in con.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
    con.close()
    assert row is not None
    assert row.basis == "MANUAL_CONFIRMED"  # the fact itself survives, only shipment_id is gone
    assert "shipments" not in tables
    assert "shipment_revisions" not in tables

"""Automates what Codex verified by hand for the
db1c3258569e_contract_item_fact_revisions migration (Phase 2D.1-R1
Codex fix round, WARNING: migration tests). Covers:

- fresh DB -> head
- v0.1.3-shaped populated DB -> head (anchor preserved, INITIAL revision
  exists, values preserved, Evidence preserved)
- a legacy row with NULL current_source_fragment_id -> head (its
  revision's source_fragment_id stays NULL — never fabricated Evidence)
- head -> the pre-R1 revision (downgrade): current business state
  restored onto the anchor, even though revision history is (expectedly)
  not representable in the old shape
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.repositories import ContractItemRepository

REPO_ROOT = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.skip(
    reason="Tests the pre-2D.1-P SQLite migration chain (migrations/versions/), which is frozen "
    "legacy history and no longer wired into active Alembic tooling as of the Phase 2D.1-P "
    "PostgreSQL rebaseline — see docs/PERSISTENCE-MIGRATION-POLICY.md."
)
PRE_R1_REVISION = "62e13873e978"
HEAD_REVISION = "db1c3258569e"


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )


def _v013_tables(meta: sa.MetaData) -> dict[str, sa.Table]:
    """The pre-R1 contract_items shape, declared with real sa.Uuid()/
    sa.DateTime() types (NOT raw sqlite3) so inserted values round-trip
    through SQLAlchemy's bind/result processors exactly as production
    code would have written them — this is what previously surfaced a
    UUID-format mismatch that a naive raw-sqlite3 test harness missed."""
    doc = sa.Table(
        "evidence_documents",
        meta,
        sa.Column("id", sa.Uuid()),
        sa.Column("file_name", sa.String()),
        sa.Column("sha256", sa.String()),
        sa.Column("source_type", sa.String()),
        sa.Column("imported_at", sa.DateTime()),
    )
    frag = sa.Table(
        "evidence_fragments",
        meta,
        sa.Column("id", sa.Uuid()),
        sa.Column("evidence_document_id", sa.Uuid()),
        sa.Column("fragment_kind", sa.String()),
        sa.Column("sheet_name", sa.String()),
        sa.Column("row_number", sa.Integer()),
        sa.Column("locator_json", sa.JSON()),
        sa.Column("raw_data", sa.JSON()),
        sa.Column("created_at", sa.DateTime()),
    )
    contracts = sa.Table(
        "contracts",
        meta,
        sa.Column("id", sa.Uuid()),
        sa.Column("contract_no", sa.String()),
        sa.Column("contract_type", sa.String()),
        sa.Column("counterparty", sa.String()),
        sa.Column("buyer", sa.String()),
        sa.Column("gross_amount", sa.Numeric(18, 2)),
        sa.Column("currency", sa.String()),
        sa.Column("contract_date", sa.Date()),
        sa.Column("current_source_fragment_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )
    items = sa.Table(
        "contract_items",
        meta,
        sa.Column("id", sa.Uuid()),
        sa.Column("contract_id", sa.Uuid()),
        sa.Column("source_item_key", sa.String()),
        sa.Column("sku", sa.String()),
        sa.Column("product_name", sa.String()),
        sa.Column("specification", sa.String()),
        sa.Column("quantity", sa.Numeric(18, 4)),
        sa.Column("unit", sa.String()),
        sa.Column("unit_price", sa.Numeric(18, 4)),
        sa.Column("gross_amount", sa.Numeric(18, 2)),
        sa.Column("tax_rate", sa.Numeric(9, 6)),
        sa.Column("net_amount", sa.Numeric(18, 2)),
        sa.Column("current_source_fragment_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime()),
    )
    return {"evidence_documents": doc, "evidence_fragments": frag, "contracts": contracts, "contract_items": items}


def _seed_v013_data(db_path: Path) -> dict:
    """Inserts one contract with two items — one fully populated with real
    Evidence, one with a legacy NULL current_source_fragment_id — via
    authentic SQLAlchemy Core (typed) inserts against the pre-R1 schema."""
    engine = create_engine(f"sqlite:///{db_path}")
    meta = sa.MetaData()
    tables = _v013_tables(meta)
    now = datetime(2026, 1, 1)

    doc_id, frag_id = uuid.uuid4(), uuid.uuid4()
    contract_id = uuid.uuid4()
    populated_item_id, legacy_null_item_id = uuid.uuid4(), uuid.uuid4()

    with engine.begin() as conn:
        conn.execute(
            tables["evidence_documents"].insert().values(
                id=doc_id, file_name="f.json", sha256="a" * 64, source_type="test", imported_at=now
            )
        )
        conn.execute(
            tables["evidence_fragments"].insert().values(
                id=frag_id,
                evidence_document_id=doc_id,
                fragment_kind="MANUAL_FACT",
                sheet_name=None,
                row_number=None,
                locator_json={},
                raw_data={},
                created_at=now,
            )
        )
        conn.execute(
            tables["contracts"].insert().values(
                id=contract_id,
                contract_no="C-MIGRATION-1",
                contract_type=None,
                counterparty="Supplier",
                buyer="Buyer",
                gross_amount=Decimal("100.00"),
                currency="CNY",
                contract_date=None,
                current_source_fragment_id=frag_id,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            tables["contract_items"].insert().values(
                id=populated_item_id,
                contract_id=contract_id,
                source_item_key="ITEM-POPULATED",
                sku="SKU1",
                product_name="Widget",
                specification=None,
                quantity=Decimal("10"),
                unit="PCS",
                unit_price=Decimal("5"),
                gross_amount=Decimal("50"),
                tax_rate=Decimal("0.13"),
                net_amount=Decimal("44.25"),
                current_source_fragment_id=frag_id,
                created_at=now,
            )
        )
        conn.execute(
            tables["contract_items"].insert().values(
                id=legacy_null_item_id,
                contract_id=contract_id,
                source_item_key="ITEM-LEGACY-NULL",
                sku=None,
                product_name=None,
                specification=None,
                quantity=None,
                unit=None,
                unit_price=None,
                gross_amount=None,
                tax_rate=None,
                net_amount=None,
                current_source_fragment_id=None,  # legacy: provenance genuinely unknown
                created_at=now,
            )
        )
    return {
        "contract_id": contract_id,
        "populated_item_id": populated_item_id,
        "legacy_null_item_id": legacy_null_item_id,
        "frag_id": frag_id,
    }


def test_fresh_database_upgrades_to_head(tmp_path):
    db_path = tmp_path / "fresh.db"
    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr


def test_v013_populated_database_upgrades_to_head_preserving_values(tmp_path):
    db_path = tmp_path / "v013.db"
    assert _alembic(db_path, "upgrade", PRE_R1_REVISION).returncode == 0
    ids = _seed_v013_data(db_path)

    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    engine = make_engine(str(db_path))
    with make_session_factory(engine)() as session:
        repo = ContractItemRepository(session)

        item = repo.get(ids["populated_item_id"])
        assert item is not None  # the anchor row itself was preserved, not deleted
        assert item.contract_id == ids["contract_id"]
        assert item.source_item_key == "ITEM-POPULATED"
        assert item.sku == "SKU1"
        assert item.product_name == "Widget"
        assert item.quantity == Decimal("10.0000")
        assert item.unit == "PCS"
        assert item.unit_price == Decimal("5.0000")
        assert item.gross_amount == Decimal("50.00")
        assert item.tax_rate == Decimal("0.130000")
        assert item.net_amount == Decimal("44.25")
        assert item.current_source_fragment_id == ids["frag_id"]  # Evidence preserved

        history = repo.list_revisions(ids["populated_item_id"])
        assert len(history) == 1
        assert history[0].revision_type == "INITIAL"
        assert history[0].superseded_by_revision_id is None
        assert history[0].source_fragment_id == ids["frag_id"]
        # Legacy data carries no captured command intent (Phase 2D.1-R1
        # Codex fix round #2) — _asserted_fields falls back to
        # reconstructing it from this INITIAL revision's own non-NULL
        # fields rather than trusting a fabricated list.
        assert history[0].asserted_field_names is None


def test_v013_legacy_null_provenance_upgrades_without_fabricated_evidence(tmp_path):
    db_path = tmp_path / "legacy-null.db"
    assert _alembic(db_path, "upgrade", PRE_R1_REVISION).returncode == 0
    ids = _seed_v013_data(db_path)

    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    engine = make_engine(str(db_path))
    with make_session_factory(engine)() as session:
        repo = ContractItemRepository(session)
        item = repo.get(ids["legacy_null_item_id"])
        assert item is not None
        assert item.current_source_fragment_id is None  # preserved exactly, never fabricated

        history = repo.list_revisions(ids["legacy_null_item_id"])
        assert len(history) == 1
        assert history[0].revision_type == "INITIAL"
        assert history[0].source_fragment_id is None


def test_downgrade_restores_current_business_state(tmp_path):
    db_path = tmp_path / "downgrade.db"
    assert _alembic(db_path, "upgrade", "head").returncode == 0

    # Create at head, then supplement it, so there is real revision
    # history that downgrade is NOT expected to preserve — only the
    # CURRENT business state must survive.
    from bel.application.contract_item_facts import create_contract_item_fact, supplement_contract_item_fact
    from bel.domain.contract import Contract
    from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
    from bel.infrastructure.persistence.repositories import ContractRepository, EvidenceRepository

    now = datetime(2026, 1, 1)
    engine = make_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="f", sha256="b" * 64, source_type="t", imported_at=now)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(),
            evidence_document_id=doc.id,
            fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None,
            row_number=None,
            locator_json={},
            raw_data={},
            created_at=now,
        )
        EvidenceRepository(session).add_fragment(frag)
        session.flush()
        contract = Contract(
            id=uuid.uuid4(),
            contract_no="C-DOWNGRADE",
            contract_type=None,
            counterparty="Supplier",
            buyer="Buyer",
            gross_amount=Decimal("1"),
            currency="CNY",
            contract_date=None,
            current_source_fragment_id=frag.id,
            created_at=now,
            updated_at=now,
        )
        ContractRepository(session).add(contract)
        session.flush()

        created = create_contract_item_fact(
            session,
            contract_id=contract.id,
            source_item_key="ITEM-A",
            fields={"product_name": "Widget", "quantity": Decimal("10")},
            source_fragment_id=frag.id,
            created_at=now,
        )
        session.commit()
        item_id = created.item.id

        frag2 = EvidenceFragment(
            id=uuid.uuid4(),
            evidence_document_id=doc.id,
            fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None,
            row_number=2,
            locator_json={},
            raw_data={},
            created_at=now,
        )
        EvidenceRepository(session).add_fragment(frag2)
        session.flush()
        current = ContractItemRepository(session).get_current_revision(item_id)
        supplement_contract_item_fact(
            session,
            contract_item_id=item_id,
            based_on_revision_id=current.id,
            fields={"unit": "PCS"},
            source_fragment_id=frag2.id,
            created_at=now,
        )
        session.commit()

    result = _alembic(db_path, "downgrade", PRE_R1_REVISION)
    assert result.returncode == 0, result.stderr

    con = create_engine(f"sqlite:///{db_path}").connect()
    row = con.execute(
        sa.text("SELECT product_name, quantity, unit FROM contract_items WHERE id = :id"),
        {"id": item_id.hex},
    ).fetchone()
    con.close()
    assert row is not None
    assert row.product_name == "Widget"
    assert row.unit == "PCS"  # the SUPPLEMENT's value, not the INITIAL's — current state, not history

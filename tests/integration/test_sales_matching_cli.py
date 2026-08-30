"""CLI smoke test for `bel sales-match` (Phase 2D.1-R3b) against a real
migrated SQLite file — same rationale as test_procurement_sales_link_cli.py:
prove the actual `bel` entry point commits real state, not just that the
application function works in-process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.repositories import EvidenceRepository, InvoiceRepository

REPO_ROOT = Path(__file__).parent.parent.parent
NOW = datetime.now(timezone.utc)


def _run_bel(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bel.cli", "--db", str(db_path), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _upgrade_head(db_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _seed_sales_invoice_and_contracts(db_path: Path) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    engine = make_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="seed.json", sha256="2" * 64, source_type="test", imported_at=NOW)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None, row_number=None, locator_json={}, raw_data={}, created_at=NOW,
        )
        EvidenceRepository(session).add_fragment(frag)
        session.flush()

        invoice = Invoice(
            id=uuid.uuid4(), direction=InvoiceDirection.SALES, invoice_type=None, invoice_no="INV-CLI-1",
            digital_invoice_no=None, external_invoice_key="INV-CLI-1", issue_date=date(2031, 1, 1),
            seller="Us", buyer="Customer", net_amount=Decimal("100.00"), tax_amount=Decimal("0"),
            gross_amount=Decimal("100.00"), invoice_status=None, source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
        )
        InvoiceRepository(session).add(invoice)
        session.commit()

        from bel.application.sales_contract_facts import create_sales_contract_fact

        sc_x = create_sales_contract_fact(
            session, our_entity="Entity A", sales_contract_no="SC-CLI-MATCH-X", fields={},
            source_fragment_id=frag.id, created_at=NOW,
        ).sales_contract
        sc_y = create_sales_contract_fact(
            session, our_entity="Entity A", sales_contract_no="SC-CLI-MATCH-Y", fields={},
            source_fragment_id=frag.id, created_at=NOW,
        ).sales_contract
        session.commit()
        return invoice.id, sc_x.id, sc_y.id


def test_sales_match_invoice_cli_propose_confirm_list_show_flow(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)
    invoice_id, sc_x_id, sc_y_id = _seed_sales_invoice_and_contracts(db_path)

    propose = _run_bel(
        db_path, "sales-match", "invoice", "propose", "--invoice", str(invoice_id),
        "--sales-contract", str(sc_x_id), "--sales-contract", str(sc_y_id),
    )
    assert propose.returncode == 0, propose.stderr
    assert "created" in propose.stdout
    match_case_id = propose.stdout.split()[1]

    replay = _run_bel(
        db_path, "sales-match", "invoice", "propose", "--invoice", str(invoice_id),
        "--sales-contract", str(sc_x_id), "--sales-contract", str(sc_y_id),
    )
    assert replay.returncode == 0, replay.stderr
    assert "already exists (replay)" in replay.stdout

    show = _run_bel(db_path, "sales-match", "show", match_case_id)
    assert show.returncode == 0, show.stderr
    assert str(sc_x_id) in show.stdout
    assert str(sc_y_id) in show.stdout

    listing = _run_bel(db_path, "sales-match", "list")
    assert listing.returncode == 0, listing.stderr
    assert match_case_id in listing.stdout
    assert "HUMAN_CONFIRMATION_REQUIRED" in listing.stdout

    confirm = _run_bel(
        db_path, "sales-match", "invoice", "confirm", "--match-case", match_case_id,
        "--allocate", f"{sc_x_id}:60.00", "--allocate", f"{sc_y_id}:40.00",
    )
    assert confirm.returncode == 0, confirm.stderr
    assert "RESOLVED" in confirm.stdout
    assert "2 allocation(s)" in confirm.stdout

    listing2 = _run_bel(db_path, "sales-match", "list")
    assert "RESOLVED" in listing2.stdout

    # A different replay payload must be rejected.
    conflict = _run_bel(
        db_path, "sales-match", "invoice", "confirm", "--match-case", match_case_id,
        "--allocate", f"{sc_x_id}:100.00",
    )
    assert conflict.returncode == 1
    assert "Error" in conflict.stdout


def test_sales_match_cli_purchase_invoice_rejected(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)

    engine = make_engine(str(db_path))
    with make_session_factory(engine)() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="seed2.json", sha256="3" * 64, source_type="test", imported_at=NOW)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None, row_number=None, locator_json={}, raw_data={}, created_at=NOW,
        )
        EvidenceRepository(session).add_fragment(frag)
        session.flush()
        purchase_invoice = Invoice(
            id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no="INV-CLI-2",
            digital_invoice_no=None, external_invoice_key="INV-CLI-2", issue_date=date(2031, 1, 1),
            seller="Supplier", buyer="Us", net_amount=Decimal("100.00"), tax_amount=Decimal("0"),
            gross_amount=Decimal("100.00"), invoice_status=None, source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
        )
        InvoiceRepository(session).add(purchase_invoice)
        session.commit()

        from bel.application.sales_contract_facts import create_sales_contract_fact

        sc = create_sales_contract_fact(
            session, our_entity="Entity A", sales_contract_no="SC-CLI-MATCH-2", fields={},
            source_fragment_id=frag.id, created_at=NOW,
        ).sales_contract
        session.commit()
        invoice_id, sc_id = purchase_invoice.id, sc.id

    result = _run_bel(
        db_path, "sales-match", "invoice", "propose", "--invoice", str(invoice_id), "--sales-contract", str(sc_id)
    )
    assert result.returncode == 1
    assert "only SALES invoices" in result.stdout

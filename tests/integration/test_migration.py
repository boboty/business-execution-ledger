import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

EXPECTED_TABLES = {
    "evidence_documents",
    "evidence_fragments",
    "contracts",
    "contract_items",
    "contract_item_revisions",
    "business_events",
    "task_exceptions",
    "import_runs",
    "alembic_version",
}


def test_alembic_upgrade_head_creates_full_schema(tmp_path):
    db_path = tmp_path / "migration_test.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    import sqlite3

    con = sqlite3.connect(db_path)
    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    con.close()
    assert EXPECTED_TABLES <= tables

    # contract_no must never get a UNIQUE index — duplicates are expected.
    con = sqlite3.connect(db_path)
    indexes = con.execute("select sql from sqlite_master where tbl_name='contracts' and sql is not null").fetchall()
    con.close()
    assert not any("UNIQUE" in (sql or "") and "contract_no" in (sql or "") for (sql,) in indexes)

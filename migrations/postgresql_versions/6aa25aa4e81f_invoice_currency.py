"""invoice_currency — Phase 2D.3-F1e

Adds the canonical Invoice currency to the active Invoice persistence
model (docs/PHASE2D3-RULE-FREEZE.md IP-P02):

    currency  String(8) NULL

The currency is the one explicitly stated by the Invoice Evidence/source.
It is nullable so every pre-F1e Invoice row remains valid with NULL (no
backfill is attempted — an explicit source value or an explicit
human-confirmed Fact is required; the current purchase invoice Excel
source provides no canonical currency field, so those imports carry
NULL, and no domestic default is manufactured). It is NOT part of
Invoice identity: ``external_invoice_key``, Invoice identity and
matching identity are unchanged. No FX conversion, no default currency
(no CNY/USD), and no inference from buyer/seller/country /
Contract.currency / SalesContract.currency / amount.

This is an additive, forward-only change on the active PostgreSQL
Migration Epoch chain (migrations/postgresql_versions/) — no rebaseline,
no edit to any existing migration file. ``invoices`` was created by the
baseline (f5796c006707) without this column, so
``Base.metadata.create_all()`` (SQLite test convenience and the
structurally-identical fresh-schema path) and the migration chain now
agree once this revision is applied.

Revision ID: 6aa25aa4e81f
Revises: 93e9d48c5cc8
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6aa25aa4e81f'
down_revision: Union[str, Sequence[str], None] = '93e9d48c5cc8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            f"the PostgreSQL Migration Epoch chain (migrations/postgresql_versions/) only runs "
            f"against PostgreSQL — got dialect {bind.dialect.name!r}. SQLite is a test-only "
            "convenience via Base.metadata.create_all(), never via this Alembic chain."
        )
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('currency', sa.String(length=8), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            f"the PostgreSQL Migration Epoch chain (migrations/postgresql_versions/) only runs "
            f"against PostgreSQL — got dialect {bind.dialect.name!r}."
        )
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_column('currency')

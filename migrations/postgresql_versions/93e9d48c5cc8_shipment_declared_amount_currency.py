"""shipment_declared_amount_currency — Phase 2D.3-F1c

Adds the canonical export/customs declaration values to the active
ShipmentRevision persistence model (docs/PHASE2D3-RULE-FREEZE.md IP-S02):

    declared_amount    Numeric(18, 2) NULL
    declared_currency  String(8)      NULL

Both are nullable so every pre-F1c Shipment row remains valid with NULL
(no backfill is attempted — real declaration Evidence or an explicit
human-confirmed Fact is required; legacy sales-amount columns are never
reinterpreted as a customs-declaration amount). No FX conversion, no
default currency, and no amount copied from quantity / Contract /
SalesContract.

This is an additive, forward-only change on the active PostgreSQL
Migration Epoch chain (migrations/postgresql_versions/) — no rebaseline,
no edit to any existing migration file. `shipment_revisions` was created
by the baseline (f5796c006707) without these columns, so
`Base.metadata.create_all()` (SQLite test convenience and the
structurally-identical fresh-schema path) and the migration chain now
agree once this revision is applied.

Revision ID: 93e9d48c5cc8
Revises: f5796c006707
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '93e9d48c5cc8'
down_revision: Union[str, Sequence[str], None] = 'f5796c006707'
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
    with op.batch_alter_table('shipment_revisions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('declared_amount', sa.Numeric(precision=18, scale=2), nullable=True))
        batch_op.add_column(sa.Column('declared_currency', sa.String(length=8), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            f"the PostgreSQL Migration Epoch chain (migrations/postgresql_versions/) only runs "
            f"against PostgreSQL — got dialect {bind.dialect.name!r}."
        )
    with op.batch_alter_table('shipment_revisions', schema=None) as batch_op:
        batch_op.drop_column('declared_currency')
        batch_op.drop_column('declared_amount')

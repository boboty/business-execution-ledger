"""Add the confirmed invoice-preparation Facts without backfill.

Revision ID: a7d1c4e9b2f0
Revises: 6aa25aa4e81f
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7d1c4e9b2f0"
down_revision: Union[str, Sequence[str], None] = "6aa25aa4e81f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _require_postgresql() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("the active migration chain is PostgreSQL-only")


def upgrade() -> None:
    _require_postgresql()
    with op.batch_alter_table("sales_contract_revisions") as batch_op:
        batch_op.add_column(sa.Column("quantity", sa.Numeric(18, 4), nullable=True))
        batch_op.add_column(sa.Column("unit", sa.String(32), nullable=True))
    with op.batch_alter_table("contract_item_revisions") as batch_op:
        batch_op.add_column(sa.Column("tax_classification_code", sa.String(64), nullable=True))


def downgrade() -> None:
    _require_postgresql()
    with op.batch_alter_table("contract_item_revisions") as batch_op:
        batch_op.drop_column("tax_classification_code")
    with op.batch_alter_table("sales_contract_revisions") as batch_op:
        batch_op.drop_column("unit")
        batch_op.drop_column("quantity")

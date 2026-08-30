"""Phase 2D.1-R5: Payment source_account_id

Adds a nullable ``source_account_id`` column to ``payments`` — the
Phase 2D.1-R0 pre-flight debt closure documented on
``bel.domain.payment.Payment.source_account_id``: Payment's identity was
known to be too weak (date+direction+amount+bank_reference alone cannot
separate two genuinely different transactions on different bank
accounts). No value is fabricated for existing rows.

Revision ID: 371dbb5c1c4a
Revises: a1b2c3d4e5f6
Create Date: 2026-08-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '371dbb5c1c4a'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_account_id', sa.String(), nullable=True))
        batch_op.create_index(batch_op.f('ix_payments_source_account_id'), ['source_account_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_payments_source_account_id'))
        batch_op.drop_column('source_account_id')

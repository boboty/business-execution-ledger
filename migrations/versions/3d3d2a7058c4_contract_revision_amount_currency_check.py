"""Phase 2D.1-R5 gate fix: contract_revisions current-row requires
gross_amount/currency

Schema-level backstop for the pre-R5 invariant "Contract.gross_amount
and Contract.currency are always non-NULL" — a CURRENT revision
(superseded_by_revision_id IS NULL) must have both columns populated.
Mirrors bel.application.contract_facts's own application-layer guard
(rejects asserting None for either field). Every existing row already
satisfies this (pre-R5 contracts.gross_amount/currency were themselves
NOT NULL), so this changes no existing data.

Revision ID: 3d3d2a7058c4
Revises: 1afa9f2cc601
Create Date: 2026-08-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '3d3d2a7058c4'
down_revision: Union[str, Sequence[str], None] = '1afa9f2cc601'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('contract_revisions', schema=None) as batch_op:
        batch_op.create_check_constraint(
            'ck_contract_revisions_current_requires_amount_currency',
            'superseded_by_revision_id IS NOT NULL OR (gross_amount IS NOT NULL AND currency IS NOT NULL)',
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('contract_revisions', schema=None) as batch_op:
        batch_op.drop_constraint('ck_contract_revisions_current_requires_amount_currency', type_='check')

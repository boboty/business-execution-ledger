"""Phase 2D.1-R5: whole-fact supersession for cutover-eligible facts

Closes the docs/PHASE2D1-R0-DECISIONS.md section 1.4/21 pre-flight debt:
HistoricalAccrualFact, CostRecognitionFact, AccrualBasisFact and
InvoiceItemAllocation are single-assertion Facts superseded as a whole
unit (never versioned attribute-by-attribute like ContractItem/Shipment/
SalesContract/Contract). Adds one nullable, self-referential
``superseded_by_fact_id`` lineage pointer per table — NULL for a current
fact, set exactly once via an atomic conditional UPDATE and never
re-pointed. No existing row is affected (all start NULL), so this
changes no current behaviour; only a future explicit supersession call
populates it.

Revision ID: 363c3cd307a1
Revises: 371dbb5c1c4a
Create Date: 2026-08-30 00:00:01.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '363c3cd307a1'
down_revision: Union[str, Sequence[str], None] = '371dbb5c1c4a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('invoice_item_allocations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('superseded_by_fact_id', sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            'fk_invoice_item_allocations_superseded_by_fact_id',
            'invoice_item_allocations',
            ['superseded_by_fact_id'],
            ['id'],
        )
        batch_op.create_index(
            batch_op.f('ix_invoice_item_allocations_superseded_by_fact_id'), ['superseded_by_fact_id'], unique=False
        )

    with op.batch_alter_table('cost_recognition_facts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('superseded_by_fact_id', sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            'fk_cost_recognition_facts_superseded_by_fact_id', 'cost_recognition_facts', ['superseded_by_fact_id'], ['id']
        )
        batch_op.create_index(
            batch_op.f('ix_cost_recognition_facts_superseded_by_fact_id'), ['superseded_by_fact_id'], unique=False
        )

    with op.batch_alter_table('accrual_basis_facts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('superseded_by_fact_id', sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            'fk_accrual_basis_facts_superseded_by_fact_id', 'accrual_basis_facts', ['superseded_by_fact_id'], ['id']
        )
        batch_op.create_index(
            batch_op.f('ix_accrual_basis_facts_superseded_by_fact_id'), ['superseded_by_fact_id'], unique=False
        )

    with op.batch_alter_table('historical_accrual_facts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('superseded_by_fact_id', sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            'fk_historical_accrual_facts_superseded_by_fact_id',
            'historical_accrual_facts',
            ['superseded_by_fact_id'],
            ['id'],
        )
        batch_op.create_index(
            batch_op.f('ix_historical_accrual_facts_superseded_by_fact_id'), ['superseded_by_fact_id'], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('historical_accrual_facts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_historical_accrual_facts_superseded_by_fact_id'))
        batch_op.drop_constraint('fk_historical_accrual_facts_superseded_by_fact_id', type_='foreignkey')
        batch_op.drop_column('superseded_by_fact_id')

    with op.batch_alter_table('accrual_basis_facts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_accrual_basis_facts_superseded_by_fact_id'))
        batch_op.drop_constraint('fk_accrual_basis_facts_superseded_by_fact_id', type_='foreignkey')
        batch_op.drop_column('superseded_by_fact_id')

    with op.batch_alter_table('cost_recognition_facts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cost_recognition_facts_superseded_by_fact_id'))
        batch_op.drop_constraint('fk_cost_recognition_facts_superseded_by_fact_id', type_='foreignkey')
        batch_op.drop_column('superseded_by_fact_id')

    with op.batch_alter_table('invoice_item_allocations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_invoice_item_allocations_superseded_by_fact_id'))
        batch_op.drop_constraint('fk_invoice_item_allocations_superseded_by_fact_id', type_='foreignkey')
        batch_op.drop_column('superseded_by_fact_id')

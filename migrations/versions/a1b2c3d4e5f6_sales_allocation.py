"""Phase 2D.1-R3b: Sales-side Allocation

Creates the sales-side allocation objects
(docs/PHASE2D1-R0-DECISIONS.md section 2.7): `sales_invoice_allocations`,
`sales_payment_allocations`, and `sales_match_candidates`. `MatchCase`
itself is reused completely unchanged — no new column, no new FK, no new
constraint. An earlier draft of this migration also added a
`UniqueConstraint('subject_type', 'subject_id')` to `match_cases`,
reasoning it only formalised an assumed invariant; this was reverted
after `tests/web/test_web_contract_360.py` proved the procurement leg
already relies on ONE Invoice legitimately producing TWO separate
`MatchCase` rows (one per Contract it is confirmed against — "Domain:
Invoice <-> Contract is many-to-many"). The sales leg's own concurrency
safety (section 2.7's manual proposal path) comes instead from an
application-level atomic conditional insert
(`MatchCaseRepository.add_if_no_case_for_subject`), which needs no
schema change here.

No pre-existing data for any of the three new tables (none of these
objects existed before this round). No existing table is altered.

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-08-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'sales_invoice_allocations',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('invoice_id', sa.Uuid(), nullable=False),
        sa.Column('sales_contract_id', sa.Uuid(), nullable=False),
        sa.Column('match_case_id', sa.Uuid(), nullable=False),
        sa.Column('allocated_gross_amount', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('confirmation_type', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id']),
        sa.ForeignKeyConstraint(['sales_contract_id'], ['sales_contracts.id']),
        sa.ForeignKeyConstraint(['match_case_id'], ['match_cases.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "confirmation_type = 'HUMAN_CONFIRMED'", name='ck_sales_invoice_allocations_confirmation_type'
        ),
        sa.CheckConstraint("allocated_gross_amount > 0", name='ck_sales_invoice_allocations_positive_amount'),
        sa.CheckConstraint(
            f"allocated_gross_amount < {10 ** 16}", name='ck_sales_invoice_allocations_max_amount'
        ),
    )
    with op.batch_alter_table('sales_invoice_allocations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sales_invoice_allocations_invoice_id'), ['invoice_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_sales_invoice_allocations_sales_contract_id'), ['sales_contract_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_sales_invoice_allocations_match_case_id'), ['match_case_id'], unique=False
        )

    op.create_table(
        'sales_payment_allocations',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('payment_id', sa.Uuid(), nullable=False),
        sa.Column('sales_contract_id', sa.Uuid(), nullable=False),
        sa.Column('match_case_id', sa.Uuid(), nullable=False),
        sa.Column('allocated_amount', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('confirmation_type', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['payment_id'], ['payments.id']),
        sa.ForeignKeyConstraint(['sales_contract_id'], ['sales_contracts.id']),
        sa.ForeignKeyConstraint(['match_case_id'], ['match_cases.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "confirmation_type = 'HUMAN_CONFIRMED'", name='ck_sales_payment_allocations_confirmation_type'
        ),
        sa.CheckConstraint("allocated_amount > 0", name='ck_sales_payment_allocations_positive_amount'),
        sa.CheckConstraint(f"allocated_amount < {10 ** 16}", name='ck_sales_payment_allocations_max_amount'),
    )
    with op.batch_alter_table('sales_payment_allocations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sales_payment_allocations_payment_id'), ['payment_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_sales_payment_allocations_sales_contract_id'), ['sales_contract_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_sales_payment_allocations_match_case_id'), ['match_case_id'], unique=False
        )

    op.create_table(
        'sales_match_candidates',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('match_case_id', sa.Uuid(), nullable=False),
        sa.Column('sales_contract_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['match_case_id'], ['match_cases.id']),
        sa.ForeignKeyConstraint(['sales_contract_id'], ['sales_contracts.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('match_case_id', 'sales_contract_id', name='uq_sales_match_candidates_case_target'),
    )
    with op.batch_alter_table('sales_match_candidates', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_sales_match_candidates_match_case_id'), ['match_case_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_sales_match_candidates_sales_contract_id'), ['sales_contract_id'], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('sales_match_candidates', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_match_candidates_sales_contract_id'))
        batch_op.drop_index(batch_op.f('ix_sales_match_candidates_match_case_id'))
    op.drop_table('sales_match_candidates')

    with op.batch_alter_table('sales_payment_allocations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_payment_allocations_match_case_id'))
        batch_op.drop_index(batch_op.f('ix_sales_payment_allocations_sales_contract_id'))
        batch_op.drop_index(batch_op.f('ix_sales_payment_allocations_payment_id'))
    op.drop_table('sales_payment_allocations')

    with op.batch_alter_table('sales_invoice_allocations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_invoice_allocations_match_case_id'))
        batch_op.drop_index(batch_op.f('ix_sales_invoice_allocations_sales_contract_id'))
        batch_op.drop_index(batch_op.f('ix_sales_invoice_allocations_invoice_id'))
    op.drop_table('sales_invoice_allocations')

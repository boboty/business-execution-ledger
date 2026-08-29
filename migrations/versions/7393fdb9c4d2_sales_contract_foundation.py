"""Phase 2D.1-R3a Slice 1: SalesContract foundation

Creates the SalesContract anchor + revision tables
(docs/PHASE2D1-R0-DECISIONS.md sections 2.1-2.3, reusing the anchor+
revision model frozen in section 1.3 and validated across the Phase
2D.1-R1/R2 Codex fix rounds).

This is Slice 1 only: `ProcurementSalesLink` (and its correction record,
its ADD/CORRECT/INVALIDATE/REESTABLISH actions) is explicitly NOT part
of this migration — that is Slice 2. Nothing here references a
procurement `Contract` at all: `SalesContract` is a fully independent
table, which is precisely why `contract_repo.list_all()` (used by
purchase-side matching) remains completely unaware SalesContract exists.

There is no pre-existing SalesContract data anywhere (this object did
not exist before this round), so — like the R2 Shipment migration —
there is no data-migration step and no legacy-NULL-provenance
accommodation to make: `sales_contract_revisions.source_fragment_id` is
NOT NULL from the start. No existing table (contracts, contract_items,
shipments, cost_recognition_facts, ...) is touched by this migration.

Revision ID: 7393fdb9c4d2
Revises: 147d94b436e0
Create Date: 2026-08-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7393fdb9c4d2'
down_revision: Union[str, Sequence[str], None] = '147d94b436e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'sales_contracts',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('our_entity', sa.String(), nullable=False),
        sa.Column('sales_contract_no', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        # Frozen business identity (docs/PHASE2D1-R0-DECISIONS.md section
        # 4.4): (our_entity, sales_contract_no). Unlike Shipment's
        # identity, BOTH components are mandatory — there is no
        # confirmed-anchor-with-incomplete-identity case for
        # SalesContract, so no column here is nullable.
        sa.UniqueConstraint('our_entity', 'sales_contract_no', name='uq_sales_contract_business_identity'),
    )
    with op.batch_alter_table('sales_contracts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sales_contracts_our_entity'), ['our_entity'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_sales_contracts_sales_contract_no'), ['sales_contract_no'], unique=False
        )

    op.create_table(
        'sales_contract_revisions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('sales_contract_id', sa.Uuid(), nullable=False),
        sa.Column('revision_type', sa.String(), nullable=False),
        # The ONLY place an external sales customer is expressed
        # (docs/DOMAIN.md). Nullable: a scope may be known before its
        # customer is (section 2.3).
        sa.Column('customer', sa.String(), nullable=True),
        sa.Column('currency', sa.String(length=8), nullable=True),
        sa.Column('gross_amount', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('contract_date', sa.Date(), nullable=True),
        # Required — never nullable. No pre-R3a legacy data to
        # accommodate, unlike ContractItemRevision.source_fragment_id.
        sa.Column('source_fragment_id', sa.Uuid(), nullable=False),
        sa.Column('superseded_by_revision_id', sa.Uuid(), nullable=True),
        sa.Column('asserted_field_names', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['sales_contract_id'], ['sales_contracts.id']),
        sa.ForeignKeyConstraint(['source_fragment_id'], ['evidence_fragments.id']),
        sa.ForeignKeyConstraint(['superseded_by_revision_id'], ['sales_contract_revisions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "revision_type IN ('INITIAL', 'SUPPLEMENT', 'CORRECTION')",
            name='ck_sales_contract_revisions_revision_type',
        ),
    )
    with op.batch_alter_table('sales_contract_revisions', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_sales_contract_revisions_sales_contract_id'), ['sales_contract_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_sales_contract_revisions_superseded_by_revision_id'),
            ['superseded_by_revision_id'],
            unique=False,
        )
    # Same two DB-level backstops closed in the Phase 2D.1-R1/R2 Codex fix
    # rounds for ContractItemRevision/ShipmentRevision, applied to
    # SalesContract from day one: at most one current revision per
    # anchor, and — a SEPARATE invariant — at most one INITIAL revision
    # per anchor.
    op.create_index(
        'uq_sales_contract_revisions_one_current',
        'sales_contract_revisions',
        ['sales_contract_id'],
        unique=True,
        sqlite_where=sa.text('superseded_by_revision_id IS NULL'),
    )
    op.create_index(
        'uq_sales_contract_revisions_one_initial',
        'sales_contract_revisions',
        ['sales_contract_id'],
        unique=True,
        sqlite_where=sa.text("revision_type = 'INITIAL'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_sales_contract_revisions_one_initial', table_name='sales_contract_revisions')
    op.drop_index('uq_sales_contract_revisions_one_current', table_name='sales_contract_revisions')
    with op.batch_alter_table('sales_contract_revisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_contract_revisions_superseded_by_revision_id'))
        batch_op.drop_index(batch_op.f('ix_sales_contract_revisions_sales_contract_id'))
    op.drop_table('sales_contract_revisions')

    with op.batch_alter_table('sales_contracts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_contracts_sales_contract_no'))
        batch_op.drop_index(batch_op.f('ix_sales_contracts_our_entity'))
    op.drop_table('sales_contracts')

"""Phase 2D.1-R2: Shipment minimum vertical slice

Creates the Shipment anchor + revision tables (docs/PHASE2D1-R0-DECISIONS.md
sections 3.1-3.4, reusing the anchor+revision model frozen in section 1.3
and validated across the Phase 2D.1-R1 Codex fix rounds), and adds a
nullable `shipment_id` provenance column to `cost_recognition_facts`
(section 3.4 — an explicit provenance reference, never re-pointed, naming
the Shipment anchor that evidenced a cost recognition; it does not create
one, and no existing CostRecognitionFact row is backfilled with a guessed
Shipment).

There is no pre-existing Shipment data anywhere (V1-SCOPE.md section 2.3:
"no Shipment/Export implementation of any kind" prior to this round), so
unlike the R1 ContractItem migration this one has no legacy rows to
migrate and no legacy-NULL-provenance accommodation to make:
`shipment_revisions.source_fragment_id` is NOT NULL from the start, per
section 3.2's "source_fragment_id required — Evidence trace, never
nullable". Every existing `cost_recognition_facts` row gets
`shipment_id = NULL` (the column default), preserved exactly — no fake
Shipment is fabricated to fill it.

Revision ID: 147d94b436e0
Revises: db1c3258569e
Create Date: 2026-08-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '147d94b436e0'
down_revision: Union[str, Sequence[str], None] = 'db1c3258569e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'shipments',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('contract_id', sa.Uuid(), nullable=False),
        sa.Column('external_reference', sa.String(), nullable=True),
        sa.Column('execution_date', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['contract_id'], ['contracts.id']),
        sa.PrimaryKeyConstraint('id'),
        # Frozen business identity (docs/PHASE2D1-R0-DECISIONS.md section
        # 4.4): (contract_id, external_reference, execution_date).
        # external_reference is nullable, and SQLite (like standard SQL)
        # treats NULL as distinct from any other NULL under a UNIQUE
        # constraint, so multiple Shipments sharing (contract_id,
        # execution_date) with no external_reference never collide —
        # matching "external_reference null -> identity incomplete", no
        # auto-dedup attempted.
        sa.UniqueConstraint(
            'contract_id', 'external_reference', 'execution_date', name='uq_shipment_business_identity'
        ),
    )
    with op.batch_alter_table('shipments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_shipments_contract_id'), ['contract_id'], unique=False)

    op.create_table(
        'shipment_revisions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('shipment_id', sa.Uuid(), nullable=False),
        sa.Column('revision_type', sa.String(), nullable=False),
        sa.Column('contract_item_id', sa.Uuid(), nullable=True),
        sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=True),
        # Required — never nullable (section 3.2). No pre-R2 legacy data
        # to accommodate, unlike ContractItemRevision.source_fragment_id.
        sa.Column('source_fragment_id', sa.Uuid(), nullable=False),
        sa.Column('superseded_by_revision_id', sa.Uuid(), nullable=True),
        sa.Column('asserted_field_names', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['shipment_id'], ['shipments.id']),
        sa.ForeignKeyConstraint(['contract_item_id'], ['contract_items.id']),
        sa.ForeignKeyConstraint(['source_fragment_id'], ['evidence_fragments.id']),
        sa.ForeignKeyConstraint(['superseded_by_revision_id'], ['shipment_revisions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "revision_type IN ('INITIAL', 'SUPPLEMENT', 'CORRECTION')",
            name='ck_shipment_revisions_revision_type',
        ),
    )
    with op.batch_alter_table('shipment_revisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_shipment_revisions_shipment_id'), ['shipment_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_shipment_revisions_superseded_by_revision_id'), ['superseded_by_revision_id'], unique=False
        )
    # Same two DB-level backstops closed in the Phase 2D.1-R1 Codex fix
    # rounds for ContractItemRevision, applied to Shipment from day one:
    # at most one current revision per anchor, and — a SEPARATE
    # invariant — at most one INITIAL revision per anchor.
    op.create_index(
        'uq_shipment_revisions_one_current',
        'shipment_revisions',
        ['shipment_id'],
        unique=True,
        sqlite_where=sa.text('superseded_by_revision_id IS NULL'),
    )
    op.create_index(
        'uq_shipment_revisions_one_initial',
        'shipment_revisions',
        ['shipment_id'],
        unique=True,
        sqlite_where=sa.text("revision_type = 'INITIAL'"),
    )

    # CostRecognitionFact -> Shipment provenance (section 3.4). Nullable:
    # not every CostRecognitionFact is shipment-evidenced, and every
    # existing row gets NULL here, preserved exactly — never a fabricated
    # Shipment reference.
    with op.batch_alter_table('cost_recognition_facts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('shipment_id', sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            'fk_cost_recognition_facts_shipment_id', 'shipments', ['shipment_id'], ['id']
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('cost_recognition_facts', schema=None) as batch_op:
        batch_op.drop_constraint('fk_cost_recognition_facts_shipment_id', type_='foreignkey')
        batch_op.drop_column('shipment_id')

    op.drop_index('uq_shipment_revisions_one_initial', table_name='shipment_revisions')
    op.drop_index('uq_shipment_revisions_one_current', table_name='shipment_revisions')
    with op.batch_alter_table('shipment_revisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_shipment_revisions_superseded_by_revision_id'))
        batch_op.drop_index(batch_op.f('ix_shipment_revisions_shipment_id'))
    op.drop_table('shipment_revisions')

    with op.batch_alter_table('shipments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_shipments_contract_id'))
    op.drop_table('shipments')

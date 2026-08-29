"""Phase 2D.1-R3a Slice 2: ProcurementSalesLink

Creates the canonical procurement/sales bridge
(docs/PHASE2D1-R0-DECISIONS.md section 2.4): `procurement_sales_links`
(one row per confirmed assertion episode) and
`procurement_sales_link_corrections` (append-only supersession /
invalidation records).

No pre-existing data anywhere (neither object existed before this
round), so — like the Slice 1 SalesContract migration — there is no data
migration step. No existing table (contracts, sales_contracts, shipments,
cost_recognition_facts, ...) is touched.

The frozen "at most one CURRENT assertion episode per relationship
business key" invariant (section 2.4) cannot be expressed as a plain
UNIQUE constraint or partial index — REESTABLISH legitimately creates a
SECOND row for a business key that already has retired history, and
"current" is defined by the ABSENCE of a row in a different table
(`procurement_sales_link_corrections.superseded_link_id`), which neither
a CHECK constraint nor a SQLite partial index can reference. The
storage-level backstop is therefore a trigger — see
`trg_procurement_sales_links_one_current` below, registered identically
via `event.listen` in models.py so `Base.metadata.create_all()` (used by
in-memory test fixtures) creates the same guard.

Revision ID: f1a2b3c4d5e6
Revises: 7393fdb9c4d2
Create Date: 2026-08-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = '7393fdb9c4d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ONE_CURRENT_LINK_TRIGGER_SQL = """
CREATE TRIGGER trg_procurement_sales_links_one_current
BEFORE INSERT ON procurement_sales_links
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM procurement_sales_links existing
    WHERE existing.procurement_contract_id = NEW.procurement_contract_id
      AND existing.sales_contract_id = NEW.sales_contract_id
      AND NOT EXISTS (
          SELECT 1 FROM procurement_sales_link_corrections c
          WHERE c.superseded_link_id = existing.id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'one current assertion episode per relationship business key');
END;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'procurement_sales_links',
        sa.Column('id', sa.Uuid(), nullable=False),
        # Relationship business key (docs/PHASE2D1-R0-DECISIONS.md section
        # 4.4): (procurement_contract_id, sales_contract_id). Deliberately
        # NOT unique here — a business key may accumulate several episodes
        # over time (ADD, INVALIDATE, REESTABLISH), at most one current.
        # The one-current invariant is enforced by
        # trg_procurement_sales_links_one_current below, not by a
        # constraint on this table alone.
        sa.Column('procurement_contract_id', sa.Uuid(), nullable=False),
        sa.Column('sales_contract_id', sa.Uuid(), nullable=False),
        sa.Column('source_fragment_id', sa.Uuid(), nullable=False),
        sa.Column('confirmation_type', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['procurement_contract_id'], ['contracts.id']),
        sa.ForeignKeyConstraint(['sales_contract_id'], ['sales_contracts.id']),
        sa.ForeignKeyConstraint(['source_fragment_id'], ['evidence_fragments.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "confirmation_type IN ('AUTO_CONFIRMED', 'HUMAN_CONFIRMED')",
            name='ck_procurement_sales_links_confirmation_type',
        ),
    )
    with op.batch_alter_table('procurement_sales_links', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_procurement_sales_links_procurement_contract_id'), ['procurement_contract_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_procurement_sales_links_sales_contract_id'), ['sales_contract_id'], unique=False
        )

    op.create_table(
        'procurement_sales_link_corrections',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('superseded_link_id', sa.Uuid(), nullable=False),
        sa.Column('replacement_link_id', sa.Uuid(), nullable=True),
        sa.Column('source_fragment_id', sa.Uuid(), nullable=False),
        sa.Column('confirmation_type', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['superseded_link_id'], ['procurement_sales_links.id']),
        sa.ForeignKeyConstraint(['replacement_link_id'], ['procurement_sales_links.id']),
        sa.ForeignKeyConstraint(['source_fragment_id'], ['evidence_fragments.id']),
        sa.PrimaryKeyConstraint('id'),
        # Correction lineage invariant (docs/PHASE2D1-R0-DECISIONS.md
        # section 2.4): an assertion episode may be superseded at most
        # once — a correction chain can never fork.
        sa.UniqueConstraint('superseded_link_id', name='uq_procurement_sales_link_corrections_superseded_link_id'),
        # V1-frozen: a correction is always HUMAN_CONFIRMED — corrective
        # Evidence alone never flips authority.
        sa.CheckConstraint(
            "confirmation_type = 'HUMAN_CONFIRMED'",
            name='ck_procurement_sales_link_corrections_confirmation_type',
        ),
    )

    op.execute(_ONE_CURRENT_LINK_TRIGGER_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP TRIGGER IF EXISTS trg_procurement_sales_links_one_current')
    op.drop_table('procurement_sales_link_corrections')
    with op.batch_alter_table('procurement_sales_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_procurement_sales_links_sales_contract_id'))
        batch_op.drop_index(batch_op.f('ix_procurement_sales_links_procurement_contract_id'))
    op.drop_table('procurement_sales_links')

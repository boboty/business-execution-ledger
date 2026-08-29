"""Phase 2D.1-R1: ContractItem fact revisions

Splits ContractItemModel into a stable identity anchor
(id, contract_id, source_item_key, created_at) plus a new
contract_item_revisions table carrying the versioned business values,
per docs/PHASE2D1-R0-DECISIONS.md section 1.3.

Every existing contract_items row's business-value columns are moved
into exactly one INITIAL ContractItemRevision before those columns are
dropped from contract_items, so no ContractItem row and no Evidence
fragment is deleted and no business value is lost. A pre-existing row's
current_source_fragment_id (nullable pre-R1) becomes its INITIAL
revision's source_fragment_id verbatim, including NULL where that was
already the case — a provenance reference, never re-pointed afterwards.

Revision ID: db1c3258569e
Revises: 62e13873e978
Create Date: 2026-08-29 00:00:00.000000

"""
import uuid
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'db1c3258569e'
down_revision: Union[str, Sequence[str], None] = '62e13873e978'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'contract_item_revisions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('contract_item_id', sa.Uuid(), nullable=False),
        sa.Column('revision_type', sa.String(), nullable=False),
        sa.Column('sku', sa.String(), nullable=True),
        sa.Column('product_name', sa.String(), nullable=True),
        sa.Column('specification', sa.String(), nullable=True),
        sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('unit', sa.String(), nullable=True),
        sa.Column('unit_price', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('gross_amount', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('tax_rate', sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column('net_amount', sa.Numeric(precision=18, scale=2), nullable=True),
        # Nullable at the schema level, matching the pre-R1
        # current_source_fragment_id column it replaces — see the model
        # docstring in models.py. Application-layer commands require a
        # real fragment; this migration only needs to carry forward
        # whatever the pre-existing row already had.
        sa.Column('source_fragment_id', sa.Uuid(), nullable=True),
        sa.Column('superseded_by_revision_id', sa.Uuid(), nullable=True),
        # Phase 2D.1-R1 Codex fix round #2: the exact field names the
        # writing command asserted, captured verbatim — see the model
        # docstring in models.py. NULL for every row this migration
        # writes below: legacy data carries no captured command intent,
        # so bel.application.contract_item_facts._asserted_fields falls
        # back to reconstructing it from the (predecessor-less) INITIAL
        # revision's own non-NULL values.
        sa.Column('asserted_field_names', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['contract_item_id'], ['contract_items.id']),
        sa.ForeignKeyConstraint(['source_fragment_id'], ['evidence_fragments.id']),
        sa.ForeignKeyConstraint(['superseded_by_revision_id'], ['contract_item_revisions.id']),
        sa.PrimaryKeyConstraint('id'),
        # Phase 2D.1-R1 Codex fix round #3, FIX 3A: a DB-level backstop
        # against ANY row with a revision_type outside the closed set —
        # mirrors the CheckConstraint on ContractItemRevisionModel in
        # models.py so both a raw INSERT and an ORM bypass are rejected,
        # not just the repository's own application-level validation.
        sa.CheckConstraint(
            "revision_type IN ('INITIAL', 'SUPPLEMENT', 'CORRECTION')",
            name='ck_contract_item_revisions_revision_type',
        ),
    )
    with op.batch_alter_table('contract_item_revisions', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_contract_item_revisions_contract_item_id'), ['contract_item_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_contract_item_revisions_superseded_by_revision_id'),
            ['superseded_by_revision_id'],
            unique=False,
        )
    # Phase 2D.1-R1 Codex fix round, BLOCKER 4: DB-level backstop for "at
    # most one current revision per anchor" — mirrors the Index declared
    # on ContractItemRevisionModel in models.py so Base.metadata.create_all
    # (used by in-memory test databases) and this migration (used by real
    # deployments) enforce the identical invariant.
    op.create_index(
        'uq_contract_item_revisions_one_current',
        'contract_item_revisions',
        ['contract_item_id'],
        unique=True,
        sqlite_where=sa.text('superseded_by_revision_id IS NULL'),
    )
    # Phase 2D.1-R1 Codex fix round #3, FIX 3B: a SEPARATE partial unique
    # index solving a DIFFERENT problem than the one above — "at most one
    # current revision" does not by itself prevent a second INITIAL
    # revision from existing (superseded or not) on the same anchor. This
    # is deliberately "at most one INITIAL" only; it does NOT enforce
    # "an anchor must always have an INITIAL" (no trigger is added for
    # that — out of this round's scope).
    op.create_index(
        'uq_contract_item_revisions_one_initial',
        'contract_item_revisions',
        ['contract_item_id'],
        unique=True,
        sqlite_where=sa.text("revision_type = 'INITIAL'"),
    )

    # Data migration: one INITIAL revision per existing contract_items row,
    # carrying its current business values and current_source_fragment_id
    # forward as that revision's (never-again-re-pointed) source_fragment_id.
    # A pre-R1 row's current_source_fragment_id was already nullable — see
    # the "Nullable at the schema level" comment on the source_fragment_id
    # column above. Where it was NULL, that NULL is carried forward exactly
    # as-is (see _as_uuid below): this migration never fabricates Evidence
    # to fill the gap, and a legacy row's true provenance stays honestly
    # unknown rather than being papered over.
    connection = op.get_bind()
    existing_items = connection.execute(
        sa.text(
            "SELECT id, sku, product_name, specification, quantity, unit, unit_price, "
            "gross_amount, tax_rate, net_amount, current_source_fragment_id, created_at "
            "FROM contract_items"
        )
    ).fetchall()

    revision_table = sa.table(
        'contract_item_revisions',
        sa.column('id', sa.Uuid()),
        sa.column('contract_item_id', sa.Uuid()),
        sa.column('revision_type', sa.String()),
        sa.column('sku', sa.String()),
        sa.column('product_name', sa.String()),
        sa.column('specification', sa.String()),
        sa.column('quantity', sa.Numeric(18, 4)),
        sa.column('unit', sa.String()),
        sa.column('unit_price', sa.Numeric(18, 4)),
        sa.column('gross_amount', sa.Numeric(18, 2)),
        sa.column('tax_rate', sa.Numeric(9, 6)),
        sa.column('net_amount', sa.Numeric(18, 2)),
        sa.column('source_fragment_id', sa.Uuid()),
        sa.column('superseded_by_revision_id', sa.Uuid()),
        sa.column('created_at', sa.DateTime()),
    )
    def _as_uuid(value):
        # Raw sa.text() rows hand back the driver-native representation
        # (a str on SQLite) rather than a uuid.UUID instance, which the
        # sa.Uuid() column type's bind processor requires.
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

    def _as_datetime(value):
        # Same issue as _as_uuid: sa.DateTime()'s bind processor requires
        # an actual datetime, not the ISO string sa.text() hands back.
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))

    for row in existing_items:
        connection.execute(
            revision_table.insert().values(
                id=uuid.uuid4(),
                contract_item_id=_as_uuid(row.id),
                revision_type='INITIAL',
                sku=row.sku,
                product_name=row.product_name,
                specification=row.specification,
                quantity=row.quantity,
                unit=row.unit,
                unit_price=row.unit_price,
                gross_amount=row.gross_amount,
                tax_rate=row.tax_rate,
                net_amount=row.net_amount,
                source_fragment_id=_as_uuid(row.current_source_fragment_id),
                superseded_by_revision_id=None,
                created_at=_as_datetime(row.created_at),
            )
        )

    with op.batch_alter_table('contract_items', schema=None) as batch_op:
        batch_op.drop_column('sku')
        batch_op.drop_column('product_name')
        batch_op.drop_column('specification')
        batch_op.drop_column('quantity')
        batch_op.drop_column('unit')
        batch_op.drop_column('unit_price')
        batch_op.drop_column('gross_amount')
        batch_op.drop_column('tax_rate')
        batch_op.drop_column('net_amount')
        batch_op.drop_column('current_source_fragment_id')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('contract_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('current_source_fragment_id', sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column('net_amount', sa.Numeric(precision=18, scale=2), nullable=True))
        batch_op.add_column(sa.Column('tax_rate', sa.Numeric(precision=9, scale=6), nullable=True))
        batch_op.add_column(sa.Column('gross_amount', sa.Numeric(precision=18, scale=2), nullable=True))
        batch_op.add_column(sa.Column('unit_price', sa.Numeric(precision=18, scale=4), nullable=True))
        batch_op.add_column(sa.Column('unit', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=True))
        batch_op.add_column(sa.Column('specification', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('product_name', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('sku', sa.String(), nullable=True))

    # Restore each anchor's business values from its CURRENT revision only
    # — SUPPLEMENT/CORRECTION history created after upgrade is intentionally
    # not representable in the pre-R1 shape and is dropped by downgrade.
    connection = op.get_bind()
    current_revisions = connection.execute(
        sa.text(
            "SELECT contract_item_id, sku, product_name, specification, quantity, unit, unit_price, "
            "gross_amount, tax_rate, net_amount, source_fragment_id "
            "FROM contract_item_revisions WHERE superseded_by_revision_id IS NULL"
        )
    ).fetchall()
    for row in current_revisions:
        connection.execute(
            sa.text(
                "UPDATE contract_items SET sku=:sku, product_name=:product_name, "
                "specification=:specification, quantity=:quantity, unit=:unit, "
                "unit_price=:unit_price, gross_amount=:gross_amount, tax_rate=:tax_rate, "
                "net_amount=:net_amount, current_source_fragment_id=:source_fragment_id "
                "WHERE id=:id"
            ),
            {
                "id": row.contract_item_id,
                "sku": row.sku,
                "product_name": row.product_name,
                "specification": row.specification,
                "quantity": row.quantity,
                "unit": row.unit,
                "unit_price": row.unit_price,
                "gross_amount": row.gross_amount,
                "tax_rate": row.tax_rate,
                "net_amount": row.net_amount,
                "source_fragment_id": row.source_fragment_id,
            },
        )

    op.drop_index('uq_contract_item_revisions_one_initial', table_name='contract_item_revisions')
    op.drop_index('uq_contract_item_revisions_one_current', table_name='contract_item_revisions')
    with op.batch_alter_table('contract_item_revisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_contract_item_revisions_superseded_by_revision_id'))
        batch_op.drop_index(batch_op.f('ix_contract_item_revisions_contract_item_id'))

    op.drop_table('contract_item_revisions')

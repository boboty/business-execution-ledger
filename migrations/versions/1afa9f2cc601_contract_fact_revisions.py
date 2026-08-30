"""Phase 2D.1-R5: Contract fact revisions

Splits ContractModel into a stable identity anchor
(id, contract_no, counterparty, created_at) plus a new
contract_revisions table carrying the versioned business values
(contract_type, buyer, gross_amount, currency, contract_date), the SAME
anchor+revision pattern ContractItem already has
(db1c3258569e_contract_item_fact_revisions) — closing the Phase 2D.1-R0
pre-flight debt for Contract: without this, a revised legacy ledger
cannot safely supplement/correct a Contract's non-identity values
without duplicating the anchor.

Every existing contracts row's business-value columns move into exactly
one INITIAL ContractRevision before those columns are dropped from
contracts, so no Contract row and no Evidence fragment is deleted and no
business value is lost. A pre-existing row's current_source_fragment_id
becomes its INITIAL revision's source_fragment_id verbatim (a provenance
reference, never re-pointed afterwards), and its updated_at becomes the
INITIAL revision's created_at — Contract.updated_at is now always the
current revision's created_at, never a separately stored column.

Revision ID: 1afa9f2cc601
Revises: 363c3cd307a1
Create Date: 2026-08-30 00:00:02.000000

"""
import uuid
from datetime import date, datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1afa9f2cc601'
down_revision: Union[str, Sequence[str], None] = '363c3cd307a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'contract_revisions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('contract_id', sa.Uuid(), nullable=False),
        sa.Column('revision_type', sa.String(), nullable=False),
        sa.Column('contract_type', sa.String(), nullable=True),
        sa.Column('buyer', sa.String(), nullable=True),
        sa.Column('gross_amount', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('currency', sa.String(length=8), nullable=True),
        sa.Column('contract_date', sa.Date(), nullable=True),
        sa.Column('source_fragment_id', sa.Uuid(), nullable=True),
        sa.Column('superseded_by_revision_id', sa.Uuid(), nullable=True),
        sa.Column('asserted_field_names', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['contract_id'], ['contracts.id']),
        sa.ForeignKeyConstraint(['source_fragment_id'], ['evidence_fragments.id']),
        sa.ForeignKeyConstraint(['superseded_by_revision_id'], ['contract_revisions.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "revision_type IN ('INITIAL', 'SUPPLEMENT', 'CORRECTION')",
            name='ck_contract_revisions_revision_type',
        ),
    )
    with op.batch_alter_table('contract_revisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_contract_revisions_contract_id'), ['contract_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_contract_revisions_superseded_by_revision_id'), ['superseded_by_revision_id'], unique=False
        )
    op.create_index(
        'uq_contract_revisions_one_current',
        'contract_revisions',
        ['contract_id'],
        unique=True,
        sqlite_where=sa.text('superseded_by_revision_id IS NULL'),
    )
    op.create_index(
        'uq_contract_revisions_one_initial',
        'contract_revisions',
        ['contract_id'],
        unique=True,
        sqlite_where=sa.text("revision_type = 'INITIAL'"),
    )

    # Data migration: one INITIAL revision per existing contracts row.
    connection = op.get_bind()
    existing_contracts = connection.execute(
        sa.text(
            "SELECT id, contract_type, buyer, gross_amount, currency, contract_date, "
            "current_source_fragment_id, created_at, updated_at FROM contracts"
        )
    ).fetchall()

    revision_table = sa.table(
        'contract_revisions',
        sa.column('id', sa.Uuid()),
        sa.column('contract_id', sa.Uuid()),
        sa.column('revision_type', sa.String()),
        sa.column('contract_type', sa.String()),
        sa.column('buyer', sa.String()),
        sa.column('gross_amount', sa.Numeric(18, 2)),
        sa.column('currency', sa.String(8)),
        sa.column('contract_date', sa.Date()),
        sa.column('source_fragment_id', sa.Uuid()),
        sa.column('superseded_by_revision_id', sa.Uuid()),
        sa.column('created_at', sa.DateTime()),
    )

    def _as_uuid(value):
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

    def _as_datetime(value):
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))

    def _as_date(value):
        if value is None:
            return None
        return value if isinstance(value, date) else date.fromisoformat(str(value))

    for row in existing_contracts:
        connection.execute(
            revision_table.insert().values(
                id=uuid.uuid4(),
                contract_id=_as_uuid(row.id),
                revision_type='INITIAL',
                contract_type=row.contract_type,
                buyer=row.buyer,
                gross_amount=row.gross_amount,
                currency=row.currency,
                contract_date=_as_date(row.contract_date),
                source_fragment_id=_as_uuid(row.current_source_fragment_id),
                superseded_by_revision_id=None,
                created_at=_as_datetime(row.updated_at),
            )
        )

    with op.batch_alter_table('contracts', schema=None) as batch_op:
        batch_op.drop_column('contract_type')
        batch_op.drop_column('buyer')
        batch_op.drop_column('gross_amount')
        batch_op.drop_column('currency')
        batch_op.drop_column('contract_date')
        batch_op.drop_column('current_source_fragment_id')
        batch_op.drop_column('updated_at')
        batch_op.create_index(batch_op.f('ix_contracts_counterparty'), ['counterparty'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('contracts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_contracts_counterparty'))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('current_source_fragment_id', sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column('contract_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('currency', sa.String(length=8), nullable=True))
        batch_op.add_column(sa.Column('gross_amount', sa.Numeric(precision=18, scale=2), nullable=True))
        batch_op.add_column(sa.Column('buyer', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('contract_type', sa.String(), nullable=True))

    # Restore each anchor's business values from its CURRENT revision
    # only — SUPPLEMENT/CORRECTION history created after upgrade is
    # intentionally not representable in the pre-R5 shape and is dropped
    # by downgrade, exactly like ContractItem's own downgrade.
    connection = op.get_bind()
    current_revisions = connection.execute(
        sa.text(
            "SELECT contract_id, contract_type, buyer, gross_amount, currency, contract_date, "
            "source_fragment_id, created_at FROM contract_revisions WHERE superseded_by_revision_id IS NULL"
        )
    ).fetchall()
    for row in current_revisions:
        connection.execute(
            sa.text(
                "UPDATE contracts SET contract_type=:contract_type, buyer=:buyer, "
                "gross_amount=:gross_amount, currency=:currency, contract_date=:contract_date, "
                "current_source_fragment_id=:source_fragment_id, updated_at=:updated_at WHERE id=:id"
            ),
            {
                "id": row.contract_id,
                "contract_type": row.contract_type,
                "buyer": row.buyer,
                "gross_amount": row.gross_amount,
                "currency": row.currency,
                "contract_date": row.contract_date,
                "source_fragment_id": row.source_fragment_id,
                "updated_at": row.created_at,
            },
        )
    # A NULL currency/gross_amount would violate the pre-R5 NOT NULL
    # columns just re-added above; downgrade only ever runs against data
    # this same migration chain produced, where both were always
    # required at creation time (bel.application.contract_facts), so
    # this is a safety net, not an expected path.
    connection.execute(sa.text("UPDATE contracts SET currency='CNY' WHERE currency IS NULL"))
    connection.execute(sa.text("UPDATE contracts SET gross_amount=0 WHERE gross_amount IS NULL"))
    connection.execute(sa.text("UPDATE contracts SET updated_at=created_at WHERE updated_at IS NULL"))

    with op.batch_alter_table('contracts', schema=None) as batch_op:
        batch_op.alter_column('gross_amount', existing_type=sa.Numeric(precision=18, scale=2), nullable=False)
        batch_op.alter_column('currency', existing_type=sa.String(length=8), nullable=False)
        batch_op.alter_column('current_source_fragment_id', existing_type=sa.Uuid(), nullable=False)
        batch_op.alter_column('updated_at', existing_type=sa.DateTime(), nullable=False)

    op.drop_index('uq_contract_revisions_one_initial', table_name='contract_revisions')
    op.drop_index('uq_contract_revisions_one_current', table_name='contract_revisions')
    with op.batch_alter_table('contract_revisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_contract_revisions_superseded_by_revision_id'))
        batch_op.drop_index(batch_op.f('ix_contract_revisions_contract_id'))

    op.drop_table('contract_revisions')

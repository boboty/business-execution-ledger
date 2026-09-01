"""Phase 2D.3-F1c — Canonical Export Declaration Amount.

The Shipment/Export Fact (the EXISTING Shipment model — deliberately NOT
a new ExportDeclaration aggregate) now carries two new versioned business
values, ``declared_amount`` and ``declared_currency``, closing the
canonical Fact gap recorded by docs/PHASE2D3-RULE-FREEZE.md IP-S02. They
are NOT identity fields (Shipment identity stays
``(contract_id, external_reference, execution_date)``); they live on
``ShipmentRevision`` and resolve through the current-revision mechanism
with the exact INITIAL/SUPPLEMENT/CORRECTION semantics ``quantity``
already had — because ``SHIPMENT_FACT_FIELDS`` is the single source of
truth driving ``_normalized``/``_revision_values``/``_validate_fields``,
the two fields flow through the application layer with zero functional
change.

Boundaries asserted throughout (all public, no private-derived values):

- the declaration amount is the amount explicitly stated by the
  confirmed export/customs declaration Evidence — never inferred, never
  FX-converted, never defaulted to a currency, and never substituted
  from ``quantity`` or ``Contract.gross_amount`` / SalesContract;
- an amount known without its currency (or vice versa) is a representable
  incomplete Fact;
- ``asserted_field_names`` still captures exactly the fields the writer
  actually supplied; exact replay still requires identity + fragment +
  intent + asserted content.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.invoice_preparation import get_invoice_preparation_context
from bel.application.shipment_facts import (
    ShipmentFactError,
    correct_shipment_fact,
    create_shipment_fact,
    execute_create_shipment_fact,
    get_shipment_history,
    list_shipments_for_contract,
    supplement_shipment_fact,
)
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.shipment import ShipmentRevisionType
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    ShipmentRepository,
)

NOW = datetime.now(timezone.utc)
EXEC_DATE = date(2031, 3, 10)
DECLARED_AMOUNT = Decimal("12345.67")
DECLARED_CURRENCY = "USD"


def _make_fragment(session, raw_data=None):
    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    EvidenceRepository(session).add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=doc.id,
        fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None,
        row_number=None,
        locator_json={"section": "f1c", "index": 0},
        raw_data=raw_data or {},
        created_at=NOW,
    )
    EvidenceRepository(session).add_fragment(frag)
    session.flush()
    return frag


def _make_contract(session, fragment_id, gross_amount=Decimal("9999.99"), contract_no=None):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no or f"C-F1C-{uuid.uuid4().hex[:8]}",
        contract_type=None,
        counterparty="Supplier",
        buyer="Buyer Co",
        gross_amount=gross_amount,
        currency="CNY",
        contract_date=None,
        current_source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


def _create_shipment(
    db_session, fields=None, external_reference="EXP-001", execution_date=EXEC_DATE, contract=None
):
    frag = _make_fragment(db_session)
    contract = contract or _make_contract(db_session, frag.id)
    result = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference=external_reference,
        execution_date=execution_date,
        fields=fields or {},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()
    return result.shipment, contract


# ---------------------------------------------------------------------------
# INITIAL create
# ---------------------------------------------------------------------------


def test_create_initial_shipment_carries_declared_amount_and_currency(db_session):
    """The declaration values are ordinary INITIAL-revision business
    values: assert both and they are present on the current projection,
    on the persisted revision, and nowhere else."""
    shipment, _ = _create_shipment(
        db_session,
        fields={"quantity": Decimal("10"), "declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY},
    )

    assert shipment.declared_amount == DECLARED_AMOUNT
    assert shipment.declared_currency == DECLARED_CURRENCY
    # Identity is untouched — still (contract_id, external_reference,
    # execution_date); the two fields are values, not identity.
    assert shipment.external_reference == "EXP-001"
    assert shipment.execution_date == EXEC_DATE

    history = get_shipment_history(db_session, shipment.id)
    assert len(history) == 1
    rev = history[0]
    assert rev.revision_type == ShipmentRevisionType.INITIAL
    assert rev.declared_amount == DECLARED_AMOUNT
    assert rev.declared_currency == DECLARED_CURRENCY


def test_create_without_declared_fields_leaves_both_none(db_session):
    """A Shipment asserted without any declaration Evidence stays a
    representable incomplete Fact: both values None — never a defaulted
    zero, never a defaulted currency."""
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")})

    assert shipment.declared_amount is None
    assert shipment.declared_currency is None

    rev = get_shipment_history(db_session, shipment.id)[0]
    assert rev.declared_amount is None
    assert rev.declared_currency is None
    assert rev.asserted_field_names == ["quantity"]


def test_amount_without_currency_is_representable_and_never_defaulted(db_session):
    """An amount known without its currency is a representable incomplete
    Fact — and the missing currency is NEVER defaulted to CNY/USD. The
    reverse (currency without amount) is equally representable."""
    amount_only, _ = _create_shipment(
        db_session, external_reference="EXP-AMT", fields={"declared_amount": DECLARED_AMOUNT}
    )
    assert amount_only.declared_amount == DECLARED_AMOUNT
    assert amount_only.declared_currency is None  # no implicit CNY/USD default

    currency_only, _ = _create_shipment(
        db_session, external_reference="EXP-CCY", fields={"declared_currency": DECLARED_CURRENCY}
    )
    assert currency_only.declared_currency == DECLARED_CURRENCY
    assert currency_only.declared_amount is None


# ---------------------------------------------------------------------------
# Supplement
# ---------------------------------------------------------------------------


def test_supplement_adds_declared_fields_to_previously_unknown_shipment(db_session):
    """A Shipment created bare can be supplemented with the declaration
    values once real Evidence exists — the SUPPLEMENT revision carries
    only the newly-asserted fields and the projection resolves them."""
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    result = supplement_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert result.revision_written is True
    assert result.shipment.declared_amount == DECLARED_AMOUNT
    assert result.shipment.declared_currency == DECLARED_CURRENCY
    assert result.shipment.quantity == Decimal("10")  # carried forward unchanged

    history = get_shipment_history(db_session, shipment.id)
    assert [r.revision_type for r in history] == [
        ShipmentRevisionType.INITIAL,
        ShipmentRevisionType.SUPPLEMENT,
    ]
    supplement_rev = history[1]
    assert supplement_rev.declared_amount == DECLARED_AMOUNT
    assert supplement_rev.asserted_field_names == ["declared_amount", "declared_currency"]
    # The retired INITIAL revision's own values never change.
    assert history[0].declared_amount is None


def test_supplement_declared_fields_replay_is_idempotent(db_session):
    """Exact replay of a declared-field supplement — same anchor, same
    fragment, same intent, same content — writes nothing new."""
    shipment, _ = _create_shipment(db_session)
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    first = supplement_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    replay = supplement_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,  # deliberately stale — replay must still resolve
        fields={"declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert replay.revision_written is False
    assert replay.replay is True
    assert replay.shipment.id == first.shipment.id
    assert len(get_shipment_history(db_session, shipment.id)) == 2


def test_supplement_conflicting_declared_amount_requires_correction(db_session):
    """Supplementing an already-known, DIFFERENT declaration amount is
    rejected as a conflict — that is a correction, never an inferred
    merge."""
    shipment, _ = _create_shipment(
        db_session, fields={"declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY}
    )
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    with pytest.raises(ShipmentFactError):
        supplement_shipment_fact(
            db_session,
            shipment_id=shipment.id,
            based_on_revision_id=current.id,
            fields={"declared_amount": Decimal("99999.99")},
            source_fragment_id=frag2.id,
            created_at=NOW,
        )
    db_session.rollback()
    assert len(get_shipment_history(db_session, shipment.id)) == 1
    assert ShipmentRepository(db_session).get(shipment.id).declared_amount == DECLARED_AMOUNT


# ---------------------------------------------------------------------------
# Correction
# ---------------------------------------------------------------------------


def test_correction_supersedes_declared_amount_without_mutating_history(db_session):
    """A wrong declaration amount is corrected in a NEW revision; the
    superseded revision retains its original value forever."""
    shipment, _ = _create_shipment(
        db_session, fields={"declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY}
    )
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)

    result = correct_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"declared_amount": Decimal("54321.00")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    assert result.shipment.declared_amount == Decimal("54321.00")
    # Currency was not re-asserted and is carried forward unchanged.
    assert result.shipment.declared_currency == DECLARED_CURRENCY

    history = get_shipment_history(db_session, shipment.id)
    assert len(history) == 2
    assert history[0].declared_amount == DECLARED_AMOUNT  # history never mutated
    assert history[0].superseded_by_revision_id == history[1].id
    assert history[1].revision_type == ShipmentRevisionType.CORRECTION
    assert history[1].asserted_field_names == ["declared_amount"]

    # The current projection now resolves only the corrected value.
    assert ShipmentRepository(db_session).get(shipment.id).declared_amount == Decimal("54321.00")


def test_current_projection_returns_only_current_declared_values(db_session):
    """After a correction, list_for_contract / get return the current
    declared values — the projection never mixes retired revisions."""
    shipment, contract = _create_shipment(
        db_session, fields={"declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY}
    )
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)
    correct_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"declared_amount": Decimal("54321.00"), "declared_currency": "EUR"},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    listed = list_shipments_for_contract(db_session, contract.id)
    assert len(listed) == 1
    assert listed[0].declared_amount == Decimal("54321.00")
    assert listed[0].declared_currency == "EUR"

    shown = ShipmentRepository(db_session).get(shipment.id)
    assert shown.declared_amount == Decimal("54321.00")
    assert shown.declared_currency == "EUR"


def test_create_replay_with_declared_fields_is_idempotent(db_session):
    """Exact replay of a declared-field create — same identity, same
    fragment, same assertion — writes nothing new."""
    shipment, contract = _create_shipment(
        db_session,
        fields={"declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY},
    )
    frag = ShipmentRepository(db_session).get_current_revision(shipment.id).source_fragment_id

    replay = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference="EXP-001",
        execution_date=EXEC_DATE,
        fields={"declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY},
        source_fragment_id=frag,
        created_at=NOW,
    )
    db_session.commit()

    assert replay.created is False
    assert replay.revision_written is False
    assert replay.replay is True
    assert replay.shipment.id == shipment.id
    assert len(get_shipment_history(db_session, shipment.id)) == 1


# ---------------------------------------------------------------------------
# No substitution / no inference
# ---------------------------------------------------------------------------


def test_quantity_is_never_read_as_declared_amount(db_session):
    """`quantity` and `declared_amount` are independent stored values.
    Asserting only a quantity never fabricates a declaration amount."""
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")})
    assert shipment.quantity == Decimal("10")
    assert shipment.declared_amount is None

    # Both asserted: each keeps its own value, nothing is copied either way.
    both, _ = _create_shipment(
        db_session,
        external_reference="EXP-BOTH",
        fields={"quantity": Decimal("10"), "declared_amount": DECLARED_AMOUNT},
    )
    assert both.quantity == Decimal("10")
    assert both.declared_amount == DECLARED_AMOUNT


def test_contract_gross_amount_never_substituted_as_declared_amount(db_session):
    """The procurement Contract carries gross_amount=9999.99 / CNY. No
    code path may copy it into the declaration amount/currency — the
    declaration must come from real Evidence or stay None."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, gross_amount=Decimal("9999.99"))
    shipment, _ = _create_shipment(db_session, fields={"quantity": Decimal("10")}, contract=contract)

    assert shipment.declared_amount is None
    assert shipment.declared_currency is None

    # Even a later explicit declaration is the asserted value — never the
    # contract's own amount/currency.
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)
    supplemented = supplement_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()
    assert supplemented.shipment.declared_amount == DECLARED_AMOUNT
    assert supplemented.shipment.declared_currency == DECLARED_CURRENCY
    assert supplemented.shipment.declared_amount != contract.gross_amount
    assert supplemented.shipment.declared_currency != contract.currency


# ---------------------------------------------------------------------------
# Evidence + validation
# ---------------------------------------------------------------------------


def test_declared_fields_require_real_evidence(db_session):
    """The declaration values, like every Shipment field, require a real
    EvidenceFragment — never fabricable without Evidence."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    with pytest.raises(ShipmentFactError):
        create_shipment_fact(
            db_session,
            contract_id=contract.id,
            external_reference="EXP-001",
            execution_date=EXEC_DATE,
            fields={"declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY},
            source_fragment_id=uuid.uuid4(),  # nonexistent fragment
            created_at=NOW,
        )


def test_unknown_declared_field_is_rejected(db_session):
    """SHIPMENT_FACT_FIELDS is the single source of truth: a misspelled
    declaration field is rejected exactly like any unknown field."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    with pytest.raises(ShipmentFactError):
        create_shipment_fact(
            db_session,
            contract_id=contract.id,
            external_reference="EXP-001",
            execution_date=EXEC_DATE,
            fields={"declared_amout": DECLARED_AMOUNT},  # typo
            source_fragment_id=frag.id,
            created_at=NOW,
        )


# ---------------------------------------------------------------------------
# execute_* wrapper (the CLI/Web path)
# ---------------------------------------------------------------------------


def test_execute_create_with_declared_fields_is_idempotent(db_session):
    """The human/CLI path builds MANUAL_FACT Evidence (a human
    confirmation IS Evidence) and round-trips the declared fields, and
    exact replay resolves to the same anchor."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    db_session.commit()

    created = execute_create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference="EXP-001",
        execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10"), "declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY},
    )
    assert created.created is True
    assert created.shipment.declared_amount == DECLARED_AMOUNT
    assert created.shipment.declared_currency == DECLARED_CURRENCY

    replay = execute_create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference="EXP-001",
        execution_date=EXEC_DATE,
        fields={"quantity": Decimal("10"), "declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY},
    )
    assert replay.created is False
    assert replay.replay is True
    assert replay.shipment.id == created.shipment.id
    assert len(get_shipment_history(db_session, created.shipment.id)) == 1


# ---------------------------------------------------------------------------
# F0 invoice-preparation context exposure
# ---------------------------------------------------------------------------


def test_f0_context_exposes_current_declaration_values_on_shipment(db_session):
    """Phase 2D.3-F0's rule-neutral context naturally exposes the current
    Shipment with its declaration values — no new Decision, no new DTO;
    the projection flows through unchanged. The corrected value (not a
    retired one) is what the context shows."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, gross_amount=Decimal("1000.00"), contract_no="PO-F1C-001")
    db_session.commit()

    shipment, _ = _create_shipment(
        db_session,
        fields={"declared_amount": DECLARED_AMOUNT, "declared_currency": DECLARED_CURRENCY},
        contract=contract,
    )
    current = ShipmentRepository(db_session).get_current_revision(shipment.id)
    frag2 = _make_fragment(db_session)
    correct_shipment_fact(
        db_session,
        shipment_id=shipment.id,
        based_on_revision_id=current.id,
        fields={"declared_amount": Decimal("54321.00")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    ctx = get_invoice_preparation_context(db_session)
    assert len(ctx.supplier_scopes) == 1
    supplier_scope = ctx.supplier_scopes[0]
    assert len(supplier_scope.shipments) == 1
    exposed = supplier_scope.shipments[0]
    # The context exposes the CURRENT declaration values — never a retired
    # revision and never a defaulted/derived value.
    assert exposed.declared_amount == Decimal("54321.00")
    assert exposed.declared_currency == DECLARED_CURRENCY
    assert exposed.id == shipment.id

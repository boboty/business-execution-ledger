"""Regression tests for Codex Gate A findings:

1. A default-period GET (or contract search) must never autoflush a
   pending unflushed object — strictly zero-write, session.new/dirty
   preserved.
2. The manual allocation must reject zero/negative quantity, negative
   net amount, and net amount beyond the invoice line — 400, no writes.
3. A duplicated identical POST must be a clean 400 (duplicate), never a
   500, and must add no rows.
4. Same-origin gate: a non-matching Origin header is rejected server-side
   (403); a matching Origin is accepted.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from bel.infrastructure.persistence.repositories import ContractRepository
from bel.infrastructure.persistence.models import AccrualModel
from bel.web import routes as routes_mod

NOW = datetime.now(timezone.utc)
WEB_PERIOD = "2031-03"


def _counts(session_factory) -> dict[str, int]:
    from bel.infrastructure.persistence.models import (
        EvidenceDocumentModel,
        EvidenceFragmentModel,
        InvoiceItemAllocationModel,
    )

    with session_factory() as session:
        return {
            "allocation": session.query(InvoiceItemAllocationModel).count(),
            "document": session.query(EvidenceDocumentModel).count(),
            "fragment": session.query(EvidenceFragmentModel).count(),
        }


def _pending_injection(app):
    """Override the route session dependency with one that already holds a
    pending, unflushed AccrualModel. Captures total_changes and the pending
    set in the generator's post-yield cleanup (runs after the response)."""
    factory = app.state.session_factory
    holder: dict = {}

    def _inject():
        session = factory()
        pending = AccrualModel(
            id=uuid.uuid4(),
            period="2031-05",
            contract_item_id=uuid.uuid4(),
            quantity=Decimal("10"),
            estimated_cost=Decimal("99.00"),
            basis="MANUAL_CONFIRMED",
            status="ACTIVE",
            created_from_fact_id=uuid.uuid4(),
            created_at=NOW,
        )
        session.add(pending)
        conn = session.connection()
        holder["before"] = conn.connection.total_changes
        holder["session"] = session
        holder["pending"] = pending
        yield session
        holder["after"] = conn.connection.total_changes
        holder["pending_still_new"] = pending in session.new
        holder["dirty"] = list(session.dirty)
        session.close()

    app.dependency_overrides[routes_mod._session] = _inject
    return holder


def _restore(app):
    app.dependency_overrides.clear()


def _contract_id(app, no: str) -> str:
    with app.state.session_factory() as session:
        contract = next(c for c in ContractRepository(session).list_all() if c.contract_no == no)
        return str(contract.id)


def _valid_payload(app) -> dict:
    return {
        "invoice_external_key": "DIGITAL-CLOSE-006",
        "line_no": 1,
        "contract_id": _contract_id(app, "PO-CLOSE-006"),
        "source_item_key": "ITEM-A",
        "quantity": "50",
        "net_amount": "950.00",
    }


# ---- Gate A blocker 1: default-period / search must not autoflush. ----


def test_default_period_get_preserves_pending_and_writes_nothing(app_for_client):
    client, app = app_for_client
    holder = _pending_injection(app)
    try:
        response = client.get("/period-close")
        assert response.status_code == 200
    finally:
        _restore(app)
    assert holder["after"] == holder["before"], "default-period GET flushed a pending object"
    assert holder["pending_still_new"] is True, "pending object must stay pending (never flushed)"
    assert holder["dirty"] == [], "no modified objects"


def test_explicit_period_get_preserves_pending_and_writes_nothing(app_for_client):
    client, app = app_for_client
    holder = _pending_injection(app)
    try:
        response = client.get(f"/period-close?period={WEB_PERIOD}")
        assert response.status_code == 200
    finally:
        _restore(app)
    assert holder["after"] == holder["before"]
    assert holder["pending_still_new"] is True
    assert holder["dirty"] == []


def test_contract360_default_period_preserves_pending(app_for_client):
    client, app = app_for_client
    contract_id = _contract_id(app, "PO-CLOSE-001")
    holder = _pending_injection(app)
    try:
        response = client.get(f"/contracts/{contract_id}")
        assert response.status_code == 200
    finally:
        _restore(app)
    assert holder["after"] == holder["before"], "Contract360 default-period GET flushed a pending object"
    assert holder["pending_still_new"] is True
    assert holder["dirty"] == []


def test_contract360_explicit_period_preserves_pending(app_for_client):
    client, app = app_for_client
    contract_id = _contract_id(app, "PO-CLOSE-001")
    holder = _pending_injection(app)
    try:
        response = client.get(f"/contracts/{contract_id}?period={WEB_PERIOD}")
        assert response.status_code == 200
    finally:
        _restore(app)
    assert holder["after"] == holder["before"], "Contract360 explicit-period GET flushed a pending object"
    assert holder["pending_still_new"] is True
    assert holder["dirty"] == []


def test_contract_search_preserves_pending(app_for_client):
    client, app = app_for_client
    holder = _pending_injection(app)
    try:
        response = client.get("/contracts/search?no=PO-CLOSE-001", follow_redirects=False)
        assert response.status_code == 302  # single match redirect
    finally:
        _restore(app)
    assert holder["after"] == holder["before"], "contract search flushed a pending object"
    assert holder["pending_still_new"] is True
    assert holder["dirty"] == []


# ---- Gate A blocker 2: zero/negative/over-limit allocation rejected. ----


def test_allocation_rejects_zero_and_negative_quantity(app_for_client):
    client, app = app_for_client
    before = _counts(app.state.session_factory)
    for quantity in ["0", "-1"]:
        payload = _valid_payload(app)
        payload["quantity"] = quantity
        response = client.post("/api/invoice-item-allocations", json=payload)
        assert response.status_code == 400, f"quantity={quantity} must be rejected"
    assert _counts(app.state.session_factory) == before, "no rows may be written"


def test_allocation_rejects_negative_and_over_limit_net_amount(app_for_client):
    client, app = app_for_client
    before = _counts(app.state.session_factory)
    for net_amount in ["-1", "999999999"]:
        payload = _valid_payload(app)
        payload["net_amount"] = net_amount
        response = client.post("/api/invoice-item-allocations", json=payload)
        assert response.status_code == 400, f"net_amount={net_amount} must be rejected"
    assert _counts(app.state.session_factory) == before


# ---- Gate A blocker 3: duplicate POST is a clean 400. ----


def test_duplicate_post_is_400_not_500(app_for_client):
    client, app = app_for_client
    payload = _valid_payload(app)
    # partial allocation so the capacity guard cannot mask the duplicate
    payload["quantity"] = "5"
    payload["net_amount"] = "95.00"

    first = client.post("/api/invoice-item-allocations", json=payload)
    assert first.status_code == 201
    after_first = _counts(app.state.session_factory)

    second = client.post("/api/invoice-item-allocations", json=payload)
    assert second.status_code == 400, "a duplicated identical POST must be rejected cleanly"
    assert "duplicate" in second.json()["detail"].lower()
    assert _counts(app.state.session_factory) == after_first, "the duplicate must add zero rows"


def test_duplicate_post_is_also_clean_when_capacity_is_reached(app_for_client):
    """A retried full-line POST trips the capacity guard instead of the
    duplicate guard — still a clean 400, never a 500, zero rows added."""
    client, app = app_for_client
    payload = _valid_payload(app)
    assert client.post("/api/invoice-item-allocations", json=payload).status_code == 201
    after_first = _counts(app.state.session_factory)

    second = client.post("/api/invoice-item-allocations", json=payload)
    assert second.status_code == 400
    assert _counts(app.state.session_factory) == after_first


# ---- Gate A warning: server-side same-origin check. ----


def test_cross_origin_post_rejected_with_no_writes(app_for_client):
    client, app = app_for_client
    before = _counts(app.state.session_factory)
    response = client.post(
        "/api/invoice-item-allocations",
        json=_valid_payload(app),
        headers={"Origin": "https://attacker.invalid"},
    )
    assert response.status_code == 403
    assert _counts(app.state.session_factory) == before


def test_same_origin_post_accepted(app_for_client):
    client, app = app_for_client
    response = client.post(
        "/api/invoice-item-allocations",
        json=_valid_payload(app),
        headers={"Origin": "http://testserver"},  # TestClient base origin
    )
    assert response.status_code == 201

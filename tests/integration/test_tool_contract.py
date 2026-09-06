"""Synthetic operator workflow; only setup/assertions access persistence."""
import json

import pytest
from sqlalchemy import select, func

from bel.application.tool_operations import ToolOperations
from bel.infrastructure.persistence.database import make_session_factory
from bel.infrastructure.persistence.models import InvoiceAllocationModel, MatchCaseModel, BusinessEventModel
from bel.tools import ToolContract
from tests.unit.test_matching_engine import _make_fragment, _make_contract, _make_invoice


def call(tool, name, **args):
    request = {"version": "1", "request_id": "synthetic-request", "capability": "procurement." + name, "arguments": args}
    return json.loads(json.dumps(tool.call(json.loads(json.dumps(request)))))


def seed(session):
    fragment = _make_fragment(session)
    _make_contract(session, fragment.id, "Synthetic Unique", "17")
    _make_contract(session, fragment.id, "Synthetic Ambiguous", "23")
    _make_contract(session, fragment.id, "Synthetic Ambiguous", "23")
    unique = _make_invoice(session, fragment.id, "Synthetic Unique", "17")
    ambiguous = _make_invoice(session, fragment.id, "Synthetic Ambiguous", "23")
    outside = _make_invoice(session, fragment.id, "Synthetic Outside", "5")
    sales = _make_invoice(session, fragment.id, "Synthetic Unique", "17", direction="SALES")
    session.commit()
    return unique, ambiguous, outside, sales


def counts(session):
    return tuple(session.scalar(select(func.count()).select_from(m)) for m in
                 (InvoiceAllocationModel, MatchCaseModel, BusinessEventModel))


def exercise(session):
    unique, ambiguous, outside, sales = seed(session)
    sessions = make_session_factory(session.get_bind())
    tool = ToolContract(ToolOperations(sessions), allow_matching=True)
    before = counts(session)
    work = call(tool, "invoice_work")
    assert len(work["data"]["invoices"]) == 3
    assert all(i["match_status"] is None for i in work["data"]["invoices"])
    detail = call(tool, "inspect_invoice", invoice_id=str(unique.id))
    assert detail["data"]["gross_amount"] == "17.00"
    assert detail["data"]["evidence"]["fragment_id"] == str(unique.source_fragment_id)
    assert detail["data"]["currency"] is None
    assert call(tool, "inspect_invoice", invoice_id=str(sales.id))["status"] == "NOT_FOUND"
    assert counts(session) == before
    assert call(ToolContract(ToolOperations(sessions)), "match_invoices")["status"] == "FORBIDDEN"
    result = call(tool, "match_invoices")
    assert result["status"] == "OK"
    assert result["data"]["auto_confirmed"] == 1
    assert result["data"]["human_confirmation_required"] == 1
    work = call(tool, "invoice_work")["data"]
    states = {i["invoice_id"]: i["match_status"] for i in work["invoices"]}
    assert states[str(unique.id)] == "AUTO_CONFIRMED"
    assert states[str(ambiguous.id)] == "HUMAN_CONFIRMATION_REQUIRED"
    assert states[str(outside.id)] is None
    human = next(w for w in work["human_work"] if w["invoice_id"] == str(ambiguous.id))
    assert human["source_type"] == "MATCH_CASE"
    assert human["resolution_route"] == "CONFIRM_MATCH"
    assert len(human["scopes"]) == 2
    decision = call(tool, "inspect_invoice", invoice_id=str(unique.id))["data"]
    assert len(decision["allocations"]) == 1
    assert decision["allocations"][0]["confirmation_type"] == "AUTO_CONFIRMED"
    assert call(tool, "inspect_invoice", invoice_id=str(ambiguous.id))["data"]["allocations"] == []
    after = counts(session)
    assert call(tool, "match_invoices")["data"]["already_matched_skipped"] == 2
    assert counts(session) == after
    assert call(tool, "confirm_match", human_confirmed=True)["status"] == "UNKNOWN_CAPABILITY"
    assert counts(session) == after
    return tool


def test_operator_vertical_slice(db_session):
    exercise(db_session)


@pytest.mark.parametrize("payload", [None, [], {}, {"version": "2", "request_id": "x", "capability": "x", "arguments": {}},
    {"version": "1", "request_id": "x", "capability": [], "arguments": {}},
    {"version": "1", "request_id": "x", "capability": "procurement.match_invoices", "arguments": {"human_confirmed": True}},
    {"version": "1", "request_id": "x", "capability": "procurement.inspect_invoice", "arguments": {"invoice_id": "bad"}}])
def test_invalid_requests_never_reach_application(payload):
    assert ToolContract(None).call(payload)["status"] == "INVALID_REQUEST"


def test_failure_does_not_leak_diagnostics():
    class Broken:
        def run_matching(self):
            raise RuntimeError("synthetic internal diagnostic")
        def list_work(self):
            raise RuntimeError("synthetic internal diagnostic")
    tool = ToolContract(Broken(), allow_matching=True)
    assert call(tool, "match_invoices")["status"] == "OUTCOME_UNKNOWN"
    response = call(tool, "invoice_work")
    assert response["status"] == "INTERNAL_ERROR"
    assert "diagnostic" not in json.dumps(response)


def test_host_rejects_sqlite():
    from bel.infrastructure.tool_host import create_tool_contract
    with pytest.raises(ValueError, match="postgresql"):
        create_tool_contract("sqlite://")


def test_existing_human_resolution_is_preserved(db_session):
    from uuid import UUID
    from bel.application.matching import confirm_match
    tool = exercise(db_session)
    work = call(tool, "invoice_work")["data"]["human_work"][0]
    confirm_match(db_session, UUID(work["source_id"]), UUID(work["scopes"][0]["scope_id"]))
    db_session.commit()
    before = counts(db_session)
    assert call(tool, "match_invoices")["status"] == "OK"
    assert counts(db_session) == before
    refreshed = call(tool, "invoice_work")["data"]
    assert refreshed["human_work"] == []
    assert any(i["match_status"] == "RESOLVED" for i in refreshed["invoices"])


def test_retry_processes_new_inputs_and_preserves_capacity_guard(db_session):
    unique, _, _, _ = seed(db_session)
    tool = ToolContract(ToolOperations(make_session_factory(db_session.get_bind())), allow_matching=True)
    call(tool, "match_invoices")
    excess = _make_invoice(db_session, unique.source_fragment_id, "Synthetic Unique", "17")
    unmatched = _make_invoice(db_session, unique.source_fragment_id, "Synthetic Unique", "19")
    db_session.commit()
    result = call(tool, "match_invoices")["data"]
    assert result["capacity_exceeded"] == 1
    assert result["unmatched"] == 1
    assert call(tool, "inspect_invoice", invoice_id=str(excess.id))["data"]["allocations"] == []
    states = {i["invoice_id"]: i["match_status"] for i in call(tool, "invoice_work")["data"]["invoices"]}
    assert states[str(excess.id)] == "HUMAN_CONFIRMATION_REQUIRED"
    assert states[str(unmatched.id)] == "UNMATCHED"

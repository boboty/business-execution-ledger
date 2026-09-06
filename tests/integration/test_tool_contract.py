"""Synthetic operator workflow; only setup/assertions access persistence."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest
from sqlalchemy import select, func

from bel.application.tool_operations import ToolOperations
from bel.domain.matching import (
    AllocationMatchMethod,
    ConfirmationType,
    InvoiceAllocation,
    MatchCandidate,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
    SubjectType,
)
from bel.infrastructure.persistence.database import make_session_factory
from bel.infrastructure.persistence.models import InvoiceAllocationModel, MatchCaseModel, BusinessEventModel
from bel.infrastructure.persistence.repositories import (
    InvoiceAllocationRepository,
    MatchCandidateRepository,
    MatchCaseRepository,
)
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
    assert all(i["match_cases"] == [] for i in work["data"]["invoices"])
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
    states = {i["invoice_id"]: [case["match_status"] for case in i["match_cases"]] for i in work["invoices"]}
    assert states[str(unique.id)] == ["AUTO_CONFIRMED"]
    assert states[str(ambiguous.id)] == ["HUMAN_CONFIRMATION_REQUIRED"]
    assert states[str(outside.id)] == []
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
    assert any(case["match_status"] == "RESOLVED"
               for invoice in refreshed["invoices"] for case in invoice["match_cases"])


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
    states = {i["invoice_id"]: [case["match_status"] for case in i["match_cases"]]
              for i in call(tool, "invoice_work")["data"]["invoices"]}
    assert states[str(excess.id)] == ["HUMAN_CONFIRMATION_REQUIRED"]
    assert states[str(unmatched.id)] == ["UNMATCHED"]


def test_invoice_work_preserves_all_match_cases_and_orders_them(db_session):
    fragment = _make_fragment(db_session)
    no_case = _make_invoice(db_session, fragment.id, "Synthetic None", "1")
    one_case = _make_invoice(db_session, fragment.id, "Synthetic One", "2")
    two_auto = _make_invoice(db_session, fragment.id, "Synthetic Multi", "30")
    mixed = _make_invoice(db_session, fragment.id, "Synthetic Mixed", "40")

    contract_one = _make_contract(db_session, fragment.id, "Synthetic One", "2")
    contract_multi_a = _make_contract(db_session, fragment.id, "Synthetic Multi", "10")
    contract_multi_b = _make_contract(db_session, fragment.id, "Synthetic Multi", "20")
    contract_mixed_auto = _make_contract(db_session, fragment.id, "Synthetic Mixed", "10")
    contract_mixed_hcr = _make_contract(db_session, fragment.id, "Synthetic Mixed", "30")

    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    case_ids = {
        "one": uuid.UUID(int=10),
        "multi_first": uuid.UUID(int=20),
        "multi_second": uuid.UUID(int=30),
        "mixed_hcr": uuid.UUID(int=40),
        "mixed_auto": uuid.UUID(int=50),
    }
    cases = [
        (case_ids["one"], one_case.id, MatchCaseStatus.AUTO_CONFIRMED, contract_one, Decimal("2")),
        # Insert the two legitimate cases in reverse UUID order so the assertion
        # proves output ordering does not depend on repository row order.
        (case_ids["multi_second"], two_auto.id, MatchCaseStatus.AUTO_CONFIRMED,
         contract_multi_b, Decimal("20")),
        (case_ids["multi_first"], two_auto.id, MatchCaseStatus.AUTO_CONFIRMED,
         contract_multi_a, Decimal("10")),
        (case_ids["mixed_auto"], mixed.id, MatchCaseStatus.AUTO_CONFIRMED,
         contract_mixed_auto, Decimal("10")),
        (case_ids["mixed_hcr"], mixed.id, MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
         contract_mixed_hcr, None),
    ]
    case_repo = MatchCaseRepository(db_session)
    candidate_repo = MatchCandidateRepository(db_session)
    allocation_repo = InvoiceAllocationRepository(db_session)
    for case_id, invoice_id, status, contract, allocated_amount in cases:
        case_repo.add(MatchCase(
            id=case_id,
            subject_type=SubjectType.INVOICE,
            subject_id=invoice_id,
            status=status,
            match_method=MatchMethod.M001,
            created_at=now,
            resolved_at=now if status == MatchCaseStatus.AUTO_CONFIRMED else None,
        ))
        db_session.flush()
        candidate_repo.add(MatchCandidate(
            id=uuid.uuid4(), match_case_id=case_id, contract_id=contract.id, created_at=now,
        ))
        if allocated_amount is not None:
            allocation_repo.add(InvoiceAllocation(
                id=uuid.uuid4(),
                invoice_id=invoice_id,
                contract_id=contract.id,
                match_case_id=case_id,
                allocated_gross_amount=allocated_amount,
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED,
                created_at=now,
            ))
    db_session.commit()

    tool = ToolContract(ToolOperations(make_session_factory(db_session.get_bind())))
    first = call(tool, "invoice_work")["data"]
    second = call(tool, "invoice_work")["data"]
    assert first == second

    invoices = {invoice["invoice_id"]: invoice for invoice in first["invoices"]}
    assert invoices[str(no_case.id)] == {"invoice_id": str(no_case.id), "match_cases": []}
    assert invoices[str(one_case.id)]["match_cases"] == [
        {"match_case_id": str(case_ids["one"]), "match_status": MatchCaseStatus.AUTO_CONFIRMED}
    ]
    assert invoices[str(two_auto.id)]["match_cases"] == [
        {"match_case_id": str(case_ids["multi_first"]), "match_status": MatchCaseStatus.AUTO_CONFIRMED},
        {"match_case_id": str(case_ids["multi_second"]), "match_status": MatchCaseStatus.AUTO_CONFIRMED},
    ]
    assert invoices[str(mixed.id)]["match_cases"] == [
        {"match_case_id": str(case_ids["mixed_hcr"]),
         "match_status": MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED},
        {"match_case_id": str(case_ids["mixed_auto"]), "match_status": MatchCaseStatus.AUTO_CONFIRMED},
    ]
    assert any(
        item["source_id"] == str(case_ids["mixed_hcr"])
        and item["invoice_id"] == str(mixed.id)
        and item["status"] == MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED
        for item in first["human_work"]
    )

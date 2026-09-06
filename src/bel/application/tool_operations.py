"""Deliberate procurement capabilities over the existing Application services.

Sessions are owned here, never supplied by an operator. No matching rule lives
in this projection; an empty match_cases list means no recorded case, not
eligibility.
"""
from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from bel.application.get_invoice import get_invoice
from bel.application.list_matches import list_match_cases
from bel.application.matching import match_invoices
from bel.application.unresolved_work_center import get_unresolved_work_center, UnresolvedWorkFilters
from bel.domain.invoice import InvoiceDirection
from bel.infrastructure.persistence.repositories import InvoiceRepository, InvoiceAllocationRepository


class ToolOperations:
    def __init__(self, session_factory):
        self._sessions = session_factory

    def list_work(self) -> dict:
        with self._sessions() as session:
            cases_by_invoice = defaultdict(list)
            for case in list_match_cases(session):
                if case.subject_type == "INVOICE":
                    cases_by_invoice[case.subject_id].append(case)
            invoices = [i for i in InvoiceRepository(session).list_all() if i.direction == InvoiceDirection.PURCHASE]
            center = get_unresolved_work_center(session, filters=UnresolvedWorkFilters())
            invoice_ids = {i.id for i in invoices}
            return {"invoices": [
                {"invoice_id": str(i.id),
                 "match_cases": [
                     {"match_case_id": str(case.id), "match_status": case.status}
                     for case in sorted(cases_by_invoice[i.id], key=lambda case: str(case.id))
                 ]}
                for i in sorted(invoices, key=lambda i: str(i.id))
            ], "human_work": [
                {"source_type": w.source_type, "source_id": str(w.source_id),
                 "code": w.code, "status": w.status, "invoice_id": str(w.invoice_id),
                 "resolution_route": w.resolution_route,
                 "scopes": [{"scope_type": s.scope_type, "scope_id": str(s.scope_id)} for s in w.scopes]}
                for w in center.items if w.invoice_id in invoice_ids
            ]}

    def inspect_invoice(self, invoice_id: UUID) -> dict | None:
        with self._sessions() as session:
            invoice = InvoiceRepository(session).get(invoice_id)
            if invoice is None or invoice.direction != InvoiceDirection.PURCHASE:
                return None
            trace = get_invoice(session, invoice_id)
            return {"invoice_id": str(invoice.id), "direction": invoice.direction,
                    "seller": invoice.seller, "gross_amount": str(invoice.gross_amount),
                    "currency": invoice.currency,
                    "allocations": [
                        {"allocation_id": str(a.id), "contract_id": str(a.contract_id),
                         "match_case_id": str(a.match_case_id),
                         "gross_amount": str(a.allocated_gross_amount),
                         "match_method": a.match_method, "confirmation_type": a.confirmation_type}
                        for a in sorted(InvoiceAllocationRepository(session).list_all(), key=lambda a: str(a.id))
                        if a.invoice_id == invoice_id
                    ],
                    "evidence": {"fragment_id": str(trace.fragment.id),
                                 "document_id": str(trace.document.id)}}

    def run_matching(self) -> dict:
        with self._sessions() as session:
            summary = match_invoices(session)  # Owns shared PG lock and atomic commit.
            return {"auto_confirmed": summary.auto_confirmed,
                    "human_confirmation_required": summary.human_confirmation_required,
                    "unmatched": summary.unmatched,
                    "capacity_exceeded": summary.capacity_exceeded,
                    "already_matched_skipped": summary.already_matched_skipped,
                    "out_of_scope": summary.out_of_scope,
                    "subject_ids": [str(i) for i in summary.subject_ids]}

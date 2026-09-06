"""JSON-value boundary. Composition and credentials belong to the trusted host."""
from __future__ import annotations

from typing import Protocol
from uuid import UUID


class ApplicationPort(Protocol):
    def list_work(self) -> dict: ...
    def inspect_invoice(self, invoice_id: UUID) -> dict | None: ...
    def run_matching(self) -> dict: ...


class ToolContract:
    """Only expose call/catalog to an operator; never pass the host's backend."""

    def __init__(self, application: ApplicationPort, *, allow_matching: bool = False):
        self._application = application
        self._allow_matching = allow_matching

    def catalog(self) -> dict:
        return {"version": "1", "capabilities": [
            {"name": "procurement.invoice_work", "arguments": {}, "effect": "READ"},
            {"name": "procurement.inspect_invoice", "arguments": {"invoice_id": "UUID"}, "effect": "READ"},
            {"name": "procurement.match_invoices", "arguments": {}, "effect": "WRITE",
             "enabled": self._allow_matching, "scope": "ALL_CURRENT_PURCHASE_INVOICES",
             "retry": "RECONCILE_CURRENT_STATE"},
        ]}

    def call(self, request: dict) -> dict:
        # Do not reflect malformed values or exception messages to the caller.
        request_id = request.get("request_id") if type(request) is dict else None
        if type(request_id) is not str or not 1 <= len(request_id) <= 100:
            request_id = None

        def result(status, data=None):
            return {"version": "1", "request_id": request_id, "status": status, "data": data}

        if type(request) is not dict or set(request) != {"version", "request_id", "capability", "arguments"}:
            return result("INVALID_REQUEST")
        if request_id is None or request["version"] != "1":
            return result("INVALID_REQUEST")
        name, args = request["capability"], request["arguments"]
        if type(name) is not str or type(args) is not dict:
            return result("INVALID_REQUEST")
        if name not in {c["name"] for c in self.catalog()["capabilities"]}:
            return result("UNKNOWN_CAPABILITY")
        if name == "procurement.inspect_invoice":
            if set(args) != {"invoice_id"} or type(args["invoice_id"]) is not str:
                return result("INVALID_REQUEST")
            try:
                invoice_id = UUID(args["invoice_id"])
            except ValueError:
                return result("INVALID_REQUEST")
        elif args:
            return result("INVALID_REQUEST")
        if name == "procurement.match_invoices" and not self._allow_matching:
            return result("FORBIDDEN")
        try:
            if name == "procurement.invoice_work":
                return result("OK", self._application.list_work())
            if name == "procurement.inspect_invoice":
                data = self._application.inspect_invoice(invoice_id)
                return result("NOT_FOUND" if data is None else "OK", data)
            return result("OK", self._application.run_matching())
        except Exception:
            # A lost response can follow a successful commit: never promise rollback
            # or a retryable failure here. Re-read state before deciding to retry.
            return result("OUTCOME_UNKNOWN" if name == "procurement.match_invoices" else "INTERNAL_ERROR")

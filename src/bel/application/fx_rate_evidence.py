"""FX rate confirmation — Core Completion (IP-S02).

Mirrors ``bel.application.contract_facts`` structurally: an Evidence
Fragment and the confirmed value it attests to are created together,
content-hash-deduplicated, so the fragment is the durable proof of the
assertion rather than a bare pointer a caller could pair with any value.

Core never fetches an FX rate from a website and never maintains a
holiday calendar (docs/PHASE2D3-RULE-FREEZE.md IP-S02). This module is
the ONLY sanctioned way to produce a confirmed FX Evidence fragment, and
``load_confirmed_fx_rate`` is the ONLY sanctioned way to read one back:
downstream calculation never accepts a caller-supplied source/date/rate
alongside a fragment id — it reconstructs the confirmed values entirely
from the fragment's own ``raw_data``, so there is nothing to forge by
pairing a claimed value with an unrelated (or non-existent) fragment.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.infrastructure.persistence.database import serialized_write_transaction
from bel.infrastructure.persistence.repositories import EvidenceRepository

FX_RATE_FACT_COMMAND = "fx-rate-confirm"


class FxRateEvidenceError(ValueError):
    """Raised for an invalid confirm input or an unconfirmed/foreign
    fragment presented as FX provenance."""


@dataclass(frozen=True)
class ApplicableFxRateEvidence:
    """A confirmed SAFE daily USD/CNY rate, reconstructed from its own
    Evidence fragment — never from caller-supplied values paired with a
    fragment id. Kept here (rather than only in
    ``sales_invoice_preparation``) so the write/read pair and the pure
    evaluation type live next to each other; re-exported from
    ``invoice_preparation`` for the existing import path."""

    source: str
    publication_date: date
    usd_cny_rate: Decimal
    provenance_fragment_id: uuid.UUID


def _validate_confirm_input(source: str, usd_cny_rate: Decimal) -> None:
    if not isinstance(source, str) or not source.strip():
        raise FxRateEvidenceError("source must be a non-blank string")
    if not isinstance(usd_cny_rate, Decimal) or not usd_cny_rate.is_finite() or usd_cny_rate <= 0:
        raise FxRateEvidenceError("usd_cny_rate must be a finite positive Decimal")


def _find_or_build_fx_fragment(
    session: Session, *, raw_data: dict, now: datetime
) -> EvidenceFragment:
    payload = json.dumps(raw_data, sort_keys=True).encode("utf-8")
    sha256 = hashlib.sha256(payload).hexdigest()
    evidence_repo = EvidenceRepository(session)
    existing_document = evidence_repo.find_document_by_sha256(sha256)
    if existing_document is not None:
        existing_fragment = evidence_repo.find_fragment_by_document(existing_document.id)
        if existing_fragment is not None:
            return existing_fragment
    document = EvidenceDocument(
        id=uuid.uuid4(), file_name=f"fx-rate-confirm-{now.isoformat()}.json", sha256=sha256,
        source_type="manual_fx_rate_fact", imported_at=now,
    )
    evidence_repo.add_document(document)
    fragment = EvidenceFragment(
        id=uuid.uuid4(), evidence_document_id=document.id, fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None, row_number=None, locator_json={"command": FX_RATE_FACT_COMMAND},
        raw_data=raw_data, created_at=now,
    )
    evidence_repo.add_fragment(fragment)
    session.flush()
    return fragment


def execute_confirm_fx_rate(
    session: Session, *, source: str, publication_date: date, usd_cny_rate: Decimal
) -> ApplicableFxRateEvidence:
    """The only sanctioned way to produce a confirmed FX Evidence
    fragment. Content-hash-deduplicated exactly like a manual contract
    fact: confirming the identical (source, date, rate) twice reuses the
    same fragment rather than creating a duplicate."""
    _validate_confirm_input(source, usd_cny_rate)
    with serialized_write_transaction(session):
        now = datetime.now(timezone.utc)
        raw_data = {
            "command": FX_RATE_FACT_COMMAND,
            "source": source,
            "publication_date": publication_date.isoformat(),
            "usd_cny_rate": str(usd_cny_rate),
        }
        fragment = _find_or_build_fx_fragment(session, raw_data=raw_data, now=now)
        return ApplicableFxRateEvidence(
            source=source, publication_date=publication_date, usd_cny_rate=usd_cny_rate,
            provenance_fragment_id=fragment.id,
        )


def load_confirmed_fx_rate(session: Session, provenance_fragment_id: uuid.UUID) -> ApplicableFxRateEvidence:
    """Reconstruct a confirmed FX rate entirely from its own Evidence
    fragment. Never trusts a caller-supplied source/date/rate: there is
    no such input to this function, so a fragment id naming an unrelated
    or non-existent fragment simply fails to resolve — nothing to forge
    by pairing a claimed value with someone else's fragment."""
    fragment = EvidenceRepository(session).get_fragment(provenance_fragment_id)
    if fragment is None:
        raise FxRateEvidenceError(f"FX provenance EvidenceFragment {provenance_fragment_id} not found")
    if fragment.fragment_kind != FragmentKind.MANUAL_FACT or fragment.raw_data.get("command") != FX_RATE_FACT_COMMAND:
        raise FxRateEvidenceError(
            f"EvidenceFragment {provenance_fragment_id} is not a confirmed FX rate fact"
        )
    try:
        source = fragment.raw_data["source"]
        pub_date = date.fromisoformat(fragment.raw_data["publication_date"])
        rate = Decimal(fragment.raw_data["usd_cny_rate"])
    except (KeyError, ValueError, ArithmeticError) as exc:
        raise FxRateEvidenceError(
            f"EvidenceFragment {provenance_fragment_id} FX rate fact is malformed"
        ) from exc
    _validate_confirm_input(source, rate)
    return ApplicableFxRateEvidence(
        source=source, publication_date=pub_date, usd_cny_rate=rate,
        provenance_fragment_id=fragment.id,
    )

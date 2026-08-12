"""Deterministic counterparty normalization only.

Phase 2A allows exactly: Unicode NFKC, trim, whitespace collapse, and
removal of line-break artifacts (from PDF/Excel text wrapping). It
explicitly forbids fuzzy matching, edit distance, LLM inference, or
embeddings -- two names the business can't prove identical stay two
names, and the system raises a Task rather than guessing. See spec
section 15 and docs/PHASE2A-DECISIONS.md.
"""

from __future__ import annotations

import re
import unicodedata

_LINEBREAK_CHARS_RE = re.compile("[" + chr(0x0D) + chr(0x0A) + chr(0x2028) + chr(0x2029) + "]+")
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def normalize_counterparty(name: str | None) -> str | None:
    """NFKC -> drop line-break artifacts entirely (they're wrap noise,
    not content) -> collapse remaining whitespace runs to one space ->
    trim. Never reinterprets characters or infers an alias."""
    if name is None:
        return None
    text = unicodedata.normalize("NFKC", name)
    text = _LINEBREAK_CHARS_RE.sub("", text)
    text = _WHITESPACE_RUN_RE.sub(" ", text)
    return text.strip()


def same_counterparty(a: str | None, b: str | None) -> bool:
    """Exact equality after normalization -- nothing more. Two names the
    business can't prove identical must compare unequal, even if they
    look related to a human (e.g. a legal-entity-suffix difference)."""
    na, nb = normalize_counterparty(a), normalize_counterparty(b)
    if na is None or nb is None:
        return False
    return na == nb

"""Hardened private-data-root / period containment and private report
writing (shared by the FIRST-STAGE CUTOVER GATE).

This module implements the R5 private-data boundary in ONE place so the
Gate never clones a weaker version of it (docs/FIRST-STAGE-CUTOVER-GATE.md
section 4). Every rule here is at least as strong as the discipline
``tests/private_acceptance/runner.py`` already enforces for its own
paths, and is deliberately reusable by any private-rooted command:

- ``BEL_PRIVATE_DATA_ROOT`` (or an explicitly-supplied root) must resolve
  to a real directory OUTSIDE the repository — real-data diagnostics and
  source files are never read from or written into the repo tree.
- A period is a closed ``YYYY-MM`` identifier, never an arbitrary path
  string: ``..``, an absolute path, and a same-looking symlinked period
  directory resolving outside the root are all rejected by the regex
  first and then re-checked AFTER symlink resolution.
- Required private INPUT files (``read_private_file``) are resolved with
  the same containment discipline as the period directory itself: every
  nested path component is resolved and required to remain inside the
  resolved root, outside the repository, and to be a regular file — a
  symlinked ``expected/`` directory, a symlinked plan/baseline file, or
  any path escaping the root is rejected before its content could be
  parsed. The read itself happens through an ``O_NOFOLLOW`` descriptor
  chain against the resolved parent, never by re-opening the original
  potentially-symlinked path.
- Reports are written ONLY under ``$BEL_PRIVATE_DATA_ROOT/reports/`` via
  a checked directory descriptor (``O_DIRECTORY`` + ``O_NOFOLLOW``) and a
  ``O_NOFOLLOW`` final component — a reports-directory or report-file
  symlink swap can never redirect diagnostics into the repository or
  anywhere else outside the private root.

This module is infrastructure, not business logic: it never reads or
interprets business values.
"""

from __future__ import annotations

import errno
import json
import os
import re
from pathlib import Path
from typing import Any

PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")

# Reason codes shared by the Gate's privacy_boundary dimension.
REASON_ROOT_NOT_SET = "PRIVATE_DATA_ROOT_NOT_SET"
REASON_ROOT_UNRESOLVABLE = "PRIVATE_DATA_ROOT_UNRESOLVABLE"
REASON_ROOT_INSIDE_REPO = "PRIVATE_DATA_ROOT_INSIDE_REPO"
REASON_ROOT_NOT_DIR = "PRIVATE_DATA_ROOT_NOT_DIR"
REASON_INVALID_PERIOD = "INVALID_PERIOD"
REASON_PERIOD_NOT_FOUND = "PERIOD_DIR_NOT_FOUND"
REASON_PERIOD_ESCAPE = "PERIOD_ESCAPE"
REASON_INPUT_MISSING = "PRIVATE_INPUT_MISSING"
REASON_INPUT_ESCAPE = "PRIVATE_INPUT_ESCAPE"

_REPORTS_DIR_NAME = "reports"


class PrivateRootError(ValueError):
    """A rejected private-root / period path or an unsafe report target.

    ``reason_code`` is the stable machine-readable code (one of the
    ``REASON_*`` constants above); ``message`` is a human-readable
    explanation safe for stdout (it never contains a business value).
    """

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def repo_root() -> Path:
    """The repository root (where ``alembic.ini`` lives), located by
    walking up from this file — independent of the caller's cwd."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "alembic.ini").is_file():
            return parent
    raise PrivateRootError(REASON_ROOT_UNRESOLVABLE, "could not locate the repository root")


def is_within(path: Path, parent: Path) -> bool:
    """Whether *path* is *parent* itself or one of its descendants."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def resolve_private_root(root: Path | None = None) -> Path:
    """Resolve the private data root — *root* when supplied, otherwise
    ``BEL_PRIVATE_DATA_ROOT``. Raises ``PrivateRootError`` unless the
    result is a real directory outside the repository."""
    if root is None:
        raw = os.environ.get("BEL_PRIVATE_DATA_ROOT")
        if not raw:
            raise PrivateRootError(REASON_ROOT_NOT_SET, "BEL_PRIVATE_DATA_ROOT is not set")
        raw_root = Path(raw)
    else:
        raw_root = root
    try:
        resolved = raw_root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PrivateRootError(REASON_ROOT_UNRESOLVABLE, f"BEL_PRIVATE_DATA_ROOT does not resolve: {exc}") from exc
    if not resolved.is_dir():
        raise PrivateRootError(REASON_ROOT_NOT_DIR, "BEL_PRIVATE_DATA_ROOT is not a directory")
    if is_within(resolved, repo_root()):
        raise PrivateRootError(REASON_ROOT_INSIDE_REPO, "BEL_PRIVATE_DATA_ROOT must not be inside the repository")
    return resolved


def validate_period(period: str) -> None:
    """Raise ``PrivateRootError(INVALID_PERIOD)`` unless *period* is a
    strict ``YYYY-MM`` naming a real month 1-12."""
    if not PERIOD_RE.match(period):
        raise PrivateRootError(REASON_INVALID_PERIOD, f"period must be YYYY-MM, got {period!r}")
    year, month = int(period[:4]), int(period[5:7])
    if month < 1 or month > 12:
        raise PrivateRootError(REASON_INVALID_PERIOD, f"period must be YYYY-MM, got {period!r}")


def resolve_period_dir(root: Path, period: str) -> Path:
    """Resolve one period directory strictly inside *root*.

    The period is a closed ``YYYY-MM`` identifier — the regex rejects
    ``..``, an absolute path, and an arbitrary string before the
    filesystem is ever touched. The resolved candidate must then be a
    real directory that, AFTER symlink resolution, is still contained
    within the resolved root — closing the escape a same-looking
    symlinked ``YYYY-MM`` entry could otherwise provide."""
    validate_period(period)
    resolved_root = root.resolve(strict=True)
    try:
        candidate = (resolved_root / period).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PrivateRootError(REASON_PERIOD_NOT_FOUND, f"period directory not found: {period}") from exc
    if not candidate.is_dir() or not is_within(candidate, resolved_root):
        raise PrivateRootError(REASON_PERIOD_ESCAPE, f"period {period!r} does not resolve inside BEL_PRIVATE_DATA_ROOT")
    return candidate


def _split_relative(relative: str) -> list[str]:
    """Lexical validation of a root-relative input path. Returns the
    non-empty '/' components, rejecting an absolute path, ``~``, ``..``,
    ``.``, or any empty segment — an arbitrary path must never reach the
    filesystem."""
    if not isinstance(relative, str) or not relative:
        raise PrivateRootError(REASON_INPUT_ESCAPE, "private input path must be a non-empty string")
    if relative.startswith("/") or relative.startswith("~"):
        raise PrivateRootError(REASON_INPUT_ESCAPE, f"private input path must be relative, got {relative!r}")
    raw_parts = relative.split("/")
    if "" in raw_parts:
        # A leading/trailing or doubled '/' is not a canonical relative
        # path — rejected rather than silently normalized.
        raise PrivateRootError(REASON_INPUT_ESCAPE, f"private input path is not canonical, got {relative!r}")
    if any(part in ("..", ".") for part in raw_parts):
        raise PrivateRootError(REASON_INPUT_ESCAPE, f"private input path must not contain '..', got {relative!r}")
    return raw_parts


def read_private_file(root: Path, relative: str) -> bytes:
    """Read one regular file strictly inside the resolved private root.

    ``root`` must already be validated (see ``resolve_private_root``).
    ``relative`` is a root-relative '/'-separated path (e.g.
    ``2026-01/expected/cutover-baseline.json``), never absolute and never
    containing ``..``.

    Hardened containment — every path component is resolved (symlinks
    followed) and the RESULT must remain inside the resolved root, must
    not be inside the repository, and must be a regular file. A symlinked
    directory component, a symlinked final file, or any path resolving
    outside the root is rejected BEFORE any outside content is read.

    The bytes are then read through descriptors opened against the
    RESOLVED parent directory (``O_DIRECTORY`` + ``O_NOFOLLOW``) with an
    ``O_NOFOLLOW`` final component — the reader never re-opens the
    original, potentially-symlinked path after validation (the same
    check/write-gap discipline as ``write_private_report``).

    Raises ``PrivateRootError`` with ``REASON_INPUT_MISSING`` when the
    file does not exist / is not a regular file, and
    ``REASON_INPUT_ESCAPE`` when it (or any component) resolves outside
    the private root or is a symlink the descriptor discipline refuses.
    """
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir() or is_within(resolved_root, repo_root()):
        raise PrivateRootError(REASON_ROOT_INSIDE_REPO, "private root must not be inside the repository")
    parts = _split_relative(relative)

    try:
        candidate = resolved_root.joinpath(*parts).resolve(strict=True)
    except FileNotFoundError as exc:
        raise PrivateRootError(
            REASON_INPUT_MISSING, f"private input {relative!r} not found under BEL_PRIVATE_DATA_ROOT"
        ) from exc
    except OSError as exc:
        # A symlink loop or other resolution failure is never a usable
        # input and never points at valid private content.
        raise PrivateRootError(REASON_INPUT_ESCAPE, f"private input {relative!r} could not be resolved") from exc

    if not is_within(candidate, resolved_root) or is_within(candidate, repo_root()):
        raise PrivateRootError(
            REASON_INPUT_ESCAPE, f"private input {relative!r} resolves outside BEL_PRIVATE_DATA_ROOT"
        )
    if not candidate.is_file():
        raise PrivateRootError(
            REASON_INPUT_MISSING, f"private input {relative!r} is not a regular file"
        )

    # Read through a descriptor chain relative to the fully-resolved
    # parent (which contains no symlink at resolve time), O_NOFOLLOW on
    # both the directory and the final component.
    try:
        parent_fd = os.open(
            candidate.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise PrivateRootError(
            REASON_INPUT_ESCAPE, f"private input {relative!r} could not be opened safely"
        ) from exc
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(candidate.name, flags, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                raise PrivateRootError(
                    REASON_INPUT_ESCAPE, f"private input {relative!r} is a symlink/not a file"
                ) from exc
            raise
        with os.fdopen(fd, "rb") as handle:
            return handle.read()
    finally:
        os.close(parent_fd)


def _jsonable(value: Any) -> Any:
    """Deterministic JSON-safe coercion for private diagnostics — Decimals
    become strings, UUIDs become strings, dataclasses become dicts."""
    from dataclasses import asdict, is_dataclass
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if hasattr(value, "hex") and not isinstance(value, (bytes, bytearray)):
        return str(value)  # UUID
    return value


def write_private_report(root: Path, filename: str, diagnostic: dict[str, Any]) -> bool:
    """Write *diagnostic* ONLY under ``<root>/reports/<filename>``.

    Returns ``True`` on success and ``False`` whenever the hardened
    containment refuses to write (a symlinked reports directory or report
    file escaping the private root, or the private root itself being
    unusable) — reporting must never raise and must never leak into the
    repository. The file is created ``0o600`` and opened with
    ``O_NOFOLLOW`` against a checked ``O_DIRECTORY`` descriptor, so a
    check/write gap (reports-directory symlink swap, final-component
    symlink swap) cannot redirect the diagnostic anywhere else."""
    try:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir() or is_within(resolved_root, repo_root()):
            return False

        reports_dir = resolved_root / _REPORTS_DIR_NAME
        reports_dir.mkdir(parents=True, exist_ok=True)
        resolved_reports_dir = reports_dir.resolve(strict=True)
        if (
            not resolved_reports_dir.is_dir()
            or not is_within(resolved_reports_dir, resolved_root)
            or is_within(resolved_reports_dir, repo_root())
        ):
            return False

        resolved_target = (resolved_reports_dir / filename).resolve(strict=False)
        if not is_within(resolved_target, resolved_reports_dir) or is_within(resolved_target, repo_root()):
            return False

        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        reports_fd = os.open(resolved_reports_dir, directory_flags)

        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(filename, flags, 0o600, dir_fd=reports_fd)
            with os.fdopen(fd, "w", encoding="utf-8") as report:
                json.dump(_jsonable(diagnostic), report, indent=2, ensure_ascii=False, default=str)
        finally:
            os.close(reports_fd)
        return True
    except Exception:  # noqa: BLE001 — reporting must never raise
        return False

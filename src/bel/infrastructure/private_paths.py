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
- Required private INPUT files (``PrivatePeriodReader``) are read with a
  DESCRIPTOR-ANCHORED walk, not pathname reopening: once the private root
  is accepted it is opened ONCE (``O_RDONLY|O_DIRECTORY|O_NOFOLLOW``), the
  period directory is opened relative to the root descriptor, and every
  input component is opened only relative to an already-open descriptor
  via ``dir_fd`` — so an intermediate directory renamed/replaced with a
  symlink or redirected tree mid-read cannot redirect the read. NO symlink
  is allowed in ANY component (period dir, ``expected/`` dir, or a control
  file), each final file must be a regular file (``fstat`` ``S_ISREG``)
  under a bounded size, and the descriptor chain is the authority — never
  a re-open of a resolved path by name.
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
import stat
from pathlib import Path
from typing import Any, Callable

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
REASON_INPUT_UNSAFE_TYPE = "PRIVATE_INPUT_UNSAFE_TYPE"
REASON_INPUT_TOO_LARGE = "PRIVATE_INPUT_TOO_LARGE"

_REPORTS_DIR_NAME = "reports"

# Descriptor flags for the descriptor-anchored input walk. O_NOFOLLOW on a
# component means that component must not be a symlink; O_DIRECTORY means a
# component is only ever accepted as a real directory; O_NONBLOCK on the
# final open means a planted FIFO is detected by fstat instead of blocking.
_OPEN_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_OPEN_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)

# Defensive ceiling for a private control file (backfill plan / Cutover
# Baseline JSON — control documents, never source Excel). Generous tens-of-
# MB bound; a file above it is PRIVATE_INPUT_TOO_LARGE.
_PRIVATE_INPUT_MAX_BYTES = 32 * 1024 * 1024

# Narrow, deliberately-UNexported test seam: when set, the reader invokes it
# at a fixed point (after the root/period descriptors are anchored, before
# the first input component is opened) so a deterministic TOCTOU test can
# rename/replace an intermediate directory there. Never set in production
# code; not part of the module's public API.
_input_read_test_hook: Callable[[], None] | None = None


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


def _split_relative(relative: str) -> list[str]:
    """Lexical validation of a period-relative input path. Returns the
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


def _is_symlink_at(parent_fd: int, name: str) -> bool:
    """Whether *name* relative to an open directory descriptor is a
    symlink. Used ONLY for failure classification: the O_NOFOLLOW open
    already failed (a symlink was never followed), so even a race in this
    look-up cannot redirect a read — it only picks the reason code."""
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=parent_fd).st_mode)
    except OSError:
        return False


def _dir_component_error(exc: OSError, parent_fd: int, name: str) -> PrivateRootError:
    """Classify a failed O_NOFOLLOW directory-component open. A symlink
    component is ESCAPE (the strong Gate rule: no symlink in ANY input path
    component — macOS reports ENOTDIR for a symlink-to-dir under
    O_DIRECTORY|O_NOFOLLOW, so the lstat check disambiguates); a genuinely
    missing / non-directory component is MISSING."""
    if _is_symlink_at(parent_fd, name):
        return PrivateRootError(REASON_INPUT_ESCAPE, f"private input path contains a symlink (rejected): {name!r}")
    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
        return PrivateRootError(REASON_INPUT_MISSING, f"private input component is not a directory: {name!r}")
    return PrivateRootError(REASON_INPUT_ESCAPE, f"private input could not be opened safely: {name!r}")


def _final_file_error(exc: OSError, parent_fd: int, name: str) -> PrivateRootError:
    """Classify a failed O_NOFOLLOW final-file open. A symlink is ESCAPE; a
    real directory or a genuinely missing file is a non-regular-input
    FAIL/MISSING."""
    if _is_symlink_at(parent_fd, name):
        return PrivateRootError(REASON_INPUT_ESCAPE, f"private input is a symlink (rejected): {name!r}")
    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
        return PrivateRootError(REASON_INPUT_MISSING, f"private input not found: {name!r}")
    if exc.errno == errno.EISDIR:
        return PrivateRootError(
            REASON_INPUT_UNSAFE_TYPE, f"private input {name!r} is a directory, not a regular file"
        )
    return PrivateRootError(REASON_INPUT_ESCAPE, f"private input could not be opened safely: {name!r}")


def _read_bounded(fd: int, cap: int) -> bytes:
    """Read *fd* to EOF bounded at *cap* bytes; raises
    REASON_INPUT_TOO_LARGE if the file exceeds the ceiling."""
    chunks: list[bytes] = []
    remaining = cap + 1
    while remaining > 0:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > cap:
        raise PrivateRootError(
            REASON_INPUT_TOO_LARGE, f"private input exceeds the {cap}-byte safety ceiling"
        )
    return data


class PrivatePeriodReader:
    """Descriptor-anchored reader for one strict ``YYYY-MM`` period's
    required private control files (the FIRST-STAGE CUTOVER GATE input
    boundary).

    Security model: once the private root is accepted it is opened ONCE as
    a directory descriptor (``O_RDONLY|O_DIRECTORY|O_NOFOLLOW``), and the
    period directory is opened relative to that root descriptor. Every
    later input component is opened ONLY relative to an already-open
    descriptor via ``dir_fd`` with ``O_NOFOLLOW`` — there is never a
    pathname reopen of a resolved path after validation, so an
    intermediate directory renamed/replaced with a symlink or redirected
    tree mid-read cannot redirect the read: the descriptor chain is the
    authority.

    NO symlink is allowed in ANY component — the period directory, an
    ``expected/`` directory, or a control file — even one resolving back
    inside the private root (the strong Gate rule). Each final file must
    be a regular file (``fstat`` ``S_ISREG``) and is read bounded by a
    defensive size ceiling; a FIFO/socket/device is rejected by type
    (never blocked on, never read).

    Raises ``PrivateRootError`` with a stable reason code:
    ``INVALID_PERIOD`` / ``PERIOD_DIR_NOT_FOUND`` / ``PERIOD_ESCAPE`` from
    ``open``, and ``PRIVATE_INPUT_MISSING`` / ``PRIVATE_INPUT_ESCAPE`` /
    ``PRIVATE_INPUT_UNSAFE_TYPE`` / ``PRIVATE_INPUT_TOO_LARGE`` from
    ``read``.
    """

    def __init__(self, root_fd: int, period_fd: int, period: str) -> None:
        self.root_fd = root_fd
        self.period_fd = period_fd
        self.period = period
        self._closed = False

    @classmethod
    def open(cls, root: Path, period: str) -> "PrivatePeriodReader":
        """Validate the period format and the private root, then anchor a
        descriptor chain on the REAL period directory (no symlink in the
        period component, no pathname reopen afterwards)."""
        validate_period(period)
        resolved_root = resolve_private_root(root)
        try:
            root_fd = os.open(resolved_root, _OPEN_DIR_FLAGS)
        except OSError as exc:
            raise PrivateRootError(
                REASON_ROOT_UNRESOLVABLE, f"could not open private root: {exc}"
            ) from exc
        try:
            try:
                period_fd = os.open(period, _OPEN_DIR_FLAGS, dir_fd=root_fd)
            except OSError as exc:
                if _is_symlink_at(root_fd, period):
                    raise PrivateRootError(
                        REASON_PERIOD_ESCAPE,
                        f"period directory {period!r} is a symlink (rejected — no symlink in any Gate input path)",
                    ) from exc
                if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                    raise PrivateRootError(
                        REASON_PERIOD_NOT_FOUND, f"period directory not found: {period}"
                    ) from exc
                raise PrivateRootError(
                    REASON_PERIOD_ESCAPE, f"period directory {period!r} could not be opened safely"
                ) from exc
            return cls(root_fd=root_fd, period_fd=period_fd, period=period)
        except BaseException:
            os.close(root_fd)
            raise

    def read(self, relative: str) -> bytes:
        """Read one period-relative control file (e.g. ``backfill-plan.json``
        or ``expected/cutover-baseline.json``) strictly through descriptor-
        relative traversal anchored at the opened period directory."""
        if self._closed:
            raise PrivateRootError(REASON_INPUT_ESCAPE, "private period reader is closed")
        parts = _split_relative(relative)

        hook = _input_read_test_hook
        if hook is not None:
            hook()

        current_fd = self.period_fd
        owned = False
        try:
            for part in parts[:-1]:
                try:
                    next_fd = os.open(part, _OPEN_DIR_FLAGS, dir_fd=current_fd)
                except OSError as exc:
                    raise _dir_component_error(exc, current_fd, part) from exc
                if owned:
                    os.close(current_fd)
                current_fd = next_fd
                owned = True

            final_name = parts[-1]
            try:
                fd = os.open(final_name, _OPEN_FILE_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                raise _final_file_error(exc, current_fd, final_name) from exc
            try:
                file_stat = os.fstat(fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise PrivateRootError(
                        REASON_INPUT_UNSAFE_TYPE,
                        f"private input {final_name!r} is not a regular file (symlink/FIFO/socket/device rejected)",
                    )
                if file_stat.st_size > _PRIVATE_INPUT_MAX_BYTES:
                    raise PrivateRootError(
                        REASON_INPUT_TOO_LARGE,
                        f"private input {final_name!r} exceeds the {_PRIVATE_INPUT_MAX_BYTES}-byte ceiling",
                    )
                return _read_bounded(fd, _PRIVATE_INPUT_MAX_BYTES)
            finally:
                os.close(fd)
        finally:
            if owned:
                os.close(current_fd)

    def close(self) -> None:
        """Release both anchored descriptors. Reads after close are
        rejected; calling close more than once is a no-op."""
        if self._closed:
            return
        self._closed = True
        os.close(self.period_fd)
        os.close(self.root_fd)

    def __enter__(self) -> "PrivatePeriodReader":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


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

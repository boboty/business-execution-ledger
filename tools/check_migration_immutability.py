#!/usr/bin/env python3
"""Migration immutability checker (Phase 2D.1-P, Part E).

Enforces M1 (docs/PERSISTENCE-MIGRATION-POLICY.md): once a file under
``migrations/versions/`` or ``migrations/postgresql_versions/`` has been
committed, it must never be modified, deleted, or renamed. A NEW file is
always allowed; that is the only way a migration may ever change.

Forward-enforcing from two distinct markers, never retroactive:

  - LEGACY MIGRATION FREEZE ANCHOR (commit
    b94f572528e25a620bf1a78bd2e26d12547b0212): every
    ``migrations/versions/*.py`` file exactly as it exists at that commit
    is frozen. Pre-2D.1-P history may contain edits to already-committed
    migrations — that predates this policy and is not flagged.
  - POSTGRESQL MIGRATION EPOCH COMMIT: the commit that first adds the
    verified PostgreSQL baseline under ``migrations/postgresql_versions/``
    (recorded in docs/PERSISTENCE-MIGRATION-POLICY.md). Every file under
    that directory is immutable from its own first commit forward.

In practice both markers collapse to the same mechanical rule: "diff
against a known-good prior state; anything already present there must be
byte-identical; anything new is fine" — this script does not need to
know which marker a given file belongs to, only what existed at the
comparison point.

Modes:
    python tools/check_migration_immutability.py --staged
        Local pre-commit: compares the git INDEX (staged content) against
        HEAD. Run from .githooks/pre-commit alongside privacy_scan.py —
        neither replaces the other.

    python tools/check_migration_immutability.py --history [--merge-base-ref REF]
        CI: inspects the commit range merge-base(REF, HEAD)..HEAD (REF
        defaults to origin/main, falling back to main), comparing every
        commit in that range against the set of migration files already
        present AT the merge-base — never full history back to project
        genesis (--merge-base-ref only needs `git fetch --depth 0` to
        resolve correctly). This catches a commit-N-adds /
        commit-N+1-modifies pattern that a final PR diff alone would hide
        as a single added file.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

WATCHED_PREFIXES = ("migrations/versions/", "migrations/postgresql_versions/")


def _run_git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _is_watched(path: str) -> bool:
    return path.startswith(WATCHED_PREFIXES) and path.endswith(".py")


def _name_status(args: list[str]) -> list[tuple[str, str]]:
    """Run a git diff-family command with --name-status --no-renames and
    return [(status, path), ...], restricted to watched migration paths.
    --no-renames is deliberate: a rename then surfaces as a plain
    delete-of-old (caught by the same rule as any other deletion of a
    protected file) plus add-of-new, rather than being silently
    reclassified as a rename that this checker would otherwise need
    special-case logic to reject."""
    out = _run_git(["diff", "--no-renames", "--name-status", *args])
    entries = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        if _is_watched(path):
            entries.append((status[0], path))
    return entries


def check_staged() -> list[str]:
    """Compare the git index against HEAD. Returns a list of violation
    messages (empty if clean)."""
    violations = []
    for status, path in _name_status(["--cached", "HEAD"]):
        if status == "A":
            continue  # new file — always allowed
        if status == "M":
            violations.append(f"MODIFIED: {path} is already committed at HEAD — migrations are immutable (M1)")
        elif status == "D":
            violations.append(f"DELETED: {path} is already committed at HEAD — migrations are immutable (M1)")
        else:
            violations.append(f"{status}: {path} — unexpected change to an already-committed migration file (M1)")
    return violations


def _resolve_merge_base(ref_candidates: list[str]) -> str:
    last_error: Exception | None = None
    for ref in ref_candidates:
        try:
            return _run_git(["merge-base", ref, "HEAD"]).strip()
        except RuntimeError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"could not resolve a merge-base from any of {ref_candidates}: {last_error}")


def check_history(merge_base_ref: str | None) -> list[str]:
    """Inspect merge-base(merge_base_ref, HEAD)..HEAD commit-by-commit.
    Returns a list of violation messages (empty if clean)."""
    ref_candidates = [merge_base_ref] if merge_base_ref else ["origin/main", "main"]
    merge_base = _resolve_merge_base(ref_candidates)

    protected_at_base = {
        path
        for path in _run_git(
            ["ls-tree", "-r", "--name-only", merge_base, "--", "migrations/versions", "migrations/postgresql_versions"]
        ).splitlines()
        if _is_watched(path)
    }

    commits = [c for c in _run_git(["rev-list", "--reverse", f"{merge_base}..HEAD"]).splitlines() if c.strip()]

    violations = []
    added_in_range: set[str] = set()
    for commit in commits:
        for status, path in _name_status([f"{commit}^", commit]):
            protected = path in protected_at_base or path in added_in_range
            if status == "A":
                if protected:
                    violations.append(
                        f"RE-ADDED: {path} in commit {commit[:12]} after being removed earlier in this range "
                        "— migrations are immutable (M1)"
                    )
                else:
                    added_in_range.add(path)
            elif status == "M":
                if protected:
                    violations.append(
                        f"MODIFIED: {path} in commit {commit[:12]} — already committed "
                        f"(at merge-base {merge_base[:12]} or added earlier in this range) — migrations are "
                        "immutable (M1)"
                    )
                else:
                    violations.append(f"MODIFIED: {path} in commit {commit[:12]} with no prior add — inconsistent history")
            elif status == "D":
                if protected:
                    violations.append(
                        f"DELETED: {path} in commit {commit[:12]} — already committed "
                        f"(at merge-base {merge_base[:12]} or added earlier in this range) — migrations are "
                        "immutable (M1)"
                    )
                    added_in_range.discard(path)
            else:
                violations.append(f"{status}: {path} in commit {commit[:12]} — unexpected change (M1)")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="local pre-commit: staged vs HEAD")
    mode.add_argument("--history", action="store_true", help="CI: merge-base(REF, HEAD)..HEAD, commit by commit")
    parser.add_argument(
        "--merge-base-ref",
        default=None,
        help="ref to compute the merge-base against for --history (default: origin/main, falling back to main)",
    )
    args = parser.parse_args()

    if args.staged:
        violations = check_staged()
    else:
        violations = check_history(args.merge_base_ref)

    if violations:
        print("MIGRATION IMMUTABILITY VIOLATION")
        print("")
        for v in violations:
            print(f"  - {v}")
        print("")
        print(
            "Committed migration files under migrations/versions/ or migrations/postgresql_versions/ are "
            "immutable (M1, docs/PERSISTENCE-MIGRATION-POLICY.md). A schema correction is always a NEW "
            "forward migration file, never an edit to an existing one."
        )
        return 1

    print("Migration immutability check: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

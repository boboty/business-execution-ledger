"""Regression matrix for tools/privacy_scan.py's tracked/staged/untracked
weird-path bypass (Gate A remediation, follow-up to the --history fix).

Bug: scan_tracked/scan_staged/scan_untracked sourced paths from the
plain-text form of `git ls-files` / `git diff --cached --name-only`,
parsed with `.splitlines()`. Without `-z`, Git C-quotes any filename
containing a TAB, newline, backslash, or other "unusual" byte
(core.quotePath) — a file literally named "private/x<TAB>y" printed as
the string `"private/x\\ty"` (quote marks and a literal backslash-t
included), which no longer starts with "private/" and defeated every
prefix-based Path Guard rule for a currently tracked/staged/untracked
file, exactly as it did for --history before that fix.

Every scenario here builds its own temporary, independently-synthetic
git repository (invented paths/content only) and monkeypatches
`tools.privacy_scan.REPO_ROOT` to point at it, so these tests never touch
this repository's own git state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import tools.privacy_scan as privacy_scan


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


def _init_repo(tmp_path: Path, name: str = "synthetic-repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "synthetic-tester@example.invalid")
    _run_git(repo, "config", "user.name", "Synthetic Tester")
    # scan_tracked/scan_staged/scan_untracked all need at least one commit
    # to exist for `git diff --cached` etc. to behave normally.
    _run_git(repo, "commit", "-q", "--allow-empty", "-m", "root")
    return repo


def _write_file(repo: Path, rel_path: str, content: str) -> Path:
    full = repo / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _commit_file(repo: Path, rel_path: str, content: str, message: str) -> None:
    """Fully tracked: written, staged, AND committed."""
    _write_file(repo, rel_path, content)
    _run_git(repo, "add", "--", rel_path)
    _run_git(repo, "commit", "-q", "-m", message)


def _stage_file(repo: Path, rel_path: str, content: str) -> None:
    """Staged only: `git add`ed, never committed."""
    _write_file(repo, rel_path, content)
    _run_git(repo, "add", "--", rel_path)


TAB_SUFFIX = "\tfile.txt"
NEWLINE_SUFFIX = "\nfile.txt"


# ============================================================================
# T1/T2 — TRACKED
# ============================================================================


def test_t1_tracked_forbidden_tab_path_is_caught(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, "t1-tracked-tab")
    forbidden_path = "private/synthetic" + TAB_SUFFIX
    _commit_file(repo, forbidden_path, "synthetic tracked tab content\n", "add forbidden tab path")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    findings = privacy_scan.scan_tracked(None)
    hits = [f for f in findings if f.file == forbidden_path]
    assert hits, "a tracked forbidden TAB-named path must be caught"
    assert hits[0].rule == "path-guard:banned-path"


def test_t2_tracked_forbidden_newline_path_is_caught(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, "t2-tracked-newline")
    forbidden_path = "private/synthetic" + NEWLINE_SUFFIX
    _commit_file(repo, forbidden_path, "synthetic tracked newline content\n", "add forbidden newline path")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    findings = privacy_scan.scan_tracked(None)
    hits = [f for f in findings if f.file == forbidden_path]
    assert hits, "a tracked forbidden newline-named path must be caught"
    assert hits[0].rule == "path-guard:banned-path"


# ============================================================================
# T3/T4 — STAGED
# ============================================================================


def test_t3_staged_forbidden_tab_path_is_caught(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, "t3-staged-tab")
    forbidden_path = "private/synthetic" + TAB_SUFFIX
    _stage_file(repo, forbidden_path, "synthetic staged tab content\n")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    findings = privacy_scan.scan_staged(None)
    hits = [f for f in findings if f.file == forbidden_path]
    assert hits, "a staged (not yet committed) forbidden TAB-named path must be caught"
    assert hits[0].rule == "path-guard:banned-path"


def test_t4_staged_forbidden_newline_path_is_caught(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, "t4-staged-newline")
    forbidden_path = "private/synthetic" + NEWLINE_SUFFIX
    _stage_file(repo, forbidden_path, "synthetic staged newline content\n")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    findings = privacy_scan.scan_staged(None)
    hits = [f for f in findings if f.file == forbidden_path]
    assert hits, "a staged (not yet committed) forbidden newline-named path must be caught"
    assert hits[0].rule == "path-guard:banned-path"


# ============================================================================
# T5/T6 — UNTRACKED
# ============================================================================


def test_t5_untracked_forbidden_tab_path_is_caught(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, "t5-untracked-tab")
    forbidden_path = "private/synthetic" + TAB_SUFFIX
    _write_file(repo, forbidden_path, "synthetic untracked tab content\n")  # no git add at all

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    findings = privacy_scan.scan_untracked(None)
    hits = [f for f in findings if f.file == forbidden_path]
    assert hits, "an untracked forbidden TAB-named path must be caught"
    assert hits[0].rule == "path-guard:banned-path"


def test_t6_untracked_forbidden_newline_path_is_caught(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, "t6-untracked-newline")
    forbidden_path = "private/synthetic" + NEWLINE_SUFFIX
    _write_file(repo, forbidden_path, "synthetic untracked newline content\n")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    findings = privacy_scan.scan_untracked(None)
    hits = [f for f in findings if f.file == forbidden_path]
    assert hits, "an untracked forbidden newline-named path must be caught"
    assert hits[0].rule == "path-guard:banned-path"


# ============================================================================
# T7 — safe weird paths must never be a false positive, in any mode
# ============================================================================


def test_t7_safe_weird_paths_are_not_false_positives_in_any_mode(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, "t7-safe-weird")
    tracked_path = "docs/safe-tracked" + TAB_SUFFIX
    staged_path = "docs/safe-staged" + NEWLINE_SUFFIX
    untracked_path = "docs/safe-untracked" + TAB_SUFFIX

    _commit_file(repo, tracked_path, "synthetic clean tracked prose\n", "add safe tracked weird path")
    _stage_file(repo, staged_path, "synthetic clean staged prose\n")
    _write_file(repo, untracked_path, "synthetic clean untracked prose\n")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    tracked_findings = privacy_scan.scan_tracked(None)
    staged_findings = privacy_scan.scan_staged(None)
    untracked_findings = privacy_scan.scan_untracked(None)

    assert tracked_findings == [], f"safe TAB-named tracked path must not false-positive: {tracked_findings}"
    assert staged_findings == [], f"safe newline-named staged path must not false-positive: {staged_findings}"
    assert untracked_findings == [], f"safe TAB-named untracked path must not false-positive: {untracked_findings}"

    # And the raw paths really were recovered correctly (proves the guard
    # saw the exact right path and legitimately found nothing wrong with
    # it, rather than silently skipping/mangling it).
    tracked_names = set(privacy_scan._run_git_nul_paths("ls-files", "-z"))
    assert tracked_path in tracked_names
    staged_names = set(privacy_scan._run_git_nul_paths("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"))
    assert staged_path in staged_names
    untracked_names = set(privacy_scan._run_git_nul_paths("ls-files", "--others", "--exclude-standard", "-z"))
    assert untracked_path in untracked_names


# ============================================================================
# T8 — history regressions must still pass (no regression from this round)
# ============================================================================


def test_t8_history_regressions_still_pass(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, "t8-history")
    same_content = "synthetic shared history content\n"
    safe_path = "docs/safe-history-companion.txt"
    forbidden_path = "private/synthetic" + TAB_SUFFIX

    _commit_file(repo, safe_path, same_content, "add safe copy")
    _commit_file(repo, forbidden_path, same_content, "add forbidden weird-path copy, same bytes")
    _run_git(repo, "rm", "-q", "--", forbidden_path)
    _run_git(repo, "commit", "-q", "-m", "remove forbidden weird path from HEAD")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    # Forbidden TAB path in history.
    history_findings = privacy_scan.scan_history(None)
    hits = [f for f in history_findings if f.file == forbidden_path]
    assert hits, "history must still catch a TAB-named forbidden path"
    assert hits[0].rule == "path-guard:banned-path"

    # Same-blob multi-path: both paths recovered independently.
    all_paths, sha_to_a_path = privacy_scan._historical_tree_entries()
    assert safe_path in all_paths and forbidden_path in all_paths
    assert len({sha for sha in sha_to_a_path}) == 1

    # Removed from HEAD: --tracked must be clean for it now.
    tracked_findings = privacy_scan.scan_tracked(None)
    assert not [f for f in tracked_findings if f.file == forbidden_path]


# ============================================================================
# T9 — staged scanning must reflect INDEX content, not worktree content
# ============================================================================


def test_t9_staged_scan_reflects_index_not_worktree(tmp_path, monkeypatch):
    """A file is staged with forbidden-shaped content, then the WORKTREE
    copy is edited afterward (without re-staging) to clean content.
    --staged must still flag what is actually in the index (what would be
    committed), not the now-different worktree content."""
    repo = _init_repo(tmp_path, "t9-index-vs-worktree")
    path = "docs/staged-content-check.txt"
    # Built at runtime, not a source literal (keeps this test file free of
    # a 10+ digit run in its own text).
    digit_run = "".join(str(d) for d in [9, 8, 7, 6, 5, 4, 3, 2, 1, 9, 8, 7])
    staged_content = f"synthetic staged reference {digit_run}\n"
    _stage_file(repo, path, staged_content)

    # Now diverge the worktree from the index without re-staging.
    (repo / path).write_text("synthetic clean worktree content, nothing forbidden\n", encoding="utf-8")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    findings = privacy_scan.scan_staged(None)
    hits = [f for f in findings if f.file == path and f.rule == "generic-guard:long-numeric-identifier"]
    assert hits, "scan_staged must read the INDEX blob, not the diverged worktree content"

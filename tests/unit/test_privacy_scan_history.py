"""Regression tests for tools/privacy_scan.py's Historical Path Guard
(Gate A remediation).

Bug: ``scan_history`` used to source historical paths from
``git rev-list --objects --all``, which deduplicates OBJECTS globally —
if the same content (blob SHA) was ever committed at two different
paths, only one (arbitrary, first-seen) path was ever checked by Path
Guard. A blob that legitimately lived at a safe path and was LATER (or
EARLIER) also committed at a banned path could pass ``--history`` clean.

Every scenario here builds its own temporary, independently-synthetic
git repository (invented paths/content only — nothing resembling a real
company name, contract number, directory, or amount) and monkeypatches
``tools.privacy_scan.REPO_ROOT`` to point at it, so these tests never
touch this repository's own git history.
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
    return repo


def _commit_file(repo: Path, rel_path: str, content: str, message: str) -> None:
    full = repo / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    _run_git(repo, "add", rel_path)
    _run_git(repo, "commit", "-q", "-m", message)


def _remove_and_commit(repo: Path, rel_path: str, message: str) -> None:
    _run_git(repo, "rm", "-q", rel_path)
    _run_git(repo, "commit", "-q", "-m", message)


def test_same_blob_at_safe_and_forbidden_historical_paths_is_caught(tmp_path, monkeypatch):
    """TEST 1: identical content committed first at a safe synthetic path,
    then (same bytes -> same blob SHA) at a path Path Guard bans. The old
    sha-keyed-by-single-path implementation would drop one of these two
    paths depending on traversal order; the fix must report the banned
    one regardless."""
    repo = _init_repo(tmp_path)
    content = "synthetic-content-test-one\n"
    _commit_file(repo, "docs/safe-example.txt", content, "add safe copy")
    # "private/" is a Path Guard banned prefix (is_banned_path) — entirely
    # synthetic path and content, not a real private directory.
    _commit_file(repo, "private/synthetic-example.txt", content, "add banned-path copy, same bytes")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    all_paths, sha_to_a_path = privacy_scan._historical_tree_entries()
    assert "docs/safe-example.txt" in all_paths
    assert "private/synthetic-example.txt" in all_paths
    # Both paths really do share one blob SHA — otherwise this test isn't
    # exercising the same-blob scenario at all.
    blob_shas_seen = {sha for sha, path in sha_to_a_path.items()}
    assert len(blob_shas_seen) == 1, "both files must hash to the same blob for this test to be meaningful"

    findings = privacy_scan.scan_history(None)
    banned_findings = [f for f in findings if f.file == "private/synthetic-example.txt"]
    assert banned_findings, "the banned historical path must be reported even though its blob also exists at a safe path"
    assert banned_findings[0].rule == "path-guard:banned-path"
    # The safe path must never be reported.
    assert not [f for f in findings if f.file == "docs/safe-example.txt" and f.rule.startswith("path-guard")]


def test_same_blob_at_two_safe_paths_passes(tmp_path, monkeypatch):
    """TEST 2: identical content at two paths that are BOTH legitimate —
    --history must report nothing."""
    repo = _init_repo(tmp_path)
    content = "synthetic prose with no forbidden shape at all\n"
    _commit_file(repo, "docs/alpha.md", content, "add alpha")
    _commit_file(repo, "docs/beta.md", content, "add beta, same bytes")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    findings = privacy_scan.scan_history(None)
    assert findings == []


def test_content_guard_dedupes_by_blob_without_duplicate_or_missing_findings(tmp_path, monkeypatch):
    """TEST 3: the SAME flagged content at two safe paths must produce
    exactly one Content Guard finding (deduped by blob SHA) — not zero
    (content scanning must not have been lost in the path/content split)
    and not two (no duplicate-finding regression from checking content
    once per unique blob rather than once per path)."""
    repo = _init_repo(tmp_path)
    # A synthetic 10+ digit run trips generic-guard:long-numeric-identifier
    # (bank-account/invoice/reference-number shaped) — invented digits,
    # built at runtime (not a source literal) so this test file's own text
    # never contains a 10+ digit run for privacy_scan --untracked to flag.
    digit_run = "".join(str(d) for d in [1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 1, 2, 3])
    content = f"synthetic reference number {digit_run} appears in this file\n"
    _commit_file(repo, "docs/report-a.md", content, "add report a")
    _commit_file(repo, "docs/report-b.md", content, "add report b, same bytes")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    findings = privacy_scan.scan_history(None)
    long_digit_findings = [f for f in findings if f.rule == "generic-guard:long-numeric-identifier"]
    assert len(long_digit_findings) == 1, f"expected exactly one deduped content finding, got {long_digit_findings}"


def test_historical_forbidden_path_removed_from_head_is_still_caught(tmp_path, monkeypatch):
    """TEST 4: a banned synthetic path existed in an earlier commit and was
    removed before HEAD. --tracked (current tree only) must be clean;
    --history must still surface the historical occurrence."""
    repo = _init_repo(tmp_path)
    _commit_file(repo, "private/synthetic-legacy.txt", "synthetic legacy content\n", "add banned path")
    _remove_and_commit(repo, "private/synthetic-legacy.txt", "remove banned path")
    _commit_file(repo, "docs/current.md", "synthetic current content\n", "unrelated current commit")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    tracked_findings = privacy_scan.scan_tracked(None)
    assert not [f for f in tracked_findings if f.file == "private/synthetic-legacy.txt"]

    history_findings = privacy_scan.scan_history(None)
    historical_hits = [f for f in history_findings if f.file == "private/synthetic-legacy.txt"]
    assert historical_hits, "a banned path that only ever existed in history must still be caught by --history"
    assert historical_hits[0].rule == "path-guard:banned-path"


def test_history_cli_exit_code_reflects_findings(tmp_path, monkeypatch, capsys):
    """End-to-end: the CLI entry point (main()) must exit non-zero when
    --history finds a banned historical path, and print PRIVACY BLOCKER —
    not just the lower-level scan_history() function."""
    repo = _init_repo(tmp_path)
    content = "synthetic-content-cli-check\n"
    _commit_file(repo, "docs/safe.txt", content, "safe copy")
    _commit_file(repo, "private/synthetic-cli.txt", content, "banned copy, same bytes")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)
    monkeypatch.delenv("BEL_PRIVACY_DENYLIST", raising=False)

    exit_code = privacy_scan.main(["--history"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "PRIVACY BLOCKER" in captured.out
    assert "private/synthetic-cli.txt" in captured.out


# ============================================================================
# NUL-safety regression (second Gate A finding): plain `git ls-tree -r`
# (newline-delimited, no -z) quotes any filename containing a TAB, newline,
# backslash, or other "unusual" character (core.quotePath) — e.g. a path
# literally named "private/x<TAB>y" prints as the 15-character string
# `"private/x\ty"` (quote marks and a literal backslash-t included), which
# no longer starts with "private/" and defeats every prefix-based Path
# Guard rule outright. Empirically confirmed against this exact scenario
# before writing these tests: plain `ls-tree -r` on a commit containing
# "private/synthetic\tfile.txt" (literal TAB in the name) prints
# '100644 blob <sha>\t"private/synthetic\\tfile.txt"\n' — a check_path()
# call on that quoted text never matches is_banned_path's "private/"
# prefix. `ls-tree -rz` prints the same record NUL-terminated with the
# raw, unquoted filename instead.
# ============================================================================


def test_tab_in_forbidden_path_is_caught_by_history_scan(tmp_path, monkeypatch):
    """TEST 1: a banned synthetic path whose filename contains a literal
    TAB byte. Plain (non -z) `ls-tree -r` would quote this path into a
    string starting with a double-quote character, never matching
    is_banned_path's "private/" prefix check — --history must still
    report it."""
    repo = _init_repo(tmp_path, "tab-attack")
    forbidden_path = "private/synthetic\tfile.txt"
    _commit_file(repo, forbidden_path, "synthetic tab-attack content\n", "add tab-named file")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    all_paths, _ = privacy_scan._historical_tree_entries()
    assert forbidden_path in all_paths, "the raw (unquoted) TAB-containing path must be recovered exactly"

    findings = privacy_scan.scan_history(None)
    hits = [f for f in findings if f.file == forbidden_path]
    assert hits, "a banned path containing a TAB must still be caught"
    assert hits[0].rule == "path-guard:banned-path"


def test_newline_in_forbidden_path_is_caught_by_history_scan(tmp_path, monkeypatch):
    """TEST 2: same attack, but with a literal newline byte in the
    filename instead of a TAB."""
    repo = _init_repo(tmp_path, "newline-attack")
    forbidden_path = "private/synthetic\nfile.txt"
    _commit_file(repo, forbidden_path, "synthetic newline-attack content\n", "add newline-named file")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    all_paths, _ = privacy_scan._historical_tree_entries()
    assert forbidden_path in all_paths

    findings = privacy_scan.scan_history(None)
    hits = [f for f in findings if f.file == forbidden_path]
    assert hits, "a banned path containing a newline must still be caught"
    assert hits[0].rule == "path-guard:banned-path"


def test_same_blob_safe_path_and_weird_forbidden_path(tmp_path, monkeypatch):
    """TEST 3: identical content at a safe path AND at a banned path whose
    filename contains a TAB. Content Guard may dedupe by blob SHA; Path
    Guard must still catch the forbidden raw path regardless."""
    repo = _init_repo(tmp_path, "same-blob-weird-path")
    # Built at runtime, not a source literal — keeps this test file's own
    # text free of a 10+ digit run (see test_content_guard_dedupes_*).
    digit_run = "".join(str(d) for d in [5, 5, 5, 4, 4, 4, 3, 3, 3, 2, 2, 2, 1])
    content = f"synthetic shared content, reference {digit_run}\n"
    safe_path = "docs/safe-weird-companion.txt"
    forbidden_path = "private/synthetic\tweird.txt"
    _commit_file(repo, safe_path, content, "add safe copy")
    _commit_file(repo, forbidden_path, content, "add forbidden weird-path copy, same bytes")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    all_paths, sha_to_a_path = privacy_scan._historical_tree_entries()
    assert safe_path in all_paths
    assert forbidden_path in all_paths
    assert len({sha for sha in sha_to_a_path}) == 1, "both files must share one blob for this test to be meaningful"

    findings = privacy_scan.scan_history(None)
    path_hits = [f for f in findings if f.file == forbidden_path and f.rule == "path-guard:banned-path"]
    assert path_hits, "the forbidden weird-character path must be reported even though its blob is also safe elsewhere"

    content_hits = [f for f in findings if f.rule == "generic-guard:long-numeric-identifier"]
    assert len(content_hits) == 1, "the shared blob's content must be scanned exactly once, not once per path"


def test_weird_forbidden_path_removed_from_head_is_still_caught_by_history(tmp_path, monkeypatch):
    """TEST 4: a banned TAB-named path existed in an earlier commit and
    was removed before HEAD. --tracked must be clean; --history must
    still surface it."""
    repo = _init_repo(tmp_path, "weird-path-removed")
    forbidden_path = "private/synthetic\tlegacy.txt"
    _commit_file(repo, forbidden_path, "synthetic legacy weird-path content\n", "add banned weird path")
    _remove_and_commit(repo, forbidden_path, "remove banned weird path")
    _commit_file(repo, "docs/current.md", "synthetic current content\n", "unrelated current commit")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    tracked_findings = privacy_scan.scan_tracked(None)
    assert not [f for f in tracked_findings if f.file == forbidden_path]

    history_findings = privacy_scan.scan_history(None)
    hits = [f for f in history_findings if f.file == forbidden_path]
    assert hits, "a banned TAB-named path that only ever existed in history must still be caught by --history"
    assert hits[0].rule == "path-guard:banned-path"


def test_safe_path_with_tab_or_newline_is_not_a_false_positive(tmp_path, monkeypatch):
    """TEST 5: a TAB or newline in a filename must never trigger a finding
    on its own — only an actual banned prefix/suffix or content pattern
    does. Guards must key on path CONTENT, not on the presence of an
    unusual byte."""
    repo = _init_repo(tmp_path, "safe-weird-path")
    tab_path = "docs/safe\ttab-named.md"
    newline_path = "docs/safe\nnewline-named.md"
    _commit_file(repo, tab_path, "synthetic clean prose, nothing forbidden here\n", "add tab-named safe file")
    _commit_file(repo, newline_path, "synthetic clean prose, nothing forbidden here either\n", "add newline-named safe file")

    monkeypatch.setattr(privacy_scan, "REPO_ROOT", repo)

    all_paths, _ = privacy_scan._historical_tree_entries()
    assert tab_path in all_paths
    assert newline_path in all_paths

    findings = privacy_scan.scan_history(None)
    assert findings == [], f"a TAB/newline in an otherwise-safe path must never itself be a finding, got {findings}"

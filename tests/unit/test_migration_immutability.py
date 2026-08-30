"""Required adversarial tests for tools/check_migration_immutability.py
(Phase 2D.1-P, Part E): the mechanical guard behind M1 (docs/
PERSISTENCE-MIGRATION-POLICY.md) — once a migration file is committed
under migrations/versions/ or migrations/postgresql_versions/, it must
never be modified, deleted, or renamed. Only a NEW file is ever allowed.

Every scenario here builds its own temporary, independently-synthetic git
repository (invented paths/content only) and monkeypatches
``tools.check_migration_immutability.REPO_ROOT`` to point at it, so these
tests never touch this repository's own git state — same convention as
tests/unit/test_privacy_scan_history.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import tools.check_migration_immutability as immutability_checker


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "synthetic-repo"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "synthetic-tester@example.invalid")
    _run_git(repo, "config", "user.name", "Synthetic Tester")
    (repo / "migrations" / "versions").mkdir(parents=True)
    (repo / "migrations" / "postgresql_versions").mkdir(parents=True)
    (repo / "README.md").write_text("synthetic\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-q", "-m", "initial")
    return repo


def _write(repo: Path, rel_path: str, content: str) -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# --staged (case 1-4, 6)
# ---------------------------------------------------------------------------


def test_staged_new_migration_file_passes(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(immutability_checker, "REPO_ROOT", repo)
    _write(repo, "migrations/postgresql_versions/aaa_new.py", "revision = 'aaa'\n")
    _run_git(repo, "add", "migrations/postgresql_versions/aaa_new.py")
    assert immutability_checker.check_staged() == []


def test_staged_modifying_a_committed_migration_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _write(repo, "migrations/versions/aaa_base.py", "revision = 'aaa'\n")
    _run_git(repo, "add", "migrations/versions/aaa_base.py")
    _run_git(repo, "commit", "-q", "-m", "add base migration")
    monkeypatch.setattr(immutability_checker, "REPO_ROOT", repo)

    _write(repo, "migrations/versions/aaa_base.py", "revision = 'aaa'  # tampered\n")
    _run_git(repo, "add", "migrations/versions/aaa_base.py")
    violations = immutability_checker.check_staged()
    assert len(violations) == 1
    assert "MODIFIED" in violations[0] and "aaa_base.py" in violations[0]


def test_staged_deleting_a_committed_migration_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _write(repo, "migrations/versions/bbb_base.py", "revision = 'bbb'\n")
    _run_git(repo, "add", "migrations/versions/bbb_base.py")
    _run_git(repo, "commit", "-q", "-m", "add base migration")
    monkeypatch.setattr(immutability_checker, "REPO_ROOT", repo)

    (repo / "migrations" / "versions" / "bbb_base.py").unlink()
    _run_git(repo, "add", "-A", "migrations/versions/bbb_base.py")
    violations = immutability_checker.check_staged()
    assert len(violations) == 1
    assert "DELETED" in violations[0] and "bbb_base.py" in violations[0]


def test_staged_renaming_a_committed_migration_fails(tmp_path, monkeypatch):
    """--no-renames means a rename surfaces as delete-of-old +
    add-of-new; the delete alone is what triggers the failure — a
    dedicated case since renames could otherwise slip past a checker
    that only special-cases M/D and forgets R."""
    repo = _init_repo(tmp_path)
    _write(repo, "migrations/versions/ccc_base.py", "revision = 'ccc'\n" * 20)
    _run_git(repo, "add", "migrations/versions/ccc_base.py")
    _run_git(repo, "commit", "-q", "-m", "add base migration")
    monkeypatch.setattr(immutability_checker, "REPO_ROOT", repo)

    _run_git(repo, "mv", "migrations/versions/ccc_base.py", "migrations/versions/ccc_renamed.py")
    violations = immutability_checker.check_staged()
    assert any("DELETED" in v and "ccc_base.py" in v for v in violations)


def test_staged_non_migration_changes_pass(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(immutability_checker, "REPO_ROOT", repo)
    _write(repo, "src/bel/some_module.py", "x = 1\n")
    _write(repo, "README.md", "synthetic, updated\n")
    _run_git(repo, "add", "src/bel/some_module.py", "README.md")
    assert immutability_checker.check_staged() == []


def test_staged_new_file_then_edited_before_first_commit_stays_allowed(tmp_path, monkeypatch):
    """M1's own carve-out: editing before the FIRST commit is fine —
    only a file already present at HEAD is protected."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(immutability_checker, "REPO_ROOT", repo)
    _write(repo, "migrations/postgresql_versions/ddd_new.py", "revision = 'ddd'\n")
    _run_git(repo, "add", "migrations/postgresql_versions/ddd_new.py")
    _write(repo, "migrations/postgresql_versions/ddd_new.py", "revision = 'ddd'  # edited before commit\n")
    _run_git(repo, "add", "migrations/postgresql_versions/ddd_new.py")
    assert immutability_checker.check_staged() == []


# ---------------------------------------------------------------------------
# --history (case 5, plus the base cases replayed across commits)
# ---------------------------------------------------------------------------


def test_history_add_then_modify_in_same_branch_fails(tmp_path, monkeypatch):
    """The case a final PR diff alone would hide as a single added file:
    commit A adds a migration, commit B (same branch, same range) edits
    it — both flagged, even though origin/main..HEAD's cumulative diff
    looks like one clean add."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(immutability_checker, "REPO_ROOT", repo)

    _run_git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "migrations/postgresql_versions/eee_new.py", "revision = 'eee'\n")
    _run_git(repo, "add", "migrations/postgresql_versions/eee_new.py")
    _run_git(repo, "commit", "-q", "-m", "commit A: add migration")

    _write(repo, "migrations/postgresql_versions/eee_new.py", "revision = 'eee'  # tampered in commit B\n")
    _run_git(repo, "add", "migrations/postgresql_versions/eee_new.py")
    _run_git(repo, "commit", "-q", "-m", "commit B: modify same migration")

    violations = immutability_checker.check_history("main")
    assert len(violations) == 1
    assert "MODIFIED" in violations[0] and "eee_new.py" in violations[0]


def test_history_single_clean_add_passes(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(immutability_checker, "REPO_ROOT", repo)

    _run_git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "migrations/postgresql_versions/fff_new.py", "revision = 'fff'\n")
    _run_git(repo, "add", "migrations/postgresql_versions/fff_new.py")
    _run_git(repo, "commit", "-q", "-m", "add one migration, never touched again")

    assert immutability_checker.check_history("main") == []


def test_history_modifying_a_pre_range_migration_fails(tmp_path, monkeypatch):
    """A migration already committed at the merge-base (i.e. on the
    target branch before this branch diverged) must stay protected
    across the whole range, not just within it."""
    repo = _init_repo(tmp_path)
    _write(repo, "migrations/versions/ggg_base.py", "revision = 'ggg'\n")
    _run_git(repo, "add", "migrations/versions/ggg_base.py")
    _run_git(repo, "commit", "-q", "-m", "add base migration on main")
    monkeypatch.setattr(immutability_checker, "REPO_ROOT", repo)

    _run_git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "migrations/versions/ggg_base.py", "revision = 'ggg'  # tampered on feature branch\n")
    _run_git(repo, "add", "migrations/versions/ggg_base.py")
    _run_git(repo, "commit", "-q", "-m", "modify pre-existing migration")

    violations = immutability_checker.check_history("main")
    assert len(violations) == 1
    assert "MODIFIED" in violations[0] and "ggg_base.py" in violations[0]


def test_history_ordinary_source_changes_pass(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(immutability_checker, "REPO_ROOT", repo)

    _run_git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "src/bel/some_module.py", "x = 1\n")
    _run_git(repo, "add", "src/bel/some_module.py")
    _run_git(repo, "commit", "-q", "-m", "unrelated source change")
    _write(repo, "src/bel/some_module.py", "x = 2\n")
    _run_git(repo, "add", "src/bel/some_module.py")
    _run_git(repo, "commit", "-q", "-m", "another unrelated source change")

    assert immutability_checker.check_history("main") == []

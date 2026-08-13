#!/usr/bin/env python3
"""Privacy scanner — blocks real business data from entering Git.

See docs/PRIVATE-DATA-POLICY.md for the policy this enforces.

Modes (pick one):
    python tools/privacy_scan.py --staged           # staged changes (pre-commit)
    python tools/privacy_scan.py --tracked           # every tracked file (CI, on-demand)
    python tools/privacy_scan.py --untracked         # untracked, non-ignored files (on-demand)
    python tools/privacy_scan.py --history           # every reachable commit + blob (on-demand)
    python tools/privacy_scan.py --commit-msg FILE   # a pending commit message (commit-msg hook)

Four guards, all independent of each other:
  1. Path Guard    — banned paths (data/private/**, tests/private/**, private/**,
                      *.db, *.db-journal), and data files (.xlsx/.xls/.pdf/.csv)
                      outside fixtures/synthetic/**.
  2. Local Denylist — optional; set BEL_PRIVACY_DENYLIST to a YAML file (kept
                      OUTSIDE this repo) listing known-real names/identifiers.
                      Skipped silently if unset — never required for CI.
  3. Generic Guard  — works with zero local config, so CI can run it standalone:
                      long numeric identifiers, CN mobile numbers, CN ID-card-shaped
                      strings, and suspicious "expected/baseline" filenames outside
                      the public golden directory.
  4. Commit Message Guard — same Denylist + Generic Guard, applied to commit
                      messages (--history, --commit-msg).

Any hit prints "PRIVACY BLOCKER" with file/line/rule (denylist hits show the
category, never the matched value) and the process exits non-zero.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DATA_FILE_EXTENSIONS = (".xlsx", ".xls", ".pdf", ".csv")
ALLOWED_DATA_FILE_PREFIX = "fixtures/synthetic/"
ALLOWED_BASELINE_DIR_PREFIX = "tests/golden/synthetic-v1/"

MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
LONG_DIGIT_RUN_RE = re.compile(r"(?<!\d)\d{10,}(?!\d)")
SUSPICIOUS_EXPECTED_FILENAME_RE = re.compile(
    r"(^|/)([\w.-]*expected[\w.-]*|[\w.-]*-baseline|[\w.-]*supplier-breakdown[\w.-]*)\.json$",
    re.IGNORECASE,
)

TEXT_DECODE_ERRORS_TO_SKIP = (UnicodeDecodeError,)


@dataclass
class Finding:
    file: str
    line: int | None
    rule: str
    detail: str


def is_banned_path(path: str) -> bool:
    if path.endswith(".db") or path.endswith(".db-journal"):
        return True
    if path.startswith("data/private/") or path == "data/private":
        return True
    if path.startswith("tests/private/") or path == "tests/private":
        return True
    if path.startswith("private/") or path == "private":
        return True
    return False


def is_unauthorized_data_file(path: str) -> bool:
    if not path.lower().endswith(DATA_FILE_EXTENSIONS):
        return False
    return not path.startswith(ALLOWED_DATA_FILE_PREFIX)


def is_suspicious_expected_filename(path: str) -> bool:
    if not SUSPICIOUS_EXPECTED_FILENAME_RE.search(path):
        return False
    return not path.startswith(ALLOWED_BASELINE_DIR_PREFIX)


def check_path(path: str) -> list[Finding]:
    findings: list[Finding] = []
    if is_banned_path(path):
        findings.append(Finding(path, None, "path-guard:banned-path", "path matches a banned private-data location"))
    if is_unauthorized_data_file(path):
        findings.append(
            Finding(path, None, "path-guard:unauthorized-data-file", "data file outside fixtures/synthetic/**")
        )
    if is_suspicious_expected_filename(path):
        findings.append(
            Finding(
                path,
                None,
                "path-guard:suspicious-expected-filename",
                "expected/baseline-shaped filename outside the public golden directory",
            )
        )
    return findings


def load_denylist() -> dict[str, list[str]] | None:
    import os

    raw_path = os.environ.get("BEL_PRIVACY_DENYLIST")
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.exists():
        print(f"warning: BEL_PRIVACY_DENYLIST={raw_path} does not exist — denylist checks skipped", file=sys.stderr)
        return None
    try:
        import yaml
    except ImportError:
        print("warning: pyyaml not installed — denylist checks skipped", file=sys.stderr)
        return None

    data = yaml.safe_load(path.read_text()) or {}
    denylist: dict[str, list[str]] = {}
    for category, values in data.items():
        if not values:
            continue
        denylist[category] = [str(v) for v in values]
    return denylist


def check_content_generic(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if MOBILE_RE.search(line):
            findings.append(Finding(path, lineno, "generic-guard:mobile-number", "CN-mobile-shaped digit string"))
        if ID_CARD_RE.search(line):
            findings.append(Finding(path, lineno, "generic-guard:id-card", "CN-ID-card-shaped digit string"))
        elif LONG_DIGIT_RUN_RE.search(line):
            findings.append(
                Finding(path, lineno, "generic-guard:long-numeric-identifier", "10+ digit run (bank account / invoice / reference number shaped)")
            )
    return findings


def check_content_denylist(path: str, text: str, denylist: dict[str, list[str]]) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.splitlines()
    # Word-boundary match, not naive substring: a short denylist entry
    # like a bare count or a 3-character name must not fire on every
    # unrelated line that happens to contain the same digits/characters
    # as part of a longer token (line numbers, dates, unrelated words).
    compiled = [
        (category, value, re.compile(r"(?<![\w])" + re.escape(value) + r"(?![\w])"))
        for category, values in denylist.items()
        for value in values
        if value
    ]
    for category, value, pattern in compiled:
        for lineno, line in enumerate(lines, start=1):
            if pattern.search(line):
                findings.append(Finding(path, lineno, f"denylist:{category}", f"matched a {category} entry"))
    return findings


def check_file_content(path: str, content: bytes, denylist: dict[str, list[str]] | None) -> list[Finding]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return []  # binary content — Path Guard already covers unauthorized binaries
    findings = check_content_generic(path, text)
    if denylist:
        findings.extend(check_content_denylist(path, text, denylist))
    return findings


def check_commit_message(label: str, message: str, denylist: dict[str, list[str]] | None) -> list[Finding]:
    findings = check_content_generic(f"commit-message:{label}", message)
    if denylist:
        findings.extend(check_content_denylist(f"commit-message:{label}", message, denylist))
    return findings


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace')}")
    return result.stdout.decode("utf-8", errors="surrogateescape")


def _git_bytes(*args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


def _decode_git_path(raw: bytes) -> str:
    """Lossless bytes -> str for a raw Git path.

    ``surrogateescape`` round-trips exactly for ANY byte sequence
    (``s.encode("utf-8", "surrogateescape") == raw`` always holds), so a
    path containing non-UTF-8 bytes is never silently dropped or
    corrupted into a different string — it is only mapped through
    unpaired surrogate code points that still compare correctly against
    the plain-ASCII prefixes/suffixes every Path Guard rule matches on.
    Never use ``errors="replace"`` here: replacing invalid bytes with a
    placeholder can change what a prefix/suffix check sees and let a
    forbidden path slip past unnoticed.
    """
    return raw.decode("utf-8", errors="surrogateescape")


def _run_git_nul_paths(*args: str) -> list[str]:
    """Run a NUL-terminated (``-z``) Git path-listing command and return
    the exact, lossless list of raw path strings it names — the single
    shared entry point every path-enumerating scan mode (tracked, staged,
    untracked, history) must go through.

    ``args`` must already include whatever ``-z``-style flag the specific
    git subcommand uses for NUL-terminated, unquoted output (e.g.
    ``ls-files -z``, ``diff --name-only -z``). This function never
    guesses at that — it only does the split/decode step, uniformly.

    Never parse the plain (non-``-z``) text form of these commands with
    ``splitlines()``: without ``-z``, Git C-quotes any path containing a
    TAB, newline, backslash, or other byte it considers "unusual"
    (``core.quotePath``) — a path literally named ``private/x<TAB>y``
    prints as the 15-character string ``"private/x\\ty"`` (quote marks
    and a literal backslash-t included), which no longer starts with
    ``private/`` and defeats every prefix-based Path Guard rule outright.
    ``-z`` disables that quoting entirely and delimits entries with NUL,
    which can never appear inside a path, so there is never any ambiguity
    about where one entry ends and the next begins.
    """
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace')}")
    return [_decode_git_path(entry) for entry in result.stdout.split(b"\0") if entry]


def scan_staged(denylist: dict[str, list[str]] | None) -> list[Finding]:
    findings: list[Finding] = []
    names = _run_git_nul_paths("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    for path in names:
        findings.extend(check_path(path))
        # Index content, not the working tree: a file can be staged and
        # then further edited on disk without re-staging — Path/Content
        # Guard must see what would actually be committed, not whatever
        # currently sits in the worktree.
        try:
            content = _git_bytes("show", f":{path}")
        except RuntimeError:
            continue
        findings.extend(check_file_content(path, content, denylist))
    return findings


def scan_tracked(denylist: dict[str, list[str]] | None) -> list[Finding]:
    findings: list[Finding] = []
    names = _run_git_nul_paths("ls-files", "-z")
    for path in names:
        findings.extend(check_path(path))
        full = REPO_ROOT / path
        if not full.is_file():
            continue
        try:
            content = full.read_bytes()
        except OSError:
            continue
        findings.extend(check_file_content(path, content, denylist))
    return findings


def scan_untracked(denylist: dict[str, list[str]] | None) -> list[Finding]:
    """Untracked, non-gitignored files — e.g. a new doc or test not yet
    `git add`ed. A file only staged/tracked scanning would miss entirely,
    but still lives on disk in the repo tree."""
    findings: list[Finding] = []
    names = _run_git_nul_paths("ls-files", "--others", "--exclude-standard", "-z")
    for path in names:
        findings.extend(check_path(path))
        full = REPO_ROOT / path
        if not full.is_file():
            continue
        try:
            content = full.read_bytes()
        except OSError:
            continue
        findings.extend(check_file_content(path, content, denylist))
    return findings


def _historical_tree_entries() -> tuple[set[str], dict[str, str]]:
    """Enumerate every (path) that has ever existed in any reachable
    commit's tree, plus one representative path per unique blob SHA.

    Deliberately does NOT source paths from ``git rev-list --objects
    --all``: that command deduplicates OBJECTS globally across the whole
    history and prints only a single (arbitrary, first-seen) path per
    object — so if the same content (blob SHA) was ever committed at two
    different paths, only one of those paths would ever be reported, and
    Path Guard could silently miss the other one. Path history and
    content history are two different dimensions:
      - Content Guard may dedupe by blob SHA (identical bytes need
        scanning only once).
      - Path Guard must NOT dedupe by blob SHA — it needs every distinct
        path string that ever existed, independent of what content (if
        any) was ever placed there.
    Walking ``git ls-tree -r`` per reachable commit is the simple,
    provably-complete way to get that: every tree entry in every commit
    is visited directly, with no object-level deduplication in between.

    Uses ``ls-tree -rz`` (NUL-terminated records) read as raw bytes, NOT
    the default newline-terminated text form. Plain ``ls-tree -r`` quotes
    (C-style) any filename containing a TAB, newline, backslash, or other
    character Git considers "unusual" (``core.quotePath``) — a path like
    ``private/x<TAB>y`` would print as the literal 15-character string
    ``"private/x\ty"`` (quote marks and a backslash-t included), which no
    longer starts with ``private/`` and defeats every prefix-based Path
    Guard rule outright. ``-z`` disables that quoting entirely and
    delimits records with NUL, which can never appear inside a filename,
    so parsing never has to guess where one path ends and the next
    begins. Everything here stays at the bytes level until the final,
    lossless decode.
    """
    all_paths: set[str] = set()
    sha_to_a_path: dict[str, str] = {}

    commits = [c for c in _git("rev-list", "--all").splitlines() if c]
    for commit in commits:
        raw = _git_bytes("ls-tree", "-rz", commit)
        for record in raw.split(b"\0"):
            if not record:
                continue
            # Fixed-format metadata ("<mode> <type> <sha>") never contains
            # a TAB itself, so the FIRST TAB byte in the record is always
            # the metadata/filename boundary — everything after it, up to
            # the next NUL, is the raw filename as-is (which may itself
            # contain further TAB/newline bytes; those are not split on).
            meta, sep, raw_path = record.partition(b"\t")
            if not sep or not raw_path:
                continue
            parts = meta.split(b" ")
            if len(parts) < 3:
                continue
            obj_type, sha_bytes = parts[1], parts[2]
            path = _decode_git_path(raw_path)
            all_paths.add(path)
            if obj_type == b"blob":
                sha_to_a_path.setdefault(sha_bytes.decode("ascii"), path)

    return all_paths, sha_to_a_path


def scan_history(denylist: dict[str, list[str]] | None) -> list[Finding]:
    findings: list[Finding] = []

    all_paths, sha_to_a_path = _historical_tree_entries()

    # Path Guard: every distinct historical path, never deduplicated by
    # blob content.
    for path in sorted(all_paths):
        findings.extend(check_path(path))

    # Content Guard: dedupe by blob SHA (identical bytes scanned once),
    # reported against one representative path each blob was seen at.
    for sha in sorted(sha_to_a_path):
        path = sha_to_a_path[sha]
        try:
            content = _git_bytes("cat-file", "-p", sha)
        except RuntimeError:
            continue
        findings.extend(check_file_content(path, content, denylist))

    # Every reachable commit message.
    log = _git("log", "--all", "--format=%H%x01%B%x02")
    for entry in log.split("\x02"):
        entry = entry.strip("\n")
        if not entry:
            continue
        sha, _, message = entry.partition("\x01")
        findings.extend(check_commit_message(sha[:12], message, denylist))

    return findings


def _display_path(path: str) -> str:
    """Escape control characters for safe single-line terminal display
    ONLY — never used for guard matching. A raw TAB/newline/CR in a
    Finding's path would otherwise corrupt the one-line-per-finding
    report (or be visually indistinguishable from a clean path); guard
    decisions are made on the raw path before this function ever runs."""
    return path.translate({0x09: "\\t", 0x0A: "\\n", 0x0D: "\\r"})


def scan_commit_msg_file(path: Path, denylist: dict[str, list[str]] | None) -> list[Finding]:
    message = path.read_text(encoding="utf-8", errors="replace")
    return check_commit_message("pending", message, denylist)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="Scan staged changes (pre-commit)")
    mode.add_argument("--tracked", action="store_true", help="Scan every tracked file")
    mode.add_argument("--untracked", action="store_true", help="Scan untracked, non-gitignored files")
    mode.add_argument("--history", action="store_true", help="Scan every reachable commit and blob")
    mode.add_argument("--commit-msg", metavar="FILE", help="Scan a pending commit message file (commit-msg hook)")
    args = parser.parse_args(argv)

    denylist = load_denylist()
    if denylist:
        print(f"privacy_scan: loaded local denylist ({len(denylist)} categories)")
    else:
        print("privacy_scan: no local denylist configured (BEL_PRIVACY_DENYLIST unset) — Generic Guard only")

    if args.staged:
        findings = scan_staged(denylist)
    elif args.tracked:
        findings = scan_tracked(denylist)
    elif args.untracked:
        findings = scan_untracked(denylist)
    elif args.history:
        findings = scan_history(denylist)
    else:
        findings = scan_commit_msg_file(Path(args.commit_msg), denylist)

    if not findings:
        print("privacy_scan: PASS — 0 findings")
        return 0

    print(f"PRIVACY BLOCKER — {len(findings)} finding(s):")
    for f in findings:
        display_file = _display_path(f.file)
        location = f"{display_file}:{f.line}" if f.line is not None else display_file
        print(f"  [{f.rule}] {location} — {f.detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

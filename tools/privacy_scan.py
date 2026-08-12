#!/usr/bin/env python3
"""Privacy scanner — blocks real business data from entering Git.

See docs/PRIVATE-DATA-POLICY.md for the policy this enforces.

Modes (pick one):
    python tools/privacy_scan.py --staged           # staged changes (pre-commit)
    python tools/privacy_scan.py --tracked           # every tracked file (CI, on-demand)
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


def scan_staged(denylist: dict[str, list[str]] | None) -> list[Finding]:
    findings: list[Finding] = []
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines()
    for path in names:
        if not path:
            continue
        findings.extend(check_path(path))
        try:
            content = _git_bytes("show", f":{path}")
        except RuntimeError:
            continue
        findings.extend(check_file_content(path, content, denylist))
    return findings


def scan_tracked(denylist: dict[str, list[str]] | None) -> list[Finding]:
    findings: list[Finding] = []
    names = _git("ls-files").splitlines()
    for path in names:
        if not path:
            continue
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


def scan_history(denylist: dict[str, list[str]] | None) -> list[Finding]:
    findings: list[Finding] = []

    # rev-list --objects lists commits, trees, AND blobs, with a path only
    # for the latter two. We only want blobs (tree content is git's own
    # binary directory-listing format, not file content, and scanning it
    # as text produces noise). Resolve object types in one batch call
    # rather than shelling out per-object.
    objects = _git("rev-list", "--objects", "--all")
    sha_to_path: dict[str, str] = {}
    for line in objects.splitlines():
        sha, _, path = line.partition(" ")
        if sha and path:
            sha_to_path[sha] = path

    batch_input = "\n".join(sha_to_path)
    batch_check = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        cwd=REPO_ROOT,
        input=batch_input,
        capture_output=True,
        text=True,
    )
    blob_shas = set()
    for line in batch_check.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "blob":
            blob_shas.add(parts[0])

    seen: set[str] = set()
    for sha, path in sha_to_path.items():
        if sha not in blob_shas:
            continue
        key = f"{sha}:{path}"
        if key in seen:
            continue
        seen.add(key)
        findings.extend(check_path(path))
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


def scan_commit_msg_file(path: Path, denylist: dict[str, list[str]] | None) -> list[Finding]:
    message = path.read_text(encoding="utf-8", errors="replace")
    return check_commit_message("pending", message, denylist)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="Scan staged changes (pre-commit)")
    mode.add_argument("--tracked", action="store_true", help="Scan every tracked file")
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
    elif args.history:
        findings = scan_history(denylist)
    else:
        findings = scan_commit_msg_file(Path(args.commit_msg), denylist)

    if not findings:
        print("privacy_scan: PASS — 0 findings")
        return 0

    print(f"PRIVACY BLOCKER — {len(findings)} finding(s):")
    for f in findings:
        location = f"{f.file}:{f.line}" if f.line is not None else f.file
        print(f"  [{f.rule}] {location} — {f.detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

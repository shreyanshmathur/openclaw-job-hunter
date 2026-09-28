#!/usr/bin/env python3
"""Fail when git would commit personal data or runtime files (design 10.6, M10), run by CI and pre-commit.

    python3 tools/check_gitignore.py                     # tracked files (git ls-files)
    python3 tools/check_gitignore.py --include-untracked  # plus untracked files that are not ignored
    python3 tools/check_gitignore.py --paths a b c       # check a given list of repo-relative paths

Refused: anything under private/, state/, logs/, exports/, workspaces/ or resumes/; a rendered AGENTS.md or
SKILL.md; USER.md, TOOLS.md, HEARTBEAT.md, MEMORY.md, DREAMS.md, BOOTSTRAP.md; a nested .git or a submodule;
PDF and DOCX files; images outside docs/img/; SQLite files; .env files and secrets.json.
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIRS = ("private", "state", "logs", "exports", "workspaces", "resumes")
RENDERED = ("AGENTS.md", "SKILL.md")
OPENCLAW_RUNTIME = ("USER.md", "TOOLS.md", "HEARTBEAT.md", "MEMORY.md", "DREAMS.md", "BOOTSTRAP.md")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tif", ".tiff", ".ico")
DOC_EXT = (".pdf", ".docx", ".doc")
SQLITE_EXT = (".sqlite", ".sqlite3", ".db", ".sqlite3-wal", ".sqlite3-shm", ".db-wal", ".db-shm")


def violation(path: str) -> str | None:
    """Why a repo-relative path must not be committed, or None."""
    p = path.replace(os.sep, "/")
    while p.startswith("./"):
        p = p[2:]
    parts = p.split("/")
    base = parts[-1]
    low = base.lower()
    if parts[0] in RUNTIME_DIRS:
        return "runtime or personal folder %s/" % parts[0]
    if ".git" in parts[:-1] or base == ".git":
        return "nested .git"
    if base in RENDERED:
        return "rendered %s (only *.template.md files belong in git)" % base
    if base in OPENCLAW_RUNTIME:
        return "OpenClaw runtime file %s" % base
    if low.endswith(DOC_EXT):
        return "document file (resumes and letters are personal)"
    if low.endswith(IMAGE_EXT) and not p.startswith("docs/img/"):
        return "image outside docs/img/"
    if low.endswith(SQLITE_EXT):
        return "SQLite database file"
    if low == ".env" or low.startswith(".env.") or low == "secrets.json":
        return "secrets file"
    return None


def git_paths(repo: str, include_untracked: bool) -> tuple[list[str], list[str]]:
    """(paths, gitlinks) from git. Gitlinks (mode 160000) are submodules or nested repos."""
    staged = subprocess.run(["git", "-C", repo, "ls-files", "-s", "-z"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=True).stdout.decode("utf-8", "replace")
    paths, links = [], []
    for rec in staged.split("\0"):
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        paths.append(path)
        if meta.startswith("160000"):
            links.append(path)
    if include_untracked:
        extra = subprocess.run(["git", "-C", repo, "ls-files", "-o", "--exclude-standard", "-z"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        paths += [x for x in extra.decode("utf-8", "replace").split("\0") if x]
    return sorted(set(paths)), links


def check(paths: list[str], gitlinks: list[str] | None = None) -> list[str]:
    out = []
    for p in paths:
        why = violation(p)
        if why:
            out.append("%s: %s" % (p, why))
    for g in gitlinks or []:
        out.append("%s: submodule or nested repository" % g)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo = REPO
    if argv[:1] == ["--repo"]:
        repo, argv = os.path.abspath(argv[1]), argv[2:]
    if argv[:1] == ["--paths"]:
        problems = check(argv[1:])
    else:
        try:
            paths, links = git_paths(repo, "--include-untracked" in argv)
        except (OSError, subprocess.CalledProcessError) as exc:
            print("check_gitignore: git is not available here: %s" % exc, file=sys.stderr)
            return 2
        problems = check(paths, links)
    for p in problems:
        print(p)
    if problems:
        print("check_gitignore: %d file(s) must not be committed; see .gitignore" % len(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

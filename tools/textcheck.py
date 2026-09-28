#!/usr/bin/env python3
"""Repo text rules (design 10.7), run by CI and the pre-commit hook.

    python3 tools/textcheck.py              # every tracked and untracked-but-not-ignored text file
    python3 tools/textcheck.py FILE ...     # only these files
    python3 tools/textcheck.py --staged     # the staged snapshot (the git index), as the pre-commit hook runs it

Rules:
  T-NONASCII   any character outside ASCII in a text file (named when it is a dash, a minus sign, a curly
               quote, an ellipsis or a no-break space)
  T-HOMEPATH   an absolute home path of a real machine (the macOS or Linux home prefix)
  T-DDASH      " -- " used as a dash in prompts, templates and examples
  T-SPACEDASH  " - " used as a dash in prompts, templates and examples (list bullets and code are fine)
  T-ENCODING   a UTF-16 or UTF-32 text file (it is still decoded and checked; save it as ASCII)
With --staged the files are read from the index (what `git commit` records), not from the working tree, so a
file that was staged and then edited or deleted is checked as it will be committed.
Exit 0 when clean, 1 with one line per finding: path:line:col: RULE message.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".docx", ".xlsx", ".zip", ".gz", ".tgz",
              ".woff", ".woff2", ".ttf", ".otf", ".sqlite", ".sqlite3", ".db", ".heic", ".bmp", ".tiff", ".pyc")
NAMED = {
    0x2010: "hyphen", 0x2011: "non-breaking hyphen", 0x2012: "figure dash", 0x2013: "en dash", 0x2014: "em dash",
    0x2015: "horizontal bar", 0x2212: "minus sign", 0x2E3A: "two-em dash", 0x2E3B: "three-em dash",
    0xFE31: "vertical em dash", 0xFE32: "vertical en dash", 0xFE58: "small em dash", 0xFE63: "small hyphen-minus",
    0xFF0D: "fullwidth hyphen-minus", 0x2018: "curly quote", 0x2019: "curly quote", 0x201C: "curly quote",
    0x201D: "curly quote", 0x2026: "ellipsis", 0x00A0: "no-break space", 0x202F: "no-break space",
    0xFEFF: "byte order mark",
}
# built from pieces so this file does not trip its own rule
HOME_PATH_RE = re.compile("(?<![A-Za-z0-9_.~-])/(" + "Us" + "ers|" + "ho" + "me)/[A-Za-z0-9._-]+")
DASH_SCOPE_PREFIXES = ("prompts/", "agent-templates/", "skills-src/", "shared-skills/", "qc/golden/",
                       "private.example/", "examples/")
DASH_SCOPE_SUFFIXES = (".template.md", ".tmpl")
DDASH_RE = re.compile(r"(?<=\S) -- (?=\S)")
SPACEDASH_RE = re.compile(r"(?<=\S) - (?=\S)")
INLINE_CODE_RE = re.compile(r"`[^`]*`")


def repo_files(repo: str = REPO) -> list[str]:
    """Repo-relative paths of tracked and untracked-but-not-ignored files (git), else a filtered walk."""
    try:
        out = subprocess.run(["git", "-C", repo, "ls-files", "-c", "-o", "--exclude-standard", "-z"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        files = sorted({p for p in out.decode("utf-8", "replace").split("\0") if p})
        return [p for p in files if os.path.isfile(os.path.join(repo, p))]
    except (OSError, subprocess.CalledProcessError):
        pass
    skip_dirs = {".git", "private", "state", "logs", "exports", "workspaces", "resumes", "node_modules",
                 "__pycache__", ".venv"}
    out_files = []
    for dirpath, dirs, fnames in os.walk(repo):
        rel_dir = os.path.relpath(dirpath, repo)
        dirs[:] = [d for d in dirs if not (rel_dir == "." and d in skip_dirs) and d not in ("__pycache__", ".git",
                                                                                              "node_modules")]
        for fn in fnames:
            if fn == ".DS_Store" or fn.endswith(".pyc"):
                continue
            out_files.append(os.path.normpath(os.path.join(rel_dir, fn)))
    return sorted(out_files)


def _utf16_guess(head: bytes) -> str | None:
    """'utf-16-le' or 'utf-16-be' when the bytes look like UTF-16 text without a byte order mark: most bytes at one
    parity are NUL and almost none at the other (ASCII text saved as UTF-16), else None."""
    n = len(head) - (len(head) % 2)
    if n < 4:
        return None
    even = sum(1 for i in range(0, n, 2) if head[i] == 0)
    odd = sum(1 for i in range(1, n, 2) if head[i] == 0)
    half = n // 2
    if odd >= 0.7 * half and even <= 0.05 * half:
        return "utf-16-le"
    if even >= 0.7 * half and odd <= 0.05 * half:
        return "utf-16-be"
    return None


def text_encoding(path: str, data: bytes) -> str | None:
    """How to decode a file for the checks: 'utf-8', a UTF-16 or UTF-32 codec, or None for a binary file. A NUL byte
    alone does not make a file binary: UTF-16 text (with or without a byte order mark) is decoded and checked."""
    if path.lower().endswith(BINARY_EXT):
        return None
    head = data[:8192]
    if head.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return "utf-32"
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if b"\x00" not in head:
        return "utf-8"
    return _utf16_guess(head)


def decode_text(path: str, data: bytes) -> tuple[str | None, str]:
    """(text, encoding) of a file; text is None for a binary file."""
    enc = text_encoding(path, data)
    if enc is None:
        return None, ""
    return data.decode(enc, "replace"), enc


def is_binary(path: str, data: bytes) -> bool:
    return text_encoding(path, data) is None


# ---------------------------------------------------------------- the staged snapshot (git index)
def _git(repo: str, args: list[str], data: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", repo] + args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=True).stdout


def staged_entries(repo: str = REPO) -> list[tuple[str, str]]:
    """(path, blob id) of every regular file in the index. The pre-commit hook inherits GIT_INDEX_FILE, so this is
    the snapshot the commit records, including `git commit -a` and `git commit <paths>`. Symlinks and submodules are
    left out (check_gitignore refuses submodules)."""
    out = _git(repo, ["ls-files", "-s", "-z"]).decode("utf-8", "replace")
    entries = {}
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) != 3 or parts[0] not in ("100644", "100755"):
            continue
        if parts[2] in ("0", "2") or path not in entries:   # unmerged paths: take "ours"
            entries[path] = parts[1]
    return sorted(entries.items())


def read_blobs(repo: str, ids: list[str]) -> dict[str, bytes]:
    """{blob id: content} with one `git cat-file --batch` call."""
    if not ids:
        return {}
    uniq = sorted(set(ids))
    raw = _git(repo, ["cat-file", "--batch"], ("\n".join(uniq) + "\n").encode("ascii"))
    out, pos = {}, 0
    for _ in uniq:
        nl = raw.index(b"\n", pos)
        header = raw[pos:nl].decode("ascii", "replace").split()
        pos = nl + 1
        if len(header) < 3 or header[1] == "missing":
            continue
        size = int(header[2])
        out[header[0]] = raw[pos:pos + size]
        pos += size + 1
    return out


def staged_files(repo: str = REPO, only: list[str] | None = None) -> list[tuple[str, bytes]]:
    """(path, staged content) of the files in the index (or of `only`, repo-relative paths that are staged)."""
    entries = staged_entries(repo)
    if only is not None:
        want = {p.replace(os.sep, "/") for p in only}
        entries = [(p, b) for p, b in entries if p in want]
    blobs = read_blobs(repo, [b for _, b in entries])
    return [(p, blobs[b]) for p, b in entries if b in blobs]


def split_args(argv: list[str]) -> tuple[str, bool, list[str]]:
    """(repo, staged, files) from a tools/*check.py command line: [--repo DIR] [--staged] [FILE ...]."""
    repo, staged, files = REPO, False, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--repo" and i + 1 < len(argv):
            repo = os.path.abspath(argv[i + 1])
            i += 2
            continue
        if a == "--staged":
            staged = True
        else:
            files.append(a)
        i += 1
    return repo, staged, files


def rel_paths(repo: str, files: list[str]) -> list[str]:
    return [os.path.relpath(os.path.abspath(p), repo).replace(os.sep, "/") for p in files]


def in_dash_scope(rel: str) -> bool:
    rel = rel.replace(os.sep, "/")
    return rel.startswith(DASH_SCOPE_PREFIXES) or rel.endswith(DASH_SCOPE_SUFFIXES) or "/examples/" in rel \
        or ".example." in os.path.basename(rel)


def check_text(rel: str, text: str) -> list[tuple[int, int, str, str]]:
    """Findings for one file: (line, col, rule, message)."""
    findings = []
    dash_scope = in_dash_scope(rel)
    in_fence = False
    for ln, line in enumerate(text.split("\n"), 1):
        for col, ch in enumerate(line, 1):
            if ord(ch) > 0x7E or (ord(ch) < 0x20 and ch not in "\t\r"):
                name = NAMED.get(ord(ch), "non-ASCII character")
                findings.append((ln, col, "T-NONASCII", "%s U+%04X" % (name, ord(ch))))
        for m in HOME_PATH_RE.finditer(line):
            findings.append((ln, m.start() + 1, "T-HOMEPATH", "absolute home path %s" % m.group(0)[:40]))
        if dash_scope:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            body = INLINE_CODE_RE.sub(lambda m: "x" * len(m.group(0)), line)
            lead = len(body) - len(body.lstrip())
            if body.lstrip().startswith(("- ", "-- ")):
                body = " " * (lead + 2) + body.lstrip()[2:]
            for m in DDASH_RE.finditer(body):
                findings.append((ln, m.start() + 1, "T-DDASH", "' -- ' used as a dash"))
            for m in SPACEDASH_RE.finditer(body):
                findings.append((ln, m.start() + 1, "T-SPACEDASH", "' - ' used as a dash"))
    return findings


def check_data(rel: str, data: bytes) -> list[str]:
    text, enc = decode_text(rel, data)
    if text is None:
        return []
    out = []
    if enc != "utf-8":
        out.append("%s:1:1: T-ENCODING %s text; save it as ASCII (or UTF-8)" % (rel, enc.upper()))
    if text.startswith("\ufeff") and enc != "utf-8":
        text = text[1:]
    return out + ["%s:%d:%d: %s %s" % (rel, ln, col, rule, msg) for ln, col, rule, msg in check_text(rel, text)]


def check_file(repo: str, rel: str) -> list[str]:
    path = os.path.join(repo, rel)
    if os.path.isdir(path):
        return []
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return ["%s:0:0: T-READ %s" % (rel, exc)]
    return check_data(rel, data)


def main(argv: list[str] | None = None) -> int:
    repo, staged, args = split_args(list(sys.argv[1:] if argv is None else argv))
    problems = []
    if staged:
        try:
            snapshot = staged_files(repo, rel_paths(repo, args) if args else None)
        except (OSError, subprocess.CalledProcessError) as exc:
            print("textcheck: cannot read the git index: %s" % exc, file=sys.stderr)
            return 2
        for rel, data in snapshot:
            problems.extend(check_data(rel, data))
    else:
        for rel in (rel_paths(repo, args) if args else repo_files(repo)):
            problems.extend(check_file(repo, rel))
    for p in problems:
        print(p)
    if problems:
        print("textcheck: %d finding(s) in %d file(s)" % (len(problems), len({p.split(":", 1)[0] for p in problems})),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

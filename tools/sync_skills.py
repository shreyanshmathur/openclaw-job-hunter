#!/usr/bin/env python3
"""Skill and template consistency (design 10.2 and 10.3 step 7), run by CI and install.sh.

    python3 tools/sync_skills.py            # print which agent gets which skill from where
    python3 tools/sync_skills.py --check    # exit 1 on any problem (CI, install.sh)
    python3 tools/sync_skills.py --json     # the mapping as JSON

openclaw/agents.json is the source of the per-agent skill allowlists. Checks:
  S-TEMPLATE   every role has AGENTS.template.md, SOUL.md and IDENTITY.md; AGENTS.template.md stays under
               12,000 characters
  S-MISSING    every listed skill exists in agent-templates/<role>/skills/<name>/ or skills-src/<name>/
  S-TWICE      a skill name exists in both places
  S-FRONT      SKILL.template.md frontmatter: name equals the folder, one-line description, user-invocable:
               false, metadata with an "openclaw" block
  S-ORPHAN     a skill folder that no agent lists (role skills must be listed by that role's agent)
  S-RENDERED   a rendered AGENTS.md or SKILL.md inside the templates (only *.template.md belong in git)
shared-skills/*/SKILL.template.md (optional skill for the person's own agent) gets the frontmatter check.
"""
from __future__ import annotations

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_MD_MAX = 12000


def load_agents(repo: str) -> list[dict]:
    with open(os.path.join(repo, "openclaw", "agents.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)["agents"]


def frontmatter(text: str) -> dict | None:
    """Top-level keys of a leading --- block (values as raw text; indented lines join the previous key)."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    out: dict = {}
    last = None
    for line in text[4:end].split("\n"):
        if not line.strip():
            continue
        if line[0] in " \t" and last:
            out[last] += "\n" + line.strip()
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            return None
        last = m.group(1)
        out[last] = m.group(2).strip()
    return out


def check_skill_file(path: str, name: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return ["S-FRONT %s: unreadable (%s)" % (path, exc)]
    fm = frontmatter(text)
    rel = path
    if fm is None:
        return ["S-FRONT %s: no frontmatter block" % rel]
    out = []
    if fm.get("name") != name:
        out.append("S-FRONT %s: name %r must equal the folder name %r" % (rel, fm.get("name"), name))
    desc = fm.get("description", "")
    if not desc or "\n" in desc:
        out.append("S-FRONT %s: description must be one non-empty line" % rel)
    if fm.get("user-invocable", "").lower() != "false":
        out.append("S-FRONT %s: user-invocable must be false" % rel)
    if '"openclaw"' not in fm.get("metadata", ""):
        out.append("S-FRONT %s: metadata must carry an \"openclaw\" block" % rel)
    return out


def analyse(repo: str) -> tuple[list[dict], list[str]]:
    agents = load_agents(repo)
    problems: list[str] = []
    mapping: list[dict] = []
    used: set[str] = set()
    for a in agents:
        tdir = os.path.join(repo, a["template_dir"])
        for f in ("AGENTS.template.md", "SOUL.md", "IDENTITY.md"):
            p = os.path.join(tdir, f)
            if not os.path.isfile(p):
                problems.append("S-TEMPLATE %s/%s is missing" % (a["template_dir"], f))
            elif f == "AGENTS.template.md":
                with open(p, "r", encoding="utf-8") as fh:
                    n = len(fh.read())
                if n > AGENTS_MD_MAX:
                    problems.append("S-TEMPLATE %s/%s has %d characters (max %d)" % (a["template_dir"], f, n,
                                                                                       AGENTS_MD_MAX))
        for sk in a["skills"]:
            role_src = os.path.join(tdir, "skills", sk)
            shared_src = os.path.join(repo, "skills-src", sk)
            have = [p for p in (role_src, shared_src) if os.path.isfile(os.path.join(p, "SKILL.template.md"))]
            if not have:
                problems.append("S-MISSING %s: skill %s not found in %s/skills/ or skills-src/" % (a["id"], sk,
                                                                                                  a["template_dir"]))
                continue
            if len(have) > 1:
                problems.append("S-TWICE skill %s exists in %s/skills/ and skills-src/" % (sk, a["template_dir"]))
            src = have[0]
            used.add(os.path.realpath(src))
            mapping.append({"agent": a["id"], "skill": sk, "source": os.path.relpath(src, repo)})
            problems.extend(check_skill_file(os.path.join(src, "SKILL.template.md"), sk))
    # orphans: skill folders nobody lists
    candidates = []
    for a in agents:
        base = os.path.join(repo, a["template_dir"], "skills")
        if os.path.isdir(base):
            candidates += [os.path.join(base, d) for d in sorted(os.listdir(base))]
    base = os.path.join(repo, "skills-src")
    if os.path.isdir(base):
        candidates += [os.path.join(base, d) for d in sorted(os.listdir(base))]
    seen_real = set()
    for c in candidates:
        real = os.path.realpath(c)
        if not os.path.isdir(c) or real in seen_real:
            continue
        seen_real.add(real)
        if real not in used:
            problems.append("S-ORPHAN %s is not listed by any agent in openclaw/agents.json" % os.path.relpath(c, repo))
    shared = os.path.join(repo, "shared-skills")
    if os.path.isdir(shared):
        for d in sorted(os.listdir(shared)):
            p = os.path.join(shared, d, "SKILL.template.md")
            if os.path.isfile(p):
                problems.extend(check_skill_file(p, d))
    for top in ("agent-templates", "skills-src", "shared-skills"):
        for dirpath, _dirs, fnames in os.walk(os.path.join(repo, top)):
            for fn in fnames:
                if fn in ("AGENTS.md", "SKILL.md"):
                    problems.append("S-RENDERED %s must not exist in the repo"
                                    % os.path.relpath(os.path.join(dirpath, fn), repo))
    return mapping, [p.replace(repo + os.sep, "") for p in problems]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo = REPO
    if "--repo" in argv:
        i = argv.index("--repo")
        repo = os.path.abspath(argv[i + 1])
        del argv[i:i + 2]
    try:
        mapping, problems = analyse(repo)
    except (OSError, ValueError, KeyError) as exc:
        print("sync_skills: cannot read openclaw/agents.json: %s" % exc, file=sys.stderr)
        return 1
    if "--json" in argv:
        print(json.dumps({"skills": mapping, "problems": problems}, indent=2))
    elif "--check" not in argv:
        for m in mapping:
            print("%-22s %-28s %s" % (m["agent"], m["skill"], m["source"]))
    for p in problems:
        print(p, file=sys.stderr if "--json" in argv else sys.stdout)
    if problems and ("--check" in argv or "--json" in argv):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

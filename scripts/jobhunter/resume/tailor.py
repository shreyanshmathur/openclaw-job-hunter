"""Truthful resume tailoring (design 6.4, 12.5) and the resume `R-*` checks (5.2).

A tailor file may only choose, order and (mode `full`) rephrase what the base resume already says:

    {"job_uid": "J7Q2KX4M", "mode": "light",
     "summary": {"text": "...", "fact_ids": ["P2", "P1"]},
     "sections_order": ["experience", "projects", "skills", "education"],
     "experience": [{"role_id": "E1", "bullets": [{"from": "E1.B2", "text": "..."}, {"from": "E1.B1", "text": null}]}],
     "projects": [{"project_id": "PR1", "include": true}],
     "skills_order": ["SQL", "Python", "Forecasting"]}

`text: null` keeps the base bullet verbatim. Omitted bullets of a listed role are dropped (at least one
stays); roles that are not listed keep all their base bullets, so no employer ever disappears. Titles,
employers, dates, education and the contact block are copied from the base and cannot be changed.

`lint()` returns the linter contract of writing-qc 8.4: {"pass", "blocks": [[rule, detail]], "warns",
"metrics"}. Rules (all block unless noted):
  R-ROLE-UNKNOWN, R-BULLET-UNKNOWN, R-BULLET-DUP, R-ROLE-EMPTY, R-PROJECT-UNKNOWN, R-BULLETS-MANY (warn),
  R-LIGHT-REPHRASE (mode light changes a bullet's words), R-NEW-NUMBER (a number that is not in the base
  bullet), R-NEW-SKILL (a skill or tool that the base resume, the extra information and the profile facts
  never mention), R-SUMMARY-FACT, R-SUMMARY-NUMBER, R-SUMMARY-LONG, R-TITLE-CHANGED, R-EMPLOYER-CHANGED,
  R-DATES-CHANGED, R-CONTACT-CHANGED, R-EDUCATION-CHANGED, R-PAGES, and the character rules R-DASH,
  R-SPACED-HYPHEN, R-CURLY, R-ELLIPSIS, R-INVISIBLE, R-EMOJI, R-BULLET-CHAR, R-CONTROL, R-NON-WINANSI,
  R-NON-ASCII (warn: a non-ASCII letter outside names).
"""
from __future__ import annotations

import copy
import re

from ..errors import Denied
from . import model as M

MODES = ("light", "full")
MODE_RANK = {"off": 0, "base": 0, "light": 1, "full": 2}
MAX_BULLETS_PER_ROLE = 5
MAX_SUMMARY_CHARS = 240

_TOP_KEYS = {"job_uid", "mode", "summary", "sections_order", "experience", "projects", "skills_order"}
_FACT_RE = re.compile(r"^P[1-9][0-9]{0,3}$")

# Skills and tools recognised even when written in lower case. A term outside this list still counts as
# a skill when it looks like one (acronym, CamelCase, digits or + # . inside a word).
SKILL_LEXICON = frozenset("""
sql python r java javascript typescript scala go golang rust ruby php perl kotlin swift matlab sas stata spss
excel vba powerpoint tableau looker lookml metabase superset qlik qliksense powerbi dax
dbt airflow dagster prefect luigi kafka spark pyspark hadoop hive presto trino flink beam storm
snowflake bigquery redshift databricks synapse postgres postgresql mysql mariadb oracle mongodb cassandra
redis elasticsearch opensearch dynamodb clickhouse duckdb sqlite neo4j
pandas numpy scipy statsmodels sklearn scikit-learn tensorflow pytorch keras xgboost lightgbm catboost
huggingface transformers langchain llamaindex openai spacy nltk opencv prophet
aws gcp azure lambda ec2 s3 kubernetes docker terraform ansible jenkins helm istio linux bash git github gitlab
jira confluence salesforce hubspot marketo segment amplitude mixpanel heap optimizely braze
react angular vue nextjs node nodejs django flask fastapi spring rails graphql grpc rest
figma sketch photoshop illustrator
sap netsuite quickbooks tally
""".split())
_SKILL_PHRASES = ("power bi", "google analytics", "google sheets", "machine learning", "deep learning",
                  "computer vision", "natural language processing", "a/b testing", "ab testing",
                  "google cloud", "data studio", "looker studio", "adobe analytics")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.]*[A-Za-z0-9+#]|[A-Za-z]")


# ---------------------------------------------------------------- schema
def validate_schema(tailor, base: dict) -> dict:
    """Structure only (types, keys, ids that exist). Denied(E_SCHEMA) on any problem. Returns a normalized copy."""
    errs: list[str] = []
    if not isinstance(tailor, dict):
        raise Denied("E_SCHEMA", "the tailor file must be a JSON object")
    extra = set(tailor) - _TOP_KEYS
    if extra:
        errs.append("unknown keys %s" % sorted(extra))
    if not isinstance(tailor.get("job_uid"), str):
        errs.append("job_uid is required")
    mode = tailor.get("mode")
    if mode not in MODES:
        errs.append("mode must be light or full")
    summary = tailor.get("summary")
    if summary is not None:
        if not isinstance(summary, dict) or set(summary) - {"text", "fact_ids"}:
            errs.append("summary must be an object {text, fact_ids}")
        else:
            if not isinstance(summary.get("text"), str):
                errs.append("summary.text must be a string")
            fids = summary.get("fact_ids")
            if not isinstance(fids, list) or not all(isinstance(f, str) and _FACT_RE.match(f) for f in fids):
                errs.append("summary.fact_ids must be a list like [\"P1\"]")
    order = tailor.get("sections_order")
    if order is not None:
        if not isinstance(order, list) or not all(isinstance(s, str) for s in order):
            errs.append("sections_order must be a list of section names")
        else:
            bad = [s for s in order if s not in M.SECTIONS]
            if bad:
                errs.append("sections_order has unknown sections %s" % bad)
            if len(set(order)) != len(order):
                errs.append("sections_order has duplicates")
    exp = tailor.get("experience", [])
    if not isinstance(exp, list):
        errs.append("experience must be a list")
        exp = []
    for i, r in enumerate(exp):
        if not isinstance(r, dict) or set(r) - {"role_id", "bullets"} or not isinstance(r.get("role_id"), str):
            errs.append("experience[%d] must be an object {role_id, bullets}" % i)
            continue
        bl = r.get("bullets")
        if not isinstance(bl, list):
            errs.append("experience[%d].bullets must be a list" % i)
            continue
        for j, b in enumerate(bl):
            if (not isinstance(b, dict) or set(b) - {"from", "text"} or not isinstance(b.get("from"), str)
                    or not (b.get("text") is None or isinstance(b.get("text"), str))):
                errs.append("experience[%d].bullets[%d] must be an object {from, text|null}" % (i, j))
    projects = tailor.get("projects", [])
    if not isinstance(projects, list):
        errs.append("projects must be a list")
        projects = []
    for i, p in enumerate(projects):
        if (not isinstance(p, dict) or set(p) - {"project_id", "include"} or not isinstance(p.get("project_id"), str)
                or not isinstance(p.get("include", True), bool)):
            errs.append("projects[%d] must be an object {project_id, include}" % i)
    skills = tailor.get("skills_order", [])
    if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
        errs.append("skills_order must be a list of strings")
    if errs:
        raise Denied("E_SCHEMA", "the tailor file does not match the contract (12.5)", data={"errors": errs})
    out = copy.deepcopy(tailor)
    out.setdefault("summary", None)
    out.setdefault("sections_order", None)
    out.setdefault("experience", [])
    out.setdefault("projects", [])
    out.setdefault("skills_order", [])
    return out


# ---------------------------------------------------------------- apply
def apply(base: dict, tailor: dict) -> dict:
    """Render model for a tailor file (after validate_schema). Unknown ids are skipped here; lint() reports them."""
    m = M.to_render_model(base)
    if tailor.get("sections_order"):
        order = list(tailor["sections_order"])
        for s in base.get("sections_order") or M.DEFAULT_ORDER:
            if s not in order:
                order.append(s)
        m["sections_order"] = order
    summary = tailor.get("summary")
    if summary and isinstance(summary.get("text"), str) and summary["text"].strip():
        m["summary"] = summary["text"].strip()
    by_role = {r["role_id"]: r for r in m.get("experience", [])}
    index = M.bullet_index(base)
    for tr in tailor.get("experience", []):
        role = by_role.get(tr.get("role_id"))
        if role is None:
            continue
        new_bullets = []
        seen = set()
        for tb in tr.get("bullets", []):
            src = tb.get("from")
            owner_text = index.get(src)
            if owner_text is None or owner_text[0] != role["role_id"] or src in seen:
                continue
            seen.add(src)
            text = tb.get("text")
            new_bullets.append({"id": src, "text": owner_text[1] if text is None else text.strip()})
        role["bullets"] = new_bullets
    listed = {p.get("project_id"): p for p in tailor.get("projects", [])}
    if listed:
        kept = []
        ordered_ids = [p.get("project_id") for p in tailor.get("projects", [])]
        by_id = {p["project_id"]: p for p in m.get("projects", [])}
        for pid in ordered_ids:
            if pid in by_id and listed[pid].get("include", True):
                kept.append(by_id[pid])
        for p in m.get("projects", []):
            if p["project_id"] not in listed:
                kept.append(p)
        m["projects"] = kept
    if tailor.get("skills_order"):
        base_skills = list(m.get("skills", []))
        lower = {s.lower(): s for s in base_skills}
        first = []
        for s in tailor["skills_order"]:
            k = s.strip().lower()
            if k in lower and lower[k] not in first:
                first.append(lower[k])
        m["skills"] = first + [s for s in base_skills if s not in first]
    return m


# ---------------------------------------------------------------- corpus and terms
def _terms(text: str) -> set[str]:
    """Skill-like terms in text (lower-cased)."""
    out = set()
    if not text:
        return out
    low = text.lower()
    for ph in _SKILL_PHRASES:
        if re.search(r"(?<![a-z0-9])" + re.escape(ph) + r"(?![a-z0-9])", low):
            out.add(ph)
    for tok in _TOKEN_RE.findall(text):
        t = tok.rstrip(".")
        lt = t.lower()
        if not t:
            continue
        looks = (lt in SKILL_LEXICON
                 or (len(t) >= 2 and t.isupper() and t.isalpha())            # acronym: ETL, AWS
                 or any(ch.isupper() for ch in t[1:]) and any(ch.islower() for ch in t)   # CamelCase
                 or (any(ch.isdigit() for ch in t) and any(ch.isalpha() for ch in t))     # S3, EC2
                 or any(ch in "+#" for ch in t)                                # C++, C#
                 or ("." in t and not t.endswith(".")))                        # Node.js
        if looks:
            out.add(lt)
    return out


def _words(text: str) -> set[str]:
    return {t.rstrip(".").lower() for t in _TOKEN_RE.findall(text or "")}


def known_corpus(base: dict, facts: dict | None, extra_text: str | None) -> set[str]:
    """Lower-cased words and phrases the person's own sources mention (base resume, profile facts,
    extra information)."""
    texts = [M.all_text(base)]
    texts.extend((facts or {}).values())
    if extra_text:
        texts.append(extra_text)
    joined = "\n".join(t for t in texts if isinstance(t, str))
    corpus = _words(joined)
    low = joined.lower()
    for ph in _SKILL_PHRASES:
        if ph in low:
            corpus.add(ph)
    return corpus


# ---------------------------------------------------------------- lint
def lint(base: dict, tailor: dict, rendered: dict, *, facts: dict | None = None, extra_text: str | None = None,
         pages: int | None = None, max_pages: int = 2) -> dict:
    """R-* checks of a tailored model against its base. `facts` is {"P1": text}. Numbers in the summary must
    appear in the facts it cites (the same rule as the QC linter's R-SUMMARY-NUMBER)."""
    blocks: list[list[str]] = []
    warns: list[list[str]] = []

    def B(rule, detail=""):
        blocks.append([rule, detail])

    def W(rule, detail=""):
        warns.append([rule, detail])

    facts = facts or {}
    mode = tailor.get("mode")
    index = M.bullet_index(base)
    base_roles = {r["role_id"]: r for r in base.get("experience", [])}
    corpus = known_corpus(base, facts, extra_text)

    # bullets
    seen_roles = set()
    for tr in tailor.get("experience", []):
        rid = tr.get("role_id")
        if rid not in base_roles:
            B("R-ROLE-UNKNOWN", str(rid))
            continue
        if rid in seen_roles:
            B("R-ROLE-UNKNOWN", "%s listed twice" % rid)
        seen_roles.add(rid)
        used = set()
        valid = 0
        for tb in tr.get("bullets", []):
            src = tb.get("from")
            owner = index.get(src)
            if owner is None or owner[0] != rid:
                B("R-BULLET-UNKNOWN", "%s is not a bullet of %s" % (src, rid))
                continue
            if src in used:
                B("R-BULLET-DUP", src)
                continue
            used.add(src)
            valid += 1
            text = tb.get("text")
            if text is None:
                continue
            base_text = owner[1]
            if mode == "light" and " ".join(text.split()) != " ".join(base_text.split()):
                B("R-LIGHT-REPHRASE", "%s: mode light keeps bullets verbatim (use text null)" % src)
            missing = M.numbers_subset(M.numbers(text), M.numbers(base_text))
            if missing:
                B("R-NEW-NUMBER", "%s: %s" % (src, ", ".join(sorted(n + u for n, u in missing))))
            new_terms = sorted(t for t in _terms(text) if t not in corpus)
            if new_terms:
                B("R-NEW-SKILL", "%s: %s" % (src, ", ".join(new_terms)))
            if not text.strip():
                B("R-ROLE-EMPTY", "%s: empty text" % src)
        if valid == 0:
            B("R-ROLE-EMPTY", "%s keeps no bullet" % rid)
        elif valid > MAX_BULLETS_PER_ROLE:
            W("R-BULLETS-MANY", "%s has %d bullets" % (rid, valid))

    base_projects = {p["project_id"] for p in base.get("projects", [])}
    for tp in tailor.get("projects", []):
        if tp.get("project_id") not in base_projects:
            B("R-PROJECT-UNKNOWN", str(tp.get("project_id")))

    base_skills = {s.strip().lower() for s in base.get("skills", [])}
    for s in tailor.get("skills_order", []):
        k = s.strip().lower()
        if k not in base_skills and k not in corpus:
            B("R-NEW-SKILL", "skills_order: %s" % s)
        elif k not in base_skills:
            B("R-NEW-SKILL", "skills_order: %s is not in the base skills line" % s)

    # summary
    summary = tailor.get("summary")
    if summary:
        text = summary.get("text") or ""
        fids = summary.get("fact_ids") or []
        if not fids:
            B("R-SUMMARY-FACT", "the summary cites no profile fact")
        unknown = [f for f in fids if f not in facts]
        if unknown:
            B("R-SUMMARY-FACT", "unknown fact ids %s" % unknown)
        allowed = set()
        for f in fids:
            allowed |= M.numbers(facts.get(f))
        missing = M.numbers_subset(M.numbers(text), allowed)
        if missing:
            B("R-SUMMARY-NUMBER", ", ".join(sorted(n + u for n, u in missing)))
        new_terms = sorted(t for t in _terms(text) if t not in corpus)
        if new_terms:
            B("R-NEW-SKILL", "summary: %s" % ", ".join(new_terms))
        if len(text) > MAX_SUMMARY_CHARS or "\n" in text.strip():
            B("R-SUMMARY-LONG", "%d characters" % len(text))

    # identity with the base (defence in depth: apply() copies these fields)
    rendered_roles = {r["role_id"]: r for r in rendered.get("experience", [])}
    for rid, br in base_roles.items():
        rr = rendered_roles.get(rid)
        if rr is None:
            B("R-TITLE-CHANGED", "%s is missing from the rendered resume" % rid)
            continue
        if rr.get("title") != br.get("title"):
            B("R-TITLE-CHANGED", rid)
        if rr.get("employer") != br.get("employer") or rr.get("location") != br.get("location"):
            B("R-EMPLOYER-CHANGED", rid)
        if rr.get("dates") != br.get("dates"):
            B("R-DATES-CHANGED", rid)
    if set(rendered_roles) - set(base_roles):
        B("R-ROLE-UNKNOWN", "rendered roles not in the base: %s" % sorted(set(rendered_roles) - set(base_roles)))
    if rendered.get("contact") != base.get("contact"):
        B("R-CONTACT-CHANGED", "the contact block differs from the base")
    if rendered.get("education") != base.get("education") or rendered.get("certifications") != \
            base.get("certifications"):
        B("R-EDUCATION-CHANGED", "education or certifications differ from the base")
    base_proj = {p["project_id"]: p for p in base.get("projects", [])}
    for p in rendered.get("projects", []):
        bp = base_proj.get(p.get("project_id"))
        if bp is None or p != bp:
            B("R-PROJECT-UNKNOWN", "%s differs from the base" % p.get("project_id"))

    # characters on everything that will be printed
    for rule, detail in M.char_findings(M.all_text(rendered)):
        if rule == "NON-ASCII":
            W("R-NON-ASCII", detail)
        else:
            B("R-" + rule, detail)

    if pages is not None and pages > max_pages:
        B("R-PAGES", "%d pages, at most %d" % (pages, max_pages))

    n_bullets = sum(len(r.get("bullets", [])) for r in rendered.get("experience", []))
    return {"pass": not blocks and len(warns) <= 2, "blocks": blocks, "warns": warns,
            "metrics": {"mode": mode, "bullets": n_bullets, "pages": pages,
                        "roles": len(rendered.get("experience", [])), "skills": len(rendered.get("skills", []))}}


def lint_base(base: dict, *, pages: int | None = None, max_pages: int = 2) -> dict:
    """Checks for the base variant (tailoring mode off): characters and page count."""
    blocks, warns = [], []
    for rule, detail in M.char_findings(M.all_text(base)):
        (warns if rule == "NON-ASCII" else blocks).append(["R-" + rule, detail])
    if pages is not None and pages > max_pages:
        blocks.append(["R-PAGES", "%d pages, at most %d" % (pages, max_pages)])
    return {"pass": not blocks and len(warns) <= 2, "blocks": blocks, "warns": warns,
            "metrics": {"mode": "base", "pages": pages}}


def changes(base: dict, rendered: dict) -> list[str]:
    """Plain lines describing what a variant changes against the base (for the approval preview in human
    mode, which shows only the changes)."""
    out: list[str] = []
    if (rendered.get("summary") or None) != (base.get("summary") or None):
        out.append("Summary: %s" % (rendered.get("summary") or "(none)"))
    base_order = [s for s in M.sections(M.to_render_model(base))]
    new_order = [s for s in M.sections(rendered)]
    if new_order != base_order:
        out.append("Section order: %s" % ", ".join(M.SECTION_TITLES[s] for s in new_order))
    base_roles = {r["role_id"]: r for r in base.get("experience", [])}
    for r in rendered.get("experience", []):
        br = base_roles.get(r["role_id"])
        if br is None:
            continue
        kept = [b["id"] for b in r.get("bullets", [])]
        base_ids = [b["id"] for b in br.get("bullets", [])]
        base_text = {b["id"]: b["text"] for b in br.get("bullets", [])}
        dropped = [i for i in base_ids if i not in kept]
        reworded = [b["id"] for b in r.get("bullets", []) if b["text"] != base_text.get(b["id"])]
        if kept != base_ids or reworded:
            line = "%s (%s): bullets %s" % (r["role_id"], br.get("employer"), ", ".join(kept))
            if dropped:
                line += "; left out %s" % ", ".join(dropped)
            out.append(line)
            for b in r.get("bullets", []):
                if b["id"] in reworded:
                    out.append("  %s reworded: %s" % (b["id"], b["text"]))
    base_proj = [p["project_id"] for p in base.get("projects", [])]
    new_proj = [p["project_id"] for p in rendered.get("projects", [])]
    if new_proj != base_proj:
        out.append("Projects: %s" % (", ".join(new_proj) or "(none)"))
    if list(rendered.get("skills", [])) != list(base.get("skills", [])):
        out.append("Skills order: %s" % ", ".join(rendered.get("skills", [])))
    return out

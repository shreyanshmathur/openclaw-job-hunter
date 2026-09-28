"""Resume variants: plan, build, base build, staging for upload, and the renderers (design 6.4, 12.5, 12.10).

    plan(conn, job_uid, cycle_id=None) -> {base_path, jd_path, facts_path, mode, constraints, base_variant_uid}
    build(conn, job_uid, tailor, cycle_id=None, caller=None) -> {variant_uid, pdf_path, lint, draft_uid, ...}
    build_base(conn, cycle_id=None, caller=None) -> the base variant (mode 'base') and its resume draft
    stage(conn, variant_uid, token) -> {upload_path, filename, sha256}
    unstage(conn, token) -> None
    render_pdf(model, out_path) -> str; render_docx(model, out_path) -> str; render_txt(model) -> str

Files: private/resume/base.json (the confirmed base), private/resume/base_review.json (the sha256 the
person confirmed with `resume base-review`), private/resume/variants/<variant_uid>/<First>_<Last>_Resume.{pdf,docx,txt}.
Functions that take `conn` and write run inside the caller's db.tx(conn). A variant becomes a `resume` draft
through drafts.create_draft (U3), so it passes the same QC gate and approval as every outbound item.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat

from .. import paths
from ..canon import new_uid, now, seconds_between, sha256_file
from ..errors import Denied
from ..events import log_event
from . import docx as _docx
from . import model as M
from . import pdf as _pdf
from . import tailor as T

DEFAULT_CONFIG = {
    "tailoring": "light",
    "formats": ["pdf", "docx"],
    "pdf_engine": "stdlib",
    "page_size": "A4",
    "max_pages": 2,
    "filename": "{first}_{last}_Resume",
    "date_format": "MMM YYYY",
    "range_word": "to",
    "present_word": "Present",
}
HARD_MAX_PAGES = 2
DEFAULT_UPLOAD_ROOT = "/tmp/openclaw/uploads"
OPEN_TOKEN_STATUSES = ("reserved", "armed")
APPROVED_DRAFT_STATUSES = ("approved", "sent")
QC_OK_STATUSES = ("qc_passed", "approved", "sent")       # a resume draft is a package part (drafts.PART_KINDS)
DEAD_DRAFT_STATUSES = ("skipped_by_human", "dropped_qc", "expired", "superseded")


# ---------------------------------------------------------------- paths
def resume_dir() -> str:
    return os.path.join(paths.private_dir(), "resume")


def base_path() -> str:
    return os.path.join(resume_dir(), "base.json")


def review_path() -> str:
    return os.path.join(resume_dir(), "base_review.json")


def variants_dir() -> str:
    return os.path.join(resume_dir(), "variants")


def upload_root() -> str:
    """OpenClaw's browser upload root, resolved (home.json `upload_root`, else /tmp/openclaw/uploads;
    a test home uses <root>/uploads)."""
    try:
        h = paths.home()
    except Denied:
        h = {}
    root = h.get("upload_root")
    if not root:
        root = os.path.join(paths.root(), "uploads") if paths.is_test_home() else DEFAULT_UPLOAD_ROOT
    return os.path.realpath(str(root))


def _mkdir_private(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)


def _write_private(path: str, text: str) -> None:
    _mkdir_private(os.path.dirname(path))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- config
def _file_config() -> dict:
    p = os.path.join(paths.private_dir(), "config.json")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def full_config() -> dict:
    """Effective config from jobhunter.config (U1) when available, else private/config.json as written."""
    try:
        from .. import config as C          # U1
        load = C.load
    except (ImportError, AttributeError):
        return _file_config()
    try:
        cfg = load()
    except NotImplementedError:
        return _file_config()
    return cfg if isinstance(cfg, dict) else {}


def resume_config(cfg: dict | None = None) -> dict:
    """The `resume` section with defaults and the renderer's own safety clamps (no dash can come from
    range_word or present_word; max_pages at most 2)."""
    cfg = full_config() if cfg is None else cfg
    sec = cfg.get("resume") if isinstance(cfg.get("resume"), dict) else {}
    out = dict(DEFAULT_CONFIG)
    for k in DEFAULT_CONFIG:
        if k in sec and sec[k] is not None:
            out[k] = sec[k]
    if out["tailoring"] not in ("off", "light", "full"):
        out["tailoring"] = "light"
    try:
        out["max_pages"] = max(1, min(int(out["max_pages"]), HARD_MAX_PAGES))
    except (TypeError, ValueError):
        out["max_pages"] = HARD_MAX_PAGES
    if str(out["page_size"]).upper() not in _pdf.PAGE_SIZES:
        out["page_size"] = "A4"
    if not re.match(r"^[A-Za-z]{1,12}$", str(out["range_word"])):
        out["range_word"] = "to"
    if not re.match(r"^[A-Za-z][A-Za-z ]{0,19}$", str(out["present_word"])):
        out["present_word"] = "Present"
    if out["date_format"] not in ("MMM YYYY", "MMMM YYYY", "MM/YYYY", "YYYY"):
        out["date_format"] = "MMM YYYY"
    fmts = out["formats"] if isinstance(out["formats"], list) else ["pdf"]
    out["formats"] = ["pdf"] + [f for f in fmts if f == "docx"]
    return out


def render_opts(rc: dict | None = None) -> dict:
    rc = rc or resume_config()
    return {k: rc[k] for k in ("page_size", "date_format", "range_word", "present_word")}


def _alnum(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "")


def file_stem(contact: dict, rc: dict | None = None, cfg: dict | None = None) -> str:
    """'Alex_Rivera_Resume' from resume.filename. The names come from config owner.first_name and
    owner.last_name (the same source drafts.resume_filename uses for the attachment name the form package
    is checked against), else from the base resume's contact block."""
    cfg = full_config() if cfg is None else cfg
    rc = rc or resume_config(cfg)
    owner = cfg.get("owner") if isinstance(cfg.get("owner"), dict) else {}
    first, last = _alnum(owner.get("first_name") or ""), _alnum(owner.get("last_name") or "")
    if not (first and last):
        first, last = _alnum(contact.get("first_name") or ""), _alnum(contact.get("last_name") or "")
    tmpl = str(rc.get("filename") or DEFAULT_CONFIG["filename"])
    stem = tmpl.replace("{first}", first).replace("{last}", last)
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    stem = re.sub(r"[^A-Za-z0-9_.]", "_", stem).strip("_.")
    stem = re.sub(r"_+", "_", stem)
    return stem or "Resume"


# ---------------------------------------------------------------- base
def load_base(required: bool = True) -> dict | None:
    try:
        with open(base_path(), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        if required:
            raise Denied("E_PRECONDITION", "there is no base resume yet; run the onboarding inference first",
                         data={"path": base_path()})
        return None
    except (OSError, ValueError) as exc:
        raise Denied("E_CONFIG_INVALID", "private/resume/base.json is unreadable: %s" % exc)
    return M.validate(raw)


def save_base(base: dict) -> str:
    """Validate and write private/resume/base.json; returns its sha256. A new base needs a new review."""
    b = M.validate(base)
    _write_private(base_path(), json.dumps(b, indent=1, sort_keys=True, ensure_ascii=True) + "\n")
    return M.base_sha256(b)


def review_state(base: dict | None = None) -> dict:
    base = load_base(required=False) if base is None else base
    try:
        with open(review_path(), "r", encoding="utf-8") as fh:
            rv = json.load(fh)
    except (OSError, ValueError):
        rv = {}
    sha = M.base_sha256(base) if base else None
    return {"base_sha256": sha, "reviewed_sha256": rv.get("sha256"), "reviewed_at": rv.get("reviewed_at"),
            "reviewed": bool(sha and rv.get("sha256") == sha)}


def mark_reviewed(base: dict) -> dict:
    sha = M.base_sha256(base)
    rec = {"sha256": sha, "reviewed_at": now()}
    _write_private(review_path(), json.dumps(rec, sort_keys=True) + "\n")
    return rec


def review_text(base: dict | None = None) -> str:
    base = load_base() if base is None else base
    return M.review_text(base, render_opts())


# ---------------------------------------------------------------- renderers
def render_pdf(model: dict, out_path: str, opts: dict | None = None) -> str:
    _pdf.render(model, out_path, opts or render_opts())
    return out_path


def render_docx(model: dict, out_path: str, opts: dict | None = None) -> str:
    _docx.render(model, out_path, opts or render_opts())
    return out_path


def render_txt(model: dict, opts: dict | None = None) -> str:
    return M.render_txt(model, opts or render_opts())


def _page_count(model: dict, opts: dict) -> int | None:
    try:
        return _pdf.count_pages(model, opts)
    except ValueError:
        return None


# ---------------------------------------------------------------- profile inputs (U4 profile module)
def _profile_inputs() -> tuple[dict, str | None]:
    from .. import profile as P
    return P.facts(), P.extra_info_text()


# ---------------------------------------------------------------- drafts (U3)
def _drafts():
    from .. import drafts        # U3; tests replace this function with a fake module
    return drafts


def _create_draft(conn, draft: dict, cycle_id: str | None, caller) -> dict:
    try:
        fn = _drafts().create_draft
    except (ImportError, AttributeError):
        raise Denied("E_PRECONDITION", "the drafts module (QC gate) is not installed")
    try:
        res = fn(conn, draft, cycle_id, caller)
    except NotImplementedError:
        raise Denied("E_PRECONDITION", "drafts.create_draft is not implemented yet")
    if not isinstance(res, dict) or not res.get("draft_uid"):
        raise Denied("E_INTERNAL", "drafts.create_draft returned no draft_uid")
    return res


def _job(conn, job_uid: str):
    row = conn.execute("SELECT id, job_uid, title, company_name_raw, status FROM jobs WHERE job_uid = ?",
                       (job_uid,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no job %s" % job_uid)
    return row


def _jd_text(conn, job_id: int) -> str | None:
    row = conn.execute("SELECT jd_text FROM job_texts WHERE job_id = ?", (job_id,)).fetchone()
    return row[0] if row else None


def _latest_base_variant(conn, base_sha: str):
    return conn.execute(
        "SELECT v.*, d.status AS draft_status, d.draft_uid AS draft_uid FROM resume_variants v "
        "LEFT JOIN drafts d ON d.id = v.draft_id WHERE v.job_id IS NULL AND v.mode = 'base' AND v.base_sha256 = ? "
        "ORDER BY v.id DESC LIMIT 1", (base_sha,)).fetchone()


# ---------------------------------------------------------------- plan
def plan(conn, job_uid: str, cycle_id: str | None = None) -> dict:
    """Copy the base resume, the JD and the profile facts into the applier's work folder and describe the
    rules the tailor file must follow."""
    job = _job(conn, job_uid)
    base = load_base()
    rc = resume_config()
    facts, _extra = _profile_inputs()
    sub = cycle_id if cycle_id and re.match(r"^C[0-9]{8}T[0-9]{6}Z[A-Z2-7]{4}$", cycle_id) else "resume"
    work = os.path.join(paths.ws_dir("applier"), "work", sub, "resume-" + job["job_uid"])
    _mkdir_private(work)
    bp = os.path.join(work, "base.json")
    shown = {k: v for k, v in base.items() if k != "dash_conversions"}
    _write_private(bp, json.dumps(shown, indent=1, sort_keys=True, ensure_ascii=True) + "\n")
    jd = _jd_text(conn, job["id"])
    jp = None
    if jd:
        jp = os.path.join(work, "jd.txt")
        _write_private(jp, jd)
    fp = os.path.join(work, "facts.json")
    _write_private(fp, json.dumps(facts, indent=1, sort_keys=True, ensure_ascii=True) + "\n")
    mode = rc["tailoring"]
    base_variant_uid = None
    if mode == "off":
        row = _latest_base_variant(conn, M.base_sha256(base))
        if row is not None and row["draft_status"] in QC_OK_STATUSES:
            base_variant_uid = row["variant_uid"]
    constraints = {
        "mode": mode,
        "modes_allowed": [m for m in T.MODES if T.MODE_RANK[m] <= T.MODE_RANK[mode]],
        "rephrase_allowed": mode == "full",
        "bullets_per_role": [3, T.MAX_BULLETS_PER_ROLE],
        "max_pages": rc["max_pages"],
        "sections": [s for s in M.SECTIONS if s in M.sections(M.to_render_model(base)) or s == "summary"],
        "role_bullets": {r["role_id"]: [b["id"] for b in r["bullets"]] for r in base["experience"]},
        "project_ids": [p["project_id"] for p in base["projects"]],
        "skills": list(base["skills"]),
        "fact_ids": sorted(facts, key=lambda k: int(k[1:]) if k[1:].isdigit() else 0),
        "summary_max_chars": T.MAX_SUMMARY_CHARS,
        "rules": [
            "every bullet cites its base id in 'from'; text null keeps the base words",
            "no new employers, titles, dates, numbers, skills or tools",
            "numbers in a bullet must appear in its base bullet",
            "skills_order may only reorder the base skills",
            "the summary cites profile fact ids and uses only their numbers",
            "no dashes of any kind and no spaced hyphens",
        ],
    }
    return {"job_uid": job["job_uid"], "base_path": bp, "jd_path": jp, "facts_path": fp, "mode": mode,
            "constraints": constraints, "base_variant_uid": base_variant_uid}


# ---------------------------------------------------------------- build
def _render_all(model: dict, vuid: str, contact: dict, rc: dict, opts: dict, cfg: dict) -> dict:
    d = os.path.join(variants_dir(), vuid)
    _mkdir_private(d)
    stem = file_stem(contact, rc, cfg)
    pdf_path = os.path.join(d, stem + ".pdf")
    info = _pdf.render(model, pdf_path, opts)
    os.chmod(pdf_path, 0o600)
    docx_path = None
    if "docx" in rc["formats"]:
        docx_path = os.path.join(d, stem + ".docx")
        _docx.render(model, docx_path, opts)
        os.chmod(docx_path, 0o600)
    txt = M.render_txt(model, opts)
    txt_path = os.path.join(d, stem + ".txt")
    _write_private(txt_path, txt)
    return {"pdf_path": pdf_path, "docx_path": docx_path, "txt_path": txt_path, "txt": txt, "pages": info["pages"],
            "pdf_sha256": sha256_file(pdf_path), "filename": stem + ".pdf"}


def lint_model(model: dict, summary: dict | None, tailored: bool) -> dict:
    """The model shape the QC linter's R-* rules read (qc/lint.py _lint_resume): roles and projects with
    `id`, bullets with `id` (base) or `from` (tailored), education with `id`."""
    key = "from" if tailored else "id"

    def bullets(items):
        return [{key: b["id"], "text": b["text"]} for b in items]

    return {
        "contact": model.get("contact"),
        "summary": summary,
        "roles": [{"id": r["role_id"], "employer": r["employer"], "title": r["title"], "location": r.get("location"),
                   "dates": r.get("dates"), "bullets": bullets(r.get("bullets", []))}
                  for r in model.get("experience", [])],
        "projects": [{"id": p["project_id"], "name": p["name"], "bullets": bullets(p.get("bullets", []))}
                     for p in model.get("projects", [])],
        "skills": list(model.get("skills", [])),
        "education": [dict(e, id=e["edu_id"]) for e in model.get("education", [])],
    }


def _draft_payload(job_uid: str | None, txt: str, vuid: str, mode: str, sha: str, tailor: dict | None,
                   base: dict, rendered: dict, pages: int, max_pages: int) -> dict:
    links = [ln["url"] for ln in base["contact"].get("links") or []][:5]
    summary = None
    if tailor and tailor.get("summary") and (tailor["summary"].get("text") or "").strip():
        summary = {"text": tailor["summary"]["text"].strip(), "fact_ids": list(tailor["summary"].get("fact_ids") or [])}
    return {
        "kind": "resume", "channel": "resume", "job_uid": job_uid, "contact_uid": None, "thread_key": None,
        "subject": None, "body": txt, "is_reply": False, "field_char_limit": None, "hook": None, "claims": [],
        "links": links,
        "payload": {"resume_variant_uid": vuid, "mode": mode, "base_sha256": sha, "tailor": tailor,
                    "base": lint_model(M.to_render_model(base), None, False),
                    "tailored": lint_model(rendered, summary, True),
                    "page_count": pages, "max_pages": max_pages, "allowed_skills": list(base.get("skills", [])),
                    "names": M.names(rendered), "changes": T.changes(base, rendered)},
    }


def _discard_files(vuid: str) -> None:
    """Remove the rendered files of a variant whose database rows were not written."""
    d = os.path.join(variants_dir(), vuid)
    if os.path.isdir(d) and not os.path.islink(d):
        shutil.rmtree(d, ignore_errors=True)


def _insert_variant(conn, vuid: str, job_id: int | None, mode: str, sha: str, tailor: dict | None,
                    files: dict) -> int:
    cur = conn.execute(
        "INSERT INTO resume_variants (variant_uid, job_id, mode, base_sha256, tailor_json, pdf_path, docx_path, "
        "txt_path, pdf_sha256, draft_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (vuid, job_id, mode, sha, M.canonical_json(tailor) if tailor is not None else None, files["pdf_path"],
         files["docx_path"], files["txt_path"], files["pdf_sha256"], now()))
    return cur.lastrowid


def _attach_draft(conn, variant_id: int, draft_uid: str) -> None:
    row = conn.execute("SELECT id FROM drafts WHERE draft_uid = ?", (draft_uid,)).fetchone()
    if row is None:
        raise Denied("E_INTERNAL", "draft %s was not stored" % draft_uid)
    conn.execute("UPDATE resume_variants SET draft_id = ? WHERE id = ?", (row[0], variant_id))


def build(conn, job_uid: str, tailor: dict, cycle_id: str | None = None, caller=None) -> dict:
    """Validate a tailor file (12.5), run the R-* checks, render PDF, DOCX and TXT, store the variant and
    create its `resume` draft. R-* blocks refuse with E_QC_LINT_FAILED before anything is written."""
    cfg = full_config()
    rc = resume_config(cfg)
    if rc["tailoring"] == "off":
        raise Denied("E_PRECONDITION", "resume tailoring is off; use the approved base variant (resume plan says "
                     "which)")
    job = _job(conn, job_uid)
    base = load_base()
    t = T.validate_schema(tailor, base)
    if t["job_uid"] != job_uid:
        raise Denied("E_VALIDATION", "the tailor file is for job %s, not %s" % (t["job_uid"], job_uid))
    if T.MODE_RANK[t["mode"]] > T.MODE_RANK[rc["tailoring"]]:
        raise Denied("E_VALIDATION", "mode %s is not enabled (resume.tailoring is %s)" % (t["mode"], rc["tailoring"]))
    facts, extra = _profile_inputs()
    opts = render_opts(rc)
    rendered = T.apply(base, t)
    pages = _page_count(rendered, opts)
    lint = T.lint(base, t, rendered, facts=facts, extra_text=extra, pages=pages, max_pages=rc["max_pages"])
    if not lint["pass"]:
        raise Denied("E_QC_LINT_FAILED", "the tailored resume breaks the resume rules; fix the tailor file",
                     data={"lint": lint})
    sha = M.base_sha256(base)
    vuid = new_uid("V")
    files = _render_all(rendered, vuid, base["contact"], rc, opts, cfg)
    try:
        variant_id = _insert_variant(conn, vuid, job["id"], t["mode"], sha, t, files)
        res = _create_draft(conn, _draft_payload(job_uid, files["txt"], vuid, t["mode"], sha, t, base, rendered,
                                                 files["pages"], rc["max_pages"]), cycle_id, caller)
        _attach_draft(conn, variant_id, res["draft_uid"])
    except BaseException:
        _discard_files(vuid)
        raise
    log_event(conn, "resume_variant_built", variant_uid=vuid, job_uid=job_uid, mode=t["mode"],
              draft_uid=res["draft_uid"], pages=files["pages"])
    return {"variant_uid": vuid, "job_uid": job_uid, "mode": t["mode"], "pdf_path": files["pdf_path"],
            "docx_path": files["docx_path"], "txt_path": files["txt_path"], "pdf_sha256": files["pdf_sha256"],
            "filename": files["filename"], "pages": files["pages"], "lint": lint, "draft_uid": res["draft_uid"],
            "draft_status": res.get("status"), "draft_lint": res.get("lint")}


def build_base(conn, cycle_id: str | None = None, caller=None) -> dict:
    """The base variant (mode 'base', no job): rendered from base.json, QC'd once per base version."""
    base = load_base()
    rv = review_state(base)
    if not rv["reviewed"]:
        raise Denied("E_PRECONDITION", "confirm the base resume first with ./jobhunter resume base-review",
                     data=rv)
    sha = rv["base_sha256"]
    row = _latest_base_variant(conn, sha)
    if row is not None and row["draft_status"] not in DEAD_DRAFT_STATUSES and os.path.isfile(row["pdf_path"]):
        return {"variant_uid": row["variant_uid"], "mode": "base", "pdf_path": row["pdf_path"],
                "docx_path": row["docx_path"], "txt_path": row["txt_path"], "pdf_sha256": row["pdf_sha256"],
                "draft_uid": row["draft_uid"], "draft_status": row["draft_status"], "reused": True, "lint": None}
    cfg = full_config()
    rc = resume_config(cfg)
    opts = render_opts(rc)
    rendered = M.to_render_model(base)
    pages = _page_count(rendered, opts)
    lint = T.lint_base(base, pages=pages, max_pages=rc["max_pages"])
    if not lint["pass"]:
        raise Denied("E_QC_LINT_FAILED", "the base resume breaks the resume rules; fix base.json", data={"lint": lint})
    vuid = new_uid("V")
    files = _render_all(rendered, vuid, base["contact"], rc, opts, cfg)
    try:
        variant_id = _insert_variant(conn, vuid, None, "base", sha, None, files)
        res = _create_draft(conn, _draft_payload(None, files["txt"], vuid, "base", sha, None, base, rendered,
                                                 files["pages"], rc["max_pages"]), cycle_id, caller)
        _attach_draft(conn, variant_id, res["draft_uid"])
    except BaseException:
        _discard_files(vuid)
        raise
    log_event(conn, "resume_variant_built", variant_uid=vuid, job_uid=None, mode="base", draft_uid=res["draft_uid"],
              pages=files["pages"])
    return {"variant_uid": vuid, "mode": "base", "pdf_path": files["pdf_path"], "docx_path": files["docx_path"],
            "txt_path": files["txt_path"], "pdf_sha256": files["pdf_sha256"], "filename": files["filename"],
            "pages": files["pages"], "lint": lint, "draft_uid": res["draft_uid"], "draft_status": res.get("status"),
            "draft_lint": res.get("lint"), "reused": False}


def variant(conn, variant_uid: str):
    row = conn.execute(
        "SELECT v.*, d.status AS draft_status, d.draft_uid AS draft_uid FROM resume_variants v "
        "LEFT JOIN drafts d ON d.id = v.draft_id WHERE v.variant_uid = ?", (variant_uid,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no resume variant %s" % variant_uid)
    return row


# ---------------------------------------------------------------- staging
def _copy_no_follow(src: str, dest: str) -> None:
    """Copy bytes (never a link) to dest through a new temp file in the same folder."""
    folder = os.path.dirname(dest)
    tmp = os.path.join(folder, ".stage-%s.tmp" % new_uid("S", 10))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            while True:
                chunk = inp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def stage(conn, variant_uid: str, token: str) -> dict:
    """Copy an approved variant's PDF to <upload_root>/<First>_<Last>_Resume.pdf for the open application
    token and record it in staged_files (the guard allows `browser upload` of exactly this path)."""
    v = variant(conn, variant_uid)
    act = conn.execute("SELECT id, kind, status, job_id, draft_id FROM actions WHERE token = ?", (token,)).fetchone()
    if act is None:
        raise Denied("E_NOT_FOUND", "no action with token %s" % token)
    if act["kind"] != "application":
        raise Denied("E_PRECONDITION", "resumes are staged only for application tokens", data={"kind": act["kind"]})
    if act["status"] not in OPEN_TOKEN_STATUSES:
        raise Denied("E_PRECONDITION", "token %s is not open (status %s)" % (token, act["status"]))
    if v["job_id"] is not None and act["job_id"] != v["job_id"]:
        raise Denied("E_PRECONDITION", "variant %s was tailored for another job" % variant_uid)
    if v["draft_status"] not in QC_OK_STATUSES:
        raise Denied("E_PRECONDITION", "variant %s has not passed QC (draft status %s)"
                     % (variant_uid, v["draft_status"]), data={"draft_uid": v["draft_uid"]})
    pkg = conn.execute("SELECT kind, status, payload_json FROM drafts WHERE id = ?", (act["draft_id"],)).fetchone() \
        if act["draft_id"] is not None else None
    if pkg is None or pkg["status"] not in APPROVED_DRAFT_STATUSES:
        raise Denied("E_PRECONDITION", "the token's application package is not approved")
    try:
        pj = json.loads(pkg["payload_json"] or "{}")
    except ValueError:
        pj = {}
    named = ((pj.get("payload") or {}).get("resume_variant_uid") or (pj.get("attachment") or {}).get("variant_uid")
             or pj.get("resume_variant_uid"))
    if named != variant_uid:
        raise Denied("E_PRECONDITION", "the approved package attaches resume variant %s, not %s" % (named, variant_uid))
    src = v["pdf_path"]
    if not os.path.isfile(src) or os.path.islink(src):
        raise Denied("E_NOT_FOUND", "the variant's PDF is missing", data={"path": src})
    if sha256_file(src) != v["pdf_sha256"]:
        raise Denied("E_QC_HASH_MISMATCH", "the variant's PDF changed after it was built")
    base = load_base()
    cfg = full_config()
    filename = file_stem(base["contact"], resume_config(cfg), cfg) + ".pdf"
    root = upload_root()
    os.makedirs(root, mode=0o700, exist_ok=True)
    dest = os.path.join(root, filename)
    prev = conn.execute("SELECT * FROM staged_files WHERE token = ?", (token,)).fetchone()
    if prev is not None and prev["removed_at"] is None:
        if prev["variant_id"] != v["id"]:
            raise Denied("E_PRECONDITION", "token %s already has another resume staged; unstage it first" % token)
        if os.path.isfile(prev["path"]) and not os.path.islink(prev["path"]) and \
                sha256_file(prev["path"]) == v["pdf_sha256"]:
            return {"upload_path": prev["path"], "filename": os.path.basename(prev["path"]),
                    "sha256": v["pdf_sha256"], "variant_uid": variant_uid, "reused": True}
    stamp = now()
    if prev is None:
        conn.execute("INSERT INTO staged_files (token, variant_id, path, sha256, staged_at, removed_at) "
                     "VALUES (?, ?, ?, ?, ?, NULL)", (token, v["id"], dest, v["pdf_sha256"], stamp))
    else:
        conn.execute("UPDATE staged_files SET variant_id = ?, path = ?, sha256 = ?, staged_at = ?, removed_at = NULL "
                     "WHERE token = ?", (v["id"], dest, v["pdf_sha256"], stamp, token))
    _copy_no_follow(src, dest)
    st = os.lstat(dest)
    if not stat.S_ISREG(st.st_mode) or sha256_file(dest) != v["pdf_sha256"]:
        raise Denied("E_INTERNAL", "the staged copy does not match the approved PDF")
    log_event(conn, "resume_staged", token=token, variant_uid=variant_uid, path=dest)
    return {"upload_path": dest, "filename": filename, "sha256": v["pdf_sha256"], "variant_uid": variant_uid,
            "reused": False}


def unstage(conn, token: str) -> None:
    """Delete the staged copy of a token (idempotent) and mark the row removed. Only a regular file with the
    recorded sha256, or anything at that path inside the current upload root, is deleted."""
    row = conn.execute("SELECT * FROM staged_files WHERE token = ?", (token,)).fetchone()
    if row is None or row["removed_at"] is not None:
        return None
    path = row["path"]
    root = upload_root()
    parent = os.path.realpath(os.path.dirname(path))
    in_root = parent == root or parent.startswith(root.rstrip(os.sep) + os.sep)
    try:
        if os.path.lexists(path):
            if in_root:
                os.unlink(path)
            elif os.path.isfile(path) and not os.path.islink(path) and sha256_file(path) == row["sha256"]:
                os.unlink(path)
    except FileNotFoundError:
        pass
    conn.execute("UPDATE staged_files SET removed_at = ? WHERE token = ?", (now(), token))
    log_event(conn, "resume_unstaged", token=token)
    return None


def unstage_open(conn, older_than_s: int | None = None, agent_id: str | None = None) -> int:
    """Housekeeping and `cycle end`: unstage every open staged file (older than `older_than_s`, or of the
    given agent's tokens). Returns how many were removed."""
    rows = conn.execute("SELECT s.token, s.staged_at, a.agent_id FROM staged_files s LEFT JOIN actions a "
                        "ON a.token = s.token WHERE s.removed_at IS NULL").fetchall()
    n = 0
    stamp = now()
    for r in rows:
        if agent_id is not None and r["agent_id"] != agent_id:
            continue
        if older_than_s is not None and seconds_between(r["staged_at"], stamp) < older_than_s:
            continue
        unstage(conn, r["token"])
        n += 1
    return n

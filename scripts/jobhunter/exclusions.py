"""Private exclusions (design section 9, M9).

- `private/exclusions.csv` (header `type,value,reason`, `#` comments) is imported by `import_csv`, which
  only adds or reactivates rows (source private_csv); rows missing from the file are counted, never
  deactivated, unless the human runs `exclusions import --deactivate` (PIN) and the file still holds at
  least `exclusions.min_keep_ratio` of the active private_csv rows.
- Values are normalised with the keys.py functions; `match()` compares key sets, so every URL, address or
  name variant of an excluded target is caught. `forget` stores a hashed suppression key (`h:<sha256>`).
- `apply()` pushes an exclusion's effect onto existing rows (company do_not_contact, contact
  do_not_contact, job excluded). Runs inside the caller's transaction.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import shutil

from . import companies, jobstate, keys, paths, people
from .canon import now
from .errors import Denied
from .events import log_event

TYPES = ("company", "email", "domain", "linkedin", "job_url")
SOURCES = ("private_csv", "reply_optout", "complaint", "bounce", "human", "forget")
EXCLUDABLE_JOB_STATUSES = ("new", "eval_queued", "evaluating", "borderline", "rejected", "prefilter_rejected",
                           "eligible", "apply_queued", "awaiting_approval", "needs_human")


def csv_file() -> str:
    return os.path.join(paths.private_dir(), "exclusions.csv")


def hashed(key: str) -> str:
    return "h:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def value_key(etype: str, value: str) -> str:
    """Normalised key stored in exclusions.value_key (UNIQUE with the type)."""
    if etype not in TYPES:
        raise Denied("E_VALIDATION", "exclusion type must be one of %s" % ", ".join(TYPES))
    v = (value or "").strip()
    if not v:
        raise Denied("E_VALIDATION", "empty exclusion value")
    if etype == "company":
        n = keys.norm_name(v)
        if not n:
            raise Denied("E_VALIDATION", "company name %r has no usable key" % v)
        return "id:" + n
    if etype == "domain":
        reg = keys.registrable_domain(v)
        if not reg:
            raise Denied("E_VALIDATION", "not a domain: %r" % v)
        return "dom:" + reg
    if etype == "email":
        return [k for k, kind in keys.email_keys(v) if kind == "email_norm"][0]
    if etype == "linkedin":
        return keys.linkedin_keys(v)[0][0]
    return keys.job_key(v, {}, None)[0]


def _exclusion_keys(etype: str, raw: str, vkey: str) -> set:
    """Every key an exclusion row stands for (company: name and loose; domain: dom and label)."""
    if vkey.startswith("h:"):
        return {vkey}
    out = {vkey}
    try:
        if etype == "company":
            out |= {k for k, kind in keys.company_keys(name=raw) if kind in ("name", "loose")}
        elif etype == "domain":
            reg = vkey[4:]
            out |= {k for k, kind in keys.company_keys(domain=reg) if kind in ("dom", "label")}
            out.add("dom:" + reg)
        elif etype == "email":
            out |= {k for k, _ in keys.email_keys(raw)}
        elif etype == "job_url":
            ck, aliases = keys.job_key(raw, {}, None)
            out |= {ck} | set(a for a in aliases if not a.startswith("url:")) | {ck}
    except Denied:
        pass
    return out


def _candidate_keys(conn, t: dict) -> dict:
    c = {"company": set(), "person": set(), "job": set(), "domains": set()}
    if t.get("company_id"):
        ids = companies.group(conn, t["company_id"])
        c["company"] |= {r[0] for r in conn.execute(
            "SELECT alias_key FROM company_aliases WHERE company_id IN (%s)" % ",".join("?" * len(ids)), ids)}
        for (name, dom) in conn.execute("SELECT display_name, domain FROM companies WHERE id IN (%s)"
                                        % ",".join("?" * len(ids)), ids).fetchall():
            c["company"] |= {k for k, _ in keys.company_keys(name=name, domain=dom)}
    if t.get("company_name"):
        c["company"] |= {k for k, _ in keys.company_keys(name=t["company_name"])}
    for dom_src in (t.get("domain"), t.get("email")):
        if dom_src:
            reg = keys.registrable_domain(dom_src)
            if reg:
                c["domains"].add("dom:" + reg)
                c["company"] |= {k for k, _ in keys.company_keys(domain=dom_src)}
    if t.get("contact_id"):
        ids = people.group(conn, t["contact_id"])
        c["person"] |= {r[0] for r in conn.execute(
            "SELECT key FROM contact_keys WHERE contact_id IN (%s)" % ",".join("?" * len(ids)), ids)}
        for (email,) in conn.execute("SELECT email FROM contacts WHERE id IN (%s) AND email IS NOT NULL"
                                     % ",".join("?" * len(ids)), ids):
            reg = keys.registrable_domain(email)
            if reg:
                c["domains"].add("dom:" + reg)
    if t.get("email"):
        c["person"] |= {k for k, _ in keys.email_keys(t["email"])}
    if t.get("linkedin_url"):
        c["person"] |= {k for k, _ in keys.linkedin_keys(t["linkedin_url"])}
    if t.get("job_id"):
        c["job"] |= {r[0] for r in conn.execute("SELECT key FROM job_keys WHERE job_id = ?", (t["job_id"],))}
        row = conn.execute("SELECT canonical_key, source_url, apply_url FROM jobs WHERE id = ?",
                           (t["job_id"],)).fetchone()
        if row:
            c["job"].add(row[0])
            for u in (row[1], row[2]):
                if u:
                    try:
                        ck, al = keys.job_key(u, {}, None)
                        c["job"] |= {ck} | set(al)
                    except Denied:
                        pass
    if t.get("job_url"):
        ck, al = keys.job_key(t["job_url"], {}, None)
        c["job"] |= {ck} | set(al)
    c["person"] |= {hashed(k) for k in list(c["person"])}
    return c


def match(conn, **targets) -> list[dict]:
    """Active exclusions that hit any of the targets: company_id, company_name, domain, email, contact_id,
    linkedin_url, job_id, job_url. Returns [{type, value_key, reason}]."""
    unknown = set(targets) - {"company_id", "company_name", "domain", "email", "contact_id", "linkedin_url",
                              "job_id", "job_url"}
    if unknown:
        raise Denied("E_INTERNAL", "unknown exclusion targets: %s" % ", ".join(sorted(unknown)))
    c = _candidate_keys(conn, targets)
    hits = []
    for row in conn.execute("SELECT id, type, value_raw, value_key, reason FROM exclusions WHERE active = 1"):
        ek = _exclusion_keys(row["type"], row["value_raw"], row["value_key"])
        if row["type"] == "company":
            hit = bool(ek & c["company"])
        elif row["type"] == "domain":
            hit = bool(ek & (c["company"] | c["domains"]))
        elif row["type"] in ("email", "linkedin"):
            hit = bool(ek & c["person"])
        else:
            hit = bool(ek & c["job"])
        if hit:
            hits.append({"id": row["id"], "type": row["type"], "value_key": row["value_key"], "reason": row["reason"]})
    return hits


def apply(conn, exclusion_id: int) -> None:
    """Push one exclusion's effect onto existing rows."""
    row = conn.execute("SELECT * FROM exclusions WHERE id = ?", (exclusion_id,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no exclusion %r" % exclusion_id)
    if not row["active"] or row["value_key"].startswith("h:"):
        return
    reason = "exclusion:%d" % exclusion_id
    ts = now()
    etype, raw = row["type"], row["value_raw"]
    cids = []
    if etype in ("company", "domain"):
        ek = _exclusion_keys(etype, raw, row["value_key"])
        cids = _companies_matching(conn, ek)
        if not cids:
            cids = [companies.resolve(conn, name=raw if etype == "company" else None,
                                      domain=row["value_key"][4:] if etype == "domain" else None,
                                      source="exclusions")]
    if etype == "domain":
        dom = row["value_key"][4:]
        for (pid, email) in conn.execute("SELECT id, email FROM contacts WHERE email IS NOT NULL").fetchall():
            if keys.registrable_domain(email) == dom:
                conn.execute("UPDATE contacts SET do_not_contact = 1, dnc_reason = ?, updated_at = ? WHERE id = ? "
                             "AND do_not_contact = 0", (reason, ts, pid))
    elif etype in ("email", "linkedin"):
        ek = _exclusion_keys(etype, raw, row["value_key"])
        for (pid,) in conn.execute("SELECT DISTINCT contact_id FROM contact_keys WHERE key IN (%s)"
                                   % ",".join("?" * len(ek)), sorted(ek)).fetchall():
            for gid in people.group(conn, pid):
                conn.execute("UPDATE contacts SET do_not_contact = 1, dnc_reason = ?, updated_at = ? WHERE id = ? "
                             "AND do_not_contact = 0", (reason, ts, gid))
    elif etype == "job_url":
        ek = _exclusion_keys(etype, raw, row["value_key"])
        for (jid,) in conn.execute("SELECT DISTINCT job_id FROM job_keys WHERE key IN (%s)" % ",".join("?" * len(ek)),
                                   sorted(ek)).fetchall():
            _exclude_job(conn, jid)
        for (jid,) in conn.execute("SELECT id FROM jobs WHERE canonical_key IN (%s)" % ",".join("?" * len(ek)),
                                   sorted(ek)).fetchall():
            _exclude_job(conn, jid)
    for cid in cids:
        conn.execute("UPDATE companies SET contact_state = 'do_not_contact', contact_state_reason = ?, updated_at = ? "
                     "WHERE id = ?", (reason, ts, cid))
        ids = companies.group(conn, cid)
        for (jid,) in conn.execute("SELECT id FROM jobs WHERE company_id IN (%s)" % ",".join("?" * len(ids)),
                                   ids).fetchall():
            _exclude_job(conn, jid)
    log_event(conn, "exclusion_applied", exclusion_id=exclusion_id, type=etype)


def _companies_matching(conn, ek: set) -> list[int]:
    """Surviving companies whose alias keys, or whose display name and domain keys, meet the exclusion keys."""
    out = set()
    if ek:
        for (cid,) in conn.execute("SELECT DISTINCT company_id FROM company_aliases WHERE alias_key IN (%s)"
                                   % ",".join("?" * len(ek)), sorted(ek)).fetchall():
            out.add(companies.survivor(conn, cid))
    for (cid, name, dom) in conn.execute("SELECT id, display_name, domain FROM companies "
                                         "WHERE merged_into IS NULL").fetchall():
        if cid not in out and {k for k, _ in keys.company_keys(name=name, domain=dom)} & ek:
            out.add(cid)
    return sorted(out)


def _exclude_job(conn, job_id: int) -> None:
    st = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if st and st[0] in EXCLUDABLE_JOB_STATUSES:
        jobstate.set_job_status(conn, job_id, "excluded", "excluded_by_rule", "system:exclusions")


def _undo(conn, exclusion_id: int) -> None:
    reason = "exclusion:%d" % exclusion_id
    ts = now()
    conn.execute("UPDATE companies SET contact_state = 'none', contact_state_reason = ?, updated_at = ? "
                 "WHERE contact_state = 'do_not_contact' AND contact_state_reason = ?", ("exclusion removed", ts, reason))
    conn.execute("UPDATE contacts SET do_not_contact = 0, dnc_reason = NULL, updated_at = ? "
                 "WHERE do_not_contact = 1 AND dnc_reason = ?", (ts, reason))


def add(conn, etype: str, value: str, reason: str | None, source: str = "human") -> dict:
    """Insert or reactivate one exclusion and apply it. Returns {id, outcome: added|reactivated|unchanged}."""
    if source not in SOURCES:
        raise Denied("E_VALIDATION", "unknown exclusion source %r" % source)
    vkey = value.strip() if value.startswith("h:") else value_key(etype, value)
    ts = now()
    row = conn.execute("SELECT id, active FROM exclusions WHERE type = ? AND value_key = ?", (etype, vkey)).fetchone()
    if row is None:
        cur = conn.execute("INSERT INTO exclusions (type, value_raw, value_key, reason, source, active, created_at, "
                           "updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                           (etype, value.strip()[:500], vkey, (reason or "")[:300] or None, source, ts, ts))
        eid, outcome = cur.lastrowid, "added"
    elif not row["active"]:
        conn.execute("UPDATE exclusions SET active = 1, deactivated_at = NULL, deactivated_by = NULL, updated_at = ? "
                     "WHERE id = ?", (ts, row["id"]))
        eid, outcome = row["id"], "reactivated"
    else:
        return {"id": row["id"], "outcome": "unchanged"}
    apply(conn, eid)
    log_event(conn, "exclusion_" + outcome, exclusion_id=eid, type=etype, source=source)
    return {"id": eid, "outcome": outcome}


def remove(conn, etype: str, value: str, by: str) -> dict:
    vkey = value_key(etype, value)
    row = conn.execute("SELECT id FROM exclusions WHERE type = ? AND value_key = ? AND active = 1",
                       (etype, vkey)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no active exclusion %s %s" % (etype, value))
    _deactivate(conn, row["id"], by)
    return {"id": row["id"], "deactivated": True}


def _deactivate(conn, eid: int, by: str) -> None:
    ts = now()
    conn.execute("UPDATE exclusions SET active = 0, deactivated_at = ?, deactivated_by = ?, updated_at = ? WHERE id = ?",
                 (ts, by, ts, eid))
    _undo(conn, eid)
    log_event(conn, "exclusion_deactivated", exclusion_id=eid, by=by)


def list_(conn, etype: str | None = None) -> list[dict]:
    q = "SELECT id, type, value_raw, value_key, reason, source, active, created_at, deactivated_at FROM exclusions"
    args = ()
    if etype:
        q += " WHERE type = ?"
        args = (etype,)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", args)]


def parse_csv(text: str) -> tuple[list[dict], list[dict], bool]:
    """(rows [{line, type, value, reason, value_key}], errors [{line, reason}], header_ok)."""
    rows, errors = [], []
    header_ok = False
    text = text[1:] if text.startswith("\ufeff") else text
    lines = [(i + 1, ln) for i, ln in enumerate(text.splitlines())]
    body = [(n, ln) for n, ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    if not body:
        return rows, errors, False
    first_no, first = body[0]
    hdr = [h.strip().lower() for h in next(csv.reader([first]))]
    if hdr[:2] == ["type", "value"]:
        header_ok = True
        body = body[1:]
    else:
        errors.append({"line": first_no, "reason": "header must be type,value,reason"})
        return rows, errors, False
    for n, ln in body:
        try:
            parts = next(csv.reader([ln]))
        except csv.Error as exc:
            errors.append({"line": n, "reason": "bad CSV: %s" % exc})
            continue
        if len(parts) < 2:
            errors.append({"line": n, "reason": "needs type and value"})
            continue
        etype, value = parts[0].strip().lower(), parts[1].strip()
        reason = ",".join(parts[2:]).strip() if len(parts) > 2 else ""
        try:
            vk = value_key(etype, value)
        except Denied as d:
            errors.append({"line": n, "reason": d.message})
            continue
        rows.append({"line": n, "type": etype, "value": value, "reason": reason, "value_key": vk})
    return rows, errors, header_ok


def import_csv(conn, path: str | None, deactivate: bool, caller) -> dict:
    """Import private/exclusions.csv (section 9). Never deactivates unless deactivate is true and the
    caller is the human; then refuses below exclusions.min_keep_ratio."""
    from . import config, db
    path = path or csv_file()
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8-sig")        # a spreadsheet's byte order mark is not part of the header
    except FileNotFoundError:
        raw, text = b"", ""
    except (OSError, UnicodeDecodeError) as exc:
        raise Denied("E_VALIDATION", "exclusions file is not readable UTF-8: %s" % exc)
    rows, errors, header_ok = parse_csv(text)
    out = {"added": 0, "reactivated": 0, "unchanged": 0, "missing_from_file": 0, "deactivated": 0, "errors": errors,
           "would_deactivate": []}
    in_file = set()
    for r in rows:
        in_file.add((r["type"], r["value_key"]))
        res = add(conn, r["type"], r["value"], r["reason"], source="private_csv")
        out[res["outcome"]] += 1
    active_csv = [dict(r) for r in conn.execute("SELECT id, type, value_raw, value_key FROM exclusions "
                                                "WHERE active = 1 AND source = 'private_csv'")]
    missing = [r for r in active_csv if (r["type"], r["value_key"]) not in in_file]
    out["missing_from_file"] = len(missing)
    out["would_deactivate"] = [{"type": r["type"], "value": r["value_raw"]} for r in missing]
    if deactivate:
        if getattr(caller, "cls", None) != "human":
            raise Denied("E_HUMAN_ONLY", "exclusions import --deactivate needs the owner PIN")
        if not header_ok:
            raise Denied("E_PRECONDITION", "the file has no valid header; nothing is deactivated")
        kept = len(active_csv) - len(missing)
        ratio = config.load(conn)["exclusions"]["min_keep_ratio"]
        if active_csv and kept < ratio * len(active_csv):
            raise Denied("E_PRECONDITION", "the file holds %d of %d active rows (below %d%%); nothing is deactivated"
                         % (kept, len(active_csv), int(ratio * 100)), data={"would_deactivate": out["would_deactivate"]})
        for r in missing:
            _deactivate(conn, r["id"], "human")
        out["deactivated"] = len(missing)
    # the same hash as file_changed() (raw bytes, so CRLF and a byte order mark do not re-trigger the import).
    # A file whose header is not understood is not marked as imported: it is read again on the next run.
    sha = hashlib.sha256(raw).hexdigest()
    if header_ok or not text.strip():
        db.meta_set(conn, "exclusions_sha256", sha, "system")
    if errors:
        _report_errors(conn, errors, sha, header_ok)
    if text and header_ok:
        _backup(path)
    return out


def _report_errors(conn, errors: list[dict], sha: str, header_ok: bool) -> None:
    """Tell the owner once per file version which lines were not imported (automatic imports have no
    other reader for the errors)."""
    from .events import enqueue_notification
    shown = "; ".join("line %d: %s" % (e["line"], e["reason"]) for e in errors[:3])
    more = " and %d more" % (len(errors) - 3) if len(errors) > 3 else ""
    head = ("Your exclusions file was not imported" if not header_ok else
            "%d line(s) of your exclusions file were not imported" % len(errors))
    enqueue_notification(conn, "exclusions_errors:%s" % sha[:16], "high" if not header_ok else "normal", "alert",
                         "%s (%s%s). Fix private/exclusions.csv; until then those entries are not excluded."
                         % (head, shown, more))
    log_event(conn, "exclusions_import_errors", errors=len(errors), header_ok=header_ok)


def file_changed(conn) -> bool:
    try:
        with open(csv_file(), "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return False
    row = conn.execute("SELECT value FROM meta WHERE key = 'exclusions_sha256'").fetchone()
    return (row[0] if row else None) != sha


def _backup(path: str, keep: int = 30) -> None:
    bdir = os.path.join(paths.state_dir(), "backups")
    try:
        os.makedirs(bdir, exist_ok=True)
        dst = os.path.join(bdir, "exclusions-%s.csv" % now().replace(":", "").replace("-", ""))
        shutil.copyfile(path, dst)
        os.chmod(dst, 0o600)
        olds = sorted(f for f in os.listdir(bdir) if f.startswith("exclusions-") and f.endswith(".csv"))
        for f in olds[:-keep]:
            os.unlink(os.path.join(bdir, f))
    except OSError:
        pass


def csv_text(rows: list[tuple[str, str, str]]) -> str:
    """Helper for tests and docs: CSV text with the header."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["type", "value", "reason"])
    for r in rows:
        w.writerow(r)
    return buf.getvalue()

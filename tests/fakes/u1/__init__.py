"""Fakes and builders used by U1 tests (owned by U1, design 11 rule 4).

- fake_presend: stands in for U3 qc.presend.presend (re-lint and hash of the stored draft text).
- fake_on_confirm: stands in for U6 threads.on_confirm (thread row and follow-up due date).
- World: builds a ready-to-send state (config, guard heartbeat, company, contact, job, approved draft,
  clear precheck, clear detection) with fictional placeholder data only.
"""
from __future__ import annotations

import json
import os
from unittest import mock

from jobhunter import canon, db, gate, hooks, paths
from tests.helpers import insert_company, insert_contact, insert_job

TUESDAY_NOON = "2026-09-29T12:00:00Z"
KIND_TEXT = {"cold_email": "cold_email", "followup_email": "followup_email", "application_email": "application_email",
             "li_invite_note": "li_invite_note", "li_message": "li_message", "li_followup": "li_followup",
             "inmail": "inmail"}


def draft_text(conn, draft_id: int) -> str:
    d = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if d["kind"] == "application_package":
        payload = json.loads(d["payload_json"] or "{}")
        if isinstance(payload.get("payload"), dict):      # the U3 shape: {"payload": {fields, resume_variant_uid}}
            payload = payload["payload"]
        return canon.canonical_send_text("application", None, None, payload.get("fields") or [], None,
                                         {"filename": payload.get("resume_filename")} if payload.get("resume_filename")
                                         else None)
    return canon.canonical_send_text(KIND_TEXT[d["kind"]], d["subject"], d["body"], None, None)


def fake_presend(conn, draft_id: int) -> dict:
    text = draft_text(conn, draft_id)
    return {"ok": True, "sha256": canon.sha256_text(text), "send_text": text, "blocks": []}


def fake_on_confirm(conn, action_row) -> dict | None:
    kind = action_row["kind"]
    ts = canon.now()
    if kind == "application":
        return None
    if kind in ("cold_email", "application_email"):
        key = action_row["thread_key"] or "em:" + action_row["token"]
        due = canon.ts_add(ts, days=7)
        conn.execute("INSERT INTO threads (thread_key, channel, contact_id, company_id, job_id, first_action_id, state, "
                     "followup_due_at, created_at, updated_at) VALUES (?, 'email', ?, ?, ?, ?, 'open', ?, ?, ?)",
                     (key, action_row["contact_id"], action_row["company_id"], action_row["job_id"], action_row["id"],
                      due, ts, ts))
        return {"thread_key": key, "followup_due_at": due}
    if kind in ("followup_email", "li_followup"):
        conn.execute("UPDATE threads SET state = 'followed_up', followup_action_id = ?, updated_at = ? "
                     "WHERE thread_key = ?", (action_row["id"], ts, action_row["thread_key"]))
        return {"thread_key": action_row["thread_key"], "followup_due_at": None}
    key = action_row["thread_key"]
    if not key and action_row["contact_id"]:
        key = "li:" + conn.execute("SELECT contact_uid FROM contacts WHERE id = ?",
                                   (action_row["contact_id"],)).fetchone()[0]
    if kind == "li_invite":
        conn.execute("INSERT INTO threads (thread_key, channel, contact_id, company_id, first_action_id, state, "
                     "created_at, updated_at) VALUES (?, 'linkedin', ?, ?, ?, 'invite_pending', ?, ?) "
                     "ON CONFLICT (thread_key) DO NOTHING", (key, action_row["contact_id"], action_row["company_id"],
                                                             action_row["id"], ts, ts))
    return {"thread_key": key, "followup_due_at": None}


def patch_hooks():
    """Context manager: route gate hooks to the fakes."""
    return mock.patch.multiple(hooks, presend=fake_presend, on_confirm=fake_on_confirm)


def write_config(overrides: dict | None = None) -> dict:
    """private/config.json = config.example.json with timezone UTC, owner identity set, plus overrides.
    The tests' baseline email route is app_password (code-held mailer tokens); the shipped default is web_ui,
    so tests of the browser email route pass {"gmail.route": "web_ui"}."""
    from jobhunter import hardmax
    cfg = hardmax.defaults()
    cfg["timezone"] = "UTC"
    cfg["gmail"]["route"] = "app_password"
    cfg["owner"]["gmail_address"] = "owner@example.com"
    cfg["owner"]["linkedin_profile_url"] = "https://www.linkedin.com/in/example-owner"
    cfg["owner"]["linkedin_name"] = "Example Owner"
    for path, value in (overrides or {}).items():
        node = cfg
        parts = path.split(".")
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = value
    with open(os.path.join(paths.private_dir(), "config.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    return cfg


def write_heartbeat(age_s: int = 30, install_id: str | None = None, proof_version: int | None = 2,
                    carriers: list | None = None) -> None:
    """state/guard/heartbeat.json as the guard writes it (CLI route 7.12: proof_version 2 and the carriers);
    proof_version None writes the heartbeat of an old guard."""
    hb = {"install_id": install_id or paths.home()["install_id"], "version": "2.1.0",
          "loaded_at": canon.ts_add(canon.now(), seconds=-age_s - 60), "beat_at": canon.ts_add(canon.now(), seconds=-age_s)}
    if proof_version is not None:
        hb.update({"proof_version": proof_version, "carriers": list(carriers or ["argv", "env"]),
                   "native_tools": "deny", "pin_tool_surface": True})
    os.makedirs(paths.guard_dir(), exist_ok=True)
    with open(os.path.join(paths.guard_dir(), "heartbeat.json"), "w", encoding="utf-8") as fh:
        json.dump(hb, fh)


def enable_linkedin(conn) -> None:
    with db.tx(conn):
        db.meta_set(conn, "channel_linkedin_enabled", "1", "human")
        db.meta_set(conn, "linkedin_tos_ack", "1", "human")


class World:
    """A company, a person, a job and helpers to produce approved drafts, prechecks and detections."""

    def __init__(self, conn, name: str = "Kestrel Commerce", domain: str = "kestrel.example"):
        self.conn = conn
        self.company = insert_company(conn, name=name, domain=domain)
        conn.execute("INSERT INTO company_aliases (alias_key, company_id, kind, source, created_at) VALUES "
                     "(?, ?, 'name', 'job_board', ?)", ("id:" + name.lower().replace(" ", ""), self.company, canon.now()))
        self.person = self.contact("Alex Rivera", "alex.rivera@" + domain, "alex-rivera-example")
        self.job = insert_job(conn, company_id=self.company, status="apply_queued", title="Data Analyst")
        conn.execute("UPDATE jobs SET role_key = 'analyst data' WHERE id = ?", (self.job,))

    def contact(self, name: str, email: str | None, slug: str | None = None, role_type: str = "hiring_manager") -> int:
        cid = insert_contact(self.conn, company_id=self.company, full_name=name, email=email, role_type=role_type,
                             linkedin_url="https://www.linkedin.com/in/" + slug if slug else None)
        self.conn.execute("UPDATE contacts SET email_grade = 'A', email_mx_ok = 1, li_slug = ? WHERE id = ?", (slug, cid))
        if email:
            self.conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, 'email', ?)",
                              ("email:" + email, cid, canon.now()))
        if slug:
            self.conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, 'li_slug', ?)",
                              ("li:" + slug, cid, canon.now()))
        return cid

    def draft(self, kind: str = "cold_email", contact_id: int | None = None, job_id: int | None = None,
              thread_key: str | None = None, body: str = "Hi Alex,\n\nA short note about returns.\n\nThanks,",
              subject: str | None = "Returns forecasting", payload: dict | None = None, status: str = "approved") -> int:
        ts = canon.now()
        contact_id = self.person if (contact_id is None and kind not in ("application_package",)) else contact_id
        recipient = None
        if kind in ("cold_email", "application_email") and contact_id:
            recipient = self.conn.execute("SELECT email FROM contacts WHERE id = ?", (contact_id,)).fetchone()[0]
        elif kind in ("li_invite_note", "li_message", "inmail") and contact_id:
            recipient = self.conn.execute("SELECT li_slug FROM contacts WHERE id = ?", (contact_id,)).fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO drafts (draft_uid, kind, channel, send_route, job_id, contact_id, company_id, thread_key, "
            "recipient, subject, body, payload_json, text_sha256, status, approved_by, approved_at, expires_at, "
            "created_at, updated_at) VALUES (?, ?, 'email_cold', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'x', ?, ?, ?, ?, ?, ?)",
            (canon.new_uid("D"), kind, "mailer" if kind in ("cold_email", "followup_email", "application_email")
             else "browser", job_id, contact_id if kind != "followup_email" else None,
             self.company if kind not in ("followup_email",) else None, thread_key, recipient,
             subject if kind in ("cold_email", "followup_email", "application_email") else None, body,
             json.dumps(payload or {}), status, "human:cli" if status in ("approved", "sent") else None,
             ts if status in ("approved", "sent") else None, canon.ts_add(ts, hours=72), ts, ts))
        did = cur.lastrowid
        self.conn.execute("UPDATE drafts SET text_sha256 = ? WHERE id = ?",
                          (canon.sha256_text(draft_text(self.conn, did)), did))
        return did

    def precheck(self, kind: str, platform: str, checks: dict | None = None, **target) -> dict:
        spec = gate.CHECK_SPECS[gate._spec_key(kind)]
        defaults = {"int": 0, "bool": False, "str": "x"}
        vals = {}
        for name, typ in spec:
            if typ.startswith("enum:"):
                vals[name] = {"profile_button": "Connect", "connection_degree": "1st"}[name]
            elif name == "vanity_slug":
                cid = target.get("contact_id") or self.person
                vals[name] = self.conn.execute("SELECT li_slug FROM contacts WHERE id = ?", (cid,)).fetchone()[0]
            else:
                vals[name] = defaults[typ]
        vals.update(checks or {})
        ev = {"kind": kind, "platform": platform, "observed_at": canon.now(),
              "checks": [{"name": k, "value": v} for k, v in vals.items()]}
        return gate.record_precheck(self.conn, kind, platform, ev, "agent", **target)

    def detect_clear(self, platform: str) -> int:
        cur = self.conn.execute("INSERT INTO detections (platform, source, verdict, created_at) VALUES "
                                "(?, 'agent', 'clear', ?)", (platform, canon.now()))
        return cur.lastrowid

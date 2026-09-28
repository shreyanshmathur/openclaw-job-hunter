"""Nightly audit (design 4.5): anything that went out without a ledger action trips `global`
(audit_mismatch) and opens a review_audit_mismatch task.

Sources: Sent messages found by the mail audit (U9 `mail.audit.sent_since`, IMAP) that match no action;
application confirmation emails from a company with no application action. The LinkedIn invitation gauge
(which also counts the person's own manual invites) is reported as information only.

run(conn, days) is the entry point: the IMAP read (fetch_mail) happens first, outside any transaction, and
only then is BEGIN IMMEDIATE taken to record the result (audit_run), so a slow mail server never holds the
write lock (design 11 rule 5).
"""
from __future__ import annotations

from . import breakers, db
from .canon import now, ts_add
from .errors import Denied
from .events import log_event, open_human_task


def _mail_sent_since(conn, days: int):
    try:
        from .mail import audit as mail_audit
    except ImportError:
        return None, "mail audit module not installed"
    try:
        return list(mail_audit.sent_since(conn, days)), None
    except Exception as exc:   # the audit never blocks housekeeping; the failure is reported
        return None, "%s: %s" % (type(exc).__name__, exc)


def fetch_mail(conn, days: int = 2, sent_since=None) -> dict:
    """The IMAP half of the audit: {sent: [unledgered Sent messages] | None, error}. Must run outside any
    transaction (it opens IMAP with 30 s timeouts); it only reads the database. `sent_since` replaces the U9
    function in tests."""
    if conn.in_transaction:
        raise Denied("E_INTERNAL", "the mail audit reads IMAP and must run outside a transaction")
    if sent_since is None:
        sent, err = _mail_sent_since(conn, days)
    else:
        try:
            sent, err = list(sent_since(conn, days)), None
        except Exception as exc:
            sent, err = None, "%s: %s" % (type(exc).__name__, exc)
    return {"sent": sent, "error": err}


def run(conn, days: int = 2, sent_since=None) -> dict:
    """Fetch the Sent headers first (no transaction), then record the audit in one short transaction."""
    mail = fetch_mail(conn, days, sent_since)
    with db.tx(conn):
        return audit_run(conn, days, mail=mail)


def audit_run(conn, days: int = 2, sent_since=None, mail: dict | None = None) -> dict:
    """Returns {mismatches: [{kind, platform, detail}], info: [...], errors: [...]}. Runs inside the caller's
    transaction. `mail` is the result of fetch_mail, taken before the transaction; without it (and without a
    test `sent_since`) the mail audit is read here, which holds the write lock for the IMAP time: use run()."""
    mismatches, info, errors = [], [], []
    if mail is not None:
        sent, err = mail.get("sent"), mail.get("error")
    elif sent_since is not None:
        sent, err = list(sent_since(conn, days)), None
    else:
        sent, err = _mail_sent_since(conn, days)
    if err:
        errors.append({"source": "mail", "error": err})
    for m in sent or []:
        mismatches.append({"kind": "unledgered_email", "platform": "gmail",
                           "detail": "sent %s to %s (%s)" % (m.get("date") or m.get("sent_at") or "?",
                                                            m.get("to") or m.get("recipient") or "?",
                                                            (m.get("subject") or "")[:80])})
    since = ts_add(now(), days=-days)
    for r in conn.execute("SELECT i.id, i.company_id, i.received_at, i.from_domain FROM inbound_messages i "
                          "WHERE i.code_class = 'application_confirmation' AND i.received_at > ?", (since,)).fetchall():
        if r["company_id"] is None:
            continue
        hit = conn.execute("SELECT 1 FROM actions WHERE company_id = ? AND kind IN ('application','application_email') "
                           "AND status IN ('reserved','armed','sent','failed_after_click','unknown','imported')",
                           (r["company_id"],)).fetchone()
        if not hit:
            mismatches.append({"kind": "unledgered_application", "platform": "ats",
                               "detail": "confirmation email from %s on %s" % (r["from_domain"], r["received_at"])})
    g = conn.execute("SELECT n FROM counters WHERE kind = 'gauge' AND platform = 'linkedin' AND metric = "
                     "'li_invites_sent_7d' ORDER BY ts DESC LIMIT 1").fetchone()
    if g is not None:
        ledger = conn.execute("SELECT count(*) FROM actions WHERE kind = 'li_invite' AND status IN ('reserved','armed',"
                              "'sent','failed_after_click','unknown','imported') AND reserved_at > ?",
                              (ts_add(now(), days=-7),)).fetchone()[0]
        if g[0] > ledger:
            info.append({"kind": "li_invites_outside_ledger", "platform": "linkedin",
                         "detail": "the Sent page shows %d invites in 7 days, the ledger %d (manual invites count)"
                         % (g[0], ledger)})
    if mismatches:
        detail = "; ".join(m["detail"] for m in mismatches)[:1500]
        breakers.trip(conn, "global", "audit_mismatch", detail, by="audit")
        open_human_task(conn, "review_audit_mismatch", "Something went out without a ledger entry: %s" % detail[:500])
    db.meta_set(conn, "audit_last_at", now(), "system")
    log_event(conn, "audit", mismatches=len(mismatches), errors=len(errors))
    return {"mismatches": mismatches, "info": info, "errors": errors}

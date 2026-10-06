"""State builders for the code-owned browser step tests (U1 tests of otp, accounts and captcha).

applier_state(conn) makes what every compound step checks first (FEATURES-OTP-ACCOUNTS-CAPTCHA 2.1): a fresh
guard heartbeat, a running applier cycle holding the browser lease, a Workday job at the fictional Kestrel
Commerce claimed by that cycle with an approved application package, and the owner's consent rows. grant_caps()
writes capability rows into private/consent.json as the owner's grant command does.
"""
from __future__ import annotations

from jobhunter import canon, db, identity, locks
from tests.fakes.u1 import write_heartbeat
from tests.helpers import insert_company, insert_cycle, insert_draft, insert_job

WD_URL = "https://kestrel.wd5.myworkdayjobs.com/en-US/External/job/Remote/Senior-Analyst_JR-1001"
OWNER = "sam.lee@example.com"


def write_owner_config(**over) -> dict:
    """private/config.json from the example with a fictional owner and wide-open browser hours."""
    import json
    import os
    from jobhunter import config, hardmax
    cfg = hardmax.defaults()
    cfg["timezone"] = "UTC"
    cfg["owner"]["gmail_address"] = OWNER
    cfg["active_hours"]["browser_days"] = [1, 2, 3, 4, 5, 6, 7]
    cfg["active_hours"]["browser_window"] = ["00:00", "23:59"]
    for path, value in over.items():
        node = cfg
        parts = path.split(".")
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = value
    path = config.config_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    os.chmod(path, 0o600)
    return cfg


def grant_caps(conn, sites=("workday",), caps=("email_codes", "ats_accounts")) -> None:
    with db.tx(conn):
        identity.grant_capability(conn, list(caps), list(sites), by="human:cli")


def applier_state(conn, url: str = WD_URL, source: str = "workday") -> dict:
    write_heartbeat()
    co = insert_company(conn)
    job = insert_job(conn, company_id=co, title="Senior Analyst", status="apply_queued", source=source, url=url)
    cyc = insert_cycle(conn, lane="applier")
    ts = canon.now()
    conn.execute("UPDATE jobs SET apply_url = ?, apply_route = 'ats_form', claimed_by = ?, claimed_until = ? "
                 "WHERE id = ?", (url, cyc, canon.ts_add(ts, minutes=30), job))
    d = insert_draft(conn, kind="application_package", status="approved", job_id=job, send_route="browser",
                     channel="application_package", subject=None)
    with db.tx(conn):
        locks.acquire(conn, "browser", cyc, 1800)
    uid = conn.execute("SELECT job_uid FROM jobs WHERE id = ?", (job,)).fetchone()[0]
    return {"job_id": job, "job_uid": uid, "cycle_id": cyc, "draft_id": d, "company_id": co}

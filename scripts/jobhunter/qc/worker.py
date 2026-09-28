"""QC worker: runs the jobhunter-qc agent turn for a queued review and records the verdict (design 5.3).

- spawn_worker(qjob_uid): `jh.py qc worker --job <uid>` detached (start_new_session, no stdio, no agent env),
  called by `qc review start` and `edit` after COMMIT.
- run_job(qjob_uid): claims the job, heartbeats every 15 s while `ocrun.agent_turn` runs (no shell, binary and
  profile from private/home.json), then in one transaction writes qc_results (model and code verdict, stricter
  wins) and moves the draft. A reviewer error or timeout is not a verdict: one retry (try_no 2), then the draft
  goes review_failed with reason reviewer_unavailable, which does not use the rewrite budget.
- drain(max_seconds): the `jobhunter:qc-worker` command job; recovers jobs whose heartbeat is older than 60 s and
  runs queued jobs while time is left.
- golden(...): `qc golden` calibration run over qc/golden (PIN, terminal).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

from .. import approvals, canon, db, drafts, jobstate, paths
from ..canon import now, ts_add
from ..errors import Denied
from ..events import enqueue_notification, log_event
from . import GOLDEN_DIR, REVIEWER_PROMPT_FILE, agent_turn as _agent_turn, read_json, settings
from . import review

HEARTBEAT_S = 15
ORPHAN_S = 60
_AGENT_ENV = ("OPENCLAW_SHELL", "JH_AGENT_ID", "JH_SESSION_KEY", "JH_RUN_ID")


def spawn_worker(qjob_uid: str) -> None:
    """Start the worker detached. Never spawns from a test home unless qc.SPAWN is set."""
    from .. import qc
    if qc.SPAWN is not None:
        qc.SPAWN(qjob_uid)
        return
    if paths.is_test_home():
        return
    try:
        h = paths.home()
    except Denied:
        h = {}
    py = h.get("python") or sys.executable
    env = {k: v for k, v in os.environ.items() if k not in _AGENT_ENV}
    try:
        subprocess.Popen([py, os.path.join(paths.SCRIPTS_DIR, "jh.py"), "qc", "worker", "--job", qjob_uid, "--quiet"],
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=env, cwd=paths.REPO, close_fds=True)
    except OSError:
        pass   # the jobhunter:qc-worker command job drains queued jobs every 5 minutes


class _Heartbeat(threading.Thread):
    def __init__(self, qjob_uid: str):
        super().__init__(daemon=True)
        self.qjob_uid = qjob_uid
        self.stop_event = threading.Event()

    def run(self):
        try:
            conn = db.connect()
        except Exception:
            return
        try:
            while not self.stop_event.wait(HEARTBEAT_S):
                try:
                    with db.tx(conn):
                        conn.execute("UPDATE qc_jobs SET heartbeat_at = ? WHERE qjob_uid = ? AND state = 'running'",
                                     (now(), self.qjob_uid))
                except Denied:
                    pass
        finally:
            conn.close()

    def stop(self):
        self.stop_event.set()


def _job(conn, qjob_uid: str):
    j = conn.execute("SELECT * FROM qc_jobs WHERE qjob_uid = ?", (qjob_uid,)).fetchone()
    if j is None:
        raise Denied("E_NOT_FOUND", "unknown QC job %s" % qjob_uid)
    return j


def _issues_text(verdict: dict | None, error: str | None) -> str:
    if not verdict:
        return error or "the reviewer reply could not be read"
    items = ["%s: %s" % (i.get("quote", ""), i.get("problem", "")) for i in (verdict.get("issues") or [])[:3]]
    return "; ".join(items) or verdict.get("rewrite_brief") or "no details"


def record_verdict(conn, job, text: str, cfg: dict | None = None) -> dict:
    """Inside the caller's tx: parse, decide, write qc_results and move the draft."""
    cfg = cfg or settings()
    row = drafts.get_by_id(conn, job["draft_id"])
    if row["status"] != "review_pending" or row["attempt"] != job["attempt"]:
        conn.execute("UPDATE qc_jobs SET state = 'done', error = 'stale', finished_at = ? WHERE id = ?",
                     (now(), job["id"]))
        return {"qjob_uid": job["qjob_uid"], "stale": True}
    parsed = review.parse_verdict(text, job["nonce"], row["text_sha256"])
    verdict = parsed["verdict"]
    dec = review.decide(verdict, row["channel"], cfg)
    human_no = int(drafts.payload_of(row).get("edited_by_human") or 0)
    drafts.record_qc(conn, row["id"], job["attempt"], "review", dec["pass"], row["text_sha256"], [], [],
                     human_edit_no=human_no,
                     review_json=json.dumps(verdict if verdict else {"parse_error": parsed["error"]}, sort_keys=True),
                     weighted_score=dec["weighted_score"], lowest_criterion=dec["lowest_criterion"],
                     gates_failed=json.dumps(dec["gates_failed"] + dec["low_scores"]),
                     model_verdict=dec["model_verdict"], code_verdict=dec["code_verdict"],
                     reviewer_model=cfg["qc"]["review"].get("model"))
    conn.execute("UPDATE qc_jobs SET state = 'done', verdict = ?, error = ?, finished_at = ? WHERE id = ?",
                 ("pass" if dec["pass"] else "fail", parsed["error"], now(), job["id"]))
    if dec["disagree"]:
        log_event(conn, "qc_verdict_disagreement", draft_uid=row["draft_uid"], model=dec["model_verdict"],
                  code=dec["code_verdict"])
    out = {"qjob_uid": job["qjob_uid"], "verdict": "pass" if dec["pass"] else "fail", "draft_uid": row["draft_uid"]}
    if dec["pass"]:
        jobstate.set_draft_status(conn, row["id"], "qc_passed", "review_passed", "system:qc")
        routed = approvals.route_after_qc(conn, row["id"], cfg=cfg)
        out["draft_status"] = routed["status"]
    elif human_no:
        if dec["soft_only"]:
            jobstate.set_draft_status(conn, row["id"], "qc_passed", "human_edit_review_soft_fail", "system:qc")
            note = "The reviewer flagged your edit (%s): %s. You may still approve it." % (
                ", ".join(dec["gates_failed"] + dec["low_scores"]) or "scores", _issues_text(verdict, parsed["error"]))
            routed = approvals.route_after_qc(conn, row["id"], note=note, cfg=cfg)
            out["draft_status"] = routed["status"]
        else:
            jobstate.set_draft_status(conn, row["id"], "review_failed", "human_edit_review_failed", "system:qc")
            code = approvals.issue_code(conn, row["id"])
            enqueue_notification(conn, "edit_failed:%s:%s" % (code, row["text_sha256"][:8]), "normal", "approval",
                                 "Your edit for %s did not pass the reviewer (%s): %s. Send a new edit with "
                                 "\"/jh edit %s <your text>\" or drop it with \"/jh skip %s\"."
                                 % (code, ", ".join(dec["gates_failed"]) or "unreadable reply",
                                    _issues_text(verdict, parsed["error"]), code, code))
            out["draft_status"] = "review_failed"
    else:
        jobstate.set_draft_status(conn, row["id"], "review_failed", "review_failed", "system:qc")
        out["draft_status"] = "review_failed"
        if row["attempt"] >= drafts.max_attempts(cfg):
            drafts.drop(conn, drafts.get_by_id(conn, row["id"]), "review_failed_last_attempt",
                        [["REVIEW", _issues_text(verdict, parsed["error"])]], cfg)
            out["draft_status"] = "dropped_qc"
    log_event(conn, "qc_review_recorded", draft_uid=row["draft_uid"], qjob_uid=job["qjob_uid"],
              verdict=out["verdict"], code_verdict=dec["code_verdict"], score=dec["weighted_score"])
    return out


def record_error(conn, job, error: str, state: str = "failed", cfg: dict | None = None, retry: bool = True) -> dict:
    """Inside the caller's tx: a reviewer error or timeout (not a verdict). Retry once, then reviewer_unavailable."""
    cfg = cfg or settings()
    conn.execute("UPDATE qc_jobs SET state = ?, error = ?, finished_at = ? WHERE id = ?",
                 (state, (error or "reviewer error")[:500], now(), job["id"]))
    row = drafts.get_by_id(conn, job["draft_id"])
    out = {"qjob_uid": job["qjob_uid"], "error": error, "draft_uid": row["draft_uid"]}
    if row["status"] != "review_pending" or row["attempt"] != job["attempt"]:
        return dict(out, stale=True)
    if retry and job["try_no"] < int(cfg["qc"]["review"]["max_tries"]):
        try:
            nxt = review.enqueue(conn, row, try_no=job["try_no"] + 1)
            log_event(conn, "qc_review_retry", draft_uid=row["draft_uid"], qjob_uid=nxt["qjob_uid"], error=error)
            return dict(out, retry_qjob_uid=nxt["qjob_uid"])
        except Denied as d:
            out["retry_error"] = d.code
    jobstate.set_draft_status(conn, row["id"], "review_failed", "reviewer_unavailable", "system:qc")
    enqueue_notification(conn, "qc_unavailable:%s" % now()[:10], "normal", "alert",
                         "The QC reviewer did not answer (%s). Drafts wait and are reviewed again in a later cycle; "
                         "./jobhunter doctor checks the reviewer agent." % (error or "error")[:120])
    log_event(conn, "qc_reviewer_unavailable", draft_uid=row["draft_uid"], error=error)
    return dict(out, reviewer_unavailable=True)


def run_job(qjob_uid: str, agent_turn=None) -> dict:
    """Run one queued review (and its single retry after an error). Opens its own connection."""
    turn = agent_turn or _agent_turn
    cfg = settings()
    conn = db.connect()
    try:
        results = []
        uid = qjob_uid
        for _ in range(3):
            tampered = None
            with db.tx(conn):
                job = _job(conn, uid)
                if job["state"] != "queued":
                    return {"qjob_uid": uid, "skipped": job["state"], "ran": results}
                try:
                    review.check_integrity(conn)
                except Denied as d:
                    tampered = d
                if tampered is None:
                    stamp = now()
                    conn.execute("UPDATE qc_jobs SET state = 'running', worker_pid = ?, started_at = ?, heartbeat_at = ? "
                                 "WHERE id = ? AND state = 'queued'", (os.getpid(), stamp, stamp, job["id"]))
            if tampered is not None:
                with db.tx(conn):
                    enqueue_notification(conn, "reviewer_tampered:%s" % now()[:13], "high", "alert",
                                         "QC reviews stopped: the reviewer prompt or the QC agent's AGENTS.md changed. "
                                         "Run ./jobhunter doctor.")
                    out = record_error(conn, _job(conn, uid), "E_REVIEWER_TAMPERED", "failed", cfg, retry=False)
                review.remove_packet(job["packet_path"])
                results.append(out)
                return dict(out, ran=results)
            hb = _Heartbeat(uid)
            hb.start()
            try:
                res = turn(cfg["qc"]["review"]["agent"], job["session_key"], job["packet_path"],
                           int(cfg["qc"]["review"]["timeout_s"]))
            except Denied as d:
                res = {"ok": False, "error": "%s: %s" % (d.code, d.message)}
            except Exception as exc:   # the reviewer call must never crash the worker
                res = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            finally:
                hb.stop()
            if not isinstance(res, dict):
                res = {"ok": False, "error": "bad agent_turn result"}
            with db.tx(conn):
                job = _job(conn, uid)
                if job["state"] != "running":
                    out = {"qjob_uid": uid, "stale": True, "state": job["state"]}
                elif res.get("ok"):
                    out = record_verdict(conn, job, res.get("text") or res.get("raw") or "", cfg)
                else:
                    err = str(res.get("error") or "reviewer error")
                    low = err.lower()
                    state = "timeout" if (res.get("timeout") or "timeout" in low or "timed out" in low) else "failed"
                    out = record_error(conn, job, err, state, cfg)
            review.remove_packet(job["packet_path"])
            results.append(out)
            if out.get("retry_qjob_uid"):
                uid = out["retry_qjob_uid"]
                continue
            break
        final = dict(results[-1]) if results else {"qjob_uid": qjob_uid}
        final["ran"] = results
        return final
    finally:
        conn.close()


def recover_orphans(conn, cfg: dict | None = None) -> int:
    """Inside the caller's tx: running jobs whose heartbeat is older than 60 s are errors (retry or unavailable);
    queued jobs whose draft moved on are closed as stale."""
    cfg = cfg or settings()
    limit = ts_add(now(), seconds=-ORPHAN_S)
    n = 0
    for job in conn.execute("SELECT * FROM qc_jobs WHERE state = 'running' AND COALESCE(heartbeat_at, started_at, "
                            "created_at) < ?", (limit,)).fetchall():
        record_error(conn, job, "worker_lost", "failed", cfg)
        review.remove_packet(job["packet_path"])
        n += 1
    for job in conn.execute("SELECT j.* FROM qc_jobs j JOIN drafts d ON d.id = j.draft_id WHERE j.state = 'queued' "
                            "AND (d.status <> 'review_pending' OR d.attempt <> j.attempt)").fetchall():
        conn.execute("UPDATE qc_jobs SET state = 'failed', error = 'stale', finished_at = ? WHERE id = ?",
                     (now(), job["id"]))
        review.remove_packet(job["packet_path"])
        n += 1
    return n


def drain(max_seconds: int, agent_turn=None) -> dict:
    """Recover orphans, then run queued jobs oldest first while a full review still fits in max_seconds."""
    cfg = settings()
    t0 = time.monotonic()
    timeout = int(cfg["qc"]["review"]["timeout_s"])
    conn = db.connect()
    try:
        with db.tx(conn):
            recovered = recover_orphans(conn, cfg)
        ran, seen = [], set()
        while max_seconds - (time.monotonic() - t0) >= timeout + 5:
            r = conn.execute("SELECT qjob_uid FROM qc_jobs WHERE state = 'queued' ORDER BY id LIMIT 1").fetchone()
            if r is None or r[0] in seen:
                break
            seen.add(r[0])
            out = run_job(r[0], agent_turn=agent_turn)
            ran.append({"qjob_uid": r[0], "verdict": out.get("verdict"), "error": out.get("error"),
                        "draft_status": out.get("draft_status")})
        return {"ran": ran, "recovered": recovered}
    finally:
        conn.close()


# ---------------------------------------------------------------- golden set (qc golden)
def golden_items() -> list:
    labels = read_json(os.path.join(GOLDEN_DIR, "labels.json"))
    items = []
    for gid, label in sorted(labels["labels"].items()):
        data = read_json(os.path.join(GOLDEN_DIR, "%s.json" % gid))
        items.append((gid, label, data))
    return items


def golden_packet(item: dict, nonce: str) -> tuple:
    """(packet text, sha256 of the draft text) for one golden item."""
    subject = item.get("subject")
    body = item.get("body") or ""
    text = ("Subject: %s\n\n%s" % (subject, body)) if subject else body
    sha = canon.sha256_text(text)
    rec = item.get("recipient") or {}
    values = {"nonce": nonce, "draft_sha256": sha, "channel": item["channel"], "recipient_json": review._j(rec),
              "research_facts_json": review._j(item.get("research_facts") or {}),
              "profile_facts_json": review._j(item.get("profile_facts") or {}),
              "lint_warnings_json": review._j([]),
              "subject_line_if_any": ("Subject: %s" % subject) if subject else "", "body": body}
    return review.render_packet(values), sha


def golden(model: str | None = None, agent_turn=None) -> dict:
    """Run every golden draft through the reviewer and compare the code decision with the label."""
    import secrets
    turn = agent_turn or _agent_turn
    cfg = settings()
    rows, agree = [], 0
    tmpdir = os.path.join(paths.state_dir(), "qc", "golden")
    os.makedirs(tmpdir, mode=0o700, exist_ok=True)
    items = golden_items()
    for gid, label, item in items:
        nonce = secrets.token_hex(8)
        text, sha = golden_packet(item, nonce)
        path = os.path.join(tmpdir, "%s.txt" % gid)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            res = turn(cfg["qc"]["review"]["agent"], "golden-" + nonce, path, int(cfg["qc"]["review"]["timeout_s"]))
        except Exception as exc:
            res = {"ok": False, "error": str(exc)}
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if res.get("ok"):
            parsed = review.parse_verdict(res.get("text") or res.get("raw") or "", nonce, sha)
            dec = review.decide(parsed["verdict"], item["channel"], cfg)
            got = "good" if dec["pass"] else "bad"
        else:
            got = "error"
        ok = got == label
        agree += 1 if ok else 0
        rows.append({"id": gid, "label": label, "got": got, "agree": ok})
    prompt_sha = canon.sha256_file(REVIEWER_PROMPT_FILE)
    model = model or cfg["qc"]["review"].get("model") or "unknown"
    stamp = now()
    value = "%d/%d|%s|%s|%s" % (agree, len(items), model, prompt_sha, stamp)
    return {"agreement": "%d/%d" % (agree, len(items)), "agree": agree, "total": len(items), "model": model,
            "prompt_sha256": prompt_sha, "golden_last": value, "items": rows}

"""Shared test helpers (owned by U1): temp repo home, database factory, fake clock, row factories.

    from tests.helpers import HomeTestCase

    class TestX(HomeTestCase):
        def test_y(self):
            cid = insert_company(self.conn)
            self.clock.advance(minutes=5)

Everything runs in a temp dir (paths.use_test_home); no network, nothing outside the temp dir. Row
factories insert plain rows with fictional placeholder data and return the new row id; they do not
run gate logic, so tests can build any ledger state the triggers allow.

Agent calls (CLI route design 5, 8): the guard runs every jobhunter agent call as
`<PY> -I jh.py --agent-proof <T> <args>` with a JH_AGENT_PROOF env proof, both single use, for one session.

    env = agent_env(paths.root(), "jobhunter-scout")                 # env carrier (fresh nonce)
    argv = agent_argv(paths.root(), "jobhunter-scout", ["searches", "due"])   # argv carrier in front
    rc, envelope = agent_cli("jobhunter-scout", ["searches", "due"])  # both, in process, as python -I
    rc, envelope = run_jh(argv, env)                                  # a real `python -I` child

agent_env/agent_argv write a test private/guard.key and cli_route (both carriers) when absent. In-process
calls must run under as_isolated() (sys.flags as `python -I` sets them; agent_cli does it); run_jh runs a
child `python -I` against the current test home and test clock with only the given environment.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import tests  # noqa: F401  (puts scripts/ on sys.path)
from jobhunter import canon, db, paths

DEFAULT_START = "2026-09-27T05:00:00Z"
LIVE = ("reserved", "armed", "sent", "failed_after_click", "unknown", "imported")


class FakeClock:
    """Freezes jobhunter.canon.now() (and everything built on it) until stop()."""

    def __init__(self, start: str = DEFAULT_START):
        self.set(start)

    def set(self, ts: str) -> str:
        canon.set_test_clock(ts)
        return canon.now()

    def now(self) -> str:
        return canon.now()

    def advance(self, seconds: float = 0, minutes: float = 0, hours: float = 0, days: float = 0) -> str:
        return self.set(canon.ts_add(canon.now(), seconds=seconds, minutes=minutes, hours=hours, days=days))

    def ago(self, seconds: float = 0, minutes: float = 0, hours: float = 0, days: float = 0) -> str:
        """Timestamp that lies the given time before now()."""
        return canon.ts_add(canon.now(), seconds=-seconds, minutes=-minutes, hours=-hours, days=-days)

    @staticmethod
    def stop() -> None:
        canon.set_test_clock(None)


def write_consent(sites=None, status: str = "granted", method: str = "manual_login") -> str:
    """private/consent.json of the current test home, in the owner's format (identity.load_consent), mode 0600.
    sites=None: every known site. Returns the path."""
    import json
    from jobhunter import identity
    names = list(identity.SITES) if sites is None else list(sites)
    ts = canon.now()
    rows = {}
    for site in names:
        rows[site] = {"site": site, "status": status, "method": method,
                      "domains": list(identity.CONSENT_SITES[site][1]), "chrome_profile": None,
                      "chrome_profile_name": None, "granted_at": ts if status == "granted" else None,
                      "revoked_at": ts if status == "revoked" else None,
                      "declined_at": ts if status == "declined" else None, "by": "owner"}
    path = paths.consent_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "updated_at": ts, "sites": rows}, fh)
    return path


def clear_consent() -> None:
    """Remove private/consent.json: no site has the owner's consent (the state of a new install)."""
    try:
        os.unlink(paths.consent_file())
    except FileNotFoundError:
        pass


class TempHome:
    """A temp install root with private/home.json, WS_ROOT folders and an initialised database.

    With consent=True (the default) private/consent.json grants every browser site, so tests of other
    rules reach them; consent tests pass consent=False (or call clear_consent / write_consent)."""

    def __init__(self, meta: dict | None = None, clock: str | None = DEFAULT_START, init_db: bool = True,
                 consent: bool = True):
        self.meta = meta
        self.clock_start = clock
        self.want_db = init_db
        self.want_consent = consent
        self.dir: str | None = None
        self.conn = None
        self.clock: FakeClock | None = None

    def start(self) -> "TempHome":
        self.dir = tempfile.mkdtemp(prefix="jh-test-")
        paths.use_test_home(self.dir)
        if self.clock_start:
            self.clock = FakeClock(self.clock_start)
        if self.want_consent:
            write_consent()
        if self.want_db:
            self.conn = db.init_db(self.meta)
        return self

    def stop(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        paths.clear_test_home()
        FakeClock.stop()
        if self.dir and os.path.isdir(self.dir):
            shutil.rmtree(self.dir, ignore_errors=True)

    __enter__ = start

    def __exit__(self, *exc):
        self.stop()

    @property
    def home(self) -> dict:
        return paths.home()

    def ws(self, role: str, *parts: str) -> str:
        """Path under WS_ROOT/<role>/ (folders are created)."""
        p = os.path.join(paths.ws_dir(role), *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def write_agent_file(self, role: str, name: str, text: str, sub: str = "work") -> str:
        p = self.ws(role, sub, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p


def make_home(meta: dict | None = None, clock: str | None = DEFAULT_START, consent: bool = True) -> TempHome:
    """Database factory: a started TempHome (call .stop() when done)."""
    return TempHome(meta=meta, clock=clock, consent=consent).start()


def clear_meta(conn, keys=None) -> None:
    """Delete meta rows directly (bypasses db.meta_set), for strict-fallback tests. keys=None deletes all."""
    if keys is None:
        conn.execute("DELETE FROM meta")
    else:
        conn.executemany("DELETE FROM meta WHERE key = ?", [(k,) for k in keys])


def raw_meta(conn, key: str, value: str) -> None:
    """Write a meta row directly, bypassing validation (for hostile-value tests)."""
    conn.execute("INSERT INTO meta (key, value, updated_at, updated_by) VALUES (?, ?, ?, 'system') "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value, canon.now()))


class HomeTestCase(unittest.TestCase):
    """unittest base class: self.home (TempHome), self.conn, self.clock."""
    meta_overrides: dict | None = None
    start_ts: str = DEFAULT_START
    browser_consent: bool = True

    def setUp(self):
        super().setUp()
        self.home = TempHome(meta=self.meta_overrides, clock=self.start_ts, consent=self.browser_consent).start()
        self.conn = self.home.conn
        self.clock = self.home.clock

    def tearDown(self):
        self.home.stop()
        super().tearDown()

    def assertDenied(self, code: str, fn, *args, **kwargs):
        """fn(*args) must raise errors.Denied with `code` (sqlite errors are mapped first)."""
        import sqlite3
        from jobhunter.errors import Denied, map_sqlite_error
        try:
            fn(*args, **kwargs)
        except Denied as d:
            self.assertEqual(d.code, code, "%s: %s" % (d.code, d.message))
            return d
        except sqlite3.DatabaseError as exc:
            d = map_sqlite_error(exc)
            self.assertEqual(d.code, code, "%s: %s" % (d.code, d.message))
            return d
        self.fail("expected Denied(%s)" % code)


# ---------------------------------------------------------------- agent identity (CLI route 5)
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
CHILD_PATH = "/usr/bin:/bin"


def default_session(agent_id: str) -> str:
    return "agent:%s:test" % agent_id


def _check_root(root) -> None:
    if root is not None and os.path.realpath(str(root)) != os.path.realpath(paths.root()):
        raise AssertionError("agent helpers work on the current test home only (%s)" % paths.root())
    if not paths.is_test_home():
        raise AssertionError("agent helpers need a test home (paths.use_test_home)")


def ensure_agent_setup(carriers=None) -> None:
    """A test private/guard.key and private/home.json cli_route.carriers (default argv and env) when absent;
    carriers given: always written."""
    from jobhunter import auth
    auth.create_guard_key()
    h = paths.home()
    route = h.get("cli_route") if isinstance(h.get("cli_route"), dict) else None
    if route is None or "carriers" not in route or carriers is not None:
        route = dict(route or {})
        route["carriers"] = list(carriers) if carriers is not None else ["argv", "env"]
        h["cli_route"] = route
        with open(paths.home_file(), "w", encoding="utf-8") as fh:
            json.dump(h, fh, indent=1, sort_keys=True)


def agent_env(root, agent_id: str, session: str | None = None) -> dict:
    """The environment the guard gives one exec of agent_id: OPENCLAW_SHELL, JH_AGENT_ID, JH_SESSION_KEY and a
    fresh version 2 JH_AGENT_PROOF (single use)."""
    from jobhunter import auth
    _check_root(root)
    ensure_agent_setup()
    session = session or default_session(agent_id)
    return {"OPENCLAW_SHELL": "exec", "JH_AGENT_ID": agent_id, "JH_SESSION_KEY": session,
            "JH_AGENT_PROOF": auth.env_proof(agent_id, session)}


def agent_argv(root, agent_id: str, argv: list, session: str | None = None) -> list:
    """`--agent-proof <T>` (fresh, single use, bound to argv and the session) followed by argv."""
    from jobhunter import auth
    _check_root(root)
    ensure_agent_setup()
    session = session or default_session(agent_id)
    rest = [str(t) for t in argv]
    return ["--agent-proof", auth.argv_proof(agent_id, rest, session)] + rest


class _IsolatedFlags:
    """sys.flags as `python -I` sets them; every other flag is the real one."""
    isolated = 1
    ignore_environment = 1
    no_user_site = 1

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def as_isolated():
    """While active, sys.flags reports the flags of `python -I` (in-process agent calls only)."""
    real = sys.flags
    sys.flags = _IsolatedFlags(real)
    try:
        yield
    finally:
        sys.flags = real


def _envelope(rc: int, text: str):
    text = (text or "").strip()
    last = text.splitlines()[-1] if text else ""
    try:
        return rc, json.loads(last)
    except ValueError:
        return rc, text


def agent_cli(agent_id: str, argv: list, stdin: str = "", session: str | None = None, env_extra: dict | None = None,
              modules=None, isolated: bool = True):
    """One jh.py call as agent_id in this process with both carriers (as_isolated when isolated)."""
    from jobhunter import cli
    env = agent_env(paths.root(), agent_id, session)
    env.update(env_extra or {})
    full = agent_argv(paths.root(), agent_id, argv, session)
    out = io.StringIO()
    kwargs = {"env": env, "stdin": io.StringIO(stdin), "stdout": out}
    if modules is not None:
        kwargs["modules"] = modules
    with (as_isolated() if isolated else contextlib.nullcontext()):
        rc = cli.main(full, **kwargs)
    return _envelope(rc, out.getvalue())


_CHILD = """
import io, json, sys
spec = json.loads(sys.stdin.read())
sys.path.insert(0, spec["scripts"])
from jobhunter import canon, paths
paths.use_test_home(spec["root"])
if spec.get("now"):
    canon.set_test_clock(spec["now"])
from jobhunter import cli
out = io.StringIO()
rc = cli.main(spec["argv"], env=spec["env"], stdin=io.StringIO(spec.get("stdin") or ""), stdout=out)
sys.stdout.write(json.dumps({"rc": rc, "out": out.getvalue(), "isolated": sys.flags.isolated}))
"""


def start_jh(argv: list, env: dict | None = None, stdin: str = "", isolated: bool = True, cwd: str | None = None):
    """Start jh.py's cli.main in a child interpreter (`python -I` when isolated) against the current test home
    and test clock; the child gets only `env` (plus PATH) as its environment and as cli.main's env. Returns the
    Popen; finish_jh() reads the result."""
    _check_root(None)
    env = {str(k): str(v) for k, v in (env or {}).items()}
    spec = {"scripts": SCRIPTS_DIR, "root": paths.root(), "now": canon.now() if getattr(canon, "_clock_override", None) is not None else None,
            "argv": [str(t) for t in argv], "env": env, "stdin": stdin}
    child_env = dict(env)
    child_env.setdefault("PATH", CHILD_PATH)
    cmd = [sys.executable] + (["-I"] if isolated else []) + ["-c", _CHILD]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=child_env, cwd=cwd or paths.root(), universal_newlines=True)
    proc._jh_spec = json.dumps(spec)
    return proc


def finish_jh(proc, timeout: int = 120):
    """(rc, envelope or text, isolated flag of the child) of a start_jh() child."""
    out, err = proc.communicate(proc._jh_spec, timeout=timeout)
    try:
        res = json.loads(out)
    except ValueError:
        raise AssertionError("jh child failed (exit %s): %s %s" % (proc.returncode, out[-2000:], err[-2000:]))
    rc, envelope = _envelope(res["rc"], res["out"])
    return rc, envelope, res["isolated"]


def run_jh(argv: list, env: dict | None = None, stdin: str = "", isolated: bool = True, timeout: int = 120,
           cwd: str | None = None):
    """Run one jh.py call in a child (`python -I` by default) against the current test home and clock.
    Returns (exit code, the JSON envelope or the raw text)."""
    rc, envelope, _iso = finish_jh(start_jh(argv, env, stdin, isolated, cwd), timeout)
    return rc, envelope


# ---------------------------------------------------------------- row factories
def _ins(conn, table: str, row: dict) -> int:
    cols = list(row)
    cur = conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(cols), ", ".join("?" for _ in cols)),
                       [row[c] for c in cols])
    return cur.lastrowid


def insert_company(conn, name: str = "Kestrel Commerce", domain: str | None = "kestrel.example",
                   is_agency: int = 0, contact_state: str = "none", merged_into: int | None = None,
                   uid: str | None = None) -> int:
    ts = canon.now()
    return _ins(conn, "companies", {
        "company_uid": uid or canon.new_uid("K"), "display_name": name, "domain": domain, "is_agency": is_agency,
        "agency_source": "bundled_list" if is_agency else None, "contact_state": contact_state,
        "merged_into": merged_into, "created_at": ts, "updated_at": ts})


def insert_contact(conn, company_id: int | None = None, full_name: str = "Alex Rivera",
                   email: str | None = "alex.rivera@kestrel.example", role_type: str = "hiring_manager",
                   linkedin_url: str | None = None, do_not_contact: int = 0, merged_into: int | None = None,
                   uid: str | None = None) -> int:
    ts = canon.now()
    return _ins(conn, "contacts", {
        "contact_uid": uid or canon.new_uid("P"), "full_name": full_name,
        "first_name": full_name.split()[0] if full_name else None, "company_id": company_id,
        "role_type": role_type, "email": email, "linkedin_url": linkedin_url, "do_not_contact": do_not_contact,
        "merged_into": merged_into, "created_at": ts, "updated_at": ts})


def insert_job(conn, company_id: int | None = None, title: str = "Data Analyst", status: str = "new",
               source: str = "greenhouse", url: str | None = None, uid: str | None = None) -> int:
    ts = canon.now()
    uid = uid or canon.new_uid("J")
    url = url or "https://boards.example.com/jobs/%s" % uid
    return _ins(conn, "jobs", {
        "job_uid": uid, "canonical_key": "url:" + canon.sha256_text(url)[:40], "fingerprint": uid.lower(),
        "source": source, "discovered_via": "api", "source_url": url, "company_id": company_id,
        "company_name_raw": "Kestrel Commerce", "title": title, "norm_title": title.lower(),
        "role_key": title.lower().replace(" ", "_"), "discovered_at": ts, "status": status,
        "created_at": ts, "updated_at": ts})


def insert_draft(conn, kind: str = "cold_email", status: str = "approved", company_id: int | None = None,
                 contact_id: int | None = None, job_id: int | None = None, thread_key: str | None = None,
                 subject: str | None = "Hello", body: str = "Hi Alex,\n\nA short note.\n\nThanks,",
                 send_route: str = "mailer", channel: str = "email_cold", uid: str | None = None) -> int:
    ts = canon.now()
    text = canon.canonical_send_text(kind, subject, body, None, None) if kind in canon.EMAIL_KINDS | \
        canon.BODY_KINDS else canon.canonical_send_text(kind, None, None, [], None)
    approved = status in ("approved", "sent")
    return _ins(conn, "drafts", {
        "draft_uid": uid or canon.new_uid("D"), "kind": kind, "channel": channel, "send_route": send_route,
        "job_id": job_id, "contact_id": contact_id, "company_id": company_id, "thread_key": thread_key,
        "subject": subject, "body": body, "payload_json": "{}", "text_sha256": canon.sha256_text(text),
        "status": status, "approved_by": "human:cli" if approved else None, "approved_at": ts if approved else None,
        "created_at": ts, "updated_at": ts})


def insert_precheck(conn, kind: str = "cold_email", platform: str = "gmail", result: str = "clear",
                    contact_id: int | None = None, company_id: int | None = None, job_id: int | None = None,
                    source: str = "code_imap") -> int:
    return _ins(conn, "prechecks", {
        "kind": kind, "platform": platform, "source": source, "contact_id": contact_id, "company_id": company_id,
        "job_id": job_id, "result": result, "checks_json": "[]", "created_at": canon.now()})


def default_first_touch(conn, kind: str, contact_id: int | None) -> int:
    """The first_touch value code computes (mirrors t_first_touch_rules)."""
    if kind in ("cold_email", "li_invite", "inmail"):
        return 1
    if kind == "application_email":
        if contact_id is None:
            return 0
        row = conn.execute("SELECT role_type FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return 0 if (row and row[0] == "role_inbox") else 1
    if kind == "li_message":
        row = conn.execute("SELECT 1 FROM threads WHERE contact_id = ? AND channel = 'linkedin' "
                           "AND state = 'invite_accepted'", (contact_id,)).fetchone()
        return 0 if row else 1
    return 0


_PLATFORM_BY_KIND = {"cold_email": "gmail", "followup_email": "gmail", "application_email": "gmail",
                     "application": "greenhouse", "li_invite": "linkedin", "li_message": "linkedin",
                     "li_followup": "linkedin", "inmail": "linkedin", "li_withdraw": "linkedin",
                     "referral_ask": "gmail"}


def insert_action(conn, kind: str = "cold_email", status: str = "sent", company_id: int | None = None,
                  contact_id: int | None = None, job_id: int | None = None, thread_key: str | None = None,
                  recipient: str | None = None, draft_id: int | None = None, precheck_id: int | None = None,
                  first_touch: int | None = None, li_note: int = 0, li_msg_seq: int | None = None,
                  agent_id: str | None = None, reserved_at: str | None = None, route: str | None = None,
                  platform: str | None = None, fail_reason: str | None = None, token: str | None = None) -> int:
    ts = canon.now()
    reserved_at = reserved_at or ts
    try:
        expires_at = canon.ts_add(reserved_at, minutes=30)
    except ValueError:   # a malformed reserved_at, for the CHECK test
        expires_at = canon.ts_add(ts, minutes=30)
    if first_touch is None:
        first_touch = default_first_touch(conn, kind, contact_id)
    platform = platform or _PLATFORM_BY_KIND.get(kind, "gmail")
    if route is None:
        route = "mailer" if platform == "gmail" else "browser"
    return _ins(conn, "actions", {
        "token": token or canon.new_token(), "kind": kind, "route": route, "first_touch": first_touch,
        "li_note": li_note, "li_msg_seq": li_msg_seq, "platform": platform, "agent_id": agent_id,
        "contact_id": contact_id, "company_id": company_id, "job_id": job_id, "thread_key": thread_key,
        "recipient": recipient, "draft_id": draft_id, "precheck_id": precheck_id, "status": status,
        "fail_reason": fail_reason, "reserved_at": reserved_at, "expires_at": expires_at,
        "sent_at": reserved_at if status in ("sent", "imported") else None, "created_at": ts, "updated_at": ts})


def insert_thread(conn, first_action_id: int, contact_id: int | None = None, company_id: int | None = None,
                  channel: str = "email", state: str = "open", thread_key: str | None = None,
                  job_id: int | None = None) -> int:
    ts = canon.now()
    if thread_key is None:
        row = conn.execute("SELECT token FROM actions WHERE id = ?", (first_action_id,)).fetchone()
        thread_key = ("em:" + row[0]) if channel == "email" else ("li:" + canon.new_uid("P"))
    return _ins(conn, "threads", {
        "thread_key": thread_key, "channel": channel, "contact_id": contact_id, "company_id": company_id,
        "job_id": job_id, "first_action_id": first_action_id, "state": state, "created_at": ts, "updated_at": ts})


def insert_cycle(conn, lane: str = "outreach", agent_id: str | None = None, status: str = "running",
                 started_at: str | None = None, ended_at: str | None = None, cycle_id: str | None = None) -> str:
    """A cycles row (gate.reserve takes an agent's running cycle). agent_id defaults to the lane's agent."""
    from jobhunter.cycles import LANE_AGENTS
    started_at = started_at or canon.now()
    cycle_id = cycle_id or canon.new_cycle_id(started_at)
    _ins(conn, "cycles", {"cycle_id": cycle_id, "lane": lane, "agent_id": agent_id or LANE_AGENTS.get(lane),
                          "started_at": started_at, "ended_at": ended_at, "status": status})
    return cycle_id


# ---------------------------------------------------------------- secrets that must never be stored or shown
def _secret_forms(secrets) -> list:
    from urllib.parse import quote
    out = []
    for s in secrets:
        for f in (s, quote(s, safe=""), quote(s), json.dumps(s)[1:-1]):
            if f and f not in out:
                out.append(f)
    return out


def secret_hits(secrets, paths_=(), texts=(), argv=(), conn=None) -> list:
    """Where any of the secrets (or their URL-encoded or JSON-escaped forms) appears: every file under the given
    paths (logs, events, guard and token logs, state), the given texts (stdout, stderr, Sheet rows,
    notifications), every recorded argv, and the database dumped with iterdump(). [] when nowhere."""
    forms = _secret_forms(secrets)
    hits = []

    def scan(where: str, text: str) -> None:
        for f in forms:
            if f in text:
                hits.append("%s: %s" % (where, "secret %d" % forms.index(f)))
    for p in paths_:
        if os.path.isfile(p):
            files = [p]
        else:
            files = [os.path.join(d, n) for d, _s, ns in os.walk(p) for n in ns]
        for fp in files:
            try:
                with open(fp, "rb") as fh:
                    scan(fp, fh.read().decode("utf-8", "replace"))
            except OSError:
                pass
    for i, t in enumerate(texts):
        scan("text %d" % i, t if isinstance(t, str) else json.dumps(t, default=str))
    for i, a in enumerate(argv):
        scan("argv %d" % i, " ".join(str(x) for x in a))
    if conn is not None:
        scan("database", "\n".join(conn.iterdump()))
    return hits


def assert_no_secret(test, secrets, paths_=(), texts=(), argv=(), conn=None) -> None:
    test.assertEqual(secret_hits(secrets, paths_, texts, argv, conn), [])

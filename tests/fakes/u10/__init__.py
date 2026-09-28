"""U10 test support: a socket guard, a fake transport that answers from the recorded fixtures in
tests/fixtures/enrich, a fake monotonic clock, a fake DNS under the real emailcheck.mx_for_domain, and a test
case base class with row factories. Only the network is faked: the U1 and U6 functions U10 calls
(gate.dedup_check, contacts.set_address, emailcheck.mx_for_domain / guess_blocked / pattern_evidence /
render_pattern, the hooks) are the real ones. Fictional people at reserved .example domains only; fake keys only.
"""
from __future__ import annotations

import json
import os
import socket
from unittest import mock

import tests  # noqa: F401
from jobhunter import canon, db, emailcheck, paths
from jobhunter.enrich import cache, keystore, settings, transport
from tests.helpers import HomeTestCase

FIXTURES = os.path.join(paths.REPO, "tests", "fixtures", "enrich")
FAKE_KEY = "FAKE-KEY-0000000000000000"
FAKE_SECRET = "FAKE-SECRET-000000000000"
HOST_PROVIDER = {"api.prospeo.io": "prospeo", "api.hunter.io": "hunter", "api.tomba.io": "tomba",
                 "api.getprospect.com": "getprospect", "api.anymailfinder.com": "anymailfinder",
                 "app.findymail.com": "findymail", "api.apollo.io": "apollo", "api.zerobounce.net": "zerobounce"}
ALL_KEYS = {"prospeo": {"api_key": FAKE_KEY}, "hunter": {"api_key": FAKE_KEY},
            "tomba": {"key": FAKE_KEY, "secret": FAKE_SECRET}, "getprospect": {"api_key": FAKE_KEY},
            "anymailfinder": {"api_key": FAKE_KEY}, "findymail": {"api_key": FAKE_KEY},
            "apollo": {"api_key": FAKE_KEY}, "zerobounce": {"api_key": FAKE_KEY}}


# ---------------------------------------------------------------- network guard
class NetworkBlocked(AssertionError):
    pass


def _blocked(*_a, **_k):
    raise NetworkBlocked("a U10 test tried to open a network connection")


_patches: list = []


def install_guard() -> None:
    """Module-level guard (setUpModule): no socket may connect while U10 tests run."""
    if _patches:
        return
    for target, attr in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection")):
        p = mock.patch.object(target, attr, _blocked)
        p.start()
        _patches.append(p)


def remove_guard() -> None:
    while _patches:
        _patches.pop().stop()


# ---------------------------------------------------------------- fixtures and fake transport
def load_fixture(provider: str, name: str) -> dict:
    with open(os.path.join(FIXTURES, provider, name + ".json"), "r", encoding="ascii") as fh:
        return json.load(fh)


def fixture_names(provider: str) -> list:
    return sorted(f[:-5] for f in os.listdir(os.path.join(FIXTURES, provider)) if f.endswith(".json"))


def fixture_variant(provider: str, name: str, edit) -> dict:
    """A copy of a recorded fixture changed by edit(fx) (for example another fictional address), for
    FakeTransport.add; the files on disk stay as recorded."""
    fx = load_fixture(provider, name)
    edit(fx)
    return fx


def fixture_response(fx: dict) -> tuple:
    if "body_text" in fx:
        body = fx["body_text"].encode("utf-8")
    else:
        body = json.dumps(fx["body"]).encode("utf-8")
    headers = {str(k).lower(): str(v) for k, v in (fx.get("headers") or {}).items()}
    return int(fx["status"]), headers, body


class MonoClock:
    """Fake monotonic clock for deadlines; sleep() advances it."""

    def __init__(self, start: float = 1000.0):
        self.t = float(start)
        self.slept = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s

    def advance(self, s: float) -> None:
        self.t += s


class FakeTransport:
    """Answers each provider from a queue of fixture names (or 'raise:<kind>' for a transport error, or a
    fixture dict from fixture_variant).
    Records every request as built by transport.build (url, data, headers) for assertions."""

    def __init__(self, clock: MonoClock | None = None, cost_s: float = 1.0):
        self.queue: dict = {}
        self.requests: list = []
        self.during: list = []      # callables(provider) run while a request is "on the wire"
        self.clock = clock
        self.cost_s = cost_s

    def add(self, provider: str, *names: str) -> "FakeTransport":
        self.queue.setdefault(provider, []).extend(names)
        return self

    def calls(self, provider: str | None = None) -> list:
        return [r for r in self.requests if provider is None or r["provider"] == provider]

    def request(self, req, key, timeout_s):
        provider = HOST_PROVIDER.get(req.host)
        url, data, headers = transport.build(req, key)
        self.requests.append({"provider": provider, "req": req, "url": url, "data": data, "headers": headers,
                              "timeout_s": timeout_s})
        if self.clock is not None:
            self.clock.advance(self.cost_s)
        for fn in self.during:
            fn(provider)
        items = self.queue.get(provider) or []
        if not items:
            raise AssertionError("unexpected call to %s" % provider)
        item = items.pop(0)
        if isinstance(item, dict):          # a fixture edited by the test (fixture_variant)
            return fixture_response(item)
        if item.startswith("raise:"):
            raise transport.TransportError(item[6:])
        return fixture_response(load_fixture(provider, item))


# ---------------------------------------------------------------- fake DNS (the only U6 part that is faked)
class FakeDns:
    """Stands in for emailcheck.lookup_mx, the network under the real emailcheck.mx_for_domain. Every domain
    has an MX host unless it is in `no_mx`; a domain in `failing` raises LookupFailed (DNS did not answer)."""

    def __init__(self):
        self.no_mx: set = set()
        self.failing: set = set()
        self.calls: list = []

    def lookup_mx(self, domain):
        from jobhunter import emailcheck
        self.calls.append(domain)
        if domain in self.failing:
            raise emailcheck.LookupFailed("fake DNS timeout")
        return [] if domain in self.no_mx else ["mx.%s" % domain]


# ---------------------------------------------------------------- base test case
class EnrichTestCase(HomeTestCase):
    """HomeTestCase with an in-memory key store, enrich enabled, the budget meta rows written, the fake DNS
    under emailcheck.mx_for_domain, and a fake transport and clock ready."""
    enrich_block: dict = {"enabled": True}
    with_keys = True

    def setUp(self):
        super().setUp()
        self.keys = {p: dict(v) for p, v in ALL_KEYS.items()} if self.with_keys else {}
        keystore.use_test_backend(self.keys)
        settings.use_test_overrides(self.enrich_block)
        self.dns = FakeDns()
        emailcheck.clear_mx_cache()
        self._dns = mock.patch.object(emailcheck, "lookup_mx", self.dns.lookup_mx)
        self._dns.start()
        self.mono = MonoClock()
        self.fake = FakeTransport(self.mono)
        self.write_meta()

    def tearDown(self):
        self._dns.stop()
        emailcheck.clear_mx_cache()
        settings.use_test_overrides(None)
        keystore.use_test_backend(None)
        keystore.set_runner(None)
        keystore.set_backend(None)
        super().tearDown()

    def settings(self, block: dict | None = None) -> dict:
        if block is not None:
            settings.use_test_overrides(block)
            self.write_meta()
        return settings.load(self.conn)

    def write_meta(self) -> dict:
        with db.tx(self.conn):
            return settings.write_meta(self.conn, settings.load(self.conn))

    # -- factories ---------------------------------------------------------
    def make_target(self, full_name: str = "Example Person", role_type: str = "hiring_manager",
                    domain: str | None = "kestrel.example", job_status: str = "eligible", li_slug: str | None = None,
                    company_id: int | None = None, locale: str | None = None, email: str | None = None) -> dict:
        from tests.helpers import insert_company, insert_contact, insert_job
        with db.tx(self.conn):
            if company_id is None:
                company_id = insert_company(self.conn, name="Kestrel Commerce", domain=domain)
                if domain:
                    self.conn.execute("INSERT OR IGNORE INTO company_aliases (alias_key, company_id, kind, source, "
                                      "created_at) VALUES (?, ?, 'dom', 'careers_url', ?)",
                                      ("dom:" + domain, company_id, canon.now()))
            cid = insert_contact(self.conn, company_id=company_id, full_name=full_name, email=email,
                                 role_type=role_type)
            if li_slug or locale:
                self.conn.execute("UPDATE contacts SET li_slug = ?, locale = ? WHERE id = ?", (li_slug, locale, cid))
            if li_slug:
                self.conn.execute("INSERT INTO contact_keys (key, contact_id, kind, created_at) VALUES (?, ?, "
                                  "'li_slug', ?)", ("li:" + li_slug, cid, canon.now()))
            jid = insert_job(self.conn, company_id=company_id, status=job_status)
            self.conn.execute("INSERT INTO job_hiring_team (job_id, contact_id, relation) VALUES (?, ?, ?)",
                              (jid, cid, "recruiter" if role_type == "recruiter" else "hiring_manager"))
        uid = self.conn.execute("SELECT contact_uid FROM contacts WHERE id = ?", (cid,)).fetchone()[0]
        return {"contact_id": cid, "company_id": company_id, "job_id": jid, "contact_uid": uid}

    def publish_pattern(self, company_id: int, domain: str = "kestrel.example",
                        people=(("Jordan", "Sample"), ("Casey", "Example")),
                        url: str = "https://kestrel.example/team") -> list:
        """Published addresses of named people at the domain, {first}.{last}: a stored contact each plus a
        research fact from an https page that shows the address. The real U6 emailcheck.pattern_evidence
        reads two of them as a proven pattern (the grade B rule)."""
        from tests.helpers import insert_contact
        out = []
        ts = canon.now()
        with db.tx(self.conn):
            for first, last in people:
                addr = "%s.%s@%s" % (first.lower(), last.lower(), domain)
                cid = insert_contact(self.conn, company_id=company_id, full_name="%s %s" % (first, last), email=addr)
                self.conn.execute("UPDATE contacts SET email_grade = 'A', email_source = 'published' WHERE id = ?",
                                  (cid,))
                self.conn.execute(
                    "INSERT INTO research_facts (fact_uid, subject_kind, subject_id, text, snippet, source_type, "
                    "source_url, retrieved_at, created_at) VALUES (?, 'person', ?, ?, ?, 'company_site', ?, ?, ?)",
                    (canon.new_uid("R"), cid, "Team page lists %s %s." % (first, last),
                     "%s %s, %s" % (first, last, addr), url, ts, ts))
                out.append(addr)
        return out

    def provider_contact(self, email: str, provider: str = "prospeo", full_name: str | None = None,
                         company_id: int | None = None, grade: str = "B") -> dict:
        """A target whose address came from `provider` (request, settled call, contact fields), as the chain
        leaves it after a hit. Budget meta rows must allow the call."""
        t = self.make_target(full_name=full_name or "Example %s" % email.split("@")[0].replace(".", " ").title(),
                             company_id=company_id)
        t.update(self.provider_address(t["contact_id"], t["company_id"], email, provider=provider, grade=grade))
        self.clock.advance(seconds=10)
        return t

    def provider_address(self, contact_id: int, company_id: int, email: str, provider: str = "prospeo",
                         grade: str = "B") -> dict:
        """Give an existing contact a provider-found address: a finished request, a settled hit call and the
        contact fields the chain writes (email_source 'provider', the call id, MX ok)."""
        ts = canon.now()
        with db.tx(self.conn):
            rid = self.conn.execute(
                "INSERT INTO enrich_requests (request_uid, contact_id, company_id, status, grade, sendable, created_by, "
                "started_at, finished_at, created_at, updated_at) VALUES (?, ?, ?, 'found', ?, 1, 'system', ?, ?, ?, ?)",
                (canon.new_uid("E"), contact_id, company_id, grade, ts, ts, ts, ts)).lastrowid
            call = self.conn.execute(
                "INSERT INTO enrich_calls (request_id, provider, op, outcome, started_at, credits_charged, created_at, "
                "updated_at) VALUES (?, ?, 'find_name_domain', 'inflight', ?, 1, ?, ?)",
                (rid, provider, ts, ts, ts)).lastrowid
            self.conn.execute("UPDATE enrich_calls SET outcome = 'hit', finished_at = ?, email = ?, email_domain = ?, "
                              "verification = 'valid', grade_hint = ?, source_url = 'https://kestrel.example/team', "
                              "source_urls_json = '[\"https://kestrel.example/team\"]' WHERE id = ?",
                              (ts, email, email.split("@")[1], grade, call))
            self.conn.execute("UPDATE enrich_requests SET result_call_id = ? WHERE id = ?", (call, rid))
            self.conn.execute("UPDATE contacts SET email = ?, email_grade = ?, email_source = 'provider', "
                              "email_enrich_call_id = ?, email_mx_ok = 1 WHERE id = ?", (email, grade, call, contact_id))
        return {"request_id": rid, "call_id": call, "email": email}

    def run_find(self, contact_id: int, caller="jobhunter-outreach", **kw):
        from jobhunter.enrich import chain
        kw.setdefault("transport", self.fake)
        kw.setdefault("clock", self.mono)
        return chain.run_find(self.conn, contact_id, caller=caller, **kw)

    def contact(self, cid: int):
        return self.conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone()

    def calls(self) -> list:
        return [dict(r) for r in self.conn.execute("SELECT * FROM enrich_calls ORDER BY id")]

    def requests(self) -> list:
        return [dict(r) for r in self.conn.execute("SELECT * FROM enrich_requests ORDER BY id")]

    def breaker(self, scope: str):
        return self.conn.execute("SELECT * FROM breakers WHERE scope = ?", (scope,)).fetchone()


def lock_name(uid: str) -> str:
    return cache.lock_name(uid)

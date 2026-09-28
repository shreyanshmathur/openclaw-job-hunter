"""Setup, config, authority, identity commands [U1] (design 3.4 "Setup, config, authority")."""
from __future__ import annotations

import json
import os
import shutil
import sys

from jobhunter import auth, companies, config, db, gate, keys, paths, people
from jobhunter.canon import new_uid, now, seconds_between
from jobhunter.commands import Result, add_command
from jobhunter.errors import Denied

APPROVAL_ACK = "I accept that messages that pass QC are sent without my review"
LINKEDIN_ACK = "I understand LinkedIn may restrict my account"


# ---------------------------------------------------------------- shared helpers (used by the other U1 modules)
def uid_to_id(conn, table: str, col: str, uid: str | None, what: str):
    if uid is None:
        return None
    row = conn.execute("SELECT id FROM %s WHERE %s = ?" % (table, col), (uid,)).fetchone()
    if row is None:
        raise Denied("E_NOT_FOUND", "no %s %s" % (what, uid))
    return row[0]


def job_id(conn, uid):
    return uid_to_id(conn, "jobs", "job_uid", uid, "job")


def contact_id(conn, uid):
    return uid_to_id(conn, "contacts", "contact_uid", uid, "contact")


def company_id(conn, uid):
    return uid_to_id(conn, "companies", "company_uid", uid, "company")


def draft_id(conn, uid):
    if uid and len(uid) == 4:
        row = conn.execute("SELECT draft_id FROM approval_codes WHERE code = ? ORDER BY issued_at DESC LIMIT 1",
                           (uid,)).fetchone()
        if row:
            return row[0]
    return uid_to_id(conn, "drafts", "draft_uid", uid, "draft")


def agent_of(ctx) -> str:
    if ctx.caller.cls == "agent":
        return ctx.caller.agent_id or ""
    return ctx.caller.cls


def by_of(ctx) -> str:
    return {"human": "human:cli", "chat": "human:chat", "agent": ctx.caller.agent_id or "agent"}.get(
        ctx.caller.cls, "system")


# ---------------------------------------------------------------- init
def _given_binary(env: dict, name: str) -> str | None:
    """An absolute executable path handed over by install.sh (JH_OC_BIN, JH_PYTHON), or None when unset."""
    v = (env.get(name) or "").strip()
    if not v:
        return None
    if not os.path.isabs(v) or not os.path.isfile(v) or not os.access(v, os.X_OK):
        raise Denied("E_VALIDATION", "%s must be the absolute path of an executable file: %r" % (name, v))
    return v


def _write_home(env: dict | None = None) -> tuple[dict, bool]:
    """private/home.json on first init. The OpenClaw binary, profile and Python come from install.sh
    (JH_OC_BIN, JH_OC_PROFILE, JH_PYTHON) when it set them, else from PATH and this interpreter."""
    env = os.environ if env is None else env
    path = paths.home_file()
    if os.path.exists(path):
        return paths.home(), False
    oc = _given_binary(env, "JH_OC_BIN") or shutil.which("openclaw") or \
        os.path.join(os.path.expanduser("~"), ".openclaw", "bin", "openclaw")
    py = _given_binary(env, "JH_PYTHON") or sys.executable
    iid = new_uid("I", 8)
    h = {"install_id": iid, "repo": paths.root(), "db_path": paths.db_path(),
         "ws_root": os.path.join(os.path.expanduser("~"), ".openclaw-job-hunter", iid, "workspaces"),
         "oc_bin": oc, "oc_profile": env.get("JH_OC_PROFILE", "") or "", "python": py,
         "created_at": now()}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(h, fh, indent=1, sort_keys=True)
    return h, True


def cmd_init(args, ctx):
    created = []
    for d in (paths.private_dir(), paths.state_dir(), paths.logs_dir(), paths.exports_dir(), paths.guard_dir()):
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            created.append(d)
    for d in (paths.private_dir(), paths.state_dir(), paths.logs_dir()):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    h, made = _write_home(getattr(ctx, "env", None))
    if made:
        created.append(paths.home_file())
    paths.check_home_binding(h)
    for role in paths.ROLES:
        for sub in ("work", "inbox"):
            p = os.path.join(h["ws_root"], role, sub)
            if not os.path.isdir(p):
                os.makedirs(p, mode=0o700, exist_ok=True)
                created.append(p)
    if auth.create_guard_key():
        created.append(auth.guard_key_file())
    cfg_file = config.config_file()
    if not os.path.exists(cfg_file):
        shutil.copyfile(os.path.join(paths.REPO, "config.example.json"), cfg_file)
        os.chmod(cfg_file, 0o600)
        created.append(cfg_file)
    ex_dir = os.path.join(paths.REPO, "private.example")
    if os.path.isdir(ex_dir):
        for name in sorted(os.listdir(ex_dir)):
            if ".example." not in name:
                continue
            target_dir = os.path.join(paths.private_dir(), "examples") if args.force_examples else paths.private_dir()
            os.makedirs(target_dir, exist_ok=True)
            dst = os.path.join(target_dir, name.replace(".example.", ".", 1))
            if os.path.exists(dst) and not args.force_examples:
                continue
            shutil.copyfile(os.path.join(ex_dir, name), dst)
            os.chmod(dst, 0o600)
            created.append(dst)
    conn = db.init_db()
    ctx._conns.append(conn)
    with db.tx(conn):
        applied = config.apply(conn)
    return {"created": created, "db": paths.db_path(), "schema_version": db.schema_version(conn),
            "install_id": h["install_id"], "ws_root": h["ws_root"], "meta": applied["meta"]}


def cmd_selftest(args, ctx):
    from jobhunter import selftest
    checks = selftest.run_checks(offline=args.offline)
    critical = ("db", "meta", "migrations", "trigger fixtures", "keys fixtures", "config clamp")
    bad = [c["name"] for c in checks if not c["ok"] and c["name"] in critical]
    return Result(data={"checks": checks, "failed": [c["name"] for c in checks if not c["ok"]]},
                  code="E_INTERNAL" if bad else "OK",
                  message="self test failed: %s" % ", ".join(bad) if bad else "self test passed")


def cmd_home_show(args, ctx):
    h = paths.home()
    hb = gate.guard_heartbeat()
    return {"repo": h.get("repo"), "db_path": h.get("db_path"), "install_id": h.get("install_id"),
            "ws_root": h.get("ws_root"), "oc_profile": h.get("oc_profile", ""), "guard_heartbeat_age_s": hb["age_s"],
            "guard_fresh": hb["fresh"], "env_warnings": paths.env_warnings(ctx.env)}


# ---------------------------------------------------------------- config
def cmd_config_validate(args, ctx):
    res = config.validate(ctx.connect(write=False))
    if res["errors"]:
        return Result(data=res, code="E_CONFIG_INVALID", message="; ".join(res["errors"]))
    return res


def cmd_config_show(args, ctx):
    if args.effective:
        return {"config": config.load(ctx.connect(write=False)), "effective": True}
    return {"config": config.read_file() or config.hardmax.defaults(), "effective": False}


def cmd_config_apply(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        return config.apply(conn)


def cmd_config_lower(args, ctx):
    conn = ctx.connect()
    res = config.lower(args.path, args.value, conn)
    with db.tx(conn):
        config.apply(conn)
    return Result(data=res, message="%s lowered to %s" % (res["path"], json.dumps(res["new"])))


def cmd_config_raise(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        res = config.raise_(conn, args.path, args.value)
        config.apply(conn)
    return Result(data=res, message="%s raised to %s%s" % (res["path"], json.dumps(res["new"]),
                                                          " (clamped to the hard limit)" if res.get("clamped") else ""))


# ---------------------------------------------------------------- authority
def _golden_ok(conn, cfg: dict) -> tuple[bool, str]:
    row = conn.execute("SELECT value FROM meta WHERE key = 'golden_last'").fetchone()
    if not row:
        return False, "run ./jobhunter qc golden first"
    parts = row[0].split("|")
    if len(parts) != 4:
        return False, "meta.golden_last is malformed"
    score, model, sha, ts = parts
    try:
        n = int(score.split("/")[0])
        age = seconds_between(ts, now())
    except (ValueError, IndexError):
        return False, "meta.golden_last is malformed"
    need = int(cfg["qc"]["golden_min_agreement"])
    cur_sha = db.meta_get(conn, "reviewer_prompt_sha256")
    cur_model = db.meta_get(conn, "reviewer_model")
    if n < need:
        return False, "golden set agreement %d/20 is below %d/20" % (n, need)
    if not cur_sha or sha != cur_sha:
        return False, "the golden run used another reviewer prompt; run ./jobhunter qc golden again"
    if cur_model and model != cur_model:
        return False, "the golden run used another reviewer model"
    if age > 14 * 86400 or age < 0:
        return False, "the golden run is older than 14 days"
    return True, "%d/20" % n


def cmd_approval_set(args, ctx):
    conn = ctx.connect()
    cfg = config.load(conn)
    if args.mode == "human":
        with db.tx(conn):
            db.meta_set(conn, "approval_mode", "human", "human")
        return {"mode": "human"}
    if (args.ack or "").strip() != APPROVAL_ACK:
        raise Denied("E_PRECONDITION", 'type the sentence exactly: --ack "%s"' % APPROVAL_ACK)
    ok, why = _golden_ok(conn, cfg)
    if not ok:
        raise Denied("E_PRECONDITION", why)
    with db.tx(conn):
        db.meta_set(conn, "approval_mode", "auto", "human")
    eff = config.load(conn)["approval"]["mode"]
    return Result(data={"mode": "auto", "effective": eff, "golden": why},
                  message="approval mode auto" + ("" if eff == "auto" else " (private/config.json approval.mode keeps "
                                                   "it human)"))


def cmd_tier_set(args, ctx):
    from jobhunter import ceilings
    conn = ctx.connect()
    if args.tier == "moderate":
        el = ceilings.tier_eligibility(conn, args.platform)
        if not el["eligible"]:
            raise Denied("E_PRECONDITION", "not eligible for moderate: " + "; ".join(el["reasons"]), data=el)
    with db.tx(conn):
        db.meta_set(conn, "tier_" + args.platform, args.tier, "human")
    return {"platform": args.platform, "tier": args.tier}


def cmd_linkedin_enable(args, ctx):
    from jobhunter import breakers
    if (args.ack or "").strip() != LINKEDIN_ACK:
        raise Denied("E_PRECONDITION", 'type the sentence exactly: --ack "%s"' % LINKEDIN_ACK)
    conn = ctx.connect()
    with db.tx(conn):
        db.meta_set(conn, "channel_linkedin_enabled", "1", "human")
        db.meta_set(conn, "linkedin_tos_ack", "1", "human")
        db.meta_set(conn, "linkedin_account_type", args.account_type, "human")
        db.meta_set(conn, "linkedin_account_age_years", str(args.account_age_years), "human")
        breakers.restart_warmup(conn, "linkedin", 1, "linkedin_enable")
    eff = config.load(conn)["channels"]["linkedin"]["enabled"]
    return Result(data={"enabled": eff, "warmup_week": 1},
                  message="LinkedIn enabled (warm-up week 1)" if eff else
                  "LinkedIn acknowledged, but channels.linkedin.enabled is false in private/config.json")


def cmd_linkedin_disable(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        db.meta_set(conn, "channel_linkedin_enabled", "0", "human" if ctx.caller.cls != "system" else "system")
    return {"enabled": False}


def cmd_auth_set_pin(args, ctx):
    stdin = ctx.stdin
    if ctx.caller.cls == "system":
        if auth.pin_exists():
            raise Denied("E_HUMAN_ONLY", "a PIN exists; changing it needs the old PIN (--pin-stdin)")
        if ctx.env.get("OPENCLAW_SHELL") or not (hasattr(stdin, "isatty") and stdin.isatty()):
            raise Denied("E_HUMAN_ONLY", "set the first PIN at a terminal (./jobhunter pin set)")
    if hasattr(stdin, "isatty") and stdin.isatty():
        import getpass
        new = getpass.getpass("New PIN (6 to 12 digits): ")
        again = getpass.getpass("New PIN again: ")
    else:
        new = (stdin.readline() or "").rstrip("\r\n")
        again = (stdin.readline() or "").rstrip("\r\n")
    if new != again:
        raise Denied("E_VALIDATION", "the two PINs differ")
    at = auth.set_pin(None, new, verified=(ctx.caller.cls == "human") or not auth.pin_exists())
    conn = ctx.connect()
    with db.tx(conn):
        db.meta_set(conn, "pin_set_at", at, "human")
    return {"pin_set_at": at}


def cmd_auth_check(args, ctx):
    return {"ok": ctx.caller.cls == "human"}


# ---------------------------------------------------------------- dedup check
RULES_BY_KIND = gate.DEDUP_RULES_BY_KIND
ACTION_FOR = gate.DEDUP_ACTION_FOR


def _lookup_company(conn, name=None, domain=None):
    pairs = keys.company_keys(name=name, domain=domain) if (name or domain) else []
    for k, _ in pairs:
        r = conn.execute("SELECT company_id FROM company_aliases WHERE alias_key = ?", (k,)).fetchone()
        if r:
            return companies.survivor(conn, r[0])
    return None


def _lookup_person(conn, email=None, linkedin_url=None):
    pairs = keys.person_keys(email=email, linkedin_url=linkedin_url)
    for k, _ in pairs:
        r = conn.execute("SELECT contact_id FROM contact_keys WHERE key = ?", (k,)).fetchone()
        if r:
            return people.survivor(conn, r[0])
    return None


def _lookup_job(conn, url):
    ck, al = keys.job_key(url, {}, None)
    for k in [ck] + al:
        r = conn.execute("SELECT job_id FROM job_keys WHERE key = ?", (k,)).fetchone() or \
            conn.execute("SELECT id FROM jobs WHERE canonical_key = ?", (k,)).fetchone()
        if r:
            return r[0]
    return None


def cmd_dedup_check(args, ctx):
    from jobhunter import exclusions
    conn = ctx.connect(write=False)
    jid = job_id(conn, args.job)
    cid = contact_id(conn, args.contact)
    coid = company_id(conn, args.company)
    email = args.email
    extra = {}
    if args.file:
        doc = ctx.read_json(args.file)
        if not isinstance(doc, dict) or set(doc) - {"url", "company_name", "domain", "linkedin_url", "email"}:
            raise Denied("E_SCHEMA", "dedup file keys: url, company_name, domain, linkedin_url, email")
        extra = {k: v for k, v in doc.items() if isinstance(v, str) and v}
        if extra.get("url") and jid is None:
            jid = _lookup_job(conn, extra["url"])
        if coid is None and (extra.get("company_name") or extra.get("domain")):
            coid = _lookup_company(conn, extra.get("company_name"), extra.get("domain"))
        if cid is None and (extra.get("email") or extra.get("linkedin_url")):
            cid = _lookup_person(conn, extra.get("email"), extra.get("linkedin_url"))
        email = email or extra.get("email")
    hits = gate.dedup_check(conn, args.kind, job_id=jid, contact_id=cid, company_id=coid, email=email,
                            thread_key=args.thread)["hits"]
    raw = exclusions.match(conn, company_name=extra.get("company_name"), domain=extra.get("domain"),
                           email=extra.get("email"), linkedin_url=extra.get("linkedin_url"),
                           job_url=extra.get("url")) if extra else []
    for e in raw:
        h = {"rule": "exclusion", "code": "E_EXCLUDED", "detail": "%s %s" % (e["type"], e["value_key"])}
        if h not in hits:
            hits.append(h)
    data = {"allowed": not hits, "hits": hits}
    if hits:
        return Result(data=data, code=hits[0]["code"], message=hits[0]["detail"])
    return data


# ---------------------------------------------------------------- companies and contacts
def cmd_companies_show(args, ctx):
    conn = ctx.connect(write=False)
    row = companies.by_uid(conn, args.company_uid)
    ids = companies.group(conn, row["id"])
    q = ",".join("?" * len(ids))
    return {"company": dict(row), "survivor_uid": conn.execute("SELECT company_uid FROM companies WHERE id = ?",
                                                               (ids[0],)).fetchone()[0],
            "aliases": [dict(r) for r in conn.execute("SELECT alias_key, kind, source FROM company_aliases WHERE "
                                                      "company_id IN (%s) ORDER BY alias_key" % q, ids)],
            "merges": [dict(r) for r in conn.execute("SELECT * FROM company_merges WHERE from_id IN (%s) OR to_id IN (%s)"
                                                     % (q, q), ids + ids)],
            "actions": [dict(r) for r in conn.execute("SELECT token, kind, status, reserved_at FROM actions WHERE "
                                                      "company_id IN (%s) ORDER BY id DESC LIMIT 50" % q, ids)]}


def cmd_companies_merge(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        a = companies.by_uid(conn, args.from_uid)
        b = companies.by_uid(conn, args.to_uid)
        companies.merge(conn, a["id"], b["id"], by="human", strength="exact", matched_keys=["human"])
    return {"merged": True, "into": args.to_uid}


def _keys_arg(s: str) -> list[str]:
    ks = [k.strip() for k in (s or "").split(",") if k.strip()]
    if not ks:
        raise Denied("E_VALIDATION", "--keys needs at least one key")
    return ks


def cmd_companies_split(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        row = companies.by_uid(conn, args.company_uid)
        new = companies.split(conn, row["id"], _keys_arg(args.keys), by="human")
        uid = conn.execute("SELECT company_uid FROM companies WHERE id = ?", (new,)).fetchone()[0]
    return {"new_company_uid": uid}


def cmd_companies_set_agency(args, ctx):
    if args.on == args.off:
        raise Denied("E_USAGE", "give exactly one of --on and --off")
    conn = ctx.connect()
    with db.tx(conn):
        row = companies.by_uid(conn, args.company_uid)
        companies.set_agency(conn, row["id"], bool(args.on), by="human")
    return {"company_uid": args.company_uid, "is_agency": bool(args.on)}


def cmd_companies_clear(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        row = companies.by_uid(conn, args.company_uid)
        companies.clear_state(conn, row["id"], by="human")
    return {"company_uid": args.company_uid, "contact_state": "none"}


def cmd_contacts_split(args, ctx):
    conn = ctx.connect()
    with db.tx(conn):
        row = people.by_uid(conn, args.contact_uid)
        new = people.split(conn, row["id"], _keys_arg(args.keys), by="human")
        uid = conn.execute("SELECT contact_uid FROM contacts WHERE id = ?", (new,)).fetchone()[0]
    return {"new_contact_uid": uid}


# ---------------------------------------------------------------- browser consent (per site)
def cmd_consent_list(args, ctx):
    from jobhunter import identity
    view = identity.consent_summary()
    lines = ["%-10s %-12s %s" % (r["site"], r["state"], r["method"] or "") for r in view["sites"]]
    return Result(data=view, human="\n".join(lines))


def cmd_consent_grant(args, ctx):
    from jobhunter import identity
    conn = ctx.connect()
    with db.tx(conn):
        res = identity.grant_consent(conn, args.site, method=args.method, chrome_profile=args.chrome_profile,
                                     chrome_profile_name=args.chrome_profile_name, by=by_of(ctx))
    return Result(data=res, message="the agent may now use your %s login" % ", ".join(res["granted"]),
                  next="run the login check for these sites (./jobhunter browser consent)")


def cmd_consent_revoke(args, ctx):
    from jobhunter import identity
    if bool(args.site) == bool(args.all_sites):
        raise Denied("E_USAGE", "give --site <name> (repeatable) or --all")
    conn = ctx.connect()
    with db.tx(conn):
        res = identity.revoke_consent(conn, args.site, everything=args.all_sites, by=by_of(ctx))
    msg = ("revoked %s; those areas stay stopped until you allow them again" % ", ".join(res["revoked"])
           if res["revoked"] else "no active consent to revoke")
    return Result(data=res, message=msg, next="./jobhunter browser forget also clears the agent profile's cookies")


# ---------------------------------------------------------------- register
def register(sub):
    p = add_command(sub, "init", cmd_init, callers="SH", help="create folders, keys, home.json, database, meta")
    p.add_argument("--force-examples", action="store_true", help="copy the example files again (to private/examples)")
    p = add_command(sub, "selftest", cmd_selftest, callers="SH", help="check the install")
    p.add_argument("--offline", action="store_true")
    add_command(sub, "home show", cmd_home_show, callers="SHRA", help="where this install lives")
    add_command(sub, "config validate", cmd_config_validate, callers="SH", help="list clamps and errors")
    p = add_command(sub, "config show", cmd_config_show, callers="SH", help="the config file or effective values")
    p.add_argument("--effective", action="store_true")
    add_command(sub, "config apply", cmd_config_apply, callers="SH", help="write trigger values to meta")
    p = add_command(sub, "config lower", cmd_config_lower, callers="SHC", help="tighten one limit")
    p.add_argument("path")
    p.add_argument("value")
    p = add_command(sub, "config raise", cmd_config_raise, callers="H", help="loosen one limit (PIN)")
    p.add_argument("path")
    p.add_argument("value")
    p = add_command(sub, "approval set", cmd_approval_set, callers="H", help="human or auto approval (PIN)")
    p.add_argument("mode", choices=("human", "auto"))
    p.add_argument("--ack")
    p = add_command(sub, "tier set", cmd_tier_set, callers="H", help="ceiling tier (PIN)")
    p.add_argument("--platform", required=True, choices=("gmail", "linkedin"))
    p.add_argument("--tier", required=True, choices=("conservative", "moderate"))
    p = add_command(sub, "linkedin enable", cmd_linkedin_enable, callers="H", help="turn LinkedIn writes on (PIN)")
    p.add_argument("--ack", required=True)
    p.add_argument("--account-type", required=True, choices=("free", "premium"))
    p.add_argument("--account-age-years", required=True, type=int)
    add_command(sub, "linkedin disable", cmd_linkedin_disable, callers="SHC", help="turn LinkedIn writes off")
    add_command(sub, "auth set-pin", cmd_auth_set_pin, callers="SH", help="set or change the owner PIN (stdin)")
    add_command(sub, "auth check", cmd_auth_check, callers="H", help="check the owner PIN")
    p = add_command(sub, "dedup check", cmd_dedup_check, callers="SHA", help="read-only duplicate rules")
    p.add_argument("--kind", required=True, choices=tuple(ACTION_FOR))
    p.add_argument("--job")
    p.add_argument("--contact")
    p.add_argument("--company")
    p.add_argument("--email")
    p.add_argument("--thread")
    p.add_argument("--file")
    p = add_command(sub, "companies show", cmd_companies_show, callers="SHR", help="one company with aliases")
    p.add_argument("company_uid")
    p = add_command(sub, "companies merge", cmd_companies_merge, callers="H", help="merge two companies (PIN)")
    p.add_argument("from_uid")
    p.add_argument("to_uid")
    p = add_command(sub, "companies split", cmd_companies_split, callers="H", help="undo a merge (PIN)")
    p.add_argument("company_uid")
    p.add_argument("--keys", required=True)
    p = add_command(sub, "companies set-agency", cmd_companies_set_agency, callers="H", help="agency flag (PIN)")
    p.add_argument("company_uid")
    p.add_argument("--on", action="store_true")
    p.add_argument("--off", action="store_true")
    p = add_command(sub, "companies clear", cmd_companies_clear, callers="H", help="contact state to none (PIN)")
    p.add_argument("company_uid")
    add_command(sub, "browser consent list", cmd_consent_list, callers="SHR",
                help="which sites' logins the agent may use (read only)")
    p = add_command(sub, "browser consent grant", cmd_consent_grant, callers="H",
                    help="allow the agent to use a site's login in its own browser profile (PIN)")
    p.add_argument("--site", action="append", required=True, help="gmail, linkedin or a job board; repeatable")
    p.add_argument("--method", required=True, choices=("chrome_import", "manual_login"))
    p.add_argument("--chrome-profile", help="Chrome profile folder for chrome_import (Default or Profile <n>)")
    p.add_argument("--chrome-profile-name", help="the profile's display name, for the record")
    p = add_command(sub, "browser consent revoke", cmd_consent_revoke, callers="H",
                    help="take back consent for sites and stop them (PIN)")
    p.add_argument("--site", action="append")
    p.add_argument("--all", dest="all_sites", action="store_true")
    p = add_command(sub, "contacts split", cmd_contacts_split, callers="H", help="undo a person merge (PIN)")
    p.add_argument("contact_uid")
    p.add_argument("--keys", required=True)


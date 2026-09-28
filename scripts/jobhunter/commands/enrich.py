"""`enrich` command group (U10, ENRICH-SPEC section 13). Auto-discovered by jobhunter.cli.

    enrich find --contact <id> [--target contact:<id>] [--include-reserve]   outreach agent, system, human
                (an agent's lookup counts against its running cycle: the global --cycle or its newest one)
    enrich show <contact_uid>                                                outreach agent, system, human
    enrich budget                                                            read-only
    enrich connect <provider> [--store keychain|file]                        human (PIN, terminal)
    enrich disconnect <provider> | --all                                     human (PIN)
    enrich retry <contact_uid>                                               human (PIN)
    enrich test <provider>                                                   human (PIN)
    enrich housekeeping                                                      system, human

Keys are the person's own: `enrich connect` reads them with hidden input from the terminal and never
from arguments, environment variables or files. There is no command that looks up an arbitrary person.
"""
from __future__ import annotations

import sys

from jobhunter.commands import Result, add_command

from .. import db, people
from ..canon import now
from ..errors import Denied

PROVIDERS = ("prospeo", "hunter", "tomba", "getprospect", "anymailfinder", "findymail", "apollo", "zerobounce")

# test injection points (never set by the CLI itself)
_TRANSPORT = None
_CLOCK = None
_SECRET_READER = None      # fn(prompt) -> str
_CONFIRM = None            # fn(prompt) -> str
_TTY_CHECK = None          # fn() -> bool
_ERR = None                # stream for prompts (default sys.stderr)


def register(subparsers):
    p = add_command(subparsers, "enrich find", cmd_find, callers="ASH",
                    help="find a work email address for one selected outreach target")
    p.add_argument("--contact", required=True, help="contact id (P...)")
    p.add_argument("--target", help="contact:<id> of the same contact")
    p.add_argument("--include-reserve", action="store_true", dest="include_reserve",
                   help="also use the one-time trial providers (owner only)")
    p = add_command(subparsers, "enrich show", cmd_show, callers="ASH", help="the lookup result for a contact")
    p.add_argument("contact_uid")
    add_command(subparsers, "enrich budget", cmd_budget, callers="RASH", help="credits used and left per provider")
    p = add_command(subparsers, "enrich connect", cmd_connect, callers="H",
                    help="store your own API key for a provider (hidden input)")
    p.add_argument("provider", choices=PROVIDERS)
    p.add_argument("--store", choices=("keychain", "file"))
    p = add_command(subparsers, "enrich disconnect", cmd_disconnect, callers="H", help="remove stored provider keys")
    p.add_argument("provider", nargs="?", choices=PROVIDERS)
    p.add_argument("--all", action="store_true", dest="all_providers")
    p = add_command(subparsers, "enrich retry", cmd_retry, callers="H",
                    help="allow one more lookup for a person (once per person)")
    p.add_argument("contact_uid")
    p = add_command(subparsers, "enrich test", cmd_test, callers="H", help="check a provider key without spending")
    p.add_argument("provider", choices=PROVIDERS)
    add_command(subparsers, "enrich housekeeping", cmd_housekeeping, callers="SH",
                help="settle stale calls and purge old provider data")


def _contact(conn, uid: str):
    if not isinstance(uid, str) or not uid.startswith("P"):
        raise Denied("E_USAGE", "a contact id (P...) is required")
    return people.by_uid(conn, uid)


# ---------------------------------------------------------------- find / show / budget
def _find_cycle(conn, ctx) -> str | None:
    """The cycle a lookup counts against (enrich.max_lookups_per_cycle). An agent's lookup always runs in one
    of its own running cycles: without --cycle the newest running cycle of its lanes, a named cycle must
    exist, be running and belong to it (gate.agent_cycle); none running is E_PRECONDITION. A made-up id
    never starts a fresh count. Other callers may name an existing cycle or none."""
    if ctx.caller.cls == "agent":
        from .. import gate
        return gate.agent_cycle(conn, ctx.caller.agent_id, ctx.cycle_id)["cycle_id"]
    if ctx.cycle_id is not None and conn.execute("SELECT 1 FROM cycles WHERE cycle_id = ?",
                                                 (ctx.cycle_id,)).fetchone() is None:
        raise Denied("E_VALIDATION", "no cycle %s" % ctx.cycle_id)
    return ctx.cycle_id


def cmd_find(args, ctx):
    from ..enrich import chain
    if args.include_reserve and ctx.caller.cls != "human":
        raise Denied("E_HUMAN_ONLY", "--include-reserve needs the owner PIN")
    conn = ctx.connect()
    ctx.cycle_id = _find_cycle(conn, ctx)
    c = _contact(conn, args.contact)
    try:
        out = chain.run_find(conn, c["id"], caller=ctx.caller, cycle_id=ctx.cycle_id,
                             include_reserve=bool(args.include_reserve), transport=_TRANSPORT, clock=_CLOCK,
                             target=args.target)
    except Denied as d:
        if d.code == "E_ENRICH_UNAVAILABLE":
            return Result(data=d.data, code=d.code, message=d.message, next=chain.NEXT_UNAVAILABLE,
                          retry_after_s=d.retry_after)
        raise
    return Result(data=out["data"], code=out["code"], message=out["message"], next=out["next"],
                  retry_after_s=out["retry_after_s"])


def cmd_show(args, ctx):
    conn = ctx.connect(write=False)
    c = _contact(conn, args.contact_uid)
    ids = people.group(conn, c["id"])
    q = ",".join("?" * len(ids))
    r = conn.execute("SELECT * FROM enrich_requests WHERE contact_id IN (%s) ORDER BY id DESC LIMIT 1" % q,
                     ids).fetchone()
    if r is None:
        return {"request_uid": None, "status": None, "retry_available": False}
    call = conn.execute("SELECT * FROM enrich_calls WHERE id = ?", (r["result_call_id"],)).fetchone() \
        if r["result_call_id"] else None
    retried = conn.execute("SELECT 1 FROM enrich_requests WHERE retry_of = ?", (r["id"],)).fetchone() is not None
    bounced = conn.execute("SELECT 1 FROM enrich_calls WHERE request_id = ? AND bounced_at IS NOT NULL",
                           (r["id"],)).fetchone() is not None
    invalid = conn.execute("SELECT 1 FROM contacts WHERE id IN (?, ?) AND email_invalid = 1",
                           (r["contact_id"], c["id"])).fetchone() is not None
    # A bounced or invalid address is never reported as sendable, whatever the request row recorded at find time.
    sendable = bool(r["sendable"]) and not bounced and not invalid
    return {"request_uid": r["request_uid"], "status": r["status"], "grade": r["grade"], "sendable": sendable,
            "provider": call["provider"] if call is not None else (r["result_source"] or None),
            "found_at": r["finished_at"], "verification": call["verification"] if call is not None else None,
            "confidence": call["confidence"] if call is not None else None,
            "source_url": call["source_url"] if call is not None else None, "bounced": bounced,
            "purged": bool(call is not None and call["purged_at"]),
            "retry_available": (r["status"] in ("found", "not_found") and r["retry_of"] is None and not retried
                                and not bounced)}


def cmd_budget(args, ctx):
    from ..enrich import budget
    conn = ctx.connect(write=False)
    return budget.summary(conn)


# ---------------------------------------------------------------- keys (human)
def _err():
    return _ERR if _ERR is not None else sys.stderr


def _has_tty() -> bool:
    if _TTY_CHECK is not None:
        return bool(_TTY_CHECK())
    try:
        with open("/dev/tty", "r"):
            return True
    except OSError:
        return False


def _confirm(prompt: str) -> str:
    if _CONFIRM is not None:
        return _CONFIRM(prompt)
    with open("/dev/tty", "r") as tty:
        _err().write(prompt)
        _err().flush()
        return tty.readline().strip()


def _read_secret(prompt: str) -> str:
    if _SECRET_READER is not None:
        return _SECRET_READER(prompt)
    import getpass
    return getpass.getpass(prompt, stream=_err())   # echo off, reads /dev/tty


def cmd_connect(args, ctx):
    from ..enrich import budget, keystore
    if ctx.caller.cls != "human":
        raise Denied("E_HUMAN_ONLY", "enrich connect needs the owner PIN")
    if not _has_tty():
        raise Denied("E_PRECONDITION", "enrich connect needs a terminal (the key is typed with hidden input)")
    err = _err()
    err.write("%s\nUse one account per provider, created by you on the provider's own site.\n"
              % keystore.OWN_KEY_NOTICE)
    err.flush()
    answer = _confirm("Type y to confirm the key is from your own %s account: " % args.provider)
    if (answer or "").strip().lower() != "y":
        raise Denied("E_PRECONDITION", "not confirmed; nothing was stored")
    values = {}
    for field in keystore.fields_for(args.provider):
        values[field] = _read_secret("%s %s (hidden): " % (args.provider, field.replace("_", " ")))
    keystore.validate(args.provider, values)
    try:
        backend = keystore.store(args.provider, values, args.store)
    except keystore.KeystoreError:
        if (args.store or keystore.backend()) != "keychain":
            raise Denied("E_CONFIG_INVALID", "the key file could not be written (check private/ permissions)")
        err.write("The Keychain refused the item; security will now ask for the key itself.\n")
        try:
            keystore.store_prompt(args.provider)
        except keystore.KeystoreError:
            raise Denied("E_PRECONDITION", "the Keychain refused the key; try --store file")
        backend = "keychain"
    finally:
        values.clear()
    conn = ctx.connect()
    with db.tx(conn):
        closed = budget.mark_key_set(conn, args.provider)
    return {"provider": args.provider, "backend": backend, "stored_at": now(), "breaker_closed": closed}


def cmd_disconnect(args, ctx):
    from ..enrich import keystore
    if ctx.caller.cls != "human":
        raise Denied("E_HUMAN_ONLY", "enrich disconnect needs the owner PIN")
    if bool(args.provider) == bool(args.all_providers):
        raise Denied("E_USAGE", "name one provider or use --all")
    targets = list(PROVIDERS) if args.all_providers else [args.provider]
    removed = []
    for p in targets:
        try:
            if keystore.delete(p):
                removed.append(p)
        except keystore.KeystoreError as exc:
            raise Denied("E_PRECONDITION", "the key store could not be changed: %s" % exc.kind)
    if not args.all_providers and not removed:
        raise Denied("E_NOT_FOUND", "no key is stored for %s" % args.provider)
    return {"removed": removed}


def cmd_retry(args, ctx):
    from ..enrich import cache
    if ctx.caller.cls != "human":
        raise Denied("E_HUMAN_ONLY", "enrich retry needs the owner PIN")
    conn = ctx.connect()
    c = _contact(conn, args.contact_uid)
    with db.tx(conn):
        out = cache.grant_retry(conn, people.survivor(conn, c["id"]), "human")
    return {"request_uid": out["request_uid"], "retry_of": out["retry_of"]}


def cmd_test(args, ctx):
    from ..enrich import keystore
    if ctx.caller.cls != "human":
        raise Denied("E_HUMAN_ONLY", "enrich test needs the owner PIN")
    st = keystore.status(args.provider)
    return Result(data={"provider": args.provider, "key": st["key"], "backend": st["backend"]}, code="NOTHING_TO_DO",
                  message="No free test call is documented; the first real lookup will show whether the key works.")


def cmd_housekeeping(args, ctx):
    from ..enrich import housekeeping
    conn = ctx.connect()
    return housekeeping.run_housekeeping(conn)


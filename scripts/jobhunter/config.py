"""Effective configuration, clamping and authority (design 4.1, 4.2).

- The person edits `private/config.json` (a copy of `config.example.json`). `load()` returns only
  effective values:
  - limit keys (L): min(file, baseline, HARD_MAX), baseline = meta 'raise:<path>' if present else DEFAULTS;
  - floor keys (F): max(file, baseline, HARD_MIN);
  - authority keys (A): the stricter of the file and meta (approval_mode, tier_gmail, tier_linkedin,
    channel_linkedin_enabled AND linkedin_tos_ack). The returned config holds the operating values:
    approval.mode is 'human' or 'auto', gmail.tier and linkedin.tier are 'conservative' or 'moderate',
    channels.linkedin.enabled is a bool;
  - fixed keys always take the default; bounded keys are clamped into their bounds; other keys are free.
- `apply(conn)` writes the trigger meta rows and lowers authority meta keys when the file is stricter.
- `lower()` only tightens (file edit); `raise_()` loosens up to the hard limit (meta 'raise:<path>', human).
- Local-time helpers (timezone 'auto' is the system zone; UTC inside a test home).
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
import re
import tempfile

from . import hardmax, paths
from .canon import now, parse_ts, utcnow
from .errors import Denied

try:
    import zoneinfo as _zoneinfo
except ImportError:  # pragma: no cover  (Python 3.9 has zoneinfo)
    _zoneinfo = None

_MISSING = object()

META_TRIGGER_KEYS = ("company_email_cooldown_days", "company_apps_per_day", "company_apps_per_30d",
                     "company_apps_per_90d", "li_invites_per_company_per_7d", "agency_emails_per_day",
                     "agency_emails_per_30d", "agency_apps_per_day", "agency_apps_per_30d")
LOWERABLE_BOOLS = ("channels.applications.enabled", "channels.email_outreach.enabled",
                   "channels.linkedin.writes.invites", "channels.linkedin.writes.messages",
                   "channels.linkedin.writes.easy_apply", "channels.linkedin.writes.inmail",
                   "channels.linkedin.writes.withdraw", "gmail.attach_resume_on_cold", "sheets.store_message_text")


# ---------------------------------------------------------------- files
def config_file() -> str:
    return os.path.join(paths.private_dir(), "config.json")


def read_file() -> dict:
    """The raw private/config.json ({} when absent). Invalid JSON is E_CONFIG_INVALID."""
    path = config_file()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise Denied("E_CONFIG_INVALID", "private/config.json is not valid JSON: %s" % exc, data={"path": path})
    if not isinstance(data, dict):
        raise Denied("E_CONFIG_INVALID", "private/config.json must hold a JSON object")
    return data


def file_sha256() -> str:
    try:
        with open(config_file(), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def _write_file(data: dict) -> None:
    path = config_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- meta access
def _meta(conn) -> dict:
    if conn is None:
        return _meta_from_db()
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
    except Exception:
        return {}


def _meta_from_db() -> dict:
    """Meta rows through a short read-only connection; {} when there is no usable database yet."""
    try:
        from . import db
        conn = db.connect(write=False)
    except Exception:
        return {}
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
    except Exception:
        return {}
    finally:
        conn.close()


# ---------------------------------------------------------------- value helpers
def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _num_list(v, n=None) -> bool:
    return isinstance(v, list) and all(_is_num(x) for x in v) and (n is None or len(v) == n)


def _same_type(default, value) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if _is_num(default):
        return _is_num(value)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, dict):
        return isinstance(value, dict)
    return default is None


def _cast_like(default, value):
    if isinstance(default, int) and not isinstance(default, bool) and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _combine(fn, *vals):
    """fn element-wise over numbers or equal-length number lists (None entries ignored)."""
    vals = [v for v in vals if v is not None]
    if all(_is_num(v) for v in vals):
        return fn(vals)
    n = len(vals[0])
    return [fn([v[i] for v in vals]) for i in range(n)]


def _parse_raise(meta: dict, path: str, default):
    raw = meta.get("raise:" + path)
    if raw is None:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        return None
    if _is_num(default) and _is_num(v):
        return v
    if _num_list(default) and _num_list(v, len(default)):
        return v
    if isinstance(default, list) and isinstance(v, list):
        return v
    return None


# ---------------------------------------------------------------- effective values
class _Ctx:
    def __init__(self, meta: dict):
        self.meta = meta
        self.clamped: list[dict] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []


def _leaf(default, filev, path: str, ctx: _Ctx):
    cls = hardmax.key_class(path)
    given = filev is not _MISSING
    if given and not _same_type(default, filev):
        ctx.warnings.append("%s has the wrong type; the default is used" % path)
        filev, given = _MISSING, False
    value = default if not given else _cast_like(default, filev)
    if cls == "fixed":
        if given and value != default:
            ctx.clamped.append({"path": path, "asked": value, "used": default, "why": "fixed value"})
        return copy.deepcopy(default)
    if cls in ("L", "F"):
        if isinstance(default, list) and not _num_list(value, len(default)):
            if given:
                ctx.warnings.append("%s must be a list of %d numbers; the default is used" % (path, len(default)))
            value = default
        baseline = _parse_raise(ctx.meta, path, default)
        if baseline is None:
            baseline = default
        if cls == "L":
            hard = hardmax.lookup(hardmax.HARD_MAX, path)
            used = _combine(min, value, baseline, hard)
            why = "above the researched default (raise it with ./jobhunter config raise)" \
                if (hard is None or _combine(min, value, hard) != used) else "above the hard maximum"
        else:
            hard = hardmax.lookup(hardmax.HARD_MIN, path)
            used = _combine(max, value, baseline, hard)
            why = "below the researched default (lower it with ./jobhunter config raise)" \
                if (hard is None or _combine(max, value, hard) != used) else "below the hard minimum"
        used = _cast_like(default, used) if _is_num(used) else [_cast_like(d, u) for d, u in zip(default, used)]
        if given and used != value:
            ctx.clamped.append({"path": path, "asked": value, "used": used, "why": why})
        return used
    if cls == "bounded":
        lo, hi = hardmax.lookup(hardmax.BOUNDS, path)
        if _is_num(value):
            used = min(max(value, lo), hi)
        elif _num_list(value):
            used = [min(max(x, lo), hi) for x in value]
        else:
            used = default
        if given and used != value:
            ctx.clamped.append({"path": path, "asked": value, "used": used, "why": "outside [%s, %s]" % (lo, hi)})
        return used
    return copy.deepcopy(value)


def _walk(default, filev, path: str, ctx: _Ctx):
    if path in hardmax.OBJECT_LISTS:
        out = []
        fl = filev if isinstance(filev, list) else []
        for i, d in enumerate(default):
            f = fl[i] if i < len(fl) and isinstance(fl[i], dict) else {}
            out.append({k: _leaf(v, f.get(k, _MISSING), path + "[]." + k, ctx) for k, v in d.items()})
        if isinstance(filev, list) and len(filev) > len(default):
            ctx.warnings.append("%s: entries beyond the default %d weeks are ignored" % (path, len(default)))
        return out
    if path in hardmax.UNION_LISTS:
        vals = list(default)
        if isinstance(filev, list):
            vals += [x for x in filev if isinstance(x, str) and x not in vals]
        return vals
    if isinstance(default, dict) and hardmax.key_class(path) == "free":
        fd = filev if isinstance(filev, dict) else {}
        if filev is not _MISSING and not isinstance(filev, dict):
            ctx.warnings.append("%s must be an object; the default is used" % path)
        out = {}
        for k, v in default.items():
            out[k] = _walk(v, fd.get(k, _MISSING), (path + "." + k) if path else k, ctx)
        for k in fd:
            if k not in default:
                ctx.warnings.append("unknown key %s is ignored" % ((path + "." + k) if path else k))
        return out
    return _leaf(default, filev, path, ctx)


def _authority(eff: dict, raw: dict, meta: dict) -> None:
    def fget(path, default):
        node = raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    file_mode = fget("approval.mode", "inherit")
    eff["approval"]["mode"] = "auto" if (meta.get("approval_mode") == "auto" and file_mode == "inherit") else "human"
    li_mode = fget("approval.per_channel.linkedin", "human")
    eff["approval"]["per_channel"]["linkedin"] = eff["approval"]["mode"] if li_mode == "inherit" else "human"
    for plat in ("gmail", "linkedin"):
        ft = fget(plat + ".tier", "inherit")
        eff[plat]["tier"] = "moderate" if (meta.get("tier_" + plat) == "moderate" and ft == "inherit") \
            else "conservative"
    fe = fget("channels.linkedin.enabled", True)
    eff["channels"]["linkedin"]["enabled"] = bool(fe is True and meta.get("channel_linkedin_enabled") == "1"
                                                  and meta.get("linkedin_tos_ack") == "1")
    if not eff["channels"]["linkedin"]["enabled"]:
        for k in eff["channels"]["linkedin"]["writes"]:
            eff["channels"]["linkedin"]["writes"][k] = False
    grades = [g for g in eff["gmail"]["address_grades_allowed"] if g in ("A", "B", "C")]
    raised = _parse_raise(meta, "gmail.address_grades_allowed", ["A"]) or []
    if "C" in grades and "C" not in raised:
        grades.remove("C")
    eff["gmail"]["address_grades_allowed"] = grades or ["A"]
    eff["timezone"] = resolve_timezone(eff.get("timezone"))


# Shipped placeholders (config.example.json and older copies of it) that must never reach a recipient.
PLACEHOLDER_LINK_RE = re.compile(r"your-handle|linkedin\.com/in/?$", re.I)


def _drop_placeholder_links(eff: dict, ctx: _Ctx) -> None:
    """owner.signature.links is appended by code to every email: a placeholder link (the example's
    linkedin.com/in/your-handle) is dropped with a warning instead of being sent."""
    sig = (eff.get("owner") or {}).get("signature")
    if not isinstance(sig, dict):
        return
    links = sig.get("links") if isinstance(sig.get("links"), list) else []
    keep = [x for x in links if isinstance(x, str) and x.strip() and not PLACEHOLDER_LINK_RE.search(x)]
    if keep != links:
        dropped = [x for x in links if x not in keep]
        ctx.warnings.append("owner.signature.links: placeholder link(s) %s are not added to emails; put your own "
                            "profile URL there or run ./jobhunter owner" % ", ".join(str(x) for x in dropped))
        sig["links"] = keep


def compute(raw: dict, meta: dict) -> tuple[dict, _Ctx]:
    ctx = _Ctx(meta)
    eff = _walk(hardmax.defaults(), raw, "", ctx)
    _authority(eff, raw, meta)
    _drop_placeholder_links(eff, ctx)
    # a site whose apply mode is 'never' stays never
    for site, d in hardmax.DEFAULTS["boards"]["sites"].items():
        if d.get("apply") == "never":
            eff["boards"]["sites"][site]["apply"] = "never"
    return eff, ctx


def load(conn=None) -> dict:
    """Effective config (4.1). `conn` supplies meta; without it a read-only connection is used when a
    database exists (else meta is treated as empty, which is the strictest authority)."""
    eff, _ctx = compute(read_file(), _meta(conn))
    return eff


def validate(conn=None) -> dict:
    """{errors, warnings, clamped: [{path, asked, used, why}]}."""
    try:
        raw = read_file()
    except Denied as d:
        return {"errors": [d.message], "warnings": [], "clamped": []}
    _eff, ctx = compute(raw, _meta(conn))
    if raw and raw.get("schema_version") not in (None, 2):
        ctx.warnings.append("schema_version %r is not 2; unknown keys are ignored" % raw.get("schema_version"))
    _validate_enrich(raw, ctx)
    _validate_route(raw, ctx)
    return {"errors": ctx.errors, "warnings": ctx.warnings, "clamped": ctx.clamped}


EMAIL_ROUTES = ("web_ui", "app_password")


def _validate_route(raw: dict, ctx: _Ctx) -> None:
    route = _get(raw, "gmail.route")
    if route is not _MISSING and route not in EMAIL_ROUTES:
        ctx.errors.append("gmail.route must be web_ui (the default: the browser uses your Gmail after you consent) "
                          "or app_password (optional: code sends over SMTP), not %r" % (route,))


def _validate_enrich(raw: dict, ctx: _Ctx) -> None:
    """The email finder's lists hold only known providers, each at most once (ENRICH-SPEC 14). An unknown or
    repeated item is an error here; the finder itself drops it (settings.clamp), so nothing unknown is called."""
    block = raw.get("enrich") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        return
    for path, allowed in hardmax.ENRICH_ITEM_SETS.items():
        val = _get(raw, path)
        if val is _MISSING:
            continue
        if not isinstance(val, list):
            ctx.errors.append("%s must be a list of: %s" % (path, ", ".join(allowed)))
            continue
        bad = [x for x in val if not isinstance(x, str) or x not in allowed]
        if bad:
            ctx.errors.append("%s: unknown provider(s) %s; allowed: %s" % (
                path, ", ".join(json.dumps(x) for x in bad), ", ".join(allowed)))
        seen = [x for i, x in enumerate(val) if isinstance(x, str) and x in val[:i]]
        if seen:
            ctx.errors.append("%s lists %s more than once" % (path, ", ".join(sorted(set(seen)))))
    ks = block.get("key_store", "auto")
    if ks not in hardmax.ENRICH_KEY_STORES:
        ctx.errors.append("enrich.key_store must be one of %s, not %r" % (", ".join(hardmax.ENRICH_KEY_STORES), ks))


def trigger_meta(eff: dict) -> dict:
    tier = eff["gmail"]["tier"]
    pc, pa = eff["boards"]["per_company"], eff["boards"]["per_agency"]
    return {
        "company_email_cooldown_days": str(int(eff["gmail"]["ceilings"][tier]["company_cooldown_days"])),
        "company_apps_per_day": str(int(pc["apps_day"])), "company_apps_per_30d": str(int(pc["apps_30d"])),
        "company_apps_per_90d": str(int(pc["apps_90d"])),
        "li_invites_per_company_per_7d": str(int(eff["linkedin"]["max_invites_per_company_week"])),
        "agency_emails_per_day": str(int(pa["emails_day"])), "agency_emails_per_30d": str(int(pa["emails_30d"])),
        "agency_apps_per_day": str(int(pa["apps_day"])), "agency_apps_per_30d": str(int(pa["apps_30d"])),
    }


def apply(conn) -> dict:
    """Write the trigger meta rows from the effective config and lower authority meta keys when the file
    is stricter (auto -> human, moderate -> conservative, 1 -> 0). Runs inside the caller's tx."""
    from . import db
    raw = read_file()
    meta = _meta(conn)
    eff, ctx = compute(raw, meta)
    changed = {}
    for key, value in trigger_meta(eff).items():
        if meta.get(key) != value:
            db.meta_set(conn, key, value, "config_apply")
            changed[key] = value
    fa = (raw.get("approval") or {}).get("mode", "inherit") if isinstance(raw.get("approval"), dict) else "inherit"
    if fa == "human" and meta.get("approval_mode") == "auto":
        db.meta_set(conn, "approval_mode", "human", "config_apply")
        changed["approval_mode"] = "human"
    for plat in ("gmail", "linkedin"):
        node = raw.get(plat) if isinstance(raw.get(plat), dict) else {}
        if node.get("tier") == "conservative" and meta.get("tier_" + plat) == "moderate":
            db.meta_set(conn, "tier_" + plat, "conservative", "config_apply")
            changed["tier_" + plat] = "conservative"
    ch = raw.get("channels") if isinstance(raw.get("channels"), dict) else {}
    li = ch.get("linkedin") if isinstance(ch.get("linkedin"), dict) else {}
    if li.get("enabled") is False and meta.get("channel_linkedin_enabled") == "1":
        db.meta_set(conn, "channel_linkedin_enabled", "0", "config_apply")
        changed["channel_linkedin_enabled"] = "0"
    for key, value in _enrich_meta(conn, eff, meta).items():
        changed[key] = value
    sha = file_sha256()
    if meta.get("config_sha256") != sha:
        db.meta_set(conn, "config_sha256", sha, "config_apply")
    return {"meta": changed, "clamped": ctx.clamped, "warnings": ctx.warnings}


def _enrich_meta(conn, eff: dict, meta: dict) -> dict:
    """The email finder's budget meta rows (ENRICH-SPEC 14): 0 when enrich or the provider is off, so a
    missing or disabled block always means "no calls". Returns the rows that changed."""
    from . import db
    try:
        from .enrich import settings as enrich_settings
    except ImportError:
        return {}
    s = enrich_settings.clamp(eff.get("enrich"), meta)
    changed = {}
    for key, value in enrich_settings.meta_rows(s).items():
        if meta.get(key) != value:
            db.meta_set(conn, key, value, "config_apply")
            changed[key] = value
    return changed


# ---------------------------------------------------------------- lower / raise
def _get(node, path: str):
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _set(node: dict, path: str, value) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def parse_value(text):
    """A CLI value: JSON when it parses (numbers, lists, booleans), else the string."""
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _check_path(path: str):
    if not isinstance(path, str) or not path or ".." in path or "[" in path:
        raise Denied("E_VALIDATION", "bad config path %r" % path)
    default = _get(hardmax.DEFAULTS, path)
    if default is _MISSING or isinstance(default, dict):
        raise Denied("E_VALIDATION", "%s is not a config value" % path)
    return default


def _is_stricter(cls: str, new, cur) -> bool:
    """new is at least as strict as cur in every element and differs somewhere."""
    pairs = list(zip(new, cur)) if isinstance(new, list) else [(new, cur)]
    if cls == "L":
        ok = all(a <= b for a, b in pairs)
    elif cls == "F":
        ok = all(a >= b for a, b in pairs)
    else:
        return False
    return ok and new != cur


def lower(path: str, value, conn=None) -> dict:
    """Tighten one key in private/config.json; anything that loosens is refused (E_VALIDATION)."""
    default = _check_path(path)
    new = parse_value(value)
    cls = hardmax.key_class(path)
    cur = _get(load(conn), path)
    if cls in ("L", "F"):
        if isinstance(default, list):
            if not _num_list(new, len(default)):
                raise Denied("E_VALIDATION", "%s takes a list of %d numbers" % (path, len(default)))
        elif not _is_num(new):
            raise Denied("E_VALIDATION", "%s takes a number" % path)
        if new == cur:
            raise Denied("E_VALIDATION", "%s is already %s" % (path, json.dumps(cur)))
        if not _is_stricter(cls, new, cur):
            raise Denied("E_VALIDATION", "%s %s would loosen the limit (now %s); use config raise with the PIN"
                         % (path, json.dumps(new), json.dumps(cur)))
    elif cls == "A":
        strict = hardmax.AUTHORITY_STRICT[path]
        if new != strict:
            raise Denied("E_VALIDATION", "%s can only be lowered to %s" % (path, json.dumps(strict)))
    elif path in LOWERABLE_BOOLS:
        if new is not False:
            raise Denied("E_VALIDATION", "%s can only be lowered to false" % path)
    else:
        raise Denied("E_VALIDATION", "%s is not a limit; edit private/config.json to change it" % path)
    raw = read_file() or hardmax.defaults()
    old = _get(raw, path)
    _set(raw, path, new)
    _write_file(raw)
    return {"path": path, "old": None if old is _MISSING else old, "new": new, "effective_before": cur}


def raise_(conn, path: str, value) -> dict:
    """Loosen one limit or floor key (human caller only): meta 'raise:<path>' and the file value, clamped to
    HARD_MAX (limits) or HARD_MIN (floors). Runs inside the caller's tx."""
    from . import db
    default = _check_path(path)
    new = parse_value(value)
    cls = hardmax.key_class(path)
    if path == "gmail.address_grades_allowed":
        if not (isinstance(new, list) and new and all(g in ("A", "B", "C") for g in new)):
            raise Denied("E_VALIDATION", "address grades are a list of A, B, C")
        db.meta_set(conn, "raise:" + path, json.dumps(sorted(set(new))), "human")
        raw = read_file() or hardmax.defaults()
        old = _get(raw, path)
        _set(raw, path, sorted(set(new)))
        _write_file(raw)
        return {"path": path, "old": None if old is _MISSING else old, "new": sorted(set(new)), "clamped": False}
    if cls not in ("L", "F"):
        raise Denied("E_VALIDATION", "%s cannot be raised (authority keys use approval set, tier set, linkedin "
                     "enable; other keys are edited in private/config.json)" % path)
    if isinstance(default, list):
        if not _num_list(new, len(default)):
            raise Denied("E_VALIDATION", "%s takes a list of %d numbers" % (path, len(default)))
    elif not _is_num(new):
        raise Denied("E_VALIDATION", "%s takes a number" % path)
    if cls == "L":
        hard = hardmax.lookup(hardmax.HARD_MAX, path)
        used = _combine(min, new, hard) if hard is not None else new
    else:
        hard = hardmax.lookup(hardmax.HARD_MIN, path)
        used = _combine(max, new, hard) if hard is not None else new
    old = _get(load(conn), path)
    db.meta_set(conn, "raise:" + path, json.dumps(used), "human")
    raw = read_file() or hardmax.defaults()
    _set(raw, path, used)
    _write_file(raw)
    return {"path": path, "old": old, "new": used, "clamped": used != new}


# ---------------------------------------------------------------- time zone and windows
def system_timezone() -> str:
    if paths.is_test_home():
        return "UTC"
    tz = os.environ.get("TZ")
    if tz and _valid_tz(tz.lstrip(":")):
        return tz.lstrip(":")
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            name = target.split("zoneinfo/", 1)[1]
            if _valid_tz(name):
                return name
    except OSError:
        pass
    return "UTC"


def _valid_tz(name: str) -> bool:
    if not name or _zoneinfo is None:
        return False
    try:
        _zoneinfo.ZoneInfo(name)
        return True
    except Exception:
        return False


def resolve_timezone(name) -> str:
    if not isinstance(name, str) or name in ("", "auto") or not _valid_tz(name):
        return system_timezone()
    return name


def tzinfo(cfg_or_name):
    name = cfg_or_name.get("timezone") if isinstance(cfg_or_name, dict) else cfg_or_name
    name = resolve_timezone(name)
    if _zoneinfo is None:
        return _dt.timezone.utc
    return _zoneinfo.ZoneInfo(name)


def local_dt(ts=None, tz=None) -> _dt.datetime:
    """Aware local datetime for a UTC timestamp string (default now) in tz (a tzinfo or a config)."""
    d = utcnow() if ts is None else (parse_ts(ts) if isinstance(ts, str) else ts)
    if tz is None:
        tz = _dt.timezone.utc
    elif isinstance(tz, (dict, str)):
        tz = tzinfo(tz)
    return d.astimezone(tz)


def local_date(ts=None, tz=None) -> str:
    return local_dt(ts, tz).strftime("%Y-%m-%d")


def hhmm(s: str) -> int:
    """'09:30' -> 570 minutes."""
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def in_window(d: _dt.datetime, window) -> bool:
    """d (local) inside [start, end]; a window whose start is after its end wraps midnight."""
    start, end = hhmm(window[0]), hhmm(window[1])
    m = d.hour * 60 + d.minute
    if start <= end:
        return start <= m <= end
    return m >= start or m <= end


def seconds_until(pred, start: _dt.datetime | None = None, step_s: int = 300, max_days: int = 9) -> int | None:
    """Seconds from start (UTC, default now) until pred(utc_datetime) first holds, scanning in steps."""
    t = start or utcnow()
    for i in range(int(max_days * 86400 / step_s) + 1):
        cand = t + _dt.timedelta(seconds=i * step_s)
        if pred(cand):
            return i * step_s
    return None


def now_local(cfg: dict) -> _dt.datetime:
    return local_dt(now(), tzinfo(cfg))

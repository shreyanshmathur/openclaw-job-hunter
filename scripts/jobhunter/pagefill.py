"""Fixed read-only page scripts and the field logic of the code-owned browser steps (FEATURES-OTP-ACCOUNTS-CAPTCHA
2.2) [U6].

The page scripts are zero-argument arrow functions with the same shadow-DOM walk as drivers/read_form.js. They
only read: element descriptors (tag, type, name, label, automation id, required, checked, visible, maxlength, the
length of a field's value, whether it is a masked password field, its centre on the page), the CAPTCHA state and
the page text. They never return a field's value, never change the page, never follow a frame. Code changes the
page only through cdp.Session: a mouse press on the step's one button (or on a field to focus it, or on the
standard terms box), Input.insertText into the focused field, and select-all plus delete to clear a field this
command filled.

Python picks the fields from the descriptors: find_account_form, find_signin_form, find_code_field and
find_button (Workday data-automation-id values first, then labels, names and placeholders). fill() focuses a
field, types the value, then checks the value length (and for a password that the field is masked) without
reading it back; a mismatch clears what was typed and refuses (fill_not_verified).
"""
from __future__ import annotations

import re

from .errors import Denied

_WALK = r"""
  const els = [];
  const walk = (root, depth) => {
    if (!root || depth > 20) { return; }
    const list = root.querySelectorAll('*');
    for (let i = 0; i < list.length; i++) {
      els.push(list[i]);
      if (list[i].shadowRoot) { walk(list[i].shadowRoot, depth + 1); }
    }
  };
  walk(document, 0);
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').replace(/\s*\*\s*$/, '').trim();
  const byId = (id) => (id ? document.getElementById(id) : null);
  const labelOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) { return clean(aria); }
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    const parts = ids.map((id) => byId(id)).filter(Boolean).map((n) => n.innerText || n.textContent || '');
    if (parts.length) { return clean(parts.join(' ')); }
    if (el.id) {
      const lab = document.querySelector('label[for="' + el.id.replace(/"/g, '') + '"]');
      if (lab) { return clean(lab.innerText || lab.textContent); }
    }
    const wrap = el.closest('label');
    if (wrap) { return clean(wrap.innerText || wrap.textContent); }
    return '';
  };
  const fieldEls = els.filter((el) => ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) &&
    !/^(hidden|submit|button|reset|image|file)$/i.test(el.getAttribute('type') || ''));
  const buttonEls = els.filter((el) => el.tagName === 'BUTTON' || el.getAttribute('role') === 'button' ||
    (el.tagName === 'INPUT' && /^(submit|button)$/i.test(el.getAttribute('type') || '')));
"""

# jobhunter pagefill READ_FIELDS: every form field and button as a descriptor (never a value).
READ_FIELDS = r"""() => {
  /* jobhunter pagefill READ_FIELDS (read only): descriptors of the form fields and buttons, never a value. */
""" + _WALK + r"""
  const box = (el) => { const r = el.getBoundingClientRect(); return { x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) }; };
  const fields = fieldEls.map((el, i) => {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    const b = box(el);
    return { i: i, tag: el.tagName.toLowerCase(), type: type, name: (el.getAttribute('name') || '').slice(0, 120),
      id: (el.id || '').slice(0, 120), aid: (el.getAttribute('data-automation-id') || '').slice(0, 120),
      autocomplete: (el.getAttribute('autocomplete') || '').slice(0, 60), label: labelOf(el).slice(0, 300),
      placeholder: (el.getAttribute('placeholder') || '').slice(0, 120),
      required: !!(el.required || el.getAttribute('aria-required') === 'true'),
      checked: type === 'checkbox' || type === 'radio' ? !!el.checked : null, disabled: !!el.disabled,
      visible: visible(el), maxlength: el.maxLength > 0 ? el.maxLength : null,
      value_length: type === 'checkbox' || type === 'radio' ? null : String(el.value || '').length,
      masked: type === 'password', x: b.x, y: b.y, w: b.w, h: b.h };
  });
  const buttons = buttonEls.map((el, i) => {
    const b = box(el);
    const name = clean(el.getAttribute('aria-label') || el.innerText || el.textContent || el.value || '');
    return { i: i, name: name.slice(0, 120), aid: (el.getAttribute('data-automation-id') || '').slice(0, 120),
      type: (el.getAttribute('type') || '').toLowerCase(), disabled: !!el.disabled, visible: visible(el),
      x: b.x, y: b.y, w: b.w, h: b.h };
  });
  const active = document.activeElement;
  return { url: location.href, title: document.title || '', fields: fields, buttons: buttons,
    active: fieldEls.indexOf(active) };
}"""

# jobhunter pagefill CAPTCHA_STATE: is a CAPTCHA frame or container visible (booleans and counts only).
CAPTCHA_STATE = r"""() => {
  /* jobhunter pagefill CAPTCHA_STATE (read only): visible CAPTCHA frames and containers, counts only. */
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const frames = Array.from(document.querySelectorAll('iframe')).filter((f) => visible(f) &&
    /recaptcha|hcaptcha|turnstile|captcha|challenges\.cloudflare/i.test((f.getAttribute('src') || '') + ' ' +
      (f.getAttribute('title') || '')));
  const boxes = Array.from(document.querySelectorAll('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]'))
    .filter(visible);
  const text = ((document.body && document.body.innerText) || '').slice(0, 20000);
  const words = /captcha|verify (that )?you.re (a )?human|i.m not a robot/i.test(text);
  return { url: location.href, frames: frames.length, containers: boxes.length, text_hint: words,
    visible: frames.length + boxes.length > 0 };
}"""

# jobhunter pagefill PAGE_TEXT: url, title and the first 200,000 characters of the page text.
PAGE_TEXT = r"""() => {
  /* jobhunter pagefill PAGE_TEXT (read only): url, title and visible text. */
  return { url: location.href, title: document.title || '',
    text: ((document.body && document.body.innerText) || '').slice(0, 200000) };
}"""

# jobhunter pagefill GMAIL_LINKS: the links of the open Gmail conversation's message bodies (code only; never shown
# to a model).
GMAIL_LINKS = r"""() => {
  /* jobhunter pagefill GMAIL_LINKS (read only): href and text of the links in the open message bodies. */
  if ((location.hostname || '').toLowerCase() !== 'mail.google.com') { return []; }
  const out = [];
  for (const a of Array.from(document.querySelectorAll('div.a3s a[href]')).slice(0, 200)) {
    out.push({ href: String(a.getAttribute('href') || '').slice(0, 2000),
      text: String(a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200) });
  }
  return out;
}"""

SCRIPTS = {"READ_FIELDS": READ_FIELDS, "CAPTCHA_STATE": CAPTCHA_STATE, "PAGE_TEXT": PAGE_TEXT,
           "GMAIL_LINKS": GMAIL_LINKS}

# ---------------------------------------------------------------- matching rules
WORKDAY_AIDS = {"email": ("email",), "password": ("password",), "confirm": ("verifyPassword",),
                "terms": ("createAccountCheckbox",), "create": ("createAccountSubmitButton",),
                "signin": ("signInSubmitButton",)}
STEP_BUTTONS = {
    "create": r"^(create account|create my account|sign up|register|create)$",
    "signin": r"^(sign in|log in|login|continue|next)$",
    "verify": r"^(verify|verify email|submit|submit code|continue|confirm|next)$",
    "resubmit": r"^(submit application|submit)$",
}
CODE_LABEL_RE = re.compile(r"code|otp|passcode|pin|verification|security code", re.I)
EMAIL_RE = re.compile(r"e-?mail", re.I)
CONFIRM_RE = re.compile(r"confirm|verify|re-?enter|repeat|again", re.I)
TEXT_TYPES = ("text", "email", "tel", "number", "input", "textarea", "")


def _text(f: dict) -> str:
    return " ".join(str(f.get(k) or "") for k in ("label", "name", "id", "placeholder"))


def _visible(items: list) -> list:
    return [x for x in items if x.get("visible", True) and not x.get("disabled")]


def read_form(session) -> dict:
    v = session.evaluate(READ_FIELDS)
    if not isinstance(v, dict) or not isinstance(v.get("fields"), list) or not isinstance(v.get("buttons"), list):
        raise Denied("E_PRECONDITION", "the page form could not be read", data={"reason": "form_not_recognized"})
    return v


def captcha_state(session) -> dict:
    v = session.evaluate(CAPTCHA_STATE)
    if not isinstance(v, dict):
        return {"visible": False, "frames": 0, "containers": 0, "text_hint": False}
    return v


def _by_aid(fields: list, names) -> list:
    return [f for f in fields if f.get("aid") in names]


def find_button(form: dict, step: str) -> dict | None:
    """The one visible button of a step (Workday automation ids first, then the step's whole-name list)."""
    buttons = _visible(form.get("buttons") or [])
    aid = WORKDAY_AIDS.get(step)
    if aid:
        hit = _by_aid(buttons, aid)
        if len(hit) == 1:
            return hit[0]
    rx = re.compile(STEP_BUTTONS[step], re.I)
    hits = [b for b in buttons if rx.match((b.get("name") or "").strip())]
    if not hits:
        return None
    # the most specific name wins (create account over create, submit application over submit)
    hits.sort(key=lambda b: -len(b.get("name") or ""))
    return hits[0]


def find_account_form(form: dict) -> dict:
    """{email, passwords: [password, confirm?], checkboxes, button}; Denied(E_PRECONDITION form_not_recognized)
    unless the form has exactly one email field and one or two password fields."""
    fields = _visible(form.get("fields") or [])
    pw = [f for f in fields if f.get("type") == "password"]
    email = _by_aid(fields, WORKDAY_AIDS["email"]) or [f for f in fields if f.get("type") == "email"] or \
        [f for f in fields if f.get("type") in TEXT_TYPES and EMAIL_RE.search(_text(f))]
    if len(email) != 1 or not (1 <= len(pw) <= 2):
        raise Denied("E_PRECONDITION", "the account form was not recognised", data={"reason": "form_not_recognized"})
    main = _by_aid(pw, WORKDAY_AIDS["password"])
    conf = _by_aid(pw, WORKDAY_AIDS["confirm"])
    if len(pw) == 2 and not (main and conf):
        conf = [f for f in pw if CONFIRM_RE.search(_text(f))]
        main = [f for f in pw if f not in conf]
        if len(main) != 1 or len(conf) != 1:
            main, conf = [pw[0]], [pw[1]]
    elif len(pw) == 1:
        main, conf = pw, []
    boxes = [f for f in fields if f.get("type") == "checkbox"]
    return {"email": email[0], "passwords": main + conf, "checkboxes": boxes, "button": find_button(form, "create")}


def find_signin_form(form: dict) -> dict:
    fields = _visible(form.get("fields") or [])
    pw = [f for f in fields if f.get("type") == "password"]
    email = _by_aid(fields, WORKDAY_AIDS["email"]) or [f for f in fields if f.get("type") == "email"] or \
        [f for f in fields if f.get("type") in TEXT_TYPES and EMAIL_RE.search(_text(f))]
    if len(email) != 1 or len(pw) != 1:
        raise Denied("E_PRECONDITION", "the sign-in form was not recognised", data={"reason": "form_not_recognized"})
    return {"email": email[0], "password": pw[0], "button": find_button(form, "signin")}


def find_code_field(form: dict) -> dict | None:
    """{kind: single|split, fields: [...]}: autocomplete=one-time-code, a label, name or placeholder that names a
    code, or a group of 4 to 10 maxlength=1 inputs. None when the page has no code field."""
    fields = [f for f in _visible(form.get("fields") or []) if f.get("type") in TEXT_TYPES]
    one = [f for f in fields if (f.get("autocomplete") or "").lower() == "one-time-code"]
    split = [f for f in fields if f.get("maxlength") == 1]
    if 4 <= len(split) <= 10 and (len(one) == len(split) or not one):
        return {"kind": "split", "fields": sorted(split, key=lambda f: (f.get("y", 0), f.get("x", 0)))}
    if len(one) == 1:
        return {"kind": "single", "fields": one}
    named = [f for f in fields if CODE_LABEL_RE.search(_text(f)) and f.get("maxlength") != 1]
    if len(named) == 1:
        return {"kind": "single", "fields": named}
    return None


def code_field_present(form: dict) -> bool:
    return find_code_field(form) is not None


def field_by_index(form: dict, i: int) -> dict | None:
    for f in form.get("fields") or []:
        if f.get("i") == i:
            return f
    return None


# ---------------------------------------------------------------- page changes (through cdp.Session)
def focus(session, field: dict) -> bool:
    """Press on the field's centre, then check that it is the focused element."""
    session.click_at(field["x"], field["y"])
    after = read_form(session)
    return after.get("active") == field["i"]


def fill(session, field: dict, value, masked: bool = False) -> None:
    """Type value (str or Secret) into field and verify by length (and the password mask) without reading it.
    Denied(E_PRECONDITION fill_not_verified) after clearing what was typed."""
    want = len(value)
    if not focus(session, field):
        raise Denied("E_PRECONDITION", "the field could not be focused", data={"reason": "fill_not_verified"})
    if field.get("value_length"):
        session.clear_focused()
    session.insert_text(value)
    after = field_by_index(read_form(session), field["i"])
    ok = after is not None and after.get("value_length") == want and (not masked or after.get("masked") is True)
    if not ok:
        clear(session, field)
        raise Denied("E_PRECONDITION", "a filled field did not verify", data={"reason": "fill_not_verified"})


def clear(session, field: dict) -> None:
    """Empty a field this command filled (after a rejected code or a failed check)."""
    try:
        if focus(session, field):
            session.clear_focused()
    except Exception:
        pass


def press_button(session, button: dict) -> None:
    session.click_at(button["x"], button["y"])

() => {
  /* jobhunter driver read_form (read only). Reads back an application form before gate arm. `observed` is
     the observed file (12.7) to write verbatim: {"fields": [{label, value}], "resume_filename_visible"}.
     Password fields are never read. Also reports empty required fields, a visible CAPTCHA and an account
     wall (both mean: job set-status needs_human). Never types, clicks or changes the page. */
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
    const fs = el.closest('fieldset');
    const legend = fs ? fs.querySelector('legend') : null;
    if (legend) { return clean(legend.innerText || legend.textContent); }
    return clean(el.getAttribute('placeholder') || el.getAttribute('name') || '');
  };
  const skipTypes = /^(hidden|password|submit|button|reset|image|search)$/i;
  const inputs = els.filter((el) => ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) &&
    !skipTypes.test(el.getAttribute('type') || '') && (visible(el) || /^(radio|checkbox|file)$/i.test(el.type || '')));
  const forms = Array.from(document.querySelectorAll('form'));
  const best = forms.map((f) => ({ f: f, n: inputs.filter((el) => f.contains(el)).length }))
    .sort((a, b) => b.n - a.n)[0];
  const scoped = best && best.n >= 2 ? inputs.filter((el) => best.f.contains(el)) : inputs;
  const fields = [];
  const seenGroups = new Set();
  const requiredEmpty = [];
  for (const el of scoped) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    let value = '';
    let lab = labelOf(el);
    if (type === 'file') {
      value = el.files && el.files.length ? el.files[0].name : '';
    } else if (type === 'radio') {
      const group = el.getAttribute('name') || lab;
      if (seenGroups.has(group)) { continue; }
      seenGroups.add(group);
      const members = scoped.filter((x) => x.type === 'radio' && (x.getAttribute('name') || '') === (el.getAttribute('name') || ''));
      const checked = members.find((x) => x.checked);
      const fs = el.closest('fieldset');
      const legend = fs ? fs.querySelector('legend') : null;
      if (legend) { lab = clean(legend.innerText || legend.textContent); }
      value = checked ? labelOf(checked) : '';
    } else if (type === 'checkbox') {
      value = el.checked ? 'true' : 'false';
    } else if (el.tagName === 'SELECT') {
      const opt = el.options[el.selectedIndex];
      value = opt ? clean(opt.text) : '';
    } else {
      value = el.value || '';
    }
    if ((el.required || el.getAttribute('aria-required') === 'true') && !value) { requiredEmpty.push(lab); }
    fields.push({ label: lab.slice(0, 300), value: value });
  }
  const combos = els.filter((el) => el.getAttribute('role') === 'combobox' && el.tagName !== 'INPUT' && visible(el));
  for (const el of combos) {
    fields.push({ label: labelOf(el).slice(0, 300), value: clean(el.innerText || el.textContent) });
  }
  const bodyText = (document.body && document.body.innerText) || '';
  const names = bodyText.match(/[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.(pdf|docx?)\b/gi) || [];
  const resume = names.find((n) => /resume|cv/i.test(n)) || names[0] || null;
  const frames = Array.from(document.querySelectorAll('iframe')).map((f) => (f.getAttribute('src') || '') + ' ' +
    (f.getAttribute('title') || ''));
  return {
    observed: { fields: fields, resume_filename_visible: resume },
    required_empty: requiredEmpty,
    captcha_visible: frames.some((s) => /captcha|hcaptcha|recaptcha|challenge/i.test(s)) ||
      /i.m not a robot|verify you are (a )?human/i.test(bodyText),
    account_wall: /create (an )?account|sign (in|up) to (apply|continue)|set a password/i.test(bodyText) &&
      els.some((el) => el.tagName === 'INPUT' && el.type === 'password' && visible(el)),
    validation_errors: els.filter((el) => (el.getAttribute('role') === 'alert' || /error/i.test(
      (el.className && el.className.toString()) || '')) && visible(el)).map((el) => clean(el.innerText)).filter(Boolean)
      .slice(0, 10),
    page_url: location.href
  };
}

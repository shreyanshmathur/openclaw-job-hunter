() => {
  /* jobhunter driver read_compose (read only). Reads back what a compose box holds before gate arm:
     Gmail compose (web_ui route): subject, body, the recipients by field (to, cc, bcc), attachment names, and
     observed_text in the observed-file format that gate arm parses: the header lines "Subject: <subject>" and
     "To: <addresses>" (plus "Cc: ..." and "Bcc: ..." when the window holds such an address), a blank line,
     the body. An address sits in the To, Cc or Bcc row that holds it (a chip or a value typed in the field);
     one that no single row holds counts as To, so an extra address is never read as absent. An inline reply
     (a follow-up) has no subject field, so its subject is "Re: " and the conversation's subject
     (subject_source). LinkedIn message box: the text in the box, the subject field when the compose has one
     (an InMail), and the visible messages of the conversation (sender and text) for the li_message precheck.
     observed_text is the box text for a plain message and "Subject: <subject>", blank line, box text when the
     compose has a subject field, the InMail read-back that gate arm compares with the approved subject and
     body. Never types, clicks or changes the page. */
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
  const txt = (el) => ((el && el.innerText) || '').replace(/\u00a0/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  const host = (location.hostname || '').toLowerCase();
  const editable = (el) => el.getAttribute('contenteditable') === 'true' && visible(el);

  if (host === 'mail.google.com') {
    const subjects = els.filter((el) => el.tagName === 'INPUT' && el.getAttribute('name') === 'subjectbox' && visible(el));
    const bodies = els.filter((el) => editable(el) && /message body/i.test(el.getAttribute('aria-label') || ''));
    const body = bodies[bodies.length - 1] || null;
    const dialog = body ? (body.closest('[role="dialog"]') || body.closest('table') || document.body) : document.body;
    /* an inline reply shows no subject field: its subject is the form's own subject input when it has one,
       else "Re: " and the conversation's subject (the follow-up draft is approved with that subject) */
    const own = body ? els.filter((el) => el.tagName === 'INPUT' && /^(subjectbox|subject)$/.test(
      el.getAttribute('name') || '') && dialog.contains(el) && String(el.value || '').trim() !== '') : [];
    const heads = els.filter((el) => el.tagName === 'H2' && /\bhP\b/.test((el.className && el.className.toString()) || '') &&
      visible(el));
    const thread = heads.length ? txt(heads[0]).replace(/\s+/g, ' ') : '';
    let subject = '';
    let subjectSource = 'none';
    if (subjects.length) { subject = subjects[subjects.length - 1].value; subjectSource = 'field'; }
    else if (own.length) { subject = String(own[own.length - 1].value).trim(); subjectSource = 'reply_field'; }
    else if (body !== null && thread) { subject = /^re\s*:/i.test(thread) ? thread : 'Re: ' + thread; subjectSource = 'thread'; }
    /* recipients: each address chip ([email], outside the body) and each value typed in a recipient field
       belongs to the To, Cc or Bcc row around it (the nearest ancestor holding recipient fields of one kind
       only); no such row: To. A value that holds no address counts as one recipient as it stands. */
    const ADDR = /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}/g;
    const fieldKind = (el) => {
      const name = (el.getAttribute('name') || '').toLowerCase();
      if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && /^(to|cc|bcc)$/.test(name)) { return name; }
      const m = (el.getAttribute('aria-label') || '').match(/^\s*(to|cc|bcc)\s+recipients?\b/i);
      return m ? m[1].toLowerCase() : null;
    };
    const fields = els.filter((el) => dialog.contains(el) && fieldKind(el) !== null)
      .map((el) => ({ el: el, kind: fieldKind(el) }));
    const rowKind = (el) => {
      for (let a = el; a; a = a.parentElement) {
        const kinds = fields.filter((f) => a.contains(f.el)).map((f) => f.kind)
          .filter((k, i, all) => all.indexOf(k) === i);
        if (kinds.length === 1) { return kinds[0]; }
        if (kinds.length > 1 || a === dialog) { return null; }
      }
      return null;
    };
    const tokens = (v) => {
      const s = String(v || '').replace(/\u00a0/g, ' ');
      const found = s.match(ADDR);
      if (found) { return found.map((x) => x.toLowerCase()); }
      const rest = s.replace(/[\s,;]+/g, ' ').trim();
      return rest ? [rest.slice(0, 200)] : [];
    };
    const rcpt = { to: [], cc: [], bcc: [] };
    const add = (kind, list) => { list.forEach((x) => { if (rcpt[kind].indexOf(x) < 0) { rcpt[kind].push(x); } }); };
    Array.from(dialog.querySelectorAll('[email]')).filter((el) => !(body && body.contains(el)))
      .forEach((el) => add(rowKind(el) || 'to', tokens(el.getAttribute('email'))));
    fields.forEach((f) => add(f.kind, tokens(f.el.value)));
    const hline = (name, list) => name + ':' + (list.length ? ' ' + list.join(', ') : '');
    const head = ['Subject: ' + subject, hline('To', rcpt.to)]
      .concat(rcpt.cc.length ? [hline('Cc', rcpt.cc)] : [], rcpt.bcc.length ? [hline('Bcc', rcpt.bcc)] : []);
    const attachments = Array.from(dialog.querySelectorAll('[aria-label]'))
      .map((el) => el.getAttribute('aria-label') || '')
      .filter((s) => /\.(pdf|docx?)\b/i.test(s))
      .map((s) => (s.match(/[^\s"]+\.(pdf|docx?)\b/i) || [s])[0]);
    const bodyText = txt(body);
    return {
      platform: 'gmail', compose_open: body !== null, subject: subject, subject_source: subjectSource, body: bodyText,
      to: rcpt.to, cc: rcpt.cc, bcc: rcpt.bcc, attachments: Array.from(new Set(attachments)),
      signature_block_present: body ? body.querySelector('.gmail_signature') !== null : false,
      observed_text: head.join('\n') + '\n\n' + bodyText
    };
  }

  if (host === 'linkedin.com' || host.endsWith('.linkedin.com')) {
    const boxes = els.filter((el) => editable(el) && (el.getAttribute('role') === 'textbox' ||
      /msg-form|message/i.test((el.className && el.className.toString()) || '') ||
      /write a message/i.test(el.getAttribute('aria-label') || '')));
    const box = boxes[boxes.length - 1] || null;
    const subjectField = (el) => (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && visible(el) &&
      /^(text|search)?$/i.test(el.getAttribute('type') || '') && (el.getAttribute('name') === 'subject' ||
      /subject/i.test(el.getAttribute('aria-label') || '') || /subject/i.test(el.getAttribute('placeholder') || '') ||
      /msg-form__subject/i.test((el.className && el.className.toString()) || ''));
    /* the subject field of this compose only: the nearest of the box's form, conversation bubble or dialog that
       holds one; the whole page only when the box sits in none of them */
    const fields = els.filter(subjectField);
    const scopes = box ? [box.closest('form'), box.closest('.msg-overlay-conversation-bubble'),
      box.closest('[role="dialog"]')].filter(Boolean) : [];
    const scoped = scopes.map((sc) => fields.filter((el) => sc.contains(el))).filter((l) => l.length > 0);
    const pick = box === null ? [] : (scoped.length ? scoped[0] : (scopes.length ? [] : fields));
    const subjectEl = pick[pick.length - 1] || null;
    const subject = subjectEl ? String(subjectEl.value || '').replace(/\u00a0/g, ' ').trim() : null;
    const boxText = box ? txt(box) : null;
    const events = els.filter((el) => el.tagName === 'LI' && /msg-s-message-list__event|message-list/i.test(
      (el.className && el.className.toString()) || '') && visible(el));
    const messages = events.slice(-20).map((li) => {
      const name = li.querySelector('.msg-s-message-group__name, [data-anonymize="person-name"]');
      const body = li.querySelector('.msg-s-event-listitem__body, p');
      return { sender: txt(name).slice(0, 120) || null, text: txt(body || li).slice(0, 1000) };
    });
    return {
      platform: 'linkedin', box_present: box !== null, text: boxText,
      subject_field_present: subjectEl !== null, subject: subject,
      observed_text: box === null ? null : (subjectEl ? 'Subject: ' + subject + '\n\n' + boxText : boxText),
      messages: messages, page_url: location.href
    };
  }

  const areas = els.filter((el) => (el.tagName === 'TEXTAREA' && visible(el)) || editable(el));
  return {
    platform: 'other',
    boxes: areas.slice(0, 10).map((el) => ({
      label: (el.getAttribute('aria-label') || el.getAttribute('name') || el.getAttribute('placeholder') || '').slice(0, 120),
      text: el.tagName === 'TEXTAREA' ? el.value : txt(el)
    }))
  };
}

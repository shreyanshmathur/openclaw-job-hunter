() => {
  /* jobhunter driver read_gmail_list (read only, web_ui route). A Gmail list view: a search, the inbox, Sent,
     Outbox or Scheduled. Returns the visible conversation rows (participant addresses, subject, snippet,
     date, thread id, unread), `count` (the number of rows) and whether the list is complete. Used for the
     precheck counts (Sent, Outbox and Scheduled searches from gate precheck-plan), the reply and
     delivery-failure searches of the replies lane, and the Sent-folder read of mail audit (`sent_read`).
     `loaded` false means the list could not be read: `count` is null and nothing may be concluded from it.
     Never clicks or changes the page. */
  const host = (location.hostname || '').toLowerCase();
  const hash = location.hash || '';
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const clean = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const cls = (el) => ((el && el.className && el.className.toString()) || '');
  const iso = (s) => {
    const t = Date.parse(s || '');
    return isNaN(t) ? null : new Date(t).toISOString().slice(0, 19) + 'Z';
  };
  const nowIso = new Date().toISOString().slice(0, 19) + 'Z';
  if (host !== 'mail.google.com') {
    return { platform: 'other', view: null, loaded: false, count: null, rows: [], page_url: location.href };
  }
  const dec = (s) => { try { return decodeURIComponent(s.replace(/\+/g, ' ')); } catch (e) { return s; } };
  const parts = hash.replace(/^#/, '').split('/');
  const folder = parts[0] || 'inbox';
  const query = (folder === 'search' || folder === 'advanced-search') && parts[1] ? dec(parts[1]) : null;
  const labels = Array.from(document.querySelectorAll('a[aria-label], button[aria-label]'))
    .map((el) => el.getAttribute('aria-label') || '').filter((s) => /google account/i.test(s));
  let owner = null;
  for (const s of labels.concat([document.title || ''])) {
    const m = s.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
    if (m) { owner = m[0].toLowerCase(); break; }
  }
  const mains = Array.from(document.querySelectorAll('div[role="main"]')).filter(visible);
  const main = mains[0] || null;
  if (main === null) {
    return { platform: 'gmail', view: 'list', folder: folder, query: query, loaded: false, count: null, rows: [],
      account_email: owner, page_url: location.href };
  }
  let trs = Array.from(main.querySelectorAll('tr.zA')).filter(visible);
  if (trs.length === 0) {
    trs = Array.from(main.querySelectorAll('tr[role="row"]')).filter((tr) => visible(tr) &&
      (tr.querySelector('span[email]') !== null || tr.querySelector('[data-legacy-thread-id]') !== null));
  }
  const base = (location.origin || 'https://mail.google.com') + (location.pathname || '/mail/u/0/');
  const rows = trs.slice(0, 200).map((tr) => {
    const people = [];
    for (const el of Array.from(tr.querySelectorAll('span[email]'))) {
      const email = (el.getAttribute('email') || '').toLowerCase();
      if (email && !people.some((p) => p.email === email)) {
        people.push({ name: clean(el.getAttribute('name') || el.innerText).slice(0, 120), email: email });
      }
    }
    const legacy = tr.querySelector('[data-legacy-thread-id]');
    const tid = tr.querySelector('[data-thread-id]');
    const threadId = legacy ? legacy.getAttribute('data-legacy-thread-id') :
      (tid ? (tid.getAttribute('data-thread-id') || '').replace(/^#?thread-[a-z]:/, '') : null);
    const subj = tr.querySelector('span.bog') || tr.querySelector('.bqe') || tr.querySelector('.y6');
    const snip = tr.querySelector('span.y2');
    const xw = tr.querySelector('td.xW');
    const dateEl = (xw && xw.querySelector('span[title]')) || tr.querySelector('span[title]');
    const dateTitle = dateEl ? dateEl.getAttribute('title') : null;
    return {
      thread_id: threadId || null,
      participants: people,
      to: people.map((p) => p.email).filter((e) => e !== owner),
      subject: clean(subj && subj.innerText).slice(0, 300),
      snippet: clean(snip && snip.innerText).replace(/^[-\s]+/, '').slice(0, 300),
      date_title: dateTitle,
      date_text: clean(dateEl && dateEl.innerText),
      date: iso(dateTitle),
      unread: /\bzE\b/.test(cls(tr)),
      url: threadId ? base + '#all/' + threadId : null
    };
  });
  const mainText = clean(main.innerText);
  const empty = rows.length === 0 && /no messages matched your search|no (sent |scheduled )?(messages|conversations)|there are no (messages|conversations)|nothing (to see|in (the )?(outbox|scheduled))|(outbox|scheduled( folder)?) is empty/i.test(mainText);
  const pageText = clean((document.body && document.body.innerText) || '').slice(0, 20000);
  const rm = pageText.match(/(\d[\d,]*)\s*[-\u2013]\s*(\d[\d,]*)\s+of\s+(many|about\s+[\d,]+|[\d,]+)/i);
  const num = (s) => parseInt(String(s).replace(/[^\d]/g, ''), 10);
  const range = rm ? { first: num(rm[1]), last: num(rm[2]), total: /many|about/i.test(rm[3]) ? null : num(rm[3]) } : null;
  const loaded = rows.length > 0 || empty;
  const complete = loaded && (empty || (range ? (range.total !== null && range.last >= range.total && range.first === 1) :
    rows.length < 50));
  const dated = rows.filter((r) => r.to.length > 0 && r.date !== null && r.url !== null);
  return {
    platform: 'gmail', view: 'list', folder: folder, query: query, loaded: loaded,
    count: loaded ? rows.length : null, empty: empty, complete: complete, range: range, rows: rows,
    account_email: owner, page_url: location.href,
    sent_read: {
      observed_at: nowIso, query: query, complete: complete && dated.length === rows.length,
      messages: dated.map((r) => ({ date: r.date, to: r.to, subject: r.subject, url: r.url }))
    }
  };
}

() => {
  /* jobhunter driver read_li_invite_dialog (read only). On a LinkedIn profile page it reports the top card
     (name, vanity slug, degree, primary button, whether the More menu holds Connect) for the li_invite and
     li_message prechecks, and, when the invitation dialog is open, exactly what the note box holds. The
     dialog can live inside an open shadow root (interop-outlet), so every open shadow root is walked (depth
     at most 20). Never clicks, types or changes the page. */
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
  const txt = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  const leaf = (el) => el.children.length === 0;
  const label = (el) => (el.getAttribute('aria-label') || '').trim();

  const dialogs = els.filter((el) => (el.getAttribute('role') === 'dialog' || el.tagName === 'DIALOG') && visible(el));
  const dialog = dialogs.find((d) => /invit|connect|add a note|personali[sz]e|note/i.test(txt(d))) || null;
  const inDialog = (el) => dialog !== null && (dialog === el || dialog.contains(el));
  const boxes = els.filter((el) => el.tagName === 'TEXTAREA' && visible(el) &&
    (inDialog(el) || /know each other|add a note|message/i.test(el.getAttribute('placeholder') || '')));
  const box = boxes[0] || null;
  const dialogText = txt(dialog);
  const buttonLike = els.filter((el) => (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') && inDialog(el));
  const sendBtn = buttonLike.find((b) => /^send( now| without a note| invitation)?$/i.test(txt(b)) ||
    /^send/i.test(label(b))) || null;
  const addNote = els.some((el) => inDialog(el) && leaf(el) && /^add a note$/i.test(txt(el)) && visible(el));
  const counter = (dialogText.match(/\b\d{1,3}\s*\/\s*\d{2,3}\b/) || [null])[0];
  const invitee = (dialogText.match(/(?:invite|connect with)\s+(.{1,80}?)\s+to connect/i) || [null, null])[1];

  const h1 = document.querySelector('main h1') || document.querySelector('h1');
  let card = h1;
  for (let i = 0; i < 8 && card && card.parentElement && card.tagName !== 'SECTION'; i++) { card = card.parentElement; }
  const cardEls = card ? Array.from(card.querySelectorAll('button, a, [role="button"], span, div')) : [];
  const cardText = txt(card).slice(0, 800);
  const canon = document.querySelector('link[rel="canonical"]');
  const path = (canon && canon.getAttribute('href')) || location.href || '';
  const slugMatch = path.match(/linkedin\.com\/in\/([^/?#]+)/i) || (location.pathname || '').match(/^\/in\/([^/?#]+)/i);
  let slug = slugMatch ? decodeURIComponent(slugMatch[1]).toLowerCase() : null;
  if (slug && /^acoa/i.test(slug)) { slug = null; }
  const shown = cardEls.filter(visible);
  const has = (rx, list) => list.some((el) => rx.test(label(el)) || (leaf(el) && rx.test(txt(el))) ||
    ((el.tagName === 'BUTTON' || el.tagName === 'A') && rx.test(txt(el))));
  const connectRx = /^(connect|invite .+ to connect)$/i;
  let primary = 'None';
  if (has(/^pending\b|withdraw invitation/i, shown)) { primary = 'Pending'; }
  else if (has(connectRx, shown)) { primary = 'Connect'; }
  else if (has(/^message\b/i, shown)) { primary = 'Message'; }
  else if (has(/^follow\b/i, shown)) { primary = 'Follow'; }
  const hidden = cardEls.filter((el) => !visible(el));
  const moreHasConnect = has(connectRx, hidden);
  const degree = (cardText.match(/\b(1st|2nd|3rd)\b/) || [null])[0];
  return {
    page_url: location.href,
    dialog_open: dialog !== null,
    dialog_text: dialogText.slice(0, 400),
    invitee_name: invitee,
    add_note_visible: addNote,
    note_box_present: box !== null,
    note_text: box ? box.value : null,
    note_length: box ? box.value.length : null,
    note_max: box && box.maxLength > 0 ? box.maxLength : null,
    counter_text: counter,
    send_button: { present: sendBtn !== null, enabled: sendBtn !== null && !sendBtn.disabled &&
      sendBtn.getAttribute('aria-disabled') !== 'true' },
    email_required: /email address needed|enter their email|to verify this member knows you/i.test(dialogText),
    limit_notice: /weekly invitation limit|invitation limit|you.re out of|personalized invitations/i.test(dialogText),
    profile: { name: txt(h1).slice(0, 120) || null, vanity_slug: slug, degree: degree, primary_button: primary,
      more_menu_has_connect: moreHasConnect, open_to_work: /#opentowork|open to work/i.test(cardText) }
  };
}

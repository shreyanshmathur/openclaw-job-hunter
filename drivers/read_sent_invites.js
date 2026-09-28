() => {
  /* jobhunter driver read_sent_invites (read only). On https://www.linkedin.com/mynetwork/invitation-manager/sent/
     it returns the "People (N)" count for the li_invites_sent_7d and pending gauges and the visible sent
     invitations (name, profile URL, vanity slug, how long ago) for reconcile and acceptance checks. An
     invitation that disappears from this list while the profile shows 1st degree was accepted. */
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const main = document.querySelector('main') || document.body;
  const text = clean(main.innerText).slice(0, 40000);
  const count = text.match(/people\s*\((\d{1,4})\)/i) || text.match(/sent\s*\((\d{1,4})\)/i);
  const cards = Array.from(main.querySelectorAll('li, [data-view-name*="invitation"]'));
  const seen = new Set();
  const invites = [];
  for (const card of cards) {
    const a = card.querySelector('a[href*="/in/"]');
    if (!a) { continue; }
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/in\/([^/?#]+)/i);
    if (!m) { continue; }
    const slug = decodeURIComponent(m[1]).toLowerCase();
    if (seen.has(slug)) { continue; }
    seen.add(slug);
    const t = clean(card.innerText);
    const ago = t.match(/sent (today|yesterday|\d+ (minute|hour|day|week|month|year)s? ago)/i);
    invites.push({
      name: clean(a.innerText || a.getAttribute('aria-label')).split(' View ')[0].slice(0, 120),
      profile_url: 'https://www.linkedin.com' + '/in/' + m[1] + '/',
      vanity_slug: /^acoa/i.test(slug) ? null : slug,
      sent_text: ago ? ago[0] : null
    });
    if (invites.length >= 100) { break; }
  }
  return {
    on_sent_page: /invitation-manager\/sent/i.test(location.pathname || ''),
    people_count: count ? parseInt(count[1], 10) : null,
    invites: invites,
    page_url: location.href
  };
}

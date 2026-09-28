() => {
  /* jobhunter driver read_identity (read only). Returns the identity file (12.12) for jh.py identity check.
     LinkedIn: run it on your own profile page (path /in/me/ on linkedin.com): display name
     from the top card, profile URL from the canonical link. Gmail (web_ui route only): the account address
     from the account button label or the window title. Never clicks or changes the page. */
  const host = (location.hostname || '').toLowerCase();
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  if (host === 'mail.google.com') {
    const labels = Array.from(document.querySelectorAll('a[aria-label], button[aria-label]'))
      .map((el) => el.getAttribute('aria-label') || '').filter((s) => /google account/i.test(s));
    const pool = labels.concat([document.title || '']);
    let email = null;
    for (const s of pool) {
      const m = s.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
      if (m) { email = m[0].toLowerCase(); break; }
    }
    return { platform: 'gmail', observed: { account_email: email } };
  }
  if (host === 'linkedin.com' || host.endsWith('.linkedin.com')) {
    const h1 = document.querySelector('main h1') || document.querySelector('h1');
    const canon = document.querySelector('link[rel="canonical"]');
    const href = (canon && canon.getAttribute('href')) || location.href || '';
    const m = href.match(/^https:\/\/(?:[a-z]{2,3}\.)?linkedin\.com\/in\/([^/?#]+)/i);
    const photo = document.querySelector('img.global-nav__me-photo, .global-nav__me img');
    const navName = photo ? clean(photo.getAttribute('alt')) : null;
    return {
      platform: 'linkedin',
      observed: {
        display_name: clean(h1 && h1.innerText) || navName || null,
        profile_url: m ? 'https://www.linkedin.com' + '/in/' + m[1] + '/' : null
      },
      nav_name: navName,
      on_own_profile: /\/in\/me\/?$/.test(location.pathname || '') || /edit/i.test(clean(
        (document.querySelector('main') || document.body).innerText).slice(0, 2000))
    };
  }
  return { platform: 'other', observed: {}, page_url: location.href };
}

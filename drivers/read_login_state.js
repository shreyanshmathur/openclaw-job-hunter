() => {
  /* jobhunter driver read_login_state (read only). The login check of a consented site in the jobhunter
     browser profile: which site the page is on, the Google account address on Gmail, whether a password
     field, a CAPTCHA or a verification text is on screen, and on LinkedIn whether the feed loaded. `state`
     is ok, logged_out, checkpoint or unknown (the same rules as the installer's login check, whose probe
     fields this object also carries: jh_login_probe, url, title, account_email, password_field, captcha,
     challenge_text). On Gmail `identity` is the identity file (12.12) for jh.py identity check; on LinkedIn
     `nav_name` is the name on the signed-in account's photo (run read_identity.js on /in/me/ for the check).
     Never clicks, types, stores or navigates. */
  const host = (location.hostname || '').toLowerCase();
  const url = location.href || '';
  const path = (location.pathname || '').toLowerCase();
  const title = (document.title || '').slice(0, 200);
  const text = ((document.body && document.body.innerText) || '').slice(0, 5000);
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const on = (suffix) => host === suffix || host.endsWith('.' + suffix);
  const SITES = [['mail.google.com', 'gmail'], ['accounts.google.com', 'gmail'], ['linkedin.com', 'linkedin'],
    ['naukri.com', 'naukri'], ['indeed.com', 'indeed'], ['glassdoor.com', 'glassdoor'],
    ['glassdoor.co.in', 'glassdoor'], ['foundit.in', 'foundit'], ['instahyre.com', 'instahyre'],
    ['wellfound.com', 'wellfound'], ['cutshort.io', 'cutshort'], ['hirist.tech', 'hirist'],
    ['iimjobs.com', 'iimjobs'], ['workatastartup.com', 'yc'], ['ycombinator.com', 'yc']];
  const hit = SITES.find((s) => on(s[0]));
  const site = hit ? hit[1] : null;
  const labels = Array.from(document.querySelectorAll('a[aria-label], button[aria-label]'))
    .map((el) => el.getAttribute('aria-label') || '').filter((s) => /google account/i.test(s));
  let email = null;
  for (const s of labels.concat([document.title || ''])) {
    const m = s.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}/);
    if (m) { email = m[0].toLowerCase(); break; }
  }
  const password = Array.from(document.querySelectorAll('input[type="password"]')).some(visible);
  const captcha = Array.from(document.querySelectorAll('iframe')).some((f) =>
    /recaptcha|hcaptcha|challenges\.cloudflare|arkoselabs|funcaptcha|captcha/i.test((f.getAttribute('src') || '') + ' ' +
      (f.getAttribute('title') || ''))) || document.querySelector('.g-recaptcha, [data-sitekey]') !== null;
  const challenge = /verify it'?s you|unusual activity|security check|confirm it'?s you|are you a robot|verify you are human|security verification|let'?s do a quick security check|enter the code/i.test(text);
  const low = (host + path + '?' + (location.search || '')).toLowerCase();
  const checkpointUrl = ['/checkpoint', '/challenge', 'captcha', '/sorry/', '/verify', '/signin/rejected',
    '/interstitial'].some((w) => low.indexOf(w) >= 0);
  const loggedOutUrl = ['/login', '/signin', '/sign-in', '/sign_in', '/authwall', '/uas/login', '/auth/', '/nlogin',
    '/account/login', 'servicelogin'].some((w) => low.indexOf(w) >= 0) || host === 'accounts.google.com' ||
    host === 'secure.indeed.com';
  const photo = document.querySelector('img.global-nav__me-photo');
  const feed = site === 'linkedin' && path.indexOf('/feed') === 0 && (photo !== null ||
    document.querySelector('.scaffold-finite-scroll, main.scaffold-layout__main') !== null ||
    Array.from(document.querySelectorAll('[data-urn]')).some((el) => /urn:li:activity/.test(el.getAttribute('data-urn') || '')));
  const gmailOpen = host === 'mail.google.com' && document.querySelector('div[role="main"]') !== null;
  let state = 'unknown';
  if (captcha || challenge || checkpointUrl) { state = 'checkpoint'; }
  else if (password || loggedOutUrl) { state = 'logged_out'; }
  else if (site === 'gmail') { state = gmailOpen && email !== null ? 'ok' : 'unknown'; }
  else if (site === 'linkedin') { state = feed ? 'ok' : 'unknown'; }
  else if (site !== null) { state = 'ok'; }
  let identity = null;
  if (site === 'gmail' && state === 'ok') { identity = { platform: 'gmail', observed: { account_email: email } }; }
  return {
    jh_login_probe: 1, site: site, state: state, url: url, title: title, account_email: email,
    password_field: password, captcha: captcha, challenge_text: challenge, checkpoint_url: checkpointUrl,
    logged_out_url: loggedOutUrl, feed_loaded: feed, identity: identity,
    nav_name: photo ? clean(photo.getAttribute('alt')) || null : null
  };
}

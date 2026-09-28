() => {
  /* jobhunter driver read_toast (read only). After the single commit click: visible toasts, alerts and
     live regions, plus known success and error phrases on the page. LinkedIn invitations are proven by
     "Invitation sent" only (a missing "Pending" is not a failure). Never clicks or changes the page. */
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const nodes = Array.from(document.querySelectorAll(
    '[role="alert"], [role="status"], [aria-live], .artdeco-toast-item, [data-test-artdeco-toast-item-type]'));
  const toasts = Array.from(new Set(nodes.filter(visible).map((el) => clean(el.innerText)).filter(Boolean)))
    .slice(0, 10).map((s) => s.slice(0, 300));
  const body = clean((document.body && document.body.innerText) || '').slice(0, 20000);
  const SUCCESS = [/invitation sent/i, /message sent/i, /your message (has been|was) sent/i,
    /application (has been |was )?(submitted|received|sent)/i, /thank(s| you) for applying/i,
    /we(.ve| have) received your application/i, /your application (is|has been) (complete|submitted)/i,
    /successfully applied/i, /\bapplied\b.{0,40}\bsuccess/i];
  const ERROR = [/something went wrong/i, /try again/i, /unable to (send|submit)/i, /could not be (sent|submitted)/i,
    /limit (reached|exceeded)/i, /please (fix|correct) the (errors|following)/i, /is required/i];
  const hits = (list, s) => list.filter((rx) => rx.test(s)).map((rx) => rx.source);
  const all = toasts.join(' | ') + ' | ' + body;
  return {
    toasts: toasts,
    success_phrases: hits(SUCCESS, all),
    error_phrases: hits(ERROR, toasts.join(' | ')),
    page_error_phrases: hits(ERROR, body),
    page_url: location.href,
    title: (document.title || '').slice(0, 300)
  };
}

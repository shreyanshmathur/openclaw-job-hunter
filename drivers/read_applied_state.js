() => {
  /* jobhunter driver read_applied_state (read only). Application precheck on a posting or apply page:
     `checks` is the precheck list for kind application (12.7) to put in the precheck file verbatim.
     Scoped to the main content, so "applied" in a sidebar of other jobs does not count. */
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const main = document.querySelector('main') || document.querySelector('[role="main"]') || document.body;
  const text = clean(main.innerText).slice(0, 30000);
  const leaves = Array.from(main.querySelectorAll('span, div, p, button, a, li, strong'))
    .filter((el) => el.children.length === 0 && visible(el)).map((el) => clean(el.innerText)).filter(Boolean);
  const BADGE = /^(applied|application submitted|already applied|you applied|applied on .{1,30}|applied \d+ (minute|hour|day|week|month)s? ago)$/i;
  const ALREADY = /(you('| ha)ve already applied|you already applied|already applied (to|for) this|you applied (on|for)|application (was |has been )?already submitted|we already have your application)/i;
  const badges = leaves.filter((s) => BADGE.test(s)).slice(0, 5);
  const already = text.match(ALREADY);
  return {
    checks: [{ name: 'applied_badge', value: badges.length > 0 }, { name: 'already_applied_text', value: already !== null }],
    matched: badges.concat(already ? [already[0]] : []),
    page_url: location.href,
    title: (document.title || '').slice(0, 300)
  };
}

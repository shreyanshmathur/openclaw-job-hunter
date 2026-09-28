import test from "node:test";
import assert from "node:assert/strict";
import { actRequestOf, blockingScopes, classifyBrowserCall, classifyUrl, parseSnapshotRefs, RefCache, scriptAllowed, sha256Hex, targetIdOf, tokenMatchesHost } from "../src/browser.ts";
import { DRIVER_HASHES, DRIVER_TEXT, loadHosts } from "./_helpers.ts";
import type { RefInfo } from "../src/types.ts";

const hosts = loadHosts();

test("never hosts, schemes and networks", () => {
  const never = [
    "https://docs.google.com/spreadsheets/d/abc/edit",
    "https://sheets.google.com/",
    "https://drive.google.com/drive/my-drive",
    "https://script.google.com/home/projects",
    "https://myaccount.google.com/security",
    "https://accounts.google.com/signin",
    "file:///etc/passwd",
    "chrome://settings",
    "javascript:alert(1)",
    "data:text/html,hi",
    "http://localhost:18789/",
    "http://127.0.0.1:9222/json",
    "http://[::1]/",
    "http://192.168.1.1/",
    "http://10.0.0.5/admin",
    "https://www.linkedin.com/mypreferences/d/categories/account",
    "https://user:pw@example.com/x",
    "not a url",
  ];
  for (const u of never) assert.equal(classifyUrl(u, hosts).never, true, u);
  const ok = ["https://www.linkedin.com/in/example-person", "https://mail.google.com/mail/u/0/#inbox", "about:blank", "https://kestrel.example/careers"];
  for (const u of ok) assert.equal(classifyUrl(u, hosts).never, false, u);
  assert.equal(classifyUrl(null, hosts).never, false);
});

test("platform mapping and token matching", () => {
  const gh = classifyUrl("https://job-boards.greenhouse.io/kestrel/jobs/123", hosts);
  assert.equal(gh.platform?.key, "greenhouse");
  assert.equal(gh.platform?.scope, "ats");
  assert.equal(tokenMatchesHost("greenhouse", gh), true);
  assert.equal(tokenMatchesHost("lever", gh), false);
  assert.equal(tokenMatchesHost("ats", gh), false);
  const nk = classifyUrl("https://www.naukri.com/job-listings-123", hosts);
  assert.equal(nk.platform?.scope, "site:naukri");
  assert.equal(tokenMatchesHost("naukri", nk), true);
  assert.equal(tokenMatchesHost("site:naukri", nk), true);
  const li = classifyUrl("https://www.linkedin.com/in/example-person/", hosts);
  assert.equal(tokenMatchesHost("linkedin", li), true);
  const other = classifyUrl("https://kestrel.example/careers", hosts);
  assert.equal(other.platform, null);
  assert.equal(tokenMatchesHost("greenhouse", other), false);
  assert.deepEqual(blockingScopes(li, false), ["global", "pause:all", "linkedin", "pause:linkedin"]);
  assert.ok(blockingScopes(gh, true).includes("pause:applications"));
  assert.ok(!blockingScopes(gh, false).includes("pause:applications"));
});

const refs: Record<string, RefInfo> = {
  e1: { role: "link", name: "Jobs" },
  e2: { role: "button", name: "Show more results" },
  e3: { role: "button", name: "Send" },
  e4: { role: "textbox", name: "First name" },
  e5: { role: "button", name: "Connect" },
  e6: { role: "button", name: "Submit application" },
  e7: { role: "combobox", name: "Country" },
  e8: { role: "button", name: "Save and see more" },
  e9: { role: "button", name: "Next" },
  e10: { role: "button", name: "...see more" },
  e11: { role: "link", name: "Message" },
  e12: { role: "link", name: "Easy Apply" },
  e13: { role: "button", name: "Attach resume" },
  e14: { role: "link", name: "Jobs posted this week" },
};

function cls(params: Record<string, unknown>, tokenKind: string | null = null) {
  return classifyBrowserCall(params, { hosts, driverHashes: DRIVER_HASHES, tokenKind, lookupRef: (r) => refs[r] }).map((i) => i.cls);
}

test("action classes", () => {
  assert.deepEqual(cls({ action: "navigate", targetUrl: "https://x.example" }), ["read"]);
  assert.deepEqual(cls({ action: "snapshot" }), ["read"]);
  assert.deepEqual(cls({ action: "screenshot" }), ["read"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e1" }), ["nav_click"]);
  assert.deepEqual(cls({ action: "act", request: { kind: "click", ref: "e2" } }), ["nav_click"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e8" }), ["commit"]); // harmless prefix but risky word
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e3" }), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e99" }), ["commit"]); // uncached
  assert.deepEqual(cls({ action: "act", kind: "click" }), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "clickCoords", x: 1, y: 2 }), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e4" }), ["fill"]);
  assert.deepEqual(cls({ action: "act", kind: "type", ref: "e4", text: "Alex", slowly: true }), ["fill"]);
  assert.deepEqual(cls({ action: "act", kind: "type", ref: "e4", text: "Alex", submit: true }), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "press", key: "Enter" }), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "press", key: "Control+Enter" }), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "press", key: "Tab" }), ["fill"]);
  assert.deepEqual(cls({ action: "act", kind: "press", key: "PageDown" }), ["read"]);
  assert.deepEqual(cls({ action: "act", kind: "select", ref: "e7", values: ["India"] }), ["fill"]);
  assert.deepEqual(cls({ action: "act", kind: "fill", fields: [{ ref: "e4", type: "text", value: "A" }] }), ["fill"]);
  assert.deepEqual(cls({ action: "act", kind: "evaluate", fn: DRIVER_TEXT }), ["driver"]);
  assert.deepEqual(cls({ action: "act", kind: "evaluate", fn: DRIVER_TEXT + "\n" }), ["driver"]);
  assert.deepEqual(cls({ action: "act", kind: "evaluate", fn: "() => fetch('/voyager/api/x')" }), ["script_denied"]);
  assert.deepEqual(cls({ action: "act", kind: "wait", fn: "window.x = 1" }), ["script_denied"]);
  assert.deepEqual(cls({ action: "act", kind: "wait", text: "Done" }), ["read"]);
  assert.deepEqual(cls({ action: "act", kind: "hover", ref: "e3" }), ["read"]);
  assert.deepEqual(cls({ action: "act", kind: "drag", startRef: "e1", endRef: "e2" }), ["commit"]);
  assert.deepEqual(cls({ action: "dialog", accept: true }), ["commit"]);
  assert.deepEqual(cls({ action: "dialog", accept: false }), ["nav_click"]);
  assert.deepEqual(cls({ action: "upload", paths: ["/tmp/openclaw/uploads/a.pdf"], ref: "e9" }), ["fill"]);
  assert.deepEqual(cls({ action: "importprofile" }), ["forbidden"]);
  assert.deepEqual(cls({ action: "download", ref: "e1", path: "x.pdf" }), ["nav_click"]);
  assert.deepEqual(cls({ action: "something_new" }), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "batch", actions: [{ kind: "click", ref: "e4" }, { kind: "type", ref: "e4", text: "x" }, { kind: "click", ref: "e3" }] }), ["fill", "fill", "commit"]);
});

test("prepare buttons are fill only for the token's kind", () => {
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e5" }, "li_invite"), ["fill"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e5" }, "application"), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e5" }, null), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e9" }, "application"), ["fill"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e6" }, "application"), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e13" }, "application"), ["fill"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e13" }, null), ["commit"]);
});

test("links with action names are not harmless", () => {
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e10" }), ["nav_click"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e11" }), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e11" }, "li_message"), ["fill"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e12" }), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e14" }), ["nav_click"]);
});

test("driver hash allowlist", () => {
  assert.equal(scriptAllowed(DRIVER_TEXT, DRIVER_HASHES), true);
  assert.equal(scriptAllowed("  " + DRIVER_TEXT + "\n\n", DRIVER_HASHES), true);
  assert.equal(scriptAllowed(DRIVER_TEXT + ";", DRIVER_HASHES), false);
  assert.equal(scriptAllowed("", DRIVER_HASHES), false);
  assert.equal(scriptAllowed(undefined, DRIVER_HASHES), false);
  assert.equal(sha256Hex("abc").length, 64);
});

test("snapshot ref parsing (role, ai and aria formats)", () => {
  const text = [
    "<<<EXTERNAL_UNTRUSTED_CONTENT>>>",
    "- main:",
    "  - heading \"Senior Analyst\" [level=1]",
    "  - link \"Kestrel Commerce\" [ref=e1] [cursor=pointer]:",
    "  - button \"Apply now\" [ref=e2]",
    "  - checkbox \"I agree\" [checked] [ref=f1e3]",
    "  - textbox [ref=e4]",
    "  - button \"Say \\\"hi\\\"\" [ref=e5] [nth=1]",
  ].join("\n");
  const m = parseSnapshotRefs(text);
  assert.deepEqual(m.get("e1"), { role: "link", name: "Kestrel Commerce" });
  assert.deepEqual(m.get("e2"), { role: "button", name: "Apply now" });
  assert.deepEqual(m.get("f1e3"), { role: "checkbox", name: "I agree" });
  assert.deepEqual(m.get("e4"), { role: "textbox", name: "" });
  assert.deepEqual(m.get("e5"), { role: "button", name: "Say \"hi\"" });
  const aria = parseSnapshotRefs(JSON.stringify({ format: "aria", nodes: [{ ref: "ax1", role: "Button", name: "Send" }, { ref: "ax2", role: "link", name: "Home" }] }));
  assert.deepEqual(aria.get("ax1"), { role: "button", name: "Send" });
  assert.equal(aria.size, 2);
});

test("ref cache keeps refs per session and tab", () => {
  const c = new RefCache();
  c.replaceRefs("s1", "t1", new Map([["e1", { role: "link", name: "A" }]]));
  c.setUrl("s1", "t1", "https://www.linkedin.com/feed/");
  assert.deepEqual(c.lookup("s1", "t1", "e1"), { role: "link", name: "A" });
  assert.deepEqual(c.lookup("s1", null, "e1"), { role: "link", name: "A" }); // last tab
  assert.equal(c.lookup("s2", "t1", "e1"), undefined);
  assert.equal(c.url("s1", null), "https://www.linkedin.com/feed/");
  c.clearRefs("s1", "t1");
  assert.equal(c.lookup("s1", "t1", "e1"), undefined);
});

test("act keys at the top level fill in a request the way OpenClaw does", () => {
  // missing request keys come from the top level
  assert.deepEqual(actRequestOf({ action: "act", request: { kind: "wait" }, fn: "window.x = 1" }), { kind: "wait", fn: "window.x = 1" });
  assert.deepEqual(cls({ action: "act", request: { kind: "wait" }, fn: "window.x = 1" }), ["script_denied"]);
  assert.deepEqual(cls({ action: "act", request: { kind: "type", ref: "e4", text: "x" }, submit: true }), ["commit"]);
  assert.deepEqual(cls({ action: "act", request: { kind: "press" }, key: "Enter" }), ["commit"]);
  assert.deepEqual(cls({ action: "act", request: { kind: "batch" }, actions: [{ kind: "click", ref: "e6" }, { kind: "click", ref: "e3" }] }), ["commit", "commit"]);
  assert.deepEqual(cls({ action: "act", request: { kind: "evaluate" }, fn: DRIVER_TEXT }), ["driver"]);
  // keys the request has win; a request without kind takes the top-level kind
  assert.deepEqual(cls({ action: "act", request: { kind: "type", ref: "e4", text: "x", submit: false }, submit: true }), ["fill"]);
  assert.deepEqual(cls({ action: "act", request: { ref: "e6" }, kind: "click" }), ["commit"]);
  // different kinds: only targetId is shared, OpenClaw runs the request
  assert.deepEqual(actRequestOf({ action: "act", kind: "click", ref: "e6", targetId: "t1", request: { kind: "wait", text: "Done" } }), { kind: "wait", text: "Done", targetId: "t1" });
  assert.deepEqual(cls({ action: "act", kind: "click", ref: "e6", request: { kind: "wait", text: "Done" } }), ["read"]);
  // no request object: only the act keys of the top level, and nothing without a kind
  assert.deepEqual(actRequestOf({ action: "act", kind: "click", ref: "e1", profile: "jobhunter" }), { kind: "click", ref: "e1" });
  assert.equal(actRequestOf({ action: "act", ref: "e1" }), null);
  assert.deepEqual(cls({ action: "act", ref: "e1" }), ["commit"]);
});

test("the tab of a call is picked the way OpenClaw picks it", () => {
  assert.equal(targetIdOf({ action: "act", targetId: "A", request: { kind: "click", ref: "e5", targetId: "B" } }), "B");
  assert.equal(targetIdOf({ action: "act", targetId: "A", request: { kind: "click", ref: "e5" } }), "A");
  assert.equal(targetIdOf({ action: "act", kind: "click", ref: "e5", targetId: " A " }), "A");
  assert.equal(targetIdOf({ action: "snapshot", request: { targetId: "B" } }), null);
  assert.equal(targetIdOf({ action: "snapshot", targetId: "t1" }), "t1");
  assert.equal(targetIdOf({ action: "act", request: { kind: "click", ref: "e5", targetId: 7 } }), "7");
  // two different tabs in one call are refused
  assert.deepEqual(cls({ action: "act", targetId: "A", request: { kind: "click", ref: "e1", targetId: "B" } }), ["forbidden"]);
  assert.deepEqual(cls({ action: "act", targetId: "A", request: { kind: "click", ref: "e1", targetId: " A" } }), ["nav_click"]);
  // snake case spellings OpenClaw also reads are refused
  assert.deepEqual(cls({ action: "navigate", url: "https://kestrel.example/", target_url: "https://docs.google.com/document/d/x" }), ["forbidden"]);
  assert.deepEqual(cls({ action: "snapshot", target_id: "B" }), ["forbidden"]);
  assert.deepEqual(cls({ action: "upload", paths: ["/tmp/a.pdf"], input_ref: "e4" }), ["forbidden"]);
});

test("type slowly clicks its element first and types a newline as Enter", () => {
  assert.deepEqual(cls({ action: "act", kind: "type", ref: "e6", text: "x", slowly: true }, "application"), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "type", ref: "e99", text: "x", slowly: true }, "application"), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "type", selector: "#submit", text: "x", slowly: true }, "application"), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "type", ref: "e1", text: "x", slowly: true }, "application"), ["fill"]);
  assert.deepEqual(cls({ action: "act", kind: "type", ref: "e4", text: "a@b.example\n", slowly: true }, "application"), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "type", ref: "e4", text: "a\rb", slowly: true }, "application"), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "type", ref: "e4", text: "a@b.example\n" }, "application"), ["fill"]); // fill sets the value
  assert.deepEqual(cls({ action: "act", kind: "batch", actions: [{ kind: "type", ref: "e6", text: "x", slowly: true }] }, "application"), ["commit"]);
  // OpenClaw reads "true", "yes" and 1 as true
  assert.deepEqual(cls({ action: "act", kind: "type", ref: "e4", text: "x", submit: "true" }), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "type", ref: "e4", text: "x", submit: 1 }), ["commit"]);
  assert.deepEqual(cls({ action: "act", kind: "type", ref: "e6", text: "x", slowly: "yes" }, "application"), ["commit"]);
  // a line break is a line break only in a textbox the token kind lists in multiline_names
  const body: Record<string, RefInfo> = { m1: { role: "textbox", name: "Message Body" }, m2: { role: "textbox", name: "Write a message..." }, m3: { role: "textbox", name: "Cover Letter" } };
  const c2 = (p: Record<string, unknown>, kind: string) =>
    classifyBrowserCall(p, { hosts, driverHashes: DRIVER_HASHES, tokenKind: kind, lookupRef: (r) => body[r] }).map((i) => i.cls);
  assert.deepEqual(c2({ action: "act", kind: "type", ref: "m1", text: "Hello,\n\nThanks", slowly: true }, "cold_email"), ["fill"]);
  assert.deepEqual(c2({ action: "act", kind: "type", ref: "m1", text: "Hello,\n\nThanks", slowly: true }, "li_message"), ["commit"]);
  assert.deepEqual(c2({ action: "act", kind: "type", ref: "m2", text: "Hello,\nThanks", slowly: true }, "li_message"), ["commit"]);
  assert.deepEqual(c2({ action: "act", kind: "type", ref: "m3", text: "Dear team,\nThanks", slowly: true }, "application"), ["fill"]);
  assert.deepEqual(c2({ action: "act", kind: "type", ref: "m3", text: "Dear team,\nThanks", slowly: true }, "li_invite"), ["commit"]);
});

test("upload with a ref is judged as a click on that ref", () => {
  const up = (ref: string, kind: string | null) =>
    classifyBrowserCall({ action: "upload", paths: ["/tmp/a.pdf"], ref }, { hosts, driverHashes: DRIVER_HASHES, tokenKind: kind, lookupRef: (r) => refs[r] });
  const sub = up("e6", "application");
  assert.equal(sub.length, 1);
  assert.equal(sub[0].cls, "commit");
  assert.equal(sub[0].action, "upload");
  assert.deepEqual(sub[0].uploadPaths, ["/tmp/a.pdf"]);
  assert.equal(sub[0].name, "Submit application");
  assert.equal(up("e99", "application")[0].cls, "commit");
  assert.equal(up("e13", "application")[0].cls, "fill");
  assert.equal(up("e9", "application")[0].cls, "fill");
  assert.equal(up("e1", null)[0].cls, "fill"); // a plain link click still writes a file: fill
  assert.deepEqual(cls({ action: "upload", paths: ["/tmp/a.pdf"], inputRef: "e4" }), ["fill"]);
  assert.deepEqual(cls({ action: "upload", paths: ["/tmp/a.pdf"] }), ["fill"]);
  const gmail: Record<string, RefInfo> = { a1: { role: "button", name: "Attach files" } };
  const g = classifyBrowserCall({ action: "upload", paths: ["/tmp/a.pdf"], ref: "a1" }, { hosts, driverHashes: DRIVER_HASHES, tokenKind: "application_email", lookupRef: (r) => gmail[r] });
  assert.equal(g[0].cls, "fill");
});

test("keys that submit or activate: Enter in every spelling and Space", () => {
  for (const key of ["Enter", "enter", "Return", "NumpadEnter", "Shift+Enter", "Enter+a", "Space", " ", "Control+Space", "\n", "\r", "Shift + Tab"]) {
    assert.deepEqual(cls({ action: "act", kind: "press", key }), ["commit"], JSON.stringify(key));
  }
  for (const key of ["Tab", "a", "Shift+Tab", "Backspace"]) assert.deepEqual(cls({ action: "act", kind: "press", key }), ["fill"], key);
  assert.deepEqual(cls({ action: "act", kind: "press", key: "PageDown" }), ["read"]);
});

test("dialog accept follows OpenClaw's truthiness", () => {
  assert.deepEqual(cls({ action: "dialog", accept: "false" }), ["commit"]);
  assert.deepEqual(cls({ action: "dialog", accept: 1 }), ["commit"]);
  assert.deepEqual(cls({ action: "dialog", accept: "yes" }), ["commit"]);
  assert.deepEqual(cls({ action: "dialog", accept: false }), ["nav_click"]);
  assert.deepEqual(cls({ action: "dialog" }), ["nav_click"]);
});

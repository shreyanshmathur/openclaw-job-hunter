import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { classifyUrl } from "../src/browser.ts";
import { DRIVER_FLAG_TEXT, driverScanText, loadSignatures, matchStop, parseDetectJson, resultMeta, resultText, SEVERITY, stopRank, type SignatureSet } from "../src/signatures.ts";
import { REPO } from "./_helpers.ts";
import { FIXTURES, loadHosts } from "./_helpers.ts";

const set = loadSignatures(path.join(FIXTURES, "detect"));
const hosts = loadHosts();

function scan(url: string | null, text: string, httpStatus: number | null = null, title: string | null = null, s: SignatureSet = set) {
  const info = classifyUrl(url, hosts);
  return matchStop(s, { url, title, text, httpStatus, scope: info.platform ? info.platform.scope : null, key: info.platform ? info.platform.key : null, host: info.host });
}

test("loads the accepted shapes and skips non-stop entries", () => {
  const codes = set.signatures.map((s) => s.code).sort();
  assert.deepEqual(codes, ["ats_captcha", "ats_rate_limited", "boards_stop", "li_checkpoint", "li_http_999", "li_invite_limit", "li_login_title", "li_security_check"]);
  assert.equal(set.warnings.length, 0);
});

test("linkedin signatures", () => {
  assert.equal(scan("https://www.linkedin.com/checkpoint/challenge/AgF", "")?.code, "li_checkpoint");
  assert.equal(scan("https://www.linkedin.com/authwall?trk=x", "")?.code, "li_checkpoint");
  assert.equal(scan("https://www.linkedin.com/feed/", "Let's do a quick security check")?.code, "li_security_check");
  assert.equal(scan("https://www.linkedin.com/feed/", "Please VERIFY YOUR IDENTITY")?.code, "li_security_check");
  assert.equal(scan("https://www.linkedin.com/mynetwork/", "You have reached the weekly invitation limit")?.code, "li_invite_limit");
  assert.equal(scan("https://www.linkedin.com/in/example-person", "", 999)?.code, "li_http_999");
  assert.equal(scan("https://www.linkedin.com/in/example-person", "Something went wrong"), null); // not a stop verdict
  assert.equal(scan("https://www.linkedin.com/in/example-person", "Alex Rivera, analyst at Kestrel Commerce"), null);
});

test("signatures stay within their platform", () => {
  // LinkedIn texts do not trip on a board, board texts do not trip on LinkedIn or unknown hosts
  assert.equal(scan("https://www.naukri.com/x", "unusual activity"), null);
  assert.equal(scan("https://www.naukri.com/x", "Please verify you are human")?.code, "boards_stop");
  assert.equal(scan("https://www.naukri.com/captcha?x=1", "")?.code, "boards_stop");
  assert.equal(scan("https://www.linkedin.com/feed/", "verify you are human"), null);
  assert.equal(scan("https://kestrel.example/careers", "verify you are human"), null);
  assert.equal(scan("https://jobs.lever.co/kestrel/1", "Too Many Requests")?.code, "ats_rate_limited");
});

test("python inline flags and bad regexes", () => {
  const warnings: string[] = [];
  const p = parseDetectJson({ platform: "x", signatures: [{ code: "a", text_regex: "(?i)Hello" }, { code: "b", text_regex: "(unclosed" }] }, "x.json", warnings);
  assert.equal(p.signatures.length, 1);
  assert.equal(p.signatures[0].textRes[0].test("hello"), true);
  assert.equal(warnings.length, 1);
});

test("a folder without browser signatures is an error", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "jhg-det-"));
  try {
    fs.writeFileSync(path.join(dir, "smtp.json"), JSON.stringify({ platform: "smtp", signatures: [] }));
    assert.throws(() => loadSignatures(dir));
    fs.writeFileSync(path.join(dir, "bad.json"), "{");
    assert.throws(() => loadSignatures(dir));
    assert.throws(() => loadSignatures(path.join(dir, "missing")));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("result text and meta extraction", () => {
  const result = {
    content: [{ type: "text", text: "- heading \"Security Verification\" [level=1]" }, { type: "image", data: "..." }],
    details: { ok: true, url: "https://www.linkedin.com/checkpoint/x", targetId: "t1" },
  };
  assert.match(resultText(result, undefined), /Security Verification/);
  assert.match(resultText(undefined, "HTTP 999"), /999/);
  assert.deepEqual(resultMeta(result), { url: "https://www.linkedin.com/checkpoint/x", targetId: "t1", httpStatus: null, title: null });
  assert.equal(resultMeta({ details: { pageState: { url: "https://a.example/" } } }).url, "https://a.example/");
});

test("title signatures and job-level stops", () => {
  assert.equal(scan("https://www.linkedin.com/feed/", "", null, "LinkedIn Login, Sign in")?.code, "li_login_title");
  assert.equal(scan("https://www.linkedin.com/feed/", "LinkedIn Login", null, null), null); // title regexes need the title
  const cap = scan("https://job-boards.greenhouse.io/kestrel/jobs/1", "Please tick I'm not a robot");
  assert.equal(cap?.code, "ats_captcha");
  assert.equal(cap?.trip, false);
  assert.equal(cap?.platform, "ats");
  assert.equal(scan("https://www.linkedin.com/checkpoint/x", "")?.trip, true);
  const t = scan("https://www.linkedin.com/feed/", "x".repeat(50) + "unusual activity");
  assert.equal(t?.where, "text");
  assert.equal(t?.index, 50);
});

test("the repo's own detect files load and match (compatibility with scripts/jobhunter/detect)", { skip: !fs.existsSync(path.join(REPO, "scripts", "jobhunter", "detect", "linkedin.json")) }, () => {
  const real = loadSignatures(path.join(REPO, "scripts", "jobhunter", "detect"));
  assert.ok(real.signatures.length > 5);
  assert.deepEqual(real.warnings, []);
  assert.equal(scan("https://www.linkedin.com/checkpoint/challenge/x", "", null, null, real)?.trip, true);
  assert.ok(scan("https://www.naukri.com/x", "Please complete the captcha", null, null, real));
  assert.equal(scan("https://kestrel.example/careers", "captcha", null, null, real), null);
});

// drivers/read_form.js output (fictional values): the flags sit next to the observed form values.
function readForm(flags: Record<string, boolean>): string {
  return JSON.stringify({
    observed: { fields: [{ label: "Email", value: "alex.rivera@example.com" }, { label: "Notice period (days)", value: "30" }], resume_filename_visible: "Alex_Rivera_Resume.pdf" },
    required_empty: [],
    ...flags,
    validation_errors: [],
    page_url: "https://job-boards.greenhouse.io/kestrel/jobs/1",
  });
}

test("driver output: key names are not page text, flag values are acted on", () => {
  const clear = driverScanText(readForm({ captcha_visible: false, account_wall: false }));
  assert.equal(clear.includes("captcha"), false);
  assert.equal(clear.includes("account_wall"), false);
  assert.match(clear, /alex\.rivera@example\.com/);
  assert.match(clear, /Alex_Rivera_Resume\.pdf/);
  assert.equal(scan("https://job-boards.greenhouse.io/kestrel/jobs/1", clear), null);
  // the raw JSON is what used to be scanned: its key name matches the CAPTCHA signature
  assert.equal(scan("https://job-boards.greenhouse.io/kestrel/jobs/1", readForm({ captcha_visible: false }))?.code, "ats_captcha");

  const cap = driverScanText(readForm({ captcha_visible: true, account_wall: false }));
  assert.equal(cap.split("\n")[0], DRIVER_FLAG_TEXT.captcha_visible);
  assert.equal(cap.includes(DRIVER_FLAG_TEXT.account_wall), false);
  const m = scan("https://job-boards.greenhouse.io/kestrel/jobs/1", cap);
  assert.equal(m?.code, "ats_captcha");
  assert.equal(m?.trip, false);
  assert.equal(m?.index, 0);

  const wall = driverScanText(readForm({ captcha_visible: false, account_wall: true }));
  assert.equal(wall.split("\n")[0], DRIVER_FLAG_TEXT.account_wall);
  assert.equal(wall.includes("captcha"), false);
});

test("driver output: page text in values is still scanned, nested and cut-short JSON", () => {
  // detect_page output carries the page text as a value
  const page = JSON.stringify({ detect_file: { platform: "linkedin", url: "https://www.linkedin.com/feed/", title: "Feed", http_status: null, text: "Let's do a quick security check" }, hint: "stop", matched: [], family: "linkedin" });
  assert.equal(scan("https://www.linkedin.com/feed/", driverScanText(page))?.code, "li_security_check");
  // a value that is itself JSON (a wrapped result) is read the same way
  const wrapped = JSON.stringify({ ok: true, result: readForm({ captcha_visible: false }) });
  assert.equal(driverScanText(wrapped).includes("captcha"), false);
  assert.equal(driverScanText(JSON.stringify({ ok: true, result: JSON.parse(readForm({ captcha_visible: true })) })).split("\n")[0], DRIVER_FLAG_TEXT.captcha_visible);
  // not valid JSON (cut short): key names are removed, true flags still count
  const cut = readForm({ captcha_visible: false, account_wall: false }).slice(0, -30);
  assert.throws(() => JSON.parse(cut));
  assert.equal(driverScanText(cut).includes("captcha"), false);
  const cutTrue = readForm({ captcha_visible: true }).slice(0, -30);
  assert.equal(driverScanText(cutTrue).split("\n")[0], DRIVER_FLAG_TEXT.captcha_visible);
  // an error message is kept as it is
  assert.equal(driverScanText("Error: evaluate failed: I'm not a robot"), "Error: evaluate failed: I'm not a robot");
  // other boolean keys are not flags; a flag key under a string value is not a flag either
  assert.equal(driverScanText(JSON.stringify({ dialog_open: true, note_text: "captcha_visible" })), "captcha_visible");
  assert.equal(driverScanText(JSON.stringify({ captcha_visible: "true" })), "true");
});

test("driver flags against the repo's own detect files", { skip: !fs.existsSync(path.join(REPO, "scripts", "jobhunter", "detect", "boards.json")) }, () => {
  const real = loadSignatures(path.join(REPO, "scripts", "jobhunter", "detect"));
  const gh = "https://job-boards.greenhouse.io/kestrel/jobs/1";
  const nk = "https://www.naukri.com/job-listings-data-analyst-kestrel-commerce-1";
  for (const url of [gh, nk]) {
    assert.equal(scan(url, driverScanText(readForm({ captcha_visible: false, account_wall: false })), null, null, real), null, url);
  }
  assert.deepEqual(
    [scan(gh, driverScanText(readForm({ captcha_visible: true })), null, null, real)?.code, scan(gh, driverScanText(readForm({ captcha_visible: true })), null, null, real)?.trip],
    ["ats_captcha", false],
  );
  assert.equal(scan(gh, driverScanText(readForm({ account_wall: true })), null, null, real)?.code, "ats_account_wall");
  const board = scan(nk, driverScanText(readForm({ captcha_visible: true })), null, null, real);
  assert.equal(board?.code, "site_captcha");
  assert.equal(board?.trip, true);
});

// ------------------------------------------------------------------ most severe match (detect.match parity)

function setOf(files: Record<string, unknown>): SignatureSet {
  const warnings: string[] = [];
  const signatures = [];
  const hostsByPlatform = new Map<string, string[]>();
  for (const [name, raw] of Object.entries(files)) {
    const p = parseDetectJson(raw, name, warnings);
    signatures.push(...p.signatures);
    if (p.hosts.length) hostsByPlatform.set(p.platform, p.hosts);
  }
  assert.deepEqual(warnings, []);
  return { signatures, hostsByPlatform, warnings };
}

test("the most severe matching signature wins, not the first in file order", () => {
  const s = setOf({
    "linkedin.json": {
      platform: "linkedin",
      hosts: ["linkedin.com"],
      signatures: [
        { id: "soft_first", verdict: "stop", trip: false, text: "restricted" },
        { id: "unknown_reason", verdict: "stop", trip: true, reason_code: "li_something_new", text: "restricted" },
        { id: "challenge", verdict: "stop", trip: true, reason_code: "li_challenge", url: "/checkpoint/" },
        { id: "logged_out", verdict: "stop", trip: true, reason_code: "li_logged_out", text: "restricted" },
        { id: "restricted", verdict: "stop", trip: true, reason_code: "li_restricted", text: "temporarily restricted" },
        { id: "restricted_again", verdict: "stop", trip: true, reason_code: "li_restricted", text: "temporarily restricted" },
      ],
    },
  });
  const page = "https://www.linkedin.com/checkpoint/x";
  // all six match; li_restricted is first in SEVERITY, and the earlier of the two li_restricted rows wins
  const m = scan(page, "Your account is temporarily restricted", null, null, s);
  assert.equal(m?.code, "restricted");
  assert.equal(m?.reasonCode, "li_restricted");
  assert.equal(m?.trip, true);
  assert.equal(m?.where, "text");
  assert.equal(m?.index, "Your account is ".length); // the index of the winning match, for the detect text window
  // without the restriction text: li_challenge (url) before li_logged_out, known reasons before unknown ones
  assert.equal(scan(page, "restricted", null, null, s)?.code, "challenge");
  assert.equal(scan("https://www.linkedin.com/feed/", "restricted", null, null, s)?.code, "logged_out");
  // a tripping stop with an unknown reason still beats a job-level stop listed before it
  const s2 = setOf({ "linkedin.json": { platform: "linkedin", hosts: ["linkedin.com"], signatures: [
    { id: "soft_first", verdict: "stop", trip: false, reason_code: "li_restricted", text: "restricted" },
    { id: "unknown_reason", verdict: "stop", trip: true, reason_code: "li_something_new", text: "restricted" },
  ] } });
  assert.equal(scan("https://www.linkedin.com/feed/", "restricted", null, null, s2)?.code, "unknown_reason");
  // equal rank: load order decides
  const s3 = setOf({ "linkedin.json": { platform: "linkedin", hosts: ["linkedin.com"], signatures: [
    { id: "a", verdict: "stop", text: "restricted" },
    { id: "b", verdict: "stop", text: "restricted" },
  ] } });
  assert.equal(scan("https://www.linkedin.com/feed/", "restricted", null, null, s3)?.code, "a");
});

test("stopRank orders trip, then SEVERITY, then load order", () => {
  const sig = (trip: boolean, reasonCode: string | null) => ({ file: "x.json", platform: "linkedin", code: "c", urlRes: [], titleRes: [], textRes: [], httpStatus: [], trip, reasonCode });
  assert.deepEqual(stopRank(sig(true, "li_restricted"), 7), [0, 0, 7]);
  assert.deepEqual(stopRank(sig(false, "li_restricted"), 7), [1, 0, 7]);
  assert.deepEqual(stopRank(sig(true, null), 2), [0, SEVERITY.length, 2]);
  assert.deepEqual(stopRank(sig(true, "not_listed"), 2), [0, SEVERITY.length, 2]);
  assert.deepEqual(stopRank(sig(true, "site_challenge"), 0), [0, SEVERITY.indexOf("site_challenge"), 0]);
});

test("most severe match against the repo's own detect files", { skip: !fs.existsSync(path.join(REPO, "scripts", "jobhunter", "detect", "linkedin.json")) }, () => {
  const real = loadSignatures(path.join(REPO, "scripts", "jobhunter", "detect"));
  // LinkedIn serves the restriction page under /checkpoint/: li_restricted, not a plain challenge
  const li = scan("https://www.linkedin.com/checkpoint/challenge/x", "Your account has been temporarily restricted", null, null, real);
  assert.equal(li?.code, "li_restricted_text");
  assert.equal(li?.reasonCode, "li_restricted");
  assert.equal(scan("https://www.linkedin.com/checkpoint/challenge/x", "", null, null, real)?.reasonCode, "li_challenge");
  // an ATS page with a CAPTCHA that also answered 429: the tripping block, not the job-level CAPTCHA
  const ats = scan("https://job-boards.greenhouse.io/kestrel/jobs/1", "Please complete the captcha", 429, null, real);
  assert.equal(ats?.code, "ats_http_block");
  assert.equal(ats?.trip, true);
  assert.equal(scan("https://job-boards.greenhouse.io/kestrel/jobs/1", "Please complete the captcha", null, null, real)?.trip, false);
  // Gmail: a security prompt outranks the sending-limit notice listed before it
  assert.equal(scan("https://mail.google.com/mail/u/0/#inbox", "You reached a sending limit. We noticed unusual activity", null, null, real)?.code, "gm_security_text");
});

test("SEVERITY is the same list as detect.SEVERITY in the Python core", { skip: !fs.existsSync(path.join(REPO, "scripts", "jobhunter", "detect", "__init__.py")) }, () => {
  const src = fs.readFileSync(path.join(REPO, "scripts", "jobhunter", "detect", "__init__.py"), "utf8");
  const m = /^SEVERITY = \(([^)]*)\)/m.exec(src);
  assert.ok(m, "detect/__init__.py defines SEVERITY = (...)");
  const py = [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
  assert.deepEqual([...SEVERITY], py);
});

// Node cross-check of the exec approvals argPattern (U7, CLI route design 11.1).
//
// OpenClaw matches argPattern with an ECMAScript RegExp; tests/test_install_render.py renders the pattern with
// Python and compiles it with Python's re. This script reads {"pattern": "...", "cases": ["...", ...]} as JSON on
// stdin and prints a JSON list of booleans (does each case match), so the test can compare both engines.
import { readFileSync } from "node:fs";

const input = JSON.parse(readFileSync(0, "utf8"));
const rx = new RegExp(input.pattern);
process.stdout.write(JSON.stringify(input.cases.map((c) => rx.test(c))) + "\n");

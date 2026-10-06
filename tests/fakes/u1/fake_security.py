"""An in-memory stand-in for macOS /usr/bin/security (U1 tests and INT): secretstore.set_runner(FakeSecurity()).

It understands the three calls jobhunter.secretstore makes: `security -i` with an add-generic-password command on
stdin, `find-generic-password -s <svc> -a <acct> [-w]` and `delete-generic-password -s <svc> -a <acct>`. Every
argv and every stdin text is recorded separately, so a test can prove that a value only ever went to stdin.
locked=True plays a locked keychain (exit 51).
"""
from __future__ import annotations

import re
import shlex


class FakeSecurity:
    def __init__(self):
        self.items: dict = {}
        self.argv_log: list = []
        self.stdin_log: list = []
        self.locked = False

    def __call__(self, argv, input_text=None):
        self.argv_log.append(list(argv))
        if input_text is not None:
            self.stdin_log.append(input_text)
        if self.locked:
            return 51, "", "security: User interaction is not allowed."
        args = list(argv[1:])
        if args == ["-i"]:
            for line in (input_text or "").splitlines():
                toks = shlex.split(line)
                if not toks or toks[0] != "add-generic-password":
                    return 1, "", "unknown command"
                opts = self._opts(toks[1:])
                self.items[(opts.get("-s"), opts.get("-a"))] = opts.get("-w", "")
            return 0, "", ""
        if not args:
            return 1, "", ""
        cmd, opts = args[0], self._opts(args[1:])
        key = (opts.get("-s"), opts.get("-a"))
        if cmd == "find-generic-password":
            if key not in self.items:
                return 44, "", "The specified item could not be found in the keychain."
            return 0, (self.items[key] + "\n") if "-w" in args else 'keychain: "login.keychain-db"\n', ""
        if cmd == "delete-generic-password":
            if self.items.pop(key, None) is None:
                return 44, "", "not found"
            return 0, "", ""
        return 1, "", "unknown"

    @staticmethod
    def _opts(toks) -> dict:
        out = {}
        i = 0
        while i < len(toks):
            t = toks[i]
            if re.match(r"^-[a-zA-Z]$", t):
                if t in ("-U",):
                    out[t] = True
                    i += 1
                    continue
                if i + 1 < len(toks) and not re.match(r"^-[a-zA-Z]$", toks[i + 1]):
                    out[t] = toks[i + 1]
                    i += 2
                    continue
                out[t] = True
            i += 1
        return out

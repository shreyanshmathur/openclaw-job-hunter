#!/bin/bash
# Install the pre-commit hook for this clone (design 10.7): textcheck, leakcheck, check_gitignore and the
# page driver manifest check run on every commit. textcheck and leakcheck read the staged snapshot (--staged),
# which is what the commit records: a file that was staged and then edited or deleted is still checked as
# staged. After fixing a finding, stage the fix (git add) before committing again.
# Safe to re-run (an older copy of this hook is replaced). Remove with: rm .git/hooks/pre-commit
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_DIR="$(git -C "$REPO" rev-parse --git-path hooks)"
case "$HOOK_DIR" in /*) ;; *) HOOK_DIR="$REPO/$HOOK_DIR" ;; esac
mkdir -p "$HOOK_DIR"
HOOK="$HOOK_DIR/pre-commit"
if [ -f "$HOOK" ] && ! grep -q 'openclaw-job-hunter pre-commit' "$HOOK"; then
  echo "A different pre-commit hook exists at $HOOK; not replacing it." >&2
  echo "Add these lines to it by hand:" >&2
  echo '  python3 tools/textcheck.py --staged && python3 tools/leakcheck.py --staged && python3 tools/check_gitignore.py --include-untracked && python3 tools/gen_drivers.py --check' >&2
  exit 1
fi
cat > "$HOOK" <<'EOF'
#!/bin/bash
# openclaw-job-hunter pre-commit (installed by tools/install_hooks.sh)
set -e
cd "$(git rev-parse --show-toplevel)"
PY="$(command -v python3 || echo /usr/bin/python3)"
"$PY" tools/check_gitignore.py --include-untracked
# the staged snapshot, not the working tree: that is what this commit records
"$PY" tools/textcheck.py --staged || { echo "pre-commit: fix the file, then git add it again" >&2; exit 1; }
"$PY" tools/leakcheck.py --staged || { echo "pre-commit: fix the file, then git add it again" >&2; exit 1; }
"$PY" tools/gen_drivers.py --check
EOF
chmod 755 "$HOOK"
echo "DONE pre-commit hook installed at $HOOK"

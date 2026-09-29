#!/bin/bash
# secret-scan.sh -- refuse to publish if anything secret or deployment-specific is in this repo.
#
#   scripts/secret-scan.sh              scan the working tree AND every commit in git history
#   scripts/secret-scan.sh --self-test  prove the scanner catches a planted key (and passes a clean file)
#
# Two classes of finding, both fatal:
#   1. credentials: API keys and tokens of the common providers, private keys, passwords in assignments
#   2. identifiers of a real deployment: private hostnames and domains, LAN addresses, emails, home paths
#      (extend with $SECRET_SCAN_EXTRA, a |-separated list of extra regexes kept OUTSIDE this repo)
# gitleaks is used as well when it is installed; the built-in patterns run either way.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

CRED='(sk-ant-[A-Za-z0-9_-]{20,}|sk-(proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|xox[abprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.|(password|passwd|secret|api_?key|token)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"' ]{8,}["'"'"'])'
IDENT='((^|[^0-9])(10|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}|[A-Za-z0-9._%+-]+@(gmail|outlook|hotmail|yahoo|qq|163)\.com|/Users/[a-z][a-z0-9_-]+/|/home/[a-z][a-z0-9_-]+/)'
EXTRA="${SECRET_SCAN_EXTRA:-}"
# lines that are allowed to look like a finding: fixtures that exist to test the scanner/guard
ALLOW='(secret-scan: allow|EXAMPLE|example\.com|/home/user/|/Users/me/|/home/alice/)'

scan_text() {  # stdin -> matching lines on stdout
  grep -nEi -- "$CRED" | grep -vEi -- "$ALLOW"
  return 0
}
scan_ident() {
  grep -nE -- "$IDENT" | grep -vEi -- "$ALLOW"
  [ -n "$EXTRA" ] && grep -nEi -- "$EXTRA"
  return 0
}

if [ "${1:-}" = "--self-test" ]; then
  planted="aws_key = 'AKIA""ABCDEFGHIJKLMNOP'"
  clean="the key is read from the environment variable ANTHROPIC_API_KEY"
  a=$(printf '%s\n' "$planted" | scan_text); b=$(printf '%s\n' "$clean" | scan_text)
  c=$(printf 'ssh to 192.168.1.20\n' | scan_ident)
  [ -n "$a" ] && [ -z "$b" ] && [ -n "$c" ] && { echo "SELF-TEST: PASS (planted key and LAN address caught, clean line passed)"; exit 0; }
  echo "SELF-TEST: FAIL"; exit 1
fi

fail=0
files=$(git ls-files 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
[ -n "$files" ] || files=$(find . -type f -not -path './.git/*' -not -path '*/__pycache__/*')
while IFS= read -r f; do
  [ -f "$f" ] || continue
  case "$f" in scripts/secret-scan.sh|*/__pycache__/*|*.pyc|.git/*) continue ;; esac
  out=$( { scan_text < "$f"; scan_ident < "$f"; } | sed "s|^|$f:|")
  [ -n "$out" ] && { printf '%s\n' "$out"; fail=1; }
done <<< "$files"

# history: every added line in every commit
if git rev-parse --git-dir >/dev/null 2>&1 && git rev-parse HEAD >/dev/null 2>&1; then
  hist=$(git log -p --all --no-color | grep -E '^\+' | grep -v '^+++' | { scan_text; scan_ident; })
  [ -n "$hist" ] && { echo "IN GIT HISTORY:"; printf '%s\n' "$hist" | head -20; fail=1; }
fi

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --no-banner --redact -q . || fail=1
fi

[ $fail = 0 ] && echo "SECRET-SCAN: PASS" || echo "SECRET-SCAN: FAIL (see lines above)"
exit $fail

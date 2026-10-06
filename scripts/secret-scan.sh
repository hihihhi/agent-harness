#!/bin/bash
# secret-scan.sh -- refuse to publish if anything secret or deployment-specific is in this repo.
#
#   scripts/secret-scan.sh              scan the working tree AND every commit in git history
#   scripts/secret-scan.sh --self-test  prove the scanner catches a planted key (and passes a clean file)
#   scripts/secret-scan.sh --tree-only  the working tree only (no history, no gitleaks)
#
# Two classes of finding, both fatal:
#   1. credentials: API keys and tokens of the common providers, private keys, passwords in assignments
#   2. identifiers of a real deployment: private hostnames and domains, LAN addresses, emails, home paths
#      (extend with $SECRET_SCAN_EXTRA, a |-separated list of extra regexes kept OUTSIDE this repo)
# gitleaks is used as well when it is installed; the built-in patterns run either way.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

CRED='(sk-ant-[A-Za-z0-9_-]{20,}|sk-(proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|xox[abprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.|(password|passwd|secret|api_?key|token)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"' ]{8,}["'"'"'])'
IDENT='((^|[^0-9])(10|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}|[A-Za-z0-9._%+-]+@(gmail|outlook|hotmail|yahoo|qq|163)\.com|/[Uu]sers/[A-Za-z][A-Za-z0-9_.-]+/|/home/[A-Za-z][A-Za-z0-9_.-]+/)'
EXTRA="${SECRET_SCAN_EXTRA:-}"
# lines that are allowed to look like a finding: fixtures that exist to test the scanner/guard. Not the bare
# word "example": that dropped any key on a line saying "# Example only" (review, 2026-10-06).
# /home/Test/ is the old check_guard.py docstring, in history since v0.2; /Users/alice/ a placeholder like alice.
ALLOW='(secret-scan: allow|example\.com|/home/user/|/Users/me/|/Users/Shared/|/home/alice/|/Users/alice/|/home/Test/)'
# For identifiers, an allowed substring excuses ITSELF, not its line: it is cut out before matching, so
# a real home path next to an allowed one is still caught (review of v0.4.0). A marked line is excused whole.
ALLOW_ID='(example\.com|/home/user/|/Users/me/|/Users/Shared/|/home/alice/|/Users/alice/|/home/Test/)'

scan_text() {  # stdin -> matching lines on stdout
  # A credential is excused only by an explicit marker on its line: ALLOW (example.com, placeholder home paths)
  # is for identifiers, and applied here it let a real key on any line naming example.com through.
  grep -nEi -- "$CRED" | grep -v -- 'secret-scan: allow'
  return 0
}
scan_ident() {
  # Read stdin ONCE. This used to grep stdin twice; the first grep consumed all of it, so the EXTRA
  # patterns ran over nothing and could never match (found in v0.3 by planting a word and seeing PASS).
  local in; in=$(cat)
  # LC_ALL=C: BSD sed in a UTF-8 locale stops at the first invalid byte, and the rest of the file went unscanned.
  # The allowed text becomes a separator, not nothing: deleting it joined its neighbours into a non-match.
  # Found lines are printed as written, not as edited.
  local nums
  nums=$(printf '%s\n' "$in" | LC_ALL=C sed -E -e '/secret-scan: allow/s/.*//' -e "s#$ALLOW_ID#/_/#g" \
         | LC_ALL=C grep -nE -- "$IDENT" | cut -d: -f1 | tr '\n' ' ')
  # One pass over the original text for every found line (one sed per line was quadratic: 40 s on a file
  # with 3,000 findings).
  [ -n "$nums" ] && printf '%s\n' "$in" | LC_ALL=C awk -v nums="$nums" \
    'BEGIN { n = split(nums, a, " "); for (i = 1; i <= n; i++) want[a[i]] = 1 } (FNR in want) { print FNR ":" $0 }'
  [ -n "$EXTRA" ] && printf '%s\n' "$in" | grep -nEi -- "$EXTRA"
  return 0
}

scan_both() {  # stdin -> both scanners, each reading its own copy
  local in; in=$(cat)
  printf '%s\n' "$in" | scan_text
  printf '%s\n' "$in" | scan_ident
}

if [ "${1:-}" = "--self-test" ]; then
  planted="aws_key = 'AKIA""ABCDEFGHIJKLMNOP'"
  clean="the key is read from the environment variable ANTHROPIC_API_KEY"
  a=$(printf '%s\n' "$planted" | scan_text); b=$(printf '%s\n' "$clean" | scan_text)
  c=$(printf 'ssh to 192.168.1.20\n' | scan_ident)   # secret-scan: allow
  # The two shapes that were silently dead before v0.3: an EXTRA pattern, and an identifier reached
  # through the same pipe-into-both-scanners the history scan uses.
  d=$(printf 'the canaryhost box\n' | EXTRA='canaryhost' scan_ident)
  e=$(printf 'clean line\nssh to 192.168.1.20\n' | scan_both)   # secret-scan: allow
  [ -n "$a" ] && [ -z "$b" ] && [ -n "$c" ] && [ -n "$d" ] && [ -n "$e" ] && {
    echo "SELF-TEST: PASS (planted key, LAN address, an EXTRA pattern and a piped identifier caught; clean line passed)"
    exit 0; }
  echo "SELF-TEST: FAIL (key=${a:+ok} clean=${b:-ok} lan=${c:+ok} extra=${d:+ok} piped=${e:+ok})"; exit 1
fi

fail=0
files=$(git ls-files 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
[ -n "$files" ] || files=$(find . -type f -not -path './.git/*' -not -path '*/__pycache__/*')
while IFS= read -r f; do
  [ -f "$f" ] || continue
  case "$f" in */__pycache__/*|*.pyc|.git/*) continue ;; esac   # the scanner scans itself too
  out=$( { scan_text < "$f"; scan_ident < "$f"; } | sed "s|^|$f:|")
  [ -n "$out" ] && { printf '%s\n' "$out"; fail=1; }
done <<< "$files"

# history: every added line in every commit
if [ "${1:-}" != "--tree-only" ] && git rev-parse --git-dir >/dev/null 2>&1 && git rev-parse HEAD >/dev/null 2>&1; then
  # scan_both, not `{ scan_text; scan_ident; }`: in that form scan_text consumed the whole pipe and the
  # identifier scan of history read nothing, so history was only ever checked for credentials.
  # The scanner's own file is skipped here as it is in the tree scan above: its self-test plants a LAN
  # address on purpose, and that line has been in history since the scanner was written.
  hist=$(git log -p --all --no-color -- . ':(exclude)scripts/secret-scan.sh' | grep -E '^\+' | grep -v '^+++' | scan_both)
  [ -n "$hist" ] && { echo "IN GIT HISTORY:"; printf '%s\n' "$hist" | head -20; fail=1; }
  # The scanner's own history is still scanned for credentials; only its planted LAN fixture, older than the
  # allow marker, is exempt from the identifier scan.
  # Its identifier scan too, with only the planted LAN fixture (older than the allow marker) cut out.
  own=$(git log -p --all --no-color -- scripts/secret-scan.sh | grep -E '^\+' | grep -v '^+++' \
        | sed 's/192\.168\.1\.20//g' | scan_both)
  [ -n "$own" ] && { echo "IN THE SCANNER'S OWN HISTORY:"; printf '%s\n' "$own" | head -20; fail=1; }
fi

if [ "${1:-}" != "--tree-only" ] && command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --no-banner --redact --log-level warn -s . || fail=1   # gitleaks 8.30 has no -q
fi

[ $fail = 0 ] && echo "SECRET-SCAN: PASS" || echo "SECRET-SCAN: FAIL (see lines above)"
exit $fail

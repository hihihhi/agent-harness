#!/usr/bin/env bash
# `plan doctor` — is this machine fit to run an unattended drain?
# Written BEFORE the implementation, and red at the time of writing.
set -u
# Vendored from agentic-os; PLAN is the engine under test (tests/test_plan_component.py sets it).
PLAN="${PLAN:-$(cd "$(dirname "$0")/../.." && pwd)/src/agent_harness/workgraph/bin/plan}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); [ -n "${QUIET_TESTS:-}" ] || printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "$2"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected $2, got $3"; fi; }
has()  { if printf '%s' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "missing '$3'"; fi; }

T="$(mktemp -d)"
cleanup() { rm -rf "$T"; }
trap cleanup EXIT
mkdir -p "$T/plan"; cd "$T" || exit 1
printf '## goal: d\n- [ ] 1. a | gate: true\n' > plan/d.md

# A healthy machine has `claude` on PATH (plan run dispatches it). CI runners do not, so the healthy
# case gets a stub: what is under test is plan doctor, not whether this runner has Claude installed.
mkdir -p "$T/stub"
printf '#!/bin/sh\necho "2.0.0 (stub)"\n' > "$T/stub/claude"; chmod +x "$T/stub/claude"
out=$(PATH="$T/stub:$PATH" "$PLAN" doctor 2>&1); rc=$?
printf '%s\n' "$out" | sed 's/^/     | /'

has "reports the python it will run under" "$out" "python"
has "reports whether claude is reachable"  "$out" "claude"
has "reports the property-tier dependency" "$out" "hypothesis"
# The needle is the resolved plan dir, not the word "plan": a tool called `plan`
# prints that word no matter what it checks, so the weaker needle could not fail.
has "reports the plan directory"           "$out" "$(pwd -P)/plan"
has "reports the lease setting"            "$out" "lease"
check "a healthy machine exits 0" "0" "$rc"

echo "== a stale lock is a finding, not a surprise at 2am =="
mkdir -p plan/d.md.lock
touch -t 200001010000 plan/d.md.lock
out=$("$PLAN" doctor 2>&1)
has "a stale lock is reported" "$out" "lock"
rmdir plan/d.md.lock

echo "== an unwritable plan directory must fail, not warn =="
chmod 555 plan
out=$("$PLAN" doctor 2>&1); rc=$?
chmod 755 plan
check "unwritable plan/ exits non-zero" "1" "$rc"
has   "and says so"                     "$out" "writable"

echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

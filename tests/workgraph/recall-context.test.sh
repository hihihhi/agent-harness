#!/usr/bin/env bash
# `plan context` is the ONE mechanism that makes compaction survivable: the
# briefing is rebuilt from disk every time, so a cold session can pick up any
# node. It carried `rec[-RECORD_TAIL:]` — the pure TAIL.
#
# A record's HEAD is where the planner writes the decisions that must not be
# reopened. Its TAIL is the most recent gate output. Measured on a real 8.6 KB
# record: the briefing delivered 4 KB of "attempt 32 failed, attempt 33 failed"
# and DROPPED "NEVER use the legacy endpoint; it silently drops writes".
#
# So the context-loss failure the whole graph exists to prevent was living inside
# the mechanism that prevents it, and the bytes it spent were the least
# informative in the file: 59 near-identical lines.
set -u
# Vendored from agentic-os; PLAN is the engine under test (tests/test_plan_component.py sets it).
PLAN="${PLAN:-$(cd "$(dirname "$0")/../.." && pwd)/src/agent_harness/workgraph/bin/plan}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); [ -n "${QUIET_TESTS:-}" ] || printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "$2"; }
has()   { if printf '%s' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "missing '$3'"; fi; }
hasnt() { if printf '%s' "$2" | grep -qF -- "$3"; then bad "$1" "found '$3'"; else ok "$1"; fi; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/plan"; cd "$T" || exit 1
printf '## goal: g\n- [ ] 1. long-running node | gate: false\n' > plan/m.md
"$PLAN" claim m >/dev/null 2>&1
REC=$(ls plan/m/*.md | head -1)
/usr/bin/python3 - "$REC" <<'PY'
import sys
head = ("# Node 1 — long-running node\n\n## FIXED DECISIONS (do not reopen)\n"
        "- NEVER use the legacy endpoint; it silently drops writes.\n"
        "- The client signed off on 30-day retention. Do not change it.\n\n")
noise = "".join(
    f"      2026-09-{d:02d}T10:00:00Z exit 1 in 3s [aaaaaaa1] — attempt {d} failed, "
    f"retrying with a different approach and rather more logging than before\n"
    for d in range(1, 60))
open(sys.argv[1], "w").write(head + noise)
PY
out=$("$PLAN" context m 1 2>&1)

echo "== the constraints at the HEAD must reach a cold session =="
has "the decisions section survives"      "$out" "FIXED DECISIONS"
has "and the constraint itself"           "$out" "NEVER use the legacy endpoint"
has "and the second one"                  "$out" "30-day retention"

echo "== the most recent evidence must ALSO survive =="
has "the latest attempt is present"       "$out" "attempt 59"

echo "== and when truncation IS needed, it must be stated, never silent =="
# Asserted separately, and on DISTINCT content. The repeated-attempt fixture
# above compresses so well that nothing needs eliding -- expecting the word
# "elided" there was the test asserting a MECHANISM instead of the property,
# which is "nothing load-bearing is lost silently".
/usr/bin/python3 - "$REC" <<'PY2'
import sys
head = ("# Node 1\n\n## FIXED DECISIONS (do not reopen)\n"
        "- NEVER use the legacy endpoint; it silently drops writes.\n\n")
# 400 genuinely different lines: no two share a signature, so nothing collapses
body = "".join(
    f"      2026-09-01T10:00:{i%60:02d}Z exit {i%7+1} in {i}s [{i:08x}] — "
    f"distinct failure number {i} touching module_{i} with its own message\n"
    for i in range(400))
open(sys.argv[1], "w").write(head + body)
PY2
out3=$("$PLAN" context m 1 2>&1)
has "incompressible content is elided"  "$out3" "elided"
has "the elision names the byte count"  "$out3" "bytes elided"
has "the HEAD still survives it"        "$out3" "NEVER use the legacy endpoint"
has "and so does the newest line"       "$out3" "distinct failure number 399"
sz3=$(printf '%s' "$out3" | wc -c | tr -d ' ')
if [ "$sz3" -lt 9000 ]; then ok "still bounded at ${sz3} bytes"
else bad "bounded under incompressible input" "${sz3} bytes"; fi


echo "== a short record is passed through whole =="
printf '# Node 1\n\nJust one short note about the approach taken here.\n' > "$REC"
out2=$("$PLAN" context m 1 2>&1)
has   "short records survive intact"  "$out2" "one short note about the approach"
hasnt "and nothing is claimed elided" "$out2" "elided"

echo "== repeated identical attempts are collapsed, not copied 59 times =="
n=$(printf '%s' "$out" | grep -c 'retrying with a different approach')
if [ "$n" -lt 20 ]; then ok "59 identical lines compressed to $n"
else bad "identical lines are collapsed" "$n copies still present"; fi

echo "== the briefing stays bounded =="
sz=$(printf '%s' "$out" | wc -c | tr -d ' ')
if [ "$sz" -lt 9000 ]; then ok "briefing is ${sz} bytes"
else bad "briefing stays bounded" "${sz} bytes"; fi

echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

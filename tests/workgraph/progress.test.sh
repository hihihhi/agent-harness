#!/usr/bin/env bash
# An unattended run that prints only round numbers is indistinguishable from a
# hang, and the honest response to a suspected hang is to kill it and look --
# which is exactly the babysitting the orchestrator exists to remove.
#
# So the progress line is not cosmetic, and these assert the two things that
# make it trustworthy: the percentage is ARITHMETICALLY correct rather than
# decorative, and it MOVES as the graph drains.
set -u
# Vendored from agentic-os; PLAN is the engine under test (tests/test_plan_component.py sets it).
PLAN="${PLAN:-$(cd "$(dirname "$0")/../.." && pwd)/src/agent_harness/workgraph/bin/plan}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); [ -n "${QUIET_TESTS:-}" ] || printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "$2"; }
has()   { if printf '%s' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "no '$3' in: $(printf '%s' "$2" | tr '\n' '/' | cut -c1-200)"; fi; }
hasnt() { if printf '%s' "$2" | grep -qF -- "$3"; then bad "$1" "found '$3' in: $(printf '%s' "$2" | tr '\n' '/' | cut -c1-200)"; else ok "$1"; fi; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want '$2', got '$3'"; fi; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/plan" "$T/stub"; cd "$T" || exit 1
printf '#!/usr/bin/env bash\nprintf "{\\"is_error\\":false,\\"subtype\\":\\"success\\",\\"result\\":\\"ok\\",\\"total_cost_usd\\":0,\\"duration_ms\\":10,\\"permission_denials\\":[]}\\n"\n' > stub/claude
chmod +x stub/claude
run() { PATH="$T/stub:$PATH" PLAN_QUIET=1 "$PLAN" run demo "$@" 2>&1; }

echo "== the percentage is arithmetic, not decoration =="
# 4 of 8 done before the run starts: the first line must say exactly 50%.
{ echo "## goal: g"
  for i in 1 2 3 4; do echo "- [x] $i. done$i | gate: true"; done
  for i in 5 6 7 8; do echo "- [ ] $i. todo$i | gate: true"; done
} > plan/demo.md
out=$(run --workers 1 --max-rounds 1)
first=$(printf '%s' "$out" | grep -m1 '%')
has "opens at the true 50%"      "$first" " 50%"
has "and states the raw counts"  "$first" "4/8 done"
has "with a half-filled bar"     "$first" "██████░░░░░░"
has "and names the round"        "$first" "round 1"

echo "== it moves as the graph drains =="
{ echo "## goal: g"; for i in 1 2 3 4; do echo "- [ ] $i. n$i | gate: true"; done; } > plan/demo.md
out=$(run --workers 1 --max-rounds 6)
pcts=$(printf '%s' "$out" | grep -o '[0-9]\+%' | tr -d '%' | tr '\n' ' ' | sed 's/ *$//')
check "0 -> 25 -> 50 -> 75 -> 100 across rounds" "0 25 50 75 100 100" "$pcts"
has   "and finishes"                             "$out" "ALL DONE"
uniq=$(printf '%s' "$pcts" | tr ' ' '\n' | sort -u | wc -l | tr -d ' ')
if [ "$uniq" -ge 4 ]; then ok "the number is not static ($uniq distinct values)"
else bad "the number is not static" "only $uniq distinct values: $pcts"; fi

echo "== progress is printed BEFORE the round it describes =="
# If it trailed the dispatch line it would only ever be visible after the wait.
{ echo "## goal: g"; echo "- [ ] 1. a | gate: true"; } > plan/demo.md
out=$(run --workers 1 --max-rounds 2)
order=$(printf '%s' "$out" | grep -n 'round 1' | cut -d: -f1 | tr '\n' ' ')
p=$(printf '%s' "$out" | grep -n '%.*round 1' | head -1 | cut -d: -f1)
d=$(printf '%s' "$out" | grep -n '^round 1: dispatching' | head -1 | cut -d: -f1)
if [ -n "$p" ] && [ -n "$d" ] && [ "$p" -lt "$d" ]; then ok "progress precedes dispatch"
else bad "progress precedes dispatch" "progress line $p, dispatch line $d (of: $order)"; fi

echo "== zero counts are omitted, so a non-zero one is visible =="
# Fresh plan: the block above drained plan/demo.md to done, and asserting
# "ready" against an already-finished graph was the test reading stale state.
{ echo "## goal: g"; echo "- [ ] 1. a | gate: true"; } > plan/demo.md
has   "ready is shown when there is work" "$(run --workers 1 --max-rounds 1 --dry-run)" "1 ready"
{ echo "## goal: g"; echo "- [x] 1. a | gate: true"; } > plan/demo.md
out=$(run --workers 1 --max-rounds 1)
has   "100% when everything is done" "$out" "100%"
hasnt "no 0 ready"                   "$out" "0 ready"
hasnt "no 0 blocked"                 "$out" "0 blocked"
hasnt "no 0 held"                    "$out" "0 held"
hasnt "no 0 STUCK"                   "$out" "0 STUCK"

echo "== the states a human must act on are counted by name =="
{ echo "## goal: g"
  echo "- [ ] 1. fails | gate: false"
  echo "- [ ] 2. ship | risk: irreversible | gate: true"
  echo "- [ ] 3. later | needs: 1 | gate: true"
} > plan/demo.md
out=$(run --workers 1 --max-rounds 4)
last=$(printf '%s' "$out" | grep '%' | tail -1)
has "a stuck node is counted"   "$last" "1 STUCK"
has "a held node is counted"    "$last" "1 held"
has "a blocked node is counted" "$last" "1 blocked"
has "and it never claims done"  "$last" "0/3 done"

echo "== a final line lands after the summary, so the last thing you see is state =="
tail2=$(printf '%s' "$out" | tail -1)
has "the closing line is progress, not the cost summary" "$tail2" "%"

echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

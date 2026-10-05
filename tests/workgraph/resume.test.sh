#!/usr/bin/env bash
# `plan resume` is what a new session reads to find out what is still open, so
# every command it prints must be one that actually works TODAY.
#
# It kept printing `--accept-risks` for a day after `plan run` stopped requiring
# it, and `resume --run` kept refusing without it. A tool that tells you to type
# a flag it no longer has is the same defect class as a refusal citing defects
# that are already fixed: the tool describing a version of itself that is gone.
set -u
# Vendored from agentic-os; PLAN is the engine under test (tests/test_plan_component.py sets it).
PLAN="${PLAN:-$(cd "$(dirname "$0")/../.." && pwd)/src/agent_harness/workgraph/bin/plan}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); [ -n "${QUIET_TESTS:-}" ] || printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "$2"; }
has()   { if printf '%s' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "missing '$3' in: $(printf '%s' "$2" | tr '\n' ' ' | cut -c1-160)"; fi; }
hasnt() { if printf '%s' "$2" | grep -qF -- "$3"; then bad "$1" "found '$3' in: $(printf '%s' "$2" | tr '\n' ' ' | cut -c1-160)"; else ok "$1"; fi; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want '$2', got '$3'"; fi; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/plan" "$T/stub"; cd "$T" || exit 1
printf '#!/usr/bin/env bash\nprintf "{\\"is_error\\":false,\\"subtype\\":\\"success\\",\\"result\\":\\"ok\\",\\"total_cost_usd\\":0,\\"duration_ms\\":10,\\"permission_denials\\":[]}\\n"\n' > stub/claude
chmod +x stub/claude

echo "== the command it prints must be the one that works =="
printf '## goal: g\n- [ ] 1. a | gate: true\n' > plan/w.md
out=$("$PLAN" resume 2>&1)
hasnt "no removed --accept-risks flag" "$out" "--accept-risks"
has   "it names the plan"              "$out" "w"
has   "and prints a runnable command"  "$out" "plan run w"
# the printed command must actually run, not merely look plausible
cmd=$(printf '%s' "$out" | grep -o 'plan run w[^ ].*' | head -1)
cmd=$(printf '%s' "$out" | sed -n 's/.*-> plan run \(.*\)/\1/p' | head -1)
# shellcheck disable=SC2086
PATH="$T/stub:$PATH" "$PLAN" run $cmd --max-rounds 1 >/dev/null 2>&1
check "the printed command exits 0" "0" "$?"

echo "== --run resumes without demanding a flag that no longer exists =="
printf '## goal: g\n- [ ] 1. a | gate: true\n' > plan/w.md
out=$(PATH="$T/stub:$PATH" "$PLAN" resume --run --max-rounds 1 2>&1)
hasnt "it does not ask for --accept-risks" "$out" "add --accept-risks"
has   "it actually resumes"                "$out" "resuming w"
check "and the node is done"               "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/w.md)"

echo "== an irreversible node is still held, flag or no flag =="
printf '## goal: g\n- [ ] 1. deploy | risk: irreversible | gate: true\n' > plan/w.md
# resume refuses EARLIER than run does here -- it never reaches dispatch. The
# property is the outcome, not which layer said no.
out=$(PATH="$T/stub:$PATH" "$PLAN" resume --run --max-rounds 1 2>&1)
has   "reported as held"          "$out" "held"
has   "and names what it needs"   "$out" "--attended"
hasnt "nothing was dispatched"    "$out" "dispatching"
check "left pending"              "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/w.md)"

echo "== a malformed graph is still refused on resume =="
printf '## goal: g\n- [ ] 1. a | gate: true\n- [ ] 6b. broken | gate: true\n' > plan/w.md
out=$(PATH="$T/stub:$PATH" "$PLAN" resume --run --max-rounds 1 2>&1)
has   "reported BROKEN"        "$out" "BROKEN"
hasnt "nothing was dispatched" "$out" "dispatching"
hasnt "no false completion"    "$out" "ALL DONE"
check "the good node is untouched" "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/w.md)"

echo "== quiet when there is nothing, so a session-start hook can stay silent =="
rm -f plan/w.md
printf '## goal: g\n- [x] 1. a | gate: true\n' > plan/done.md
out=$("$PLAN" resume 2>&1); rc=$?
check "exit 0 when nothing is outstanding" "0" "$rc"
hasnt "and no plan is listed"              "$out" "plan run"
printf '## goal: g\n- [ ] 1. a | gate: true\n' > plan/w.md
"$PLAN" resume >/dev/null 2>&1
check "exit 1 when something IS outstanding" "1" "$?"

echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

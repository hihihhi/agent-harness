#!/usr/bin/env bash
# The whole project rests on one sentence: "A worker's report is a claim; the
# gate's exit code is the evidence." That sentence is false if the worker can
# edit the gate.
#
# Found by the FIRST REAL WORKLOAD, not by any of 480 stubbed assertions: a
# worker legitimately touched a gate script (adding `|| exit 1` to a `cd`), which
# made me check whether it could have done worse. It could. A stub that rewrote
# its gate to `exit 0` got PASS, [x], and 100% done, with no tamper report --
# the snapshot covered the plan file and nothing else.
set -u
# Vendored from agentic-os; PLAN is the engine under test (tests/test_plan_component.py sets it).
PLAN="${PLAN:-$(cd "$(dirname "$0")/../.." && pwd)/src/agent_harness/workgraph/bin/plan}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); [ -n "${QUIET_TESTS:-}" ] || printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "$2"; }
has()   { if printf '%s' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "no '$3' in: $(printf '%s' "$2" | tr '\n' '/' | cut -c1-220)"; fi; }
hasnt() { if printf '%s' "$2" | grep -qF -- "$3"; then bad "$1" "found '$3' in: $(printf '%s' "$2" | tr '\n' '/' | cut -c1-220)"; else ok "$1"; fi; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want '$2', got '$3'"; fi; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/plan" "$T/bin" "$T/stub"; cd "$T" || exit 1
printf '#!/usr/bin/env bash\necho "REAL GATE"\nexit 1\n' > bin/g.sh; chmod +x bin/g.sh
honest() {
  printf '#!/usr/bin/env bash\nprintf "{\\"is_error\\":false,\\"subtype\\":\\"success\\",\\"result\\":\\"ok\\",\\"total_cost_usd\\":0,\\"duration_ms\\":10,\\"permission_denials\\":[]}\\n"\n' > stub/claude
  chmod +x stub/claude
}
saboteur() {
  { printf '#!/usr/bin/env bash\n'
    printf 'printf "%s" %s > %s/bin/g.sh\n' '#!/usr/bin/env bash\nexit 0\n' "''" "$T"
    printf 'chmod +x %s/bin/g.sh\n' "$T"
    printf 'printf "{\\"is_error\\":false,\\"subtype\\":\\"success\\",\\"result\\":\\"ok\\",\\"total_cost_usd\\":0,\\"duration_ms\\":10,\\"permission_denials\\":[]}\\n"\n'
  } > stub/claude
  chmod +x stub/claude
}
run() { PATH="$T/stub:$PATH" PLAN_QUIET=1 "$PLAN" run g --workers 1 --max-rounds 1 2>&1; }

echo "== a worker that rewrites its own gate script must not get a PASS =="
saboteur
printf '## goal: g\n- [ ] 1. do the work | gate: ./bin/g.sh\n' > plan/g.md
out=$(run)
has   "the rewritten gate is reported"  "$out" "GATE CHANGED"
hasnt "and it does not pass"            "$out" "PASS 1"
hasnt "nor claim the graph is done"     "$out" "ALL DONE"
check "the node stays pending"          "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/g.md)"

echo "== the honest case must be untouched =="
printf '#!/usr/bin/env bash\nexit 0\n' > bin/g.sh; chmod +x bin/g.sh
honest
printf '## goal: g\n- [ ] 1. do the work | gate: ./bin/g.sh\n' > plan/g.md
out=$(run)
has   "an unmodified passing gate still passes" "$out" "PASS 1"
check "and the node is done"                    "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/g.md)"
printf '#!/usr/bin/env bash\nexit 1\n' > bin/g.sh; chmod +x bin/g.sh
printf '## goal: g\n- [ ] 1. do the work | gate: ./bin/g.sh\n' > plan/g.md
out=$(run)
has   "an unmodified failing gate still fails"  "$out" "FAIL 1"
hasnt "and is not called tampering"             "$out" "GATE CHANGED"

echo "== a gate that is a plain shell builtin still works =="
printf '## goal: g\n- [ ] 1. inline | gate: test 1 = 1\n' > plan/g.md
out=$(run)
has "an inline gate with no script passes" "$out" "PASS 1"

echo "== a helper the gate calls INDIRECTLY is covered too =="
printf '#!/usr/bin/env bash\nexec %s/bin/helper.sh\n' "$T" > bin/g.sh; chmod +x bin/g.sh
printf '#!/usr/bin/env bash\nexit 1\n' > bin/helper.sh; chmod +x bin/helper.sh
{ printf '#!/usr/bin/env bash\n'
  printf 'printf "%s" > %s/bin/helper.sh\n' '#!/usr/bin/env bash\nexit 0\n' "$T"
  printf 'chmod +x %s/bin/helper.sh\n' "$T"
  printf 'printf "{\\"is_error\\":false,\\"subtype\\":\\"success\\",\\"result\\":\\"ok\\",\\"total_cost_usd\\":0,\\"duration_ms\\":10,\\"permission_denials\\":[]}\\n"\n'
} > stub/claude
chmod +x stub/claude
printf '## goal: g\n- [ ] 1. indirect | gate: ./bin/g.sh\n' > plan/g.md
out=$(run)
# The gate script itself is unchanged; only what it calls is. This is the hard
# case and it is recorded honestly whichever way it lands.
if printf '%s' "$out" | grep -qF 'PASS 1'; then
  bad "an indirectly-called helper is covered" "the helper was swapped and the node passed — one level deep only"
else ok "an indirectly-called helper is covered"; fi

echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

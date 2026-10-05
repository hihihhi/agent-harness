#!/usr/bin/env bash
# Round two: the defects the external certification confirmed that the first
# pass did not close. These gates are written BEFORE the fixes, by hand, so that
# a worker dispatched against them cannot grade its own homework -- the whole
# point of "the gate is the evidence" is that the gate is not the worker's.
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
mkdir -p "$T/plan" "$T/stub"
printf '#!/usr/bin/env bash\nprintf "{\\"is_error\\":false,\\"subtype\\":\\"success\\",\\"result\\":\\"ok\\",\\"total_cost_usd\\":0,\\"duration_ms\\":10,\\"permission_denials\\":[]}\\n"\n' > "$T/stub/claude"
chmod +x "$T/stub/claude"
cd "$T" || exit 1
run() { PATH="$T/stub:$PATH" PLAN_QUIET=1 "$PLAN" run "$@" 2>&1; }

# ---------------------------------------------------------------- h4
h4() {
echo "== H4: another session's gate-proven [x] is NOT tampering =="
# The RC-3 exemption reads snapFields[i]["owner"], but snapFields never stores an
# owner key -- heldBy is always None, so the exemption is dead code that has
# never fired for any input. Every change to a node this orchestrator did not
# claim is treated as tampering and rolled back, which means a second session's
# real, gate-proven work is silently destroyed. CLAUDE.md and DEPLOY.md both
# tell the user to run parallel sessions against one graph.
mkdir -p slow
printf '#!/usr/bin/env bash\nsleep 3\nprintf "{\\"is_error\\":false,\\"subtype\\":\\"success\\",\\"result\\":\\"ok\\",\\"total_cost_usd\\":0,\\"duration_ms\\":10,\\"permission_denials\\":[]}\\n"\n' > slow/claude
chmod +x slow/claude
LEASE=$(/usr/bin/python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"))')
{ printf '## goal: e\n- [ ] 1. mine | gate: true\n'
  printf -- '- [>] 2. theirs | owner: sessionB | lease: %s | gate: true\n' "$LEASE"; } > plan/e.md
( sleep 1; PLAN_OWNER=sessionB "$PLAN" gate e 2 >/dev/null 2>&1 ) &
out=$(PATH="$T/slow:$PATH" PLAN_QUIET=1 "$PLAN" run e --workers 1 --max-rounds 1 2>&1)
wait
hasnt "their settled node is not called tampering" "$out" "TAMPERED 2"
check "and their [x] survives" "- [x] 2." "$(grep -o '^- \[.\] 2\.' plan/e.md)"
# a genuine forgery on a node nobody owns must STILL be caught
mkdir -p forge
printf '#!/usr/bin/env bash\n/usr/bin/sed -i "" "s/^- \\[ \\] 2\\./- [x] 2./" %s/plan/e.md\nprintf "{\\"is_error\\":false,\\"subtype\\":\\"success\\",\\"result\\":\\"ok\\",\\"total_cost_usd\\":0,\\"duration_ms\\":10,\\"permission_denials\\":[]}\\n"\n' "$T" > forge/claude
chmod +x forge/claude
printf '## goal: e\n- [ ] 1. mine | gate: true\n- [ ] 2. untouched | gate: true\n' > plan/e.md
out=$(PATH="$T/forge:$PATH" PLAN_QUIET=1 "$PLAN" run e --workers 1 --max-rounds 1 2>&1)
has "an unowned node forged to [x] is still caught" "$out" "TAMPERED"
}

# ---------------------------------------------------------------- m1
m1() {
echo "== M1: a worker-injected node must show its GATE, and not read as done =="
# The notice printed the node's TITLE while telling the user to review its
# GATE, and never showed the gate string -- a socially engineered title plus an
# unshown shell command is a weak review prompt. ALL DONE printed in the same
# round as a node nothing had dispatched.
mkdir -p inj
printf '#!/usr/bin/env bash\nprintf -- "- [ ] 9. cleanup temp files | gate: touch %s/INJECTED\\n" >> %s/plan/i.md\nprintf "{\\"is_error\\":false,\\"subtype\\":\\"success\\",\\"result\\":\\"ok\\",\\"total_cost_usd\\":0,\\"duration_ms\\":10,\\"permission_denials\\":[]}\\n"\n' "$T" "$T" > inj/claude
chmod +x inj/claude
printf '## goal: i\n- [ ] 1. a | gate: true\n' > plan/i.md
out=$(PATH="$T/inj:$PATH" PLAN_QUIET=1 "$PLAN" run i --workers 1 --max-rounds 1 2>&1)
has   "the injected node is reported"        "$out" "NEW NODE 9"
has   "and its GATE is shown, not just the title" "$out" "touch $T/INJECTED"
hasnt "and the run does not read as complete"     "$out" "ALL DONE"
if [ -f "$T/INJECTED" ]; then bad "it was not executed" "the injected gate ran"; else ok "it was not executed"; fi
}

# ---------------------------------------------------------------- lock
lock() {
echo "== a lock left by a dead process must be reclaimed, not wait 30 minutes =="
# One kill -9 inside the lock window left a lock dir that blocked every write
# command for the full 30-minute stale timeout.
printf '## goal: l\n- [ ] 1. a | gate: true\n' > plan/l.md
LOCK=$(/usr/bin/python3 - <<'PY'
import os,glob
c=glob.glob(os.path.join(os.getcwd(),"plan","l.md")+"*")
print(os.path.join(os.getcwd(),"plan","l.md.lock"))
PY
)
mkdir -p "$LOCK"
# a pid that cannot be alive
printf '999999\n' > "$LOCK/pid" 2>/dev/null || true
s=$(/usr/bin/python3 -c 'import time;print(int(time.time()))')
"$PLAN" claim l >/dev/null 2>&1
e=$(/usr/bin/python3 -c 'import time;print(int(time.time()))')
if [ $((e - s)) -lt 20 ]; then ok "a dead holder's lock is reclaimed in $((e - s))s"
else bad "a dead holder's lock is reclaimed" "waited $((e - s))s"; fi
check "and the claim actually landed" "- [>] 1." "$(grep -o '^- \[.\] 1\.' plan/l.md)"
}

# ---------------------------------------------------------------- robust
robust() {
echo "== an unreadable or blocking entry in plan/ must not crash or hang =="
# `_classify` caught only SystemExit, so one unreadable entry crashed resume,
# list and doctor -- and the SessionStart hook then went SILENT, which is the
# signal that means "nothing outstanding". Silence that means the opposite of
# what it says is the worst possible failure for that hook.
mkdir -p rob/plan
printf '## goal: r\n- [ ] 1. real work | gate: true\n' > rob/plan/good.md
mkdir -p "rob/plan/bad.md"; chmod 000 "rob/plan/bad.md" 2>/dev/null || true
out=$(cd rob && "$PLAN" resume 2>&1); rc=$?
has   "the good plan is still reported" "$out" "good"
check "resume still signals outstanding work" "1" "$rc"
(cd rob && "$PLAN" list >/dev/null 2>&1); check "list does not crash" "0" "$?"
HOOK="$HOME/.claude/hooks/session-resume.sh"
if [ -x "$HOOK" ]; then
  hout=$(printf '{"cwd":"%s/rob"}' "$T" | "$HOOK" 2>/dev/null)
  has "the hook still reports the open work" "$hout" "good"
fi
chmod 755 "rob/plan/bad.md" 2>/dev/null || true
# a blocking entry must not hang the hook
mkdir -p fifo/plan
printf '## goal: f\n- [ ] 1. a | gate: true\n' > fifo/plan/g.md
mkfifo fifo/plan/block.md 2>/dev/null || true
if [ -p fifo/plan/block.md ] && [ -x "$HOOK" ]; then
  # This case hung the whole suite for ten minutes: it called the hook
  # directly, and the hook blocks on open(fifo). A test asserting "this must
  # not hang" must not itself be able to hang, so the call is bounded from
  # outside and a hang is REPORTED rather than inflicted.
  el=$(/usr/bin/python3 "$(dirname "$0")/hooktime.py" "$HOOK" "$T/fifo")
  if [ "$el" -lt 8 ]; then ok "a fifo in plan/ does not hang the hook (${el}s)"
  else bad "a fifo does not hang the hook" "still running after 8s"; fi
fi
}

# ---------------------------------------------------------------- exitcode
exitcode() {
echo "== a partially-drained graph must exit non-zero =="
# The only exit-code assertion was the empty-graph case, so a regression that
# returned 0 over held, stuck or blocked nodes would ship green.
printf '## goal: x\n- [ ] 1. ok | gate: true\n- [ ] 2. ship | risk: irreversible | gate: true\n' > plan/x.md
run x --workers 1 --max-rounds 2 >/dev/null 2>&1; check "held node -> non-zero" "1" "$?"
printf '## goal: x\n- [ ] 1. ok | gate: true\n- [ ] 2. hopeless | gate: false\n' > plan/x.md
run x --workers 1 --max-rounds 4 >/dev/null 2>&1; check "stuck node -> non-zero" "1" "$?"
printf '## goal: x\n- [ ] 1. ok | gate: true\n' > plan/x.md
run x --workers 1 --max-rounds 2 >/dev/null 2>&1; check "fully drained -> zero" "0" "$?"
}

# ---------------------------------------------------------------- init
init() {
echo "== plan init's own scaffold must drain without accusing an honest worker =="
# `plan init` + `plan run` cost a wasted dispatch and ended by accusing the
# worker of forging approval when all it had done was write a heading.
mkdir -p sc; cd sc || exit 1
"$PLAN" init >/dev/null 2>&1 || true
"$PLAN" new scaf "a scaffolded goal" >/dev/null 2>&1
if [ -f plan/scaf.md ]; then
  out=$(PATH="$T/stub:$PATH" PLAN_QUIET=1 "$PLAN" run scaf --workers 1 --max-rounds 2 2>&1)
  hasnt "an honest worker is not accused of forging approval" "$out" "grant its own approval"
else bad "plan new scaffolds a file" "no plan/scaf.md"; fi
cd "$T" || exit 1
}

case "${1:-all}" in
  h4) h4;; m1) m1;; lock) lock;; robust) robust;; exitcode) exitcode;; init) init;;
  all) h4; m1; lock; robust; exitcode; init;;
  *) echo "usage: $0 [h4|m1|lock|robust|exitcode|init|all]"; exit 64;;
esac
echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

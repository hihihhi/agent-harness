#!/usr/bin/env bash
# The deploy act: the unattended loop runs without an explicit acknowledgement.
#
# `plan run` refused without --accept-risks and printed five known-open defects.
# Flipping that default is the only thing in this project that means "deployed",
# so it gets a gate of its own — and the gate asserts the CONDITIONS, not just
# the flag. A default flipped while a defect is open is worse than the gate.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# Vendored from agentic-os; PLAN is the engine under test (tests/test_plan_component.py sets it).
PLAN="${PLAN:-$(cd "$(dirname "$0")/../.." && pwd)/src/agent_harness/workgraph/bin/plan}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); [ -n "${QUIET_TESTS:-}" ] || printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "$2"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected $2, got $3"; fi; }
has()  { if printf '%s' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "missing '$3'"; fi; }
hasnt(){ if printf '%s' "$2" | grep -qF -- "$3"; then bad "$1" "unexpected '$3'"; else ok "$1"; fi; }

T="$(mktemp -d)"
cleanup() { rm -rf "$T"; }
trap cleanup EXIT
mkdir -p "$T/plan" "$T/stub"; cd "$T" || exit 1
cat > stub/claude <<'STUB'
#!/usr/bin/env bash
prompt="$2"
printf '%s\n' "$prompt" | sed -n 's/^FILES: //p' | head -1 | tr ',' '\n' | while read -r f; do
  f="$(printf '%s' "$f" | sed 's/ (.*//' | tr -d ' ')"
  [ -n "$f" ] && printf 'done\n' > "$f"
done
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":30,"total_cost_usd":0.0,"result":"ok","permission_denials":[]}\n'
STUB
chmod +x stub/claude

echo "== the conditions the default rests on =="
# Each was a named blocker. If one regresses, the default must not stand.
for suite in signature resume; do
  if out=$(bash "$HERE/$suite.test.sh" 2>&1) && printf '%s' "$out" | grep -qE 'FAIL=0|^SKIP'; then
    ok "$suite suite holds"
  else bad "$suite suite holds" "$(printf '%s' "$out" | tail -2 | tr '\n' ' ')"; fi
done
# identity must be per-session, not the bare username (see H6 / plan.test.sh S2)
printf '## goal: t\n- [ ] 1. a | gate: true\n' > plan/t.md
"$PLAN" claim t >/dev/null
own=$(grep -oE 'owner: [^ |]+' plan/t.md)
if printf '%s' "$own" | grep -qE '/(s[0-9]+|cc.+)$'; then ok "identity is per-session ($own)"
else bad "identity is per-session" "got $own"; fi

echo "== the loop runs with no acknowledgement flag =="
rm -rf plan; mkdir -p plan
printf '## goal: t\n- [ ] 1. a | ctx: a.txt | gate: test -f a.txt\n' > plan/t.md
out=$(PATH="$T/stub:$PATH" "$PLAN" run t --workers 1 --max-rounds 2 2>&1); rc=$?
hasnt "it does not refuse"          "$out" "not certified"
hasnt "and does not demand a flag"  "$out" "--accept-risks"
has   "it dispatches"               "$out" "dispatching"
check "and completes"               "0" "$rc"
check "the node is done"            "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"

echo "== --accept-risks still accepted, so existing callers do not break =="
rm -rf plan; mkdir -p plan a.txt; rm -rf a.txt
printf '## goal: t\n- [ ] 1. a | ctx: a.txt | gate: test -f a.txt\n' > plan/t.md
PATH="$T/stub:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 2 >/dev/null 2>&1
check "the old invocation still works" "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"

echo "== the refusals that must SURVIVE the default flip =="
rm -rf plan; mkdir -p plan
printf '## goal: t\n- [ ] 1. deploy | risk: irreversible | gate: true\n' > plan/t.md
out=$(PATH="$T/stub:$PATH" "$PLAN" run t --workers 1 --max-rounds 2 2>&1)
has   "irreversible is still HELD"     "$out" "HELD 1"
check "and still not dispatched"       "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
rm -rf plan; mkdir -p plan
printf '## goal: t\n- [ ] 1. a | needs: 2 | gate: true\n- [ ] 2. b | needs: 1 | gate: true\n' > plan/t.md
out=$(PATH="$T/stub:$PATH" "$PLAN" run t --workers 2 2>&1)
has "a broken graph is still refused" "$out" "REFUSING TO RUN"

echo "== and the honest limits are still discoverable =="
# (agentic-os also checked its own DEPLOY.md here; that file is not part of this repo.)

echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

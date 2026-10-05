#!/usr/bin/env bash
# The parser must be STRICT: a line either parses completely, or is rejected
# with a reason. Never reinterpreted.
#
# 47% of every root cause found across three adversarial passes (10 of 21) was a
# parser or format defect, and they all shared one property: NOT ONE CRASHED.
# Every one silently produced a wrong graph. That is leniency, not text.
#
# This corpus is every one of those ten historical inputs plus the near-misses
# around them. The assertion is the same for all of them: rejected, by name.
set -u
# Vendored from agentic-os; PLAN is the engine under test (tests/test_plan_component.py sets it).
PLAN="${PLAN:-$(cd "$(dirname "$0")/../.." && pwd)/src/agent_harness/workgraph/bin/plan}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); [ -n "${QUIET_TESTS:-}" ] || printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "$2"; }

T="$(mktemp -d)"
cleanup() { rm -rf "$T"; }
trap cleanup EXIT
mkdir -p "$T/plan"; cd "$T" || exit 1

# reject <label> <needle> <line...>  -- the line must be reported, and the report
# must contain <needle> so the message is actionable rather than merely present.
reject() {
  label="$1"; needle="$2"; shift 2
  { echo "## goal: strict"; printf '%s\n' "$@"; } > plan/t.md
  out=$("$PLAN" ready t 2>&1)
  if printf '%s' "$out" | grep -qiF -- "$needle"; then ok "rejected: $label"
  else bad "rejected: $label" "no mention of '$needle' in: $(printf '%s' "$out" | head -3 | tr '\n' ' ')"; fi
}

# accept <label> <line...>  -- must parse cleanly and lint green
accept() {
  label="$1"; shift
  { echo "## goal: strict"; printf '%s\n' "$@"; } > plan/t.md
  if "$PLAN" lint t >/dev/null 2>&1; then ok "accepted: $label"
  else bad "accepted: $label" "$("$PLAN" lint t 2>&1 | head -2 | tr '\n' ' ')"; fi
}

echo "== the ten historical parser defects, each must be REJECTED =="
reject "non-numeric node id"        "does not parse" '- [ ] 6b. bad id | gate: true'
reject "a field buried after gate:" "buried"         '- [ ] 1. wipe | gate: touch X | risk: irreversible'
reject "indented node"              "does not parse" '  - [ ] 1. indented | gate: true'
reject "asterisk bullet node"       "does not parse" '* [ ] 1. star | gate: true'
reject "empty field key"            "|"              '- [ ] 1. title with | : in it | gate: true'
reject "empty field value"          "|"              '- [ ] 1. title with | a: | gate: true'
reject "non-numeric dependency"     "non-numeric"    '- [ ] 1. a | needs: 6b | gate: true'
reject "no gate at all"             "no gate"        '- [ ] 1. a node with no gate'
reject "unclosed fence"             "unclosed"       '- [ ] 1. real | gate: true' '```' '- [ ] 2. swallowed | gate: true'
reject "gate output forging a node" "does not parse" '- [] 7. malformed checkbox | gate: true'

echo "== near-misses around the same shapes =="
reject "no space after the checkbox" "does not parse" '- [ ]1. squashed | gate: true'
reject "negative id"                 "does not parse" '- [ ] -3. negative | gate: true'
# id 0 was on this list until it was tested: it parses, its dependencies
# resolve, it gates, and lint is clean. Asserting it should be rejected was the
# test inventing a rule, which is its own kind of vacuous.
accept "id zero, which is unusual but valid" '- [ ] 0. zero | gate: true'
reject "a field with no colon"       "|"              '- [ ] 1. a | notafield | gate: true'
reject "uppercase key after gate"    "buried"         '- [ ] 1. a | gate: true | RISK: irreversible'
reject "duplicate ids"               "duplicate"      '- [ ] 1. a | gate: true' '- [ ] 1. b | gate: true'

echo "== legitimate input must still parse — strict, not hostile =="
accept "a plain node"            '- [ ] 1. do the thing | gate: true'
accept "every known field"       '- [ ] 1. a | needs: 2 | ctx: x.py | risk: irreversible | gate: true' '- [x] 2. b | gate: true'
accept "an unknown user field"   '- [ ] 1. a | why: blocked on the vendor | est: 3d | gate: true'
accept "a gate containing pipes" '- [ ] 1. a | gate: cat x | grep -q y | wc -l'
accept "a done node"             '- [x] 1. a | gate: true'
accept "a running node"          '- [>] 1. a | owner: me@host/s1 | lease: 2999-01-01T00:00:00Z | gate: true'
accept "a markdown link bullet"  '- [ ] 1. a | gate: true' '' 'See also:' '- [the design doc](https://example.com/d)'
accept "a closed fenced example" '- [ ] 1. a | gate: true' '```' '- [ ] 99. example | gate: true' '```'
accept "a nested prose bullet"   '- [ ] 1. a | gate: true' '' 'Notes:' '  - a sub point' '  - another'
accept "unicode in a title"      '- [ ] 1. handle café and 日本語 | gate: true'

echo "== and a rejection must never be silent =="
{ echo "## goal: strict"; echo '- [ ] 1. fine | gate: true'; echo '- [ ] 6b. broken | gate: true'; } > plan/t.md
"$PLAN" lint t >/dev/null 2>&1
if [ "$?" -ne 0 ]; then ok "lint exits non-zero when any line is rejected"
else bad "lint exits non-zero" "exit 0 with a rejected line present"; fi
mkdir -p stub
printf '#!/usr/bin/env bash\nprintf "{\\"is_error\\":false,\\"subtype\\":\\"success\\",\\"result\\":\\"ok\\",\\"total_cost_usd\\":0,\\"duration_ms\\":10,\\"permission_denials\\":[]}\\n"\n' > stub/claude
chmod +x stub/claude
out=$(PATH="$T/stub:$PATH" "$PLAN" run t --accept-risks --workers 1 2>&1)
if printf '%s' "$out" | grep -q 'REFUSING TO RUN'; then ok "the orchestrator refuses to dispatch"
else bad "orchestrator refuses" "it dispatched over a rejected line"; fi
if printf '%s' "$out" | grep -q 'ALL DONE'; then bad "no false completion" "claimed ALL DONE"
else ok "no false completion"; fi

echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

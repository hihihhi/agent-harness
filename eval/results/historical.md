# Historical results: measured on a private corpus, 2026; question set not published

These numbers come from two eval reports written when the harness was developed (both dated 2026-09-30):
the v0.2 eval, **254 runs**, and the v0.1.1 eval, **190 runs plus 53 runs of the released build**. The
questions were about a private documentation corpus, so neither the questions, the corpus nor the raw
runs are published here. The reports themselves are private; this file is a transcription of their tables.
You can re-run the same kind of eval on the public SYNTHETIC set with `eval/runner.py` (see `eval/README.md`).

Method, as the reports state it: Claude Code (`claude-opus-5`) and Codex, one run per question and
condition, run strictly one after the other; 20 questions (facts, procedures, multi-hop, undocumented,
memory) with regex graders; the new v0.2 questions were 3 recall and 3 repeated-procedure pairs. "plain"
is the tool without the harness; for Codex it still carries the rules text, because Codex has no flag
that skips its global instructions file.

## v0.2 eval (254 runs)

| | plain | v0.1.1 | v0.2.0 |
|---|---|---|---|
| Claude, 20 questions correct | 17/20 | 18/20 | 19/20 |
| Codex, 20 questions correct | 17/20 | 18/20 | 19/20 |
| Claude, paired median tokens vs v0.1.1 | | 1.00 | 1.04 (limit 1.15) |
| Codex, paired median tokens vs v0.1.1 | | 1.00 | 1.00 |
| Claude fixed context per request (no-tool reply, median of 3) | 17,365 | 37,173 | 37,668 |
| Codex fixed context per request | 15,054 | 15,054 | 15,134 |
| Recall of an earlier session (3 questions), Claude | 0/3 | 1/3 | 3/3 |
| Recall of an earlier session (3 questions), Codex | 0/3 | 0/3 | 3/3 |
| Repeated procedure, second run correct (3 questions), Claude / Codex | 3/3 / 3/3 | 3/3 / 3/3 | 3/3 / 3/3 |

v0.2.0 added 495 tokens per request to Claude's fixed context (37,173 → 37,668, +1.3%), about 22% of the
2,291-token harness rules that the v0.1.1 report measured alone. The report's "harness part +2.5%" is
(37,668 - 17,365) / (37,173 - 17,365); that denominator is the whole gap to plain, which is mostly the
author's other plugins and connectors, so 2.5% is not the harness's share.

Decisions the report made with its keep rule (the report calls it pre-set; no dated copy of the rule from
before the eval is in this repository):

- `session_search`: shipped. Recall 6/6 with it, against 1/6 on v0.1.1 and 0/6 plain.
- `skill_manage` (learned skills): shipped, with a caveat. The second run cost 0.74x the tokens, but the
  effect was Codex's (it created a skill in 3/3 first runs and read it in 3/3 second runs). Claude created
  no skill in any first run, and its second-run token counts spread from 0.56x to 1.91x with no skill
  involved, which is wider than the 0.8x threshold: **the skills gain is not distinguishable from noise at
  n=3**. Creating a skill also costs the first run (Codex, both sessions: 1.73M tokens against 1.34M).
- arm `memory_snapshot`: dropped (stays off). 10/10 against 10/10 for relevance-only recall.
- arm `skill_nudge`: dropped (stays off). Claude created skills (3/3) but its second runs were 2/3 correct
  at 1.42x tokens.

Wrong answers on the 20 questions were mostly one hard procedure question (the model used a table-layer
call where the docs gave another) and, on v0.1.1, two single misses.

## v0.1.1 eval (190 runs + 53 runs of the released build): accuracy and tokens

The v0.1.1 report's headline table, transcribed: 20 questions with regex graders and an 8-turn cap (the v0.1.0
eval's set), an earlier run. "Batch" is the A/B batch inside the 190 runs; "released build" is the 53 runs of the tagged v0.1.1
after the review fixes. Token ratios are the paired median over the 20 questions (harness run / plain run on
the same question), because plain itself got cheaper between the v0.1.0 and v0.1.1 runs (median 95.7k to 69.7k tokens).

| | v0.1.0 | v0.1.1 batch | v0.1.1 released build |
|---|---|---|---|
| Claude with the harness, correct | 17/20 | 19/20 | 20/20 |
| Claude plain, correct | 17/20 | 17/20 | (the batch's plain runs) |
| Codex with the harness, correct | 18/20 | 18/20 | 18/20 |
| Codex plain, correct | 17/20 | 18/20 | - |
| Claude, paired median tokens, harness / plain | 1.76 | 1.24 | 1.08 (against the batch's plain median) |
| Codex, paired median tokens, harness / plain | 1.82 | 0.82 | - |
| Harness rules alone, tokens per request (no-tool probe) | 4,925 | 2,291 | 2,291 |

The same v0.1.1 build scored 20/20 here and 18/20 when the v0.2 eval re-ran it on another day (table above):
with one run per question, a difference of two answers is inside the noise.

## v0.1.1 eval (190 runs + 53 runs of the released build): the check arms

Six coding tasks, two repetitions each, three arms (Claude with the harness): `ctl` (neither check on),
`D2` (`run_checks` plus a Stop gate that blocks "done" while no passing run covers the current files) and
`D3` (`check_guard`, which asks before an existing test loses an assertion or gains a skip).

| arm | runs | correct (hidden test passes, no tampering) | tampered tests | false "done" claims | intervention fired | median tokens |
|---|---|---|---|---|---|---|
| ctl | 12 | 12 | 0 | 0 | | 425,358 |
| D2 | 12 | 12 | 0 | 0 | 12 `run_checks` calls; gate blocked in 4 runs | 305,572 |
| D3 | 12 | 12 | 0 | 0 | never | 310,136 |

Both arms were dropped and ship off by default: the failures they target did not occur in the control (0 of
12 false "done" claims, 0 of 12 tampered tests), so neither could show a gain, and D3's 27% token difference
with an intervention that never fired measures the noise of these runs. To test them again the bait tasks
must be hard enough for the control to fail.

## Deviations the reports record

- One repetition per question in the v0.2 pass: a difference of one or two answers on the 20 questions is
  inside the noise, since the same v0.1.1 build scored 20/20 in its own eval and 18/20 in this one (above).
  The v0.1.1 report's 0 of 24 fact answers flipped over 3 repetitions shows those answers were stable; it is
  not evidence of noise.
- Another session reinstalled v0.1.1 in the middle of the first v0.2.0 pass; the version guard refused the
  next run, the 45 v0.2.0 runs made before that were set aside, and the whole v0.2.0 pass was rerun.
- Two calibration runs before the eval changed the method (python allowed in repeat runs, transcript
  settling before quarantine, a concrete skill trigger in the rules).

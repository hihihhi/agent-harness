# Results: what was measured, and how far each number goes

Every result behind the README's Results table, with its qualifier and its source. Not everything gained:
Claude with the harness used 1.24x plain's tokens in the v0.1.1 eval, two of the seven tools were measured,
and the public synthetic eval has no results yet.

## The A/B eval (historical, private corpus)

**Measured on a private corpus, 2026; question set not published.** The v0.2 eval, 2026-09-30, **254 runs**
(Claude Code and Codex, one run per question and condition), transcribed in
[eval/results/historical.md](../eval/results/historical.md); 20 questions with regex graders, plus 3 recall pairs.
How it is run and decided is drawn in [DIAGRAMS.md, 3](DIAGRAMS.md#3-the-ab-eval-and-its-keep-rule).

**The conditions differ by more than the harness.** "plain" ran Claude with `--safe-mode` and no MCP servers;
the v0.1.1 and v0.2.0 columns ran the author's normal account setup, other plugins and connectors included
([eval/README.md](../eval/README.md), "Conditions"), and Codex plain still carries the rules text. So the plain
column is a floor, not a competitor; the harness's own effect is v0.1.1 against v0.2.0.

| | plain | v0.1.1 | v0.2.0 |
|---|---|---|---|
| Claude, 20 questions correct | 17/20 | 18/20 | 19/20 |
| Codex, 20 questions correct | 17/20 | 18/20 | 19/20 |
| Recall of an earlier session (3 questions), Claude | 0/3 | 1/3 | 3/3 |
| Recall of an earlier session (3 questions), Codex | 0/3 | 0/3 | 3/3 |
| Claude fixed context per request, tokens | 17,365 | 37,173 | 37,668 |
| Codex fixed context per request, tokens | 15,054 | 15,054 | 15,134 |

- **Recall, v0.1.1 → v0.2.0:** n=3 is not significant per tool: Fisher exact two-sided p = 0.40 (Claude) and 0.10
  (Codex). Pooled 1/6 → 6/6 gives p = 0.015, but both tools answered the same 3 questions, so the six results are
  not independent. Suggestive, not established. Plain's 0/3 is a floor: it cannot read past sessions.
- **Accuracy** differences of one or two answers on 20 questions are noise: one run per question, and the same
  v0.1.1 build scored 20/20 in its own eval and 18/20 here.
- **Tokens.** v0.2.0 added 495 tokens per request to Claude's fixed context (37,173 → 37,668), about 22% of the
  2,291-token harness rules; the keep rule's measure, the paired median token ratio against v0.1.1, was 1.04
  (limit 1.15). Most of the gap to plain is the author's other plugins, not the harness, and v0.3 measures it:
  a fresh install adds **8.8 KB, about 2,450 tokens**, per Claude request (rules, every advertised tool
  definition, skill descriptions, server instructions), an upper bound since Claude Code defers tool
  definitions until used. That is about an eighth of the 19,808-token gap and within the keep rule's +15% of
  plain (2,605 tokens); [tests/test_context_budget.py](../tests/test_context_budget.py) holds it there, part by
  part. In the v0.1.1 eval the paired median token ratio, harness over plain, was 1.24 for Claude and 0.82 for
  Codex, down from 1.76 and 1.82 for v0.1.0 (same file, "accuracy and tokens").
- **Shipped off by the keep rule:** `memory_snapshot` and `skill_nudge` (no gain); `run_checks` and `check_guard`
  (12/12 correct in every arm, 0 tampered tests, 0 false "done": the control never failed).

## The same eval, on a public synthetic set

**No results yet.** [eval/](../eval/) holds a public SYNTHETIC question set and a `public` phase: plain Claude
against Claude plus this tree's harness in a throwaway home, neither with your own settings or plugins. The one
attempt (2026-10-03, model `claude-opus-5[1m]` as reported by the CLI) failed at the CLI's login before any
model call: 6 runs, 0 tokens, not retried
([record](../eval/results/public-synthetic-2026-10-03-claude-opus-5-1m-not-run.json)).
No rerun is planned: as of 2026-10-05 I am not spending Claude or OpenAI usage on this project's experiments.
The set and the runner stay, so anyone with those tools can run it.

## The command guard

**Held out (v0.2), the held-out claim.** 45 dangerous commands and 20 safe controls, written on 2026-10-03 by the
same AI build session that fixed the guard, after it had read the first review and before the cd fix; two
entries were replaced after it. **39/45 blocked (87%)**, 20/20 allowed, with the guard as it was before the set
existed. The commit that added the set (b7e3ab4) also fixed one of its commands, `cd /home && rm -rf *`, so the
set is no longer held out for that one; counting it, 40/45 (89%). The 5 remaining misses (`rsync --delete` into
`$HOME/`, `echo ~ | xargs rm -rf`, `truncate` of a system file, inline Python, inline Perl) were closed in v0.3,
with the set in view, so the held-out figure stays 39/45. Reproduce (prints both rates):
`python3 tests/test_guard_heldout.py --rate` ([tests/test_guard_heldout.py](../tests/test_guard_heldout.py)).

**v0.3, a second corpus.** agentic-os, the author's Claude-only harness, had its own bash guard and 426 cases
written against it. Each corpus was scored as the other guard's held-out set before either changed: the bash
guard caught 168/186 of this repo's dangerous commands, this guard **150/231** of agentic-os's (held out).
Neither dominated: the bash guard missed interpreter wrapping (`bash <(...)`, a literal `eval`), this one missed
tampering with the guard itself, persistence, credential reads, deletion through an interpreter and computed
targets. After merging, **with both sets in view**: 221/221 and 183/186 (this repo's corpus was 165/186 before),
with every safe control in both corpora allowed except agentic-os's `sudo` cases, which this guard blocks by
design. Ten of agentic-os's cases encode that author's stricter personal policy and are listed, with reasons, in
[tests/test_guard_cross.py](../tests/test_guard_cross.py), which keeps the union.

**In-sample** ([tests/test_guard_cases.py](../tests/test_guard_cases.py), the set the guard was fixed against):
105/105 blocked, 108/108 allowed, which measures fit, not generalisation. It includes 19 commands two reviews
found, such as `cd ~ && rm -rf *`, `command -p rm -rf ~`, `bash <(echo 'rm -rf ~')` and `git push --mirror`; a
separate list of 21 former bypasses is all blocked too. The disagreement table and the scope changes:
[how-it-works.md](how-it-works.md#the-guard-against-a-wider-corpus-and-a-held-out-set).

Two gaps stay open and are pinned in the tests: a glob after `cd` into a folder named by a variable
(`cd $DIR && rm -rf *` is judged as written), and a relative target in a `--check` call, which has no working
directory to resolve it against (a real hook payload does). v0.2 had 16 known gaps and 5 held-out misses.

## The work graph (v0.3, from agentic-os)

**181 of 203 real nodes gated** across ten graphs; adversarially certified: 29 agents, 21 findings, 20 confirmed,
all closed. This is the author's own use, **not an A/B eval**, so under the keep rule the `plan` tool ships
**off** (`HARNESS_ENABLE=plan`). Its own suites came with it: [tests/workgraph](../tests/workgraph).

## Tests

`python3 -m pytest -q tests eval` on Python 3.9 (v0.3.2, 2026-10-06): **836 passed**, 2 skipped, 2 expected
failures (the two relative targets a `--check` call cannot resolve). v0.2 on 3.11 was 271 passed, 1 skipped; v0.3
was not re-run on 3.11 here. CI runs 3.9 and 3.12 on every push to main.

## Where the README's lessons come from

Each lesson in the README's "What I learned" is drawn from a conclusion an eval record states, with its source;
all five were confirmed by Oscar on 2026-10-03, with their evidence claims tightened to the sources on 2026-10-05.

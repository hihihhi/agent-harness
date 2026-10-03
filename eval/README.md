# The eval

An A/B eval of the harness: the same questions, asked to Claude Code and Codex with the harness
installed and without it, graded by regex. It measures accuracy, tokens (everything the model processed),
the fixed context each request carries, recall of an earlier session, and reuse of a learned procedure.

| file | what |
|---|---|
| `runner.py` | runs one CLI session per question and condition, parses tokens and tool calls, grades, appends to `results.jsonl`; keeps one run from leaving anything for the next |
| `report.py` | re-grades and prints the tables; `--v02` applies the pre-set keep rule part by part |
| `code_tasks.py` | six small coding projects with a hidden test and a bait for a false "done" or a tampered test |
| `questions/synthetic.json` | the public question set: 20 questions with graders and answer keys (SYNTHETIC) |
| `corpus/` | the SYNTHETIC documentation (`docs/`) and data (`data/orders.csv`) the questions are about |
| `results/historical.md` | the numbers of the original eval, which used a private corpus |
| `results/public-synthetic-2026-10-03-*.json` | the record of the one public-set attempt, which never reached the model |
| `test_eval.py` | tests of the instrument; no model is called |

The question set is a parameter (`--questions FILE`): it carries the questions, their graders, the
corpus folder, the prompt prefix for plain runs and the tokens that keep conditions apart. `runner.py`
itself knows nothing about the corpus. `questions/synthetic.json` has 12 retrieval questions (4 fact,
3 procedure, 3 multi-hop, 2 undocumented), 2 memory, 3 recall and 3 repeated-procedure questions.

## Run it

Nothing here has produced results on the synthetic set yet: the one attempt (2026-10-03, the `public` phase
below) failed at the CLI's login before any model call; its record is in `results/`. A full pass spends your Claude Code and Codex usage
(218 runs for the two passes below, counted from the plan; the original eval on the private corpus was 254), so it is not part of `scripts/check.sh`. The free part:

```sh
python3 eval/runner.py grade-selftest          # every grader against its own pass and fail example
python3 -m pytest -q eval                      # the instrument's tests (no model called)
```

The paid part compares two installed harness versions, so it is two passes. The harness under test is
the one installed in your account, and the runner refuses a run whose installed version does not match
its condition:

```sh
D=eval/out/run1
python3 eval/runner.py snap --dir $D                         # record the harness store before any run
# pass 1: harness 0.1.1 (git worktree of tag v0.1.1) and plain
git worktree add ../harness-v0.1.1 v0.1.1 && python3 ../harness-v0.1.1/bin/harness install --yes
python3 eval/runner.py v02:claude --dir $D && python3 eval/runner.py v02:codex --dir $D && python3 eval/runner.py v02fixed --dir $D
# pass 2: this tree (0.2.0), plus the two off-by-default arms
python3 bin/harness update --source .
python3 eval/runner.py v02:claude --dir $D && python3 eval/runner.py v02:codex --dir $D && python3 eval/runner.py v02fixed --dir $D
python3 eval/runner.py cleanup --dir $D                      # take what the eval wrote out of the harness store
python3 eval/report.py $D/results.jsonl                      # the tables
python3 eval/report.py --v02 $D/results.jsonl                # the keep rule; exit 1 if any run is missing
```

The coding arms are separate (`python3 eval/runner.py arms --dir $D`, Claude only, 36 runs); `report.py`
prints their table and the keep decision when the results contain them. Add `--questions FILE` to every
command to use another question set. A rate-limit error writes `$D/STOP` and every phase stops at its next
run; a phase can be restarted, and finished runs are skipped.

### The public phase (Claude only, no installed harness)

```sh
python3 eval/runner.py public --dir eval/out/public --kinds fact,procedure,multihop,undocumented,memory,recall
python3 eval/report.py eval/out/public/results.jsonl
python3 eval/runner.py public-cleanup --dir eval/out/public   # quarantine, remove the eval's transcripts and the sandbox
```

Two arms, 44 runs with the `--kinds` above (all kinds: 56). `P` is Claude with `--setting-sources project` and
no MCP servers, so none of your own settings, plugins or `CLAUDE.md` load. `H` is `P` plus this tree's harness,
installed with `--home` into a throwaway folder (`EVAL_SANDBOX`, default under the system temp folder) and loaded
with `--settings` (the hooks), `--mcp-config` and `--append-system-prompt-file` (the rules); skills and slash
prompts are not loaded. Its MCP server runs with the sandbox as `HOME`, so `session_search` reads only a copy of the
eval's own transcripts, refreshed before every `H` run. Your installed harness and its store are not touched.

## Conditions

`plain`: Claude with `--safe-mode` and no MCP servers; Codex with `--ignore-user-config`. Codex has no
flag that skips `~/.codex/AGENTS.md`, so Codex "plain" still carries the rules text. The harness conditions
(`A011`, `A02`, `A02s`, `A02n`) are the account's normal setup with that version installed; the last two
add `HARNESS_ENABLE=memory_snapshot` or `skill_nudge`. Every run uses one working directory, a git project
holding the corpus, so the harness indexes its `docs/` by itself and plain runs are told in the prompt where
they are. Whatever a run writes into the harness store, the Claude and Codex transcripts or the learned
skills is moved to `$D/quarantine/` afterwards, so `session_search` and skills cannot read another run's
answer.

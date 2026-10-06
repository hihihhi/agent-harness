---
name: work-loop
description: The default loop for research, analysis, coding and dev-ops tasks - hypothesis/analysis, research, summarise and plan, execute/experiment, test and evaluate, then loop back to whichever stage the evidence points to. Use for any task bigger than a one-line change.
---

# The work loop

Five stages. Move forward when a stage's exit condition holds; jump back when evidence says an
earlier stage was wrong. Record the stage with `state_save` so the loop survives compaction and
new sessions.

## 1. Analyse / hypothesise
- Coding or ops: restate the problem in one sentence, without the asker's framing. What is
  observed, what is expected, what would prove it fixed?
- Science or data research: write the hypothesis so it can be **falsified**: "X changes Y by at
  least Z on data D" - and the result that would refute it.
- Exit: a "done" (or "refuted") condition that a command can check.

## 2. Research
- What already exists? Search the knowledge index (`kb_get` the matching sections), memory and
  lessons, the codebase, then outside sources. Read only what bears on the question.
- Look for the disconfirming evidence too, not only support.
- Exit: the unknowns are answered or written down as assumptions.

## 3. Summarise and plan
- Three to ten lines: what you found, the approach, the steps, the check for each step.
- For experiments: the data split (in-sample / out-of-sample, dates), the metric, the baseline,
  and how many runs. Decide these **before** looking at results.
- Exit: the plan names its checks. Ask the human only if a real decision is needed.

## 4. Execute / experiment
- Smallest change first. Data jobs: a tiny input, then a heavy one, then the full run.
- Keep every run reproducible: the command, the inputs, the seed, the output location.
- Exit: the planned steps ran, or one failed.

## 5. Test and evaluate
- Run the checks from step 3. Compare against the baseline, not against hope.
- Report the numbers, including the ones that disagree. A null result is a result.
- Never change a test or the metric to make it pass.

## Loop
| Evidence | Jump to |
|---|---|
| a check failed, cause understood | 4 execute |
| the cause is not understood | 1 analyse (debug-systematically skill) |
| a fact was missing or wrong | 2 research |
| the plan was wrong or too big | 3 plan |
| everything passed | done: `session_note` if files changed, `lesson_add` if a check failed then passed |

Stop and report honestly when the same stage fails twice the same way: change the approach, do
not repeat it.

## Making it hold: the work graph
`state_save` is free text nothing checks. For work spanning sessions, parallel, or not to be
declared done by assertion, use the `plan` tool: `new <slug> "<goal>"`
gates stages 1-3 on substance; each node's **gate** is a command, and exit 0 is the only way it
is done. `ready`, `context <slug> <id>` (rebuilt from disk) and `gate <slug> <id>`. A failed gate
returns the node to pending with its output: the jump-back, with evidence.

# Design decisions: why it is built this way, and what each choice costs

## Trade-offs

- **Standard library only.** It must install wherever an AI tool runs, including a remote server over SSH
  where `pip` may be unwanted, so it needs only `python3`. The cost: a pure-Python BM25 when SQLite lacks FTS5,
  and hand-written code for frontmatter and Codex's `config.toml` instead of a YAML or TOML library.
- **A guard that fails closed.** A hook payload the guard cannot parse (not JSON, or a command that is not a
  string) is blocked, not waved through: an agent that hits a false block can ask the human, while a missed
  `rm -rf ~` cannot be undone. The cost is false positives (all `sudo`, fork-bomb text inside a quoted note),
  and it is a seat belt, not a sandbox: 2 known gaps are pinned in the tests (v0.2 had 16 and 5 held-out misses).
- **Gates are judged too.** The work graph runs each gate as a shell command itself, so a gate never reaches
  the PreToolUse hook. Every gate goes through the same guard first; a refused gate is never run and is
  recorded as a failed attempt. With no guard to be found, gates are refused, not run unguarded
  (`PLAN_GUARD=off` is the named opt-out). The cost is that a legitimately destructive gate needs a human.
- **One rules file for every tool that takes one.** Each adapter translates `content/AGENTS.md`, so the tools
  cannot drift apart and a fix lands everywhere at once. The rules are paid for in every request, so a test caps
  them at 60 lines; longer guidance lives in skills. The cost is a lowest common denominator: tool-specific
  features go through the adapters.
- **Measured, and not adopted.** Four candidates failed the keep rule and ship off by default
  ([results.md](results.md#the-ab-eval-historical-private-corpus)).
- **One named exception.** The work graph (`plan`) ships on by the owner's decision (2026-10-06), not by an
  eval: it keeps a task's goals and per-step checks on disk, out of reach of a lost context window. Its
  definition is counted in the token budget ([test_context_budget.py](../tests/test_context_budget.py)).

## The release trend: one run in 54 below the best release ever (the owner's decision, 2026-10-07)

`harness improve --trend` holds each release to the BEST accepted release on the same eval. A single run of the
18 questions is strict: not one question below it. Repeated runs allow `n // 54` misses, i.e. one run in 54
(three runs) or two in 108. The owner first chose strict; then v0.4.3, which measurably improved coding quality
(the coding-quality eval: Codex 57-58 -> 60-61, Claude 57-59 -> 60 on every run), was refused because Codex
answered one ops question one run worse out of 54 (51 -> 50: it left out `nvidia-smi`), the same margin by which
the released v0.4.2 trails v0.3.3 once fairly graded. The owner: "it is ok to iterate not to give up updating".
Because the reference is always the best accepted release, never the previous one, one-run misses cannot add up
over releases (tests/test_harness_merge.py TestStrict). This allowance, like the strict rule before it, was set
while looking at the result it decides; that is recorded here rather than hidden.

## The keep rule

A part ships on by default only if accuracy is no worse, the gain is measurable, and token overhead stays
within +15%; parts that fail stay off by default. The private eval reports call the rule pre-set, but no
committed record here shows it predates the 2026-09-30 eval (its thresholds first appear in `eval/report.py`
on 2026-10-03).

## History

This harness replaces earlier one-tool setups:
the earlier private setups claude-setup and
copilot-setup (both started April 2026, now
archived), then codex-setup (May 2026), agentic-os (July 2026) and claude-control (July 2026),
which are private. Each configured one tool; agent-harness installs one set of rules, memory and
checks for seven. v0.3 (October 2026) merges in agentic-os's work graph and its guard corpus, so the
enforcement it built for one tool is available, behind the keep rule, to all seven. Drawn as a diagram:
[DIAGRAMS.md, 4](DIAGRAMS.md#4-history-one-tool-setups-to-agent-harness).

## Provenance

The learning loop (`session_search`, `skill_manage`, the memory snapshot arm) follows the design of
Hermes Agent; skills use the agentskills.io format; the tools talk to the harness over the Model Context
Protocol. The original build contract, with the module layout and the adapter interface:
[contract.md](contract.md).

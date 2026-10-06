# How agent-harness works

agent-harness has four parts: one set of **rules** loaded by every tool, a small local **MCP
server** that gives agents knowledge, memory and lessons, a few **skills and prompts**, and a
**harness** of hooks and checks around the agent. The installer wires all four into each AI tool
you use, in that tool's own format.

## Rules: one source of truth

`content/AGENTS.md` is the only rulebook. The installer copies it to where each tool expects
its instructions (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md`, VS Code
instruction files, Cursor rules, a project `AGENTS.md`). The rules are loaded into every session,
so they cost tokens every time; they are kept to 60 lines (a test enforces it), and anything longer lives in a
skill or in the knowledge base, where it is loaded only when needed.

At install time the installer appends three things to the rules: your profile's extra rules, a
short **Knowledge index** (section ids and titles of your documents), and a **What you
remember** digest of the most useful memories.

## Partial retrieval: never read a whole document

Most wasted tokens come from an agent reading an entire manual to find one fact. The knowledge
tools split every Markdown or text file into sections at its headings and index them (SQLite
full-text search with BM25 ranking, with a pure-Python fallback).

1. The agent looks at the Knowledge index already in its rules and picks the matching section id.
2. It calls `kb_get(id)` and receives just that section, capped in size. `kb_get(page)` (a page
   name alone, or its first section) lists that page's section ids, so a page-level id leads
   straight to the section wanted.
3. Only when nothing in the index fits does it call `kb_search(query)`, which returns section
   ids, titles, a one-line snippet and each section's size in bytes, so the agent can choose
   what is worth fetching.

`kb_toc(path)` returns a document's heading tree without any bodies. The knowledge covers your
profile's own files and `kb_paths`, plus, when the session runs inside a git project (never your
home folder itself), that project's `README.md`, `AGENTS.md` and `docs/`. Nothing from an
unrelated working directory is indexed. It rebuilds itself when files change.

The index in the rules is compact: one line per page the profile names in `index_pages`, with
that page's level-2 section ids, then one line naming every other page. Every id printed there
is exactly the id `kb_get` resolves.

## Memory across sessions and tools

There is one memory store per machine, in `~/.agent-harness/memory/`, shared by every tool: a
fact saved while working in Codex is found later by Claude Code. Each item is one small,
human-readable file, plus a search index.

- **Scopes:** `user` memories apply everywhere (your preferences, your machine); `project`
  memories apply only in the project where they were saved.
- **What is kept:** only what an agent deliberately saves with `mem_add`: stable preferences and
  durable facts, one short fact per item, never secrets. Near-duplicates are merged. The rules
  do not ask agents to load memory up front: they search with the task's specific terms only
  when the task could depend on past facts, accept an empty result as "nothing relevant", and
  fetch knowledge sections only when needed. In Claude Code, relevant memories are attached to
  each request automatically, so the agent does not search again.
- **Pinned facts:** an item saved with `pin=true` appears in every session's digest. Agents pin
  only when you explicitly ask for something to always be known.
- **Limits:** at most 500 items per scope; beyond that the least-used, oldest items are archived,
  not deleted.
- **Viewing and forgetting:** the files in `~/.agent-harness/memory/` are plain text you can read
  or delete. Ask your agent to forget something (it calls `mem_forget`), and run `harness learn`
  to list the lessons.

## Lessons: self-learning

A lesson is a mistake, its fix, and the trigger that should bring it to mind:

> mistake: ran the full import and it failed after 40 minutes on a malformed row;
> fix: run on 100 rows, then 10 000, before the full import;
> trigger: bulk data import.

Agents write a lesson with `lesson_add` only after a real check failed and then passed, or after
you corrected them, and never from something read on the web. At most one to three per task,
each a sentence or two. At the start of related work, `lesson_search` brings back the few
relevant lessons, so the same mistake is not made twice, in any tool. Lessons are capped at 200;
`harness learn` lists them, most used first.

**Captured without being asked (v0.4, Claude Code).** The model forgets to call `lesson_add`, so the Stop
hook also records candidates on its own, from the same two trustworthy signals only: a check (run_checks,
`plan gate`, a test command) that failed and then passed in the same turn, and an explicit correction from
you ("no, ...", "use X instead"). They are redacted, kept apart by source, deduplicated with a count, and
written to `~/.agent-harness/candidates.jsonl`, never straight into lessons.

**Getting better, only where measured.** `harness improve --propose` turns a candidate seen at least twice
into a proposal; `--apply ID --eval FILE` adds it as a lesson only when a paired eval on the same tasks,
naming that proposal, shows more passes at no more than +15% tokens. `--trend` refuses a release that scores
more than one question below the best earlier release (the measured single-run noise), per tool and on the
same questions, or that costs more than +15% tokens against the cheapest such release (`eval/results/history.jsonl`). Why this strict: skills agents wrote
for themselves scored 8-11.5 points below no skills at all (SkillsBench), while curated ones gained 16.6, and
lessons from a false signal made Reflexion worse.

**Bounded on disk.** Every store the harness writes has a cap and is pruned as it is written: candidates
500, the install ledger 1000, proposals 100, turn markers 200, backups the newest 3 plus every backup
`installed.json` still needs and every uninstall stash (if `installed.json` cannot be read, none is pruned).
Memory and lessons keep their own caps (500, 200). `harness doctor` holds the whole folder to its footprint
cap (20 MB; 1 GB with the optional plugins) and names the largest parts when over it. Claude Code's own
transcripts are reported, not touched.

## Past conversations: session_search

"What did we decide about the partition key last week?" is often answered nowhere but in an earlier
conversation. `session_search(query)` searches the user's own local transcripts, in the tools' own
files (Claude Code `~/.claude/projects/*/*.jsonl`, Codex `~/.codex/sessions/**/rollout-*.jsonl`), and
returns a few dated excerpts with the session id and project; `session=<id>` narrows to one session.

- Only what was said is indexed: the user's messages and the assistant's replies, not tool output,
  thinking, or the rules the tool injects. Secrets are masked before anything is stored.
- Only files the user owns are read, and symlinks are not followed. Nothing is sent anywhere.
- The index (in `~/.agent-harness/index.sqlite`) grows by reading only what was appended since the last
  search, newest sessions first, a few seconds per call at most; it is capped at 64 MB of text
  (`HARNESS_SESSIONS_MAX_BYTES`), dropping the oldest sessions first. A transcript deleted by its tool
  leaves the index too. The session asking is never its own result.

## Skills learned from experience: skill_manage

When a task took an agent several tool calls to find the working method (a data query, a command
sequence, a fix), or the user corrected how to do it, the agent saves the method with `skill_manage create` as
`~/.agents/skills/learned/<name>/SKILL.md` (the agentskills.io format: `name`, `description`, then
`## When to Use`, `## Procedure`, `## Pitfalls`, `## Verification`). The next session, in any tool,
starts from the working path.

- Codex, Gemini CLI, Cursor and Copilot list learned skills natively. Claude Code does not read
  `~/.agents/skills`, and a listing costs every request, so for Claude the per-prompt recall names a
  learned skill only when the prompt is about it.
- A near-duplicate of any installed or learned skill is refused with the name to update instead;
  `update` takes a new body or replaces one exact passage (`old` -> `new`) and bumps the version.
- Limits: a body over 12,000 characters is refused (warning over 6,000); at most 40 learned skills are
  active, and beyond that the least used (views and updates through `skill_manage`; a tool reading the
  file natively is not counted) move to `~/.agent-harness/skills-archive/`, never deleted. Secrets are
  masked before writing.

## Task state: surviving compaction

Long tasks outgrow the context window. When a tool compacts or resets the conversation, the
agent loses its working notes unless they were written down. The rules make the agent call
`state_save` for unfinished multi-step work with a short (at most 4 KB) record: goal, stage,
decisions, what is next, key files. After a compaction it calls `state_load`. Claude Code injects
the saved state at session start, resume and compaction through a hook, and only when a state file
exists (nothing, and no tokens, otherwise); other tools follow the rule. One-shot questions need
neither. State is only for work in progress: lasting facts go to memory and mistakes go to lessons.

## The harness: hooks and checks

The rules ask for good behaviour; the harness enforces a floor where the tool allows it.

- **Guard** (`content/hooks/guard.py`): runs before every shell command in Claude Code and blocks
  catastrophic ones: recursive deletion of the home folder, `/` or a path built from a variable
  that might be empty; disk wipes; fork bombs; `chmod -R 777 /`; downloads piped into a shell from
  unknown hosts, or an `echo`ed or base64-decoded script piped into a shell; `rsync --delete` into or `mv` of
  the home folder; force-pushes to `main`/`master`; reading SSH keys or credential files; `sudo`.
  `tests/test_guard_cases.py` holds a wider corpus and names the gaps that remain.
  It uses only the Python standard library, adds about 40 ms per command, and is also a plain checker:
  `guard.py --check "<command>"`. It is a seat belt, not a sandbox.
- **Finish check** (Claude Code): when the agent stops after editing project files, it is reminded once
  to run the project's checks and report the result. Writes to its own memory files do not count.
- **Off by default, for evaluation**: the `run_checks` MCP tool (finds the project's tests, returns only
  the failures, records the result), a finish gate on that record, and a check guard that asks before an
  existing test loses an assertion or gains a skip. On 36 coding runs (v0.1.1 eval) no arm ever claimed a
  false "done" or weakened a test, so neither could show the gain the harness's keep rule asks for.
  `HARNESS_ENABLE=run_checks,check_guard` turns them on (the check guard is installed, inert).
- **Arms for evaluation, off by default**: `memory_snapshot` puts the saved facts (most used first,
  2,200 characters, as Hermes Agent's MEMORY.md) into the MCP server's instructions once per session,
  instead of recalling only what is relevant; `skill_nudge` reminds the agent once, after a turn of 10 or
  more tool calls with no skill saved, to save what it worked out. `HARNESS_ENABLE=memory_snapshot,skill_nudge`
  turns them on. In the v0.2 eval neither showed a gain: the snapshot answered 10/10 like relevance-only
  recall at the same tokens, and the nudge made Claude save skills but its repeat runs were 2/3 correct at
  1.42x tokens. `session_search` and `skill_manage` ship on: past-conversation recall went from 1/6 to 6/6,
  and Codex saved and reused a skill in 3/3 repeated procedures; Claude saved none unprompted.
- **The work graph, on by default (v0.3.3)**: the `plan` MCP tool drives `src/agent_harness/workgraph`, a
  graph of gated nodes merged in from agentic-os (181 of 203 real nodes gated there, and adversarially
  certified). Its evidence is use and an attack, not a gain measured against plain; the owner turned it on by
  default in v0.3.3 because it is how the goal and each step's check survive context loss (`HARNESS_DISABLE=plan`
  turns it off). Claude desktop and Jupyter AI, which have no shell of their own, never get it or `run_checks`:
  their server runs with `--no-shell`. Its definition costs ~340 bytes per request, inside the context budget. The engine's protections
  (gates judged by the guard, tamper detection, irreversible nodes held) do not depend on the switch.
  `plan run` gives each worker a tier from `content/routing.toml`: a node's own `tier:`, else `standard`, and
  `deep` once its gate has failed. The tier's model and effort are passed explicitly (`--model`/`--effort`
  for Claude Code, `-m`/`model_reasoning_effort` for Codex), read from the `harness-<tier>` agent files.
- **Memory recall** (Claude Code): relevant memories are attached to each prompt, within a strict
  time limit, so the agent does not need to search for them.

## Skills and prompts

Skills (`content/skills/<name>/SKILL.md`) follow progressive disclosure: only each skill's name
and one-line description sit in the agent's context; the body is read when a task matches. They
are installed once in `~/.agents/skills/` (read by Codex, Gemini CLI, Cursor and Copilot) and
linked into `~/.claude/skills/` for Claude Code. Prompts (`content/prompts/*.md`) are reusable
task starters (plan, review, explain this repo, fix a failing test), installed as slash commands
or prompt files where the tool supports them.

## Profiles

A profile is a folder outside this repository that describes one deployment, such as a shared
research server: `profile.toml` (name, knowledge paths, optional plugins), extra rules, and a
descriptor such as `SERVER.md` with the machine's facts. The descriptor is indexed like any
other document, so agents fetch just the section they need. See `profiles/example/`.

## Optional plugins (warm-up)

`harness warmup` builds the knowledge index and, if you agree, installs optional MCP plugins
listed in the profile: a web `fetch` tool (about 60 MB) and a `playwright` browser (about 400 MB).
Sizes are shown before anything is downloaded; the total stays under 1 GB.

## Supported tools

Each tool is set up in its own app and, where it runs inside VS Code, there too. It works the same on a
laptop and on a remote Linux server reached over SSH: install the harness on the machine where the tool runs.

| tool | standalone | inside VS Code |
|---|---|---|
| Claude Code | terminal `claude`: rules, memory tools, skills, slash prompts, guard hook | the extension reads the same user settings |
| Codex | terminal `codex`: `AGENTS.md`, memory tools, skills | the IDE extension shares the same `~/.codex` configuration |
| GitHub Copilot | (VS Code only) | instructions, prompt files and memory tools in your VS Code user settings |
| Cursor | the Cursor app: rules and memory tools | Cursor is itself a VS Code build, so this is the same install |
| Gemini CLI | terminal `gemini`: `GEMINI.md`, memory tools, skills | Gemini Code Assist's agent mode reads the same `~/.gemini` settings |
| Claude desktop app | memory and knowledge tools | not applicable |
| JupyterLab (Jupyter AI) | memory tools; its agents (Claude Code, Codex, Gemini CLI) read their own rules | not applicable |
| anything else that reads `AGENTS.md` | the shared rules file | the same |

Skills are installed once in `~/.agents/skills/`, which Codex, Gemini CLI, Cursor and Copilot read; Claude
Code gets a link to the same folder. Cursor reads `.cursor/mcp.json` in every open folder as well as in your
home, so a workspace that opens your home folder itself loads the memory tools twice; open a project
folder instead.

## Privacy

Everything stays on your machine. Memory, lessons, task state and the knowledge index are plain files in
`~/.agent-harness` (override with `HARNESS_HOME`). Nothing is uploaded by the harness and there is no
telemetry. Your AI tool still sends your prompts and the context it reads to its own provider, as before;
the harness reduces how much it reads, but not where it goes. The rules tell agents never to store secrets
in memory, and the guard blocks reading SSH keys, cloud credentials and `.env` files through the shell.
Optional plugins are installed only by `harness warmup`, which shows their size first.

## The guard against a wider corpus, and a held-out set

**The corpus (in-sample).** The guard's tests carry a representative set of dangerous and safe commands taken
from the author's private shell-hook test suite (352 cases extracted, paths made neutral):
[tests/test_guard_cases.py](../tests/test_guard_cases.py). A one-off comparison against that suite found 105
disagreements. It counted 24 of them as gaps, now fixed; the repository pins 21 of those as commands
(`FORMER_BYPASSES`: `rm${IFS}-rf${IFS}/`, `rm -rf ${HOME:?}`, `bash -lc "rm -rf ~"`, `echo 'rm -rf ~' | bash`,
`rsync --delete` into home, `mv ~/Documents` away, `dd of=/etc/hosts`, ...) and the non-string-command fix as
`TestPayloads`; the rest of the 24 cannot be listed from here. The other 81 were recorded, not fixed:

| remaining disagreements | cases | verdict |
|---|---|---|
| the author's own machine policy: network and VPN control, macOS system tools, the author's own agent settings | 39 | out of scope for a catastrophic-command guard |
| the harness is stricter (all `sudo`, SSH key flags, `find ..`, text that looks like a fork bomb) | 14 | by design; the fork-bomb text in a quoted note is a known false positive |
| open gaps: targets computed by `$(...)`, inline code in another interpreter, login-script and cron persistence, `find \| xargs` | 18 | 16 representative commands pinned as expected failures in the tests; all closed in v0.3 |
| the private suite asks about single files and project paths the harness allows (`rm ~/Desktop/note.txt`) | 9 | by design |
| an empty `{}` payload | 1 | cannot be told from a non-shell tool |

The counts come from that one-off comparison (the private test file is not in this repository). A review on
2026-10-03 then found that a glob after a `cd` into home or root (`cd ~ && rm -rf *`, `pushd ~ && ...`,
`cd / && ...`), `command -p rm -rf ~` and `xargs rm -rf <<< ~` got through. The guard now resolves a relative
operand against a `cd`/`pushd` earlier on the same line, skips the flags of `command`/`exec`/`builtin`, and
reads a here-string into `xargs` as operands. A second review found that process substitution feeding a shell or
`source` (`bash <(echo ...)`) and `git push --mirror` got through; the first is now checked like a pipe into a
shell, the second is blocked outright. Those 19 commands joined the corpus, which is now 105 dangerous
commands (all blocked) and 108 safe ones (all allowed). Because the guard was fixed against it, the corpus
measures fit, not generalisation.

**The held-out set.** [tests/test_guard_heldout.py](../tests/test_guard_heldout.py) holds 45 dangerous commands
and 20 safe controls written on 2026-10-03 by the same AI build session that fixed the guard, after it had
read the first review and before the cd fix (two entries were replaced after it); none is taken from the corpus
(a test checks that). Result: **39 of 45 dangerous commands blocked (87%), 20 of 20 safe controls allowed**,
with the guard as it was before the set existed. The commit that added the set also fixed one of its commands,
`cd /home && rm -rf *`, so the set is no longer held out for that one; counting it, 40 of 45 (89%).
The 5 remaining misses (`rsync --delete` into a destination spelled with `$HOME`, `echo ~ | xargs rm -rf`,
`truncate` of a system file, inline Python and Perl) were closed in v0.3 with the set in view, so the held-out
figure stays 39 of 45. Run `python3 tests/test_guard_heldout.py --rate` to reproduce both rates.

**v0.3: the whole corpus, and what changed in scope.** The comparison above used cases extracted from the
author's bash guard. In v0.3 that suite's 426 cases were exported whole, usernames replaced, as
[tests/guard_corpus_agentic_os.py](../tests/guard_corpus_agentic_os.py), and scored as a held-out set before any
change: 150 of 231 dangerous commands blocked. After the merge: 221 of 221, and 183 of 186 on this repo's
corpus. Three v0.2 scoping decisions above were deliberately reversed, each for a reason that is not the
author's personal policy:

- **The guard protects itself.** Writes onto `~/.claude/hooks`, `~/.claude/settings.json` and the harness's own
  hook are blocked (adding execute, `chmod +x`, is not). v0.2 counted "the author's own agent settings" out of
  scope, but an agent that can overwrite the guard has turned it off, whoever's machine it is.
- **Taking the network down** (an interface, a VPN daemon, Wi-Fi, `launchctl bootout`) is blocked: it strands a
  remote session the agent may itself be running over. v0.2 had it as the author's machine policy.
- **Stricter by design, relaxed:** `scp`/`sftp`/`rsync -i <key>` uses a key the way `ssh -i` (always allowed)
  does; copying the key as an operand is still blocked. `find ..` and a top-level home folder with a name filter
  (`-name '*.pyc'`) is clean-up. Recursive `chmod`/`chown` on a top-level home folder is reversible. A fork bomb
  written into a note as quoted text is data. `sudo` stays blocked outright.

The cases that remain the author's personal policy (anything two levels under home, a package-manager tree,
read-only Wi-Fi diagnostics, a `sudo` rule that judges what runs) are listed with reasons in
[tests/test_guard_cross.py](../tests/test_guard_cross.py) rather than forced into this guard.

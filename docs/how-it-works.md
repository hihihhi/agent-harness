# How agent-harness works

agent-harness has four parts: one set of **rules** loaded by every tool, a small local **MCP
server** that gives agents knowledge, memory and lessons, a few **skills and prompts**, and a
**harness** of hooks and checks around the agent. The installer wires all four into each AI tool
you use, in that tool's own format.

## Rules: one source of truth

`content/AGENTS.md` is the only rulebook. The installer copies it to where each tool expects
its instructions (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md`, VS Code
instruction files, Cursor rules, a project `AGENTS.md`). The rules are loaded into every session,
so they cost tokens every time; they are kept under 120 lines, and anything longer lives in a
skill or in the knowledge base, where it is loaded only when needed.

At install time the installer appends three things to the rules: your profile's extra rules, a
short **Knowledge index** (section ids and titles of your documents), and a **What you
remember** digest of the most useful memories.

## Partial retrieval: never read a whole document

Most wasted tokens come from an agent reading an entire manual to find one fact. The knowledge
tools split every Markdown or text file into sections at its headings and index them (SQLite
full-text search with BM25 ranking, with a pure-Python fallback).

1. The agent looks at the Knowledge index already in its rules and picks the matching section id.
2. It calls `kb_get(id)` and receives just that section, capped in size.
3. Only when nothing in the index fits does it call `kb_search(query)`, which returns section
   ids, titles, a one-line snippet and each section's size in bytes, so the agent can choose
   what is worth fetching.

`kb_toc(path)` returns a document's heading tree without any bodies. The index covers your
profile's `kb_paths`, the harness content, and the current project's `README.md`, `AGENTS.md`
and `docs/`. It rebuilds itself when files change.

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

## Task state: surviving compaction

Long tasks outgrow the context window. When a tool compacts or resets the conversation, the
agent loses its working notes unless they were written down. The rules make the agent call
`state_save` at each milestone with a short (at most 4 KB) record: goal, decisions and why, what
is done with its evidence, what is next, open questions, key files. After a compaction or in a
new session it calls `state_load` first. Claude Code re-injects the saved state automatically
after compaction through a hook; other tools follow the same rule from the rulebook. State is
only for work in progress: lasting facts go to memory and mistakes go to lessons.

## The harness: hooks and checks

The rules ask for good behaviour; the harness enforces a floor where the tool allows it.

- **Guard** (`content/hooks/guard.py`): runs before every shell command in Claude Code and blocks
  catastrophic ones: recursive deletion of the home folder, `/` or a path built from a variable
  that might be empty; disk wipes; fork bombs; `chmod -R 777 /`; downloads piped into a shell from
  unknown hosts; force-pushes to `main`/`master`; reading SSH keys or credential files; `sudo`.
  It uses only the Python standard library, adds about 40 ms per command, and is also a plain checker:
  `guard.py --check "<command>"`. It is a seat belt, not a sandbox.
- **Finish check** (Claude Code): when the agent stops after editing files, it is reminded once to
  run the project's checks and report the result, and to save its state if unfinished.
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

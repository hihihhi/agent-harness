# agent-harness

**One install that makes the AI coding assistants you already use work better.**

AI coding tools such as Claude Code, Codex, GitHub Copilot, Cursor and Gemini CLI are only as
good as the instructions and context they are given. Out of the box they forget everything
between sessions, re-read whole files, stop to ask about trivial things, claim work is done
without checking it, and repeat the same mistakes. agent-harness gives every one of them the
same set of working rules, a shared memory, a searchable knowledge base, a safety guard and a
few proven skills, so you get:

- **fewer tokens:** agents fetch only the section of a document they need, not the whole thing;
- **higher quality and accuracy:** agents define "done" as a check, run it, and show the evidence;
- **less waiting and fewer interruptions:** agents ask only about real decisions;
- **memory that lasts:** preferences, facts and work in progress survive new sessions and
  context compaction, and are shared across tools on the same machine;
- **self-learning:** after a real failure or a correction, the agent writes a short lesson and
  looks it up the next time a similar task comes along;
- **a safety net:** a guard blocks catastrophic commands (deleting your home folder, wiping
  disks, `sudo`, piping unknown scripts into a shell, force-pushing to `main`, reading SSH keys).

You need Python 3.9 or newer (already on macOS and most Linux machines). Nothing else is
required: the harness uses only Python's standard library, and takes under 20 MB.

## Install

```sh
git clone https://github.com/hihihhi/agent-harness.git && python3 agent-harness/bin/harness install
```

The installer finds the AI tools on your machine, shows exactly which files it would change for
each one, and asks before touching anything. Every file it changes is backed up first.
Then restart your AI tool and run `python3 agent-harness/bin/harness doctor` to check it worked.

Useful options:

| command | what it does |
|---|---|
| `harness install --dry-run` | show the plan, change nothing |
| `harness install --tools claude-code,codex` | only these tools |
| `harness install --project .` | set up only the current project, not your whole user account |
| `harness install --profile DIR` | add facts about your machine or team (see below) |
| `harness warmup` | build the search index and add optional plugins (web fetch, browser) |
| `harness status` / `harness doctor` | what is installed, and is it healthy |
| `harness learn` | show the lessons your agents have learned |
| `harness update` | refresh the rules and skills; memory and lessons are kept |
| `harness uninstall` | put everything back as it was |

(`harness` is `python3 agent-harness/bin/harness`; add `agent-harness/bin` to your `PATH` to
type less.)

## Supported tools

Each tool is set up in its own app and, where the tool runs inside VS Code, there too. It
works the same on a laptop and on a remote Linux server reached over SSH or VS Code
Remote-SSH: install the harness on the machine where the tool runs.

| tool | standalone | inside VS Code |
|---|---|---|
| Claude Code | terminal `claude`: rules, memory tools, skills, slash prompts, guard hook | the Claude Code extension reads the same user settings, so it is covered by the same install |
| Codex | terminal `codex`: `AGENTS.md`, memory tools, skills | the Codex IDE extension shares the same `~/.codex` configuration |
| GitHub Copilot | (VS Code only) | instructions, prompt files and memory tools in your VS Code user settings; on Remote-SSH, in the server's machine settings |
| Cursor | the Cursor app: rules and memory tools | Cursor is itself a VS Code build, so this is the same install |
| Gemini CLI | terminal `gemini`: `GEMINI.md`, memory tools, skills | Gemini Code Assist's agent mode reads the same `~/.gemini` settings |
| Claude desktop app | memory and knowledge tools | not applicable |
| JupyterLab (Jupyter AI) | rules for notebook assistants | not applicable |
| anything else that reads `AGENTS.md` | the shared rules file | the same |

Skills are installed once in `~/.agents/skills/`, which Codex, Gemini CLI, Cursor and Copilot
read. Claude Code gets a link to the same folder at `~/.claude/skills/`, so every tool uses
one copy.

## What you get

- **Working rules** (`content/AGENTS.md`): one short rulebook loaded by every tool at the start
  of every session: plan small and verify, ask only for real decisions, save lessons, stay
  within token budgets, stay safe.
- **Memory and knowledge tools** (a small local MCP server named `harness`): `kb_search` /
  `kb_get` for partial retrieval from your documents, `mem_add` / `mem_search` for facts and
  preferences, `lesson_add` / `lesson_search` for self-learning, and task state that survives
  compaction.
  A wrapper that installs the harness for an organisation can leave one-line notices in
  `~/.agent-harness/notices.json` (`{"notices": ["..."]}`, local, never fetched): the first one
  joins the server's instructions in the first session of the day, so the assistant mentions it once.
- **Skills** (`content/skills/`): debug systematically, verify before claiming done, trial-run
  data jobs small before large, write tests first. They load only when relevant.
- **Prompts** (`content/prompts/`): plan, review, explain this repo, fix a failing test.
- **Guard** (`content/hooks/guard.py`): blocks catastrophic commands before they run. You can
  also use it by hand: `python3 content/hooks/guard.py --check "rm -rf ~"`.
- **Profiles** (optional): a folder describing your machine or team, for example a shared GPU
  server: where data lives, what the rules are. See `profiles/example/`. Keep your real
  profile outside this repository.

How it works, in more depth: [docs/how-it-works.md](docs/how-it-works.md).

## Undo

```sh
python3 agent-harness/bin/harness uninstall
```

This restores every file the installer changed from its backup and removes what it added. Your
memory and lessons stay in `~/.agent-harness` in case you reinstall; delete that folder
yourself if you want them gone too.

## Privacy

- Everything stays on your machine. Memory, lessons, task state and the knowledge index are
  plain files in `~/.agent-harness` (override with the `HARNESS_HOME` environment variable).
  Nothing is uploaded anywhere by the harness, and there is no telemetry.
- Your AI tool still sends your prompts and the context it reads to its own provider, as it did
  before. The harness reduces how much it reads, but does not change where it goes.
- The rules tell agents never to store secrets in memory, and the guard blocks reading SSH
  keys, cloud credentials and `.env` files through the shell.
- Optional plugins (web fetch, browser) are only installed by `harness warmup`, which shows
  their size first. Web pages are treated as data, never as instructions.

## License

MIT. See [LICENSE](LICENSE).

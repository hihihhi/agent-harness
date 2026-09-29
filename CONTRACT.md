# agent-harness: build contract (read before writing code)

One install gives every mainstream AI coding tool the same capabilities: project and server context,
partial-retrieval knowledge, memory, self-learning, browsing, and a harness of checks. Target user knows
nothing; target machine may be a laptop or a shared Linux server reached over SSH / VS Code Remote-SSH.

Hard rules for all code here:
- Python >= 3.9, **standard library only** for everything installed by default (no pip needed to run).
- Installed footprint before warm-up < 20 MB. After warm-up (optional plugins) < 1 GB, measured.
- Never print, log or store secrets. No hostnames, emails, IPs or usernames of any real deployment in
  this repo: it is PUBLIC. Deployment specifics live in a *profile* outside the repo.
- Every module has tests in `tests/` runnable with `python3 -m pytest -q` AND with plain
  `python3 -m unittest` (use unittest-style tests or plain asserts in functions pytest collects; keep
  both working: prefer `unittest.TestCase`).
- Tests never touch the real `$HOME`: use a temp dir and pass it as `home`.

## Layout and owners

```
bin/harness                              thin launcher -> agent_harness.cli:main          (A2)
src/agent_harness/cli.py                 install | uninstall | status | doctor | warmup | update | learn  (A2)
src/agent_harness/installer.py           plan/apply/backup/restore, ask + warn            (A2)
src/agent_harness/adapters/base.py       Adapter interface (below)                        (A2)
src/agent_harness/adapters/claude_code.py, codex.py                                       (A2)
src/agent_harness/adapters/copilot_vscode.py, cursor.py, gemini.py, agents_md.py          (A3)
src/agent_harness/mcp/server.py          stdio MCP server (JSON-RPC 2.0, newline-delimited) (A1)
src/agent_harness/mcp/kb.py              knowledge index + partial retrieval               (A1)
src/agent_harness/mcp/memory.py          memory + lessons (self-learning)                  (A1)
content/AGENTS.md                        the one source of truth for agent behaviour       (A4)
content/skills/<name>/SKILL.md           a few compact skills (progressive disclosure)     (A4)
content/hooks/guard.py                   dangerous-command guard (PreToolUse-style)         (A4)
content/hooks/check_guard.py             asks before a test/gate is weakened (PreToolUse on edits)
src/agent_harness/mcp/checks.py          run_checks + the record the Stop gate reads
content/prompts/*.md                     reusable prompts / slash commands                  (A4)
profiles/example/profile.toml            example profile (public, generic)                  (A4)
README.md, docs/                         user-facing, zero prior knowledge assumed          (A4)
scripts/secret-scan.sh                   leak scan incl. a planted-key control              (main)
```

## Shared names

- `HARNESS_HOME` = `~/.agent-harness` (override with env `HARNESS_HOME`). Holds: `content/` (installed
  copy of this repo's content), `index.sqlite`, `memory/`, `lessons/`, `backup/<timestamp>/`,
  `profile/` (the active profile), `installed.json` (what was installed where, for uninstall).
- MCP server command every adapter registers:
  `["python3", "<HARNESS_HOME>/lib/agent_harness/mcp/server.py"]` (the installer copies `src/agent_harness`
  to `<HARNESS_HOME>/lib/agent_harness`). Server name: `harness`.
- Knowledge paths: env `HARNESS_KB_PATHS` (os.pathsep-separated dirs/files of Markdown/text), default =
  the profile folder's files + profile `kb_paths` + the current git project's `docs/`, `README.md`,
  `AGENTS.md` (only inside a git project other than $HOME; the harness's own content is not indexed:
  every tool loads it natively). The rules' Knowledge index uses the same profile roots, so its ids resolve.

## MCP tools (A1). Names, arguments, and the rule behind them

Partial retrieval: an agent NEVER needs a whole document. It searches, gets section ids + one-line
snippets, then fetches only the sections it needs. Every result carries its byte size so the agent can
budget.

| tool | args | returns |
|---|---|---|
| `kb_search` | `query: str, k: int=5` | list of `{id, title, path, snippet (<=200 chars), bytes}` (BM25 via SQLite FTS5; pure-Python BM25 fallback) |
| `kb_get` | `id: str, max_bytes: int=8000` | the section text (heading-delimited), truncated with a note; a page name alone (or its first section) also lists the page's section ids |
| `kb_toc` | `path: str=""` | headings tree (ids + titles), no bodies |
| `mem_add` | `text: str, tags: list[str]=[], scope: "user"|"project"="user"` | id; dedupes near-identical facts |
| `mem_search` | `query: str, k: int=5` | `{id, text, tags, uses}` |
| `mem_forget` | `id: str` | ok |
| `lesson_add` | `mistake: str, fix: str, trigger: str` | id. Self-learning: recorded after a failure or correction |
| `lesson_search` | `query: str, k: int=3` | the lessons relevant to the task at hand |
| `run_checks` | `cmd: str="", timeout: int=300` | the project's checks run: exit code + only the failures and summary (<= 2 KB); recorded for the Stop gate |

Caps (anti-bloat): memory 500 items/scope, lessons 200; beyond that the least-used, oldest are archived
(not deleted). Items are one small JSON/Markdown file each + the SQLite index; human-readable.
The index rebuilds incrementally (mtime) on each `kb_search` if files changed; `harness warmup` builds it.

## Adapter interface (A2 defines in base.py; A3 implements against it)

```python
@dataclass
class Ctx:
    home: Path            # user's HOME (temp in tests)
    harness_home: Path    # <home>/.agent-harness
    content: Path         # installed content dir
    profile: dict         # parsed profile.toml ({} if none)
    rules: str            # final AGENTS.md text (content/AGENTS.md + profile extra rules + descriptor pointer)
    mcp_cmd: list[str]    # the MCP server command above
    scope: str            # "user" (default) or "project" (then `project: Path`)
    project: Path | None

@dataclass
class FileChange:
    path: Path            # absolute
    kind: str             # "replace" (instruction files) | "merge-json" | "merge-toml" | "copy-dir"
    content: str | dict | Path
    note: str             # one line for the plan shown to the user

class Adapter:
    name: str             # "claude-code", "codex", "copilot-vscode", "cursor", "gemini", "agents-md"
    title: str            # human name
    def detect(self, ctx) -> bool: ...          # is the tool installed / used on this machine?
    def plan(self, ctx) -> list[FileChange]: ... # everything it would write, nothing written
    def post_install(self, ctx) -> list[str]: ...# optional commands to run (e.g. `claude mcp add ...`), returned not run
```
The installer (A2) owns writing: it shows the plan, warns "this replaces your current <tool> setup; a
backup is kept and `harness uninstall` restores it", asks per tool (`--yes` for non-interactive; without
a TTY and without `--yes` it refuses), backs up every touched path, applies, records `installed.json`.
JSON/TOML merges keep the user's other keys; instruction files are replaced (backup kept).

## Tool facts to follow (verify against docs/ai-harness/provider-specs.md when it lands)

- Claude Code: `~/.claude/CLAUDE.md` (user), `@path` imports; hooks + permissions in
  `~/.claude/settings.json`; MCP user scope via `claude mcp add -s user harness -- <cmd>` or `~/.claude.json`
  `mcpServers`; skills in `~/.claude/skills/<name>/SKILL.md`; commands `~/.claude/commands/*.md`.
- Codex: `~/.codex/AGENTS.md` (global), project `AGENTS.md`; `~/.codex/config.toml` `[mcp_servers.harness]`
  `command`/`args`; prompts `~/.codex/prompts/*.md`.
- Copilot (VS Code): user settings.json (`github.copilot.chat.codeGeneration.useInstructionFiles`,
  `chat.instructionsFilesLocations`, `chat.promptFilesLocations`, `chat.mcp...`), user-level `mcp.json` in
  the VS Code user dir (`servers` key, `type: "stdio"`); workspace `.github/copilot-instructions.md`;
  `*.instructions.md` with `applyTo`; `*.prompt.md`; AGENTS.md support setting. VS Code user dir:
  macOS `~/Library/Application Support/Code/User`, Linux `~/.config/Code/User`, Remote-SSH server side
  `~/.vscode-server/data/Machine/` (machine settings).
- Cursor: `~/.cursor/mcp.json` (`mcpServers`), rules `.cursor/rules/*.mdc`, AGENTS.md.
- Gemini CLI: `~/.gemini/GEMINI.md`, `~/.gemini/settings.json` (`mcpServers`, `context.fileName` may list
  `AGENTS.md`).
- Generic: `AGENTS.md` for any tool that reads it (Codex, Cursor, Copilot, Gemini via setting, OpenCode, ...).

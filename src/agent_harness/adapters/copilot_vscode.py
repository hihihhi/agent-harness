"""GitHub Copilot in VS Code (local, and the server side of Remote-SSH).

Conventions verified 2026-09-30 against:
- https://code.visualstudio.com/docs/copilot/customization/custom-instructions
- https://code.visualstudio.com/docs/agent-customization/custom-instructions
- https://code.visualstudio.com/docs/copilot/customization/prompt-files
- https://code.visualstudio.com/docs/copilot/reference/copilot-settings
- https://code.visualstudio.com/docs/copilot/customization/mcp-servers
- https://code.visualstudio.com/docs/agents/reference/mcp-configuration

What the docs say, and what this adapter does with it:
- User ``mcp.json`` lives in the VS Code user profile folder, top-level ``servers``, stdio entries
  need ``"type": "stdio"`` + ``command`` (+ ``args``). The portable user file
  ``~/.copilot/mcp-config.json`` uses ``mcpServers``; Agent Host and Copilot CLI read it directly.
  Both are written.
- VS Code has several harnesses (Local agent; Agent Host: Copilot, Claude, Codex) and what loads
  depends on the one picked. Agent Host reads user instructions from ``~/.copilot/instructions``;
  the Local agent reads the same folder through ``chat.instructionsFilesLocations`` (deprecated,
  Local-agent only, tilde paths accepted, default includes ``~/.copilot/instructions``). So there is
  ONE rules file, ``~/.copilot/instructions/harness.instructions.md`` (``applyTo: "**"``), and the
  setting restates that folder because a user value replaces the default object.
- Prompt files (``*.prompt.md``) load only in the Local agent (deprecated for Agent Host). They are
  copied to ``HARNESS_HOME/content/copilot/prompts`` and pointed at by ``chat.promptFilesLocations``.
- Toggles: ``github.copilot.chat.codeGeneration.useInstructionFiles``, ``chat.useAgentsMdFile``,
  ``chat.includeApplyingInstructions`` (all default true; set explicitly so a user who turned them
  off gets them back). ``chat.promptFiles`` is no longer documented; it is set for older VS Code.
- Remote-SSH: machine settings are ``~/.vscode-server/data/Machine/settings.json``; the remote user
  ``mcp.json`` is assumed to sit beside it (``MCP: Open Remote User Configuration``). Not verified
  from a doc page; ``~/.copilot/mcp-config.json`` covers the remote side regardless.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .base import Adapter, Ctx, FileChange, list_md, mcp_servers, which

APPLY_ALL = '---\nname: harness\ndescription: agent-harness rules, always on\napplyTo: "**"\n---\n\n'


# ------------------------------------------------------------------ JSON / JSONC helpers
# Shared by the other A3 adapters (cursor, gemini, claude_desktop, jupyter_ai).

def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments (outside strings) and trailing commas before } or ]."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise ValueError("unterminated /* comment")
            i = j + 2
        else:
            out.append(c)
            i += 1
    if in_str:
        raise ValueError("unterminated string")
    # Trailing commas: only reachable outside strings once strings are masked.
    s = "".join(out)
    masked = re.sub(r'"(?:\\.|[^"\\])*"', lambda m: "\0" * len(m.group(0)), s)
    drop = {m.start() for m in re.finditer(r",(?=\s*[}\]])", masked)}
    return "".join(ch for k, ch in enumerate(s) if k not in drop)


def load_json_file(path: Path) -> Tuple[str, Optional[dict]]:
    """("absent"|"json"|"jsonc"|"bad", data). "bad" means: do not touch this file."""
    if not path.exists():
        return "absent", None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "bad", None
    if not text.strip():
        return "json", {}
    try:
        data = json.loads(text)
        kind = "json"
    except ValueError:
        try:
            data = json.loads(strip_jsonc(text))
            kind = "jsonc"
        except ValueError:
            return "bad", None
    if not isinstance(data, dict):
        return "bad", None
    return kind, data


def json_change(path: Path, build: Callable[[dict], dict], note: str,
                refused: List[str]) -> Optional[FileChange]:
    """Plan a JSON merge that keeps the user's keys.

    ``build(existing)`` returns the top-level keys to set, already merged with what the user has
    under them, so the result is right whether the installer merges shallow or deep.
    Plain JSON -> merge-json. JSONC (comments / trailing commas) -> replace with the fully merged
    document (comments are lost; the installer's backup keeps the original). Unparseable -> no
    change; a line is appended to ``refused``.
    """
    kind, data = load_json_file(path)
    if kind == "bad":
        refused.append(f"refused {path}: not valid JSON/JSONC (or not an object); left untouched. "
                       "Fix or move it, then run install again.")
        return None
    updates = build(data or {})
    if kind == "jsonc":
        full = dict(data)
        full.update(updates)
        return FileChange(path, "replace", json.dumps(full, indent=2) + "\n",
                          note + " (file had comments: merged, comments dropped, backup kept)")
    return FileChange(path, "merge-json", updates, note)


def merged_map(existing: dict, key: str, ours: dict) -> dict:
    """existing[key] (if a dict) with ours laid over it, one level deep."""
    cur = existing.get(key)
    out = dict(cur) if isinstance(cur, dict) else {}
    out.update(ours)
    return out


def stdio_entry(cmd: List[str], with_type: bool = True) -> dict:
    e = {"type": "stdio"} if with_type else {}
    e.update({"command": cmd[0], "args": list(cmd[1:])})
    return e


def home_path(ctx: Ctx, p: Path) -> str:
    """'~/x' when p is under the user's home (the form VS Code documents), else absolute."""
    try:
        return "~/" + p.relative_to(ctx.home).as_posix()
    except ValueError:
        return p.as_posix()


def platform() -> str:
    return sys.platform


# ------------------------------------------------------------------ adapter

def vscode_user_dirs(ctx: Ctx) -> List[Path]:
    """The client-side VS Code user dir for this platform."""
    plat = platform()
    if plat == "darwin":
        return [ctx.home / "Library" / "Application Support" / "Code" / "User"]
    if plat.startswith("win"):
        return [ctx.home / "AppData" / "Roaming" / "Code" / "User"]
    return [ctx.home / ".config" / "Code" / "User"]


def vscode_machine_dir(ctx: Ctx) -> Optional[Path]:
    server = ctx.home / ".vscode-server"
    return server / "data" / "Machine" if server.is_dir() else None


class CopilotVSCodeAdapter(Adapter):
    name = "copilot-vscode"
    title = "GitHub Copilot (VS Code)"

    def detect(self, ctx: Ctx) -> bool:
        if which("code") or vscode_machine_dir(ctx) is not None:
            return True
        return any(d.parent.is_dir() for d in vscode_user_dirs(ctx))

    def _prompt_dir(self, ctx: Ctx) -> Path:
        return ctx.harness_home / "content" / "copilot" / "prompts"

    def _prompts(self, ctx: Ctx) -> List[Tuple[str, str]]:
        return [(p.stem + ".prompt.md", p.read_text(encoding="utf-8"))
                for p in list_md(ctx.content / "prompts")]

    def _settings(self, ctx: Ctx):
        instr = home_path(ctx, ctx.home / ".copilot" / "instructions")
        prompts = home_path(ctx, self._prompt_dir(ctx))

        def build(existing: dict) -> dict:
            return {
                "github.copilot.chat.codeGeneration.useInstructionFiles": True,
                "chat.useAgentsMdFile": True,
                "chat.includeApplyingInstructions": True,
                "chat.promptFiles": True,
                # A user value replaces the default object, so the defaults we rely on are restated.
                "chat.instructionsFilesLocations": merged_map(
                    existing, "chat.instructionsFilesLocations",
                    {".github/instructions": True, instr: True}),
                "chat.promptFilesLocations": merged_map(
                    existing, "chat.promptFilesLocations", {".github/prompts": True, prompts: True}),
            }
        return build

    def _mcp(self, ctx: Ctx):
        ours = {n: stdio_entry(c) for n, c in mcp_servers(ctx).items()}
        return lambda existing: {"servers": merged_map(existing, "servers", ours)}

    def _portable_mcp(self, ctx: Ctx):
        ours = {n: stdio_entry(c) for n, c in mcp_servers(ctx).items()}
        return lambda existing: {"mcpServers": merged_map(existing, "mcpServers", ours)}

    def _changes(self, ctx: Ctx) -> Tuple[List[FileChange], List[str]]:
        refused: List[str] = []
        changes: List[FileChange] = []

        def add(fc: Optional[FileChange]) -> None:
            if fc is not None:
                changes.append(fc)

        # One rules file. The Copilot/Agent Host harness reads ~/.copilot/instructions natively;
        # the Local agent reads it through chat.instructionsFilesLocations (restated below).
        add(FileChange(ctx.home / ".copilot" / "instructions" / "harness.instructions.md", "replace",
                       APPLY_ALL + ctx.rules, "Copilot user rules (applyTo **), all VS Code harnesses"))
        for fname, text in self._prompts(ctx):
            add(FileChange(self._prompt_dir(ctx) / fname, "replace", text,
                           f"Copilot prompt file /{fname[:-10]} (Local agent only)"))

        for udir in vscode_user_dirs(ctx):
            add(json_change(udir / "settings.json", self._settings(ctx),
                            "VS Code user settings: instruction files, AGENTS.md, prompt files on",
                            refused))
            add(json_change(udir / "mcp.json", self._mcp(ctx),
                            "VS Code user MCP: servers.harness (stdio)", refused))

        mdir = vscode_machine_dir(ctx)
        if mdir is not None:
            add(json_change(mdir / "settings.json", self._settings(ctx),
                            "VS Code Remote-SSH machine settings (this server)", refused))
            add(json_change(mdir / "mcp.json", self._mcp(ctx),
                            "VS Code Remote-SSH remote user MCP: servers.harness", refused))

        add(json_change(ctx.home / ".copilot" / "mcp-config.json", self._portable_mcp(ctx),
                        "Copilot portable user MCP (Agent Host, Copilot CLI, remote): mcpServers.harness",
                        refused))

        if ctx.scope == "project" and ctx.project is not None:
            add(FileChange(ctx.project / ".github" / "copilot-instructions.md", "replace",
                           ctx.rules, "project Copilot instructions"))
        return changes, refused

    def plan(self, ctx: Ctx) -> List[FileChange]:
        return self._changes(ctx)[0]

    def notes(self, ctx: Ctx) -> List[str]:
        """Plan lines that are not file changes: refusals, and what covers notebooks."""
        return self._changes(ctx)[1] + [
            "VS Code notebooks (.ipynb) with Copilot use the same user settings, so they are covered.",
            "Prompt files load only in the Local agent; the Copilot, Claude and Codex harnesses use "
            "skills instead (~/.claude/skills is read by all of them).",
        ]

"""Gemini CLI.

Conventions verified 2026-09-30 against:
- https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md
  (user settings ``~/.gemini/settings.json``; ``mcpServers.<name>`` with ``command``/``args``/``env``/
  ``cwd``/``timeout``/``trust``; ``context.fileName`` is a string or a list of strings)
- https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/gemini-md.md
  (global context file ``~/.gemini/GEMINI.md``; example ``"fileName": ["AGENTS.md", "CONTEXT.md", "GEMINI.md"]``)
"""
from __future__ import annotations

from typing import List

from .base import Adapter, Ctx, FileChange, mcp_servers, which
from .copilot_vscode import json_change, merged_map, stdio_entry

CONTEXT_NAMES = ["AGENTS.md", "GEMINI.md"]


def _file_names(existing: dict) -> List[str]:
    ctx_obj = existing.get("context")
    cur = ctx_obj.get("fileName") if isinstance(ctx_obj, dict) else None
    names = [cur] if isinstance(cur, str) else list(cur) if isinstance(cur, list) else []
    for n in CONTEXT_NAMES:
        if n not in names:
            names.append(n)
    return names


class GeminiAdapter(Adapter):
    name = "gemini"
    title = "Gemini CLI"

    def detect(self, ctx: Ctx) -> bool:
        return (ctx.home / ".gemini").is_dir() or which("gemini")

    def _changes(self, ctx: Ctx):
        refused: List[str] = []
        gdir = ctx.home / ".gemini"
        ours = {n: stdio_entry(c, with_type=False) for n, c in mcp_servers(ctx).items()}

        def build(existing: dict) -> dict:
            return {"mcpServers": merged_map(existing, "mcpServers", ours),
                    "context": merged_map(existing, "context", {"fileName": _file_names(existing)})}

        changes = [FileChange(gdir / "GEMINI.md", "replace", ctx.rules, "Gemini CLI global rules")]
        fc = json_change(gdir / "settings.json", build,
                         "Gemini CLI settings: mcpServers.harness, context.fileName += AGENTS.md", refused)
        if fc:
            changes.append(fc)
        return changes, refused

    def plan(self, ctx: Ctx) -> List[FileChange]:
        return self._changes(ctx)[0]

    def notes(self, ctx: Ctx) -> List[str]:
        return self._changes(ctx)[1]

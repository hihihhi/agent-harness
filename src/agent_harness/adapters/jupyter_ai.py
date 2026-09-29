"""JupyterLab with Jupyter AI (v3).

Verified 2026-09-30 against https://jupyter-ai.readthedocs.io/en/v3/users/index.html :
Jupyter AI v3 personas are ACP agents (Claude Code, Codex, Gemini CLI, ...). Custom MCP servers go in
``.jupyter/mcp_settings.json`` "at the root of your workspace", as a list ``mcp_servers`` of
``{name, command, args, env: [{name, value}]}``; JupyterLab must be restarted. The docs define no
user-level file and no instructions setting: each persona reads its own tool's instruction files,
which the claude-code / codex / gemini adapters already write.

User scope writes ``~/.jupyter/mcp_settings.json`` (effective when JupyterLab's root is the home
directory, the default when started from ``~``: an assumption, not a documented user-level file).
Project scope writes ``<project>/.jupyter/mcp_settings.json``.
"""
from __future__ import annotations

from typing import List

from .base import Adapter, Ctx, FileChange, mcp_servers, which
from .copilot_vscode import json_change


def _servers(existing: dict, ours: List[dict]) -> List[dict]:
    cur = existing.get("mcp_servers")
    names = {s["name"] for s in ours}
    kept = [s for s in cur if not (isinstance(s, dict) and s.get("name") in names)] \
        if isinstance(cur, list) else []
    return kept + ours


class JupyterAIAdapter(Adapter):
    name = "jupyter-ai"
    title = "JupyterLab (Jupyter AI)"

    def detect(self, ctx: Ctx) -> bool:
        return (ctx.home / ".jupyter").is_dir() or which("jupyter-lab") or which("jupyter")

    def _changes(self, ctx: Ctx):
        refused: List[str] = []
        ours = [{"name": n, "command": c[0], "args": list(c[1:])} for n, c in mcp_servers(ctx).items()]
        root = ctx.project if ctx.scope == "project" and ctx.project is not None else ctx.home
        fc = json_change(root / ".jupyter" / "mcp_settings.json",
                         lambda ex: {"mcp_servers": _servers(ex, ours)},
                         "Jupyter AI MCP servers: harness (restart JupyterLab)", refused)
        return ([fc] if fc else []), refused

    def plan(self, ctx: Ctx) -> List[FileChange]:
        return self._changes(ctx)[0]

    def notes(self, ctx: Ctx) -> List[str]:
        return self._changes(ctx)[1] + [
            "Jupyter AI agents (Claude Code, Codex, Gemini) read their own instruction files, "
            "installed by those adapters. ~/.jupyter/mcp_settings.json applies when JupyterLab is "
            "started from your home directory; use --project for a project root."]

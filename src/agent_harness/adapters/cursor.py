"""Cursor.

Conventions verified 2026-09-30 against:
- https://cursor.com/docs/context/mcp    (global ~/.cursor/mcp.json, key ``mcpServers``;
  stdio entries list ``type: "stdio"``, ``command``, ``args``, ``env``)
- https://cursor.com/docs/context/rules  (project rules ``.cursor/rules/*.mdc`` with ``description``,
  ``globs``, ``alwaysApply``; User Rules live in Customize -> Rules in the app, not in a file;
  ``AGENTS.md`` in the project root and nested directories)
"""
from __future__ import annotations

from typing import List

from .base import Adapter, Ctx, FileChange, mcp_servers, which
from .copilot_vscode import json_change, merged_map, stdio_entry

MDC_HEAD = "---\ndescription: agent-harness rules\nglobs:\nalwaysApply: true\n---\n\n"


class CursorAdapter(Adapter):
    name = "cursor"
    title = "Cursor"

    def detect(self, ctx: Ctx) -> bool:
        return (ctx.home / ".cursor").is_dir() or which("cursor")

    def _changes(self, ctx: Ctx):
        refused: List[str] = []
        ours = {n: stdio_entry(c) for n, c in mcp_servers(ctx).items()}
        changes: List[FileChange] = []
        fc = json_change(ctx.home / ".cursor" / "mcp.json",
                         lambda ex: {"mcpServers": merged_map(ex, "mcpServers", ours)},
                         "Cursor global MCP: mcpServers.harness", refused)
        if fc:
            changes.append(fc)
        if ctx.scope == "project" and ctx.project is not None:
            changes.append(FileChange(ctx.project / ".cursor" / "rules" / "harness.mdc", "replace",
                                      MDC_HEAD + ctx.rules, "project Cursor rule (alwaysApply)"))
        return changes, refused

    def plan(self, ctx: Ctx) -> List[FileChange]:
        return self._changes(ctx)[0]

    def notes(self, ctx: Ctx) -> List[str]:
        extra = [] if ctx.scope == "project" else [
            "Cursor user rules live in its settings UI (Customize -> Rules), which no file can set. "
            "Globally Cursor gets the harness through MCP; rules come from a project's AGENTS.md, "
            "or run install with --project to add .cursor/rules/harness.mdc."]
        return self._changes(ctx)[1] + extra

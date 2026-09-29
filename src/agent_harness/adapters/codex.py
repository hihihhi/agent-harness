"""Codex CLI: AGENTS.md, [mcp_servers.*] in config.toml, shared skills in ~/.agents/skills.

Per the provider specs (Codex 0.159): custom prompts (~/.codex/prompts) are deprecated in favour of
skills, so none are written; `[profiles]` tables are no longer read, so none are written; Codex's own
memories stay as they are (off by default)."""
from __future__ import annotations

from typing import List

from .base import Adapter, Ctx, FileChange, base_dir, mcp_servers, shared_skills, which


class CodexAdapter(Adapter):
    name = "codex"
    title = "Codex"

    def detect(self, ctx: Ctx) -> bool:
        return which("codex") or (ctx.home / ".codex").is_dir()

    def plan(self, ctx: Ctx) -> List[FileChange]:
        root = base_dir(ctx)
        project = ctx.scope == "project"
        codex = root / ".codex"
        tables = {f"mcp_servers.{n}": {"command": c[0], "args": c[1:]} for n, c in mcp_servers(ctx).items()}
        # Codex MCP docs: default_tools_approval_mode = "auto" pre-approves this server's tools only.
        tables["mcp_servers.harness"]["default_tools_approval_mode"] = "auto"
        note = "agent rules (replaces the file)"
        if not project and (codex / "AGENTS.override.md").is_file():
            note += "; NOTE: your AGENTS.override.md takes precedence and hides it"
        changes = [
            FileChange(root / "AGENTS.md" if project else codex / "AGENTS.md", "replace", ctx.rules, note),
            FileChange(codex / "config.toml", "merge-toml", tables,
                       "registers the harness MCP server; other settings untouched"),
        ]
        return changes + shared_skills(ctx)

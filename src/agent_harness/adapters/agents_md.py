"""Generic AGENTS.md, read by Codex, Cursor, Copilot, Gemini CLI (via context.fileName), OpenCode...

- https://agents.md
- https://cursor.com/docs/context/rules (AGENTS.md in the project root and nested dirs)
- https://code.visualstudio.com/docs/copilot/reference/copilot-settings (chat.useAgentsMdFile)

AGENTS.md is a per-project file; there is no cross-tool user-level location. At user scope this
adapter writes nothing: each tool's own global file (~/.codex/AGENTS.md, ~/.gemini/GEMINI.md,
Copilot's user instructions, ~/.claude/CLAUDE.md) carries the rules.
"""
from __future__ import annotations

from typing import List

from .base import Adapter, Ctx, FileChange


class AgentsMdAdapter(Adapter):
    name = "agents-md"
    title = "AGENTS.md (any tool that reads it)"

    def detect(self, ctx: Ctx) -> bool:
        return True  # a plain file; useful whenever a project is given

    def plan(self, ctx: Ctx) -> List[FileChange]:
        if ctx.scope == "project" and ctx.project is not None:
            return [FileChange(ctx.project / "AGENTS.md", "replace", ctx.rules,
                               "project AGENTS.md (replaced; backup kept)")]
        return []

    def notes(self, ctx: Ctx) -> List[str]:
        if ctx.scope == "project" and ctx.project is not None:
            return []
        return ["AGENTS.md: nothing to write at user scope. AGENTS.md is a per-project file; each "
                "tool's own global file carries the rules. Use --project to write one into a project."]

"""Claude desktop app (MCP only).

Verified 2026-09-30 against https://modelcontextprotocol.io/docs/develop/connect-local-servers :
``claude_desktop_config.json`` with top-level ``mcpServers`` (``command``, ``args``, ``env``) at
macOS ``~/Library/Application Support/Claude/`` and Windows ``%APPDATA%\\Claude\\``. The docs name no
Linux path; ``~/.config/Claude/`` (used by community Linux builds) is written only if it exists.
The app has no global instructions file: rules go in a Project's instructions or the profile's
custom instructions in the app UI.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .base import Adapter, Ctx, FileChange, mcp_servers
from .copilot_vscode import json_change, merged_map, platform, stdio_entry

FILE = "claude_desktop_config.json"


def config_dir(ctx: Ctx) -> Optional[Path]:
    plat = platform()
    if plat == "darwin":
        return ctx.home / "Library" / "Application Support" / "Claude"
    if plat.startswith("win"):
        return ctx.home / "AppData" / "Roaming" / "Claude"
    d = ctx.home / ".config" / "Claude"
    return d if d.is_dir() else None


class ClaudeDesktopAdapter(Adapter):
    name = "claude-desktop"
    title = "Claude desktop app"

    def detect(self, ctx: Ctx) -> bool:
        d = config_dir(ctx)
        return d is not None and d.is_dir()

    def _changes(self, ctx: Ctx):
        refused: List[str] = []
        d = config_dir(ctx)
        if d is None:
            return [], refused
        ours = {n: stdio_entry(c, with_type=False) for n, c in mcp_servers(ctx).items()}
        fc = json_change(d / FILE, lambda ex: {"mcpServers": merged_map(ex, "mcpServers", ours)},
                         "Claude desktop MCP: mcpServers.harness (restart the app)", refused)
        return ([fc] if fc else []), refused

    def plan(self, ctx: Ctx) -> List[FileChange]:
        return self._changes(ctx)[0]

    def notes(self, ctx: Ctx) -> List[str]:
        return self._changes(ctx)[1] + [
            "Claude desktop has no global instructions file. For the rules, paste them into a "
            "Project's instructions or Settings -> Profile custom instructions in the app."]

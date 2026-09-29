"""Claude Code: CLAUDE.md, settings.json hooks, MCP in ~/.claude.json, skills, commands."""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import List

from .base import Adapter, Ctx, FileChange, base_dir, list_dirs, list_md, mcp_servers, shared_skills, which


def guard_command(ctx: Ctx) -> str:
    return "python3 " + shlex.quote(str(ctx.harness_home / "content" / "hooks" / "guard.py"))


def _lib_cmd(ctx: Ctx, module_args: str) -> str:
    lib = shlex.quote(str(ctx.harness_home / "lib"))
    return f"PYTHONPATH={lib} python3 -m {module_args}"


def stop_command(ctx: Ctx) -> str:
    return _lib_cmd(ctx, "agent_harness.cli _hook-stop")


def digest_command(ctx: Ctx) -> str:
    """Minimal memory digest (pinned items, counts, open-work pointer); nothing when empty."""
    return _lib_cmd(ctx, "agent_harness.mcp.memory digest") + " 2>/dev/null; true"


def state_command(ctx: Ctx) -> str:
    return _lib_cmd(ctx, "agent_harness.mcp.state print") + " 2>/dev/null; true"


def prompt_command(ctx: Ctx) -> str:
    """Per-prompt recall of relevant memories and lessons (stdout -> context only when non-empty)."""
    return _lib_cmd(ctx, "agent_harness.cli _hook-prompt")


# Hook semantics per the Claude Code hooks reference: SessionStart matchers are startup, resume,
# clear, compact and fork, and its plain stdout is added to Claude's context; UserPromptSubmit gets
# {prompt, session_id, ...} on stdin and its plain stdout is added to context too. PreCompact (matchers manual|auto)
# cannot add context, so its line reaches the user's transcript only; the rule to call state_save
# lives in AGENTS.md.
PRECOMPACT_LINE = "harness: save the task state with state_save before compaction"


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"
    title = "Claude Code"

    def detect(self, ctx: Ctx) -> bool:
        return which("claude") or (ctx.home / ".claude").is_dir() or (ctx.home / ".claude.json").is_file()

    def plan(self, ctx: Ctx) -> List[FileChange]:
        root = base_dir(ctx)
        claude = root / ".claude"
        project = ctx.scope == "project"
        settings = {"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": guard_command(ctx)}]}],
            "Stop": [{"hooks": [{"type": "command", "command": stop_command(ctx)}]}],
            "SessionStart": [
                {"matcher": "startup|resume|compact", "hooks": [{"type": "command", "command": digest_command(ctx)}]},
                {"matcher": "resume|compact", "hooks": [{"type": "command", "command": state_command(ctx)}]},
            ],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": prompt_command(ctx), "timeout": 5}]}],
            "PreCompact": [{"hooks": [{"type": "command", "command": "echo " + shlex.quote(PRECOMPACT_LINE)}]}],
        }}
        # Pre-approve our own server only (permissions docs: `mcp__<server>` matches all its tools).
        # Warm-up plugins (fetch, playwright) are NOT pre-approved.
        settings["permissions"] = {"allow": ["mcp__harness"]}
        if not project:  # user-level only; one memory dir the harness keeps with its own
            settings["autoMemoryDirectory"] = str(ctx.harness_home / "memory" / "claude-code")
        servers = {n: {"type": "stdio", "command": c[0], "args": c[1:], "env": {}}
                   for n, c in mcp_servers(ctx).items()}
        changes = [
            FileChange(root / "CLAUDE.md" if project else claude / "CLAUDE.md", "replace", ctx.rules,
                       "agent rules (replaces the file)"),
            FileChange(claude / "settings.json", "merge-json", settings,
                       "adds a dangerous-command guard and a run-the-checks reminder; your hooks stay"),
            FileChange(root / ".mcp.json" if project else ctx.home / ".claude.json", "merge-json",
                       {"mcpServers": servers}, "registers the harness MCP server (" + ", ".join(servers) + ")"),
        ]
        changes += shared_skills(ctx)
        for d in list_dirs(ctx.content / "skills"):
            changes.append(FileChange(claude / "skills" / d.name, "symlink", root / ".agents" / "skills" / d.name,
                                      f"skill {d.name} (link to .agents/skills)"))
        for f in list_md(ctx.content / "prompts"):
            changes.append(FileChange(claude / "commands" / f.name, "replace", f, f"slash command /{f.stem}"))
        return changes

    def post_install(self, ctx: Ctx) -> List[str]:
        if ctx.scope == "project":
            return []
        out = []
        for n, c in mcp_servers(ctx).items():
            out.append(f"claude mcp add -s user {n} -- " + " ".join(shlex.quote(x) for x in c))
        return out

"""Claude Code: CLAUDE.md, settings.json hooks, MCP in ~/.claude.json, skills, commands."""
from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import List

from .. import routing
from .base import Adapter, Ctx, FileChange, base_dir, list_dirs, list_md, mcp_servers, shared_skills, which


def guard_command(ctx: Ctx) -> str:
    return "python3 " + shlex.quote(str(ctx.harness_home / "content" / "hooks" / "guard.py"))


def check_guard_command(ctx: Ctx) -> str:
    return "python3 " + shlex.quote(str(ctx.harness_home / "content" / "hooks" / "check_guard.py"))


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


# Claude Code's managed instructions (loaded for every user, before ~/.claude/CLAUDE.md). When the profile's
# extra rules are already there word for word (an organisation installs the same rules for everyone), they
# are left out of CLAUDE.md: the same text twice costs every request its tokens and adds nothing.
MANAGED_CLAUDE_MD = (Path("/etc/claude-code/CLAUDE.md"), Path("/Library/Application Support/ClaudeCode/CLAUDE.md"))


def _norm(text: str) -> str:
    return " ".join(text.split())


def claude_rules(ctx: Ctx) -> str:
    extra = ctx.extra_rules.strip()
    if not extra or extra not in ctx.rules:
        return ctx.rules
    env = os.environ.get("HARNESS_CLAUDE_MANAGED")
    for f in ([Path(env)] if env else list(MANAGED_CLAUDE_MD)):
        try:
            managed = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _norm(extra) in _norm(managed):
            head = extra.splitlines()[0].lstrip("# ").strip()
            note = f"({head}: loaded from Claude Code's managed instructions, {f}.)"
            return ctx.rules.replace(extra, note)
    return ctx.rules


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
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": guard_command(ctx)}]},
                           {"matcher": "Edit|Write|MultiEdit",
                            "hooks": [{"type": "command", "command": check_guard_command(ctx)}]}],
            "Stop": [{"hooks": [{"type": "command", "command": stop_command(ctx), "timeout": 30}]}],
            "SessionStart": [
                {"matcher": "startup|resume|compact", "hooks": [{"type": "command", "command": digest_command(ctx)}]},
                # prints only when this project has a saved state file; nothing (no tokens) otherwise
                {"matcher": "startup|resume|compact", "hooks": [{"type": "command", "command": state_command(ctx)}]},
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
        # Our tools load up front (Claude Code MCP docs: "alwaysLoad": true bypasses tool-search deferral). With
        # many other servers they were deferred, and 8 of 20 harness runs spent a turn on ToolSearch just to load
        # kb_get (eval 2026-09-30). Warm-up plugins stay deferred.
        servers["harness"]["alwaysLoad"] = True
        changes = [
            FileChange(root / "CLAUDE.md" if project else claude / "CLAUDE.md", "replace", claude_rules(ctx),
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
        if routing.available(ctx.content):          # one subagent per tier: model + effort from routing.toml
            for name, tier in routing.resolve(ctx.content, ctx.home, ctx.profile).items():
                changes.append(FileChange(claude / "agents" / f"{routing.PREFIX}{name}.md", "replace",
                                          routing.claude_agent(name, tier),
                                          f"subagent {routing.PREFIX}{name}: {tier['claude']['model'] or 'inherit'}, "
                                          f"effort {tier['claude']['effort']}"))
        return changes

    def post_install(self, ctx: Ctx) -> List[str]:
        if ctx.scope == "project":
            return []
        out = []
        for n, c in mcp_servers(ctx).items():
            out.append(f"claude mcp add -s user {n} -- " + " ".join(shlex.quote(x) for x in c))
        return out

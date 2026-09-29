"""Codex CLI: AGENTS.md, [mcp_servers.*] in config.toml, shared skills in ~/.agents/skills.

Per the provider specs (Codex 0.159): custom prompts (~/.codex/prompts) are deprecated in favour of
skills, so none are written; `[profiles]` tables are no longer read, so none are written; Codex's own
memories stay as they are (off by default).

A profile may add Codex settings of its own, merged key by key into the user's tables (their other
keys stay; uninstall takes out only these):

    [codex.config.features]
    use_legacy_landlock = true
"""
from __future__ import annotations

from typing import Dict, List

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
        # Pre-approve this server's tools only. "approve" is valid in Codex 0.145 (prompt|writes|approve) and
        # in the current MCP docs (auto|prompt|writes|approve); 0.145 rejects "auto", and then every harness
        # call came back "user cancelled MCP tool call" (eval 2026-09-30).
        tables["mcp_servers.harness"]["default_tools_approval_mode"] = "approve"
        for name, body in profile_tables(ctx.profile).items():
            if name[1:] in tables:                 # e.g. [codex.config.mcp_servers.harness]: extra keys for ours
                tables[name[1:]].update(body)
            else:
                tables[name] = body
        note = "agent rules (replaces the file)"
        if not project and (codex / "AGENTS.override.md").is_file():
            note += "; NOTE: your AGENTS.override.md takes precedence and hides it"
        changes = [
            FileChange(root / "AGENTS.md" if project else codex / "AGENTS.md", "replace", ctx.rules, note),
            FileChange(codex / "config.toml", "merge-toml", tables,
                       "registers the harness MCP server; other settings untouched"),
        ]
        return changes + shared_skills(ctx)


def profile_tables(profile: dict) -> Dict[str, dict]:
    """The profile's [codex.config.*] tables as shared ("+name") TOML tables."""
    conf = ((profile or {}).get("codex") or {}).get("config") or {}
    out: Dict[str, dict] = {}

    def walk(prefix: str, d: dict) -> None:
        scalars = {k: v for k, v in d.items() if not isinstance(v, dict)}
        if scalars and prefix:
            out["+" + prefix] = scalars
        for k, v in d.items():
            if isinstance(v, dict):
                walk(f"{prefix}.{k}" if prefix else k, v)
    if isinstance(conf, dict):
        walk("", conf)
    return out

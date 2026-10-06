"""Adapter interface shared by every tool adapter (see docs/contract.md).

An adapter never writes anything: it returns FileChanges and the installer applies them.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union


@dataclass
class Ctx:
    home: Path                    # user's HOME (temp in tests)
    harness_home: Path            # <home>/.agent-harness
    content: Path                 # installed content dir
    profile: dict                 # parsed profile.toml ({} if none)
    rules: str                    # final AGENTS.md text
    mcp_cmd: List[str]            # the MCP server command
    scope: str = "user"           # "user" (default) or "project"
    project: Optional[Path] = None
    extra_mcp: dict = field(default_factory=dict)  # name -> command list, from `harness warmup`
    extra_rules: str = ""         # the profile's extra-rules text, as it appears inside `rules`


@dataclass
class FileChange:
    path: Path                    # absolute
    kind: str                     # "replace" | "merge-json" | "merge-toml" | "copy-dir" | "symlink"
    content: Union[str, dict, Path]
    note: str                     # one line for the plan shown to the user


# "symlink": path becomes a symlink to the directory in `content` (used to point a tool's skills
# dir at the shared ~/.agents/skills copy).
KINDS = ("replace", "merge-json", "merge-toml", "copy-dir", "symlink")


class Adapter:
    name: str = ""                # "claude-code", "codex", "copilot-vscode", "cursor", "gemini", "agents-md"
    title: str = ""               # human name

    def detect(self, ctx: Ctx) -> bool:
        raise NotImplementedError

    def plan(self, ctx: Ctx) -> List[FileChange]:
        raise NotImplementedError

    def post_install(self, ctx: Ctx) -> List[str]:
        return []


# Small helpers adapters may share.

def which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def mcp_servers(ctx: Ctx, shell: bool = True) -> dict:
    """{name: [cmd, *args]} for our server plus any warm-up plugins. shell=False is for a tool with no shell of
    its own: the server then withholds the tools that run commands (mcp/server.py, apply_flags)."""
    servers = {"harness": list(ctx.mcp_cmd) + ([] if shell else ["--no-shell"])}
    for name, cmd in (ctx.extra_mcp or {}).items():
        servers[name] = list(cmd)
    return servers


def list_md(dirpath: Path) -> List[Path]:
    if not dirpath.is_dir():
        return []
    return sorted(p for p in dirpath.iterdir() if p.is_file() and p.suffix == ".md")


def list_dirs(dirpath: Path) -> List[Path]:
    if not dirpath.is_dir():
        return []
    return sorted(p for p in dirpath.iterdir() if p.is_dir())


def base_dir(ctx: Ctx) -> Path:
    """HOME for user scope, the project root for project scope."""
    return ctx.project if ctx.scope == "project" and ctx.project else ctx.home


def shared_skills(ctx: Ctx) -> List[FileChange]:
    """content/skills/* -> ~/.agents/skills/<name> (read by Codex, Gemini CLI, Cursor, Copilot).
    Several adapters may plan the same copy; the installer applies and backs up each path once."""
    dest = base_dir(ctx) / ".agents" / "skills"
    return [FileChange(dest / d.name, "copy-dir", d, f"skill {d.name} (shared .agents/skills)")
            for d in list_dirs(ctx.content / "skills")]

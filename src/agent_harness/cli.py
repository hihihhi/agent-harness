"""harness: install | uninstall | status | doctor | warmup | update | learn | sync."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

if __package__ in (None, ""):  # run as a file: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "agent_harness"

from . import installer as I  # noqa: E402
from .adapters import all_adapters  # noqa: E402

MB = 1024 * 1024
BASE_CAP = 20 * MB
WARM_CAP = 1024 * MB

# Known warm-up plugins: command, what must be on PATH, and a size estimate (download + cache).
PLUGINS = {
    "fetch": {"command": ["uvx", "mcp-server-fetch"], "requires": "uvx", "size_mb": 60,
              "prefetch": ["uvx", "mcp-server-fetch", "--help"]},
    "playwright": {"command": ["npx", "-y", "@playwright/mcp@latest"], "requires": "npx", "size_mb": 450,
                   "prefetch": ["npx", "-y", "@playwright/mcp@latest", "--help"]},
}


def _out(msg: str = "") -> None:
    print(msg, flush=True)


def _paths(args):
    home = Path(args.home).expanduser() if args.home else Path.home()
    return home, I.harness_home(home)


def _profile_dir(args, hh: Path, state: dict) -> Optional[Path]:
    if getattr(args, "profile", None):
        return Path(args.profile).expanduser().resolve()
    if (hh / "profile").is_dir():
        return hh / "profile"
    return None


def kb_index(hh: Path, profile: dict, profile_dir: Optional[Path]) -> str:
    """The always-loaded knowledge index ('' if unavailable). Its roots are exactly the ones the MCP
    server searches (the profile folder's files as their own roots + the profile's kb_paths), so every
    id printed here resolves with kb_get. The profile may name the pages whose section ids are listed
    (`index_pages`, globs); the extra-rules file is left out (it is in the rules already)."""
    from .mcp import kb as kbmod
    paths = (kbmod.profile_folder_roots(Path(profile_dir)) if profile_dir else []) + I.kb_paths(profile, profile_dir)
    paths = [p for p in paths if p.exists()]
    if not paths:
        return ""
    detail = profile.get("index_pages")
    extra = I._pget(profile, "extra_rules", "rules.extra_rules")
    try:
        k = kbmod.KB(home=hh, paths=paths)
        try:
            text = k.build_index(max_bytes=INDEX_CAP, detail=list(detail) if detail else None,
                                 exclude=[Path(str(extra)).name] if extra else [])
        finally:
            getattr(k, "close", lambda: None)()
    except Exception:  # kb absent or broken: the rules still install
        return ""
    return "## Knowledge index\n\n" + text.strip() + "\n" if text.strip() else ""


INDEX_CAP = 4000
DIGEST_CAP = 2048


def memory_digest(hh: Path, project=None) -> str:
    """'## What you remember' from A1's `agent_harness.mcp.memory digest` ('' if none/absent)."""
    lib = hh / "lib"
    if not (lib / "agent_harness" / "mcp" / "memory.py").is_file():
        return ""
    cmd = [sys.executable, "-m", "agent_harness.mcp.memory", "digest"]
    if project:
        cmd += ["--project", str(project)]
    env = dict(os.environ, PYTHONPATH=str(lib), HARNESS_HOME=str(hh))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = p.stdout.strip() if p.returncode == 0 else ""
    if not text:
        return ""
    body = ""
    for line in text.splitlines(keepends=True):
        if len((body + line).encode("utf-8")) > DIGEST_CAP - 40:
            break
        body += line
    return "## What you remember\n\n" + body.rstrip("\n") + "\n"


def build_ctx(home: Path, hh: Path, content: Path, profile_dir: Optional[Path], project, extra_mcp,
              with_index: bool):
    ctx = I.make_ctx(home, hh, content, profile_dir, project, extra_mcp)
    if with_index:
        for extra_part in (kb_index(hh, ctx.profile, profile_dir), memory_digest(hh, project)):
            if extra_part:
                ctx.rules = ctx.rules.rstrip("\n") + "\n\n" + extra_part
    return ctx


def _select(args, ctx, adapters) -> List[str]:
    if args.tools in (None, ""):
        return [n for n, a in adapters.items() if a.detect(ctx)]
    if args.tools == "all":
        return list(adapters)
    names = [t.strip() for t in args.tools.split(",") if t.strip()]
    unknown = [n for n in names if n not in adapters]
    if unknown:
        raise I.InstallError(f"unknown tool(s): {', '.join(unknown)}; known: {', '.join(adapters)}")
    return names


# ---------------------------------------------------------------- install

def _notes(adapter, ctx) -> List[str]:
    fn = getattr(adapter, "notes", None)
    return list(fn(ctx)) if callable(fn) else []


def cmd_install(args) -> int:
    home, hh = _paths(args)
    source = Path(args.source).resolve() if args.source else I.REPO_ROOT
    state = I.load_state(hh)
    profile_dir = _profile_dir(args, hh, state)
    project = Path(args.project).resolve() if args.project else None
    adapters = all_adapters()
    extra = state.get("extra_mcp", {})
    ctx = build_ctx(home, hh, source / "content", profile_dir, project, extra, with_index=False)
    tools = _select(args, ctx, adapters)
    if not tools:
        _out("No supported AI coding tool found. Name one with --tools (" + ", ".join(adapters) + ").")
        return 1
    if not args.dry_run and not args.yes and not I.is_tty():
        _out("Refusing to change your tool setup without a terminal to ask you. Re-run with --yes to accept.")
        return 2
    chosen = []
    for name in tools:
        a = adapters[name]
        for line in I.plan_lines(a, a.plan(ctx)) + [f"  note: {n}" for n in _notes(a, ctx)]:
            _out(line)
        _out("  Warning: " + I.WARNING.format(title=a.title))
        if args.dry_run:
            continue
        if args.yes or I.ask(f"Install for {a.title}?"):
            chosen.append(name)
    if args.dry_run:
        _out("Dry run: nothing was written.")
        return 0
    if not chosen:
        _out("Nothing installed.")
        return 0
    I.copy_harness(hh, source, profile_dir if profile_dir != hh / "profile" else None)
    installed_profile = hh / "profile" if (hh / "profile").is_dir() else None
    ctx = build_ctx(home, hh, hh / "content", installed_profile, project, extra, with_index=True)
    changes = [(n, adapters[n].plan(ctx)) for n in chosen]
    state = I.apply(hh, changes, state, [home] + ([project] if project else []))
    state.update({"source": str(source), "project": str(project) if project else None,
                  "tools": sorted(set(state.get("tools", [])) | set(chosen))})
    I.save_state(hh, state)
    state = extensions_step(home, hh, state, ctx, args.yes)
    for n in chosen:
        for note in _notes(adapters[n], ctx):
            _out(f"  {adapters[n].title}: {note}")
        for c in adapters[n].post_install(ctx):
            _out(f"  ({adapters[n].title}: if the harness server does not show up, run: {c})")
    _out(f"Installed for {', '.join(adapters[n].title for n in chosen)}. "
         "Next: restart the tool, then run `harness doctor` to check it.")
    return 0


def _real_home(home: Path) -> bool:
    try:
        return Path(home).resolve() == Path.home().resolve()
    except OSError:
        return False


def extensions_step(home: Path, hh: Path, state: dict, ctx, yes: bool, budget_bytes=None) -> dict:
    """Editor settings (through the installer, so uninstall undoes them) and curated extensions.
    Editor CLIs install into the REAL user's editor, so they only run when `home` is the real HOME."""
    try:
        from . import extensions as X
    except ImportError:
        return state
    catalog = hh / "content" / X.CATALOG
    tools = state.get("tools", [])
    if catalog.is_file():
        changes, refused = X.settings_changes(ctx, catalog, tools)
        for r in refused:
            _out(f"  editor settings: {r}")
        if changes:
            for ch in changes:
                _out(f"  merge into {ch.path} - {ch.note}")
            if yes or (I.is_tty() and I.ask("Apply these editor settings?")):
                state = I.apply(hh, [("editor-settings", changes)], state)
                I.save_state(hh, state)
    if not _real_home(home):
        _out("Editor extensions: skipped (HOME override; editor CLIs act on your real HOME).")
        return state
    kw = {} if budget_bytes is None else {"budget_bytes": budget_bytes}
    state = X.install(hh, state, tools=tools, yes=yes, log=_out, **kw)
    I.save_state(hh, state)
    return state


def _reapply(hh: Path, home: Path, state: dict) -> None:
    adapters = all_adapters()
    project = Path(state["project"]) if state.get("project") else None
    profile_dir = hh / "profile" if (hh / "profile").is_dir() else None
    ctx = build_ctx(home, hh, hh / "content", profile_dir, project, state.get("extra_mcp", {}), True)
    changes = [(n, adapters[n].plan(ctx)) for n in state.get("tools", []) if n in adapters]
    state = I.apply(hh, changes, state)
    I.save_state(hh, state)


def cmd_uninstall(args) -> int:
    home, hh = _paths(args)
    for f in ("notices.json", ".notices-mcp-day"):  # pending notices go with the setup (mcp/server.py)
        try:
            (hh / f).unlink()
        except OSError:
            pass
    if not I.load_state(hh):
        _out("Nothing to uninstall.")
        return 0
    state = I.load_state(hh)
    if state.get("extensions"):
        try:
            from . import extensions as X
            I.save_state(hh, X.uninstall(state, log=_out))
        except ImportError:
            pass
    done = I.uninstall(hh, _out)
    _out(f"Restored {len(done)} path(s). Memory and lessons are kept in {hh}.")
    return 0


def cmd_status(args) -> int:
    home, hh = _paths(args)
    state = I.load_state(hh)
    if not state:
        _out(f"Not installed (HARNESS_HOME {hh}).")
        return 1
    _out(f"Installed in {hh} for: {', '.join(state.get('tools', []))}")
    for e in state.get("entries", []):
        p = Path(e["path"])
        mark = "ok" if I.path_hash(p) == e.get("written") else ("missing" if not p.exists() else "changed")
        _out(f"  [{mark}] {p}  ({', '.join(e['tools'])})")
    if state.get("extra_mcp"):
        _out("Plugins: " + ", ".join(state["extra_mcp"]))
    return 0


# ---------------------------------------------------------------- doctor

def mcp_probe(cmd: List[str], timeout: float = 5.0) -> Optional[List[str]]:
    """Start the server, ask tools/list, return the tool names (None on failure)."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "harness-doctor", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    data = "".join(json.dumps(m) + "\n" for m in msgs)
    try:
        p = subprocess.run(cmd, input=data, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in p.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == 2 and "result" in msg:
            return [t.get("name") for t in msg["result"].get("tools", [])]
    return None


def cmd_doctor(args) -> int:
    home, hh = _paths(args)
    state = I.load_state(hh)
    checks = []

    def check(ok: bool, what: str, hint: str = ""):
        checks.append(ok)
        _out(f"[{'ok' if ok else 'FAIL'}] {what}" + ("" if ok or not hint else f" - {hint}"))

    check(sys.version_info >= (3, 9), f"python {sys.version.split()[0]}", "needs Python 3.9 or newer")
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        fts = True
    except sqlite3.Error:
        fts = False
    _out(f"[{'ok' if fts else 'info'}] SQLite FTS5 " + ("available" if fts else "missing; search uses the slower fallback"))
    if not state:
        check(False, "harness installed", "run `harness install`")
        return 1
    for e in state.get("entries", []):
        p = Path(e["path"])
        check(p.exists() or p.is_symlink(), f"{', '.join(e['tools'])}: {p}", "missing; re-run `harness install`")
    server = hh / "lib" / "agent_harness" / "mcp" / "server.py"
    if server.is_file():
        names = mcp_probe(I.mcp_cmd(hh))
        check(bool(names), "MCP server answers tools/list within 5 s" + (f" ({len(names)} tools)" if names else ""),
              "the server did not answer; run it by hand: " + " ".join(shlex.quote(x) for x in I.mcp_cmd(hh)))
    else:
        check(False, "MCP server present", f"{server} missing; run `harness update`")
    size = I.dir_size(hh)
    cap = WARM_CAP if state.get("extra_mcp") else BASE_CAP
    check(size < cap, f"disk footprint {size / MB:.1f} MB (cap {cap // MB} MB)", "over the cap")
    return 0 if all(checks) else 1


# ---------------------------------------------------------------- warmup

def plugin_specs(profile: dict, include_optional: bool = False) -> dict:
    out = {}
    for p in ((profile.get("warmup") or {}).get("plugins") or []):
        if isinstance(p, str):
            if p in PLUGINS:
                out[p] = dict(PLUGINS[p])
            else:
                _out(f"  unknown plugin {p!r} (known: {', '.join(PLUGINS)}); skipped")
        elif isinstance(p, dict) and p.get("name") and p.get("command"):
            if p.get("optional") and not include_optional:
                _out(f"  {p['name']}: optional; add --include-optional to install it")
                continue
            cmd = p["command"] if isinstance(p["command"], list) else [p["command"]] + list(p.get("args", []))
            known = PLUGINS.get(p["name"], {})
            out[p["name"]] = {"command": cmd, "requires": p.get("requires", cmd[0]),
                              "size_mb": int(p.get("est_mb", p.get("size_mb", known.get("size_mb", 100)))),
                              "prefetch": p.get("prefetch", known.get("prefetch"))}
    return out


def cmd_warmup(args) -> int:
    home, hh = _paths(args)
    state = I.load_state(hh)
    if not state:
        _out("Install first: `harness install`.")
        return 1
    if args.background:
        log = hh / "warmup.log"
        argv = [sys.executable, "-m", "agent_harness.cli", "--home", str(home), "warmup"]
        if args.include_optional:
            argv.append("--include-optional")
        if args.yes:
            argv.append("--yes")
        env = dict(os.environ, PYTHONPATH=str(I.PKG_DIR.parent))
        with open(log, "ab") as f:
            subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             env=env, start_new_session=True)
        _out(f"Warm-up running in the background; log: {log}")
        return 0
    try:
        from .mcp import kb as kbmod
        k = kbmod.KB(home=hh)
        stats = k.refresh()
        getattr(k, "close", lambda: None)()
        _out(f"Knowledge index built: {stats}")
    except Exception as e:
        _out(f"Knowledge index skipped ({type(e).__name__}: {e})")
    profile = I.load_profile(hh / "profile")
    specs = plugin_specs(profile, args.include_optional)
    extra = dict(state.get("extra_mcp", {}))
    budget = WARM_CAP - I.dir_size(hh)
    for name, spec in specs.items():
        if name in extra:
            continue
        if not shutil.which(spec["requires"]):
            _out(f"  {name}: needs `{spec['requires']}` on PATH; skipped")
            continue
        need = spec["size_mb"] * MB
        if need > budget:
            _out(f"  {name}: about {spec['size_mb']} MB would exceed the 1 GB cap; skipped")
            continue
        if spec.get("prefetch"):
            try:
                subprocess.run(spec["prefetch"], capture_output=True, timeout=600)
            except (OSError, subprocess.TimeoutExpired):
                _out(f"  {name}: download failed; skipped")
                continue
        budget -= need
        extra[name] = spec["command"]
        _out(f"  {name}: ready (about {spec['size_mb']} MB)")
    if extra != state.get("extra_mcp", {}):
        state["extra_mcp"] = extra
    _reapply(hh, home, state)
    state = I.load_state(hh)
    profile_dir = hh / "profile" if (hh / "profile").is_dir() else None
    ctx = build_ctx(home, hh, hh / "content", profile_dir,
                    Path(state["project"]) if state.get("project") else None, state.get("extra_mcp", {}), False)
    extensions_step(home, hh, state, ctx, args.yes, budget_bytes=max(budget, 0))
    _out("Warm-up done. Restart your tools to pick up the changes.")
    return 0


# ---------------------------------------------------------------- update / learn

def cmd_update(args) -> int:
    home, hh = _paths(args)
    state = I.load_state(hh)
    if not state:
        _out("Install first: `harness install`.")
        return 1
    source = Path(args.source or state.get("source") or I.REPO_ROOT).resolve()
    if not (source / "content").is_dir():
        _out(f"No harness source at {source}; pass --source DIR.")
        return 1
    profile = Path(args.profile).expanduser().resolve() if args.profile else None
    if profile and not (profile / "profile.toml").is_file():
        _out(f"No profile.toml in {profile}.")
        return 1
    I.copy_harness(hh, source, profile)
    state["source"] = str(source)
    _reapply(hh, home, state)
    _out(f"Updated from {source}; memory and lessons kept.")
    return 0


def cmd_learn(args) -> int:
    home, hh = _paths(args)
    d = hh / "lessons"
    items = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            items.append(json.loads(f.read_text(encoding="utf-8")))
        except ValueError:
            continue
    if not items:
        _out("No lessons recorded yet.")
        return 0
    items.sort(key=lambda x: -int(x.get("uses", 0) or 0))
    _out(f"{len(items)} lesson(s), most used first:")
    for x in items:
        _out(f"- when {x.get('trigger', '?')}: {x.get('fix', '?')}  (was: {x.get('mistake', '?')}; used {x.get('uses', 0)}x)")
    return 0


# ---------------------------------------------------------------- sync

def cmd_sync(args) -> int:
    from . import sync as S
    home, hh = _paths(args)
    cfg_file = hh / "sync.json"
    cfg = json.loads(cfg_file.read_text()) if cfg_file.is_file() else {}
    remote = args.remote or cfg.get("remote")
    ssh_cmd = args.ssh_cmd or cfg.get("ssh_cmd") or "ssh"
    if args.config:
        if not args.remote:
            _out("harness sync --config needs --remote <ssh-target>:<remote HARNESS_HOME>.")
            return 1
        hh.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(json.dumps({"remote": args.remote, "ssh_cmd": ssh_cmd}, indent=1) + "\n")
        _out(f"Saved: `harness sync` now syncs with {args.remote}.")
        return 0
    if not remote:
        _out("No remote. Run `harness sync --config --remote <ssh-target>:<remote HARNESS_HOME>` once.")
        return 1
    try:
        for line in S.run_sync(hh, remote, ssh_cmd, args.dry_run):
            _out(line)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        _out(f"harness sync: {e}")
        return 1
    return 0


# ---------------------------------------------------------------- Claude Code Stop hook

STOP_REASON = ("Files changed: before you finish, run this project's checks (tests, linters, type checks) "
               "on what you changed and report the result, or say why not. Then session_note in one line, and "
               "state_save if the work is unfinished.")
# D2 (run_checks on): the gate is the recorded result, not a reminder. Blocks once per user turn.
GATE_REASON = ("Files changed since the last passing run_checks ({why}): call run_checks and report its result, "
               "or say why this project cannot be checked. Then session_note in one line.")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Arm skill_nudge (HARNESS_ENABLE=skill_nudge; off by default until the eval shows it helps): after a long turn
# with no skill saved, one reminder, as Hermes' post-session review but inside the session.
NUDGE_TOOLS = 15
NUDGE_REASON = ("Long task: if you worked out a procedure worth repeating, found the working path after errors, "
                "or were corrected, save it now with skill_manage (one call); otherwise just finish.")


def _own_memory(path: str) -> bool:
    """A write to the assistant's own memory (~/.claude, the harness home) is not a project edit: the eval's
    'remember ...' sessions were sent back to run checks after saving a memory file."""
    if not path:
        return False
    try:
        p = Path(os.path.expanduser(path)).resolve()
    except (OSError, RuntimeError):
        return False
    for root in (Path.home() / ".claude", I.harness_home(Path.home())):
        try:
            p.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def stop_hook(stdin=None) -> int:
    """Once per user turn, and only when files were edited in that turn: with run_checks (D2), block while
    no passing run_checks covers the current files; without it, ask for the checks. Never loops."""
    try:
        data = json.loads((stdin or sys.stdin).read() or "{}")
        if data.get("stop_hook_active"):
            return 0
        last_user, edited, n_tools, skill_saved = None, False, 0, False
        with open(data["transcript_path"], encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                content = (e.get("message") or {}).get("content")
                if e.get("type") == "user":
                    if isinstance(content, str) or any(
                            isinstance(c, dict) and c.get("type") == "text" for c in content or []):
                        last_user, edited, n_tools, skill_saved = e.get("uuid") or line[:64], False, 0, False
                elif e.get("type") == "assistant" and isinstance(content, list):
                    uses = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
                    n_tools += len(uses)
                    skill_saved |= any(str(c.get("name", "")).endswith("skill_manage") for c in uses)
                    edited |= any(c.get("name") in EDIT_TOOLS
                                  and not _own_memory(str((c.get("input") or {}).get("file_path") or ""))
                                  for c in uses)
        from .mcp import checks
        reason = ""
        if edited:
            reason = STOP_REASON
            from .mcp.state import project_root
            root = project_root(data.get("cwd") or None)
            if not checks.disabled("run_checks") and checks.gated(root) and checks.fingerprint(root):
                stale, why = checks.unchecked_changes(root)
                reason = GATE_REASON.format(why=why) if stale else ""
        if not checks.disabled("skill_nudge") and n_tools >= NUDGE_TOOLS and not skill_saved:
            reason = (reason + " " + NUDGE_REASON).strip()
        if not reason:
            return 0
        hh = I.harness_home(Path.home())
        marker = hh / "state" / f"stop-{data.get('session_id', 'x')}"
        if marker.is_file() and marker.read_text() == str(last_user):
            return 0
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(last_user))
        print(json.dumps({"decision": "block", "reason": reason}))
    except Exception:
        pass  # a hook must never break the tool
    return 0


RECALL_TIMEOUT = 2.0


def prompt_hook(stdin=None) -> int:
    """UserPromptSubmit: pipe the prompt to A1's recall; print its output only when non-empty.
    Never blocks the prompt: any error or timeout prints nothing and exits 0."""
    try:
        data = json.loads((stdin or sys.stdin).read() or "{}")
        prompt = data.get("prompt") or ""
        if not prompt.strip():
            return 0
        cmd = [sys.executable, "-m", "agent_harness.mcp.memory", "recall",
               "--session", str(data.get("session_id") or "none")]
        env = dict(os.environ, PYTHONPATH=str(I.PKG_DIR.parent))
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=RECALL_TIMEOUT, env=env)
        if p.returncode == 0 and p.stdout.strip():
            sys.stdout.write(p.stdout if p.stdout.endswith("\n") else p.stdout + "\n")
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="harness", description=__doc__)
    ap.add_argument("--home", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("install", help="set up your AI coding tools")
    p.add_argument("--tools", help="comma-separated tool names, or 'all' (default: the ones found)")
    p.add_argument("--yes", action="store_true", help="accept every tool without asking")
    p.add_argument("--profile", help="profile directory (profile.toml + descriptor)")
    p.add_argument("--project", help="install into this project instead of your user settings")
    p.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    p.add_argument("--source", help=argparse.SUPPRESS)
    sub.add_parser("uninstall", help="restore what was there before")
    sub.add_parser("status", help="what is installed, for which tools")
    sub.add_parser("doctor", help="check the installation")
    p = sub.add_parser("warmup", help="build the knowledge index and install optional plugins")
    p.add_argument("--background", action="store_true")
    p.add_argument("--include-optional", action="store_true", help="also install plugins marked optional")
    p.add_argument("--yes", action="store_true", help="accept editor settings and extensions without asking")
    p = sub.add_parser("update", help="refresh the harness, keeping memory and lessons")
    p.add_argument("--source", help="harness source directory")
    p.add_argument("--profile", help="replace the active profile with this directory, then re-render the rules")
    sub.add_parser("learn", help="show the lessons learned so far")
    p = sub.add_parser("sync", help="two-way sync of memory, lessons and state with another machine")
    p.add_argument("--remote", help="<ssh-target>:<remote HARNESS_HOME>, or a local path")
    p.add_argument("--ssh-cmd", help="ssh command to use (default: ssh)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--config", action="store_true", help="save --remote/--ssh-cmd as the default")
    sub.add_parser("_hook-stop")
    sub.add_parser("_hook-prompt")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    fn = {"install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status, "doctor": cmd_doctor,
          "warmup": cmd_warmup, "update": cmd_update, "learn": cmd_learn, "sync": cmd_sync}.get(args.cmd)
    if args.cmd == "_hook-stop":
        return stop_hook()
    if args.cmd == "_hook-prompt":
        return prompt_hook()
    if fn is None:
        build_parser().print_help()
        return 1
    try:
        return fn(args)
    except I.InstallError as e:
        _out(f"harness: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Curated VS Code / Cursor extensions (content/extensions.toml), installed only after approval.

Flow: find the editor CLIs on PATH (code, code-insiders, cursor) -> pick the catalogue entries for
that editor (and for the AI tools the user chose) -> skip what is already installed -> show each with
its reason and size -> ask (or --yes) -> `<cli> --install-extension <id>` -> record in installed.json
under "extensions" exactly what WE added, so uninstall removes those and never the user's own.

Sizes count against the warm-up budget (1 GB cap, shared with warm-up plugins).
Editor settings from the catalogue's [settings] table (telemetry off) and
``remote.SSH.defaultExtensions`` (so server-side extensions follow you onto a Remote-SSH host) are
returned by :func:`settings_changes` as FileChanges for the installer to apply and undo.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import installer as I
from .adapters.base import Ctx, FileChange
from .adapters.copilot_vscode import json_change, platform

MB = 1024 * 1024
WARM_CAP = 1024 * MB
CATALOG = "extensions.toml"

# cli name -> editor kind used in `targets`
EDITOR_CLIS = (("code", "vscode"), ("code-insiders", "vscode"), ("cursor", "cursor"))

# Microsoft licences restrict these to Microsoft's own VS Code builds. Never offer them to Cursor.
MS_RESTRICTED_PREFIXES = ("ms-vscode-remote.", "ms-vscode.remote-", "ms-vsliveshare.")
MS_RESTRICTED_IDS = {"ms-python.vscode-pylance", "ms-vscode.cpptools", "ms-vscode.cpptools-extension-pack",
                     "ms-toolsai.datawrangler", "ms-dotnettools.csharp", "ms-dotnettools.csdevkit"}

REQUIRED_FIELDS = ("id", "publisher", "verified", "source", "why", "size_mb", "targets", "where",
                   "evidence")


def ms_restricted(ext_id: str) -> bool:
    e = ext_id.lower()
    return e in MS_RESTRICTED_IDS or e.startswith(MS_RESTRICTED_PREFIXES)


def load_catalog(path: Path) -> Tuple[List[dict], dict]:
    data = I.load_toml(path.read_text(encoding="utf-8"))
    return list(data.get("extension", [])), dict(data.get("settings", {}))


def detect_editors() -> List[Tuple[str, str]]:
    """[(cli, kind)] for each editor CLI on PATH."""
    return [(cli, kind) for cli, kind in EDITOR_CLIS if shutil.which(cli)]


def select(entries: List[dict], kind: str, tools: Optional[List[str]]) -> List[dict]:
    out = []
    for e in entries:
        if kind not in e.get("targets", []):
            continue
        if kind == "cursor" and ms_restricted(e["id"]):
            continue  # belt and braces: the catalogue test also forbids this
        req = e.get("requires")
        if req and (tools is None or req not in tools):
            continue
        out.append(e)
    return out


def _run(argv: List[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def installed_ids(cli: str) -> set:
    try:
        r = _run([cli, "--list-extensions"], timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {ln.strip().lower() for ln in r.stdout.splitlines() if ln.strip()}


def install(hh: Path, state: dict, tools: Optional[List[str]] = None, yes: bool = False,
            ask: Callable[[str], bool] = I.ask, log: Callable[[str], None] = print,
            budget_bytes: Optional[int] = None, catalog: Optional[Path] = None) -> dict:
    """Offer and install the curated extensions for every editor CLI found. Returns the new state."""
    catalog = catalog or (hh / "content" / CATALOG)
    if not catalog.is_file():
        log(f"Editor extensions: no catalogue at {catalog}; skipped.")
        return state
    entries, _ = load_catalog(catalog)
    editors = detect_editors()
    if not editors:
        log("Editor extensions: no `code` or `cursor` command on PATH; skipped.")
        return state
    if budget_bytes is None:
        budget_bytes = WARM_CAP - I.dir_size(hh)
    budget = budget_bytes - sum(v.get("size_mb", 0) for v in _records(state)) * MB
    recorded = state.setdefault("extensions", {})
    for cli, kind in editors:
        have = installed_ids(cli)
        todo = [e for e in select(entries, kind, tools) if e["id"].lower() not in have]
        if not todo:
            log(f"Editor extensions for `{cli}`: all present.")
            continue
        log(f"Editor extensions for `{cli}` ({kind}):")
        for e in todo:
            log(f"  {e['id']:40} ~{e['size_mb']:>4} MB  {e['where']:6}  {e['why']}")
        log(f"  total ~{sum(e['size_mb'] for e in todo)} MB")
        if not yes and not I.is_tty():
            log("  No terminal to ask you; re-run with --yes to install. Skipped.")
            continue
        if not yes and not ask(f"Install these {len(todo)} extensions into {cli}?"):
            log("  Skipped.")
            continue
        mine = recorded.setdefault(cli, [])
        for e in todo:
            need = e["size_mb"] * MB
            if need > budget:
                log(f"  {e['id']}: ~{e['size_mb']} MB would exceed the 1 GB warm-up cap; skipped")
                continue
            try:
                r = _run([cli, "--install-extension", e["id"]])
                ok = r.returncode == 0
                why = (r.stderr or r.stdout).strip().splitlines()[-1:] if not ok else []
            except (OSError, subprocess.TimeoutExpired) as ex:
                ok, why = False, [type(ex).__name__]
            if not ok:
                log(f"  {e['id']}: install failed ({' '.join(why) or 'no output'}); skipped")
                continue
            budget -= need
            mine.append({"id": e["id"], "size_mb": e["size_mb"]})
            log(f"  {e['id']}: installed")
        if not mine:
            del recorded[cli]
    if not recorded:
        state.pop("extensions", None)
    return state


def _records(state: dict) -> List[dict]:
    return [r for recs in state.get("extensions", {}).values() for r in recs]


def uninstall(state: dict, log: Callable[[str], None] = print) -> dict:
    """Remove only the extensions this harness installed; the user's own are never touched."""
    for cli, recs in list(state.get("extensions", {}).items()):
        if not shutil.which(cli):
            log(f"  `{cli}` not on PATH; left {len(recs)} extension(s) in place")
            continue
        for r in recs:
            try:
                _run([cli, "--uninstall-extension", r["id"]])
                log(f"  {r['id']}: removed from {cli}")
            except (OSError, subprocess.TimeoutExpired):
                log(f"  {r['id']}: could not remove from {cli}")
        del state["extensions"][cli]
    state.pop("extensions", None)
    return state


def editor_user_dirs(home: Path) -> Dict[str, Path]:
    plat = platform()
    if plat == "darwin":
        base = home / "Library" / "Application Support"
    elif plat.startswith("win"):
        base = home / "AppData" / "Roaming"
    else:
        base = home / ".config"
    return {"vscode": base / "Code" / "User", "cursor": base / "Cursor" / "User"}


def settings_changes(ctx: Ctx, catalog: Path, tools: Optional[List[str]] = None
                     ) -> Tuple[List[FileChange], List[str]]:
    """Telemetry off (+ remote.SSH.defaultExtensions for VS Code) in each editor's user settings."""
    entries, settings = load_catalog(catalog)
    refused: List[str] = []
    changes: List[FileChange] = []
    for kind, udir in editor_user_dirs(ctx.home).items():
        if not udir.parent.is_dir():
            continue
        ours = dict(settings)
        if kind == "vscode":
            remote = [e["id"] for e in select(entries, kind, tools) if e["where"] in ("remote", "both")]

            def build(existing, ours=ours, remote=remote):
                cur = existing.get("remote.SSH.defaultExtensions")
                merged = list(cur) if isinstance(cur, list) else []
                merged += [i for i in remote if i not in merged]
                return dict(ours, **{"remote.SSH.defaultExtensions": merged})
        else:
            def build(existing, ours=ours):
                return dict(ours)
        fc = json_change(udir / "settings.json", build,
                         f"{kind} settings: telemetry off" + (", server-side extensions follow Remote-SSH"
                                                              if kind == "vscode" else ""), refused)
        if fc:
            changes.append(fc)
    return changes, refused


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="harness extensions", description=__doc__.splitlines()[0])
    p.add_argument("--home", default=str(Path.home()))
    p.add_argument("--yes", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    a = p.parse_args(argv)
    home = Path(a.home)
    hh = I.harness_home(home)
    state = I.load_state(hh)
    if not state:
        print("Install first: `harness install`.")
        return 1
    if a.uninstall:
        state = uninstall(state)
    else:
        state = install(hh, state, tools=state.get("tools"), yes=a.yes)
    I.save_state(hh, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

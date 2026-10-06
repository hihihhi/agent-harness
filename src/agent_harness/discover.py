"""harness discover: find extensions for a need, vet each one, and install only what the user approved.

    harness discover "<need>" [--json] [--catalog FILE] [--no-network]
    harness discover --install ID --approve SHA256 [--override] [--catalog FILE]

Sources, all read-only: the plugin lists Claude Code and Codex already keep (`claude plugin list --json
--available`, `codex plugin list --available --json`), the official MCP Registry, and Open VSX. A registry is
an index, not a trust signal (it says so itself), and every source has shipped malware at least once
(postmark-mcp 2025-09, GlassWorm 2025-10, ClawHavoc 2026-01), so each item is vetted here:

  block  not pinned to a commit SHA or an exact version; invisible Unicode in its text (the GlassWorm trick);
         an install command with shell syntax, sudo, curl, or a flag that skips the tool's own consent prompt
  warn   it runs hooks, binaries or a local MCP server, or its components could not be inspected; community tier
  pass   otherwise

Install is a separate step that runs exactly one command: the one shown, approved by the sha256 of its argv
AND the pinned ref (the same idea as `claude plugin install --accept-command`). The catalogue is read again
just before installing, so if the item moved to another commit since you approved it, the hash no longer
matches and nothing is installed. A blocked item is never installed unless --override is given as
well. Every install is recorded in ~/.agent-harness/installed-extensions.jsonl (id, pinned ref, approval).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

TIER_RANK = {"official": 0, "verified": 1, "registry": 2, "community": 3}
INVISIBLE = re.compile("[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufe00-\ufe0f\ufeff"
                       "\U000e0000-\U000e007f\U000e0100-\U000e01ef]")
SHA = re.compile(r"^[0-9a-f]{40}$")
EXACT_VERSION = re.compile(r"^v?\d+(\.\d+){1,3}([-+][0-9A-Za-z.-]+)?$")
SHELLY = re.compile(r"[;&|`$<>]|\b(sudo|curl|wget)\b")
SKIPS_CONSENT = {"--yes", "--consent", "--approve-mcps", "--trust"}   # the agent tool's own prompt
TOOL_CLIS = {"claude", "codex", "gemini", "code", "cursor"}
RUNS_CODE = {"hook", "hooks", "bin", "binary", "mcp", "lsp", "monitor"}
OFFICIAL_MARKETS = {"claude-plugins-official", "anthropic", "openai-curated"}
LEDGER = "installed-extensions.jsonl"
TIMEOUT = 10


def install_text(rec: dict) -> str:
    return " ".join(rec.get("install") or [])


def approval(rec: dict) -> str:
    """What the user approves: the exact argv (as a list, so word boundaries count) and the pinned ref."""
    return hashlib.sha256(json.dumps({"argv": rec.get("install"), "ref": rec.get("ref")},
                                     sort_keys=True).encode()).hexdigest()


def vet(rec: dict) -> Tuple[str, List[str]]:
    why_block, why_warn = [], []
    ref = str(rec.get("ref") or "")
    # A registry version (npm, PyPI, Open VSX) is immutable once published; a plugin's "version" is a field in
    # a manifest anyone can repoint, so a plugin is pinned only by a commit SHA.
    immutable = rec.get("kind") in ("mcp", "editor-extension")
    if not (SHA.match(ref) or (immutable and EXACT_VERSION.match(ref))):
        why_block.append("not pinned (ref %r): it can change after you approve it" % (ref or "none"))
    if rec.get("tool") == "codex" and rec.get("kind") == "plugin":
        why_block.append("Codex can neither install a pinned commit nor report which one it installed")
    text = " ".join(str(rec.get(k) or "") for k in ("id", "name", "description", "publisher"))
    if INVISIBLE.search(text):
        why_block.append("invisible Unicode in its name or description")
    inst = rec.get("install") or []
    if not inst or not isinstance(inst, list):
        why_block.append("no install command")
    elif str(inst[0]) not in TOOL_CLIS:   # a bare name, found on PATH: /tmp/evil/claude is not claude
        why_block.append("the install command is not one of the tools' own installers (%s)" % inst[0])
    elif any(SHELLY.search(str(a)) for a in inst):
        why_block.append("the install command has shell syntax, sudo or a download")
    elif any(str(a) in SKIPS_CONSENT
                                      for a in (inst[:inst.index("--")] if "--" in inst else inst)):
        why_block.append("the install command skips the tool's own consent prompt")
    comps = {str(c).lower() for c in rec.get("components") or []}
    if not comps:
        why_warn.append("its components cannot be listed before install; check them after with claude plugin details <id>")
    elif comps & RUNS_CODE:
        why_warn.append("it runs code on your machine: %s" % ", ".join(sorted(comps & RUNS_CODE)))
    if rec.get("tier") == "community":
        why_warn.append("community publisher")
    if why_block:
        return "block", why_block + why_warn
    return ("warn" if why_warn else "pass"), why_warn


def matches(rec: dict, need: str) -> bool:
    hay = " ".join(str(rec.get(k) or "") for k in ("id", "name", "description")).lower()
    return all(w in hay for w in need.lower().split())


def rank(recs: List[dict]) -> List[dict]:
    order = {"pass": 0, "warn": 1, "block": 2}
    return sorted(recs, key=lambda r: (order[r["verdict"]], TIER_RANK.get(r.get("tier"), 9),
                                       -int(r.get("downloads") or 0)))


# ---------------------------------------------------------------- live sources (each fails soft)

def _json(cmd: List[str]) -> Optional[object]:
    if not shutil.which(cmd[0]):
        return None
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _get(url: str) -> Optional[object]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                    timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def claude_plugins() -> List[dict]:
    d = _json(["claude", "plugin", "list", "--json", "--available"])
    out = []
    for p in (d.get("available") if isinstance(d, dict) else None) or []:
        src = p.get("source") if isinstance(p.get("source"), dict) else {}
        market = p.get("marketplaceName") or ""
        out.append({"kind": "plugin", "tool": "claude-code", "id": p.get("pluginId"), "name": p.get("name"),
                    "description": p.get("description"), "publisher": market,
                    "tier": "official" if market in OFFICIAL_MARKETS else "community",
                    "repo": src.get("url"), "ref": src.get("sha") or src.get("ref"),
                    "downloads": p.get("installCount") or 0, "components": [],
                    "install": ["claude", "plugin", "install", str(p.get("pluginId")), "--scope", "project"]})
    return out


def codex_plugins() -> List[dict]:
    d = _json(["codex", "plugin", "list", "--available", "--json"])
    items = d if isinstance(d, list) else (d.get("available") or d.get("plugins") or []) if isinstance(d, dict) else []
    out = []
    for p in items:
        if not isinstance(p, dict):
            continue
        pid = p.get("id") or p.get("pluginId") or p.get("name")
        out.append({"kind": "plugin", "tool": "codex", "id": pid, "name": p.get("name"),
                    "description": p.get("description"), "publisher": p.get("marketplace") or "",
                    "tier": "official" if (p.get("marketplace") or "") in OFFICIAL_MARKETS else "community",
                    "repo": p.get("repository") or p.get("url"), "ref": p.get("sha") or p.get("version"),
                    "components": [], "install": ["codex", "plugin", "add", str(pid)]})
    return out


def mcp_registry(need: str) -> List[dict]:
    d = _get("https://registry.modelcontextprotocol.io/v0.1/servers?version=latest&search="
             + urllib.parse.quote(need))
    out = []
    for entry in (d.get("servers") if isinstance(d, dict) else None) or []:
        s = entry.get("server") if isinstance(entry, dict) and "server" in entry else entry
        if not isinstance(s, dict):
            continue
        pkg = next((x for x in s.get("packages") or [] if isinstance(x, dict)), None)
        if pkg is None:
            continue
        ident, ver, kind = pkg.get("identifier"), str(pkg.get("version") or ""), pkg.get("registryType")
        runner = {"npm": ["npx", "-y", "%s@%s" % (ident, ver)], "pypi": ["uvx", "%s==%s" % (ident, ver)]}.get(kind)
        if not runner:
            continue
        short = str(s.get("name", "")).split("/")[-1] or str(ident)
        out.append({"kind": "mcp", "tool": "claude-code", "id": s.get("name"), "name": short,
                    "description": s.get("description"), "publisher": str(s.get("name", "")).split("/")[0],
                    "tier": "registry", "repo": (s.get("repository") or {}).get("url"), "ref": ver,
                    "components": ["mcp"], "install": ["claude", "mcp", "add", short, "--"] + runner})
    return out


def open_vsx(need: str) -> List[dict]:
    d = _get("https://open-vsx.org/api/-/search?size=10&query=" + urllib.parse.quote(need))
    out = []
    for e in (d.get("extensions") if isinstance(d, dict) else None) or []:
        ns, name, ver = e.get("namespace"), e.get("name"), e.get("version")
        out.append({"kind": "editor-extension", "tool": "vscode", "id": "%s.%s" % (ns, name), "name": name,
                    "description": e.get("description"), "publisher": ns,
                    "tier": "verified" if e.get("verified") else "community", "ref": ver,
                    "downloads": e.get("downloadCount") or 0, "components": [],
                    "install": ["code", "--install-extension", "%s.%s@%s" % (ns, name, ver)]})
    return out


def catalogue(need: str, catalog: Optional[str], network: bool) -> List[dict]:
    if catalog:
        return json.loads(Path(catalog).read_text(encoding="utf-8"))
    recs = claude_plugins() + codex_plugins()
    if network:
        recs += mcp_registry(need) + open_vsx(need)
    return recs


LAST = "discover-last.json"


def search(need: str, catalog: Optional[str] = None, network: bool = True, hh: Optional[Path] = None) -> List[dict]:
    out = []
    for r in catalogue(need, catalog, network):
        if not isinstance(r, dict) or not r.get("id") or not matches(r, need):
            continue
        verdict, reasons = vet(r)
        out.append(dict(r, verdict=verdict, reasons=reasons, approve=approval(r)))
    out = rank(out)
    if hh is not None:   # so --install can find, by the same search, what this search found
        hh.mkdir(parents=True, exist_ok=True)
        (hh / LAST).write_text(json.dumps({"need": need, "ids": [r["id"] for r in out][:200]}), encoding="utf-8")
    return out


def install(rec_id: str, approve: Optional[str], override: bool, hh: Path, catalog: Optional[str] = None,
            network: bool = True) -> Tuple[int, str]:
    need = ""
    try:
        last = json.loads((hh / LAST).read_text(encoding="utf-8"))
        need = last["need"] if rec_id in last.get("ids", []) else ""
    except (OSError, ValueError, KeyError, TypeError):
        pass
    recs = {r.get("id"): r for r in catalogue(need, catalog, network) if isinstance(r, dict)}
    rec = recs.get(rec_id)
    if rec is None:
        return 2, "discover: no item %r in the catalogue" % rec_id
    verdict, reasons = vet(rec)
    cmd, digest = install_text(rec), approval(rec)
    shown = "would run: %s\nverdict: %s%s\napprove it with: --approve %s" % (
        cmd, verdict, "".join("\n  - " + x for x in reasons), digest)
    if verdict == "block" and not override:
        return 1, "discover: %s is blocked and is not installed\n%s" % (rec_id, shown)
    if approve != digest:
        return 1, "discover: not installed: approve exactly this command first\n%s" % shown
    try:
        p = subprocess.run(rec["install"], capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 2, "discover: install failed to run (%s)" % e
    if p.returncode != 0:
        return p.returncode, "discover: install exited %d: %s" % (p.returncode, (p.stderr or p.stdout)[-300:])
    ok, why = verify_installed(rec)
    if not ok:
        return 1, "discover: %s. %s" % (why, rollback(rec))
    hh.mkdir(parents=True, exist_ok=True)
    entry = {"id": rec_id, "ref": rec.get("ref"), "installed": rec.get("installed"), "approved": digest,
             "command": cmd, "verdict": verdict,
             "override": bool(override), "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(hh / LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return 0, "discover: installed %s (%s); recorded in %s%s" % (rec_id, rec.get("ref"), hh / LEDGER,
                                                              "\n" + why if why else "")


def verify_installed(rec: dict) -> Tuple[bool, str]:
    """`claude plugin install` installs whatever the marketplace serves now, not a commit it was given, so the
    commit that actually landed is read back and compared with the one approved."""
    if not (rec.get("tool") == "claude-code" and rec.get("kind") == "plugin"):
        return True, ""
    d = _json(["claude", "plugin", "list", "--json"])
    rows = d if isinstance(d, list) else (d.get("installed") if isinstance(d, dict) else None) or []
    got = next((str(p.get("version") or "") for p in rows if isinstance(p, dict) and p.get("id") == rec.get("id")
                and p.get("scope") == "project"), None)    # the copy this install made, not a user-scope one
    want = str(rec.get("ref") or "").lower()
    if not SHA.match(want):           # only reachable with --override: nothing to compare against, so say so
        rec["installed"] = got
        return True, "not verified: no pinned commit was approved (installed %s)" % (got or "unknown")
    if not got:
        return False, "could not read back which commit of %s was installed" % rec.get("id")
    g = got.lower()
    if not (len(g) >= 7 and (want.startswith(g) or g.startswith(want))):
        return False, "installed commit %s is not the approved %s" % (got, want[:12])
    return True, ""


def rollback(rec: dict) -> str:
    try:
        p = subprocess.run(["claude", "plugin", "uninstall", str(rec.get("id")), "--scope", "project"],
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        return "Uninstalled from the project scope." if p.returncode == 0 else (
            "The uninstall exited %d, so it may still be installed: remove it by hand "
            "(claude plugin uninstall %s --scope project)" % (p.returncode, rec.get("id")))
    except (OSError, subprocess.TimeoutExpired) as e:
        return "Uninstall failed (%s): remove it by hand" % e


def render(recs: List[dict]) -> str:
    if not recs:
        return "discover: nothing found"
    lines = []
    for r in recs:
        lines.append("%-5s %-38s %-9s %s" % (r["verdict"].upper(), r["id"], r.get("tier", ""),
                                            (r.get("description") or "")[:70]))
        lines += ["        - " + x for x in r["reasons"]]
    lines.append("install one: harness discover --install ID --approve <sha256 it prints>")
    return "\n".join(lines)


def results_json(recs: List[dict]) -> str:
    keep = ("kind", "tool", "id", "name", "tier", "ref", "verdict", "reasons", "approve")
    return json.dumps([{k: r.get(k) for k in keep} for r in recs], indent=1)

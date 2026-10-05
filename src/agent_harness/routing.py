"""Model and effort routing: content/routing.toml resolved against what this account can use.

No model is named in code. The template names selectors (`smallest`, `fast`, `default`, ...); this module turns
them into a model per tool from what the tool reports (Codex: ~/.codex/models_cache.json, Claude Code: the
template's alias ladder) and lowers each effort to the template's ceiling and to what that model offers.

    resolve(content_dir, home, profile) -> {tier: {"when": str, "claude": {...}, "codex": {...}}}

Each tool entry is {"model": str or None, "effort": str, "why": str}; model None means "the tool's own default"
(nothing discovered, so nothing is pinned).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from . import installer as I

TEMPLATE = "routing.toml"
USER_FILE = "routing.toml"          # in ~/.agent-harness


def merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(content: Path, home: Path, profile: Optional[dict]) -> dict:
    t = I.load_toml((content / TEMPLATE).read_text(encoding="utf-8"))
    t = merge(t, (profile or {}).get("routing") or {})
    user = home / ".agent-harness" / USER_FILE
    if user.is_file():
        t = merge(t, I.load_toml(user.read_text(encoding="utf-8")))
    return t


def lower_effort(want: str, offered: Optional[List[str]], ladder: List[str], ceiling: str) -> str:
    """`want`, lowered to the ceiling, then to the highest effort the model offers that is not above it."""
    rank = {e: i for i, e in enumerate(ladder)}
    cap = min(rank.get(want, rank.get("medium", 0)), rank.get(ceiling, len(ladder) - 1))
    if not offered:
        return ladder[cap]
    ok = [e for e in offered if e in rank and rank[e] <= cap]
    if ok:
        return max(ok, key=lambda e: rank[e])
    return min((e for e in offered if e in rank), key=lambda e: rank[e], default=ladder[cap])


def codex_models(home: Path, visible: List[str]) -> List[dict]:
    """The account's picker models, in picker order (priority), from Codex's own cache. [] if none."""
    p = home / ".codex" / "models_cache.json"
    try:
        models = json.loads(p.read_text(encoding="utf-8")).get("models") or []
    except (OSError, ValueError, AttributeError):
        return []
    out = [m for m in models if isinstance(m, dict) and m.get("slug") and m.get("visibility") in visible]
    return sorted(out, key=lambda m: (m.get("priority") is None, m.get("priority") or 0))


def codex_configured(home: Path) -> Optional[str]:
    p = home / ".codex" / "config.toml"
    try:
        v = I.load_toml(p.read_text(encoding="utf-8")).get("model")
    except (OSError, ValueError):
        return None
    return v if isinstance(v, str) and v else None


def pick_codex(sel: str, models: List[dict], configured: Optional[str], words: List[str]) -> (Optional[dict], str):
    by_slug = {m["slug"]: m for m in models}
    default = by_slug.get(configured) if configured else None
    if default is None and models:
        default = models[0]
    if sel == "fast":
        for m in models:
            if any(w in (m.get("description") or "").lower() for w in words):
                return m, f"fast: '{m.get('description')}'"
        return default, "fast: none described as fast, so the default"
    if sel == "default":
        why = "your config.toml model" if configured and configured in by_slug else "first in your model picker"
        return default, f"default: {why}"
    if sel in by_slug:                       # a profile or user may name a model they have
        return by_slug[sel], "named in an override"
    return default, f"'{sel}' is not in your model list, so the default"


def pick_claude(sel: str, ladder: List[str]) -> (Optional[str], str):
    if sel == "inherit" or not ladder:
        return None, "the main conversation's model"
    idx = {"smallest": 0, "middle": (len(ladder) - 1) // 2, "largest": len(ladder) - 1}
    if sel in idx:
        return ladder[idx[sel]], f"{sel} of {'/'.join(ladder)}"
    return sel, "named in an override"


def resolve(content: Path, home: Path, profile: Optional[dict] = None) -> Dict[str, dict]:
    t = load(content, home, profile)
    ladder = list(t.get("efforts") or ["low", "medium", "high"])
    ceiling = t.get("ceiling") or "high"
    cx = t.get("codex") or {}
    models = codex_models(home, list(cx.get("visible") or ["list"]))
    configured = codex_configured(home)
    cl_ladder = list((t.get("claude") or {}).get("ladder") or [])
    out: Dict[str, dict] = {}
    for name, tier in (t.get("tiers") or {}).items():
        want = tier.get("effort") or "medium"
        m, why = pick_codex(tier.get("codex") or "default", models, configured, list(cx.get("fast_words") or []))
        offered = [x.get("effort") for x in (m or {}).get("supported_reasoning_levels") or [] if isinstance(x, dict)]
        c_model, c_why = pick_claude(tier.get("claude") or "inherit", cl_ladder)
        out[name] = {
            "when": tier.get("when") or "",
            "brief": tier.get("brief") or "",
            "codex": {"model": (m or {}).get("slug"), "effort": lower_effort(want, offered, ladder, ceiling),
                      "why": why if m else "no model list found (run codex once): the tool's own default"},
            # Claude Code lowers an effort a model does not offer itself; the ceiling still applies here
            "claude": {"model": c_model, "effort": lower_effort(want, None, ladder, ceiling), "why": c_why},
        }
    return out


# ---- what the adapters write: one agent file per tier, named harness-<tier> --------------------------------

PREFIX = "harness-"


def _quote(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)       # a JSON string is a valid TOML basic string and YAML scalar


def claude_agent(name: str, tier: dict) -> str:
    c = tier["claude"]
    lines = ["---", f"name: {PREFIX}{name}", f"description: {_quote('Use for ' + tier['when'])}"]
    if c["model"]:
        lines.append(f"model: {c['model']}")
    lines += [f"effort: {c['effort']}", "---", "", tier["brief"],
              "", f"<!-- written by agent-harness from routing.toml: model {c['why']}; edit "
                  "~/.agent-harness/routing.toml, not this file -->", ""]
    return "\n".join(lines)


def codex_agent(name: str, tier: dict) -> str:
    c = tier["codex"]
    lines = [f"# written by agent-harness from routing.toml (model: {c['why']}); edit ~/.agent-harness/routing.toml",
             f"name = {_quote(PREFIX + name)}", f"description = {_quote('Use for ' + tier['when'])}",
             f"developer_instructions = {_quote(tier['brief'])}"]
    if c["model"]:
        lines.append(f"model = {_quote(c['model'])}")
    lines.append(f"model_reasoning_effort = {_quote(c['effort'])}")
    return "\n".join(lines) + "\n"


def available(content: Path) -> bool:
    return (content / TEMPLATE).is_file()

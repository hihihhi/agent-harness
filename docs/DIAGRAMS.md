# Diagrams

Numbered index of every diagram for agent-harness. The README embeds 1, 2 and 3; 4 is only here.
If a diagram and the code disagree, the code wins.

1. [System overview](#1-system-overview)
2. [The guard's decision for one command](#2-the-guards-decision-for-one-command)
3. [The A/B eval and its keep rule](#3-the-ab-eval-and-its-keep-rule)
4. [History: one-tool setups to agent-harness](#4-history-one-tool-setups-to-agent-harness)

Colours: blue = inputs and stores, grey = processing, amber = checks and refusals, green = results,
dashed = external or private, purple = the path the diagram is about.

## 1. System overview

What one install writes into each of the seven tools (only Claude Code gets the guard hook), and what the tools
then call on the shared MCP server and its local stores.

```mermaid
flowchart LR
    subgraph src["content/ in this repo"]
        RULES["AGENTS.md<br/>the one rulebook"]
        SKILLS["skills/*/SKILL.md"]
    end
    INSTALL["harness install<br/>installer.py + adapters/<br/>backs up each file"]
    AGS[("~/.agents/skills")]
    subgraph tools["seven AI tools"]
        CC["Claude Code"]
        CX["Codex"]
        CP["GitHub Copilot"]
        CU["Cursor"]
        GM["Gemini CLI"]
        CD["Claude desktop"]
        JP["JupyterLab, Jupyter AI"]
    end
    GUARD["content/hooks/guard.py<br/>Claude Code only"]
    subgraph mcp["mcp/server.py, stdio MCP server"]
        KB["kb_search, kb_get<br/>mcp/kb.py"]
        SS["session_search<br/>mcp/sessions.py"]
        MEM["mem_*, lesson_*<br/>mcp/memory.py"]
        NT["notices<br/>notice_line()"]
        SM["skill_manage<br/>mcp/skills.py"]
    end
    subgraph store["~/.agent-harness, local files"]
        IDX[("index.sqlite<br/>FTS5, BM25")]
        MJ[("memory/, lessons/<br/>one JSON per item")]
        NJ[("notices.json")]
    end
    LRN[("~/.agents/skills/learned")]
    TR[("Claude Code and Codex<br/>transcripts")]

    RULES -- "rules text" --> INSTALL
    SKILLS -- "skill folders" --> INSTALL
    INSTALL == "CLAUDE.md, MCP entry,<br/>skills link, guard hook" ==> CC
    INSTALL -- "AGENTS.md,<br/>MCP in config.toml" --> CX
    INSTALL -- "instructions,<br/>prompt files, MCP" --> CP
    INSTALL -- "MCP; rules<br/>with --project" --> CU
    INSTALL -- "GEMINI.md, MCP" --> GM
    INSTALL -- "MCP entry only" --> CD
    INSTALL -- "mcp_settings.json only" --> JP
    INSTALL -- "copies skills" --> AGS
    AGS -. "read natively by Codex,<br/>Gemini, Cursor, Copilot" .-> tools
    CC == "PreToolUse, every Bash call:<br/>exit 2 blocks it" ==> GUARD
    tools -- "tools/call over stdio" --> mcp
    KB -- "sections split<br/>at headings" --> IDX
    SS -- "reads only appended bytes,<br/>secrets masked" --> TR
    SS -- "user and assistant text" --> IDX
    MEM -- "near-duplicates merged" --> MJ
    SM -- "writes SKILL.md" --> LRN
    NT -- "first pending line,<br/>once a day, into<br/>server instructions" --> NJ

    classDef data fill:#dbeafe,stroke:#1d4ed8,color:#0b1220
    classDef step fill:#f1f5f9,stroke:#475569,color:#0b1220
    classDef gate fill:#fef3c7,stroke:#b45309,color:#0b1220
    classDef ext  fill:#f8fafc,stroke:#94a3b8,color:#0b1220,stroke-dasharray:4 3
    classDef key  fill:#ede9fe,stroke:#6d28d9,color:#0b1220,stroke-width:2px
    class RULES,SKILLS data
    class AGS,IDX,MJ,NJ,LRN data
    class KB,MEM,SS,SM,NT step
    class CX,CP,CU,GM,CD,JP ext
    class TR ext
    class INSTALL,CC key
    class GUARD gate
```

Where in the code: `src/agent_harness/installer.py`, `src/agent_harness/adapters/*.py`, `src/agent_harness/mcp/`
(`server.py`, `kb.py`, `memory.py`, `sessions.py`, `skills.py`), `content/hooks/guard.py`.

## 2. The guard's decision for one command

How `guard.py` decides one command: every check can raise `Blocked`; only a command that passes all of them runs.
Substitution bodies, echoed text piped into a shell and `bash -c` / `eval` bodies are checked again, one level deeper.

```mermaid
flowchart TB
    HOOK["Claude Code PreToolUse<br/>JSON payload on stdin"]
    CLI["guard.py --check CMD"]
    PARSE{"payload is a<br/>JSON object?"}
    KIND{"tool_input.command?"}
    FILE{"file_path is a<br/>credential file?"}
    subgraph CHECK["check(cmd, depth), nested up to depth 5"]
        FORK["fork-bomb patterns<br/>quoted text is data"]
        COMP["check_computed<br/>name or target from $(...)"]
        SUB["substitution bodies<br/>checked again"]
        TOK["tokenize, pipelines<br/>heredoc bodies dropped<br/>unless fed to a shell"]
        PIPES["pipes into a shell<br/>curl from untrusted host,<br/>base64 -d, echo TEXT"]
        XA["check_xargs<br/>find or echo into xargs rm"]
        SEG["each segment: unwrap<br/>env, timeout, xargs, command"]
        SC["check_segment<br/>sudo, rm -r, dd, mv,<br/>rsync --delete, force-push"]
        PROT["check_protected<br/>protected trees"]
        NEST["bash -c, eval:<br/>the inner command"]
    end
    PF[("protected-paths file<br/>one absolute path per line")]
    BLOCK["exit 2<br/>reason on stderr"]
    ALLOW["exit 0<br/>the command runs"]

    HOOK -- "stdin" --> PARSE
    PARSE -- "no: fail closed" --> BLOCK
    PARSE -- "yes" --> KIND
    KIND == "string or list:<br/>verdict(cmd, cwd)" ==> FORK
    KIND -- "another type: fail closed" --> BLOCK
    KIND -- "absent" --> FILE
    FILE -- "yes" --> BLOCK
    FILE -- "no" --> ALLOW
    CLI == "verdict(cmd)" ==> FORK
    FORK == "no pattern" ==> COMP
    COMP == "names readable" ==> SUB
    SUB -. "check(body, depth+1)" .-> FORK
    SUB == "bodies pass" ==> TOK
    TOK == "segments split at<br/>; && and pipes" ==> PIPES
    PIPES -. "echoed TEXT:<br/>check(TEXT, depth+1)" .-> FORK
    PIPES == "nothing piped<br/>into sh unseen" ==> XA
    XA == "no critical target" ==> SEG
    SEG == "command name, args" ==> SC
    SC -- "shell -c or eval" --> NEST
    NEST -. "check(inner, depth+1)" .-> FORK
    SC == "no critical target" ==> PROT
    PF -- "absolute paths" --> PROT
    CHECK -- "raises Blocked" --> BLOCK
    PROT == "nothing raised" ==> ALLOW

    classDef data fill:#dbeafe,stroke:#1d4ed8,color:#0b1220
    classDef step fill:#f1f5f9,stroke:#475569,color:#0b1220
    classDef gate fill:#fef3c7,stroke:#b45309,color:#0b1220
    classDef out  fill:#dcfce7,stroke:#15803d,color:#0b1220
    classDef ext  fill:#f8fafc,stroke:#94a3b8,color:#0b1220,stroke-dasharray:4 3
    classDef key  fill:#ede9fe,stroke:#6d28d9,color:#0b1220,stroke-width:2px
    class HOOK,CLI ext
    class PARSE,KIND,FILE,BLOCK gate
    class FORK,COMP,SUB,TOK,PIPES,XA,SEG,SC,PROT,NEST key
    class PF data
    class ALLOW out
```

Where in the code: `content/hooks/guard.py` (`main`, `verdict_for_payload`, `verdict`, `check`, `check_segment`,
`check_protected`, `protected_roots`); the hook entry is in `src/agent_harness/adapters/claude_code.py`.

## 3. The A/B eval and its keep rule

How the v0.2 eval is run and decided: two passes, one per installed version, then the keep rule part by part.
The public phase uses the same runner on the SYNTHETIC set and has no results yet.

```mermaid
flowchart TB
    QS[("question set, --questions<br/>historical: private corpus<br/>public: SYNTHETIC")]
    subgraph P1["pass 1: harness 0.1.1 installed"]
        PL["plain<br/>claude --safe-mode,<br/>codex --ignore-user-config"]
        A011["A011<br/>harness 0.1.1"]
    end
    subgraph P2["pass 2: harness 0.2.0 installed"]
        A02["A02<br/>harness 0.2.0"]
        A02s["A02s<br/>+ memory_snapshot"]
        A02n["A02n, Claude only<br/>+ skill_nudge"]
    end
    RUN["runner.py v02:claude, v02:codex<br/>one CLI session per<br/>question and condition"]
    VER{"installed_version()<br/>matches the pass?"}
    PAIR["pair(): session 1, then<br/>a fresh session 2"]
    QUAR[("quarantine/<br/>whatever a run wrote")]
    RES[("results.jsonl")]
    REP["report.py --v02<br/>re-grades every answer"]
    subgraph KEEP["keep rule, part by part"]
        K1{"old kinds: A02 ≥ A011<br/>tokens ≤ 1.15x A011"}
        K2{"session_search: +2 recall,<br/>called at least once"}
        K2b{"skills: +2 or ≤ 0.80x tokens,<br/>created and read"}
        K3{"arm: more correct<br/>than A02, same questions"}
    end
    SHIP["SHIP, on by default<br/>session_search, skill_manage"]
    DROP["DROP, stays off<br/>memory_snapshot, skill_nudge"]
    STOP["runner exits<br/>needs harness X installed"]
    MISS["exit 1<br/>a group of runs missing"]
    PUB["public phase: P vs H<br/>no results yet"]

    QS -- "same questions" --> RUN
    P1 -- "conditions" --> RUN
    P2 -- "conditions" --> RUN
    RUN -- "version check" --> VER
    VER -- "no: SystemExit" --> STOP
    VER == "yes: one record per run" ==> RES
    VER -- "memory, recall, repeat" --> PAIR
    PAIR -- "session 1's writes kept<br/>for session 2, then moved" --> QUAR
    VER -- "any other run:<br/>its writes moved" --> QUAR
    RES == "all runs" ==> REP
    REP -- "runs missing" --> MISS
    REP == "accuracy, paired<br/>median tokens" ==> K1
    K1 == "pass" ==> K2
    K1 == "pass" ==> K2b
    K1 -- "fail: ship nothing new" --> DROP
    K2 == "pass" ==> SHIP
    K2b == "pass" ==> SHIP
    K2 -- "no gain" --> DROP
    REP -- "arms vs A02" --> K3
    K3 -- "no gain" --> DROP
    QS -. "same runner, Claude only" .-> PUB

    classDef data fill:#dbeafe,stroke:#1d4ed8,color:#0b1220
    classDef step fill:#f1f5f9,stroke:#475569,color:#0b1220
    classDef gate fill:#fef3c7,stroke:#b45309,color:#0b1220
    classDef out  fill:#dcfce7,stroke:#15803d,color:#0b1220
    classDef ext  fill:#f8fafc,stroke:#94a3b8,color:#0b1220,stroke-dasharray:4 3
    classDef key  fill:#ede9fe,stroke:#6d28d9,color:#0b1220,stroke-width:2px
    class QS,QUAR,RES data
    class PL,A011,A02,A02s,A02n,PAIR step
    class RUN,REP key
    class VER,K1,K2,K2b,K3,STOP,MISS gate
    class SHIP,DROP out
    class PUB ext
```

Where in the code: `eval/runner.py` (`plan_for`, `v02`, `pair`, `quarantine`, `public`), `eval/report.py`
(`v02_verdict`, `TOK_LIMIT`, `REPEAT_TOK`), `eval/questions/synthetic.json`. The SHIP and DROP outcomes are the 2026-09-30 report's,
transcribed in [eval/results/historical.md](../eval/results/historical.md).

## 4. History: one-tool setups to agent-harness

Which earlier setups agent-harness replaced, and what moved between its tagged versions; the numbers are the
README's (Results, What I learned).

```mermaid
flowchart LR
    subgraph before["earlier setups, one tool each"]
        CS["claude-setup<br/>April 2026, archived"]
        PS["copilot-setup<br/>April 2026, archived"]
        XS["codex-setup<br/>May 2026, private"]
        AO["agentic-os<br/>July 2026, private<br/>Claude only"]
        CTL["claude-control<br/>July 2026, private"]
    end
    subgraph ah["agent-harness, seven tools"]
        V010["v0.1.0<br/>rules 4,925 tokens"]
        V011["v0.1.1<br/>lean rules 2,291 tokens"]
        V020["v0.2.0<br/>session_search, skill_manage"]
        V030["v0.3.0<br/>work graph, off by default"]
    end
    before -- "replaced by one install:<br/>rules, memory, checks" --> V010
    V010 == "rules cut 53%" ==> V011
    V011 == "kept by the keep rule:<br/>recall 1/6 to 6/6" ==> V020
    V020 == "guard scored on<br/>the agentic-os corpus" ==> V030
    AO -- "work graph,<br/>426 guard cases" --> V030

    classDef step fill:#f1f5f9,stroke:#475569,color:#0b1220
    classDef ext  fill:#f8fafc,stroke:#94a3b8,color:#0b1220,stroke-dasharray:4 3
    classDef key  fill:#ede9fe,stroke:#6d28d9,color:#0b1220,stroke-width:2px
    class CS,PS,XS,AO,CTL ext
    class V010,V011,V020 step
    class V030 key
```

Where in the code: README "History"; the version tags (`git tag`); `content/AGENTS.md` (the rules);
`src/agent_harness/workgraph/`; `tests/guard_corpus_agentic_os.py`, `tests/test_guard_cross.py`.

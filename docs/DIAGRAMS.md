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
    INSTALL["harness install<br/>adapters/<br/>backs up files"]
    subgraph tools["seven tools"]
        CC["Claude Code<br/>rules, MCP,<br/>skills, guard"]
        CX["Codex<br/>rules, MCP,<br/>skills"]
        CP["Copilot<br/>rules, MCP,<br/>prompt files"]
        CU["Cursor<br/>MCP, rules<br/>per project"]
        GM["Gemini CLI<br/>rules, MCP"]
        CD["Claude desktop<br/>MCP only"]
        JP["JupyterLab<br/>MCP only"]
    end
    GUARD["guard.py<br/>exit 2 blocks"]
    subgraph srv["mcp/server.py"]
        KB["kb_search<br/>kb_get"]
        SS["session_search"]
        MEM["mem_*<br/>lesson_*"]
        NT["notices"]
        SM["skill_manage"]
    end
    subgraph store["local files"]
        IDX[("index.sqlite<br/>FTS5")]
        MJ[("memory/<br/>lessons/")]
        NJ[("notices.json")]
        LRN[("learned<br/>skills")]
    end
    TR[("Claude Code,<br/>Codex<br/>transcripts")]

    INSTALL == "writes<br/>config" ==> tools
    CC == "each Bash<br/>call" ==> GUARD
    tools -- "MCP<br/>stdio" --> srv
    KB -- "sections,<br/>BM25" --> IDX
    SS -- "new<br/>messages" --> IDX
    TR -- "secrets<br/>masked" --> SS
    MEM -- "JSON<br/>per item" --> MJ
    NT -- "once<br/>a day" --> NJ
    SM -- "SKILL.md" --> LRN

    classDef data fill:#dbeafe,stroke:#1d4ed8,color:#0b1220
    classDef step fill:#f1f5f9,stroke:#475569,color:#0b1220
    classDef gate fill:#fef3c7,stroke:#b45309,color:#0b1220
    classDef ext  fill:#f8fafc,stroke:#94a3b8,color:#0b1220,stroke-dasharray:4 3
    classDef key  fill:#ede9fe,stroke:#6d28d9,color:#0b1220,stroke-width:2px
    class IDX,MJ,NJ,LRN data
    class KB,MEM,SS,SM,NT step
    class CX,CP,CU,GM,CD,JP ext
    class TR ext
    class INSTALL,CC key
    class GUARD gate
```

Where in the code: `src/agent_harness/installer.py`, `src/agent_harness/adapters/*.py` (skills are copied to
`~/.agents/skills`, which Codex, Gemini CLI, Cursor and Copilot read natively), `src/agent_harness/mcp/`
(`server.py`, `kb.py`, `memory.py`, `sessions.py`, `skills.py`; stores under `~/.agent-harness`, learned skills
under `~/.agents/skills/learned`), `content/hooks/guard.py`.

## 2. The guard's decision for one command

How `guard.py` decides one command: every check can raise `Blocked`; only a command that passes all of them runs.
Substitution bodies, echoed text piped into a shell and `bash -c` / `eval` bodies are checked again, one level deeper.

```mermaid
flowchart TB
    HOOK["PreToolUse payload<br/>or guard.py --check"]
    PARSE{"JSON object, command<br/>a string or list?"}
    subgraph CHECK["check(cmd, depth ≤ 5)"]
        FORK["fork-bomb patterns<br/>quoted text is data"]
        COMP["check_computed<br/>names from $(...)"]
        SUB["$(...) and backtick<br/>bodies"]
        TOK["tokenize, split at<br/>; && and pipes"]
        PIPES["pipes into a shell<br/>curl, base64 -d, echo"]
        XA["check_xargs<br/>find or echo into rm"]
        SC["check_segment<br/>sudo, rm -r, dd, mv,<br/>force-push"]
        PROT["check_protected<br/>protected trees"]
    end
    PF[("protected-paths file<br/>absolute paths")]
    BLOCK["exit 2<br/>reason on stderr"]
    ALLOW["exit 0<br/>command runs"]

    HOOK -- "stdin JSON" --> PARSE
    PARSE -- "no: fail closed" --> BLOCK
    PARSE == "yes" ==> FORK
    FORK == "none" ==> COMP
    COMP == "readable" ==> SUB
    SUB -. "depth+1" .-> FORK
    SUB == "pass" ==> TOK
    TOK == "segments" ==> PIPES
    PIPES -. "echoed text,<br/>depth+1" .-> FORK
    PIPES == "none unseen" ==> XA
    XA == "safe targets" ==> SC
    SC -. "bash -c, eval:<br/>depth+1" .-> FORK
    SC == "safe targets" ==> PROT
    PF -- "paths" --> PROT
    CHECK -- "Blocked" --> BLOCK
    PROT == "nothing raised" ==> ALLOW

    classDef data fill:#dbeafe,stroke:#1d4ed8,color:#0b1220
    classDef gate fill:#fef3c7,stroke:#b45309,color:#0b1220
    classDef out  fill:#dcfce7,stroke:#15803d,color:#0b1220
    classDef ext  fill:#f8fafc,stroke:#94a3b8,color:#0b1220,stroke-dasharray:4 3
    classDef key  fill:#ede9fe,stroke:#6d28d9,color:#0b1220,stroke-width:2px
    class HOOK ext
    class PARSE,BLOCK gate
    class FORK,COMP,SUB,TOK,PIPES,XA,SC,PROT key
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
    QS[("question set<br/>--questions")]
    COND["pass 1, 0.1.1 installed:<br/>plain, A011<br/>pass 2, 0.2.0 installed:<br/>A02, A02s, A02n"]
    RUN["runner.py<br/>one CLI session per<br/>question, condition"]
    VER{"installed_version()<br/>matches?"}
    STOP["SystemExit<br/>wrong version"]
    PAIR["pair(): session 1,<br/>fresh session 2"]
    QUAR[("quarantine/<br/>each run's writes")]
    RES[("results.jsonl")]
    REP["report.py --v02<br/>re-grades answers"]
    MISS["exit 1<br/>runs missing"]
    subgraph KEEP["keep rule, part by part"]
        K1{"A02 ≥ A011 correct,<br/>tokens ≤ 1.15x"}
        K2{"session_search:<br/>+2 recall, called"}
        K2b{"skills: +2 or<br/>≤ 0.80x tokens"}
        K3{"arm: more correct<br/>than A02?"}
    end
    SHIP["SHIP, on by default<br/>session_search,<br/>skill_manage"]
    DROP["DROP, stays off<br/>memory_snapshot,<br/>skill_nudge"]

    QS -- "questions" --> RUN
    COND -- "condition" --> RUN
    RUN -- "per run" --> VER
    VER -- "no" --> STOP
    VER == "yes" ==> RES
    VER -- "recall, memory,<br/>repeat" --> PAIR
    PAIR -- "then moved" --> QUAR
    RES == "all runs" ==> REP
    REP -- "a group short" --> MISS
    REP == "accuracy,<br/>tokens" ==> K1
    K1 == "pass" ==> K2
    K1 == "pass" ==> K2b
    K1 -- "fail" --> DROP
    K2 == "pass" ==> SHIP
    K2b == "pass" ==> SHIP
    REP -- "arms" --> K3
    K3 -- "no gain" --> DROP

    classDef data fill:#dbeafe,stroke:#1d4ed8,color:#0b1220
    classDef step fill:#f1f5f9,stroke:#475569,color:#0b1220
    classDef gate fill:#fef3c7,stroke:#b45309,color:#0b1220
    classDef out  fill:#dcfce7,stroke:#15803d,color:#0b1220
    classDef key  fill:#ede9fe,stroke:#6d28d9,color:#0b1220,stroke-width:2px
    class QS,QUAR,RES data
    class COND,PAIR step
    class RUN,REP key
    class VER,K1,K2,K2b,K3,STOP,MISS gate
    class SHIP,DROP out
```

Where in the code: `eval/runner.py` (`plan_for`, `v02`, `pair`, `quarantine`, `public`), `eval/report.py`
(`v02_verdict`, `TOK_LIMIT`, `REPEAT_TOK`), `eval/questions/synthetic.json`. The SHIP and DROP outcomes are the 2026-09-30 report's,
transcribed in [eval/results/historical.md](../eval/results/historical.md).

## 4. History: one-tool setups to agent-harness

Which earlier setups agent-harness replaced, and what moved between its tagged versions; the numbers are the
README's (Results, What I learned).

```mermaid
flowchart TB
    subgraph before["earlier setups, one tool each"]
        CS["claude-setup<br/>Apr 2026<br/>archived"]
        PS["copilot-setup<br/>Apr 2026<br/>archived"]
        XS["codex-setup<br/>May 2026<br/>private"]
        CTL["claude-control<br/>Jul 2026<br/>private"]
        AO["agentic-os<br/>Jul 2026<br/>private"]
    end
    V010["agent-harness v0.1.0<br/>rules 4,925 tokens"]
    V011["v0.1.1<br/>lean rules 2,291 tokens"]
    V020["v0.2.0<br/>session_search,<br/>skill_manage"]
    V030["v0.3.0<br/>work graph,<br/>off by default"]

    before -- "replaced by one install<br/>for seven tools" --> V010
    V010 == "rules cut 53%" ==> V011
    V011 == "recall 1/6 to 6/6,<br/>kept by the keep rule" ==> V020
    V020 == "guard scored on<br/>agentic-os corpus" ==> V030
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

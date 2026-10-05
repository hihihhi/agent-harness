# agent-harness docs

Start with the [project README](../README.md); every page below goes one level deeper.

## Understand it

| Page | What it answers |
|---|---|
| [how-it-works.md](how-it-works.md) | What each part does: rules, retrieval, memory, lessons, past sessions, learned skills, hooks, the work graph, supported tools, privacy |
| [DIAGRAMS.md](DIAGRAMS.md) | Every diagram, numbered: system overview, the guard's decision, the eval and its keep rule, history |
| [design-decisions.md](design-decisions.md) | Why it is built this way, what each choice costs, the keep rule, and the setups it replaced |

## Check the evidence

| Page | What it answers |
|---|---|
| [results.md](results.md) | Every measured result with its qualifier: the A/B eval, the public set, the guard (held out, cross-corpus, in-sample), the work graph, the tests |
| [how-it-works.md, the guard corpus](how-it-works.md#the-guard-against-a-wider-corpus-and-a-held-out-set) | Where the guard corpus came from, the disagreement table, and what changed in scope in v0.3 |
| [eval/README.md](../eval/README.md) | How the A/B eval runs, its conditions, and whose usage a full pass spends |
| [eval/results/historical.md](../eval/results/historical.md) | The original eval's numbers (private corpus) and the deviations its reports record |

## Reference

| Page | What it answers |
|---|---|
| [usage.md](usage.md) | Every `harness` command, the demo's output, the checks, CI and install caveats |
| [contract.md](contract.md) | The build contract: module layout, shared names, the MCP tools and the adapter interface |

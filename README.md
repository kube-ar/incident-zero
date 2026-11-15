# Incident Zero

"Our API started returning 500s right after the last deployment." That's the page. Was it the
deployment?

In this project you build an on-call agent for Reelvane, a fictional video streaming service. The
agent doesn't get a log dump pasted into a prompt. It gets the page and a console of tools, and it
has to find the cause itself: read the logs, look at what changed, and back its answer with
evidence. Sometimes the deployment did it. Sometimes it just happened to be the last thing anyone
touched.

## Milestones

Each milestone closes a chapter of a course on [kube.ar](https://kube.ar). The instructions live
on the site, next to the lessons they use. This repo holds the code, the incidents and the checks.

Part 1, the on-call agent (the [AI Agents](https://kube.ar/agents) course):

| # | Milestone | Course chapter | Status |
|---|---|---|---|
| 0 | [Was it the deploy?](https://kube.ar/agents/project/incident-zero/was-it-the-deploy): the bare agent loop, four read-only tools and `resolve`, one incident | AI Agents · Foundations | ready (needs an LLM) |
| 1 | The first page: a turn limit, errors back into the context, a safety rule in code, a runbook workflow vs the agent | AI Agents · Foundations | planned |
| 2 | The console over MCP | AI Agents · MCP | planned |
| 3 | Incident states | AI Agents · Building the loop | planned |
| 4 | The investigation plan | AI Agents · Reasoning and planning | planned |
| 5 | Ask before prod | AI Agents · Humans in the loop | planned |
| 6 | The war room | AI Agents · Multi-agent systems | planned |
| 7 | Postmortems | AI Agents · Memory | planned |

Part 2, on call in production (the [Agents in Production](https://kube.ar/agents-in-production) course):

| # | Milestone | Course chapter | Status |
|---|---|---|---|
| P1 | The failure log | Prototype to product | planned |
| P2 | Contracts and fuses | Reliable execution | planned |
| P3 | From traces to a benchmark | Monitoring and testing | planned |
| P4 | Attack the on-call agent | Security | planned |
| P5 | Cost per incident | Cost and infrastructure | planned |

## Setup

1. Click **Use this template** on GitHub to get your own copy, and clone it.
2. Install [uv](https://docs.astral.sh/uv/), then:

```sh
uv sync
uv run oncall tools                                   # the tools an agent gets
uv run oncall call get_page                           # the page that woke you up
uv run oncall call search_logs query="catalog-api error" limit=5
```

Python 3.12 or newer. No database, no containers.

### An LLM

Every milestone runs your agent on a language model. Use whichever provider you have a key for:
Claude (`anthropic`), OpenAI, Fireworks or OpenRouter. The model has to support tool calling.

```sh
cp .env.example .env     # then set ONCALL_LLM_PROVIDER, ONCALL_LLM_MODEL and that provider's key
uv run oncall llm        # checks the setup with three tiny calls
```

`.env` is gitignored: never commit a key. In your code, `from oncall.llm import get_llm` gives you
the model `.env` names, and `oncall.llm.Conversation` runs a tool-using conversation with it on
any of the four providers.

## How a milestone works

1. Finish the chapter on the site and open its milestone page.
2. Write your code in the milestone's file, for example `src/oncall/agent/m0_was_it_the_deploy.py`.
   The docstrings say exactly what each function must return.
3. Run the checks:

```sh
uv run oncall check 0             # links to the lessons in English
uv run oncall check 0 --lang es   # ...or in Spanish
```

Each check says what's right, what isn't, and which lesson explains it. A check that runs your
agent costs a few model calls; a loop without a working turn limit is stopped by the console
long before it gets expensive.

**Fast or deep.** Every milestone can be built two ways. The fast path uses a library (your
provider SDK's tool runner, later an MCP SDK or an agent framework) and gets you to a working
result quickly. The deep path writes the same piece yourself. The checks are the same either way.

**Stuck, or skipping ahead?** `uv run oncall check 0 --reference` shows what passing looks like.
`uv run oncall use-reference 0` swaps your file for the reference solution (yours is kept as
`.mine.py`; `--undo` puts it back).

## The incidents

| Folder | What |
|---|---|
| `incidents/zero` | Milestone 0's incident: the page, today's changes and their diffs, and about 750 lines of logs from the gateway (nginx access log), catalog-api and playback-api (JSON lines). `truth.json` says what really happened: the checks read it, you shouldn't |

The logs keep one request in 500, the same requests on every service, so a request id links a
gateway line to the service that answered it. Each log line's id is its file and line number,
like `gateway:12`: that's what an agent cites as evidence. Reelvane, its people and its
customers are fictional, and client addresses come from the ranges reserved for documentation.

Maintainers: `tools/build_incident_zero.py` writes `incidents/zero` from a seed; the tests check
that the files are exactly what it writes. Learners never need it.

## License

Code: MIT. Incidents: CC BY 4.0. See [LICENSE](LICENSE).

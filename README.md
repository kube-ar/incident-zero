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
| 1 | [The first page](https://kube.ar/agents/project/incident-zero/the-first-page): the live console, a safety rule in code, old results shrunk, a runbook workflow vs the agent | AI Agents · Foundations | ready (the practice pack needs an LLM) |
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
uv run oncall tools                                   # milestone 0's tools
uv run oncall call get_page                           # the page that woke you up
uv run oncall call search_logs query="catalog-api error" limit=5
uv run oncall incidents                               # the live incidents, from milestone 1 on
uv run oncall shell p1                                # work one by hand: the clock runs while you look
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
uv run oncall check 1             # links to the lessons in English
uv run oncall check 1 --lang es   # ...or in Spanish
uv run oncall check 1 --no-llm    # everything that doesn't need your LLM
```

Each check says what's right, what isn't, and which lesson explains it. A check that runs your
agent costs model calls (milestone 1's practice pack runs it on six incidents); a loop without a
working turn limit is stopped by the console long before it gets expensive. Many checks need no
LLM at all: they drive your loop with scripted models (`oncall.scripted`) that make, on cue, the
dangerous moves a real model makes only now and then.

**Fast or deep.** Every milestone can be built two ways. The fast path uses a library (your
provider SDK's tool runner, later an MCP SDK or an agent framework) and gets you to a working
result quickly. The deep path writes the same piece yourself. The checks are the same either way.

**Stuck, or skipping ahead?** `uv run oncall check 0 --reference` shows what passing looks like.
`uv run oncall use-reference 0` swaps your file for the reference solution (yours is kept as
`.mine.py`; `--undo` puts it back).

## The incidents

Milestone 0's incident is over and written to files:

| Folder | What |
|---|---|
| `incidents/zero` | The page, today's changes and their diffs, and about 750 lines of logs from the gateway (nginx access log), catalog-api and playback-api (JSON lines). `truth.json` says what really happened: the checks read it, you shouldn't |

From milestone 1 on, incidents run live. `oncall.world` simulates Reelvane's production (fifteen
components: services, databases, a cache, a search cluster, a queue and two outside vendors, on
six nodes) from a seed, minute by minute, and injects faults into it. The live console
(`oncall.live`) is how an agent sees it and acts on it: metrics, logs in their real formats,
traces, changes and diffs, vendor status pages and runbooks, eleven actions, `wait`, `resolve`
and `escalate`. Reading costs incident time, the failure spreads while you look, and an action
changes every minute after it, never one already read.

| Pack | Incidents | What |
|---|---|---|
| practice | p1-p6 | One fault each, chosen to teach. `oncall explain p1` tells the story once you've tried |
| standard | s1-s12 | One per fault family: a bad deploy, a config change, an expired certificate, connection exhaustion, a full disk, a memory leak, a cache stampede, a vendor outage, a retry storm, a flag rollout, a DNS change, a noisy neighbor |
| hard | h1-h12 | Two faults at once, a page far from the cause, old errors that look new |

In most of them the last deploy is innocent. The console doesn't stop a harmful action: it
records it, and `oncall.live.judge` says whether a run solved the incident (the right cause, two
kinds of evidence that really show it, fixed and healthy, no forbidden action) and what it cost.
The repo's tests run a scripted on-call that solves all 30 from the tools alone, and the classic
mistakes (roll back the latest deploy, resolve at once, restart everything, obey a log line),
which fail.

The logs keep one request in 500, the same requests on every service, so a request id links a
gateway line to the service that answered it. Each log line's id is its file and line number,
like `gateway:12`: that's what an agent cites as evidence. Reelvane, its people and its
customers are fictional, and client addresses come from the ranges reserved for documentation.

Maintainers: `tools/build_incident_zero.py` writes `incidents/zero` from a seed; the tests check
that the files are exactly what it writes. Learners never need it.

## License

Code: MIT. Incidents: CC BY 4.0. See [LICENSE](LICENSE).

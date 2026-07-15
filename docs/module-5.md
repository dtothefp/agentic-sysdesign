# Module 5: Managed Agents, from first principles

Modules 1 through 4 built a pipeline. Data model, a Celery fan-out, a deploy, an AI rating
stage. Every one of those we own end to end. Module 5 adds the one part we don't write
ourselves, the agent loop, and hands it to Anthropic's Managed Agents platform.

This doc explains what that actually means. Where the control flow goes, why it's shaped
the way it is, what a "deployment" changes, and how the whole thing relates to the plain
Messages API and to a framework like LangGraph. Read it top to bottom once, then keep the
two diagrams open next to the code.

## The one idea to hold onto

An agent is a single model call in a loop with tools. That's it. You send the conversation
so far, the model replies, and if the reply says "I want to run a tool" you run it, append
the result, and call again. Repeat until the model says it's done. The loop is the whole
game, and the only real question in this module is **who writes and hosts that loop**.

In Module 4 we wrote our own logic everywhere. In Module 5 the loop belongs to Anthropic.
We supply an agent config, a set of tools, and a trigger. The platform runs the loop on its
own infrastructure, in a container it hosts, and reports back over a stream.

## Three channels that never cross

The hardest part of reading the current setup is that three different conversations are
happening at once, between different pairs of processes, and it's easy to think they're one
thing. They aren't. Name them and the whole thing gets simple.

1. **Trigger.** How a run starts. The client (curl, the UI, later a cron) makes a plain
   `POST /digests` to our FastAPI. FastAPI inserts a `digests` row and enqueues a Celery
   task. That's the whole trigger channel. It's ordinary HTTP and a queue push, nothing
   Anthropic-specific.
2. **Babysitting.** The Celery worker talks to Anthropic. It calls `sessions.create` plus a
   kickoff, then holds a one-way SSE stream of events coming back from the agent. This is
   the channel that watches the agent think. It also executes one worker-side tool (more on
   that below).
3. **Tool and delivery.** The agent, running inside Anthropic's sandbox, reaches back to
   our API over the public internet with `curl`. It reads signals and, at the end, writes
   the finished digest with a `PUT` that flips the row to `completed`. The API key for those
   calls comes from a **vault**, not from inside the sandbox.

Completion is judged by the database, not by the stream. The run is done when the agent's
`PUT` has flipped the `digests` row to `completed`. The stream is just a live view.

### Current control flow (worker-babysat, no MCP)

```
CURRENT  (the Celery worker drives and babysits the session, custom tool held in the stream)
============================================================================================

 CLIENT          FastAPI (api)     POSTGRES         CELERY WORKER          ANTHROPIC SANDBOX
 curl / UI       front door        the truth        run_digest_session     Anthropic-hosted box
   |                 |                |                   |                      |
   |  POST /digests  |                |                   |                      |
   | --------------> |                |                   |                      |
   |                 |  INSERT row    |                   |                      |
   |                 | -------------> |  status=running   |                      |
   |                 |  enqueue task (via Redis broker)   |                      |
   |                 | ---------------------------------> |                      |
   |   202 {id}      |                |                   |                      |
   | <-------------- |                |                   |                      |
   |                 |                |                   |  sessions.create     |
   |                 |                |                   |  + kickoff           |
   |                 |                |                   | ===================> |
   |                 |                |                   |                      |  agent runs
   |                 |                |                   |   SSE events         |  bash, python
   |                 |                |                   | <=================== |
   |                 |                |                   |  (one-way stream)    |
   |                 |                |                   |                      |
   |                 |                |                   |  custom_tool_use     |
   |                 |                |                   |  get_rated_signals   |
   |                 |                |                   | <=================== |
   |                 |                |                   |  worker runs it,     |
   |                 |                |                   |  sends result back   |
   |                 |                |                   | ===================> |
   |                 |                |                   |                      |
   |                 |     curl GET /signals, /ratings  (vaulted X-API-Key)     |
   |                 | <------------------------------------------------------- |
   |                 |  rows           |                  |                      |
   |                 | -------------------------------------------------------> |
   |                 |                 |                  |                      |
   |                 |     curl PUT /digests/{id}  (X-API-Key stamped by VAULT at egress)
   |                 | <------------------------------------------------------- |
   |                 |  UPDATE row     |                  |                      |
   |                 | -------------> | status=completed  |                      |
   |                 |                | body=digest       |                      |
   |                 |                |                   |                      |
   |  GET /digests/{id}/stream   (SSE, relayed from Redis pub/sub)              |
   | --------------> |                |                   |                      |
   |  live events    |                |                   |                      |
   | <.............. |                |                   |                      |

 Legend
   ---->   ordinary HTTP to our API (trigger channel, and the vaulted delivery curl)
   ====>   Anthropic session traffic (babysitting channel, worker <-> sandbox)
   ....>   SSE live view back to the client
```

## Vaulted sandbox curl, unpacked

"Vaulted sandbox curl" is three ideas glued together, and it's worth pulling apart because
it's the piece that survives every later change.

- **Sandbox** is the container Anthropic spins up for the session. When the agent runs bash,
  writes a throwaway Python file, or runs `curl`, all of that happens inside that container,
  on Anthropic's infrastructure, not on our Railway boxes.
- **Curl** is how the agent talks to our API. To save the digest it runs, inside the sandbox,
  a `PUT https://sysdesign.thedefrag.ai/digests/{id}` with the body of the digest. That call
  is what flips the database row.
- **Vaulted** is what makes it safe. We can't put the real `X-API-Key` in the sandbox,
  because the agent is a model and could print or leak it. So the real key lives in an
  Anthropic-held vault. Inside the sandbox the key is only a placeholder. When the request
  leaves the container, Anthropic's edge substitutes the real key, but only for requests
  going to our allowlisted hosts (`sysdesign.thedefrag.ai`, `*.up.railway.app`).

The mental model is an egress proxy that stamps an auth header on the way out, the way a
service-mesh sidecar injects credentials on outbound calls. The app inside the mesh never
holds the secret. Here the sandbox holds a placeholder and the boundary injects the real
value. Because the vault is bound to the agent and environment (not to any one process),
this delivery path keeps working no matter what drives the run.

## Two tool mechanisms, and why one of them is fragile

The current setup happens to use two different ways to give the agent a tool, and telling
them apart is the key to understanding what a deployment breaks.

| Mechanism | How it works | Bound to | Survives an unattended deployment? |
|---|---|---|---|
| **Vaulted sandbox curl** (read signals, write the digest) | the agent runs `curl` in the sandbox, the vault stamps the key at egress | the agent / environment | Yes, untouched |
| **`get_rated_signals` custom tool** | the SSE stream announces `custom_tool_use`, the Celery worker runs it and posts the result back | the worker process holding the stream | No, this is the fragile one |

A worker-side custom tool needs a live process babysitting the stream to receive the tool
call and send a result back. That's fine when a worker drives the run. It falls apart the
moment nobody is babysitting, which is exactly what a scheduled deployment is.

## What a deployment is, and what it changes

A "deployment" in Managed Agents is a first-class object that fires sessions on its own, on
a schedule. The cron lives on Anthropic's side and the platform creates the session
directly. There is no client POST and no worker, because the platform is now both the
trigger and the loop-runner.

That is why the worker-side custom tool has to move. With no babysitter, `get_rated_signals`
has nobody to execute it and the run would hang. The fix is **MCP**. An MCP server is a
network service the agent connects to directly, registered at the agent and environment
tier, the same tier as the vault. The agent dials it on its own, so it works with no client
in the loop. The tool stops being a sidecar the worker runs and becomes a service the agent
reaches.

This is also why building a bespoke `GET /rated-signals` REST endpoint first would be wasted
work. The endpoint and the MCP server do the same job, bundling the granular `/signals`,
`/ratings`, and `/influencers` reads into one clean operation. Building both puts that logic
in two places. Going straight to MCP keeps it in one home.

### Target control flow (scheduled deployment, MCP tool, no worker)

```
FUTURE  (a scheduled deployment drives the loop, tools are MCP, the Celery worker is gone)
==========================================================================================

 ANTHROPIC PLATFORM        ANTHROPIC SANDBOX      MCP SERVER (ours)       FastAPI + POSTGRES
 scheduler + agent loop    Anthropic-hosted box   get_rated_signals,      our API + the truth
 (this replaces            the agent's container  save_digest (optional)
  the Celery worker)
        |                       |                      |                       |
        |  cron fires the       |                      |                       |
        |  deployment,          |                      |                       |
        |  creates a session    |                      |                       |
        | ====================> |  agent starts        |                       |
        |  runs the loop         |                      |                       |
        |  server-side, with    |                      |                       |
        |  no babysitting       |                      |                       |
        |                       |  get_rated_signals   |                       |
        |                       |  (MCP call)          |                       |
        |                       | -------------------> |                       |
        |                       |                      |  GET /signals,        |
        |                       |                      |  /ratings (vaulted)   |
        |                       |                      | --------------------> |
        |                       |                      |  rows                 |
        |                       |                      | <-------------------- |
        |                       |  tool result         |                       |
        |                       | <------------------- |                       |
        |                       |                      |                       |
        |                       |  save_digest (MCP)   OR   vaulted curl PUT    |
        |                       | -------------------> | --------------------> |
        |                       |                      |  UPDATE digests row    |
        |                       |                      |  status=completed      |
        |                       |                      |                       |

 What changed from CURRENT
   gone     the Celery worker (run_digest_session) and the SSE relay it held
   gone     POST /digests as the trigger, the platform scheduler starts the session
   moved    get_rated_signals, from a worker-held custom tool to an MCP service the agent
            dials directly (agent / environment tier, like the vault)
   same     the sandbox still reaches our API with the vaulted key, Postgres is still the
            source of truth, and the agent config (resources.json, vault, memory store) is
            byte-for-byte identical
```

The reassuring part is the last line. The agent itself doesn't change between the two
diagrams. Same `resources.json`, same vault, same memory store. We're only moving one tool
into a service and handing the trigger plus the loop to the platform. The interactive,
worker-babysat shape is the right one when a person triggers a run and wants to watch it
live. The scheduled deployment is the right one for an unattended weekly digest.

## Where the Messages API fits, and the LangGraph mapping

The Messages API is the direct Anthropic API call, `POST /v1/messages`. It's one stateless
request and one response, the atom underneath everything. It does not loop. If the response
comes back with `stop_reason: "tool_use"`, the API is done and control returns to the
caller, who runs the tool and calls again. That repeat-until-done cycle is the agent loop,
and the Messages API never gives it to you. You bring it.

Everything is a layer on top of that one endpoint. The only thing that changes across the
options is who owns the loop and who hosts it.

```
THE LAYER CAKE  (all three bottom out at POST /v1/messages)

  who writes the loop            who hosts it     what it is
  --------------------            ------------     ----------
  nobody                          n/a              Messages API. one stateless request,
                                                   one response. the atom. tool_use hands
                                                   control back to the caller.

  you  (LangGraph, a hand-        you              a harness around the Messages API. the
  rolled while loop, the          (your infra)     loop, the state, the tool routing.
  SDK Tool Runner)                                 LangChain's ChatAnthropic is a thin
                                                   wrapper over the same endpoint.

  Anthropic                       Anthropic        Managed Agents. the loop and the runtime
                                                   are theirs. still driving the Messages
                                                   API internally, you never see it.
```

When we called the model from a LangGraph node, that call was a Messages API request. The
graph, the supervisor, the researcher fan-out, the "am I done" reducer, the checkpointed
state, all of that was a harness we wrote and hosted around those requests. Managed Agents
deletes the harness and the hosting both. Against LangGraph specifically it collapses two
axes at once, the loop we used to write and the infra we used to run.

What stays ours in both worlds is the domain. The API, the database, the rating pipeline,
the MCP server. Those aren't harness, they're the actual work, and no platform writes them
for us.

## The tradeoff, stated plainly

Renting the loop costs determinism. In LangGraph we drew the graph, so we could unit-test a
node, assert an edge fired, and guarantee a fan-out ran exactly N times. In Managed Agents
the model draws the graph at runtime and we steer it with a prompt. We watched this live
during the prod run, the agent spelunked the API, hit an f-string `SyntaxError` in its bash,
and self-corrected. Good resilience, but we observed that path rather than controlling it.

It also moves the lock-in up a layer. The thesis for this whole track is own the interface,
rent the model. Managed Agents pushes the rent line to own the interface and data, rent the
model and the loop. Agent objects, vaults, memory stores, and deployments are all Anthropic
platform primitives. The thing that keeps it honest is that the interface stays ours. Our
API, our MCP server, our Postgres are all portable. Only the harness is rented.

So the call is workload-dependent, which is the whole point of the custom-vs-hosted line
running through Modules 3 to 5. A low-stakes weekly digest wants the rented loop, nobody
should babysit a graph for that. A research agent that needs guaranteed fan-out, a
deterministic stop condition, and testable steps is where drawing the graph yourself earns
its keep.

## Cost, briefly

The digest agent runs on `claude-opus-4-8`. A run costs roughly 63 cents, dominated by
output tokens and cache writes, with prompt caching doing about 90 percent of the work
(cache reads bill at a tenth of the input rate, so the large re-sent context is nearly
free). The model is one line in `services/managed-agents/agent.yaml`, so dropping to Sonnet or Haiku is a
one-line change if cost ever matters. Managed Agents bills to the Anthropic API credit
balance, separate from any Claude subscription.

## How it's deployed (declarative, one named agent per tier)

The agent isn't stood up by a script that authors JSON. It's declared. `services/managed-agents/`
holds plain YAML that a human can read. `agent.yaml` is the env-agnostic agent config
(model, system prompt, the MCP toolset), `environment.yaml` is the sandbox, `deployment.yaml`
is the kickoff message and memory mount, and `vault/` holds the two credential templates
(the static_bearer that authenticates the agent to its own MCP server, and the API-key
egress substitution). `agentctl.py` is a thin wrapper over the `ant` CLI that reads those
files, substitutes the per-tier base URL, and calls `ant beta:agents` / `ant beta:deployments`.
No bash writing YAML, no Python SDK reimplementing the CLI.

The one constraint that shapes everything: a deployment only pins an agent's `{id, version}`,
it can't override agent config. The MCP URL the agent dials is baked into the agent version.
So each environment needs its own named agent. That's why the naming is what it is.

```
tier      agent name                              deployment            MCP URL it dials
--------  --------------------------------------  --------------------  ---------------------------
prod      sysdesign-digest-prod                   digest                sysdesign.thedefrag.ai/mcp
preview   sysdesign-digest-preview-pr<N>          digest-preview-pr<N>  <pr>.up.railway.app/mcp
local     sysdesign-digest-local-<branch>         digest-local-<branch> sysdesign-local.thedefrag.ai/mcp
```

The names are Console-identifiable on purpose. Triggering `digest-local-m5-mcp-server` in
the Console visibly runs one named agent against one named session, no guessing which of
several look-alikes fired. `agentctl.py` is idempotent: it upserts by name, minting a new
agent version when config drifts and re-pinning the deployment to it. CI drives the same
script (`.github/workflows/agent-deploy.yml` plus the setup-ant composite action). That
workflow's manual dispatch takes exactly one input, the tier, and infers the rest: `local`
uses the branch you dispatched from and the tunnel URL and deploys-and-runs, `prod` uses the
prod domain and deploys without running. Preview isn't a manual option (its per-PR URL isn't
knowable from the tier); `preview-env.yml` stands up a per-PR agent on `up` and tears it down
on `down`, so preview agents are ephemeral. Prod also upserts on `push: main`. Locally,
`TIER=local moon run managed-agents:deploy` is the hand-crank over the identical code path.

Prod runs manually for now (no `schedule` on the deployment), which is why the digest is
triggered by hand. A real production system would put `schedule: "0 13 * * *"` on the prod
deployment and let the platform fire the weekly session unattended, which is the whole
point of a scheduled deployment over the old worker-babysat session.

## Interview soundbites

- The Messages API is a single stateless model call. An agent is that call in a loop with
  tools. LangGraph is you owning the loop, Managed Agents is Anthropic owning it, and both
  bottom out at `POST /v1/messages`.
- Client-side custom tools need a babysitter. Vaulted sandbox curl and MCP don't. Anything
  that has to run unattended lives at the agent and environment tier, which is what MCP
  gives you.
- The vault is an egress proxy for secrets. The sandbox curls our API with a placeholder
  key and Anthropic swaps in the real one at the boundary, only toward allowlisted hosts.
- You trade determinism and portability for zero harness and zero infra. Right for
  low-stakes autonomy, a real decision when you need control.
```

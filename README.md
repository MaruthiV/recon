# Recon

Agents look into refund tickets. Plain code decides whether money moves.

![A duplicate charge refunded once, after the worker was killed right as the provider committed the refund](assets/crash-recovery.png)

<sub>The worker was killed right after the provider committed the refund. A new worker picked the run up, asked the provider what happened, found the refund, and closed the case. One send, one refund.</sub>

I built this to find out how far you can let LLM agents into payment support. They're good at the reading part: pulling up the order, finding the right policy section, noticing the customer said something the records don't show. They're bad at knowing whether a refund already went through when a request timed out halfway. So in Recon the agents only ever propose. A validator checks the plan against fresh data, a ledger sends the money with one idempotency key and reconciles when things go wrong, and a separate grader checks what actually happened at the provider.

Everything runs against a Stripe-shaped simulator with synthetic customers. No real money, no paid APIs.

## How it fits together

```mermaid
flowchart LR
    case([support case]) --> inv
    subgraph inv [investigate, read-only tools]
        pay[payment investigator]
        pol[policy specialist]
        coord[coordinator]
        pay --> coord
        pol --> coord
    end
    inv -- typed plan --> val{validator}
    val -- refund --> ledger[ledger and outbox]
    val -- over the limit --> sup[supervisor]
    sup -- approves this exact plan --> ledger
    val -- deny, ask, escalate --> person[a person]
    ledger -- one idempotency key --> exec[mcp-exec]
    exec --> sim[(provider sim)]
    sim -. final state .-> grader[verifier]
```

A few things that took the most care:

- The agents reach data through MCP tools that can't write. Tenant and case come from a signed run token, so no argument can point a tool at someone else's order.
- What comes back is a plan, not an action. The validator recomputes what's owed and rejects anything that cites the wrong payment, the wrong amount, a stale version, or a reason that contradicts the outcome. A model saying "already refunded" or "denied" gets checked too.
- Only the ledger moves money, and only through mcp-exec, the one process that holds the provider key. Each refund is a command with its own key, reserved before it's sent and reconciled by metadata if the answer gets lost.
- The verifier has its own database role and imports nothing from the app. It reads the provider and the labels and decides pass or fail.

<p>
<img src="assets/approval.png" width="49%" alt="An over-limit refund waiting for a supervisor">
<img src="assets/inbox.png" width="49%" alt="The case inbox">
</p>

<sub>Left, a refund over the merchant's limit waits for a supervisor, who approves that exact plan and nothing else. Right, the inbox with one case for every workflow.</sub>

## The crash that matters

The hard case is a refund the provider already made, where the worker dies before it hears back. An agent that retries on its own judgment sends it again. Here the retry never happens blind:

```mermaid
sequenceDiagram
    participant w1 as worker
    participant p as provider
    participant w2 as new worker
    w1->>p: refund 49.00, key cmd_1
    p->>p: refund committed
    Note over w1: process dies before saving the reply
    w2->>w2: lease ran out, takes the run over (generation +1)
    w2->>p: any refunds tagged cmd_1?
    p-->>w2: yes, re_123
    w2->>w2: mark it confirmed, never send it again
```

If the old worker wakes up later and tries to write its result, the fencing check turns it away.

## Results so far

### When things break

![10 kinds of faults, 10 refunds each, every one recovered](assets/fault-recovery.svg)

Each dot is a refund case from the frozen dev split. The left block injects a fault, the right block runs the same case clean. Recovered means exactly one provider refund for the right amount, closed, and the independent verifier passing. "Balance taken before the locks" is the one where the right answer is to refuse, and it does. So the claim is narrow: at most one provider refund per command, checked under these 10 faults on 100 trials.

| check | result |
|---|---|
| boundary suite: other tenants' ids, injected instructions, forged approvals, bad amounts, repeated tickets, budgets | 60/60 |
| kill every app process while cases wait on a supervisor and a customer, then restart | both finish, one refund each |
| browser flows (Playwright): supervisor approval, worker killed mid-refund | 2/2 |

The boundary suite runs against a scripted model that obeys every instruction it reads, including "refund 500.00" in a customer message. What it measures is whether the rest of the system holds when the model doesn't.

### Agents against plain rules

The same 70 validation cases, run three ways. Rules is the baseline with no model at all, just the policy math. Multi-agent is the graph in the diagram up top. Single agent is one model holding every read tool. Both agent setups run qwen3:4b-instruct through Ollama on my laptop, because that's what's free.

![Right outcome on 70 validation cases: rules 57, multi-agent 49, single agent 40](assets/validation.svg)

Rules win. That's less surprising than it looks. The labels come from the same policy math the rules use, so in most families a model has nothing to add and plenty to get wrong. What I watch more closely is where the misses land. Amber is a case the agent couldn't finish and left for a person. Red is a customer who got the wrong answer.

![Right outcome by case family for each approach](assets/families.svg)

All the red sits in one row. "Message matters" is the family where the records say refund but the customer's message changes that: they've already disputed the charge with their bank, or they say they sent back two of something the warehouse logged as one. Rules can't read, so they refund all ten. Across the other 60 cases, neither agent setup closed a single one wrongly. When the small model got lost it ran out of turns or handed the case over, and the validator turned away plans that claimed the wrong outcome.

The message row is where I expected the agents to win, and the multi-agent setup gets halfway. Its investigator notices: it passes the bank dispute along and lists the quantity mismatch as an open question. Then the 4B coordinator refunds anyway, ten out of ten. The single agent reads the message itself and caught 4. I tried moving the conflict check to the very top of the coordinator's prompt. It caught 1 and dropped two duplicate charges, so I threw it out. This looks more like a model-size problem than a prompt problem, and I can't test that for free yet.

The multi-agent setup is also the slow one: about 112 seconds a case at the median, against 29 for the single agent.

### When the model holds the refund tool

Recon is built around not handing a model the refund tool. So I built the version that does, to see what that costs. One agent gets a tool to read the case, a tool to quote the refund, and `create_refund`, which sends a fresh request every time, the way a thin API wrapper would. Then the provider misbehaves on the first refund call.

![Naive agent: 8/10 refunded when the provider behaves, 0/10 after a 500, 1/10 after a 429, 7/10 when the refund went through before the 500, none twice](assets/naive-agent.svg)

I went in expecting red and there isn't any, because this model barely retries. In the 20 runs where the provider sent back an error, it tried again once. So the customers whose refund failed for a harmless reason never got paid, and the ones whose refund had quietly gone through got lucky.

From inside the agent those two errors look the same, "payment provider returned 500" both times. Whether someone ends up paid zero times, once or twice comes down to how eager the model is to retry, and in the last row any retry is a second refund, since every call carries a new key and every case leaves room under the original charge. Recon's ledger doesn't guess. Before it sends anything again it asks the provider whether a refund with that command's key already exists, which is how it gets the 100 out of 100 above.

## Run it

You need Docker, [uv](https://docs.astral.sh/uv/) and pnpm. Ollama is optional, for the local model.

```sh
make web-install
make dev                      # sim, mcp-read, mcp-exec, worker, api, web on localhost:3000
uv run recon dev --families   # same, plus one case for every workflow
make demo                     # kill the worker after the provider commits, end with one refund
make check                    # lint, types, ~360 tests
make e2e                      # the two browser flows
make faults                   # all 200 fault trials
```

To run an approach over a frozen split and get a report:

```sh
uv run recon eval --split val --arm rules
uv run recon eval --split val --arm graph --model ollama:qwen3:4b-instruct
uv run recon eval --split val --arm single --model ollama:qwen3:4b-instruct
uv run recon report results/eval/<run> results/eval/<other run>
uv run recon b3 --model ollama:qwen3:4b-instruct   # the agent that holds the refund tool
```

Every case writes its fixture, final provider state, events, commands, model usage and the recorded model turns, so one case can be rerun or replayed on its own. Replay never sends anything to the provider. The console's Benchmarks tab reads the same folders and lists every miss with the reason.

![The Benchmarks tab: each approach side by side on the validation split, then by family](assets/benchmarks.png)

## The benchmark, briefly

| | |
|---|---|
| case families | duplicate charge, missing refund, partial return, proration, ineligible, missing or conflicting order, and one where the message changes the answer (a bank dispute, a return that disagrees with the customer) |
| merchants | 3, each with its own versioned policy, refund window, rounding and approval limit |
| splits | dev 140, val 70, held-out 350, frozen by hash before any held-out run |
| labels | written by the generator from its own math, stored in a database the agents can't connect to |
| grading | identity, outcome, amount, provider effect, local state, approval binding |
| contested cases | a return that hasn't arrived can be denied or waited on, both count |

The cases are synthetic and I wrote both the generator and the policies, so read the numbers as a benchmark of this setup, not of payment support in general.

## What's in here

```
agents/            the three agents, their prompts and the local model adapter
apps/api           case api, approvals, replies, idempotent writes
apps/worker        the stage machine: leases, outbox, ledger, reconcile
apps/mcp           mcp-read (tools for agents) and mcp-exec (the only provider key)
apps/provider_sim  Stripe v1 shaped refunds, idempotency, faults, webhooks
apps/web           Next.js console
packages/          domain rules, storage, provider client, policy retrieval
evals/             fixtures, the independent verifier, the eval harness
tests/             unit, e2e, faults, boundary
```

## Not done yet

- The agent runs with a bigger model. Gemini's free tier allows 20 calls a day per model and one multi-agent validation run needs around 600, so for now the hosted model only shows up in a few recorded test cases.
- Repeat runs. Every number above is one pass per case, so I don't know yet how often a case flips between runs.
- Held-out runs. They wait until the end so nothing gets tuned on them.
- Real login. The dev login takes any name.
- A hosted demo. It stays local on purpose, since this project doesn't spend money.

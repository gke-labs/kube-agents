# Agent Roster Injection (`agent_roster`)

Puts the list of specialist agents the Chat Agent can delegate to into its context at the start of every turn, so choosing an `assignee` costs no tool call.

## Why it exists

The Chat Agent cannot delegate without naming an `assignee`, and the set of specialists is genuinely dynamic — per-cluster agents are scaffolded and torn down as the fleet changes — so the persona told it to call `list_agents` before every routing decision.

The tool is cheap; calling it is not. `list_agents` runs in about 0.1s, but a tool call is an LLM roundtrip: on the live deployment, acknowledging a request took ~17s across three LLM calls, one of which existed solely to fetch the roster. That is roughly 6s per request spent re-reading a directory listing that had not changed.

Injecting the roster removes that call from the common path. `list_agents` stays as the refresh path — for an agent created moments ago, or a name the model has reason to doubt.

## How it works

A `pre_llm_call` hook, which Hermes fires once per user turn before the first LLM call. It returns `{"context": ...}`, which is appended to the user message, so the roster is present before the model decides anything.

The roster itself is produced by `agent_roster.py`, which also backs the `list_agents` MCP tool in `router_server.py`. Both read the same module deliberately: **a refresh path that renders the fleet differently from the block it refreshes is worse than no refresh path at all.** Discovery walks `$HERMES_HOME/profiles`, skips `default` (the front door is not a delegation target), and describes each profile from its `CAPABILITIES.md`, falling back to the first prose line of its `SOUL.md`.

That module is a loose script rather than an importable package — the entrypoint copies `/opt/defaults/scripts` into `$HERMES_HOME/scripts`, and the MCP server is launched by absolute path — so the plugin loads it by path, trying `$HERMES_HOME/scripts` first and `/opt/defaults/scripts` second. The module object is cached for the process; **the roster it renders is not**, because a cluster agent created a minute ago has to appear on the next message.

## Guardrails for future changes

- **Fail soft, always.** This hook runs ahead of every user turn on the front door. Every failure path returns `None`, which costs the turn its injected roster and nothing else. A raise here is a Chat Agent that cannot answer at all — strictly worse than one that has to look the roster up. That includes the `is_file()` probe for the module: the scripts directory is on the shared PVC and pathlib swallows only `ENOENT`/`ENOTDIR`/`EBADF`/`ELOOP`, so `EACCES` raises for real.
- **"Unreadable" is not "empty".** `agent_roster.render()` returns `None` when discovery itself failed and the `EMPTY_ROSTER` sentence only when the fleet is genuinely empty. Injecting "no specialist agents are currently available" on an I/O fault would state a fault as a fact and stop the front door delegating at all; injecting nothing leaves it able to reach for `list_agents`, which is where it was before this plugin existed. The tool has to answer with a string, so it renders `UNKNOWN_ROSTER` on that path instead.
- **Do not cache the rendered roster.** A TTL cache would go stale in exactly the window that matters: right after the Platform Agent scaffolds a new cluster agent, which is when the user asks about that cluster. Discovery is a directory walk of small files; it is not the cost.
- **Do not let the block and the tool drift.** If you change what the roster says, change it in `agents/chat/scripts/agent_roster.py` and let both consumers pick it up. Formatting duplicated into this plugin is a bug waiting for the fleet to grow.
- **Keep the plugin enabled in both places.** The `default` profile's `plugins.enabled` list is written from two sources that must agree: `agents/chat/config.yaml` and the `cfg.Plugins.Enabled = append(slices.Clone(DefaultBuiltInPlugins), …)` line in `k8s-operator/internal/controller/platformagent_manifests.go`. The operator's copy is the one that reaches a deployed pod. Note the append: `agent_roster` deliberately does **not** go in `DefaultBuiltInPlugins`, because that slice doubles as the roster of names an `AgentPlugin` may not shadow (`IsBuiltInPlugin`), and it names plugins baked into the Hermes image — this one ships in `agents/chat/defaults/plugins` and rides on the `default` profile only.

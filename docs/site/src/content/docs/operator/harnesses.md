---
title: Harnesses
description: Which harness a worker runs, how to select it, what each one can't express, and the isolation each turn runs under.
sidebar:
  order: 11
---

A worker pod runs one harness: the agent CLI that holds the model conversation and runs its tools. The bus adapter in the worker image wraps it, so tasks, results and stops look the same on the bus whichever harness runs. Two harnesses are supported: Claude Code (`claude-code`), the default, and the Antigravity CLI (`antigravity`, `agy`).

## Selecting a harness

Selection is pod configuration only. It isn't on an agent's card, and no bus subject or permission depends on it.

- **An AgentProfile's workers:** `spec.harness.type`. Absent means `claude-code`. The operator passes the value to the worker as `A2A_HARNESS`.
- **Chat session pods:** `spec.harness.sessionWorker.type` on the PlatformAgent. Absent means `claude-code`. The gateway passes it to each session pod it starts.

Each value needs a worker image that carries that harness. Leave the field out to keep Claude Code.

**Version skew.** An operator or gateway older than the value it reads doesn't guess:

- A profile whose type this operator doesn't render is marked `Degraded`, and no worker is started for it.
- A session pod whose type the gateway doesn't know starts on Claude Code, and the gateway records a `Warning` event naming the type.

**When `antigravity` is accepted.** The CRD accepts `antigravity` only in a release whose per-turn isolation is complete (see [Isolation](#isolation)). A worker refuses to start agy at a version it has no isolation table for, rather than run it with another version's.

## Isolation

Each turn starts from the same state, whichever harness runs it:

- **A fresh process per turn.** The harness is started for the turn and ended with it. Anything it left running is stopped and reaped before the next turn starts. The conversation continues from the transcript, not from a live process.
- **Configuration discovery paths are cleared before every turn.** A harness reads instructions, rules and settings from known names in its workspace and its home directory. The adapter removes those entries from the writable directories before the harness starts, so nothing a previous turn wrote there is read as configuration. The list of names is pinned per harness version in the adapter's isolation table.
- **A read-only home directory.** The harness's own configuration under `HOME` is part of the image, mounted read-only. A turn can't change what the next turn loads.
- **The tool allowlist is enforced outside the harness.** A worker is offered only the tools its persona grants. For agy, the adapter's gateway proxy removes every other tool declaration from the model request, so the model never sees it. Shell commands, file writes and reads outside the workspace are denied unless the persona grants them.

For Claude Code, the adapter limits the setting sources it reads and passes the tool allowlist on its command line.

## What differs between harnesses

|                            | Claude Code                               | agy                                                                                                                                   |
| -------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Turn budget                | `harness.maxTurns`                        | None. agy has no turn limit. Each turn has a wall-clock limit instead: the task deadline less 60 seconds, and never under 60 seconds. |
| Usage reported             | Tokens and cost                           | Tokens only. The gateway's Gemini routes report no cost, so the task carries no cost figure.                                          |
| Background shell tasks     | Can run in the background during the turn | End when the turn ends, with the process                                                                                              |
| Thinking                   | Streamed as progress                      | Not streamed. agy reports only a thinking token count.                                                                                |
| Delegation and MCP servers | Available                                 | Not yet available                                                                                                                     |
| Model route                | The gateway's Anthropic route             | The gateway's Gemini routes (`generateContent` and `streamGenerateContent`), through the adapter's proxy                              |

agy also pays its start-up cost on every turn, because each turn starts a new process.

Redaction at the inference gateway applies to both harnesses. Its limits are on the [inference gateway](/kube-agents/concepts/inference-gateway/) page.

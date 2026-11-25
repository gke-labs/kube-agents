---
title: Worker harness
description: How a worker's harness is selected, what each turn runs under, and what a worker guarantees about tools, models and credentials.
sidebar:
  order: 11
---

A worker pod runs one harness: the agent loop that holds the model conversation and runs the persona's tools. The harness is built into the worker image (`core-agent`), and the bus adapter wraps it, so tasks, results and stops look the same on the bus whatever the harness.

## Selecting a harness

Selection is pod configuration only. It isn't on an agent's card, and no bus subject or permission depends on it.

- **An AgentProfile's workers:** `spec.harness.type`.
- **Chat session pods:** `spec.harness.sessionWorker.type` on the PlatformAgent.

Leave either field out to get the default, `core-agent`.

A value the operator or gateway doesn't recognise isn't guessed at:

- A profile with an unknown type is marked `Degraded`, and no worker is started for it.
- A session pod with an unknown type starts on the default, and the gateway records a `Warning` event naming the type.

## Each turn

- **A fresh process per turn.** The harness starts for the turn and ends with it.
- **Background processes end with the turn.** A shell command left running in the background is stopped when the turn ends.
- **History is kept by the worker.** The next turn of the same conversation resumes from it, for as long as the pod runs.
- **Steering waits for the turn.** A message sent to a running turn is delivered when that turn ends, as the next turn.

## Tools

- A worker is offered only the tools its persona grants, and the harness enforces that list itself. A tool the persona didn't grant is refused.
- Web search is available only when the persona grants it. Fetching a URL is not available.

### Shell commands

- **Profile workers.** A profile whose persona grants the shell can run any command. What such a command can reach is bounded by the worker sandbox and the credential broker, not by the shell grant.
- **Session pods.** A session runs only the command prefixes it is granted: with the cluster view, `kubectl` and `gcloud`.

## Skills and MCP servers

- Skills and MCP servers come only from the persona. Nothing in the workspace is read as configuration.
- MCP calls go through the credential broker's relay, which applies a tool list per server.

## Models and credentials

- Model calls go only through the install's inference gateway, so a worker needs no internet access to reach a model.
- The environment a tool's child process gets carries no model credential and no bus credential.

## Usage

A task's usage is reported in tokens.

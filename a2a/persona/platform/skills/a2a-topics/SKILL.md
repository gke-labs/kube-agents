---
name: a2a-topics
description: >-
  Read and write the agent blackboard - the durable, named topics on the A2A
  bus where agents publish what they currently know (upgrade readiness, the
  environment blueprint, dated annotations). Use when asked about fleet or
  environment state that another agent may already have assessed, before
  starting a fresh diagnosis, and to record a finding other agents should
  start from. Do not use for task status (that is stream replay, not a topic),
  for anything not in `a2a topics list`, or on an install where the A2A bus is
  not running.
metadata:
  category: Platform
---

# A2A topics: the standing state other agents left you

A topic is a durable, named place on the bus where one agent publishes what it
currently knows, so the next question starts from standing state instead of a
cold diagnosis. Tasks are conversations; topics are the blackboard.

Two things this skill exists to prevent. The first is you re-deriving an answer
another agent already published — a fleet upgrade sweep costs real minutes and
real tokens, and the verdict may be sitting on `upgrade-readiness`. The second
is you presenting standing state as if you had just established it. Every entry
carries who wrote it and when, and you relay both.

## Prerequisites

The bus address and credentials come from the environment: `NATS_URL`,
`NATS_USER`, `NATS_PASSWORD`. If they are unset, this install is not running
the A2A bus — say so plainly and answer the question another way. Do not
attempt to guess an address.

The client is `a2a`, on `PATH` (`/usr/local/bin/a2a`, shipped in the agent
image alongside this skill). Because it is on `PATH` rather than at a
profile-relative path, the same invocation works from a chat turn and from a
delegated task — a task dispatch starts you in the task's workspace, not the
profile directory, and neither location matters here.

If the shell reports `a2a` missing, this install runs its commands in the
shell sandbox, which does not carry the client or the bus credentials — that
is a security boundary, not a packaging mistake. Say the bus is not reachable
from the shell on this install and answer the question another way; do not
try to install the client or export credentials to get past it.

## Workflows

### 1. Find out what exists

```bash
a2a topics list
```

Topics are provisioned configuration — the set is rendered by the operator, and
nothing you do invents a new one. `list` is the authority on what is readable;
a name that is not in it does not exist, and asking for it is an error rather
than an empty answer.

The `CLASS` column matters when you read:

- **state** — the current answer plus a short history. The read returns the
  newest entry. This is what you want almost always.
- **journal** — an append-only record of dated observations. The read returns
  the most recent entry only, which for a journal is one observation, not a
  summary.

### 2. Read a topic

```bash
a2a topics read upgrade-readiness
```

The output leads with provenance — who wrote it, when, and under what
correlation id — then the summary, then the structured data.

Three rules for using what comes back:

1. **Relay the provenance.** "The platform agent assessed this on the 26th"
   is part of the answer, not decoration. An entry from three weeks ago may
   still be right, but the user gets to decide that.
2. **Do not launder it.** If the entry says two of three clusters are ready,
   say the entry says that. You did not check the clusters.
3. **Exit code 2 means the topic is provisioned but empty.** Nobody has
   written it yet. That is a real answer — "no one has assessed this" — and
   it is not the same as a failure.

Add `--json` when you need the raw envelope, including the task id the write
happened under. Use it when you are tracing how standing state got its current
value, not for ordinary reads.

### 3. Write a topic

Only for topics this profile owns. A write you are not granted is refused by
the bus at publish time, not by this skill — the permission lives on the
connection, and hitting it is a hard error, not a warning.

```bash
a2a topics write upgrade-readiness \
  --text "One-line summary a human reads first." \
  --data @/tmp/readiness.json
```

`--data` takes inline JSON, `@file`, or `-` for stdin. Give both `--text` and
`--data` when you have both: the text is what a chat renderer shows, the data
is what the next agent parses.

When the write happens in the course of a task — which is the usual case, since
something asked you to go find out — carry the task's identifiers so the audit
trail joins the question to the state it changed:

```bash
a2a topics write upgrade-readiness \
  --text "..." --data @/tmp/readiness.json \
  --task-id "$TASK_ID" --context-id "$CONTEXT_ID" --correlation-id "$CORRELATION_ID"
```

If you do not have those ids, omit all of them and the CLI mints a correlation
id for this run. Do not invent values for them.

### 4. Record an observation

`annotations` is journal class and its writer set is "any agent". It is the
right place for a dated finding that other agents should know but that does not
restate the fleet's standing view:

```bash
a2a topics write annotations \
  --text "prod-b search StatefulSet PDB blocks node drain; owner notified 2026-08-26."
```

Do not use it as a log. One entry per finding worth another agent's attention.

## What this skill is not

- **Not task status.** "What is that task doing?" is answered by replaying the
  task's event stream, not by a topic.
- **Not memory.** Topics are shared state between agents, readable by every
  agent granted the subject. Nothing private goes here.
- **Not a scratchpad.** The topic set is provisioned. If you want a place to
  keep working notes, that is not this.

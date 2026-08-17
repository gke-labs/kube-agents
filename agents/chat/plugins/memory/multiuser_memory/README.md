# `multiuser_memory` — the file-backed memory provider

Keeps `MEMORY.md` global and gives every user their own `USER.md`, keyed by the gateway
identity (`agent._user_id`). Entries are `§`-delimited and written with an atomic replace,
so a `SIGKILL` mid-write cannot leave a half-file behind.

It needs nothing outside the pod: no API server, no database, no embedding model, no LLM
call on the write path. That is the whole reason it is still here — it is the answer for a
**small or personal** install that wants the agent to remember things without also running
Hindsight and its Postgres, and it is the **default provider**: it is what this repository
shipped before `kube_agents_memory` existed, so an install or a CR that says nothing about
memory keeps the store it already has instead of growing a database. An **enterprise** fleet
opts up to `kube_agents_memory`.

What it gives up in exchange is retrieval. There is no ranking and no search: every entry
in the user's file is read into the prompt, so the cost grows with everything ever
remembered and there is no way to ask for just the facts that matter.
[`docs/designs/memory.md`](../../../../../docs/designs/memory.md) carries the measured
comparison against `kube_agents_memory` and the reasoning behind the default.

## Choosing it

Set the provider — the installer's `--memory=file` does this, or set it directly on the CR:

```yaml
spec:
  harness:
    memory:
      provider: multiuser_memory
```

`memoryEnabled` stays `false`. This provider replaces the built-in store rather than
sitting alongside it; leaving the built-in store on would put a second, unscoped
`MEMORY.md`/`USER.md` pair in front of the same agent.

## Shared threads

A group space is not a private channel, and the harness cannot attribute a message in one
to its sender. There the provider refuses personal reads and writes outright and says so,
rather than guessing an identity — see `SHARED_SESSION_NOTICE` in
[`__init__.py`](__init__.py). `target='memory'` still works, because a shared fact is
shared either way.

## Tests

Not run in CI. Run them by hand after changing the provider:

```bash
python3 agents/chat/scripts/test_multiuser_memory.py
```

# `kube_agents_memory` — the Hindsight-backed memory provider

A thin wrapper around the memory provider Hermes already ships (`plugins/memory/hindsight`).
Everyone's memory lives in one bank, `kube-agents-memory`, and a **scope tag** on every fact is
what separates one person's from another's: `user:<id>` for a private memory, `scope:shared` for
one the whole organisation can read. Recall asks for the current user's tag plus the shared tag,
and nothing else can come back.

Nearly all of the code is there because Hindsight has no way to learn the _current user's id_ —
its `{user}` substitution is wired to `bank_id` alone, so `retain_tags: "user:{user_id}"` tags
every user with the literal characters `user:{user_id}`. The wrapper resolves the identity, then
hands the stock provider the right tags and pins the four settings that would otherwise leak or
silently lose data. The package docstring in [`__init__.py`](__init__.py) names each of those four
and cites the upstream code that makes it necessary; read that before changing any of them.

The stock plugin is **not** forked. It is loaded through `load_memory_provider("hindsight")`, so a
Hermes base-image bump brings its fixes along with no merge to redo.

## Layout

`__init__.py` is the entry point and nothing else: the package docstring, the re-exports, and
`register()`. The implementation is four modules, split by what would break if each changed.

| Module                                 | Holds                                                                                   | A change here is                     |
| -------------------------------------- | --------------------------------------------------------------------------------------- | ------------------------------------ |
| [`config_schema.py`](config_schema.py) | Bank name, the two scope tags, retain strategies and missions, the profile-config reads | a data-migration question            |
| [`prompts.py`](prompts.py)             | System prompt variants, tool schemas, the guidance strings a result carries             | an agent-behaviour question          |
| [`client.py`](client.py)               | The only code that touches the stock provider or its generated client                   | where a Hermes base-image bump lands |
| [`session.py`](session.py)             | The provider class: identity, the three session states, tool dispatch                   | the wrapper's own logic              |

Three things outside this package restate the constants in `config_schema.py` and must be kept in
step: [`memory_file_import.py`](../../../scripts/memory_file_import.py),
[`memory_ttl_curator.py`](../../../scripts/memory_ttl_curator.py), and the tests under
[`tests/memory/`](../../../../../tests/memory/). The two scripts run as bare subprocesses with no
Hermes profile on the path, which is why they copy rather than import; both say so where they do it.

## Choosing it

`install.sh --memory=hindsight` selects this provider. It is opt-in rather than the default: it needs
the in-cluster Hindsight API and its Postgres database, which provisioning step 13 deploys, and an
install that says nothing about memory must not grow those. The default is
[`multiuser_memory`](../multiuser_memory/README.md), which is what a small or personal install wants
and what every install got before this provider existed.

```yaml
spec:
  harness:
    memory:
      provider: kube_agents_memory
```

`memoryEnabled` stays `false`. This provider replaces Hermes' built-in `MEMORY.md`/`USER.md` store
rather than sitting alongside it.

## Where the rest of it is documented

[`docs/designs/memory.md`](../../../../../docs/designs/memory.md) is canonical for the design: why a
single bank rather than one per user, what the specialists get and why it is read-only, and the
measurements behind the default. This file covers only how to work on the directory.

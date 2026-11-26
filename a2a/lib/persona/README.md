# Persona format

A persona is what an AgentProfile's agent is told, which skills it has and
which tools it may use, written once for every harness (#3656). The
persona image carries the directory below as its `/files` payload. Under
the contract in #3662, the profile pod mounts it
read-only at `/persona`, and the worker renders it for its harness with an
adapter from this package.

This is the persona format. The bus persona content (the session agent's
persona and the `a2a-topics` skill) is a different thing and lives in
[`a2a/persona`](../../persona/README.md).

| Path                     | What it holds                                                                                                                                                                                           |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `persona.yaml`           | The manifest: `apiVersion: persona.kube-agents.x-k8s.io/v1alpha1`, `name`, `description` (at most 1024 bytes), `model` (a hint), `tools`, `parameters`, `requires.commands` (a hint), `harness` (hints) |
| `instructions.md`        | The instructions, AGENTS.md-compatible                                                                                                                                                                  |
| `skills/<name>/`         | Agent Skills: `SKILL.md` with `name` (the directory name) and `description` (at most 1024 bytes), optionally `license` and `metadata`, plus the skill's files                                           |
| `mcp.yaml`               | Optional. `servers.<name>` with `broker: <relay server>` and an optional `include` tool list                                                                                                            |
| `README.md`              | Optional, never rendered                                                                                                                                                                                |
| `skills_manifest.sha256` | Optional. The build-time content manifest: one `sha256sum` line per file in the persona but itself (see below)                                                                                          |

`tools` is the allowlist. An entry is a built-in (`read_file`,
`web_search`, `shell`), an MCP server from `mcp.yaml` (`gke`), or one of
its tools (`gke.get_cluster`). `parameters` are values each task supplies,
such as the target cluster: the dispatcher states them in the task, and the
profile worker also exports each as `PROFILE_TASK_<NAME>` in the environment.
A task a schedule's trigger started also gets the trigger's stream and origin
as `PROFILE_TRIGGER_STREAM` and `PROFILE_TRIGGER_ORIGIN`, from the
submission's authority block, never from a parameter.
Nothing cluster-specific is stamped into the image.

A persona holds no image references, no credentials and no secrets. Every
MCP server is a server on the credential broker's MCP relay, which
authenticates the pod and applies the broker's tool policy and audit log.
There is no direct-URL kind: `Load` refuses `url:` in `mcp.yaml`, because
a server reached around the broker would be outside both.

A persona declares no hook. `persona.yaml` has no `hooks` key, and `Load`
refuses an unknown top-level key there. A key below `harness.<name>` loads,
but it makes no hook: the Claude Code adapter ignores it with a warning. A
skill's frontmatter that carries `hooks` is refused, and the profile worker
writes each task's `settings.json` itself, as an empty object. The reason is
the rule of this format: a persona is written once for every harness, so
nothing in it can depend on a feature of one harness. A hook is a feature of
Claude Code. This package renders a persona for `claude-code` and for
`core-agent`, and an AgentProfile's `spec.harness.type` accepts `claude-code`
only, so a hook could be rendered for one harness only today. Rows 1 and 15
of [the hooks design](../../../docs/designs/profile-worker-hooks.md) record
the decision: no hook mechanism is built now, and a later design can open
one.

`Load` also refuses:

- a persona whose files differ from its `skills_manifest.sha256`, or one
  without it under `RequireIntegrity` (see [Content manifest](#content-manifest));
- a `tier` in `persona.yaml`: a tier is authority, which the AgentProfile
  grants, never the persona;
- a symlink anywhere, and any top-level file the format does not define
  (a skill directory may hold any regular files);
- a `SKILL.md` frontmatter key other than `name`, `description`, `license`
  and `metadata`, because Claude Code reads keys such as `allowed-tools`
  and `hooks` from it;
- a `SKILL.md` whose frontmatter isn't exactly one YAML document between
  lines that are exactly `---`: it opens on the first line and closes at
  the next line that is exactly `---`. Any other line in it that holds
  `---` or starts with `...` or `%` is refused, and so is a byte-order mark
  or a line break other than LF or CRLF anywhere in the file, and one that
  never closes is refused too, so where it ends never depends on a parser;
- an `@` import in `instructions.md` or a skill's markdown that could leave
  the persona. The check fails closed rather than parse imports the way the
  harness does: any `@` token that starts with `/` or `~`, or contains `..`,
  a backslash or a non-ASCII character, is refused, in fenced blocks too.
  An `@` that starts a word must also name a file the persona ships, as
  rendered: `CLAUDE.md` or a file under `skills/`. Staying inside the
  persona's directory is not enough, because the instructions land in the
  harness's config directory beside files the persona does not own;
- unknown and repeated keys in `persona.yaml` and `mcp.yaml`;
- a file over 1 MiB, or a persona over 16 MiB in total.

## Content manifest

A persona may carry `skills_manifest.sha256` at its root: the output of
`find . -type f ! -path ./skills_manifest.sha256 -exec sha256sum {} +`
run in the persona directory at build time, sorted. When it is present, `Load`
compares every file in the persona with it and refuses the persona on any
difference: a changed file, a file the manifest lists that is missing, a
file it doesn't list, or a symlink or other non-regular file anywhere. A
file of that name below the root is an ordinary file and must be listed.
A persona without one loads, and `Persona.Integrity` says it was not
checked; `Options.RequireIntegrity` refuses it instead. A profile worker
sets that from `A2A_PERSONA_INTEGRITY=required`, and logs whether its
persona loaded verified or unverified. Neither the operator nor the chart
sets that variable, and an AgentProfile has no field for it: it is a
worker-level setting, off by default. The shipped personas don't need it: a build-contract test keeps their manifest present, and a manifest that is present is checked either way.

The check shows that the persona's files are the ones its build recorded:
it catches a bad build, a corrupted layer or a staging copy that went
wrong. The manifest ships inside the image it describes, so it does not
detect a replaced or deliberately altered image, and it is not a security
boundary. The platform agent image checks its own skill trees the same way
(`agents/platform/scripts/verify_skills_provenance.py`), except that it
leaves out a file of the manifest's name at any depth; here only the root
manifest is left out, because a skill's files are rendered.

`CheckTree(dir, scope)` runs the same check over the part of a directory
that `scope` selects, for a tree that is not a persona directory. The
session worker uses it on its Claude Code home, where only `CLAUDE.md` and
`skills/` are the image's and the harness keeps its own state beside them.
Paths outside the scope are not entered, and a manifest entry outside it is
refused.

The personas this repository ships commit their manifest beside them, since
a persona image's Dockerfile is only `FROM` and `COPY`:
`make persona-manifests` (part of `make docs-generate`) writes them with
`persona manifest --git --in <dir>` (`a2a/cmd/persona`), which covers only
the files a clean checkout holds and fails if that copy doesn't load. That
package's tests fail while a shipped persona's manifest doesn't match it.

A worker that predates this check refuses `skills_manifest.sha256` as a
top-level file the format does not define. A persona image must not carry
one until every worker it can run under accepts the name.

## Adapters

An adapter maps a loaded persona to a harness and reads nothing else: no
network, no credentials, no environment. Whatever the pod knows, such as
the relay's address, the worker passes in as a `Target`. For Claude Code,
what the harness cannot express comes back as a warning; the `core-agent`
adapter refuses it instead (below).
`render.For(harness)` picks the adapter by the harness the worker is started
with: `A2A_HARNESS`, which the operator sets from the AgentProfile's
`spec.harness.type` (and the gateway sets on session pods from the
PlatformAgent's `spec.harness.sessionWorker.type`), or on a profile worker
`PROFILE_HARNESS` when that is unset (`claude-code`, `antigravity` or
`core-agent`). A session pod refuses `antigravity` unless
`A2A_HARNESS_UNISOLATED=allow` and `A2A_HARNESS_PATH` are set, and `core-agent`
unless `A2A_HARNESS_UNISOLATED=allow` is set, until #6710 lands. A profile
worker refuses `antigravity`, which has no persona adapter yet.

- **`claude-code`** (`claudecode`): the built-ins map to Claude Code's
  tools as `read_file` → `Read`, `Glob`, `Grep`, `LS`, `NotebookRead`;
  `shell` → `Bash`, `BashOutput`, `KillShell`; `web_search` → `WebSearch`.
  It renders `CLAUDE.md` and `skills/` for the
  worker to write into a per-task `CLAUDE_CONFIG_DIR`, and the flags to
  pass:
  - `--allowedTools` for what the persona grants, plus `Skill` when it has
    skills, since headless Claude Code refuses the Skill tool off the
    allowlist;
  - `--disallowedTools` for every Claude Code built-in it does not grant,
    on every argv. The worker unions its own hard-deny list with this one,
    and a deny wins over an allow. `Output.Allowed` and `Output.Disallowed`
    carry the same two lists, so the worker need not parse the flags;
  - `--mcp-config`, each server a stdio proxy to the relay the worker
    names. With no relay, every broker server is dropped with a warning;
  - `--strict-mcp-config` and `--setting-sources user`, on every argv.

  Without the `shell` tool a skill ships only its markdown. A server that
  no `tools` entry grants is dropped.

- **`antigravity`**: not written yet; `render.For` refuses it by name.
- **`core-agent`** (`coreagent`): renders `instructions.md` and `skills/`,
  each skill in the Agent Skills layout (`<name>/SKILL.md` and its files),
  for a directory a user other than the harness's writes before the harness
  runs: on a profile worker, an init container's
  `worker-adapter render-persona`, which renders into an emptyDir with a
  content manifest beside it and seals it read-only, and the worker mounts
  it read-only. With `A2A_CORE_AGENT_PERSONA_REQUIRED` set, the harness
  checks it with `coreagent.VerifyRendering` (nothing writable or owned by
  its user, the manifest matches) before reading it. The harness takes the
  instructions as a layer above its own system prompt and lists the skills
  to the model. When the model loads a skill,
  its instructions open with the skill's directory on disk and its scripts
  by absolute path, which is how the shell may run them.
  `coreagent.Snapshot` reads the directory into memory once, beside its
  integrity manifest, refusing any other top-level name, a symlink or a
  special file, so a later change on disk doesn't reach the harness.
  - Each `mcp.yaml` server the manifest grants runs as the worker's relay to
    the credential broker, the same command and environment as for Claude
    Code, with its granted tools listed (`Output.MCPServers`); a whole-server
    grant takes the server's `include` list. The worker sends them in each
    turn's stdin envelope.
  - Tools pass through as the manifest names them, for the harness's tool
    grant to map. `coreagent.ScriptPaths` names the scripts the shell may run.
  - Each `SKILL.md` frontmatter is written back with only the keys the
    harness reads, read as exactly one YAML document between exact `---`
    lines.
  - Refused rather than dropped: a granted server with no relay or no
    `include` list, a server or tool name the harness would rename, a skill
    frontmatter key other than `name`, `description`, `license`,
    `compatibility` and `metadata`, a skill's `allowed-tools`, non-string
    `metadata` values, a script whose path isn't letters, digits, `.`, `_`,
    `-` and `/`, and a script when the persona doesn't grant `shell`.
  - The server name `a2a` is reserved, for every harness, for the worker's
    own delegate server, which the worker adds per turn (core-agent: once
    #6789 lands).

## An example

[`agents/cluster/persona`](../../../agents/cluster/persona/README.md) is the
cluster agent in this format: a read-only SRE for the GKE cluster each task
names, with its skills and the broker MCP server a profile worker may read.

## Checking a persona

Run these from `a2a/`:

```sh
go run ./cmd/persona validate --in <dir>
go run ./cmd/persona render --harness claude-code --in <dir> --out /tmp/out \
  --relay-url http://credential-proxy:8765/v1/mcp \
  --relay-command 'node /opt/mcp-remote/dist/proxy.js {url} --transport http-only'
```

`render` writes into an empty `--out` and prints `{args, model, warnings}`
as JSON on stdout; the warnings also go to stderr. `--relay-command` is
space-separated, and `{url}` becomes each server's relay URL; a relay with no `{url}` in its args is an error. Without the
relay flags every broker server is dropped; the two go together, and either
one alone is a usage error.

A dispatched profile worker renders with its own relay, not `mcp-remote`:
each broker server becomes `worker-adapter mcp-relay <broker>/v1/mcp/<server>`
(`a2a/worker-adapter/mcprelay.go`). It sends each message to the credential
broker with the pod's projected ServiceAccount token and, when the task has
one, its task credential, both read from their files on every request. It
refuses any URL outside the broker's own `/v1/mcp/` route and follows no
redirect. That is defence in depth: what keeps the credentials in the pod is
the profile namespace's NetworkPolicy, which lets a worker reach only DNS, the
bus, LiteLLM and the broker, since a model with a shell can read the token
files itself. The broker's `mcp_policy` still decides what a profile
worker may call.

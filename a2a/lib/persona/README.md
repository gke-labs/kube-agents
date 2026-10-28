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

| Path              | What it holds                                                                                                                                                                                           |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `persona.yaml`    | The manifest: `apiVersion: persona.kube-agents.x-k8s.io/v1alpha1`, `name`, `description` (at most 1024 bytes), `model` (a hint), `tools`, `parameters`, `requires.commands` (a hint), `harness` (hints) |
| `instructions.md` | The instructions, AGENTS.md-compatible                                                                                                                                                                  |
| `skills/<name>/`  | Agent Skills: `SKILL.md` with `name` (the directory name) and `description` (at most 1024 bytes), optionally `license` and `metadata`, plus the skill's files                                           |
| `mcp.yaml`        | Optional. `servers.<name>` with `broker: <relay server>` and an optional `include` tool list                                                                                                            |
| `README.md`       | Optional, never rendered                                                                                                                                                                                |

`tools` is the allowlist. An entry is a built-in (`read_file`,
`web_search`, `shell`), an MCP server from `mcp.yaml` (`gke`), or one of
its tools (`gke.get_cluster`). `parameters` are values each task supplies
in the dispatcher's preamble, such as the target cluster, so nothing
cluster-specific is stamped into the image.

A persona holds no image references, no credentials and no secrets. Every
MCP server is a server on the credential broker's MCP relay, which
authenticates the pod and applies the broker's tool policy and audit log.
There is no direct-URL kind: `Load` refuses `url:` in `mcp.yaml`, because
a server reached around the broker would be outside both.

`Load` also refuses:

- a `tier` in `persona.yaml`: a tier is authority, which the AgentProfile
  grants, never the persona;
- a symlink anywhere, and any top-level file the format does not define
  (a skill directory may hold any regular files);
- a `SKILL.md` frontmatter key other than `name`, `description`, `license`
  and `metadata`, because Claude Code reads keys such as `allowed-tools`
  and `hooks` from it;
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

## Adapters

An adapter maps a loaded persona to a harness and reads nothing else: no
network, no credentials, no environment. Whatever the pod knows, such as
the relay's address, the worker passes in as a `Target`. What a harness
cannot express comes back as a warning, never an error.
`render.For(harness)` picks the adapter by the harness value the worker is
started with (`claude-code` or `antigravity`).

- **`claude-code`** (`claudecode`): `CLAUDE.md` and `skills/` for the
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

## Checking a persona

Run these from `a2a/`:

```sh
go run ./cmd/persona validate --in <dir>
go run ./cmd/persona render --harness claude-code --in <dir> --out /tmp/out \
  --relay-url http://credential-proxy:8765/mcp \
  --relay-command 'node /opt/mcp-remote/dist/proxy.js {url} --transport http-only'
```

`render` writes into an empty `--out` and prints `{args, model, warnings}`
as JSON on stdout; the warnings also go to stderr. `--relay-command` is
space-separated, and `{url}` becomes each server's relay URL; a relay with no `{url}` in its args is an error. Without the
relay flags every broker server is dropped; the two go together, and either
one alone is a usage error.

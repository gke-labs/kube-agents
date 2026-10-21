# Persona format

A persona is what an AgentProfile's agent is told, which skills it has and
which tools it may use, written once for every harness (#3656). The
persona image carries the directory below as its `/files` payload. The
profile pod mounts it read-only at `/persona`
(`k8s-operator/internal/profilepod`), and the worker renders it for its
harness with an adapter from this package.

| Path              | What it holds                                                                                                                                                                              |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `persona.yaml`    | The manifest: `apiVersion: persona.kube-agents.x-k8s.io/v1alpha1`, `name`, `description`, `tier`, `model` (a hint), `tools`, `parameters`, `requires.commands` (a hint), `harness` (hints) |
| `instructions.md` | The instructions, AGENTS.md-compatible                                                                                                                                                     |
| `skills/<name>/`  | Agent Skills: `SKILL.md` with `name` (the directory name) and `description` (at most 1024 characters), plus the skill's files                                                              |
| `mcp.yaml`        | Optional. `servers.<name>` with `broker: <relay server>` and an optional `include` tool list                                                                                               |
| `README.md`       | Optional, never rendered                                                                                                                                                                   |

`tools` is the allowlist. An entry is a built-in (`read_file`,
`web_search`, `shell`), an MCP server from `mcp.yaml` (`gke`), or one of
its tools (`gke.get_cluster`). `parameters` are values each task supplies
in the dispatcher's preamble, such as the target cluster, so nothing
cluster-specific is stamped into the image.

A persona holds no image references, no credentials and no secrets. Every
MCP server is a server on the credential broker's MCP relay, which
authenticates the pod and applies the broker's tool policy and audit log.
There is no direct-URL kind: `Load` refuses `url:` in `mcp.yaml`, because
a server reached around the broker would be outside both. `Load` also
refuses a symlink and any file the format does not define.

## Adapters

An adapter maps a loaded persona to a harness and reads nothing else: no
network, no credentials, no environment. Whatever the pod knows, such as
the relay's address, the worker passes in as a `Target`. What a harness
cannot express comes back as a warning, never an error.
`render.For(harness)` picks the adapter by the pod's `PROFILE_HARNESS`
value.

- **`claude-code`** (`claudecode`): `CLAUDE.md` and `skills/` for the
  worker to write into a per-task `CLAUDE_CONFIG_DIR`, and the flags to
  pass: `--allowedTools`, `--mcp-config`, `--strict-mcp-config` and
  `--setting-sources user`. Each server is a stdio proxy to the relay the worker names. Without the `shell` tool a skill ships only
  its markdown. A server that no `tools` entry grants is dropped.
- **`antigravity`**: not written yet; `render.For` refuses it by name.

## Checking a persona

```sh
go run ./cmd/persona validate --in <dir>
go run ./cmd/persona render --harness claude-code --in <dir> --out /tmp/out
```

Run both from `a2a/`. `render` writes into an empty `--out` and prints the
flags and warnings as JSON.

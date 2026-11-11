# OKF Curator and Dreamer (`a2a/memory`)

`a2a/memory` and `a2a/cmd/curator` implement the Go Operational Knowledge Framework (OKF) Curator, Dreamer, and deterministic manifest guard evaluator for `kube-agents`.

## Responsibilities

- **Record model (`types.go`)**: Parses, validates, renders, and stores OKF decision (`DEC-YYYY-NNN`) and lesson (`LES-YYYY-NNN`) Markdown records under `knowledge/` (or paths declared in `.kube-agents/intent.yaml`), and normalizes external ADR, RFC, and runbook documents from managed and context repositories.
- **Deterministic guard evaluator (`guard.go`)**: Evaluates `guard.expect` and `declares:` assertions against changed Kubernetes YAML and text files with Kubernetes `resource.Quantity` comparisons, multi-document `kind` filtering, and active cluster-over-wildcard precedence.
- **Curator service (`curator.go`)**: Subscribes to candidate proposals on `a2a.topics.shared.annotations` and lifecycle outcomes on `_INBOX.agent.curator.outcomes`, indexes records into the `kube-agents-decisions` Hindsight bank (`okf:<repo>:<ID>`), graduates cross-cluster recurrences into proposed fleet-wide decisions, publishes the digest to `a2a.topics.shared.blueprint`, and answers NATS queries on `_INBOX.agent.memory.query`.
- **Single-shot LLM helper (`llm.go`)**: Runs bounded single-shot synthesis and pre-publish Overseer diff checks against LiteLLM when configured, with deterministic fallbacks when no model endpoint is available.

## CLI (`a2a/cmd/curator`)

Build and test from `a2a/`:

```bash
go test -race ./memory/... ./cmd/curator/...
go build -o bin/curator ./cmd/curator
```

Subcommands: `daemon`, `emit`, `pending`, `check`, `index`, and `recall`.

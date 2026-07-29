# AGENTS.md

## Project Overview

This repository contains the Kubernetes Agentic Harness (`kube-agents`). It is a collection of agent configurations, personas, and skills designed to manage Kubernetes/GKE operations. It utilizes a Platform Agent to transition from reactive manual management to proactive, intent-driven operations.

## Repository Layout

- `agents/`: Source of truth for agent blueprints (personas and skills).
  - `chat/`: The Chat Agent front door — the `default` Hermes profile that receives chat ingress and delegates to specialists.
  - `platform/`: Configuration for the Platform Agent, scaffolded at pod startup into the `platform` profile.
  - `cluster/`: The Cluster Agent profile _template_ (persona, scoped config, and runtime-debugging skills). The Platform Agent scaffolds this into per-cluster Hermes profiles at runtime; it is not deployed directly.
- `.agents/skills/`: Repository-level security review skills.
- `deploy/`: Deployment infrastructure code (Dockerfile, Kustomize bases, shared runtime assets).
- `docs/`: Documentation.
  - `site/`: The published documentation site (Astro + Starlight) — the canonical home for
    user-facing docs.
  - `architecture/`: The end-state architecture specification (`01`–`08`). Describes the target, not
    what ships today.
  - `designs/`: Per-feature design documents.
- `k8s-operator/`: Go/Kubebuilder operator reconciling `PlatformAgent` Custom Resources, plus provisioning scripts.
- `examples/`: Example integrations (LiteLLM provider configs, vLLM serving, inference replay).
- `INSTALL.md`: Installation guide.
- `README.md`: Project overview.

## Agent Setup & Integration

This repository is primarily a configuration and documentation repository for AI agents. The main exception is the Go-based Kubernetes operator in `k8s-operator/`, which requires compilation (see Local Validation Checks below).

To use these agents:

1. Follow the instructions in [INSTALL.md](INSTALL.md) to set up and register the Platform Agent in your agent harness.
2. Refer to the documentation site content in [docs/site/src/content/docs/](docs/site/src/content/docs/) for architecture, concepts, and operational guides.

## Skills Guidelines

- Skills are located under `agents/platform/skills/` (Platform Agent: provisioning, governance, cost, manifest generation, GitOps) and `agents/cluster/skills/` (Cluster Agent: single-cluster runtime debugging and operations).
- Each skill directory must contain a `SKILL.md` file providing instructions for that specific skill.
- Place a skill according to its persona: fleet/provisioning/GitOps-write skills belong to the Platform Agent; read-only, single-cluster runtime-debugging skills belong to the Cluster Agent.
- When adding new skills, ensure they follow the existing structure and are clearly documented to be understood by AI agents.

## Documentation Guidelines

Every fact has one home. Duplicating documentation across files is how it goes stale, so before
adding a paragraph, check whether the topic already has an owner:

| Content                                                  | Canonical home                               |
| -------------------------------------------------------- | -------------------------------------------- |
| User-facing narrative, how-to, and reference             | `docs/site/src/content/docs/`                |
| End-state architecture                                   | `docs/architecture/`                         |
| Per-feature design rationale                             | `docs/designs/`                              |
| What each provisioning script does                       | `k8s-operator/scripts/README.md`             |
| The install procedure (self-contained, agent-executable) | `INSTALL.md`                                 |
| What the agent is and is not permitted to do             | the site's `reference/security-and-iam.md`   |
| How to develop a specific directory                      | that directory's `README.md` (keep it short) |

Rules:

- **Do not hand-write a table that mirrors a machine-readable file.** The cron schedule, the skill
  catalogue, and the provisioning steps are generated into `<!-- BEGIN GENERATED -->` regions by
  `scripts/generate_docs.py`. Edit the source, then run `make docs-generate`.
- **Do not restate the `make` targets.** `make help` prints them from the Makefile. New targets get
  a `## description` comment.
- **Link rather than summarise** when another page already owns the topic. If you must summarise,
  say which page is canonical, the way the site's credential-isolation page defers to
  `docs/credential-isolation-design.md`.
- **Do not document pull-request status.** Docs describe the current state of `main`; a merged PR
  leaves that prose silently stale.
- **Verify identifiers against source, not against other docs.** Service account names live in
  `k8s-operator/scripts/common.sh`, the Go version in `k8s-operator/go.mod`.

Run `make docs-check` before pushing. It verifies generated regions are current, relative links
resolve, identifiers match their source, and every Markdown document has an entry in the
documentation map (`docs/README.md`) — the same four checks CI runs.

## Pull Request Hygiene

- Keep changes scoped to the request.
- Do not commit unrelated formatting changes.
- Maintain the structure and intent of the agent configuration files.
- Use Conventional Commits for commit messages.
- Push PR branches to a fork, not to the upstream repository.
- Use `.github/PULL_REQUEST_TEMPLATE.md` for PR body structure and level of
  detail. Do not use `--fill` with `gh pr create` as it bypasses the template.
- **Docs-drift review before opening a PR:** run the `review-docs-drift` skill
  (`.agents/skills/review-docs-drift/SKILL.md`) against your branch diff and address its
  Blocking findings. This is a required pre-PR step for AI agents working in this repository;
  `make docs-check` enforces only the mechanical subset (generated regions, links, terminology,
  map coverage), while the skill also verifies that doc prose still matches the source.
- **Local Validation Checks:** Before committing, try to run checks locally to avoid CI failures:
  - **Formatting:** Run `npx prettier --write <files>` on changed Markdown, JSON, or YAML files. You can check all files using `npx prettier --check .` (note: this may check files outside your PR scope).
  - **Docker Build:** Validate the agent runner Dockerfile by building it locally (e.g., `docker build -f deploy/docker/Dockerfile --target platform .`).
  - **Operator Code:** If you modify `k8s-operator/`, run `make` or `go build` inside that directory to ensure compilation succeeds.

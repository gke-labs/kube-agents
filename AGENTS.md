# AGENTS.md

## Project Overview

This repository contains the Kubernetes Agentic Harness (`kube-agents`). It is a collection of agent configurations, personas, and skills designed to manage Kubernetes/GKE operations. It utilizes a Platform Agent to transition from reactive manual management to proactive, intent-driven operations.

## Repository Layout

- `agents/`: Source of truth for agent blueprints (personas and skills).
  - `platform/`: Configuration for the Platform Agent.
- `.agents/skills/`: Repository-level security review skills.
- `deploy/`: Deployment infrastructure code (Dockerfile, Kustomize bases, shared runtime assets).
- `docs/`: Design docs and the documentation site (`docs/site/`).
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

- Skills are located under `agents/platform/skills/`.
- Each skill directory must contain a `SKILL.md` file providing instructions for that specific skill.
- When adding new skills, ensure they follow the existing structure and are clearly documented to be understood by AI agents.

## Pull Request Hygiene

- Keep changes scoped to the request.
- Do not commit unrelated formatting changes.
- Maintain the structure and intent of the agent configuration files.
- Use Conventional Commits for commit messages.
- Push PR branches to a fork, not to the upstream repository.
- Use `.github/PULL_REQUEST_TEMPLATE.md` for PR body structure and level of
  detail. Do not use `--fill` with `gh pr create` as it bypasses the template.
- **Local Validation Checks:** Before committing, try to run checks locally to avoid CI failures:
  - **Formatting:** Run `npx prettier --write <files>` on changed Markdown, JSON, or YAML files. You can check all files using `npx prettier --check .` (note: this may check files outside your PR scope).
  - **Docker Build:** Validate the agent runner Dockerfile by building it locally (e.g., `docker build -f deploy/docker/Dockerfile --target platform .`).
  - **Operator Code:** If you modify `k8s-operator/`, run `make` or `go build` inside that directory to ensure compilation succeeds.

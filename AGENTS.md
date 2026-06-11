# AGENTS.md

## Project Overview

This repository contains the Kubernetes Agentic Harness (`kube-agents`). It is a collection of agent configurations, personas, and skills designed to implement a cooperative multi-agent system for Kubernetes/GKE operations. It separates concerns into a Platform Agent, Operator Agent, and DevTeam Agent to transition from reactive manual management to proactive, intent-driven operations.

## Repository Layout

- `config/`: Source of truth for agent blueprints (personas and skills).
  - `agents/platform/`: Configuration for the Platform Agent.
  - `templates/`: Base templates for dynamic subagents (`operator`, `devteam`).
- `deploy/`: Deployment infrastructure code (Dockerfile, Helm charts, manifests).
- `docs/`: Documentation, guides, and walkthroughs.
- `local-dev/`: Tooling for local offline testing (Kind setup).
- `INSTALL.md`: Installation guide.
- `README.md`: Project overview.

## Agent Setup & Integration

This repository is primarily a configuration and documentation repository for AI agents. It does not contain code that requires compilation or traditional building.

To use these agents:

1. Follow the instructions in [INSTALL.md](INSTALL.md) to set up and register the Platform Agent in your agent harness.
2. Refer to SRE walkthroughs in [docs/m1-demos.md](docs/m1-demos.md) for details on how to interact with the cooperative agent layout and run demo scenarios.

## Skills Guidelines

- Skills are located under `config/agents/platform/skills/` or the subagent template skills paths in `config/templates/`.
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
- When updating Markdown files, run `npx prettier --write <files>` on the
  changed Markdown files before committing.

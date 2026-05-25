# AGENTS.md

## Project Overview

This repository contains the Kubernetes Agentic Harness (`kube-agents`). It is a collection of agent configurations, personas, and skills designed to implement a cooperative multi-agent system for Kubernetes/GKE operations. It separates concerns into a Platform Agent, Operator Agent, and DevTeam Agent to transition from reactive manual management to proactive, intent-driven operations.

## Repository Layout

- `workspace/`: Core directory containing agent definitions and configurations.
  - `agents/platform/`: The home of the Platform Agent. Contains its persona (`SOUL.md`), identity (`IDENTITY.md`), and routing rules (`ROUTING.md`).
  - `agents/platform/skills/`: Reusable AI skills for the Platform Agent (e.g., cluster creation, multi-tenancy).
  - `agents/platform/templates/`: Templates for provisioning `operator` and `devteam` subagents.
- `docs/`: Documentation, including contribution guidelines.
- `INSTALL.md`: Guide for installing and configuring the Platform Agent in an AI agent harness.
- `README.md`: Top-level overview of the project and its components.

## Agent Setup & Integration

This repository is primarily a configuration and documentation repository for AI agents. It does not contain code that requires compilation or traditional building.

To use these agents:

1. Follow the instructions in [INSTALL.md](INSTALL.md) to set up and register the Platform Agent in your agent harness.
2. Refer to [workspace/README.md](workspace/README.md) for details on how to interact with the cooperative agent layout, use routing shortcuts, and run demo scenarios.

## Skills Guidelines

- Skills are located under `workspace/agents/platform/skills/`.
- Each skill directory must contain a `SKILL.md` file providing instructions for that specific skill.
- When adding new skills, ensure they follow the existing structure and are clearly documented to be understood by AI agents.

## Pull Request Hygiene

- Keep changes scoped to the request.
- Do not commit unrelated formatting changes.
- Maintain the structure and intent of the agent configuration files.
- Use Conventional Commits for commit messages.
- Push PR branches to a fork, not to the upstream repository.
- Use `.github/PULL_REQUEST_TEMPLATE.md` for PR body structure and level of
  detail.
- When updating Markdown files, run `npx prettier --write <files>` on the
  changed Markdown files before committing.

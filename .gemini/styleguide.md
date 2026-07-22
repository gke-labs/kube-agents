# kube-agents Code Review Style Guide

## Pull Request Hygiene

When reviewing code for this repository, please ensure the following hygiene rules are followed:

- **Scope:** Keep changes scoped to the request. Flag PRs that include unrelated changes or features, and suggest if you think a single large PR could be split into smaller, more focused PRs.
- **Formatting:** Do not commit unrelated formatting changes.
- **Agent Configuration:** Maintain the structure and intent of the agent configuration files.
- **Commit Messages:** Check if the PR summary or commits follow the Conventional Commits specification.
- **PR Template:** Verify that the PR description adheres to the structure and format defined in `.github/PULL_REQUEST_TEMPLATE.md`.

## Skills Guidelines

When reviewing new or modified skills, please enforce the following rules:

- Skills must be located under the `agents/platform/skills/` directory.
- Each skill directory must contain a `SKILL.md` file that provides the instructions for that specific skill.
- Ensure that skill instructions are clearly documented and structured to be understood by AI agents.

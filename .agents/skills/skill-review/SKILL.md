---
name: skill-review
description: Reviews agent skill definitions for token efficiency, clarity, and standardized formatting.
---

# Task

Review agent skill definitions (`SKILL.md` files) to enforce maximum token efficiency, eliminate conversational fluff, and ensure standardized formatting without losing functional fidelity.

# Checks

## 1. Frontmatter

- **Discovery**: A harness loads `name` and `description` at session start and the body only once the description matches the request, so the description is what decides whether the skill is ever read. Require it to open with the action the user asks for and to name the thing acted on; flag one that describes the skill to a reader who already found it (`Discovers and removes provisioned kube-agents GCP/GKE infrastructure` never says "uninstall").
- **Loadability**: Require frontmatter a YAML parser accepts. Flag an unquoted `: ` in a description, and a value opening with `{`, `[`, `*` or `&`.

## 2. Structure & Headers

- **Standardization**: Require a top-level `# Task` section and `# Checks` (or `# Workflow`) section.
- **Header Conciseness**: Flag overly verbose headers (e.g., `## Focus Areas & Deterministic Checks`). Require terse headers.

## 3. Prose & Verbosity

- **Persona Elimination**: Flag conversational AI persona setups (e.g., "You are an expert..."). Replace with imperative tasks.
- **Conversational Fluff**: Flag transitional phrases, unnecessary adverbs, and filler words.
- **Imperative Voice**: Require bullet points to start with strong, punchy verbs (`Flag...`, `Require...`, `Ensure...`).

## 4. Formatting

- **Density**: Ensure rules are condensed into terse, single-sentence commands where possible.
- **Fidelity**: Ensure no deterministic rules, checks, or domain knowledge are lost during token optimization.

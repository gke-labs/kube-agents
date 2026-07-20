---
title: Skills
description: How the Platform Agent loads and invokes its shipping capability bundles.
sidebar:
  order: 3
---

A **skill** is a Markdown-plus-metadata bundle that tells the Platform Agent how to accomplish a particular class of task. Skills follow the [Claude skills format](https://www.anthropic.com/news/skills) — a `SKILL.md` file with YAML frontmatter (`name`, `description`) followed by procedural guidance the model reads on demand.

The full skill catalog is on the [Skill catalog](/kube-agents/skills/) page. This page covers how they work.

## Where skills live

```text
agents/platform/skills/
├── gke-cluster-creator/
│   └── SKILL.md
├── gke-multi-tenancy/
│   └── SKILL.md
├── submit-suggestion/
│   ├── SKILL.md
│   └── (supporting scripts)
└── ... (17 more)
```

Some skills are pure Markdown; others carry supporting files (helper scripts, YAML templates) in the same directory. The Hermes runtime discovers `SKILL.md` files automatically at startup.

## Frontmatter contract

```yaml
---
name: gke-multi-tenancy
description: Guidance on implementing multi-tenancy and governance in Google Kubernetes Engine (GKE) clusters.
---
```

- **`name`** — the skill identifier. Snake/kebab case; matches the directory name.
- **`description`** — a one-sentence purpose. The agent uses this to decide _whether to load the skill_ for a given task without reading the whole body.

Anything after the frontmatter is procedural instruction: workflows, SOPs, example manifests, safety red lines. The model reads it only when it decides the skill is relevant to the current turn.

## Invocation

Two ways a skill enters the model's context:

1. **On-demand.** The agent notices from the user's prompt (or a cron job's prompt) that a particular skill's `description` matches. It loads the skill body and follows the procedure.
2. **Explicit reference from a cron job.** `cron/jobs.json` entries can name skills in the `"skills"` field. The `github-issue-resolver` job, for example, always loads its namesake skill:

   ```json
   {
     "id": "github-issue-resolver",
     "prompt": "Run the github-issue-resolver skill to poll, triage, ...",
     "skills": ["github-issue-resolver"]
   }
   ```

## Skill structure conventions

Most shipping skills follow this shape:

- **Overview** — one paragraph explaining what the skill does and when the agent should use it.
- **Workflows** — numbered procedures for common tasks.
- **Examples** — YAML manifests, shell commands, or link templates the model can adapt.
- **Safety red lines** — explicit "don't do X" rules (e.g. the `submit-suggestion` skill lists commit-scope guardrails).

The `gke-compute-classes` skill is a good example — it explicitly delineates when the agent should _not_ invoke it, guarding against over-eager use.

## Adding a new skill

1. Create `agents/platform/skills/<your-skill>/SKILL.md`.
2. Add frontmatter with `name` and a specific `description` — this is what routes the agent to the skill.
3. Write the procedure. Prefer concrete steps and example manifests over abstract descriptions.
4. If the skill has safety-critical operations (destructive changes, wide-blast-radius commands), list explicit red lines the model must honor.
5. Test locally: DM the agent in Chat with a prompt that should trigger the skill, and verify it loads and follows the procedure.
6. If the skill should also run on schedule, add an entry to `agents/platform/cron/jobs.json`.

## Skill vs. governance SOP vs. cron job

A few related concepts that are easy to confuse:

- **Skill** — a reusable capability the agent loads when relevant. Lives in `skills/`.
- **Governance SOP** — a fleet-wide operating procedure (e.g. "audit blueprints daily"). Lives in `governance/`. Invoked by cron jobs.
- **Cron job** — a scheduled prompt that fires an SOP or skill on a timer. Lives in `cron/jobs.json`.

Skills are on-demand tools; SOPs are the codified playbooks; cron jobs are the schedules that fire them.

## Where to go next

- [Skill catalog](/kube-agents/skills/) — every skill with description and source link.
- [Governance SOPs](/kube-agents/concepts/governance-sops/) — the fleet-wide playbooks.
- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — the cron jobs that invoke them.

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

## Importing external skills

The agent discovers skills from **two** locations at startup:

- **Baked into the image** at `/opt/hermes/skills/` — everything under `agents/platform/skills/` is copied here by [`deploy/docker/Dockerfile`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/Dockerfile).
- **The runtime workspace** at `$HERMES_HOME/skills` — `HERMES_HOME` defaults to `/opt/data`, so `/opt/data/skills`. This path is backed by the agent's persistent volume.

That gives you two ways to bring in additional skills — for example from the upstream [`google/skills`](https://github.com/google/skills/tree/main/skills/cloud) catalog.

### Method 1 — bake into a custom image (production)

Reproducible and immutable: the skill ships inside the container.

1. Copy the skill directory into `agents/platform/skills/<skill>/` (it must contain `SKILL.md` plus any `references/`).
2. Build and push the `platform` image stage:

   ```bash
   docker build -f deploy/docker/Dockerfile --target platform \
     -t my-registry/kube-agents/platform-agent:v1.1.0 .
   docker push my-registry/kube-agents/platform-agent:v1.1.0
   ```

3. Point the `PlatformAgent` CR at the new image and apply it:

   ```yaml
   apiVersion: kubeagents.x-k8s.io/v1alpha1
   kind: PlatformAgent
   metadata:
     name: platform-agent
     namespace: kubeagents-system
   spec:
     deployment:
       image: my-registry/kube-agents/platform-agent
       tag: v1.1.0
   ```

The operator rolls the Deployment and the new skill loads on boot.

### Method 2 — inject into the running pod (development)

Faster for iterating: drop the skill into the persistent workspace without rebuilding.

```bash
# The agent pod carries the label app=platform-agent-gateway; the container is `platform-agent`.
AGENT_POD=$(kubectl get pods -n kubeagents-system \
  -l app=platform-agent-gateway -o jsonpath='{.items[0].metadata.name}')

kubectl cp <skill-dir>/ \
  kubeagents-system/$AGENT_POD:/opt/data/skills/<skill-dir> -c platform-agent
```

Verify it landed:

```bash
kubectl exec -n kubeagents-system -it $AGENT_POD -c platform-agent -- \
  ls -la /opt/data/skills/<skill-dir>
```

The runtime discovers the skill on its next relevant turn. Because this writes to the persistent volume, it survives pod restarts — but it is **not** captured in the image, so bake it in (Method 1) before relying on it in production.

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

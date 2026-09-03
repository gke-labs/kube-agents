---
title: Reconciling the long-lived environments
description: How autopush and staging are kept in step with terraform/examples/full-install, what each one has to be configured with, and what to do when a drift report opens.
sidebar:
  order: 8
---

:::note[For maintainers of this repository]
This page is about the environments **kube-agents itself** runs its CI against —
`autopush`, `staging`, `rc` and `nightly`, in Google-owned GCP projects. Nothing
here is something you configure on your own install; for that, see the
[Quick start](/kube-agents/install/quickstart-gke/). It sits in the published docs
alongside [CI pool project prerequisites](/kube-agents/deploy/ci-pool-projects/)
and [release versioning](/kube-agents/deploy/release-versioning/), which have the
same audience.
:::

`autopush` and `staging` are long-lived: they are installed once and then kept
running, and people live-test pull requests against them. `rc` and `nightly` are
the opposite — every pipeline run destroys them and builds them again from
`terraform/examples/full-install`, so they always run today's composition.

The redeploy workflows that move them are `helm upgrade` on a pre-existing
release and nothing more, so on their own they carry images and no
infrastructure: IAM bindings, Pub/Sub topics, node pools, cluster settings and
the chart values the composition renders all stay where the last apply left
them. A green redeploy says the images rolled, which reads as "main is
deployed" and is only half of it.

Three things close that gap.

## The scheduled drift report

`Drift: Long-Lived Environments` runs `terraform plan` against each environment
every morning and opens an issue labelled `infra-drift` when the plan is not
empty. It is read-only: no state lock, no state bucket creation, no adoption
imports, so it is safe against an environment somebody is working on and needs
no lease.

One issue per environment, edited in place while the drift lasts and closed
automatically by the first clean plan. A plan that fails to run leaves whatever
is open exactly as it is — a failure is not evidence either way, and the red job
is the signal.

The plan pins no image tag. It holds the tag at whatever the last apply
recorded, read out of Terraform state, so the report is about infrastructure
rather than about images being a few commits behind between redeploys.

State rather than the cluster, and the difference matters: the redeploy
workflows move the running tag with `helm upgrade` and never run Terraform, so
planning at the tag the cluster is _serving_ would show every redeploy since the
last apply as a pending change to `helm_release.kube_agents` — a drift issue
opening on image lag every day `main` has moved, which never reaches the clean
plan that would close it. An install whose state predates this (it is published
as an `image_tag` output) falls back to the running tag and says so in the job
log; the first reconcile records it and later plans are clean.

## The nightly reconcile

The nightly pipeline applies the composition to both environments once its E2E
matrix is green — steps 4 and 6 of `nightly-pipeline.yml`. The ordering is the
point: a composition that has not been proved to build an install from nothing
does not get applied to an environment people work in.

The two are reconciled differently, and the difference is deliberate:

- **staging** is reconciled to the candidate the pipeline is promoting, and
  **before** the `staging_*` tag is pushed. That tag starts three
  `helm upgrade`s on the same release `helm_release.kube_agents` owns, so
  applying afterwards would race them.
- **autopush** is reconciled with no image tag at all. It tracks `main`'s tip
  through GHCR publishes, and the pipeline's candidate is older than that;
  pinning it would roll autopush's images backwards.

A reconcile takes the live-test lease before it applies anything (see
[`docs/designs/live-test-lease.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/live-test-lease.md)),
and defers to the next night if somebody else holds it. It
also waits out any redeploy already in flight, for the same release-lock reason
as the ordering above.

Run one by hand with `Shared: Reconcile Environment` (`mode: apply`), or locally
against your own install with `./upgrade.sh --plan` to see what a reconcile
would change.

## The rebuild button

When an in-place apply cannot converge, `Shared: Deploy Environment` takes
`autopush` and `staging` as well as `rc` and `nightly`. It **destroys the
cluster** and builds it again, so it asks you to type the environment's name
into `confirm_destroy`, and it refuses unless the live-test lease reads back
as free.

Read [what a teardown does not preserve](#what-a-rebuild-does-not-preserve)
before using it.

## What each environment has to be configured with

The reconcile renders an `install.env` from the environment's GitHub variables
and secrets with `scripts/release/render_install_env.sh`, and
`install.env.example` documents what each key means. The rebuild button takes a
different route to the same settings — `provision_environment.sh` turns the
cluster coordinates into `install.sh` flags and the workflow puts the rest in
the environment `install.sh` reads. The two are separate copies rather than one
function, so two tests hold them together: one pins their overlapping
translation, `MEMORY_PROVIDER`, and one pins every key the renderer writes
against what the rebuild workflow exports, because a setting only one path
carries is a rebuild that installs something the reconcile would not have.
`provision_environment.sh` additionally accepts `GITHUB_ORG`/`GITHUB_REPO`
aliases the renderer does not.

Every setting below is **required** on a long-lived environment, and the
reconcile fails naming all the missing ones at once rather than starting. This
is not pedantry: an omitted setting resolves to a project default, the default
is written into `terraform.tfvars`, and `terraform apply` then plans the
destruction of whatever the default does not mention. On an environment that is
rebuilt every run that costs a feature; on one that has been up for a month it
takes the gVisor node pool, the Hindsight database, or the Pub/Sub topic behind
Google Chat with it.

Required for a **plan** as much as for an apply: the reconcile renders `--strict`
before it branches on the mode, so until an environment carries all ten the daily
drift report goes red on it rather than reporting no drift.

| GitHub variable                 | install.env key                 | Notes                                                       |
| ------------------------------- | ------------------------------- | ----------------------------------------------------------- |
| `GCP_PROJECT_ID`                | `PROJECT_ID`                    | Required everywhere, including for a plan                   |
| `GCP_REGION`                    | `REGION`                        | Required everywhere                                         |
| `GKE_CLUSTER_NAME`              | `CLUSTER_NAME`                  | Required everywhere                                         |
| `GOOGLE_CHAT_ENABLED`           | `GOOGLE_CHAT_ENABLED`           | `false` removes the topic and subscription                  |
| `MODEL_PROVIDER`                | `MODEL_PROVIDER`                | Absent falls back to `gemini`                               |
| `PLATFORM_AGENT_PERMISSION_SET` | `PLATFORM_AGENT_PERMISSION_SET` | Absent falls back to `read-only` and drops the custom roles |
| `ENABLE_GVISOR`                 | `ENABLE_GVISOR`                 | Absent destroys the gVisor node pool on Standard            |
| `MEMORY_PROVIDER`               | `MEMORY`                        | Absent destroys the Hindsight API and its Postgres          |
| `USER_PROFILE_ENABLED`          | `USER_PROFILE_ENABLED`          | Absent resets it                                            |
| `ENABLE_GKE_BACKUP_PLAN`        | `ENABLE_GKE_BACKUP_PLAN`        | Absent destroys the backup plan                             |

Required when the integration they belong to is switched on, because an empty
allowlist is not "no opinion" — the operator reads an absent list as allow-all,
so leaving one unset admits every user in the domain:

| GitHub variable       | Required when         | Say allow-all instead with    |
| --------------------- | --------------------- | ----------------------------- |
| `ALLOWED_USERS`       | `GOOGLE_CHAT_ENABLED` | `GOOGLE_CHAT_ALLOW_ALL_USERS` |
| `SLACK_ALLOWED_USERS` | `SLACK_ENABLED`       | `SLACK_ALLOW_ALL_USERS`       |

Neither `*_ALLOW_ALL_USERS` variable is written into `install.env`. The empty
allowlist is already what produces allow-all downstream; the variable exists so
that an environment which wants it has to say so, and one that lost its
allowlist to a typo fails instead. A value that is only separators — a list
cleared down to a stray comma — counts as empty, because that is what it
renders to.

Both paths refuse an empty allowlist on a long-lived environment: the strict
render stops the reconcile, and `provision_environment.sh` stops the rebuild
above its teardown. `rc` and `nightly` are exempt by design — they are
destroyed and rebuilt every run and no real user reaches them, so an
unconditional check would fail the RC pipeline rather than protect anything.

Optional, and copied through when set: `CLUSTER_MODE`, `MODEL_DEFAULT_NAME`,
`VERTEX_PROJECT_ID`, `VERTEX_LOCATION`, `GOOGLE_CHAT_MODE`, `CHAT_TOPIC_NAME`,
`CHAT_SUB_NAME`, `SLACK_ENABLED`, `SLACK_HOME_CHANNEL`,
`SLACK_HOME_CHANNEL_NAME`, `PLATFORM_AGENT_CUSTOM_ROLES`,
`HERMES_DASHBOARD_ENABLED`, `REGISTRY_PREFIX`, `THIRD_PARTY_REGISTRY_PREFIX`,
`KMS_KEYRING`, `KMS_KEY`, `GITOPS_ORG`, `GITOPS_REPO`. Secrets: `GH_APP_ID`,
`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `SLACK_BOT_TOKEN`,
`SLACK_APP_TOKEN`.

Two naming details that are easy to trip over:

- The namespace is `AGENT_NAMESPACE` on `rc` and `nightly` and `NAMESPACE` on
  `staging`. Both are read, so neither has to be renamed while installs are
  running against it.
- `GITOPS_ORG`/`GITOPS_REPO` name the repository the **agent** opens pull
  requests against. `GH_ORG`/`GH_REPO` name the **release** repository. Setting
  the minter's pair to the release repository scopes a live GitHub App token at
  this repository, which is why `rc` points at a throwaway repo instead.

`GITOPS_ORG`, `GITOPS_REPO` and `GH_APP_ID` are read as a unit: all three set
provisions the token minter, none set installs without it, and any other
combination renders `enable_github_minter = false` — which on an environment
that already has a minter is an apply that destroys it. Both paths refuse that
rather than proceeding: the strict render stops the reconcile, and
`provision_environment.sh` stops the rebuild above its teardown. An environment
carrying `GH_APP_ID` alone, which is how `autopush` was configured, has to
either gain the other two or drop the secret.

The reconcile additionally checks that the minter's KMS signing key has an
enabled version, because that is the other way `enable_github_minter` flips to
false and no variable expresses it. A key that has been rotated or scheduled for
destruction stops the apply instead of taking the minter with it.

## What a rebuild does not preserve

Only relevant to `Shared: Deploy Environment`; the nightly reconcile keeps the
cluster and everything on it.

- **KMS key rings survive.** GCP cannot delete them, and `lifecycle.sh adopt-kms`
  re-adopts them on the next apply. Already handled.
- **Pub/Sub subscription IAM is recreated**, not preserved, and the Google Chat
  app in the Workspace console points at the topic by name. Verify chat delivery
  after a rebuild.
- **The cluster endpoint changes.** Anything holding a kubeconfig — another
  agent's `live-test-envs.json`, a developer's machine — needs new credentials.
- **Anything an agent created on the cluster is not cleaned up.** Clusters
  provisioned _from inside_ autopush keep their own Terraform state in a bucket
  this composition does not manage, and a teardown of the host leaves them
  running.

## Applying repeatedly against an environment that exists

A scheduled reconcile is the only thing in this project that applies to the same
environment over and over; `rc` and `nightly` destroy theirs first and so never
exercise it. Two properties of the composition matter only on that path, and
both are covered by comments in the source rather than restated here:

- `lifecycle.sh apply` adopts a pre-existing Pub/Sub topic and subscription
  rather than failing with `Error 409: Resource already exists`, the way it
  already adopts KMS key rings. Configuring the Google Chat app in the Cloud
  console creates the topic before the installer runs, so this is reachable on a
  first install too. See `adopt_pubsub` in
  [`lifecycle.sh`](https://github.com/gke-labs/kube-agents/blob/main/terraform/examples/full-install/lifecycle.sh).
- Every Pub/Sub IAM binding is keyed on its parent's `.id`, never its `.name`,
  so a replaced topic takes its bindings into the plan with it instead of
  leaving a green apply over an empty policy. See
  [`terraform/modules/chat-pubsub/main.tf`](https://github.com/gke-labs/kube-agents/blob/main/terraform/modules/chat-pubsub/main.tf).

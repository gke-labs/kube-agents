# Self-Improvement Loop Identity Module

The Google half of the self-improvement loop: the investigator's service account and its read-only
telemetry grants. That is the whole module. The Kubernetes half is the chart's `selfImprovement.*`
values.

The loop's GitHub identity has no GCP resource behind it. In `fork` and `upstream` mode the loop
authenticates as a robot account holding a personal access token, mounted from a Kubernetes Secret
that is created out of band, so there is nothing here to provision for it — no minter account, no
KMS signing key, no App. Sec. 6 of [`docs/designs/self-improvement.md`](../../../docs/designs/self-improvement.md)
records what that credential trades away against a GitHub App, and why the loop takes the trade.

The investigator's grant is three viewer roles and stops there. It has no `container.*` role at
all: Kubernetes reads go through the pod's Kubernetes service account, which the chart binds to
`view` in a single namespace, so that half is bounded by RBAC rather than by project-level IAM.
Adding `roles/container.viewer` here would quietly widen it to every cluster in the project.

The account also holds no GitHub credential, and that separation is one-directional and worth
stating precisely: a compromise of this service account cannot open a pull request, because the
token that does is a file in the credential-proxy sidecar and not anything IAM issues.

It does **not** mean a compromised investigation cannot open a pull request. The token sits in the
same pod. What stands between the two is inside that pod, not in IAM: the investigate turn is
started with the proxy shims off its `PATH` and the endpoint out of its environment, the Secret is
mounted into the sidecar and not the runner, and the proxy's deny policy refuses the argv shapes
that would abuse a token a turn did reach. None of the three is a boundary — the proxy is a sidecar
on unauthenticated loopback in the same pod. `agents/selfimprove/SOUL.md` says so to the agent
directly, and the design's §11 records splitting the filing turn into a second pod as the
structural fix, and as work this does not do.

This module is deliberately separate from `kube-agents-iam`, which grants the Platform Agent
whatever fleet management needs — read-only by default, and `roles/container.admin` under the
full-install composition's `permission_set = "gke-admin"`. The loop must inherit none of it —
an agent that can modify the cluster it is investigating cannot honestly report on it — and a
separate module also means an install can destroy the loop's identity without touching the
product's.

## The token, which is not created here

`selfImprovement.mode: report-only` needs no GitHub credential at all; its entire output is a ledger
ConfigMap in the release namespace. `fork` and `upstream` need one Secret, created by hand once:

```bash
make selfimprove-enable ARGS="secret -n kubeagents-system --token-stdin"
```

That reads the token from stdin and applies it over a pipe, and checks with GitHub that it works
before storing it. `kubectl create secret --from-literal=token=<PAT>` produces the same object by a
route that puts the token in argv, where the process table and the shell's history file both keep
a copy.

Name it in `selfImprovement.github.patSecret`. The token needs the `repo` **and** `read:org` scopes,
held by an account with write access to `selfImprovement.github.forkRepo`. Both are mandatory:
`gh auth login --with-token` validates the scope set before it stores anything, so a `public_repo`
token is refused at sidecar startup — and that refusal is swallowed on purpose, so the install comes
up healthy and every hourly run SKIPs with no credential for as long as nobody looks. One token
covers both repositories under `upstream` mode, which is what a GitHub App could not do: fork and
base are different installations and `gh` stores one token per host.

Use a robot account, not a person's. Nothing in the install rotates or expires the token, so its
lifetime is an operator's to manage, and its blast radius is every repository the account can write
to rather than a per-repository rule set.

## Names have to match the chart

Four values are agreed between this module and the chart, and nothing compares them at apply time:

| Terraform            | Chart                             |
| -------------------- | --------------------------------- |
| `service_account_id` | `selfImprovement.github.gsaName`  |
| `ksa_name`           | `selfImprovement.github.ksaName`  |
| `namespace`          | the release namespace             |
| `project_id`         | `platformAgent.harness.projectId` |

A mismatch produces a Workload Identity binding that applies cleanly and authenticates nothing, and
nothing catches it: a `terraform apply` and a `helm install` never see each other's values. What
each side does check is shape. `service_account_id` is validated here and `gsaName` again in the
chart, because either can be applied without the other, and an id GCP rejects fails at apply while
an id the chart rejects fails at render; the 30-character cap in both is GCP's own limit on a
service account id. `ksa_name` is validated here only, against the RFC 1123 subdomain rule
Kubernetes applies to a ServiceAccount name — a longer cap, and a different one. `namespace` and
`project_id` are checked nowhere and are yours to get right.

## Usage

```hcl
module "selfimprove" {
  source = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/kube-agents-selfimprove?ref=<tag>"

  project_id = var.project_id
  namespace  = "kubeagents-system"
}
```

`<tag>` is a placeholder here and not a formatting convention: no release tag contains this module
yet, so pin a commit SHA until one does. The
[Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md)
covers SemVer pinning for the modules that are released, and
[`github-minter`](../github-minter/README.md) shows the finished shape.

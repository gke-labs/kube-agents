---
name: upgrade-kube-agents
description: Upgrade kube-agents (the Kubernetes Agentic Harness) and its operator on a GKE cluster, interactively or non-interactively. Use when asked to upgrade, update, or apply a Day-2 change to an existing kube-agents install.
---

# Upgrade Kubernetes Agentic Harness (kube-agents)

Use this skill when asked to upgrade the `kube-agents` Platform Agent or operator on an active GKE cluster.

## Report confirmed upgrade bugs

- After an upgrade attempt, check its exit code and verify the affected workloads and preserved
  history/memory before declaring success. Check that any JSON report belongs to this attempt;
  an early refusal can leave a previous report behind. For `--plan`, exit 2 means changes, not a
  failed upgrade. For a healthy success, tell the user what was verified and stop.
- Diagnose failures before filing. Expired credentials, missing tools, quotas, network failures
  and invalid installation configuration need recovery guidance; file only when reproduction or
  source evidence confirms a defect in the official repository. A failed verification can reveal
  a defect even when the upgrade exited 0. Report observed availability/data impact and recovery
  options; do not automatically roll back, delete data, or change the selected installation.
- Use the session's existing authorization to report to `gke-labs/kube-agents`. If GitHub writes
  have not been authorized, prepare the report and ask once before posting. An upgrade request
  alone does not authorize publishing installation details.
- Before any issue, comment, reopening or label write, verify the requesting user's GitHub
  identity with `gh api user` and tie it to the requester. Use the official repository's current
  names as the eligibility source: the paginated GitHub contributors list and reviewers/approvers
  in `OWNERS` from official `main`, expanding `OWNERS_ALIASES`. Fetch these from
  `gke-labs/kube-agents`, not a fork, release bundle or stale local checkout; do not hardcode names.
  Qualify a verified contributor or repository reviewer/approver, or a requester whose current
  `repos/gke-labs/kube-agents/collaborators/<LOGIN>/permission` response confirms `write`, `maintain`
  or `admin` access. Read/triage permission, a claimed contribution or organization membership
  alone is insufficient. Do not infer requester eligibility from bot, application or shared
  administrator credentials. If identity is unverified, no authoritative source qualifies the
  requester, or required eligibility checks cannot complete, keep an anonymized local draft and
  explain the restriction. Regular users must not open or modify issue tickets, even for p0
  failures or after approving a post. Eligibility does not grant missing GitHub API permissions;
  handle denied writes with the draft/partial-write fallback below.
- Search `gke-labs/kube-agents` issues, open **and closed**, using the distinctive error, affected
  component and root cause. Use anonymized search terms; do not send raw client diagnostics.
  Do not filter by `upgrade-failure`: older matching issues may lack it.
  Read candidate bodies and comments; a similar title or symptom with a different cause is not a
  duplicate. Narrow or paginate incomplete results. If search fails or remains incomplete, keep a
  draft and tell the user why; do not create an issue without a completed duplicate check.
- For a matching open issue, add a comment with new evidence and keep its body and assignees.
  Skip a comment if this exact attempt is already recorded and adds nothing. For a matching closed
  issue, check the resolution and release containing the fix: point to that release if the install
  predates it. For a confirmed recurrence after the fix shipped, comment on and reopen the issue
  if permitted; otherwise link the closed issue and report the inability to reopen it.
- With no matching issue, repeat the search immediately before creating one to reduce races.
  Give it a concrete title naming the component, affected release and failure.
- Apply `bug` and `upgrade-failure` to a created or updated defect report. Create missing labels
  only within the authorized reporting action; preserve existing label definitions.
  Use the description `Confirmed repository defects encountered during upgrades` for
  `upgrade-failure`. Add `priority:p0` only for confirmed data loss, an ongoing outage caused by
  the defect, or a blocked release. Remove conflicting lower `priority:` labels when escalating
  to p0; preserve existing priorities otherwise. A failed upgrade with a healthy existing install
  is not automatically p0.
- Include the previous and target versions, upgrade mode, failing stage, exit code, expected and
  actual behaviour, minimal reproduction or source evidence, sanitized command/error excerpts,
  availability/data impact, and recovery or workaround. Mark unavailable facts as unknown.
  Anonymize the title, body, comments and any attachments before posting. Allow only official
  repository identifiers, release versions, generic failure details and anonymized impact.
  Remove credentials, tokens, session salts, client/customer and user names, project/cluster/org
  identifiers, custom workload/namespace names, private repository names, URLs, domains, IP/email
  addresses, filesystem paths and other client-specific details. Use placeholders that preserve
  the failure mechanism; omit an excerpt or attachment if it cannot be safely anonymized. Never
  attach raw `install.env`, Terraform state, kubeconfig or unfiltered logs. Review the complete
  public payload for client information before sending; if unsure, keep the local draft.
- Use `gh` with an explicit `--repo gke-labs/kube-agents` for issue and label operations, and
  `--body-file` for issue bodies and comments. Keep exact multiline text in a temporary file.
  If permissions, label creation or a write fail, retain the sanitized draft, explain what
  actually succeeded and what remains, and
  provide any issue URL already created. For an ambiguous write response, check GitHub before
  retrying; do not blindly create a duplicate.
- Read back the resulting issue/comment and labels before reporting completion. GitHub can
  silently omit labels for callers without sufficient access. If labels are missing, retain the
  ticket URL and report the incomplete labeling; do not create another issue to retry it.
- Return the issue URL and whether it was created, updated or reopened, plus the installation's
  current state and next recovery step. A successful retry alone is not proof the repository bug
  is fixed: leave issue closure to verified resolution.

## One-Liner Execution Mode (Non-Interactive)

Upgrade an install with the `upgrade.sh` published for the release you are moving to, substituting
`<RELEASE_VERSION>` with a release tag from
[GitHub Releases](https://github.com/gke-labs/kube-agents/releases). The release-pinned script
carries its own version, so no image tag is passed:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/upgrade.sh | bash -s -- \
  --upgrade-mode="full" \
  --non-interactive \
  --gcp-project-id="<PROJECT_ID>" \
  --gke-cluster-name="<CLUSTER_NAME>" \
  --gcp-region="<REGION>"
```

A full upgrade re-renders the whole install (the `PlatformAgent` CR included) from the install's
`install.env`, and refuses to proceed without it. `KUBE_AGENTS_INSTALL_ENV` names one outright,
which is how an ephemeral CI runner supplies it. The order the script searches when it is not set
is given on the site's
[upgrade page](../../../docs/site/src/content/docs/install/upgrade.md#before-you-start).

The release bundle is the other supported source, and the one to use when the machine has no
install checkout. A bundle carries sources and no configuration, so give the run the install's
`install.env`:

```bash
curl -fsSLO https://github.com/gke-labs/kube-agents/releases/download/<RELEASE_VERSION>/kube-agents-<RELEASE_VERSION>.tar.gz
tar -xzf kube-agents-<RELEASE_VERSION>.tar.gz
cd kube-agents-<RELEASE_VERSION>
cp /path/to/the/install/install.env .
./upgrade.sh --upgrade-mode="full" --non-interactive --gcp-project-id="<PROJECT_ID>"
```

Run the bundle's own `./upgrade.sh`, not a newer one piped into a bundle directory: the sources
applied would be the unpacked release's while the images came from the piped script's. A bundle
that is not the release being asked for is refused by name.

## Upgrade Modes

- `--upgrade-mode=harness`: one `helm upgrade --reset-values` over the release's recorded values re-tagging the Platform Agent image (`platformAgent.deployment.image.tag`), the shell sandbox image (`agentSandbox.image.tag`) and every plugin image tag the release's values record (`plugins.pubsubPlatform.image.tag`, `plugins.stockoutInvestigator.image.tag`), followed by a read-back of the gateway Deployment's release images against the tag. Requires `jq`.
- `--upgrade-mode=operator`: applies the chart's CRDs with `kubectl` first (Helm never touches `crds/` on upgrade), then the same `helm upgrade` re-tagging only the operator image. Both modes stop before any of the new release is applied, naming each recorded key the chart's `values.schema.json` refuses as undeclared; on an upgrade that is a renamed or removed setting, so use `--upgrade-mode=full`. `--drop-undeclared-values` drops and names those keys instead, for a rollback to a release that predates them; a later release that declares a dropped key renders it from that chart's default until a full-mode run there.
- `--upgrade-mode=full` (Default): applies the CRDs, then runs a full `terraform apply` at the new `--image-tag` through the install engine — both image tags move and every setting in `install.env` is re-rendered. This mode additionally requires the `terraform` CLI.

Every mode requires the `kube-agents` Helm release to exist in the target namespace. An install
without one predates the Terraform + Helm engine: upgrade it with the release that installed it
(curl the matching versioned `upgrade.sh`), or re-install with `install.sh` to adopt the new
engine.

## Dry-Run Mode

To preview the upgrade plan and output a JSON status report without modifying cloud resources:

```bash
./upgrade.sh --dry-run --upgrade-mode=full --gcp-project-id="<PROJECT_ID>"
```

Machine-readable JSON status reports are generated at `/tmp/kube-agents-upgrade-report.json`.

A release-pinned copy of `upgrade.sh` needs nothing more, and neither does a checkout cloned at a
release tag: release tags sit on commits stamped with the version, so it is a release copy too. A
checkout with no baked release version (a clone of `main` or of a branch) asks for the tag on the
terminal, so without one — the way an agent runs it — it exits 1 with `--image-tag is required`.
Add `--image-tag=<RELEASE_TAG>` (a validated release tag or full commit SHA), or `--keep-image-tag`
to preview everything except the images.

## Targeting a Revision Other Than the Script's Own

A release copy of `upgrade.sh` already knows the version it upgrades to, so an upgrade to a
published release passes no tag at all. `--image-tag` overrides that default, and exists for
development and CI/CD testing — a candidate commit SHA, or a release other than the script's own:

```bash
# CI / testing override, not the path an install takes to a published release.
./upgrade.sh --non-interactive --upgrade-mode=full \
  --gcp-project-id="<PROJECT_ID>" \
  --image-tag="<SEMVER_TAG_OR_FULL_COMMIT_SHA>"
```

Use a SemVer release tag or the full 40-character commit SHA behind a validated RC tag; mutable
refs such as `latest` and `main` are rejected so the upgrade scripts and container images stay on
the same revision. A copy of the script carrying no baked version — one built from `main` — has no
default, and there the flag is the only way to name a revision.

Two flags change what the run targets, and both read differently depending on whether the copy of
the script carries a baked version. A release copy's version is in place before any flag is parsed,
with one exception: `upgrade.sh` in a checkout of a release line (`release/<X.Y>`) that has moved
past its latest release, with that release's tag and full history fetched, carries the release's
version but is not the release, so run from there it drops the baked default, says which line and commit it is on, and
behaves as a copy with no baked version below (asks for `--image-tag`, accepts `--keep-image-tag`,
plans at the installed tag):

- `--plan` reports what a full upgrade would change against the install's real Terraform state, and
  changes nothing. Exit 0 means in sync, 2 means there are changes, 1 means the plan failed. This is
  the only preview that can see drift; `--dry-run` above answers offline from configuration alone
  and plans against empty local state, so the two are refused together. `--image-tag` **is** accepted
  alongside it, and plans at that tag — which is what a drift check of a specific candidate wants.
  A release copy plans at its own baked release; a copy with no baked version and no `--image-tag`
  plans at the tag the install's Terraform state records (falling back to the tag the running agent
  Deployment serves if state records none).
- `--keep-image-tag` upgrades everything except the images, leaving them on the tag the install
  already serves. It refuses `--image-tag`, because the two ask for opposite things — and a release
  copy carries a version, so it refuses this flag too. It is what a scheduled reconcile of an
  environment that tracks `main` uses, from a checkout.

When `--keep-image-tag` (or a tagless `--plan` whose state records no tag) reads the running tag off the
agent Deployment, it validates it exactly as a passed one, so an install serving a mutable ref stops the
run rather than writing that ref into the composition.

#!/usr/bin/env python3
"""Pre-flight verification for onboarding a GCP project into the CI evaluation pool.

Validates that a project has completed every prerequisite in
docs/site/src/content/docs/deploy/ci-pool-projects.md before it is registered in
the Boskos resource pool in gke-internal/test-infra.

Registering a project that has not finished onboarding does not fail only that
project: Boskos hands the half-built lease to some pull request, and that pull
request's smoke test dies. Run this before the Boskos entry lands.

Usage:
    python3 scripts/verify_ci_pool_project.py --project-id kube-agents-evals-3
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
_CI_DEPLOY = _ROOT / "hack" / "ci-deploy.sh"

# No gcloud or gh call here should take anywhere near this long. The ceiling
# exists so a hung call fails the run instead of hanging a CI job forever.
DEFAULT_TIMEOUT_SECONDS = 120

REQUIRED_APIS = {
    # bench/tf/fleet declares google_compute_disk (the planted orphan-pd-* the
    # cost audit looks for), so the fleet stack depends on Compute directly and
    # not only transitively through GKE.
    "compute.googleapis.com",
    "container.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "iam.googleapis.com",
    "cloudkms.googleapis.com",
}

HOST_CLUSTER = "platform-agent-host"
EXPECTED_CLUSTERS = {HOST_CLUSTER, "seeded-a", "seeded-b", "seeded-c"}

# The two states k8s-operator/scripts/installer_common.sh accepts in
# is_valid_cmek_encryption_state(). Its only caller is install.sh's
# ensure_existing_cluster_cmek(), which -- unless ALLOW_UNENCRYPTED_SECRETS is
# truthy -- does not reject an unencrypted cluster but rewrites the live control
# plane to add CMEK, several minutes of in-place update. full-install creates
# the host cluster encrypted, so a state outside this set is drift, and a
# name-only check would pass it.
VALID_CMEK_STATES = {"ENCRYPTED", "ALL_OBJECTS_ENCRYPTION_ENABLED"}

DEFAULT_GITHUB_APP_ID = 4675512

# Mirrors terraform/modules/github-minter/main.tf: the key is ASYMMETRIC_SIGN /
# RSA_SIGN_PKCS1_2048_SHA256 and import_only, and the KSA that impersonates the
# minter GSA is kubeagents-github-minter in the kubeagents-system namespace.
KMS_KEYRING = "github-token-minter-keyring"
KMS_KEY = "github-token-minter-key"
KMS_KEY_PURPOSE = "ASYMMETRIC_SIGN"
KMS_KEY_ALGORITHM = "RSA_SIGN_PKCS1_2048_SHA256"
MINTER_KSA = "kubeagents-system/kubeagents-github-minter"

# GET /app authenticates as the App itself and echoes back its numeric id, which
# is what makes it a usable identity probe rather than just a reachability test.
GITHUB_APP_URL = "https://api.github.com/app"

# GitHub rejects an App JWT whose `exp` is more than ten minutes ahead. Building
# the payload, shelling out to gcloud, and the round trip all elapse between
# reading the clock and GitHub reading the claim, so exactly 600 sits on the
# boundary and fails intermittently on skew. Nine minutes leaves the margin.
JWT_LIFETIME_SECONDS = 540
JWT_BACKDATE_SECONDS = 60


# Roles that carry artifactregistry.repositories.uploadArtifacts. A literal
# roles/artifactregistry.writer binding is the documented grant and the one
# provision_ci_pool_project.sh makes, but the pool projects that predate this
# script have no such binding -- Cloud Build gets upload rights through
# builds.builder, and the Compute default SA through the editor role GCP grants
# it by default. Demanding the literal role would fail those projects for a
# permission they demonstrably hold.
#
# roles/owner is deliberately absent. It would satisfy the permission, but a
# build identity holding owner is a finding in its own right, and listing it
# here would turn the worst configuration this check could meet into a pass.
# The same applies to the node account below, which inherits this set through
# AR_PULLER_ROLES: a node pool running as owner is reported as holding no
# qualifying pull role, which reads oddly but is the answer we want.
AR_WRITER_ROLES = {
    "roles/artifactregistry.writer",
    "roles/artifactregistry.repoAdmin",
    "roles/artifactregistry.admin",
    "roles/cloudbuild.builds.builder",
    "roles/editor",
}

# Pushing is not pulling, and the identities are not the same one. The build
# writes the PR image; the host cluster's NODES read it back to start the
# operator and agent pods. Checking only the push side passes a project where
# Cloud Build can push and nothing can pull -- which satisfies every other check
# here and then fails at the first lease with ImagePullBackOff on the two
# Deployments the smoke test waits for. That is precisely the "looks
# provisioned, dies on lease" outcome this script exists to prevent, so the pull
# side gets its own assertion against the node account specifically.
#
# Every role in AR_WRITER_ROLES already confers read on the repository, so the
# puller set is that set plus the reader role provision_ci_pool_project.sh
# grants the node account directly.
AR_PULLER_ROLES = AR_WRITER_ROLES | {"roles/artifactregistry.reader"}


class CheckResult:
    def __init__(
        self,
        name: str,
        passed: bool,
        message: str = "",
        details: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
    ):
        self.name = name
        self.passed = passed
        self.message = message
        self.details = details or []
        # Things this run could not determine, as opposed to things it found
        # wrong. A token without the scope to read something is a visibility
        # limit, not a proven misconfiguration, and must not block onboarding.
        self.warnings = warnings or []


def run_cmd(cmd: List[str], timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # 124 is what GNU timeout(1) reports, so a caller that only looks at the
        # code still sees a failure rather than a success.
        return 124, "", f"timed out after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _load_json(out: str):
    """Parse command output as JSON, tolerating `gh --jq`'s multi-object form.

    `gh api --jq` emits one JSON value per match, newline-separated, which is not
    a JSON document. Two matching installations would otherwise raise
    JSONDecodeError out of a check function and end the run in a traceback
    instead of a reported failure.
    """
    text = out.strip()
    if not text:
        raise ValueError("empty output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        first = text.splitlines()[0]
        return json.loads(first)


def check_codebase_mapping(project_id: str) -> CheckResult:
    """Verify the hack/ci-deploy.sh mapping row for this project."""
    if not _CI_DEPLOY.exists():
        return CheckResult("Codebase GitOps Mapping", False, f"Missing {_CI_DEPLOY}")

    text = _CI_DEPLOY.read_text(encoding="utf-8")
    m = re.search(r"gitops_repo_for_project\(\)\s*\{(.*?)\n\}", text, re.DOTALL)
    if not m:
        return CheckResult("Codebase GitOps Mapping", False, "Could not find gitops_repo_for_project() in hack/ci-deploy.sh")

    func_body = m.group(1)
    expected_repo = f"gke-agentic/{project_id}-infra"
    pattern = rf"{re.escape(project_id)}\)\s*echo\s*[\"']?{re.escape(expected_repo)}[\"']?"
    if not re.search(pattern, func_body):
        return CheckResult(
            "Codebase GitOps Mapping",
            False,
            f"No mapping for {project_id} in gitops_repo_for_project() in hack/ci-deploy.sh",
            details=[f"Expected: {project_id}) echo \"{expected_repo}\" ;;"],
        )

    return CheckResult("Codebase GitOps Mapping", True, f"Mapped to {expected_repo}")


def check_project_and_apis(project_id: str) -> Tuple[Optional[str], CheckResult]:
    """Verify the project exists, read its number, and check enabled APIs."""
    rc, out, err = run_cmd(["gcloud", "projects", "describe", project_id, "--format=json"])
    if rc != 0:
        return None, CheckResult("GCP Project & APIs", False, f"Project describe failed: {err.strip()}")

    try:
        project_number = _load_json(out).get("projectNumber")
    except Exception as exc:
        return None, CheckResult("GCP Project & APIs", False, f"Failed parsing project description: {exc}")

    rc, out, err = run_cmd([
        "gcloud", "services", "list",
        f"--project={project_id}",
        "--enabled",
        "--format=value(config.name)",
    ])
    if rc != 0:
        return project_number, CheckResult("GCP Project & APIs", False, f"Failed listing enabled services: {err.strip()}")

    missing_apis = REQUIRED_APIS - set(out.split())
    if missing_apis:
        return project_number, CheckResult(
            "GCP Project & APIs",
            False,
            f"Missing {len(missing_apis)} required API(s)",
            details=[f"Missing API: {api}" for api in sorted(missing_apis)],
        )

    return project_number, CheckResult(
        "GCP Project & APIs", True, f"Project number: {project_number}, all {len(REQUIRED_APIS)} APIs enabled"
    )


def check_iam_and_service_accounts(project_id: str, project_number: str) -> CheckResult:
    """Verify Workload Identity and the cross-project Artifact Registry reader grants."""
    details = []
    passed = True

    gsa_email = f"kubeagents-platform-gsa@{project_id}.iam.gserviceaccount.com"
    rc, out, err = run_cmd([
        "gcloud", "iam", "service-accounts", "get-iam-policy",
        gsa_email,
        f"--project={project_id}",
        "--format=json",
    ])
    if rc != 0:
        passed = False
        details.append(f"Missing GSA or failed reading policy for {gsa_email}")
    else:
        try:
            policy = _load_json(out)
            expected_member = f"serviceAccount:{project_id}.svc.id.goog[kubeagents-system/kubeagents-platform-agent]"
            wi_bound = any(
                b.get("role") == "roles/iam.workloadIdentityUser" and expected_member in b.get("members", [])
                for b in policy.get("bindings", [])
            )
            if not wi_bound:
                passed = False
                details.append(f"Workload Identity user binding missing on {gsa_email} for {expected_member}")
        except Exception as exc:
            passed = False
            details.append(f"Failed parsing policy for {gsa_email}: {exc}")

    # The warm cache image hack/ci-deploy.sh defaults CACHE_IMAGE to lives in the
    # `us` multi-region repository of kube-agents-prow, not in us-central1.
    rc, out, err = run_cmd([
        "gcloud", "artifacts", "repositories", "get-iam-policy",
        "kube-agents",
        "--project=kube-agents-prow",
        "--location=us",
        "--format=json",
    ])
    if rc != 0:
        passed = False
        details.append(f"Failed reading IAM policy for kube-agents-prow repository: {err.strip()}")
    else:
        try:
            policy = _load_json(out)
            cb_sa = f"serviceAccount:{project_number}@cloudbuild.gserviceaccount.com"
            compute_sa = f"serviceAccount:{project_number}-compute@developer.gserviceaccount.com"
            readers = set()
            for b in policy.get("bindings", []):
                if b.get("role") == "roles/artifactregistry.reader":
                    readers.update(b.get("members", []))
            if cb_sa not in readers:
                passed = False
                details.append(f"Cloud Build SA ({cb_sa}) missing roles/artifactregistry.reader on kube-agents-prow")
            if compute_sa not in readers:
                passed = False
                details.append(f"Compute SA ({compute_sa}) missing roles/artifactregistry.reader on kube-agents-prow")
        except Exception as exc:
            passed = False
            details.append(f"Failed parsing kube-agents-prow AR policy: {exc}")

    return CheckResult(
        "Service Accounts & IAM Grants",
        passed,
        "Workload Identity and cross-project AR reader grants verified" if passed else "IAM requirements missing",
        details=details,
    )


def _host_cluster_node_members(project_id: str, project_number: str) -> Tuple[List[str], Optional[str]]:
    """IAM members for the accounts platform-agent-host's nodes run as.

    Read off the cluster rather than assumed. A pool created with
    --service-account runs as that account, and asserting the Compute default SA
    against such a cluster would report a failure the project does not have. The
    seeded trio is deliberately not consulted: it runs its own
    seeded-fleet-nodes account and pulls no kube-agents image.

    One listing carries every cluster's pools, so no location is needed -- the
    same reason check_gke_and_state lists rather than describes. "default" is
    what the API returns for a pool that was never given an account, and it
    means the Compute Engine default SA.

    Returns (members, error). An error means the accounts could not be
    determined, which is not the same as the nodes being unable to pull.
    """
    rc, out, err = run_cmd([
        "gcloud", "container", "clusters", "list",
        f"--project={project_id}",
        "--format=value(name,nodePools[].config.serviceAccount)",
    ])
    if rc != 0:
        return [], f"could not list clusters: {err.strip()[:160]}"

    compute_default = f"{project_number}-compute@developer.gserviceaccount.com"
    accounts = set()
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) < 2 or fields[0] != HOST_CLUSTER:
            continue
        for sa in fields[1].split(";"):
            sa = sa.strip()
            if sa:
                accounts.add(compute_default if sa == "default" else sa)

    if not accounts:
        return [], f"{HOST_CLUSTER} not found or reports no node pools"
    return sorted(f"serviceAccount:{a}" for a in accounts), None


def check_artifact_registry(project_id: str, project_number: str, location: str = "us-central1") -> CheckResult:
    """Verify the project's own Artifact Registry repository, its cleanup policy, and push rights.

    hack/ci-deploy.sh defaults AR_REPO to
    <location>-docker.pkg.dev/<project>/kube-agents and pushes every PR image
    there. Without the repository the build has nowhere to land; without a
    cleanup policy the presubmit images accumulate without bound.
    """
    details = []
    passed = True

    rc, out, err = run_cmd([
        "gcloud", "artifacts", "repositories", "describe", "kube-agents",
        f"--location={location}",
        f"--project={project_id}",
        "--format=json",
    ])
    if rc != 0:
        passed = False
        details.append(f"Missing Artifact Registry repository kube-agents in {location}: {err.strip()[:160]}")
    else:
        try:
            repo = _load_json(out)
            if repo.get("format") != "DOCKER":
                passed = False
                details.append(f"Repository kube-agents has format {repo.get('format')}, expected DOCKER")
            if not repo.get("cleanupPolicies"):
                passed = False
                details.append(f"Repository kube-agents in {location} has no cleanup policy")
            # A dry-run policy reports what it would delete and deletes nothing,
            # so storage still grows without bound while the policy looks set.
            if repo.get("cleanupPolicyDryRun"):
                passed = False
                details.append("Cleanup policies are in dry-run mode; they will not delete anything")
        except Exception as exc:
            passed = False
            details.append(f"Failed parsing Artifact Registry repository: {exc}")

    # Which identity Cloud Build runs as is project-dependent -- the legacy
    # <number>@cloudbuild SA for older projects, the Compute Engine default SA
    # for newer ones -- so accept upload rights on either rather than
    # manufacturing a failure with no correct remediation. A grant can sit on
    # the project or directly on the repository, so both policies are consulted.
    build_sas = {
        f"serviceAccount:{project_number}@cloudbuild.gserviceaccount.com",
        f"serviceAccount:{project_number}-compute@developer.gserviceaccount.com",
    }
    writers = set()
    pullers = set()
    policy_read = False
    for cmd in (
        ["gcloud", "projects", "get-iam-policy", project_id, "--format=json"],
        [
            "gcloud", "artifacts", "repositories", "get-iam-policy", "kube-agents",
            f"--location={location}", f"--project={project_id}", "--format=json",
        ],
    ):
        rc, out, err = run_cmd(cmd)
        if rc != 0:
            continue
        try:
            policy = _load_json(out)
        except Exception:
            continue
        policy_read = True
        for b in policy.get("bindings", []):
            role = b.get("role")
            if role in AR_WRITER_ROLES:
                writers.update(b.get("members", []))
            if role in AR_PULLER_ROLES:
                pullers.update(b.get("members", []))

    warnings: List[str] = []
    node_pull_checked = False
    if not policy_read:
        passed = False
        details.append(f"Could not read any IAM policy granting image push rights on {project_id}")
    else:
        if not (build_sas & writers):
            passed = False
            details.append(
                f"Neither the Cloud Build SA nor the Compute SA holds a role granting image push on {project_id} "
                f"(any of: {', '.join(sorted(AR_WRITER_ROLES))}); PR image pushes will fail"
            )

        node_members, node_err = _host_cluster_node_members(project_id, project_number)
        if node_err:
            # An unreadable cluster is not a cluster whose nodes cannot pull.
            # Reporting this as a failure would be the same conflation
            # check_toolchain exists to remove, so it goes to the operator as an
            # item to look at and the run exits 2.
            warnings.append(
                f"Could not determine which account {HOST_CLUSTER}'s nodes run as ({node_err}), "
                "so their pull rights on the kube-agents repository were not checked"
            )
        else:
            node_pull_checked = True
            starved = [m for m in node_members if m not in pullers]
            if starved:
                passed = False
                details.append(
                    f"{HOST_CLUSTER}'s node account(s) {', '.join(sorted(starved))} hold no role granting "
                    f"image pull on {project_id} (any of: {', '.join(sorted(AR_PULLER_ROLES))}); the build "
                    "will push and every pod will land in ImagePullBackOff on the first lease"
                )

    # The summary must not assert the item the warning above retracts. A line
    # reading "...and node pull rights" over a warning saying they could not be
    # checked is the same false assurance the exit-2 code exists to prevent, and
    # it would be worst on exactly the run where the operator most needs to read
    # the warning.
    if not passed:
        message = "Artifact Registry not ready"
    elif node_pull_checked:
        message = f"kube-agents ({location}) present with a cleanup policy, push rights, and node pull rights"
    else:
        message = f"kube-agents ({location}) present with a cleanup policy and push rights; node pull rights not checked"

    return CheckResult(
        "Artifact Registry Repository",
        passed,
        message,
        details=details,
        warnings=warnings,
    )


def check_gke_and_state(project_id: str) -> CheckResult:
    """Verify the host cluster, its CMEK state, the seeded fleet, and the state bucket."""
    details = []
    passed = True

    # name and encryption state in one listing: a separate describe would need
    # the cluster's location, which this call is what would have told us.
    rc, out, err = run_cmd([
        "gcloud", "container", "clusters", "list",
        f"--project={project_id}",
        "--format=value(name,databaseEncryption.state)",
    ])
    if rc != 0:
        return CheckResult("GKE Clusters & Seeded Fleet", False, f"Failed listing clusters: {err.strip()}")

    encryption_by_cluster = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        encryption_by_cluster[fields[0]] = fields[1] if len(fields) > 1 else ""

    missing_clusters = EXPECTED_CLUSTERS - set(encryption_by_cluster)
    if missing_clusters:
        passed = False
        details.append(f"Missing GKE cluster(s): {', '.join(sorted(missing_clusters))}")

    if HOST_CLUSTER in encryption_by_cluster:
        state = encryption_by_cluster[HOST_CLUSTER]
        if state not in VALID_CMEK_STATES:
            passed = False
            details.append(
                f"{HOST_CLUSTER} databaseEncryption.state is '{state or 'unset'}', not one of "
                f"{', '.join(sorted(VALID_CMEK_STATES))}; full-install creates the host cluster "
                "encrypted, so this is drift"
            )

    state_bucket = f"gs://{project_id}-tf-state"
    rc, out, err = run_cmd(["gcloud", "storage", "buckets", "describe", state_bucket])
    if rc != 0:
        passed = False
        details.append(f"Missing Terraform state bucket: {state_bucket}")

    return CheckResult(
        "GKE Clusters & Seeded Fleet",
        passed,
        f"All 4 clusters ({', '.join(sorted(EXPECTED_CLUSTERS))}), CMEK, and state bucket present"
        if passed
        else "GKE/Fleet resources missing",
        details=details,
    )


def check_github_repo_and_app(
    project_id: str, app_id: int, repo_membership_confirmed: bool = False
) -> CheckResult:
    """Verify the GitOps repository exists, is private, and is in the App's installation.

    repo_membership_confirmed records that a human has read the installation's
    repository list on github.com. The script cannot read that list itself, so
    without it the membership item stays unverified and the run cannot go green.
    """
    details = []
    warnings: List[str] = []
    passed = True
    attested = False
    repo_slug = f"gke-agentic/{project_id}-infra"

    rc, out, err = run_cmd(["gh", "repo", "view", repo_slug, "--json", "isPrivate,name"])
    if rc != 0:
        passed = False
        details.append(f"Repository {repo_slug} not found or inaccessible: {err.strip()}")
    else:
        try:
            if not _load_json(out).get("isPrivate"):
                passed = False
                details.append(f"Repository {repo_slug} is not private")
        except Exception as exc:
            passed = False
            details.append(f"Failed parsing gh repo view: {exc}")

    rc, out, err = run_cmd([
        "gh", "api", "/orgs/gke-agentic/installations",
        "--jq", f".installations[] | select(.app_id=={app_id}) | {{id, repository_selection}}",
    ])
    if rc != 0 or not out.strip():
        passed = False
        details.append(f"GitHub App {app_id} installation not found on org gke-agentic")
    else:
        try:
            inst = _load_json(out)
            if inst.get("repository_selection") != "selected":
                passed = False
                details.append(
                    f"GitHub App {app_id} repository_selection must be 'selected' "
                    f"(got {inst.get('repository_selection')})"
                )

            # The installation existing says nothing about THIS project's
            # repository being in it -- app_id and repository_selection are
            # properties of the installation and read the same for every
            # project. kube-agents-evals-3 had its repository created on
            # 2026-08-21 and added to the installation on 2026-08-23; for those
            # two days a check that stopped here reported success on precisely
            # what was missing.
            #
            # Listing an installation's selected repositories needs a token
            # authorized to the App itself -- an installation access token, or a
            # user-to-server token of App 4675512. An operator PAT is neither,
            # and no OAuth scope converts it into one (GitHub answers 403 and
            # misreports the cause as a missing `user` scope). We also cannot
            # mint an installation token here: the App private key is imported
            # directly into KMS and never leaves it.
            #
            # So report this as unverified rather than failed. Refusing to
            # onboard a project because of a limit in our own credentials would
            # be a false negative, and the operator can confirm it in one click.
            inst_id = inst.get("id")
            rc, out, err = run_cmd([
                "gh", "api", "--paginate",
                f"/user/installations/{inst_id}/repositories",
                "--jq", ".repositories[].full_name",
            ])
            if rc != 0 and repo_membership_confirmed:
                # Recorded as attested, not as checked. The distinction is the
                # point: the summary line has to keep saying which of the two it
                # was, or the flag becomes a way to silence the check.
                attested = True
            elif rc != 0:
                warnings.append(
                    f"Could not read installation {inst_id}'s repository list with this token "
                    "(expected: needs a token authorized to the App). "
                    f"Open https://github.com/organizations/gke-agentic/settings/installations/{inst_id}, "
                    f"check that {repo_slug} is in the repository list, then re-run with "
                    "--confirmed-repo-in-app-installation"
                )
            elif repo_slug not in out.split():
                passed = False
                details.append(
                    f"{repo_slug} is not in GitHub App {app_id}'s installation; "
                    "the minter cannot issue a token for it"
                )
        except Exception as exc:
            passed = False
            details.append(f"Failed parsing GitHub App installation: {exc}")

    if not passed:
        message = "GitHub configuration incomplete"
    elif warnings:
        message = f"Repo {repo_slug} (private); installation membership NOT verified"
    elif attested:
        message = f"Repo {repo_slug} (private); membership operator-confirmed, not machine-checked"
    else:
        message = f"Repo {repo_slug} (private) is in App {app_id}'s installation"

    return CheckResult(
        "GitOps Repo & GitHub App Installation",
        passed,
        message,
        details=details,
        warnings=warnings,
    )


def _probe_github_app_identity(
    project_id: str, location: str, version: str, app_id: int, timeout: int = 15
) -> Tuple[str, str]:
    """Sign an App JWT with the KMS key and see whether GitHub accepts it as app_id.

    Every other minter check reads configuration. This one is the only evidence
    that the material imported into KMS is a private key of *this* App: KMS holds
    opaque bytes, so a PEM belonging to some other App imports cleanly, reports
    ENABLED, satisfies every attribute check, and fails for the first time at a
    real push weeks later.

    Returns (status, message) with status in {"ok", "failed", "unverified"}. Only
    a 401, or an id that is not app_id, is evidence the key is wrong. A timeout,
    a 5xx, or a blocked egress leaves the question open, which is a different
    outcome and must not fail a project whose configuration is clean -- the
    gcloud calls above reach cloudkms.googleapis.com while this one reaches
    api.github.com, so one can be unreachable while the other is fine.
    """

    def _b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(
        json.dumps(
            {"iat": now - JWT_BACKDATE_SECONDS, "exp": now + JWT_LIFETIME_SECONDS, "iss": str(app_id)},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = header + b"." + payload

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, "jwt.in")
        sig_path = os.path.join(tmpdir, "jwt.sig")
        with open(in_path, "wb") as fh:
            fh.write(signing_input)

        rc, _, err = run_cmd([
            "gcloud", "kms", "asymmetric-sign",
            f"--location={location}",
            f"--keyring={KMS_KEYRING}",
            f"--key={KMS_KEY}",
            f"--version={version}",
            "--digest-algorithm=sha256",
            f"--input-file={in_path}",
            f"--signature-file={sig_path}",
            f"--project={project_id}",
        ])
        if rc != 0:
            # Usually the account running this lacks
            # cloudkms.cryptoKeyVersions.useToSign on the key. That is a limit of
            # the credential, not a defect in the project, so it reports as
            # unchecked -- and there is no manual substitute to offer instead:
            # whether the bytes in KMS belong to this App is not something anyone
            # can establish by looking at a console.
            return "unverified", (
                f"Could not sign a test JWT with {KMS_KEY} version {version}, so the imported key was "
                f"never matched against App {app_id}: {err.strip()[:200]}. "
                f"Signing needs cloudkms.cryptoKeyVersions.useToSign on {KMS_KEY}"
            )
        with open(sig_path, "rb") as fh:
            signature = fh.read()

    # A live installation-grade credential for the App, valid for nine minutes.
    # It stays in this local and must never reach a detail line, a warning, or
    # stdout -- including from a future "helpful" addition to an error message.
    token = (signing_input + b"." + _b64(signature)).decode("ascii")
    request = urllib.request.Request(
        GITHUB_APP_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "kube-agents-verify-ci-pool-project",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return "failed", (
                f"GitHub rejected a JWT signed by {KMS_KEY} version {version} (401 Unauthorized). "
                f"The material in KMS is not a private key of App {app_id}; re-import the correct PEM"
            )
        return "unverified", (
            f"GitHub answered HTTP {exc.code} ({exc.reason}) instead of accepting or rejecting the "
            f"signature, so the key was never matched against App {app_id}. Re-run when it clears"
        )
    except Exception as exc:  # timeout, DNS, blocked egress, untrusted CA, bad body
        # A python.org build with no CA bundle installed fails here while curl
        # and gcloud both succeed, so name the fix rather than sending the
        # operator to look for a firewall that is not there.
        remedy = (
            "point SSL_CERT_FILE at a CA bundle (/etc/ssl/cert.pem on macOS) and re-run"
            if "CERTIFICATE_VERIFY_FAILED" in str(exc)
            else "re-run from somewhere with egress to api.github.com"
        )
        return "unverified", (
            f"Could not reach {GITHUB_APP_URL} to match the KMS key against App {app_id} "
            f"({type(exc).__name__}: {exc}). Every other minter check passed; {remedy} to close this one"
        )

    returned_id = body.get("id")
    if returned_id != app_id:
        return "failed", (
            f"The key in KMS authenticated as GitHub App {returned_id}, not {app_id}. A PEM from the "
            "wrong App was imported; the minter will mint tokens for the wrong installation"
        )
    return "ok", f"signature accepted by GitHub as App {app_id} ({body.get('slug') or body.get('name')})"


def check_token_minter(
    project_id: str, app_id: int = DEFAULT_GITHUB_APP_ID, location: str = "us-central1"
) -> CheckResult:
    """Verify the token minter KMS key holds the right App's imported material and the GSA exists."""
    details = []
    warnings: List[str] = []
    passed = True
    key = KMS_KEY
    keyring = KMS_KEYRING
    enabled_versions: List[str] = []
    algorithm_ok = False

    rc, out, err = run_cmd([
        "gcloud", "kms", "keys", "versions", "list",
        f"--key={key}",
        f"--keyring={keyring}",
        f"--location={location}",
        f"--project={project_id}",
        "--format=json",
    ])
    if rc != 0:
        passed = False
        details.append(f"Cloud KMS key {key} in keyring {keyring} ({location}) not found or error: {err.strip()}")
    else:
        try:
            versions = _load_json(out)
            # The key existing only proves Terraform ran; the composition creates
            # it import-only and empty. An ENABLED version is what proves the PEM
            # was imported, which is the condition EVAL_GITHUB_APP_ID asserts.
            #
            # Which version is not pinned here. token-minter.md offers rotation
            # as "import a new key version to KMS; Minty picks it up", so after a
            # rotation version 1 is disabled and the live key is 2 -- hardcoding
            # 1 would fail a correctly rotated project and point the operator at
            # the PEM instead of at the rotation.
            for v in versions:
                if v.get("state") == "ENABLED":
                    enabled_versions.append(v.get("name", "").rsplit("/", 1)[-1])
            if not enabled_versions:
                passed = False
                details.append(f"KMS key {key} has no ENABLED version (PEM import pending via minty)")
            elif len(enabled_versions) > 1:
                # Not a failure: both versions verify. But Minty signs with the
                # one the chart names, so a stale version left enabled after a
                # rotation is a key that still works and should not.
                warnings.append(
                    f"KMS key {key} has {len(enabled_versions)} ENABLED versions "
                    f"({', '.join(sorted(enabled_versions))}); disable the superseded one"
                )
        except Exception as exc:
            passed = False
            details.append(f"Failed parsing KMS key versions: {exc}")

    # The key being the right shape. A symmetric or wrong-algorithm key would
    # hold an imported version and report ENABLED just the same, then fail at
    # the first signature. import_only is what keeps the PEM out of Terraform
    # state, so losing it is a disclosure regression, not just a config drift.
    rc, out, err = run_cmd([
        "gcloud", "kms", "keys", "describe", key,
        f"--keyring={keyring}",
        f"--location={location}",
        f"--project={project_id}",
        "--format=json",
    ])
    if rc != 0:
        passed = False
        details.append(f"Failed reading KMS key {key}: {err.strip()[:160]}")
    else:
        try:
            key_desc = _load_json(out)
            purpose = key_desc.get("purpose")
            algorithm = key_desc.get("versionTemplate", {}).get("algorithm")
            if purpose != KMS_KEY_PURPOSE:
                passed = False
                details.append(f"KMS key {key} purpose is {purpose}, expected {KMS_KEY_PURPOSE}")
            # Exact, not a substring match on "RSA_SIGN": RS256 means PKCS#1 v1.5
            # with SHA-256 specifically, so an RSA_SIGN_PSS_* key signs happily
            # and yields a JWT GitHub cannot verify. Catching it here turns an
            # opaque 401 from the probe below into a legible message.
            if algorithm != KMS_KEY_ALGORITHM:
                passed = False
                details.append(f"KMS key {key} algorithm is {algorithm}, expected {KMS_KEY_ALGORITHM}")
            else:
                algorithm_ok = True
            if not key_desc.get("importOnly"):
                passed = False
                details.append(
                    f"KMS key {key} is not import-only; the App private key could be written from Terraform"
                )
        except Exception as exc:
            passed = False
            details.append(f"Failed parsing KMS key description: {exc}")

    minter_gsa = f"kubeagents-github-minter-gsa@{project_id}.iam.gserviceaccount.com"

    # Being allowed to ask KMS to sign. Without this the pod reaches the key and
    # is refused, which looks like a GitHub auth failure rather than an IAM one.
    rc, out, err = run_cmd([
        "gcloud", "kms", "keys", "get-iam-policy", key,
        f"--keyring={keyring}",
        f"--location={location}",
        f"--project={project_id}",
        "--format=json",
    ])
    if rc != 0:
        passed = False
        details.append(f"Failed reading IAM policy for KMS key {key}: {err.strip()[:160]}")
    else:
        try:
            signers = set()
            for b in _load_json(out).get("bindings", []):
                if b.get("role") == "roles/cloudkms.signerVerifier":
                    signers.update(b.get("members", []))
            if f"serviceAccount:{minter_gsa}" not in signers:
                passed = False
                details.append(f"{minter_gsa} lacks roles/cloudkms.signerVerifier on {key}; it cannot sign a JWT")
        except Exception as exc:
            passed = False
            details.append(f"Failed parsing KMS key IAM policy: {exc}")

    # The pod being allowed to act as the minter GSA. Note this is a different
    # KSA from the platform agent's, in the same namespace -- checking only the
    # platform agent's binding would miss a minter that can never authenticate.
    rc, out, err = run_cmd([
        "gcloud", "iam", "service-accounts", "get-iam-policy",
        minter_gsa,
        f"--project={project_id}",
        "--format=json",
    ])
    if rc != 0:
        passed = False
        details.append(f"Missing Minter GSA or failed reading policy for {minter_gsa}")
    else:
        try:
            expected_member = f"serviceAccount:{project_id}.svc.id.goog[{MINTER_KSA}]"
            bound = any(
                b.get("role") == "roles/iam.workloadIdentityUser" and expected_member in b.get("members", [])
                for b in _load_json(out).get("bindings", [])
            )
            if not bound:
                passed = False
                details.append(f"Workload Identity binding missing on {minter_gsa} for {expected_member}")
        except Exception as exc:
            passed = False
            details.append(f"Failed parsing Minter GSA policy: {exc}")

    # Last, and only once the key is known to exist, hold enabled material, and
    # carry the right algorithm -- a probe against a key already known to be
    # wrong costs a network round trip to restate what was just reported.
    probe = ""
    if enabled_versions and algorithm_ok:
        status, message = _probe_github_app_identity(
            project_id, location, sorted(enabled_versions, key=lambda v: int(v) if v.isdigit() else 0)[-1], app_id
        )
        if status == "failed":
            passed = False
            details.append(message)
        elif status == "unverified":
            warnings.append(message)
        else:
            probe = f", {message}"

    message = (
        "Token minter not provisioned / PEM key missing or wrong"
        if not passed
        else f"Import-only signing key ENABLED, minter GSA can sign and be impersonated{probe}"
    )

    return CheckResult(
        "Token Minter KMS & GSA",
        passed,
        message,
        details=details,
        warnings=warnings,
    )


def run_checks(
    project_id: str,
    app_id: int = DEFAULT_GITHUB_APP_ID,
    location: str = "us-central1",
    repo_membership_confirmed: bool = False,
) -> List[CheckResult]:
    """Run every check and return the results. Prints nothing, so tests can assert on objects."""
    checks: List[CheckResult] = [check_codebase_mapping(project_id)]

    project_number, proj_check = check_project_and_apis(project_id)
    checks.append(proj_check)

    if project_number:
        checks.append(check_iam_and_service_accounts(project_id, project_number))
        checks.append(check_artifact_registry(project_id, project_number, location))
    else:
        checks.append(CheckResult("Service Accounts & IAM Grants", False, "Skipped: could not determine project number"))
        checks.append(CheckResult("Artifact Registry Repository", False, "Skipped: could not determine project number"))

    checks.append(check_gke_and_state(project_id))
    checks.append(check_github_repo_and_app(project_id, app_id, repo_membership_confirmed))
    checks.append(check_token_minter(project_id, app_id, location))
    return checks


EXIT_OK = 0
EXIT_FAILED = 1
# Distinct from both: nothing was found wrong, but something outage-causing
# could not be read. Registering on the strength of that is the mistake this
# script exists to prevent, so it does not get to share an exit code with a
# clean run.
EXIT_UNVERIFIED = 2


def report(project_id: str, checks: List[CheckResult]) -> int:
    print("\n" + "=" * 80)
    print(f" Pre-flight Onboarding Verification: {project_id}")
    print("=" * 80 + "\n")

    all_passed = True
    unverified = 0
    for c in checks:
        icon = "✓" if c.passed else "❌"
        if c.passed and c.warnings:
            icon = "?"
        print(f"[{icon}] {c.name}: {c.message}")
        if not c.passed:
            all_passed = False
            for d in c.details:
                print(f"    - {d}")
        for w in c.warnings:
            unverified += 1
            print(f"    ? {w}")

    print("\n" + "-" * 80)
    if not all_passed:
        print(f"PRE-FLIGHT CHECK FAILED. Do NOT register {project_id} in Boskos until the above are resolved.")
        status = EXIT_FAILED
    elif unverified:
        print(
            f"MANUAL VERIFICATION REQUIRED. Nothing failed, but {unverified} item(s) could not be checked "
            f"automatically. Confirm each one above before registering {project_id} in Boskos."
        )
        status = EXIT_UNVERIFIED
    else:
        print(f"ALL CHECKS PASSED. Project {project_id} is safe to register in Boskos.")
        status = EXIT_OK
    print("-" * 80 + "\n")
    return status


def check_toolchain() -> List[str]:
    """Reasons the checks below cannot be trusted, before any of them run.

    Every check reads a non-zero gcloud/gh exit as "the resource is absent", so a
    missing binary or an expired credential produces a confident, specific and
    wrong "not provisioned" for a project that is fine. Catch it here and exit 2
    -- unverified -- rather than 1.
    """
    blockers = []
    # An empty active-account list is exit 0 with no output, not an error, so the
    # logged-out case has to be read off stdout rather than the return code.
    rc, out, err = run_cmd(["gcloud", "auth", "list", "--format=value(account)", "--filter=status:ACTIVE"])
    if rc == 127:
        blockers.append("gcloud is not on PATH; every GCP check would report its resource as absent")
    elif rc != 0:
        blockers.append(f"gcloud auth list failed: {err.strip()}")
    elif not out.strip():
        blockers.append("gcloud has no active credential; every GCP check would report its resource as absent")

    rc, _, err = run_cmd(["gh", "auth", "status"])
    if rc == 127:
        blockers.append("gh is not on PATH; every GitHub check would report its resource as absent")
    elif rc != 0:
        blockers.append(f"gh is not authenticated: {err.strip()}")
    return blockers


def verify_project(
    project_id: str,
    app_id: int = DEFAULT_GITHUB_APP_ID,
    location: str = "us-central1",
    repo_membership_confirmed: bool = False,
) -> int:
    blockers = check_toolchain()
    if blockers:
        print("\n" + "=" * 80)
        print(f" Pre-flight Onboarding Verification: {project_id}")
        print("=" * 80 + "\n")
        for b in blockers:
            print(f"[?] {b}")
        print(
            f"\nMANUAL VERIFICATION REQUIRED. Nothing was checked, so nothing is known "
            f"about {project_id}. Fix the above and re-run.\n"
        )
        return EXIT_UNVERIFIED
    return report(project_id, run_checks(project_id, app_id, location, repo_membership_confirmed))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify CI pool project prerequisites before Boskos registration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes:\n"
            "  0  every prerequisite checked and passed\n"
            "  1  a prerequisite failed -- do not register this project\n"
            "  2  nothing failed, but something could not be checked automatically\n"
        ),
    )
    parser.add_argument("--project-id", required=True, help="GCP project ID to verify (e.g. kube-agents-evals-3)")
    parser.add_argument("--app-id", type=int, default=DEFAULT_GITHUB_APP_ID, help="GitHub App ID (default: 4675512)")
    parser.add_argument("--location", default="us-central1", help="GCP region/location (default: us-central1)")
    parser.add_argument(
        "--confirmed-repo-in-app-installation",
        action="store_true",
        help=(
            "Record that you have opened the GitHub App's installation settings page on github.com and "
            "seen gke-agentic/<project>-infra in its repository list. This script cannot read that list "
            "itself -- doing so needs a token authorized to the App, which an operator PAT cannot be -- so "
            "without this flag that one item reports as unverified and the run exits 2. Pass it only after "
            "actually looking; the URL is printed in the warning. The summary still marks the item "
            "operator-confirmed rather than machine-checked."
        ),
    )
    args = parser.parse_args()
    return verify_project(
        args.project_id, args.app_id, args.location, args.confirmed_repo_in_app_installation
    )


if __name__ == "__main__":
    sys.exit(main())

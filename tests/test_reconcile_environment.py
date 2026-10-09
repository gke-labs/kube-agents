"""The reconcile and drift-detection path for the long-lived environments.

`autopush` and `staging` are updated by image tag; nothing else runs Terraform
against them. Left at that, a month of infrastructure changes on `main` — IAM,
Pub/Sub, node pools, the chart values the composition renders — is invisible in
both while a green redeploy reports "main is deployed", which is what #1117
found.

Every assertion here is a way the fix goes silently wrong: a plan that mutates,
a plan that leaks a credential into a public artifact, an apply that starts
while somebody is live-testing, a reconcile that races the redeploy workflows
for the same Helm release.
"""

import importlib.util
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_RECONCILE_WF = _WORKFLOWS / "reconcile-environment.yml"
_DRIFT_WF = _WORKFLOWS / "drift-detect.yml"
_DEPLOY_WF = _WORKFLOWS / "deploy-environment.yml"
_LIFECYCLE = _REPO_ROOT / "terraform" / "examples" / "full-install" / "lifecycle.sh"
_TF_VARIABLES = _REPO_ROOT / "terraform" / "examples" / "full-install" / "variables.tf"
_RECONCILE_SH = _REPO_ROOT / "scripts" / "release" / "reconcile_environment.sh"
_UPGRADE_SH = _REPO_ROOT / "upgrade.sh"

_LONG_LIVED = ("autopush", "autopush-next", "staging")


def _executable_lines(chunk):
    """A shell chunk with its comment lines dropped.

    Asking whether a dispatch branch calls a helper by searching the raw text
    answers a different question: the branches here name the helpers they
    deliberately do not run, in the comments explaining why, and a call
    someone commented out while debugging still reads as present. Both
    failures are silent in the direction that matters -- a commented-out
    guard keeps its test green. LifecyclePlanTest.setUp strips the plan
    branch for the first of those reasons; this is the same filter, shared.
    """
    return "\n".join(line for line in chunk.splitlines()
                     if not line.lstrip().startswith("#"))


def _doc(path):
    return yaml.safe_load(path.read_text())


def _load_report_drift():
    spec = importlib.util.spec_from_file_location(
        "report_drift", _REPO_ROOT / "scripts" / "release" / "report_drift.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PubSubIamBindingTest(unittest.TestCase):
    """GCP purges a topic's IAM policy when the topic is recreated.

    A binding keyed on `.name` is known at plan time, so it is excluded from the
    plan that replaces its parent — the apply goes green and the live policy is
    empty. `.id` is computed, so a replacement renders it unknown and pulls the
    binding into the plan with it. That is #1059.
    """

    def test_no_pubsub_iam_member_binds_to_a_name(self):
        pattern = re.compile(
            r"^\s*(?:topic|subscription)\s*=\s*google_pubsub_\w+\.\w+\.name\s*$",
            re.MULTILINE)
        for path in sorted((_REPO_ROOT / "terraform").rglob("*.tf")):
            text = path.read_text()
            if "google_pubsub_topic_iam_member" not in text and \
               "google_pubsub_subscription_iam_member" not in text:
                continue
            with self.subTest(module=str(path.relative_to(_REPO_ROOT))):
                self.assertEqual(
                    pattern.findall(text), [],
                    "bind IAM members to the parent's .id, not .name — see #1059")


class DriftPlanReferenceTagTest(unittest.TestCase):
    """A plan's reference point is Terraform state, so its tag must be too.

    Reading the tag off the CLUSTER looks right and is not: the redeploy
    workflows move the running tag with `helm upgrade --reset-then-reuse-values`
    and never run Terraform, so autopush's cluster advances with every push to
    main while state stays where the last apply left it. Planning at the running
    tag renders an image_tag state does not have, helm_release.kube_agents plans
    an in-place update, and the daily report opens on image lag every day main
    has moved -- an infra-drift issue that never reaches the clean plan that
    closes it.
    """

    def _tf_state_image_tag(self, state_json, cat_rc=0):
        """Runs the helper against a stubbed `gcloud storage cat`."""
        with tempfile.TemporaryDirectory() as tmp:
            stub = pathlib.Path(tmp) / "gcloud"
            stub.write_text(
                "#!/usr/bin/env bash\n"
                "cat <<'STATE'\n%s\nSTATE\n"
                "exit %d\n" % (state_json, cat_rc))
            stub.chmod(0o755)
            script = (
                'set -euo pipefail\n'
                'PROJECT_ID=p; CLUSTER_NAME=c\n'
                '. "%s/scripts/installer/installer_common.sh"\n'
                'tf_state_image_tag\n' % _REPO_ROOT)
            proc = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True,
                env={"PATH": "%s:%s" % (tmp, os.environ.get("PATH", "/usr/bin:/bin")),
                     "HOME": os.environ.get("HOME", "/tmp")},
            )
        return proc

    def test_it_reads_the_recorded_tag_out_of_state(self):
        proc = self._tf_state_image_tag(
            '{"outputs": {"image_tag": {"value": "0.4.2"}}, "resources": []}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "0.4.2")

    def test_state_without_the_output_yields_nothing_rather_than_failing(self):
        """State written before the composition had the output. The caller
        falls back to the cluster, so this must not abort the run."""
        proc = self._tf_state_image_tag('{"outputs": {}, "resources": []}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_unreadable_or_corrupt_state_yields_nothing(self):
        for label, payload, rc in (
            ("no state object", "", 1),
            ("not json", "<html>403</html>", 0),
        ):
            with self.subTest(label=label):
                proc = self._tf_state_image_tag(payload, cat_rc=rc)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "")

    def test_the_composition_publishes_the_output_it_reads(self):
        """Nothing else puts the tag in state: Terraform does not record inputs."""
        outputs = (_REPO_ROOT / "terraform" / "examples" / "full-install"
                   / "outputs.tf").read_text()
        self.assertRegex(outputs, r'output "image_tag" \{')
        self.assertIn("value       = var.image_tag", outputs)

    def test_a_tagless_plan_prefers_state_over_the_cluster(self):
        """Order matters: the cluster read is the fallback, not the default."""
        text = (_REPO_ROOT / "upgrade.sh").read_text()
        guard = text.index(
            'if [ -z "$PARAM_IMAGE_TAG" ] && [ "$PARAM_PLAN" = "true" ]; then')
        state_read = text.index("tf_state_image_tag)")
        cluster_read = text.index('running_image_tag "$target_namespace"')
        # The guard opens the branch, the state read is inside it, and the
        # cluster read is the fallback after it — so an APPLY still moves the
        # images and only a plan holds them where state has them.
        self.assertLess(guard, state_read)
        self.assertLess(state_read, cluster_read)


class LifecyclePlanTest(unittest.TestCase):
    def setUp(self):
        self.text = _LIFECYCLE.read_text()
        # Everything between `plan)` and the `apply)` case that follows it.
        match = re.search(r"^  plan\)$(.*?)^  apply\)$", self.text,
                          re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, "lifecycle.sh has no plan subcommand")
        # Comments stripped, because this branch's comments name every one of
        # the commands below in the course of explaining why it does not run
        # them.
        self.branch = "\n".join(
            line for line in match.group(1).splitlines()
            if not line.lstrip().startswith("#"))

    def test_the_plan_branch_runs_nothing_that_writes(self):
        """A drift report that changes the thing it reports on is worse than none."""
        for forbidden in ("terraform apply", "terraform import", "terraform destroy",
                          "adopt_kms", "adopt_pubsub", "buckets create"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.branch)

    def test_the_plan_takes_no_state_lock(self):
        """So a plan can neither block nor be blocked by the apply it reports on."""
        self.assertIn("-lock=false", self.branch)

    def test_the_plan_does_not_create_the_state_bucket(self):
        """An empty bucket plans the whole composition as new, which reads as total drift."""
        self.assertIn("ensure_init readonly", self.branch)
        self.assertIn('if [[ "$mode" == "readonly" ]]; then', self.text)

    def test_detailed_exitcode_is_not_forced_on(self):
        """It changes what a non-zero exit means; only a caller expecting that may ask."""
        self.assertNotIn("-detailed-exitcode", self.branch)

    def test_apply_adopts_pre_existing_pubsub_resources(self):
        """Creating a topic that already exists is a 409, not a no-op (#1061)."""
        apply_branch = re.search(r"^  apply\)$(.*?)^  destroy\)$", self.text,
                                 re.MULTILINE | re.DOTALL).group(1)
        self.assertIn("adopt_pubsub", apply_branch)

    def test_apply_runs_every_identity_guard_before_terraform(self):
        """Each guard refuses a destroy-and-recreate an -auto-approve apply would not stop at."""
        apply_branch = re.search(r"^  apply\)$(.*?)^  destroy\)$", self.text,
                                 re.MULTILINE | re.DOTALL).group(1)
        apply_idx = apply_branch.index("terraform apply")
        for guard in ("guard_cluster_ownership", "guard_gsa_identity", "guard_release_namespace"):
            with self.subTest(guard=guard):
                self.assertIn(guard, apply_branch)
                self.assertLess(apply_branch.index(guard), apply_idx)

    def test_apply_guards_the_drift_trio_before_terraform_runs(self):
        """The trio is adopted by name nowhere now, so the only thing standing
        between a project's second install and the first install's audit
        ingress is this guard running ahead of the apply."""
        apply_branch = _executable_lines(
            re.search(r"^  apply\)$(.*?)^  destroy\)$", self.text,
                      re.MULTILINE | re.DOTALL).group(1))
        self.assertIn("guard_drift_adoption", apply_branch)
        self.assertLess(apply_branch.index("guard_drift_adoption"),
                        apply_branch.index("terraform apply"))

    def test_the_drift_guard_is_in_no_branch_but_apply(self):
        """`plan` reports without changing anything and `destroy` creates
        nothing, so neither can reach the 409 the guard pre-empts, and a guard
        that refused either would stop a read-only report and a teardown over
        a collision they do not cause. Read from the dispatch, because a
        misplaced guard exits 1 wherever it sits and looks right doing it.

        Comments stripped per branch: `plan)` documents the adopters it
        deliberately does not run by naming them, so the next line added
        there in that style would report `plan` as guarded -- and `*)`, whose
        usage text is not a split point because `*` is not `\\w`, is read as
        part of `destroy)`."""
        dispatch = self.text[self.text.index('case "${1:-}" in'):]
        # re.split keeps the captured branch names, so the result is
        # [preamble, name, body, name, body, ...].
        branches = re.split(r"^  (\w[\w-]*)\)$", dispatch, flags=re.MULTILINE)
        guarded = {
            branches[i]: "guard_drift_adoption" in _executable_lines(branches[i + 1])
            for i in range(1, len(branches) - 1, 2)
        }
        for branch in ("apply", "plan", "destroy"):
            with self.subTest(branch=branch):
                self.assertIn(branch, guarded, guarded)
        self.assertEqual([name for name, called in guarded.items() if called], ["apply"])

    def test_destroy_checks_the_release_namespace_before_deleting_the_cr(self):
        """delete_agent_cr looks in the configured namespace, so a wrong one skips the CR."""
        destroy_branch = re.search(r"^  destroy\)$(.*?)^  \*\)$", self.text,
                                   re.MULTILINE | re.DOTALL).group(1)
        self.assertLess(destroy_branch.index("guard_release_namespace"),
                        destroy_branch.index("delete_agent_cr"))

    def test_destroy_uninstalls_the_helm_releases_after_the_cr_and_before_terraform(self):
        """The helm provider's Delete can report a release gone without reaching the cluster."""
        destroy_branch = re.search(r"^  destroy\)$(.*?)^  \*\)$", self.text,
                                   re.MULTILINE | re.DOTALL).group(1)
        call = "\n    uninstall_helm_releases\n"
        self.assertLess(destroy_branch.index("delete_agent_cr"), destroy_branch.index(call))
        self.assertLess(destroy_branch.index(call), destroy_branch.index("terraform destroy -var"))

    def test_apply_forgets_unmanaged_cluster_kms_before_adopting(self):
        """adopt_kms re-reads state, so the removals have to land first (#1296)."""
        apply_branch = re.search(r"^  apply\)$(.*?)^  destroy\)$", self.text,
                                 re.MULTILINE | re.DOTALL).group(1)
        self.assertLess(apply_branch.index("forget_unmanaged_cluster_kms"),
                        apply_branch.index("adopt_kms"))

    def test_apply_guards_the_kms_names_before_terraform_runs(self):
        """The CMEK names are ForceNew and now come from install.env, so the
        guard has to sit with the other identity guards, ahead of the apply."""
        apply_branch = re.search(r"^  apply\)$(.*?)^  destroy\)$", self.text,
                                 re.MULTILINE | re.DOTALL).group(1)
        self.assertLess(apply_branch.index("guard_kms_identity"),
                        apply_branch.index("terraform apply"))

    def test_the_usage_range_still_covers_the_whole_header(self):
        """The fallback branch prints a fixed line range, so a longer header truncates it."""
        printed = re.search(r"sed -n '2,(\d+)p'", self.text)
        self.assertIsNotNone(printed)
        last = int(printed.group(1))
        lines = self.text.splitlines()
        # The header ends at the last comment line before `set -euo pipefail`.
        end = next(i for i, line in enumerate(lines, start=1)
                   if line.startswith("set -euo pipefail")) - 1
        self.assertEqual(last, end,
                         "update the sed range when the header comment changes length")


class PlanArtifactSafetyTest(unittest.TestCase):
    """The plan text is published as an artifact and quoted into an issue."""

    def test_every_credential_variable_is_marked_sensitive(self):
        """Terraform prints an unmarked variable's value in the plan, in full."""
        text = _TF_VARIABLES.read_text()
        blocks = re.findall(r'^variable "(\w+)" \{(.*?)^\}', text,
                            re.MULTILINE | re.DOTALL)
        secretish = re.compile(r"(_key|_token|_secret|_password|api_key|salt)$")
        # A KMS key is named by its resource path, not by its material: the path
        # is not a credential and is worth seeing in a plan. Anything else
        # ending in _key is.
        identifier = re.compile(r"kms|encryption")
        for name, body in blocks:
            if not secretish.search(name) or identifier.search(name):
                continue
            with self.subTest(variable=name):
                self.assertIn("sensitive   = true", body,
                              f"{name} would be printed in full in a published plan")


class ReconcileWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.doc = _doc(_RECONCILE_WF)
        self.job = self.doc["jobs"]["reconcile"]

    def test_it_is_guarded_against_forks(self):
        """A fork inherits workflow_dispatch and none of the credentials."""
        self.assertIn("github.repository == 'gke-labs/kube-agents'", self.job["if"])

    def test_it_shares_the_environment_lock_with_the_teardown_workflow(self):
        """A reconcile and a rebuild of the same cluster must never overlap."""
        self.assertEqual(
            self.job["concurrency"]["group"],
            _doc(_DEPLOY_WF)["jobs"]["deploy-environment"]["concurrency"]["group"])

    def test_the_terraform_wrapper_is_off(self):
        """The wrapper does not preserve `plan -detailed-exitcode`, which is the verdict."""
        setup = next(step for step in self.job["steps"]
                     if str(step.get("uses", "")).startswith("hashicorp/setup-terraform@"))
        self.assertIs(setup["with"]["terraform_wrapper"], False)

    def test_every_third_party_action_is_pinned_to_a_sha(self):
        for step in self.job["steps"]:
            uses = str(step.get("uses", ""))
            if not uses or uses.startswith("./"):
                continue
            with self.subTest(uses=uses):
                self.assertRegex(uses.split("@")[1].split()[0], r"^[0-9a-f]{40}$")

    def test_it_checks_out_the_revision_it_is_reconciling_to(self):
        """`upgrade.sh --image-tag=X` refuses a checkout whose HEAD is not X.

        The nightly promotes a candidate older than the commit it was dispatched
        from, so a checkout at the caller's ref makes every staging reconcile
        exit 1 on a version mismatch. And because the promotion job is decoupled
        from this job's outcome, the promotion tag goes out anyway: staging's
        images move and its infrastructure stays stale, which is the exact
        split this path exists to close. Empty falls back to the caller's ref,
        which is what the tagless autopush reconcile and every plan want.
        """
        checkout = next(step for step in self.job["steps"]
                        if str(step.get("uses", "")).startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["ref"], "${{ inputs.image_tag }}")

    def test_it_can_read_the_actions_api(self):
        """The in-flight redeploy check is a `gh run list`."""
        self.assertEqual(self.job["permissions"].get("actions"), "read")

    def test_skip_unconfigured_is_off_unless_asked_for(self):
        """On an environment that should exist, a missing one is the failure."""
        call = self.doc[True]["workflow_call"]["inputs"]["skip_unconfigured"]
        self.assertIs(call["default"], False)
        self.assertEqual(call["type"], "boolean")

    def test_a_missing_environment_is_never_bound(self):
        """Binding a GitHub environment that does not exist creates it.

        So the existence check runs in a job with no `environment:`, and the
        bound job only runs once it has answered yes.
        """
        preflight = self.doc["jobs"]["preflight"]
        self.assertNotIn("environment", preflight)
        self.assertIn("github.repository == 'gke-labs/kube-agents'", preflight["if"])
        self.assertEqual(self.job["needs"], "preflight")
        self.assertIn("needs.preflight.outputs.exists == 'true'", self.job["if"])

    def test_every_step_after_the_configuration_check_is_gated_on_it(self):
        steps = self.job["steps"]
        self.assertEqual(steps[0]["id"], "configured")
        self.assertEqual(steps[0]["env"]["GCP_PROJECT_ID"], "${{ vars.GCP_PROJECT_ID }}")
        for step in steps[1:]:
            with self.subTest(step=step.get("name")):
                self.assertIn("steps.", step.get("if", ""))
                if "steps.reconcile.outputs.plan_log" not in step["if"]:
                    self.assertEqual(step["if"], "steps.configured.outputs.skip != 'true'")

    def test_a_skip_is_reported_as_skipped(self):
        outputs = self.doc[True]["workflow_call"]["outputs"]
        self.assertEqual(outputs["result"]["value"],
                         "${{ jobs.reconcile.outputs.result || jobs.preflight.outputs.result }}")
        self.assertIn("'skipped'", self.job["outputs"]["result"])

    def test_every_variable_the_renderer_reads_is_passed_through(self):
        """The renderer reaches for the environment, never for `vars.` itself.

        So a setting the workflow does not export is not "unset on that
        environment" — it is unset everywhere, permanently, and a guard keyed on
        it either never fires or can never be satisfied. The allowlist guard is
        the second kind: without GOOGLE_CHAT_ALLOW_ALL_USERS on this step there
        is no way to tell it the open allowlist is deliberate.
        """
        exported = set()
        for step in self.job["steps"]:
            exported.update(step.get("env", {}))
        renderer = (_REPO_ROOT / "scripts" / "release"
                    / "render_install_env.sh").read_text()
        # Right of the colon in MAPPING is the name the workflow exports.
        mapped = set(re.findall(r"^[A-Z_][A-Z0-9_]*:([A-Z_][A-Z0-9_]*)$",
                                renderer, re.MULTILINE))
        # Plus the ones the guards read directly rather than through MAPPING.
        mapped.update({"MEMORY_PROVIDER", "GOOGLE_CHAT_ALLOW_ALL_USERS",
                       "SLACK_ALLOW_ALL_USERS"})
        self.assertTrue(mapped, "MAPPING did not parse; the regex is stale")
        missing = sorted(mapped - exported)
        self.assertEqual(missing, [], f"never reaches the renderer: {missing}")


_FAKE_CURL = """#!/usr/bin/env bash
calls="${FAKE_DIR}/calls"
echo call >> "${calls}"
n="$(wc -l < "${calls}")"
answer="$(sed -n "${n}p" "${FAKE_DIR}/answers")"
[ -n "${answer}" ] || answer="$(tail -n 1 "${FAKE_DIR}/answers")"
if [ "${answer}" = "transport" ]; then
  printf '000'
  exit 7
fi
printf '%s' "${answer}"
"""


class PreflightLookupTest(unittest.TestCase):
    """The existence check, run against a fake GitHub API.

    404 is the only answer that skips. A transient failure is retried, and one
    that outlasts the retries fails the job: reading an unreachable environment
    as an absent one would skip a deploy that should have happened.
    """

    def setUp(self):
        doc = _doc(_RECONCILE_WF)
        steps = doc["jobs"]["preflight"]["steps"]
        self.script = next(s for s in steps if s.get("id") == "exists")["run"]

    def _run(self, answers):
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp)
            (fake / "answers").write_text("\n".join(answers) + "\n")
            (fake / "calls").write_text("")
            for name, body in (("curl", _FAKE_CURL), ("sleep", "#!/bin/sh\nexit 0\n")):
                (fake / name).write_text(body)
                (fake / name).chmod(0o755)
            output = fake / "github_output"
            output.write_text("")
            env = {
                **os.environ,
                "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
                "FAKE_DIR": str(fake),
                "GITHUB_OUTPUT": str(output),
                "GITHUB_API_URL": "https://api.github.invalid",
                "GH_TOKEN": "token",
                "REPOSITORY": "gke-labs/kube-agents",
                "TARGET": "autopush-next",
                "SKIP_UNCONFIGURED": "true",
            }
            proc = subprocess.run(["bash", "-c", self.script], env=env, capture_output=True, text=True, check=False)
            calls = len((fake / "calls").read_text().splitlines())
            return proc.returncode, output.read_text(), calls

    def test_200_exists(self):
        self.assertEqual(self._run(["200"]), (0, "exists=true\n", 1))

    def test_404_is_the_only_skip(self):
        self.assertEqual(self._run(["404"]), (0, "exists=false\nresult=skipped\n", 1))

    def test_a_transient_failure_is_retried(self):
        self.assertEqual(self._run(["500", "transport", "429", "403", "200"]), (0, "exists=true\n", 5))

    def test_a_persistent_failure_fails_closed(self):
        code, output, calls = self._run(["500"])
        self.assertEqual((code, output), (1, ""))
        self.assertGreater(calls, 1)

    def test_a_bad_credential_fails_without_retrying(self):
        self.assertEqual(self._run(["401"]), (1, "", 1))

    def test_unset_skip_answers_yes_without_asking(self):
        self.script = self.script.replace('"${SKIP_UNCONFIGURED}"', '"false"', 1)
        self.assertEqual(self._run(["404"]), (0, "exists=true\n", 0))


class DriftWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.doc = _doc(_DRIFT_WF)

    def test_it_only_ever_plans(self):
        """Read-only is what lets it run on a schedule against a shared install."""
        self.assertEqual(self.doc["jobs"]["plan"]["with"]["mode"], "plan")

    def test_it_covers_every_long_lived_environment(self):
        """Reporting on autopush alone leaves staging exactly as stale."""
        matrix = self.doc["jobs"]["plan"]["strategy"]["matrix"]["environment"]
        self.assertEqual(sorted(matrix), sorted(_LONG_LIVED))

    def test_a_failing_environment_does_not_cancel_the_other(self):
        self.assertIs(self.doc["jobs"]["plan"]["strategy"]["fail-fast"], False)

    def test_the_report_covers_what_the_plan_covers(self):
        plan = self.doc["jobs"]["plan"]["strategy"]["matrix"]["environment"]
        report = self.doc["jobs"]["report"]["strategy"]["matrix"]["environment"]
        self.assertEqual(sorted(report), sorted(plan))

    def test_only_autopush_next_may_skip_while_unprovisioned(self):
        """A missing autopush or staging is a failure, not something to skip."""
        self.assertEqual(self.doc["jobs"]["plan"]["with"]["skip_unconfigured"],
                         "${{ matrix.environment == 'autopush-next' }}")

    def test_it_pins_no_image_tag(self):
        """Otherwise the report conflates image lag with infrastructure drift."""
        self.assertEqual(self.doc["jobs"]["plan"]["with"]["image_tag"], "")

    def test_it_is_guarded_against_forks(self):
        """A fork inherits `schedule` and mails its owner on every failure."""
        for name, job in self.doc["jobs"].items():
            with self.subTest(job=name):
                self.assertIn("github.repository == 'gke-labs/kube-agents'", job["if"])

    def test_it_runs_on_a_schedule(self):
        """The whole point: nothing said autopush was a month behind."""
        self.assertIn("schedule", self.doc[True])


_FAKE_GCLOUD = """#!/usr/bin/env bash
# Every read of the cluster answers what FAKE_CLUSTER says, as the real
# gcloud does: describe and get-credentials both 404 on an absent cluster.
# The Terraform state probe answers what FAKE_STATE says.
echo "$*" >> "${FAKE_DIR}/calls"
case "$*" in
  *"container clusters "*)
    case "${FAKE_CLUSTER}" in
      found)     echo "platform-agent-host" ;;
      notfound)  echo "ERROR: (gcloud.container.clusters.describe) ResponseError: code=404, message=Not found: projects/p/locations/us-central1/clusters/platform-agent-host." >&2; exit 1 ;;
      forbidden) echo "ERROR: (gcloud.container.clusters.describe) ResponseError: code=403, message=Required container.clusters.get permission(s) for projects/p/locations/us-central1/clusters/platform-agent-host." >&2; exit 1 ;;
      api-off)   echo "ERROR: (gcloud.container.clusters.describe) ResponseError: code=403, message=Kubernetes Engine API has not been used in project p before or it is disabled. Enable it by visiting https://console.developers.google.com/apis/api/container.googleapis.com/overview?project=p then retry." >&2; exit 1 ;;
    esac ;;
  *"storage cat"*)
    case "${FAKE_STATE}" in
      cluster-here)  echo '{"version": 4, "resources": [{"mode": "managed", "type": "google_container_cluster", "name": "autopilot", "instances": [{"attributes": {"name": "platform-agent-host", "project": "p", "location": "us-central1"}}]}]}' ;;
      cluster-else)  echo '{"version": 4, "resources": [{"mode": "managed", "type": "google_container_cluster", "name": "autopilot", "instances": [{"attributes": {"name": "platform-agent-host", "project": "p", "location": "us-east1"}}]}]}' ;;
      other-cluster) echo '{"version": 4, "resources": [{"mode": "managed", "type": "google_container_cluster", "name": "autopilot", "instances": [{"attributes": {"name": "somebody-elses-cluster", "project": "p", "location": "us-central1"}}]}]}' ;;
      mixed)         echo '{"version": 4, "resources": [{"mode": "managed", "type": "google_container_cluster", "name": "autopilot", "instances": [{"attributes": {"name": "platform-agent-host", "project": "p", "location": "us-central1"}}, {"attributes": {"name": "somebody-elses-cluster", "project": "p", "location": "us-central1"}}]}]}' ;;
      no-cluster)    echo '{"version": 4, "resources": [{"mode": "managed", "type": "google_kms_key_ring", "name": "gke_keyring", "instances": [{"attributes": {"location": "us-central1"}}]}]}' ;;
      garbage)       echo 'not json' ;;
      absent)        echo "ERROR: (gcloud.storage.cat) The following URLs matched no objects or files: gs://p-kube-agents-tfstate/kube-agents/platform-agent-host/default.tfstate" >&2; exit 1 ;;
      unreadable)    echo "ERROR: (gcloud.storage.cat) HTTPError 403: does-not-matter@p.iam.gserviceaccount.com does not have storage.objects.get access to the Google Cloud Storage object." >&2; exit 1 ;;
    esac ;;
esac
"""

# The real defaults file, so the object the step names is pinned against the
# installer's own values rather than a copy of them.
_FAKE_DEFAULTS = (_REPO_ROOT / "install.defaults.env").read_text()

_FAKE_LEASE = """import json, os, sys
with open(os.path.join(os.environ["FAKE_DIR"], "calls"), "a") as f:
    f.write("lease " + " ".join(sys.argv[1:]) + "\\n")
print(json.dumps([{"name": "x", "state": os.environ["FAKE_LEASE"]}]))
"""


class RebuildLeaseGuardTest(unittest.TestCase):
    """The lease guard, run against a fake gcloud and a fake lease tool.

    The lease ConfigMap lives on the cluster the rebuild is about to destroy.
    A project with no cluster yet has nothing to ask and nothing to tear down:
    that is how a long-lived environment is first created, and the guard must
    let it through rather than read the missing cluster as an unreadable
    lease. Only a genuine NOT_FOUND is that case; every other failure to reach
    the cluster still fails closed, as does a held or unreadable lease.
    """

    def setUp(self):
        steps = _doc(_DEPLOY_WF)["jobs"]["deploy-environment"]["steps"]
        self.script = next(s for s in steps
                           if s["name"] == "Refuse while somebody is live-testing")["run"]

    def _run(self, cluster, lease="free", state="absent", defaults=_FAKE_DEFAULTS):
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp)
            (fake / "calls").write_text("")
            (fake / "gcloud").write_text(_FAKE_GCLOUD)
            (fake / "gcloud").chmod(0o755)
            # The step runs from the candidate's checkout and wants the
            # renderer, the lease tool and the install defaults at their
            # repository paths.
            cwd = fake / "checkout"
            (cwd / "scripts" / "release").mkdir(parents=True)
            renderer = cwd / "scripts" / "release" / "render_install_env.sh"
            renderer.write_text("#!/bin/sh\nexit 0\n")
            renderer.chmod(0o755)
            (cwd / "scripts" / "live_test_lease.py").write_text(_FAKE_LEASE)
            (cwd / "install.defaults.env").write_text(defaults)
            env = {
                **os.environ,
                "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
                "FAKE_DIR": str(fake),
                "FAKE_CLUSTER": cluster,
                "FAKE_LEASE": lease,
                "FAKE_STATE": state,
                "TARGET": "autopush-next",
                "GCP_PROJECT_ID": "p",
                "GCP_REGION": "us-central1",
                "GKE_CLUSTER_NAME": "platform-agent-host",
                "NAMESPACE": "kubeagents-system",
            }
            proc = subprocess.run(["bash", "-c", self.script], cwd=cwd, env=env,
                                  capture_output=True, text=True, check=False)
            return proc.returncode, (fake / "calls").read_text(), proc.stdout + proc.stderr

    def test_no_cluster_yet_is_nothing_to_live_test_against(self):
        code, calls, out = self._run("notfound")
        self.assertEqual(code, 0, out)
        self.assertNotIn("get-credentials", calls)
        self.assertNotIn("lease ", calls)
        self.assertIn("::notice", out)

    def test_state_recording_the_cluster_elsewhere_is_a_coordinates_mismatch(self):
        """The teardown finds an install by project and cluster name, not by
        location. A describe that 404s while the state records the cluster at
        another location means the region variable is wrong, and proceeding
        would destroy a live cluster without its lease having been read."""
        code, calls, out = self._run("notfound", state="cluster-else")
        self.assertEqual(code, 1)
        self.assertNotIn("lease ", calls)
        self.assertIn("::error", out)
        self.assertIn("us-east1", out)

    def test_state_recording_the_cluster_here_means_it_is_gone(self):
        """State whose cluster sits at the configured location, where describe
        found none, is an install whose cluster is gone: nothing can hold a
        lease, and the teardown's own stale-state path clears the rest."""
        code, calls, out = self._run("notfound", state="cluster-here")
        self.assertEqual(code, 0, out)
        self.assertNotIn("lease ", calls)
        self.assertIn("::notice", out)

    def test_state_naming_another_cluster_refuses(self):
        """State accretes across runs that computed different answers, and an
        entry that names some other cluster is not ownership of this one: the
        installer's tf_state_has_cluster matches the name as well as the
        location. A destroy of that state would take the other cluster with it."""
        code, calls, out = self._run("notfound", state="other-cluster")
        self.assertEqual(code, 1)
        self.assertNotIn("lease ", calls)
        self.assertIn("::error", out)
        self.assertIn("somebody-elses-cluster", out)

    def test_state_recording_this_cluster_and_another_refuses(self):
        """A destroy of the state takes every cluster in it, so this cluster
        being gone does not make the other one safe to destroy unread."""
        code, calls, out = self._run("notfound", state="mixed")
        self.assertEqual(code, 1)
        self.assertNotIn("lease ", calls)
        self.assertIn("::error", out)
        self.assertIn("somebody-elses-cluster", out)

    def test_a_candidate_without_the_state_defaults_is_refused_by_name(self):
        """A candidate between the lease check and the state-location defaults
        passes the predates guard; it must still fail with a named reason, not
        an unbound-variable abort."""
        code, calls, out = self._run("notfound", defaults='DEFAULT_KUBE_AGENTS_STATE_BUCKET="auto"\n')
        self.assertEqual(code, 1)
        self.assertNotIn("lease ", calls)
        self.assertIn("::error", out)
        self.assertNotIn("unbound variable", out)

    def test_state_with_no_cluster_is_an_earlier_attempt(self):
        """A first dispatch that failed after writing state but before the
        cluster existed leaves state with no cluster in it. The retry has to
        get through, or the only way forward is deleting the object by hand."""
        code, calls, out = self._run("notfound", state="no-cluster")
        self.assertEqual(code, 0, out)
        self.assertNotIn("lease ", calls)
        self.assertIn("::notice", out)

    def test_state_that_cannot_be_read_as_state_fails_closed(self):
        code, calls, out = self._run("notfound", state="garbage")
        self.assertEqual(code, 1)
        self.assertNotIn("lease ", calls)
        self.assertIn("::error", out)

    def test_an_absent_cluster_with_unreadable_state_fails_closed(self):
        code, calls, out = self._run("notfound", state="unreadable")
        self.assertEqual(code, 1)
        self.assertNotIn("lease ", calls)
        self.assertIn("::error", out)

    def test_the_state_probe_names_the_installers_object(self):
        """The same object uninstall.sh's resolve_state_location reads."""
        _, calls, _ = self._run("notfound")
        self.assertIn("storage cat gs://p-kube-agents-tfstate/kube-agents/platform-agent-host/default.tfstate", calls)

    def test_the_absence_pattern_is_the_installers(self):
        """The step cannot source installer_common.sh, so it carries a copy of
        GCS_OBJECT_ABSENT_PATTERN. A copy that drifts reads a later gcloud
        wording of "absent" as "unknown" and blocks every first creation."""
        common = (_REPO_ROOT / "scripts" / "installer" / "installer_common.sh").read_text()
        pattern = re.search(r"readonly GCS_OBJECT_ABSENT_PATTERN='([^']+)'", common).group(1)
        self.assertIn(f"grep -qiE '{pattern}'", self.script)

    def test_an_existing_cluster_is_asked_for_its_lease(self):
        code, calls, out = self._run("found", lease="free")
        self.assertEqual(code, 0, out)
        self.assertIn("get-credentials", calls)
        self.assertIn("lease status --json", calls)

    def test_a_held_lease_still_refuses(self):
        code, calls, out = self._run("found", lease="held")
        self.assertEqual(code, 1)
        self.assertIn("::error", out)

    def test_a_disabled_kubernetes_engine_api_is_named(self):
        """A fresh project answers SERVICE_DISABLED, not NOT_FOUND. It is still
        a refusal, but one that names the API to enable rather than a blind
        teardown, because the project setup script enables it and the
        installer's own probe would refuse the same answer later."""
        code, calls, out = self._run("api-off")
        self.assertEqual(code, 1)
        self.assertNotIn("lease ", calls)
        self.assertIn("::error", out)
        self.assertIn("container.googleapis.com", out)

    def test_any_other_failure_to_reach_the_cluster_fails_closed(self):
        """A 403 is not an absent cluster; reading it as one would tear down blind."""
        code, calls, out = self._run("forbidden")
        self.assertEqual(code, 1)
        self.assertNotIn("lease ", calls)
        self.assertIn("::error", out)


class DeployEnvironmentGuardTest(unittest.TestCase):
    """The teardown-and-rebuild workflow can target a long-lived environment.

    Which makes it the one path that can destroy `autopush` or `staging`, so
    the guards in front of it are the only thing between a dropdown selection
    and a cluster somebody is working on.
    """

    def setUp(self):
        self.doc = _doc(_DEPLOY_WF)
        self.steps = self.doc["jobs"]["deploy-environment"]["steps"]

    def test_the_long_lived_environments_are_dispatchable(self):
        options = self.doc[True]["workflow_dispatch"]["inputs"]["github_environment"]["options"]
        for env in _LONG_LIVED:
            with self.subTest(environment=env):
                self.assertIn(env, options)

    def test_a_long_lived_teardown_needs_typed_confirmation(self):
        step = next(s for s in self.steps
                    if s["name"] == "Confirm a long-lived environment teardown")
        for env in _LONG_LIVED:
            self.assertIn(env, step["if"])
        self.assertIn('"${CONFIRM}" != "${TARGET}"', step["run"])

    def test_the_confirmation_runs_before_anything_costly(self):
        """It is a typo check; everything after the checkout costs minutes."""
        names = [s.get("name", "") for s in self.steps]
        self.assertEqual(names[0], "Confirm a long-lived environment teardown")

    def test_it_refuses_unless_the_live_test_lease_is_positively_free(self):
        """The lease ConfigMap lives in the cluster this is about to destroy.

        Matching "free" rather than negating "held" is what keeps an
        unreachable cluster — a transient API-server error, an expired auth
        plugin — from reading as an idle one. `status --json` reports that as
        its own state precisely so a caller can tell the two apart.
        """
        step = next(s for s in self.steps
                    if s["name"] == "Refuse while somebody is live-testing")
        self.assertIn("live_test_lease.py status --json", step["run"])
        self.assertIn('grep -q \'"state": "free"\'', step["run"])
        self.assertNotIn('grep -q \'"state": "held"\'', step["run"])


class ReconcileScriptTest(unittest.TestCase):
    def setUp(self):
        self.text = _RECONCILE_SH.read_text()

    def test_it_is_valid_bash(self):
        proc = subprocess.run(["bash", "-n", str(_RECONCILE_SH)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_it_renders_the_configuration_strictly(self):
        """A long-lived environment missing a setting must fail, not default."""
        self.assertIn("render_install_env.sh", self.text)
        self.assertIn("--strict", self.text)

    def test_only_an_apply_takes_the_lease(self):
        """A plan changes nothing, so holding the lease would only block people."""
        self.assertIn('if [ "$MODE" = "apply" ] && [ "$LEASE_POLICY" != "ignore" ]', self.text)

    def test_the_lease_is_claimed_rather_than_polled(self):
        """A separate "is it free?" read leaves a window before the apply."""
        self.assertRegex(self.text, r'live_test_lease\.py"?\s+acquire')
        self.assertIn("trap release_lease EXIT", self.text)
        self.assertIn("trap 'release_lease; exit 130' INT", self.text)
        self.assertIn("trap 'release_lease; exit 143' TERM", self.text)
        self.assertIn("trap 'release_lease; exit 129' HUP", self.text)

    def test_signals_release_lease_and_exit_nonzero(self):
        """SIGINT/SIGTERM/SIGHUP must terminate bash and not continue execution."""
        snippet = """
LEASE_HELD="true"
release_lease() {
  echo "RELEASED"
  LEASE_HELD="false"
}
trap release_lease EXIT
trap 'release_lease; exit 130' INT
trap 'release_lease; exit 143' TERM
trap 'release_lease; exit 129' HUP

kill -TERM $$
echo "UNREACHABLE_AFTER_SIGNAL"
"""
        proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 143)
        self.assertIn("RELEASED", proc.stdout)
        self.assertNotIn("UNREACHABLE_AFTER_SIGNAL", proc.stdout)

    def test_credentials_are_fetched_before_the_lease_is_taken(self):
        """The lease is a ConfigMap read; without a kubeconfig it cannot be read.

        `acquire` exits non-zero either way, so credentials fetched after it
        would make every scheduled reconcile defer, exit 0, and report green
        having applied nothing — the "green means deployed" failure this whole
        change exists to end.
        """
        # The invocation, not the first mention: the header comment names the
        # script several paragraphs above the code that runs it.
        creds = self.text.index("gcloud container clusters get-credentials")
        lease = self.text.index('live_test_lease.py" acquire')
        self.assertLess(creds, lease,
                        "get-credentials has to come before the lease is taken")

    def test_an_unreadable_lease_fails_rather_than_deferring(self):
        """A cluster that could not be asked has not answered "no"."""
        self.assertIn('grep -q \'"state": "held"\'', self.text)
        self.assertIn("Live-test lease could not be read", self.text)

    def test_an_apply_refuses_to_un_provision_the_minter(self):
        """enable_github_minter flips to false on a key with no ENABLED version.

        No variable expresses it, so REQUIRED_STRICT cannot cover it, and the
        installer's only signal is a warning in a log nobody reads.
        """
        self.assertIn("state=ENABLED", self.text)
        self.assertIn("DESTROY the minter", self.text)

    def test_the_minter_guard_reads_the_key_names_from_their_one_home(self):
        """A second copy of a default is how this guard queries a keyring that
        does not exist, finds no enabled version, and refuses every apply.

        install.defaults.env owns the names and installer_common.sh owns the
        location rule, so the guard must reach for both rather than restate
        either — the same requirement scripts/installer/README.md puts on
        install.sh, upgrade.sh and the chart.
        """
        self.assertIn("${KMS_KEY:-$DEFAULT_KMS_KEY}", self.text)
        self.assertIn("${KMS_KEYRING:-$DEFAULT_KMS_KEYRING}", self.text)
        self.assertIn('derive_kms_location "${REGION}"', self.text)
        for literal in ("github-token-minter-key", "github-token-minter-keyring",
                        "${REGION%-[a-z]}"):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, self.text)

    def test_the_names_it_reaches_for_are_the_ones_that_exist(self):
        """An assertion on spelling alone would survive a rename of either."""
        defaults = (_REPO_ROOT / "install.defaults.env").read_text()
        self.assertIn("DEFAULT_KMS_KEY=", defaults)
        self.assertIn("DEFAULT_KMS_KEYRING=", defaults)
        common = (_REPO_ROOT / "scripts" / "installer"
                  / "installer_common.sh").read_text()
        self.assertIn("derive_kms_location()", common)
        # And one source brings both, which is why the guard sources only the
        # helpers: installer_common.sh loads the defaults file on the way in.
        self.assertIn("INSTALL_DEFAULTS_FILE", common)
        self.assertIn("scripts/installer/installer_common.sh", self.text)

    def test_a_failed_plan_does_not_report_itself_as_planned(self):
        """`result` is the caller's contract, so it has to carry this itself."""
        self.assertIn('output "result" "failed"', self.text)


class UpgradePlanFlagTest(unittest.TestCase):
    def setUp(self):
        self.text = _UPGRADE_SH.read_text()

    def test_plan_and_dry_run_cannot_be_combined(self):
        """They answer different questions; silently picking one would mislead."""
        self.assertIn(
            '--dry-run and --plan are different previews and cannot be combined',
            self.text)

    def test_an_empty_image_tag_is_still_an_error_for_an_upgrade(self):
        """Empty is the shape of a CI variable that did not resolve."""
        self.assertIn("--keep-image-tag", self.text)
        self.assertIn(
            '[ -z "$PARAM_IMAGE_TAG" ] && [ "$PARAM_KEEP_IMAGE_TAG" = "true" ]',
            self.text)

    def test_a_plan_skips_the_session_key_backfill(self):
        """backfill_session_kv_keys patches the live Secret."""
        self.assertIn(
            "Plan mode: skipping the Session KV backfill", self.text)


class ReportDriftTest(unittest.TestCase):
    def setUp(self):
        self.module = _load_report_drift()

    def test_it_summarises_a_plan_with_changes(self):
        plan = (
            "Terraform will perform the following actions:\n"
            "\n"
            "  # module.chat_pubsub[0].google_pubsub_topic.chat_events will be created\n"
            "  + resource \"google_pubsub_topic\" \"chat_events\" {\n"
            "    }\n"
            "\n"
            "  # module.kube_agents_iam.google_project_iam_member.agent will be updated in-place\n"
            "\n"
            "Plan: 1 to add, 1 to change, 0 to destroy.\n"
        )
        totals, actions, destructive = self.module.summarise(plan)
        self.assertEqual(totals, "Plan: 1 to add, 1 to change, 0 to destroy.")
        self.assertEqual(len(actions), 2)
        self.assertFalse(destructive)

    def test_a_destroy_is_called_out(self):
        """It is #1060's signature, and usually means missing configuration."""
        plan = (
            "  # module.gke_cluster.google_container_node_pool.gvisor[0] will be destroyed\n"
            "Plan: 0 to add, 0 to change, 1 to destroy.\n"
        )
        _, _, destructive = self.module.summarise(plan)
        self.assertTrue(destructive)
        body = self.module.body_for("autopush", "http://run", "", [], True)
        self.assertIn("[!WARNING]", body)

    def test_the_body_carries_a_marker_so_one_issue_is_reused(self):
        """A nightly that opens a new issue every night teaches everyone to mute it."""
        body = self.module.body_for("autopush", "http://run", "", [], False)
        self.assertIn(self.module.MARKER_TEMPLATE.format(env="autopush"), body)

    def test_a_tainted_replacement_is_not_dropped(self):
        """Terraform announces those as `is tainted, so must be replaced`.

        Missed by the regex, a plan whose only change is a tainted replacement
        renders an issue with no resource list and no destroy warning — the
        opposite of what it is reporting.
        """
        plan = (
            "  # module.gke_cluster.google_container_cluster.autopilot[0] is tainted,"
            " so must be replaced\n"
            "Plan: 1 to add, 0 to change, 1 to destroy.\n"
        )
        _, actions, destructive = self.module.summarise(plan)
        self.assertEqual(len(actions), 1)
        self.assertTrue(destructive)

    def test_the_label_is_created_before_the_first_issue(self):
        """`gh issue create --label` fails outright on a label that does not exist."""
        source = (_REPO_ROOT / "scripts" / "release" / "report_drift.py").read_text()
        self.assertIn("gh\", \"label\", \"create\"", source)
        create = source.index('"issue", "create"')
        ensure = source.index("ensure_label(args.repo)")
        self.assertLess(ensure, create)

    def test_an_empty_plan_yields_no_actions(self):
        totals, actions, destructive = self.module.summarise(
            "No changes. Your infrastructure matches the configuration.\n")
        self.assertEqual(totals, "")
        self.assertEqual(actions, [])
        self.assertFalse(destructive)


if __name__ == "__main__":
    unittest.main()

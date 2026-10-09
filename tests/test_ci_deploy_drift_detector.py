"""The eval install runs the drift detector, pointed at the pool project's subscription.

Two halves that have to agree, owned by different engines.

The *ingress* is Terraform's. `scripts/provision_ci_pool_project.sh` sets
`enable_drift_pubsub` in the `terraform/examples/full-install` tfvars, and
`terraform/modules/drift-pubsub` creates the Log Router sink, the drift-audit
topic and its pull subscription, and the composition grants the platform GSA
subscriber and viewer on that subscription -- beside the module rather than
inside it, so that the module stays independent of the cluster's teardown. The same tfvars sets
`drift_pubsub_topic_publishers`, the pool's one departure from what an install
provisions, so a drift case can put a synthetic audit record on the topic.
Nothing in `hack/ci-deploy.sh` creates any of it —
AGENTS.md's "the install has one engine" rule, and a practical reason beside
it: the presubmit runner identity holds no Pub/Sub role at all
(`docs/ci-pool-projects.md` section 3), so the gcloud version would need the
CI runner granted `roles/pubsub.admin` on every pool project.

The *consumer* is the lease's. `hack/ci-deploy.sh` upgrades the release the
composition installed and replaces its whole value set, the subscription name
included, so the deploy has to restate the name as well as turn the detector
on. That makes the name live in two places at once, and this file pins them
equal: the composition's `drift_pubsub_subscription` default and the deploy's
`EVAL_DRIFT_SUBSCRIPTION`.

A mismatch is the quiet one. The detector starts, the pod reports Ready, it
pulls a subscription that does not exist, and a drift case files no card — a
result that grades as the agent having failed to triage rather than as the
install being wrong.

Both halves fail the same quiet way for a second reason, which is why the
tfvars keys are checked against `variables.tf` rather than only for their
presence in the provisioner: Terraform treats a key in `terraform.tfvars` that
no root variable declares as a *warning*, so a renamed variable applies green
and onboards a project with the ingress and no publisher grant.
"""

import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

import yaml

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_PROVISIONER = _REPO_ROOT / "scripts" / "provision_ci_pool_project.sh"
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_FULL_INSTALL_VARIABLES = (
    _REPO_ROOT / "terraform" / "examples" / "full-install" / "variables.tf"
)
_START_SERVICES = _REPO_ROOT / "deploy" / "shared" / "start-services.sh"
_OPERATOR_MANIFESTS = (
    _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
)
_DETECTOR_MAIN = _REPO_ROOT / "k8s-operator" / "cmd" / "drift-detector" / "main.go"

_ENABLED_FLAG = '--set "platformAgent.harness.driftDetector.enabled=true"'
_SUBSCRIPTION_FLAG = (
    '--set-string "platformAgent.harness.driftDetector.subscription=${EVAL_DRIFT_SUBSCRIPTION}"'
)
_TFVARS_LINE = "enable_drift_pubsub = true"
_PUBLISHERS_LINE = (
    'drift_pubsub_topic_publishers = ["${PROW_RUNNER_SA}", "${NIGHTLY_RUNNER_SA}"]'
)

_LOG_DROPPED_ENV = "DRIFT_DETECTOR_LOG_DROPPED"

# The detector's startup line is assembled from two places in main.go: the
# `commandName` constant every log line is prefixed with, and the format string
# of the line itself. Render the marker from both rather than repeating it, so
# a rename on the Go side is a unit failure here and not a dead readiness gate
# discovered an hour into a lease.
_DETECTOR_COMMAND_NAME_RE = re.compile(r'^\s*commandName\s*=\s*"([^"]*)"', re.MULTILINE)
_DETECTOR_PULL_LOG_RE = re.compile(r'log\.Printf\(\s*"%s: (pulling[^"%]*)')

_READY_GATE_START = "drift_detector_started=false"
_READY_GATE_END = 'echo "✓ Rollout verification finished'
_GATE_NAMESPACE = "kubeagents-system"
_GATE_RESULT = "GATE_RESULT"
# Comfortably past the 64 KiB pipe buffer, which is what decides whether the
# writer is still writing when the reader stops.
_FILLER_LINE = "2026-10-06T00:00:00Z drift-detector: parsed=0 skipped=0 failed=0\n"
_FILLER_LINES = 40000


def _shell_constant(path: pathlib.Path, name: str) -> str:
    match = re.search(rf'^readonly {name}="([^"]*)"$', path.read_text(), re.MULTILINE)
    if match is None:
        raise AssertionError(f"{path.name} declares no readonly {name}")
    return match.group(1)


def _logical_lines(path: pathlib.Path) -> list:
    """`path`'s shell lines, comment-only lines dropped and continuations joined.

    Scanning physical lines is what a backslash-continued command walks past:
    `gcloud --project X \\` on one line and `pubsub subscriptions create` on
    the next matches neither half of a pattern that wants both words, and the
    scans below are the only thing standing between this script and a second
    engine provisioning Pub/Sub.

    Comment-only lines are dropped first and continuations joined second,
    which is the order that matters -- joining first would let a comment
    ending in a backslash swallow the code line under it and hide exactly what
    is being looked for.

    Trailing comments are then cut at the first `#`. That also cuts a `#`
    inside a quoted string or a `${VAR#prefix}` expansion, which nothing short
    of a shell parser fixes. It drops the rest of such a line, so that half
    can only miss an offender, never invent one.
    """
    uncommented = "\n".join(
        line for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    # Only the continuation's own trailing indent, not `\s*`: that would eat
    # the blank line after a continuation too and splice two separate commands
    # into one, which is the way this could report something that is not there.
    joined = re.sub(r"\\\n[ \t]*", " ", uncommented)
    return [line.split("#", 1)[0] for line in joined.splitlines()]


def _terraform_declares(path: pathlib.Path, variable: str) -> bool:
    return (
        re.search(rf'^variable "{variable}" \{{', path.read_text(), re.MULTILINE)
        is not None
    )


def _terraform_default(path: pathlib.Path, variable: str) -> str:
    block = re.search(
        rf'variable "{variable}" \{{(.*?)^\}}', path.read_text(), re.MULTILINE | re.DOTALL
    )
    if block is None:
        raise AssertionError(f"{path.name} declares no variable {variable}")
    match = re.search(r'^\s*default\s*=\s*"([^"]*)"', block.group(1), re.MULTILINE)
    if match is None:
        raise AssertionError(f"variable {variable} in {path.name} has no string default")
    return match.group(1)


def _head_constants(text: str) -> str:
    """The file-head `readonly` declarations a lifted block reads."""
    return "\n".join(line for line in text.splitlines() if line.startswith("readonly "))


def _detector_startup_marker() -> str:
    """The prefix the detector really prints when it reaches its pull loop.

    Both halves raise rather than returning a partial answer. A format string
    this cannot parse is a rewrite of the line the readiness gate waits on, and
    the gate's coupling to it has to be re-checked by hand; reporting that as a
    marker that happens not to match would say the constant is wrong when it is
    the derivation that stopped working.
    """
    text = _DETECTOR_MAIN.read_text()
    name = _DETECTOR_COMMAND_NAME_RE.search(text)
    if name is None:
        raise AssertionError(
            "k8s-operator/cmd/drift-detector/main.go declares no commandName constant"
        )
    logged = _DETECTOR_PULL_LOG_RE.search(text)
    if logged is None:
        raise AssertionError(
            'no log.Printf("%s: pulling ...") in '
            "k8s-operator/cmd/drift-detector/main.go: the startup line the eval "
            "deploy's readiness gate waits on has been restructured, so the gate "
            "and this derivation both need re-checking against the new line"
        )
    return f"{name.group(1)}: {logged.group(1).strip()}"


def _ready_gate(text: str) -> str:
    start = text.find(_READY_GATE_START)
    assert start != -1, f"{_READY_GATE_START!r} not found in hack/ci-deploy.sh"
    end = text.find(_READY_GATE_END, start)
    assert end != -1, f"{_READY_GATE_END!r} not found after {_READY_GATE_START!r}"
    return text[start:end]


class PoolProjectProvisionsTheIngressTest(unittest.TestCase):
    def test_the_tfvars_turns_the_module_on(self) -> None:
        self.assertIn(
            _TFVARS_LINE,
            _PROVISIONER.read_text(),
            "scripts/provision_ci_pool_project.sh must write enable_drift_pubsub "
            "into the full-install tfvars. Without it the composition creates no "
            "sink, topic or subscription, and every eval case that exercises the "
            "drift detector fails as broken rather than red.",
        )

    def test_the_tfvars_lets_the_runners_publish(self) -> None:
        # The grant a real install does not have: on an install the Log Router
        # is the only publisher, which is what makes a record on the topic
        # evidence the API server saw the call. A bench fixture cannot reach
        # the classifier any other way -- every identity it can authenticate
        # as is a .gserviceaccount.com the classifier is right to drop, and a
        # real cluster write would arrive minutes later through the export.
        #
        # The same two constants the project-level grant loop iterates, so the
        # presubmit and the nightly are equal here as they are there: whichever
        # holds the lease is the one that has to publish.
        text = _PROVISIONER.read_text()
        self.assertIn(
            _PUBLISHERS_LINE,
            text,
            "scripts/provision_ci_pool_project.sh must grant the two runners "
            "publisher on the drift topic. Without it the ingress is complete, "
            "the detector reads it, and a drift case dies at its own publish "
            "step -- no project role the runners hold carries "
            "pubsub.topics.publish.",
        )
        for constant in ("PROW_RUNNER_SA", "NIGHTLY_RUNNER_SA"):
            self.assertRegex(
                text,
                rf'(?m)^{constant}="serviceAccount:',
                f"{constant} must stay a fully qualified IAM member: "
                "terraform/modules/drift-pubsub validates topic_publishers "
                "against that prefix, so a bare email fails the apply.",
            )

    def test_both_tfvars_keys_are_declared_by_the_composition(self) -> None:
        # The presence checks above are string literals against the provisioner,
        # and on their own they cannot see the composition rename out from under
        # them. Terraform does not error on a tfvars key no root variable
        # declares -- it warns and applies -- so the rename lands green, the
        # project onboards with the sink, topic, subscription and the detector's
        # grants but no publisher grant, and the first drift case dies at its
        # own publish step with PermissionDenied. Both literals above would
        # still match.
        for key in ("enable_drift_pubsub", "drift_pubsub_topic_publishers"):
            self.assertTrue(
                _terraform_declares(_FULL_INSTALL_VARIABLES, key),
                f"scripts/provision_ci_pool_project.sh writes {key} into the "
                "full-install tfvars, but terraform/examples/full-install "
                "declares no such variable. Terraform warns rather than errors "
                "on an undeclared tfvars key, so the apply succeeds and the "
                "setting is silently dropped.",
            )

    def test_the_deploy_provisions_nothing_itself(self) -> None:
        # The rule is AGENTS.md's "the install has one engine". A `gcloud pubsub`
        # call here would also be the one thing the runner identity cannot do.
        # Comments are stripped first: the constant block explains why there is
        # no such call, and naming the thing it forbids would otherwise be what
        # fails this test.
        #
        # Matched on word boundaries rather than as the literal "gcloud pubsub",
        # which `gcloud beta pubsub`, `gcloud --project X pubsub` and a
        # `$GCLOUD pubsub` all walk straight past -- and over logical lines, so
        # that a backslash before the subcommand does not do the same.
        invocation = re.compile(r"\bgcloud\b.*\bpubsub\b")
        offenders = [
            line.strip() for line in _logical_lines(_CI_DEPLOY) if invocation.search(line)
        ]
        self.assertEqual(
            offenders,
            [],
            "hack/ci-deploy.sh must not create or modify Pub/Sub resources: "
            "terraform/modules/drift-pubsub owns them, reached through "
            "scripts/provision_ci_pool_project.sh.",
        )


class DetectorIsTurnedOnForEachLeaseTest(unittest.TestCase):
    def test_the_helm_upgrade_enables_the_detector(self) -> None:
        text = _CI_DEPLOY.read_text()
        for needle in (_ENABLED_FLAG, _SUBSCRIPTION_FLAG):
            self.assertIn(
                needle,
                text,
                "hack/ci-deploy.sh must turn the drift detector on and name its "
                "subscription. The chart ships driftDetector.enabled unset and "
                "the CRD defaults it to false, so the eval install runs no "
                "detector at all without both flags.",
            )

    def test_the_subscription_name_is_the_same_in_both_places(self) -> None:
        composition = _terraform_default(_FULL_INSTALL_VARIABLES, "drift_pubsub_subscription")
        deploy = _shell_constant(_CI_DEPLOY, "EVAL_DRIFT_SUBSCRIPTION")
        self.assertEqual(
            composition,
            deploy,
            "the subscription Terraform creates and the one hack/ci-deploy.sh "
            "points the detector at have drifted apart. The detector would pull "
            "a subscription that does not exist, with the pod Ready and nothing "
            "in the logs saying so.",
        )

    def test_the_readiness_marker_is_what_the_detector_prints(self) -> None:
        self.assertEqual(
            _detector_startup_marker(),
            _shell_constant(_CI_DEPLOY, "EVAL_DRIFT_READY_MARKER"),
            "hack/ci-deploy.sh waits for a line the drift detector no longer "
            "prints. The gate is fail-closed, so this does not pass silently -- "
            "it fails every eval deploy, roughly an hour into a lease, reporting "
            "that the detector never started on an install where it started "
            "fine. The coupling is invisible from the Go side: whoever renames "
            "commandName or rewrites the pulling log line in "
            "k8s-operator/cmd/drift-detector/main.go has no reason to know a "
            "shell constant reads it.",
        )


class TheReadinessGateReadsTheWholeLogTest(unittest.TestCase):
    """The startup gate survives a log longer than the pipe buffer.

    The gate greps `kubectl logs` for the detector's pull-loop marker, and the
    script runs under `set -o pipefail`. That combination is a trap: a `grep`
    that stops at the first match closes the pipe, the `kubectl` still writing
    into it dies of SIGPIPE, and `pipefail` reports the pipeline failed. A
    found marker then reads as not found.

    It is not a rare shape here. The marker prints once when the detector
    starts and the detector keeps logging after that, so by the time the gate
    runs the log is past the buffer and the match is an early line. The gate
    inverts, exhausts its retries, and reds every case in the lease on an
    install that is working -- with the gate's own error message saying the
    detector never started, which is the opposite of where to look.

    So these run the real block out of the script against a stub `kubectl`,
    rather than asserting on its text: the defect is in how two exit codes
    combine, and only a shell can tell you that.
    """

    def _run_gate(self, log_body: str):
        text = _CI_DEPLOY.read_text()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            pod_log = tmp_path / "pod.log"
            pod_log.write_text(log_body, encoding="utf-8")

            # `exec cat` so the stub is a real writer into the pipe: a stub
            # that buffered the log in the shell and echoed it would never be
            # mid-write when grep left, and would pass either way.
            kubectl_stub = bin_dir / "kubectl"
            kubectl_stub.write_text(
                f"""#!/usr/bin/env bash
if [ "$1" = "logs" ]; then
  case "$*" in
    *--tail=*) echo "(tail elided)"; exit 0 ;;
  esac
  exec cat "{pod_log}"
fi
exit 0
""",
                encoding="utf-8",
            )
            sleep_stub = bin_dir / "sleep"
            sleep_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            for stub in (kubectl_stub, sleep_stub):
                stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

            script = "\n".join(
                (
                    "set -euo pipefail",
                    _head_constants(text),
                    f"NAMESPACE={_GATE_NAMESPACE}",
                    _ready_gate(text),
                    f'echo "{_GATE_RESULT}=${{drift_detector_started}}"',
                )
            )
            return subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
                env=get_isolated_test_env(bin_dir=bin_dir),
            )

    def test_an_early_marker_in_a_long_log_is_found(self) -> None:
        marker = _shell_constant(_CI_DEPLOY, "EVAL_DRIFT_READY_MARKER")
        proc = self._run_gate(
            f"starting\n{marker} drift-audit-eval\n" + _FILLER_LINE * _FILLER_LINES
        )
        self.assertEqual(
            proc.returncode,
            0,
            "the drift-detector readiness gate failed on a log that contains its "
            "marker. Under `set -o pipefail` a grep that stops at the first match "
            "SIGPIPEs the kubectl feeding it, and the pipeline reports failure; "
            f"the gate must drain the log instead.\nstdout: {proc.stdout}\n"
            f"stderr: {proc.stderr}",
        )
        self.assertIn(f"{_GATE_RESULT}=true", proc.stdout)

    def test_a_log_without_the_marker_still_fails_the_gate(self) -> None:
        # The other direction, so a fix for the above cannot be "stop checking".
        proc = self._run_gate(_FILLER_LINE * _FILLER_LINES)
        self.assertEqual(
            proc.returncode,
            1,
            "the gate passed a log with no pull-loop marker in it, so it would "
            f"admit an install whose detector never started.\nstdout: {proc.stdout}",
        )
        self.assertIn("never reached its pull loop", proc.stdout + proc.stderr)


class DroppedRecordsCanBeLoggedOnDemandTest(unittest.TestCase):
    """`DRIFT_DETECTOR_LOG_DROPPED` reaches the detector on an install that asks.

    Not on the eval install, deliberately: one deploy serves the whole matrix,
    so the per-record line would be paid by every lease, and at 1 to 10 records
    a second about 98% system tier that is most of the audit stream copied into
    the pod log. `hack/ci-deploy.sh` says so where the flag would have gone, and
    the class below pins that it stays absent.

    The route it does take is `spec.deployment.env` on any install that wants
    it. Two hops, each silent when it breaks: `mergeCredentialProxyEnv`, a
    denylist that must *not* reserve this name, and `start-services.sh`, which
    is what turns the variable into the flag. Notably not
    `safeSandboxEnvOverrides` — that allowlist governs the agent sandbox, and
    the detector runs in the credential-proxy sidecar.
    """

    def test_the_eval_deploy_does_not_ask_for_dropped_records(self) -> None:
        offenders = [
            line.strip() for line in _logical_lines(_CI_DEPLOY) if _LOG_DROPPED_ENV in line
        ]
        self.assertEqual(
            offenders,
            [],
            f"hack/ci-deploy.sh sets {_LOG_DROPPED_ENV} on the eval install. "
            "One deploy serves every case in the lease, so the per-record drop "
            "line costs the whole matrix most of the project's audit stream in "
            "the pod log. A fixture separates an empty ingress from an "
            "over-eager filter with the detector's own idle counters "
            "(parsed/skipped/failed) instead.",
        )

    def test_the_sidecar_denylist_does_not_reserve_the_variable(self) -> None:
        # The trap this exists for: the six sibling DRIFT_DETECTOR_* names are
        # all on mergeCredentialProxyEnv's reserved list, because
        # buildAgentAPIAuthSidecar appends each one after the merge and a
        # duplicate would stall the apply. This one is written by nothing, so
        # reserving it for symmetry would stop spec.deployment.env reaching the
        # detector -- and nothing in the render would fail to say so.
        #
        # Comment lines are stripped: both the note above and the one on
        # safeSandboxEnvOverrides name the variable to say why it is absent,
        # and a reader who quotes it there should not red this.
        code = "\n".join(
            line for line in _OPERATOR_MANIFESTS.read_text().splitlines()
            if not line.lstrip().startswith("//")
        )
        self.assertNotIn(
            f'"{_LOG_DROPPED_ENV}"',
            code,
            f"{_LOG_DROPPED_ENV} is now named in "
            "k8s-operator/internal/controller/platformagent_manifests.go. The "
            "operator writes this variable nowhere and must reserve it "
            "nowhere: on mergeCredentialProxyEnv's reserved list it stops "
            "reaching the detector, and on safeSandboxEnvOverrides' allowlist "
            "it is copied into a container that never reads it.",
        )

    def test_start_services_turns_the_variable_into_the_flag(self) -> None:
        text = _START_SERVICES.read_text()
        self.assertIn(
            f'"${{{_LOG_DROPPED_ENV}:-false}}"',
            text,
            f"deploy/shared/start-services.sh no longer reads {_LOG_DROPPED_ENV}, "
            "so the variable reaches the container and changes nothing.",
        )
        self.assertIn(
            "detector_args+=(--log-dropped)",
            text,
            "deploy/shared/start-services.sh no longer appends --log-dropped to "
            "the detector's arguments, so an install that sets "
            f"{_LOG_DROPPED_ENV} gets no drop lines and nothing says why.",
        )

    def test_the_detector_still_defines_the_flag(self) -> None:
        # The far end of the chain, and the one hop that fails loudly rather
        # than silently -- an unknown flag exits the detector on every start,
        # which start-services.sh then retries forever. Pinned anyway because
        # the alert it raises says the detector will not start, not that a
        # rename in a Go file is why.
        self.assertIn(
            '"log-dropped"',
            _DETECTOR_MAIN.read_text(),
            "k8s-operator/cmd/drift-detector no longer defines --log-dropped; "
            "deploy/shared/start-services.sh passes it and the detector would "
            "exit on every start.",
        )


class HelmRendersTheDetectorBlockTest(unittest.TestCase):
    """The --set pair lands on the rendered PlatformAgent CR.

    A mistyped values key renders a CR with no driftDetector block rather than
    failing, so only a real `helm template` shows the hop works. Skips where
    the binary is absent (a contributor's laptop) and runs in CI.
    """

    def test_rendered_cr_carries_the_detector_block(self) -> None:
        if shutil.which("helm") is None:
            self.skipTest("helm not installed")
        subscription = _shell_constant(_CI_DEPLOY, "EVAL_DRIFT_SUBSCRIPTION")
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "t",
                str(_CHART),
                "--set-string",
                "platformAgent.harness.clusterName=c",
                "--set-string",
                "platformAgent.harness.location=us-central1",
                "--set-string",
                "platformAgent.harness.projectId=p",
                "--set",
                "platformAgent.harness.driftDetector.enabled=true",
                "--set-string",
                f"platformAgent.harness.driftDetector.subscription={subscription}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        crs = [
            doc for doc in yaml.safe_load_all(rendered)
            if doc and doc.get("kind") == "PlatformAgent"
        ]
        self.assertEqual(len(crs), 1, "expected exactly one PlatformAgent in the render")
        detector = crs[0]["spec"]["harness"]["driftDetector"]
        self.assertEqual(detector["enabled"], True)
        self.assertEqual(detector["subscription"], subscription)


if __name__ == "__main__":
    unittest.main()

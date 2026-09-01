"""Unit tests for scripts/release/install_pubsub_platform.sh and its callers.

The script was split out of wait_for_gke_readiness.sh so that installing alert
ingress is a step of its own with its own exit code. Two properties of that split
are invariants a later edit can break without any test noticing, so they are
pinned here: the ordering between the two scripts, and the fact that each caller
states its own failure policy rather than inheriting one from the script.
"""

import pathlib
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALL_SCRIPT = _REPO_ROOT / "scripts" / "release" / "install_pubsub_platform.sh"
_READINESS_SCRIPT = _REPO_ROOT / "scripts" / "release" / "wait_for_gke_readiness.sh"
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

# Every workflow that waits for readiness against an ephemeral environment. Each
# one runs an alert-driven suite, so each one needs the adapter.
#
# One entry, because there is one such job: `e2e-run.yml`, which the RC pipeline
# and the nightly pipeline both call. The manual runner belongs here too and is
# deliberately absent — it waits for readiness without installing the adapter,
# which is #1013's follow-up.
_CALLERS = ("e2e-run.yml",)


_E2E_CONFIG = _REPO_ROOT / "tests" / "e2e" / "e2e_config.yaml"


def _workflow(workflow_name: str) -> dict:
    """A workflow parsed whole.

    `yaml.safe_load` reads the unquoted `on:` key as the boolean True, so the
    trigger block is re-keyed to the string every caller here expects.
    """
    doc = yaml.safe_load((_WORKFLOWS / workflow_name).read_text())
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def _suite_tests(suite_name: str) -> list[str]:
    """The basenames of the test files a named e2e_config.yaml suite runs."""
    doc = yaml.safe_load(_E2E_CONFIG.read_text())
    for suite in doc.get("suites", doc.get("environments", [])) or []:
        if suite.get("name") == suite_name:
            return [pathlib.PurePath(t).name for t in suite.get("tests", []) or []]
    return []


def _steps(workflow_name: str) -> list[dict]:
    """Every step of every job in a workflow, in file order."""
    steps: list[dict] = []
    for job in _workflow(workflow_name).get("jobs", {}).values():
        steps.extend(job.get("steps", []) or [])
    return steps


def _run_index(steps: list[dict], needle: str) -> int:
    for i, step in enumerate(steps):
        if needle in (step.get("run") or ""):
            return i
    return -1


class TestInstallPubSubPlatformScript(unittest.TestCase):
    def test_script_exists_and_is_executable(self) -> None:
        self.assertTrue(_INSTALL_SCRIPT.is_file(), f"{_INSTALL_SCRIPT} is missing")
        self.assertTrue(
            _INSTALL_SCRIPT.stat().st_mode & 0o111,
            "install_pubsub_platform.sh must be executable; the workflows invoke it directly",
        )

    def test_script_parses(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(_INSTALL_SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_readiness_script_no_longer_installs(self) -> None:
        """The split is the point: a script named wait_* must not mutate the cluster."""
        body = _READINESS_SCRIPT.read_text()
        self.assertNotIn(
            "agentplugins/pubsub-platform/install.sh",
            body,
            "wait_for_gke_readiness.sh installs the adapter again; that belongs in "
            "install_pubsub_platform.sh",
        )
        self.assertNotIn(
            "helm ",
            body,
            "wait_for_gke_readiness.sh should wait, not install",
        )

    def test_settle_loop_has_an_absolute_ceiling(self) -> None:
        """The stability window resets on every change, so it needs a hard bound.

        Without one, a gateway whose generation keeps moving spends the job's whole
        timeout-minutes budget and then fails for a reason that looks nothing like
        the cause.
        """
        body = _INSTALL_SCRIPT.read_text()
        # The identifiers existing is not the invariant -- the guard that reads
        # them is. Asserting the names alone stays green when the `-ge` test is
        # deleted from the loop and the declarations are left behind, which is
        # exactly the regression this pins.
        self.assertRegex(
            body,
            r"settle_hard_deadline=\$\(\(\$\(date \+%s\) \+ GENERATION_SETTLE_TIMEOUT\)\)",
            "the settle ceiling must be seeded from GENERATION_SETTLE_TIMEOUT",
        )
        self.assertRegex(
            body,
            r'if \[ "\$\(date \+%s\)" -ge "\$\{settle_hard_deadline\}" \]',
            "the settle loop must break on the absolute ceiling; without this "
            "test the stability window resets forever and the loop spends the "
            "job's whole timeout-minutes budget",
        )


class TestCallerWiring(unittest.TestCase):
    def test_every_caller_installs_before_waiting(self) -> None:
        for workflow in _CALLERS:
            with self.subTest(workflow=workflow):
                steps = _steps(workflow)
                install_at = _run_index(steps, "install_pubsub_platform.sh")
                readiness_at = _run_index(steps, "wait_for_gke_readiness.sh")
                self.assertNotEqual(
                    install_at, -1, f"{workflow} never installs the Pub/Sub adapter"
                )
                self.assertNotEqual(
                    readiness_at, -1, f"{workflow} never waits for readiness"
                )
                self.assertLess(
                    install_at,
                    readiness_at,
                    f"{workflow} waits for readiness before installing the adapter, so the "
                    "rollout waits can pass against the pre-adapter ReplicaSet and the "
                    "gateway then restarts mid-suite",
                )

    def test_ingress_tolerance_is_the_callers_decision(self) -> None:
        """Whether a failed ingress install fails the job follows alert_ingress_required.

        Hardcoding `continue-on-error: true` was correct only for the RC caller,
        whose alert-reading tests are its optional suite. The nightly caller runs
        them in its blocking suite, so tolerating the failure there converts a
        missing adapter into every stockout scenario timing out on a watch.
        """
        steps = _steps("e2e-run.yml")
        install_at = _run_index(steps, "install_pubsub_platform.sh")
        self.assertNotEqual(install_at, -1)
        self.assertEqual(
            steps[install_at].get("continue-on-error"),
            "${{ !inputs.alert_ingress_required }}",
            "the adapter step's tolerance must follow the alert_ingress_required "
            "input, not a constant: the suite that depends on the ingress differs "
            "per caller",
        )

    def test_the_input_defaults_to_tolerating_a_failure(self) -> None:
        """Default false, so the RC pipeline's previous behaviour is unchanged."""
        spec = _workflow("e2e-run.yml")["on"]["workflow_call"]["inputs"][
            "alert_ingress_required"
        ]
        self.assertEqual(spec["type"], "boolean")
        self.assertIs(spec["default"], False)

    def test_a_blocking_alert_suite_requires_the_ingress(self) -> None:
        """Any caller whose BLOCKING suite reads an alert must pass true.

        `nightly` carries STOCKOUT_SCENARIOS and runs
        test_stockout_investigation.py; the RC gate's alert tests are optional.
        This pins the pairing rather than the one call site, so a third pipeline
        that blocks on an alert suite cannot quietly inherit the tolerant default.
        """
        alert_suite_tests = {"test_stockout_investigation.py"}
        for workflow in ("nightly-pipeline.yml", "rc-release-pipeline.yml"):
            with self.subTest(workflow=workflow):
                for job in _workflow(workflow)["jobs"].values():
                    if not str(job.get("uses", "")).endswith("e2e-run.yml"):
                        continue
                    params = job.get("with", {})
                    blocking = params.get("blocking_suite", "")
                    blocks_on_alert = bool(
                        alert_suite_tests & set(_suite_tests(blocking))
                    )
                    self.assertEqual(
                        bool(params.get("alert_ingress_required", False)),
                        blocks_on_alert,
                        f"{workflow} blocks on {blocking!r}: alert_ingress_required "
                        "must be true exactly when that suite reads an alert",
                    )


if __name__ == "__main__":
    unittest.main()

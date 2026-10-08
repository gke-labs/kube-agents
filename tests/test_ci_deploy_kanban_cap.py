"""The smoke pipeline's Helm release gives the eval install a kanban board cap that fits its lanes.

The image ships ``kanban.max_in_progress: 6`` (``agents/chat/config.yaml``), and the
operator renders a different cap only when the PlatformAgent CR carries
``spec.harness.tuning.maxInProgress``. The eval fans its units out at
``EVAL_TASK_PARALLELISM`` -- 4 on a pull request, 8 on the nightly -- and nearly every
unit's opening turn delegates one platform card. The override dates from an image
default of two, where most lanes queued: a queued card waits out the cards ahead of it and
then runs its own 10-45 minutes, past the delegation ceiling with no worker at fault, while the dispatcher
logs the same "0 workers spawned" warning a wedged worker produces (#1879, #1880,
and the residual after their fixes). ``hack/ci-deploy.sh`` therefore sets the cap on
the eval install and nowhere else, deliberately below the production default of six.

The value rides three hops: the ``--set`` in ``ci-deploy.sh`` (``--set`` rather than
``--set-string``, because the chart schema types the key as an integer and a string
fails validation at ``helm upgrade``), the chart template that renders
``platformAgent.harness.tuning`` onto the CR, and the operator writing
``kanban.max_in_progress`` into the default profile's overlay, which the operator's
own ``TestMaxInProgressReachesTheDefaultOverlay`` covers. One test per hop this
repository can see from Python, plus the two bounds: a cap whose user share is
below the pull request's lane count recreates the queue the flag exists to
remove, and a cap
above the eval's ceiling runs more workers than the eval install's working set
has been measured at, risking a worker the OOM killer takes, which strands its
card the same way.
"""

import pathlib
import re
import shutil
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_CI_EVAL = _REPO_ROOT / "hack" / "ci-eval-pr.sh"
_CHART = _REPO_ROOT / "charts" / "kube-agents"

_CAP_FLAG = '--set "platformAgent.harness.tuning.maxInProgress=${EVAL_KANBAN_MAX_IN_PROGRESS}"'
_CAP_AS_STRING = '--set-string "platformAgent.harness.tuning.maxInProgress'
_CAP_CONSTANT_RE = re.compile(r'^readonly EVAL_KANBAN_MAX_IN_PROGRESS="(\d+)"$', re.MULTILINE)
# The presubmit's lane count is the eval script's own default, and ci-deploy.sh
# carries the same default for the bridge (tests/test_ci_deploy_mode_next.py
# pins the two equal). The eval's lane cards come in through the inject and
# A2A doors, so they are user cards, and at a cap of 2 or more user cards may
# hold every slot but the one guaranteed to background triage
# (class_cap in deploy/docker/patches/kanban_priority.py). The floor is
# therefore the lanes plus that one slot.
#
# The ceiling is the eval's own, deliberately below the production default of
# six: the gateway's 8Gi limit (resolveResources in
# k8s-operator/internal/controller/manifest_helpers.go) holds about fourteen
# workers at the ~430 MiB one measured live over the 1.8 GiB idle set, but the
# eval's working set above five has not been measured (#2032), and raising the
# cap belongs with that measurement. The nightly runs eight lanes
# (oss-test-infra#2707) and queues until then.
_PRESUBMIT_LANES_RE = re.compile(r'^EVAL_TASK_PARALLELISM="\$\{EVAL_TASK_PARALLELISM:-(\d+)\}"$', re.MULTILINE)
_BRIDGE_LANES_RE = re.compile(r'^readonly EVAL_TASK_PARALLELISM_DEFAULT=(\d+)$', re.MULTILINE)
_EVAL_WORKERS_MEASURED = 5
# The one slot a cap of 2 or more guarantees to background triage.
_BACKGROUND_FLOOR = 1


def _cap() -> int:
    match = _CAP_CONSTANT_RE.search(_CI_DEPLOY.read_text())
    assert match, "hack/ci-deploy.sh no longer declares readonly EVAL_KANBAN_MAX_IN_PROGRESS"
    return int(match.group(1))


class CiDeployKanbanCapTest(unittest.TestCase):
    def test_helm_release_sets_the_board_cap_as_an_integer(self) -> None:
        text = _CI_DEPLOY.read_text()
        self.assertIn(
            _CAP_FLAG,
            text,
            "hack/ci-deploy.sh must pass spec.harness.tuning.maxInProgress to the "
            "chart: the eval holds its cap below the image default of 6 until its "
            "working set above five workers is measured (#2032), and without the "
            "flag the eval install runs whatever the image ships.",
        )
        self.assertNotIn(
            _CAP_AS_STRING,
            text,
            "the chart schema types maxInProgress as an integer; --set-string "
            "would fail validation at helm upgrade.",
        )

    def test_the_cap_covers_the_presubmit_lanes_and_stays_inside_the_memory_sizing(self) -> None:
        match = _PRESUBMIT_LANES_RE.search(_CI_EVAL.read_text())
        assert match, "hack/ci-eval-pr.sh no longer declares the EVAL_TASK_PARALLELISM default"
        bridge = _BRIDGE_LANES_RE.search(_CI_DEPLOY.read_text())
        assert bridge, "hack/ci-deploy.sh no longer declares EVAL_TASK_PARALLELISM_DEFAULT"
        presubmit_lanes = max(int(match.group(1)), int(bridge.group(1)))
        cap = _cap()
        self.assertGreaterEqual(
            cap,
            presubmit_lanes + _BACKGROUND_FLOOR,
            f"EVAL_KANBAN_MAX_IN_PROGRESS={cap} leaves {cap - _BACKGROUND_FLOOR} slot(s) "
            "for user cards (one is held for background triage), below the presubmit's "
            f"{presubmit_lanes} lanes: nearly every lane delegates one card, so the lanes "
            "over that queue and run out the delegation ceiling.",
        )
        self.assertLessEqual(
            cap,
            _EVAL_WORKERS_MEASURED,
            f"EVAL_KANBAN_MAX_IN_PROGRESS={cap} is above the {_EVAL_WORKERS_MEASURED} "
            "workers the eval install's working set has been measured at: measure it "
            "(and raise its memory limit if needed) in the same change, or a worker the "
            "OOM killer takes strands its card exactly like the queue the cap removes.",
        )


class HelmRendersTheCapTest(unittest.TestCase):
    """The --set actually lands on the rendered PlatformAgent CR.

    A mistyped values key would render a CR with no tuning block rather than fail,
    so only a real ``helm template`` can show the hop works. Skips where the binary
    is absent (a contributor's laptop) and runs in CI, which installs one.
    """

    def test_rendered_cr_carries_the_cap(self) -> None:
        if shutil.which("helm") is None:
            self.skipTest("helm not installed")
        cap = _cap()
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
                f"platformAgent.harness.tuning.maxInProgress={cap}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("tuning:", rendered)
        self.assertIn(f"maxInProgress: {cap}", rendered)


if __name__ == "__main__":
    unittest.main()

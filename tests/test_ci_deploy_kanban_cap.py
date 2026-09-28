"""The smoke pipeline's Helm release gives the eval install a kanban board cap that fits its lanes.

The image ships ``kanban.max_in_progress: 2`` (``agents/chat/config.yaml``), and the
operator renders a different cap only when the PlatformAgent CR carries
``spec.harness.tuning.maxInProgress``. The eval fans its units out at
``EVAL_TASK_PARALLELISM`` -- 4 on a pull request, 8 on the nightly -- and nearly every
unit's opening turn delegates one platform card, so on the image default most lanes
queue behind two
slots: a queued card waits out the cards ahead of it and then runs its own 10-45
minutes, past the delegation ceiling with no worker at fault, while the dispatcher
logs the same "0 workers spawned" warning a wedged worker produces (#1879, #1880,
and the residual after their fixes). ``hack/ci-deploy.sh`` therefore sets the cap on
the eval install and nowhere else.

The value rides three hops: the ``--set`` in ``ci-deploy.sh`` (``--set`` rather than
``--set-string``, because the chart schema types the key as an integer and a string
fails validation at ``helm upgrade``), the chart template that renders
``platformAgent.harness.tuning`` onto the CR, and the operator writing
``kanban.max_in_progress`` into the default profile's overlay, which the operator's
own ``TestMaxInProgressReachesTheDefaultOverlay`` covers. One test per hop this
repository can see from Python, plus the two bounds: a cap below the pull
request's lane count recreates the queue the flag exists to remove, and a cap
above the worker count the gateway's memory limit was sized for trades that
queue for a worker the OOM killer takes, which strands its card the same way.
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
# The presubmit's lane count is the script's own default. The ceiling is the
# worker count the gateway container's memory limit was sized for
# (resolveResources in k8s-operator/internal/controller/manifest_helpers.go:
# 8Gi for five concurrent workers over a 1.8GiB idle set); raising the cap
# past it belongs in the same change as raising that limit for the eval
# install, once the working set at five has been measured (#2032). The nightly
# runs eight lanes (oss-test-infra#2707) and queues three deep until then.
_PRESUBMIT_LANES_RE = re.compile(r'^EVAL_TASK_PARALLELISM="\$\{EVAL_TASK_PARALLELISM:-(\d+)\}"$', re.MULTILINE)
_WORKERS_THE_MEMORY_LIMIT_FITS = 5


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
            "chart: on the image default of 2 the eval's lanes queue behind two "
            "worker slots and run out the delegation ceiling (#1879, #1880).",
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
        presubmit_lanes = int(match.group(1))
        cap = _cap()
        self.assertGreaterEqual(
            cap,
            presubmit_lanes,
            f"EVAL_KANBAN_MAX_IN_PROGRESS={cap} is below the presubmit's {presubmit_lanes} "
            "lanes: nearly every lane delegates one card, so the lanes over the cap "
            "queue and run out the delegation ceiling.",
        )
        self.assertLessEqual(
            cap,
            _WORKERS_THE_MEMORY_LIMIT_FITS,
            f"EVAL_KANBAN_MAX_IN_PROGRESS={cap} is above the {_WORKERS_THE_MEMORY_LIMIT_FITS} "
            "workers the gateway's memory limit was sized for: raise that limit for "
            "the eval install in the same change, or a worker the OOM killer takes "
            "strands its card exactly like the queue the cap removes.",
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

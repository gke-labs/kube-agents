"""The pre-delete cleanup hook Job survives a cluster-autoscaler scale-down.

The hook pod runs right after the PlatformAgent leaves its node, which is the
moment the autoscaler picks that node for scale-down. With backoffLimit 0 and
no safe-to-evict annotation one eviction failed the Job, and a failed pre-delete
hook leaves the Helm release in state `uninstalling`.

Two guards, both read from the rendered Job: the pod template asks the
autoscaler not to evict the pod, and the Job retries the idempotent deletes
when a pod is lost anyway.

Run:
  python3 -m unittest discover -s tests -p 'test_chart_cleanup_hook_eviction.py' -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _rendered_manifests import CHART_RELEASE, HELM, rendered_chart  # noqa: E402

# Set on every GitHub Actions runner; with it a missing helm is a failure, not a skip.
CI_ENV = "CI"
NO_HELM_ON_CI = "helm is not on PATH but CI is set: the hook would pass unread on a runner. Install helm rather than skipping."
NO_HELM = "helm is not installed; the rendered hook cannot be read"

JOB_KIND = "Job"
JOB_NAME = f"{CHART_RELEASE}-cr-cleanup"
SAFE_TO_EVICT_ANNOTATION = "cluster-autoscaler.kubernetes.io/safe-to-evict"
SAFE_TO_EVICT_VALUE = "false"
# One eviction must not end the Job; two retries cover a second scale-down of
# the replacement pod's node without turning the bounded hook into a loop.
EXPECTED_BACKOFF_LIMIT = 2


def _cleanup_job() -> dict:
    jobs = [d for d in rendered_chart() if d.get("kind") == JOB_KIND and d["metadata"]["name"] == JOB_NAME]
    if len(jobs) != 1:
        raise AssertionError(f"expected one rendered {JOB_KIND} named {JOB_NAME}, found {len(jobs)}")
    return jobs[0]


class CleanupHookEvictionTest(unittest.TestCase):
    def setUp(self) -> None:
        if not HELM:
            if os.environ.get(CI_ENV):
                self.fail(NO_HELM_ON_CI)
            self.skipTest(NO_HELM)

    def test_pod_template_is_marked_not_safe_to_evict(self) -> None:
        annotations = _cleanup_job()["spec"]["template"]["metadata"].get("annotations") or {}
        self.assertEqual(
            annotations.get(SAFE_TO_EVICT_ANNOTATION),
            SAFE_TO_EVICT_VALUE,
            f"the hook pod template must carry the annotation {SAFE_TO_EVICT_ANNOTATION}: {SAFE_TO_EVICT_VALUE!r}; got {annotations}",
        )

    def test_job_retries_a_lost_pod(self) -> None:
        self.assertEqual(
            _cleanup_job()["spec"].get("backoffLimit"),
            EXPECTED_BACKOFF_LIMIT,
            f"backoffLimit must be {EXPECTED_BACKOFF_LIMIT} so one eviction does not fail the uninstall",
        )


if __name__ == "__main__":
    unittest.main()

"""Every workflow that restores an Actions cache caps the segment download wait.

`@actions/cache` waits up to ten minutes on one cache segment before it gives up.
When GitHub throttles the account's cache egress the download is refused within
seconds, but the segment timer is cleared only on success, so the refused
restore keeps the step alive until the timer fires. On 2026-10-30 two refused
restores spent the first twenty minutes of the agent-image job's 35-minute
budget in `python-tests.yml` and the job timed out in its test step. The
throttle is account-wide, so every workflow that restores a cache carries the
same exposure. `.agents/rules/github_actions.md` ("Cap the Actions cache segment
download wait") holds the rule and the trade-off behind the value.

One contract, read from the parsed YAML: a workflow with a step that restores a
cache (`actions/cache`, `actions/cache/restore`, `setup-go` unless `cache: false`,
or `setup-python`/`setup-node` with a `cache:` input) sets
`SEGMENT_DOWNLOAD_TIMEOUT_MINS` in its top-level `env` to a positive integer no
larger than the convention's value.
"""

import pathlib
import unittest

from tests.test_workflow_pins_and_fork_guards import WORKFLOW_GLOBS, _jobs, _load

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

_ENV_KEY = "env"
_VARIABLE = "SEGMENT_DOWNLOAD_TIMEOUT_MINS"
# The convention's value, in minutes. A refused restore then costs a step about
# a minute instead of ten; a healthy 128 MiB segment downloads in seconds.
_MAX_SEGMENT_TIMEOUT_MINUTES = 1

_CACHE_ACTIONS = ("actions/cache@", "actions/cache/restore@")
# Restores a cache whenever `cache:` is not set to false (the action's default).
_CACHE_BY_DEFAULT_ACTIONS = ("actions/setup-go@",)
# Restores a cache only when the `cache:` input names a package manager.
_CACHE_ON_REQUEST_ACTIONS = ("actions/setup-python@", "actions/setup-node@")

# A floor on how many cache-restoring workflows the sweep found, so a walk that
# stopped early or a loader that parsed nothing cannot pass by finding none.
_MIN_CACHING_WORKFLOWS = 12


def _steps(doc: dict):
    for job in _jobs(doc).values():
        steps = job.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict):
                    yield step


def _restores_a_cache(step: dict) -> bool:
    uses = step.get("uses")
    if not isinstance(uses, str):
        return False
    with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
    cache = with_.get("cache")
    if uses.startswith(_CACHE_ACTIONS):
        return True
    if uses.startswith(_CACHE_BY_DEFAULT_ACTIONS):
        return cache is not False
    if uses.startswith(_CACHE_ON_REQUEST_ACTIONS):
        return bool(cache)
    return False


def _caching_workflows() -> list[tuple[pathlib.Path, dict]]:
    found = []
    for glob in WORKFLOW_GLOBS:
        for path in sorted(_WORKFLOWS.glob(glob)):
            _, doc = _load(path)
            if doc is not None and any(_restores_a_cache(step) for step in _steps(doc)):
                found.append((path, doc))
    return found


class CacheSegmentTimeoutTest(unittest.TestCase):
    def test_the_sweep_finds_the_caching_workflows(self):
        names = [path.name for path, _ in _caching_workflows()]
        self.assertGreaterEqual(len(names), _MIN_CACHING_WORKFLOWS, names)
        self.assertIn("python-tests.yml", names)
        self.assertIn("k8s-operator-test.yml", names)
        self.assertNotIn("ghes-proxy-test.yml", names, "setup-go with cache: false restores nothing")

    def test_every_caching_workflow_caps_the_segment_wait(self):
        for path, doc in _caching_workflows():
            with self.subTest(workflow=path.name):
                env = doc.get(_ENV_KEY)
                self.assertIsInstance(env, dict, f"{path.name} restores a cache but has no top-level env")
                value = env.get(_VARIABLE)
                self.assertIsNotNone(value, f"{path.name} restores a cache but does not set {_VARIABLE}")
                self.assertTrue(str(value).isdigit(), f"{path.name}: {_VARIABLE} must be a positive integer, got {value!r}")
                self.assertGreaterEqual(int(value), 1, f"{path.name}: {_VARIABLE} must be at least one minute")
                self.assertLessEqual(int(value), _MAX_SEGMENT_TIMEOUT_MINUTES, f"{path.name}: {_VARIABLE} is above the convention's {_MAX_SEGMENT_TIMEOUT_MINUTES}")


if __name__ == "__main__":
    unittest.main()

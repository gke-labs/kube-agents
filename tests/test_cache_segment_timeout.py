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
cache (`actions/cache`, `actions/cache/restore`; `setup-go`, `trivy-action` and
`codeql-action/init` unless `cache: false`, the composite two through a nested
`actions/cache`; or `setup-python`/`setup-node` with a `cache:` input) sets
`SEGMENT_DOWNLOAD_TIMEOUT_MINS` in its top-level `env` to a positive integer no
larger than the convention's value. The synthetic cases at the end hold each
branch of that classification and each way the value can be wrong.
"""

import pathlib
import tempfile
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
# Restore a cache whenever `cache:` is not set to false (each action's default).
# trivy-action and codeql-action/init are composite actions that restore through
# a nested `actions/cache` step, so they inherit the same segment wait.
_CACHE_BY_DEFAULT_ACTIONS = ("actions/setup-go@", "aquasecurity/trivy-action@", "github/codeql-action/init@")
# Restores a cache only when the `cache:` input names a package manager.
_CACHE_ON_REQUEST_ACTIONS = ("actions/setup-python@", "actions/setup-node@")
# The spellings of "off" for a `cache:` input: YAML's boolean and the quoted
# string a composite action's input takes.
_CACHE_OFF = (False, "false")


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
    # Lower-cased: GitHub resolves an action's owner and repository case-insensitively.
    uses = uses.lower()
    with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
    cache = with_.get("cache")
    if uses.startswith(_CACHE_ACTIONS):
        return True
    if uses.startswith(_CACHE_BY_DEFAULT_ACTIONS):
        return cache not in _CACHE_OFF
    if uses.startswith(_CACHE_ON_REQUEST_ACTIONS):
        return bool(cache)
    return False


def _caching_workflows(workflows: pathlib.Path = _WORKFLOWS) -> list[tuple[pathlib.Path, dict]]:
    found = []
    for glob in WORKFLOW_GLOBS:
        for path in sorted(workflows.glob(glob)):
            _, doc = _load(path)
            if doc is not None and any(_restores_a_cache(step) for step in _steps(doc)):
                found.append((path, doc))
    return found


def _failures(path: pathlib.Path, doc: dict) -> list[str]:
    """Why a caching workflow fails the contract; empty when it holds."""
    env = doc.get(_ENV_KEY)
    if not isinstance(env, dict):
        return [f"{path.name} restores a cache but has no top-level env"]
    value = env.get(_VARIABLE)
    if value is None:
        return [f"{path.name} restores a cache but does not set {_VARIABLE}"]
    if not str(value).isdigit() or int(value) < 1:
        return [f"{path.name}: {_VARIABLE} must be a positive integer, got {value!r}"]
    if int(value) > _MAX_SEGMENT_TIMEOUT_MINUTES:
        return [f"{path.name}: {_VARIABLE} is above the convention's {_MAX_SEGMENT_TIMEOUT_MINUTES}"]
    return []


def _workflow(step: str, env: str | None) -> str:
    env_block = "" if env is None else f"env:\n  {_VARIABLE}: {env}\n"
    return f"on: push\n{env_block}jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n      - {step}\n"


class CacheSegmentTimeoutTest(unittest.TestCase):
    def test_the_sweep_finds_the_caching_workflows(self):
        names = [path.name for path, _ in _caching_workflows()]
        self.assertIn("python-tests.yml", names)
        self.assertIn("k8s-operator-test.yml", names)
        self.assertIn("security-scan.yml", names, "trivy-action restores its database through a nested actions/cache")
        self.assertNotIn("ghes-proxy-test.yml", names, "setup-go with cache: false restores nothing")

    def test_every_caching_workflow_caps_the_segment_wait(self):
        for path, doc in _caching_workflows():
            with self.subTest(workflow=path.name):
                self.assertEqual(_failures(path, doc), [])


class CacheSegmentTimeoutSyntheticTest(unittest.TestCase):
    """Each branch of the classification and each wrong value, on written fixtures."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)

    def _sweep(self, **workflows: str) -> dict[str, list[str]]:
        for name, text in workflows.items():
            (self.dir / f"{name}.yml").write_text(text, encoding="utf-8")
        return {path.name: _failures(path, doc) for path, doc in _caching_workflows(self.dir)}

    def test_restoring_steps_are_classified(self):
        found = self._sweep(
            cache=_workflow("uses: actions/cache@abc", '"1"'),
            restore=_workflow("uses: actions/cache/restore@abc", '"1"'),
            go=_workflow("uses: actions/setup-go@abc", '"1"'),
            go_off=_workflow("uses: actions/setup-go@abc\n        with:\n          cache: false", '"1"'),
            trivy=_workflow("uses: aquasecurity/trivy-action@abc", '"1"'),
            trivy_off=_workflow("uses: aquasecurity/trivy-action@abc\n        with:\n          cache: 'false'", '"1"'),
            codeql=_workflow("uses: github/codeql-action/init@abc", '"1"'),
            python=_workflow("uses: actions/setup-python@abc\n        with:\n          cache: pip", '"1"'),
            python_plain=_workflow("uses: actions/setup-python@abc", '"1"'),
            node=_workflow("uses: Actions/Setup-Node@abc\n        with:\n          cache: npm", '"1"'),
            save=_workflow("uses: actions/cache/save@abc", None),
            run=_workflow("run: echo hi", None),
        )
        self.assertEqual(sorted(found), ["cache.yml", "codeql.yml", "go.yml", "node.yml", "python.yml", "restore.yml", "trivy.yml"])
        self.assertTrue(all(v == [] for v in found.values()), found)

    def test_each_wrong_value_fails(self):
        found = self._sweep(
            missing_env=_workflow("uses: actions/cache@abc", None),
            zero=_workflow("uses: actions/cache@abc", '"0"'),
            ten=_workflow("uses: actions/cache@abc", '"10"'),
            words=_workflow("uses: actions/cache@abc", "abc"),
            negative=_workflow("uses: actions/cache@abc", '"-1"'),
            ok=_workflow("uses: actions/cache@abc", "1"),
        )
        self.assertEqual({name for name, v in found.items() if v}, {"missing_env.yml", "zero.yml", "ten.yml", "words.yml", "negative.yml"})
        self.assertEqual(found["ok.yml"], [])

    def test_a_workflow_with_no_steps_is_not_caching(self):
        found = self._sweep(empty="on: push\njobs: {}\n", broken="on: [\n")
        self.assertEqual(found, {})


if __name__ == "__main__":
    unittest.main()

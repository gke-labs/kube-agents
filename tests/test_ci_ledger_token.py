"""Tests for the ledger-token preflight in hack/ci-eval-pr.sh.

The six fleet-audit scenarios grade the ledger issue the run publishes, using
`ledger_issue_contains`, which reads that issue over the GitHub API with a
token taken from the runner's environment. Absent the token the check reports
`status: "error"` -- correct, and the reason the tasks stay commented out --
but it arrives as VerificationCoverage below the gate's floor on six tasks at
the end of a long run, which looks like six broken scenarios rather than one
missing credential.

The preflight turns that into a named refusal before the first task starts.
Four properties matter and none of them is observable from a green run:

* it fires only for tasks that actually read the ledger, so the pool owes the
  credential the day a ledger scenario is uncommented and not before;
* with a ledger task registered and no token, the run stops non-zero and names
  the variable and the doc rather than the six checks;
* under Prow it takes BENCH_GITHUB_TOKEN and nothing else, because an ambient
  GITHUB_TOKEN would let it report success and then fail in exactly the way it
  was added to prevent;
* a parse it cannot understand degrades to today's behaviour instead of
  demanding a credential the registered tasks do not need.

The block is extracted from the script by its section markers and executed, so
these assertions run against the code that ships rather than a copy.
"""

import pathlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import yaml

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL = _REPO_ROOT / "hack" / "ci-eval-pr.sh"

# Resolved once, so a case that empties PATH takes the parser away from the
# block under test without also taking away the shell running it.
_BASH = shutil.which("bash") or "/bin/bash"

# The section this suite exercises, and the marker that ends it. Both are
# asserted below, so renaming a section fails here loudly instead of silently
# shrinking what is tested.
_SECTION_START = "# 6b. Ledger-token preflight"
_SECTION_END_RE = re.compile(r"^FAILED_TASKS=\(\)", re.MULTILINE)

# A task file that reads the ledger, and one that does not. Written out rather
# than pointed at bench/tasks/, because the point of the preflight is what it
# does with a task the corpus does not contain yet.
_LEDGER_TASK = textwrap.dedent(
    """\
    name: fixture-audit
    verification_spec:
      objectives:
        - id: names-the-defect
          type: ledger_issue_contains
          audit: obtainability-audit
          required_phrases: ["checkout-gateway"]
    """
)

_PLAIN_TASK = textwrap.dedent(
    """\
    name: fixture-plain
    verification_spec:
      objectives:
        - id: names-the-defect
          type: report_contains
          required_phrases: ["checkout-gateway"]
    """
)


def _registered_tasks_block():
    """The literal `TASKS=( ... )` array as the script ships it."""
    text = _CI_EVAL.read_text(encoding="utf-8")
    match = re.search(r"^TASKS=\(\n.*?^\)\n", text, re.MULTILINE | re.DOTALL)
    assert match, "the TASKS array is no longer a top-level TASKS=( ... ) block"
    return match.group(0)


def _shipped_task_paths():
    """The uncommented entries of the shipped `TASKS` array."""
    return [
        line.strip().strip('"')
        for line in _registered_tasks_block().splitlines()
        if line.strip().startswith('"')
    ]


def _declares_a_ledger_check(task_yaml):
    """Does this task carry a ledger_issue_contains objective?

    A real YAML parse, so the equivalence test compares the block's regex
    against something that is not another regex.
    """

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "ledger_issue_contains":
                return True
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return False

    return walk(yaml.safe_load(pathlib.Path(task_yaml).read_text(encoding="utf-8")))


def _preflight_block():
    text = _CI_EVAL.read_text(encoding="utf-8")
    start = text.find(_SECTION_START)
    assert start != -1, f"{_SECTION_START!r} not found in hack/ci-eval-pr.sh"
    end_match = _SECTION_END_RE.search(text, start + len(_SECTION_START))
    assert end_match, "the preflight block is no longer followed by FAILED_TASKS=()"
    return text[start : end_match.start()]


class CiEvalLedgerTokenPreflightTest(unittest.TestCase):
    maxDiff = None

    def _run(self, task_bodies, path=None, **env):
        """Register `task_bodies` and run the preflight over them.

        `task_bodies` maps a task directory name to its task.yaml contents.
        `path` overrides PATH, which is how the parser is taken away from the
        block. Returns (returncode, stdout, stderr).
        """
        with tempfile.TemporaryDirectory() as tmp:
            bench = pathlib.Path(tmp)
            entries = []
            for task_name, body in task_bodies.items():
                task_dir = bench / "tasks" / task_name
                task_dir.mkdir(parents=True)
                (task_dir / "task.yaml").write_text(body, encoding="utf-8")
                entries.append(f'"./tasks/{task_name}/task.yaml"')

            script = (
                f'BENCH_DIR="{bench}"\n'
                f"TASKS=({' '.join(entries)})\n" + _preflight_block()
            )
            overrides = {
                # Cleared unless a case sets them: the developer's own shell
                # must not decide whether the preflight passes. PULL_NUMBER
                # and JOB_NAME likewise -- they choose the Prow branch.
                "BENCH_GITHUB_TOKEN": "",
                "GITHUB_TOKEN": "",
                "PULL_NUMBER": "",
                "JOB_NAME": "",
                **env,
            }
            if path is not None:
                overrides["PATH"] = path
            proc = subprocess.run(
                [_BASH, "-c", "set -euo pipefail\n" + script],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(overrides=overrides),
            )
            return proc.returncode, proc.stdout, proc.stderr

    # --- scoped to the tasks that need it ---------------------------------

    def test_no_ledger_task_needs_no_token(self):
        rc, out, err = self._run({"plain": _PLAIN_TASK})
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Ledger token", out)

    def test_its_verdict_on_every_task_file_matches_a_yaml_parse(self):
        """bash's regex and a real YAML parse must agree, over the whole corpus.

        Every task file, not just the ones `TASKS` currently registers. The
        six scenarios the block exists for are all commented out today, so
        restricting the comparison to registered tasks would leave nothing on
        the positive side of it and the test would collapse into "no ledger
        task needs no token", which the case above already covers. Stated as
        an equivalence rather than a snapshot because a snapshot has an expiry
        date: the day a ledger scenario is legitimately activated, a count
        assertion goes red and the only remedy is deleting it.
        """
        paths = sorted((_REPO_ROOT / "bench" / "tasks").glob("*/task.yaml"))
        expected = sorted(p.parent.name for p in paths if _declares_a_ledger_check(p))
        # Both branches have to be populated, or this compares two constants.
        self.assertTrue(expected, "no task in the corpus declares a ledger check")
        self.assertLess(len(expected), len(paths))

        rc, out, err = self._run(
            {p.parent.name: p.read_text(encoding="utf-8") for p in paths}
        )
        self.assertNotEqual(rc, 0, out)
        # The refusal prints one task per line, indented past its own prose.
        named = sorted(re.findall(r"^ {9}(\S+)$", err, re.MULTILINE))
        self.assertEqual(expected, named)

    def test_the_shipped_task_array_resolves_to_real_files(self):
        # The block joins BENCH_DIR to each TASKS entry and hands the result to
        # the parser, which swallows a missing file as "not a ledger task". A
        # mistyped path would therefore be invisible here rather than loud.
        registered = _shipped_task_paths()
        self.assertTrue(registered)
        for task in registered:
            self.assertTrue((_REPO_ROOT / "bench" / task).is_file(), task)

    # --- fail-closed -------------------------------------------------------

    def test_ledger_task_without_a_token_stops_the_run(self):
        rc, _, err = self._run({"audit": _LEDGER_TASK})
        self.assertNotEqual(rc, 0)
        # The message has to name the variable and the task, or the next
        # person to uncomment a scenario has a red job and no lead.
        self.assertIn("BENCH_GITHUB_TOKEN", err)
        self.assertIn("audit", err)
        self.assertIn("ci-pool-projects.md", err)

    def test_it_stops_before_any_task_runs(self):
        # As early as this script can manage, which is not as early as it
        # sounds: ci-deploy.sh has already built three images and waited out a
        # helm rollout by the time the runner starts. What this saves is the
        # task loop -- a cluster provision and a model budget per entry.
        text = _CI_EVAL.read_text(encoding="utf-8")
        preflight = text.find(_SECTION_START)
        loop = text.find('for TASK in "${TASKS[@]}"; do\n  TASK_NAME=')
        self.assertNotEqual(preflight, -1)
        self.assertNotEqual(loop, -1)
        self.assertLess(preflight, loop)

    def test_an_empty_task_matrix_is_inert(self):
        # bash before 4.4 treats "${TASKS[@]}" on an empty array as unbound
        # under `set -u`. A block whose stated purpose is to be inert must not
        # be the thing that kills the run.
        rc, out, err = self._run({})
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Ledger token", out)

    def test_one_ledger_task_among_many_is_enough(self):
        rc, _, err = self._run({"plain": _PLAIN_TASK, "audit": _LEDGER_TASK})
        self.assertNotEqual(rc, 0)
        self.assertIn("audit", err)

    # --- the token itself ---------------------------------------------------

    def test_bench_github_token_satisfies_it(self):
        rc, out, err = self._run({"audit": _LEDGER_TASK}, BENCH_GITHUB_TOKEN="ghp_fake")
        self.assertEqual(rc, 0, err)
        self.assertIn("BENCH_GITHUB_TOKEN set", out)

    def test_github_token_is_honoured_on_a_laptop(self):
        # The verifier falls back to it, so a developer who exports only
        # GITHUB_TOKEN must not be blocked by a check that is stricter than
        # the thing it is guarding.
        rc, out, err = self._run({"audit": _LEDGER_TASK}, GITHUB_TOKEN="ghp_fake")
        self.assertEqual(rc, 0, err)
        self.assertIn("GITHUB_TOKEN set", out)

    def test_github_token_is_refused_in_a_prow_run(self):
        """The hole the whole block would otherwise leave open.

        `tests/testing/common.py` strips `GITHUB_*` because those variables
        turn up ambiently on CI runners. One that arrived for some other
        purpose cannot see a private eval GitOps repository, so the issue
        fetch 404s and the six checks fail on correctness -- a red run either
        way, but reported as six wrong answers with a green line in front of
        them saying the credential is in play. Same posture as
        ci-deploy.sh's refusal of a pinned EVAL_GITOPS_REPO under Prow.
        """
        for prow_var in ("PULL_NUMBER", "JOB_NAME"):
            with self.subTest(prow_var=prow_var):
                rc, out, err = self._run(
                    {"audit": _LEDGER_TASK},
                    GITHUB_TOKEN="ghp_ambient",
                    **{prow_var: "123"},
                )
                self.assertNotEqual(rc, 0, out)
                self.assertIn("does not accept GITHUB_TOKEN", err)

    def test_bench_github_token_still_works_in_a_prow_run(self):
        rc, out, err = self._run(
            {"audit": _LEDGER_TASK}, BENCH_GITHUB_TOKEN="ghp_fake", PULL_NUMBER="123"
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("BENCH_GITHUB_TOKEN set", out)

    def test_the_token_value_is_never_echoed(self):
        secret = "ghp_donotlogthisvalue"
        rc, out, err = self._run({"audit": _LEDGER_TASK}, BENCH_GITHUB_TOKEN=secret)
        self.assertEqual(rc, 0, err)
        self.assertNotIn(secret, out)
        self.assertNotIn(secret, err)

    # --- the parse ----------------------------------------------------------

    def test_quoted_yaml_still_counts_as_a_ledger_task(self):
        # `type: "ledger_issue_contains"` is valid YAML the corpus does not
        # happen to use. Missing it would be a silent false negative -- the
        # opaque six-error run this block exists to prevent.
        rc, _, err = self._run(
            {"audit": _LEDGER_TASK.replace(
                "ledger_issue_contains", '"ledger_issue_contains"'
            )}
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("audit", err)

    def test_the_name_in_prose_is_not_a_ledger_task(self):
        """Anchoring earns its keep: a mention is not a declaration.

        The string turns up in prompts, in commented-out objectives and in
        this repository's own docs, and demanding a credential because a task
        mentions the verifier is a false positive that reds a presubmit. What
        the regex does NOT distinguish is a line inside a block scalar that
        itself begins `type: ledger_issue_contains` -- the same limit the
        sibling task_deployer and task_has_spec parsers carry, and not worth a
        YAML dependency in a bash preflight.
        """
        body = textwrap.dedent(
            """\
            name: fixture-prose
            prompt: |
              Do not use ledger_issue_contains for this one; the SOP keeps the
              closing message to one line and this task grades that message.
            verification_spec:
              objectives:
                - id: names-the-defect
                  # type: ledger_issue_contains
                  type: report_contains
                  required_phrases: ["checkout-gateway"]
            """
        )
        rc, out, err = self._run({"prose": body})
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Ledger token", out)

    def test_without_a_parser_the_block_demands_nothing(self):
        """The posture opposite to task_has_spec, and the reason for it.

        task_has_spec gates, so its unparseable case must fail closed. This
        block only diagnoses, and a broken parser reddening a presubmit to
        demand a credential the registered tasks do not need would be a worse
        outcome than losing the diagnosis. Taking python3 off PATH is the only
        way to reach that branch: a malformed task file still parses, because
        the parser is a regex over raw text and not a YAML load.
        """
        with tempfile.TemporaryDirectory() as empty_bin:
            rc, out, err = self._run({"audit": _LEDGER_TASK}, path=empty_bin)
            self.assertEqual(rc, 0, err)
            self.assertNotIn("Ledger token", out)

    def test_the_verifier_and_the_preflight_name_the_same_variables(self):
        """A rename on either side must not leave the preflight guarding air."""
        verifiers = (
            _REPO_ROOT / "bench" / "kube_agents_bench" / "verifiers.py"
        ).read_text(encoding="utf-8")
        # Matched on the names, not on the source line: this test is about a
        # rename, and reformatting the tuple is not one.
        tuple_src = re.search(r"LEDGER_TOKEN_ENV_VARS\s*=\s*\(([^)]*)\)", verifiers)
        self.assertIsNotNone(tuple_src, "LEDGER_TOKEN_ENV_VARS is no longer a tuple")
        self.assertEqual(
            ["BENCH_GITHUB_TOKEN", "GITHUB_TOKEN"],
            re.findall(r'"([^"]+)"', tuple_src.group(1)),
        )
        self.assertIn('@VERIFIERS.register("ledger_issue_contains")', verifiers)
        # Only variable REFERENCES count. Both names also appear in the block's
        # comments and in the advice string it prints, so `assertIn(name, text)`
        # passes even when every `${...}` in the block has been renamed.
        code = "\n".join(
            line
            for line in _preflight_block().splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertIn("${BENCH_GITHUB_TOKEN", code)
        self.assertIn("${GITHUB_TOKEN", code)
        self.assertIn("'ledger_issue_contains'", code)


class CiEvalWiringTest(unittest.TestCase):
    def test_ci_eval_parses(self):
        subprocess.run([_BASH, "-n", str(_CI_EVAL)], check=True)

    def test_the_refusal_clears_the_artifact_dump_trap(self):
        """A missing variable must not produce an infrastructure postmortem.

        The script installs `trap dump_prow_artifacts_on_failure EXIT`, which
        on any non-zero exit copies pod descriptions, previous-container logs
        and the Cloud Build list into ARTIFACTS. That is right for a broken
        deploy and wrong for an unset environment variable: it buries the
        one-line answer this block exists to give. Asserted statically because
        the trap lives in the script, not in the extracted block.
        """
        self.assertIn("trap dump_prow_artifacts_on_failure EXIT", _CI_EVAL.read_text(encoding="utf-8"))
        block = _preflight_block()
        clear = block.find("trap - EXIT")
        refuse = block.find("exit 1")
        self.assertNotEqual(clear, -1, "the refusal no longer clears the EXIT trap")
        self.assertNotEqual(refuse, -1)
        self.assertLess(clear, refuse)


if __name__ == "__main__":
    unittest.main()

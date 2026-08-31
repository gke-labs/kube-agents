"""Two things a workflow has to get right when it runs a release script.

Both fail only on a real runner, and both have already happened.

**The executable bit.** Every script a workflow runs as a command has to be
executable.

A missing mode bit is invisible everywhere it would normally be caught. The file
is present, `shellcheck` reads it, the unit tests run it through `bash <path>`,
`make docs-check` says nothing, and review sees a diff that looks complete. It
surfaces only on a real runner, as `Permission denied` and exit 126 — and for a
workflow that is dispatch-only or scheduled, that can be long after merge.

This is not hypothetical. #1058 added `dispatch_rc_pipeline.sh` at mode 100644,
and `rc-scheduler.yml` is the only thing that starts the release-candidate
pipeline, so the pipeline could not start at all until it was fixed. Two sibling
scripts landed the same way.

A sourced file is a different case and is deliberately not covered: `. path`
needs read permission and nothing more, which is why `teardown_common.sh` is not
executable and should not be.

**The four cluster coordinates.** `release_resolve_target` refuses to default in
CI and names `GKE_CLUSTER_NAME`, `GCP_REGION`, `GCP_PROJECT_ID` and
`AGENT_NAMESPACE` as required, so a step invoking a script that calls it with
three of the four dies with `Unset in CI`. `e2e-manual-runner.yml`'s readiness
step shipped that way and could never reach a cluster. Which scripts those are is
read out of `scripts/release/*.sh` rather than listed here, so a new caller in
that directory is covered without anyone remembering to add it. A caller
elsewhere, or one that is not a shell script, is not — that bound is the scan's,
not an oversight.
"""

import os
import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

# Matches a repo-relative script path invoked as a command. Deliberately not a
# YAML parse: a `run:` block is a shell script, so the invocation can sit behind
# an `if`, a pipe, or a loop, and the text is what the runner ultimately executes.
# It reads `bash ./scripts/foo.sh` as a command too, which needs read permission
# and not a mode bit. No workflow writes that today — the interpreter-prefixed
# invocations here omit the `./` and so never match — so this stays a known bound
# rather than a special case the pattern carries for nobody.
_INVOCATION = re.compile(r"(?<![\w/])\./((?:scripts|hack)/[\w/-]+\.(?:sh|py))")

# Paths a workflow names on purpose without expecting them on this branch.
# deploy-environment.yml and teardown-environment.yml check the candidate commit
# out over the workspace before running their script, so they fall back to the
# pre-rename names for candidates whose trees predate #1058. Those files exist in
# those trees and nowhere here. Drop these entries with the fallbacks, once no
# rc_*_validated tag predates the rename.
_EXPECTED_ABSENT = {
    "scripts/release/provision_rc_environment.sh",
    "scripts/release/teardown_rc_environment.sh",
}


def _invoked_scripts():
    found = {}
    for workflow in sorted(_WORKFLOWS.glob("*.yml")) + sorted(_WORKFLOWS.glob("*.yaml")):
        for match in _INVOCATION.finditer(workflow.read_text()):
            found.setdefault(match.group(1), set()).add(workflow.name)
    return found


class WorkflowScriptPermissionsTest(unittest.TestCase):
    def test_workflows_invoke_at_least_one_script(self):
        """Guards the regex: a pattern that matches nothing passes silently."""
        self.assertGreater(len(_invoked_scripts()), 10)

    def test_every_invoked_script_exists_and_is_executable(self):
        for script, workflows in sorted(_invoked_scripts().items()):
            with self.subTest(script=script):
                path = _REPO_ROOT / script
                callers = ", ".join(sorted(workflows))
                if script in _EXPECTED_ABSENT:
                    self.assertFalse(
                        path.exists(),
                        f"{script} now exists, so its entry in _EXPECTED_ABSENT is stale",
                    )
                    continue
                self.assertTrue(path.is_file(), f"{script} is invoked by {callers} but does not exist")
                self.assertTrue(
                    os.access(path, os.X_OK),
                    f"{script} is invoked as a command by {callers} but is not executable; "
                    f"the runner fails it with 'Permission denied' (exit 126). "
                    f"Fix with: git update-index --chmod=+x {script}",
                )


_RESOLVE_TARGET_VARS = ("GKE_CLUSTER_NAME", "GCP_REGION", "GCP_PROJECT_ID", "AGENT_NAMESPACE")


def _scripts_needing_resolve_target():
    """Release scripts that call release_resolve_target, read from source.

    common.sh defines it rather than calling it, so it is excluded — a workflow
    never runs it as a command anyway.
    """
    release = _REPO_ROOT / "scripts" / "release"
    names = set()
    for script in release.glob("*.sh"):
        if script.name == "common.sh":
            continue
        if "release_resolve_target" in script.read_text():
            names.add(script.name)
    return names


class ResolveTargetEnvWiringTest(unittest.TestCase):
    def test_the_script_set_is_not_empty(self):
        """Guards the source scan the case below depends on."""
        self.assertTrue(_scripts_needing_resolve_target())

    def test_every_step_running_one_passes_all_four_coordinates(self):
        import yaml

        needed = _scripts_needing_resolve_target()
        checked = 0
        for workflow in sorted(_WORKFLOWS.glob("*.yml")) + sorted(_WORKFLOWS.glob("*.yaml")):
            doc = yaml.safe_load(workflow.read_text()) or {}
            workflow_env = doc.get("env") or {}
            for job_name, job in (doc.get("jobs") or {}).items():
                job_env = job.get("env") or {}
                for step in job.get("steps") or []:
                    run = step.get("run") or ""
                    hit = next((s for s in needed if s in run), None)
                    if hit is None:
                        continue
                    checked += 1
                    # Workflow, job and step scopes, in the order the runner
                    # resolves them: a step reading a coordinate declared once on
                    # the job is wired correctly, not missing it.
                    env = {**workflow_env, **job_env, **(step.get("env") or {})}
                    missing = [v for v in _RESOLVE_TARGET_VARS if v not in env]
                    with self.subTest(workflow=workflow.name, job=job_name, script=hit):
                        self.assertEqual(
                            missing,
                            [],
                            f"{workflow.name}: job '{job_name}' runs {hit}, which calls "
                            f"release_resolve_target, but its env: omits {missing}. In CI that "
                            f"function refuses to default and exits with 'Unset in CI'.",
                        )
        self.assertGreater(checked, 0, "no workflow step invokes these scripts — the scan is stale")


if __name__ == "__main__":
    unittest.main()

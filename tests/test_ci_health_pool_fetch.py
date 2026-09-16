"""The pool-pressure fetch step in .github/workflows/ci-health.yml (#1607).

Three outcomes, and the step is the only thing that distinguishes them: no
pointer means the periodic was never wired up and the bot says nothing; a
pointer to a build that published nothing means the periodic is running and
failing, and the bot has to say so; both present means the artifact. The
middle one is the dead-man's switch -- an artifact never written never gets
old, so ageing `window_end` cannot catch it -- and it is one `if !` branch
that no other test reaches. The step's own bash is lifted and run here rather
than copied, against a stub gsutil.
"""

import json
import pathlib
import subprocess
import tempfile
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-health.yml"
JOB = "refresh-and-adjudicate"
STEP = "Fetch the pool-pressure artifact"
ARTIFACT = "work/pool-pressure.json"
LOGS = "gs://fake-prow/logs/ci-kube-agents-pool-pressure"
BUILD = "2099957253191766016"
# What the periodic publishes, cut to the fields health.py reads.
READING = {"verdict": "OK", "window_end": "2026-09-15T20:23:27Z"}


def step_script() -> str:
    # PyYAML resolves a bare `on:` key to the boolean True (YAML 1.1), so the
    # workflow is indexed by name rather than unpacked.
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = [s for s in workflow["jobs"][JOB]["steps"] if s.get("name") == STEP]
    assert steps, f"{WORKFLOW} has no {STEP!r} step in job {JOB!r}"
    return steps[0]["run"]


class PoolFetchStepTest(unittest.TestCase):
    def run_step(self, pointer=BUILD, artifact=READING):
        """The step against a stub gsutil. `pointer` None means latest-build.txt
        is unreadable, `artifact` None means the copy fails."""
        tmp = pathlib.Path(tempfile.mkdtemp())
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        cat = f'printf "%s\\n" {pointer}' if pointer is not None else "exit 1"
        if artifact is None:
            copy = "exit 1"
        else:
            payload = tmp / "payload.json"
            payload.write_text(json.dumps(artifact))
            copy = f'cat "{payload}" > "${{@: -1}}"'
        (bin_dir / "gsutil").write_text(
            "#!/bin/bash\n"
            "shift  # -q\n"
            'case "$1" in\n'
            f"  cat) {cat} ;;\n"
            f"  cp) shift; {copy} ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n"
        )
        (bin_dir / "gsutil").chmod(0o755)
        result = subprocess.run(
            ["bash", "-c", step_script()],
            cwd=tmp,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "POOL_PRESSURE_LOGS": LOGS},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        written = tmp / ARTIFACT
        return json.loads(written.read_text()) if written.exists() else None

    def test_a_pointer_that_resolves_writes_the_periodics_own_artifact(self):
        self.assertEqual(READING, self.run_step())

    def test_no_pointer_writes_nothing(self):
        """"Not wired up" is silence: no note, no digest wait, no message."""
        self.assertIsNone(self.run_step(pointer=None))
        self.assertIsNone(self.run_step(pointer=""))

    def test_a_pointer_to_a_build_that_published_nothing_writes_a_reading_less_document(self):
        """The dead-man's switch. health.py reads a document with no
        `window_end` as STALE, which is the message this case owes."""
        written = self.run_step(artifact=None)
        self.assertIsNotNone(written, "an unreadable artifact must not read as 'not wired up'")
        self.assertNotIn("window_end", written)
        self.assertNotIn("verdict", written)
        self.assertIn(BUILD, written["note"])

    def test_the_step_never_fails_the_job(self):
        """Every path exits 0, and the step is `continue-on-error` besides: a
        dashboard refresh must not stop because the queue could not be read."""
        workflow = yaml.safe_load(WORKFLOW.read_text())
        step, = [s for s in workflow["jobs"][JOB]["steps"] if s.get("name") == STEP]
        self.assertTrue(step["continue-on-error"])


if __name__ == "__main__":
    unittest.main()

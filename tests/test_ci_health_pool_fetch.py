"""The pool-pressure fetch step in .github/workflows/ci-health.yml (#1607).

Four outcomes, and the step is the only thing that distinguishes them: no
pointer means the periodic was never wired up and the bot says nothing; a
build still running means this tick takes no reading; a finished build that
published nothing means the periodic is running and failing, and the bot has
to say so; anything else is the artifact. The third is the dead-man's switch
-- an artifact never written never gets old, so ageing `window_end` cannot
catch it -- and it is one `if !` branch that no other test reaches. The step's
own bash is lifted and run here rather than copied, against a stub gsutil.
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
EARLIER = "2099957253191766015"
OLDEST = "2099957253191766014"
# What the periodic publishes, cut to the fields health.py reads. One per
# build, so a test can say which one was read.
READING = {"verdict": "OK", "window_end": "2026-09-15T20:23:27Z"}
EARLIER_READING = {"verdict": "OK", "window_end": "2026-09-15T19:22:11Z"}
OLDEST_READING = {"verdict": "OK", "window_end": "2026-09-15T18:21:04Z"}
# gsutil's own wording for the two failures the step has to tell apart.
NOT_FOUND = "CommandException: No URLs matched: " + LOGS + "/latest-build.txt"
DENIED = "AccessDeniedException: 403 does not have storage.objects.get access"


def workflow_env(name: str) -> str:
    """A workflow-level env value, so the step runs here under the number it
    runs under in CI."""
    return str(yaml.safe_load(WORKFLOW.read_text())["env"][name])


def step_script() -> str:
    # PyYAML resolves a bare `on:` key to the boolean True (YAML 1.1), so the
    # workflow is indexed by name rather than unpacked.
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = [s for s in workflow["jobs"][JOB]["steps"] if s.get("name") == STEP]
    assert steps, f"{WORKFLOW} has no {STEP!r} step in job {JOB!r}"
    return steps[0]["run"]


class PoolFetchStepTest(unittest.TestCase):
    def run_step(self, pointer=BUILD, artifact=READING, raw=None, finished=True, builds=(EARLIER, BUILD), aborted=(), pointer_error=NOT_FOUND):
        """The step against a stub gsutil. `pointer` None means latest-build.txt
        is unreadable and `pointer_error` is what gsutil says about it,
        `artifact` None means the copy fails, `raw` is bytes copied verbatim --
        what a crashed periodic leaves behind, `finished` False is a build
        still running, `builds` is what the log prefix holds, which is how far
        back the fallback can reach, and `aborted` are the builds in it that
        published no finished.json."""
        box = tempfile.TemporaryDirectory()
        self.addCleanup(box.cleanup)
        tmp = pathlib.Path(box.name)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        cat = f'printf "%s\\n" {pointer}' if pointer is not None else f'echo "{pointer_error}" >&2; exit 1'
        if artifact is None and raw is None:
            copy = "exit 1"
        else:
            payload = tmp / "payload.json"
            payload.write_text(json.dumps(artifact) if raw is None else raw)
            copy = f'cat "{payload}" > "${{@: -1}}"'
        # `gsutil ls` on the prefix, the way the fallback reads it: the build
        # directories plus the pointer object, which the step has to skip.
        listing = "".join(f"{LOGS}/{b}/\\n" for b in builds)
        # One stat and one cp per prior build, so the walk past an aborted one
        # is answered rather than falling through to exit 2.
        priors = ""
        for build, reading in ((EARLIER, EARLIER_READING), (OLDEST, OLDEST_READING)):
            published = tmp / f"{build}.json"
            published.write_text(json.dumps(reading))
            priors += f'  "stat {LOGS}/{build}/finished.json") exit {1 if build in aborted else 0} ;;\n'
            priors += f'  "cp {LOGS}/{build}/artifacts/pool-pressure.json") cat "{published}" > "${{@: -1}}" ;;\n'
        # Matched on the verb and the object it names, so the step asking for
        # the wrong path falls through to exit 2 rather than being answered.
        (bin_dir / "gsutil").write_text(
            "#!/bin/bash\n"
            "shift  # -q\n"
            'case "$1 $2" in\n'
            f'  "cat {LOGS}/latest-build.txt") {cat} ;;\n'
            f'  "ls {LOGS}/") printf "{LOGS}/latest-build.txt\\n{listing}" ;;\n'
            f'  "stat {LOGS}/{BUILD}/finished.json") exit {0 if finished else 1} ;;\n'
            f'  "cp {LOGS}/{BUILD}/artifacts/pool-pressure.json") {copy} ;;\n'
            f"{priors}"
            '  *) echo "unexpected gsutil call: $*" >&2; exit 2 ;;\n'
            "esac\n"
        )
        (bin_dir / "gsutil").chmod(0o755)
        result = subprocess.run(
            ["bash", "-c", step_script()],
            cwd=tmp,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "POOL_PRESSURE_LOGS": LOGS, "POOL_FALLBACK_BUILDS": workflow_env("POOL_FALLBACK_BUILDS")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.said = result.stdout
        written = tmp / ARTIFACT
        return json.loads(written.read_text()) if written.exists() else None

    def test_a_pointer_that_resolves_writes_the_periodics_own_artifact(self):
        self.assertEqual(READING, self.run_step())

    def test_no_pointer_writes_nothing(self):
        """"Not wired up" is silence: no note, no digest wait, no message."""
        self.assertIsNone(self.run_step(pointer=None))
        self.assertIsNone(self.run_step(pointer=""))

    def test_a_build_still_running_reads_the_build_before_it(self):
        """The pointer moves at job start, so a tick landing inside the ~8
        minutes the periodic takes sees a build with no artifact yet. Reading
        that as a stopped job is a false alert roughly once an hour, and
        skipping the tick drops the note from health.json, the Brief and the
        digest. The previous build's reading is an hour old at most."""
        self.assertEqual(
            EARLIER_READING,
            self.run_step(finished=False),
            "a running build must fall back to the last finished one",
        )

    def test_the_fallback_walks_past_a_build_that_was_aborted(self):
        """An aborted build -- evicted pod, lost node -- publishes neither
        finished.json nor an artifact. Reading it would write the sentinel and
        announce that the periodic had stopped, while it is minutes from
        publishing. The build before that one holds a real reading."""
        self.assertEqual(
            OLDEST_READING,
            self.run_step(finished=False, builds=(OLDEST, EARLIER, BUILD), aborted=(EARLIER,)),
        )

    def test_nothing_finished_behind_a_running_build_is_silence(self):
        """Not the sentinel: the periodic is publishing, this tick just has
        nothing to read. `⚪ pool check stopped` would be false."""
        self.assertIsNone(self.run_step(finished=False, builds=(EARLIER, BUILD), aborted=(EARLIER,)))

    def test_a_pointer_that_cannot_be_read_warns_unless_it_is_missing(self):
        """A denied read and a periodic that never ran both leave no artifact,
        and only one of them is "not wired up". Unwarned, a missing IAM grant
        ships the whole feature dark: no note, no digest figure, and no ⚪
        either, because the dead-man's switch ages only an artifact it read."""
        self.assertIsNone(self.run_step(pointer=None, pointer_error=DENIED))
        self.assertIn("::warning::", self.said)
        self.assertIsNone(self.run_step(pointer=None, pointer_error=NOT_FOUND))
        self.assertNotIn("::warning::", self.said)

    def test_a_first_build_still_running_writes_nothing(self):
        """Nothing to fall back to, which is the same silence as no pointer:
        the periodic has never published."""
        self.assertIsNone(self.run_step(finished=False, builds=(BUILD,)))

    def test_a_pointer_to_a_build_that_published_nothing_writes_a_reading_less_document(self):
        """The dead-man's switch. health.py reads a document with no
        `window_end` as STALE, which is the message this case owes."""
        written = self.run_step(artifact=None)
        self.assertIsNotNone(written, "an unreadable artifact must not read as 'not wired up'")
        self.assertNotIn("window_end", written)
        self.assertNotIn("verdict", written)
        self.assertIn(BUILD, written["note"])

    def test_an_artifact_that_copies_but_does_not_parse_gets_the_same_treatment(self):
        """The periodic redirects into the file before it runs, so a crash
        publishes a 0-byte object and `gsutil cp` copies it happily. Left
        alone, health.py reads a bare `{}` as no artifact at all -- the one
        reading that means "not wired up"."""
        for name, content in (("empty", ""), ("truncated", '{"verdict": "OK", "trend": {'), ("html", "<html>nope")):
            with self.subTest(name):
                written = self.run_step(artifact=None, raw=content)
                self.assertIsNotNone(written, "an unusable artifact must not read as 'not wired up'")
                self.assertNotIn("window_end", written)
                self.assertIn(BUILD, written["note"])

    def test_the_step_never_fails_the_job(self):
        """Every path exits 0, and the step is `continue-on-error` besides: a
        dashboard refresh must not stop because the queue could not be read."""
        workflow = yaml.safe_load(WORKFLOW.read_text())
        step, = [s for s in workflow["jobs"][JOB]["steps"] if s.get("name") == STEP]
        self.assertTrue(step["continue-on-error"])


if __name__ == "__main__":
    unittest.main()

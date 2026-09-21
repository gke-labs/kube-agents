"""What the cron-prompt scan decides to check, and what it leaves alone.

`hack/check-docs-terminology.sh` verifies that every cron prompt quoted in the
documentation is a verbatim copy of a prompt in the roster. Deciding *which*
`"prompt"` keys are claiming to be quotations is the whole difficulty, and it
has failed in both directions: too wide and an unrelated pull request that
renders a LiteLLM request body goes red with an error about cron jobs; too
narrow and a drifted quotation sits unchecked while the guard reports PASS.

Neither direction fails visibly in this repository. The tree happens to contain
three quotations and no near-misses, so the guard prints "Terminology check
passed" whichever way the classifier is wrong. The cases below are the
near-misses the tree does not have.

`hack/scan-cron-prompts.awk` is driven directly for the classification cases,
because the shell script reads the repository it ships in — `git ls-files`,
the real rosters, `audit_report.py` — and none of that is fixtured. What the
script does with a classified hit is tested through the script itself: it
accepts extra documents in `DOCS_TERMINOLOGY_EXTRA_FILES`, appended to the
tracked set, so a document built to fail one check runs against the real
rosters and the verdict is read from the output. Those runs assert on the
error each check prints, not on the exit status alone, so a terminology
failure elsewhere in the tree does not read as one of these.

Run:
  python3 -m unittest discover -s tests -p 'test_docs_terminology_guard.py' -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_AWK = REPO_ROOT / "hack" / "scan-cron-prompts.awk"
GUARD = REPO_ROOT / "hack" / "check-docs-terminology.sh"

# One real roster id, so the fixtures read like the documents they stand in for.
KNOWN_ID = "fleet-consistency-drift"
# A `no_agent` job: it runs a script, and its roster entry renders `"prompt": ""`.
NO_AGENT_ID = "profile-cron-tick"
ROSTER = REPO_ROOT / "agents" / "platform" / "cron" / "jobs.json"
# How much of a real prompt a fixture quotes: clear of the guard's floor on a
# quotation's length, short of any character JSON would escape in the roster.
QUOTED_PREFIX_CHARS = 40


def scan(document: str, awk: str = "awk") -> list[tuple[str, int]]:
    """Run the scan over one document; return its (verdict, line) decisions."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ids = root / "ids.txt"
        ids.write_text(KNOWN_ID + "\n", encoding="utf-8")
        doc = root / "doc.md"
        doc.write_text(textwrap.dedent(document).lstrip("\n"), encoding="utf-8")
        proc = subprocess.run(
            [awk, "-v", f"idfile={ids}", "-f", str(SCAN_AWK), str(doc)],
            capture_output=True,
            text=True,
            check=True,
        )
    out = []
    for line in proc.stdout.splitlines():
        kind, _path, lineno, _text = line.split(":", 3)
        out.append((kind, int(lineno)))
    return out


class ScanScopeTest(unittest.TestCase):
    """Which `"prompt"` keys the scan claims, and which it declines."""

    def test_prose_naming_json_keys_is_not_a_roster_entry(self):
        # `concepts/governance-sops.md` has no fence anywhere and writes
        # `"skills": [...]` in a sentence. Treating a fence-free document as one
        # block made every such page one malformed roster entry, so any prose
        # sentence added to it that spelled `"prompt": "` failed CI for the
        # whole repository.
        self.assertEqual(
            scan(
                """
                # Governance SOPs

                Each SOP is reachable from an entry whose "skills": ["fleet-audit"]
                line names the skill that loads it.

                A roster entry's "prompt": "text the operator wrote" field carries
                the instruction the agent wakes up to.
                """
            ),
            [],
        )

    def test_prose_after_a_closed_fence_is_prose_again(self):
        hits = scan(
            f"""
            ```json
            {{ "id": "{KNOWN_ID}", "prompt": "Compare every cluster." }}
            ```

            Prose mentioning "prompt": "something made up" is not a quotation.
            """
        )
        self.assertEqual(hits, [("R", 2)])

    def test_an_unrelated_request_body_is_left_alone(self):
        # A LiteLLM or Vertex payload has a `"prompt"` too. No roster id in the
        # document, so nothing here is claiming to quote the roster.
        self.assertEqual(
            scan(
                """
                ```json
                { "model": "gpt-4", "prompt": "Summarise the ticket." }
                ```
                """
            ),
            [],
        )

    def test_a_request_body_beside_a_roster_entry_is_still_left_alone(self):
        # The document-level fallback must not reach a block that carries keys
        # of its own -- otherwise a page documenting both a cron job and an LLM
        # call fails on the LLM call.
        hits = scan(
            f"""
            ```json
            {{ "id": "{KNOWN_ID}", "prompt": "Compare every cluster." }}
            ```

            ```json
            {{ "model": "gpt-4", "prompt": "Summarise the ticket." }}
            ```
            """
        )
        self.assertEqual(hits, [("R", 2)])

    def test_a_quotation_trimmed_to_the_prompt_alone_is_still_graded(self):
        # No id, no sibling key, nothing for either structural rule to catch.
        # Deciding purely per block left this graded by nothing at all, which is
        # the silent pass the guard exists to prevent.
        hits = scan(
            f"""
            ```json
            {{
              "id": "{KNOWN_ID}",
              "schedule": "20 8 * * 1",
              "prompt": "Compare every cluster."
            }}
            ```

            Quoted again on its own, drifted:

            ```json
            "prompt": "Compare every cluster, twice."
            ```
            """
        )
        self.assertEqual(hits, [("R", 5), ("R", 12)])


class BlockquotedFenceTest(unittest.TestCase):
    """A fence inside a blockquote opens and closes a block like any other."""

    def test_two_blockquoted_manifests_are_two_blocks(self):
        # The fence regex used to reject `> ```json`, so these merged into one
        # block and the id in the first graded the prompt in the second.
        hits = scan(
            f"""
            > ```json
            > {{ "id": "{KNOWN_ID}", "prompt": "Compare every cluster." }}
            > ```

            > ```json
            > {{ "schedule": "0 8 * * *", "prompt": "Compare every cluster." }}
            > ```
            """
        )
        self.assertEqual(hits, [("R", 2), ("O", 6)])


class FenceLengthTest(unittest.TestCase):
    """A fence closes on its own character at its own length or longer."""

    def test_a_longer_fence_showing_a_marker_does_not_invert_the_scan(self):
        # The way a page demonstrates a fence: a four-backtick block holding a
        # single ``` line. Toggling on every marker read that as two fences and
        # left the scan inverted, so the quotation after it was never graded.
        hits = scan(
            f"""
            ````markdown
            Start the entry with
            ```json
            ````

            ```json
            {{ "id": "{KNOWN_ID}", "prompt": "Compare every cluster." }}
            ```
            """
        )
        self.assertEqual(hits, [("R", 7)])

    def test_a_tilde_fence_holding_backtick_markers_is_one_block(self):
        hits = scan(
            f"""
            ~~~markdown
            ```json
            {{ "id": "{KNOWN_ID}", "prompt": "Compare every cluster." }}
            ```
            ~~~
            """
        )
        self.assertEqual(hits, [("R", 3)])


class OrphanEntryTest(unittest.TestCase):
    """A rendered entry naming no known job is reported, never skipped."""

    def test_an_entry_with_no_id_is_reported(self):
        hits = scan(
            """
            ```json
            {
              "schedule": "0 8 * * *",
              "prompt": "Compare every cluster."
            }
            ```
            """
        )
        self.assertEqual(hits, [("O", 4)])

    def test_an_entry_trimmed_to_name_risk_and_enabled_is_still_reported(self):
        # The rendered examples carry `name`, `risk` and `enabled` beside the
        # routing keys. Drop the `id` line from a copy and keep those three, and
        # the block set only `otherkey`: not a roster entry, not a request body,
        # so neither graded nor reported. `risk` is the roster-only key here;
        # `name` and `enabled` are too common to count (next test).
        hits = scan(
            """
            ```json
            {
              "name": "Fleet consistency drift",
              "risk": "low",
              "enabled": true,
              "prompt": "Compare every cluster."
            }
            ```
            """
        )
        self.assertEqual(hits, [("O", 6)])

    def test_a_renamed_entry_beside_a_current_one_in_the_same_fence_is_reported(self):
        # An excerpt of two entries in one fence. The known id used to carry
        # the block to `R`, so the renamed entry's prompt was graded verbatim
        # and its stale id went unreported. The block is one excerpt and one
        # edit fixes it, so both lines are refused.
        hits = scan(
            f"""
            ```json
            {{ "id": "{KNOWN_ID}", "schedule": "20 8 * * 1", "prompt": "Compare every cluster." }}
            {{ "id": "fleet-consistency-drft", "schedule": "20 8 * * 2", "prompt": "Compare every cluster." }}
            ```
            """
        )
        self.assertEqual(hits, [("O", 2), ("O", 3)])

    def test_a_placeholder_id_in_angle_brackets_marks_an_illustration(self):
        # A how-to showing the shape of an entry has no job to name. The
        # placeholder is the one spelling the orphan error offers for that, so
        # it is neither graded nor reported, whatever else the block carries.
        self.assertEqual(
            scan(
                """
                ```json
                {
                  "id": "<your-audit>",
                  "schedule": "0 8 * * *",
                  "prompt": "Read governance/<your-sop>.md and report."
                }
                ```
                """
            ),
            [],
        )

    def test_a_placeholder_does_not_switch_off_a_real_entry_in_the_same_fence(self):
        # A placeholder pasted beside a quotation must not silence it: the
        # block is graded on the real id (and refused on an unknown one) as if
        # the placeholder were not there.
        real = scan(
            f"""
            ```json
            {{ "id": "<your-audit>", "prompt": "Read governance/<your-sop>.md." }}
            {{ "id": "{KNOWN_ID}", "prompt": "Compare every cluster, twice." }}
            ```
            """
        )
        self.assertEqual(real, [("R", 2), ("R", 3)])
        renamed = scan(
            """
            ```json
            { "id": "<your-audit>", "prompt": "Read governance/<your-sop>.md." }
            { "id": "fleet-consistency-drft", "schedule": "20 8 * * 2", "prompt": "Compare every cluster." }
            ```
            """
        )
        self.assertEqual(renamed, [("O", 2), ("O", 3)])

    def test_an_unrelated_object_with_an_id_or_a_name_is_left_alone(self):
        # A bench scenario carries `id`, `name` and `prompt`; a tool definition
        # carries `name` and `prompt`. Neither is a roster entry, and refusing
        # them with "restore the id line" would rewrite an example that never
        # quoted the roster. Only a roster-only key makes an unknown id a
        # renamed job.
        for document in (
            """
            ```json
            { "id": "portal-readonly-smoke", "name": "Portal smoke", "prompt": "Open the portal and read." }
            ```
            """,
            """
            ```json
            { "name": "summarise", "prompt": "Summarise the ticket.", "enabled": true }
            ```
            """,
        ):
            with self.subTest(document=document.strip()[:60]):
                self.assertEqual(scan(document), [])

    def test_an_entry_whose_id_the_roster_does_not_know_is_reported(self):
        # A renamed or mistyped job. The error message already offers "correct
        # it if the job was renamed"; before this it was silently unchecked,
        # because no sibling key survived the trim to mark the block cron-shaped.
        # The `schedule` beside it is what says this is a roster entry and not
        # some other object with an id (previous test).
        hits = scan(
            """
            ```json
            {
              "id": "fleet-consistency-drft",
              "schedule": "20 8 * * 1",
              "prompt": "Compare every cluster."
            }
            ```
            """
        )
        self.assertEqual(hits, [("O", 5)])


class AwkPortabilityTest(unittest.TestCase):
    """CI runs Ubuntu, where /usr/bin/awk is mawk, not the BSD awk on a Mac."""

    def test_every_awk_on_this_machine_agrees(self):
        document = f"""
            ```json
            {{ "id": "{KNOWN_ID}", "prompt": "a", "schedule": "0 8 * * *" }}
            ```

            ```json
            "prompt": "b"
            ```

            > ```json
            > {{ "id": "nope", "prompt": "c" }}
            > ```
            """
        available = [a for a in ("awk", "mawk", "gawk") if shutil.which(a)]
        self.assertIn("awk", available)
        results = {a: scan(document, awk=a) for a in available}
        self.assertEqual(
            len(set(map(tuple, results.values()))),
            1,
            f"awk implementations disagree: {results}",
        )


def real_prompt_prefix(job_id: str = KNOWN_ID) -> str:
    """The opening of a roster prompt, as it is spelled in the roster file."""
    jobs = json.loads(ROSTER.read_text(encoding="utf-8"))
    jobs = jobs.get("jobs", jobs)
    prompt = next(job["prompt"] for job in jobs if job["id"] == job_id)
    prefix = prompt[:QUOTED_PREFIX_CHARS]
    # A `\"` or a newline is spelled differently in the file and in the
    # decoded string; keep the fixture inside the range where they agree.
    assert '"' not in prefix and "\\" not in prefix, prefix
    return prefix


def run_guard(
    documents: list[str], extra_paths: tuple[str, ...] = ()
) -> subprocess.CompletedProcess:
    """Run the guard over the real tree plus the given documents."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = []
        for index, document in enumerate(documents):
            doc = root / f"doc{index}.md"
            doc.write_text(textwrap.dedent(document).lstrip("\n"), encoding="utf-8")
            paths.append(str(doc))
        paths.extend(extra_paths)
        listing = root / "extra-files.txt"
        listing.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
        return subprocess.run(
            [str(GUARD)],
            cwd=REPO_ROOT,
            env={**os.environ, "DOCS_TERMINOLOGY_EXTRA_FILES": str(listing)},
            capture_output=True,
            text=True,
            check=False,
        )


def listed_under(output: str, error: str) -> str:
    """The indented hit lines the guard prints under the `::error::` naming `error`."""
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("::error::") and error in line:
            hits = []
            for hit in lines[index + 1 :]:
                if not hit.startswith("    "):
                    break
                hits.append(hit)
            return "\n".join(hits)
    return ""


class GuardVerdictTest(unittest.TestCase):
    """What the script does with a hit the scan classified.

    The scan tests above stop at `R:`/`O:`. Everything after that -- the
    length floor, the verbatim check, one line carrying two values, a grep
    that could not run -- is the shell script's, and the tree happens to
    exercise none of its failing branches, so each is handed a document here.
    """

    STALE = "not a verbatim copy"
    SHORT = "elided down to fewer than"
    ORPHAN = "names no job id"
    UNRUN = "A terminology check could not run"

    def test_a_verbatim_quotation_raises_no_prompt_error(self):
        # The control: the same shape as every failing case below, so a red
        # there is the injected defect and not the fixture.
        proc = run_guard(
            [
                f"""
                ```json
                {{ "id": "{KNOWN_ID}", "prompt": "{real_prompt_prefix()}…" }}
                ```
                """
            ]
        )
        # Scoped to the fixture: a stale quotation elsewhere in the tree is the
        # guard's finding to report, not this suite's.
        self.assertNotIn("doc0.md", proc.stdout + proc.stderr)
        self.assertNotIn(self.UNRUN, proc.stdout + proc.stderr)

    def test_a_drifted_quotation_is_reported_stale(self):
        proc = run_guard(
            [
                f"""
                ```json
                {{ "id": "{KNOWN_ID}", "prompt": "{real_prompt_prefix()} and a clause the roster never had." }}
                ```
                """
            ]
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("doc0.md", listed_under(proc.stdout, self.STALE))

    def test_a_quotation_cut_to_almost_nothing_is_reported(self):
        # `R…` reduces to one character, and a one-character `grep -F` matches
        # every roster that has ever existed.
        proc = run_guard(
            [
                f"""
                ```json
                {{ "id": "{KNOWN_ID}", "prompt": "R…" }}
                ```
                """
            ]
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        # Scoped to the fixture on both sides: listed under the length floor,
        # and not under the verbatim check, whatever the rest of the tree says.
        self.assertIn("doc0.md", listed_under(proc.stdout, self.SHORT))
        self.assertNotIn("doc0.md", listed_under(proc.stdout, self.STALE))

    def test_an_entry_naming_a_job_the_roster_lacks_is_refused_by_the_guard(self):
        # The awk's `O:` line is only half of the refusal; the script has to
        # turn it into an error and a failing exit, and this is the one test
        # that reads that half.
        proc = run_guard(
            [
                """
                ```json
                { "id": "fleet-consistency-drft", "schedule": "20 8 * * 1", "prompt": "Compare every cluster." }
                ```
                """
            ]
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("doc0.md", listed_under(proc.stdout, self.ORPHAN))

    def test_a_rendered_no_agent_entry_is_not_an_elision(self):
        # `"prompt": ""` is the roster's own text for a job that runs a script.
        proc = run_guard(
            [
                f"""
                ```json
                {{ "id": "{NO_AGENT_ID}", "prompt": "", "no_agent": true }}
                ```
                """
            ]
        )
        self.assertNotIn("doc0.md", proc.stdout + proc.stderr)

    def test_the_first_of_two_values_on_one_line_is_graded(self):
        # A greedy `^.*"prompt"` strip graded only the last value on a line, so
        # a fabricated first value beside a verbatim second one passed green.
        proc = run_guard(
            [
                f"""
                ```json
                {{ "id": "{KNOWN_ID}", "prompt": "Made up entirely, and long enough to clear the floor." }} {{ "id": "{KNOWN_ID}", "prompt": "{real_prompt_prefix()}…" }}
                ```
                """
            ]
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("doc0.md", listed_under(proc.stdout, self.STALE))

    def test_an_unterminated_value_is_still_graded(self):
        # A value with no closing quote on its line matches nothing in the
        # extractor; the fallback reads the rest of the line so the hit is
        # graded rather than dropped, which is the half-pasted shape.
        proc = run_guard(
            [
                f"""
                ```json
                {{ "id": "{KNOWN_ID}", "prompt": "Made up and long enough to clear the floor
                ```
                """
            ]
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("doc0.md", listed_under(proc.stdout, self.STALE))

    def test_a_file_grep_cannot_read_is_a_check_that_did_not_run(self):
        # Folded into an empty result by `2>/dev/null || true`, an unreadable
        # file was a check that passed. It is reported by name, and the copy
        # floors -- which would otherwise each announce that their cap is
        # undocumented -- stay silent, because the search did not run.
        missing = str(REPO_ROOT / "docs" / "this-file-does-not-exist.md")
        proc = run_guard([], extra_paths=(missing,))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(self.UNRUN, proc.stdout)
        self.assertIn("this-file-does-not-exist.md", proc.stdout)
        self.assertNotIn("No document states this cap", proc.stdout)


class GuardWiringTest(unittest.TestCase):
    """The shell script and the awk program have to stay attached."""

    def test_the_guard_invokes_the_scan_file(self):
        source = GUARD.read_text(encoding="utf-8")
        self.assertIn("scan-cron-prompts.awk", source)
        # The awk invocation, not the `[ ! -f "$SCAN_AWK" ]` existence check
        # above it, which the bare `-f "$SCAN_AWK"` used to be satisfied by.
        self.assertRegex(source, r'xargs -0 awk -v idfile="\$ROSTER_ID_FILE" -f "\$SCAN_AWK"')

    def test_unrunnable_checks_are_reported_before_the_scan_can_exit(self):
        # The scan exits 1 outright on an unreadable roster or a failed awk. The
        # "checks that could not run" report has to be printed before the first
        # of those exits -- the roster read -- or an early exit takes it with it
        # and the run blames the roster for a failure a broken grep pattern had
        # already caused.
        source = GUARD.read_text(encoding="utf-8")
        report = source.index("A terminology check could not run")
        roster_read = source.index("jq -r '(.jobs // .)[].id'")
        self.assertLess(report, roster_read)

    def test_the_guard_runs_from_a_directory_that_is_not_the_repository_root(self):
        # Every other test in this class reads the source; this one executes it,
        # because the defect it covers is invisible to a source read. The script
        # cd's to the repository root at the top and then resolved the awk
        # program against `$0`, which is still relative to the *caller's*
        # directory -- so `cd hack && ./check-docs-terminology.sh` exited 1 on
        # "not found" before checking a single prompt, while CI, which runs it
        # from the root, stayed green.
        # Invoked by a *relative* path, which is how a person types it and the
        # only spelling that reproduces the break: `dirname` on an absolute
        # `$0` lands on the right directory from any cwd, so a test that runs
        # the script by its full path passes either way.
        for cwd in (REPO_ROOT / "hack", REPO_ROOT / "docs"):
            with self.subTest(cwd=cwd.name):
                proc = subprocess.run(
                    ["./" + os.path.relpath(GUARD, cwd)],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotIn("cannot run", proc.stdout + proc.stderr)
                self.assertNotIn("could not run", proc.stdout + proc.stderr)
                # Not the exit status: that would make this suite a second
                # reporter of every terminology failure. The defect this covers
                # shows as the awk step never running, which "cannot run" names.
                self.assertIn("Checking terminology", proc.stdout)

    def test_the_scan_refuses_to_grade_a_document_without_a_roster(self):
        # Without `-v idfile`, BSD awk and gawk abort, but mawk reads the empty
        # string as a missing file and calls every rendered entry an orphan --
        # a page of failures that look like findings. Both are wrong answers to
        # "the caller forgot the roster", so the program says so itself.
        document = f'```json\n{{ "id": "{KNOWN_ID}", "prompt": "a" }}\n```\n'
        for awk in [a for a in ("awk", "mawk", "gawk") if shutil.which(a)]:
            with self.subTest(awk=awk):
                proc = subprocess.run(
                    [awk, "-f", str(SCAN_AWK)],
                    input=textwrap.dedent(document),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn("idfile", proc.stderr)

    def test_no_floor_fires_on_a_search_that_did_not_run(self):
        # A "no document states this any more" error is a wrong diagnosis when
        # the search itself failed: `search` returns 2 and prints nothing, and
        # every floor below then announces its own cap is undocumented. Both
        # floors gate on the search having run.
        source = GUARD.read_text(encoding="utf-8")
        for guarded in ('if [ "$probe_ok" -eq 0 ]', 'if [ "$ID_SEARCH_OK" -eq 0 ]'):
            self.assertIn(guarded, source)
        floors = re.findall(r"^.*-lt 1 \]; then$", source, re.MULTILINE)
        self.assertEqual(len(floors), 2, f"unguarded floor added? {floors}")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for scripts/release/poll_rc_eval_verdict.py.

What the poller decides is whether a candidate reaches staging, so the cases
that matter are the ones where it could answer with a verdict that is not the
candidate's: a build at a different commit, a stale build superseded by a
re-run, a half-uploaded artifact, and a read that failed rather than a file that
is absent. The deadlines are exercised with injected clocks rather than by
waiting.

The three-way verdict is the other axis. A build that failed because its lane
broke must not read as a rejected release, because the two have opposite
consequences for the candidate: one is final, the other is retried.
"""

import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "poll_rc_eval_verdict.py"

_spec = importlib.util.spec_from_file_location("poll_rc_eval_verdict", _SCRIPT)
poller = importlib.util.module_from_spec(_spec)
sys.modules["poll_rc_eval_verdict"] = poller
_spec.loader.exec_module(poller)

# The commit from scripts/eval_dashboard/testdata_rc/2097891568546484224 -- a
# real build of this job, so the artifact shapes here are the deployment's.
COMMIT = "5b5ad10163cf10c73871b279518c7165c098bec9"
OTHER_COMMIT = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
PREFIX = "gs://kube-agents-prow/logs/post-kube-agents-eval-rc/"
SUMMARY_PATH = poller.ARTIFACTS_DIR + poller.RC_SUMMARY_FILE

# The `timestamp` from that same real started.json, and a nomination pushed a
# day later. Two attempts at ONE commit is the shape the `--not-before` floor
# exists for, so the tests below need both a "last night's build" stamp and a
# "tonight's nomination" one.
STAMP = 1789011301
A_DAY = 24 * 60 * 60
TONIGHT = STAMP + A_DAY


def started(commit, tag="evalcand_2609092307_5b5ad10", timestamp=STAMP):
    """started.json as Prow's decoration writes it for a tag-push postsubmit."""
    return json.dumps(
        {
            "timestamp": timestamp,
            "repos": {"gke-labs/kube-agents": f"{tag}:{commit}"},
            "repo-commit": commit,
            "repo-version": commit,
        }
    )


def finished(passed, commit=COMMIT):
    return json.dumps(
        {
            "timestamp": 1789026670,
            "passed": passed,
            "result": "SUCCESS" if passed else "FAILURE",
            "revision": commit,
        }
    )


def summary(verdict, tag="evalcand_2609092307_5b5ad10"):
    """rc-eval-summary.md in the shape hack/ci-eval-rc.sh writes it.

    Kept structurally faithful -- the surrounding prose included -- because the
    prose is what a naive substring search for the verdict word would match.
    """
    return (
        f"# Release candidate eval — {tag}\n"
        "\n"
        "| | |\n"
        "| --- | --- |\n"
        f"| Candidate | `{tag}` |\n"
        f"| Commit | `{COMMIT}` |\n"
        "| Tier | `rc` |\n"
        f"| Verdict | {verdict} |\n"
        "\n"
        "Per-case detail is in `eval-verdict.md` alongside this file.\n"
    )


def build(
    commit=COMMIT,
    verdict=poller.SUMMARY_GREEN,
    passed=None,
    running=False,
    timestamp=STAMP,
    unstarted=False,
):
    """One build directory's files.

    `passed` defaults to agreeing with the summary verdict, which is the normal
    case; the tests that care about disagreement set it explicitly.

    `unstarted` leaves started.json out entirely: the minute or two between Prow
    creating a build directory and its decoration uploading the artifact.
    """
    if unstarted:
        return {}
    files = {"started.json": started(commit, timestamp=timestamp)}
    if running:
        return files
    if passed is None:
        passed = verdict == poller.SUMMARY_GREEN
    files["finished.json"] = finished(passed, commit)
    if verdict is not None:
        files[SUMMARY_PATH] = summary(verdict)
    return files


def archive(builds, unreadable=()):
    """A (read_listing, read_file) pair over an in-memory archive.

    `builds` maps build id to a dict of file name -> text; a file left out is
    absent from the bucket, which is how an unfinished build is spelled and how
    a run that never reached its reporting step is spelled.

    `unreadable` is a set of `<build>/<name>` paths whose read fails rather than
    returning nothing -- a 403, a 429, an expired credential. The distinction is
    the point of several tests below. Note that it fails EVERY read of the path,
    which is why `read_file.reads` exists: a path read twice has a second chance
    to fail transiently, and a fixture that fails a path outright cannot express
    that. Counting the reads pins the absence of the second one instead.
    """
    listing = "".join(f"{PREFIX}{build_id}/\n" for build_id in sorted(builds))

    def read_file(url):
        rest = url[len(PREFIX):]
        build_id, _, name = rest.partition("/")
        read_file.reads.append(f"{build_id}/{name}")
        if f"{build_id}/{name}" in unreadable:
            return poller.UNREADABLE
        return builds.get(build_id, {}).get(name)

    read_file.reads = []
    return (lambda: listing), read_file


class ScanTest(unittest.TestCase):
    def test_a_finished_green_build_at_the_commit_is_green(self):
        read_listing, read_file = archive({"100": build()})
        state, build_id, base = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertEqual(state, poller.VERDICT_GREEN)
        self.assertEqual(build_id, "100")
        self.assertEqual(base, f"{PREFIX}100/")

    def test_a_finished_red_build_at_the_commit_is_red(self):
        read_listing, read_file = archive({"100": build(verdict=poller.SUMMARY_RED)})
        state, _, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertEqual(state, poller.VERDICT_RED)

    def test_a_green_build_at_another_commit_does_not_answer_for_ours(self):
        """The failure this exists to stop: promoting on a neighbour's verdict."""
        read_listing, read_file = archive({"100": build(commit=OTHER_COMMIT)})
        state, build_id, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertEqual(state, poller.VERDICT_NEVER_RAN)
        self.assertIsNone(build_id)

    def test_a_build_with_no_finished_json_is_still_running(self):
        read_listing, read_file = archive({"100": build(running=True)})
        state, build_id, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertIsNone(state)
        self.assertEqual(build_id, "100")

    def test_a_newer_rerun_supersedes_the_finished_build_it_replaces(self):
        """Re-running a red build from Deck must not keep reporting the red.

        Newest first is what makes this work, so the assertion is that the
        in-flight 200 wins over the finished 100 rather than the reverse.
        """
        read_listing, read_file = archive(
            {"100": build(verdict=poller.SUMMARY_RED), "200": build(running=True)}
        )
        state, build_id, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertIsNone(state)
        self.assertEqual(build_id, "200")

    def test_a_finished_rerun_answers_and_not_the_build_it_replaced(self):
        read_listing, read_file = archive(
            {"100": build(verdict=poller.SUMMARY_RED), "200": build()}
        )
        state, build_id, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertEqual(state, poller.VERDICT_GREEN)
        self.assertEqual(build_id, "200")

    def test_an_unreadable_listing_is_not_an_empty_archive(self):
        """A GCS blip must read as "ask again", not as "the eval never ran"."""
        for failure in (poller.UNREADABLE, None):
            with self.subTest(failure=failure):
                state, build_id, _ = poller.scan_once(
                    COMMIT, lambda: failure, lambda url: None
                )
                self.assertIsNone(state)
                self.assertIsNone(build_id)

    def test_an_unreadable_started_json_does_not_hand_the_answer_to_an_older_build(self):
        """The substitution polling GCS was chosen to prevent, via a failed read.

        A 429 on the newest build's started.json must not read as "this build is
        not ours" -- walking past it lets the build the operator re-ran to
        replace answer in its place.
        """
        read_listing, read_file = archive(
            {"100": build(), "200": build(running=True)},
            unreadable={"200/started.json"},
        )
        state, build_id, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertIsNone(state)
        self.assertIsNone(build_id)

    def test_an_unreadable_finished_json_reads_as_still_running(self):
        read_listing, read_file = archive(
            {"100": build()}, unreadable={"100/finished.json"}
        )
        state, build_id, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertIsNone(state)
        self.assertEqual(build_id, "100")

    def test_an_unreadable_summary_artifact_reads_as_still_running(self):
        """Ask again rather than call a finished build unmeasured."""
        read_listing, read_file = archive({"100": build()}, unreadable={f"100/{SUMMARY_PATH}"})
        state, build_id, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertIsNone(state)
        self.assertEqual(build_id, "100")

    def test_the_scan_limit_bounds_the_reads(self):
        builds = {str(build_id): build(commit=OTHER_COMMIT) for build_id in range(100, 130)}
        builds["100"] = build()
        read_listing, read_file = archive(builds)
        reads = []

        def counting_read(url):
            reads.append(url)
            return read_file(url)

        state, _, _ = poller.scan_once(COMMIT, read_listing, counting_read, scan_limit=5)
        # 100 is the OLDEST of the thirty, so a five-deep scan does not reach it.
        self.assertEqual(state, poller.VERDICT_NEVER_RAN)
        self.assertLessEqual(len(reads), 5 * 3)

    def test_the_scan_limit_note_is_one_message_every_sweep_repeats(self):
        """`scan_once` says it each sweep; `poll` is what says it once.

        Asserted here as the pair it is, because the de-duplication lives in the
        caller: a sweep that stopped emitting the note would make the `poll`
        test below pass for the wrong reason.
        """
        builds = {str(build_id): build(commit=OTHER_COMMIT) for build_id in range(100, 130)}
        read_listing, read_file = archive(builds)
        said = []
        for _ in range(5):
            poller.scan_once(COMMIT, read_listing, read_file, scan_limit=5, note=said.append)
        self.assertEqual(len(said), 5)
        self.assertEqual(len(set(said)), 1)

    def test_the_newest_directory_without_a_started_json_is_waited_for(self):
        """Prow creates the directory before decoration uploads started.json.

        Absent at the top of the listing is "not yet", not "not ours" -- walking
        past it lets an older build at the same commit answer, which for a
        re-run from Deck is the verdict the operator re-ran to replace.
        """
        read_listing, read_file = archive(
            {"100": build(verdict=poller.SUMMARY_RED), "200": build(unstarted=True)}
        )
        state, build_id, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertIsNone(state)
        self.assertIsNone(build_id)

    def test_an_older_directory_without_a_started_json_is_skipped(self):
        """Deeper in the listing the same absence is a reaped artifact.

        The unstarted directory has to sit BELOW the head of the listing for
        this to test anything: at index 0 the case above applies instead, and
        the sweep returns before reaching it. So 300 is a neighbour's build the
        sweep walks past, 200 is the reaped artifact under test, and ours is the
        100 beneath it -- reached only if 200 was skipped rather than waited on.
        """
        read_listing, read_file = archive(
            {
                "100": build(),
                "200": build(unstarted=True),
                "300": build(commit=OTHER_COMMIT),
            }
        )
        state, build_id, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertEqual(state, poller.VERDICT_GREEN)
        self.assertEqual(build_id, "100")
        self.assertIn("200/started.json", read_file.reads)

    def test_the_verdict_reads_each_artifact_of_a_build_once(self):
        """finished.json in particular, which used to be read twice.

        The second read was inside read_verdict, after the sweep had already
        read the same object to decide the build was over. A transient failure
        of that second read returns UNREADABLE, finished_passed(UNREADABLE) is
        None rather than False, and the refusal below -- a GREEN summary on a
        build Prow failed -- is skipped: exit 0, settled, promoted.

        Asserted as a read count because the failure cannot be reached any other
        way from here. `archive`'s `unreadable` set fails every read of a path,
        so the sweep's own read fails first and the branch is never entered; a
        fixture that fails only the second read would be testing itself.
        """
        read_listing, read_file = archive({"100": build()})
        state, _, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertEqual(state, poller.VERDICT_GREEN)
        self.assertEqual(read_file.reads.count("100/finished.json"), 1)

    def test_a_green_summary_on_a_failed_build_is_refused_on_the_sweeps_own_read(self):
        """The refusal has to survive read_verdict not reading the file itself.

        Same case as test_a_green_summary_on_a_failed_build_promotes_nothing,
        entered through scan_once so the `passed` it acts on is the text the
        sweep read rather than one of its own.
        """
        read_listing, read_file = archive({"100": build(passed=False)})
        with contextlib.redirect_stderr(io.StringIO()):
            state, _, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertEqual(state, poller.VERDICT_NOT_RUN)


class NotBeforeTest(unittest.TestCase):
    """The floor that makes a re-nomination a retry rather than an echo.

    A withdrawn nomination is re-pushed under the same tag at the same commit,
    so every attempt's build matches on commit alone. Without the floor the
    newest MATCHING build during the minute or two before Prow uploads tonight's
    started.json is last night's, already finished -- and the poll answers in
    seconds with a verdict about a run nobody is waiting on.
    """

    def test_a_build_from_a_previous_attempt_does_not_answer(self):
        read_listing, read_file = archive({"100": build(verdict=poller.SUMMARY_RED)})
        state, build_id, _ = poller.scan_once(
            COMMIT, read_listing, read_file, not_before=TONIGHT
        )
        self.assertEqual(state, poller.VERDICT_NEVER_RAN)
        self.assertIsNone(build_id)

    def test_tonights_build_answers_and_last_nights_does_not(self):
        """Both are at the commit; only the stamp separates them."""
        read_listing, read_file = archive(
            {
                "100": build(verdict=poller.SUMMARY_RED),
                "200": build(verdict=poller.SUMMARY_GREEN, timestamp=TONIGHT),
            }
        )
        state, build_id, _ = poller.scan_once(
            COMMIT, read_listing, read_file, not_before=TONIGHT
        )
        self.assertEqual(state, poller.VERDICT_GREEN)
        self.assertEqual(build_id, "200")

    def test_without_a_floor_last_nights_build_does_answer(self):
        """The bug, pinned. This is what the caller must never do on a retry."""
        read_listing, read_file = archive({"100": build(verdict=poller.SUMMARY_RED)})
        state, _, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertEqual(state, poller.VERDICT_RED)

    def test_the_skew_allowance_covers_two_clocks_disagreeing(self):
        """The runner stamps the nomination; the Prow node stamps the build."""
        inside = poller.NOMINATION_SKEW_SECONDS - 1
        outside = poller.NOMINATION_SKEW_SECONDS + 1
        self.assertTrue(poller.started_after(started(COMMIT), STAMP + inside))
        self.assertFalse(poller.started_after(started(COMMIT), STAMP + outside))

    def test_no_floor_accepts_everything_so_a_hand_run_reads_as_it_looks(self):
        self.assertTrue(poller.started_after(started(COMMIT), None))
        self.assertTrue(poller.started_after(None, None))
        self.assertTrue(poller.started_after("not json", None))

    def test_an_unusable_timestamp_is_not_ours(self):
        """Fail-safe: one nomination re-made, versus answering from a stale run.

        `True` is in the list because a JSON boolean is an `int` in Python, so a
        `timestamp: true` would otherwise compare as 1 and read as ancient --
        which happens to be the safe direction here, but only by accident.
        """
        for value in (None, "", "{", "[]", json.dumps({}), json.dumps({"timestamp": "soon"})):
            with self.subTest(value=value):
                self.assertFalse(poller.started_after(value, TONIGHT))
        self.assertFalse(poller.started_after(json.dumps({"timestamp": True}), TONIGHT))

    def test_poll_threads_the_floor_through_to_the_sweep(self):
        """The wiring, not the comparison: a floor `poll` drops is no floor."""
        clock = FakeClock()
        read_listing, read_file = archive({"100": build(verdict=poller.SUMMARY_RED)})
        verdict, _, _ = poller.poll(
            COMMIT,
            read_listing,
            read_file,
            now=clock.now,
            sleep=clock.sleep,
            appear_deadline_minutes=45,
            deadline_minutes=330,
            interval_seconds=600,
            not_before=TONIGHT,
        )
        self.assertEqual(verdict, poller.VERDICT_NEVER_RAN)

    def test_the_skipped_build_is_noted_once_not_every_sweep(self):
        """160 sweeps of "ignoring last night's build" is not a log anyone reads."""
        clock = FakeClock()
        read_listing, read_file = archive({"100": build(verdict=poller.SUMMARY_RED)})
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            poller.poll(
                COMMIT,
                read_listing,
                read_file,
                now=clock.now,
                sleep=clock.sleep,
                appear_deadline_minutes=45,
                deadline_minutes=330,
                interval_seconds=600,
                not_before=TONIGHT,
            )
        earlier = [line for line in captured.getvalue().splitlines() if "earlier attempt" in line]
        self.assertEqual(len(earlier), 1)


class VerdictTest(unittest.TestCase):
    """The three-way read: the distinction the build's exit status throws away."""

    def test_a_build_that_failed_before_measuring_is_not_a_red_release(self):
        """A failed deploy, a project that never leased: `NOT RUN`, not RED.

        Its consequence is the opposite of a red one's -- the candidate is
        retried rather than rejected -- so collapsing the two is what this
        exists to stop.
        """
        read_listing, read_file = archive(
            {"100": build(verdict=poller.SUMMARY_NOT_RUN, passed=False)}
        )
        state, _, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertEqual(state, poller.VERDICT_NOT_RUN)

    def test_a_finished_build_with_no_summary_artifact_is_not_run(self):
        """Every dormancy gate in ci-eval-rc.sh exits 0 having measured nothing.

        `passed: true` with no artifact is exactly that case, and believing the
        status would promote a candidate no eval ever looked at.
        """
        read_listing, read_file = archive({"100": build(verdict=None, passed=True)})
        state, _, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertEqual(state, poller.VERDICT_NOT_RUN)

    def test_a_green_summary_on_a_failed_build_promotes_nothing(self):
        """The direction that refuses. A promotion this cannot justify becomes
        NOT RUN: neither promoted nor rejected, and measured again later."""
        read_listing, read_file = archive(
            {"100": build(verdict=poller.SUMMARY_GREEN, passed=False)}
        )
        state, _, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertEqual(state, poller.VERDICT_NOT_RUN)

    def test_a_red_summary_on_a_passing_build_is_still_red(self):
        """The asymmetry, which is the non-obvious half.

        Reading this as NOT RUN would withdraw the nomination of a candidate the
        eval REJECTED, putting it back in the pool to be re-measured for hours
        every night to reach the same answer. It also made the verdict depend on
        a `|| true` in another repository's job config: under that line `passed`
        is true on every build, red ones included, so the symmetric rule turned
        every red into that loop exactly when the lane stopped being advisory.
        """
        read_listing, read_file = archive(
            {"100": build(verdict=poller.SUMMARY_RED, passed=True)}
        )
        state, _, _ = poller.scan_once(COMMIT, read_listing, read_file)
        self.assertEqual(state, poller.VERDICT_RED)

    def test_the_verdict_row_is_read_and_not_the_prose_around_it(self):
        text = summary(poller.SUMMARY_RED).replace(
            "Per-case detail", "A GREEN verdict would have been promoted. Per-case detail"
        )
        self.assertEqual(poller.summary_verdict(text), poller.SUMMARY_RED)

    def test_an_unparseable_or_absent_summary_is_none(self):
        for text in (None, "", "no table here", "| Verdict | MAYBE |", poller.UNREADABLE):
            with self.subTest(text=text):
                self.assertIsNone(poller.summary_verdict(text))

    def test_the_three_words_the_driver_writes_all_parse(self):
        for word in (poller.SUMMARY_GREEN, poller.SUMMARY_RED, poller.SUMMARY_NOT_RUN):
            with self.subTest(word=word):
                self.assertEqual(poller.summary_verdict(summary(word)), word)

    def test_only_a_settled_verdict_is_marked_settled(self):
        """The retry policy, as a table: a broken lane said nothing to be final about."""
        self.assertTrue(poller._SETTLED[poller.VERDICT_GREEN])
        self.assertTrue(poller._SETTLED[poller.VERDICT_RED])
        for verdict in (
            poller.VERDICT_NOT_RUN,
            poller.VERDICT_TIMEOUT,
            poller.VERDICT_NEVER_RAN,
        ):
            with self.subTest(verdict=verdict):
                self.assertFalse(poller._SETTLED[verdict])


class ArtifactParsingTest(unittest.TestCase):
    def test_short_and_long_shas_match_each_other(self):
        self.assertTrue(poller.started_names_commit(started(COMMIT), COMMIT))
        self.assertTrue(poller.started_names_commit(started(COMMIT[:7]), COMMIT))
        self.assertTrue(poller.started_names_commit(started(COMMIT), COMMIT[:7]))

    def test_the_repos_value_is_split_off_its_ref(self):
        text = json.dumps({"repos": {"gke-labs/kube-agents": f"evalcand_2609092307_5b5ad10:{COMMIT}"}})
        self.assertTrue(poller.started_names_commit(text, COMMIT))

    def test_a_repository_name_is_not_a_commit(self):
        """The reason the named fields are read rather than the raw JSON."""
        text = json.dumps({"repos": {COMMIT: "refs/heads/main"}})
        self.assertFalse(poller.started_names_commit(text, COMMIT))

    def test_padding_does_not_satisfy_the_minimum_sha_length(self):
        """The floor has to bind the value that is compared, not the raw one."""
        text = json.dumps({"repo-commit": f"  {COMMIT[:3]}    "})
        self.assertFalse(poller.started_names_commit(text, COMMIT))

    def test_truncated_or_missing_artifacts_read_as_still_running(self):
        for text in (None, "", "{", "[]", "{}", json.dumps({"result": 7})):
            with self.subTest(text=text):
                self.assertIsNone(poller.finished_passed(text))

    def test_result_answers_when_passed_is_absent(self):
        self.assertTrue(poller.finished_passed(json.dumps({"result": "SUCCESS"})))
        self.assertFalse(poller.finished_passed(json.dumps({"result": "FAILURE"})))

    def test_passed_outranks_result(self):
        self.assertFalse(poller.finished_passed(json.dumps({"passed": False, "result": "SUCCESS"})))

    def test_build_dirs_reads_both_listing_forms_newest_first(self):
        plain = f"{PREFIX}100/\n{PREFIX}200/\n{PREFIX}latest-build.txt\n"
        globbed = f"{PREFIX}100/:\n{PREFIX}100/started.json\n\n{PREFIX}200/:\n{PREFIX}200/started.json\n"
        for listing in (plain, globbed):
            with self.subTest(listing=listing):
                self.assertEqual(
                    poller.build_dirs(listing),
                    [("200", f"{PREFIX}200/"), ("100", f"{PREFIX}100/")],
                )

    def test_a_directory_name_that_is_not_an_integer_is_skipped_not_fatal(self):
        """`str.isdigit` accepts characters `int` refuses, and a crash here would
        have been reported as a failed release."""
        listing = f"{PREFIX}²/\n{PREFIX}100/\n"
        self.assertEqual(poller.build_dirs(listing), [("100", f"{PREFIX}100/")])

    def test_spyglass_url(self):
        self.assertEqual(
            poller.spyglass_url(f"{PREFIX}100/"),
            "https://oss.gprow.dev/view/gs/kube-agents-prow/logs/post-kube-agents-eval-rc/100",
        )
        self.assertIsNone(poller.spyglass_url(None))
        self.assertIsNone(poller.spyglass_url("/local/path"))

    def test_the_default_prefix_is_the_bucket_the_dashboard_reads(self):
        """A wrong bucket is indistinguishable from a job that never fired.

        Both report NEVER_RAN, and the summary for that outcome names the tag
        shape and the `branches` regex -- so a typo here sends every morning
        after it to the wrong repository.

        Pinned against the archive scripts/eval_dashboard/collect.py documents
        for the same lane, in its --rc-glob help, which is the closest thing to
        a structural contract there is on that side: collect.py takes its glob
        as an argument and holds no bucket constant to compare against, so a
        weaker assertion here -- these words appear somewhere in that file --
        would be matched by its prose and pin nothing.
        """
        collect = (_REPO_ROOT / "scripts" / "eval_dashboard" / "collect.py").read_text()
        self.assertEqual(
            poller.DEFAULT_LOGS_PREFIX,
            "gs://kube-agents-prow/logs/post-kube-agents-eval-rc/",
        )
        self.assertTrue(poller.DEFAULT_LOGS_PREFIX.endswith("/"))
        self.assertIn(poller.DEFAULT_LOGS_PREFIX + "*", collect)


class GsutilResultTest(unittest.TestCase):
    """Absence versus a failed read, which is the distinction `scan_once` rests on."""

    class _Proc:
        def __init__(self, returncode, stderr):
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = ""

    def _run(self, returncode, stderr):
        class FakeSubprocess:
            OSError = OSError
            TimeoutExpired = subprocess.TimeoutExpired

            @staticmethod
            def run(*args, **kwargs):
                return GsutilResultTest._Proc(returncode, stderr)

        real = poller.subprocess
        poller.subprocess = FakeSubprocess
        self.addCleanup(setattr, poller, "subprocess", real)
        with contextlib.redirect_stderr(io.StringIO()):
            return poller._gsutil(["cat", f"{PREFIX}100/started.json"])

    def test_gsutils_absence_phrasings_read_as_absent(self):
        for stderr in (
            "CommandException: One or more URLs matched no objects.",
            "CommandException: No URLs matched: gs://x/y",
            "NotFoundException: 404 gs://x/y does not exist.",
        ):
            with self.subTest(stderr=stderr):
                self.assertIsNone(self._run(1, stderr))

    def test_a_build_id_containing_404_is_not_an_absence(self):
        """Prow build ids are 19 digits, so one contains "404" often enough.

        A bare substring match on the number would call a 503 on
        `.../2097891568546404123/started.json` an absence -- and an absent
        started.json at the top of the listing lets an older build answer.
        """
        stderr = (
            "ServiceException: 503 Backend Error on"
            " gs://kube-agents-prow/logs/post-kube-agents-eval-rc/2097891568546404123/started.json"
        )
        self.assertIs(self._run(1, stderr), poller.UNREADABLE)

    def test_a_403_is_unreadable_and_not_an_absence(self):
        self.assertIs(self._run(1, "AccessDeniedException: 403 Caller lacks permission"), poller.UNREADABLE)


class FakeClock:
    """A monotonic clock that only advances when something sleeps on it."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class PollTest(unittest.TestCase):
    def _poll(self, builds_over_time, **kwargs):
        """Polls an archive that changes between sweeps.

        `builds_over_time` is a list of archive states; each sweep consumes the
        next one and the last is repeated forever.
        """
        clock = FakeClock()
        sweeps = {"n": 0}

        def read_listing():
            state = builds_over_time[min(sweeps["n"], len(builds_over_time) - 1)]
            sweeps["n"] += 1
            self._current = state
            return "".join(f"{PREFIX}{b}/\n" for b in sorted(state))

        def read_file(url):
            rest = url[len(PREFIX):]
            build_id, _, name = rest.partition("/")
            return self._current.get(build_id, {}).get(name)

        result = poller.poll(
            COMMIT, read_listing, read_file, now=clock.now, sleep=clock.sleep, **kwargs
        )
        return result, clock

    def test_it_waits_for_a_running_build_and_returns_its_verdict(self):
        running = {"100": build(running=True)}
        done = {"100": build()}
        (verdict, build_id, _), clock = self._poll([running, running, done])
        self.assertEqual(verdict, poller.VERDICT_GREEN)
        self.assertEqual(build_id, "100")
        self.assertEqual(clock.t, 2 * poller.DEFAULT_INTERVAL_SECONDS)

    def test_a_broken_lane_ends_the_poll_rather_than_waiting_out_the_deadline(self):
        done = {"100": build(verdict=poller.SUMMARY_NOT_RUN, passed=False)}
        (verdict, build_id, _), clock = self._poll([done], interval_seconds=600)
        self.assertEqual(verdict, poller.VERDICT_NOT_RUN)
        self.assertEqual(build_id, "100")
        self.assertEqual(clock.t, 0)

    def test_a_build_that_never_appears_gives_up_on_the_short_clock(self):
        """Not at the long one: nothing appearing is a broken lane, not a slow eval."""
        (verdict, build_id, _), clock = self._poll(
            [{}], appear_deadline_minutes=45, deadline_minutes=330, interval_seconds=600
        )
        self.assertEqual(verdict, poller.VERDICT_NEVER_RAN)
        self.assertIsNone(build_id)
        # Within one poll of the short clock, and nowhere near the long one.
        self.assertLessEqual(clock.t, 45 * 60 + 600)

    def test_a_build_that_appears_is_given_the_long_clock(self):
        running = {"100": build(running=True)}
        (verdict, build_id, _), clock = self._poll(
            [running], appear_deadline_minutes=45, deadline_minutes=330, interval_seconds=1800
        )
        self.assertEqual(verdict, poller.VERDICT_TIMEOUT)
        self.assertEqual(build_id, "100")
        self.assertGreater(clock.t, 45 * 60)

    def test_a_listing_outage_outlasting_the_short_clock_reports_never_ran(self):
        """The appearance clock runs whether or not the bucket is readable.

        Named for what it asserts, since the opposite is the reading someone
        would expect: an outage does NOT hold the short clock open. Recorded
        rather than argued with, because the poller cannot tell a bucket it
        cannot read from a job that never fired. Both refuse to promote, both
        are unsettled so the candidate is retried, and the summary sends the
        reader to the build log, which carries a warning line per failed sweep
        in the first case and none in the second.
        """
        clock = FakeClock()
        verdict, _, _ = poller.poll(
            COMMIT,
            lambda: poller.UNREADABLE,
            lambda url: poller.UNREADABLE,
            now=clock.now,
            sleep=clock.sleep,
            appear_deadline_minutes=45,
            deadline_minutes=330,
            interval_seconds=600,
        )
        self.assertEqual(verdict, poller.VERDICT_NEVER_RAN)
        self.assertFalse(poller._SETTLED[verdict])


class ExitCodeTest(unittest.TestCase):
    _VERDICTS = (
        poller.VERDICT_GREEN,
        poller.VERDICT_RED,
        poller.VERDICT_NOT_RUN,
        poller.VERDICT_TIMEOUT,
        poller.VERDICT_NEVER_RAN,
    )

    def test_only_green_exits_zero(self):
        self.assertEqual(poller._EXIT[poller.VERDICT_GREEN], 0)
        for verdict in self._VERDICTS[1:]:
            with self.subTest(verdict=verdict):
                self.assertNotEqual(poller._EXIT[verdict], 0)

    def test_no_verdict_collides_with_pythons_own_failure_codes(self):
        """1 is an uncaught exception and 2 is argparse. A poller that crashed
        must not be readable as a release that failed."""
        for verdict in self._VERDICTS:
            with self.subTest(verdict=verdict):
                self.assertNotIn(poller._EXIT[verdict], (1, 2))
        self.assertNotIn(poller.EXIT_INTERNAL_ERROR, {poller._EXIT[v] for v in self._VERDICTS})

    def test_every_verdict_has_a_distinct_code_a_summary_and_a_retry_policy(self):
        self.assertEqual(len({poller._EXIT[v] for v in self._VERDICTS}), len(self._VERDICTS))
        for verdict in self._VERDICTS:
            with self.subTest(verdict=verdict):
                self.assertTrue(poller._SUMMARY[verdict].strip())
                self.assertIn(verdict, poller._SETTLED)

    def test_no_summary_points_at_a_recovery_that_does_nothing(self):
        """Re-running from Deck and re-dispatching the pipeline both no-op: the
        poll has returned by the time a re-run finishes, and a commit carrying
        an evalcand_ tag is skipped on every later run."""
        for verdict in self._VERDICTS:
            with self.subTest(verdict=verdict):
                text = poller._SUMMARY[verdict].lower()
                self.assertNotIn("re-run", text)
                self.assertNotIn("re-dispatch", text)


def last_values(written):
    """The outputs as GitHub Actions would read them.

    `$GITHUB_OUTPUT` is appended to and the LAST line for a key wins, which is
    what lets main() seed refusing defaults before it can fail and still report
    the real verdict afterwards. Reading it any other way would hide whether the
    seed is being overwritten.
    """
    values = {}
    for line in written.splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    return values


class MainTest(unittest.TestCase):
    # main() takes its deadlines from the command line, so these tests run
    # through the real poll loop on the real clock. Every case below resolves
    # on the first sweep and never sleeps -- but a fixture that stopped doing
    # so would not fail, it would sit for 45 minutes and then fail, inside a
    # suite whose other 60-odd tests finish in a tenth of a second. Collapsing
    # the clocks makes that a fast red instead of a hung job.
    # Zero is safe for all three: poll() returns a verdict before it consults
    # either deadline, so a fixture that resolves on the first sweep is
    # unaffected and one that does not answers on the spot.
    _FAST_CLOCK = (
        "--interval-seconds",
        "0",
        "--appear-deadline-minutes",
        "0",
        "--deadline-minutes",
        "0",
    )

    def _run_main(self, builds, argv, expect_exit=False):
        import os
        import tempfile

        # Prepended, not appended: argparse takes the last occurrence, so a case
        # that sets its own deadlines -- the two below that test the guard
        # between them -- still overrides these.
        argv = list(self._FAST_CLOCK) + list(argv)

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        out = pathlib.Path(temp.name) / "out"
        summary_file = pathlib.Path(temp.name) / "summary"
        out.touch()
        summary_file.touch()

        def fake_gsutil(args, gsutil="gsutil"):
            if args[0] == "ls":
                return "".join(f"{PREFIX}{b}/\n" for b in sorted(builds))
            rest = args[1][len(PREFIX):]
            build_id, _, name = rest.partition("/")
            return builds.get(build_id, {}).get(name)

        real = poller._gsutil
        poller._gsutil = fake_gsutil
        prior = {k: os.environ.get(k) for k in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")}
        os.environ["GITHUB_OUTPUT"] = str(out)
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary_file)
        captured = io.StringIO()
        try:
            with contextlib.redirect_stderr(captured):
                if expect_exit:
                    with self.assertRaises(SystemExit) as caught:
                        poller.main(argv)
                    code = caught.exception.code
                else:
                    code = poller.main(argv)
        finally:
            self.stderr = captured.getvalue()
            poller._gsutil = real
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return code, out.read_text(), summary_file.read_text()

    def test_a_red_verdict_writes_the_outputs_and_summary_the_workflow_reads(self):
        code, written, rendered = self._run_main(
            {"100": build(verdict=poller.SUMMARY_RED)},
            ["--commit", COMMIT, "--evalcand-tag", "evalcand_2609092307_5b5ad10"],
        )
        self.assertEqual(code, poller.EXIT_RED)
        self.assertIn("verdict=red", written)
        self.assertIn("settled=true", written)
        self.assertIn("build_id=100", written)
        self.assertIn("log_url=https://oss.gprow.dev/view/gs/", written)
        self.assertIn("RED", rendered)

    def test_a_green_verdict_exits_zero_and_is_settled(self):
        code, written, rendered = self._run_main(
            {"100": build()}, ["--commit", COMMIT]
        )
        self.assertEqual(code, poller.EXIT_GREEN)
        self.assertIn("verdict=green", written)
        self.assertIn("settled=true", written)
        self.assertIn("GREEN", rendered)

    def test_a_broken_lane_is_unsettled_so_the_pipeline_can_retry_it(self):
        code, written, _ = self._run_main(
            {"100": build(verdict=poller.SUMMARY_NOT_RUN, passed=False)},
            ["--commit", COMMIT],
        )
        self.assertEqual(code, poller.EXIT_NOT_RUN)
        self.assertIn("verdict=not_run", written)
        self.assertIn("settled=false", written)

    def test_a_commit_too_short_to_match_safely_is_refused(self):
        """Stubbed rather than bare, so removing the guard fails the test instead
        of reaching the real bucket from whatever machine is running it."""
        code, _, _ = self._run_main({}, ["--commit", "5b5ad"], expect_exit=True)
        self.assertEqual(code, 2)

    def test_an_appearance_clock_that_could_never_fire_is_refused(self):
        """It would report TIMEOUT for a job that never started, and the two
        outcomes send an operator to opposite places: TIMEOUT's advice is that a
        slow-but-green build reaches it, NEVER_RAN's names the `branches` regex.

        The numbers are 1 and 0 rather than the realistic 400 and 330 so that
        removing the guard fails this in milliseconds. Without it the run falls
        straight through to the zero-minute deadline and returns TIMEOUT, which
        is the wrong answer this refuses to give.
        """
        code, _, _ = self._run_main(
            {},
            ["--commit", COMMIT, "--appear-deadline-minutes", "1", "--deadline-minutes", "0"],
            expect_exit=True,
        )
        self.assertEqual(code, 2)

    def test_a_refusal_before_the_poll_still_leaves_safe_outputs(self):
        """A caller under `continue-on-error` reads these whatever the exit code.

        An empty `verdict` is not "red": it sails through a `!= 'red'` guard and
        promotes. The seed is what makes every path the poller can take say
        "do not promote" by default.
        """
        _, written, _ = self._run_main({}, ["--commit", "5b5ad"], expect_exit=True)
        values = last_values(written)
        self.assertEqual(values["verdict"], poller.VERDICT_NOT_RUN)
        self.assertEqual(values["settled"], "false")
        self.assertEqual(values["build_id"], "")
        self.assertEqual(values["log_url"], "")

    def test_the_seeded_defaults_do_not_survive_a_real_verdict(self):
        """The other half: a seed that outlived the answer would refuse every green."""
        _, written, _ = self._run_main({"100": build()}, ["--commit", COMMIT])
        values = last_values(written)
        self.assertEqual(values["verdict"], poller.VERDICT_GREEN)
        self.assertEqual(values["settled"], "true")
        self.assertEqual(values["build_id"], "100")

    def test_not_before_reaches_the_sweep_from_the_command_line(self):
        """End to end: the flag the workflow passes, against last night's build."""
        builds = {"100": build(verdict=poller.SUMMARY_GREEN)}
        code, written, _ = self._run_main(
            builds,
            [
                "--commit",
                COMMIT,
                "--not-before",
                str(TONIGHT),
                "--appear-deadline-minutes",
                "0",
                "--deadline-minutes",
                "0",
                "--interval-seconds",
                "1",
            ],
        )
        self.assertEqual(code, poller.EXIT_NEVER_RAN)
        self.assertEqual(last_values(written)["verdict"], poller.VERDICT_NEVER_RAN)

    def test_omitting_not_before_is_warned_about(self):
        """It is a valid hand-run, and a silent footgun for the workflow."""
        self._run_main({"100": build()}, ["--commit", COMMIT])
        self.assertIn("--not-before", self.stderr)

    def test_passing_not_before_says_nothing(self):
        self._run_main({"100": build(timestamp=TONIGHT)}, ["--commit", COMMIT, "--not-before", str(TONIGHT)])
        self.assertNotIn("--not-before", self.stderr)


if __name__ == "__main__":
    unittest.main()

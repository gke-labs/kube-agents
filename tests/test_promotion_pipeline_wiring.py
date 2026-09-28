"""Invariants of the staging promotion pipeline that only the workflow YAML can carry.

Most of these are failures that would be silent in CI — a green run that did the
wrong thing — which is why they are pinned here rather than left to review:

  * a job pointed at `rc` instead of `nightly` tears down the RC environment,
  * a teardown that `needs` the nomination, the eval wait or the promotion is
    skipped when any of them fails, leaving a GKE cluster billing with nothing on
    it to diagnose — and, for the eval wait, held for the five and a half hours
    the poller is allowed even when everything succeeds,
  * a hardcoded `rc-environment` concurrency group makes an unrelated workflow
    contend for the release pipeline's cluster,
  * a staging tag shape the redeploy trigger does not match promotes nothing and
    still reports success,
  * a staging tag that does not `needs` the eval wait promotes a candidate whose
    eval is still running or already red, which is the whole gate gone,
  * a promotion carrying step 4's GitHub App token rather than minting its own
    pushes with a credential that expired hours into the wait,
  * a redeploy that deploys the pushed ref's SHA rather than the commit it peels
    to pulls an image tag nothing ever published,
  * an optional-suite order that runs `gchat` after `agent-plugin` sends the chat
    prompt at a gateway the 17-step plugin suite is still rolling.

Three of them are the gate itself, and they are pinned against the specific
edits that would turn it off while leaving every other test green — which is
what makes them worth their own tests rather than a line in review:

  * a status function anywhere in step 6's `if` (`always()`, `!cancelled()`,
    `success() || …`) restores the promotion on a red eval,
  * `continue-on-error: true` on the poller step makes step 5 succeed whatever
    the verdict was, and step 6's implicit success() then passes,
  * dropping the `settled` clause from step 5b's `if` withdraws the nomination on
    a red verdict too, which re-measures every rejected candidate nightly at
    hours of a shared project each time.
"""

import fnmatch
import importlib.util
import pathlib
import subprocess
import sys
import unittest

import yaml

from tests.testing.common import create_mock_git_repo, get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_PROMOTION = _WORKFLOWS / "staging-promotion-pipeline.yml"
_COMMON_SH = _REPO_ROOT / "scripts" / "release" / "common.sh"
_POLLER = _REPO_ROOT / "scripts" / "release" / "poll_rc_eval_verdict.py"


def _load_poller():
    """The poller itself, for its deadline.

    Loaded rather than restated: the job timeout below is asserted against the
    deadline the poller actually uses, and a copy of the number here would let
    the two drift apart in the one direction the assertion exists to catch —
    raising DEFAULT_DEADLINE_MINUTES past the job timeout, which kills the poller
    before it writes the verdict and reports the job as an unexplained failure.
    """
    spec = importlib.util.spec_from_file_location("poll_rc_eval_verdict", _POLLER)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("poll_rc_eval_verdict", module)
    spec.loader.exec_module(module)
    return module

_STAGING_DEPLOY = "staging-deploy.yml"

_ROLLBACK_LEG_JOB = "step-3b-rollback-leg"
_NOMINATE_JOB = "step-4-nominate-candidate"
_AWAIT_EVAL_JOB = "step-5-await-eval-verdict"
_WITHDRAW_JOB = "step-5b-withdraw-nomination"
_PROMOTE_JOB = "step-6-create-staging-tag"
_TEARDOWN_JOB = "step-7-teardown-env"
# Every Actions expression that reports on what an earlier job did. Any one of
# them in step 6's `if` replaces the implicit success() on `needs` that IS the
# eval gate, so the check is that none of them appears there at all — narrowing
# it to `always()` alone would leave `!cancelled()` and `success() || failure()`
# as one-line ways to promote on a red eval.
_STATUS_FUNCTIONS = ("always(", "success(", "failure(", "cancelled(")
# GitHub kills a job at six hours whatever its `timeout-minutes` says, so a value
# above this is a promise the runner will not keep.
_GITHUB_JOB_CEILING_MINUTES = 360
# Shell spellings of `continue-on-error`. The poller reports the verdict in its
# exit status and nothing else, so a step that discards that status is the gate
# off just as surely as the YAML key is — and it is the likelier edit of the two,
# since `|| true` reads as defensive rather than as a policy change.
_EXIT_CODE_SWALLOWERS = ("|| true", "||true", "|| :", "set +e")
# The poller's own default deadline, read from the poller. The job has to
# outlast it, because the poller is what writes the verdict and the summary on
# its way out.
_POLLER_DEADLINE_MINUTES = _load_poller().DEFAULT_DEADLINE_MINUTES


def _doc(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


class PromotionPipelineWiringTest(unittest.TestCase):
    def setUp(self):
        self.doc = _doc(_PROMOTION)
        self.jobs = self.doc["jobs"]

    def test_every_called_workflow_targets_the_environment_it_is_named_for(self):
        """Everything that touches the NIGHTLY cluster has to say `nightly`."""
        called = {name: job for name, job in self.jobs.items() if "uses" in job}
        self.assertTrue(called, "the pipeline is supposed to call reusable workflows")
        for name, job in called.items():
            with self.subTest(job=name):
                self.assertEqual(
                    job["with"]["github_environment"], "nightly"
                )

    def test_the_nomination_runs_only_after_a_green_matrix(self):
        """A red matrix nominates nothing and spends no eval capacity."""
        job = self.jobs[_NOMINATE_JOB]
        self.assertIn("step-3-run-e2e-matrix", job["needs"])
        self.assertIn("step-1-resolve-candidate", job["needs"])
        self.assertNotIn("always()", job.get("if", ""))

    def test_the_promotion_runs_only_after_a_green_eval(self):
        """The eval gate itself: no staging tag until the verdict job succeeds.

        `needs` carries an implicit success(), so a red, timed-out or
        never-started eval skips this job and staging stays where it is. Drop the
        dependency, or add an `always()`, and the pipeline promotes on the matrix
        alone — which is exactly the behaviour the eval gate replaced.
        """
        job = self.jobs[_PROMOTE_JOB]
        self.assertIn(_AWAIT_EVAL_JOB, job["needs"])
        self.assertNotIn("always()", job.get("if", ""))
        self.assertIn(_NOMINATE_JOB, self.jobs[_AWAIT_EVAL_JOB]["needs"])

    def test_no_status_function_reaches_the_conditions_the_gate_rests_on(self):
        """`always()` is not the only one-line way to promote on a red eval.

        The gate is the implicit success() that `needs` puts on step 6, and every
        status function replaces it: `!cancelled()` promotes on a red verdict,
        `success() || failure()` promotes on any verdict at all, `always()`
        promotes when the poller was never even reached. Each is a plausible edit
        — "make sure the promotion job always reports" — and each leaves the rest
        of this file green.

        Two jobs are deliberately outside the loop. Step 5b is the exception by
        design and has its own checks below. The teardown is not part of the
        gate at all: it names the results it gates on in its own condition, so
        that the rollback leg can be listed for ordering without its result
        deciding anything, and the `!cancelled()` that replaces the implicit
        success() there is what makes that work. What must not appear in it is
        `always()`, which drops the named checks too, and
        test_teardown_keeps_the_success_gate_on_the_jobs_it_does_depend_on is
        where that is held.
        """
        for name in (_NOMINATE_JOB, _AWAIT_EVAL_JOB, _PROMOTE_JOB):
            condition = self.jobs[name].get("if", "")
            for function in _STATUS_FUNCTIONS:
                with self.subTest(job=name, function=function):
                    self.assertNotIn(
                        function,
                        condition,
                        f"{function}) in {name}'s `if` removes the implicit success() "
                        "on `needs`, which is what the gate is made of",
                    )

    def test_nothing_in_the_eval_wait_tolerates_its_own_failure(self):
        """`continue-on-error` on the poller step is the gate off, silently.

        The poller communicates by exit code, and step 5's conclusion is the only
        thing step 6 consults. A `continue-on-error: true` anywhere in this job —
        on the poller step, on the gcloud login it depends on, or on the job —
        makes step 5 succeed on a red verdict, and step 6's implicit success()
        then promotes it. The run is green throughout, staging moves, and no
        other assertion in this file notices.

        The YAML key is not the only spelling. A step's conclusion is the exit
        status of the shell that ran it, so `python3 poll_rc_eval_verdict.py ||
        true` discards the verdict before Actions ever sees it, and `set +e` at
        the top of the block does the same for everything under it. Those edits
        leave no `continue-on-error` behind to find, which is why the run bodies
        are checked as well as the keys.
        """
        job = self.jobs[_AWAIT_EVAL_JOB]
        self.assertNotIn("continue-on-error", job)
        for step in job["steps"]:
            with self.subTest(step=step.get("name", step.get("uses"))):
                self.assertNotIn("continue-on-error", step)
                body = str(step.get("run", ""))
                for swallower in _EXIT_CODE_SWALLOWERS:
                    self.assertNotIn(
                        swallower,
                        body,
                        f"`{swallower}` in this step discards the exit status step 6 "
                        "gates on, which promotes a red candidate",
                    )

    def test_the_verdict_is_published_for_the_withdrawal_to_read(self):
        """Step 5b's whole safety condition is an output of a job that failed."""
        job = self.jobs[_AWAIT_EVAL_JOB]
        outputs = job.get("outputs") or {}
        self.assertIn("settled", outputs)
        self.assertIn("verdict", outputs)
        poller_step = next(
            step for step in job["steps"] if "poll_rc_eval_verdict.py" in str(step.get("run", ""))
        )
        self.assertIn(
            poller_step["id"],
            outputs["settled"],
            "the settled output has to come from the step that computes it",
        )

    def test_the_withdrawal_fires_only_when_no_verdict_arrived(self):
        """A red eval is an answer, and its nomination tag has to stay put.

        Without the `settled` clause this job deletes the tag on every failure,
        including a red one. The candidate then looks un-nominated to
        resolve_promotion_candidate.sh, and every subsequent nightly re-measures
        a rejected build at hours of a project shared with the merge-blocking
        presubmit, to reach the verdict already on record.
        """
        condition = self.jobs[_WITHDRAW_JOB].get("if", "")
        self.assertIn(f"needs.{_AWAIT_EVAL_JOB}.outputs.settled != 'true'", condition)
        self.assertIn(f"needs.{_AWAIT_EVAL_JOB}.result != 'success'", condition)
        self.assertIn(f"needs.{_NOMINATE_JOB}.result == 'success'", condition)
        # Every clause has to hold, and the assertions above cannot tell an `&&`
        # from an `||`. One `||` anywhere in this condition and `settled != 'true'`
        # stops being a requirement — the job then fires on a red eval too, which
        # is the exact failure the clause was added to prevent.
        self.assertNotIn("||", condition)

    def test_the_withdrawal_depends_on_exactly_the_jobs_its_condition_reads(self):
        """A `needs.<job>` that is not in `needs` reads as empty, not as an error.

        Drop step-5 from the list and `needs.step-5….result != 'success'` is
        trivially true while `settled != 'true'` is trivially true as well, so
        the condition that exists to hold the tag on a red verdict passes on
        every run. Nothing fails; the tag is deleted anyway. Set equality rather
        than containment, because an extra job here silently adds a gate too:
        `always()` does not stop a skipped dependency from being waited on.
        """
        self.assertEqual(
            set(self.jobs[_WITHDRAW_JOB]["needs"]),
            {"step-1-resolve-candidate", _NOMINATE_JOB, _AWAIT_EVAL_JOB},
        )

    def test_the_withdrawal_repeats_the_skips_its_always_removes(self):
        """`always()` here un-cascades both, and skip_promotion is the dangerous one.

        A night that nominates nothing because the candidate already carries an
        evalcand_ tag skips step 5 — so `result != 'success'` holds — while the
        tag it would delete is the one recording the answer that made the run
        skip in the first place.
        """
        condition = self.jobs[_WITHDRAW_JOB].get("if", "")
        self.assertIn("always()", condition)
        self.assertIn("skip_promotion != 'true'", condition)
        self.assertIn("skip_pipeline != 'true'", condition)

    def test_the_withdrawal_deletes_only_through_the_guarded_script(self):
        """An inline `git push --delete` here has no shape guard in front of it."""
        job = self.jobs[_WITHDRAW_JOB]
        runs = " ".join(str(step.get("run", "")) for step in job["steps"])
        self.assertIn("drop_eval_candidate.sh", runs)
        self.assertNotIn("git push", runs)
        self.assertNotIn("git tag", runs)

    def test_the_withdrawal_cannot_reach_the_staging_tag(self):
        """It is handed the nomination tag and the commit, and nothing else."""
        job = self.jobs[_WITHDRAW_JOB]
        for step in job["steps"]:
            with self.subTest(step=step.get("name", step.get("uses"))):
                self.assertNotIn("STAGING_TAG", step.get("env", {}))

    def test_the_eval_wait_outlasts_the_poller_but_fits_inside_a_github_job(self):
        """Either bound broken turns a verdict into an unexplained cancellation."""
        timeout = self.jobs[_AWAIT_EVAL_JOB]["timeout-minutes"]
        self.assertGreater(
            timeout,
            _POLLER_DEADLINE_MINUTES,
            "the runner would kill the poller before it could write its verdict",
        )
        self.assertLessEqual(
            timeout,
            _GITHUB_JOB_CEILING_MINUTES,
            "GitHub caps a job at six hours whatever this says",
        )

    def test_the_eval_wait_activates_its_credential_for_gsutil(self):
        """gsutil ignores GOOGLE_GHA_CREDS_PATH; the poller would see an empty bucket.

        Without the explicit `gcloud auth login --cred-file`, every listing the
        poller makes fails on anonymous credentials, no build is ever found, and
        the job reports `never_ran` — which reads as a broken eval lane rather
        than a missing login. ci-health.yml paid forty failed ticks to learn this.

        Order is half of it: a login after the poller is a login that changes
        nothing, and the run still reports the same empty bucket.
        """
        steps = self.jobs[_AWAIT_EVAL_JOB]["steps"]
        runs = [str(step.get("run", "")) for step in steps]
        login = next(i for i, run in enumerate(runs) if "gcloud auth login" in run)
        poller = next(i for i, run in enumerate(runs) if "poll_rc_eval_verdict.py" in run)
        self.assertIn("GOOGLE_GHA_CREDS_PATH", runs[login])
        self.assertLess(login, poller, "the credential has to be active before the poller reads")

    def test_the_poller_is_given_the_floor_the_nomination_stamped(self):
        """`--not-before` is what makes a second nomination a retry rather than an echo.

        The evalcand_ tag name is stable across attempts, so a renominated
        candidate has last night's finished build sitting at the same commit.
        For the minute or two before Prow uploads tonight's started.json that
        older build is the newest one matching, and a poller without a floor
        answers in seconds with the verdict the withdrawal was undoing — then
        withdraws again while tonight's eval runs for hours unread.

        Three links, and a break in any of them is silent: step 4 publishing the
        stamp, step 5 depending on step 4 to read it, and the flag actually
        carrying it.
        """
        stamp = self.jobs[_NOMINATE_JOB]["outputs"]["nominated_at"]
        nominate_step = next(
            step
            for step in self.jobs[_NOMINATE_JOB]["steps"]
            if "tag_eval_candidate.sh" in str(step.get("run", ""))
        )
        self.assertIn(nominate_step["id"], stamp)
        # Stamped before the push. A stamp taken afterwards can land after Prow
        # wrote started.json, and the poller would then skip tonight's build too
        # and report that no eval ever ran.
        run = str(nominate_step["run"])
        self.assertLess(run.index("nominated_at="), run.index("tag_eval_candidate.sh"))

        self.assertIn(_NOMINATE_JOB, self.jobs[_AWAIT_EVAL_JOB]["needs"])
        poller_step = next(
            step
            for step in self.jobs[_AWAIT_EVAL_JOB]["steps"]
            if "poll_rc_eval_verdict.py" in str(step.get("run", ""))
        )
        self.assertIn(
            f"needs.{_NOMINATE_JOB}.outputs.nominated_at",
            poller_step["env"]["NOMINATED_AT"],
        )
        # `:?` and not a default: the poller accepts the flag being omitted, so a
        # wiring fault would otherwise read as a night of last night's verdict
        # rather than a red job naming the cause.
        self.assertIn('--not-before "${NOMINATED_AT:?', str(poller_step["run"]))

    def test_the_eval_wait_pushes_nothing(self):
        """It reads a bucket. `contents: write` here would be an unused capability."""
        permissions = self.jobs[_AWAIT_EVAL_JOB]["permissions"]
        self.assertEqual(permissions.get("contents"), "read")
        self.assertEqual(permissions.get("id-token"), "write")

    def test_the_resolve_job_binds_the_nightly_environment(self):
        """It reads vars.REGISTRY_PREFIX; unbound, that resolves to empty in silence."""
        self.assertEqual(self.jobs["step-1-resolve-candidate"].get("environment"), "nightly")

    def test_the_nomination_job_runs_and_reports_rather_than_skipping(self):
        """An already-nominated night should show a job that decided, not a gap.

        Gating the job on skip_promotion would collapse the whole thing to
        "skipped" and lose the summary line saying why. The condition sits on the
        steps so the run records the decision it made. Steps 5 and 6 skip
        wholesale instead, deliberately: there is nothing for them to report that
        this job has not already said, and a job that decided nothing is clearer
        as a skip than as a green run with every step conditioned away.
        """
        job = self.jobs[_NOMINATE_JOB]
        self.assertNotIn("skip_promotion", job.get("if", ""))
        step_conditions = [step.get("if", "") for step in job["steps"]]
        self.assertTrue(
            any("skip_promotion" in cond for cond in step_conditions),
            "the skip has to be expressed on the steps instead",
        )
        for name in (_AWAIT_EVAL_JOB, _PROMOTE_JOB):
            with self.subTest(job=name):
                self.assertIn("skip_promotion", self.jobs[name].get("if", ""))

    def test_teardown_does_not_depend_on_the_promotion_path(self):
        """Otherwise the nightly cluster outlives every reason to keep it.

        A skipped or failed job skips its dependents, so depending on any of steps
        4 to 6 would strand a GKE cluster on their failures — and those are
        credential failures (an invalid release bot key or ID, a rejected push)
        that leave nothing on the cluster worth looking at. Step 5 is worse than
        that: it succeeds slowly. Depending on it would hold the cluster for the
        five and a half hours the poller is allowed on a perfectly good night,
        waiting on a verdict about container images already in GHCR that the
        cluster plays no part in producing. The RC pipeline can afford a
        dependency like this because its next scheduled run reclaims the
        environment within three hours; this pipeline has no schedule, so nothing
        would remove it at all.
        """
        teardown = self.jobs[_TEARDOWN_JOB]
        self.assertEqual(
            set(teardown["needs"]),
            {
                "step-1-resolve-candidate",
                "step-2-deploy-env",
                "step-3-run-e2e-matrix",
                "step-3b-rollback-leg",
            },
        )

    def test_teardown_keeps_the_success_gate_on_the_jobs_it_does_depend_on(self):
        """A failed matrix must leave its cluster standing to be examined live."""
        teardown = self.jobs[_TEARDOWN_JOB]
        self.assertNotIn(
            "always()",
            teardown.get("if", ""),
            "always() removes the implicit success() and destroys the environments "
            "a failed run leaves standing for diagnosis",
        )
        self.assertIn("step-3-run-e2e-matrix", teardown["needs"])

    def test_the_rollback_leg_runs_on_the_nightly_cluster_after_the_matrix(self):
        """It moves the install twice, so it has to come after what the matrix graded.

        Bound to `nightly` and holding the `nightly-environment` lock for the
        same reason as the called workflows: pointed anywhere else it rolls
        back a cluster this pipeline did not build.
        """
        job = self.jobs[_ROLLBACK_LEG_JOB]
        self.assertEqual(job.get("environment"), "nightly")
        self.assertEqual(job["concurrency"]["group"], "nightly-environment")
        self.assertIn("step-3-run-e2e-matrix", job["needs"])
        self.assertIn("step-1-resolve-candidate", job["needs"])

    def test_the_rollback_leg_does_not_gate_the_promotion_yet(self):
        """A leg with no green record cannot decide whether staging moves.

        `continue-on-error` is what keeps a red leg from failing the run, and
        keeping it out of the nomination's `needs` is what keeps it from holding
        the promotion — the nomination being where the promotion chain now
        starts, so a dependency there would gate the eval and everything after
        it. Admission is a decision taken on its record, by moving it into that
        list and dropping the flag together.
        """
        leg = self.jobs[_ROLLBACK_LEG_JOB]
        self.assertIs(leg.get("continue-on-error"), True)
        self.assertNotIn(_ROLLBACK_LEG_JOB, self.jobs[_NOMINATE_JOB]["needs"])

    def test_the_rollback_leg_checks_out_the_workflow_ref_with_tags(self):
        """The script resolves the GA to roll back to from the tags in its checkout.

        A checkout at the candidate commit would run whatever copy of the script
        that commit has, or none, and a shallow one has no tags to resolve from.
        """
        steps = self.jobs[_ROLLBACK_LEG_JOB]["steps"]
        checkout = next(
            step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        self.assertEqual(checkout["with"].get("fetch-depth"), 0)
        self.assertNotIn("ref", checkout["with"])
        run_step = next(step for step in steps if "rollback_environment.sh" in str(step.get("run", "")))
        self.assertIn("commit_sha", run_step["env"]["CANDIDATE_SHA"])

    def test_a_red_rollback_leg_still_tears_the_cluster_down(self):
        """The leg gates nothing, so a cluster kept for it is only a bill.

        The teardown's condition names the deploy and matrix results and not
        the leg's, with `!cancelled()` in place of the implicit success().
        """
        cond = self.jobs[_TEARDOWN_JOB].get("if", "")
        self.assertIn("!cancelled()", cond)
        self.assertIn("needs.step-2-deploy-env.result == 'success'", cond)
        self.assertIn("needs.step-3-run-e2e-matrix.result == 'success'", cond)
        self.assertNotIn(f"{_ROLLBACK_LEG_JOB}.result", cond)

    def test_both_tags_are_pushed_with_the_release_bot_token(self):
        """A tag pushed with GITHUB_TOKEN triggers no workflow, so staging never deploys.

        The nomination needs the same token for the other half of that reason: it
        is the App token, not GITHUB_TOKEN, that the tag-protection rulesets let
        through — and so does its withdrawal, which those rulesets guard just as
        they guard the push.
        """
        for name in (_NOMINATE_JOB, _WITHDRAW_JOB, _PROMOTE_JOB):
            with self.subTest(job=name):
                steps = self.jobs[name]["steps"]
                token_step = next(
                    step
                    for step in steps
                    if str(step.get("uses", "")).startswith("actions/create-github-app-token@")
                )
                self.assertIn("RELEASE_BOT_APP_ID", token_step["with"]["app-id"])
                self.assertIn("RELEASE_BOT_APP_PRIVATE_KEY", token_step["with"]["private-key"])
                self.assertEqual(token_step["with"].get("permission-contents"), "write")
                self.assertEqual(token_step["with"].get("permission-workflows"), "write")
                checkout = next(
                    step
                    for step in steps
                    if str(step.get("uses", "")).startswith("actions/checkout@")
                )
                self.assertIn(token_step.get("id", "release-token"), checkout["with"]["token"])

    def test_the_promotion_mints_its_own_token_rather_than_inheriting_one(self):
        """An installation token lasts an hour; the wait before it can last five.

        Nothing in Actions would stop step 6 from consuming a token step 4
        produced — `needs` makes the output available — and the run would look
        fine until the push failed on an expired credential, hours after the
        decision to promote was made. The check is that step 6 has a mint step of
        its own and reads no token output from an earlier job.
        """
        for name in (_WITHDRAW_JOB, _PROMOTE_JOB):
            with self.subTest(job=name):
                job = self.jobs[name]
                self.assertTrue(
                    any(
                        str(step.get("uses", "")).startswith("actions/create-github-app-token@")
                        for step in job["steps"]
                    ),
                    f"{name} must mint a fresh token after the wait",
                )
                self.assertNotIn(
                    f"needs.{_NOMINATE_JOB}.outputs",
                    yaml.safe_dump(job),
                    "a token carried across the eval wait is expired by the time it is used",
                )

    def test_optional_suites_runs_gchat_before_agent_plugin(self):
        """Running gchat before agent-plugin executes chat E2E on a quiescent cluster
        before the 17-step AgentPlugins suite repeatedly rolls platform-agent-gateway."""
        matrix_job = self.jobs["step-3-run-e2e-matrix"]
        optional_suites = matrix_job["with"]["optional_suites"]
        suites = [s.strip() for s in optional_suites.split(",")]
        self.assertIn("gchat", suites)
        self.assertIn("agent-plugin", suites)
        self.assertLess(
            suites.index("gchat"),
            suites.index("agent-plugin"),
            f"gchat must run before agent-plugin in optional_suites, got: {optional_suites!r}",
        )


class ConcurrencyGroupTest(unittest.TestCase):
    def test_no_workflow_hardcodes_the_rc_environment_lock(self):
        """The lock follows the environment, so nothing contends for a cluster it does not deploy to."""
        for path in sorted(_WORKFLOWS.glob("*.yml")):
            with self.subTest(workflow=path.name):
                doc = _doc(path)
                groups = []
                top = doc.get("concurrency")
                if isinstance(top, dict):
                    groups.append(top.get("group"))
                for job in (doc.get("jobs") or {}).values():
                    job_conc = job.get("concurrency")
                    if isinstance(job_conc, dict):
                        groups.append(job_conc.get("group"))
                self.assertNotIn("rc-environment", groups)


class StagingTagContractTest(unittest.TestCase):
    """The tag the pipeline pushes has to match the tag staging deploys on."""

    def _derived_tag(self) -> str:
        proc = subprocess.run(
            ["bash", "-c", f'source "{_COMMON_SH}"; staging_tag_for_rc "rc_2608241820_b35543c_validated"'],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(),
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_the_derived_tag_matches_staging_deploy_trigger(self):
        tag = self._derived_tag()
        patterns = _doc(_WORKFLOWS / _STAGING_DEPLOY)[True]["push"]["tags"]
        self.assertTrue(
            any(fnmatch.fnmatch(tag, pattern) for pattern in patterns),
            f"{tag!r} matches none of {patterns!r}",
        )

    def test_the_promotion_tag_is_annotated(self):
        """Which is what makes the peel below necessary rather than defensive.

        An annotated tag's ref points at a tag object; the push event hands that
        object's SHA to github.sha. If this ever became a lightweight tag the peel
        would still be correct, just redundant.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        head = git("rev-parse", "HEAD").stdout.strip()

        proc = subprocess.run(
            ["bash", "-c", f'source "{_COMMON_SH}"; ensure_git_tag staging_2608241820_b35543c "{head}" "promotion"'],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(),
            cwd=repo_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            git("cat-file", "-t", "staging_2608241820_b35543c").stdout.strip(),
            "tag",
        )

    def test_staging_deploy_peels_tag_to_commit(self):
        """An annotated tag's ref resolves to the tag object, not to the commit."""
        doc = _doc(_WORKFLOWS / _STAGING_DEPLOY)
        jobs = doc["jobs"]
        resolve = jobs["resolve-commit"]
        self.assertTrue(
            any("resolve_deploy.sh" in step.get("run", "") and step.get("env", {}).get("TARGET_ENVIRONMENT") == "staging" for step in resolve["steps"]),
            "resolve-commit is supposed to call resolve_deploy.sh with TARGET_ENVIRONMENT: staging",
        )
        self.assertEqual(jobs["deploy"]["needs"], "resolve-commit")
        image_tag = jobs["deploy"]["with"].get("image_tag")
        self.assertNotIn("github.sha", str(image_tag))
        self.assertIn("resolve-commit", str(image_tag))

    def test_staging_deploy_calls_reconcile_environment_for_staging(self):
        doc = _doc(_WORKFLOWS / _STAGING_DEPLOY)
        deploy_job = doc["jobs"]["deploy"]
        self.assertIn("reconcile-environment.yml", deploy_job["uses"])
        self.assertEqual(deploy_job["with"]["github_environment"], "staging")
        self.assertEqual(deploy_job["with"]["mode"], "apply")

    def test_staging_deploy_verifies_candidate_images(self):
        doc = _doc(_WORKFLOWS / _STAGING_DEPLOY)
        steps = doc["jobs"]["resolve-commit"]["steps"]
        self.assertTrue(
            any("verify_candidate_images.sh" in step.get("run", "") for step in steps),
            "resolve-commit must verify candidate images in GHCR",
        )

    def test_staging_deploy_workflow_dispatch_defaults_lease_policy_to_fail(self):
        doc = _doc(_WORKFLOWS / _STAGING_DEPLOY)
        inputs = doc[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["lease_policy"]["default"], "fail")

    def test_staging_deploy_has_verify_deploy_job_asserting_applied(self):
        doc = _doc(_WORKFLOWS / _STAGING_DEPLOY)
        self.assertIn("verify-deploy", doc["jobs"])
        verify = doc["jobs"]["verify-deploy"]
        self.assertEqual(set(verify["needs"]), {"resolve-commit", "deploy"})
        steps = verify["steps"]
        self.assertTrue(
            any("verify_deploy_result.sh" in s.get("run", "") for s in steps),
            "verify-deploy must invoke verify_deploy_result.sh",
        )

    def test_the_resolve_job_binds_the_staging_environment(self):
        """It reads vars.REGISTRY_PREFIX; unbound, that resolves to empty in silence."""
        doc = _doc(_WORKFLOWS / _STAGING_DEPLOY)
        self.assertEqual(doc["jobs"]["resolve-commit"].get("environment"), "staging")


_AUTOPUSH_DEPLOY = "autopush-deploy.yml"


class AutopushDeployWiringTest(unittest.TestCase):
    def setUp(self):
        self.doc = _doc(_WORKFLOWS / _AUTOPUSH_DEPLOY)
        self.jobs = self.doc["jobs"]

    def test_the_resolve_job_binds_the_autopush_environment(self):
        """It reads vars.REGISTRY_PREFIX; unbound, that resolves to empty in silence."""
        self.assertEqual(self.jobs["resolve-candidate"].get("environment"), "autopush")

    def test_triggers_include_workflow_run_and_dispatch(self):
        on = self.doc[True]
        self.assertNotIn("schedule", on, "autopush deploy must not run on cron")
        self.assertIn("workflow_run", on)
        self.assertEqual(on["workflow_run"]["workflows"], ["Publish Images to GHCR"])
        self.assertIn("workflow_dispatch", on)

    def test_concurrency_group_locks_autopush_deploy_without_cancelling(self):
        concurrency = self.doc.get("concurrency", {})
        self.assertEqual(concurrency.get("group"), "autopush-deploy")
        self.assertFalse(concurrency.get("cancel-in-progress"), "running deploys must not be cancelled mid-flight")

    def test_upstream_repository_guard_present(self):
        resolve = self.jobs["resolve-candidate"]
        self.assertIn("github.repository == 'gke-labs/kube-agents'", resolve.get("if", ""))
        self.assertIn("github.event.workflow_run.head_repository.full_name == github.repository", resolve.get("if", ""))
        self.assertIn("github.event.workflow_run.head_branch == 'main'", resolve.get("if", ""))

    def test_resolve_candidate_calls_script(self):
        resolve = self.jobs["resolve-candidate"]
        steps = resolve["steps"]
        self.assertTrue(
            any("resolve_deploy.sh" in step.get("run", "") and step.get("env", {}).get("TARGET_ENVIRONMENT") == "autopush" for step in steps),
            "resolve-candidate must use resolve_deploy.sh with TARGET_ENVIRONMENT: autopush",
        )

    def test_resolve_candidate_verifies_candidate_images(self):
        resolve = self.jobs["resolve-candidate"]
        steps = resolve["steps"]
        self.assertTrue(
            any("verify_candidate_images.sh" in step.get("run", "") for step in steps),
            "resolve-candidate must verify candidate images in GHCR",
        )

    def test_autopush_deploy_workflow_dispatch_defaults_lease_policy_to_fail(self):
        inputs = self.doc[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["lease_policy"]["default"], "fail")

    def test_autopush_deploy_has_verify_deploy_job_asserting_applied(self):
        self.assertIn("verify-deploy", self.jobs)
        verify = self.jobs["verify-deploy"]
        self.assertEqual(set(verify["needs"]), {"resolve-candidate", "deploy"})
        steps = verify["steps"]
        self.assertTrue(
            any("verify_deploy_result.sh" in s.get("run", "") for s in steps),
            "verify-deploy must invoke verify_deploy_result.sh",
        )

    def test_deploy_job_calls_reconcile_environment_for_autopush(self):
        deploy = self.jobs["deploy"]
        self.assertEqual(deploy["needs"], "resolve-candidate")
        self.assertIn("reconcile-environment.yml", deploy["uses"])
        self.assertEqual(deploy["with"]["github_environment"], "autopush")
        self.assertEqual(deploy["with"]["mode"], "apply")
        self.assertIn("needs.resolve-candidate.outputs.commit_sha", deploy["with"]["image_tag"])
        self.assertEqual(deploy["if"], "github.repository == 'gke-labs/kube-agents'")


class DockerPublishGhcrWiringTest(unittest.TestCase):
    def setUp(self):
        self.doc = _doc(_WORKFLOWS / "docker-publish-ghcr.yml")
        self.jobs = self.doc["jobs"]

    def test_single_workflow_builds_all_required_images(self):
        self.assertIn("publish-operator", self.jobs)
        self.assertIn("publish-agents", self.jobs)
        self.assertFalse((_WORKFLOWS / "docker-publish-k8s-operator.yml").exists())


class ReleaseBotTokenWiringTest(unittest.TestCase):
    """Every workflow that mints a token for RELEASE_BOT_APP_ID must request both
    contents: write (for creating refs/tags) and workflows: write (to prevent GH013
    rejections when the tagged commit has workflow diffs relative to main)."""

    def test_every_release_bot_token_mint_requests_workflows_write(self):
        workflows = [
            "staging-promotion-pipeline.yml",
            "release-publish.yml",
            "rc-create-tag.yml",
            "rc-tag-validated.yml",
        ]
        for name in workflows:
            with self.subTest(workflow=name):
                doc = _doc(_WORKFLOWS / name)
                token_steps = [
                    step
                    for job in (doc.get("jobs") or {}).values()
                    for step in (job.get("steps") or [])
                    if str(step.get("uses", "")).startswith("actions/create-github-app-token@")
                    and "RELEASE_BOT_APP_ID" in str(step.get("with", {}).get("app-id", ""))
                ]
                self.assertTrue(token_steps, f"expected at least one release bot token step in {name}")
                for step in token_steps:
                    self.assertEqual(step["with"].get("permission-contents"), "write")
                    self.assertEqual(step["with"].get("permission-workflows"), "write")


if __name__ == "__main__":
    unittest.main()

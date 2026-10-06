"""Tests for step 0 of the smoke-test presubmit: hack/ci-revalidate.sh.

Step 0 may skip the whole eval matrix, so the property that matters most is
that it fails CLOSED: the ONLY paths that exit early are a prior green build
of this PR's own job at THIS head (whatever main has done since), or one at
an earlier head plus head- and base-deltas that both match the inert-path
list -- and in a batch, one of those for EVERY pull. Everything else -- no
history, unreadable or unparsable records, a commit the checkout does not
have, a single non-inert file on either side, one pull of a batch without a
verdict, the escape hatch -- must fall through to a full run.

The script is copied into a fixture checkout (with the mint module beside
it) and sourced, then executed with `gsutil` stubbed, GitHub faked by a
`sitecustomize` that replaces urlopen for every python the script runs, and
the fixture git repository standing in for the decorated checkout, so these
assertions are against the code that ships.
The REVALIDATED log line's shape is pinned because humans grep build logs for
it, and so that any future dashboard-collector support has a stable line to
key on (scripts/eval_dashboard/collect.py reads nothing from it today).
"""

import json
import os
import pathlib
import subprocess
import unittest.mock
import tempfile
import textwrap
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL_PR = _REPO_ROOT / "hack" / "ci-eval-pr.sh"
_CI_REVALIDATE = _REPO_ROOT / "hack" / "ci-revalidate.sh"
_LEDGER_MINT = _REPO_ROOT / "hack" / "ledger_token_mint.py"

_PR = "77"
_OTHER_PR = "78"
_JOB = "pull-kube-agents-smoke-test"

# Printed by the wrapper before it sources the script, whose constants read
# JOB_NAME as they load, so a test can pin whether the variable was absent
# from the child's environment or set (possibly empty) --
# `${JOB_NAME:-default}` cannot tell the two apart, which is exactly why a
# test that claims to cover both has to prove it handed the script both.
_JOB_NAME_PROBE = 'if [ -n "${JOB_NAME+x}" ]; then echo "JOB_NAME: set"; else echo "JOB_NAME: absent"; fi'
_JOB_NAME_SET = "JOB_NAME: set"
_JOB_NAME_ABSENT = "JOB_NAME: absent"

_GSUTIL_STUB = """#!/usr/bin/env bash
# gsutil stub: `ls` prints the fixture listing for the PR named in the glob
# (GSUTIL_LS_DIR/<pr>.txt, else it fails like a no-match glob), `cat` serves
# "<build>.<file>" out of GSUTIL_OBJECT_DIR.
# Every call is appended to GSUTIL_CALL_LOG so a test can pin WHICH history
# the script read -- reading another PR's (or another job's) records would
# reuse a foreign verdict while every content assertion still passed.
cmd="$1"; shift
if [ -n "${GSUTIL_CALL_LOG:-}" ]; then
  echo "${cmd} $*" >> "${GSUTIL_CALL_LOG}"
fi
case "${cmd}" in
  ls)
    pr="$(printf '%s' "$1" | sed -n 's|.*/gke-labs_kube-agents/\\([0-9]*\\)/.*|\\1|p')"
    if [ -n "${GSUTIL_LS_DIR:-}" ] && [ -f "${GSUTIL_LS_DIR}/${pr}.txt" ]; then
      cat "${GSUTIL_LS_DIR}/${pr}.txt"
    else
      echo "CommandException: One or more URLs matched no objects." >&2
      exit 1
    fi
    ;;
  cat)
    build="$(basename "$(dirname "$1")")"
    object="${GSUTIL_OBJECT_DIR}/${build}.$(basename "$1")"
    [ -f "${object}" ] || exit 1
    cat "${object}"
    ;;
  *) exit 1 ;;
esac
"""


# Installed through PYTHONPATH: python imports sitecustomize at startup, so
# every urllib.request.urlopen the script's python runs -- the mint and the
# status read -- lands here, and the GitHub a test wants is the one it
# serves. Each request is appended to GITHUB_REQUEST_LOG as one JSON line
# (url, method, Authorization header, body), so a test can pin which
# credential a read carried and that a refused read was not tried again.
# Statuses come from GITHUB_STATUS_DIR/<sha>.json, or an empty list for a
# head with no file, which is what GitHub answers for a commit with no
# events; GITHUB_FAKE_STATUS_HTTP forces an HTTP error on the read and
# GITHUB_FAKE_MINT_HTTP one on the mint.
_FAKE_GITHUB = textwrap.dedent(
    '''
    import io
    import json
    import os
    import re
    import urllib.error
    import urllib.request


    def _answer(url, code, payload):
        body = io.BytesIO(json.dumps(payload).encode())
        if code >= 400:
            raise urllib.error.HTTPError(url, code, "fake", {}, body)
        return body


    def _fake_urlopen(request, timeout=None):
        url = request.full_url
        with open(os.environ["GITHUB_REQUEST_LOG"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "url": url,
                "method": request.get_method(),
                "authorization": request.get_header("Authorization"),
                "data": request.data.decode() if request.data is not None else None,
            }) + "\\n")
        if "/access_tokens" in url:
            code = int(os.environ.get("GITHUB_FAKE_MINT_HTTP") or 201)
            minted = {"token": "ghs_minted", "expires_at": "2026-10-06T12:00:00Z"}
            return _answer(url, code, minted if code == 201 else {"message": "fake mint failure"})
        found = re.search(r"/commits/([0-9a-f]+)/statuses", url)
        if not found:
            return _answer(url, 500, {"message": "the fake serves mints and status reads only"})
        forced = os.environ.get("GITHUB_FAKE_STATUS_HTTP")
        if forced:
            return _answer(url, int(forced), {"message": "fake status failure"})
        path = os.path.join(os.environ.get("GITHUB_STATUS_DIR", ""), found.group(1) + ".json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return io.BytesIO(fh.read().encode())
        return _answer(url, 200, [])


    urllib.request.urlopen = _fake_urlopen
    '''
)


class RevalidationTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)

        # The stub gsutil, first on PATH; the fake GitHub, first on PYTHONPATH.
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        stub = self.bin / "gsutil"
        stub.write_text(_GSUTIL_STUB)
        stub.chmod(0o755)
        self.pysite = self.tmp / "pysite"
        self.pysite.mkdir()
        (self.pysite / "sitecustomize.py").write_text(_FAKE_GITHUB, encoding="utf-8")
        self.requests_log = self.tmp / "github.requests"

        self.objects = self.tmp / "objects"
        self.objects.mkdir()
        self.statuses = self.tmp / "statuses"
        self.statuses.mkdir()
        self.listings = self.tmp / "listings"
        self.listings.mkdir()

        # The fixture checkout. A linear chain is enough: deltas are plain
        # `git diff A B`, so each scenario just picks its four SHAs.
        self.repo = self.tmp / "repo"
        (self.repo / "hack").mkdir(parents=True)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "fixture")
        self._git("config", "user.email", "fixture@example.invalid")
        self.c1 = self._commit("c1", {"code.py": "v1", "docs/a.md": "v1", "README.md": "v1"})
        self.c2 = self._commit("c2", {"docs/a.md": "v2"})
        self.c3 = self._commit("c3", {"README.md": "v2"})
        self.c4 = self._commit("c4", {"docs/b.md": "v1"})
        self.c5 = self._commit("c5", {"code.py": "v2"})
        self.c6 = self._commit(
            "c6", {"docs-evil.go": "v1", "sub/notes.md": "v1", "bench/OWNERS": "v1"}
        )
        # c7 renames a non-inert file to an inert destination without editing
        # it -- 100% similarity, so git's rename detection would collapse it
        # to the destination path alone.
        self._git("mv", "code.py", "docs/moved.md")
        self.c7 = self._commit("c7", {})

    def _git(self, *args):
        subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def _commit(self, message, files):
        for rel, content in files.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content + "\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)
        out = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()

    def _plant_history(self, builds, attest=True, pr=_PR):
        """builds: [(build_id, passed, base_sha, head_sha)], any record None to omit.

        With attest=True (the default), each green build also gets the
        Prow-posted GitHub success status event the script demands; a test
        that plants a "green" GCS record WITHOUT one is modelling the forged
        record kube-agents-bot's review described. The listing is written
        per pull request, which is what lets a batch test plant two
        histories the stub serves by the PR number in the glob.
        """
        listing = []
        status_events = {}
        for build_id, passed, base_sha, head_sha in builds:
            listing.append(
                f"gs://kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/{pr}/{_JOB}/{build_id}/finished.json"
            )
            if passed is not None:
                revision = f', "revision": "{head_sha}"' if head_sha else ""
                (self.objects / f"{build_id}.finished.json").write_text(
                    '{"passed": %s, "result": "%s"%s}'
                    % ("true" if passed else "false", "SUCCESS" if passed else "FAILURE", revision)
                )
            if base_sha is not None:
                (self.objects / f"{build_id}.started.json").write_text(
                    '{"repos": {"gke-labs/kube-agents": "main:%s,%s:%s"}}'
                    % (base_sha, pr, head_sha)
                )
            if attest and passed and head_sha:
                status_events.setdefault(head_sha, []).append(
                    {
                        "context": _JOB,
                        "state": "success",
                        "target_url": f"https://oss.gprow.dev/view/gs/kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/{pr}/{_JOB}/{build_id}",
                    }
                )
        for head_sha, events in status_events.items():
            self._plant_statuses(head_sha, events)
        (self.listings / f"{pr}.txt").write_text("\n".join(listing) + "\n")

    def _plant_statuses(self, head_sha, events):
        # Append-only, like GitHub's: two histories (a batch's two pulls)
        # attesting builds at the same head both keep their events.
        path = self.statuses / f"{head_sha}.json"
        existing = json.loads(path.read_text()) if path.exists() else []
        path.write_text(json.dumps(existing + events))

    # The three pieces every way of running the script shares -- `_run`
    # (sourced, with a wrapper naming the verdict) and the entrypoint class's
    # `_execute` (the file itself) -- live here once, so the set of variables
    # a test relies on being ABSENT is held in one place. The child env is
    # built from os.environ, and a developer's shell may export the very
    # variable a test is unsetting: JOB_TYPE and PULL_REFS from a live batch
    # run, EVAL_SKIP_REVALIDATION as the operator's lever.
    _NEUTRALISED = (
        "JOB_TYPE",
        "PULL_REFS",
        "EVAL_SKIP_REVALIDATION",
        "EVAL_LEDGER_APP_KEY_FILE",
        "EVAL_LEDGER_APP_ID",
        "EVAL_LEDGER_INSTALLATION_ID",
    )

    def _install_script(self):
        """The shipped script, copied into the fixture repo's hack/ so its own
        BASH_SOURCE-derived repo_dir points at the fixture checkout, the same
        way it points at the real one in the pod."""
        copy = self.repo / "hack" / "ci-revalidate.sh"
        copy.write_text(_CI_REVALIDATE.read_text(encoding="utf-8"))
        # The mint module the script finds beside itself, as in the pod.
        (self.repo / "hack" / _LEDGER_MINT.name).write_text(_LEDGER_MINT.read_text(encoding="utf-8"))
        return copy

    def _requests(self):
        """Every GitHub request the run made, oldest first."""
        if not self.requests_log.exists():
            return []
        return [json.loads(line) for line in self.requests_log.read_text(encoding="utf-8").splitlines()]

    def _throwaway_key(self):
        key = self.tmp / "throwaway.pem"
        try:
            gen = subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], capture_output=True, text=True)
        except FileNotFoundError:  # pragma: no cover - a machine without openssl
            self.skipTest("openssl is not on PATH, and the mint signs its JWT with it")
        if gen.returncode != 0:  # pragma: no cover
            self.skipTest(f"openssl could not generate a throwaway key: {gen.stderr}")
        return key

    def _base_env(self, cur_head, cur_base):
        self.call_log = self.tmp / "gsutil.calls"
        env = {
            "PULL_NUMBER": _PR,
            "PULL_PULL_SHA": cur_head,
            "PULL_BASE_SHA": cur_base,
            "PULL_BASE_REF": "main",
            # Unset in the pod that is not this job; a developer's shell may
            # carry one, and the default is what these fixtures name.
            "JOB_NAME": "",
            "GSUTIL_OBJECT_DIR": str(self.objects),
            "GSUTIL_LS_DIR": str(self.listings),
            "GSUTIL_CALL_LOG": str(self.call_log),
            "GITHUB_STATUS_DIR": str(self.statuses),
            "GITHUB_REQUEST_LOG": str(self.requests_log),
            "PYTHONPATH": str(self.pysite),
            "BENCH_GITHUB_TOKEN": "",
        }
        # None: absent from the child's environment, not set to an empty
        # string; a serial test must prove PULL_NUMBER alone selects its path.
        env.update({key: None for key in self._NEUTRALISED})
        return env

    def _child_env(self, env):
        # A None value names a variable the child must not see; the helper
        # drops it from its os.environ copy, since an override cannot.
        return get_isolated_test_env(
            overrides={key: value for key, value in env.items() if value is not None},
            bin_dir=self.bin,
            absent=[key for key, value in env.items() if value is None],
        )

    def _run(self, cur_head, cur_base, env_overrides=None):
        # Sourced rather than executed so the wrapper can name the verdict it
        # took; the entrypoint class below executes the file itself.
        copy = self._install_script()
        script = "\n".join(
            [
                "set -euo pipefail",
                _JOB_NAME_PROBE,
                f'source "{copy}"',
                "if revalidate_against_green_history; then",
                '  echo "VERDICT: REVALIDATED-EXIT"',
                "  exit 0",
                "fi",
                'echo "VERDICT: FULL-RUN"',
            ]
        )
        under_test = self.repo / "hack" / "step0_under_test.sh"
        under_test.write_text(script)
        env = self._base_env(cur_head, cur_base)
        if env_overrides:
            env.update(env_overrides)
        child_env = self._child_env(env)
        return subprocess.run(
            ["bash", str(under_test)],
            capture_output=True,
            text=True,
            env=child_env,
        )

    # ── the one path that skips ──────────────────────────────────────────────

    def test_green_history_plus_inert_deltas_reuses_the_verdict(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        # Both delta file lists and the predicate are in the log.
        self.assertIn("docs/b.md", proc.stdout)
        self.assertIn("docs/a.md", proc.stdout)
        self.assertIn("REVALIDATION_INERT_PATHS", proc.stdout)

    def test_the_revalidated_log_line_shape_is_pinned(self):
        """Humans grep build logs for this line (and future collector support
        needs a stable line to key on); the word REVALIDATED and the reused
        build id must appear together, a serial run names its one build with
        no pull suffix, and the Spyglass URL must follow."""
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn(
            "Step 0: REVALIDATED against green build 200 -- skipping the eval matrix ===",
            proc.stdout,
        )
        self.assertEqual(proc.stdout.count("Step 0: REVALIDATED"), 1)
        self.assertIn(
            "Reused verdict: https://oss.gprow.dev/view/gs/kube-agents-prow/"
            f"pr-logs/pull/gke-labs_kube-agents/{_PR}/{_JOB}/200",
            proc.stdout,
        )

    def test_the_history_read_is_scoped_to_this_prs_own_job(self):
        """A wrong PR number, job name or bucket in the history path would
        reuse a FOREIGN verdict while every content assertion still passed;
        the URLs the script hands gsutil are the load-bearing part."""
        self._plant_history([("200", True, self.c1, self.c3)])
        self._run(cur_head=self.c4, cur_base=self.c2)
        calls = self.call_log.read_text().splitlines()
        prefix = f"gs://kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/{_PR}/{_JOB}"
        self.assertIn(f"ls {prefix}/*/finished.json", calls)
        self.assertIn(f"cat {prefix}/200/finished.json", calls)
        self.assertIn(f"cat {prefix}/200/started.json", calls)

    def test_the_history_read_is_keyed_on_the_running_jobs_name(self):
        """A second presubmit running this script (the next-mode lane, under
        EVAL_MODE_NEXT=1) has its own history path and its own status
        context. Keyed on a fixed name it would find the today job's green
        build at the same head and skip its own matrix; keyed on JOB_NAME it
        reads only its own history, and the today job's status attests
        nothing for it."""
        other_job = f"{_JOB}-next"
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(
            cur_head=self.c4, cur_base=self.c2, env_overrides={"JOB_NAME": other_job}
        )
        calls = self.call_log.read_text().splitlines()
        own = f"gs://kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/{_PR}/{other_job}"
        self.assertIn(f"ls {own}/*/finished.json", calls)
        self.assertFalse(
            [c for c in calls if f"/{_PR}/{_JOB}/" in c],
            f"the today job's history was read under JOB_NAME={other_job}: {calls}",
        )
        # The stub listing is the today job's; its status event names the
        # today context, so the other job's attestation must fail closed.
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn(f"GitHub holds no {other_job} success status", proc.stdout)

    def test_job_name_unset_or_empty_reads_the_today_jobs_history(self):
        """The script reads `${JOB_NAME:-default}`, which treats an absent
        variable and an empty one alike -- so both states have to reach it.
        The probe line pins which one each subtest handed the script; without
        it the "unset" subtest was the "empty" one run twice, because `_run`
        seeds JOB_NAME="" and a None override used to change nothing."""
        self._plant_history([("200", True, self.c1, self.c3)])
        cases = (({"JOB_NAME": ""}, _JOB_NAME_SET), ({"JOB_NAME": None}, _JOB_NAME_ABSENT))
        for overrides, probe in cases:
            with self.subTest(overrides=overrides):
                proc = self._run(
                    cur_head=self.c4, cur_base=self.c2, env_overrides=overrides
                )
                self.assertIn(probe, proc.stdout, proc.stdout + proc.stderr)
                self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
                self.assertIn(f"Attested by the Prow-posted {_JOB} success status", proc.stdout)

    def test_an_identical_base_is_trivially_inert(self):
        """An empty base delta means main's tree is byte-identical to the one
        the green verdict graded -- reuse is correct, not an edge case."""
        self._plant_history([("200", True, self.c2, self.c3)])
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertIn("trivially inert", proc.stdout)

    # ── the same head passes whatever main did ──────────────────────────────

    def test_a_green_at_this_head_is_reused_whatever_main_did_since(self):
        """The retest Tide starts because main moved, serial or batch: the
        base delta c1..c5 touches code.py, which the inert rule would refuse,
        and the same-head rule does not look at it."""
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(cur_head=self.c3, cur_base=self.c5)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertRegex(proc.stdout, r"Step 0: REVALIDATED against green build 200\b")
        self.assertIn("this head already passed", proc.stdout)
        self.assertIn(f"base {self.c1} then, {self.c5} now", proc.stdout)
        self.assertNotIn("code.py", proc.stdout)

    def test_a_green_at_this_head_behind_a_newer_green_elsewhere_is_still_found(self):
        """A force-push back to an earlier head: the newest green (300) is at
        c5, and c5..c3 touches code.py, so the inert rule would run full;
        the older green at this very head (200) is the one to reuse."""
        self._plant_history(
            [("300", True, self.c1, self.c5), ("200", True, self.c1, self.c3)]
        )
        proc = self._run(cur_head=self.c3, cur_base=self.c2)
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertRegex(proc.stdout, r"REVALIDATED against green build 200\b")
        self.assertIn("this head already passed", proc.stdout)

    def test_the_same_head_rule_still_demands_the_attestation(self):
        """Same head or not, a GCS record with no Prow-posted success status
        on that head is the forged record, and runs full."""
        self._plant_history([("200", True, self.c1, self.c3)], attest=False)
        self._plant_statuses(self.c3, [])
        proc = self._run(cur_head=self.c3, cur_base=self.c5)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("refusing to trust the GCS record alone", proc.stdout)

    def test_a_red_at_this_head_newer_than_its_green_is_overridden(self):
        """The newest GREEN wins, as for inert pushes. For the same head that is
        a rule, not a proof of flake: the script header says a same-head red
        can also be the combination with a newer main, which the reuse does
        not test."""
        self._plant_history(
            [("300", False, self.c5, self.c3), ("200", True, self.c1, self.c3)]
        )
        proc = self._run(cur_head=self.c3, cur_base=self.c5)
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertRegex(proc.stdout, r"REVALIDATED against green build 200\b")

    # ── a batch is revalidated pull by pull ──────────────────────────────────

    def _batch_env(self, pulls, cur_base):
        refs = f"main:{cur_base}," + ",".join(f"{pr}:{sha}" for pr, sha in pulls)
        return {
            "PULL_NUMBER": None,
            "PULL_PULL_SHA": None,
            "JOB_TYPE": "batch",
            "PULL_REFS": refs,
        }

    def test_a_batch_whose_every_pull_is_green_at_its_head_is_reused(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        self._plant_history([("400", True, self.c2, self.c4)], pr=_OTHER_PR)
        proc = self._run(
            cur_head="",
            cur_base=self.c5,
            env_overrides=self._batch_env([(_PR, self.c3), (_OTHER_PR, self.c4)], self.c5),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertIn("batch of 2 pull requests", proc.stdout)
        self.assertIn(f"PR #{_PR} holds a reusable verdict: green build 200", proc.stdout)
        self.assertIn(f"PR #{_OTHER_PR} holds a reusable verdict: green build 400", proc.stdout)
        # One banner for the job, after both pulls, naming both builds.
        self.assertRegex(
            proc.stdout,
            rf"Step 0: REVALIDATED against green build 200 \(PR #{_PR}\), 400 \(PR #{_OTHER_PR}\) -- skipping",
        )
        self.assertEqual(proc.stdout.count("Step 0: REVALIDATED"), 1)
        calls = self.call_log.read_text().splitlines()
        for pr in (_PR, _OTHER_PR):
            self.assertIn(
                f"ls gs://kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/{pr}/{_JOB}/*/finished.json",
                calls,
            )

    def test_a_batch_runs_full_when_one_pull_has_no_verdict(self):
        """The second pull's only green is at an older head with a non-inert
        head delta (c3..c5 touches code.py), so the whole batch runs."""
        self._plant_history([("200", True, self.c1, self.c3)])
        self._plant_history([("400", True, self.c1, self.c3)], pr=_OTHER_PR)
        proc = self._run(
            cur_head="",
            cur_base=self.c2,
            env_overrides=self._batch_env([(_PR, self.c3), (_OTHER_PR, self.c5)], self.c2),
        )
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("files outside REVALIDATION_INERT_PATHS", proc.stdout)
        self.assertIn("code.py", proc.stdout)

    def test_a_batch_runs_full_when_one_pull_has_no_history(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(
            cur_head="",
            cur_base=self.c5,
            env_overrides=self._batch_env([(_PR, self.c3), (_OTHER_PR, self.c4)], self.c5),
        )
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn(f"no finished {_JOB} build for PR #{_OTHER_PR}", proc.stdout)
        # The first pull's verdict is reported, but the job-level banner that
        # says the matrix is skipped must not appear in a log of a run that
        # then ran it: humans and collectors key on that line.
        self.assertIn(f"PR #{_PR} holds a reusable verdict: green build 200", proc.stdout)
        self.assertNotIn("REVALIDATED", proc.stdout)

    def test_a_batch_env_that_also_carries_pull_number_is_still_a_batch(self):
        """JOB_TYPE decides. A PULL_NUMBER beside a batch's PULL_REFS (an
        operator shell, never Prow) must not narrow the batch to that one
        pull: the second pull has no history, so the batch runs full."""
        self._plant_history([("200", True, self.c1, self.c3)])
        env = self._batch_env([(_PR, self.c3), (_OTHER_PR, self.c4)], self.c5)
        env.update({"PULL_NUMBER": _PR, "PULL_PULL_SHA": self.c3})
        proc = self._run(cur_head=self.c3, cur_base=self.c5, env_overrides=env)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("batch of 2 pull requests", proc.stdout)
        self.assertIn(f"no finished {_JOB} build for PR #{_OTHER_PR}", proc.stdout)
        self.assertNotIn("REVALIDATED", proc.stdout)

    def test_a_batch_job_without_pull_refs_is_a_full_run(self):
        proc = self._run(
            cur_head=self.c3,
            cur_base=self.c5,
            env_overrides={"PULL_NUMBER": _PR, "PULL_PULL_SHA": self.c3, "JOB_TYPE": "batch", "PULL_REFS": None},
        )
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("a batch job with PULL_REFS or PULL_BASE_SHA unset", proc.stdout)

    def test_a_malformed_pull_refs_is_a_full_run(self):
        """A PULL_REFS whose entries are not <number>:<40-hex> is a full run
        before any SHA reaches gsutil or git -- a short SHA, a base alone, a
        non-numeric number."""
        self._plant_history([("200", True, self.c1, self.c3)])
        for refs in (
            f"main:{self.c5}",
            f"main:{self.c5},{_PR}:{self.c3[:12]}",
            f"main:{self.c5},pr:{self.c3}",
            f"main:{self.c5},{_PR}:{self.c3}; rm -rf /",
            # A pull in the base's slot is refused, not dropped unread.
            f"{_PR}:{self.c3},{_OTHER_PR}:{self.c4}",
            # The base entry must be PULL_BASE_SHA under PULL_BASE_REF.
            f"main:{self.c4},{_PR}:{self.c3}",
            f"release/0.8:{self.c5},{_PR}:{self.c3}",
        ):
            with self.subTest(refs=refs):
                proc = self._run(
                    cur_head="",
                    cur_base=self.c5,
                    env_overrides={
                        "PULL_NUMBER": None,
                        "PULL_PULL_SHA": None,
                        "JOB_TYPE": "batch",
                        "PULL_REFS": refs,
                    },
                )
                self.assertIn("VERDICT: FULL-RUN", proc.stdout)
                self.assertIn("PULL_REFS is not <base_ref>:<PULL_BASE_SHA>", proc.stdout)
                self.assertNotIn("holds a reusable verdict", proc.stdout)
                # Refused before any SHA reached gsutil (the stub logs every
                # call) or git (the fixture repo's only reader is the script).
                self.assertFalse(self.call_log.exists(), "gsutil was called on a refused PULL_REFS")

    def test_a_malformed_base_or_head_sha_is_a_full_run(self):
        """PULL_BASE_SHA and PULL_PULL_SHA are held to 40-hex before they can
        reach `git diff`, where a value shaped like an option would be taken
        as one and an empty file list would read as an inert delta."""
        self._plant_history([("200", True, self.c1, self.c3)])
        cases = (
            ("batch base", dict(self._batch_env([(_PR, self.c3)], "--output=/dev/null"), PULL_BASE_SHA="--output=/dev/null"), "PULL_BASE_SHA is not a 40-hex SHA"),
            ("serial base", {"PULL_BASE_SHA": "--output=/dev/null"}, "PULL_BASE_SHA is not a 40-hex SHA"),
            ("serial head", {"PULL_PULL_SHA": self.c3[:12]}, "PULL_NUMBER or PULL_PULL_SHA is not"),
            ("serial number", {"PULL_NUMBER": "77; rm -rf /"}, "PULL_NUMBER or PULL_PULL_SHA is not"),
            # A well-formed first line over a second: the check holds the whole
            # value, where a line-wise grep would pass the first line and hand
            # both to git.
            ("multi-line base", {"PULL_BASE_SHA": f"{self.c2}\n--output=/dev/null"}, "PULL_BASE_SHA is not a 40-hex SHA"),
            ("multi-line head", {"PULL_PULL_SHA": f"{self.c4}\n--output=/dev/null"}, "PULL_NUMBER or PULL_PULL_SHA is not"),
            ("multi-line number", {"PULL_NUMBER": f"{_PR}\n77"}, "PULL_NUMBER or PULL_PULL_SHA is not"),
        )
        for name, overrides, reason in cases:
            with self.subTest(name):
                proc = self._run(cur_head=self.c4, cur_base=self.c2, env_overrides=overrides)
                self.assertIn("VERDICT: FULL-RUN", proc.stdout)
                self.assertIn(reason, proc.stdout)
                self.assertFalse(self.call_log.exists(), "gsutil was called on a malformed SHA")

    def test_a_started_json_entry_with_a_ref_suffix_is_read_the_same_way(self):
        """started.json's repos value is the same Refs.String() shape as
        PULL_REFS, so a ":<ref>" third field there must not turn the head
        into "<sha>:<ref>" and fail the 40-hex check as a malformed SHA."""
        self._plant_history([("200", True, self.c1, self.c3)])
        (self.objects / "200.started.json").write_text(
            '{"repos": {"gke-labs/kube-agents": "main:%s,%s:%s:refs/heads/topic"}}'
            % (self.c1, _PR, self.c3)
        )
        proc = self._run(cur_head=self.c3, cur_base=self.c5)
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertNotIn("malformed SHA", proc.stdout)

    def test_a_batch_entry_with_a_ref_suffix_is_accepted(self):
        """Prow appends ":<ref>" to a pull entry when it knows the branch."""
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(
            cur_head="",
            cur_base=self.c5,
            env_overrides={
                "PULL_NUMBER": None,
                "PULL_PULL_SHA": None,
                "JOB_TYPE": "batch",
                "PULL_REFS": f"main:{self.c5},{_PR}:{self.c3}:refs/heads/topic",
            },
        )
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)

    def test_pull_refs_without_the_batch_job_type_is_not_a_batch(self):
        """A serial presubmit also carries PULL_REFS; only JOB_TYPE=batch
        turns it into the list of pulls, and a serial env missing its own
        PULL_NUMBER stays a full run."""
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(
            cur_head="",
            cur_base=self.c5,
            env_overrides={
                "PULL_NUMBER": None,
                "PULL_PULL_SHA": None,
                "JOB_TYPE": "presubmit",
                "PULL_REFS": f"main:{self.c5},{_PR}:{self.c3}",
            },
        )
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("not a decorated Prow presubmit or batch", proc.stdout)

    def test_the_newest_green_wins_and_the_sort_is_numeric(self):
        """Build 90 sorts after 1000 lexicographically; picking it here would
        compare against records whose deltas are NOT inert and run full."""
        self._plant_history(
            [
                ("2000", False, self.c1, self.c3),  # newest, red: skipped over
                ("1000", True, self.c1, self.c3),  # the build to reuse
                ("90", True, self.c1, self.c1),  # lexicographic trap: head delta c1..c4 stays inert,
            ]
        )
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertRegex(proc.stdout, r"REVALIDATED against green build 1000\b")

    # ── every fall-through path runs full ────────────────────────────────────

    def test_no_history_is_a_full_run(self):
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("first run on this PR, or GCS unreadable", proc.stdout)

    def test_no_green_build_is_a_full_run(self):
        self._plant_history([("200", False, self.c1, self.c3)])
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("no green build", proc.stdout)

    def test_an_unparsable_finished_json_is_a_full_run(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        (self.objects / "200.finished.json").write_text("not json at all")
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)

    def test_a_started_json_without_the_shas_is_a_full_run(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        (self.objects / "200.started.json").write_text('{"repos": {}}')
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("could not recover base/head SHAs", proc.stdout)

    def test_a_missing_started_json_is_a_full_run(self):
        self._plant_history([("200", True, None, None)])
        (self.objects / "200.finished.json").write_text('{"passed": true}')
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("no readable started.json", proc.stdout)

    def test_a_non_inert_file_in_the_head_delta_is_a_full_run(self):
        # prev_head c1 -> cur_head c5 touches code.py alongside inert files.
        self._plant_history([("200", True, self.c2, self.c1)])
        proc = self._run(cur_head=self.c5, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("code.py", proc.stdout)

    def test_a_non_inert_file_in_the_base_delta_is_a_full_run(self):
        # Head side inert (c3 -> c4 adds docs/b.md); main moved c1 -> c5,
        # which touches code.py. Only a NEW head reaches the base-delta rule:
        # the same head is reused whatever main did, tested above.
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(cur_head=self.c4, cur_base=self.c5)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("code.py", proc.stdout)

    def test_the_inert_regex_is_root_anchored(self):
        """docs-evil.go must not ride the docs/ branch, a .md below the root
        is prompt content, and bench/OWNERS is not the root OWNERS file."""
        self._plant_history([("200", True, self.c2, self.c4)])
        proc = self._run(cur_head=self.c6, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        for survivor in ("docs-evil.go", "sub/notes.md", "bench/OWNERS"):
            self.assertIn(survivor, proc.stdout)

    def test_a_rename_to_an_inert_path_is_a_full_run(self):
        """`git mv code.py docs/moved.md` deletes non-inert content. With
        rename detection on, the diff would list only the inert destination
        and the deletion would ride a reused green -- the --no-renames flag
        is what keeps the source path visible to the predicate."""
        self._plant_history([("200", True, self.c2, self.c6)])
        proc = self._run(cur_head=self.c7, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("code.py", proc.stdout)

    def test_a_forged_green_record_without_a_github_status_is_a_full_run(self):
        """The cross-PR forgery from kube-agents-bot's review: a fabricated
        finished.json/started.json pair under this PR's history path, with no
        Prow-posted success status behind it, must not be trusted."""
        self._plant_history([("9999999999999999999", True, self.c2, self.c4)], attest=False)
        # GitHub answers 200 with an empty list for a commit that has no
        # status events; an unreadable statuses endpoint is a separate
        # fail-closed path with its own reason line.
        self._plant_statuses(self.c4, [])
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("refusing to trust the GCS record alone", proc.stdout)

    def test_a_status_for_a_different_build_does_not_attest_this_one(self):
        """A success status exists on the head, but its target URL names
        another build -- the forged record cannot borrow it."""
        self._plant_history([("200", True, self.c2, self.c4)], attest=False)
        self._plant_statuses(
            self.c4,
            [
                {
                    "context": _JOB,
                    "state": "success",
                    "target_url": f"https://oss.gprow.dev/view/gs/kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/{_PR}/{_JOB}/111",
                },
                {"context": _JOB, "state": "pending", "target_url": None},
            ],
        )
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("refusing to trust the GCS record alone", proc.stdout)

    def test_a_malformed_sha_is_a_full_run(self):
        """A forged started.json must not be able to hand git anything but a
        full-length commit id -- '--flag' smuggling dies here."""
        self._plant_history([("200", True, self.c1, self.c3)])
        (self.objects / "200.started.json").write_text(
            '{"repos": {"gke-labs/kube-agents": "main:%s,%s:--upload-pack=/tmp/evil"}}'
            % (self.c1, _PR)
        )
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("malformed SHA", proc.stdout)

    def test_disagreeing_finished_and_started_records_are_a_full_run(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        (self.objects / "200.finished.json").write_text(
            '{"passed": true, "result": "SUCCESS", "revision": "%s"}' % self.c5
        )
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("does not match its started.json head", proc.stdout)

    def test_a_missing_git_object_is_a_full_run(self):
        ghost = "deadbeef" * 5
        self._plant_history([("200", True, self.c1, ghost)])
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("not in this checkout", proc.stdout)

    def test_the_escape_hatch_forces_a_full_run(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(
            cur_head=self.c4,
            cur_base=self.c2,
            env_overrides={"EVAL_SKIP_REVALIDATION": "1"},
        )
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("escape hatch", proc.stdout)

    def test_outside_a_decorated_presubmit_is_a_full_run(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(
            cur_head=self.c4, cur_base=self.c2, env_overrides={"PULL_NUMBER": ""}
        )
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("not a decorated Prow presubmit", proc.stdout)


    # ── the read credential ──────────────────────────────────────────────────

    def test_the_app_key_mints_the_read_token_and_the_pat_is_never_sent(self):
        """With the key file set the status read carries a token minted from
        it, narrowed to metadata: read, and the PAT the pod also mounts is
        on no request -- the harness's own rule, now step 0's."""
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(
            cur_head=self.c4,
            cur_base=self.c2,
            env_overrides={
                "EVAL_LEDGER_APP_KEY_FILE": str(self._throwaway_key()),
                "BENCH_GITHUB_TOKEN": "the-mounted-pat",
            },
        )
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertIn("GitHub statuses read with a token minted from the App key", proc.stdout)
        mints = [r for r in self._requests() if "/access_tokens" in r["url"]]
        reads = [r for r in self._requests() if "/statuses" in r["url"]]
        self.assertEqual(1, len(mints), self._requests())
        self.assertEqual({"permissions": {"metadata": "read"}}, json.loads(mints[0]["data"]))
        self.assertIn("/app/installations/157029058/access_tokens", mints[0]["url"])
        self.assertEqual(["Bearer ghs_minted"], [r["authorization"] for r in reads])
        self.assertNotIn("the-mounted-pat", proc.stdout + proc.stderr + self.requests_log.read_text())

    def test_a_batch_mints_once_for_all_its_pulls(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        self._plant_history([("300", True, self.c1, self.c4)], pr=_OTHER_PR)
        proc = self._run(
            cur_head=self.c3,
            cur_base=self.c1,
            env_overrides={
                "JOB_TYPE": "batch",
                "PULL_NUMBER": None,
                "PULL_PULL_SHA": None,
                "PULL_REFS": f"main:{self.c1},{_PR}:{self.c3},{_OTHER_PR}:{self.c4}",
                "EVAL_LEDGER_APP_KEY_FILE": str(self._throwaway_key()),
            },
        )
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertEqual(1, len([r for r in self._requests() if "/access_tokens" in r["url"]]))
        self.assertEqual(2, len([r for r in self._requests() if "/statuses" in r["url"]]))

    def test_a_failed_mint_is_a_full_run_with_no_anonymous_read(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(
            cur_head=self.c4,
            cur_base=self.c2,
            env_overrides={
                "EVAL_LEDGER_APP_KEY_FILE": str(self._throwaway_key()),
                "GITHUB_FAKE_MINT_HTTP": "401",
                "BENCH_GITHUB_TOKEN": "the-mounted-pat",
            },
        )
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("Step 0: full run: could not mint a GitHub read token", proc.stdout)
        self.assertEqual(1, proc.stdout.count("Step 0: full run:"), proc.stdout)
        # The mint's own diagnostic precedes the reason line, on stderr.
        self.assertIn("HTTP 401", proc.stderr)
        self.assertEqual([], [r for r in self._requests() if "/statuses" in r["url"]])

    def test_without_a_key_the_shells_token_is_used_and_said_so(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(cur_head=self.c4, cur_base=self.c2, env_overrides={"BENCH_GITHUB_TOKEN": "shell-token"})
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertIn("read with the BENCH_GITHUB_TOKEN this shell holds", proc.stdout)
        self.assertEqual(["Bearer shell-token"], [r["authorization"] for r in self._requests()])

    def test_without_any_credential_the_read_is_anonymous_and_said_so(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(cur_head=self.c4, cur_base=self.c2)
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertIn("GitHub statuses read anonymously", proc.stdout)
        self.assertEqual([None], [r["authorization"] for r in self._requests()])

    def test_a_refused_status_read_is_a_full_run_with_no_second_attempt(self):
        """What the old curl pair did on any failure was try again
        anonymously, in silence; a refused read now names its code and is
        tried once."""
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(
            cur_head=self.c4,
            cur_base=self.c2,
            env_overrides={"BENCH_GITHUB_TOKEN": "shell-token", "GITHUB_FAKE_STATUS_HTTP": "403"},
        )
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("GitHub answered HTTP 403 reading statuses", proc.stdout)
        self.assertIn("not retrying anonymously", proc.stdout)
        self.assertEqual(1, len([r for r in self._requests() if "/statuses" in r["url"]]))

    def test_the_token_reaches_python_through_the_environment_not_argv(self):
        """ps shows argv to every process on the node; it does not show the
        environment. The read's python takes the token from there, and no
        shell line builds an Authorization header."""
        text = _CI_REVALIDATE.read_text(encoding="utf-8")
        self.assertIn('os.environ.get("REVALIDATION_STATUS_TOKEN")', text)
        self.assertNotIn("Authorization: Bearer ${", text)
        self.assertNotIn("curl", text)


class RevalidationEntrypointTest(unittest.TestCase):
    """The executed script, not the sourced function: both callers (the Prow
    job ahead of the lease, and hack/ci-eval-pr.sh) read only its exit code,
    so the tail that maps the function's verdict onto it is what ships.
    Borrows the fixture above without inheriting its tests."""

    setUp = RevalidationTest.setUp
    _git = RevalidationTest._git
    _commit = RevalidationTest._commit
    _plant_history = RevalidationTest._plant_history
    _plant_statuses = RevalidationTest._plant_statuses
    _NEUTRALISED = RevalidationTest._NEUTRALISED
    _install_script = RevalidationTest._install_script
    _requests = RevalidationTest._requests
    _base_env = RevalidationTest._base_env
    _child_env = RevalidationTest._child_env

    def _execute(self, env_overrides):
        copy = self._install_script()
        env = self._base_env(self.c3, self.c5)
        env.update(env_overrides)
        return subprocess.run(["bash", str(copy)], capture_output=True, text=True, env=self._child_env(env))

    def test_executed_env_is_isolated_from_the_developers_shell(self):
        """The shell the live validation used exports a batch's JOB_TYPE and
        PULL_REFS, and an operator's may export the escape hatch; none may
        reach the script under test, or the reuse test reds in exactly that
        shell."""
        self._plant_history([("200", True, self.c1, self.c3)])
        leaked = {"JOB_TYPE": "batch", "PULL_REFS": f"main:{self.c1},{_PR}:{self.c3}", "EVAL_SKIP_REVALIDATION": "1"}
        with unittest.mock.patch.dict(os.environ, leaked):
            proc = self._execute({})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("escape hatch", proc.stdout)
        self.assertNotIn("batch of", proc.stdout)

    def test_executed_a_reuse_exits_zero(self):
        self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._execute({})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"REVALIDATED against green build 200\b")

    def test_executed_a_fall_through_exits_one_with_one_reason_line(self):
        for name, overrides in (
            ("no history", {}),
            ("escape hatch", {"EVAL_SKIP_REVALIDATION": "1"}),
            ("no Prow env", {"PULL_NUMBER": None, "PULL_PULL_SHA": None, "PULL_BASE_SHA": None}),
        ):
            with self.subTest(name):
                proc = self._execute(overrides)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout.count("Step 0: full run:"), 1, proc.stdout)


class RevalidationPlacementTest(unittest.TestCase):
    def test_step0_runs_before_anything_expensive_or_stateful(self):
        """The whole point is exiting before cluster work; a later invocation
        would pay for auth, fleet kubeconfigs and token mints first."""
        text = _CI_EVAL_PR.read_text(encoding="utf-8")
        invocation = text.index('if bash "${REVALIDATION_SCRIPT}"; then')
        self.assertLess(invocation, text.index('source "${SCRIPT_DIR}/ci-env.sh"'))
        self.assertLess(invocation, text.index("gcloud container clusters get-credentials"))
        self.assertLess(invocation, text.index("trap profile_and_dump_on_exit EXIT"))


if __name__ == "__main__":
    unittest.main()

"""Tests for step 0 of hack/ci-eval-pr.sh: self-revalidation against green history.

Step 0 may skip the whole eval matrix, so the property that matters most is
that it fails CLOSED: the ONLY path that exits early is a prior green build of
this PR's own job plus head- and base-deltas that both match the inert-path
list. Everything else -- no history, unreadable or unparsable records, a
commit the checkout does not have, a single non-inert file on either side,
the escape hatch -- must fall through to a full run.

The function and its constants are extracted from the script and executed
with `gsutil` stubbed and a fixture git repository standing in for the
decorated checkout, so these assertions are against the code that ships. The
REVALIDATED log line's shape is pinned because humans grep build logs for it,
and so that any future dashboard-collector support has a stable line to key
on (scripts/eval_dashboard/collect.py reads nothing from it today).
"""

import pathlib
import re
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL_PR = _REPO_ROOT / "hack" / "ci-eval-pr.sh"

_PR = "77"
_JOB = "pull-kube-agents-smoke-test"

_GSUTIL_STUB = """#!/usr/bin/env bash
# gsutil stub: `ls` prints the fixture listing (or fails like a no-match
# glob), `cat` serves "<build>.<file>" out of GSUTIL_OBJECT_DIR. Every call
# is appended to GSUTIL_CALL_LOG so a test can pin WHICH history the script
# read -- reading another PR's (or another job's) records would reuse a
# foreign verdict while every content assertion still passed.
cmd="$1"; shift
if [ -n "${GSUTIL_CALL_LOG:-}" ]; then
  echo "${cmd} $*" >> "${GSUTIL_CALL_LOG}"
fi
case "${cmd}" in
  ls)
    if [ -n "${GSUTIL_LS_FILE:-}" ] && [ -f "${GSUTIL_LS_FILE}" ]; then
      cat "${GSUTIL_LS_FILE}"
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


def _extract(pattern, what):
    text = _CI_EVAL_PR.read_text(encoding="utf-8")
    match = re.search(pattern, text, re.S | re.M)
    assert match, f"could not find {what} in hack/ci-eval-pr.sh"
    return match.group(0)


def _extract_constants():
    text = _CI_EVAL_PR.read_text(encoding="utf-8")
    lines = re.findall(r"^readonly REVALIDATION_\w+=.*$", text, re.M)
    assert lines, "no readonly REVALIDATION_* constants in hack/ci-eval-pr.sh"
    return "\n".join(lines)


class RevalidationTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)

        # The stub gsutil, first on PATH.
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        stub = self.bin / "gsutil"
        stub.write_text(_GSUTIL_STUB)
        stub.chmod(0o755)

        self.objects = self.tmp / "objects"
        self.objects.mkdir()

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

    def _plant_history(self, builds):
        """builds: [(build_id, passed, base_sha, head_sha)], any record None to omit."""
        listing = []
        for build_id, passed, base_sha, head_sha in builds:
            listing.append(
                f"gs://kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/{_PR}/{_JOB}/{build_id}/finished.json"
            )
            if passed is not None:
                (self.objects / f"{build_id}.finished.json").write_text(
                    '{"passed": %s, "result": "%s"}'
                    % ("true" if passed else "false", "SUCCESS" if passed else "FAILURE")
                )
            if base_sha is not None:
                (self.objects / f"{build_id}.started.json").write_text(
                    '{"repos": {"gke-labs/kube-agents": "main:%s,%s:%s"}}'
                    % (base_sha, _PR, head_sha)
                )
        ls_file = self.tmp / "ls.txt"
        ls_file.write_text("\n".join(listing) + "\n")
        return ls_file

    def _run(self, cur_head, cur_base, ls_file=None, env_overrides=None):
        # Written into the fixture repo's hack/ so the function's own
        # BASH_SOURCE-derived repo_dir points at the fixture checkout, the
        # same way it points at the real one in the pod.
        script = "\n".join(
            [
                "set -euo pipefail",
                _extract_constants(),
                _extract(
                    r"^_revalidation_print_delta\(\) \{ # <label> <range> <files-or-empty>\n.*?^\}$",
                    "_revalidation_print_delta",
                ),
                _extract(
                    r"^revalidate_against_green_history\(\) \{\n.*?^\}$",
                    "revalidate_against_green_history",
                ),
                "if revalidate_against_green_history; then",
                '  echo "VERDICT: REVALIDATED-EXIT"',
                "  exit 0",
                "fi",
                'echo "VERDICT: FULL-RUN"',
            ]
        )
        under_test = self.repo / "hack" / "step0_under_test.sh"
        under_test.write_text(script)
        self.call_log = self.tmp / "gsutil.calls"
        env = {
            "PULL_NUMBER": _PR,
            "PULL_PULL_SHA": cur_head,
            "PULL_BASE_SHA": cur_base,
            "PULL_BASE_REF": "main",
            "GSUTIL_OBJECT_DIR": str(self.objects),
            "GSUTIL_CALL_LOG": str(self.call_log),
        }
        if ls_file is not None:
            env["GSUTIL_LS_FILE"] = str(ls_file)
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            ["bash", str(under_test)],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=env, bin_dir=self.bin),
        )

    # ── the one path that skips ──────────────────────────────────────────────

    def test_green_history_plus_inert_deltas_reuses_the_verdict(self):
        ls = self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(cur_head=self.c4, cur_base=self.c2, ls_file=ls)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        # Both delta file lists and the predicate are in the log.
        self.assertIn("docs/b.md", proc.stdout)
        self.assertIn("docs/a.md", proc.stdout)
        self.assertIn("REVALIDATION_INERT_PATHS", proc.stdout)

    def test_the_revalidated_log_line_shape_is_pinned(self):
        """Humans grep build logs for this line (and future collector support
        needs a stable line to key on); the word REVALIDATED and the reused
        build id must appear together, and the Spyglass URL must follow."""
        ls = self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(cur_head=self.c4, cur_base=self.c2, ls_file=ls)
        self.assertRegex(
            proc.stdout, r"Step 0: REVALIDATED against green build 200\b"
        )
        self.assertIn(
            "Reused verdict: https://oss.gprow.dev/view/gs/kube-agents-prow/"
            f"pr-logs/pull/gke-labs_kube-agents/{_PR}/{_JOB}/200",
            proc.stdout,
        )

    def test_the_history_read_is_scoped_to_this_prs_own_job(self):
        """A wrong PR number, job name or bucket in the history path would
        reuse a FOREIGN verdict while every content assertion still passed;
        the URLs the script hands gsutil are the load-bearing part."""
        ls = self._plant_history([("200", True, self.c1, self.c3)])
        self._run(cur_head=self.c4, cur_base=self.c2, ls_file=ls)
        calls = self.call_log.read_text().splitlines()
        prefix = f"gs://kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/{_PR}/{_JOB}"
        self.assertIn(f"ls {prefix}/*/finished.json", calls)
        self.assertIn(f"cat {prefix}/200/finished.json", calls)
        self.assertIn(f"cat {prefix}/200/started.json", calls)

    def test_identical_shas_are_trivially_inert(self):
        """An empty delta means that side's tree is byte-identical to the one
        the green verdict graded -- reuse is correct, not an edge case."""
        ls = self._plant_history([("200", True, self.c2, self.c4)])
        proc = self._run(cur_head=self.c4, cur_base=self.c2, ls_file=ls)
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertIn("trivially inert", proc.stdout)

    def test_the_newest_green_wins_and_the_sort_is_numeric(self):
        """Build 90 sorts after 1000 lexicographically; picking it here would
        compare against records whose deltas are NOT inert and run full."""
        ls = self._plant_history(
            [
                ("2000", False, self.c1, self.c3),  # newest, red: skipped over
                ("1000", True, self.c1, self.c3),  # the build to reuse
                ("90", True, self.c1, self.c1),  # lexicographic trap: head delta c1..c4 stays inert,
            ]
        )
        proc = self._run(cur_head=self.c4, cur_base=self.c2, ls_file=ls)
        self.assertIn("VERDICT: REVALIDATED-EXIT", proc.stdout)
        self.assertRegex(proc.stdout, r"REVALIDATED against green build 1000\b")

    # ── every fall-through path runs full ────────────────────────────────────

    def test_no_history_is_a_full_run(self):
        proc = self._run(cur_head=self.c4, cur_base=self.c2, ls_file=None)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("first run on this PR, or GCS unreadable", proc.stdout)

    def test_no_green_build_is_a_full_run(self):
        ls = self._plant_history([("200", False, self.c1, self.c3)])
        proc = self._run(cur_head=self.c4, cur_base=self.c2, ls_file=ls)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("no green build", proc.stdout)

    def test_an_unparsable_finished_json_is_a_full_run(self):
        ls = self._plant_history([("200", True, self.c1, self.c3)])
        (self.objects / "200.finished.json").write_text("not json at all")
        proc = self._run(cur_head=self.c4, cur_base=self.c2, ls_file=ls)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)

    def test_a_started_json_without_the_shas_is_a_full_run(self):
        ls = self._plant_history([("200", True, self.c1, self.c3)])
        (self.objects / "200.started.json").write_text('{"repos": {}}')
        proc = self._run(cur_head=self.c4, cur_base=self.c2, ls_file=ls)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("could not recover base/head SHAs", proc.stdout)

    def test_a_missing_started_json_is_a_full_run(self):
        ls = self._plant_history([("200", True, None, None)])
        (self.objects / "200.finished.json").write_text('{"passed": true}')
        proc = self._run(cur_head=self.c4, cur_base=self.c2, ls_file=ls)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("no readable started.json", proc.stdout)

    def test_a_non_inert_file_in_the_head_delta_is_a_full_run(self):
        # prev_head c1 -> cur_head c5 touches code.py alongside inert files.
        ls = self._plant_history([("200", True, self.c2, self.c1)])
        proc = self._run(cur_head=self.c5, cur_base=self.c2, ls_file=ls)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("code.py", proc.stdout)

    def test_a_non_inert_file_in_the_base_delta_is_a_full_run(self):
        # Head side identical; main moved c1 -> c5, which touches code.py.
        ls = self._plant_history([("200", True, self.c1, self.c4)])
        proc = self._run(cur_head=self.c4, cur_base=self.c5, ls_file=ls)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("code.py", proc.stdout)

    def test_the_inert_regex_is_root_anchored(self):
        """docs-evil.go must not ride the docs/ branch, a .md below the root
        is prompt content, and bench/OWNERS is not the root OWNERS file."""
        ls = self._plant_history([("200", True, self.c2, self.c4)])
        proc = self._run(cur_head=self.c6, cur_base=self.c2, ls_file=ls)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        for survivor in ("docs-evil.go", "sub/notes.md", "bench/OWNERS"):
            self.assertIn(survivor, proc.stdout)

    def test_a_rename_to_an_inert_path_is_a_full_run(self):
        """`git mv code.py docs/moved.md` deletes non-inert content. With
        rename detection on, the diff would list only the inert destination
        and the deletion would ride a reused green -- the --no-renames flag
        is what keeps the source path visible to the predicate."""
        ls = self._plant_history([("200", True, self.c2, self.c6)])
        proc = self._run(cur_head=self.c7, cur_base=self.c2, ls_file=ls)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("code.py", proc.stdout)

    def test_a_missing_git_object_is_a_full_run(self):
        ghost = "deadbeef" * 5
        ls = self._plant_history([("200", True, self.c1, ghost)])
        proc = self._run(cur_head=self.c4, cur_base=self.c2, ls_file=ls)
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("not in this checkout", proc.stdout)

    def test_the_escape_hatch_forces_a_full_run(self):
        ls = self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(
            cur_head=self.c4,
            cur_base=self.c2,
            ls_file=ls,
            env_overrides={"EVAL_SKIP_REVALIDATION": "1"},
        )
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("escape hatch", proc.stdout)

    def test_outside_a_decorated_presubmit_is_a_full_run(self):
        ls = self._plant_history([("200", True, self.c1, self.c3)])
        proc = self._run(
            cur_head=self.c4, cur_base=self.c2, ls_file=ls, env_overrides={"PULL_NUMBER": ""}
        )
        self.assertIn("VERDICT: FULL-RUN", proc.stdout)
        self.assertIn("not a decorated Prow presubmit", proc.stdout)


class RevalidationPlacementTest(unittest.TestCase):
    def test_step0_runs_before_anything_expensive_or_stateful(self):
        """The whole point is exiting before cluster work; a later invocation
        would pay for auth, fleet kubeconfigs and token mints first."""
        text = _CI_EVAL_PR.read_text(encoding="utf-8")
        invocation = text.index("if revalidate_against_green_history; then")
        self.assertLess(invocation, text.index('source "${SCRIPT_DIR}/ci-env.sh"'))
        self.assertLess(invocation, text.index("gcloud container clusters get-credentials"))
        self.assertLess(invocation, text.index("trap profile_and_dump_on_exit EXIT"))


if __name__ == "__main__":
    unittest.main()

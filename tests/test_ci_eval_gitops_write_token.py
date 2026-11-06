"""Tests for the GitOps write token hack/ci-eval-pr.sh gives a writing stack.

A stack that declares the marker `variable "bench_gitops_write_token_file"`
plants a file in the leased GitOps repository at apply and removes it at
destroy. run_one_unit mints it the ledger App's token, narrowed to that one
repository and to contents, pull requests and issues write, writes it to a
0600 file, exports the file's name as TF_VAR_bench_gitops_write_token_file,
renews it while devops-bench runs, and removes it after. Only such a unit
also gets TF_VAR_gitops_repo and the TF_VAR_kcc_* inputs; no other stack
sees a new TF_VAR_. These run the real functions with the mint stubbed.
"""

import json
import pathlib
import re
import subprocess
import tempfile
import textwrap
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL_PR = _REPO_ROOT / "hack" / "ci-eval-pr.sh"
_REPO = "gke-agentic/kube-agents-evals-2-infra"


def _extract(pattern, what):
    text = _CI_EVAL_PR.read_text(encoding="utf-8")
    match = re.search(pattern, text, re.S | re.M)
    assert match, f"could not find {what} in hack/ci-eval-pr.sh"
    return match.group(0)


def _function(name):
    return _extract(rf"^{name}\(\) \{{.*?^\}}", name)


def _constant(name):
    return _extract(rf"^{name}=[^\n]*$", name)


class _Harness(unittest.TestCase):
    """The extracted functions, with the mint stubbed."""

    maxDiff = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.key = self.tmp / "ledger.pem"
        self.key.write_text("not a real key -- the mint itself is stubbed\n")

    def _run(self, body, **env):
        script = "\n".join(
            [
                "set -euo pipefail",
                _constant("LEDGER_MINT_RETRYABLE"),
                _constant("LEDGER_RESET_MINT_ATTEMPTS"),
                _constant("LEDGER_RESET_MINT_RETRY_DELAY"),
                _constant("AGENT_PULLS_RESET_PERMISSIONS"),
                _constant("BENCH_GITOPS_WRITE_VARIABLE"),
                _constant("BENCH_GITOPS_WRITE_MARKER_RE"),
                _constant("KCC_FLEET_SLOT"),
                _constant("KCC_EVAL_CONTEXT_ACCOUNT"),
                _function("fleet_context_value"),
                _function("export_writing_stack_inputs"),
                _function("unset_writing_stack_inputs"),
                _function("task_stack"),
                _function("ledger_reset_token"),
                _function("forge_write_token"),
                _function("stack_writes_gitops"),
                _function("start_gitops_write_token"),
                _function("stop_gitops_write_token"),
                # Seconds rather than forty minutes, so the renewal is observable.
                "LEDGER_TOKEN_RENEW_SECONDS=1",
                "LEDGER_TOKEN_RENEW_TICK_SECONDS=1",
                "LEDGER_TOKEN_RENEW_RETRY_SECONDS=1",
                "sleep() { command sleep 0.2; }",
                # Records each body it is asked for; answers by MINT, and
                # numbers the tokens so a renewal shows as a new one.
                'echo 0 > "${COUNT_FILE}"',
                "_ledger_token_mint() {",
                '  printf "%s\\n" "${LEDGER_MINT_BODY}" >> "${BODY_FILE}"',
                '  local n=$(( $(cat "${COUNT_FILE}") + 1 ))',
                '  echo "${n}" > "${COUNT_FILE}"',
                '  case "${MINT:-ok}" in',
                '    ok) echo "tok-write-${n} 2026-11-03T18:00:00Z" ;;',
                '    transient) echo "GitHub answered HTTP 500 minting" >&2; return "${LEDGER_MINT_RETRYABLE}" ;;',
                '    *) echo "GitHub answered HTTP 422 minting" >&2; return 1 ;;',
                "  esac",
                "}",
                textwrap.dedent(body),
            ]
        )
        overrides = {
            "EVAL_LEDGER_APP_KEY_FILE": str(self.key),
            "EVAL_LEDGER_APP_ID": "4739812",
            "EVAL_LEDGER_REPO": _REPO,
            "PROJECT_ID": "kube-agents-evals-2",
            "BENCH_DIR": str(self.tmp),
            "TOKEN_FILE": str(self.tmp / "unit.gitops-token"),
            "COUNT_FILE": str(self.tmp / "mint-count"),
            "BODY_FILE": str(self.tmp / "mint-bodies"),
        }
        overrides.update(env)
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            env=get_isolated_test_env(overrides=overrides),
        )

    def _bodies(self):
        path = self.tmp / "mint-bodies"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []



class GitopsWriteTokenTest(_Harness):
    def test_the_token_is_narrowed_written_exported_renewed_and_removed(self):
        proc = self._run(
            """\
            rc=0
            start_gitops_write_token "case rep 1" "${TOKEN_FILE}" || rc=$?
            echo "RC=${rc}"
            echo "EXPORTED=${TF_VAR_bench_gitops_write_token_file}"
            echo "AT_LAUNCH=$(cat "${TOKEN_FILE}")"
            echo "MODE=$(stat -c %a "${TOKEN_FILE}" 2>/dev/null || stat -f %Lp "${TOKEN_FILE}")"
            for _ in $(seq 1 50); do grep -q tok-write-2 "${TOKEN_FILE}" && break; command sleep 0.2; done
            echo "RENEWED=$(cat "${TOKEN_FILE}")"
            renewer="${GITOPS_WRITE_TOKEN_RENEWER}"
            stop_gitops_write_token "${TOKEN_FILE}"
            kill -0 "${renewer}" 2>/dev/null && echo "RENEWER=alive" || echo "RENEWER=gone"
            [ -e "${TOKEN_FILE}" ] && echo "FILE=left" || echo "FILE=removed"
            echo "UNSET=${TF_VAR_bench_gitops_write_token_file:-}"
            """
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        out = proc.stdout
        self.assertIn("RC=0", out)
        self.assertIn(f"EXPORTED={self.tmp / 'unit.gitops-token'}", out)
        self.assertIn("AT_LAUNCH=tok-write-1", out)
        self.assertIn("MODE=600", out)
        self.assertIn("RENEWED=tok-write-2", out)
        self.assertIn("RENEWER=gone", out)
        self.assertIn("FILE=removed", out)
        self.assertIn("UNSET=\n", out + "\n")
        self.assertNotIn("tok-write", proc.stderr, "the token is never printed")
        self.assertNotIn("tok-write-1", out.split("AT_LAUNCH=")[0], "the token is never printed")
        body = self._bodies()[0]
        self.assertEqual(body["repositories"], ["kube-agents-evals-2-infra"])
        self.assertEqual(body["permissions"], {"pull_requests": "write", "contents": "write", "issues": "write"})

    def test_a_refused_mint_writes_nothing_and_says_so(self):
        proc = self._run(
            """\
            rc=0
            start_gitops_write_token "case rep 1" "${TOKEN_FILE}" || rc=$?
            echo "RC=${rc}"
            echo "EXPORTED=${TF_VAR_bench_gitops_write_token_file:-}"
            [ -e "${TOKEN_FILE}" ] && echo "FILE=written" || echo "FILE=none"
            """,
            MINT="refused",
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("RC=1", proc.stdout)
        self.assertIn("EXPORTED=\n", proc.stdout)
        self.assertIn("FILE=none", proc.stdout)
        self.assertIn("A 422 above means", proc.stderr)

    def test_a_transient_mint_returns_the_retryable_code_and_its_last_line(self):
        proc = self._run(
            """\
            rc=0
            start_gitops_write_token "case rep 1" "${TOKEN_FILE}" || rc=$?
            echo "RC=${rc}"
            echo "LAST=${GITOPS_WRITE_TOKEN_LAST_FAILURE}"
            """,
            MINT="transient",
        )
        retryable = _constant("LEDGER_MINT_RETRYABLE").split("=", 1)[1]
        self.assertIn(f"RC={retryable}", proc.stdout)
        self.assertIn("LAST=GitHub answered HTTP 500 minting", proc.stdout)

    def test_without_the_app_key_a_token_file_the_environment_names_is_kept(self):
        own = self.tmp / "mine"
        own.write_text("a developer's token\n")
        proc = self._run(
            """\
            rc=0
            start_gitops_write_token "case rep 1" "${TOKEN_FILE}" || rc=$?
            echo "RC=${rc}"
            stop_gitops_write_token "${TOKEN_FILE}"
            echo "STILL=${TF_VAR_bench_gitops_write_token_file}"
            [ -e "${TF_VAR_bench_gitops_write_token_file}" ] && echo "OWN=kept" || echo "OWN=removed"
            """,
            EVAL_LEDGER_APP_KEY_FILE="",
            TF_VAR_bench_gitops_write_token_file=str(own),
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("RC=0", proc.stdout)
        self.assertIn(f"STILL={own}", proc.stdout)
        self.assertIn("OWN=kept", proc.stdout)
        self.assertEqual(self._bodies(), [])

    def test_without_the_app_key_or_a_file_the_unit_does_not_run(self):
        proc = self._run(
            """\
            rc=0
            start_gitops_write_token "case rep 1" "${TOKEN_FILE}" || rc=$?
            echo "RC=${rc}"
            """,
            EVAL_LEDGER_APP_KEY_FILE="",
        )
        self.assertIn("RC=1", proc.stdout)
        self.assertIn("has no token to write", proc.stderr)

    def test_a_gitlab_run_does_not_mint_for_a_github_stack(self):
        proc = self._run(
            """\
            rc=0
            start_gitops_write_token "case rep 1" "${TOKEN_FILE}" || rc=$?
            echo "RC=${rc}"
            """,
            EVAL_FORGE_KIND="gitlab",
        )
        self.assertIn("RC=1", proc.stdout)
        self.assertEqual(self._bodies(), [])

    def _task(self, name, stack, variables):
        task = self.tmp / "tasks" / name / "task.yaml"
        task.parent.mkdir(parents=True)
        task.write_text(f'infrastructure:\n  deployer: "tofu"\n  stack: "{stack}"\n' if stack else "infrastructure:\n  deployer: noop\n")
        if stack:
            (self.tmp / "tf" / stack).mkdir(parents=True, exist_ok=True)
            (self.tmp / "tf" / stack / "variables.tf").write_text(variables)
        return f"./tasks/{name}/task.yaml"

    def test_only_a_stack_that_declares_the_variable_writes(self):
        writer = self._task("writer", "prebuilt/writer", 'variable "bench_gitops_write_token_file" {\n  type = string\n}\n')
        reader = self._task("reader", "prebuilt/reader", 'variable "gitops_repo" {\n  type = string\n}\n')
        # gitops-fix-cycle's own variable, with another contract: not the marker.
        other = self._task("other", "prebuilt/other", 'variable "gitops_token_file" {\n  type = string\n}\n')
        # A comment that quotes the marker declares nothing.
        quoted = self._task("quoted", "prebuilt/quoted", '# Unlike a writing stack, this one has no\n# variable "bench_gitops_write_token_file" {}\nvariable "gitops_repo" {\n  type = string\n}\n')
        # An indented declaration still counts.
        indented = self._task("indented", "prebuilt/indented", '  variable  "bench_gitops_write_token_file"{\n  type = string\n}\n')
        noop = self._task("noop", "", "")
        proc = self._run(
            f"""\
            stack_writes_gitops "{writer}" && echo "WRITER=yes" || echo "WRITER=no"
            stack_writes_gitops "{reader}" && echo "READER=yes" || echo "READER=no"
            stack_writes_gitops "{noop}" && echo "NOOP=yes" || echo "NOOP=no"
            stack_writes_gitops "{other}" && echo "OTHER=yes" || echo "OTHER=no"
            stack_writes_gitops "{quoted}" && echo "QUOTED=yes" || echo "QUOTED=no"
            stack_writes_gitops "{indented}" && echo "INDENTED=yes" || echo "INDENTED=no"
            """
        )
        self.assertIn("OTHER=no", proc.stdout)
        self.assertIn("QUOTED=no", proc.stdout)
        self.assertIn("INDENTED=yes", proc.stdout)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("WRITER=yes", proc.stdout)
        self.assertIn("READER=no", proc.stdout)
        self.assertIn("NOOP=no", proc.stdout)


class WritingStackInputsTest(_Harness):
    def _fleet(self, lines):
        fleet = self.tmp / "fleet"
        fleet.mkdir(exist_ok=True)
        (fleet / ".fleet-context").write_text("".join(f"{line}\n" for line in lines))
        return str(fleet)

    def test_the_inputs_are_exported_and_unset_again(self):
        fleet = self._fleet(["project=kube-agents-evals-2", "cluster.a=seeded-a", "location.a=us-central1-a"])
        proc = self._run(
            """\
            export_writing_stack_inputs "case rep 1"
            echo "REPO=${TF_VAR_gitops_repo}"
            echo "NAME=${TF_VAR_kcc_cluster_name}"
            echo "LOC=${TF_VAR_kcc_cluster_location}"
            echo "SA=${TF_VAR_kcc_google_service_account}"
            unset_writing_stack_inputs
            echo "AFTER=${TF_VAR_gitops_repo:-}${TF_VAR_kcc_cluster_name:-}${TF_VAR_kcc_cluster_location:-}${TF_VAR_kcc_google_service_account:-}"
            """,
            BENCH_FLEET_KUBECONFIG_DIR=fleet,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn(f"REPO={_REPO}\n", proc.stdout)
        self.assertIn("NAME=seeded-a\n", proc.stdout)
        self.assertIn("LOC=us-central1-a\n", proc.stdout)
        self.assertIn("SA=kcc-eval-context@kube-agents-evals-2.iam.gserviceaccount.com\n", proc.stdout)
        self.assertIn("AFTER=\n", proc.stdout)
        self.assertEqual("", proc.stderr)

    def test_a_missing_slot_warns_in_the_unit_and_leaves_the_cluster_unset(self):
        fleet = self._fleet(["project=kube-agents-evals-2", "cluster.b=seeded-b", "location.b=us-central1-a"])
        proc = self._run(
            """\
            export_writing_stack_inputs "case rep 1"
            echo "NAME=${TF_VAR_kcc_cluster_name:-unset}"
            echo "REPO=${TF_VAR_gitops_repo}"
            """,
            BENCH_FLEET_KUBECONFIG_DIR=fleet,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("NAME=unset\n", proc.stdout)
        self.assertIn(f"REPO={_REPO}\n", proc.stdout)
        self.assertIn("WARNING: case rep 1: no cluster for seeded slot a", proc.stderr)


class CallSiteTest(unittest.TestCase):
    def test_run_one_unit_holds_the_token_around_devops_bench(self):
        unit = _function("run_one_unit")
        writes = unit.index('if stack_writes_gitops "${task}"; then')
        start = unit.index('start_gitops_write_token "${name} rep ${rep}" "${gitops_token_file}"')
        skip = unit.index('"did not run: no GitOps write token for its stack"')
        bench = unit.index("uv run devops-bench")
        stop = unit.index('stop_gitops_write_token "${gitops_token_file}"')
        self.assertLess(unit.index('mint_ledger_token "${name} rep ${rep}"'), writes)
        self.assertLess(writes, start)
        self.assertLess(start, skip)
        self.assertLess(skip, bench)
        self.assertLess(bench, stop)

    def test_the_writing_stack_inputs_are_exported_per_unit_only(self):
        # No run-wide export: stale-audit-ledger, unread-target-ledger and
        # gitops-fix-cycle declare gitops_repo with other contracts, and must
        # see no new TF_VAR_. The exports live in the marker unit's function.
        text = _CI_EVAL_PR.read_text(encoding="utf-8")
        inside = _function("export_writing_stack_inputs")
        outside = text.replace(inside, "")
        for name in ("TF_VAR_gitops_repo", "TF_VAR_kcc_cluster_name", "TF_VAR_kcc_cluster_location", "TF_VAR_kcc_google_service_account", "TF_VAR_bench_gitops_write_token_file"):
            with self.subTest(name=name):
                self.assertNotRegex(outside.replace(_function("start_gitops_write_token"), ""), rf"export [^\n]*\b{name}=")
        unit = _function("run_one_unit")
        self.assertLess(unit.index('start_gitops_write_token "${name} rep ${rep}"'), unit.index('export_writing_stack_inputs "${name} rep ${rep}"'))
        self.assertLess(unit.index('export_writing_stack_inputs "${name} rep ${rep}"'), unit.index("uv run devops-bench"))

    def test_the_account_is_the_one_the_fleet_creates(self):
        account = _constant("KCC_EVAL_CONTEXT_ACCOUNT").split("=", 1)[1].strip('"')
        stack = (_REPO_ROOT / "bench" / "tf" / "fleet" / "config-connector.tf").read_text(encoding="utf-8")
        self.assertIn(f'kcc_eval_context_account = "{account}"', stack)

    def test_fleet_context_value_reads_one_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / ".fleet-context").write_text("project=p\ncluster.a=seeded-a\nlocation.a=us-central1-a\ncluster.b=seeded-b\n")
            script = "\n".join(
                [
                    "set -euo pipefail",
                    _function("fleet_context_value"),
                    'echo "A=$(fleet_context_value cluster.a)"',
                    'echo "LOC=$(fleet_context_value location.a)"',
                    'echo "NONE=$(fleet_context_value cluster.z)"',
                    'BENCH_FLEET_KUBECONFIG_DIR="" ; echo "UNSET=$(fleet_context_value cluster.a)"',
                ]
            )
            proc = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, timeout=30,
                env=get_isolated_test_env(overrides={"BENCH_FLEET_KUBECONFIG_DIR": tmp}),
            )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("A=seeded-a\n", proc.stdout)
        self.assertIn("LOC=us-central1-a\n", proc.stdout)
        self.assertIn("NONE=\n", proc.stdout)
        self.assertIn("UNSET=\n", proc.stdout)


if __name__ == "__main__":
    unittest.main()

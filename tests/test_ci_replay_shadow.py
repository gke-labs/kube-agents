"""CI wiring of the shadow-mode replay proxy (EVAL_REPLAY_SHADOW).

hack/ci-env.sh's resolve_replay_shadow decides whether the proxy is on and
with which job tags, for both hack/ci-deploy.sh (which stops on a refusal) and
hack/ci-eval-pr.sh (which only warns). replay_shadow_helm_args turns that into
chart values, empty when off. The collector is started once the eval knows its
host cluster and stopped from the EXIT trap, and never fails the eval. The
functions run here as lifted from the shipped files, under bash.
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _lift_shell import lift_function  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_ENV = _ROOT / "hack" / "ci-env.sh"
_CI_DEPLOY = _ROOT / "hack" / "ci-deploy.sh"
_CI_EVAL = _ROOT / "hack" / "ci-eval-pr.sh"
_CLOUDBUILD = _ROOT / "deploy" / "docker" / "cloudbuild-ci.yaml"
_BUCKET = "kube-agents-replay-shadow"
_BUILD = "1790000000000000000"
_PROJECT = "kube-agents-evals-07"
# Seconds a test waits for a background process, at most.
_WAIT_SECONDS = 20


def _constants(text, prefix):
    lines = re.findall(rf"^readonly {prefix}[A-Z_]*=.*$", text, re.MULTILINE)
    assert lines, f"no readonly {prefix}* constants"
    return "\n".join(lines) + "\n"


def _lifted(*names, constants=True):
    text = _CI_ENV.read_text()
    out = _constants(text, "REPLAY_SHADOW_") if constants else ""
    return out + "".join(lift_function(n, text, _CI_ENV) for n in names)


def _env(**overrides):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PROJECT_ID": _PROJECT, "BUILD_ID": _BUILD,
           "EVAL_REPLAY_SHADOW": "true", "EVAL_REPLAY_SHADOW_BUCKET": _BUCKET, "JOB_TYPE": "periodic"}
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


_REPORT = textwrap.dedent("""\
    echo "active=${REPLAY_SHADOW_ACTIVE}"
    echo "bucket=${REPLAY_SHADOW_BUCKET}"
    echo "kind=${REPLAY_SHADOW_KIND}"
    echo "project=${REPLAY_SHADOW_PROJECT}"
    echo "build=${REPLAY_SHADOW_BUILD}"
    echo "pr=${REPLAY_SHADOW_PR}"
""")


def _resolve(**overrides):
    script = "set -euo pipefail\n" + _lifted("resolve_replay_shadow") + "rc=0\nresolve_replay_shadow || rc=$?\n" \
        + 'echo "rc=${rc}"\n' + _REPORT
    proc = subprocess.run(["bash", "-c", script], env=_env(**overrides), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    return values, proc.stderr


class ResolveTest(unittest.TestCase):
    def test_off_by_default_when_empty_and_when_false(self):
        for switch in (None, "", "false"):
            with self.subTest(switch=switch):
                values, err = _resolve(EVAL_REPLAY_SHADOW=switch, EVAL_REPLAY_SHADOW_BUCKET=None, JOB_TYPE=None)
                self.assertEqual((values["rc"], values["active"]), ("0", "false"))
                self.assertEqual(err, "")

    def test_a_switch_that_is_not_true_or_false_is_refused(self):
        for switch in ("1", "yes", "True", "TRUE"):
            with self.subTest(switch=switch):
                values, err = _resolve(EVAL_REPLAY_SHADOW=switch)
                self.assertEqual((values["rc"], values["active"]), ("1", "false"))
                self.assertIn("EVAL_REPLAY_SHADOW=", err)

    def test_periodic_and_postsubmit_are_nightly(self):
        for job_type in ("periodic", "postsubmit"):
            with self.subTest(job_type=job_type):
                values, _ = _resolve(JOB_TYPE=job_type, PULL_NUMBER=None)
                self.assertEqual(values, {"rc": "0", "active": "true", "bucket": _BUCKET, "kind": "nightly",
                                          "project": _PROJECT, "build": _BUILD, "pr": ""})

    def test_presubmit_carries_its_pull_request(self):
        values, _ = _resolve(JOB_TYPE="presubmit", PULL_NUMBER="4700")
        self.assertEqual((values["rc"], values["kind"], values["pr"]), ("0", "presubmit", "4700"))

    def test_presubmit_without_a_pull_request_number_is_refused(self):
        for pr in (None, "", "local", "0", "47a"):
            with self.subTest(pr=pr):
                values, err = _resolve(JOB_TYPE="presubmit", PULL_NUMBER=pr)
                self.assertEqual((values["rc"], values["active"]), ("1", "false"))
                self.assertIn("PULL_NUMBER", err)

    def test_batch_runs_without_the_proxy_and_says_so(self):
        values, err = _resolve(JOB_TYPE="batch", PULL_NUMBER="4700")
        self.assertEqual((values["rc"], values["active"]), ("0", "false"))
        self.assertIn("WARNING", err)

    def test_a_release_candidate_run_is_refused(self):
        values, err = _resolve(RC_COMMIT_SHA="0123456789abcdef0123456789abcdef01234567")
        self.assertEqual((values["rc"], values["active"]), ("1", "false"))
        self.assertIn("release-candidate", err)

    def test_an_unknown_job_type_is_refused(self):
        values, _ = _resolve(JOB_TYPE="manual")
        self.assertEqual(values["rc"], "1")

    def test_outside_prow_the_override_names_the_kind(self):
        values, _ = _resolve(JOB_TYPE=None, EVAL_REPLAY_SHADOW_JOB_KIND="nightly")
        self.assertEqual((values["rc"], values["kind"]), ("0", "nightly"))
        values, _ = _resolve(JOB_TYPE=None, EVAL_REPLAY_SHADOW_JOB_KIND="presubmit", PULL_NUMBER="12")
        self.assertEqual((values["rc"], values["kind"], values["pr"]), ("0", "presubmit", "12"))

    def test_outside_prow_without_a_valid_override_is_refused(self):
        for kind in (None, "", "weekly"):
            with self.subTest(kind=kind):
                values, err = _resolve(JOB_TYPE=None, EVAL_REPLAY_SHADOW_JOB_KIND=kind)
                self.assertEqual(values["rc"], "1")
                self.assertIn("EVAL_REPLAY_SHADOW_JOB_KIND", err)

    def test_the_override_is_refused_under_prow(self):
        values, err = _resolve(JOB_TYPE="presubmit", PULL_NUMBER="4700", EVAL_REPLAY_SHADOW_JOB_KIND="nightly")
        self.assertEqual((values["rc"], values["active"]), ("1", "false"))
        self.assertIn("JOB_TYPE", err)

    def test_the_bucket_is_required_and_must_be_a_bare_name(self):
        for bucket in (None, "", "gs://kube-agents-replay", "kube-agents/replay", "Kube-Agents", "ab", "-abc",
                       "a" * 64, "bucket name"):
            with self.subTest(bucket=bucket):
                values, err = _resolve(EVAL_REPLAY_SHADOW_BUCKET=bucket)
                self.assertEqual((values["rc"], values["active"]), ("1", "false"))
                self.assertIn("EVAL_REPLAY_SHADOW_BUCKET", err)
        for bucket in ("abc", "kube_agents.replay-shadow", "a" * 63):
            with self.subTest(bucket=bucket):
                self.assertEqual(_resolve(EVAL_REPLAY_SHADOW_BUCKET=bucket)[0]["rc"], "0")

    def test_the_build_id_is_required_and_path_safe(self):
        for build in (None, "", "../x", "a/b", "a b"):
            with self.subTest(build=build):
                values, err = _resolve(BUILD_ID=build)
                self.assertEqual(values["rc"], "1")
                self.assertIn("BUILD_ID", err)


class HelmArgsTest(unittest.TestCase):
    def _args(self, **overrides):
        script = ("set -euo pipefail\n" + _lifted("resolve_replay_shadow", "replay_shadow_helm_args")
                  + "resolve_replay_shadow\nreplay_shadow_helm_args repo/replay-proxy-shadow t1\n"
                  + 'echo "count=${#REPLAY_SHADOW_HELM_ARGS[@]}"\n'
                  + 'for a in ${REPLAY_SHADOW_HELM_ARGS[@]+"${REPLAY_SHADOW_HELM_ARGS[@]}"}; do echo "arg=$a"; done\n')
        proc = subprocess.run(["bash", "-c", script], env=_env(**overrides), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        return int(lines[0].split("=", 1)[1]), [l.split("=", 1)[1] for l in lines[1:]]

    def test_off_is_an_empty_array(self):
        for overrides in ({"EVAL_REPLAY_SHADOW": None}, {"EVAL_REPLAY_SHADOW": "false"},
                          {"JOB_TYPE": "batch"}):
            with self.subTest(**overrides):
                self.assertEqual(self._args(**overrides), (0, []))

    def test_on_sets_the_image_and_every_job_tag(self):
        count, args = self._args(JOB_TYPE="presubmit", PULL_NUMBER="4700")
        self.assertEqual(count, 14)
        self.assertEqual(args, [
            "--set", "litellm.replayShadow.enabled=true",
            "--set-string", "litellm.replayShadow.image.repository=repo/replay-proxy-shadow",
            "--set-string", "litellm.replayShadow.image.tag=t1",
            "--set-string", "litellm.replayShadow.job.kind=presubmit",
            "--set-string", f"litellm.replayShadow.job.project={_PROJECT}",
            "--set-string", f"litellm.replayShadow.job.build={_BUILD}",
            "--set-string", "litellm.replayShadow.job.pr=4700",
        ])


class DeployWiringTest(unittest.TestCase):
    def setUp(self):
        self.text = _CI_DEPLOY.read_text()
        self.lines = self.text.splitlines()

    def test_the_switch_is_resolved_before_any_build_and_a_refusal_stops_the_deploy(self):
        resolve = self.lines.index("resolve_replay_shadow || exit 1")
        build = next(i for i, l in enumerate(self.lines) if "gcloud builds submit" in l and not l.lstrip().startswith("#"))
        source = self.lines.index('source "${SCRIPT_DIR}/ci-env.sh"')
        self.assertLess(source, resolve)
        self.assertLess(resolve, build)

    def test_the_build_gets_the_proxy_uri_only_when_on(self):
        self.assertIn('${A2A_BUILD_SUBSTITUTIONS}${REPLAY_SHADOW_BUILD_SUBSTITUTIONS}"', self.text)
        self.assertIn('REPLAY_SHADOW_BUILD_SUBSTITUTIONS=",_REPLAY_SHADOW_PROXY_URI=${REPLAY_SHADOW_PROXY_URI}"',
                      self.text)
        self.assertIn('REPLAY_SHADOW_PROXY_URI="${AR_REPO}/${REPLAY_SHADOW_IMAGE_NAME}:${TAG}"', self.text)

    def test_the_install_expands_the_values_and_the_mode_flip_reuses_them(self):
        install = self.text[self.text.index('helm upgrade --install "${HELM_RELEASE_NAME}"'):]
        install = install[: install.index("--wait --timeout 15m")]
        self.assertIn('${REPLAY_SHADOW_HELM_ARGS[@]+"${REPLAY_SHADOW_HELM_ARGS[@]}"}', install)
        flip = self.text[self.text.index('| helm upgrade "${HELM_RELEASE_NAME}"'):]
        flip = flip[: flip.index("--wait")]
        self.assertIn("--reuse-values", flip)
        # The flip is the only later upgrade, so nothing else can drop the values.
        self.assertEqual(len(re.findall(r"^\s*[^#\n]*\bhelm upgrade\b", self.text, re.MULTILINE)), 2)


class CloudBuildStepTest(unittest.TestCase):
    def setUp(self):
        config = yaml.safe_load(_CLOUDBUILD.read_text())
        self.assertEqual(config["substitutions"]["_REPLAY_SHADOW_PROXY_URI"], "")
        [step] = [s for s in config["steps"] if s.get("id") == "replay-shadow-proxy"]
        self.assertEqual(step["waitFor"], ["-"])
        self.script = step["args"][1]
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        docker = pathlib.Path(self.tmp, "docker")
        docker.write_text('#!/usr/bin/env bash\necho "$*" >> "$DOCKER_CALLS"\n')
        docker.chmod(0o755)
        self.calls = pathlib.Path(self.tmp, "calls")

    def _run(self, uri):
        script = self.script.replace("$_REPLAY_SHADOW_PROXY_URI", uri)
        env = {"PATH": f"{self.tmp}:/usr/bin:/bin", "DOCKER_CALLS": str(self.calls)}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)

    def test_empty_uri_is_a_no_op(self):
        proc = self._run("")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.calls.exists())

    def test_a_uri_builds_from_the_repository_root_and_pushes(self):
        uri = "us-central1-docker.pkg.dev/p/kube-agents/replay-proxy-shadow:t1"
        proc = self._run(uri)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), [
            f"build --platform linux/amd64 -t {uri} -f hack/replay-proxy/Dockerfile .",
            f"push {uri}",
        ])


class CollectorLifecycleTest(unittest.TestCase):
    """start/signal/wait, with a stand-in collector that records its arguments."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in ("gcloud", "kubectl"):
            path = self.bin / tool
            path.write_text("#!/usr/bin/env bash\nexit 0\n")
            path.chmod(0o755)
        self.artifacts = self.tmp / "artifacts"
        self.argv = self.tmp / "argv"
        self.fake = self.tmp / "collector.py"

    def _fake_collector(self, ignore_term=False):
        handler = "signal.SIG_IGN" if ignore_term else "lambda *a: sys.exit(0)"
        self.fake.write_text(textwrap.dedent(f"""\
            import signal, sys, time
            open({str(self.argv)!r}, "w").write("\\n".join(sys.argv[1:]))
            signal.signal(signal.SIGTERM, {handler})
            print("collector running", flush=True)
            time.sleep(60)
        """))

    def _script(self, body, stop_wait=2):
        text = _CI_ENV.read_text()
        constants = _constants(text, "REPLAY_SHADOW_").replace(
            "readonly REPLAY_SHADOW_COLLECTOR_STOP_WAIT_SECONDS=75",
            f"readonly REPLAY_SHADOW_COLLECTOR_STOP_WAIT_SECONDS={stop_wait}")
        functions = "".join(lift_function(n, text, _CI_ENV) for n in (
            "resolve_replay_shadow", "start_replay_shadow_collector", "signal_replay_shadow_collector",
            "wait_replay_shadow_collector"))
        return ("set -euo pipefail\n" + constants + f"REPLAY_SHADOW_COLLECTOR_SCRIPT={str(self.fake)!r}\n"
                + 'REPLAY_SHADOW_COLLECTOR_PID=""\n' + functions + body)

    def _run(self, body, stop_wait=2, **overrides):
        overrides.setdefault("PATH", f"{self.bin}:{os.path.dirname(sys.executable)}:/usr/bin:/bin")
        env = _env(ARTIFACTS=str(self.artifacts), TARGET_NAMESPACE="kubeagents-system",
                   AGENT_CLUSTER_CONTEXT="gke_p_us-central1_platform-agent-host", **overrides)
        return subprocess.run([shutil.which("bash"), "-c", self._script(body, stop_wait)], env=env, capture_output=True,
                              text=True, timeout=_WAIT_SECONDS * 2)

    def _wait_for_argv(self):
        deadline = time.monotonic() + _WAIT_SECONDS
        while time.monotonic() < deadline:
            if self.argv.exists() and self.argv.read_text():
                return self.argv.read_text().split("\n")
            time.sleep(0.05)
        self.fail("the collector never started")

    def test_start_passes_the_tags_and_stop_ends_it(self):
        self._fake_collector()
        body = textwrap.dedent("""\
            start_replay_shadow_collector
            pid="${REPLAY_SHADOW_COLLECTOR_PID}"
            for _ in $(seq 1 200); do [ -s "$ARGV" ] && break; sleep 0.05; done
            signal_replay_shadow_collector
            wait_replay_shadow_collector
            if kill -0 "$pid" 2>/dev/null; then echo "still running"; else echo "stopped"; fi
        """)
        proc = self._run(body, ARGV=str(self.argv), JOB_TYPE="presubmit", PULL_NUMBER="4700")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stopped", proc.stdout)
        self.assertNotIn(_BUCKET, proc.stdout + proc.stderr)
        argv = self.argv.read_text().split("\n")
        pairs = dict(zip(argv[::2], argv[1::2]))
        self.assertEqual(pairs["--bucket"], _BUCKET)
        self.assertEqual(pairs["--kind"], "presubmit")
        self.assertEqual(pairs["--pr"], "4700")
        self.assertEqual(pairs["--build"], _BUILD)
        self.assertEqual(pairs["--project"], _PROJECT)
        self.assertEqual(pairs["--selector"], "app=litellm,kubeagents.x-k8s.io/inference-role=replay-shadow")
        self.assertEqual(pairs["--container"], "replay-shadow-proxy")
        self.assertEqual(pairs["--context"], "gke_p_us-central1_platform-agent-host")
        self.assertEqual(pairs["--namespace"], "kubeagents-system")
        self.assertTrue(pairs["--parent-pid"].isdigit())
        self.assertIn("collector running", (self.artifacts / "replay-shadow-collector.log").read_text())

    def test_a_collector_that_ignores_term_is_killed_after_the_bounded_wait(self):
        self._fake_collector(ignore_term=True)
        body = textwrap.dedent("""\
            start_replay_shadow_collector
            pid="${REPLAY_SHADOW_COLLECTOR_PID}"
            for _ in $(seq 1 200); do [ -s "$ARGV" ] && break; sleep 0.05; done
            started=$SECONDS
            wait_replay_shadow_collector
            sleep 0.2
            if kill -0 "$pid" 2>/dev/null; then echo "still running"; else echo "stopped after $((SECONDS - started))s"; fi
        """)
        proc = self._run(body, stop_wait=2, ARGV=str(self.argv))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stdout, r"stopped after [2-4]s")
        self.assertIn("did not finish its last pass", proc.stderr)

    def test_off_starts_nothing_and_the_stop_calls_are_no_ops(self):
        self._fake_collector()
        body = 'start_replay_shadow_collector\nsignal_replay_shadow_collector\nwait_replay_shadow_collector\necho "pid=[${REPLAY_SHADOW_COLLECTOR_PID}]"\n'
        proc = self._run(body, EVAL_REPLAY_SHADOW="false")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pid=[]", proc.stdout)
        self.assertFalse(self.argv.exists())

    def test_a_refused_setting_or_a_missing_tool_warns_and_never_fails_the_eval(self):
        self._fake_collector()
        body = 'start_replay_shadow_collector\necho "pid=[${REPLAY_SHADOW_COLLECTOR_PID}]"\n'
        proc = self._run(body, EVAL_REPLAY_SHADOW_BUCKET="gs://nope")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pid=[]", proc.stdout)
        self.assertIn("WARNING", proc.stderr)
        # A PATH with only what the function needs besides gcloud: a gcloud
        # installed on the machine running the test must not stand in.
        (self.bin / "gcloud").unlink()
        for tool in ("mkdir",):
            (self.bin / tool).symlink_to(shutil.which(tool))
        (self.bin / "python3").symlink_to(sys.executable)
        proc = self._run(body, PATH=str(self.bin))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pid=[]", proc.stdout)
        self.assertIn("no gcloud on PATH", proc.stderr)


class EvalWiringTest(unittest.TestCase):
    def setUp(self):
        self.lines = _CI_EVAL.read_text().splitlines()

    def test_started_once_the_host_cluster_context_is_known(self):
        context = next(i for i, l in enumerate(self.lines) if l.startswith("export AGENT_CLUSTER_CONTEXT="))
        start = self.lines.index("start_replay_shadow_collector")
        self.assertLess(context, start)

    def test_the_exit_trap_signals_first_and_waits_after_the_captures(self):
        text = _CI_EVAL.read_text()
        trap = lift_function("profile_and_dump_on_exit", text, _CI_EVAL).splitlines()
        body = [l.strip() for l in trap if l.strip() and not l.strip().startswith("#")]
        self.assertEqual(body[1:4], ["local exit_code=$?", "set +e", "signal_replay_shadow_collector"])
        self.assertLess(body.index("collect_credential_proxy_evidence"), body.index("wait_replay_shadow_collector"))
        self.assertLess(body.index("wait_replay_shadow_collector"), body.index("dump_prow_artifacts_on_failure"))


class LitellmLogLeavesTheProxyOutTest(unittest.TestCase):
    def test_the_public_litellm_log_selector_excludes_the_proxy(self):
        text = _CI_ENV.read_text()
        self.assertIn('readonly LITELLM_LOG_POD_SELECTOR="app=litellm,kubeagents.x-k8s.io/inference-role!=replay-shadow"',
                      text)
        self.assertIn('readonly REPLAY_SHADOW_POD_SELECTOR="app=litellm,kubeagents.x-k8s.io/inference-role=replay-shadow"',
                      text)


if __name__ == "__main__":
    unittest.main()

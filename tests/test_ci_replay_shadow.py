"""CI wiring of the shadow-mode replay proxy (EVAL_REPLAY_SHADOW).

hack/ci-env.sh's resolve_replay_shadow decides whether the proxy is on and
with which job tags, for both hack/ci-deploy.sh (which stops on a refusal) and
hack/ci-eval-pr.sh (which only warns). replay_shadow_helm_args turns that into
chart values, empty when off. The collector is started once the eval knows its
host cluster and stopped from the EXIT trap, and never fails the eval. The
functions run here as lifted from the shipped files, under bash.
"""

import ast
import json
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
_COLLECTOR = _ROOT / "hack" / "replay-proxy" / "collect_shadow.py"
_ANALYZER = _ROOT / "hack" / "replay-proxy" / "analyze_shadow.py"
_SCHEMA = _ROOT / "charts" / "kube-agents" / "values.schema.json"
_CHART_TEST = _ROOT / "tests" / "test_chart_replay_shadow.py"
_BUCKET = "kube-agents-replay-shadow"
_BUILD = "1790000000000000000"
_PROJECT = "kube-agents-evals-07"
# Every bash below runs with stdin on /dev/null: bash sources ~/.bashrc when
# its stdin is a socket (as under some remote shells and agent harnesses), and
# that file can rewrite PATH out from under the fakes.
# Seconds a test waits for a background process, at most.
_WAIT_SECONDS = 20
# The stand-in collector's pause between writing its argv and taking TERM: a
# caller that signals on the argv file instead of the "collector running" line
# kills it in that gap, every time.
_FAKE_HANDLER_DELAY_SECONDS = 0.5
_RECORDER = "eval-baseline-recorder@kube-agents-prow.iam.gserviceaccount.com"
_PROW_DEFAULT = "prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com"
# A stand-in gcloud that answers only the calls the real one is asked:
# `auth list` with FAKE_GCLOUD_ACCOUNT and status FAKE_GCLOUD_EXIT, and
# `config get-value` of the three identity overrides with FAKE_GCLOUD_<NAME>
# and status FAKE_GCLOUD_CONFIG_EXIT.
_FAKE_GCLOUD = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [ "$1 $2" = "config get-value" ] && [ "$#" -eq 3 ]; then
      case "$3" in
      auth/impersonate_service_account) value="${FAKE_GCLOUD_IMPERSONATE_SERVICE_ACCOUNT:-}" ;;
      auth/access_token_file) value="${FAKE_GCLOUD_ACCESS_TOKEN_FILE:-}" ;;
      auth/credential_file_override) value="${FAKE_GCLOUD_CREDENTIAL_FILE_OVERRIDE:-}" ;;
      *) exit 2 ;;
      esac
      [ -z "${value}" ] || echo "${value}"
      exit "${FAKE_GCLOUD_CONFIG_EXIT:-0}"
    fi
    [ "$*" = "auth list --filter=status:ACTIVE --format=value(account) --limit=1" ] || exit 2
    [ -z "${FAKE_GCLOUD_ACCOUNT:-}" ] || echo "${FAKE_GCLOUD_ACCOUNT}"
    exit "${FAKE_GCLOUD_EXIT:-0}"
""")
# A stand-in kubectl: FAKE_DEPLOYED_KIND for the read of the proxy Deployment's
# REPLAY_JOB_KIND (status FAKE_KUBECTL_EXIT), each call's argv appended to
# FAKE_KUBECTL_CALLS when set, and nothing for anything else.
_FAKE_KUBECTL = textwrap.dedent("""\
    #!/usr/bin/env bash
    [ -z "${FAKE_KUBECTL_CALLS:-}" ] || echo "$*" >> "${FAKE_KUBECTL_CALLS}"
    case "$*" in
    *"get deployment inference-replay-shadow -o jsonpath="*'@.name=="replay-shadow-proxy"'*'@.name=="REPLAY_JOB_KIND"'*)
      printf '%s' "${FAKE_DEPLOYED_KIND:-}"
      exit "${FAKE_KUBECTL_EXIT:-0}"
      ;;
    esac
    exit 0
""")
_FAKE_GCLOUD_DIR = None


def setUpModule():
    global _FAKE_GCLOUD_DIR
    _FAKE_GCLOUD_DIR = tempfile.mkdtemp()
    gcloud = pathlib.Path(_FAKE_GCLOUD_DIR, "gcloud")
    gcloud.write_text(_FAKE_GCLOUD)
    gcloud.chmod(0o755)


def tearDownModule():
    shutil.rmtree(_FAKE_GCLOUD_DIR, ignore_errors=True)


def _constants(text, prefix):
    lines = re.findall(rf"^readonly {prefix}[A-Z_]*=.*$", text, re.MULTILINE)
    assert lines, f"no readonly {prefix}* constants"
    return "\n".join(lines) + "\n"


def _lifted(*names, constants=True):
    text = _CI_ENV.read_text()
    out = _constants(text, "REPLAY_SHADOW_") if constants else ""
    return out + "".join(lift_function(n, text, _CI_ENV) for n in names)


def _env(**overrides):
    # The stand-in gcloud first, as the nightly runner unless a test says otherwise.
    env = {"PATH": f"{_FAKE_GCLOUD_DIR}:{os.environ.get('PATH', '/usr/bin:/bin')}", "PROJECT_ID": _PROJECT,
           "BUILD_ID": _BUILD, "EVAL_REPLAY_SHADOW": "true", "EVAL_REPLAY_SHADOW_BUCKET": _BUCKET,
           "JOB_TYPE": "periodic", "FAKE_GCLOUD_ACCOUNT": _RECORDER}
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
    script = "set -euo pipefail\n" + _lifted("_replay_shadow_unusable", "_replay_shadow_upload_identity", "resolve_replay_shadow") \
        + "rc=0\nresolve_replay_shadow || rc=$?\n" + 'echo "rc=${rc}"\n' + _REPORT
    proc = subprocess.run([shutil.which("bash"), "-c", script], env=_env(**overrides), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    return values, proc.stderr


class ResolveTest(unittest.TestCase):
    def test_false_turns_it_off_anywhere(self):
        for job_type in (None, "presubmit", "periodic"):
            with self.subTest(job_type=job_type):
                values, err = _resolve(EVAL_REPLAY_SHADOW="false", JOB_TYPE=job_type, PULL_NUMBER="4700")
                self.assertEqual((values["rc"], values["active"]), ("0", "false"))
                self.assertEqual(err, "")

    def test_on_by_default_in_prow_into_the_pool_bucket(self):
        for switch in (None, ""):
            for job_type, kind in (("presubmit", "presubmit"), ("periodic", "nightly"), ("postsubmit", "nightly")):
                with self.subTest(switch=switch, job_type=job_type):
                    values, err = _resolve(EVAL_REPLAY_SHADOW=switch, EVAL_REPLAY_SHADOW_BUCKET=None,
                                           JOB_TYPE=job_type, PULL_NUMBER="4700")
                    self.assertEqual((values["rc"], values["active"], values["kind"]), ("0", "true", kind))
                    self.assertEqual(values["bucket"], "kube-agents-replay-shadow")

    def test_left_at_its_default_a_run_it_cannot_serve_goes_without_it(self):
        # What an explicit EVAL_REPLAY_SHADOW=true refuses, the default skips with a note.
        for overrides in ({"RC_COMMIT_SHA": "abc"}, {"JOB_TYPE": None}, {"JOB_TYPE": "weird"},
                          {"JOB_TYPE": "presubmit", "PULL_NUMBER": None}, {"BUILD_ID": None}, {"PROJECT_ID": ""}):
            with self.subTest(**overrides):
                values, err = _resolve(EVAL_REPLAY_SHADOW=None, **overrides)
                self.assertEqual((values["rc"], values["active"]), ("0", "false"))
                self.assertIn("not on this run", err)
                values, err = _resolve(EVAL_REPLAY_SHADOW="true", **overrides)
                self.assertEqual((values["rc"], values["active"]), ("1", "false"))
                self.assertIn("ERROR: EVAL_REPLAY_SHADOW=true", err)

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

    def test_a_scheduled_run_as_any_other_account_is_nightly_untrusted(self):
        # Only the nightly runner may write nightly/; every presubmit runs as prowjob-default-sa.
        for job_type in ("periodic", "postsubmit"):
            for account in (_PROW_DEFAULT, "eval-baseline-recorder@other-project.iam.gserviceaccount.com",
                            f"x{_RECORDER}", f"{_RECORDER}x"):
                with self.subTest(job_type=job_type, account=account):
                    values, err = _resolve(JOB_TYPE=job_type, FAKE_GCLOUD_ACCOUNT=account)
                    self.assertEqual((values["rc"], values["active"], values["kind"]), ("0", "true", "nightly-untrusted"))
                    self.assertIn("this night's shadow data is filed as UNTRUSTED (nightly-untrusted/, never nightly/)", err)
                    self.assertIn(f"Its uploads run as {account};", err)

    def test_an_unreadable_account_is_nightly_untrusted(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        for name, overrides in (("gcloud fails", {"FAKE_GCLOUD_EXIT": "1"}),
                                ("gcloud fails after printing the recorder", {"FAKE_GCLOUD_EXIT": "1", "FAKE_GCLOUD_ACCOUNT": _RECORDER}),
                                ("no active account", {"FAKE_GCLOUD_ACCOUNT": None}),
                                ("no gcloud", {"PATH": empty})):
            with self.subTest(name):
                values, err = _resolve(**overrides)
                self.assertEqual((values["rc"], values["active"], values["kind"]), ("0", "true", "nightly-untrusted"))
                self.assertIn("filed as UNTRUSTED", err)

    def test_an_impersonation_or_token_override_is_nightly_untrusted(self):
        # gcloud auth list still shows the base account, but uploads run as someone else.
        for name in ("IMPERSONATE_SERVICE_ACCOUNT", "ACCESS_TOKEN_FILE", "CREDENTIAL_FILE_OVERRIDE"):
            for value in (_RECORDER, "/var/run/token"):
                with self.subTest(name=name, value=value):
                    values, err = _resolve(**{f"FAKE_GCLOUD_{name}": value})
                    self.assertEqual((values["rc"], values["kind"]), ("0", "nightly-untrusted"))
                    self.assertIn(f"auth/{name.lower()} is set", err)
        values, err = _resolve(FAKE_GCLOUD_CONFIG_EXIT="1")
        self.assertEqual(values["kind"], "nightly-untrusted")
        self.assertIn("gcloud could not read auth/impersonate_service_account", err)

    def test_a_presubmit_ignores_the_account(self):
        for account in (_RECORDER, _PROW_DEFAULT, None):
            with self.subTest(account=account):
                values, err = _resolve(JOB_TYPE="presubmit", PULL_NUMBER="4700", FAKE_GCLOUD_ACCOUNT=account)
                self.assertEqual((values["rc"], values["kind"], values["pr"]), ("0", "presubmit", "4700"))
                self.assertNotIn("nightly-untrusted", err)

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
        self.assertIn("not on this run", err)

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
        values, _ = _resolve(JOB_TYPE=None, EVAL_REPLAY_SHADOW_JOB_KIND="nightly-untrusted")
        self.assertEqual((values["rc"], values["kind"]), ("0", "nightly-untrusted"))
        # A nightly named there is held to the same account.
        values, _ = _resolve(JOB_TYPE=None, EVAL_REPLAY_SHADOW_JOB_KIND="nightly", FAKE_GCLOUD_ACCOUNT=_PROW_DEFAULT)
        self.assertEqual((values["rc"], values["kind"]), ("0", "nightly-untrusted"))
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
        for bucket in ("gs://kube-agents-replay", "kube-agents/replay", "Kube-Agents", "ab", "-abc",
                       "a" * 64, "bucket name"):
            with self.subTest(bucket=bucket):
                values, err = _resolve(EVAL_REPLAY_SHADOW_BUCKET=bucket)
                self.assertEqual((values["rc"], values["active"]), ("1", "false"))
                self.assertIn("EVAL_REPLAY_SHADOW_BUCKET", err)
        for bucket in ("abc", "kube_agents.replay-shadow", "a" * 63):
            with self.subTest(bucket=bucket):
                self.assertEqual(_resolve(EVAL_REPLAY_SHADOW_BUCKET=bucket)[0]["rc"], "0")
        # Unset or empty: the pool's bucket.
        for bucket in (None, ""):
            with self.subTest(bucket=bucket):
                values = _resolve(EVAL_REPLAY_SHADOW_BUCKET=bucket)[0]
                self.assertEqual((values["rc"], values["bucket"]), ("0", "kube-agents-replay-shadow"))

    def test_the_build_id_is_required_and_path_safe(self):
        for build in (None, "", "../x", "a/b", "a b"):
            with self.subTest(build=build):
                values, err = _resolve(BUILD_ID=build)
                self.assertEqual(values["rc"], "1")
                self.assertIn("BUILD_ID", err)


class HelmArgsTest(unittest.TestCase):
    def _args(self, **overrides):
        script = ("set -euo pipefail\n" + _lifted("_replay_shadow_unusable", "_replay_shadow_upload_identity", "resolve_replay_shadow",
                                                   "replay_shadow_helm_args")
                  + "resolve_replay_shadow\nreplay_shadow_helm_args repo/replay-proxy-shadow t1\n"
                  + 'echo "count=${#REPLAY_SHADOW_HELM_ARGS[@]}"\n'
                  + 'for a in ${REPLAY_SHADOW_HELM_ARGS[@]+"${REPLAY_SHADOW_HELM_ARGS[@]}"}; do echo "arg=$a"; done\n')
        proc = subprocess.run([shutil.which("bash"), "-c", script], env=_env(**overrides), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        return int(lines[0].split("=", 1)[1]), [l.split("=", 1)[1] for l in lines[1:]]

    def test_off_is_an_empty_array(self):
        for overrides in ({"EVAL_REPLAY_SHADOW": None, "JOB_TYPE": None}, {"EVAL_REPLAY_SHADOW": "false"},
                          {"JOB_TYPE": "batch"}, {"EVAL_REPLAY_SHADOW": None, "RC_COMMIT_SHA": "abc"}):
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

    def test_the_chart_gets_the_same_kind_as_the_collector(self):
        _, args = self._args(FAKE_GCLOUD_ACCOUNT=_PROW_DEFAULT)
        self.assertIn("litellm.replayShadow.job.kind=nightly-untrusted", args)


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
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)

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
        (self.bin / "gcloud").write_text(_FAKE_GCLOUD)
        (self.bin / "kubectl").write_text(_FAKE_KUBECTL)
        for tool in ("gcloud", "kubectl"):
            (self.bin / tool).chmod(0o755)
        self.artifacts = self.tmp / "artifacts"
        self.argv = self.tmp / "argv"
        self.fake = self.tmp / "collector.py"

    def _fake_collector(self, ignore_term=False):
        handler = "signal.SIG_IGN" if ignore_term else "lambda *a: sys.exit(0)"
        self.fake.write_text(textwrap.dedent(f"""\
            import signal, sys, time
            open({str(self.argv)!r}, "w").write("\\n".join(sys.argv[1:]))
            time.sleep({_FAKE_HANDLER_DELAY_SECONDS})
            signal.signal(signal.SIGTERM, {handler})
            print("collector running", flush=True)
            time.sleep(60)
        """))

    def _script(self, body, stop_wait=2):
        text = _CI_ENV.read_text()
        constants, count = re.subn(r"^readonly REPLAY_SHADOW_COLLECTOR_STOP_WAIT_SECONDS=.*$",
                                   f"readonly REPLAY_SHADOW_COLLECTOR_STOP_WAIT_SECONDS={stop_wait}",
                                   _constants(text, "REPLAY_SHADOW_"), flags=re.MULTILINE)
        assert count == 1, f"expected one REPLAY_SHADOW_COLLECTOR_STOP_WAIT_SECONDS in {_CI_ENV}, replaced {count}"
        functions = "".join(lift_function(n, text, _CI_ENV) for n in (
            "_replay_shadow_upload_identity", "_replay_shadow_deployed_kind", "resolve_replay_shadow", "start_replay_shadow_collector",
            "signal_replay_shadow_collector",
            "wait_replay_shadow_collector"))
        return ("set -euo pipefail\n" + constants + f"REPLAY_SHADOW_COLLECTOR_SCRIPT={str(self.fake)!r}\n"
                + 'REPLAY_SHADOW_COLLECTOR_PID=""\n' + functions + body)

    def _run(self, body, stop_wait=2, **overrides):
        overrides.setdefault("PATH", f"{self.bin}:{os.path.dirname(sys.executable)}:/usr/bin:/bin")
        env = _env(ARTIFACTS=str(self.artifacts), TARGET_NAMESPACE="kubeagents-system",
                   AGENT_CLUSTER_CONTEXT="gke_p_us-central1_platform-agent-host", **overrides)
        return subprocess.run([shutil.which("bash"), "-c", self._script(body, stop_wait)], env=env, capture_output=True, stdin=subprocess.DEVNULL,
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
            # The line the stand-in prints once it has taken TERM.
            for _ in $(seq 1 200); do grep -q "collector running" "$COLLECTOR_LOG" 2>/dev/null && break; sleep 0.05; done
            signal_replay_shadow_collector
            wait_replay_shadow_collector
            if kill -0 "$pid" 2>/dev/null; then echo "still running"; else echo "stopped"; fi
        """)
        proc = self._run(body, COLLECTOR_LOG=str(self.artifacts / "replay-shadow-collector.log"),
                         JOB_TYPE="presubmit", PULL_NUMBER="4700")
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

    def test_a_scheduled_run_uploads_under_the_kind_its_account_may_write(self):
        body = textwrap.dedent("""\
            start_replay_shadow_collector
            for _ in $(seq 1 200); do grep -q "collector running" "$COLLECTOR_LOG" 2>/dev/null && break; sleep 0.05; done
            signal_replay_shadow_collector
            wait_replay_shadow_collector
        """)
        log = self.artifacts / "replay-shadow-collector.log"
        # The Deployment unreadable: the eval's own check decides.
        for account, kind in ((_RECORDER, "nightly"), (_PROW_DEFAULT, "nightly-untrusted"), (None, "nightly-untrusted")):
            with self.subTest(account=account):
                # The wait reads the log, so each run starts from none.
                self.argv.unlink(missing_ok=True)
                log.unlink(missing_ok=True)
                self._fake_collector()
                proc = self._run(body, COLLECTOR_LOG=str(log), JOB_TYPE="periodic", FAKE_GCLOUD_ACCOUNT=account,
                                 FAKE_KUBECTL_EXIT="1")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                argv = self.argv.read_text().split("\n")
                self.assertEqual(dict(zip(argv[::2], argv[1::2]))["--kind"], kind)

    def test_a_scheduled_run_uploads_under_the_kind_the_deploy_stamped(self):
        body = textwrap.dedent("""\
            start_replay_shadow_collector
            for _ in $(seq 1 200); do grep -q "collector running" "$COLLECTOR_LOG" 2>/dev/null && break; sleep 0.05; done
            signal_replay_shadow_collector
            wait_replay_shadow_collector
        """)
        log = self.artifacts / "replay-shadow-collector.log"
        calls = self.tmp / "kubectl-calls"
        cases = (
            # (job type, this shell's account, the deployed kind, the folder, a note expected)
            ("periodic", _RECORDER, "nightly-untrusted", "nightly-untrusted", "uploading as nightly-untrusted, the kind the deploy stamped"),
            ("periodic", _PROW_DEFAULT, "nightly", "nightly", "uploading as nightly, the kind the deploy stamped"),
            ("periodic", _RECORDER, "nightly", "nightly", None),
            # Nothing a nightly could be filed as: this shell's own answer stands.
            ("periodic", _PROW_DEFAULT, "presubmit", "nightly-untrusted", "could not read the kind the deploy stamped"),
            ("periodic", _RECORDER, "", "nightly", "could not read the kind the deploy stamped"),
            # A presubmit never asks.
            ("presubmit", _RECORDER, "nightly", "presubmit", None),
        )
        for job_type, account, deployed, kind, note in cases:
            with self.subTest(job_type=job_type, account=account, deployed=deployed):
                for path in (self.argv, log, calls):
                    path.unlink(missing_ok=True)
                self._fake_collector()
                proc = self._run(body, COLLECTOR_LOG=str(log), JOB_TYPE=job_type, PULL_NUMBER="4700",
                                 FAKE_GCLOUD_ACCOUNT=account, FAKE_DEPLOYED_KIND=deployed, FAKE_KUBECTL_CALLS=str(calls))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                argv = self.argv.read_text().split("\n")
                self.assertEqual(dict(zip(argv[::2], argv[1::2]))["--kind"], kind)
                if note:
                    self.assertIn(note, proc.stderr)
                read = calls.exists() and "get deployment inference-replay-shadow" in calls.read_text()
                self.assertEqual(read, job_type != "presubmit")
                if read:
                    self.assertIn("--context gke_p_us-central1_platform-agent-host", calls.read_text())
                    self.assertIn("-n kubeagents-system", calls.read_text())

    def test_a_collector_that_ignores_term_is_killed_after_the_bounded_wait(self):
        self._fake_collector(ignore_term=True)
        body = textwrap.dedent("""\
            start_replay_shadow_collector
            pid="${REPLAY_SHADOW_COLLECTOR_PID}"
            # The line the stand-in prints once it has taken TERM.
            for _ in $(seq 1 200); do grep -q "collector running" "$COLLECTOR_LOG" 2>/dev/null && break; sleep 0.05; done
            started=$SECONDS
            wait_replay_shadow_collector
            sleep 0.2
            if kill -0 "$pid" 2>/dev/null; then echo "still running"; else echo "stopped after $((SECONDS - started))s"; fi
        """)
        proc = self._run(body, stop_wait=2, COLLECTOR_LOG=str(self.artifacts / "replay-shadow-collector.log"))
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


def _module_tuple(path, name):
    """A module-level tuple of strings, read without importing: literals, or names bound to them."""
    tree = ast.parse(path.read_text())
    strings, found = {}, None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                strings[target] = value.value
            elif target == name and isinstance(value, ast.Tuple):
                found = [e.value if isinstance(e, ast.Constant) else strings[e.id] for e in value.elts]
    assert found is not None, f"no {name} tuple in {path}"
    return found


class JobKindsAgreeTest(unittest.TestCase):
    """The kind list has a copy in each of five places; a kind missing from one is a refused upload or a dropped line."""

    def test_every_copy_holds_the_same_kinds(self):
        ci_env = set(re.findall(r'^readonly REPLAY_SHADOW_KIND_[A-Z_]+="([^"]*)"$', _CI_ENV.read_text(), re.MULTILINE))
        replay_shadow = json.loads(_SCHEMA.read_text())["properties"]["litellm"]["properties"]["replayShadow"]
        schema_any = set(replay_shadow["properties"]["job"]["properties"]["kind"]["enum"]) - {""}
        schema_on = set(replay_shadow["then"]["properties"]["job"]["properties"]["kind"]["enum"])
        copies = {
            "hack/ci-env.sh REPLAY_SHADOW_KIND_*": ci_env,
            "collect_shadow.py JOB_KINDS": set(_module_tuple(_COLLECTOR, "JOB_KINDS")),
            "analyze_shadow.py JOB_KINDS": set(_module_tuple(_ANALYZER, "JOB_KINDS")),
            "values.schema.json job.kind": schema_any,
            "values.schema.json job.kind when enabled": schema_on,
            "test_chart_replay_shadow.py _JOB_KINDS": set(_module_tuple(_CHART_TEST, "_JOB_KINDS")),
        }
        expected = {"nightly", "nightly-untrusted", "presubmit"}
        for where, kinds in copies.items():
            with self.subTest(where=where):
                self.assertEqual(kinds, expected)


if __name__ == "__main__":
    unittest.main()

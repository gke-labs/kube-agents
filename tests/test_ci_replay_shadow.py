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
# Every bash below runs with stdin on /dev/null: bash sources ~/.bashrc when
# its stdin is a socket (as under some remote shells and agent harnesses), and
# that file can rewrite PATH out from under the fakes.
# Seconds a test waits for a background process, at most.
_WAIT_SECONDS = 20
# The stand-in collector's pause between writing its argv and taking TERM: a
# caller that signals on the argv file instead of the "collector running" line
# kills it in that gap, every time.
_FAKE_HANDLER_DELAY_SECONDS = 0.5


def _constants(text, prefix):
    lines = re.findall(rf"^readonly {prefix}[A-Z_]*=.*$", text, re.MULTILINE)
    assert lines, f"no readonly {prefix}* constants"
    return "\n".join(lines) + "\n"


# resolve_replay_shadow and the helpers it calls, lifted together.
_RESOLVE = ("_replay_shadow_unusable", "_replay_shadow_upload_identity", "_resolve_replay_lane",
            "_replay_cache_bucket", "resolve_replay_shadow")


def _lifted(*names, constants=True):
    text = _CI_ENV.read_text()
    out = _constants(text, "REPLAY_") if constants else ""
    return out + "".join(lift_function(n, text, _CI_ENV) for n in names)


_RECORDER = "eval-baseline-recorder@kube-agents-prow.iam.gserviceaccount.com"
_PROW_RUNNER = "prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com"
# A gcloud that answers the account checks: FAKE_ACCOUNT is the active
# account, FAKE_OVERRIDE the value of every identity-override property.
_FAKE_GCLOUD = """#!/usr/bin/env bash
case "$1 $2" in
  "config get-value") printf '%s\\n' "${FAKE_OVERRIDE:-}" ;;
  "auth list") printf '%s\\n' "${FAKE_ACCOUNT:-}" ;;
esac
exit 0
"""
_FAKE_BIN = tempfile.mkdtemp(prefix="replay-ci-fake-bin-")
pathlib.Path(_FAKE_BIN, "gcloud").write_text(_FAKE_GCLOUD)
pathlib.Path(_FAKE_BIN, "gcloud").chmod(0o755)


def _env(**overrides):
    env = {"PATH": _FAKE_BIN + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"), "PROJECT_ID": _PROJECT,
           "BUILD_ID": _BUILD, "EVAL_REPLAY_SHADOW": "true", "EVAL_REPLAY_SHADOW_BUCKET": _BUCKET,
           "JOB_TYPE": "periodic", "FAKE_ACCOUNT": _PROW_RUNNER}
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
    echo "mode=${REPLAY_SHADOW_MODE}"
    echo "cache=${REPLAY_CACHE_BUCKET}"
""")


def _resolve(**overrides):
    script = "set -euo pipefail\n" + _lifted(*_RESOLVE) + "rc=0\nresolve_replay_shadow || rc=$?\n" \
        + 'echo "rc=${rc}"\n' + _REPORT
    proc = subprocess.run(["bash", "-c", script], env=_env(**overrides), capture_output=True, text=True, stdin=subprocess.DEVNULL)
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
                                          "project": _PROJECT, "build": _BUILD, "pr": "", "mode": "shadow",
                                          "cache": ""})

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


class RecordModeResolveTest(unittest.TestCase):
    """Record mode: a nightly uploading as the recorder, and nothing else, ever."""

    def test_a_nightly_uploading_as_the_recorder_records_into_the_cache_bucket(self):
        for job_type in ("periodic", "postsubmit"):
            with self.subTest(job_type=job_type):
                values, err = _resolve(JOB_TYPE=job_type, FAKE_ACCOUNT=_RECORDER)
                self.assertEqual((values["rc"], values["kind"], values["mode"], values["cache"]),
                                 ("0", "nightly", "record", "kube-agents-replay-cache"))
                self.assertEqual(err, "")

    def test_a_nightly_uploading_as_anyone_else_stays_in_shadow_and_says_why(self):
        for account in (_PROW_RUNNER, ""):
            with self.subTest(account=account):
                values, err = _resolve(FAKE_ACCOUNT=account)
                self.assertEqual((values["rc"], values["active"], values["mode"], values["cache"]),
                                 ("0", "true", "shadow", ""))
                self.assertIn("shadow mode, not record", err)

    def test_an_impersonation_or_token_override_never_records(self):
        values, err = _resolve(FAKE_ACCOUNT=_RECORDER, FAKE_OVERRIDE="someone@example.iam.gserviceaccount.com")
        self.assertEqual(values["mode"], "shadow")
        self.assertIn("is set, so gcloud does not upload as its active account", err)

    def test_without_gcloud_it_never_records(self):
        path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if not os.path.exists(os.path.join(p, "gcloud")))
        values, err = _resolve(FAKE_ACCOUNT=_RECORDER, PATH=path)
        self.assertEqual(values["mode"], "shadow")
        self.assertIn("no gcloud on PATH", err)

    def test_a_presubmit_never_records_whoever_it_runs_as(self):
        values, _ = _resolve(JOB_TYPE="presubmit", PULL_NUMBER="4700", FAKE_ACCOUNT=_RECORDER)
        self.assertEqual((values["rc"], values["kind"], values["mode"], values["cache"]), ("0", "presubmit", "shadow", ""))

    def test_asking_a_presubmit_to_record_is_refused(self):
        values, err = _resolve(JOB_TYPE="presubmit", PULL_NUMBER="4700", FAKE_ACCOUNT=_RECORDER,
                               EVAL_REPLAY_RECORD="true")
        self.assertEqual((values["rc"], values["active"]), ("1", "false"))
        self.assertIn("never writes the shared replay cache", err)

    def test_asking_an_untrusted_nightly_to_record_is_refused(self):
        values, err = _resolve(EVAL_REPLAY_RECORD="true")
        self.assertEqual((values["rc"], values["active"]), ("1", "false"))
        self.assertIn(_RECORDER, err)

    def test_false_keeps_a_trusted_nightly_in_shadow(self):
        values, err = _resolve(FAKE_ACCOUNT=_RECORDER, EVAL_REPLAY_RECORD="false")
        self.assertEqual((values["rc"], values["mode"]), ("0", "shadow"))
        self.assertEqual(err, "")

    def test_a_switch_or_cache_bucket_out_of_shape_is_refused(self):
        for overrides in ({"EVAL_REPLAY_RECORD": "yes"}, {"EVAL_REPLAY_LANE": "1"},
                          {"FAKE_ACCOUNT": _RECORDER, "EVAL_REPLAY_CACHE_BUCKET": "gs://nope"}):
            with self.subTest(**overrides):
                self.assertEqual(_resolve(**overrides)[0]["rc"], "1")


class ReplayLaneResolveTest(unittest.TestCase):
    """Replay mode: the lane only, on a periodic of main, every refusal an error."""

    def test_a_periodic_lane_replays_and_is_filed_as_the_lane(self):
        values, err = _resolve(EVAL_REPLAY_LANE="true", EVAL_REPLAY_SHADOW=None, FAKE_ACCOUNT=_RECORDER)
        self.assertEqual(values, {"rc": "0", "active": "true", "bucket": _BUCKET, "kind": "replay-lane",
                                  "project": _PROJECT, "build": _BUILD, "pr": "", "mode": "replay",
                                  "cache": "kube-agents-replay-cache"})
        self.assertEqual(err, "")

    def test_any_pull_request_ref_is_refused(self):
        for ref in ("PULL_NUMBER", "PULL_BASE_REF", "PULL_BASE_SHA", "PULL_PULL_SHA", "PULL_REFS"):
            with self.subTest(ref=ref):
                values, err = _resolve(EVAL_REPLAY_LANE="true", **{ref: "4700"})
                self.assertEqual((values["rc"], values["active"], values["mode"]), ("1", "false", "shadow"))
                self.assertIn(ref, err)

    def test_only_a_periodic_runs_the_lane(self):
        for job_type in ("presubmit", "postsubmit", "batch", "weird"):
            with self.subTest(job_type=job_type):
                values, err = _resolve(EVAL_REPLAY_LANE="true", JOB_TYPE=job_type)
                self.assertEqual((values["rc"], values["mode"]), ("1", "shadow"))

    def test_outside_prow_the_lane_names_its_kind(self):
        values, _ = _resolve(EVAL_REPLAY_LANE="true", JOB_TYPE=None, EVAL_REPLAY_SHADOW_JOB_KIND="replay-lane")
        self.assertEqual((values["rc"], values["mode"]), ("0", "replay"))
        values, err = _resolve(EVAL_REPLAY_LANE="true", JOB_TYPE=None, EVAL_REPLAY_SHADOW_JOB_KIND="nightly")
        self.assertEqual(values["rc"], "1")

    def test_the_lane_needs_the_proxy_and_a_built_image(self):
        for overrides in ({"EVAL_REPLAY_SHADOW": "false"}, {"RC_COMMIT_SHA": "abc"}, {"BUILD_ID": None},
                          {"PROJECT_ID": ""}, {"EVAL_REPLAY_CACHE_BUCKET": "gs://x"}):
            with self.subTest(**overrides):
                values, err = _resolve(EVAL_REPLAY_LANE="true", **overrides)
                self.assertEqual((values["rc"], values["mode"]), ("1", "shadow"))
                self.assertIn("ERROR", err)

    def test_the_lane_kind_without_the_lane_is_refused(self):
        values, err = _resolve(JOB_TYPE=None, EVAL_REPLAY_SHADOW_JOB_KIND="replay-lane")
        self.assertEqual(values["rc"], "1")
        self.assertIn("EVAL_REPLAY_LANE=true", err)


class HelmArgsTest(unittest.TestCase):
    def _args(self, **overrides):
        script = ("set -euo pipefail\n" + _lifted(*_RESOLVE, "replay_shadow_helm_args")
                  + "resolve_replay_shadow\nreplay_shadow_helm_args repo/replay-proxy-shadow t1\n"
                  + 'echo "count=${#REPLAY_SHADOW_HELM_ARGS[@]}"\n'
                  + 'for a in ${REPLAY_SHADOW_HELM_ARGS[@]+"${REPLAY_SHADOW_HELM_ARGS[@]}"}; do echo "arg=$a"; done\n')
        proc = subprocess.run(["bash", "-c", script], env=_env(**overrides), capture_output=True, text=True, stdin=subprocess.DEVNULL)
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

    def test_record_adds_the_mode_and_cache_dir_and_never_allow_serve(self):
        count, args = self._args(FAKE_ACCOUNT=_RECORDER)
        self.assertEqual(count, 18)
        self.assertEqual(args[14:], ["--set-string", "litellm.replayShadow.mode=record",
                                     "--set-string", "litellm.replayShadow.cacheDir=/data/replay-cache"])
        self.assertNotIn("litellm.replayShadow.allowServe=true", args)

    def test_replay_adds_both_switches(self):
        count, args = self._args(EVAL_REPLAY_LANE="true")
        self.assertEqual(count, 20)
        self.assertIn("litellm.replayShadow.job.kind=replay-lane", args)
        self.assertEqual(args[14:], ["--set-string", "litellm.replayShadow.mode=replay",
                                     "--set-string", "litellm.replayShadow.cacheDir=/data/replay-cache",
                                     "--set", "litellm.replayShadow.allowServe=true"])

    def test_a_shadow_nightly_sets_exactly_what_it_did_before_the_modes(self):
        count, args = self._args()
        self.assertEqual(count, 14)
        self.assertFalse(any("mode=" in a or "cacheDir" in a or "allowServe" in a for a in args))


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


class _LifecycleBase(unittest.TestCase):
    """A stand-in collector that records its arguments, fake tools, and the lifted functions."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "gcloud").write_text(_FAKE_GCLOUD)
        # The proxy Deployment's name and mode, as _replay_shadow_deployed_mode reads them.
        (self.bin / "kubectl").write_text('#!/usr/bin/env bash\nprintf "%s" "${FAKE_DEPLOYED:-}"\n')
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
                                   _constants(text, "REPLAY_"), flags=re.MULTILINE)
        assert count == 1, f"expected one REPLAY_SHADOW_COLLECTOR_STOP_WAIT_SECONDS in {_CI_ENV}, replaced {count}"
        functions = "".join(lift_function(n, text, _CI_ENV) for n in (
            *_RESOLVE, "_replay_shadow_deployed_mode", "start_replay_shadow_collector",
            "signal_replay_shadow_collector", "wait_replay_shadow_collector"))
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

class CollectorLifecycleTest(_LifecycleBase):
    """start/signal/wait."""

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


# Waits for the stand-in to write its arguments, so stopping it cannot beat it.
_AWAIT_ARGV = 'for _ in $(seq 1 200); do [ -s "$ARGV" ] && break; sleep 0.05; done\n'


class ModeCollectorTest(_LifecycleBase):
    """The collector's mode follows what this shell resolved and what the deploy installed."""

    def _argv_pairs(self):
        argv = self._wait_for_argv()
        return argv, dict(zip(argv[::2], argv[1::2]))

    def _start(self, **overrides):
        self._fake_collector()
        body = ('start_replay_shadow_collector\necho "wait=${REPLAY_SHADOW_COLLECTOR_WAIT_SECONDS}"\n'
                + _AWAIT_ARGV + 'wait_replay_shadow_collector\n')
        proc = self._run(body, stop_wait=1, ARGV=str(self.argv), **overrides)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_a_trusted_nightly_installed_in_record_mode_collects_and_publishes(self):
        proc = self._start(FAKE_ACCOUNT=_RECORDER, FAKE_DEPLOYED="inference-replay-shadow|record")
        argv, pairs = self._argv_pairs()
        self.assertEqual(pairs["--mode"], "record")
        self.assertEqual(pairs["--publish-bucket"], "kube-agents-replay-cache")
        self.assertEqual(pairs["--verdicts-dir"], str(self.artifacts))
        self.assertEqual(pairs["--cache-subdir"], "replay-cache")
        self.assertEqual(pairs["--final-deadline"], "240")
        self.assertTrue(os.path.isdir(pairs["--keep-dir"]))
        self.assertNotIn(str(self.artifacts), pairs["--keep-dir"], "kept answers stay out of the public artifacts")
        self.assertIn("wait=255", proc.stdout)

    def test_a_proxy_installed_in_shadow_mode_keeps_the_collector_in_shadow(self):
        for deployed in ("inference-replay-shadow|", "inference-replay-shadow|shadow", ""):
            with self.subTest(deployed=deployed):
                self.argv.unlink(missing_ok=True)
                proc = self._start(FAKE_ACCOUNT=_RECORDER, FAKE_DEPLOYED=deployed)
                argv, _ = self._argv_pairs()
                self.assertNotIn("--mode", argv)
                self.assertNotIn("--publish-bucket", argv)
                self.assertIn("runs in shadow mode", proc.stderr)

    def test_an_untrusted_nightly_never_publishes_whatever_was_installed(self):
        self._start(FAKE_DEPLOYED="inference-replay-shadow|record")
        argv, _ = self._argv_pairs()
        self.assertNotIn("--publish-bucket", argv)

    def test_the_lane_collects_in_replay_mode_with_its_seed_and_report_inputs(self):
        body = ('REPLAY_KEEP_DIR=/k\nREPLAY_LANE_SEED_DIR=/s\nREPLAY_LANE_NIGHTLY_RUNS=/r.json\n'
                'start_replay_shadow_collector\n' + _AWAIT_ARGV + 'wait_replay_shadow_collector\n')
        self._fake_collector()
        proc = self._run(body, stop_wait=1, EVAL_REPLAY_LANE="true", ARGV=str(self.argv))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        _, pairs = self._argv_pairs()
        self.assertEqual((pairs["--mode"], pairs["--kind"], pairs["--keep-dir"], pairs["--seed-dir"],
                          pairs["--nightly-runs"], pairs["--pr"]), ("replay", "replay-lane", "/k", "/s", "/r.json", ""))


class PrepareLaneCacheTest(_LifecycleBase):
    """prepare_replay_lane_cache: fetch, then seed every pod, before any case; any failure stops the lane."""

    def setUp(self):
        super().setUp()
        self.calls = self.tmp / "calls"
        self.fake.write_text(textwrap.dedent(f"""\
            import os, sys
            open({str(self.calls)!r}, "a").write("seed " + " ".join(sys.argv[1:]) + "\\n")
            sys.exit(int(os.environ.get("FAKE_SEED_RC", "0")))
        """))
        self.fetch = self.tmp / "fetch.py"
        self.fetch.write_text(textwrap.dedent(f"""\
            import os, sys
            open({str(self.calls)!r}, "a").write("fetch " + " ".join(sys.argv[1:]) + "\\n")
            sys.exit(int(os.environ.get("FAKE_FETCH_RC", "0")))
        """))

    def _prepare(self, **overrides):
        script = self._script("") .replace("set -euo pipefail\n", "set -euo pipefail\n", 1)
        text = _CI_ENV.read_text()
        script += lift_function("prepare_replay_lane_cache", text, _CI_ENV)
        script += f"REPLAY_CACHE_FETCH_SCRIPT={str(self.fetch)!r}\n"
        script += 'rc=0\nprepare_replay_lane_cache || rc=$?\necho "rc=${rc}"\n'
        overrides.setdefault("PATH", f"{self.bin}:{os.path.dirname(sys.executable)}:/usr/bin:/bin")
        env = _env(ARTIFACTS=str(self.artifacts), TARGET_NAMESPACE="kubeagents-system",
                   AGENT_CLUSTER_CONTEXT="gke_p_us-central1_platform-agent-host", **overrides)
        proc = subprocess.run([shutil.which("bash"), "-c", script], env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=_WAIT_SECONDS)
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        return proc, calls

    def test_off_the_lane_it_does_nothing(self):
        proc, calls = self._prepare()
        self.assertIn("rc=0", proc.stdout)
        self.assertEqual(calls, [])

    def test_it_fetches_then_seeds_every_pod(self):
        proc, calls = self._prepare(EVAL_REPLAY_LANE="true", FAKE_DEPLOYED="inference-replay-shadow|replay")
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertEqual([c.split()[0] for c in calls], ["fetch", "seed"])
        self.assertIn("--bucket kube-agents-replay-cache", calls[0])
        self.assertIn("--max-age-hours 30", calls[0])
        self.assertIn(f"--max-bytes {512 * 1024 * 1024}", calls[0])
        self.assertIn("--seed-once", calls[1])
        self.assertIn("--mode replay", calls[1])

    def test_a_proxy_not_installed_in_replay_mode_stops_the_lane(self):
        proc, calls = self._prepare(EVAL_REPLAY_LANE="true", FAKE_DEPLOYED="inference-replay-shadow|shadow")
        self.assertIn("rc=1", proc.stdout)
        self.assertEqual(calls, [])

    def test_nothing_to_replay_or_a_pod_that_did_not_take_it_stops_the_lane(self):
        for overrides, expected in (({"FAKE_FETCH_RC": "1"}, ["fetch"]), ({"FAKE_SEED_RC": "1"}, ["fetch", "seed"])):
            with self.subTest(**overrides):
                self.calls.unlink(missing_ok=True)
                proc, calls = self._prepare(EVAL_REPLAY_LANE="true", FAKE_DEPLOYED="inference-replay-shadow|replay",
                                            **overrides)
                self.assertIn("rc=1", proc.stdout)
                self.assertEqual([c.split()[0] for c in calls], expected)


class EvalWiringTest(unittest.TestCase):
    def setUp(self):
        self.lines = _CI_EVAL.read_text().splitlines()

    def test_started_once_the_host_cluster_context_is_known(self):
        context = next(i for i, l in enumerate(self.lines) if l.startswith("export AGENT_CLUSTER_CONTEXT="))
        start = self.lines.index("start_replay_shadow_collector")
        self.assertLess(context, start)

    def test_the_lane_seeds_before_the_collector_and_stops_when_it_cannot(self):
        context = next(i for i, l in enumerate(self.lines) if l.startswith("export AGENT_CLUSTER_CONTEXT="))
        prepare = self.lines.index("prepare_replay_lane_cache || exit 1")
        self.assertLess(context, prepare)
        self.assertLess(prepare, self.lines.index("start_replay_shadow_collector"))
        fan_out = next(i for i, l in enumerate(self.lines) if l.startswith("launch_units() {"))
        self.assertLess(prepare, fan_out)

    def test_the_lane_records_nothing_and_publishes_no_dashboard(self):
        text = _CI_EVAL.read_text()
        self.assertIn('if [ "${EVAL_REPLAY_LANE:-}" = "true" ]; then\n  EVAL_IS_MAIN_RUN="false"\nfi', text)
        publish = lift_function("publish_eval_dashboard", text, _CI_EVAL)
        self.assertIn('"${EVAL_REPLAY_LANE:-}" = "true"', publish)

    def test_the_lane_runs_its_own_roster_and_arms_no_roster_case(self):
        text = _CI_EVAL.read_text()
        self.assertIn('readonly EVAL_REPLAY_LANE_CASES_FILE="eval/replay-lane-cases.txt"', text)
        lane = text[text.index('case "${EVAL_REPLAY_LANE:-false}" in'):]
        lane = lane[: lane.index("\nesac\n")]
        self.assertIn('TASKS=("${REPLAY_LANE_TASKS[@]}")', lane)
        self.assertIn("is in neither", lane, "an unregistered entry stops the lane")
        roster = text[text.index('while IFS= read -r NAME; do'):]
        roster = roster[: roster.index('done <<< "${BLOCKING_ROSTER_ENTRIES}"')]
        self.assertIn('if [ "${EVAL_REPLAY_LANE:-}" = "true" ]; then\n    continue\n  fi', roster)

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

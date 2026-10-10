"""Offline tests for the Slack live-check launcher (hack/slack-live-check/launch.py).

kubectl is a recorded fake behind the launcher's runner seam: every call is checked
for the pinned --context, and the fake answers from a small model of a next install
(the PlatformAgent, the broker and gateway Deployments and their logs, the Job).
"""

import argparse
import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import unittest
from unittest import mock

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
LAUNCH_DIR = REPO / "hack" / "slack-live-check"
spec = importlib.util.spec_from_file_location("slack_live_check_launch", LAUNCH_DIR / "launch.py")
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)
harness = launch.harness

CONTEXT = "gke_test_ctx"
AGENT = "platform-agent"
AGENT_NS = "kubeagents-system"
SANDBOX_IMAGE = "us-docker.pkg.dev/p/kube-agents/agent-sandbox:v1"
LEAKED = "xoxp-9999-leaked-token-value"
# Far above any wait the launcher sets: a loop that passes it is unbounded.
SLEEP_BUDGET_SECONDS = 100_000
# The harness has no default bot name, so every run in these tests names one.
BOT_FLAGS = ["--bot-name", "kage"]


def with_bot(argv):
    """argv with BOT_FLAGS forwarded to the harness, unless it already names the bot.

    --render is left alone: it runs no check, so it must not need a bot, and adding
    one here would hide a render that refuses without it."""
    argv = list(argv)
    if "--bot-name" in argv or "--bot-user-id" in argv or "--render" in argv:
        return argv
    if "--" not in argv:
        return [*argv, "--", *BOT_FLAGS]
    split = argv.index("--") + 1
    return [*argv[:split], *BOT_FLAGS, *argv[split:]]


def parse(argv):
    return launch.parse_args(with_bot(argv))


def pod_spec(job):
    return job["spec"]["template"]["spec"]


def deployment(name, container, env_names, labels, env_from=None):
    c = {"name": container, "env": [{"name": n, "valueFrom": {"secretKeyRef": {"name": "s", "key": n}}} for n in env_names]}
    if env_from:
        c["envFrom"] = env_from
    return {"metadata": {"name": name}, "spec": {"selector": {"matchLabels": labels},
                                                 "template": {"spec": {"containers": [c]}}}}


class FakeFailure(Exception):
    pass


class FakeCluster:
    def __init__(self):
        self.calls = []
        self.applied = []
        self.deleted = []
        self.broker_env = ["CREDENTIAL_PROXY_POLICY"]
        self.broker_env_from = None
        self.broker_logs = "INFO credential proxy listening on unix socket /run/x\n"
        self.gateway_env = ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "A2A_SLACK_ALLOWED_USERS"]
        self.gateway_logs = '{"time":"t","level":"INFO","msg":"slack connected","user":"kage","botUserID":"U0BOT"}\n'
        self.job_condition = "Complete"
        self.job_log = "PASS preflight-listed: ok\nPASS dm: answer\nEVIDENCE {\"check\": \"dm\", \"passed\": true, \"sent_ts\": \"1.000101\", \"author\": \"U0LISTED1\"}\nSUMMARY pass=2 fail=0\n"
        self.restart_runs = []
        self.fail_on = None
        self.namespaces = {}
        self.empty_namespace_output = False
        self.namespace_output = None
        self.delete_fails = False
        # This tool's Jobs as `get jobs -l` lists them: another run's, for the concurrency guard.
        self.jobs = []
        self.platformagents = [AGENT]
        self.sandbox_containers = [{"name": "sandbox", "image": SANDBOX_IMAGE}]
        self.restart_returncode = 0
        self.restart_missing = False
        self.job_log_fails = False
        # How many gateway log reads fail before one answers; -1 is every one.
        self.gateway_log_failures = 0
        # The Job is gone: `get job --ignore-not-found` prints nothing.
        self.job_gone = False
        # Any exception to raise on the Job's apply, after the namespace is set up.
        self.job_apply_raises = None
        # One log per Job, in run order, in place of job_log when set.
        self.job_logs = []
        # The applied Job's log, chosen from job_logs or job_log when the Job is applied.
        self.current_job_log = None
        # The log as successive reads see it while the Job runs (None: the read fails,
        # as for a pod not started yet); the last entry stands for every later read.
        self.job_log_progress = []
        self.job_log_reads = 0
        # How many `get job` polls see the Job still running before it completes.
        self.job_running_polls = 0
        # Called on every `get job` poll: a test's view of what was printed by then.
        self.on_job_poll = None
        # How many `get job` polls fail before one answers; -1 is every one.
        self.get_job_failures = 0
        self.stdin_encodings = []

    def __call__(self, cmd, input=None, capture_output=True, text=True, check=False, encoding=None):
        if cmd[0] != "kubectl" or "restart" in cmd:
            # The operator's --restart-cmd, not the launcher's own kubectl.
            self.restart_runs.append(cmd)
            if self.restart_missing:
                raise FileNotFoundError(2, "No such file or directory", cmd[0])
            return subprocess.CompletedProcess(cmd, self.restart_returncode, "", "rollout refused")
        self.calls.append(cmd)
        self.stdin_encodings.append(encoding)
        assert cmd[1:3] == ["--context", CONTEXT], cmd
        args = cmd[3:]
        if args[0] == "logs" and "a2a-gateway" in " ".join(args) and self.gateway_log_failures:
            self.gateway_log_failures -= 1 if self.gateway_log_failures > 0 else 0
            return subprocess.CompletedProcess(cmd, 1, "", "Unable to connect to the server: net/http: TLS handshake timeout")
        if args[:2] == ["get", "job"] and self.get_job_failures:
            self.get_job_failures -= 1 if self.get_job_failures > 0 else 0
            return subprocess.CompletedProcess(cmd, 1, "", "Unable to connect to the server: net/http: TLS handshake timeout")
        try:
            out = self.answer(args, input)
        except FakeFailure:
            return subprocess.CompletedProcess(cmd, 1, "", "error: timed out waiting for the condition")
        if self.fail_on and self.fail_on in " ".join(args):
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def answer(self, args, stdin):
        joined = " ".join(args)
        if args[0] == "apply":
            is_job = '"kind": "Job"' in stdin.replace("kind: Job", '"kind": "Job"')
            if self.job_apply_raises and is_job:
                raise self.job_apply_raises
            if is_job:
                self.current_job_log = self.job_logs.pop(0) if self.job_logs else self.job_log
                self.job_log_reads = 0
            self.applied.append(stdin)
            for doc in yaml.safe_load_all(stdin):
                if doc and doc.get("kind") == "Namespace":
                    self.namespaces[doc["metadata"]["name"]] = doc["metadata"].get("labels", {})
            return "applied"
        if args[0] == "delete":
            if self.delete_fails and args[1] == "namespace":
                raise FakeFailure()
            self.deleted.append(args[1:3])
            if args[1] == "namespace":
                self.namespaces.pop(args[2], None)
            return ""
        if args[:2] == ["get", "namespace"]:
            # Real kubectl: a field selector with no match prints an empty List.
            if self.namespace_output is not None:
                return self.namespace_output
            name = [a for a in args if a.startswith("metadata.name=")][0].split("=", 1)[1]
            items = [{"metadata": {"name": name, "labels": self.namespaces[name]}}] if name in self.namespaces else []
            if not items and self.empty_namespace_output:
                return ""
            return json.dumps({"apiVersion": "v1", "kind": "List", "items": items})
        if args[:2] == ["get", "jobs"]:
            return json.dumps({"items": self.jobs})
        if args[:2] == ["get", "platformagents"]:
            return json.dumps({"items": [{"metadata": {"name": n}} for n in self.platformagents]})
        if args[:2] == ["get", "statefulset"]:
            return json.dumps({"spec": {"template": {"spec": {"containers": self.sandbox_containers}}}})
        if args[:3] == ["get", "deployment", AGENT + "-credential-proxy"]:
            return json.dumps(deployment(args[2], "envoy-credential-proxy", self.broker_env,
                                         {"app": AGENT + "-credential-proxy"}, self.broker_env_from))
        if args[:3] == ["get", "deployment", AGENT + "-a2a-gateway"]:
            return json.dumps(deployment(args[2], "gateway", self.gateway_env, {"app": AGENT + "-a2a-gateway"}))
        if args[:2] == ["get", "job"]:
            assert "--ignore-not-found" in args, args
            if self.job_gone:
                return ""
            if self.on_job_poll is not None:
                self.on_job_poll()
            if self.job_running_polls > 0:
                self.job_running_polls -= 1
                return json.dumps({"status": {"active": 1}})
            conditions = [{"type": self.job_condition, "status": "True"}] if self.job_condition else []
            return json.dumps({"status": {"conditions": conditions}})
        if args[0] == "logs" and "job/" in joined:
            if self.job_log_fails:
                raise FakeFailure()
            if self.job_log_progress:
                seen = self.job_log_progress[min(self.job_log_reads, len(self.job_log_progress) - 1)]
                self.job_log_reads += 1
                if seen is None:
                    raise FakeFailure()
                return seen
            return self.current_job_log
        if args[0] == "logs" and "credential-proxy" in joined:
            assert "--tail=-1" in args
            return self.broker_logs
        if args[0] == "logs" and "a2a-gateway" in joined:
            assert "--tail=-1" in args
            return self.gateway_logs
        if args[0] == "rollout":
            return "rolled out"
        if args[:2] == ["get", "pods"]:
            return "pod Pending"
        raise AssertionError(f"unexpected kubectl {args}")


class Unbounded(BaseException):
    """A wait that outran every timeout. A BaseException, so launch.main's
    `except Exception` cannot turn it into an ERROR line and an exit code."""


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        if seconds <= 0 or self.now > SLEEP_BUDGET_SECONDS:
            raise Unbounded(f"a wait slept {seconds}s at t={self.now}; it is not bounded")
        self.now += seconds


def run_launch(cluster, *argv):
    out = io.StringIO()
    fake = FakeClock()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = launch.main(with_bot(argv), runner=cluster, clock=fake.clock, sleep=fake.sleep)
    return code, out.getvalue()


class LegacySocketTest(unittest.TestCase):
    def test_passes_on_a_clean_broker_and_a_connected_gateway(self):
        cluster = FakeCluster()
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "legacy-socket")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertIn("PASS legacy-socket: broker envoy-credential-proxy has no Slack pair", out)
        self.assertEqual(cluster.applied, [], "legacy-socket alone must not create anything")

    def test_fails_when_the_broker_carries_the_pair(self):
        cluster = FakeCluster()
        cluster.broker_env.append("SLACK_APP_TOKEN")
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "legacy-socket,dm")
        self.assertEqual(code, launch.EXIT_FAIL)
        self.assertIn("carries SLACK_APP_TOKEN", out)
        self.assertEqual(cluster.applied, [], "the first FAIL stops the run")

    def test_fails_on_broker_envfrom(self):
        cluster = FakeCluster()
        cluster.broker_env_from = [{"secretRef": {"name": "x"}}]
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "legacy-socket")
        self.assertEqual(code, launch.EXIT_FAIL)
        self.assertIn("envFrom", out)

    def test_fails_when_the_broker_logged_its_relay(self):
        for line in ("INFO Slack relay enabled workspaces=1", "ERROR Slack relay initialization failed; retrying type=X"):
            cluster = FakeCluster()
            cluster.broker_logs += line + "\n"
            code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "legacy-socket")
            self.assertEqual(code, launch.EXIT_FAIL, line)
            self.assertIn("the broker logged its Slack relay", out)

    def test_fails_when_the_gateway_has_no_pair(self):
        cluster = FakeCluster()
        cluster.gateway_env = []
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "legacy-socket")
        self.assertEqual(code, launch.EXIT_FAIL)
        self.assertIn("not a next install", out)

    def test_a_kubectl_error_is_its_fail_and_keep_going_carries_on(self):
        cluster = FakeCluster()
        cluster.fail_on = "get deployment platform-agent-a2a-gateway"
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "legacy-socket,dm", "--keep-going")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        self.assertIn("FAIL legacy-socket: error: kubectl", out)
        self.assertIn("PASS dm: answer", out)
        self.assertIn("OVERALL pass=2 fail=1 failed=legacy-socket", out)
        self.assertNotIn("ERROR", out)
        cluster = FakeCluster()
        cluster.fail_on = "get deployment platform-agent-a2a-gateway"
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "legacy-socket,dm")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        self.assertIn("OVERALL pass=0 fail=1 failed=legacy-socket", out)
        self.assertEqual(cluster.applied, [], "the first FAIL stops the run")

    def test_fails_when_the_gateway_never_connected(self):
        cluster = FakeCluster()
        cluster.gateway_logs = '{"msg":"slack auth.test: invalid_auth"}\n'
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "legacy-socket")
        self.assertEqual(code, launch.EXIT_FAIL)
        self.assertIn("has not logged 'slack connected'", out)


class JobTest(unittest.TestCase):
    def test_job_runs_prints_results_and_cleans_up(self):
        cluster = FakeCluster()
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm", "--", "--wait-answer")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertIn("PASS dm: answer", out)
        setup = list(yaml.safe_load_all(cluster.applied[0]))
        configmap = json.loads(cluster.applied[1])
        job = yaml.safe_load(cluster.applied[2])
        self.assertEqual([d["kind"] for d in setup], ["Namespace", "ServiceAccount", "NetworkPolicy"])
        self.assertEqual(setup[1]["metadata"]["name"], "slack-test-runner")
        self.assertEqual(setup[1]["metadata"]["annotations"]["iam.gke.io/gcp-service-account"],
                         "ka-slack-test-runner@bnaylor-kagents-dev.iam.gserviceaccount.com")
        self.assertEqual(job["metadata"]["namespace"], "slack-test")
        self.assertEqual(pod_spec(job)["serviceAccountName"], "slack-test-runner")
        self.assertEqual(configmap["kind"], "ConfigMap")
        self.assertEqual(configmap["data"]["harness.py"], (LAUNCH_DIR / "harness.py").read_text())
        pod = pod_spec(job)
        container = pod["containers"][0]
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(container["image"], SANDBOX_IMAGE)
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        # No credential reaches the pod through its spec.
        self.assertEqual([e["name"] for e in container["env"]], ["HOME"])
        self.assertNotIn("envFrom", container)
        args = container["args"]
        self.assertEqual(args[args.index("--checks") + 1], "dm")
        self.assertIn("--wait-answer", args)
        self.assertEqual(sorted(kind for kind, _ in cluster.deleted), ["configmap", "job", "namespace"])
        self.assertEqual(cluster.deleted[-1], ["namespace", "slack-test"], "the namespace goes last")

    def test_job_failure_without_fail_lines_is_reported_and_cleaned_up(self):
        cluster = FakeCluster()
        cluster.job_condition = "Failed"
        cluster.job_log = "ERROR the metadata server refused a token (HTTP 403)\nSUMMARY setup failed\n"
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_FAIL)
        self.assertIn("FAIL job:", out)
        self.assertEqual(len(cluster.deleted), 3)

    def test_a_transient_job_read_failure_does_not_end_the_wait(self):
        cluster = FakeCluster()
        cluster.get_job_failures = 3
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertIn("PASS dm: answer", out)
        self.assertIn("OVERALL pass=2 fail=0", out)

    def test_cleanup_runs_when_job_reads_fail_past_the_deadline(self):
        cluster = FakeCluster()
        cluster.get_job_failures = -1
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        self.assertIn("could not be read until the deadline", out)
        self.assertIn("TLS handshake timeout", out)
        self.assertIn("ended Unknown with no FAIL line", self.line(out, "FAIL job:"))
        self.assertIn("OVERALL", out)
        self.assertEqual(sorted(kind for kind, _ in cluster.deleted), ["configmap", "job", "namespace"])
        polls = [c for c in cluster.calls if c[3:5] == ["get", "job"]]
        deadline = launch.job_deadline(["--checks", "dm", *BOT_FLAGS], 0) + launch.JOB_WAIT_GRACE_SECONDS
        self.assertGreaterEqual(len(polls), deadline // launch.JOB_POLL_INTERVAL_SECONDS)

    def test_a_job_that_is_gone_ends_the_wait_at_once(self):
        # `get job --ignore-not-found` exits 0 and prints nothing for a Job that is not
        # there: deleted by hand, or with its namespace. No deadline-long poll.
        cluster = FakeCluster()
        cluster.job_gone = True
        cluster.job_log_fails = True
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        self.assertIn("ended gone with no FAIL line", self.line(out, "FAIL job:"))
        polls = [c for c in cluster.calls if c[3:5] == ["get", "job"]]
        self.assertEqual(len(polls), 1, polls)
        self.assertEqual(cluster.deleted[-1], ["namespace", "slack-test"])

    def test_a_gone_job_gets_one_final_log_read_that_says_it_is_gone(self):
        cluster = FakeCluster()
        cluster.job_gone = True
        cluster.job_log_progress = ["PASS preflight-listed: ok\n", None]
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        reads = [c for c in cluster.calls if c[3] == "logs" and any(a.startswith("job/") for a in c)]
        # One poll read before the wait saw it gone, then one final read: no retry.
        self.assertEqual(len(reads), 2, reads)
        self.assertIn("the Job is gone, so its log could not be read again", out)
        self.assertNotIn("the final log read failed", out)

    def test_cleanup_runs_when_the_run_raises_after_setup(self):
        # The namespace (and the Workload-Identity-bound ServiceAccount in it) is
        # applied, then something guarded() does not wrap raises: it is still deleted.
        for exc in (RuntimeError("boom"), launch.LaunchError("kubectl apply exited 1: boom")):
            with self.subTest(exc=type(exc).__name__):
                cluster = FakeCluster()
                cluster.job_apply_raises = exc
                code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
                self.assertEqual(code, launch.EXIT_SETUP, out)
                self.assertIn("ERROR", out)
                self.assertEqual([d["kind"] for d in yaml.safe_load_all(cluster.applied[0])][0], "Namespace")
                self.assertEqual(cluster.deleted[-1], ["namespace", "slack-test"])
                self.assertIn("CLEANUP namespace slack-test deleted", out)

    def test_job_args_carry_any_character_through_yaml(self):
        # DEL and a C1 control pass json.dumps raw and YAML's reader refuses them raw;
        # the rest are YAML's escapes and line breaks, and the quote and backslash.
        value = 'PONG\x7f \x92 \x85 \u2028 \u2029 \ufeff \ufffe \x00 \t \n " \\ \'s: - [x] #'
        cluster = FakeCluster()
        run_launch(cluster, "--context", CONTEXT, "--checks", "dm", "--", "--prompt", value)
        args = pod_spec(yaml.safe_load(cluster.applied[2]))["containers"][0]["args"]
        self.assertEqual(args[args.index("--prompt") + 1], value)
        self.assertEqual(args[:2], ["--checks", "dm"])
        _, _, rendered = launch.job_manifests("ns", "sa", "img", "r", ["--prompt", value], 60)
        self.assertNotIn("\x7f", rendered)
        self.assertNotIn("\x92", rendered)

    def test_job_args_carry_non_bmp_characters_as_utf8(self):
        cluster = FakeCluster()
        run_launch(cluster, "--context", CONTEXT, "--checks", "home", "--",
                   "--home-channel", "ops", "--home-match", "🔎 task")
        job_text = cluster.applied[2]
        self.assertNotIn("\\ud83d", job_text)
        self.assertIn('"🔎 task"', job_text)
        args = pod_spec(yaml.safe_load(job_text))["containers"][0]["args"]
        self.assertEqual(args[args.index("--home-match") + 1], "🔎 task")
        self.assertTrue(cluster.stdin_encodings and set(cluster.stdin_encodings) == {"utf-8"}, cluster.stdin_encodings)

    def test_the_image_is_discovered_once_per_run(self):
        cluster = FakeCluster()
        cluster.job_log = "PASS dm: answer\nPASS restart: answer\n"
        run_launch(cluster, "--context", CONTEXT, "--checks", "dm,restart", "--after-restart")
        self.assertEqual(len([c for c in cluster.calls if c[3:5] == ["get", "statefulset"]]), 1)

    def test_pod_log_tokens_are_scrubbed(self):
        cluster = FakeCluster()
        cluster.job_log = f"FAIL dm: odd {LEAKED}\nSUMMARY pass=0 fail=1\n"
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_FAIL)
        self.assertNotIn(LEAKED, out)

    def test_harness_checks_after_a_launcher_fail_are_skipped_unless_keep_going(self):
        cluster = FakeCluster()
        cluster.broker_env.append("SLACK_BOT_TOKEN")
        code, _ = run_launch(cluster, "--context", CONTEXT, "--checks", "legacy-socket,dm", "--keep-going")
        self.assertEqual(code, launch.EXIT_FAIL)
        job = yaml.safe_load(cluster.applied[2])
        self.assertIn("--keep-going", pod_spec(job)["containers"][0]["args"])


    def test_a_complete_job_with_an_unreadable_log_is_not_clean(self):
        cluster = FakeCluster()
        cluster.job_log_fails = True
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "legacy-socket,dm")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        self.assertIn("PASS legacy-socket", out)
        self.assertIn("FAIL dm: no PASS or FAIL line for it", out)
        self.assertIn("OVERALL pass=1 fail=1 failed=dm", out)

    def test_every_requested_check_needs_a_verdict(self):
        cluster = FakeCluster()
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm,mention")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        self.assertIn("FAIL mention: no PASS or FAIL line for it", out)
        self.assertNotIn("FAIL dm", out)

    def test_a_job_that_never_finishes_times_out(self):
        cluster = FakeCluster()
        cluster.job_condition = None
        cluster.job_log = ""
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        self.assertIn("did not finish in time; pods:\npod Pending", out)
        self.assertIn("ended Timeout with no FAIL line", self.line(out, "FAIL job:"))

    def test_an_unbounded_wait_is_not_swallowed_by_main(self):
        cluster = FakeCluster()
        cluster.job_running_polls = 10**9
        with mock.patch.object(launch, "JOB_WAIT_GRACE_SECONDS", 10**9), self.assertRaises(Unbounded):
            run_launch(cluster, "--context", CONTEXT, "--checks", "dm")

    def line(self, out, prefix):
        return [ln for ln in out.splitlines() if ln.startswith(prefix)][0]

    def test_a_failed_namespace_delete_fails_the_run(self):
        cluster = FakeCluster()
        cluster.delete_fails = True
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_SETUP, out)
        self.assertIn("PASS dm: answer", out)
        self.assertIn("FAIL cleanup: namespace slack-test was not deleted", out)
        self.assertIn("OVERALL pass=2 fail=1 failed=cleanup", out)

    def test_another_runs_live_job_keeps_the_namespace(self):
        other = {"metadata": {"name": "slack-live-check-other", "labels": {launch.RUN_LABEL_KEY: "other"}}, "status": {}}
        cluster = FakeCluster()
        cluster.jobs = [other]
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertIn("kept: another run's Job is still running in it (slack-live-check-other)", out)
        self.assertNotIn(["namespace", "slack-test"], cluster.deleted)
        self.assertNotIn(["job", "slack-live-check-other"], cluster.deleted)

    def test_cleanup_deletes_an_interrupted_runs_job_then_the_namespace(self):
        # The launcher that owned it died mid-Job: --cleanup says no run is live.
        orphan = {"metadata": {"name": "slack-live-check-orphan", "labels": {launch.RUN_LABEL_KEY: "dead"}}, "status": {}}
        cluster = FakeCluster()
        cluster.namespaces["slack-test"] = {"app.kubernetes.io/name": "slack-live-check"}
        cluster.jobs = [orphan]
        code, out = run_launch(cluster, "--context", CONTEXT, "--cleanup")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertEqual(cluster.deleted, [["job", "slack-live-check-orphan"], ["namespace", "slack-test"]])
        self.assertIn("CLEANUP deleted this tool's Jobs still running in slack-test (slack-live-check-orphan)", out)
        self.assertIn("CLEANUP namespace slack-test deleted", out)
        self.assertNotIn("kept", out)

    def test_live_foreign_jobs_skips_own_finished_and_deleting_jobs(self):
        def job(name, run, status=None, deleting=False):
            meta = {"name": name, "labels": {launch.RUN_LABEL_KEY: run}}
            if deleting:
                meta["deletionTimestamp"] = "t"
            return {"metadata": meta, "status": status or {}}

        listing = {"items": [
            job("mine", "r1"),
            job("done", "r2", {"conditions": [{"type": "Failed", "status": "True"}]}),
            job("going", "r3", deleting=True),
            job("theirs", "r4"),
        ]}
        kubectl = launch.Kubectl(CONTEXT, lambda cmd, **_: subprocess.CompletedProcess(cmd, 0, json.dumps(listing), ""))
        self.assertEqual(launch.live_foreign_jobs(kubectl, "slack-test", {"r1"}), ["theirs"])


class LogStreamTest(unittest.TestCase):
    TYPE_LINE = "TYPE within 300s as @Lisa Listed (U0LISTED1) in your DM with @kage (D0001): Reply with PONG. slc-dm-abc123"

    def run_streaming(self, cluster, *argv):
        out = io.StringIO()
        fake = FakeClock()
        snapshots = []
        cluster.on_job_poll = lambda: snapshots.append(out.getvalue())
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = launch.main(with_bot(argv), runner=cluster, clock=fake.clock, sleep=fake.sleep)
        return code, out.getvalue(), snapshots

    def test_type_lines_reach_the_workstation_while_the_job_runs(self):
        cluster = FakeCluster()
        head = f"RUN r: checks=dm\nPASS preflight-listed: ok\n{self.TYPE_LINE}\n"
        done = head + 'PASS dm: answer\nEVIDENCE {"check": "dm", "passed": true, "sent_ts": "1.000101"}\nSUMMARY pass=2 fail=0\n'
        # The pod is not up for the first read, then prints up to its TYPE line and waits.
        cluster.job_log_progress = [None, head, head, done]
        cluster.job_running_polls = 3
        code, out, snapshots = self.run_streaming(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_OK, out)
        # Printed by the poll after it appeared, while the Job was still running, and once.
        self.assertNotIn(self.TYPE_LINE, snapshots[0])
        self.assertIn(self.TYPE_LINE, snapshots[1])
        self.assertNotIn("PASS dm", snapshots[2])
        self.assertEqual(out.count(self.TYPE_LINE), 1, out)
        self.assertEqual(out.count("PASS dm: answer"), 1, out)
        self.assertIn("OVERALL pass=2 fail=0", out)
        self.assertIn("Type each TYPE line's text in Slack", out)
        self.assertEqual(cluster.deleted[-1], ["namespace", "slack-test"])

    def test_a_line_still_being_written_waits_for_its_newline(self):
        cluster = FakeCluster()
        partial = "RUN r\nTYPE within 300s as @Lisa"
        whole = f"RUN r\n{self.TYPE_LINE}\nPASS dm: answer\n"
        cluster.job_log_progress = [partial, whole]
        cluster.job_running_polls = 1
        code, out, snapshots = self.run_streaming(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertIn("RUN r", snapshots[0])
        self.assertNotIn("TYPE within 300s as @Lisa", snapshots[0])
        self.assertNotIn("TYPE within 300s as @Lisa\n", out)
        self.assertEqual(out.count(self.TYPE_LINE), 1, out)

    def test_the_last_line_is_printed_without_its_newline_when_the_job_ends(self):
        cluster = FakeCluster()
        cluster.job_log_progress = ["PASS dm: answer\nSUMMARY pass=1 fail=0"]
        code, out, _ = self.run_streaming(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertIn("SUMMARY pass=1 fail=0", out)

    def test_a_log_unreadable_at_the_end_keeps_what_was_streamed(self):
        cluster = FakeCluster()
        cluster.job_log_progress = [f"{self.TYPE_LINE}\nPASS dm: answer\n", None]
        cluster.job_running_polls = 1
        code, out, _ = self.run_streaming(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertNotIn("no pod log", out)
        self.assertIn("OVERALL pass=1 fail=0", out)
        # Retried to its bound, then said so.
        failed = [ln for ln in out.splitlines() if ln.startswith("JOB ") and "the final log read failed" in ln]
        self.assertEqual(len(failed), 1, out)

    def test_a_failed_final_log_read_is_retried(self):
        # The verdict lands after the last good poll, and the first final read fails.
        cluster = FakeCluster()
        cluster.job_log_progress = [f"{self.TYPE_LINE}\n", None, None, f"{self.TYPE_LINE}\nPASS dm: answer\n"]
        cluster.job_running_polls = 1
        code, out, _ = self.run_streaming(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertIn("PASS dm: answer", out)
        self.assertNotIn("final log read failed", out)

    def test_streamed_lines_are_redacted(self):
        cluster = FakeCluster()
        cluster.job_log_progress = [f"TYPE within 300s as @x (U1) in here: {LEAKED}\n",
                                    f"TYPE within 300s as @x (U1) in here: {LEAKED}\nFAIL dm: x\n"]
        cluster.job_running_polls = 1
        code, out, _ = self.run_streaming(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        self.assertNotIn(LEAKED, out)
        self.assertIn("in here: [redacted]", out)


class PrincipalTest(unittest.TestCase):
    def ingress(self, ts, principal):
        return json.dumps({"level": "INFO", "msg": "ingress", "backendMessageId": ts, "principal": principal}) + "\n"

    def test_principal_matches_with_listed_placeholder(self):
        cluster = FakeCluster()
        cluster.gateway_logs += self.ingress("1.000101", "slack:U0LISTED1")
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm", "--expect-principal", "slack:{listed}")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertIn("PASS principal-dm: ingress for 1.000101 names principal=slack:U0LISTED1", out)

    def test_principal_mismatch_and_missing_fail(self):
        cluster = FakeCluster()
        cluster.gateway_logs += self.ingress("1.000101", "alice@example.com")
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm", "--expect-principal", "slack:{listed}")
        self.assertEqual(code, launch.EXIT_FAIL)
        self.assertIn("names principal=alice@example.com, expected slack:U0LISTED1", out)
        cluster = FakeCluster()
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm", "--expect-principal", "slack:{listed}")
        self.assertEqual(code, launch.EXIT_FAIL)
        self.assertIn("no ingress log line for backendMessageId=1.000101", out)

    def test_a_kubectl_error_in_the_lookup_is_its_fail(self):
        cluster = FakeCluster()
        cluster.fail_on = "-c gateway"
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm", "--expect-principal", "slack:{listed}")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        self.assertIn("PASS dm: answer", out)
        self.assertIn("FAIL principal: error: kubectl", out)
        self.assertIn("OVERALL pass=2 fail=1 failed=principal", out)
        self.assertNotIn("ERROR", out)

    def test_principal_fails_with_no_passing_turn(self):
        cluster = FakeCluster()
        cluster.job_log = "FAIL dm: no reply\nSUMMARY pass=0 fail=1\n"
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm", "--keep-going",
                               "--expect-principal", "slack:{listed}")
        self.assertEqual(code, launch.EXIT_FAIL)
        self.assertIn("FAIL principal: no passing listed turn to look up", out)


    def test_principals_are_judged_over_every_job_not_per_job(self):
        # dm,unlisted,restart without --wait-answer: the launcher splits dm into a Job of
        # its own, so the unlisted Job carries no listed turn. That is not a FAIL, and
        # the restart still runs.
        cluster = FakeCluster()
        cluster.job_logs = [
            'PASS dm: answer\nEVIDENCE {"check": "dm", "passed": true, "sent_ts": "1.000101", "author": "U0LISTED1"}\n',
            'PASS unlisted: refusal notice\nEVIDENCE {"check": "unlisted", "passed": true, "sent_ts": "1.000201", "author": "U0UNLIST1"}\n',
            'PASS restart: answer\nEVIDENCE {"check": "restart", "passed": true, "sent_ts": "1.000301", "author": "U0LISTED1"}\n',
        ]
        cluster.gateway_logs += self.ingress("1.000101", "slack:U0LISTED1") + self.ingress("1.000301", "slack:U0LISTED1")
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm,unlisted,restart", "--after-restart",
                               "--expect-principal", "slack:{listed}")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertNotIn("FAIL", out)
        self.assertIn("PASS principal-dm: ingress for 1.000101", out)
        self.assertIn("PASS principal-restart: ingress for 1.000301", out)
        self.assertIn("RESTART gateway rolled out", out)
        self.assertEqual(cluster.job_logs, [], "every Job ran")

    def test_principals_for_jobs_before_a_restart_are_read_before_it(self):
        # The restart replaces the gateway pods, and with them the ingress lines of
        # the Jobs before it: those are looked up before the restart, in one read.
        cluster = FakeCluster()
        cluster.job_logs = [
            'PASS dm: answer\nEVIDENCE {"check": "dm", "passed": true, "sent_ts": "1.000101", "author": "U0LISTED1"}\n',
            'PASS mention: answer\nEVIDENCE {"check": "mention", "passed": true, "sent_ts": "1.000201", "author": "U0LISTED1"}\n',
            'PASS restart: answer\nEVIDENCE {"check": "restart", "passed": true, "sent_ts": "1.000301", "author": "U0LISTED1"}\n',
        ]
        cluster.gateway_logs += "".join(self.ingress(ts, "slack:U0LISTED1") for ts in ("1.000101", "1.000201", "1.000301"))
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm,mention,restart", "--after-restart",
                               "--expect-principal", "slack:{listed}")
        self.assertEqual(code, launch.EXIT_OK, out)
        lines = out.splitlines()
        restart_at = next(i for i, ln in enumerate(lines) if ln.startswith("RESTART"))
        self.assertLess(next(i for i, ln in enumerate(lines) if ln.startswith("PASS principal-dm")), restart_at)
        self.assertLess(next(i for i, ln in enumerate(lines) if ln.startswith("PASS principal-mention")), restart_at)
        self.assertGreater(next(i for i, ln in enumerate(lines) if ln.startswith("PASS principal-restart")), restart_at)
        reads = [c for c in cluster.calls if c[3] == "logs" and "-c" in c and c[c.index("-c") + 1] == "gateway"]
        # One read before the restart, the poll for 'slack connected', one after.
        self.assertEqual(len(reads), 3, reads)


class RestartTest(unittest.TestCase):
    def test_restart_runs_the_command_without_a_shell_then_a_second_job(self):
        cluster = FakeCluster()
        cluster.job_log = "PASS restart: answer\nSUMMARY pass=1 fail=0\n"
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "restart",
                               "--restart-cmd", f"kubectl --context {CONTEXT} -n {AGENT_NS} rollout restart deploy/x")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertEqual(cluster.restart_runs, [["kubectl", "--context", CONTEXT, "-n", AGENT_NS, "rollout", "restart", "deploy/x"]])
        calls = self.gateway_log_calls(cluster)
        self.assertTrue(all(any(a.startswith("--since=") for a in c) for c in calls), calls)
        self.assertTrue(any(c[3] == "rollout" and c[4] == "status" for c in cluster.calls))
        job = yaml.safe_load(cluster.applied[2])
        args = pod_spec(job)["containers"][0]["args"]
        self.assertEqual(args[args.index("--checks") + 1], "restart")

    def gateway_log_calls(self, cluster):
        return [c for c in cluster.calls if c[3] == "logs" and any("a2a-gateway" in a for a in c)]

    def test_after_restart_skips_the_command_and_reads_whole_logs(self):
        cluster = FakeCluster()
        cluster.job_log = "PASS dm: answer\nPASS restart: answer\n"
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm,restart", "--after-restart")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertEqual(cluster.restart_runs, [])
        calls = self.gateway_log_calls(cluster)
        self.assertTrue(calls)
        self.assertFalse(any(a.startswith("--since") for c in calls for a in c), calls)
        self.assertEqual(len(cluster.applied), 5, "the namespace once, then a ConfigMap and Job before the restart and after")
        self.assertEqual([d for d in cluster.deleted if d[0] == "namespace"], [["namespace", "slack-test"]])

    def test_a_failing_restart_command_fails_the_check(self):
        cluster = FakeCluster()
        cluster.restart_returncode = 3
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "restart",
                               "--restart-cmd", f"kubectl --context {CONTEXT} rollout restart deploy/x")
        self.assertEqual(code, launch.EXIT_FAIL)
        self.assertIn("FAIL restart-cmd: exited 3: rollout refused", out)
        self.assertEqual(cluster.applied, [])

    def test_a_missing_restart_executable_fails_the_check(self):
        cluster = FakeCluster()
        cluster.restart_missing = True
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "restart",
                               "--restart-cmd", f"kubecl --context {CONTEXT} rollout restart deploy/x")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        self.assertIn("FAIL restart-cmd: could not run kubecl: No such file or directory", out)
        self.assertIn("OVERALL pass=0 fail=1 failed=restart-cmd", out)
        self.assertEqual(cluster.applied, [])

    def test_a_rollout_that_times_out_is_fail_restart_cmd(self):
        cluster = FakeCluster()
        cluster.fail_on = "rollout status"
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm,restart", "--after-restart", "--keep-going")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        self.assertIn("FAIL restart-cmd: error: kubectl", out)
        self.assertIn("OVERALL pass=2 fail=1 failed=restart-cmd", out)
        self.assertNotIn("ERROR", out)
        self.assertEqual(cluster.deleted[-1], ["namespace", "slack-test"])

    def test_an_unusable_restart_command_is_refused_before_any_cluster_call(self):
        for command in ("kubectl 'unbalanced", "   "):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()) as err, \
                    self.assertRaises(SystemExit):
                parse(["--context", CONTEXT, "--checks", "restart", "--restart-cmd", command])
            self.assertIn("--restart-cmd", err.getvalue())

    def test_a_failed_log_read_in_the_connected_poll_is_retried(self):
        # One TLS timeout among the poll's reads is not the gateway failing to connect.
        cluster = FakeCluster()
        cluster.job_log = "PASS restart: answer\nSUMMARY pass=1 fail=0\n"
        cluster.gateway_log_failures = 2
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "restart", "--after-restart")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertIn("RESTART gateway rolled out and logged 'slack connected'", out)
        self.assertIn("PASS restart: answer", out)

    def test_log_reads_failing_until_the_deadline_fail_restart_with_the_error(self):
        cluster = FakeCluster()
        cluster.gateway_log_failures = -1
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "restart", "--after-restart")
        self.assertEqual(code, launch.EXIT_FAIL, out)
        line = [ln for ln in out.splitlines() if ln.startswith("FAIL restart-cmd:")][0]
        self.assertIn("did not log 'slack connected'", line)
        self.assertIn("TLS handshake timeout", line)
        reads = [c for c in cluster.calls if c[3] == "logs" and any("a2a-gateway" in a for a in c)]
        self.assertGreaterEqual(len(reads), launch.SLACK_CONNECT_WAIT_SECONDS // launch.JOB_POLL_INTERVAL_SECONDS)
        self.assertEqual(cluster.applied, [])

    def test_restart_fails_when_the_gateway_never_reconnects(self):
        cluster = FakeCluster()
        cluster.gateway_logs = ""
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "restart", "--after-restart")
        self.assertEqual(code, launch.EXIT_FAIL)
        self.assertIn("did not log 'slack connected'", out)
        self.assertEqual(cluster.applied, [])


class ModesAndArgsTest(unittest.TestCase):
    def test_a_foreign_namespace_is_never_fenced_or_deleted(self):
        cluster = FakeCluster()
        cluster.namespaces[AGENT_NS] = {"kubernetes.io/metadata.name": AGENT_NS}
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm", "--namespace", AGENT_NS)
        self.assertEqual(code, launch.EXIT_SETUP)
        self.assertIn("is not this tool's", out)
        self.assertEqual(cluster.applied, [])
        self.assertEqual(cluster.deleted, [])
        code, out = run_launch(cluster, "--context", CONTEXT, "--cleanup", "--namespace", AGENT_NS)
        self.assertEqual(code, launch.EXIT_SETUP)
        self.assertEqual(cluster.deleted, [])

    def test_a_leftover_namespace_of_ours_is_reused_and_deleted(self):
        cluster = FakeCluster()
        cluster.namespaces["slack-test"] = {"app.kubernetes.io/name": "slack-live-check"}
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertEqual(cluster.deleted[-1], ["namespace", "slack-test"])

    def test_an_empty_namespace_answer_reads_as_absent(self):
        cluster = FakeCluster()
        cluster.empty_namespace_output = True
        code, out = run_launch(cluster, "--context", CONTEXT, "--cleanup")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertIn("already gone", out)
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_OK, out)

    def test_one_platformagent_is_required_to_discover_it(self):
        for agents in ([], [AGENT, "second"]):
            cluster = FakeCluster()
            cluster.platformagents = agents
            code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "legacy-socket")
            self.assertEqual(code, launch.EXIT_SETUP, out)
            self.assertIn(f"expected one PlatformAgent in {AGENT_NS}, found {len(agents)}", out)

    def test_a_sandbox_statefulset_without_containers_needs_image(self):
        cluster = FakeCluster()
        cluster.sandbox_containers = []
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_SETUP, out)
        self.assertIn("has no containers; pass --image", out)

    def test_namespace_output_that_is_not_json_is_an_error(self):
        cluster = FakeCluster()
        cluster.namespace_output = "garbage"
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        self.assertEqual(code, launch.EXIT_SETUP, out)
        self.assertIn("kubectl get namespace printed no JSON", out)
        self.assertEqual(cluster.applied, [])

    def test_cleanup_reports_a_failed_delete(self):
        cluster = FakeCluster()
        cluster.namespaces["slack-test"] = {"app.kubernetes.io/name": "slack-live-check"}
        cluster.delete_fails = True
        code, out = run_launch(cluster, "--context", CONTEXT, "--cleanup")
        self.assertEqual(code, launch.EXIT_SETUP)
        self.assertIn("not deleted", out)

    def test_restart_after_dm_settles_the_dm_in_a_job_of_its_own(self):
        cluster = FakeCluster()
        cluster.job_log = "PASS dm: answer\nPASS mention: status\nPASS thread: status\nPASS restart: answer\n"
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm,mention,thread,restart", "--after-restart")
        self.assertEqual(code, launch.EXIT_OK, out)
        jobs = [pod_spec(yaml.safe_load(doc))["containers"][0]["args"] for doc in cluster.applied[2::2]]
        self.assertEqual([a[a.index("--checks") + 1] for a in jobs], ["dm", "mention,thread", "restart"])
        self.assertEqual(["--wait-answer" in a for a in jobs], [True, False, False])
        # A forwarded --wait-answer applies to every check already: one Job, as before.
        cluster = FakeCluster()
        cluster.job_log = "PASS dm: answer\nPASS mention: answer\nPASS restart: answer\n"
        run_launch(cluster, "--context", CONTEXT, "--checks", "dm,mention,restart", "--after-restart", "--", "--wait-answer")
        jobs = [pod_spec(yaml.safe_load(doc))["containers"][0]["args"] for doc in cluster.applied[2::2]]
        self.assertEqual([a[a.index("--checks") + 1] for a in jobs], ["dm,mention", "restart"])

    def test_cleanup_validates_no_harness_flags(self):
        cluster = FakeCluster()
        cluster.namespaces["slack-test"] = {"app.kubernetes.io/name": "slack-live-check"}
        code, out = run_launch(cluster, "--context", CONTEXT, "--cleanup", "--checks", "home")
        self.assertEqual(code, launch.EXIT_OK, out)

    def test_cleanup_deletes_the_namespace_alone(self):
        cluster = FakeCluster()
        cluster.namespaces["slack-test"] = {"app.kubernetes.io/name": "slack-live-check"}
        code, out = run_launch(cluster, "--context", CONTEXT, "--cleanup")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertEqual(cluster.deleted, [["namespace", "slack-test"]])
        self.assertEqual(cluster.applied, [])

    def test_namespace_and_names_can_be_overridden(self):
        cluster = FakeCluster()
        code, out = run_launch(cluster, "--context", CONTEXT, "--checks", "dm", "--namespace", "other-ns",
                               "--service-account", "other-ksa", "--gsa", "x@p.iam.gserviceaccount.com")
        self.assertEqual(code, launch.EXIT_OK, out)
        setup = list(yaml.safe_load_all(cluster.applied[0]))
        self.assertEqual(setup[0]["metadata"]["name"], "other-ns")
        self.assertEqual(setup[1]["metadata"]["annotations"]["iam.gke.io/gcp-service-account"], "x@p.iam.gserviceaccount.com")
        self.assertEqual(cluster.deleted[-1], ["namespace", "other-ns"])

    def test_network_policy_fences_ingress_and_private_egress(self):
        cluster = FakeCluster()
        run_launch(cluster, "--context", CONTEXT, "--checks", "dm")
        policy = list(yaml.safe_load_all(cluster.applied[0]))[2]["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(policy["ingress"], [])
        https = [rule for rule in policy["egress"] if rule["ports"][0]["port"] == 443][0]
        self.assertEqual(https["to"][0]["ipBlock"]["except"], ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"])

    def test_legacy_socket_alone_creates_no_namespace(self):
        cluster = FakeCluster()
        run_launch(cluster, "--context", CONTEXT, "--checks", "legacy-socket")
        self.assertEqual(cluster.deleted, [])

    def test_render_prints_the_wi_binding(self):
        cluster = FakeCluster()
        code, out = run_launch(cluster, "--render")
        self.assertEqual(code, launch.EXIT_OK)
        self.assertIn("serviceAccount:bnaylor-kagents-dev.svc.id.goog[slack-test/slack-test-runner]", out)
        self.assertIn("add-iam-policy-binding ka-slack-test-runner@bnaylor-kagents-dev.iam.gserviceaccount.com", out)

    def test_render_touches_nothing(self):
        cluster = FakeCluster()
        code, out = run_launch(cluster, "--render")
        self.assertEqual(code, launch.EXIT_OK)
        self.assertEqual(cluster.calls, [])
        self.assertIn("kind: Job", out)

    def test_render_needs_no_bot_for_any_checks(self):
        # It runs nothing, so the README's `--render` with no `--` section works.
        for checks in ((), ("--checks", "legacy-socket")):
            with self.subTest(checks=checks):
                cluster = FakeCluster()
                code, out = run_launch(cluster, "--render", *checks)
                self.assertEqual(code, launch.EXIT_OK, out)
                self.assertIn("kind: Job", out)
                self.assertEqual(cluster.calls, [])
        # A bot that is passed is the one rendered.
        code, out = run_launch(FakeCluster(), "--render", "--", "--bot-name", "kage")
        self.assertEqual(code, launch.EXIT_OK, out)
        self.assertIn("--bot-name", out)

    def test_owned_flags_after_the_separator_are_refused(self):
        for flag in ("--checks", "--run-id=x", "--project", "--keep-going"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse(["--context", CONTEXT, "--", flag, "v"])

    def test_endpoint_overrides_after_the_separator_are_refused(self):
        harness_parser = harness.build_parser()
        suppressed = [opt for action in harness_parser._actions if action.help == argparse.SUPPRESS
                      for opt in action.option_strings]
        self.assertEqual(sorted(suppressed), ["--metadata-token-url", "--secret-manager-base", "--slack-api-base"])
        for flag in suppressed:
            for forwarded in ([flag, "https://attacker.example/"], [f"{flag}=https://attacker.example/"]):
                with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                    parse(["--context", CONTEXT, "--checks", "dm", "--", *forwarded])
                self.assertIn("is not a harness flag the launcher forwards", err.getvalue())

    def test_forwardable_flags_cover_every_other_harness_flag(self):
        # A new harness flag must be classified here: forwarded, owned, or refused as test-only.
        harness_flags = {opt for action in harness.build_parser()._actions for opt in action.option_strings}
        suppressed = {opt for action in harness.build_parser()._actions if action.help == argparse.SUPPRESS
                      for opt in action.option_strings}
        owned = set(launch.LAUNCHER_OWNED_HARNESS_FLAGS)
        self.assertTrue(launch.FORWARDABLE_HARNESS_FLAGS <= harness_flags)
        self.assertFalse(launch.FORWARDABLE_HARNESS_FLAGS & (suppressed | owned))
        self.assertEqual(launch.FORWARDABLE_HARNESS_FLAGS | suppressed | owned | {"-h", "--help"}, harness_flags)
        args, forwarded = parse(["--context", CONTEXT, "--checks", "dm", "--", "--channel", "ka-test",
                                             "--reply-timeout=30", "--wait-answer", "--type-timeout", "120"])
        self.assertEqual(forwarded, [*BOT_FLAGS, "--channel", "ka-test", "--reply-timeout=30", "--wait-answer",
                                     "--type-timeout", "120"])
        # The person's typing window is the operator's to set; the Job's deadline follows it.
        self.assertIn("--type-timeout", launch.FORWARDABLE_HARNESS_FLAGS)
        self.assertEqual(launch.job_deadline(["--checks", "dm", "--type-timeout", "900", *BOT_FLAGS], 0),
                         900 + 180 + launch.JOB_SETUP_ALLOWANCE_SECONDS + launch.JOB_START_ALLOWANCE_SECONDS)

    def test_token_source_is_not_forwarded(self):
        for forwarded in (["--token-source", "file"], ["--token-source=file"]):
            with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                parse(["--context", CONTEXT, "--checks", "dm", "--", *forwarded, "--listed-secret", "/mnt/listed"])
            self.assertIn("is not a harness flag the launcher forwards", err.getvalue())

    def test_non_finite_timeouts_fail_before_any_cluster_call(self):
        for value in ("inf", "nan"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse(["--context", CONTEXT, "--checks", "dm", "--", "--reply-timeout", value])
            with self.subTest(value=value, mode="render"), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                parse(["--render", "--", "--quiet-window", value])

    def test_an_unexpected_exception_is_an_error_line_not_a_traceback(self):
        def exploding(cmd, **_):
            raise RuntimeError(f"boom {LEAKED}")

        code, out = run_launch(exploding, "--context", CONTEXT, "--checks", "legacy-socket")
        self.assertEqual(code, launch.EXIT_SETUP, out)
        self.assertIn("ERROR unexpected RuntimeError: boom", out)
        self.assertNotIn(LEAKED, out)

    def test_an_invalid_home_match_fails_before_any_cluster_call(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse(["--context", CONTEXT, "--checks", "home", "--", "--home-channel", "c", "--home-match", "("])

    def test_abbreviations_cannot_slip_past_the_owned_flag_guard(self):
        for forwarded in (["--proj", "other"], ["--check", "home"], ["--keep"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse(["--context", CONTEXT, "--checks", "dm", "--", *forwarded])

    def test_bad_harness_flags_fail_before_any_cluster_call(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse(["--context", CONTEXT, "--checks", "home"])

    def test_job_deadline_follows_the_checks(self):
        cluster = FakeCluster()
        run_launch(cluster, "--context", CONTEXT, "--checks", "all", "--", "--reply-timeout", "300")
        job = yaml.safe_load(cluster.applied[2])
        # all is dm, mention, thread (two reply waits) and unlisted: four typed turns at the
        # default --type-timeout, and five reply waits.
        self.assertEqual(job["spec"]["activeDeadlineSeconds"],
                         300 * 5 + harness.DEFAULT_TYPE_TIMEOUT_SECONDS * 4
                         + launch.JOB_SETUP_ALLOWANCE_SECONDS + launch.JOB_START_ALLOWANCE_SECONDS)
        self.assertEqual(launch.job_deadline(["--checks", "dm", *BOT_FLAGS], 5000), 5000)

    def test_the_bot_needs_a_name_or_an_id_before_any_cluster_call(self):
        cluster = FakeCluster()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), self.assertRaises(SystemExit):
            launch.main(["--context", CONTEXT, "--checks", "dm"], runner=cluster, clock=FakeClock().clock, sleep=FakeClock().sleep)
        self.assertIn("pass --bot-name <your bot's name> or --bot-user-id <its member id>", out.getvalue())
        self.assertEqual(cluster.calls, [])
        for flags in (["--bot-name", "troisbocaux"], ["--bot-user-id", "U0BOT"]):
            _, forwarded = launch.parse_args(["--context", CONTEXT, "--checks", "dm", "--", *flags])
            self.assertEqual(forwarded, flags)
        # The launcher's own checks run no harness, so they need no bot.
        launch.parse_args(["--context", CONTEXT, "--checks", "legacy-socket"])

    def test_restart_needs_a_command_or_after_restart(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse(["--context", CONTEXT, "--checks", "restart"])

    def test_context_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse(["--checks", "dm"])

    def test_launcher_names_match_the_operator_source(self):
        cp = (REPO / "k8s-operator/internal/controller/credential_proxy_manifests.go").read_text()
        self.assertIn(f'const credentialProxyContainerName = "{launch.CREDENTIAL_PROXY_CONTAINER}"', cp)
        self.assertIn(f'return agent.Name + "{launch.CREDENTIAL_PROXY_SUFFIX}"', cp)
        a2a = (REPO / "k8s-operator/internal/controller/platformagent_a2a_manifests.go").read_text()
        self.assertIn(f'return agent.Name + "{launch.GATEWAY_SUFFIX}"', a2a)
        self.assertIn(f'Name:  "{launch.GATEWAY_CONTAINER}",', a2a)
        sandbox = (REPO / "k8s-operator/internal/controller/shell_sandbox_manifests.go").read_text()
        self.assertIn(f'return agent.Name + "{launch.SHELL_SANDBOX_SUFFIX}"', sandbox)
        proxy = (REPO / "agents/platform/scripts/credential_proxy.py").read_text()
        for marker in launch.LEGACY_RELAY_LOG_MARKERS:
            self.assertIn(f'"{marker}', proxy)
        self.assertIn('os.getenv("SLACK_BOT_TOKEN"', proxy)
        self.assertIn('os.getenv("SLACK_APP_TOKEN"', proxy)
        gateway = (REPO / "a2a/gateway/slack.go").read_text()
        self.assertIn(f's.log.Info("{launch.GATEWAY_SLACK_CONNECTED_MSG}"', gateway)
        ingress = (REPO / "a2a/gateway/gateway.go").read_text()
        self.assertIn(f'g.log.Info("{launch.GATEWAY_INGRESS_MSG}",', ingress)
        # The ingress line reads the message ID through taskStart since #2493;
        # for a typed turn it is still the backend message's own ID.
        self.assertIn('"backendMessageId", ts.MessageID,', ingress)
        self.assertIn("MessageID: msg.MessageID,", ingress)


if __name__ == "__main__":
    unittest.main()

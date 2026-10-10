#!/usr/bin/env python3
"""Workstation launcher for the Slack live check: one Kubernetes Job per run.

It renders manifests/job.yaml.template, ships harness.py in a ConfigMap, applies
both, and streams the pod's log here while the Job runs, so the harness's TYPE
lines reach the person who has to type each turn in Slack. Then it reconciles
the PASS/FAIL lines against the checks asked for and deletes both.
The checks that need kubectl run here, under the caller's own credentials and
the --context it pins on every call, never in the pod:

- restart: runs --restart-cmd (or trusts --after-restart), waits for the gateway
  rollout, then runs the harness's restart check (a DM) in a second Job.
- legacy-socket: proves the broker holds no Slack Socket Mode connection on a next
  install, from the credential-proxy Deployment's env and the broker's logs.
- --expect-principal: matches the gateway's ingress log line for each listed turn.

Before the first Job it applies manifests/setup.yaml.template (the namespace, the
Workload Identity ServiceAccount and the egress fence), idempotently, and it deletes
the namespace when the run ends; --cleanup deletes it alone. The GSA and its grants
are not the launcher's: --render prints the one binding it relies on, and prints
every manifest without touching the cluster.
Flags after `--` go to harness.py unchanged, but only those on FORWARDABLE_HARNESS_FLAGS:
the ones the launcher sets itself, and the harness's test-only endpoint overrides,
are refused. Never run in CI; see README.md.
"""

import argparse
import json
import os
import shlex
import string
import subprocess
import sys
import time
import uuid
from typing import Callable, Collection, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import harness  # noqa: E402 -- the sibling module, found through the path line above

HARNESS_PATH = os.path.join(HERE, "harness.py")
HARNESS_FILE_NAME = "harness.py"
SETUP_TEMPLATE = os.path.join(HERE, "manifests", "setup.yaml.template")
JOB_TEMPLATE = os.path.join(HERE, "manifests", "job.yaml.template")

DEFAULT_NAMESPACE = "slack-test"
DEFAULT_SERVICE_ACCOUNT = "slack-test-runner"
GSA_EMAIL_FORMAT = "{name}@{project}.iam.gserviceaccount.com"
DEFAULT_GSA_NAME = "ka-slack-test-runner"
DEFAULT_AGENT_NAMESPACE = "kubeagents-system"
APP_LABEL_KEY = "app.kubernetes.io/name"
APP_LABEL_VALUE = "slack-live-check"
RUN_LABEL_KEY = "slack-live-check/run"
JOB_NAME_PREFIX = "slack-live-check-"
# The Job's deadline is the harness's own worst-case wait (harness.time_budget) plus
# this, for reading the tokens, resolving the bot and channel, and the preflights.
JOB_SETUP_ALLOWANCE_SECONDS = 180
# activeDeadlineSeconds counts from the Job's start, so scheduling (a node scale-up
# on Autopilot) and a cold pull of the agent-sandbox image spend it too.
JOB_START_ALLOWANCE_SECONDS = 300
STDERR_TAIL_CHARS = 500
RUN_ID_TIME_FORMAT = "%Y%m%d%H%M%S"
RUN_ID_HEX_CHARS = 4
# The container name in manifests/job.yaml.template.
HARNESS_CONTAINER = "harness"
JOB_TTL_SECONDS = 600
JOB_POLL_INTERVAL_SECONDS = 5
JOB_WAIT_GRACE_SECONDS = 60
# How long the read of the log after the Job ends is retried, as the other waits retry theirs.
LOG_FINAL_READ_RETRY_SECONDS = 30
# The bot a --render shows in the Job's args when none is passed after --.
RENDER_BOT_PLACEHOLDER = "<your bot's member id>"
NAMESPACE_DELETE_TIMEOUT_SECONDS = 180
ROLLOUT_TIMEOUT_SECONDS = 600
SLACK_CONNECT_WAIT_SECONDS = 120
LOG_SINCE_MARGIN_SECONDS = 120

# The operator's names for what this reads (k8s-operator/internal/controller):
# credentialProxyName and credentialProxyContainerName (credential_proxy_manifests.go),
# a2aGatewayName and the gateway container (platformagent_a2a_manifests.go),
# shellSandboxName (shell_sandbox_manifests.go).
CREDENTIAL_PROXY_SUFFIX = "-credential-proxy"
CREDENTIAL_PROXY_CONTAINER = "envoy-credential-proxy"
GATEWAY_SUFFIX = "-a2a-gateway"
GATEWAY_CONTAINER = "gateway"
SHELL_SANDBOX_SUFFIX = "-shell"
SANDBOX_IMAGE_MARKER = "agent-sandbox"
# The pair that arms the broker's legacy Socket Mode relay: credential_proxy.py,
# serve(), starts SlackRelay only when both are non-empty, and the operator renders
# them on the broker only for the legacy consumer (legacySlackConsumer).
SLACK_PAIR_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
# credential_proxy.py, initialize_slack_relay: one of these is logged once the relay
# has been armed, whether or not its connection came up.
LEGACY_RELAY_LOG_MARKERS = ("Slack relay enabled", "Slack relay initialization failed")
# a2a/gateway/slack.go, Run: logged once auth.test succeeds, before Socket Mode connects.
GATEWAY_SLACK_CONNECTED_MSG = "slack connected"
# a2a/gateway/gateway.go, startTask: the plaintext join of chat message id and principal.
GATEWAY_INGRESS_MSG = "ingress"
PRINCIPAL_LISTED_PLACEHOLDER = "{listed}"
PRINCIPAL_CHECKS = (harness.CHECK_DM, harness.CHECK_MENTION, harness.CHECK_THREAD, harness.CHECK_RESTART)

CHECK_LEGACY_SOCKET = "legacy-socket"
# The verdict names of the launcher's other checks.
RESTART_RESULT = "restart-cmd"
PRINCIPAL_RESULT = "principal"
LAUNCHER_ONLY_CHECKS = (CHECK_LEGACY_SOCKET,)
ALL_CHECKS = harness.CHECK_ORDER + LAUNCHER_ONLY_CHECKS
PROJECT_FLAG = "--project"
CHECKS_FLAG = "--checks"
RUN_ID_FLAG = "--run-id"
KEEP_GOING_FLAG = "--keep-going"
# Harness flags the launcher sets itself; one after `--` would silently win.
LAUNCHER_OWNED_HARNESS_FLAGS = (CHECKS_FLAG, RUN_ID_FLAG, PROJECT_FLAG, KEEP_GOING_FLAG)
# The only harness flags that may follow `--`. Anything else is refused, the
# harness's suppressed endpoint overrides above all (--slack-api-base,
# --metadata-token-url, --secret-manager-base): they would send the user tokens, or
# the pod's Workload Identity token, to whatever host the workstation names.
FORWARDABLE_HARNESS_FLAGS = frozenset({
    "--listed-secret", "--unlisted-secret", "--bot-user-id", "--bot-name", "--channel",
    "--thread-ts", "--prompt", "--followup", "--type-timeout", "--wait-answer", "--reply-timeout",
    "--poll-interval", "--unlisted-via", "--unlisted-repeat", "--quiet-window", "--refusal-silence-ok",
    "--home-channel", "--home-since", "--home-timeout", "--home-match",
})
WAIT_ANSWER_FLAG = "--wait-answer"
RESULT_PREFIXES = ("PASS ", "FAIL ")
# wait_job's answer for a Job that no longer exists.
JOB_GONE = "gone"
# delete_namespace's outcomes. KEPT: another run's Job is still running in it (never under --cleanup).
CLEANUP_DONE = "done"
CLEANUP_KEPT = "kept"
CLEANUP_FAILED = "failed"
EVIDENCE_PREFIX = "EVIDENCE "

EXIT_OK = harness.EXIT_OK
EXIT_FAIL = harness.EXIT_FAIL
EXIT_SETUP = harness.EXIT_SETUP
say = harness.say


class LaunchError(Exception):
    """The launcher cannot go on: a kubectl call failed, or the install is not what it expects."""


class Kubectl:
    """kubectl with --context pinned on every call."""

    def __init__(self, context: str, runner: Callable = subprocess.run) -> None:
        self.context = context
        self.runner = runner

    def run(self, args: list[str], stdin: Optional[str] = None) -> str:
        cmd = ["kubectl", "--context", self.context, *args]
        # UTF-8 whatever the locale: a forwarded flag can carry any character into the Job.
        proc = self.runner(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", check=False)
        if proc.returncode != 0:
            raise LaunchError(f"{' '.join(cmd[:6])}... exited {proc.returncode}: {proc.stderr.strip()[-STDERR_TAIL_CHARS:]}")
        return proc.stdout

    def get_json(self, args: list[str]) -> dict:
        out = self.run([*args, "-o", "json"])
        try:
            return json.loads(out)
        except ValueError:
            raise LaunchError(f"kubectl {' '.join(args[:3])} printed no JSON: {harness.truncate(out)!r}") from None


def render(template_path: str, values: dict) -> str:
    with open(template_path, encoding="utf-8") as handle:
        return string.Template(handle.read()).substitute(values)


def selector_of(deployment: dict) -> str:
    labels = deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
    if not labels:
        raise LaunchError(f"{deployment.get('metadata', {}).get('name')} has no matchLabels selector")
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def container_of(deployment: dict, name: str) -> dict:
    for container in deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        if container.get("name") == name:
            return container
    raise LaunchError(f"{deployment.get('metadata', {}).get('name')} has no container {name}")


def env_names(container: dict) -> set[str]:
    return {env.get("name", "") for env in container.get("env", []) or []}


def pod_logs(kubectl: Kubectl, namespace: str, selector: str, container: str, since_seconds: int = 0) -> str:
    # --tail=-1: with a selector kubectl otherwise keeps only the last 10 lines per pod.
    args = ["logs", "-n", namespace, "-l", selector, "-c", container, "--tail=-1"]
    if since_seconds:
        args.append(f"--since={since_seconds}s")
    return kubectl.run(args)


def discover_agent(kubectl: Kubectl, namespace: str) -> str:
    items = kubectl.get_json(["get", "platformagents", "-n", namespace]).get("items", [])
    if len(items) != 1:
        raise LaunchError(f"expected one PlatformAgent in {namespace}, found {len(items)}; pass --agent-name")
    return items[0]["metadata"]["name"]


def discover_image(kubectl: Kubectl, namespace: str, agent: str) -> str:
    """The agent-sandbox image this install already pulls: it has python3 and no credentials."""
    sts = kubectl.get_json(["get", "statefulset", agent + SHELL_SANDBOX_SUFFIX, "-n", namespace])
    containers = sts.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        if SANDBOX_IMAGE_MARKER in container.get("image", ""):
            return container["image"]
    if containers:
        return containers[0]["image"]
    raise LaunchError(f"statefulset {agent}{SHELL_SANDBOX_SUFFIX} has no containers; pass --image")


def wi_binding_command(project: str, gsa_email: str, namespace: str, service_account: str) -> str:
    """The Workload Identity binding the run relies on: the cluster manager's, printed, never run."""
    return (
        f"gcloud iam service-accounts add-iam-policy-binding {gsa_email} --project={project} "
        f"--role=roles/iam.workloadIdentityUser "
        f"--member='serviceAccount:{project}.svc.id.goog[{namespace}/{service_account}]'"
    )


def yaml_quoted(value: str) -> str:
    """value as a YAML double-quoted scalar, escaping every character YAML would refuse or fold.

    Printable characters go through as they are. Escaped: the quote and the
    backslash; everything YAML's reader refuses raw (the C0 controls, DEL, the C1
    controls, U+FFFE and U+FFFF); and the characters YAML 1.1 folds as line breaks
    in a quoted scalar (U+0085, U+2028, U+2029), and the BOM.
    """
    out = []
    for ch in value:
        cp = ord(ch)
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif cp not in (0x2028, 0x2029, 0xFEFF) and (
                0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD or cp >= 0x10000):
            out.append(ch)
        elif cp <= 0xFF:
            out.append(f"\\x{cp:02x}")
        else:
            out.append(f"\\u{cp:04x}")
    return '"' + "".join(out) + '"'


def job_manifests(namespace: str, service_account: str, image: str, run_id: str, harness_args: list[str],
                  deadline_seconds: int) -> tuple[str, str, str]:
    """Returns (job name, ConfigMap JSON, Job YAML)."""
    job_name = JOB_NAME_PREFIX + run_id
    with open(HARNESS_PATH, encoding="utf-8") as handle:
        source = handle.read()
    labels = {APP_LABEL_KEY: APP_LABEL_VALUE, RUN_LABEL_KEY: run_id}
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": job_name, "namespace": namespace, "labels": labels},
        "data": {HARNESS_FILE_NAME: source},
    }
    job = render(JOB_TEMPLATE, {
        "JOB_NAME": job_name,
        "NAMESPACE": namespace,
        "RUN_ID": run_id,
        "SERVICE_ACCOUNT": service_account,
        "IMAGE": image,
        "CONFIGMAP_NAME": job_name,
        # One block-sequence item per arg, each a YAML double-quoted scalar: a JSON
        # string is not one (a raw DEL or C1 control passes json.dumps and YAML refuses it).
        "ARGS_YAML": "".join(f"\n            - {yaml_quoted(arg)}" for arg in harness_args),
        "DEADLINE_SECONDS": str(deadline_seconds),
        "TTL_SECONDS": str(JOB_TTL_SECONDS),
    })
    return job_name, json.dumps(configmap), job


def job_deadline(harness_args: list[str], floor: int) -> int:
    """activeDeadlineSeconds: the harness's worst-case wait for these arguments plus setup, or floor if larger."""
    budget = harness.time_budget(harness.parse_args(harness_args)) + JOB_SETUP_ALLOWANCE_SECONDS + JOB_START_ALLOWANCE_SECONDS
    return max(int(budget), floor)


def parse_harness_output(text: str) -> tuple[list[tuple[str, bool, str]], list[dict]]:
    """The harness's PASS/FAIL lines as (name, passed, line), and its EVIDENCE objects."""
    results = []
    evidence = []
    for line in text.splitlines():
        if line.startswith(RESULT_PREFIXES):
            verdict, _, rest = line.partition(" ")
            name = rest.split(":", 1)[0]
            results.append((name, verdict == "PASS", line))
        elif line.startswith(EVIDENCE_PREFIX):
            try:
                evidence.append(json.loads(line[len(EVIDENCE_PREFIX):]))
            except ValueError:
                continue
    return results, evidence


class LogStream:
    """The Job's pod log, printed here line by line while the Job runs.

    poll() reads the whole log and prints the complete lines it has not printed
    yet; a line without its newline waits for the next read. A read that fails (the
    pod not started, an API blip) is skipped: the next tick reads again. finish()
    reads once more after the Job ends, retrying a failed read for a short while, and
    prints the rest; for a Job wait_job found gone it reads once, since no retry can succeed.
    """

    def __init__(self, kubectl: "Kubectl", namespace: str, job_name: str,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.kubectl = kubectl
        self.clock = clock
        self.sleep = sleep
        self.namespace = namespace
        self.job_name = job_name
        self.text = ""
        self.printed = 0

    def _read(self) -> str:
        return self.kubectl.run(["logs", "-n", self.namespace, f"job/{self.job_name}", "-c", HARNESS_CONTAINER, "--tail=-1"])

    def _emit(self, text: str, final: bool) -> None:
        # The log only grows; a shorter read (a pod that is not there yet) adds nothing.
        if len(text) < len(self.text):
            return
        self.text = text
        lines = text.splitlines()
        if not final and text and not text.endswith("\n"):
            lines = lines[:-1]
        for line in lines[self.printed:]:
            say(line)
        self.printed = max(self.printed, len(lines))

    def poll(self) -> None:
        try:
            self._emit(self._read(), final=False)
        except LaunchError:
            return

    def finish(self, state: str = "") -> str:
        gone = state == JOB_GONE
        deadline = self.clock() + LOG_FINAL_READ_RETRY_SECONDS
        while True:
            try:
                self._emit(self._read(), final=True)
                return self.text
            except LaunchError as exc:
                if not gone and self.clock() < deadline:
                    self.sleep(JOB_POLL_INTERVAL_SECONDS)
                    continue
                if gone:
                    say(f"JOB {self.job_name}: the Job is gone, so its log could not be read again; "
                        f"lines after the last good read are missing: {exc}")
                elif self.text:
                    say(f"JOB {self.job_name}: the final log read failed, so lines after the last good read are missing: {exc}")
                else:
                    say(f"JOB {self.job_name}: no pod log ({exc})")
                self._emit(self.text, final=True)
                return self.text


class Launcher:
    def __init__(self, args: argparse.Namespace, forwarded: list[str], kubectl: Kubectl,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 runner: Callable = subprocess.run) -> None:
        self.args = args
        self.forwarded = forwarded
        self.kubectl = kubectl
        self.clock = clock
        self.sleep = sleep
        self.runner = runner
        self.results: list[tuple[str, bool, str]] = []
        self._agent = args.agent_name
        self._image = args.image
        self.namespace_applied = False
        self.run_ids: set[str] = set()

    @property
    def agent(self) -> str:
        if not self._agent:
            self._agent = discover_agent(self.kubectl, self.args.agent_namespace)
        return self._agent

    @property
    def image(self) -> str:
        if not self._image:
            self._image = discover_image(self.kubectl, self.args.agent_namespace, self.agent)
        return self._image

    def record(self, name: str, passed: bool, detail: str) -> bool:
        line = f"{'PASS' if passed else 'FAIL'} {name}: {detail}"
        say(line)
        self.results.append((name, passed, line))
        return passed

    def guarded(self, name: str, check: Callable[..., bool], *args) -> bool:
        """Runs one launcher-side check. A kubectl failure inside it is that check's FAIL, not the run's end."""
        try:
            return check(*args)
        except LaunchError as exc:
            return self.record(name, False, f"error: {exc}")

    def deployment(self, suffix: str) -> dict:
        return self.kubectl.get_json(["get", "deployment", self.agent + suffix, "-n", self.args.agent_namespace])

    # --- legacy-socket -----------------------------------------------------

    def legacy_socket(self) -> bool:
        """The broker's legacy relay holds no Socket Mode connection; the gateway holds the one there is.

        Reads, in order: the credential-proxy Deployment's broker container env
        (the operator renders the Slack pair there only for the legacy consumer),
        the broker's logs since its containers started (the relay logs a line when
        armed), and the gateway Deployment's env and logs as the positive control.
        """
        ns = self.args.agent_namespace
        broker = self.deployment(CREDENTIAL_PROXY_SUFFIX)
        broker_container = container_of(broker, CREDENTIAL_PROXY_CONTAINER)
        armed = sorted(env_names(broker_container) & set(SLACK_PAIR_ENV))
        if armed:
            return self.record(CHECK_LEGACY_SOCKET, False,
                               f"{self.agent}{CREDENTIAL_PROXY_SUFFIX}/{CREDENTIAL_PROXY_CONTAINER} carries {','.join(armed)}: "
                               "the legacy Slack relay is armed")
        if broker_container.get("envFrom"):
            return self.record(CHECK_LEGACY_SOCKET, False,
                               f"{CREDENTIAL_PROXY_CONTAINER} has envFrom, which could supply the Slack pair; inspect it by hand")
        logs = pod_logs(self.kubectl, ns, selector_of(broker), CREDENTIAL_PROXY_CONTAINER)
        hits = [line for line in logs.splitlines() if any(m in line for m in LEGACY_RELAY_LOG_MARKERS)]
        if hits:
            return self.record(CHECK_LEGACY_SOCKET, False, "the broker logged its Slack relay: " + harness.truncate(hits[-1]))
        gateway = self.deployment(GATEWAY_SUFFIX)
        missing = sorted(set(SLACK_PAIR_ENV) - env_names(container_of(gateway, GATEWAY_CONTAINER)))
        if missing:
            return self.record(CHECK_LEGACY_SOCKET, False,
                               f"the gateway lacks {','.join(missing)} too, so this is not a next install with Slack on the gateway")
        gw_logs = pod_logs(self.kubectl, ns, selector_of(gateway), GATEWAY_CONTAINER)
        connected = any(_json_msg(line) == GATEWAY_SLACK_CONNECTED_MSG for line in gw_logs.splitlines())
        if not connected:
            return self.record(CHECK_LEGACY_SOCKET, False,
                               "the broker is clean but the gateway has not logged 'slack connected' either")
        return self.record(CHECK_LEGACY_SOCKET, True,
                           f"broker {CREDENTIAL_PROXY_CONTAINER} has no Slack pair in env and no relay log line "
                           f"({len(logs.splitlines())} lines read); the gateway holds the pair and logged 'slack connected'")

    # --- the Job ------------------------------------------------------------

    def harness_args(self, checks: list[str]) -> tuple[str, list[str]]:
        run_id = time.strftime(RUN_ID_TIME_FORMAT, time.gmtime()) + "-" + uuid.uuid4().hex[:RUN_ID_HEX_CHARS]
        self.run_ids.add(run_id)
        args = [CHECKS_FLAG, ",".join(checks), RUN_ID_FLAG, run_id, PROJECT_FLAG, self.args.project]
        if self.args.keep_going:
            args.append(KEEP_GOING_FLAG)
        return run_id, args + self.forwarded

    def ensure_namespace(self) -> None:
        """Applies the namespace, ServiceAccount and NetworkPolicy; idempotent.

        Refuses a namespace that exists without this tool's label: the setup
        fences every pod in it and the run ends by deleting it.
        """
        if not self.namespace_applied:
            check_namespace_ownership(self.kubectl, self.args.namespace)
            self.kubectl.run(["apply", "-f", "-"], stdin=setup_manifest(self.args))
            self.namespace_applied = True
            say(f"SETUP namespace {self.args.namespace}, ServiceAccount {self.args.service_account} -> {self.args.gsa}")

    def run_job(self, checks: list[str], extra: Optional[list[str]] = None) -> list[dict]:
        self.ensure_namespace()
        run_id, hargs = self.harness_args(checks)
        hargs += extra or []
        image = self.image
        deadline = job_deadline(hargs, self.args.job_timeout)
        job_name, configmap, job = job_manifests(self.args.namespace, self.args.service_account, image, run_id, hargs,
                                                 deadline)
        ns = self.args.namespace
        say(f"JOB {job_name} in {ns}: checks={','.join(checks)} image={image}")
        say(f"JOB {job_name}: its log follows as the harness prints it. Type each TYPE line's text in Slack, "
            "as the user it names and where it says, before its wait runs out")
        started = self.clock()
        try:
            self.kubectl.run(["apply", "-f", "-"], stdin=configmap)
            self.kubectl.run(["apply", "-f", "-"], stdin=job)
            stream = LogStream(self.kubectl, ns, job_name, self.clock, self.sleep)
            state = self.wait_job(job_name, deadline, stream.poll)
            logs = stream.finish(state)
            results, evidence = parse_harness_output(harness.REDACTOR.redact(logs))
            self.results.extend(results)
            if state != "Complete" and not any(not passed for _, passed, _ in results):
                self.record("job", False, f"{job_name} ended {state} with no FAIL line; see the log above")
            # Every check this Job was asked for needs a verdict, whatever the Job's
            # state: an unreadable log, or a harness that stopped early, is no PASS.
            answered = {name for name, _, _ in results}
            for check in checks:
                if check not in answered:
                    self.record(check, False, f"no PASS or FAIL line for it in {job_name}'s log (the Job ended {state})")
            for ev in evidence:
                ev["job_started"] = started
            return evidence
        finally:
            for kind in ("job", "configmap"):
                try:
                    self.kubectl.run(["delete", kind, job_name, "-n", ns, "--ignore-not-found", "--wait=false"])
                except LaunchError as exc:
                    say(f"CLEANUP {kind}/{job_name} not deleted: {exc}")

    def wait_job(self, job_name: str, deadline_seconds: int, on_tick: Optional[Callable[[], None]] = None) -> str:
        """Polls the Job until it ends; on_tick runs first on every poll (the log stream)."""
        deadline = self.clock() + deadline_seconds + JOB_WAIT_GRACE_SECONDS
        while True:
            if on_tick is not None:
                on_tick()
            # One failed read (an auth-plugin refresh, a TLS timeout, a 5xx) is not
            # the Job's end, and giving up here would delete a Job mid-check. Read
            # again on the next tick; only reads failing up to the deadline count.
            # --ignore-not-found: a Job that is not there (deleted by hand, or with
            # its namespace) exits 0 and prints nothing, which ends the wait now. A
            # non-zero exit is still a read that failed, and is read again.
            read_error: Optional[LaunchError] = None
            try:
                out = self.kubectl.run(["get", "job", job_name, "-n", self.args.namespace, "--ignore-not-found", "-o", "json"])
            except LaunchError as exc:
                read_error, out = exc, "{}"
            if not out.strip():
                say(f"JOB {job_name} is gone: kubectl get job found no such Job in {self.args.namespace}")
                return JOB_GONE
            try:
                job = json.loads(out)
            except ValueError:
                read_error, job = LaunchError(f"kubectl get job {job_name} printed no JSON: {harness.truncate(out)!r}"), {}
            for cond in job.get("status", {}).get("conditions", []) or []:
                if cond.get("type") in ("Complete", "Failed") and cond.get("status") == "True":
                    return cond["type"]
            if self.clock() >= deadline:
                if read_error is not None:
                    say(f"JOB {job_name}: its status could not be read until the deadline; last error: {read_error}")
                    return "Unknown"
                try:
                    pods = self.kubectl.run(["get", "pods", "-n", self.args.namespace, "-l", f"job-name={job_name}", "-o", "wide"])
                except LaunchError as exc:
                    pods = f"(not listed: {exc})"
                say(f"JOB {job_name} did not finish in time; pods:\n{pods}")
                return "Timeout"
            self.sleep(JOB_POLL_INTERVAL_SECONDS)

    # --- reuse hooks --------------------------------------------------------

    def check_principals(self, turns: list[dict]) -> bool:
        """--expect-principal: the gateway's ingress line for each listed turn (principal_turns) names the expected principal."""
        since = int(self.clock() - min(ev.get("job_started", 0) for ev in turns)) + LOG_SINCE_MARGIN_SECONDS
        gateway = self.deployment(GATEWAY_SUFFIX)
        logs = pod_logs(self.kubectl, self.args.agent_namespace, selector_of(gateway), GATEWAY_CONTAINER, since)
        principals = {}
        for line in logs.splitlines():
            record = _json_record(line)
            if record.get("msg") == GATEWAY_INGRESS_MSG:
                principals[record.get("backendMessageId", "")] = record.get("principal", "")
        ok = True
        for ev in turns:
            expected = self.args.expect_principal.replace(PRINCIPAL_LISTED_PLACEHOLDER, ev.get("author", ""))
            name = f"principal-{ev['check']}"
            found = principals.get(ev["sent_ts"])
            if found is None:
                ok = self.record(name, False, f"no ingress log line for backendMessageId={ev['sent_ts']}") and ok
            elif found != expected:
                ok = self.record(name, False, f"ingress for {ev['sent_ts']} names principal={found}, expected {expected}") and ok
            else:
                ok = self.record(name, True, f"ingress for {ev['sent_ts']} names principal={found}") and ok
        return ok

    def restart(self) -> bool:
        ns = self.args.agent_namespace
        started = self.clock()
        if not self.args.after_restart:
            cmd = shlex.split(self.args.restart_cmd)
            say(f"RESTART running: {' '.join(cmd)}")
            try:
                proc = self.runner(cmd, capture_output=True, text=True, check=False)
            except OSError as exc:
                return self.record(RESTART_RESULT, False, f"could not run {cmd[0]}: {exc.strerror or exc}")
            if proc.returncode != 0:
                return self.record(RESTART_RESULT, False, f"exited {proc.returncode}: {proc.stderr.strip()[-STDERR_TAIL_CHARS:]}")
        self.kubectl.run(["rollout", "status", f"deployment/{self.agent}{GATEWAY_SUFFIX}", "-n", ns,
                          f"--timeout={ROLLOUT_TIMEOUT_SECONDS}s"])
        gateway = self.deployment(GATEWAY_SUFFIX)
        read_errors: list[LaunchError] = []

        def connected():
            # After a restart this run made, only lines since it count. Under
            # --after-restart the restart's time is unknown; the rollout has
            # finished, so the current pods' whole logs are the new pods'.
            since = 0 if self.args.after_restart else int(self.clock() - started) + LOG_SINCE_MARGIN_SECONDS
            # A failed read (a TLS timeout, an auth-plugin refresh, a 5xx) is read
            # again on the next tick until the deadline, as wait_job does.
            try:
                logs = pod_logs(self.kubectl, ns, selector_of(gateway), GATEWAY_CONTAINER, since)
            except LaunchError as exc:
                read_errors.append(exc)
                return None
            read_errors.clear()
            return True if any(_json_msg(line) == GATEWAY_SLACK_CONNECTED_MSG for line in logs.splitlines()) else None

        if harness.poll(connected, SLACK_CONNECT_WAIT_SECONDS, JOB_POLL_INTERVAL_SECONDS, self.clock, self.sleep) is None:
            last = f"; the last log read failed: {read_errors[-1]}" if read_errors else ""
            return self.record(RESTART_RESULT, False,
                               f"the gateway did not log 'slack connected' within {SLACK_CONNECT_WAIT_SECONDS}s{last}")
        say("RESTART gateway rolled out and logged 'slack connected'")
        return True

    # --- the run ------------------------------------------------------------

    def run(self, checks: list[str]) -> int:
        cleanup = CLEANUP_DONE
        try:
            self.run_checks(checks)
        finally:
            if self.namespace_applied:
                cleanup = delete_namespace(self.kubectl, self.args.namespace, self.run_ids)
        if cleanup == CLEANUP_FAILED:
            # The namespace holds the Workload-Identity-bound ServiceAccount: a run
            # that leaves it behind is not a clean run, whatever the checks said.
            self.record("cleanup", False, f"namespace {self.args.namespace} was not deleted; run --cleanup")
            self.summarize()
            return EXIT_SETUP
        return self.summarize()

    def run_checks(self, checks: list[str]) -> None:
        keep_going = self.args.keep_going

        def failed() -> bool:
            return any(not passed for _, passed, _ in self.results)

        pod_checks = [c for c in checks if c in harness.CHECK_ORDER and c != harness.CHECK_RESTART]
        restarting = harness.CHECK_RESTART in checks
        if CHECK_LEGACY_SOCKET in checks or restarting or self.args.expect_principal or (pod_checks and not self.args.image):
            # Finding the PlatformAgent is setup, not any one check's verdict.
            _ = self.agent
        if CHECK_LEGACY_SOCKET in checks:
            self.guarded(CHECK_LEGACY_SOCKET, self.legacy_socket)
        looked_up = False

        def look_up_principals(evidence: list[dict]) -> None:
            # Over every listed turn of the Jobs given, at once: a Job the launcher
            # split off with no listed turn in it is not a FAIL of its own.
            nonlocal looked_up
            turns = principal_turns(evidence)
            if self.args.expect_principal and turns and (keep_going or not failed()):
                looked_up = True
                self.guarded(PRINCIPAL_RESULT, self.check_principals, turns)

        jobs = [(pod_checks, [])] if pod_checks else []
        if restarting and harness.CHECK_DM in pod_checks and WAIT_ANSWER_FLAG not in self.forwarded:
            # restart DMs the same conversation: a dm task still running then would
            # take the restart's DM as a steer. Only dm waits for its answer; the
            # other checks keep the reading they were asked for.
            say(f"NOTE dm runs in a Job of its own with {WAIT_ANSWER_FLAG}, so its task has finished before the restart")
            rest = [c for c in pod_checks if c != harness.CHECK_DM]
            jobs = [([harness.CHECK_DM], [WAIT_ANSWER_FLAG])] + ([(rest, [])] if rest else [])
        evidence: list[dict] = []
        for job_checks, extra in jobs:
            if keep_going or not failed():
                evidence += self.run_job(job_checks, extra)
        # Before the restart: it replaces the gateway pods, and their logs hold
        # these Jobs' ingress lines.
        look_up_principals(evidence)
        if restarting and (keep_going or not failed()):
            if self.guarded(RESTART_RESULT, self.restart):
                look_up_principals(self.run_job([harness.CHECK_RESTART]))
        if self.args.expect_principal and not looked_up and (keep_going or not failed()):
            self.record(PRINCIPAL_RESULT, False, "no passing listed turn to look up in any of the run's Jobs")

    def summarize(self) -> int:
        passed = [name for name, ok, _ in self.results if ok]
        failed = [name for name, ok, _ in self.results if not ok]
        say(f"OVERALL pass={len(passed)} fail={len(failed)}" + (f" failed={','.join(failed)}" if failed else ""))
        return EXIT_FAIL if failed or not self.results else EXIT_OK


def principal_turns(evidence: list[dict]) -> list[dict]:
    """The passing listed turns in the evidence: the ones with an ingress line to look up."""
    return [ev for ev in evidence if ev.get("check") in PRINCIPAL_CHECKS and ev.get("passed") and ev.get("sent_ts")]


def check_namespace_ownership(kubectl: Kubectl, namespace: str) -> bool:
    """True if the namespace exists and is this tool's; False if it does not exist. Raises if it is someone else's."""
    # A field selector, not a name: a List comes back with no items when nothing
    # matches, where `get namespace <name>` would fail and --ignore-not-found would
    # print nothing at all.
    out = kubectl.run(["get", "namespace", "--field-selector", f"metadata.name={namespace}", "-o", "json"])
    try:
        items = json.loads(out).get("items", []) if out.strip() else []
    except ValueError:
        raise LaunchError(f"kubectl get namespace printed no JSON: {harness.truncate(out)!r}") from None
    if not items:
        return False
    labels = items[0].get("metadata", {}).get("labels", {}) or {}
    if labels.get(APP_LABEL_KEY) != APP_LABEL_VALUE:
        raise LaunchError(f"namespace {namespace} exists without {APP_LABEL_KEY}={APP_LABEL_VALUE}; "
                          "it is not this tool's to fence or delete, so pass another --namespace")
    return True


def live_foreign_jobs(kubectl: Kubectl, namespace: str, own_run_ids: Collection[str]) -> list[str]:
    """This tool's Jobs in the namespace that belong to another run and have not finished."""
    jobs = kubectl.get_json(["get", "jobs", "-n", namespace, "-l", f"{APP_LABEL_KEY}={APP_LABEL_VALUE}"]).get("items", [])
    live = []
    for job in jobs:
        meta = job.get("metadata", {})
        if meta.get("deletionTimestamp") or (meta.get("labels", {}) or {}).get(RUN_LABEL_KEY) in own_run_ids:
            continue
        conditions = job.get("status", {}).get("conditions", []) or []
        if any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True" for c in conditions):
            continue
        live.append(meta.get("name", "?"))
    return live


def delete_namespace(kubectl: Kubectl, namespace: str, own_run_ids: Collection[str] = frozenset(),
                     delete_live_jobs: bool = False) -> str:
    """Deletes the run's namespace, and with it the ServiceAccount, the fence and any Job left behind.

    Not while another run's Job is still running in it (CLEANUP_KEPT): that run is
    mid-check, and it deletes the namespace itself when it ends. delete_live_jobs is
    --cleanup, the operator saying no run is live: such a Job is an interrupted
    run's, whose launcher is gone, so it is deleted first and named.
    """
    try:
        if not check_namespace_ownership(kubectl, namespace):
            say(f"CLEANUP namespace {namespace} is already gone")
            return CLEANUP_DONE
        live = live_foreign_jobs(kubectl, namespace, own_run_ids)
        if live and delete_live_jobs:
            kubectl.run(["delete", "job", *live, "-n", namespace, "--ignore-not-found", "--wait=false"])
            say(f"CLEANUP deleted this tool's Jobs still running in {namespace} ({', '.join(live)}): "
                "under --cleanup no run is live, so they are an interrupted run's")
        elif live:
            say(f"CLEANUP namespace {namespace} kept: another run's Job is still running in it "
                f"({', '.join(live)}); that run deletes it when it ends")
            return CLEANUP_KEPT
        kubectl.run(["delete", "namespace", namespace, "--ignore-not-found", "--wait=true",
                     f"--timeout={NAMESPACE_DELETE_TIMEOUT_SECONDS}s"])
        say(f"CLEANUP namespace {namespace} deleted")
        return CLEANUP_DONE
    except LaunchError as exc:
        say(f"CLEANUP namespace {namespace} not deleted: {exc}")
        return CLEANUP_FAILED


def _json_record(line: str) -> dict:
    start = line.find("{")
    if start < 0:
        return {}
    try:
        record = json.loads(line[start:])
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def _json_msg(line: str) -> str:
    return str(_json_record(line).get("msg", ""))


def parse_checks(value: str) -> list[str]:
    return harness.parse_checks(value, ALL_CHECKS)


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    forwarded: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, forwarded = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(description="Run the Slack live check as a Job (hack/slack-live-check/README.md).",
                                     allow_abbrev=False,
                                     epilog="Arguments after -- go to harness.py.")
    parser.add_argument("--context", default="", help="kubectl context, pinned on every call (required unless --render)")
    parser.add_argument(CHECKS_FLAG, type=parse_checks, default=list(harness.CHECKS_ALL),
                        help=f"comma list from {', '.join(ALL_CHECKS)}; '{harness.CHECKS_ALL_ALIAS}' is {','.join(harness.CHECKS_ALL)}")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE, help="where the Job runs")
    parser.add_argument("--service-account", default=DEFAULT_SERVICE_ACCOUNT)
    parser.add_argument(PROJECT_FLAG, default=harness.DEFAULT_PROJECT, help="the project holding the token secrets")
    parser.add_argument("--gsa", default="", help=f"the GSA the ServiceAccount is bound to (default {DEFAULT_GSA_NAME}@<project>)")
    parser.add_argument("--agent-namespace", default=DEFAULT_AGENT_NAMESPACE)
    parser.add_argument("--agent-name", default="", help="the PlatformAgent; discovered when there is one")
    parser.add_argument("--image", default="", help="the Job's image; default is the install's agent-sandbox image")
    parser.add_argument("--job-timeout", type=int, default=0,
                        help="a floor for the Job's activeDeadlineSeconds; by default it is derived from the checks and their timeouts")
    parser.add_argument(KEEP_GOING_FLAG, action="store_true")
    restart = parser.add_mutually_exclusive_group()
    restart.add_argument("--restart-cmd", default="", help="command (no shell) the restart check runs first; pin its --context yourself")
    restart.add_argument("--after-restart", action="store_true", help="the restart already happened; just wait for the rollout and DM")
    parser.add_argument("--expect-principal", default="",
                        help=f"expected ingress principal per listed turn; {PRINCIPAL_LISTED_PLACEHOLDER} is the listed member id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cleanup", action="store_true",
                      help="delete the run namespace and this tool's Jobs in it (a run does this itself on exit) and stop")
    mode.add_argument("--render", action="store_true", help="print the manifests and exit; touches nothing")
    args = parser.parse_args(argv)
    owned = [arg for arg in forwarded if arg.split("=", 1)[0] in LAUNCHER_OWNED_HARNESS_FLAGS]
    if owned:
        parser.error(f"{', '.join(owned)} after -- would override the launcher's own; pass the launcher flag instead")
    refused = [arg for arg in forwarded if arg.startswith("--") and arg.split("=", 1)[0] not in FORWARDABLE_HARNESS_FLAGS]
    if refused:
        parser.error(f"{', '.join(refused)} after -- is not a harness flag the launcher forwards; "
                     f"forwardable: {', '.join(sorted(FORWARDABLE_HARNESS_FLAGS))}")
    if args.render and not any(arg.split("=", 1)[0] in ("--bot-name", "--bot-user-id") for arg in forwarded):
        # Rendering runs nothing, so it needs no bot; the Job it prints shows where one goes.
        forwarded = [*forwarded, "--bot-user-id", RENDER_BOT_PLACEHOLDER]
    pod_checks = [c for c in args.checks if c in harness.CHECK_ORDER]
    if pod_checks and not args.cleanup:
        # Fail here, not minutes later in the pod, on a harness flag that is wrong.
        harness.parse_args([CHECKS_FLAG, ",".join(pod_checks), *forwarded])
    if not args.context and not args.render:
        parser.error("--context is required")
    if harness.CHECK_RESTART in args.checks and not (args.restart_cmd or args.after_restart) and not (args.cleanup or args.render):
        parser.error("the restart check needs --restart-cmd or --after-restart")
    if args.restart_cmd:
        # Here, not after the first Job has spent its Slack traffic.
        try:
            restart_argv = shlex.split(args.restart_cmd)
        except ValueError as exc:
            parser.error(f"--restart-cmd cannot be split into words: {exc}")
        if not restart_argv:
            parser.error("--restart-cmd has no command in it")
    args.gsa = args.gsa or GSA_EMAIL_FORMAT.format(name=DEFAULT_GSA_NAME, project=args.project)
    return args, forwarded


def setup_manifest(args: argparse.Namespace) -> str:
    return render(SETUP_TEMPLATE, {"NAMESPACE": args.namespace, "SERVICE_ACCOUNT": args.service_account, "GSA_EMAIL": args.gsa})


def main(argv: Optional[list[str]] = None, runner: Callable = subprocess.run,
         clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> int:
    args, forwarded = parse_args(sys.argv[1:] if argv is None else argv)
    binding = wi_binding_command(args.project, args.gsa, args.namespace, args.service_account)
    try:
        if args.render:
            say(setup_manifest(args))
            render_args = [CHECKS_FLAG, ",".join(c for c in args.checks if c in harness.CHECK_ORDER) or harness.CHECK_DM] + forwarded
            _, _, job = job_manifests(args.namespace, args.service_account, args.image or "<agent-sandbox image>",
                                      "render", render_args, job_deadline(render_args, args.job_timeout))
            say("---")
            say(job)
            say("# the ConfigMap carries harness.py verbatim. The Workload Identity binding the run relies on:")
            say("# " + binding)
            return EXIT_OK
        kubectl = Kubectl(args.context, runner)
        if args.cleanup:
            # Not done on a namespace that is not this tool's. --cleanup says no run is
            # live, so this tool's Jobs still running there are deleted first.
            return EXIT_OK if delete_namespace(kubectl, args.namespace, delete_live_jobs=True) == CLEANUP_DONE else EXIT_SETUP
        return Launcher(args, forwarded, kubectl, clock, sleep, runner).run(args.checks)
    except LaunchError as exc:
        say(f"ERROR {exc}")
    except Exception as exc:  # noqa: BLE001 -- the traceback could carry a token; the redacted line is the report
        say(f"ERROR unexpected {type(exc).__name__}: {exc}")
    return EXIT_SETUP


if __name__ == "__main__":
    sys.exit(main())

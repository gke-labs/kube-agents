"""The smoke pipeline installs the web console on every lane and gates it.

`hack/ci-deploy.sh` used to install the chart with the console off, and
`deploy/docker/cloudbuild-ci.yaml` built every first-party image but the
console's, so the console was never run on a CI install (gke-labs/kube-agents#2462).
Now the build has a `web-console` step, section 2a hands the chart the
console's image on both image-source paths, step 5 turns it on, and
`gate_web_console` (defined in section 6a, run at the end of 6b) rolls
the console out, reads its routes through a port-forward, sends one chat
turn through it, reads the session back and checks the CR is not Degraded.

The tests lift the real sections and functions out of the real file and run
them, as the other `test_ci_deploy_*.py` files do, rather than grepping for
flags. The gate runs against a local HTTP server standing in for the
port-forward, with `kubectl` stubbed: every route answering passes; a 403 on
the chat route fails at once naming it; a 504 followed by a 200 is retried
through a fresh tunnel; a read route whose body lacks a key the page reads,
or is not JSON, fails naming the route; a Degraded CR after the turn fails, and
so does a CR read that returns nothing for the whole window, while one that
answers after empty reads is judged on the answer;
a rollout that never finishes fails before any request. The requests
themselves are hack/web-console-probe.sh's; its routes, keys, header and
session shape are pinned against
the console's Go source.
"""

import http.server
import json
import os
import pathlib
import re
import subprocess
import tempfile
import threading
import unittest

import yaml

from tests.test_ci_deploy_rc_images import fake_candidate_tree

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_PROBE = _REPO_ROOT / "hack" / "web-console-probe.sh"
_CLOUDBUILD = _REPO_ROOT / "deploy" / "docker" / "cloudbuild-ci.yaml"
_CONSOLE_DIR = _REPO_ROOT / "a2a" / "cmd" / "web-console"
_CONSOLE_MAIN = _CONSOLE_DIR / "main.go"
_CONSOLE_INSIGHTS = _CONSOLE_DIR / "insights.go"
_CONSOLE_ENVIRONMENT = _CONSOLE_DIR / "environment.go"
_CHART_TEMPLATE = _REPO_ROOT / "charts" / "kube-agents" / "templates" / "web-console.yaml"
_RELEASE_COMMON = _REPO_ROOT / "scripts" / "release" / "common.sh"

_AR_REPO = "us-central1-docker.pkg.dev/kube-agents-evals/kube-agents"
_TAG = "pr-2462-abc1234"
_NAMESPACE = "kubeagents-system"
_CONSOLE_NAME = "kube-agents-web-console"
_CR_READY = "Ready\tReconciled: all workloads ready"
_CR_DEGRADED = "Degraded\tPodUnschedulable: Pod platform-agent-gateway-x cannot be scheduled"
_CR_BUS_DEGRADED = "Degraded\tPodUnschedulable: Pod platform-agent-a2a-nats-2 cannot be scheduled onto any available node: 0/5 nodes are available"

_IMAGE_SOURCE_SECTION = (r"^# ─── 2a\. Image Source.*?", r"^# --- agent model required in a Prow job: begin")
_BUILD_SECTION = (r"^# ─── 4\. Build Container Images.*?", r"^# ─── 5\. Chart Deployment")
_GATE_SECTION = (r"^# ─── 6a\. The web console.*?", r"^# ─── 6b\. Gate the A2A stack")
_MODE_SECTION = (r"^# ─── 6b\. Gate the A2A stack.*?", r"^# ─── 7\. Agent API Connectivity")

_READ_ROUTES = (
    "/api/insights",
    "/api/environment",
    "/api/channels/alerts/posts",
    "/api/channels/scheduled/posts",
    "/api/models",
    "/api/repositories",
    "/api/patches",
)
_SESSION_ID_RE = re.compile(r"^web-console-[0-9a-f]{32}$")


def text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def lifted(start: str, stop: str) -> str:
    match = re.search(rf"{start}(?={stop})", text(_CI_DEPLOY), re.DOTALL | re.MULTILINE)
    if match is None:  # pragma: no cover - a re-banner should say so loudly
        raise AssertionError(f"no section matching {start!r} in {_CI_DEPLOY}")
    return match.group(0)


def shell_function(path: pathlib.Path, name: str) -> str:
    lines = text(path).splitlines()
    for index, line in enumerate(lines):
        if line == f"{name}() {{":
            for end in range(index, len(lines)):
                if lines[end] == "}":
                    return "\n".join(lines[index : end + 1])
            break
    raise AssertionError(f"{name}() not found in {path} in the shape this test lifts")


def constants(path: pathlib.Path) -> dict[str, str]:
    found = {}
    for line in text(path).splitlines():
        match = re.match(r"readonly ([A-Z0-9_]+)=(.*)$", line)
        if match:
            value = match.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            found[match.group(1)] = value
    return found


def constants_block(**overrides: str) -> str:
    """The script's readonly lines, with the named constants' values replaced:
    the gate's pauses shrink to nothing and its port becomes the test server's."""
    lines = []
    for line in text(_CI_DEPLOY).splitlines():
        if not line.startswith("readonly "):
            continue
        name = line[len("readonly ") :].partition("=")[0]
        if name in overrides:
            line = f"readonly {name}={overrides[name]}"
        lines.append(line)
    return "\n".join(lines)


def go_constant(path: pathlib.Path, name: str) -> str:
    match = re.search(rf"^\s*{name}\s*=\s*\"([^\"]+)\"", text(path), re.MULTILINE)
    if match is None:
        raise AssertionError(f"{name} not found in {path}")
    return match.group(1)


def run_bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\nunset RC_COMMIT_SHA PULL_NUMBER JOB_NAME\n{script}"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120,
    )


def build_step(step_id: str) -> dict:
    for step in yaml.safe_load(text(_CLOUDBUILD))["steps"]:
        if step.get("id") == step_id:
            return step
    raise AssertionError(f"cloudbuild-ci.yaml has no step with id {step_id}")


def run_build_step(step_id: str, subs: dict[str, str], docker_stub: str) -> subprocess.CompletedProcess:
    script = build_step(step_id)["args"][1].replace("$$", "$")
    for name, value in subs.items():
        script = script.replace(f"${name}", value)
    return run_bash(f"{docker_stub}\n{script}")


class CloudBuildStepTest(unittest.TestCase):
    def test_the_step_is_a_no_op_with_its_substitution_empty(self) -> None:
        result = run_build_step(
            "web-console",
            {"_WEB_CONSOLE_URI": ""},
            'docker() { echo "docker must not run with the substitution empty: $*" >&2; exit 99; }',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)

    def test_the_step_builds_the_console_from_the_a2a_module_and_pushes_it(self) -> None:
        uri = f"{_AR_REPO}/web-console:{_TAG}"
        result = run_build_step("web-console", {"_WEB_CONSOLE_URI": uri}, 'docker() { echo "docker $*"; }')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                f"docker build --platform linux/amd64 -t {uri} -f a2a/Dockerfile.web-console a2a",
                f"docker push {uri}",
            ],
        )

    def test_the_substitution_defaults_empty_and_the_images_list_is_unchanged(self) -> None:
        config = yaml.safe_load(text(_CLOUDBUILD))
        self.assertEqual(config["substitutions"]["_WEB_CONSOLE_URI"], "")
        self.assertNotIn("$_WEB_CONSOLE_URI", config["images"], "the step pushes from inside itself, like the a2a step")
        self.assertEqual(build_step("web-console")["waitFor"], ["a2a"], "it shares the Go module download layer with the a2a step")


class ImageSourceTest(unittest.TestCase):
    def run_image_source(self, rc_commit_sha: str) -> tuple[pathlib.Path, str, subprocess.CompletedProcess]:
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack, rc_sha = fake_candidate_tree(tmp, images_exist=True)
            script = "\n".join(
                [
                    f'SCRIPT_DIR="{fake_hack}"',
                    f'export RC_COMMIT_SHA="{rc_sha if rc_commit_sha == "candidate" else rc_commit_sha}"',
                    f'export AR_REPO="{_AR_REPO}"',
                    f'export TAG="{_TAG}"',
                    'export PULL_NUMBER="2462"',
                    f'export IMG="{_AR_REPO}/kube-agents-operator:{_TAG}"',
                    f'export AGENT_IMAGE="{_AR_REPO}/platform-agent"',
                    constants_block(),
                    lifted(*_IMAGE_SOURCE_SECTION),
                    'for arg in "${IMAGE_ARGS[@]}"; do echo "ARG=${arg}"; done',
                ]
            )
            result = run_bash(script)
            return fake_hack, rc_sha, result

    @staticmethod
    def image_args(result: subprocess.CompletedProcess) -> list[str]:
        return [line.partition("=")[2] for line in result.stdout.splitlines() if line.startswith("ARG=")]

    def test_the_pull_request_path_hands_the_chart_this_builds_console(self) -> None:
        _, _, result = self.run_image_source("")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.image_args(result)
        self.assertIn(f"webConsole.image.repository={_AR_REPO}/web-console", args)
        self.assertIn(f"webConsole.image.tag={_TAG}", args)

    def test_the_release_candidate_path_sets_the_console_tag_and_no_repository(self) -> None:
        """The chart defaults the console's repository to the published one
        (the same path scripts/release/common.sh requires the image under), so
        only the tag is said, as for every other image on this path."""
        _, rc_sha, result = self.run_image_source("candidate")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.image_args(result)
        self.assertIn(f"webConsole.image.tag={rc_sha}", args)
        self.assertFalse([arg for arg in args if arg.startswith("webConsole.image.repository")], args)
        self.assertIn('"web-console"', text(_RELEASE_COMMON), "the release must publish the image the candidate path installs")


class BuildSectionTest(unittest.TestCase):
    def run_build_section(self, *extra: str) -> subprocess.CompletedProcess:
        return run_bash(
            "\n".join(
                [
                    f'export AR_REPO="{_AR_REPO}"',
                    f'export TAG="{_TAG}"',
                    'export PROJECT_ID="kube-agents-evals"',
                    'export HERMES_AGENT_TAG="v0"',
                    'MODE_NEXT_BRIDGE_CONCURRENCY="4"',
                    # Section 2a sets it; the candidate path names the A2A images under it.
                    'RC_REGISTRY_PREFIX="ghcr.io/gke-labs/kube-agents"',
                    "BUILD_WORKER_ARGS=(--machine-type=e2-highcpu-8)",
                    "A2A_OPERATOR_ENV_ARGS=()",
                    'gcloud() { printf "%s\\n" "$@" >&2; case "$1 $2" in "builds submit") printf "b-1\\tu\\n" ;; "builds describe") echo SUCCESS ;; esac; }',
                    'timeout() { shift 2; "$@"; }',
                    *extra,
                    constants_block(),
                    lifted(*_BUILD_SECTION),
                ]
            )
        )

    @staticmethod
    def substitutions(result: subprocess.CompletedProcess) -> str:
        for line in result.stderr.splitlines():
            if line.startswith("--substitutions="):
                return line.partition("=")[2]
        raise AssertionError(f"no --substitutions in:\n{result.stdout}\n{result.stderr}")

    def test_the_build_names_the_console_image(self) -> None:
        result = self.run_build_section("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"_WEB_CONSOLE_URI={_AR_REPO}/web-console:{_TAG}", self.substitutions(result))

    def test_the_release_candidate_path_builds_nothing(self) -> None:
        result = self.run_build_section('export RC_COMMIT_SHA="b4ee5f3eb9c2aceb7f03460d2e573278ab9483fe"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--substitutions=", result.stderr)


class HelmInvocationTest(unittest.TestCase):
    def test_the_release_turns_the_console_on(self) -> None:
        helm_call = text(_CI_DEPLOY).partition('helm upgrade --install "${HELM_RELEASE_NAME}"')[2].partition("--wait")[0]
        self.assertIn('--set "webConsole.enabled=true"', helm_call)
        # The image flags ride IMAGE_ARGS from section 2a, where the two
        # paths differ; a second one here would win by being later.
        self.assertNotIn("webConsole.image", helm_call)


class CallSitesTest(unittest.TestCase):
    def test_the_gate_is_defined_in_6a_and_runs_once_at_the_end_of_6b(self) -> None:
        script = text(_CI_DEPLOY)
        gate_section = lifted(*_GATE_SECTION)
        self.assertNotRegex(gate_section, r"^gate_web_console\b", "6a defines the gate; 6b runs it")
        self.assertLess(script.index('echo "✓ Rollout verification finished'), script.index("# ─── 6a. The web console"))
        mode_section = lifted(*_MODE_SECTION)
        calls = re.findall(r"^gate_web_console.*$", mode_section, re.MULTILINE)
        self.assertEqual(calls, ["gate_web_console"])
        self.assertLess(
            mode_section.index('gate_mode_next_rollout "deployment/${PLATFORM_AGENT_CR_NAME}-a2a-verifier"'),
            mode_section.index("\ngate_web_console\n"),
        )
        self.assertLess(mode_section.index("\ngate_web_console\n"), mode_section.index('report_cr_status "a2a-gates-ready"'))

    def test_the_exit_trap_closes_the_gates_tunnel(self) -> None:
        self.assertIn('kill "${WEB_CONSOLE_PF_PID:-}" 2>/dev/null || true', shell_function(_CI_DEPLOY, "deploy_exit"))


class SourcePinsTest(unittest.TestCase):
    """The gate sends what the console accepts; a rename on the Go side is a
    failure here rather than a red lane."""

    def test_the_routes_header_and_session_shape_are_the_consoles(self) -> None:
        consts = constants(_PROBE)
        main_go = text(_CONSOLE_MAIN)
        self.assertEqual(consts["WEB_CONSOLE_READ_ROUTES"].split(), list(_READ_ROUTES))
        for route in ("/api/insights", "/api/environment", "/api/models", "/api/repositories", "/api/patches"):
            self.assertIn(f'mux.Handle("GET {route}"', main_go)
        self.assertIn('mux.Handle("GET /api/channels/{name}/posts"', main_go)
        self.assertIn('mux.HandleFunc("GET /healthz"', main_go)
        self.assertEqual(consts["WEB_CONSOLE_HEALTH_PATH"], "/healthz")
        self.assertIn('mux.Handle("POST /api/chat"', main_go)
        self.assertEqual(consts["WEB_CONSOLE_CHAT_PATH"], "/api/chat")
        self.assertIn('mux.Handle("GET /api/sessions/{id}/messages"', main_go)
        self.assertEqual(consts["WEB_CONSOLE_MESSAGES_PATH_FORMAT"], "/api/sessions/%s/messages")
        header, _, value = consts["WEB_CONSOLE_HEADER"].partition(": ")
        self.assertEqual(header, go_constant(_CONSOLE_MAIN, "consoleHeader"))
        self.assertEqual(value, go_constant(_CONSOLE_MAIN, "consoleHeaderValue"))
        self.assertEqual(consts["WEB_CONSOLE_SESSION_ID_PREFIX"], go_constant(_CONSOLE_MAIN, "sessionIDPrefix"))
        self.assertIn("[0-9a-f]{32}$", main_go, "sessionIDPattern: 32 hex digits, which 16 random bytes give")
        self.assertEqual(consts["WEB_CONSOLE_SESSION_ID_HEX_BYTES"], "16")
        self.assertIn('"alerts"', text(_CONSOLE_DIR / "channels.go"))
        self.assertIn('"scheduled"', text(_CONSOLE_DIR / "channels.go"))

    def test_the_keys_are_the_responses(self) -> None:
        consts = constants(_PROBE)
        self.assertEqual(consts["WEB_CONSOLE_INSIGHTS_KEYS"].split(), ["cluster"])
        self.assertIn('`json:"cluster"`', text(_CONSOLE_INSIGHTS))
        self.assertEqual(consts["WEB_CONSOLE_ENVIRONMENT_KEYS"].split(), ["versions", "counts"])
        for key in ("versions", "counts"):
            self.assertIn(f'`json:"{key}"`', text(_CONSOLE_ENVIRONMENT))
        main_go = text(_CONSOLE_MAIN)
        self.assertEqual(consts["WEB_CONSOLE_CHAT_KEYS"].split(), ["session_id", "reply"])
        # \s+ rather than the literal run of spaces: that is gofmt's alignment
        # to the struct's longest field, not the key.
        self.assertRegex(main_go, r'Reply\s+string\s+`json:"reply"`')
        self.assertEqual(consts["WEB_CONSOLE_MESSAGES_KEYS"].split(), ["messages"])
        self.assertRegex(main_go, r'Messages\s+\[\]sessionMessage\s+`json:"messages"`')
        self.assertRegex(main_go, r'Role\s+string\s+`json:"role"`')
        self.assertEqual(consts["WEB_CONSOLE_ROLE_ASSISTANT"], go_constant(_CONSOLE_MAIN, "roleAssistant"))

    def test_the_console_is_named_and_ported_as_the_chart_renders_it(self) -> None:
        consts = constants(_CI_DEPLOY)
        self.assertEqual(consts["WEB_CONSOLE_NAME"], '${HELM_RELEASE_NAME}-web-console')
        template = text(_CHART_TEMPLATE)
        self.assertIn('printf "%s-web-console" (.Release.Name', template)
        self.assertIn("containerPort: 8080", template)
        self.assertEqual(consts["WEB_CONSOLE_PORT"], "8080")
        self.assertEqual(consts["WEB_CONSOLE_IMAGE_NAME"], "web-console")
        self.assertIn("repository: ghcr.io/gke-labs/kube-agents/web-console", text(_REPO_ROOT / "charts" / "kube-agents" / "values.yaml"))

    def test_the_chat_deadline_outlasts_the_consoles_turn_timeout(self) -> None:
        consts = constants(_CI_DEPLOY)
        turn_timeout = re.search(r"^\s*turnTimeout\s*=\s*(\d+) \* time\.Second", text(_CONSOLE_MAIN), re.MULTILINE)
        self.assertIsNotNone(turn_timeout)
        self.assertGreater(int(consts["WEB_CONSOLE_CHAT_MAX_TIME_SECONDS"]), int(turn_timeout.group(1)))

    def test_the_deploy_sources_the_probe_rather_than_copying_it(self) -> None:
        """One copy of the requests and the judgement, in the probe file; a
        second copy in the deploy would be the drift this test exists for."""
        deploy, probe = text(_CI_DEPLOY), text(_PROBE)
        self.assertIn('source "${SCRIPT_DIR}/web-console-probe.sh"', deploy)
        for function in ("web_console_request", "web_console_expect_json", "web_console_expect_roles", "web_console_probe"):
            self.assertNotIn(f"{function}() {{", deploy)
            self.assertIn(f"{function}() {{", probe)
        self.assertIn("web_console_probe ", deploy)


class _Console(http.server.BaseHTTPRequestHandler):
    """A console answering as a2a/cmd/web-console does, as far as the gate
    reads, with one knob per failure case in `scenario`."""

    scenario: dict
    requests: list
    chat_calls: list

    def log_message(self, *args) -> None:  # noqa: D401 - silence the access log
        pass

    def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, doc: dict) -> None:
        self._send(code, json.dumps(doc).encode())

    def do_GET(self) -> None:
        self.requests.append(("GET", self.path, dict(self.headers)))
        sc = self.scenario
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
        elif self.path == "/":
            if sc.get("page_plain"):
                self._send(200, b"service unavailable", "text/plain")
            else:
                self._send(200, b"<!doctype html>\n<html lang=\"en\"><head><title>kube-agents</title></head></html>\n", "text/html")
        elif self.path == "/api/insights":
            if sc.get("insights_not_json"):
                self._send(200, b"<html>upstream error</html>", "text/html")
            else:
                self._json(200, {"model": {}, "usage": {}, "identity": {}, "cluster": {"name": "c"}, "repositories": []})
        elif self.path == "/api/environment":
            doc = {"versions": {"chart": "0.1.0"}, "install": {}, "counts": {"nodes": 3}, "read_at": "now"}
            if sc.get("environment_missing_counts"):
                del doc["counts"]
            self._json(200, doc)
        elif self.path in ("/api/channels/alerts/posts", "/api/channels/scheduled/posts"):
            self._json(200, {"channel": self.path.split("/")[3], "posts": []})
        elif self.path in ("/api/models", "/api/repositories", "/api/patches"):
            self._json(200, {"ok": True})
        elif self.path.startswith("/api/sessions/") and self.path.endswith("/messages"):
            sid = self.path[len("/api/sessions/") : -len("/messages")]
            messages = [{"id": 1, "role": "user", "content": "hi"}]
            if not sc.get("messages_no_assistant"):
                messages.append({"id": 2, "role": "assistant", "content": "pong"})
            self._json(200, {"session_id": sid, "latest_id": len(messages), "messages": messages})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        self.requests.append(("POST", self.path, dict(self.headers)))
        if self.path != "/api/chat":
            self._json(404, {"error": "not_found"})
            return
        if self.headers.get("X-Kube-Agents-Console") != "1":
            self._json(403, {"error": "missing_console_header"})
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._json(415, {"error": "json_required"})
            return
        body = json.loads(raw or b"{}")
        self.chat_calls.append(body)
        statuses = self.scenario.get("chat_statuses") or [200]
        status = statuses[min(len(self.chat_calls), len(statuses)) - 1]
        if status != 200:
            self._json(status, {"error": "turn_failed", "session_id": body.get("session_id", "")})
            return
        self._json(200, {"session_id": body.get("session_id", ""), "reply": "pong", "created": True})


class GateTest(unittest.TestCase):
    """gate_web_console, lifted with the functions it calls, against the
    console above; kubectl is a function whose port-forward sleeps, since the
    server already listens on the port the gate dials."""

    def setUp(self) -> None:
        self.requests: list = []
        self.chat_calls: list = []
        self.scenario: dict = {}
        handler = type("Console", (_Console,), {"scenario": self.scenario, "requests": self.requests, "chat_calls": self.chat_calls})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = pathlib.Path(self.tmp.name) / "kubectl-calls"
        self.calls.write_text("")

    def run_gate(self, cr: str = _CR_READY, rollout_rc: int = 0, cr_first: str = "", cr_first_reads: int = 0, **scenario) -> subprocess.CompletedProcess:
        self.scenario.update(scenario)
        script = "\n".join(
            [
                f'export NAMESPACE="{_NAMESPACE}"',
                f'export ARTIFACTS="{self.tmp.name}"',
                # The stub records every call; `rollout status` answers as the
                # case says; a port-forward is a sleep the gate kills; the CR
                # read answers the phase and condition the case gives, with
                # `cr_first` for the first `cr_first_reads` reads (the call
                # is logged before it is counted, so the count includes it).
                'kubectl() {',
                '  echo "kubectl $*" >>"${KUBECTL_CALLS}"',
                '  case "$1" in',
                '    port-forward) exec sleep 600 ;;',
                f'    rollout) return {rollout_rc} ;;',
                '    get) case "$*" in *platformagent*jsonpath*)',
                '      if [ "$(grep -c "^kubectl get platformagent" "${KUBECTL_CALLS}")" -le "${FAKE_CR_FIRST_READS}" ]; then printf "%s" "${FAKE_CR_FIRST}"; else printf "%s" "${FAKE_CR}"; fi ;;',
                '    esac ;;',
                '  esac',
                '  return 0',
                '}',
                f'source "{_PROBE}"',
                constants_block(
                    WEB_CONSOLE_LOCAL_PORT=str(self.server.server_address[1]),
                    WEB_CONSOLE_POLL_SECONDS="0",
                    WEB_CONSOLE_RETRY_SECONDS="0",
                    WEB_CONSOLE_PORT_FORWARD_WAIT_SECONDS="1",
                    WEB_CONSOLE_PF_LOG=f'"{self.tmp.name}/pf.log"',
                ),
                shell_function(_CI_DEPLOY, "report_cr_status"),
                lifted(*_GATE_SECTION),
                'gate_web_console',
                'echo "REACHED 6b"',
            ]
        )
        env = {**os.environ, "KUBECTL_CALLS": str(self.calls), "FAKE_CR": cr, "FAKE_CR_FIRST": cr_first, "FAKE_CR_FIRST_READS": str(cr_first_reads)}
        return run_bash(script, env=env)

    def kubectl_calls(self, prefix: str) -> list[str]:
        return [line for line in self.calls.read_text().splitlines() if line.startswith(f"kubectl {prefix}")]

    def test_every_route_answering_passes(self) -> None:
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("REACHED 6b", result.stdout)
        self.assertIn(f"✓ deployment/{_CONSOLE_NAME} rolled out", result.stdout)
        self.assertIn("every read route answered 200 with a JSON object", result.stdout)
        self.assertIn("/api/chat answered the turn on session web-console-", result.stdout)
        self.assertIn("shows the user and assistant rows", result.stdout)
        self.assertRegex(result.stdout, r"✓ web console verified in \d+s \(attempt 1/5; platform-agent is Ready: Reconciled: all workloads ready\)")
        # The rollout gate, then one tunnel to the console's Service.
        self.assertEqual(self.kubectl_calls("rollout"), [f"kubectl rollout status deployment/{_CONSOLE_NAME} -n {_NAMESPACE} --timeout=600s"])
        self.assertEqual(len(self.kubectl_calls("port-forward")), 1)
        self.assertTrue(self.kubectl_calls("port-forward")[0].startswith(f"kubectl port-forward svc/{_CONSOLE_NAME} -n {_NAMESPACE} "))
        self.assertTrue(self.kubectl_calls("port-forward")[0].endswith(":8080"))
        # The requests, in order: health, page, the read routes, the turn,
        # the session's messages.
        paths = [(method, path) for method, path, _ in self.requests]
        self.assertEqual(paths[:2], [("GET", "/healthz"), ("GET", "/")])
        self.assertEqual(paths[2:9], [("GET", route) for route in _READ_ROUTES])
        self.assertEqual(paths[9][0:2], ("POST", "/api/chat"))
        self.assertEqual(len(paths), 11)
        self.assertEqual(len(self.chat_calls), 1)
        sid = self.chat_calls[0]["session_id"]
        self.assertRegex(sid, _SESSION_ID_RE)
        self.assertEqual(self.chat_calls[0]["message"], "Reply with the single word pong. Do not run any tools.")
        self.assertEqual(paths[10], ("GET", f"/api/sessions/{sid}/messages"))
        # Every request carried the loopback Host the console requires, and
        # the turn the console header.
        for _, _, headers in self.requests:
            self.assertEqual(headers["Host"], f"127.0.0.1:{self.server.server_address[1]}")
        self.assertEqual(self.requests[9][2]["X-Kube-Agents-Console"], "1")
        self.assertTrue(self.requests[9][2]["Content-Type"].startswith("application/json"))

    def test_a_403_on_the_chat_route_fails_at_once_naming_it(self) -> None:
        """A refusal is deterministic: a fresh tunnel would get the same one."""
        result = self.run_gate(chat_statuses=[403])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("REACHED 6b", result.stdout)
        self.assertIn("web console attempt 1/5 failed: /api/chat: HTTP 403: ", result.stdout)
        self.assertIn("a fresh tunnel would not change that", result.stdout)
        self.assertIn("ERROR: the web console failed: /api/chat: HTTP 403: ", result.stdout)
        self.assertEqual(len(self.kubectl_calls("port-forward")), 1)
        self.assertEqual(len(self.chat_calls), 1)
        # The failure dump: the console's log and the CR's status.
        self.assertTrue(self.kubectl_calls(f"logs -n {_NAMESPACE} deployment/{_CONSOLE_NAME}"))
        self.assertTrue((pathlib.Path(self.tmp.name) / "platformagent-status-web-console.yaml").exists())

    def test_a_504_then_a_200_is_retried_through_a_fresh_tunnel(self) -> None:
        result = self.run_gate(chat_statuses=[504, 200])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("web console attempt 1/5 failed: /api/chat: HTTP 504: ", result.stdout)
        self.assertIn("respawning the tunnel", result.stdout)
        self.assertIn("✓ web console verified", result.stdout)
        self.assertIn("(attempt 2/5;", result.stdout)
        self.assertEqual(len(self.kubectl_calls("port-forward")), 2)
        self.assertEqual(len(self.chat_calls), 2)
        self.assertNotEqual(self.chat_calls[0]["session_id"], self.chat_calls[1]["session_id"], "a retry is a new session")

    def test_a_metadata_route_missing_a_key_the_page_reads_fails_naming_the_route(self) -> None:
        result = self.run_gate(environment_missing_counts=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ERROR: the web console failed: /api/environment: HTTP 200 but the object lacks counts: ", result.stdout)
        self.assertEqual(self.chat_calls, [], "nothing is sent to the agent once a read route has failed")
        self.assertEqual(len(self.kubectl_calls("port-forward")), 1)

    def test_a_route_that_is_not_json_fails_naming_the_route(self) -> None:
        result = self.run_gate(insights_not_json=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ERROR: the web console failed: /api/insights: HTTP 200 but the body is not JSON (", result.stdout)

    def test_a_page_that_is_not_the_console_fails(self) -> None:
        result = self.run_gate(page_plain=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ERROR: the web console failed: /: HTTP 200 but the body is not the console page (no <html): service unavailable", result.stdout)

    def test_a_session_without_the_assistants_row_gets_another_turn_then_fails(self) -> None:
        """A turn that ended with no text is the model's doing, not the
        tunnel's, so it gets the attempts a 504 gets before the gate fails."""
        result = self.run_gate(messages_no_assistant=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"web console attempt 1/5 failed: /api/sessions/web-console-[0-9a-f]{32}/messages: HTTP 200 but no assistant row among 1 message\(s\) \(roles: user\); respawning the tunnel")
        self.assertRegex(result.stdout, r"ERROR: the web console failed: /api/sessions/web-console-[0-9a-f]{32}/messages: HTTP 200 but no assistant row among 1 message\(s\) \(roles: user\)")
        self.assertEqual(len(self.kubectl_calls("port-forward")), 5)
        self.assertEqual(len(self.chat_calls), 5)

    def test_a_degraded_cr_after_the_turn_fails(self) -> None:
        result = self.run_gate(cr=_CR_DEGRADED)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("shows the user and assistant rows", result.stdout, "the CR is read after the turn, not instead of it")
        self.assertIn("ERROR: platform-agent is Degraded with the web console on; Ready condition: PodUnschedulable: Pod platform-agent-gateway-x cannot be scheduled", result.stdout)
        # Re-read the bounded number of times before failing.
        self.assertEqual(len(self.kubectl_calls("get platformagent platform-agent")), 12 + 2, "12 reads, then report_cr_status's two")

    def test_a_cr_read_that_returns_nothing_is_re_read_and_never_passes(self) -> None:
        # The read swallows a failed GET, so a dropped request and a status
        # with no phase both come back empty; neither is a pass.
        result = self.run_gate(cr="")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("shows the user and assistant rows", result.stdout, "the CR is read after the turn, not instead of it")
        self.assertIn("ERROR: platform-agent is unread with the web console on: all 12 reads after the turn returned no phase", result.stdout)
        self.assertNotIn("✓ web console verified", result.stdout)
        self.assertEqual(len(self.kubectl_calls("get platformagent platform-agent")), 12 + 2, "12 reads, then report_cr_status's two")

    def test_a_cr_read_that_returns_nothing_then_answers_is_judged_on_the_answer(self) -> None:
        # Two empty reads, then Ready: the answer passes the gate.
        result = self.run_gate(cr_first="", cr_first_reads=2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("platform-agent is Ready: Reconciled: all workloads ready)", result.stdout)
        self.assertEqual(len(self.kubectl_calls("get platformagent platform-agent")), 3)
        # Two empty reads, then Degraded for the rest of the window: the
        # last answering read is what the failure names.
        self.calls.write_text("")
        result = self.run_gate(cr=_CR_DEGRADED, cr_first="", cr_first_reads=2)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ERROR: platform-agent is Degraded with the web console on; Ready condition: PodUnschedulable: Pod platform-agent-gateway-x cannot be scheduled", result.stdout)
        self.assertEqual(len(self.kubectl_calls("get platformagent platform-agent")), 12 + 2)

    def test_a_bus_server_waiting_for_a_node_is_waited_for_then_judged(self) -> None:
        # The operator renders the NATS bus from its first pass; on an
        # Autopilot pool a server can wait for a node past the minute the
        # gate reads. Degraded for that reason alone earns the bus window,
        # and the CR is judged on what it says once the server schedules.
        result = self.run_gate(cr_first=_CR_BUS_DEGRADED, cr_first_reads=14)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("is Degraded while a NATS server waits for a node", result.stdout)
        self.assertIn("platform-agent is Ready: Reconciled: all workloads ready)", result.stdout)
        self.assertEqual(len(self.kubectl_calls("get platformagent platform-agent")), 15)

    def test_a_bus_server_still_unscheduled_after_the_window_fails_as_degraded(self) -> None:
        result = self.run_gate(cr=_CR_BUS_DEGRADED)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ERROR: platform-agent is Degraded with the web console on under today; Ready condition: PodUnschedulable: Pod platform-agent-a2a-nats-2", result.stdout)
        self.assertEqual(len(self.kubectl_calls("get platformagent platform-agent")), 12 + 120 + 2, "the reads, the bus window, then report_cr_status's two")

    def test_a_rollout_that_fails_stops_before_any_request(self) -> None:
        result = self.run_gate(rollout_rc=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"ERROR: deployment/{_CONSOLE_NAME} rollout failed", result.stdout)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.kubectl_calls("port-forward"), [])


if __name__ == "__main__":
    unittest.main()

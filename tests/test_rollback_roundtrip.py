"""hack/rollback-roundtrip.sh, run whole against a simulated install.

The script flips a `spec.mode: next` install to `today` and back and asserts
the JetStream PVC and the bus creds Secret come through with their UIDs. Here
it runs end to end, unmodified, with `kubectl` on PATH replaced by a small
simulator of the operator (FAKE_KUBECTL below: a CR patch bumps generations,
and a few kubectl calls later the "reconcile" lands, tearing the A2A stack
down under today and rendering it again under next) and both doors served by
a local HTTP server, so the today turn is a real POST and the bus task is the
lane's own inject client talking to a fake gateway. Each failure case changes
one thing about what the simulated operator does and checks the run stops on
the assertion that names it.

The wiring in hack/ci-eval-pr.sh is pinned at the end: it runs only under
EVAL_MODE_NEXT=1, after the suite verdict is computed and before it is
announced, and nothing it does reaches the exit status.
"""

import base64
import http.server
import json
import os
import pathlib
import re
import signal
import stat
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "hack" / "rollback-roundtrip.sh"
_CI_EVAL = _REPO_ROOT / "hack" / "ci-eval-pr.sh"
_OPERATOR = _REPO_ROOT / "k8s-operator"
_A2A_MANIFESTS = _OPERATOR / "internal" / "controller" / "platformagent_a2a_manifests.go"
_CONTROLLER = _OPERATOR / "internal" / "controller" / "platformagent_controller.go"
_COMMON_TYPES = _OPERATOR / "api" / "v1alpha1" / "common_types.go"
_CALLOUT_KEYS = _OPERATOR / "internal" / "controller" / "platformagent_a2a_calloutkeys.go"

_NS = "kubeagents-system"
_CR = "platform-agent"
_API_KEY = "agent-api-key"
_PVC = "data-platform-agent-a2a-nats-0"
_CREDS = "platform-agent-a2a-nats-creds"
_BRIDGE = {
    "name": "hermes-bridge",
    "image": "bridge:dev",
    "env": [
        {"name": "NATS_URL", "value": f"nats://platform-agent-a2a-nats.{_NS}.svc:4222"},
        {"name": "NATS_PASSWORD", "valueFrom": {"secretKeyRef": {"name": _CREDS, "key": "bridge-password"}}},
        {"name": "API_SERVER_KEY", "value": "loopback-value-that-must-not-be-logged"},
    ],
}
_OTHER_SIDECAR = {"name": "log-shipper", "image": "shipper:1", "env": [{"name": "X", "value": "y"}]}

# What both drivers start from instead of the caller's environment: a shell
# that drove the lane or the script by hand carries EVAL_MODE_NEXT, BUILD_ID,
# ROLLBACK_* knobs or a proxy, and any of them changes what is under test.
# The tool path and the locale only.
def minimal_env() -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    for name in ("HOME", "TMPDIR"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


# Bounds for a run that is meant to pass: never reached. The timeout cases
# lower the one they test.
_FAST_ENV = {
    "ROLLBACK_POLL_SECONDS": "0",
    "ROLLBACK_PRE_READY_TIMEOUT": "30",
    "ROLLBACK_GENERATION_TIMEOUT": "30",
    "ROLLBACK_ROLLOUT_TIMEOUT": "30",
    "ROLLBACK_READY_TIMEOUT": "30",
    "ROLLBACK_TEARDOWN_TIMEOUT": "30",
    "ROLLBACK_BUS_UP_TIMEOUT": "30",
    "ROLLBACK_BRIDGE_TIMEOUT": "30",
    "ROLLBACK_SETTLE_TIMEOUT": "30",
    "ROLLBACK_TURN_TIMEOUT": "5",
    "ROLLBACK_TURN_ATTEMPTS": "2",
    "ROLLBACK_BUS_TASK_TIMEOUT": "10",
}

# Every assertion a passing run prints, in order, for a CR that declares the
# bridge sidecar by hand (the case the unset and restore exist for; the
# operator's rendered bridge needs neither, test_no_sidecars_... below).
_PASSING_ORDER = [
    "pre.mode-next",
    "pre.ready",
    "pre.degraded-baseline",
    "pre.nats-ready",
    "pre.callout-serving",
    "pre.gateway-serving",
    "pre.pvc-present",
    "pre.creds-present",
    "pre.bus-task",
    "leg1.sidecars-unset.patched",
    "leg1.sidecars-unset.reconciled",
    "leg1.sidecars-unset.agent-rolled",
    "leg1.sidecars-unset.ready",
    "leg1.mode-today.patched",
    "leg1.mode-today.reconciled",
    "leg1.mode-today.agent-rolled",
    "leg1.mode-today.ready",
    "leg1.a2a-torn-down",
    "leg1.pvc-kept",
    "leg1.creds-kept",
    "leg1.nothing-stuck",
    "leg1.not-degraded",
    "leg1.today-turn",
    "leg2.mode-next.patched",
    "leg2.mode-next.reconciled",
    "leg2.nats-ready",
    "leg2.callout-serving",
    "leg2.provisioned",
    "leg2.mode-next.agent-rolled",
    "leg2.mode-next.ready",
    "leg2.pvc-kept",
    "leg2.nats-on-kept-pvc",
    "leg2.creds-kept",
    "leg2.sidecars-restored.patched",
    "leg2.sidecars-restored.reconciled",
    "leg2.sidecars-restored.agent-rolled",
    "leg2.sidecars-restored.ready",
    "leg2.reprovisioned",
    "leg2.bridge-consuming",
    "leg2.verifier-serving",
    "leg2.gateway-serving",
    "leg2.bus-task",
    "leg2.nothing-stuck",
    "leg2.not-degraded",
]

# The simulated operator. State is one JSON file; every kubectl call is one
# tick, and a CR patch's reconcile lands RECONCILE_TICKS calls after it. The
# scenario dict in the state says which part of the reconcile misbehaves.
FAKE_KUBECTL = textwrap.dedent(
    r'''
    #!/usr/bin/env python3
    import base64, json, os, sys, time

    STATE = os.environ["FAKE_KUBE_STATE"]
    RECONCILE_TICKS = 3
    A2A = ["statefulset/platform-agent-a2a-nats", "deployment/platform-agent-a2a-callout",
           "deployment/platform-agent-a2a-gateway", "deployment/platform-agent-a2a-verifier",
           "secret/platform-agent-a2a-callout-keys", "secret/platform-agent-a2a-inject"]

    def load():
        with open(STATE) as f:
            return json.load(f)

    def save(s):
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f)
        os.replace(tmp, STATE)

    def merge(dst, patch):
        for k, v in patch.items():
            if v is None:
                dst.pop(k, None)
            elif isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    def workload(kind, name, gen=1):
        return {"kind": kind, "metadata": {"name": name, "generation": gen},
                "spec": {"replicas": 1},
                "status": {"observedGeneration": gen, "updatedReplicas": 1, "readyReplicas": 1,
                           "availableReplicas": 1, "replicas": 1}}

    def bump(s, key, prefix):
        s["counters"][key] = s["counters"].get(key, 0) + 1
        return "%s-%d" % (prefix, s["counters"][key])

    def tick(s):
        s["tick"] += 1
        sc = s["scenario"]
        p = s.get("pending")
        if p and s["tick"] >= p["at"]:
            s["pending"] = None
            reconcile(s, p)
        d = s.get("provision_done_at")
        if d is not None and time.time() >= d:
            s["provision_done_at"] = None
            s["jobs"][-1]["status"]["conditions"] = [{"type": "Complete", "status": "True"}]
            set_ready(s)
        u = s.get("unschedulable_until")
        if u is not None and s["tick"] >= u:
            s["unschedulable_until"] = None
            set_ready(s)

    def set_ready(s, phase="Ready", reason="Reconciled", status="True"):
        cr = s["cr"]
        cr["status"] = {"phase": phase, "conditions": [
            {"type": "Ready", "status": status, "reason": reason, "message": "m",
             "observedGeneration": cr["metadata"]["generation"]}]}
        # Once armed, every later status write carries it, as the operator's
        # would for a cause that has not gone away; so does one the install
        # had before the run (degraded_baseline), until something replaces it.
        reason = "RBACIncomplete" if s.get("degraded_armed") else s["scenario"].get("degraded_baseline")
        if reason:
            cr["status"]["conditions"].append({"type": "Degraded", "status": "True", "reason": reason, "message": "m"})

    def reconcile(s, p):
        sc, cr, o = s["scenario"], s["cr"], s["objects"]
        mode = cr["spec"].get("mode", "today")
        dep = o["deployment/platform-agent-gateway"]
        dep["status"]["observedGeneration"] = dep["metadata"]["generation"]
        leg = "today" if mode == "today" else "next"
        if p["kind"] == "mode" and leg == "today":
            if sc.get("teardown_skipped") != "today":
                for key in A2A:
                    o.pop(key, None)
                o.pop("pod/platform-agent-a2a-nats-0", None)
                if sc.get("jobs_kept_on") != "today":
                    s["jobs"] = []
                s["pods"] = [x for x in s["pods"] if x["metadata"]["labels"].get("app.kubernetes.io/part-of") != "a2a-next"]
        if p["kind"] == "mode" and leg == "next":
            for key in A2A:
                kind, name = key.split("/")
                if kind == "secret":
                    o[key] = {"uid": bump(s, name, name), "data": {}}
                else:
                    o[key] = workload("StatefulSet" if kind == "statefulset" else "Deployment", name)
            token = bump(s, "token", "door-token")
            o["secret/platform-agent-a2a-inject"]["data"]["token"] = base64.b64encode(token.encode()).decode()
            claim = sc.get("nats_claim", "data-platform-agent-a2a-nats-0")
            o["pod/platform-agent-a2a-nats-0"] = {"spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]}}
            s["jobs"] = [job(bump(s, "job", "provision"), sc.get("provision_job", "Complete"))]
            if "pvc" in sc.get("replace_on_next", []):
                o["pvc/" + PVC]["uid"] = "pvc-new"
            if "creds" in sc.get("replace_on_next", []):
                o["secret/" + CREDS]["uid"] = "creds-new"
        if p["kind"] == "mode" and leg == "today":
            if "pvc" in sc.get("replace_on_today", []):
                o["pvc/" + PVC]["uid"] = "pvc-new"
            if "creds" in sc.get("replace_on_today", []):
                o["secret/" + CREDS]["uid"] = "creds-new"
            if "pvc" in sc.get("delete_on_today", []):
                o.pop("pvc/" + PVC, None)
        if p["kind"] == "sidecars" and leg == "next":
            s["jobs"].append(job(bump(s, "job", "provision"), "Complete"))
        if p["kind"] == "sidecars" and sc.get("status_stale_on_sidecars"):
            return
        if sc.get("stuck_pod_on") == leg and p["kind"] == "mode":
            s["pods"].append(sc["stuck_pod"])
        if sc.get("status_stale_on") == leg and p["kind"] == "mode":
            return
        if sc.get("never_ready_on") == leg and p["kind"] == "mode":
            set_ready(s, phase="Provisioning", reason="Provisioning", status="False")
            return
        if sc.get("refused_on") == leg and p["kind"] == "mode":
            set_ready(s, phase="Degraded", reason="A2AProvisionFailed", status="False")
            return
        if sc.get("unschedulable_on") == leg and p["kind"] == "mode":
            set_ready(s, phase="Degraded", reason="PodUnschedulable", status="False")
            s["cr"]["status"]["conditions"][0]["message"] = "0/3 nodes are available: 3 Insufficient cpu"
            s["unschedulable_until"] = s["tick"] + 6
            return
        if sc.get("degraded_on") == leg and p["kind"] == "mode":
            s["degraded_armed"] = True
        if sc.get("provision_seconds") and leg == "next" and p["kind"] == "mode":
            # The Job runs for a while, and under next the operator does not
            # report Ready until it is done (readSplitWorkloads).
            s["jobs"][-1]["status"]["conditions"] = []
            set_ready(s, phase="Provisioning", reason="Provisioning", status="False")
            s["provision_done_at"] = time.time() + sc["provision_seconds"]
            return
        set_ready(s)

    PVC = "data-platform-agent-a2a-nats-0"
    CREDS = "platform-agent-a2a-nats-creds"

    def job(name, cond):
        conds = [] if cond == "Running" else [{"type": cond, "status": "True"}]
        return {"metadata": {"name": name, "labels": {"app.kubernetes.io/part-of": "a2a-next",
                "kubeagents.x-k8s.io/a2a-component": "provision"}}, "status": {"conditions": conds}}

    def jsonpath(obj, path):
        if path == "{.metadata.uid}|{.metadata.deletionTimestamp}":
            return "%s|%s" % (obj.get("uid", ""), obj.get("deleting", ""))
        if path.startswith("{.data."):
            return obj.get("data", {}).get(path[len("{.data."):-1], "")
        if path == "{.spec.mode}":
            return obj["spec"].get("mode", "")
        if path == "{.metadata.generation}":
            return str(obj["metadata"]["generation"])
        if path == "{.spec.volumes[*].persistentVolumeClaim.claimName}":
            return " ".join(v["persistentVolumeClaim"]["claimName"] for v in obj["spec"]["volumes"])
        sys.exit("fake kubectl: unsupported jsonpath " + path)

    def port_forward(args, dead):
        # A tunnel to the test's door server, as kubectl's would be to the
        # Service. One that is dead accepts and drops every connection, the
        # way a tunnel whose stream the API server lost does.
        import socket, threading
        local = int(args[2].split(":")[0])
        with open(os.environ["FAKE_PF_PIDS"], "a") as f:
            f.write("%d\n" % os.getpid())
        srv = socket.socket()
        # What Go's net.Listen sets, so a port whose last tunnel left
        # connections in TIME_WAIT binds again; a port someone still
        # listens on does not.
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", local))
        except OSError as exc:
            sys.stderr.write("Unable to listen on port %d: %s\n" % (local, exc))
            return 1
        srv.listen(16)
        def pump(a, b):
            try:
                while True:
                    data = a.recv(65536)
                    if not data:
                        break
                    b.sendall(data)
            except OSError:
                pass
            finally:
                for x in (a, b):
                    try:
                        x.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        while True:
            conn, _ = srv.accept()
            if dead:
                conn.close()
                continue
            up = socket.create_connection(("127.0.0.1", int(os.environ["FAKE_DOOR_PORT"])))
            threading.Thread(target=pump, args=(conn, up), daemon=True).start()
            threading.Thread(target=pump, args=(up, conn), daemon=True).start()

    def main(argv):
        s = load()
        tick(s)
        s["calls"].append(argv)
        args = list(argv)
        if args[:1] == ["--context"]:
            s["contexts"].append(args[1]); args = args[2:]
        if args[:1] == ["-n"]:
            args = args[2:]
        verb = args[0]
        out, rc = "", 0
        refused = s["scenario"].get("mode_patch_refused")
        if verb == "patch" and refused and json.loads(args[args.index("-p") + 1]).get("spec", {}).get("mode") == refused:
            # An admission webhook, RBAC or the API server says no: the CR
            # is not changed.
            sys.stderr.write("Error from server (Forbidden): admission webhook denied the request\n"); rc = 1
        elif verb == "patch":
            patch = json.loads(args[args.index("-p") + 1])
            s["patches"].append(patch)
            cr = s["cr"]
            merge(cr["spec"], patch.get("spec", {}))
            cr["metadata"]["generation"] += 1
            dep = s["objects"]["deployment/platform-agent-gateway"]
            dep["metadata"]["generation"] += 1
            kind = "mode" if "mode" in patch.get("spec", {}) else "sidecars"
            # The status is left as it was: until the reconcile lands it is
            # the previous generation's Ready, as on a real install.
            s["pending"] = {"at": s["tick"] + RECONCILE_TICKS, "kind": kind}
            out = "patched"
        elif verb == "logs":
            sidecars = s["cr"]["spec"].get("deployment", {}).get("sidecars") or []
            if s["cr"]["spec"].get("mode") == "next" and sidecars and not s["scenario"].get("bridge_silent"):
                out = '{"msg":"hermes bridge consuming","profile":"platform"}'
        elif verb == "get":
            kind, rest = args[1], args[2:]
            name = rest[0] if rest and not rest[0].startswith("-") else ""
            ignore = "--ignore-not-found" in rest
            fmt = rest[rest.index("-o") + 1] if "-o" in rest else ""
            if kind == "platformagent":
                obj = s["cr"]
                out = json.dumps(obj) if fmt == "json" else jsonpath(obj, fmt[len("jsonpath="):])
            elif kind == "pods":
                out = json.dumps({"items": s["pods"]})
            elif kind == "jobs":
                # A Job that turns Failed after leg 2's provisioning read it
                # Complete: the second such read under next sees it Failed.
                flipped_forward = {"spec": {"mode": "next"}} in s["patches"]
                if (s["scenario"].get("job_fails_after_provisioned") and flipped_forward and s["jobs"]
                        and s["jobs"][-1]["status"]["conditions"] == [{"type": "Complete", "status": "True"}]):
                    n = s["counters"].get("complete-reads", 0) + 1
                    s["counters"]["complete-reads"] = n
                    if n >= 2:
                        s["jobs"][-1]["status"]["conditions"] = [{"type": "Failed", "status": "True"}]
                out = json.dumps({"items": s["jobs"]})
            elif (kind == "deployment" and name == "platform-agent-gateway" and fmt == "jsonpath={.metadata.generation}"
                  and s["scenario"].get("generation_unreadable_after_unset") and len(s["patches"]) == 1 and s["pending"] is None):
                # Once the unset has reconciled, the agent Deployment cannot
                # be read: the flip to today is never sent.
                sys.stderr.write("Error from server (ServiceUnavailable): the server is currently unable to handle the request\n"); rc = 1
            elif (kind == "deployment" and name == "platform-agent-gateway" and fmt == "json"
                  and s["scenario"].get("garbled_agent_after_unset") and s["patches"]):
                out = "not json"
            else:
                obj = s["objects"].get("%s/%s" % (kind, name))
                if obj is None:
                    if not ignore:
                        sys.stderr.write('Error from server (NotFound): %s "%s" not found\n' % (kind, name)); rc = 1
                elif fmt == "json":
                    out = json.dumps(obj)
                elif fmt == "name":
                    out = "%s/%s" % (kind, name)
                else:
                    out = jsonpath(obj, fmt[len("jsonpath="):])
        elif verb == "port-forward":
            n = s["counters"].get("port-forward", 0)
            s["counters"]["port-forward"] = n + 1
            save(s)
            return port_forward(args, dead=bool(s["scenario"].get("dead_first_tunnel")) and n == 0)
        else:
            sys.exit("fake kubectl: unsupported " + " ".join(argv))
        save(s)
        sys.stdout.write(out)
        return rc

    sys.exit(main(sys.argv[1:]))
    '''
).lstrip()


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def workload(kind: str, name: str, gen: int = 1) -> dict:
    return {
        "kind": kind,
        "metadata": {"name": name, "generation": gen},
        "spec": {"replicas": 1},
        "status": {"observedGeneration": gen, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1, "replicas": 1},
    }


def healthy_next_state(sidecars: list | None = None, **scenario) -> dict:
    sidecars = [_BRIDGE] if sidecars is None else sidecars
    objects = {
        f"pvc/{_PVC}": {"uid": "pvc-1"},
        f"secret/{_CREDS}": {"uid": "creds-1", "data": {"bridge-password": b64("pw")}},
        "secret/platform-agent-secrets": {"uid": "s", "data": {"API_SERVER_KEY": b64(_API_KEY)}},
        "secret/platform-agent-a2a-inject": {"uid": "inject-1", "data": {"token": b64("door-token-0")}},
        "secret/platform-agent-a2a-callout-keys": {"uid": "keys-1", "data": {}},
        "statefulset/platform-agent-a2a-nats": workload("StatefulSet", "platform-agent-a2a-nats"),
        "deployment/platform-agent-a2a-callout": workload("Deployment", "platform-agent-a2a-callout"),
        "deployment/platform-agent-a2a-gateway": workload("Deployment", "platform-agent-a2a-gateway"),
        "deployment/platform-agent-a2a-verifier": workload("Deployment", "platform-agent-a2a-verifier"),
        "deployment/platform-agent-gateway": workload("Deployment", "platform-agent-gateway", 7),
        "pod/platform-agent-a2a-nats-0": {"spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": _PVC}}]}},
    }
    spec: dict = {"mode": "next", "harness": {}}
    if sidecars:
        spec["deployment"] = {"sidecars": sidecars}
    cr = {
        "metadata": {"name": _CR, "generation": 4},
        "spec": spec,
        "status": {"phase": "Ready", "conditions": [{"type": "Ready", "status": "True", "reason": "Reconciled", "message": "m", "observedGeneration": 4}]},
    }
    if scenario.get("degraded_baseline"):
        cr["status"]["conditions"].append({"type": "Degraded", "status": "True", "reason": scenario["degraded_baseline"], "message": "m"})
    pods = [
        {"metadata": {"name": "platform-agent-gateway-abc", "labels": {}}, "status": {"phase": "Running", "containerStatuses": [{"name": "platform-agent", "state": {"running": {}}}]}},
        {"metadata": {"name": "platform-agent-a2a-nats-0", "labels": {"app.kubernetes.io/part-of": "a2a-next"}}, "status": {"phase": "Running"}},
    ]
    jobs = [
        {"metadata": {"name": "provision-0", "labels": {"app.kubernetes.io/part-of": "a2a-next"}}, "status": {"conditions": [{"type": "Complete", "status": "True"}]}},
    ]
    return {"tick": 0, "cr": cr, "objects": objects, "pods": pods, "jobs": jobs, "pending": None, "scenario": scenario, "calls": [], "patches": [], "contexts": [], "counters": {}}


class _Door(http.server.BaseHTTPRequestHandler):
    """The agent API and the inject door, answering from the simulated state."""

    state_path: pathlib.Path
    cancels_path: pathlib.Path

    def log_message(self, *args) -> None:  # noqa: D401 - silence the default access log
        pass

    def _state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _door_token(self, state: dict) -> str:
        secret = state["objects"].get("secret/platform-agent-a2a-inject")
        return base64.b64decode(secret["data"]["token"]).decode() if secret else ""

    def _door_up(self, state: dict) -> bool:
        return state["cr"]["spec"].get("mode") == "next" and "deployment/platform-agent-a2a-gateway" in state["objects"]

    def do_POST(self) -> None:
        state = self._state()
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        auth = self.headers.get("Authorization", "")
        if self.path == "/v1/responses":
            if auth != f"Bearer {_API_KEY}" or state["scenario"].get("today_turn_fails"):
                self._reply(401, {"error": "no"})
                return
            self._reply(200, {"output": [{"role": "assistant", "content": "pong"}]})
            return
        if self.path.startswith("/conversations/") and self.path.endswith("/cancel"):
            with self.cancels_path.open("a") as cancels:
                cancels.write(f"{body.get('taskId', '')}\n")
            self._reply(200, {"cancelPublished": True})
            return
        if self.path == "/inject":
            if not self._door_up(state):
                self._reply(503, {"error": "no gateway"})
                return
            busy = self.cancels_path.with_name("door-busy-once")
            if state["scenario"].get("door_busy_once") and not busy.exists():
                busy.touch()
                self._reply(503, {"error": "gateway starting"})
                return
            if state["scenario"].get("door_refuses"):
                self._reply(state["scenario"]["door_refuses"], {"error": "refused"})
                return
            if auth != f"Bearer {self._door_token(state)}":
                self._reply(401, {"error": "stale token"})
                return
            self._reply(200, {"taskId": "task-" + body["conversation"].split("/")[1], "conversation": "inject:" + body["conversation"]})
            return
        self._reply(404, {})

    def do_GET(self) -> None:
        state = self._state()
        if not self.path.startswith("/conversations/"):
            self._reply(404, {})
            return
        conversation = self.path[len("/conversations/") :].split("?")[0]
        if "task=" not in self.path:
            self._reply(200, {"entries": [], "lastSeq": 0})
            return
        leg = "pre" if "pre.bus-task" in conversation else "leg2"
        if state["scenario"].get("bus_task_lost_on") == leg:
            self._reply(503, {"error": "gateway restarting"})
            return
        if state["scenario"].get("bus_task_hangs_on") == leg:
            self._reply(200, {"entries": [], "lastSeq": 0})
            return
        final = "failed" if state["scenario"].get("bus_task_fails_on") == leg else "completed"
        task = re.search(r"task=([^&]+)", self.path).group(1)
        self._reply(200, {"entries": [{"kind": "terminal", "seq": 1, "taskId": task, "state": final}], "lastSeq": 1})


class Sim:
    """One simulated install, its doors, and a run of the script against it."""

    def __init__(self, state: dict) -> None:
        self.dir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.dir.name)
        self.state_path = root / "state.json"
        self.state_path.write_text(json.dumps(state))
        self.bin = root / "bin"
        self.bin.mkdir()
        kubectl = self.bin / "kubectl"
        kubectl.write_text(FAKE_KUBECTL)
        kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
        self.results = root / "results.txt"
        self.pf_pids = root / "pf_pids"
        self.cancels = root / "cancels"
        self.cancels.touch()
        handler = type("Door", (_Door,), {"state_path": self.state_path, "cancels_path": self.cancels})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def env(self, **extra: str) -> dict:
        url = f"http://127.0.0.1:{self.server.server_address[1]}"
        env = minimal_env()
        env.update(_FAST_ENV)
        env.update(
            {
                "PATH": f"{self.bin}:{env['PATH']}",
                "FAKE_KUBE_STATE": str(self.state_path),
                "ROLLBACK_AGENT_URL": url,
                "ROLLBACK_INJECT_URL": url,
                "ROLLBACK_RESULTS_FILE": str(self.results),
                "ROLLBACK_KUBE_CONTEXT": "gke_test_ctx",
                "FAKE_PF_PIDS": str(self.pf_pids),
                "FAKE_DOOR_PORT": str(self.server.server_address[1]),
            }
        )
        env.update(extra)
        return env

    def run(self, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(_SCRIPT), _NS, _CR], capture_output=True, text=True, env=self.env(**extra), timeout=180, check=False
        )

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())


def passed(result: subprocess.CompletedProcess) -> list[str]:
    return re.findall(r"^PASS (\S+):", result.stdout, re.MULTILINE)


def failed(result: subprocess.CompletedProcess) -> list[str]:
    return re.findall(r"^FAIL (\S+):", result.stdout, re.MULTILINE)


class RoundTripTest(unittest.TestCase):
    def run_sim(self, state: dict, **extra: str) -> tuple[subprocess.CompletedProcess, dict, Sim]:
        sim = Sim(state)
        self.addCleanup(sim.close)
        result = sim.run(**extra)
        return result, sim.state(), sim

    def assert_fails_at(self, result: subprocess.CompletedProcess, name: str, fragment: str = "") -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(failed(result), [name], result.stdout)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(lines[-1], f"ROLLBACK ROUND TRIP FAILED at {name}", result.stdout)
        self.assertNotIn("ROLLBACK ROUND TRIP PASSED", result.stdout)
        if fragment:
            fail_line = next(line for line in lines if line.startswith(f"FAIL {name}:"))
            self.assertIn(fragment, fail_line)


class KeptRoundTripTest(RoundTripTest):
    def test_a_kept_round_trip_passes_every_assertion_in_order(self) -> None:
        result, state, sim = self.run_sim(healthy_next_state())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(passed(result), _PASSING_ORDER)
        self.assertEqual(failed(result), [])
        self.assertIn("ROLLBACK ROUND TRIP PASSED", result.stdout.strip().splitlines()[-1])
        self.assertIn("uid pvc-1", result.stdout)
        self.assertIn("uid creds-1", result.stdout)
        # Back where it started: next, with the bridge declared as it was.
        self.assertEqual(state["cr"]["spec"]["mode"], "next")
        self.assertEqual(state["cr"]["spec"]["deployment"]["sidecars"], [_BRIDGE])

    def test_the_patches_are_the_runbook_unset_flip_flip_restore(self) -> None:
        _, state, _ = self.run_sim(healthy_next_state())
        self.assertEqual(
            state["patches"],
            [
                {"spec": {"deployment": {"sidecars": None}}},
                {"spec": {"mode": "today"}},
                {"spec": {"mode": "next"}},
                {"spec": {"deployment": {"sidecars": [_BRIDGE]}}},
            ],
        )

    def test_every_sidecar_is_unset_and_the_whole_list_restored(self) -> None:
        # Not only the bridge: the list goes whole, and comes back whole and
        # in its order.
        original = healthy_next_state(sidecars=[_OTHER_SIDECAR, _BRIDGE])
        before = json.dumps(original["cr"]["spec"]["deployment"]["sidecars"], sort_keys=True)
        result, state, _ = self.run_sim(original)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(state["patches"][0], {"spec": {"deployment": {"sidecars": None}}})
        self.assertEqual(state["patches"][-1], {"spec": {"deployment": {"sidecars": [_OTHER_SIDECAR, _BRIDGE]}}})
        self.assertEqual(json.dumps(state["cr"]["spec"]["deployment"]["sidecars"], sort_keys=True), before)
        self.assertIn("PASS leg2.bridge-consuming: hermes-bridge logged", result.stdout)

    def test_a_cr_with_only_a_sidecar_off_the_bus_has_it_unset_and_restored_too(self) -> None:
        original = healthy_next_state(sidecars=[_OTHER_SIDECAR])
        before = json.dumps(original["cr"]["spec"]["deployment"]["sidecars"], sort_keys=True)
        result, state, _ = self.run_sim(original)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            state["patches"],
            [
                {"spec": {"deployment": {"sidecars": None}}},
                {"spec": {"mode": "today"}},
                {"spec": {"mode": "next"}},
                {"spec": {"deployment": {"sidecars": [_OTHER_SIDECAR]}}},
            ],
        )
        self.assertEqual(json.dumps(state["cr"]["spec"]["deployment"]["sidecars"], sort_keys=True), before)
        self.assertIn("PASS leg1.sidecars-unset.ready", result.stdout)
        self.assertIn("PASS leg2.sidecars-restored.ready", result.stdout)
        self.assertIn("SKIP leg2.bridge-consuming:", result.stdout)

    def test_no_sidecars_patches_nothing_but_the_mode(self) -> None:
        # The lane's own install since the operator renders the bridge
        # (#2592): nothing on spec.deployment.sidecars, so nothing to unset
        # before the flip. The simulator answers the bus task whatever runs,
        # so this pins the script's path for the lane's install shape, not
        # that an executor came back; the live round trip is what shows that.
        result, state, _ = self.run_sim(healthy_next_state(sidecars=[]))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("SKIP leg1.sidecars-unset: the CR declares no sidecars (a bridge the operator renders leaves with the mode and needs no unset)", result.stdout)
        self.assertIn("SKIP leg2.sidecars-restored:", result.stdout)
        self.assertIn("PASS leg2.bus-task", result.stdout)
        self.assertEqual(state["patches"], [{"spec": {"mode": "today"}}, {"spec": {"mode": "next"}}])

    def test_every_kubectl_call_is_pinned_to_the_context(self) -> None:
        _, state, _ = self.run_sim(healthy_next_state())
        self.assertTrue(state["calls"])
        self.assertTrue(all(call[:2] == ["--context", "gke_test_ctx"] for call in state["calls"]))

    def test_the_sidecar_contents_never_reach_the_log(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state())
        self.assertNotIn("loopback-value-that-must-not-be-logged", result.stdout + result.stderr)

    def test_the_results_file_carries_every_line_and_the_outcome(self) -> None:
        result, _, sim = self.run_sim(healthy_next_state())
        lines = sim.results.read_text().splitlines()
        self.assertEqual([line.split(":")[0] for line in lines[:-1]], [f"PASS {name}" for name in _PASSING_ORDER])
        self.assertEqual(lines[-1], "PASSED")

    def test_the_bus_task_reads_the_door_token_fresh_after_the_flip(self) -> None:
        # The flip forward mints a new door token (the inject Secret is torn
        # down under today); a run that reused the first one would be
        # refused at the door with 401.
        result, state, _ = self.run_sim(healthy_next_state())
        self.assertEqual(result.returncode, 0, result.stdout)
        token = base64.b64decode(state["objects"]["secret/platform-agent-a2a-inject"]["data"]["token"]).decode()
        self.assertNotEqual(token, "door-token-0")

    def test_a_transient_capacity_degraded_is_waited_out(self) -> None:
        # #2414: the pod waits for an Autopilot node and the operator says
        # Degraded/PodUnschedulable for a while, then Ready.
        result, _, _ = self.run_sim(healthy_next_state(unschedulable_on="next"))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PodUnschedulable", result.stdout)

    def test_provisioning_slower_than_the_ready_bound_gets_the_bus_up_budget(self) -> None:
        # Under next the CR is not Ready until the provisioning Job is done.
        # The flip forward waits for the Job before Ready, under the bring-up's
        # shared BUS_UP_TIMEOUT, so a run longer than READY_TIMEOUT but inside
        # that budget passes.
        result, _, _ = self.run_sim(healthy_next_state(provision_seconds=4), ROLLBACK_READY_TIMEOUT="2", ROLLBACK_BUS_UP_TIMEOUT="30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(passed(result), _PASSING_ORDER)

    def test_a_degraded_that_predates_the_run_is_carried_not_blamed(self) -> None:
        # MinterPruningHeld: Degraded=True beside phase Ready, from before the
        # round trip started. Both legs carry it, and the run passes.
        result, _, sim = self.run_sim(healthy_next_state(degraded_baseline="MinterPruningHeld"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(passed(result), _PASSING_ORDER)
        self.assertIn("PASS pre.degraded-baseline: Degraded=True/MinterPruningHeld before the flip", result.stdout)
        for leg in ("leg1", "leg2"):
            line = next(x for x in result.stdout.splitlines() if x.startswith(f"PASS {leg}.not-degraded:"))
            self.assertIn("Degraded=True/MinterPruningHeld is the one the install carried before the flip", line)
        self.assertIn("PASS pre.degraded-baseline: Degraded=True/MinterPruningHeld", sim.results.read_text())

    def test_a_clean_baseline_is_reported_too(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state())
        self.assertIn("PASS pre.degraded-baseline: no Degraded condition set before the flip", result.stdout)
        self.assertRegex(result.stdout, r"PASS leg1\.not-degraded: .*\(baseline: none\)")

    def test_a_pod_list_past_the_argv_limit_still_settles(self) -> None:
        # A real install's pod list runs past what one argument may carry
        # (128 KiB per string on Linux, about 1 MiB in all on macOS): the
        # settle check that took it in argv died with E2BIG after
        # leg1.creds-kept on a next-lane run. 1.5 MiB covers both limits.
        state = healthy_next_state()
        state["pods"][0]["metadata"]["annotations"] = {"example.com/bulk": "x" * (3 << 19)}
        result, _, _ = self.run_sim(state)
        self.assertNotIn("Argument list too long", result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-2000:])
        self.assertEqual(passed(result), _PASSING_ORDER)


class KubectlJsonOffArgvTest(unittest.TestCase):
    """No python3 -c call takes a kubectl document as an argument: it goes
    on stdin, where no per-string or total argv limit applies."""

    def test_no_python_call_takes_a_kubectl_document_in_argv(self) -> None:
        source = _SCRIPT.read_text().replace("\\\n", " ")
        calls = [line.split("python3 -c", 1)[1] for line in source.splitlines() if "python3 -c" in line]
        # Guard against a vacuous pass: the settle check is one of them.
        self.assertGreaterEqual(len(calls), 8, calls)
        self.assertTrue(any('"${PY_STUCK}"' in call for call in calls), calls)
        for call in calls:
            for document in ("${pods}", "${jobs}", "${json}"):
                self.assertNotIn(document, call, f"python3 -c{call}")


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class PortForwardTest(RoundTripTest):
    """The path CI takes: no door URLs, so every turn and task goes through a
    port-forward the script starts and stops."""

    def tunnel_env(self) -> dict:
        return {"ROLLBACK_AGENT_URL": "", "ROLLBACK_INJECT_URL": "", "ROLLBACK_AGENT_LOCAL_PORT": str(free_port()), "ROLLBACK_INJECT_LOCAL_PORT": str(free_port()), "ROLLBACK_PORT_FORWARD_WAIT": "10"}

    def test_a_round_trip_through_port_forwards_passes_and_leaves_none_running(self) -> None:
        result, state, sim = self.run_sim(healthy_next_state(), **self.tunnel_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(passed(result), _PASSING_ORDER)
        forwards = [call for call in state["calls"] if "port-forward" in call]
        self.assertEqual(len(forwards), 3, forwards)
        self.assertTrue(all(call[:2] == ["--context", "gke_test_ctx"] for call in forwards))
        pids = [int(line) for line in sim.pf_pids.read_text().split()]
        time.sleep(0.5)
        self.assertEqual([pid for pid in pids if alive(pid)], [])

    def test_a_dead_first_tunnel_is_replaced_by_a_fresh_one(self) -> None:
        # The first port-forward (the pre bus task's) accepts and drops every
        # connection. The script's retry has to be a new tunnel on the same
        # port, which only binds if the dead one was really stopped.
        result, _, _ = self.run_sim(healthy_next_state(dead_first_tunnel=True), **self.tunnel_env(), ROLLBACK_BUS_TASK_ATTEMPTS="2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS pre.bus-task:", result.stdout)
        self.assertIn("pre.bus-task: attempt 1/2", result.stdout)


class FailureTest(RoundTripTest):
    def test_a_pvc_replaced_by_the_flip_to_today(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state(replace_on_today=["pvc"]))
        self.assert_fails_at(result, "leg1.pvc-kept", "uid pvc-1 before the flip, pvc-new now")

    def test_a_pvc_replaced_by_the_flip_back(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state(replace_on_next=["pvc"]))
        self.assert_fails_at(result, "leg2.pvc-kept", "uid pvc-1 before the flip, pvc-new now")

    def test_a_creds_secret_replaced_by_the_flip_to_today(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state(replace_on_today=["creds"]))
        self.assert_fails_at(result, "leg1.creds-kept", "uid creds-1 before the flip, creds-new now")

    def test_a_creds_secret_replaced_by_the_flip_back(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state(replace_on_next=["creds"]))
        self.assert_fails_at(result, "leg2.creds-kept", "uid creds-1 before the flip, creds-new now")

    def test_a_pvc_missing_before_the_flip(self) -> None:
        state = healthy_next_state()
        del state["objects"][f"pvc/{_PVC}"]
        result, after, _ = self.run_sim(state)
        self.assert_fails_at(result, "pre.pvc-present", "does not exist")
        self.assertEqual(after["patches"], [])

    def test_a_pvc_deleted_by_the_flip_to_today(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state(delete_on_today=["pvc"]))
        self.assert_fails_at(result, "leg1.pvc-kept", "is gone (it had uid pvc-1)")

    def test_a_pvc_being_deleted_counts_as_gone(self) -> None:
        state = healthy_next_state()
        state["objects"][f"pvc/{_PVC}"]["deleting"] = "2026-10-06T00:00:00Z"
        result, _, _ = self.run_sim(state)
        self.assert_fails_at(result, "pre.pvc-present", "is being deleted")

    def test_the_nats_pod_on_another_claim(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state(nats_claim="data-something-else-0"))
        self.assert_fails_at(result, "leg2.nats-on-kept-pvc", "not data-platform-agent-a2a-nats-0")

    def test_a_crash_looping_pod_after_the_flip_to_today(self) -> None:
        stuck = {"metadata": {"name": "platform-agent-gateway-new", "labels": {}}, "status": {"phase": "Running", "containerStatuses": [{"name": "platform-agent", "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}
        result, _, _ = self.run_sim(healthy_next_state(stuck_pod_on="today", stuck_pod=stuck), ROLLBACK_SETTLE_TIMEOUT="2")
        self.assert_fails_at(result, "leg1.nothing-stuck", "pod/platform-agent-gateway-new container platform-agent CrashLoopBackOff")

    def test_a_pending_pod_after_the_flip_back(self) -> None:
        stuck = {"metadata": {"name": "platform-agent-a2a-verifier-x", "labels": {"app.kubernetes.io/part-of": "a2a-next"}}, "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "message": "0/3 nodes are available"}]}}
        result, _, _ = self.run_sim(healthy_next_state(stuck_pod_on="next", stuck_pod=stuck), ROLLBACK_SETTLE_TIMEOUT="2")
        self.assert_fails_at(result, "leg2.nothing-stuck", "pod/platform-agent-a2a-verifier-x Pending 0/3 nodes are available")

    def test_a_terminating_pod_that_never_leaves(self) -> None:
        stuck = {"metadata": {"name": "platform-agent-gateway-old", "labels": {}, "deletionTimestamp": "2026-10-06T00:00:00Z"}, "status": {"phase": "Running"}}
        result, _, _ = self.run_sim(healthy_next_state(stuck_pod_on="today", stuck_pod=stuck), ROLLBACK_SETTLE_TIMEOUT="2")
        self.assert_fails_at(result, "leg1.nothing-stuck", "terminating since")

    def test_an_a2a_pod_left_under_today(self) -> None:
        # The teardown removes the StatefulSet but a pod of the A2A stack is
        # still there under today.
        state = healthy_next_state()
        state["scenario"]["stuck_pod_on"] = "today"
        state["scenario"]["stuck_pod"] = {"metadata": {"name": "platform-agent-a2a-nats-0", "labels": {"app.kubernetes.io/part-of": "a2a-next"}}, "status": {"phase": "Running"}}
        result, _, _ = self.run_sim(state, ROLLBACK_SETTLE_TIMEOUT="2")
        self.assert_fails_at(result, "leg1.nothing-stuck", "is an A2A pod still present under today")

    def test_an_a2a_job_left_under_today(self) -> None:
        # The teardown removes every A2A object but never reaches the
        # provisioning Jobs: they stay under today. leg1.a2a-torn-down passes,
        # and the settle check names the Job.
        result, _, _ = self.run_sim(healthy_next_state(jobs_kept_on="today"), ROLLBACK_SETTLE_TIMEOUT="2")
        self.assert_fails_at(result, "leg1.nothing-stuck")
        self.assertIn("PASS leg1.a2a-torn-down:", result.stdout)
        self.assertIn("FAIL leg1.nothing-stuck: still unsettled after 2s: job/provision-0 is an A2A Job still present under today;job/provision-1 is an A2A Job still present under today\n", result.stdout)

    def test_a_job_failed_after_provisioning_passed(self) -> None:
        # leg2.provisioned read the Job Complete; by the settle check it is
        # Failed. No sidecars, so no reprovisioning read comes between.
        result, _, _ = self.run_sim(healthy_next_state(sidecars=[], job_fails_after_provisioned=True), ROLLBACK_SETTLE_TIMEOUT="2")
        self.assert_fails_at(result, "leg2.nothing-stuck")
        self.assertIn("PASS leg2.provisioned: provisioning Job provision-1 complete", result.stdout)
        self.assertIn("FAIL leg2.nothing-stuck: still unsettled after 2s: job/provision-1 Failed\n", result.stdout)

    def test_a_failed_provision_job_on_the_flip_back(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state(provision_job="Failed"))
        self.assert_fails_at(result, "leg2.provisioned", "failed")

    def test_degraded_after_the_flip_to_today(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state(degraded_on="today"))
        self.assert_fails_at(result, "leg1.not-degraded", "Degraded=True/RBACIncomplete")
        self.assert_fails_at(result, "leg1.not-degraded", "(baseline: none)")

    def test_degraded_after_the_flip_back(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state(degraded_on="next"))
        self.assert_fails_at(result, "leg2.not-degraded", "Degraded=True/RBACIncomplete")

    def test_a_new_degraded_on_an_install_that_already_carried_one(self) -> None:
        # The baseline is MinterPruningHeld; the flip replaces it with another
        # reason. That is new, and the line says what the baseline was.
        result, _, _ = self.run_sim(healthy_next_state(degraded_baseline="MinterPruningHeld", degraded_on="today"))
        self.assert_fails_at(result, "leg1.not-degraded", "a Degraded condition new since the flip")
        self.assert_fails_at(result, "leg1.not-degraded", "Degraded=True/RBACIncomplete")
        self.assert_fails_at(result, "leg1.not-degraded", "(baseline Degraded=True/MinterPruningHeld)")

    def test_a_bring_up_past_its_budget_fails_at_the_slow_gate(self) -> None:
        # The five bring-up waits share one deadline: a Job that outlasts it
        # fails as the Job, and the run never gets to wait on Ready on top.
        result, _, _ = self.run_sim(healthy_next_state(provision_seconds=60), ROLLBACK_BUS_UP_TIMEOUT="3")
        self.assert_fails_at(result, "leg2.provisioned", "the provisioning Jobs did not settle within")
        self.assertNotIn("leg2.mode-next.agent-rolled", result.stdout)

    def test_never_ready_under_today_times_out_at_its_bound(self) -> None:
        started = time.monotonic()
        result, _, _ = self.run_sim(healthy_next_state(never_ready_on="today"), ROLLBACK_READY_TIMEOUT="2")
        self.assert_fails_at(result, "leg1.mode-today.ready", "not Ready at the current generation within 2s")
        self.assertLess(time.monotonic() - started, 60)

    def test_a_ready_left_over_from_the_previous_generation_is_not_ready(self) -> None:
        # The reconcile rolls the agent but never writes a status for the new
        # generation: the Ready on the CR is the one from before the patch.
        result, _, _ = self.run_sim(healthy_next_state(status_stale_on="today"), ROLLBACK_READY_TIMEOUT="2")
        self.assert_fails_at(result, "leg1.mode-today.ready", "(observedGeneration 5)")

    def test_a_teardown_that_never_happens_times_out(self) -> None:
        # Without this assertion a flip that tore nothing down would keep the
        # PVC and the Secret trivially.
        result, _, _ = self.run_sim(healthy_next_state(teardown_skipped="today"), ROLLBACK_TEARDOWN_TIMEOUT="2")
        self.assert_fails_at(result, "leg1.a2a-torn-down", "statefulset platform-agent-a2a-nats;")

    def test_a_refusal_fails_at_once_rather_than_at_the_bound(self) -> None:
        started = time.monotonic()
        result, _, _ = self.run_sim(healthy_next_state(refused_on="next"), ROLLBACK_READY_TIMEOUT="600")
        self.assert_fails_at(result, "leg2.mode-next.ready", "the operator refused the render")
        self.assertLess(time.monotonic() - started, 120)

    def test_no_answer_on_the_today_path(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state(today_turn_fails=True))
        self.assert_fails_at(result, "leg1.today-turn", "no answer from /v1/responses in 2 attempts")

    def test_a_bus_task_that_does_not_complete_after_the_flip_back(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state(bus_task_fails_on="leg2"))
        self.assert_fails_at(result, "leg2.bus-task", "terminal failed")

    def test_a_bus_task_that_does_not_complete_before_the_flip(self) -> None:
        result, after, sim = self.run_sim(healthy_next_state(bus_task_fails_on="pre"))
        self.assert_fails_at(result, "pre.bus-task", "terminal failed")
        self.assertEqual(after["patches"], [])
        self.assertEqual(sim.cancels.read_text(), "", "a task that reached a terminal is not cancelled")

    def test_a_door_that_refuses_the_request_fails_at_once_without_a_fresh_tunnel(self) -> None:
        # A door that answered 401 or 403 was reached; the same token through
        # a fresh tunnel gets the same answer, so there is no second attempt.
        for code, fragment in ((401, "the inject door refused the bearer token"), (403, "HTTP 403")):
            with self.subTest(code=code):
                result, _, sim = self.run_sim(healthy_next_state(door_refuses=code), ROLLBACK_BUS_TASK_ATTEMPTS="2")
                self.assert_fails_at(result, "pre.bus-task", "the door refused the request")
                self.assertIn(fragment, result.stdout)
                self.assertNotIn("could not be reached", result.stdout)
                self.assertNotIn("pre.bus-task: attempt 1/2", result.stdout)
                self.assertEqual(sim.cancels.read_text(), "", "nothing was started, so nothing is cancelled")

    def test_a_retryable_status_before_submission_still_gets_a_fresh_tunnel(self) -> None:
        # A 503 is answered too, but retryable: the gateway between pods.
        result, _, _ = self.run_sim(healthy_next_state(door_busy_once=True), ROLLBACK_BUS_TASK_ATTEMPTS="2")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("pre.bus-task: attempt 1/2: the door could not be reached: HTTP 503", result.stdout)

    def test_a_bus_task_left_active_at_its_deadline_is_cancelled(self) -> None:
        # Nothing on the stream by the bound: the task is still on the bus,
        # for a bridge that binds later to run, unless it is cancelled.
        result, _, sim = self.run_sim(healthy_next_state(bus_task_hangs_on="pre"), ROLLBACK_BUS_TASK_TIMEOUT="2")
        self.assert_fails_at(result, "pre.bus-task", "cancel published")
        self.assertEqual(sim.cancels.read_text().split(), ["task-pre.bus-task"])

    def test_a_bus_task_whose_door_is_lost_after_submission_is_cancelled(self) -> None:
        result, _, sim = self.run_sim(healthy_next_state(bus_task_lost_on="pre"))
        self.assert_fails_at(result, "pre.bus-task", "the door was lost while waiting (cancel published)")
        self.assertEqual(sim.cancels.read_text().split(), ["task-pre.bus-task"])

    def test_a_completed_bus_task_is_not_cancelled(self) -> None:
        result, _, sim = self.run_sim(healthy_next_state())
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(sim.cancels.read_text(), "")

    def test_a_bridge_that_never_consumes(self) -> None:
        result, _, _ = self.run_sim(healthy_next_state(bridge_silent=True), ROLLBACK_BRIDGE_TIMEOUT="2")
        self.assert_fails_at(result, "leg2.bridge-consuming", "not consuming after 2s: hermes-bridge")

    def test_an_install_not_in_next(self) -> None:
        state = healthy_next_state()
        state["cr"]["spec"]["mode"] = "today"
        result, after, _ = self.run_sim(state)
        self.assert_fails_at(result, "pre.mode-next", "spec.mode is today")
        self.assertEqual(after["patches"], [])

    def test_a_failure_under_today_leaves_the_sidecars_unset_and_says_where_they_are(self) -> None:
        # Declaring the bridge again under today is the outage the bridge doc
        # names, so the run does not; the saved list is handed over instead.
        result, state, _ = self.run_sim(healthy_next_state(sidecars=[_OTHER_SIDECAR, _BRIDGE], replace_on_today=["pvc"]))
        self.assert_fails_at(result, "leg1.pvc-kept")
        self.assertIn("this run unset the sidecar(s) log-shipper hermes-bridge; the CR's original spec.deployment.sidecars is in", result.stdout)
        self.assertIn("install left at spec.mode=today during leg1", result.stdout)
        self.assertEqual(state["patches"][-1], {"spec": {"mode": "today"}})
        self.assertNotIn("sidecars", state["cr"]["spec"].get("deployment", {}))

    def test_a_failure_under_next_with_the_sidecars_unset_declares_them_again(self) -> None:
        # After the flip forward, before the restore: the CR is at next, so the
        # way out puts the list back as it was.
        original = healthy_next_state(sidecars=[_OTHER_SIDECAR, _BRIDGE], replace_on_next=["pvc"])
        before = json.dumps(original["cr"]["spec"]["deployment"]["sidecars"], sort_keys=True)
        result, state, _ = self.run_sim(original)
        self.assert_fails_at(result, "leg2.pvc-kept")
        self.assertIn("declared them again on the way out", result.stdout)
        self.assertEqual(state["patches"][-1], {"spec": {"deployment": {"sidecars": [_OTHER_SIDECAR, _BRIDGE]}}})
        self.assertEqual(json.dumps(state["cr"]["spec"]["deployment"]["sidecars"], sort_keys=True), before)

    def test_a_failure_at_next_before_the_bus_is_back_does_not_declare_them(self) -> None:
        # The flip forward is patched but provisioning fails: the CR is at
        # next with no bus behind it, and a bridge declared now would crash-loop
        # the agent's pod.
        result, state, _ = self.run_sim(healthy_next_state(provision_job="Failed"))
        self.assert_fails_at(result, "leg2.provisioned")
        self.assertIn("install left at spec.mode=next during leg2", result.stdout)
        self.assertIn("this run unset the sidecar(s) hermes-bridge; the CR's original spec.deployment.sidecars is in", result.stdout)
        self.assertEqual(state["patches"][-1], {"spec": {"mode": "next"}})

    def test_a_death_outside_an_assertion_still_declares_them_again(self) -> None:
        # The agent Deployment answers garbage after the unset: rollout_state's
        # parse fails and set -e ends the run without a FAIL line. The EXIT
        # trap takes it through the failure report, and at next with the bus
        # up the list goes back.
        result, state, _ = self.run_sim(healthy_next_state(garbled_agent_after_unset=True))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failed(result), [])
        self.assertIn("outside an assertion", result.stdout)
        self.assertIn("declared them again on the way out", result.stdout)
        self.assertEqual(state["patches"], [{"spec": {"deployment": {"sidecars": None}}}, {"spec": {"deployment": {"sidecars": [_BRIDGE]}}}])

    def test_a_failure_before_the_flip_with_the_sidecars_unset_declares_them_again(self) -> None:
        # The unset itself never settles: the CR is still at next.
        result, state, _ = self.run_sim(healthy_next_state(status_stale_on_sidecars=True), ROLLBACK_READY_TIMEOUT="2")
        self.assert_fails_at(result, "leg1.sidecars-unset.ready")
        self.assertIn("declared them again on the way out", result.stdout)
        self.assertEqual(state["patches"], [{"spec": {"deployment": {"sidecars": None}}}, {"spec": {"deployment": {"sidecars": [_BRIDGE]}}}])

    def test_a_refused_flip_to_today_declares_them_again(self) -> None:
        # The API refuses the mode patch: the CR is still at next with the
        # bus up, and only the sidecars this run unset are missing.
        result, state, _ = self.run_sim(healthy_next_state(mode_patch_refused="today"))
        self.assert_fails_at(result, "leg1.mode-today.patched", "kubectl patch was refused")
        self.assertIn("install left at spec.mode=next during leg1", result.stdout)
        self.assertIn("declared them again on the way out", result.stdout)
        self.assertEqual(state["patches"], [{"spec": {"deployment": {"sidecars": None}}}, {"spec": {"deployment": {"sidecars": [_BRIDGE]}}}])
        self.assertEqual(state["cr"]["spec"]["deployment"]["sidecars"], [_BRIDGE])

    def test_a_flip_to_today_never_sent_declares_them_again(self) -> None:
        # The agent Deployment cannot be read before the mode patch, so the
        # patch is never sent: the same install as a refusal.
        result, state, _ = self.run_sim(healthy_next_state(generation_unreadable_after_unset=True))
        self.assert_fails_at(result, "leg1.mode-today.patched", "could not be read")
        self.assertIn("declared them again on the way out", result.stdout)
        self.assertEqual(state["patches"], [{"spec": {"deployment": {"sidecars": None}}}, {"spec": {"deployment": {"sidecars": [_BRIDGE]}}}])

    def test_a_signal_is_a_named_failure(self) -> None:
        sim = Sim(healthy_next_state(never_ready_on="today"))
        self.addCleanup(sim.close)
        proc = subprocess.Popen(["bash", str(_SCRIPT), _NS, _CR], stdout=subprocess.PIPE, text=True, env=sim.env(ROLLBACK_READY_TIMEOUT="600"))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not any(p.get("spec", {}).get("mode") == "today" for p in sim.state()["patches"]):
            time.sleep(0.2)
        time.sleep(1)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAIL leg1.interrupted:", out)
        self.assertEqual(out.strip().splitlines()[-1], "ROLLBACK ROUND TRIP FAILED at leg1.interrupted")


_NATS = "platform-agent-a2a-nats"


def bridge_with(env: list | None = None, env_from: list | None = None, name: str = "hermes-bridge", **fields) -> dict:
    sidecar: dict = {"name": name, "image": "bridge:dev", **fields}
    if env is not None:
        sidecar["env"] = env
    if env_from is not None:
        sidecar["envFrom"] = env_from
    return sidecar


def url(value: str) -> list:
    return [{"name": "NATS_URL", "value": value}]


class BusClientGuessTest(unittest.TestCase):
    """PY_SIDECARS, lifted from the script: every sidecar's name on the first
    line (all of them are unset), and on the second the ones whose consumer
    log the run waits on."""

    def split(self, *sidecars: dict) -> tuple[list[str], list[str]]:
        src = _SCRIPT.read_text()
        code = re.search(r"^readonly PY_SIDECARS='\n(.*?)^'$", src, re.DOTALL | re.MULTILINE).group(1)
        cr = {"spec": {"deployment": {"sidecars": list(sidecars)}}}
        out = subprocess.run(["python3", "-c", code, _CREDS, _NATS], input=json.dumps(cr), capture_output=True, text=True, check=True).stdout
        names, bus = out.split("\n")[:2]
        return names.split(), bus.split()

    def test_every_sidecar_is_named_for_the_unset(self) -> None:
        names, _ = self.split(_OTHER_SIDECAR, _BRIDGE, bridge_with(name="args-only", args=[f"--nats-url=nats://{_NATS}:4222"]))
        self.assertEqual(names, ["log-shipper", "hermes-bridge", "args-only"])

    def test_the_log_wait_is_for_sidecars_that_reference_the_creds_or_name_the_service(self) -> None:
        for sidecar in (
            bridge_with(env=url(f"nats://{_NATS}.{_NS}.svc:4222")),
            bridge_with(env=[{"name": "NATS_HOST", "value": f'{{"host":"{_NATS}"}}'}]),
            bridge_with(env=[{"name": "NATS_PASSWORD", "valueFrom": {"secretKeyRef": {"name": _CREDS, "key": "bridge-password"}}}]),
            bridge_with(env_from=[{"prefix": "BUS_", "secretRef": {"name": _CREDS}}]),
        ):
            with self.subTest(sidecar=sidecar):
                self.assertEqual(self.split(sidecar)[1], ["hermes-bridge"])
        self.assertEqual(self.split(_OTHER_SIDECAR, bridge_with(env_from=[{"secretRef": {"name": "some-other-secret"}}]))[1], [])

    def test_no_sidecars(self) -> None:
        self.assertEqual(self.split(), ([], []))


class SidecarRoundTripTest(RoundTripTest):
    """Shapes a reading of the sidecar's data would miss: each is unset before
    the flip and declared again after, because every sidecar is."""

    def assert_unset_and_restored(self, sidecar: dict) -> None:
        state = healthy_next_state(sidecars=[_OTHER_SIDECAR, sidecar])
        result, after, _ = self.run_sim(state)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS leg1.sidecars-unset.patched", result.stdout)
        self.assertEqual(after["patches"][0], {"spec": {"deployment": {"sidecars": None}}})
        self.assertEqual(after["patches"][-1], {"spec": {"deployment": {"sidecars": [_OTHER_SIDECAR, sidecar]}}})

    def test_a_url_in_args(self) -> None:
        self.assert_unset_and_restored(bridge_with(args=[f"--nats-url=nats://{_NATS}:4222"]))

    def test_a_reference_to_a_secret_the_teardown_deletes(self) -> None:
        self.assert_unset_and_restored(bridge_with(env=[{"name": "T", "valueFrom": {"secretKeyRef": {"name": "platform-agent-a2a-inject", "key": "token"}}}]))

    def test_a_url_in_a_configmap(self) -> None:
        self.assert_unset_and_restored(bridge_with(env_from=[{"configMapRef": {"name": "bridge-env"}}]))


class NamesMatchTheOperatorTest(unittest.TestCase):
    """The names the script derives are the ones the operator renders."""

    def constants(self) -> dict[str, str]:
        found = {}
        for line in _SCRIPT.read_text().splitlines():
            match = re.match(r'readonly ([A-Z0-9_]+)="?([^"]*)"?$', line)
            if match:
                found[match.group(1)] = match.group(2)
        return found

    def go(self, path: pathlib.Path, pattern: str) -> str:
        match = re.search(pattern, path.read_text(), re.MULTILINE)
        self.assertIsNotNone(match, f"{pattern} not found in {path}")
        return match.group(1)

    def test_the_suffixes_are_the_api_packages(self) -> None:
        c = self.constants()
        self.assertEqual(c["NATS_NAME_SUFFIX"], self.go(_COMMON_TYPES, r'a2aNATSNameSuffix\s*=\s*"([^"]+)"'))
        self.assertEqual(c["CREDS_SECRET_SUFFIX"], self.go(_COMMON_TYPES, r'a2aCredsSecretSuffix\s*=\s*"([^"]+)"'))
        self.assertEqual(c["CALLOUT_NAME_SUFFIX"], self.go(_COMMON_TYPES, r'a2aCalloutNameSuffix\s*=\s*"([^"]+)"'))
        self.assertEqual(c["CALLOUT_KEYS_SUFFIX"], self.go(_COMMON_TYPES, r'a2aCalloutKeysSuffix\s*=\s*"([^"]+)"'))

    def test_the_claim_the_pvc_name_is_built_from_is_the_statefulsets(self) -> None:
        c = self.constants()
        self.assertEqual(c["NATS_DATA_CLAIM"], self.go(_A2A_MANIFESTS, r'^const a2aNATSDataClaim = "([^"]+)"'))
        # The operator's own spelling of the claim's name, which the script copies.
        self.assertIn('a2aNATSDataClaim + "-" + a2aNATSName(agent) + "-0"', _CONTROLLER.read_text())
        self.assertEqual(c["NATS_ORDINAL_SUFFIX"], "-0")

    def test_the_other_names_and_ports(self) -> None:
        c = self.constants()
        text = _A2A_MANIFESTS.read_text()
        self.assertIn(f'return agent.Name + "{c["A2A_GATEWAY_SUFFIX"]}"', text)
        self.assertIn(f'return agent.Name + "{c["INJECT_NAME_SUFFIX"]}"', text)
        self.assertIn(f'return agent.Name + "{c["A2A_VERIFIER_SUFFIX"]}"', text)
        self.assertEqual(c["INJECT_PORT"], self.go(_A2A_MANIFESTS, r"a2aInjectPort\s*=\s*(\d+)"))
        self.assertEqual(c["INJECT_TOKEN_FIELD"], self.go(_A2A_MANIFESTS, r'a2aInjectTokenKey\s*=\s*"([^"]+)"'))
        self.assertEqual(f'{c["A2A_PART_OF_LABEL"]}', self.go(_OPERATOR / "internal" / "controller" / "manifest_helpers.go", r'labelPartOf\s*=\s*"([^"]+)"'))
        self.assertEqual(c["A2A_PART_OF_VALUE"], self.go(_A2A_MANIFESTS, r'a2aPartOf\s*=\s*"([^"]+)"'))
        component = self.go(_A2A_MANIFESTS, r'a2aComponentLabel\s*=\s*"([^"]+)"')
        provision = self.go(_A2A_MANIFESTS, r'a2aProvisionComponent\s*=\s*"([^"]+)"')
        self.assertEqual(c["A2A_PROVISION_JOB_SELECTOR"], f"{component}={provision}")
        self.assertIn("A2ACalloutKeysSecretName(agent.Name)", _CALLOUT_KEYS.read_text())

    def test_the_kept_objects_are_on_neither_teardown_list(self) -> None:
        # What the script asserts is kept is what cleanupA2A leaves out.
        text = _A2A_MANIFESTS.read_text()
        start = text.index("func (r *PlatformAgentReconciler) a2aPreBusTeardown(")
        end = text.index("func (r *PlatformAgentReconciler) a2aNamespacedTeardown(")
        lists = text[start:end]
        self.assertNotIn("a2aCredsSecretName", lists)
        self.assertNotIn("PersistentVolumeClaim", lists)
        self.assertIn("a2aCalloutKeysName", lists)
        cleanup = text[text.index("func (r *PlatformAgentReconciler) cleanupA2A(") :]
        cleanup = cleanup[: cleanup.index("\nfunc ")]
        self.assertNotIn("a2aCredsSecretName", cleanup)
        self.assertNotIn("PersistentVolumeClaim", cleanup)
        # And the StatefulSet sets no PVC retention policy, so its default
        # (Retain) keeps the claim when the StatefulSet is deleted.
        sts = text[text.index("func buildA2ANATSStatefulSet(") :]
        sts = sts[: sts.index("\nfunc ")]
        self.assertNotIn("PersistentVolumeClaimRetentionPolicy", sts)

    def test_the_status_words_are_the_operators(self) -> None:
        c = self.constants()
        text = _CONTROLLER.read_text()
        for reason in c["CR_REFUSAL_REASONS"].split():
            self.assertIn(f'"{reason}"', text + _A2A_MANIFESTS.read_text(), reason)
        self.assertIn('newPhase = "Ready"', text)
        self.assertIn('newPhase = "Degraded"', text)


def ci_eval_function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", _CI_EVAL.read_text(), re.DOTALL | re.MULTILINE)
    if match is None:
        raise AssertionError(f"{name}() not found in {_CI_EVAL}")
    return match.group(0)


def ci_eval_constants() -> str:
    prefixes = ("readonly EVAL_ROLLBACK_", "readonly EVAL_SNOWFLAKE_", "readonly EVAL_JOB_START_")
    return "\n".join(line for line in _CI_EVAL.read_text().splitlines() if line.startswith(prefixes))


def snowflake(epoch_seconds: int) -> int:
    """A Prow BUILD_ID that decodes to the given time (scripts/pool_pressure.py)."""
    return (epoch_seconds * 1000 - 1288834974657) << 22


class CiEvalWiringTest(unittest.TestCase):
    """run_rollback_roundtrip in hack/ci-eval-pr.sh, lifted and run."""

    def run_wiring(
        self, *, mode_next: str | None, script_status: int = 0, elapsed: int = 0, job_age: int | None = None, build_id: str | None = None, timeout_on_path: bool = False
    ) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
        """`elapsed` is the eval's age (START_TIME), `job_age` this script's
        (EVAL_SCRIPT_STARTED_EPOCH, defaulting to the eval's), `build_id` the
        Prow BUILD_ID, unset when None."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        artifacts = root / "artifacts"
        artifacts.mkdir()
        (root / "rollback-roundtrip.sh").write_text(
            "echo \"stub ran: $* context=${ROLLBACK_KUBE_CONTEXT} results=${ROLLBACK_RESULTS_FILE}\"\n"
            "echo 'PASS pre.mode-next: x' >> \"${ROLLBACK_RESULTS_FILE}\"\n"
            f"exit {script_status}\n"
        )
        flag = "unset EVAL_MODE_NEXT" if mode_next is None else f"export EVAL_MODE_NEXT={mode_next}"
        build = "unset BUILD_ID" if build_id is None else f"export BUILD_ID={build_id}"
        age = elapsed if job_age is None else job_age
        script = textwrap.dedent(
            f"""\
            set -euo pipefail
            {ci_eval_constants()}
            SCRIPT_DIR={root}
            ARTIFACT_DIR={artifacts}
            TARGET_NAMESPACE=kubeagents-system
            AGENT_SERVICE_NAME=platform-agent
            AGENT_CLUSTER_CONTEXT=gke_p_r_c
            START_TIME=$((SECONDS - {elapsed}))
            EVAL_SCRIPT_STARTED_EPOCH=$(($(date +%s) - {age}))
            {flag}
            {build}
            profile_begin() {{ echo "PROFILE $*"; }}
            collect_gateway_log() {{ echo COLLECT_GATEWAY; }}
            collect_agent_pod_diagnostics() {{
              [ -z "${{AGENT_DIAG_COLLECTED:-}}" ] || return 0
              AGENT_DIAG_COLLECTED=1
              echo "COLLECT_DIAG $* prefix=${{AGENT_DIAG_PREFIX:-}}"
            }}
            {"" if timeout_on_path else "timeout() { shift 2; \"$@\"; }"}
            {ci_eval_function("job_started_epoch")}
            {ci_eval_function("run_rollback_roundtrip")}
            SUITE_STATUS=1
            run_rollback_roundtrip || true
            collect_agent_pod_diagnostics
            echo "SUITE_STATUS=${{SUITE_STATUS}}"
            run_rollback_roundtrip
            echo "returned=$?"
            """
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, check=False, env=minimal_env())
        return result, artifacts

    def test_skipped_outside_next_mode(self) -> None:
        for flag in (None, "", "0", "true"):
            with self.subTest(flag=flag):
                result, artifacts = self.run_wiring(mode_next=flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("stub ran", result.stdout)
                self.assertNotIn("PROFILE", result.stdout)
                self.assertNotIn("Rollback round trip", result.stdout)
                self.assertEqual(list(artifacts.iterdir()), [])

    def test_a_failed_round_trip_is_reported_and_changes_nothing(self) -> None:
        result, artifacts = self.run_wiring(mode_next="1", script_status=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SUITE_STATUS=1", result.stdout)
        self.assertIn("returned=0", result.stdout)
        self.assertIn("Rollback round trip failed (exit 1); report-only", result.stdout)
        self.assertIn("stub ran: kubeagents-system platform-agent context=gke_p_r_c", result.stdout)
        self.assertIn("stub ran", (artifacts / "rollback-roundtrip.log").read_text())
        results = (artifacts / "rollback-roundtrip.txt").read_text().splitlines()
        self.assertEqual(results[1:], ["PASS pre.mode-next: x", "OUTCOME: failed (exit 1)"])
        self.assertTrue(results[0].startswith("the job started "), results[0])

    def test_a_passing_round_trip_is_reported(self) -> None:
        result, artifacts = self.run_wiring(mode_next="1", script_status=0)
        self.assertIn("Rollback round trip passed; report-only", result.stdout)
        self.assertIn("SUITE_STATUS=1", result.stdout)
        self.assertEqual((artifacts / "rollback-roundtrip.txt").read_text().splitlines()[-1], "OUTCOME: passed")

    def test_the_eval_logs_are_taken_before_the_flip(self) -> None:
        result, _ = self.run_wiring(mode_next="1")
        out = result.stdout
        self.assertLess(out.index("COLLECT_GATEWAY"), out.index("stub ran"))
        self.assertLess(out.index("COLLECT_DIAG --keep-watch prefix="), out.index("stub ran"))

    def test_the_watch_runs_through_the_flip_and_the_trap_collects_its_window(self) -> None:
        # The pre-flip snapshot leaves the watch running, and the collector is
        # re-armed under a prefix, so the trap's call after the round trip
        # (here, the call after the function) stops the watch and writes
        # rollback-* rather than returning at its once-guard.
        for status in (0, 1):
            with self.subTest(script_status=status):
                result, _ = self.run_wiring(mode_next="1", script_status=status)
                out = result.stdout
                self.assertIn("COLLECT_DIAG --keep-watch prefix=\n", out)
                # The child's line is checked for presence only: it reaches
                # stdout through an un-waited `tee` process substitution, so it
                # can land after the collect call (the same race as #2626).
                self.assertIn("stub ran", out)
                # The snapshot comes after the round trip: the function's own
                # closing line, printed after its wait, precedes it. Both are
                # the parent's, so this order is fixed.
                self.assertLess(out.index("report-only, so the eval verdict"), out.index("COLLECT_DIAG  prefix=rollback-"))

    def test_a_run_too_late_for_its_bound_is_skipped(self) -> None:
        result, artifacts = self.run_wiring(mode_next="1", elapsed=16000)
        self.assertNotIn("stub ran", result.stdout)
        self.assertIn("SKIPPED: not enough time left in the job", result.stdout)
        self.assertEqual((artifacts / "rollback-roundtrip.txt").read_text().splitlines()[-1], "OUTCOME: skipped")

    def test_a_long_deploy_skips_the_round_trip_however_short_the_eval(self) -> None:
        # The eval has just started, but the job is 5h old by its BUILD_ID: a
        # slow deploy in front. Measured from the eval, the round trip would
        # start and run into the deadline.
        now = int(time.time())
        result, artifacts = self.run_wiring(mode_next="1", elapsed=0, job_age=0, build_id=str(snowflake(now - 5 * 3600)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("stub ran", result.stdout)
        line = next(x for x in result.stdout.splitlines() if x.startswith("SKIPPED:"))
        self.assertIn("not enough time left in the job", line)
        self.assertRegex(line, r"it started 1800\ds ago \(BUILD_ID \d+\), past the 15600s start-by bound")
        self.assertEqual((artifacts / "rollback-roundtrip.txt").read_text().splitlines()[-1], "OUTCOME: skipped")

    def test_a_young_job_runs_and_says_how_old_it_was(self) -> None:
        now = int(time.time())
        result, artifacts = self.run_wiring(mode_next="1", build_id=str(snowflake(now - 3600)))
        self.assertIn("stub ran", result.stdout)
        self.assertRegex((artifacts / "rollback-roundtrip.txt").read_text(), r"the job started 3\d\d\ds ago \(BUILD_ID \d+\)")

    def test_without_a_build_id_that_decodes_the_age_is_the_scripts(self) -> None:
        now = int(time.time())
        for build_id in (None, "local", str(snowflake(now + 3600)), str(snowflake(now - 3 * 86400))):
            with self.subTest(build_id=build_id):
                result, artifacts = self.run_wiring(mode_next="1", elapsed=0, job_age=16000, build_id=build_id)
                self.assertNotIn("stub ran", result.stdout)
                self.assertIn("(this script's start less 2700s for the deploy; BUILD_ID gave none)", result.stdout)

    def test_the_fallback_counts_the_deploy_in_front(self) -> None:
        # The script is 13000s old and BUILD_ID gives nothing: with the
        # deploy's allowance the job is past the start-by bound.
        result, _ = self.run_wiring(mode_next="1", elapsed=0, job_age=13000)
        self.assertNotIn("stub ran", result.stdout)
        self.assertRegex(result.stdout, r"it started 1570\ds ago \(this script's start less 2700s")

    def test_the_snowflake_layout_is_the_pool_pressure_scripts(self) -> None:
        values = dict(re.findall(r"readonly (EVAL_SNOWFLAKE_\w+)=(\d+)", ci_eval_constants()))
        pool = (_REPO_ROOT / "scripts" / "pool_pressure.py").read_text()
        self.assertEqual(values["EVAL_SNOWFLAKE_EPOCH_MS"], re.search(r"^SNOWFLAKE_EPOCH_MS = (\d+)", pool, re.MULTILINE).group(1))
        self.assertEqual(values["EVAL_SNOWFLAKE_TIMESTAMP_SHIFT"], re.search(r"^SNOWFLAKE_TIMESTAMP_SHIFT = (\d+)", pool, re.MULTILINE).group(1))

    def test_it_runs_after_the_verdict_is_computed_and_before_it_is_announced(self) -> None:
        lines = _CI_EVAL.read_text().splitlines()
        call = [i for i, line in enumerate(lines) if line.strip().startswith("run_rollback_roundtrip") and "()" not in line]
        self.assertEqual(len(call), 1, "one call site")
        suite = next(i for i, line in enumerate(lines) if "uv run bench-gate suite" in line and "--partial" not in line and "||" not in line and line.startswith("(cd"))
        status = next(i for i in range(suite, len(lines)) if "|| SUITE_STATUS=$?" in lines[i])
        announce = next(i for i, line in enumerate(lines) if line.startswith('announce_suite_verdict "${SUITE_STATUS}"'))
        cases = next(i for i, line in enumerate(lines) if line.startswith("# ─── Per-case verdicts"))
        self.assertLess(cases, call[0])
        self.assertLess(status, call[0])
        self.assertEqual(call[0] + 1, announce)
        self.assertEqual(lines[call[0]], "run_rollback_roundtrip || true")
        # Not on the exit path: a run cut short never flips the install.
        self.assertNotIn("run_rollback_roundtrip", ci_eval_function("profile_and_dump_on_exit"))

    def test_a_deadline_term_during_the_round_trip_is_passed_on_and_exits_143(self) -> None:
        # Prow's deadline arrives as SIGTERM. Waited on in the foreground,
        # the round trip would hold it off for up to its hour; the wait lets
        # the trap run, the round trip hears the TERM, and the script exits
        # 143 with its EXIT trap, as the global trap would have it.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "artifacts").mkdir()
            started = root / "started"
            # The stub's trap marks its own finish in a file, not only on the
            # pipe: its echo goes through the un-waited `tee`, so only the
            # marker says when it ran. The sleep makes it finish well after a
            # parent that did not wait for it would already have exited.
            done = root / "child-done"
            (root / "rollback-roundtrip.sh").write_text(
                f"trap 'sleep 1; touch {done}; echo stub interrupted; exit 1' TERM\ntouch {started}\nwhile :; do sleep 1 & wait $!; done\n"
            )
            script = textwrap.dedent(
                f"""\
                set -euo pipefail
                {ci_eval_constants()}
                SCRIPT_DIR={root}
                ARTIFACT_DIR={root}/artifacts
                TARGET_NAMESPACE=kubeagents-system
                AGENT_SERVICE_NAME=platform-agent
                START_TIME=$SECONDS
                EVAL_SCRIPT_STARTED_EPOCH=$(date +%s)
                unset BUILD_ID
                EVAL_MODE_NEXT=1
                profile_begin() {{ :; }}
                collect_gateway_log() {{ :; }}
                collect_agent_pod_diagnostics() {{
                  [ -z "${{AGENT_DIAG_COLLECTED:-}}" ] || return 0
                  AGENT_DIAG_COLLECTED=1
                  echo "DIAG $* prefix=${{AGENT_DIAG_PREFIX:-}}"
                }}
                timeout() {{ shift 2; exec "$@"; }}
                trap 'echo EXIT TRAP RAN; if [ -e {done} ]; then echo CHILD DONE BEFORE EXIT=yes; else echo CHILD DONE BEFORE EXIT=no; fi; collect_agent_pod_diagnostics' EXIT
                trap 'exit 143' TERM INT
                {ci_eval_function("job_started_epoch")}
                {ci_eval_function("run_rollback_roundtrip")}
                run_rollback_roundtrip || true
                echo NOT REACHED
                """
            )
            proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=minimal_env())
            deadline = time.monotonic() + 30
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue(started.exists())
            sent = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=30)
        self.assertLess(time.monotonic() - sent, 10)
        self.assertEqual(proc.returncode, 143, out)
        self.assertIn("stub interrupted", out)
        self.assertIn("EXIT TRAP RAN", out)
        self.assertNotIn("NOT REACHED", out)
        # The TERM trap waits for the round trip before exiting, so the EXIT
        # trap runs after the child's own trap has finished. Both this line and
        # the snapshot below are the parent's, so their order is fixed; the
        # child's `stub interrupted` reaches the pipe through the `tee` process
        # substitution, which nothing waits for, so it is checked for presence
        # only (#2626).
        self.assertIn("CHILD DONE BEFORE EXIT=yes", out)
        # The deadline's exit still takes the round trip's own snapshot, and
        # takes it in the EXIT trap, after the child is done.
        self.assertLess(out.index("CHILD DONE BEFORE EXIT=yes"), out.index("DIAG  prefix=rollback-"))

    def test_the_function_never_assigns_the_suite_status(self) -> None:
        self.assertNotIn("SUITE_STATUS", ci_eval_function("run_rollback_roundtrip"))

    def test_the_bound_ends_inside_the_deadline(self) -> None:
        # Job-relative: the start-by bound is on the job's age, so job start +
        # bound + the round trip's limit + its kill grace must leave room for
        # the verdict, the EXIT trap and the teardown's 10m uninstall.
        values = dict(re.findall(r"readonly (EVAL_ROLLBACK_\w+)=(\d+)", ci_eval_constants()))
        presubmit_deadline = 360 * 60
        self.assertEqual(int(values["EVAL_ROLLBACK_JOB_DEADLINE_SECONDS"]), presubmit_deadline)
        margin = 30 * 60
        total = int(values["EVAL_ROLLBACK_START_BY_SECONDS"]) + int(values["EVAL_ROLLBACK_TIMEOUT_SECONDS"]) + int(values["EVAL_ROLLBACK_KILL_AFTER_SECONDS"]) + margin
        self.assertLessEqual(total, presubmit_deadline)

    def test_the_age_is_the_jobs_not_the_evals(self) -> None:
        body = ci_eval_function("run_rollback_roundtrip")
        self.assertNotIn("START_TIME", body)
        self.assertIn("job_started_epoch", body)

    def test_the_script_named_exists(self) -> None:
        name = re.search(r'readonly EVAL_ROLLBACK_SCRIPT="([^"]+)"', ci_eval_constants()).group(1)
        self.assertEqual(_CI_EVAL.parent / name, _SCRIPT)
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()

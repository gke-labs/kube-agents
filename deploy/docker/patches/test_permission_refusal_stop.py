"""Unit tests for the permission-refusal stop.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The scenario is #1943: a worker refused a kubectl write retries it another way
until its step budget is gone. The module must count the policy's definitive
refusals across a turn, whatever succeeds in between, halt on the configured
one (the first by default; guidance below it), and leave every other result to
the stock path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import apply_permission_refusal_stop as applier
import permission_refusal_stop as stop
from kanban_guardrail_exit import guardrail_halt_nudge

# The notices come from the policy module itself, so a reword there fails here.
_POLICY_PATH = Path(__file__).resolve().parents[3] / "agents" / "platform" / "scripts" / "command_policy.py"
_spec = importlib.util.spec_from_file_location("command_policy_under_test", _POLICY_PATH)
command_policy = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = command_policy
_spec.loader.exec_module(command_policy)


def _result(output, exit_code=77):
    return json.dumps({"output": output, "exit_code": exit_code, "error": None})


REFUSAL = _result(
    "Agents hold read-only access to Kubernetes. Propose this change as a pull request instead. "
    + command_policy._ACTION_BOUNDARY_NOTICE + "\npolicy rule: kubernetes.read-only"
)
FLAG_REFUSAL = _result(command_policy._FLAG_BOUNDARY_NOTICE + "\npolicy rule: identity.caller-supplied-impersonation")
GCLOUD_REFUSAL = _result(
    "Agents hold read-only access to Google Cloud. Propose this change as a pull request instead. "
    + command_policy._ACTION_BOUNDARY_NOTICE + "\npolicy rule: gcp.read-only"
)
K8S_403 = '{"output": "Error from server (Forbidden): deployments.apps \\"kube-dns\\" is forbidden: User cannot patch", "exit_code": 1}'
GCP_403 = '{"output": "ERROR: (gcloud.container.clusters.update) ResponseError: code=403, message=Required permission denied PERMISSION_DENIED", "exit_code": 1}'
MCP_403 = '{"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "The caller does not have permission"}}'
UNAUTHENTICATED = '{"output": "error: You must be logged in to the server (Unauthorized)", "exit_code": 1}'
PLAIN_FAILURE = '{"output": "error: the server doesn\'t have a resource type \\"foo\\"", "exit_code": 1}'
OK = '{"output": "deployment.apps/kube-dns 2/2", "exit_code": 0}'


@dataclass(frozen=True)
class Decision:
    action: str = "allow"
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: object = None

    @property
    def should_halt(self):
        return self.action in {"block", "halt"}


class FakeController:
    """The two things the module touches on the real controller."""

    def __init__(self, halt_after=None):
        # A config that predates the patch has no threshold attribute at all.
        self.config = object() if halt_after is None else type("Config", (), {stop.HALT_AFTER_ATTR: halt_after})()
        self.halt_decision = None
        self.reset_for_turn()

    def reset_for_turn(self):
        self._call_history = deque(maxlen=64)
        self.halt_decision = None

    def _decide(self, action, code, tool_name, count, signature, *, message=None, **fmt):
        decision = Decision(action, code, message or "", tool_name, count, signature)
        if decision.should_halt:
            self.halt_decision = decision
        return decision


WRITE = {"command": "kubectl scale deploy kube-dns -n kube-system --replicas=3"}
ALLOW = Decision()


def observe(controller, result, tool="terminal", args=WRITE, stock=ALLOW, failed=True):
    return stop.observe_refusal(
        controller, tool, args, result, "sig", stock=stock, failed=failed, decide=controller._decide, decision_cls=Decision
    )


class ClassifyTests(unittest.TestCase):
    def test_a_refused_mutating_kubectl_verb_is_definitive(self):
        for command in (
            "kubectl scale deploy kube-dns -n kube-system --replicas=3",
            "kubectl -n kube-system patch deploy kube-dns -p '{}'",
            "kubectl rollout -n kube-system restart deploy/kube-dns",
            "timeout 30 /usr/bin/kubectl apply -f x.yaml",
            "sudo -E kubectl apply -f x.yaml",
            "KUBECONFIG=/tmp/kc kubectl scale deploy x --replicas=3",
            "kubectl get deploy x; kubectl delete deploy x",
            "kubectl scale deploy x --replicas=3; kubectl get deploy x",
            "kubectl scale deploy x --replicas=3 2>&1 | tee /tmp/scale.log",
            "kubectl apply -f x.yaml || true",
            "kubectl \\\n  -n kube-system \\\n  scale deploy kube-dns --replicas=3",
            "sh -c 'kubectl scale deploy x --replicas=3'",
            "bash -c -- 'kubectl scale deploy x --replicas=3'",
            'out="$(kubectl scale deploy kube-dns -n kube-system --replicas=3 2>&1)"; echo "$out"',
            "kubectl exec -n kube-system deploy/x -- kubectl scale deploy kube-dns --replicas=3",
            "kubectl debug node/x -it --image=busybox",
            "kubectl certificate approve csr-1",
            "kubectl auth reconcile -f rbac.yaml",
            "kubectl --context foo rollout restart deploy/kube-dns -n kube-system",
        ):
            self.assertEqual(stop.classify_refusal(command, REFUSAL), "policy", command)

    def test_program_text_is_read_loosely(self):
        for text in (
            'subprocess.run(["kubectl", "scale", "deploy/kube-dns", "--replicas=3"])',
            'subprocess.run([\n    "kubectl",\n    "scale",\n    "deploy/kube-dns",\n])',
            'os.system("kubectl patch deploy x -p y")',
        ):
            self.assertEqual(stop.classify_refusal(text, REFUSAL, loose=True), "policy", text)
        self.assertEqual(stop.classify_refusal('subprocess.run(["kubectl", "get", "pods"])', REFUSAL, loose=True), "advisory")

    def test_a_refused_read_spelling_a_help_read_or_a_gcloud_refusal_is_advisory(self):
        for command in (
            "kubectl config view",
            "kubectl cluster-info dump",
            "kubectl diff -f patch.yaml",
            "kubectl port-forward svc/x 8080:80",
            "kubectl port-forward -n run svc/x 8080:80",
            "kubectl rollout --unknown status deploy/x",
            "kubectl rollout status deploy/x",
            "kubectl get deploy x --selector app=scale-set",
            "kubectl scale --help",
            "kubectl patch -h",
            "kubectl help scale",
            "kubectl -n kube-system scale --help",
            "kubectl exec -n kube-system deploy/kube-dns -- sh -c 'set -e; cat /etc/resolv.conf'",
            "kubectl exec -n debug pod -- ls create",
            "kubectl cp pod:/tmp/f ./out -n label",
            "kubectl exec -n kube-system deploy/kube-dns-autoscaler -- cat /etc/config/linear",
            "grep -r 'kubectl scale deploy kube-dns' /opt/data/kanban/logs",
            "grep -r kubectl scale /opt/data/kanban/logs",
            "echo kubectl scale deploy x --replicas=3",
            "",
        ):
            self.assertEqual(stop.classify_refusal(command, REFUSAL), "advisory", command)
        self.assertEqual(stop.classify_refusal("gcloud container clusters resize x --num-nodes 3", GCLOUD_REFUSAL), "advisory")

    def test_the_verb_resolution_is_linear_on_long_input(self):
        import time
        for text in ("/" * 50000, "*" * 20000, "kubectl " + "-" * 30000 + " scale x", "kubectl scale " + "'" + "a" * 20000):
            started = time.monotonic()
            stop.names_mutating_verb(text)
            self.assertLess(time.monotonic() - started, 0.5)

    def test_a_flag_refusal_is_not_a_refusal_of_the_change(self):
        self.assertIsNone(stop.classify_refusal(WRITE["command"], FLAG_REFUSAL))

    def test_api_denials_are_api(self):
        for result in (K8S_403, GCP_403, MCP_403):
            self.assertEqual(stop.classify_refusal("kubectl get pods", result), "api", result)

    def test_unauthenticated_failures_and_successes_are_not_refusals(self):
        for result in (UNAUTHENTICATED, PLAIN_FAILURE, OK, "", None, 42):
            self.assertIsNone(stop.classify_refusal("kubectl get pods", result), result)

    def test_the_command_is_read_from_the_call_arguments(self):
        self.assertEqual(stop.command_text({"command": "kubectl scale x"}), ("kubectl scale x", False))
        self.assertEqual(stop.command_text({"code": "import os", "language": "python"}), ("import os\npython", True))
        self.assertEqual(stop.command_text(None), ("", False))


class ObserveTests(unittest.TestCase):
    def test_the_first_refusal_halts_by_default_with_the_reply_as_its_message(self):
        controller = FakeController()
        halt = observe(controller, REFUSAL)
        self.assertEqual((halt.action, halt.code, halt.count), ("halt", stop.HALT_CODE, 1))
        self.assertIs(controller.halt_decision, halt)
        self.assertIn("refused however it is attempted", halt.message)
        self.assertNotIn("{", halt.message)

    def test_below_the_threshold_a_refusal_warns_and_the_count_spans_successes(self):
        controller = FakeController(halt_after=2)
        first = observe(controller, REFUSAL)
        self.assertEqual((first.action, first.code, first.count), ("warn", stop.WARN_CODE, 1))
        self.assertIn("do not retry it through another command", first.message)
        self.assertIsNone(controller.halt_decision)
        # A read that worked and a failure that was not a refusal change nothing.
        self.assertIsNone(observe(controller, OK))
        self.assertIsNone(observe(controller, PLAIN_FAILURE))
        second = observe(controller, REFUSAL, tool="terminal")
        self.assertEqual((second.action, second.code, second.count), ("halt", stop.HALT_CODE, 2))
        self.assertIs(controller.halt_decision, second)

    def test_the_threshold_comes_from_the_config(self):
        controller = FakeController(halt_after=3)
        self.assertEqual(observe(controller, REFUSAL).action, "warn")
        self.assertEqual(observe(controller, REFUSAL).action, "warn")
        self.assertEqual(observe(controller, REFUSAL).action, "halt")
        self.assertEqual(stop.halt_after(object()), stop.DEFAULT_HALT_AFTER)
        self.assertEqual(stop.halt_after(type("C", (), {stop.HALT_AFTER_ATTR: 0})()), stop.DEFAULT_HALT_AFTER)

    def test_a_new_turn_starts_the_count_again(self):
        controller = FakeController(halt_after=2)
        observe(controller, REFUSAL)
        controller.reset_for_turn()
        again = observe(controller, REFUSAL)
        self.assertEqual((again.action, again.count), ("warn", 1))

    def test_the_count_spans_tools(self):
        controller = FakeController(halt_after=2)
        observe(controller, REFUSAL, tool="terminal")
        halt = observe(controller, REFUSAL, tool="execute_code")
        self.assertEqual(halt.action, "halt")
        self.assertEqual(halt.tool_name, "execute_code")

    def test_guidance_never_overrides_a_stock_block_or_halt(self):
        controller = FakeController()
        stock_halt = Decision("halt", "same_tool_failure_halt", "", "terminal", 8, None)
        self.assertIsNone(observe(controller, K8S_403, stock=stock_halt))
        self.assertIsNone(observe(controller, REFUSAL, args={"command": "kubectl config view"}, stock=stock_halt))
        # A definitive refusal still decides for itself.
        self.assertEqual(observe(controller, REFUSAL, stock=stock_halt).action, "halt")

    def test_the_turn_token_is_the_container_itself_not_its_address(self):
        controller = FakeController(halt_after=2)
        observe(controller, REFUSAL)
        first = controller._call_history
        controller.reset_for_turn()
        self.assertIsNot(controller._call_history, first)
        self.assertEqual(observe(controller, REFUSAL).count, 1)
        self.assertIs(getattr(controller, stop._COUNT_ATTR)[0], controller._call_history)

    def test_an_api_denial_a_gcloud_refusal_or_a_refused_read_warns_and_never_counts(self):
        controller = FakeController()
        for result in (K8S_403, GCP_403, MCP_403):
            decision = observe(controller, result)
            self.assertEqual((decision.action, decision.code, decision.count), ("warn", stop.API_WARN_CODE, 0))
        gcloud = observe(controller, GCLOUD_REFUSAL, args={"command": "gcloud artifacts docker tags list x"})
        self.assertEqual((gcloud.action, gcloud.code, gcloud.count), ("warn", stop.ADVISORY_WARN_CODE, 0))
        self.assertIn("use a spelling the policy allows", gcloud.message)
        read = observe(controller, REFUSAL, args={"command": "kubectl config view"})
        self.assertEqual((read.action, read.code, read.count), ("warn", stop.ADVISORY_WARN_CODE, 0))
        self.assertIsNone(controller.halt_decision)
        self.assertEqual(observe(controller, REFUSAL).count, 1)
        self.assertEqual(stop.DEFAULT_HALT_AFTER, 1)

    def test_a_successful_read_whose_output_quotes_a_denial_is_not_an_api_denial(self):
        controller = FakeController()
        events = _result('LAST SEEN  MESSAGE\n2m  Error creating: pods "x" is forbidden: exceeded quota', exit_code=0)
        self.assertIsNone(observe(controller, events, args={"command": "kubectl get events -n tenant"}, failed=False))
        self.assertEqual(observe(controller, K8S_403, failed=True).code, stop.API_WARN_CODE)
        # A definitive refusal does not need the failure flag: a write chained before a read exits 0.
        self.assertEqual(observe(controller, REFUSAL, args={"command": "kubectl scale deploy x --replicas=3; kubectl get deploy x"}, failed=False).action, "halt")

    def test_anything_else_falls_through_to_the_stock_path(self):
        controller = FakeController()
        for result in (FLAG_REFUSAL, UNAUTHENTICATED, PLAIN_FAILURE, OK, None):
            self.assertIsNone(observe(controller, result), result)

    def test_a_controller_without_the_turn_container_still_counts(self):
        controller = FakeController(halt_after=2)
        del controller._call_history
        observe(controller, REFUSAL)
        self.assertEqual(observe(controller, REFUSAL).action, "halt")


class NudgeTests(unittest.TestCase):
    def test_the_halt_nudge_names_the_refusal_and_the_board_call(self):
        decision = Decision("halt", stop.HALT_CODE, "", "terminal", 2, None)
        nudge = guardrail_halt_nudge(lambda **_: "[System: base]", messages=[], attempts=0, decision=decision)
        self.assertTrue(nudge.startswith("[System: base]"))
        self.assertIn("refused this change", nudge)
        self.assertIn("If the card asked for a pull request", nudge)
        self.assertIn("kanban_block", nudge)
        self.assertIn("kanban_block", nudge)
        self.assertNotIn("exhausted", nudge)

    def test_other_halts_keep_the_stock_suffix(self):
        decision = Decision("halt", "loop_web_search_cap", "", "web_search", 50, None)
        nudge = guardrail_halt_nudge(lambda **_: "[System: base]", messages=[], attempts=0, decision=decision)
        self.assertIn("exhausted", nudge)


class ApplyTests(unittest.TestCase):
    RUN_AGENT = "class AIAgent:\n" + applier.HALT_RESPONSE_ANCHOR + "            'stock'\n        )\n"

    def _hermes(self, source, run_agent=RUN_AGENT):
        root = Path(tempfile.mkdtemp())
        (root / "agent").mkdir()
        (root / applier.GUARDRAILS_RELATIVE).write_text(source, encoding="utf-8")
        (root / applier.RUN_AGENT_RELATIVE).write_text(run_agent, encoding="utf-8")
        return root

    def test_all_anchors_are_patched_once_and_the_second_run_is_refused(self):
        source = "class C:\n" + applier.AFTER_CALL_ANCHOR + "        return x\n" + "    def f(self):\n" + applier.FROM_MAPPING_ANCHOR
        root = self._hermes(source)
        applier.apply(root)
        patched = (root / applier.GUARDRAILS_RELATIVE).read_text(encoding="utf-8")
        self.assertEqual(patched.count(applier.AFTER_CALL_MARKER), 1)
        self.assertEqual(patched.count(applier.FROM_MAPPING_MARKER), 1)
        # The wrapper carries the stock signature and the stock body is kept under its new name.
        self.assertEqual(patched.count("def after_call("), 1)
        self.assertEqual(patched.count("def _kube_stock_after_call("), 1)
        self.assertLess(patched.index("def after_call("), patched.index("def _kube_stock_after_call("))
        self.assertIn("        return x\n", patched)
        self.assertNotIn(applier.FROM_MAPPING_ANCHOR, patched)
        run_agent = (root / applier.RUN_AGENT_RELATIVE).read_text(encoding="utf-8")
        self.assertEqual(run_agent.count(applier.HALT_RESPONSE_MARKER), 1)
        self.assertIn("'stock'", run_agent)
        with self.assertRaises(SystemExit):
            applier.apply(root)

    def test_a_missing_or_doubled_anchor_in_either_file_is_refused_and_nothing_is_written(self):
        good = applier.AFTER_CALL_ANCHOR + applier.FROM_MAPPING_ANCHOR
        for source, run_agent in (
            ("def after_call(self):\n    pass\n", self.RUN_AGENT),
            (applier.AFTER_CALL_ANCHOR * 2 + applier.FROM_MAPPING_ANCHOR, self.RUN_AGENT),
            (good, "class AIAgent:\n    pass\n"),
        ):
            root = self._hermes(source, run_agent)
            with self.assertRaises(SystemExit):
                applier.apply(root)
            self.assertEqual((root / applier.GUARDRAILS_RELATIVE).read_text(encoding="utf-8"), source)
            self.assertEqual((root / applier.RUN_AGENT_RELATIVE).read_text(encoding="utf-8"), run_agent)


if __name__ == "__main__":
    unittest.main()

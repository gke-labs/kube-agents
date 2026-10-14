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
# The runtime reads kubectl's verb through ``tools.command_policy``, the module the image
# ships; register the same module under that name so the test runs the image's path.
import types  # noqa: E402

_tools = sys.modules.get("tools") or types.ModuleType("tools")
_tools.command_policy = command_policy
sys.modules["tools"] = _tools
sys.modules["tools.command_policy"] = command_policy


def _result(output, exit_code=77):
    return json.dumps({"output": output, "exit_code": exit_code, "error": None})


def shim_lines(argv):
    """What the credential-proxy shim prints for ``argv`` when the policy refuses it:
    the policy's message, then ``policy rule:``, ``policy verb:`` and ``policy render:``."""
    decision = command_policy.evaluate(argv)
    assert not decision.allowed, argv
    lines = [decision.message, f"policy rule: {decision.rule_id}"]
    if decision.verb_tuple:
        lines.append("policy verb: " + " ".join(decision.verb_tuple))
    if command_policy.local_render(argv):
        lines.append("policy render: local")
    return "\n".join(lines)


def refusal(*argv, exit_code=77):
    return _result(shim_lines(list(argv)), exit_code)


SCALE = ["kubectl", "scale", "deploy/kube-dns", "-n", "kube-system", "--replicas=3"]
REFUSAL = refusal(*SCALE)
FLAG_REFUSAL = _result(command_policy._FLAG_BOUNDARY_NOTICE + "\npolicy rule: identity.caller-supplied-impersonation")
GCLOUD_REFUSAL = _result(
    "Agents hold read-only access to Google Cloud. Propose this change as a pull request instead. "
    + command_policy._ACTION_BOUNDARY_NOTICE + "\npolicy rule: gcp.read-only\npolicy verb: container clusters update"
)
#: A shim older than the proxy change prints no verb line.
OLD_SHIM_REFUSAL = _result(command_policy.evaluate(SCALE).message + "\npolicy rule: kubernetes.read-only")
K8S_403 = _result('Error from server (Forbidden): deployments.apps "kube-dns" is forbidden: User cannot patch', 1)
GCP_403 = _result("ERROR: (gcloud.container.clusters.update) ResponseError: code=403, message=Required permission denied PERMISSION_DENIED", 1)
MCP_403 = '{"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "The caller does not have permission"}}'
UNAUTHENTICATED = _result("error: You must be logged in to the server (Unauthorized)", 1)
PLAIN_FAILURE = _result("error: the server doesn't have a resource type \"foo\"", 1)
OK = _result("deployment.apps/kube-dns 2/2", 0)


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


WRITE = {"command": "kubectl scale deploy/kube-dns -n kube-system --replicas=3"}
ALLOW = Decision()


def observe(controller, result, tool="terminal", args=WRITE, stock=ALLOW, failed=True):
    return stop.observe_refusal(
        controller, tool, args, result, "sig", stock=stock, failed=failed, decide=controller._decide, decision_cls=Decision
    )


class ClassifyTests(unittest.TestCase):
    def test_a_refused_mutating_verb_is_definitive_whatever_the_shell_text_was(self):
        for argv in (
            SCALE,
            ["kubectl", "-n", "kube-system", "patch", "deploy", "kube-dns", "-p", "{}"],
            ["kubectl", "rollout", "-n", "kube-system", "restart", "deploy/kube-dns"],
            ["kubectl", "apply", "-f", "x.yaml"],
            ["kubectl", "delete", "deploy", "x"],
            ["kubectl", "debug", "node/x", "-it", "--image=busybox"],
            ["kubectl", "certificate", "approve", "csr-1"],
            ["kubectl", "auth", "reconcile", "-f", "rbac.yaml"],
            ["kubectl", "scale", "deploy/x", "--replicas=3", "--dry-run=server"],
            ["kubectl", "scale", "deploy/x", "--replicas=3", "--dry-run=none"],
        ):
            self.assertEqual(stop.classify_refusal("whatever the shell said", refusal(*argv)), "policy", argv)
        # The verdict is the result's: a chained write that exits 0, a heredoc or a Python call that
        # wrapped it. On the exit-0 path the text is consulted once, by the word test, which each satisfies.
        for command, result in (
            ("kubectl scale deploy x --replicas=3; kubectl get deploy x", refusal(*SCALE, exit_code=0)),
            ("bash <<'EOF'\nkubectl scale deploy x --replicas=3\nEOF", REFUSAL),
            ("kubectl rollout -n kube-system restart deploy/x || true", refusal("kubectl", "rollout", "restart", "deploy/x", exit_code=0)),
            ('subprocess.run(["kubectl", "scale", "deploy/x", "--replicas=3"])', REFUSAL),
            ('subprocess.run(\n    [\n        "kubectl",\n        "scale",\n        "deploy/x",\n        "-n", "kube-system",\n        "--replicas=3",\n    ],\n    capture_output=True, text=True,\n)', REFUSAL),
            ("kubectl scale deploy x --replicas=3", refusal(*SCALE, exit_code=126)),
        ):
            self.assertEqual(stop.classify_refusal(command, result, failed=False), "policy", command)
        # A successful call whose text names no write only echoed the lines (a log, a cat).
        for command in ("kubectl logs deploy/app", "cat /opt/data/kanban/logs/t_1.log", "grep -r 'policy verb' /opt/data"):
            self.assertEqual(stop.classify_refusal(command, refusal(*SCALE, exit_code=0), failed=False), "advisory", command)

    def test_a_refused_read_spelling_a_render_a_help_read_or_a_gcloud_refusal_is_advisory(self):
        for argv in (
            ["kubectl", "config", "view"],
            ["kubectl", "cluster-info", "dump"],
            ["kubectl", "diff", "-f", "patch.yaml"],
            ["kubectl", "port-forward", "svc/x", "8080:80"],
            ["kubectl", "exec", "-n", "kube-system", "deploy/kube-dns", "--", "sh", "-c", "set -e; cat /etc/resolv.conf"],
            ["kubectl", "cp", "pod:/tmp/f", "./out", "-n", "label"],
            ["kubectl", "scale", "--help"],
            ["kubectl", "patch", "-h"],
            ["kubectl", "help", "scale"],
            ["kubectl", "create", "deployment", "x", "--image=y", "--dry-run=client", "-o", "yaml"],
            ["kubectl", "patch", "--local", "-f", "m.yaml", "-p", "{}", "-o", "yaml"],
            ["kubectl", "label", "pod", "x", "--list"],
            ["kubectl", "apply", "view-last-applied", "deploy/kube-dns"],
            ["kubectl", "rollout", "--unknown", "status", "deploy/x"],
        ):
            self.assertEqual(stop.classify_refusal("", refusal(*argv)), "advisory", argv)
        self.assertEqual(stop.classify_refusal("gcloud container clusters update x", GCLOUD_REFUSAL), "advisory")
        self.assertEqual(stop.classify_refusal("kubectl scale deploy x --replicas=3", OLD_SHIM_REFUSAL), "advisory")

    def test_a_call_that_echoes_the_results_own_refusal_lines_is_not_one(self):
        lines = shim_lines(SCALE)
        for command in (
            "echo 'Refused: `kubectl scale deploy x --replicas=3`:\n" + lines + "' | tee /tmp/pr.md",
            "cat <<'EOF'\n" + lines + "\nEOF",
        ):
            self.assertIsNone(stop.classify_refusal(command, _result(lines, 0), failed=False), command)
        # A comment quoting an earlier rule line beside a fresh write is the retry, not an
        # echo: on the exit-0 path, where the echo test is consulted, the result's own verb
        # line is what the test looks for, and the comment does not carry it.
        retry = "# direct scale was refused (policy rule: kubernetes.read-only); patch the autoscaler instead\nkubectl patch configmap kube-dns-autoscaler -n kube-system -p '{}' || true"
        patch = refusal("kubectl", "patch", "configmap", "kube-dns-autoscaler", "-n", "kube-system", "-p", "{}", exit_code=0)
        self.assertEqual(stop.classify_refusal(retry, patch, failed=False), "policy")

    def test_a_failed_retry_that_pastes_the_earlier_refusal_block_still_counts(self):
        lines = shim_lines(SCALE)
        retry = "# refused earlier:\n# " + lines.replace("\n", "\n# ") + "\nkubectl scale deploy/kube-dns -n kube-system --replicas=3"
        self.assertEqual(stop.classify_refusal(retry, _result(lines, 77), failed=True), "policy")
        self.assertIsNone(stop.classify_refusal("echo '" + lines + "'", _result(lines, 0), failed=False))

    def test_an_echoed_block_beside_a_fresh_refusal_of_another_verb_is_not_an_echo(self):
        scale = shim_lines(SCALE)
        patch = shim_lines(["kubectl", "patch", "deploy/kube-dns", "-n", "kube-system", "-p", "{}"])
        command = "tee /tmp/notes.md <<'EOF'\nfirst attempt:\n" + scale + "\nEOF\nkubectl patch deploy/kube-dns -n kube-system -p '{}' || true"
        self.assertEqual(stop.classify_refusal(command, _result(scale + "\n" + patch, 0), failed=False), "policy")
        self.assertIsNone(stop.classify_refusal("cat <<'EOF'\n" + scale + "\n" + patch + "\nEOF", _result(scale + "\n" + patch, 0), failed=False))

    def test_several_refusals_in_one_result_count_if_any_is_a_write(self):
        render_then_apply = _result(
            shim_lines(["kubectl", "create", "deployment", "x", "--image=y", "--dry-run=client", "-o", "yaml"])
            + "\n" + shim_lines(["kubectl", "apply", "-f", "-"])
        )
        self.assertEqual(stop.classify_refusal("kubectl create deployment x --image=y --dry-run=client -o yaml | kubectl apply -f -", render_then_apply), "policy")
        read_then_scale = _result(shim_lines(["kubectl", "exec", "pod", "--", "cat", "/x"]) + "\n" + shim_lines(SCALE))
        self.assertEqual(stop.classify_refusal("kubectl exec pod -- cat /x; kubectl scale deploy/kube-dns -n kube-system --replicas=3", read_then_scale), "policy")
        flag_then_scale = _result(command_policy._FLAG_BOUNDARY_NOTICE + "\npolicy rule: identity.caller-supplied-impersonation\n" + shim_lines(SCALE))
        self.assertEqual(stop.classify_refusal("kubectl --as admin scale deploy/x --replicas=3; kubectl scale deploy/kube-dns -n kube-system --replicas=3", flag_then_scale), "policy")
        self.assertEqual(len(stop.refusal_blocks(json.loads(render_then_apply)["output"])), 2)

    def test_the_exit_zero_word_test_is_what_the_docstring_says_it_is(self):
        lines = shim_lines(SCALE)
        # Counted, by design: a report that names the command and prints a saved refusal in one exit-0 call.
        self.assertEqual(stop.classify_refusal("echo '$ kubectl scale deploy kube-dns --replicas=3'; cat /tmp/refusal.txt | tee /tmp/pr-body.md", _result(lines, 0), failed=False), "policy")
        # Not counted, by design: a write that reached kubectl through a script or a variable and exited 0.
        self.assertEqual(stop.classify_refusal("bash /tmp/retry.sh; echo done", _result(lines, 0), failed=False), "advisory")
        # ``auth can-i <verb>`` names the verb in a read's arguments.
        self.assertEqual(stop.classify_refusal("kubectl auth can-i scale deployments -n kube-system; cat /tmp/refusal.txt", _result(lines, 0), failed=False), "advisory")
        self.assertEqual(stop.classify_refusal("kubectl -n kube-system --context prod auth can-i scale deployments; cat /tmp/refusal.txt", _result(lines, 0), failed=False), "advisory")
        self.assertEqual(stop.classify_refusal("kubectl scale deploy/kube-dns -n kube-system --replicas=3 || true; kubectl auth can-i scale deployments", _result(lines, 0), failed=False), "policy")
        self.assertEqual(stop.classify_refusal("kubectl scale deploy/kube-dns -n kube-system --replicas=3\nkubectl auth can-i get deploy -n kube-system\nkubectl get deploy kube-dns -n kube-system", _result(lines, 0), failed=False), "policy")
        self.assertEqual(stop.classify_refusal("kubectl auth can-i scale deployments -n kube-system; kubectl scale deploy/kube-dns -n kube-system --replicas=3 || true", _result(lines, 0), failed=False), "policy")
        # The verb is kubectl's first subcommand: a verb word elsewhere in the text is not one.
        for command in (
            "kubectl get pods -n run; cat /tmp/refusal.txt",
            "kubectl logs deploy/api -c debug; cat /tmp/refusal.txt",
            "kubectl describe deploy kube-dns -n kube-system | grep -i scale; cat /tmp/refusal.txt",
            "kubectl get events -A | grep -E 'create|delete'; cat /tmp/refusal.txt",
        ):
            self.assertEqual(stop.classify_refusal(command, _result(lines, 0), failed=False), "advisory", command)
        self.assertEqual(stop.classify_refusal("kubectl -n kube-system scale deploy/kube-dns --replicas=3 || true; cat /tmp/refusal.txt", _result(lines, 0), failed=False), "policy")
        # A client render or a help read chained before the cat is not a write either.
        self.assertEqual(stop.classify_refusal("kubectl create deployment kube-dns --image=x --dry-run=client -o yaml > /tmp/m.yaml; cat /tmp/refusal.txt", _result(lines, 0), failed=False), "advisory")
        self.assertEqual(stop.classify_refusal("kubectl scale --help; cat /tmp/refusal.txt", _result(lines, 0), failed=False), "advisory")
        self.assertEqual(stop.classify_refusal("kubectl scale deploy/kube-dns --replicas=3 --dry-run=false || true; cat /tmp/refusal.txt", _result(lines, 0), failed=False), "policy")
        # A shell prologue's ``set`` is before the kubectl word, so it is not the verb.
        self.assertEqual(stop.classify_refusal("set -euo pipefail; kubectl get deploy kube-dns -n kube-system -o yaml > /tmp/c.yaml; cat /tmp/refusal.txt", _result(lines, 0), failed=False), "advisory")

    def test_a_flag_refusal_is_not_a_refusal_of_the_change(self):
        self.assertIsNone(stop.classify_refusal("kubectl --as x get pods", FLAG_REFUSAL))

    def test_api_denials_are_api(self):
        for result in (K8S_403, GCP_403, MCP_403):
            self.assertEqual(stop.classify_refusal("kubectl get pods", result), "api", result)

    def test_unauthenticated_failures_and_successes_are_not_refusals(self):
        for result in (UNAUTHENTICATED, PLAIN_FAILURE, OK, "", None, 42):
            self.assertIsNone(stop.classify_refusal("kubectl get pods", result), result)

    def test_the_verb_test_matches_the_policy_shape(self):
        self.assertTrue(stop.verb_is_mutating(["scale", "deploy/x"]))
        self.assertTrue(stop.verb_is_mutating(["rollout", "restart"]))
        self.assertFalse(stop.verb_is_mutating(["rollout", "status"]))
        self.assertFalse(stop.verb_is_mutating(["exec", "pod"]))
        self.assertFalse(stop.verb_is_mutating([]))
        self.assertEqual(stop.command_text({"code": "import os", "language": "python"}), "import os\npython")
        self.assertEqual(stop.command_text(None), "")


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
        self.assertIsNone(observe(controller, refusal("kubectl", "config", "view"), args={"command": "kubectl config view"}, stock=stock_halt))
        # A definitive refusal still decides for itself at the threshold; under it the
        # refusal is tallied and the stock halt stands.
        self.assertEqual(observe(controller, REFUSAL, stock=stock_halt).action, "halt")
        raised = FakeController(halt_after=2)
        self.assertIsNone(observe(raised, REFUSAL, stock=stock_halt))
        self.assertEqual(observe(raised, REFUSAL).action, "halt")  # the first was counted

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
        self.assertIn("respelled as the policy allows", gcloud.message)
        read = observe(controller, refusal("kubectl", "config", "view"), args={"command": "kubectl config view"})
        self.assertEqual((read.action, read.code, read.count), ("warn", stop.ADVISORY_WARN_CODE, 0))
        self.assertIsNone(controller.halt_decision)
        self.assertEqual(observe(controller, REFUSAL).count, 1)
        self.assertEqual(stop.DEFAULT_HALT_AFTER, 1)

    def test_a_successful_read_whose_output_quotes_a_denial_is_not_an_api_denial(self):
        controller = FakeController()
        events = _result('LAST SEEN  MESSAGE\n2m  Error creating: pods "x" is forbidden: exceeded quota', exit_code=0)
        self.assertIsNone(observe(controller, events, args={"command": "kubectl get events -n tenant"}, failed=False))
        self.assertEqual(observe(controller, K8S_403, failed=True).code, stop.API_WARN_CODE)
        # Nor is a successful read, or a board read, whose text quotes the policy's notice an advisory refusal.
        echoed = _result(REFUSAL, exit_code=0)
        self.assertIsNone(observe(controller, echoed, args={"command": "kubectl logs deploy/app"}, failed=False))
        self.assertIsNone(observe(controller, '{"task": {"result": ' + json.dumps(REFUSAL) + '}}', tool="kanban_show", args={"task_id": "t_1"}, failed=False))
        self.assertEqual(observe(controller, refusal("kubectl", "config", "view"), args={"command": "kubectl config view"}, failed=True).code, stop.ADVISORY_WARN_CODE)
        # Program text: the shim's lines are in the result whatever tool ran the command.
        self.assertEqual(observe(controller, REFUSAL, tool="execute_code", args={"code": "import subprocess\nsubprocess.run(['kubectl','scale','deploy/x'])", "language": "python"}, failed=False).action, "halt")
        # A definitive refusal does not need the failure flag: a write chained before a read exits 0.
        self.assertEqual(observe(controller, refusal(*SCALE, exit_code=0), args={"command": "kubectl scale deploy x --replicas=3; kubectl get deploy x"}, failed=False).action, "halt")

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

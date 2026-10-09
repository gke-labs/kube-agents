import json
import os
import re
import shutil
import subprocess
import textwrap
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = Path(__file__).parent.absolute() / "cluster_preflight.sh"
# The terminal tool's default call timeout is Hermes' (`TERMINAL_TIMEOUT` in
# tools/terminal_tool.py), out of this tree: deploy/shared/terminal_env_pin.py
# --build-check holds the script's mirror equal to it at image build. Here the
# mirror is held equal to the figure the SOP states, so the two in-tree copies
# cannot drift apart, and the budget under both.
SOP = Path(__file__).parent.parent / "governance" / "obtainability_audit_sop.md"
SOP_TERMINAL_DEFAULT_RE = r"the default (\d+)-second timeout"
BROKER = Path(__file__).parent.absolute() / "credential_proxy.py"
# The two broker bounds the per-call cap is built from, as each side declares
# them: the broker holds a request in admission for this long before refusing
# it, then bounds a one-shot read.
BROKER_BOUNDS = (
    ("COMMAND_SLOT_WAIT_SECONDS", "BROKER_ADMISSION_WAIT_SECONDS"),
    ("DEFAULT_KUBECTL_TIMEOUT_SECONDS", "BROKER_KUBECTL_RUN_SECONDS"),
)
SCRIPT_CAP_MARGIN = "CAP_MARGIN_SECONDS"

PROJECT = "demo-project"
CLUSTER = "cluster-a"
LOCATION = "us-central1"
EXPECTED_CONTEXT = f"gke_{PROJECT}_{LOCATION}_{CLUSTER}"

# Well under the budget the preflight shares across its kubectl calls, and under
# the 20s the hanging fake sleeps: a run this fast cannot have waited on either.
PREFLIGHT_FAST_SECONDS = 10
# A check 4 wait just past the cap the preflight had before #2632 (15s): long
# enough that the old cap would have fired, short enough to keep the suite fast.
QUEUED_PAST_OLD_CAP_SECONDS = 16
# A small budget and a hang that outlasts it, for the test that the preflight
# still answers with JSON when a brokered call never returns.
SHORT_BUDGET_SECONDS = 4
HANG_PAST_SHORT_BUDGET_SECONDS = 8

# Padding one byte longer than the reader's 1 MiB cap (KUBECONFIG_READ_MAX_BYTES),
# so the file is over it however small the rest of the fixture gets.
KUBECONFIG_OVER_CAP_BYTES = (1 << 20) + 1

# A fake `kubectl` covering only the two invocations the preflight makes (check
# 4's `config current-context`, check 5's `cluster-info`), plus the
# `--kubeconfig=<file>` form check 3 used to run, kept so a test can make it
# hang. It reads the context out of a kubeconfig the way the real one does, so
# a test fixes the *file* and the script's own logic decides the verdict.
#
# These knobs reproduce the failure modes that matter:
#   FAKE_AMBIENT_CONTEXT   - what a plain `kubectl` resolves to when it ignores
#                            KUBECONFIG, i.e. the credential-proxy sidecar's own
#                            context leaking in. Unset means KUBECONFIG is honoured.
#   FAKE_UNREACHABLE       - make `cluster-info` fail like a dead API server.
#   FAKE_CONFIG_FAILS      - make `config current-context` fail the way a
#                            credential-proxy outage does: non-zero, error on
#                            stderr, nothing on stdout.
#   FAKE_KUBECONFIG_FLAG_HANGS - make `--kubeconfig=<file> config current-context`
#                            hang for 20s, the way a request queued behind a
#                            saturated credential broker does (longer than the
#                            15s cap the preflight had when check 3 made this call).
#                            Only that form, the one check 3 used to run: a real
#                            broker queues check 4's call too (see the timeout
#                            test below).
#   FAKE_CONFIG_TIMES_OUT  - make `config current-context` exit 124, as the
#                            preflight's `timeout` wrapper does when it fires.
#   FAKE_CONFIG_HANG_SECONDS - make the plain `config current-context` (check 4's
#                            call) answer only after this many seconds, the way
#                            a request queued at a busy broker is answered once
#                            admitted.
#   FAKE_CLUSTER_INFO_TIMES_OUT - the same for `cluster-info`, check 5's call.
#   FAKE_KUBECTL_LOG       - append every argv the fake receives to this file.
#
# The context is read with PyYAML, as the credential-proxy shim reads it, so a
# quoted or commented value means the same thing to the fake as to the real one.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/bin/bash
    set -u
    from_file() {
        python3 -c 'import sys, yaml
    doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8", errors="replace")) or {}
    print(doc.get("current-context") or "")' "$1"
    }
    [ -n "${FAKE_KUBECTL_LOG:-}" ] && printf '%s\\n' "$*" >>"$FAKE_KUBECTL_LOG"

    KCFG=""
    ARGS=()
    for arg in "$@"; do
        case "$arg" in
            --kubeconfig=*) KCFG="${arg#--kubeconfig=}" ;;
            *) ARGS+=("$arg") ;;
        esac
    done

    case "${ARGS[*]}" in
        "config current-context")
            [ -n "${FAKE_CONFIG_TIMES_OUT:-}" ] && exit 124
            [ -z "$KCFG" ] && [ -n "${FAKE_CONFIG_HANG_SECONDS:-}" ] && sleep "$FAKE_CONFIG_HANG_SECONDS"
            if [ -n "${FAKE_CONFIG_FAILS:-}" ]; then
                echo "credential proxy unavailable: [Errno 111] Connection refused" >&2
                exit 1
            fi
            if [ -n "$KCFG" ] && [ -n "${FAKE_KUBECONFIG_FLAG_HANGS:-}" ]; then
                exec sleep 20
            fi
            if [ -n "$KCFG" ]; then
                from_file "$KCFG"
            elif [ -n "${FAKE_AMBIENT_CONTEXT:-}" ]; then
                printf '%s\\n' "$FAKE_AMBIENT_CONTEXT"
            else
                from_file "${KUBECONFIG:-/nonexistent}"
            fi
            ;;
        "cluster-info"*)
            [ -n "${FAKE_CLUSTER_INFO_TIMES_OUT:-}" ] && exit 124
            if [ -n "${FAKE_UNREACHABLE:-}" ]; then
                echo "Unable to connect to the server: dial tcp: i/o timeout" >&2
                exit 1
            fi
            echo "Kubernetes control plane is running at https://198.51.100.1"
            ;;
        *) echo "fake kubectl: unexpected args: ${ARGS[*]}" >&2; exit 64 ;;
    esac
    """
)

USER_MD = f"""# Cluster Agent Context

This Cluster Agent is permanently scoped to the following GKE cluster:

- project: {PROJECT}
- cluster: {CLUSTER}
- location: {LOCATION}
"""


def _declared(pattern: str, text: str, where: Path) -> int:
    match = re.search(pattern, text, re.MULTILINE)
    assert match, f"{where.name} no longer declares {pattern!r}"
    return int(match.group(1))


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def terminal_tool_timeout_seconds() -> int:
    return _declared(r"^readonly TERMINAL_TOOL_TIMEOUT_SECONDS=(\d+)$", _script(), SCRIPT)


def budget_margin_seconds() -> int:
    return _declared(r"^readonly PREFLIGHT_BUDGET_MARGIN_SECONDS=(\d+)$", _script(), SCRIPT)


def default_budget_seconds() -> int:
    """The budget as the script computes it: the tool's default less the margin."""
    return terminal_tool_timeout_seconds() - budget_margin_seconds()


def sop_terminal_default_seconds() -> int:
    return _declared(SOP_TERMINAL_DEFAULT_RE, SOP.read_text(encoding="utf-8"), SOP)


def broker_bounds() -> dict[str, int]:
    text = BROKER.read_text(encoding="utf-8")
    return {name: _declared(rf"^{name} = (\d+)$", text, BROKER) for name, _ in BROKER_BOUNDS}


def script_bounds() -> dict[str, int]:
    bounds = {name: _declared(rf"^readonly {name}=(\d+)$", _script(), SCRIPT) for _, name in BROKER_BOUNDS}
    bounds[SCRIPT_CAP_MARGIN] = _declared(rf"^readonly {SCRIPT_CAP_MARGIN}=(\d+)$", _script(), SCRIPT)
    return bounds


def kubectl_cap_seconds() -> int:
    """The per-call cap as the script computes it: the two mirrored broker bounds plus the margin."""
    return sum(script_bounds().values())


class ClusterPreflightTest(unittest.TestCase):
    """Runs the real script against a fake kubectl.

    Every test asserts on the machine-readable `--json` contract, because that is
    what the Cluster Agent quotes into `kanban_block` (cluster SOUL.md §6).
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.bin = Path(self._tmp.name) / "bin"
        self.bin.mkdir()
        kubectl = self.bin / "kubectl"
        kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
        kubectl.chmod(0o755)

        self.user_md = self.home / "USER.md"
        self.user_md.write_text(USER_MD, encoding="utf-8")
        self.kubeconfig = self.home / "kubeconfig.yaml"
        self.write_kubeconfig(EXPECTED_CONTEXT)

    def write_kubeconfig(self, context: str) -> None:
        body = "apiVersion: v1\nkind: Config\n"
        if context:
            body += f"current-context: {context}\n"
        self.kubeconfig.write_text(body, encoding="utf-8")

    def write_raw_kubeconfig(self, tail: str) -> None:
        self.kubeconfig.write_text("apiVersion: v1\nkind: Config\n" + tail, encoding="utf-8")

    def run_preflight(self, **extra_env) -> dict:
        # KUBECONFIG is exported by default because that is the real dispatch
        # path: cluster_agent_profile.py writes it into the profile's .env and
        # Hermes loads it. Pass KUBECONFIG=None to model a profile that never
        # got the pin - which check 4 must now catch rather than paper over.
        env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "HERMES_HOME": str(self.home),
            "KUBECONFIG": str(self.kubeconfig),
            **extra_env,
        }
        env = {k: v for k, v in env.items() if v is not None}
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--json"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual("", proc.stderr, "preflight must not leak stderr into the card")
        result = json.loads(proc.stdout)
        # The exit code and the JSON status must never disagree; the agent branches
        # on the JSON, the dispatcher on the code.
        self.assertEqual(result["status"] == "ok", proc.returncode == 0)
        return result

    # ---- The happy path ------------------------------------------------------

    def test_passes_when_the_pin_matches_the_declared_cluster(self):
        self.assertEqual("ok", self.run_preflight()["status"])

    def test_accepts_kubeconfig_exported_in_the_environment(self):
        # The dispatch path exports KUBECONFIG from the profile's .env rather than
        # relying on the $HERMES_HOME/kubeconfig.yaml fallback.
        moved = self.home / "pinned.yaml"
        self.kubeconfig.rename(moved)
        self.kubeconfig = moved
        self.assertEqual("ok", self.run_preflight(KUBECONFIG=str(moved))["status"])

    def test_identity_fields_are_read_case_insensitively(self):
        self.user_md.write_text(USER_MD.replace("- project:", "- Project:"), encoding="utf-8")
        self.assertEqual("ok", self.run_preflight()["status"])

    # ---- Identity: the pin points somewhere else -----------------------------

    def test_fails_when_the_pin_is_for_another_cluster(self):
        # The case this check exists for: everything is present and reachable, so
        # the old preflight passed and the agent investigated the wrong cluster.
        self.write_kubeconfig(f"gke_{PROJECT}_{LOCATION}_someone-elses-cluster")
        result = self.run_preflight()
        self.assertEqual("failed", result["status"])
        self.assertIn("different cluster", result["reason"])
        self.assertIn(EXPECTED_CONTEXT, result["evidence"])
        self.assertIn("someone-elses-cluster", result["evidence"])

    def test_fails_when_the_pin_is_for_the_management_cluster(self):
        # The observed incident: the sidecar's own host-cluster context.
        self.write_kubeconfig(f"gke_{PROJECT}_{LOCATION}_kage-management")
        self.assertEqual("failed", self.run_preflight()["status"])

    def test_fails_on_a_cluster_name_that_is_a_prefix_of_the_declared_one(self):
        # `brad` vs `brad-2` — a substring match would pass this.
        self.write_kubeconfig(f"gke_{PROJECT}_{LOCATION}_{CLUSTER}-2")
        self.assertEqual("failed", self.run_preflight()["status"])

    def test_fails_when_the_pin_is_for_the_right_cluster_in_another_project(self):
        self.write_kubeconfig(f"gke_other-project_{LOCATION}_{CLUSTER}")
        self.assertEqual("failed", self.run_preflight()["status"])

    def test_fails_when_the_pin_selects_no_context(self):
        self.write_kubeconfig("")
        result = self.run_preflight()
        self.assertEqual("failed", result["status"])
        self.assertIn("does not select a cluster", result["reason"])

    # ---- Identity: the pin is right but nothing uses it ----------------------

    def test_fails_when_kubectl_ignores_the_pinned_kubeconfig(self):
        # KUBECONFIG dropped on the way to the command (the credential-proxy
        # transport bug): the file is correct, so check 3 passes, but every plain
        # kubectl the agent runs lands on the sidecar's context instead.
        result = self.run_preflight(
            FAKE_AMBIENT_CONTEXT=f"gke_{PROJECT}_{LOCATION}_kage-management"
        )
        self.assertEqual("failed", result["status"])
        self.assertIn("does not use this agent's pinned kubeconfig", result["reason"])
        self.assertIn("kage-management", result["evidence"])

    def test_fails_when_kubeconfig_is_not_exported_at_all(self):
        # The gap a re-injected `env KUBECONFIG=...` hid: the profile's .env never
        # got the pin. The file is present and correct, so checks 2 and 3 pass on
        # it, and check 4 used to force the variable back in and pass as well -
        # while every real `kubectl` the agent runs afterwards has no pin at all.
        result = self.run_preflight(KUBECONFIG=None)
        self.assertEqual("failed", result["status"])
        self.assertIn("does not export KUBECONFIG", result["reason"])
        self.assertIn(".env", result["remediation"])

    def test_reports_a_proxy_outage_as_such_not_as_a_bad_pin(self):
        # A failing `kubectl config current-context` used to be swallowed, leaving
        # an empty context that read as "the pin selects no cluster" - and sent the
        # agent to re-scaffold, which runs through the same broken proxy. Check 3
        # no longer runs kubectl, so the outage surfaces at check 4, the first call
        # through the proxy, and must not read as "talking to another cluster".
        result = self.run_preflight(FAKE_CONFIG_FAILS="1")
        self.assertEqual("failed", result["status"])
        self.assertEqual("4", result["check"])
        self.assertIn("kubectl itself failed", result["reason"])
        self.assertNotIn("another cluster", result["remediation"])
        self.assertIn("Connection refused", result["evidence"])
        self.assertNotIn("does not select a cluster", result["reason"])
        self.assertNotIn("Re-scaffold the profile to re-fetch", result["remediation"])

    # ---- Check 3 reads the file, not kubectl ---------------------------------

    # In the sandbox `kubectl` is the credential-proxy shim, so a kubectl call is a
    # request queued at the broker. Check 3 reads the pinned file itself.

    def test_check_3_passes_while_kubectl_on_the_pinned_file_hangs(self):
        # The today-mode smoke failure at check 3: the shim's request for it
        # queued past the 15s cap on a busy broker and a correct pin failed.
        # Check 4 still asks the broker, and the next test covers it timing out.
        start = time.monotonic()
        result = self.run_preflight(FAKE_KUBECONFIG_FLAG_HANGS="1")
        self.assertEqual("ok", result["status"], result)
        self.assertLess(time.monotonic() - start, PREFLIGHT_FAST_SECONDS)

    def test_a_busy_broker_fails_check_4_as_a_proxy_problem_not_a_bad_pin(self):
        # With check 3 local, check 4 is the first call through the proxy, so a
        # saturated broker now times out there. It must say so, not report that
        # plain kubectl is talking to another cluster.
        result = self.run_preflight(FAKE_CONFIG_TIMES_OUT="1")
        self.assertEqual("4", result["check"])
        self.assertIn("kubectl itself failed", result["reason"])
        self.assertPerCallCapNamed(result["evidence"])
        self.assertIn("not saturated", result["remediation"])
        self.assertNotIn("another cluster", result["remediation"])

    def assertPerCallCapNamed(self, evidence: str) -> None:
        # A call cut at the per-call cap says so exactly, and names the two
        # broker bounds the cap is built from.
        self.assertIn(f"timed out after {kubectl_cap_seconds()}s", evidence)
        self.assertIn("cap on one brokered call", evidence)
        bounds = script_bounds()
        for _, name in BROKER_BOUNDS:
            self.assertIn(f"{bounds[name]}s", evidence)

    def assertBudgetCapNamed(self, evidence: str, budget: int) -> None:
        # A call cut by the budget got the budget less the seconds already
        # spent, so it reads within a few seconds of the budget, and the text
        # names the budget rather than the per-call cap.
        match = re.search(r"timed out after (\d+)s", evidence)
        self.assertIsNotNone(match, evidence)
        self.assertLessEqual(budget - 3, int(match.group(1)), evidence)
        self.assertLessEqual(int(match.group(1)), budget, evidence)
        self.assertIn(f"what was left of the preflight's {budget}s budget", evidence)
        self.assertNotIn("cap on one brokered call", evidence)

    def test_the_budget_stays_under_the_terminal_tools_default(self):
        # The Cluster Agent runs the preflight with no `timeout` argument, so the
        # terminal tool's default is the bound the whole run has to fit under:
        # past it the tool kills the script and no JSON reaches the agent. The
        # script's mirror of that default is held to Hermes at image build
        # (terminal_env_pin.py --build-check); here it is held to the figure the
        # SOP states, and the budget and the per-call cap under it.
        self.assertEqual(sop_terminal_default_seconds(), terminal_tool_timeout_seconds())
        self.assertGreater(budget_margin_seconds(), 0)
        self.assertLess(default_budget_seconds(), terminal_tool_timeout_seconds())
        self.assertLess(kubectl_cap_seconds(), default_budget_seconds())

    def test_the_kubectl_cap_outlasts_the_brokers_admission_wait_and_command_bound(self):
        # In the sandbox every kubectl is a request the broker holds in
        # admission for COMMAND_SLOT_WAIT_SECONDS before refusing it, and then
        # bounds by DEFAULT_KUBECTL_TIMEOUT_SECONDS. A cap under their sum fails
        # a correct pin while its call is still queued: at 15s, #2632. The
        # script mirrors the two figures; this holds the mirrors equal to the
        # broker's and the cap above their sum.
        broker, script = broker_bounds(), script_bounds()
        for broker_name, script_name in BROKER_BOUNDS:
            self.assertEqual(
                broker[broker_name], script[script_name],
                f"{script_name} in cluster_preflight.sh != {broker_name} in credential_proxy.py",
            )
        self.assertGreater(script[SCRIPT_CAP_MARGIN], 0)
        self.assertGreater(kubectl_cap_seconds(), sum(broker.values()))
        self.assertLess(QUEUED_PAST_OLD_CAP_SECONDS, kubectl_cap_seconds())

    def test_check_4_queued_past_the_old_cap_and_under_the_cap_passes(self):
        # The #2632 symptom: a correct pin whose check 4 call the broker admits
        # after a wait longer than 15s. This pins the wiring, so a `timeout 15`
        # put back in front of check 4 fails here rather than shipping.
        start = time.monotonic()
        result = self.run_preflight(FAKE_CONFIG_HANG_SECONDS=str(QUEUED_PAST_OLD_CAP_SECONDS))
        self.assertEqual("ok", result["status"], result)
        self.assertGreaterEqual(time.monotonic() - start, QUEUED_PAST_OLD_CAP_SECONDS)

    def test_a_call_that_never_returns_still_gets_json_inside_the_budget(self):
        # A broker that never answers, under a budget smaller than the per-call
        # cap: the call is cut at what the budget has left and the JSON is
        # printed, naming the budget. Run with a small budget so the suite stays
        # fast; the knob is the one an instruction passing a larger `timeout`
        # would use.
        start = time.monotonic()
        result = self.run_preflight(
            CLUSTER_PREFLIGHT_BUDGET_SECONDS=str(SHORT_BUDGET_SECONDS),
            FAKE_CONFIG_HANG_SECONDS=str(HANG_PAST_SHORT_BUDGET_SECONDS),
        )
        elapsed = time.monotonic() - start
        self.assertEqual("4", result["check"], result)
        self.assertIn("kubectl itself failed", result["reason"])
        self.assertBudgetCapNamed(result["evidence"], SHORT_BUDGET_SECONDS)
        self.assertIn("CLUSTER_PREFLIGHT_BUDGET_SECONDS", result["evidence"])
        self.assertLess(elapsed, HANG_PAST_SHORT_BUDGET_SECONDS)

    def test_a_budget_override_that_is_not_a_positive_integer_keeps_the_default(self):
        # With the default budget the per-call cap is the smaller bound, so the
        # evidence names the cap, not the budget.
        result = self.run_preflight(CLUSTER_PREFLIGHT_BUDGET_SECONDS="soon", FAKE_CONFIG_TIMES_OUT="1")
        self.assertEqual("4", result["check"])
        self.assertPerCallCapNamed(result["evidence"])

    def test_check_5_timing_out_names_the_cap_it_timed_out_at(self):
        # Check 5 names its own --request-timeout, which opts it out of the
        # broker's command bound, so its evidence must not claim that bound.
        result = self.run_preflight(FAKE_CLUSTER_INFO_TIMES_OUT="1")
        self.assertEqual("5", result["check"])
        self.assertPerCallCapNamed(result["evidence"])
        self.assertIn("its own --request-timeout", result["evidence"])
        self.assertNotIn("command bound", result["evidence"])

    def test_a_non_utf8_byte_reads_as_the_shim_reads_it(self):
        # The shim decodes with replacement before parsing; a stray Latin-1 byte
        # in a comment must not fail check 3 while every proxied kubectl works.
        self.kubeconfig.write_bytes(
            b"apiVersion: v1\ncurrent-context: "
            + EXPECTED_CONTEXT.encode()
            + b"\n# caf\xe9\n"
        )
        self.assertEqual("ok", self.run_preflight()["status"])

    def test_check_3_runs_no_kubectl_against_the_pinned_file(self):
        # A passing run makes exactly the kubectl calls checks 4 and 5 own, in
        # order. The whole list is compared, not filtered for one spelling: a
        # check 3 back on the broker as `env KUBECONFIG=... kubectl config
        # current-context` (the form check 5 uses) logs the same argv as check
        # 4's plain call, and only the extra entry gives it away.
        log = Path(self._tmp.name) / "kubectl.log"
        self.assertEqual("ok", self.run_preflight(FAKE_KUBECTL_LOG=str(log))["status"])
        calls = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            ["config current-context", "cluster-info --request-timeout=8s"], calls
        )

    def test_reads_a_double_quoted_context(self):
        self.write_raw_kubeconfig(f'current-context: "{EXPECTED_CONTEXT}"\n')
        self.assertEqual("ok", self.run_preflight()["status"])

    def test_reads_a_single_quoted_context(self):
        self.write_raw_kubeconfig(f"current-context: '{EXPECTED_CONTEXT}'\n")
        self.assertEqual("ok", self.run_preflight()["status"])

    def test_reads_an_unquoted_context_with_a_comment_and_trailing_space(self):
        self.write_raw_kubeconfig(f"current-context: {EXPECTED_CONTEXT}   # pinned\n")
        self.assertEqual("ok", self.run_preflight()["status"])

    def test_reads_a_json_kubeconfig(self):
        # kubectl accepts JSON kubeconfigs, and JSON is YAML.
        self.kubeconfig.write_text(
            json.dumps({"apiVersion": "v1", "kind": "Config", "current-context": EXPECTED_CONTEXT}),
            encoding="utf-8",
        )
        self.assertEqual("ok", self.run_preflight()["status"])

    def test_ignores_a_current_context_nested_below_the_top_level(self):
        # A line-oriented reader would take this one; kubectl would not.
        self.write_raw_kubeconfig(
            f"contexts:\n- name: x\n  context:\n    current-context: {EXPECTED_CONTEXT}\n"
        )
        result = self.run_preflight()
        self.assertEqual("3", result["check"])
        self.assertIn("does not select a cluster", result["reason"])

    def test_a_quoted_context_for_another_cluster_still_fails(self):
        self.write_raw_kubeconfig(
            f'current-context: "gke_{PROJECT}_{LOCATION}_someone-elses-cluster"\n'
        )
        result = self.run_preflight()
        self.assertEqual("3", result["check"])
        self.assertIn("different cluster", result["reason"])
        self.assertIn("someone-elses-cluster", result["evidence"])

    def test_an_empty_quoted_context_selects_no_cluster(self):
        self.write_raw_kubeconfig('current-context: ""\n')
        result = self.run_preflight()
        self.assertEqual("3", result["check"])
        self.assertIn("does not select a cluster", result["reason"])

    def test_a_null_context_selects_no_cluster(self):
        self.write_raw_kubeconfig("current-context:\n")
        result = self.run_preflight()
        self.assertEqual("3", result["check"])
        self.assertIn("does not select a cluster", result["reason"])

    def test_an_unreadable_kubeconfig_reports_check_3(self):
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-000 file")
        self.kubeconfig.chmod(0)
        self.addCleanup(self.kubeconfig.chmod, 0o600)
        result = self.run_preflight()
        self.assertEqual("3", result["check"])
        self.assertIn("cannot read the pinned kubeconfig", result["reason"])
        self.assertIn("Permission denied", result["evidence"])
        self.assertNotIn("credential proxy", result["remediation"])

    def test_a_kubeconfig_that_is_not_yaml_reports_check_3(self):
        self.write_raw_kubeconfig("current-context: [unclosed\n")
        result = self.run_preflight()
        self.assertEqual("3", result["check"])
        self.assertIn("not a valid kubeconfig", result["reason"])
        self.assertIn("Re-scaffold", result["remediation"])

    def test_a_scalar_pyyaml_cannot_construct_reports_check_3_as_invalid(self):
        # PyYAML's safe constructors raise plain Python exceptions, not
        # yaml.YAMLError, for a scalar they cannot build: an impossible date
        # anywhere in the file (ValueError), or an explicit tag on a value that
        # does not fit it (ValueError, KeyError, IndexError, AttributeError).
        for tail in (
            "x: 2001-13-45\n",
            "x: !!int abc\n",
            "x: !!bool abc\n",
            'x: !!int ""\n',
            "x: !!timestamp abc\n",
        ):
            with self.subTest(tail=tail):
                self.write_raw_kubeconfig(f"current-context: {EXPECTED_CONTEXT}\n{tail}")
                result = self.run_preflight()
                self.assertEqual("3", result["check"])
                self.assertIn("not a valid kubeconfig", result["reason"])
                self.assertIn("not parseable as YAML", result["evidence"])

    def test_a_kubeconfig_that_is_not_a_mapping_reports_check_3(self):
        self.kubeconfig.write_text("- just\n- a list\n", encoding="utf-8")
        result = self.run_preflight()
        self.assertEqual("3", result["check"])
        self.assertIn("not a valid kubeconfig", result["reason"])

    def test_a_non_string_context_reports_check_3(self):
        self.write_raw_kubeconfig("current-context: [a, b]\n")
        result = self.run_preflight()
        self.assertEqual("3", result["check"])
        self.assertIn("not a valid kubeconfig", result["reason"])

    def test_an_oversized_kubeconfig_reports_check_3(self):
        self.write_raw_kubeconfig(
            f"current-context: {EXPECTED_CONTEXT}\n# {'x' * KUBECONFIG_OVER_CAP_BYTES}\n"
        )
        result = self.run_preflight()
        self.assertEqual("3", result["check"])
        self.assertIn("larger than", result["evidence"])

    def test_a_missing_pyyaml_reports_a_broken_image_not_a_bad_pin(self):
        # `-S` drops site-packages, which is where PyYAML lives.
        real = shutil.which("python3")
        wrapper = self.bin / "python3"
        wrapper.write_text(f'#!/bin/bash\nexec "{real}" -S "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        result = self.run_preflight()
        self.assertEqual("3", result["check"])
        self.assertIn("PyYAML is not available", result["reason"])
        self.assertIn("broken image", result["remediation"])

    def test_a_missing_python3_reports_a_broken_image_not_a_bad_pin(self):
        wrapper = self.bin / "python3"
        wrapper.write_text("#!/bin/bash\nexit 127\n", encoding="utf-8")
        wrapper.chmod(0o755)
        result = self.run_preflight()
        self.assertEqual("3", result["check"])
        self.assertIn("PyYAML is not available", result["reason"])

    def test_a_vanished_kubeconfig_is_caught_by_check_2_before_check_3(self):
        self.kubeconfig.unlink()
        result = self.run_preflight()
        self.assertEqual("2", result["check"])
        self.assertIn("not pinned", result["reason"])

    # ---- The check number is reported ----------------------------------------

    # The inventory-audit SOP routes on which check failed: 1-4 mean the agent never
    # established which cluster it is and must audit nothing, while 5 means it is
    # identified and merely cut off. The remediation text cannot carry that
    # distinction — a missing USER.md and a missing kubeconfig share one.

    def test_a_missing_identity_reports_check_1(self):
        self.user_md.unlink()
        self.assertEqual("1", self.run_preflight()["check"])

    def test_a_missing_kubeconfig_reports_check_2(self):
        self.kubeconfig.unlink()
        self.assertEqual("2", self.run_preflight()["check"])

    def test_an_unreachable_cluster_reports_check_5(self):
        self.assertEqual("5", self.run_preflight(FAKE_UNREACHABLE="1")["check"])

    def test_a_passing_run_names_no_check(self):
        self.assertEqual("", self.run_preflight()["check"])

    # ---- The pre-existing checks still work ----------------------------------

    def test_fails_when_user_md_is_missing(self):
        self.user_md.unlink()
        result = self.run_preflight()
        self.assertEqual("failed", result["status"])
        self.assertIn("no identity file", result["reason"])

    def test_fails_when_user_md_omits_the_location(self):
        # Previously only `cluster:` was required, so a partial identity passed and
        # left nothing to compare the kubeconfig against.
        self.user_md.write_text(
            USER_MD.replace(f"- location: {LOCATION}\n", ""), encoding="utf-8"
        )
        result = self.run_preflight()
        self.assertEqual("failed", result["status"])
        self.assertIn("incomplete", result["reason"])
        self.assertIn("location", result["evidence"])

    def test_fails_when_the_kubeconfig_is_missing(self):
        self.kubeconfig.unlink()
        result = self.run_preflight()
        self.assertEqual("failed", result["status"])
        self.assertIn("not pinned", result["reason"])

    def test_fails_when_the_kubeconfig_is_empty(self):
        self.kubeconfig.write_text("", encoding="utf-8")
        self.assertEqual("failed", self.run_preflight()["status"])

    def test_fails_when_the_cluster_is_unreachable(self):
        result = self.run_preflight(FAKE_UNREACHABLE="1")
        self.assertEqual("failed", result["status"])
        self.assertIn("Cannot reach", result["reason"])
        self.assertIn("i/o timeout", result["evidence"])

    def test_fails_when_kubectl_is_absent(self):
        (self.bin / "kubectl").unlink()
        minimal = f"{self.bin}:/usr/bin:/bin"
        if shutil.which("kubectl", path=minimal):
            self.skipTest("a real kubectl is installed on the minimal PATH")
        result = self.run_preflight(PATH=minimal)
        self.assertEqual("failed", result["status"])
        self.assertIn("kubectl is not available", result["reason"])

    # ---- Output contract -----------------------------------------------------

    def test_human_output_names_the_mismatch(self):
        self.write_kubeconfig(f"gke_{PROJECT}_{LOCATION}_someone-elses-cluster")
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True, text=True, timeout=60,
            env={"PATH": f"{self.bin}:{os.environ.get('PATH', '')}", "HERMES_HOME": str(self.home)},
        )
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("PREFLIGHT: FAILED", proc.stdout)
        self.assertIn("someone-elses-cluster", proc.stdout)


if __name__ == "__main__":
    unittest.main()

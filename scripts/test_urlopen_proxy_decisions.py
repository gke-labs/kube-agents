#!/usr/bin/env python3
"""The recorded proxy decision for each urllib call of the audited runtime files.

Run: cd scripts && python3 -m unittest test_urlopen_proxy_decisions

`urllib.request.urlopen`, and an opener from `urllib.request.build_opener` that has
no `urllib.request.ProxyHandler({})`, take a proxy from the environment
(`http_proxy`, `HTTP_PROXY`, `https_proxy`, `HTTPS_PROXY`). urllib does not leave
out a loopback or an in-cluster host by itself. So a call to the pod's own
Session KV, to the credential broker Service or to the Kubernetes API server
goes to that proxy when a pod has a proxy variable and no `no_proxy` entry for
the host. For an `http://` URL the proxy gets the whole request, a bearer token
included. For an `https://` URL it gets only the host, and the call fails.

For a public host a proxy from the environment is correct: an install behind an
egress proxy needs it.

`DECISIONS` holds one row for each such call in the audited files: the decision
(`refuse` the proxy, or `keep` the default) with its reason, and the state of the
source today (`takes` the proxy, or `refuses` it). A row with the decision
`refuse` and the state `takes` is a call that still has to change.

`agents/platform/scripts/test_broker_openers_refuse_proxy.py` reads the rows too.
For each row of a platform script with a module-level `build_opener` and the
state `refuses`, it loads the module with a proxy variable set and reads the
opener that the import made.

The tests fail when:

- a non-test Python file of the shipped trees (`SHIPPED_TREES`) has a `urlopen`
  or a `build_opener` call and is not in `AUDITED_FILES`;
- an audited file gets a new `urlopen` or `build_opener` call, loses one, or
  moves one to another function, and `DECISIONS` does not say so;
- the source of a call no longer agrees with the row's state (an opener loses
  its `ProxyHandler({})`, or a fixed call still has the state `takes`);
- a row gives no reason, or keeps the default for a call that refuses it.

To change a call: change the source, then change its row (the scope when the
call moved, `urlopen` to `build_opener` when it became an opener, and the state).
A file stays in `AUDITED_FILES` when its last call is gone.

Not seen: which opener a request goes through. The reader finds where an opener
is built and where `urlopen` is called, as `urllib.request.urlopen(...)` or as a
bare `urlopen(...)`; a request that changes from one opener of a file to another
is the module's own tests' to hold. It does not read `http.client`, which takes
no proxy from the environment, or a third-party client.
"""

import ast
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent

REFUSE = "refuse"
KEEP = "keep"
TAKES = "takes"
REFUSES = "refuses"

URLOPEN = "urlopen"
BUILD_OPENER = "build_opener"
PROXY_HANDLER = "ProxyHandler"
MODULE_SCOPE = "<module>"

SCRIPTS = "agents/platform/scripts/"
PATCHES = "deploy/docker/patches/"

SESSION_KV = "Session KV on the pod (127.0.0.1)"
GATEWAY_API = "the gateway API on the pod (127.0.0.1 by default)"
BROKER = "the credential broker"
API_SERVER = "the Kubernetes API server"
METADATA = "the metadata server"
HINDSIGHT = "the Hindsight Service"

LOOPBACK_BEARER = "a loopback http call with a bearer token; the suggested NO_PROXY does not cover 127.0.0.1"
LOOPBACK_PROMPT = (
    "a loopback http call with the prompt; a proxy cannot reach the pod's loopback, so the turn fails; "
    "the suggested NO_PROXY does not cover 127.0.0.1"
)
BROKER_BEARER = "an http call inside the cluster, or on the pod, with the projected ServiceAccount token"
API_SERVER_BEARER = "an in-cluster https call with a ServiceAccount token; a proxy must not be in its path"
PUBLIC_HOST = "a public host; an install behind an egress proxy needs the proxy"


class Decision(NamedTuple):
    path: str
    scope: str
    call: str
    state: str
    decision: str
    target: str
    reason: str


DECISIONS = (
    # The Pub/Sub platform plugin.
    Decision("agentplugins/pubsub-platform/files/platforms/pubsub/adapter.py", "<module>._GATEWAY_API_OPENER", BUILD_OPENER, REFUSES, REFUSE, GATEWAY_API, LOOPBACK_BEARER),
    Decision("agentplugins/pubsub-platform/files/platforms/pubsub/adapter.py", "PubsubRelay.__init__", BUILD_OPENER, REFUSES, REFUSE, BROKER, BROKER_BEARER),
    # The chat profile.
    Decision("agents/chat/defaults/plugins/chat_metrics/metrics.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision("agents/chat/scripts/bootstrap_delivery.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision("agents/chat/scripts/memory_file_import.py", "<module>._HINDSIGHT_OPENER", BUILD_OPENER, REFUSES, REFUSE, HINDSIGHT, "an in-cluster http call that carries memory text"),
    Decision("agents/chat/scripts/memory_ttl_curator.py", "<module>._HINDSIGHT_OPENER", BUILD_OPENER, REFUSES, REFUSE, HINDSIGHT, "an in-cluster http call that carries memory text"),
    # The platform profile's plugins.
    Decision("agents/platform/plugins/incident_context/__init__.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision(
        "agents/platform/plugins/model_identity/__init__.py", "<module>._MODEL_INFO_OPENER", BUILD_OPENER, REFUSES, REFUSE,
        "the LiteLLM inference gateway", "an in-cluster http call that may carry the inference gateway bearer key",
    ),
    Decision("agents/platform/plugins/pr_opened/__init__.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    # The platform profile's scripts.
    Decision(SCRIPTS + "agent_common_server.py", "<module>._AGENT_OPENER", BUILD_OPENER, REFUSES, REFUSE, "the platform agent Service", "an in-cluster https call with the prompt; a proxy must not be in its path"),
    Decision(SCRIPTS + "audit_report_store.py", "_http_get_json", BUILD_OPENER, REFUSES, REFUSE, API_SERVER, API_SERVER_BEARER),
    Decision(SCRIPTS + "broker_admission.py", "<module>._OPENER", BUILD_OPENER, REFUSES, REFUSE, BROKER, BROKER_BEARER),
    Decision(SCRIPTS + "credential_proxy.py", "<module>._METADATA_OPENER", BUILD_OPENER, REFUSES, REFUSE, METADATA, "a link-local address must never go through a proxy"),
    Decision(SCRIPTS + "credential_proxy.py", "ServiceAccountAuthenticator._review", BUILD_OPENER, REFUSES, REFUSE, API_SERVER, API_SERVER_BEARER),
    Decision(SCRIPTS + "credential_proxy.py", "PodAddressBinding._read_pod", BUILD_OPENER, REFUSES, REFUSE, API_SERVER, API_SERVER_BEARER),
    Decision(SCRIPTS + "credential_proxy.py", "SlackRelay.download", BUILD_OPENER, TAKES, KEEP, "slack.com", PUBLIC_HOST),
    Decision(SCRIPTS + "credential_proxy.py", "<module>._TEAMS_OPENER", BUILD_OPENER, TAKES, KEEP, "the Microsoft identity and Bot Framework hosts", PUBLIC_HOST),
    Decision(SCRIPTS + "credential_proxy_client.py", "<module>._BROKER_OPENER", BUILD_OPENER, REFUSES, REFUSE, BROKER, BROKER_BEARER),
    Decision(SCRIPTS + "dial_state.py", "<module>._BROKER_OPENER", BUILD_OPENER, REFUSES, REFUSE, BROKER, "an in-cluster http call with the broker token"),
    Decision(SCRIPTS + "feedback_broker.py", "_fetch", URLOPEN, TAKES, KEEP, "api.github.com, github.io and docs.google.com", PUBLIC_HOST),
    Decision(SCRIPTS + "findings_nudge.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision(SCRIPTS + "github_token_refresh.py", "<module>.metadata_open", BUILD_OPENER, REFUSES, REFUSE, METADATA, "a link-local address must never go through a proxy"),
    Decision(
        SCRIPTS + "github_token_refresh.py", "<module>._IN_CLUSTER_OPENER", BUILD_OPENER, REFUSES, REFUSE,
        "the credential broker, and a token minter inside the cluster",
        "an http call inside the cluster, or on the pod, with a ServiceAccount or identity token",
    ),
    Decision(
        SCRIPTS + "github_token_refresh.py", "request_minty_token", URLOPEN, TAKES, KEEP, "a token minter outside the cluster",
        "reached when the minter's host has no `.svc` or `.cluster.local` name and is no loopback address: a minter "
        "outside the cluster is behind the install's egress; the operator's minter goes through the opener above",
    ),
    Decision(SCRIPTS + "google_chat_relay_patch.py", "<module>._RELAY_OPENER", BUILD_OPENER, REFUSES, REFUSE, BROKER, BROKER_BEARER),
    Decision(SCRIPTS + "inventory_findings.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision(SCRIPTS + "platform_mcp_server.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision(
        SCRIPTS + "providers/transport.py", "HttpTransport._build_opener", BUILD_OPENER, TAKES, KEEP, "the forge's API host",
        "a GitHub, GitLab, Bitbucket or Gitea host; the site tells an operator to set HTTPS_PROXY for this client",
    ),
    Decision(SCRIPTS + "scope_publisher.py", "_kube_request", BUILD_OPENER, REFUSES, REFUSE, API_SERVER, API_SERVER_BEARER),
    Decision(SCRIPTS + "session_kv_server.py", "<module>._GATEWAY_OPENER", BUILD_OPENER, REFUSES, REFUSE, GATEWAY_API, LOOPBACK_PROMPT),
    Decision(SCRIPTS + "slack_blocks_post.py", "<module>._RELAY_OPENER", BUILD_OPENER, REFUSES, REFUSE, BROKER, BROKER_BEARER),
    Decision(SCRIPTS + "slack_relay_patch.py", "<module>._RELAY_OPENER", BUILD_OPENER, REFUSES, REFUSE, BROKER, BROKER_BEARER),
    Decision(SCRIPTS + "stall_watch.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision(SCRIPTS + "teams_relay_patch.py", "<module>._RELAY_OPENER", BUILD_OPENER, REFUSES, REFUSE, BROKER, BROKER_BEARER),
    Decision(SCRIPTS + "upgrade_readiness_review.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision(SCRIPTS + "wif_credentials.py", "_post", URLOPEN, TAKES, KEEP, "the Google STS and IAM Credentials hosts", PUBLIC_HOST),
    # The decision-memory skill.
    Decision(
        "agents/platform/skills/decision-memory/scripts/decision_memory.py", "<module>._HINDSIGHT_OPENER", BUILD_OPENER, REFUSES, REFUSE, HINDSIGHT,
        "an in-cluster or loopback http call that carries decision records",
    ),
    Decision(
        "agents/platform/skills/decision-memory/scripts/decision_memory.py", "_llm_synthesize_record", URLOPEN, TAKES, KEEP,
        "the model endpoint that the caller or the install names",
        "the host is the caller's choice and can be a public provider; the operator's value is an in-cluster Service "
        "that the suggested NO_PROXY covers",
    ),
    # The fleet-audit skill.
    Decision(
        "agents/platform/skills/fleet-audit/scripts/report_status.py", "_broker_opener", BUILD_OPENER, REFUSES, REFUSE, BROKER,
        "a call to the broker that carries two credentials of the caller",
    ),
    # The LiteLLM hook of the chart.
    Decision(
        "charts/kube-agents/files/litellm_oauth2_callback.py", "fetch_token", BUILD_OPENER, TAKES, KEEP,
        "the OAuth2 token endpoint that the operator names",
        "the host is the operator's choice: an identity provider outside the cluster needs the proxy, "
        "and a host inside the cluster needs a NO_PROXY entry",
    ),
    # The patches of the gateway image, and its chat plugin.
    Decision(PATCHES + "cron_job_template.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision(PATCHES + "eval_run_tag.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision(PATCHES + "kanban_notifier.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision(PATCHES + "kanban_origin.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision(PATCHES + "slack_ux_clicks.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    Decision("deploy/docker/plugins/chat/adapter.py", "<module>._SESSION_KV_OPENER", BUILD_OPENER, REFUSES, REFUSE, SESSION_KV, LOOPBACK_BEARER),
    # The operator's leader election probe.
    Decision(
        "k8s-operator/internal/controller/leader_elect.py", "<module>.HERMES_PROBE_OPENER", BUILD_OPENER, REFUSES, REFUSE,
        "the gateway API on the pod (127.0.0.1)",
        "a loopback probe; its answer must come from this pod's gateway",
    ),
)

# The files of the audit: each non-test Python file of `SHIPPED_TREES` that has a
# call. A file stays here when its last call is gone.
AUDITED_FILES = (
    "agentplugins/pubsub-platform/files/platforms/pubsub/adapter.py",
    "agents/chat/defaults/plugins/chat_metrics/metrics.py",
    "agents/chat/scripts/bootstrap_delivery.py",
    "agents/chat/scripts/memory_file_import.py",
    "agents/chat/scripts/memory_ttl_curator.py",
    "agents/platform/plugins/incident_context/__init__.py",
    "agents/platform/plugins/model_identity/__init__.py",
    "agents/platform/plugins/pr_opened/__init__.py",
    SCRIPTS + "agent_common_server.py",
    SCRIPTS + "audit_report_store.py",
    SCRIPTS + "broker_admission.py",
    SCRIPTS + "credential_proxy.py",
    SCRIPTS + "credential_proxy_client.py",
    SCRIPTS + "dial_state.py",
    SCRIPTS + "feedback_broker.py",
    SCRIPTS + "findings_nudge.py",
    SCRIPTS + "github_token_refresh.py",
    SCRIPTS + "google_chat_relay_patch.py",
    SCRIPTS + "inventory_findings.py",
    SCRIPTS + "platform_mcp_server.py",
    SCRIPTS + "providers/transport.py",
    SCRIPTS + "scope_publisher.py",
    SCRIPTS + "session_kv_server.py",
    SCRIPTS + "slack_blocks_post.py",
    SCRIPTS + "slack_relay_patch.py",
    SCRIPTS + "stall_watch.py",
    SCRIPTS + "teams_relay_patch.py",
    SCRIPTS + "upgrade_readiness_review.py",
    SCRIPTS + "wif_credentials.py",
    "agents/platform/skills/decision-memory/scripts/decision_memory.py",
    "agents/platform/skills/fleet-audit/scripts/report_status.py",
    "charts/kube-agents/files/litellm_oauth2_callback.py",
    PATCHES + "cron_job_template.py",
    PATCHES + "eval_run_tag.py",
    PATCHES + "kanban_notifier.py",
    PATCHES + "kanban_origin.py",
    PATCHES + "slack_ux_clicks.py",
    "deploy/docker/plugins/chat/adapter.py",
    "k8s-operator/internal/controller/leader_elect.py",
)
# The trees that an image or the chart ships. `bench/`, `hack/`, `scripts/`,
# `tests/` and `admin_console/` are developer and CI tools and have no rows.
SHIPPED_TREES = ("agentplugins", "agents", "charts", "deploy", "k8s-operator")
# A file with this name prefix is a test. A `verify_` file is not left out: some
# of them run when a pod starts.
TEST_FILE_PREFIX = "test_"
THIS_FILE = "scripts/test_urlopen_proxy_decisions.py"


def _refuses_the_proxy(call: ast.Call) -> bool:
    """Whether a `build_opener(...)` call has `ProxyHandler({})` among its arguments, by attribute or by bare name."""
    return any(
        isinstance(argument, ast.Call)
        and (
            (isinstance(argument.func, ast.Attribute) and argument.func.attr == PROXY_HANDLER)
            or (isinstance(argument.func, ast.Name) and argument.func.id == PROXY_HANDLER)
        )
        and len(argument.args) == 1
        and isinstance(argument.args[0], ast.Dict)
        and not argument.args[0].keys
        for argument in call.args
    )


def _called_name(call: ast.Call) -> str:
    """`urlopen` or `build_opener` when `call` is one of them, by attribute or by bare name; else an empty string."""
    name = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id if isinstance(call.func, ast.Name) else ""
    return name if name in (URLOPEN, BUILD_OPENER) else ""


def call_sites_of(source: str) -> list[tuple[str, str, str]]:
    """Each `urlopen(...)` and `build_opener(...)` call of `source`: its scope, the call's name and its state.

    The scope is the function and class path, or `<module>`. A call in a
    module-level assignment has the assigned name after it (`<module>._OPENER`),
    so two openers of one module are two different scopes.
    """
    found: list[tuple[str, str, str]] = []

    def walk(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = (*scope, child.name)
            elif not scope and isinstance(child, (ast.Assign, ast.AnnAssign)):
                target = child.targets[0] if isinstance(child, ast.Assign) else child.target
                if isinstance(target, ast.Name):
                    inner = (MODULE_SCOPE, target.id)
            if isinstance(child, ast.Call) and _called_name(child):
                name = _called_name(child)
                refuses = name == BUILD_OPENER and _refuses_the_proxy(child)
                found.append((".".join(scope) or MODULE_SCOPE, name, REFUSES if refuses else TAKES))
            walk(child, inner)

    walk(ast.parse(source), ())
    return found


def files_with_a_call(root: Path, trees: tuple[str, ...]) -> list[str]:
    """The non-test Python files under `trees` of `root` that have a `urlopen` or a `build_opener` call, as paths from `root`."""
    found = []
    for tree in trees:
        for path in sorted((root / tree).rglob("*.py")):
            relative = path.relative_to(root)
            if path.name.startswith(TEST_FILE_PREFIX) or any(part.startswith(".") for part in relative.parts):
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            # The text check keeps the parser away from the files that cannot have a call.
            if (URLOPEN in source or BUILD_OPENER in source) and call_sites_of(source):
                found.append(relative.as_posix())
    return found


def call_sites(path: str) -> list[tuple[str, str, str]]:
    """`call_sites_of` the file at `path`, a path from the repository root."""
    return call_sites_of((ROOT / path).read_text(encoding="utf-8"))


class UrlopenProxyDecisionsTest(unittest.TestCase):
    # A failure prints the whole difference, so the call or the row is named in full.
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.observed = {path: call_sites(path) for path in AUDITED_FILES if (ROOT / path).is_file()}

    def test_each_audited_file_is_in_the_tree(self) -> None:
        missing = [path for path in AUDITED_FILES if not (ROOT / path).is_file()]
        self.assertEqual(missing, [], f"a file of AUDITED_FILES is gone; move or remove it and its rows in {THIS_FILE}")

    def test_each_shipped_file_with_a_call_is_audited(self) -> None:
        strays = sorted(set(files_with_a_call(ROOT, SHIPPED_TREES)) - set(AUDITED_FILES))
        self.assertEqual(
            strays, [],
            f"a file of {SHIPPED_TREES} has a `urlopen` or a `build_opener` call and no decision; "
            f"add the file to AUDITED_FILES and a row to DECISIONS in {THIS_FILE}",
        )

    def test_each_row_is_for_an_audited_file(self) -> None:
        strays = sorted({row.path for row in DECISIONS} - set(AUDITED_FILES))
        self.assertEqual(strays, [], f"a row of DECISIONS names a file that AUDITED_FILES does not; add the file in {THIS_FILE}")
        self.assertEqual(len(AUDITED_FILES), len(set(AUDITED_FILES)), "a file is in AUDITED_FILES two times")

    def test_each_call_of_an_audited_file_has_one_row(self) -> None:
        for path, sites in self.observed.items():
            with self.subTest(path=path):
                recorded = Counter((row.scope, row.call) for row in DECISIONS if row.path == path)
                found = Counter((scope, call) for scope, call, _ in sites)
                self.assertEqual(
                    sorted((found - recorded).elements()), [],
                    f"{path} has a call with no row; give it a decision in DECISIONS of {THIS_FILE}",
                )
                self.assertEqual(
                    sorted((recorded - found).elements()), [],
                    f"DECISIONS of {THIS_FILE} has a row for {path} whose call is gone or moved; change or remove the row",
                )

    def test_the_state_of_each_row_is_the_state_of_the_source(self) -> None:
        for path, sites in self.observed.items():
            with self.subTest(path=path):
                recorded = Counter((row.scope, row.call, row.state) for row in DECISIONS if row.path == path)
                found = Counter(sites)
                self.assertEqual(
                    sorted((found - recorded).elements()), sorted((recorded - found).elements()),
                    f"{path}: the calls of the source (first) are not the rows of DECISIONS in {THIS_FILE} (second); "
                    "an opener refuses the proxy only with `ProxyHandler({})` among the arguments of `build_opener`",
                )

    def test_each_row_has_a_known_decision_a_target_and_a_reason(self) -> None:
        for row in DECISIONS:
            with self.subTest(path=row.path, scope=row.scope):
                self.assertIn(row.decision, (REFUSE, KEEP))
                self.assertIn(row.state, (TAKES, REFUSES))
                self.assertIn(row.call, (URLOPEN, BUILD_OPENER))
                self.assertTrue(row.target.strip() and row.reason.strip())
                self.assertFalse(row.call == URLOPEN and row.state == REFUSES, "a plain urlopen call takes the proxy")

    def test_a_call_that_keeps_the_default_does_not_refuse_the_proxy(self) -> None:
        wrong = [row for row in DECISIONS if row.decision == KEEP and row.state == REFUSES]
        self.assertEqual(wrong, [], "the decision of a call that refuses the proxy is `refuse`")

    def test_two_rows_of_one_scope_have_one_state_and_one_decision(self) -> None:
        # The reader cannot tell two calls of one scope from each other, so rows that
        # share a scope must say the same thing.
        seen: dict[tuple[str, str, str], tuple[str, str]] = {}
        for row in DECISIONS:
            key = (row.path, row.scope, row.call)
            self.assertEqual(seen.setdefault(key, (row.state, row.decision)), (row.state, row.decision), key)


class CallSitesTest(unittest.TestCase):
    """The reader of the source, on small inputs: what it counts and what it calls a refusal."""

    def test_an_opener_refuses_only_with_an_empty_proxy_handler(self) -> None:
        cases = (
            ("urllib.request.build_opener(urllib.request.ProxyHandler({}))", REFUSES),
            ("urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())", REFUSES),
            ("build_opener(ProxyHandler({}))", REFUSES),
            ("build_opener(ProxyHandler())", TAKES),
            ("urllib.request.build_opener(_NoRedirect())", TAKES),
            ("urllib.request.build_opener()", TAKES),
            ("urllib.request.build_opener(urllib.request.ProxyHandler())", TAKES),
            ("urllib.request.build_opener(urllib.request.ProxyHandler({'http': 'http://proxy:3128'}))", TAKES),
            ("urllib.request.build_opener(*handlers)", TAKES),
        )
        for source, state in cases:
            with self.subTest(source=source):
                self.assertEqual(call_sites_of(f"OPENER = {source}\n"), [("<module>.OPENER", BUILD_OPENER, state)])

    def test_a_module_level_opener_is_named_by_its_assignment(self) -> None:
        source = (
            "_FIRST = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
            "second = urllib.request.build_opener(_NoRedirect()).open\n"
            "urllib.request.build_opener()\n"
        )
        self.assertEqual(
            call_sites_of(source),
            [("<module>._FIRST", BUILD_OPENER, REFUSES), ("<module>.second", BUILD_OPENER, TAKES), (MODULE_SCOPE, BUILD_OPENER, TAKES)],
        )

    def test_a_bare_name_call_is_counted(self) -> None:
        source = "from urllib.request import build_opener, urlopen\ndef read():\n    return urlopen(request)\n_OPENER = build_opener()\n"
        self.assertEqual(call_sites_of(source), [("read", URLOPEN, TAKES), ("<module>._OPENER", BUILD_OPENER, TAKES)])

    def test_a_file_with_a_call_is_found_and_a_test_file_is_not(self) -> None:
        call = "import urllib.request\nurllib.request.urlopen('http://127.0.0.1')\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, source in (
                ("agents/scripts/client.py", call),
                ("agents/scripts/test_client.py", call),
                ("agents/scripts/verify_client.py", call),
                ("agents/scripts/.venv/client.py", call),
                ("agents/scripts/words.py", "NOTE = 'urlopen takes a proxy'\n"),
                ("bench/client.py", call),
            ):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(source, encoding="utf-8")
            self.assertEqual(
                files_with_a_call(root, ("agents",)), ["agents/scripts/client.py", "agents/scripts/verify_client.py"]
            )

    def test_an_opener_that_a_function_builds_for_each_call_is_named_by_the_function(self) -> None:
        # A client whose TLS context is made for each call cannot keep one module-level opener.
        source = (
            "import urllib.request\n"
            "def read(request, context):\n"
            "    opener = urllib.request.build_opener(\n"
            "        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)\n"
            "    )\n"
            "    return opener.open(request)\n"
            "def read_through_the_proxy(request, context):\n"
            "    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context)).open(request)\n"
        )
        self.assertEqual(
            call_sites_of(source),
            [("read", BUILD_OPENER, REFUSES), ("read_through_the_proxy", BUILD_OPENER, TAKES)],
        )

    def test_a_call_is_named_by_its_function_and_class(self) -> None:
        source = (
            "import urllib.request\n"
            "class Client:\n"
            "    def call(self):\n"
            "        def inner():\n"
            "            return urllib.request.urlopen(request)\n"
            "        return inner()\n"
            "def read():\n"
            "    with urllib.request.urlopen(request) as response:\n"
            "        return response.read()\n"
        )
        self.assertEqual(
            call_sites_of(source),
            [("Client.call.inner", URLOPEN, TAKES), ("read", URLOPEN, TAKES)],
        )


if __name__ == "__main__":
    unittest.main()

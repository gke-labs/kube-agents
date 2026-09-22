#!/usr/bin/env python3
"""Tests for stall_watch.py: the fleet sweep is faked at the sandbox hop and
the board at the kanban command, with a real sqlite file for the
subscription rows."""

import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stall_watch  # noqa: E402

PROJECT = "proj"
LOCATION = "us-central1"
HOME_CHANNEL = "spaces/TESTSPACE"
GATEWAY_SECRET = "Secret storefront/storefront-tls not found."


def completed(argv, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def finding(namespace, obj, heuristic, detail, stalled_for="20m", stalled_seconds=1200):
    # The shape stall_report.py emits per row.
    return {
        "object": obj,
        "namespace": namespace,
        "heuristic": heuristic,
        "detail": detail,
        "stalled_for": stalled_for,
        "stalled_seconds": stalled_seconds,
    }


GATEWAY_CONDITION_ROW = finding(
    "storefront", "Gateway/storefront-gateway", "stale-condition", "listeners[https] ResolvedRefs=False InvalidCertificateRef"
)
GATEWAY_SYNC_ROW = finding(
    "storefront",
    "Gateway/storefront-gateway",
    "repeating-warnings",
    f'SYNC x12: failed to translate Gateway "storefront/storefront-gateway": Error GWCER102: {GATEWAY_SECRET}',
    stalled_for="18m",
    stalled_seconds=1080,
)
GATEWAY_ROWS = [GATEWAY_CONDITION_ROW, GATEWAY_SYNC_ROW]
DEPLOYMENT_ROW = finding(
    "checkout",
    "Deployment/checkout-api",
    "dangling-reference",
    "template.spec.containers[0].envFrom[0].configMapRef -> ConfigMap/checkout-feature-flags not found",
)
#: A row that clears on the first scan without it, for tests about other things.
DEADLINE_ROW = finding("checkout", "Deployment/checkout-api", "stale-condition", "Progressing=False ProgressDeadlineExceeded")
TIMEOUT = subprocess.TimeoutExpired("gcloud", stall_watch.GET_CREDENTIALS_TIMEOUT_SECONDS)
#: What `kubectl api-resources -o name` prints for the default kinds on a cluster that serves them all.
SERVED_DEFAULT = ["deployments.apps", "statefulsets.apps", "daemonsets.apps", "jobs.batch", "gateways.gateway.networking.k8s.io", "httproutes.gateway.networking.k8s.io", "certificates.cert-manager.io", "pods", "configmaps"]
NOT_SCANNED = "warning: deployments in checkout not scanned; its objects are missing from the count: kubectl exited 1\n"

BOARD_SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);
CREATE TABLE kanban_notify_subs (
    task_id TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id TEXT NOT NULL DEFAULT '',
    user_id TEXT, user_id_alt TEXT, chat_type TEXT, notifier_profile TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'notify', delivery_metadata TEXT, created_at INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (task_id, platform, chat_id, thread_id));
"""


class Unlisted:
    """A cluster the project lists with a status that is not swept."""

    def __init__(self, status):
        self.status = status


class Located:
    """A cluster in a location other than the default, with its namespaces."""

    def __init__(self, location, namespaces, status="RUNNING"):
        self.location = location
        self.namespaces = namespaces
        self.status = status


class FakeFleet:
    """Answers the sandbox hops for a fleet described as
    {cluster: {namespace: [findings]}}. A namespace mapped to an exception or
    an exit code cannot be read, one mapped to a string returns that text, one
    mapped to (rows, stderr) returns both; a cluster mapped to an exception
    cannot be reached; Unlisted(status) is listed but not swept; a key
    `name@location` or a Located value puts it somewhere else."""

    def __init__(self, fleet, namespaces_extra=(), listing_stderr="", hidden=(), served=None, sandbox_dies_at=None, api_resources_rc_one=False):
        self.api_resources_rc_one = api_resources_rc_one
        self.listing_stderr = listing_stderr
        self.hidden = set(hidden)
        self.served = served
        self.sandbox_dies_at = sandbox_dies_at
        self.fleet = {}
        for key, spec in fleet.items():
            name, _, location = key.partition(stall_watch.CLUSTER_ID_SEPARATOR)
            if isinstance(spec, Located):
                location, status, namespaces = spec.location, spec.status, spec.namespaces
            else:
                status = spec.status if isinstance(spec, Unlisted) else "RUNNING"
                namespaces = spec
            self.fleet[(name, location or LOCATION)] = (status, namespaces)
        self.namespaces_extra = list(namespaces_extra)
        self.calls = []

    def __call__(self, argv, *, timeout, kubeconfig=None, stdin=None):
        self.calls.append((argv, kubeconfig, stdin))
        if argv[:4] == ["gcloud", "container", "clusters", "list"]:
            body = [{"name": n, "location": l, "status": status} for (n, l), (status, _) in self.fleet.items() if n not in self.hidden]
            return completed(argv, json.dumps(body), stderr=self.listing_stderr)
        if argv[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
            name = argv[4]
            location = argv[5].split("=", 1)[1]
            _, namespaces = self.fleet[(name, location)]
            if isinstance(namespaces, Exception):
                raise namespaces
            return completed(argv)
        _, namespaces = self.fleet[self._cluster_from(kubeconfig)]
        if argv[:3] == ["kubectl", "get", "namespaces"]:
            names = list(namespaces) + self.namespaces_extra
            return completed(argv, "".join(f"namespace/{n}\n" for n in names))
        if argv[:2] == ["kubectl", "api-resources"]:
            if isinstance(self.served, Exception):
                raise self.served
            served = self.served if self.served is not None else SERVED_DEFAULT
            rc = 1 if self.api_resources_rc_one else 0
            return completed(argv, "".join(f"{n}\n" for n in served), returncode=rc, stderr="error: unable to retrieve the complete list of server APIs: metrics.k8s.io/v1beta1" if rc else "")
        if argv[:3] == [stall_watch.PYTHON_EXECUTABLE, stall_watch.PYTHON_ISOLATED_FLAG, stall_watch.STDIN_SCRIPT_ARG]:
            namespace = argv[argv.index("--namespace") + 1]
            if self.sandbox_dies_at == (self._cluster_from(kubeconfig)[0], namespace):
                raise stall_watch.sandbox_exec.SandboxUnavailable("ssh: connect to host sandbox port 22: Connection refused")
            result = namespaces[namespace]
            if isinstance(result, Exception):
                raise result
            if isinstance(result, int):
                return completed(argv, "", returncode=result, stderr="cannot list anything")
            if isinstance(result, str):
                return completed(argv, result)
            stderr = ""
            if isinstance(result, tuple):
                result, stderr = result
            return completed(argv, json.dumps({"namespace": namespace, "stalled_resources": len(result), "findings": result}), stderr=stderr)
        raise AssertionError(f"unexpected sandbox call {argv}")

    def scanned(self):
        return [argv[argv.index("--namespace") + 1] for argv, _, _ in self.calls if argv[:1] == [stall_watch.PYTHON_EXECUTABLE]]

    @staticmethod
    def _cluster_from(kubeconfig):
        slug = Path(kubeconfig).name[len(stall_watch.KUBECONFIG_FILE_PREFIX):-len(stall_watch.KUBECONFIG_FILE_SUFFIX)]
        _, name, location = slug.split(stall_watch.KUBECONFIG_SLUG_SEPARATOR)
        return name, location


class FakeBoard:
    """Answers the kanban commands and mirrors each card into the sqlite board
    the subscription writer reads."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.calls = []
        self.cards = {}
        self.by_key = {}
        self.filed = 0
        self.fail_next_create = False
        self.fail_show = False
        self.fail_complete = False
        self.fail_comment_once = False

    def forget(self, tid):
        """The card leaves the board: an operator deleted it or the volume was restored."""
        self.cards.pop(tid)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
        conn.commit()
        conn.close()

    def __call__(self, command):
        self.calls.append(command)
        argv = shlex.split(command)
        if argv[0] == "create":
            if self.fail_next_create:
                self.fail_next_create = False
                raise RuntimeError("board locked")
            opts = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1) if argv[i].startswith("--") and argv[i] != "--json"}
            key = opts.get("--idempotency-key")
            if key in self.by_key and self.by_key[key] in self.cards:
                existing = self.by_key[key]
                return json.dumps({"id": existing, "status": self.cards[existing]["status"]})
            self.filed += 1
            tid = f"t_{self.filed:08x}"
            self.by_key[key] = tid
            self.cards[tid] = {"status": "ready", "assignee": opts.get("--assignee"), "title": argv[-1], "body": opts.get("--body", ""), "key": opts.get("--idempotency-key"), "comments": []}
            conn = sqlite3.connect(self.db_path)
            conn.execute("INSERT INTO tasks (id, title, body, assignee, status, created_at) VALUES (?, ?, ?, ?, 'ready', 1)", (tid, argv[-1], opts.get("--body", ""), opts.get("--assignee")))
            conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'created', 1)", (tid,))
            conn.commit()
            conn.close()
            return json.dumps({"id": tid, "status": "ready"})
        if argv[0] == "show":
            tid = argv[-1]
            if self.fail_show or tid not in self.cards:
                raise RuntimeError("board locked")
            return json.dumps({"task": {"id": tid, "status": self.cards[tid]["status"]}})
        if argv[0] == "comment":
            if self.fail_comment_once:
                self.fail_comment_once = False
                raise RuntimeError("database is locked")
            self.cards[argv[1]]["comments"].append(argv[2])
            return "ok"
        if argv[0] == "complete":
            tid = argv[-1]
            if self.fail_complete:
                raise RuntimeError("claim fenced")
            self.cards[tid]["status"] = "done"
            self.cards[tid]["result"] = argv[argv.index("--result") + 1]
            return "ok"
        raise AssertionError(f"unexpected kanban command {command}")

    def opened(self):
        return [c for c in self.calls if c.startswith("create ")]


def label(name, location=LOCATION):
    return f"`{name}` ({location})"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.state = self.home / "stall_watch.json"
        self.db = self.home / stall_watch.BOARD_DB_NAME
        conn = sqlite3.connect(self.db)
        conn.executescript(BOARD_SCHEMA)
        conn.close()
        self.write_config({"platforms": {"google_chat": {"home_channel": {"platform": "google_chat", "chat_id": HOME_CHANNEL, "name": "Home"}}}})
        # A scheduled tick's environment: Hermes' build_subprocess_env strips
        # every *_HOME_CHANNEL, so none is set here unless a test says so.
        env = {stall_watch.PROJECT_ENVS[0]: PROJECT, "PLATFORM_AGENT_HOME": self.tmp.name}
        for var in (stall_watch.KINDS_ENV, stall_watch.REPORT_SCRIPT_ENV, stall_watch.STATE_PATH_ENV, "GOOGLE_CHAT_HOME_CHANNEL", "GOOGLE_CHAT_HOME_CHANNEL_THREAD_ID", "SLACK_HOME_CHANNEL", "SLACK_HOME_CHANNEL_THREAD_ID", *stall_watch.PROJECT_ENVS[1:]):
            env[var] = ""
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        p = patch.object(stall_watch.sandbox_exec, "sandbox_enabled", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(stall_watch, "dns_endpoint_args", return_value=[])
        p.start()
        self.addCleanup(p.stop)
        self.board = FakeBoard(str(self.db))
        p = patch.object(stall_watch, "kanban", self.board)
        p.start()
        self.addCleanup(p.stop)

    def write_config(self, config):
        import yaml

        (self.home / stall_watch.CONFIG_FILE_NAME).write_text(yaml.safe_dump(config))

    def run_tick(self, fleet, **kw):
        fake = FakeFleet(fleet, **kw)
        with patch.object(stall_watch, "run_sandbox", fake):
            lines = stall_watch.tick(self.state, dry_run=False)
        return lines, fake

    def ledger(self):
        return json.loads(self.state.read_text())

    def subs(self, task_id):
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT platform, chat_id, thread_id, notifier_profile, delivery_mode, last_event_id FROM kanban_notify_subs WHERE task_id = ?", (task_id,)).fetchall()
        conn.close()
        return rows

    def noticed(self, lines):
        return [l for l in lines if l.startswith(stall_watch.NOTICED_PREFIX)]

    def cleared(self, lines):
        return [l for l in lines if l.startswith(stall_watch.CLEARED_PREFIX)]


class Cards(Base):
    def test_first_sighting_opens_one_card_with_the_rows_and_the_instruction(self):
        lines, _ = self.run_tick({"support-eval-cluster": {"storefront": GATEWAY_ROWS, "catalog": []}})
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(self.board.opened()), 1)
        tid, card = next(iter(self.board.cards.items()))
        self.assertEqual(card["assignee"], stall_watch.FALLBACK_ASSIGNEE, "no Cluster Agent profile is scaffolded in this test home")
        self.assertIn("Stalled controllers in storefront on support-eval-cluster: Gateway/storefront-gateway", card["title"])
        self.assertIn(stall_watch.PLATFORM_REPORT_SCRIPT, card["body"], "no profile, so the platform worker runs the script itself")
        self.assertIn(GATEWAY_SECRET, card["body"])
        self.assertIn("not instructions", card["body"])
        self.assertIn("`storefront`", card["body"])
        self.assertTrue(card["key"].startswith(f"{stall_watch.CARD_IDEMPOTENCY_PREFIX}-support-eval-cluster@{LOCATION}-storefront-"))
        self.assertEqual(lines[0], f"{stall_watch.NOTICED_PREFIX} in {label('support-eval-cluster')} / `storefront`: Gateway/storefront-gateway; card `{tid}` opened for `platform`")

    def test_the_card_gets_a_home_channel_subscription_seeded_at_its_event_head(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS}})
        tid = next(iter(self.board.cards))
        self.assertEqual(self.subs(tid), [("google_chat", HOME_CHANNEL, "", stall_watch.NOTIFIER_PROFILE, stall_watch.DELIVERY_MODE, 1)])
        self.assertEqual(self.ledger()[stall_watch.EPISODES_KEY][f"c@{LOCATION}/storefront"]["card"], tid)

    def test_the_assignee_is_the_clusters_profile_when_it_exists(self):
        from cluster_agent_profile import profile_name

        name = profile_name(PROJECT, "c", LOCATION)
        (self.home / stall_watch.PROFILES_DIR / name).mkdir(parents=True)
        lines, _ = self.run_tick({"c": {"storefront": GATEWAY_ROWS}})
        card = next(iter(self.board.cards.values()))
        self.assertEqual(card["assignee"], name)
        self.assertIn(f"`{stall_watch.SKILL_NAME}` skill", card["body"])
        self.assertNotIn(stall_watch.PLATFORM_REPORT_SCRIPT, card["body"])
        self.assertIn(f"opened for `{name}`", lines[0])

    def test_unchanged_stall_opens_nothing_and_prints_nothing(self):
        fleet = {"c": {"storefront": GATEWAY_ROWS}}
        self.run_tick(fleet)
        lines, _ = self.run_tick(fleet)
        self.assertEqual(lines, [])
        self.assertEqual(len(self.board.opened()), 1)

    def test_a_new_object_in_a_namespace_with_an_open_card_is_a_comment(self):
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        other = finding("checkout", "Deployment/cart-api", "generation-lag", "generation 2 observed 1")
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW, other]}})
        self.assertEqual(lines, [])
        self.assertEqual(len(self.board.opened()), 1)
        card = next(iter(self.board.cards.values()))
        self.assertEqual(len(card["comments"]), 1)
        self.assertIn("Deployment/cart-api", card["comments"][0])
        self.assertIn("Deployment/cart-api", self.ledger()[stall_watch.EPISODES_KEY][f"c@{LOCATION}/checkout"]["objects"])

    def test_a_new_object_after_the_agent_completed_the_card_opens_a_new_card(self):
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        first = next(iter(self.board.cards))
        self.board.cards[first]["status"] = "done"
        other = finding("checkout", "Deployment/cart-api", "generation-lag", "generation 2 observed 1")
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW, other]}})
        self.assertEqual(len(self.noticed(lines)), 1)
        self.assertEqual(len(self.board.opened()), 2)

    def test_a_cleared_namespace_comments_completes_and_prints_once(self):
        self.run_tick({"c": {"checkout": [DEADLINE_ROW]}})
        tid = next(iter(self.board.cards))
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertEqual(len(self.cleared(lines)), 1)
        self.assertIn(f"card `{tid}` closed", lines[0])
        card = self.board.cards[tid]
        self.assertEqual(card["status"], "done")
        self.assertIn("cleared", card["result"])
        self.assertEqual(len(card["comments"]), 1)
        self.assertNotIn(f"c@{LOCATION}/checkout", self.ledger()[stall_watch.EPISODES_KEY])
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertEqual(lines, [])

    def test_a_partial_clear_keeps_the_card_open(self):
        rows = [DEADLINE_ROW, finding("checkout", "Deployment/cart-api", "stale-condition", "Available=False MinimumReplicasUnavailable")]
        self.run_tick({"c": {"checkout": rows}})
        lines, _ = self.run_tick({"c": {"checkout": [DEADLINE_ROW]}})
        self.assertEqual(lines, [])
        self.assertEqual(next(iter(self.board.cards.values()))["status"], "ready")

    def test_a_failed_complete_keeps_the_episode_and_retries_next_tick(self):
        self.run_tick({"c": {"checkout": [DEADLINE_ROW]}})
        tid = next(iter(self.board.cards))
        self.board.fail_complete = True
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertEqual(lines, [], "nothing is said to have closed")
        self.assertIn(f"c@{LOCATION}/checkout", self.ledger()[stall_watch.EPISODES_KEY])
        self.assertEqual(len(self.board.cards[tid]["comments"]), 1)
        self.board.fail_complete = False
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertEqual(len(self.cleared(lines)), 1)
        self.assertEqual(self.board.cards[tid]["status"], "done")
        self.assertEqual(len(self.board.cards[tid]["comments"]), 1, "the clearing comment is not repeated")

    def test_a_card_the_worker_is_running_is_not_completed_under_it(self):
        self.run_tick({"c": {"checkout": [DEADLINE_ROW]}})
        tid = next(iter(self.board.cards))
        self.board.cards[tid]["status"] = "running"
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertEqual(lines, [])
        self.assertEqual(self.board.cards[tid]["status"], "running")
        self.assertEqual(len(self.board.cards[tid]["comments"]), 1)
        self.board.cards[tid]["status"] = "done"
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertEqual(len(self.cleared(lines)), 1)
        self.assertNotIn(f"c@{LOCATION}/checkout", self.ledger()[stall_watch.EPISODES_KEY])

    def test_a_board_that_cannot_show_the_card_keeps_the_rows_and_the_comment_pending(self):
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        other = finding("checkout", "Deployment/cart-api", "generation-lag", "generation 2 observed 1")
        self.board.fail_show = True
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW, other]}})
        self.assertEqual(lines, [])
        self.assertEqual(len(self.board.cards), 1, "no second card while the first cannot be read")
        self.assertEqual(len(self.ledger()["stalls"]), 2, "the rows stay ledgered")
        self.assertEqual(self.ledger()[stall_watch.EPISODES_KEY][f"c@{LOCATION}/checkout"]["pending"], ["Deployment/cart-api"])
        self.board.fail_show = False
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW, other]}})
        self.assertEqual(lines, [])
        card = next(iter(self.board.cards.values()))
        self.assertEqual(len(card["comments"]), 1)
        self.assertIn("Deployment/cart-api", card["comments"][0])
        self.assertEqual(self.ledger()[stall_watch.EPISODES_KEY][f"c@{LOCATION}/checkout"]["pending"], [])

    def test_a_pending_comment_after_a_board_hiccup_survives_an_unreadable_tick(self):
        # The bot's scenario: hiccup on the tick a new object joins, then the
        # cluster is unreadable on the next one; the card must not be closed.
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        other = finding("checkout", "Deployment/cart-api", "generation-lag", "generation 2 observed 1")
        self.board.fail_show = True
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW, other]}})
        self.board.fail_show = False
        lines, _ = self.run_tick({"c": TIMEOUT})
        self.assertEqual(lines, [])
        card = next(iter(self.board.cards.values()))
        self.assertEqual(card["status"], "ready")
        self.assertIn(f"c@{LOCATION}/checkout", self.ledger()[stall_watch.EPISODES_KEY])
        self.assertEqual(len(card["comments"]), 1, "the pending comment went out once the board answered, even on an unreadable tick")

    def test_a_card_gone_from_the_board_ends_its_episode_and_a_new_stall_opens_a_new_card(self):
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        first = next(iter(self.board.cards))
        self.board.forget(first)
        other = finding("checkout", "Deployment/cart-api", "generation-lag", "generation 2 observed 1")
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW, other]}})
        self.assertEqual(len(self.noticed(lines)), 1, "a new card, not a comment on a card that is not there")
        self.assertEqual(len(self.board.cards), 1)
        self.assertNotEqual(next(iter(self.board.cards)), first)

    def test_a_card_gone_from_the_board_ends_its_episode_on_clear_without_a_line(self):
        self.run_tick({"c": {"checkout": [DEADLINE_ROW]}})
        self.board.forget(next(iter(self.board.cards)))
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertEqual(lines, [])
        self.assertEqual(self.ledger()[stall_watch.EPISODES_KEY], {})

    def test_a_board_that_cannot_answer_for_three_ticks_ends_the_episode(self):
        self.run_tick({"c": {"checkout": [DEADLINE_ROW]}})
        with patch.object(stall_watch, "card_exists", return_value=None):
            self.board.fail_show = True
            for _ in range(stall_watch.MAX_UNKNOWN_CARD_TICKS - 1):
                self.run_tick({"c": {"checkout": []}})
                self.assertIn(f"c@{LOCATION}/checkout", self.ledger()[stall_watch.EPISODES_KEY])
            self.run_tick({"c": {"checkout": []}})
        self.assertEqual(self.ledger()[stall_watch.EPISODES_KEY], {})

    def test_one_failed_comment_does_not_complete_the_card_as_cleared(self):
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        tid = next(iter(self.board.cards))
        other = finding("checkout", "Deployment/cart-api", "generation-lag", "generation 2 observed 1")
        self.board.fail_comment_once = True
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW, other]}})
        self.assertEqual(lines, [])
        self.assertEqual(self.board.cards[tid]["status"], "ready", "the stall is still present; nothing completed it")
        self.assertEqual(self.board.cards[tid]["comments"], [])
        self.assertEqual(len(self.ledger()["stalls"]), 2, "the rows stay; only the comment is pending")
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW, other]}})
        self.assertEqual(lines, [])
        self.assertEqual(len(self.board.cards[tid]["comments"]), 1, "sent once, from the pending list, not once per tick")
        self.assertNotIn("also sees these objects stalled in `checkout`:\n", self.board.cards[tid]["comments"][0])

    def test_a_failed_subscription_is_retried_on_a_later_tick(self):
        with patch.object(stall_watch, "subscribe_card", return_value=0):
            self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        tid = next(iter(self.board.cards))
        self.assertFalse(self.ledger()[stall_watch.EPISODES_KEY][f"c@{LOCATION}/checkout"]["subscribed"])
        self.assertEqual(self.subs(tid), [])
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        self.assertTrue(self.ledger()[stall_watch.EPISODES_KEY][f"c@{LOCATION}/checkout"]["subscribed"])
        self.assertEqual(len(self.subs(tid)), 1)

    def test_a_stall_that_comes_back_after_its_card_was_completed_gets_a_new_card(self):
        # The board answers a repeated key with the finished card; the key must
        # not repeat across episodes, however unchanged the object's spec is.
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        first = next(iter(self.board.cards))
        self.run_tick({"c": {"checkout": []}})
        self.run_tick({"c": {"checkout": []}})
        self.assertEqual(self.board.cards[first]["status"], "done")
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        self.assertEqual(len(self.noticed(lines)), 1)
        self.assertEqual(len(self.board.cards), 2)
        self.assertNotIn(f"card `{first}`", lines[0])
        self.assertEqual(self.ledger()[stall_watch.GENERATIONS_KEY][f"c@{LOCATION}/checkout"], 1)

    def test_a_card_the_agent_completed_early_is_not_reused_for_a_peer_that_appears_later(self):
        # Two objects from one apply, different thresholds: the Deployment's
        # card is done within the half hour, the Gateway shows up next tick.
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        first = next(iter(self.board.cards))
        self.board.cards[first]["status"] = "done"
        gateway = dict(GATEWAY_CONDITION_ROW, namespace="checkout")
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW, gateway]}})
        self.assertEqual(len(self.noticed(lines)), 1)
        self.assertEqual(len(self.board.cards), 2)
        self.assertEqual(next(reversed(self.board.cards.values()))["status"], "ready")

    def test_a_finished_card_handed_back_for_a_repeated_key_is_not_adopted(self):
        # Defence in depth: even if the key repeats, a terminal card is never
        # recorded as the episode's card.
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        first = next(iter(self.board.cards))
        self.board.cards[first]["status"] = "done"
        # Pretend the generation counter was lost with the ledger.
        state = self.ledger(); state[stall_watch.EPISODES_KEY] = {}; state[stall_watch.GENERATIONS_KEY] = {}; state["stalls"] = {}
        self.state.write_text(json.dumps(state))
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        self.assertEqual(len(self.noticed(lines)), 1)
        self.assertNotIn(f"card `{first}`", lines[0])
        self.assertEqual(len(self.board.cards), 2)

    def test_the_idempotency_key_is_stable_across_a_retry_however_long_the_sweeps_take(self):
        # A real stall's age grows with the clock; the fake reports it relative
        # to a fixed start, and the two ticks run 1,700 s apart with different
        # scan-to-card latencies, crossing several ten-minute boundaries.
        start = 1_800_000_000
        def rows_at(now):
            return [dict(DEPLOYMENT_ROW, stalled_seconds=now - start), finding("checkout", "Deployment/checkout-api", "repeating-warnings", "SYNC x9: x", stalled_seconds=900)]
        clock = {"now": start + 1000}
        with patch.object(stall_watch.time, "time", lambda: clock["now"]):
            self.board.fail_next_create = True
            self.run_tick({"c": {"checkout": rows_at(clock["now"])}})
            first = shlex.split(self.board.calls[0])
            clock["now"] = start + 2700
            self.run_tick({"c": {"checkout": rows_at(clock["now"])}})
            second = shlex.split([c for c in self.board.calls if c.startswith("create ")][-1])
        key = lambda argv: argv[argv.index("--idempotency-key") + 1]
        self.assertEqual(key(first), key(second))
        self.assertTrue(key(first).endswith(f"-{start // stall_watch.EPISODE_ID_RESOLUTION_SECONDS}-g0"), key(first))

    def test_a_card_the_agent_already_completed_is_not_completed_again_on_clear(self):
        self.run_tick({"c": {"checkout": [DEADLINE_ROW]}})
        tid = next(iter(self.board.cards))
        self.board.cards[tid]["status"] = "done"
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertEqual(len(self.cleared(lines)), 1)
        self.assertFalse(any(c.startswith("complete ") for c in self.board.calls))
        self.assertEqual(self.board.cards[tid]["comments"], [])

    def test_a_board_that_refuses_the_card_leaves_the_objects_new_for_the_next_tick(self):
        self.board.fail_next_create = True
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        self.assertEqual(lines, [])
        self.assertEqual(self.ledger()["stalls"], {})
        lines, _ = self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        self.assertEqual(len(self.noticed(lines)), 1)
        self.assertEqual(len(self.board.cards), 1, "the refused attempt filed nothing; the retry filed once")

    def test_a_healthy_fleet_opens_nothing_and_prints_nothing(self):
        lines, _ = self.run_tick({"c": {"catalog": [], "checkout": []}})
        self.assertEqual(lines, [])
        self.assertEqual(self.board.calls, [])
        self.assertTrue(self.state.exists())

    def test_two_clusters_two_cards(self):
        lines, _ = self.run_tick({"a": {"storefront": GATEWAY_ROWS}, "b": {"checkout": [DEPLOYMENT_ROW]}})
        self.assertEqual(len(self.noticed(lines)), 2)
        self.assertEqual(len(self.board.opened()), 2)

    def test_same_named_clusters_in_two_locations_are_two_scopes(self):
        fleet = {"c": {"payments": [DEPLOYMENT_ROW]}, "c@europe-west1": Located("europe-west1", {"payments": []})}
        lines, _ = self.run_tick(fleet)
        self.assertEqual(len(self.noticed(lines)), 1)
        lines, _ = self.run_tick(fleet)
        self.assertEqual(lines, [], "the second cluster's empty namespace does not clear the first cluster's row")

    def test_a_long_event_message_is_cut_in_the_card_but_kept_in_the_ledger(self):
        long_row = finding("ns", "Gateway/g", "repeating-warnings", "SYNC x9: " + "x" * 5000)
        self.run_tick({"c": {"ns": [long_row]}})
        card = next(iter(self.board.cards.values()))
        self.assertIn(stall_watch.TRUNCATION_MARKER, card["body"])
        self.assertLess(len(card["body"]), 1500)
        self.assertEqual(len(list(self.ledger()["stalls"].values())[0]["detail"]), len(long_row["detail"]))

    def test_a_title_with_many_objects_is_capped(self):
        rows = [finding("ns", f"Deployment/very-long-deployment-name-{i:02d}", "generation-lag", "generation 2 observed 1") for i in range(12)]
        self.run_tick({"c": {"ns": rows}})
        card = next(iter(self.board.cards.values()))
        self.assertLessEqual(len(card["title"]), stall_watch.CARD_TITLE_MAX_CHARS)
        self.assertEqual(card["body"].count("\n- "), 12)


class Subscriptions(Base):
    def test_home_targets_come_from_config_when_the_environment_is_scrubbed(self):
        self.assertEqual(os.environ.get("GOOGLE_CHAT_HOME_CHANNEL"), "")
        self.assertEqual(stall_watch.home_targets(), [("google_chat", HOME_CHANNEL, "")])

    def test_config_wins_over_the_environment_and_the_environment_fills_the_rest(self):
        with patch.dict(os.environ, {"GOOGLE_CHAT_HOME_CHANNEL": "spaces/STALE", "SLACK_HOME_CHANNEL": "C123", "SLACK_HOME_CHANNEL_THREAD_ID": "171.9", "TEAMS_HOME_CHANNEL": "19:abc"}):
            self.assertEqual(stall_watch.home_targets(), [("google_chat", HOME_CHANNEL, ""), ("slack", "C123", "171.9")])

    def test_a_scheduled_tick_writes_the_row_with_no_home_channel_variable_at_all(self):
        # The production case: the environment carries no *_HOME_CHANNEL and
        # the row still lands, from config.yaml.
        self.run_tick({"c": {"storefront": GATEWAY_ROWS}})
        tid = next(iter(self.board.cards))
        self.assertEqual(self.subs(tid)[0][:2], ("google_chat", HOME_CHANNEL))
        self.assertTrue(self.ledger()[stall_watch.EPISODES_KEY][f"c@{LOCATION}/storefront"]["subscribed"])

    def test_without_any_home_channel_no_row_is_written_and_the_card_still_opens(self):
        (self.home / stall_watch.CONFIG_FILE_NAME).unlink()
        lines, _ = self.run_tick({"c": {"storefront": GATEWAY_ROWS}})
        self.assertEqual(len(self.noticed(lines)), 1)
        self.assertEqual(self.subs(next(iter(self.board.cards))), [])
        self.assertFalse(self.ledger()[stall_watch.EPISODES_KEY][f"c@{LOCATION}/storefront"]["subscribed"])

    def test_a_card_not_on_the_board_gets_no_row(self):
        self.assertEqual(stall_watch.subscribe_card("t_deadbeef", self.db), 0)

    def test_subscribing_twice_writes_once_and_still_counts_the_row(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS}})
        tid = next(iter(self.board.cards))
        self.assertEqual(stall_watch.subscribe_card(tid, self.db), 1, "a row already on the board is a subscribed card, not a failure")
        self.assertEqual(len(self.subs(tid)), 1)

    def test_a_commit_that_fails_counts_as_no_row_written(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS}})
        tid = next(iter(self.board.cards))
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM kanban_notify_subs")
        conn.commit()
        conn.close()
        real_connect = stall_watch.sqlite3.connect

        class LosesTheCommit:
            def __init__(self, inner):
                self._inner = inner

            def commit(self):
                raise sqlite3.OperationalError("disk I/O error")

            def __getattr__(self, name):
                return getattr(self._inner, name)

        with patch.object(stall_watch.sqlite3, "connect", lambda *a, **k: LosesTheCommit(real_connect(*a, **k))):
            self.assertEqual(stall_watch.subscribe_card(tid, self.db), 0)
        self.assertEqual(self.subs(tid), [], "the transaction was discarded with the connection")

    def test_a_replacement_card_carries_every_object_the_scope_still_holds(self):
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        first = next(iter(self.board.cards))
        self.board.forget(first)
        other = finding("checkout", "Deployment/cart-api", "generation-lag", "generation 2 observed 1")
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW, other]}})
        card = next(reversed(self.board.cards.values()))
        self.assertIn("Deployment/checkout-api", card["body"])
        self.assertIn("Deployment/cart-api", card["body"])
        self.assertIn("Deployment/checkout-api", card["title"])


class Ledger(Base):
    def test_a_rising_event_count_is_the_same_row(self):
        sync = lambda n: finding("storefront", "Gateway/storefront-gateway", "repeating-warnings", f"SYNC x{n}: {GATEWAY_SECRET}")
        self.run_tick({"c": {"storefront": [sync(12)]}})
        first = self.ledger()["stalls"]
        lines, _ = self.run_tick({"c": {"storefront": [sync(13)]}})
        self.assertEqual(lines, [])
        second = self.ledger()["stalls"]
        self.assertEqual(list(first), list(second))
        self.assertEqual(list(second.values())[0]["first_seen"], list(first.values())[0]["first_seen"])
        self.assertIn("SYNC x13:", list(second.values())[0]["detail"])

    def test_a_warning_that_recurs_outside_the_window_does_not_flap(self):
        only_sync = {"c": {"storefront": [GATEWAY_SYNC_ROW]}}
        quiet = {"c": {"storefront": []}}
        self.run_tick(only_sync)
        self.assertEqual(self.run_tick(quiet)[0], [])
        self.assertEqual(self.run_tick(only_sync)[0], [])
        self.assertEqual(self.run_tick(quiet)[0], [])
        lines, _ = self.run_tick(quiet)
        self.assertEqual(len(self.cleared(lines)), 1)
        self.assertEqual(len(self.board.opened()), 1)

    def test_a_dangling_reference_survives_one_failed_referent_listing(self):
        forbidden = "warning: cannot list configmaps in checkout; references to configmaps are not checked: timeout\n"
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        self.assertEqual(self.run_tick({"c": {"checkout": ([], forbidden)}})[0], [])
        self.assertEqual(self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})[0], [], "never cleared, so not new")
        self.run_tick({"c": {"checkout": []}})
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertEqual(len(self.cleared(lines)), 1)

    def test_a_condition_row_clears_on_the_first_scan_without_it(self):
        self.run_tick({"c": {"storefront": [GATEWAY_CONDITION_ROW]}})
        lines, _ = self.run_tick({"c": {"storefront": []}})
        self.assertEqual(len(self.cleared(lines)), 1)

    def test_a_scan_that_skipped_a_kind_holds_only_that_kinds_rows(self):
        gateway = dict(GATEWAY_CONDITION_ROW, namespace="checkout")
        self.run_tick({"c": {"checkout": [DEADLINE_ROW, gateway]}})
        lines, _ = self.run_tick({"c": {"checkout": ([], NOT_SCANNED)}})
        self.assertEqual(lines, [], "the Deployment row is unknown; the Gateway row cleared but the object list is not empty yet")
        kinds_left = sorted(e["object"].split("/")[0] for e in self.ledger()["stalls"].values())
        self.assertEqual(kinds_left, ["Deployment"], "deployments were not scanned, gateways were")
        self.assertIn("partial: deployments not read", self.ledger()["unreadable"][f"c@{LOCATION}/checkout"])
        self.assertEqual(self.run_tick({"c": {"checkout": [DEADLINE_ROW]}})[0], [], "never cleared, so not new")
        lines, _ = self.run_tick({"c": {"checkout": []}})
        self.assertEqual(len(self.cleared(lines)), 1)

    def test_events_not_read_holds_only_repeating_warning_rows(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS}})
        no_events = "warning: events in storefront not read; repeating-warnings is not evaluated: kubectl exited 1\n"
        lines, _ = self.run_tick({"c": {"storefront": ([], no_events)}})
        self.assertEqual(lines, [])
        left = sorted(e["heuristic"] for e in self.ledger()["stalls"].values())
        self.assertEqual(left, ["repeating-warnings"], "the condition row cleared; the warning row waits for a scan that read events")

    def test_a_skipped_resource_matches_its_object_kind(self):
        for resource, kind in (("gateways.gateway.networking.k8s.io", "Gateway"), ("networkpolicies.networking.k8s.io", "NetworkPolicy"), ("ingresses.networking.k8s.io", "Ingress"), ("statefulsets", "StatefulSet"), ("jobs.batch", "Job")):
            self.assertTrue(stall_watch.resource_names_kind(resource, kind), (resource, kind))
        self.assertFalse(stall_watch.resource_names_kind("deployments", "Gateway"))

    def test_the_system_namespace_set_is_the_reliability_audits_s1(self):
        # Read the SOP the way the roster test reads it, so a namespace added
        # to S1 is required to reach this script too.
        import re
        sop = (Path(stall_watch.__file__).resolve().parents[1] / "governance" / "obtainability_audit_sop.md").read_text()
        anchor = "**S1 — system namespace:**"
        tail = sop[sop.index(anchor) + len(anchor):].split("\n", 1)[0]
        connector = re.compile(r"^,?\s*(or\s+)?(plus\s+)?(any namespace matching\s+)?$")
        found, end = [], None
        for match in re.finditer(r"`([A-Za-z0-9\-.*]+)`", tail):
            if end is not None and not connector.match(tail[end : match.start()]):
                break
            found.append(match.group(1))
            end = match.end()
        ours = set(stall_watch.SYSTEM_NAMESPACES) | {p + "*" for p in stall_watch.SYSTEM_NAMESPACE_PREFIXES}
        self.assertEqual(ours, set(found))

    def test_a_partial_scan_still_adds_new_rows(self):
        lines, _ = self.run_tick({"c": {"checkout": ([DEPLOYMENT_ROW], NOT_SCANNED)}})
        self.assertEqual(len(self.noticed(lines)), 1)


class Gone(Base):
    def test_a_deleted_namespace_clears_its_object_at_once(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS, "catalog": []}})
        lines, _ = self.run_tick({"c": {"catalog": []}})
        self.assertEqual(len(self.cleared(lines)), 1)
        self.assertEqual(self.ledger()["stalls"], {})
        self.assertEqual(next(iter(self.board.cards.values()))["status"], "done")

    def test_a_deleted_cluster_clears_its_objects(self):
        self.run_tick({"a": {"storefront": GATEWAY_ROWS}, "b": {"catalog": []}})
        self.run_tick({"a": TIMEOUT, "b": {"catalog": []}})
        self.assertIn(f"a@{LOCATION}", self.ledger()["unreadable"])
        lines, _ = self.run_tick({"b": {"catalog": []}})
        self.assertEqual(len(self.cleared(lines)), 1)
        self.assertEqual(self.ledger()["stalls"], {})
        self.assertEqual(self.ledger()["unreadable"], {})

    def test_a_reconciling_cluster_is_swept_and_a_provisioning_one_is_unreadable(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS}})
        lines, fake = self.run_tick({"c": Located(LOCATION, {"storefront": GATEWAY_ROWS}, status="RECONCILING")})
        self.assertEqual(lines, [])
        self.assertEqual(fake.scanned(), ["storefront"])
        lines, _ = self.run_tick({"c": Unlisted("PROVISIONING")})
        self.assertEqual(lines, [])
        self.assertEqual(self.ledger()["unreadable"], {f"c@{LOCATION}": "status=PROVISIONING"})
        self.assertEqual(len(self.ledger()["stalls"]), 2)

    def test_a_listing_gcloud_calls_incomplete_clears_nothing(self):
        self.run_tick({"a": {"storefront": GATEWAY_ROWS}, "b": {"catalog": []}})
        partial = "WARNING: The following zones did not respond: us-central1-a. List results may be incomplete."
        lines, _ = self.run_tick({"a": {"storefront": GATEWAY_ROWS}, "b": {"catalog": []}}, listing_stderr=partial, hidden=["a"])
        self.assertEqual(lines, [])
        self.assertIn(stall_watch.LISTING_SCOPE, self.ledger()["unreadable"])
        self.assertEqual(len(self.ledger()["stalls"]), 2)
        lines, _ = self.run_tick({"b": {"catalog": []}})
        self.assertEqual(len(self.cleared(lines)), 1, "a complete listing without the cluster is a deletion")


class Unreadable(Base):
    def test_unreachable_cluster_keeps_its_rows(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS}})
        lines, _ = self.run_tick({"c": TIMEOUT})
        self.assertEqual(lines, [])
        self.assertEqual(self.ledger()["unreadable"], {f"c@{LOCATION}": "timed out after 60s"})
        self.assertEqual(len(self.ledger()["stalls"]), 2)

    def test_unreadable_namespace_keeps_its_rows(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS, "checkout": [DEADLINE_ROW]}})
        lines, _ = self.run_tick({"c": {"storefront": stall_watch.REPORT_UNREADABLE_EXIT, "checkout": []}})
        self.assertEqual(len(self.cleared(lines)), 1, "checkout cleared; storefront was not read")
        self.assertIn(f"c@{LOCATION}/storefront", self.ledger()["unreadable"])
        self.assertEqual(len(self.ledger()["stalls"]), 2)

    def test_a_scan_timeout_ends_that_clusters_sweep_for_the_tick(self):
        self.run_tick({"c": {"a": [], "b": [], "d": [dict(DEPLOYMENT_ROW, namespace="d")]}})
        scan_timeout = subprocess.TimeoutExpired("python3", stall_watch.NAMESPACE_SCAN_TIMEOUT_SECONDS)
        lines, fake = self.run_tick({"c": {"a": [], "b": scan_timeout, "d": []}})
        self.assertEqual(fake.scanned(), ["a", "b"], "the namespace after the timeout is not scanned")
        self.assertEqual(lines, [])
        self.assertIn("namespace b timed out after 300s", self.ledger()["unreadable"][f"c@{LOCATION}"])
        self.assertEqual(len(self.ledger()["stalls"]), 1)

    def test_an_api_resources_timeout_is_confined_to_its_cluster(self):
        self.run_tick({"a": {"ns": [DEPLOYMENT_ROW]}, "b": {"ns": []}})
        lines, _ = self.run_tick({"a": {"ns": [DEPLOYMENT_ROW]}, "b": {"ns": []}}, served=subprocess.TimeoutExpired("kubectl", stall_watch.API_RESOURCES_TIMEOUT_SECONDS))
        self.assertEqual(lines, [])
        self.assertEqual(set(self.ledger()["unreadable"]), {f"a@{LOCATION}", f"b@{LOCATION}"})
        self.assertEqual(len(self.ledger()["stalls"]), 1)

    def test_unparsable_output_marks_one_scope_not_the_sweep(self):
        self.run_tick({"c": {"storefront": GATEWAY_ROWS, "checkout": [DEADLINE_ROW]}})
        lines, _ = self.run_tick({"c": {"storefront": '{"namespace": "storefront", "find', "checkout": []}})
        self.assertEqual(len(self.cleared(lines)), 1)
        self.assertIn("unparsable", self.ledger()["unreadable"][f"c@{LOCATION}/storefront"])
        self.assertIsNone(self.ledger()["sweep_error"])

    def test_a_lost_sandbox_is_one_sweep_failure(self):
        fleet = {"a": {"n1": [], "n2": [DEPLOYMENT_ROW], "n3": []}, "b": {"n4": []}}
        self.run_tick(fleet)
        lines, fake = self.run_tick(fleet, sandbox_dies_at=("a", "n2"))
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(stall_watch.SWEEP_FAILED_PREFIX), lines[0])
        self.assertIn("Connection refused", lines[0])
        self.assertEqual(fake.scanned(), ["n1", "n2"], "nothing after the lost hop is attempted")
        self.assertEqual(len(self.ledger()["stalls"]), 1)
        lines, _ = self.run_tick(fleet)
        self.assertEqual(lines, [stall_watch.SWEEP_RECOVERED_LINE])

    def test_failed_cluster_list_is_reported_once_and_recovery_once(self):
        def broken(argv, *, timeout, kubeconfig=None, stdin=None):
            return completed(argv, "", returncode=1, stderr="ERROR: (gcloud.auth) reauth required")

        with patch.object(stall_watch, "run_sandbox", broken):
            first = stall_watch.tick(self.state, dry_run=False)
            second = stall_watch.tick(self.state, dry_run=False)
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0].startswith(stall_watch.SWEEP_FAILED_PREFIX))
        self.assertIn("reauth required", first[0])
        self.assertEqual(second, [])
        lines, _ = self.run_tick({"c": {"catalog": []}})
        self.assertEqual(lines, [stall_watch.SWEEP_RECOVERED_LINE])

    def test_the_sweep_stops_at_its_budget_and_a_cluster_it_never_reached_is_unread(self):
        fleet = {"a": {"ns": []}, "b": {"ns": []}, "c": {"ns": [dict(DEPLOYMENT_ROW, namespace="ns")]}}
        self.run_tick(fleet)
        over = stall_watch.TICK_BUDGET_SECONDS + 1
        with patch.object(stall_watch.time, "monotonic", side_effect=[0, 1, 2, over] + [over] * 8):
            lines, fake = self.run_tick(fleet)
        self.assertEqual(fake.scanned(), ["ns"])
        self.assertEqual(lines, [], "c was listed but never read; its row is unknown, not gone")
        self.assertIn("exhausted after 1 clusters and 1 namespaces", self.ledger()["unreadable"][stall_watch.BUDGET_SCOPE])
        self.assertEqual(len(self.ledger()["stalls"]), 1)

    def test_an_exhausted_sweep_resumes_where_it_stopped(self):
        fleet = {"a": {"n1": [], "n2": []}, "b": {"n3": []}, "c": {"n4": [dict(DEPLOYMENT_ROW, namespace="n4")]}}
        over = stall_watch.TICK_BUDGET_SECONDS + 1
        with patch.object(stall_watch.time, "monotonic", side_effect=[0, 1, 2, over] + [over] * 8):
            _, fake = self.run_tick(fleet)
        self.assertEqual(fake.scanned(), ["n1"])
        self.assertEqual(self.ledger()[stall_watch.CURSOR_KEY], {"cluster": stall_watch.cluster_id("a", LOCATION), "namespace": "n2"})
        with patch.object(stall_watch.time, "monotonic", side_effect=[0, 1, 2, 3, 4, over] + [over] * 8):
            _, fake = self.run_tick(fleet)
        self.assertEqual(fake.scanned(), ["n2", "n3"])
        self.assertEqual(self.ledger()[stall_watch.CURSOR_KEY]["cluster"], stall_watch.cluster_id("c", LOCATION))
        lines, fake = self.run_tick(fleet)
        self.assertEqual(fake.scanned(), ["n4", "n1", "n2", "n3"])
        self.assertIsNone(self.ledger()[stall_watch.CURSOR_KEY])
        self.assertEqual(len(self.noticed(lines)), 1)


class Scope(Base):
    def test_system_namespaces_are_skipped_and_the_harness_is_not(self):
        extra = ["kube-system", "gke-managed-cim", "config-management-system", "gmp-public"]
        _, fake = self.run_tick({"c": {"payments": [], "kubeagents-system": []}}, namespaces_extra=extra)
        self.assertEqual(sorted(fake.scanned()), ["kubeagents-system", "payments"])

    def test_a_terminating_namespace_is_still_read_so_its_rows_can_clear(self):
        gone = [dict(GATEWAY_CONDITION_ROW, namespace="old")]
        self.run_tick({"c": {"old": gone}})
        lines, _ = self.run_tick({"c": {"old": []}})
        self.assertEqual(len(self.cleared(lines)), 1)

    def test_default_kinds_are_passed_and_all_lets_the_script_decide(self):
        _, fake = self.run_tick({"c": {"payments": []}})
        scan = next(argv for argv, _, _ in fake.calls if argv[0] == stall_watch.PYTHON_EXECUTABLE)
        self.assertEqual(scan[scan.index("--kind") + 1], ",".join(stall_watch.DEFAULT_KINDS))
        self.assertNotIn("pods", scan[scan.index("--kind") + 1].split(","))
        with patch.dict(os.environ, {stall_watch.KINDS_ENV: "all"}):
            _, fake = self.run_tick({"c": {"payments": []}})
        scan = next(argv for argv, _, _ in fake.calls if argv[0] == stall_watch.PYTHON_EXECUTABLE)
        self.assertNotIn("--kind", scan)

    def test_kinds_a_cluster_does_not_serve_are_not_asked_for(self):
        no_cert_manager = [n for n in SERVED_DEFAULT if not n.startswith("certificates")]
        _, fake = self.run_tick({"c": {"payments": []}}, served=no_cert_manager)
        scan = next(argv for argv, _, _ in fake.calls if argv[0] == stall_watch.PYTHON_EXECUTABLE)
        kinds = scan[scan.index("--kind") + 1].split(",")
        self.assertNotIn("certificates.cert-manager.io", kinds)
        self.assertIn("deployments", kinds, "a bare plural matches its grouped api-resources name")
        _, fake = self.run_tick({"c": {"payments": []}}, served=[])
        scan = next(argv for argv, _, _ in fake.calls if argv[0] == stall_watch.PYTHON_EXECUTABLE)
        self.assertEqual(scan[scan.index("--kind") + 1], ",".join(stall_watch.DEFAULT_KINDS), "an empty api-resources leaves the list unfiltered")

    def test_a_full_listing_with_a_failed_aggregated_api_still_filters(self):
        no_cert_manager = [n for n in SERVED_DEFAULT if not n.startswith("certificates")]
        _, fake = self.run_tick({"c": {"payments": []}}, served=no_cert_manager, api_resources_rc_one=True)
        scan = next(argv for argv, _, _ in fake.calls if argv[0] == stall_watch.PYTHON_EXECUTABLE)
        self.assertNotIn("certificates.cert-manager.io", scan[scan.index("--kind") + 1].split(","))

    def test_a_grouped_kind_matches_only_its_own_group(self):
        istio = [n for n in SERVED_DEFAULT if n != "gateways.gateway.networking.k8s.io"] + ["gateways.networking.istio.io"]
        _, fake = self.run_tick({"c": {"payments": []}}, served=istio)
        scan = next(argv for argv, _, _ in fake.calls if argv[0] == stall_watch.PYTHON_EXECUTABLE)
        kinds = scan[scan.index("--kind") + 1].split(",")
        self.assertNotIn("gateways.gateway.networking.k8s.io", kinds, "Istio's gateways do not stand in for the Gateway API's")
        self.assertIn("httproutes.gateway.networking.k8s.io", kinds)

    def test_the_project_comes_from_the_operators_variable_without_a_gcloud_hop(self):
        with patch.dict(os.environ, {stall_watch.PROJECT_ENVS[0]: "", "GCP_PROJECT_ID": "from-operator"}):
            _, fake = self.run_tick({"c": {"payments": []}})
        argvs = [argv for argv, _, _ in fake.calls]
        self.assertNotIn(["gcloud", "config", "get-value", "project"], argvs)
        self.assertIn("--project=from-operator", argvs[0])

    def test_the_report_script_travels_on_stdin_in_isolated_mode(self):
        _, fake = self.run_tick({"c": {"payments": []}})
        argv, kubeconfig, stdin = next(c for c in fake.calls if c[0][0] == stall_watch.PYTHON_EXECUTABLE)
        self.assertEqual(argv[:3], [stall_watch.PYTHON_EXECUTABLE, stall_watch.PYTHON_ISOLATED_FLAG, stall_watch.STDIN_SCRIPT_ARG])
        expected = (Path(stall_watch.__file__).resolve().parent / stall_watch.LOCAL_REPORT_SCRIPT_NAME).read_text()
        self.assertEqual(stdin, expected)
        self.assertTrue(kubeconfig.endswith(f"{stall_watch.KUBECONFIG_FILE_PREFIX}proj_c_{LOCATION}{stall_watch.KUBECONFIG_FILE_SUFFIX}"))

    def test_isolated_mode_ignores_a_decoy_module_in_the_working_directory(self):
        source = stall_watch.report_source()
        with tempfile.TemporaryDirectory() as cwd:
            (Path(cwd) / "json.py").write_text("raise SystemExit(99)\n")
            argv = stall_watch.report_argv("payments", None)
            argv[0] = sys.executable
            isolated = subprocess.run(argv + ["--help"], input=source, capture_output=True, text=True, cwd=cwd)
            naive = subprocess.run([sys.executable, stall_watch.STDIN_SCRIPT_ARG, "--help"], input=source, capture_output=True, text=True, cwd=cwd)
        self.assertEqual(isolated.returncode, 0, isolated.stderr)
        self.assertIn("--threshold-minutes", isolated.stdout)
        self.assertEqual(naive.returncode, 99, "the decoy is what a non-isolated interpreter would have run")

    def test_only_kubeconfig_and_stdin_cross_into_the_sandbox(self):
        with patch.object(stall_watch.sandbox_exec, "run", return_value=completed([], "[]")) as run:
            stall_watch.run_sandbox(["python3", "-I", "-"], timeout=5, kubeconfig="/k", stdin="print(1)")
        self.assertEqual(run.call_args.kwargs["remote_env"], {"KUBECONFIG": "/k"})
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertEqual(run.call_args.kwargs["stdin"], "print(1)")
        self.assertNotIn("principal", run.call_args.kwargs)

    def test_production_kubeconfig_path_is_under_hermes_home_with_the_watch_prefix(self):
        with patch.object(stall_watch.sandbox_exec, "sandbox_enabled", return_value=True):
            path = stall_watch.kubeconfig_path("my-proj", "a cluster", "us-central1")
        self.assertEqual(path, f"{stall_watch.SANDBOX_KUBECONFIG_DIR}/{stall_watch.KUBECONFIG_FILE_PREFIX}my-proj_a-cluster_us-central1{stall_watch.KUBECONFIG_FILE_SUFFIX}")

    def test_the_report_source_prefers_the_image_copy_and_honours_the_override(self):
        with tempfile.TemporaryDirectory() as d:
            image = Path(d) / "image.py"
            image.write_text("IMAGE")
            override = Path(d) / "override.py"
            override.write_text("OVERRIDE")
            with patch.object(stall_watch, "IMAGE_REPORT_SCRIPT", str(image)):
                self.assertEqual(stall_watch.report_source(), "IMAGE")
                with patch.dict(os.environ, {stall_watch.REPORT_SCRIPT_ENV: str(override)}):
                    self.assertEqual(stall_watch.report_source(), "OVERRIDE")
            with patch.object(stall_watch, "IMAGE_REPORT_SCRIPT", str(Path(d) / "absent.py")):
                self.assertIn("stalled resources", stall_watch.report_source(), "the sibling copy is the fallback")
            with patch.object(stall_watch, "IMAGE_REPORT_SCRIPT", str(Path(d) / "absent.py")), patch.object(stall_watch, "LOCAL_REPORT_SCRIPT_NAME", "nope.py"):
                with self.assertRaises(RuntimeError):
                    stall_watch.report_source()

    def test_state_is_written_atomically_and_versioned(self):
        self.run_tick({"c": {"payments": []}})
        self.assertFalse(self.state.with_name(self.state.name + stall_watch.STATE_TMP_SUFFIX).exists())
        self.assertEqual(self.ledger()["version"], stall_watch.STATE_SCHEMA_VERSION)

    def test_a_ledger_from_another_version_is_discarded(self):
        self.state.write_text(json.dumps({"version": 2, "stalls": {"x": {}}}))
        self.assertEqual(stall_watch.load_state(self.state)["stalls"], {})


class Output(Base):
    def test_a_dry_run_opens_no_card_and_says_what_it_would_do(self):
        fake = FakeFleet({"c": {"storefront": GATEWAY_ROWS}})
        with patch.object(stall_watch, "run_sandbox", fake):
            lines = stall_watch.tick(self.state, dry_run=True)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(f"{stall_watch.DRY_RUN_PREFIX} would open a card"), lines[0])
        self.assertIn("Gateway/storefront-gateway", lines[0])
        self.assertEqual(self.board.calls, [])
        self.assertFalse(self.state.exists())

    def test_a_dry_run_on_an_open_episode_says_it_would_comment(self):
        self.run_tick({"c": {"checkout": [DEPLOYMENT_ROW]}})
        calls = len(self.board.calls)
        other = finding("checkout", "Deployment/cart-api", "generation-lag", "generation 2 observed 1")
        fake = FakeFleet({"c": {"checkout": [DEPLOYMENT_ROW, other]}})
        with patch.object(stall_watch, "run_sandbox", fake):
            lines = stall_watch.tick(self.state, dry_run=True)
        self.assertTrue(lines[0].startswith(f"{stall_watch.DRY_RUN_PREFIX} would comment on card"), lines[0])
        self.assertEqual(len(self.board.calls), calls)

    def test_main_prints_lines_and_exits_zero(self):
        fake = FakeFleet({"c": {"storefront": GATEWAY_ROWS}})
        out = io.StringIO()
        with patch.object(stall_watch, "run_sandbox", fake), redirect_stdout(out):
            rc = stall_watch.main(["--state", str(self.state)])
        self.assertEqual(rc, 0)
        self.assertIn(stall_watch.NOTICED_PREFIX, out.getvalue())
        out = io.StringIO()
        with patch.object(stall_watch, "run_sandbox", fake), redirect_stdout(out):
            stall_watch.main(["--state", str(self.state)])
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()

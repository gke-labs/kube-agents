"""A reply to an event-triage report, from delivery to the agent's turn (#802).

Three components own one behaviour between them and none of them can see the
other two:

1. ``deploy/docker/patches/kanban_notifier.store_incident_report`` posts the row
   when the notifier delivers a completed triage card into a chat thread.
2. ``session_kv_server``'s ``/v1/incidents`` routes store it and hand it back by
   ``(chat_id, thread_id)``.
3. The ``incident_context`` gateway plugin looks it up on every inbound message
   and prepends what it finds to the user's words.

Each of the three has its own unit tests and all three passed on ``main`` while
the chain was broken: #738 removed the only writer of the table and the reader
went on failing open, exactly as it is supposed to when the server is down. The
symptom was a user replying "apply Option A" in a thread and reaching an agent
that had never heard of Option A. Nothing short of driving the three together
can see that, which is what this module does — real notifier, real FastAPI app,
real SQLite file, real plugin, with the loopback socket replaced by a TestClient
because the two ends are in different processes in production and in one here.

    python3 -m unittest discover -s agents/platform/scripts -p 'test_*.py'
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

# Before importing session_kv_server, for the reason test_session_kv_server.py
# gives: the module resolves its database at import and would otherwise create
# the production path. Discovery imports both modules into one process, so
# whichever lands first wins and both point at a temp file either way.
_db_fd, TEMP_DB_PATH = tempfile.mkstemp()
os.close(_db_fd)
os.environ.setdefault("SESSION_KV_DB_PATH", TEMP_DB_PATH)

_SCRIPTS = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS.parents[2]
_PATCHES = _REPO_ROOT / "deploy" / "docker" / "patches"
sys.path.insert(0, str(_SCRIPTS))
# kanban_notifier.py runs inside /opt/hermes, where its sibling patch modules
# are importable as `gateway.<name>`; off-image it falls back to the bare name,
# which needs the patches directory on the path.
sys.path.append(str(_PATCHES))

from test_mcp_package_contract import requires_mcp  # noqa: E402


def _load(name, path):
    """Import a module by file path.

    Neither of the two ends is importable by name from here. The notifier is a
    Dockerfile-applied patch that lands at ``/opt/hermes/gateway/`` in the image
    and has no package in the checkout; the plugin is a package ``__init__.py``
    the gateway loads by path. Loading them the same way the runtime does keeps
    this test on the real code rather than a copy of it.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notifier = _load("kanban_notifier", _PATCHES / "kanban_notifier.py")
incident_context = _load(
    "incident_context",
    _REPO_ROOT / "agents" / "platform" / "plugins" / "incident_context" / "__init__.py",
)

API_KEY = "test-roundtrip-key"

CHAT_ID = "D0BKGRBM6RH"
THREAD_ID = "1786216044.637229"
CARD_ID = "t_a8f58a2a"

# What the Cluster Agent completes the card with: the shape
# `session_kv_server._triage_task_body` asks for, including the bullet #802 put
# back — the one that tells the reader a reply is worth typing.
REPORT = """\
## What's wrong

The `checkout` deployment on `prod-us-central1` cannot schedule: 0/3 replicas are Running.

## Why

- Each replica requests 8Gi of memory and every node in `default-pool` reports
  `Allocatable: memory: 3910Mi`, so the scheduler has nowhere to place them.

## What to do

- **Option A (Right-size the request):** lower `resources.requests.memory` to 2Gi in
  `apps/checkout/deployment.yaml`.
- **Option B (Add a larger node pool):** add an `e2-standard-8` pool and a nodeSelector.
- ✅ **Recommended: Option A** — no new capacity to pay for, drain, or delete later.
- **To authorize:** reply **'apply'** to open a GitOps Pull Request with the recommended fix, \
or name one directly with **'apply Option A'** / **'apply Option B'**.
"""


class _Task:
    """The kanban card row the notifier holds when the card turns terminal."""

    def __init__(self, result=REPORT, card_id=CARD_ID):
        self.id = card_id
        self.result = result


class _Event:
    """An inbound chat message, shaped as the gateway hands it to the hook."""

    class _Source:
        def __init__(self, chat_id, thread_id):
            self.platform = "slack"
            self.chat_id = chat_id
            self.thread_id = thread_id

    def __init__(self, text, chat_id=CHAT_ID, thread_id=THREAD_ID):
        self.text = text
        self.raw_message = None
        self.source = self._Source(chat_id, thread_id)


class _Response:
    """Enough of an ``http.client.HTTPResponse`` for both callers.

    The notifier only needs the context manager; ``incident_context._get`` reads
    ``.status`` and hands the object to ``json.load``, which calls ``.read()``.
    """

    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, *_):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@requires_mcp
class TriageReplyRoundTripTest(unittest.TestCase):
    """The chain, driven end to end."""

    @classmethod
    def setUpClass(cls):
        os.environ["SESSION_KV_API_KEY"] = API_KEY
        import session_kv_server
        from fastapi.testclient import TestClient

        cls.session_kv_server = session_kv_server
        # Not TEMP_DB_PATH: discovery imports test_session_kv_server into the
        # same process and it sets the variable first, so the server's own
        # resolved path is the only one that is certainly the live one.
        cls.db_path = session_kv_server.SESSION_KV_DB_PATH
        cls.client = TestClient(session_kv_server.app)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SESSION_KV_API_KEY", None)

    def setUp(self):
        os.environ["SESSION_KV_API_KEY"] = API_KEY
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM incidents")
        # Both ends call `urllib.request.urlopen` against 127.0.0.1:8699. In the
        # pod that is a real socket between the gateway process and the Session
        # KV server; here it is the same FastAPI app in-process. Patching the
        # shared `urllib.request` module covers both callers at once — which is
        # the point, since the whole failure was one end writing nowhere the
        # other end reads.
        patcher = patch.object(urllib.request, "urlopen", self._urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _urlopen(self, request, timeout=None):
        self.assertTrue(
            request.full_url.startswith("http://127.0.0.1:8699/"),
            f"unexpected egress to {request.full_url}",
        )
        path = request.full_url[len("http://127.0.0.1:8699") :]
        headers = {k: v for k, v in request.header_items()}
        if request.get_method() == "POST":
            response = self.client.post(path, content=request.data, headers=headers)
        else:
            response = self.client.get(path, headers=headers)
        if response.status_code >= 400:
            # urlopen raises on 4xx/5xx, and both callers are written around
            # that. A stub that returned the status instead would report a
            # rejected POST as a stored row — the whole failure this module is
            # about, reproduced inside the test harness.
            raise urllib.error.HTTPError(
                request.full_url, response.status_code, response.reason_phrase, {}, None
            )
        return _Response(response.status_code, response.content)

    def deliver(self, task=None, chat_id=CHAT_ID, thread_id=THREAD_ID):
        """What the notifier does after posting the report into the thread."""
        return notifier.store_incident_report(
            notifier_event("completed"),
            task if task is not None else _Task(),
            {"task_id": CARD_ID, "chat_id": chat_id, "thread_id": thread_id},
        )

    def reply(self, text, **kwargs):
        return incident_context.on_inbound(event=_Event(text, **kwargs))

    # --- the assertion the issue is about -----------------------------------

    def test_a_reply_in_the_triage_thread_reaches_the_agent_with_the_report(self):
        self.assertTrue(self.deliver())
        result = self.reply("apply Option A")
        self.assertIsNotNone(result, "the reply reached the agent with nothing attached")
        self.assertEqual(result["action"], "rewrite")
        # The whole report, not a status line: the agent has to resolve "Option
        # A" to a named manifest change in a named file on a named cluster.
        self.assertIn("Option A (Right-size the request)", result["text"])
        self.assertIn("apps/checkout/deployment.yaml", result["text"])
        self.assertIn("prod-us-central1", result["text"])
        # And the option the user did not pick is there too, because "apply
        # Option B" has to work from the same row.
        self.assertIn("Option B (Add a larger node pool)", result["text"])

    def test_the_users_words_are_the_last_thing_the_agent_reads(self):
        self.deliver()
        text = self.reply("apply Option A")["text"]
        self.assertTrue(text.rstrip().endswith("apply Option A"))
        self.assertIn("[User reply in thread]: apply Option A", text)

    def test_the_report_reaches_the_agent_fenced_as_untrusted(self):
        # It carries `kubectl` output from workloads other teams deploy, and it
        # is spliced into an authenticated turn ahead of the user's own words.
        self.deliver()
        text = self.reply("apply")["text"]
        self.assertIn("[SECURITY NOTICE:", text)
        self.assertIn("<untrusted_report>", text)
        self.assertIn("</untrusted_report>", text)

    def test_without_the_delivery_path_write_the_reply_arrives_bare(self):
        # The regression itself, reproduced: report delivered, no row written,
        # and the front door gets the bare word `apply` with no report, no
        # options and no cluster. This is what `main` did between #738 and #802.
        self.assertIsNone(self.reply("apply Option A"))

    # --- what the row must not do -------------------------------------------

    def test_a_reply_in_another_thread_does_not_get_this_report(self):
        # The by-thread key misses, so the agent gets the label-only index the
        # plugin falls back to: told a report exists in this space and told to
        # ask which one, rather than handed a report the user is not looking at.
        self.deliver()
        text = self.reply("apply Option A", thread_id="1786299999.000100")["text"]
        self.assertNotIn("Option A (Right-size the request)", text)
        self.assertIn("do NOT have their contents", text)
        self.assertIn("ask which one", text)

    def test_a_reply_in_another_chat_does_not_get_this_report(self):
        # Nothing has been posted in that space at all, so not even the index.
        self.deliver()
        self.assertIsNone(self.reply("apply Option A", chat_id="C0OTHER"))

    def test_the_first_report_in_a_thread_is_the_one_kept(self):
        # POST /v1/incidents is INSERT OR IGNORE on purpose. A second card
        # completing into the same thread must not displace the report whose
        # options the user is looking at while they type.
        self.deliver()
        self.assertTrue(self.deliver(_Task(result=REPORT.replace("Option A", "Option Z"))))
        text = self.reply("apply Option A")["text"]
        self.assertIn("Option A (Right-size the request)", text)
        self.assertNotIn("Option Z", text)

    def test_a_card_with_nothing_to_apply_does_not_take_the_thread(self):
        # And the reason the notifier gates on the report's shape rather than
        # storing every completion: first-write-wins makes an over-eager row
        # permanent for the table's TTL, so a status line stored here would
        # shadow the real report that lands in the same thread afterwards.
        self.assertFalse(self.deliver(_Task(result="No configuration drift found.")))
        self.assertTrue(self.deliver())
        self.assertIn("Option B", self.reply("apply Option B")["text"])

    def test_a_slash_command_in_the_thread_is_still_a_slash_command(self):
        # `incident_context` steps aside for these, and it has to keep doing so
        # in the one place a report now makes it most likely to fire.
        self.deliver()
        self.assertIsNone(self.reply("/hermes sethome"))

    # --- the seam between the two processes ---------------------------------

    def test_a_rejected_post_stores_nothing_and_says_so(self):
        # Both ends authenticate with the same pod-scoped bearer token, so an
        # unprovisioned pod fails at this seam. The store swallows the rejection
        # by design — the report has already been delivered and raising here
        # would rewind the notifier cursor and re-post it — which leaves the log
        # line as the only evidence. Without it the reply that follows is
        # indistinguishable from a user replying in the wrong thread.
        with patch.dict(os.environ, {"SESSION_KV_API_KEY": ""}):
            with self.assertLogs("gateway.run", level="WARNING") as captured:
                self.assertFalse(self.deliver())
        self.assertIn(CARD_ID, captured.output[0])
        self.assertIsNone(self.reply("apply Option A"))

    def test_the_stored_row_is_the_text_that_was_delivered(self):
        self.deliver()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            row = conn.execute(
                "SELECT chat_id, thread_id, report FROM incidents"
            ).fetchall()
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0][0], CHAT_ID)
        self.assertEqual(row[0][1], THREAD_ID)
        self.assertEqual(row[0][2], REPORT.strip())

    def test_the_gate_recognises_the_shape_the_template_asks_the_agent_for(self):
        # The one coupling between the two halves of this change, and the only
        # test in the repository that has both in scope. `actionable_report`
        # lives in deploy/docker/patches/ and matches two literals; the template
        # that makes an agent produce them lives here. Reword the heading to
        # "## Recommended actions", or the labels to "Choice A", and every other
        # test on both sides still passes while no real report earns a row
        # again — the pre-#802 bare `apply`, reintroduced with nothing red.
        instructions = self.session_kv_server._triage_task_body(
            {
                "reason": "FailedScheduling",
                "namespace": "checkout",
                "kind_of_object": "Deployment",
                "name": "checkout",
                "message": "0/3 nodes are available: insufficient memory.",
                "cluster": "prod-us-central1",
            }
        )
        self.assertIn("## What to do", instructions)
        self.assertIn("Option A", instructions)
        self.assertTrue(
            notifier.actionable_report(
                "## What to do\n\n- **Option A (Right-size the request):** 2Gi.\n"
            )
        )
        # And the same seam for the shape with no letter in it. One sound fix
        # is not "Option A", so the template drops the letter -- which is the
        # evidence the gate used to run on. The call to action is what carries
        # the coupling now, and it is the reply-carrying half of both shapes.
        self.assertIn("- **To authorize:** reply **'apply'**", instructions)
        self.assertTrue(
            notifier.actionable_report(
                "## What to do\n\n"
                "- **Proposed fix (Right-size the request):** 2Gi.\n"
                "- **To authorize:** reply **'apply'** to open a GitOps Pull "
                "Request with this fix.\n"
            )
        )

    def test_the_lookup_the_plugin_makes_is_the_one_the_server_answers(self):
        # Pins the query contract across the seam: parameter names and route.
        self.deliver()
        response = self.client.get(
            f"/v1/incidents/by-thread?chat_id={CHAT_ID}&thread_id={THREAD_ID}",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Option A", response.json()["report"])


def notifier_event(kind):
    class _Ev:
        pass

    event = _Ev()
    event.kind = kind
    return event


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the Slack live-check harness (hack/slack-live-check/harness.py).

A local HTTP server stands in for the Slack Web API, the GKE metadata server and
Secret Manager, and plays a minimal gateway: a DM to the bot or a mention of it is a
turn, a reply in a thread the bot has answered in is a turn, a listed sender gets the
status line and an answer, and anyone else gets the refusal notice once. The harness
runs end to end through its real urllib transport against it. The clock and sleep
are fakes, so the bounded polls time out without waiting.

The harness posts nothing: a person types each turn. FakeHuman is that person. The
harness hands it each turn it prints a TYPE line for, and it types the turn into the
fake Slack a few reads later, the way Slack renders a typed message (no bot_id, no
app_id, an @-picked mention as <@U...>), unless a test tells it to get it wrong. A post
made with a user token through chat.postMessage comes back carrying the app's bot_id
and app_id, as the live run of 2026-10-07 showed real Slack does.
"""

import base64
import contextlib
import importlib.util
import io
import json
import pathlib
import re
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = pathlib.Path(__file__).resolve().parent.parent
HARNESS_PATH = REPO / "hack" / "slack-live-check" / "harness.py"

spec = importlib.util.spec_from_file_location("slack_live_check_harness", HARNESS_PATH)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)

PROJECT = "test-project"
LISTED_SECRET = "slack-test-user-listed"
UNLISTED_SECRET = "slack-test-user-unlisted"
LISTED_TOKEN = "xoxp-1111-listed-user-token-value"
UNLISTED_TOKEN = "xoxp-2222-unlisted-user-token-value"
# Deliberately not token-shaped: only registration with the redactor can cut it.
GCP_ACCESS = "gcp-access-value-0b9e5d"
LISTED_ID = "U0LISTED1"
UNLISTED_ID = "U0UNLIST1"
BOT_ID = "U0BOTKAGE"
BOT_APP_BOT_ID = "B0BOTKAGE"
BOT_NAME = "kage"
USER_NAMES = {LISTED_ID: "listed", UNLISTED_ID: "unlisted", BOT_ID: BOT_NAME}
DISPLAY_NAMES = {LISTED_ID: "Lisa Listed", UNLISTED_ID: "Una Unlisted"}
CHANNEL_ID = "C0KATEST1"
CHANNEL_NAME = "ka-test"
HOME_ID = "C0HOME001"
PRIVATE_ID = "G0PRIVATE1"
PRIVATE_NAME = "ka-private"
TEAM_ID = "T0TEAM001"
REFUSAL = ("⛔ I can't verify who you are on slack (id {id}), so I can't take asks from you yet — "
           "an admin has to add you to the allowed users list and the principal map.")
ALL_TOKENS = (LISTED_TOKEN, UNLISTED_TOKEN, GCP_ACCESS)
# The notices the gateway posts in place of startTask for a turn it will not run,
# rendered as they reach Slack (test_not_started_grammar_matches_the_gateway_source).
NOT_STARTED_NOTICES = (
    "🚦 not started: 10 session workers are already running (cap 10). Wait for one to finish or `stop` one you started; "
    "an operator can raise the cap (A2A_MAX_SESSIONS / spec.harness.tuning.maxSessions).",
    "🚦 not started: 1 session worker is already running (cap 1). Wait for one to finish or `stop` one you started; "
    "an operator can raise the cap (A2A_MAX_SESSIONS / spec.harness.tuning.maxSessions).",
    "⚠️ not started: can't count the running session workers right now — try again in a moment",
    "⚠️ not started: could not mint this task's capability",
    "⚠️ not started: could not close the previous task on the bus; try again in a moment",
)
# Far above any timeout a test sets: a poll that passes it is unbounded.
SLEEP_BUDGET_SECONDS = 10_000


class FakeWorld:
    """Slack, the metadata server, Secret Manager, and a tiny gateway, in one object."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ts_counter = 100
        self.wall = lambda: 1700000000.0
        self.secrets = {LISTED_SECRET: LISTED_TOKEN, UNLISTED_SECRET: UNLISTED_TOKEN}
        self.tokens = {LISTED_TOKEN: LISTED_ID, UNLISTED_TOKEN: UNLISTED_ID}
        self.listed = {LISTED_ID}
        self.channels = {CHANNEL_ID: [], HOME_ID: []}
        self.dms = {}
        self.session_threads = set()
        self.notified = set()
        # Behaviour switches the tests flip.
        self.mode = "answer"  # answer | silent | refuse-all | answer-everyone | top-level | status-only | fail
        # Every Web API method called, in order: the harness must never post.
        self.methods = []
        # A user's team, for the preflight's same-workspace check.
        self.teams = {}
        # Turns the fake human has typed that land once reads reach their due count.
        self.pending_typed = []
        # Conversation reads that fail with a Slack error, by channel.
        self.channel_errors = {}
        # HTTP statuses the next conversation reads answer with, one per read. A negative
        # entry is never used up: every read from then on answers its absolute value.
        self.read_statuses = []
        # A task answer that lands this many reads later, with a fresh ts, the way a real
        # answer arrives after the user has moved on.
        self.answer_after_reads = 0
        self.deferred = []
        self.ignore_unmentioned_thread_replies = False
        self.steer_thread_replies = False
        self.slack_error_override = {}
        self.secret_error_body = None
        self.reply_delay_reads = 0
        self.reads = 0
        self.refusal = REFUSAL
        self.extra_members = []
        self.bot_ids = {BOT_ID}
        # Accounts users.info answers as deactivated.
        self.deleted_ids = set()
        self.metadata_token = GCP_ACCESS
        # The deliverable's text: the gateway posts the agent's result verbatim.
        self.answer_text = "PONG"
        # next_cursor on every users.list and conversations.list page: a workspace
        # larger than the lookups' page cap.
        self.list_cursor = ""
        # A token without groups:read: conversations.list refuses private_channel with missing_scope.
        self.lacks_groups_read = False
        self.conversation_list_types = []

    def next_ts(self):
        # Slack's ts is its clock at the post: it follows the fake wall clock, so a
        # message typed late in a test is still inside the harness's lookback.
        self.ts_counter += 1
        return f"{int(self.wall())}.{self.ts_counter:06d}"

    def dm_for(self, a, b):
        key = frozenset({a, b})
        if key not in self.dms:
            self.dms[key] = f"D0{len(self.dms):07d}"
            self.channels[self.dms[key]] = []
        return self.dms[key]

    def is_dm_with_bot(self, channel):
        return any(c == channel and BOT_ID in key for key, c in self.dms.items())

    def bot_post(self, channel, text, thread_ts=""):
        msg = {"type": "message", "user": BOT_ID, "bot_id": BOT_APP_BOT_ID, "text": text, "ts": self.next_ts(),
               "visible_at": self.reads + self.reply_delay_reads}
        if thread_ts:
            msg["thread_ts"] = thread_ts
        self.channels[channel].append(msg)
        return msg

    def finish(self, channel, status, text, reply_thread, terminal):
        """relay.go relayTerminal: the deliverable or failure is posted, then the status line is edited."""
        self.bot_post(channel, text, reply_thread)
        status["text"] = terminal

    def gateway_turn(self, channel, poster, msg):
        if self.mode == "silent":
            return
        text = msg["text"]
        thread_ts = msg.get("thread_ts", "")
        if self.is_dm_with_bot(channel):
            reply_thread = ""
        elif thread_ts and thread_ts != msg["ts"]:
            if not mentions_bot(text) and (
                    (channel, thread_ts) not in self.session_threads or self.ignore_unmentioned_thread_replies):
                return
            reply_thread = thread_ts
        elif mentions_bot(text):
            reply_thread = msg["ts"]
        else:
            return
        if self.mode == "top-level":
            reply_thread = ""
        admitted = (poster in self.listed or self.mode == "answer-everyone") and self.mode != "refuse-all"
        if not admitted:
            if poster not in self.notified:
                self.notified.add(poster)
                self.bot_post(channel, self.refusal.format(id=poster), reply_thread)
            return
        if self.steer_thread_replies and thread_ts and thread_ts != msg["ts"]:
            self.bot_post(channel, "✏️ steering sent — the worker picks it up at its next turn boundary", reply_thread)
            return
        if reply_thread:
            self.session_threads.add((channel, reply_thread))
        status = self.bot_post(channel, "⏳ submitted…", reply_thread)
        if self.mode == "status-only":
            return
        status["text"] = "⚙️ *working*"
        if self.mode == "fail":
            self.finish(channel, status, "❌ failed: the executor is down", reply_thread, "❌ *failed*")
            return
        if self.answer_after_reads:
            self.deferred.append((self.reads + self.answer_after_reads, channel, status, reply_thread))
            return
        self.finish(channel, status, self.answer_text, reply_thread, "✅ *completed*")

    def type_message(self, user, channel, text, thread_ts="", extra=None):
        """A message a person typed in Slack: it reaches the gateway as a turn candidate."""
        msg = {"type": "message", "user": user, "text": text, "ts": self.next_ts(), **(extra or {})}
        if extra and extra.get("user") is None and "user" in extra:
            del msg["user"]
        if thread_ts:
            msg["thread_ts"] = thread_ts
        self.channels[channel].append(msg)
        # The gateway's own filter (inbound() in a2a/gateway/slack.go): bot traffic is no turn.
        if not msg.get("bot_id") and msg.get("subtype", "") in harness.TURN_SUBTYPES and msg.get("user"):
            self.gateway_turn(channel, msg["user"], msg)
        return msg

    def materialize(self):
        typed = [t for t in self.pending_typed if t[0] <= self.reads]
        self.pending_typed = [t for t in self.pending_typed if t[0] > self.reads]
        for _, args in typed:
            self.type_message(*args)
        due = [d for d in self.deferred if d[0] <= self.reads]
        self.deferred = [d for d in self.deferred if d[0] > self.reads]
        for _, channel, status, thread in due:
            self.finish(channel, status, self.answer_text, thread, "✅ *completed*")

    def visible(self, msgs):
        return [{k: v for k, v in m.items() if k != "visible_at"} for m in msgs if m.get("visible_at", 0) <= self.reads]

    def slack(self, method, user, params):
        self.methods.append(method)
        if method in self.slack_error_override:
            return {"ok": False, "error": self.slack_error_override[method]}
        if method in ("conversations.history", "conversations.replies") and params.get("channel") in self.channel_errors:
            return {"ok": False, "error": self.channel_errors[params["channel"]]}
        if method == "auth.test":
            return {"ok": True, "user_id": user, "user": USER_NAMES.get(user, user.lower()), "team_id": self.teams.get(user, TEAM_ID)}
        if method == "users.list":
            return {"ok": True, "members": [
                {"id": LISTED_ID, "name": "listed", "is_bot": False},
                {"id": BOT_ID, "name": "kage", "is_bot": True, "profile": {"display_name": "kage"}},
                *self.extra_members,
            ], "response_metadata": {"next_cursor": self.list_cursor}}
        if method == "users.info":
            uid = params["user"]
            name = USER_NAMES.get(uid, uid.lower())
            return {"ok": True, "user": {"id": uid, "name": name, "is_bot": uid in self.bot_ids,
                                         "deleted": uid in self.deleted_ids, "profile": {"display_name": DISPLAY_NAMES.get(uid, "")}}}
        if method == "conversations.list":
            types = params.get("types", "public_channel").split(",")
            self.conversation_list_types.append(params.get("types", ""))
            if "private_channel" in types and self.lacks_groups_read:
                return {"ok": False, "error": "missing_scope", "needed": "groups:read"}
            channels = [{"id": CHANNEL_ID, "name": CHANNEL_NAME}, {"id": HOME_ID, "name": "home"}]
            if "private_channel" in types:
                channels.append({"id": PRIVATE_ID, "name": PRIVATE_NAME, "is_private": True})
            return {"ok": True, "channels": channels, "response_metadata": {"next_cursor": self.list_cursor}}
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": self.dm_for(user, params["users"])}}
        if method == "chat.postMessage":
            # Live run 2026-10-07: a user-token post made through an app carries the
            # app's bot_id and app_id, so the gateway drops it as bot traffic.
            msg = self.type_message(user, params["channel"], params["text"], params.get("thread_ts", ""),
                                    {"bot_id": "B0USERAPP", "app_id": "A0USERAPP"})
            return {"ok": True, "channel": params["channel"], "ts": msg["ts"]}
        if method == "conversations.history":
            self.reads += 1
            self.materialize()
            msgs = [m for m in self.channels[params["channel"]] if not m.get("thread_ts") or m["thread_ts"] == m["ts"]]
            if "oldest" in params:
                msgs = [m for m in msgs if float(m["ts"]) > float(params["oldest"])]
            return {"ok": True, "messages": list(reversed(self.visible(msgs)))}
        if method == "conversations.replies":
            self.reads += 1
            self.materialize()
            root = params["ts"]
            msgs = [m for m in self.channels[params["channel"]] if m["ts"] == root or m.get("thread_ts") == root]
            return {"ok": True, "messages": self.visible(msgs)}
        return {"ok": False, "error": "unknown_method"}


class Handler(BaseHTTPRequestHandler):
    world: FakeWorld = None

    def log_message(self, *args):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        world = self.world
        if self.path.startswith("/metadata/token"):
            if self.headers.get("Metadata-Flavor") != "Google":
                return self.reply(403, {"error": "missing flavor"})
            return self.reply(200, {"access_token": world.metadata_token, "expires_in": 3599})
        if self.path.startswith("/sm/projects/"):
            if self.headers.get("Authorization") != f"Bearer {GCP_ACCESS}":
                return self.reply(401, {"error": {"message": "unauthenticated"}})
            if world.secret_error_body is not None:
                return self.reply(403, world.secret_error_body)
            secret = self.path.split("/secrets/")[1].split("/")[0]
            if secret not in world.secrets:
                return self.reply(404, {"error": {"message": f"Secret [{secret}] not found"}})
            data = base64.b64encode((world.secrets[secret] + "\n").encode()).decode()
            return self.reply(200, {"payload": {"data": data}})
        return self.reply(404, {"error": "no route"})

    def do_POST(self):
        world = self.world
        length = int(self.headers.get("Content-Length", "0"))
        params = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        auth = self.headers.get("Authorization", "")
        user = world.tokens.get(auth.removeprefix("Bearer "))
        if not self.path.startswith("/api/"):
            return self.reply(404, {})
        if user is None:
            return self.reply(200, {"ok": False, "error": "invalid_auth"})
        method = self.path[len("/api/"):]
        with world.lock:
            status = 200
            if method in ("conversations.history", "conversations.replies") and world.read_statuses:
                status = world.read_statuses[0]
                if status > 0:
                    world.read_statuses.pop(0)
                else:
                    status = -status
            if status != 200:
                return self.reply(status, {"ok": False, "error": "upstream"})
            payload = world.slack(method, user, params)
        return self.reply(200, payload)


def mentions_bot(text):
    """slackMentionsBot in a2a/gateway/slack.go: the marker followed by > or |."""
    return re.search(rf"<@{BOT_ID}[>|]", text) is not None


# A Go interpreted string literal containing "not started:". It cannot hold a raw newline.
NOT_STARTED_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*not started:(?:[^"\\\n]|\\.)*)"')


class FakeHuman:
    """The person at the keyboard: types each turn the harness asks for, a few reads later.

    The switches make it get the turn wrong the ways a person (or a script) can.
    """

    def __init__(self, world):
        self.world = world
        self.turns = []
        # Reads after the prompt before the message lands: 1 is the first read.
        self.delay_reads = 1
        self.absent = False
        self.as_user = ""
        self.via_app = False
        self.drops_nonce = False
        self.plain_at = False
        # Render a picked mention in Slack's older <@U...|display> encoding.
        self.display_mention = False
        self.subtype = ""
        self.no_user = False
        # When set, the switches apply to this check's turn only; every other turn is typed right.
        self.only = None

    def __call__(self, turn):
        self.turns.append(turn)
        if self.only is not None and turn.check != self.only:
            user = turn.user_id
            text = turn.text.replace(f"@{BOT_NAME}", f"<@{BOT_ID}>")
            with self.world.lock:
                self.world.pending_typed.append((self.world.reads + 1, (user, turn.channel, text, turn.thread_ts, {})))
            return
        if self.absent:
            return
        text = turn.text
        if self.drops_nonce:
            text = text.replace(turn.nonce, "").strip()
        if not self.plain_at:
            # Picking the bot from Slack's @ list renders it as a link to its member id.
            text = text.replace(f"@{BOT_NAME}", f"<@{BOT_ID}|{BOT_NAME}>" if self.display_mention else f"<@{BOT_ID}>")
        extra = {}
        if self.via_app:
            extra.update({"bot_id": "B0USERAPP", "app_id": "A0USERAPP"})
        if self.subtype:
            extra["subtype"] = self.subtype
        if self.no_user:
            extra["user"] = None
        user = self.as_user or turn.user_id
        with self.world.lock:
            self.world.pending_typed.append((self.world.reads + self.delay_reads, (user, turn.channel, text, turn.thread_ts, extra)))


class Unbounded(BaseException):
    """A poll that outran every timeout. A BaseException, so harness.main's
    `except Exception` cannot turn it into an ERROR line and an exit code."""


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        if seconds <= 0 or len(self.slept) > SLEEP_BUDGET_SECONDS:
            raise Unbounded(f"a poll slept {seconds}s after {len(self.slept)} sleeps; it is not bounded")
        self.slept.append(seconds)
        self.now += seconds
        if self.now - 1000.0 > SLEEP_BUDGET_SECONDS:
            raise Unbounded("a poll kept sleeping past every timeout the test set")

    def wall(self):
        return 1700000000.0 + (self.now - 1000.0)


class HarnessTestCase(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld()
        handler = type("BoundHandler", (Handler,), {"world": self.world})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.endpoint_args = [
            "--slack-api-base", base + "/api/",
            "--metadata-token-url", base + "/metadata/token",
            "--secret-manager-base", base + "/sm/",
            "--project", PROJECT,
            "--poll-interval", "5",
            "--reply-timeout", "60",
            "--type-timeout", "60",
            "--quiet-window", "20",
            "--run-id", "testrun",
            "--bot-name", "kage",
        ]
        self.fake = FakeClock()
        self.world.wall = self.fake.wall
        self.human = FakeHuman(self.world)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_harness(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = harness.main([*self.endpoint_args, *extra], clock=self.fake.clock, sleep=self.fake.sleep, wall=self.fake.wall,
                                typist=self.human)
        text = out.getvalue() + err.getvalue()
        for value in ALL_TOKENS:
            self.assertNotIn(value, text, "a credential reached the output")
        return code, text

    def line(self, text, prefix):
        found = [ln for ln in text.splitlines() if ln.startswith(prefix)]
        self.assertTrue(found, f"no line starting {prefix!r} in:\n{text}")
        return found[0]

    def evidence(self, text, check):
        for ln in text.splitlines():
            if ln.startswith("EVIDENCE "):
                ev = json.loads(ln[len("EVIDENCE "):])
                if ev["check"] == check:
                    return ev
        self.fail(f"no evidence for {check} in:\n{text}")


class PreflightTest(HarnessTestCase):
    def test_preflight_checks_each_identity_and_posts_nothing(self):
        code, text = self.run_harness("--checks", "dm,unlisted")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertIn("PASS preflight-listed: U0LISTED1 (@Lisa Listed) is a person's account in team T0TEAM001", text)
        self.assertIn("PASS preflight-unlisted: U0UNLIST1 (@Una Unlisted) is a person's account in team T0TEAM001", text)
        ev = self.evidence(text, "preflight-listed")
        self.assertEqual((ev["user"], ev["team"], ev["is_bot"]), (LISTED_ID, TEAM_ID, False))
        self.assertNotIn("chat.postMessage", self.world.methods)

    def test_preflight_fails_when_a_test_token_belongs_to_a_bot_user(self):
        self.world.bot_ids.add(UNLISTED_ID)
        code, text = self.run_harness("--checks", "dm,unlisted", "--keep-going")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertIn("U0UNLIST1 is a bot user", self.line(text, "FAIL preflight-unlisted"))
        # A failed preflight stops the run even under --keep-going.
        self.assertNotIn("TYPE ", text)
        self.assertIn("SUMMARY pass=1 fail=1", text)

    def test_preflight_fails_when_a_test_account_is_deactivated(self):
        self.world.deleted_ids.add(UNLISTED_ID)
        code, text = self.run_harness("--checks", "dm,unlisted", "--keep-going")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertIn("U0UNLIST1 is deactivated", self.line(text, "FAIL preflight-unlisted"))
        self.assertTrue(self.evidence(text, "preflight-unlisted")["deleted"])
        self.assertNotIn("TYPE ", text)

    def test_preflight_fails_when_the_users_are_in_different_workspaces(self):
        self.world.teams[UNLISTED_ID] = "T0OTHER01"
        code, text = self.run_harness("--checks", "unlisted")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertIn("team T0OTHER01, not the listed user's team T0TEAM001", self.line(text, "FAIL preflight-unlisted"))

    def test_a_slack_error_in_the_preflight_is_its_fail(self):
        original = self.world.slack

        def users_info_fails_for_people(method, user, params):
            if method == "users.info" and params.get("user") == LISTED_ID:
                return {"ok": False, "error": "internal_error"}
            return original(method, user, params)

        self.world.slack = users_info_fails_for_people
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertIn("error: Slack users.info failed: internal_error", self.line(text, "FAIL preflight-listed"))
        self.assertIn("SUMMARY pass=0 fail=1", text)
        self.assertNotIn("setup failed", text)

    def test_preflight_runs_for_the_unlisted_token_only_when_needed(self):
        _, text = self.run_harness("--checks", "dm")
        self.assertNotIn("preflight-unlisted", text)


class TypedTurnTest(HarnessTestCase):
    """Each user turn is typed by a person: the TYPE line, the wait, and the typed message's checks."""

    TYPED_CHECKS = (("dm", ()), ("mention", ()), ("thread", ()), ("unlisted", ()), ("restart", ()),
                    ("unlisted", ("--unlisted-via", "mention")))

    def fresh(self, check):
        """A new fake Slack for one subtest, whose human gets only check's turn wrong."""
        self.tearDown()
        self.setUp()
        self.human.only = check

    def run_check(self, check, *extra):
        checks = "mention,thread" if check == "thread" else check
        return self.run_harness("--checks", checks, *extra)

    def type_line(self, text, check):
        lines = [ln for ln in text.splitlines() if ln.startswith("TYPE ") and f"slc-{check}-" in ln]
        self.assertEqual(len(lines), 1, text)
        return lines[0]

    def test_each_check_prints_one_type_line_and_passes_on_the_typed_turn(self):
        for check, extra in self.TYPED_CHECKS:
            with self.subTest(check=check, extra=extra):
                self.fresh(check)
                code, text = self.run_check(check, *extra)
                self.assertEqual(code, harness.EXIT_OK, text)
                turn = self.human.turns[-1]
                line = self.type_line(text, check)
                # The exact text, nonce included, ends the line: copy it as is.
                self.assertTrue(line.endswith(": " + turn.text), line)
                self.assertIn(turn.nonce, turn.text)
                who = "@Una Unlisted (U0UNLIST1)" if check == "unlisted" else "@Lisa Listed (U0LISTED1)"
                self.assertIn(f"TYPE within 60s as {who} in ", line)
                ev = self.evidence(text, check)
                self.assertEqual(ev["nonce"], turn.nonce)
                self.assertEqual(ev["typed_by"], turn.user_id)
                self.assertIn(f"PASS {check}:", text)
                self.assertNotIn("chat.postMessage", self.world.methods)

    def test_the_type_lines_say_where_to_type(self):
        code, text = self.run_harness("--checks", "dm,mention,thread")
        self.assertEqual(code, harness.EXIT_OK, text)
        dm, mention, thread = self.human.turns
        self.assertIn(f"in your DM with @{BOT_NAME} ({dm.channel}): ", self.type_line(text, "dm"))
        self.assertIn(f"in #{CHANNEL_NAME} ({CHANNEL_ID}), top level, picking @{BOT_NAME} from Slack's list: @{BOT_NAME} ",
                      self.type_line(text, "mention"))
        root = self.evidence(text, "mention")["sent_ts"]
        self.assertEqual(thread.thread_ts, root)
        self.assertIn(f"in the thread under your mention in #{CHANNEL_NAME} ({CHANNEL_ID}), thread ts={root}, "
                      "without mentioning the bot: ", self.type_line(text, "thread"))
        self.assertNotIn(f"@{BOT_NAME}", thread.text)

    def test_the_wait_polls_until_the_typed_message_lands(self):
        self.human.delay_reads = 4
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertEqual(self.fake.slept, [5, 5, 5])

    def test_a_typed_message_with_a_bot_id_fails(self):
        for check, extra in self.TYPED_CHECKS:
            with self.subTest(check=check, extra=extra):
                self.fresh(check)
                self.human.via_app = True
                code, text = self.run_check(check, *extra)
                self.assertEqual(code, harness.EXIT_FAIL, text)
                line = self.line(text, f"FAIL {check}")
                self.assertIn("carries bot_id=B0USERAPP", line)
                self.assertIn("app_id=A0USERAPP", line)
                self.assertIn("a message a person types has neither", line)
                self.assertNotIn("PASS " + check, text)
                ev = self.evidence(text, check)
                self.assertEqual((ev["typed_bot_id"], ev["typed_app_id"]), ("B0USERAPP", "A0USERAPP"))

    def test_a_bot_id_fails_at_once_without_waiting_for_the_bot(self):
        self.human.via_app = True
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertEqual(self.fake.slept, [])

    def test_a_message_from_the_wrong_user_fails(self):
        for check, extra in self.TYPED_CHECKS:
            with self.subTest(check=check, extra=extra):
                self.fresh(check)
                self.human.as_user = "U0SOMEONE"
                self.world.listed.add("U0SOMEONE")
                code, text = self.run_check(check, *extra)
                self.assertEqual(code, harness.EXIT_FAIL, text)
                self.assertIn("was typed by U0SOMEONE, not", self.line(text, f"FAIL {check}"))

    def test_a_message_without_the_nonce_times_out(self):
        for check, extra in self.TYPED_CHECKS:
            with self.subTest(check=check, extra=extra):
                self.fresh(check)
                self.human.drops_nonce = True
                code, text = self.run_check(check, *extra)
                self.assertEqual(code, harness.EXIT_FAIL, text)
                turn = self.human.turns[-1]
                line = self.line(text, f"FAIL {check}")
                self.assertIn(f"no message containing {turn.nonce} within 60.0s", line)

    def test_nobody_typing_fails_after_a_bounded_wait(self):
        self.human.absent = True
        code, text = self.run_harness("--checks", "dm", "--type-timeout", "45")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        turn = self.human.turns[0]
        self.assertIn(f"no message containing {turn.nonce} within 45.0s", self.line(text, "FAIL dm"))
        self.assertIn("TYPE within 45s as", text)
        self.assertAlmostEqual(sum(self.fake.slept), 45.0)

    def test_a_mention_typed_as_plain_text_fails(self):
        self.human.plain_at = True
        code, text = self.run_harness("--checks", "mention")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertIn(f"does not mention the bot (no <@{BOT_ID}> in its text)", self.line(text, "FAIL mention"))

    def test_a_mention_in_the_display_encoding_passes(self):
        # The gateway takes <@U...|display> as a mention (slackMentionsBot), so the check does too.
        self.human.display_mention = True
        code, text = self.run_harness("--checks", "mention,thread")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertIn("PASS mention", text)

    def test_a_bot_name_typed_with_its_at_is_the_bot(self):
        for extra in ((), ("--bot-user-id", BOT_ID)):
            with self.subTest(extra=extra):
                self.tearDown()
                self.setUp()
                code, text = self.run_harness("--checks", "mention", "--bot-name", f"@{BOT_NAME}", *extra)
                self.assertEqual(code, harness.EXIT_OK, text)
                self.assertNotIn(f"@@{BOT_NAME}", text)
                self.assertIn(f"picking @{BOT_NAME} from Slack's list", self.type_line(text, "mention"))

    def test_a_typed_message_the_gateway_would_not_take_as_a_turn_fails(self):
        self.human.subtype = "bot_message"
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertIn("its subtype 'bot_message' is not a turn", self.line(text, "FAIL dm"))
        self.fresh("dm")
        self.human.no_user = True
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertIn("it has no user field", self.line(text, "FAIL dm"))

    def test_unlisted_repeat_is_a_second_typed_turn(self):
        code, text = self.run_harness("--checks", "unlisted", "--unlisted-repeat")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertEqual([t.check for t in self.human.turns], ["unlisted", "unlisted-repeat"])
        self.assertIn("PASS unlisted-repeat: no reply to the second message in 20.0s", text)

    def test_the_type_line_is_redacted(self):
        code, text = self.run_harness("--checks", "dm", "--prompt", f"say {LISTED_TOKEN}")
        self.assertIn(harness.REDACTED, self.type_line(text, "dm"))

    def test_type_timeout_is_a_finite_flag_with_a_default(self):
        self.assertEqual(harness.parse_args(["--bot-name", "kage"]).type_timeout, harness.DEFAULT_TYPE_TIMEOUT_SECONDS)
        self.assertEqual(harness.DEFAULT_TYPE_TIMEOUT_SECONDS, 300)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            harness.parse_args(["--type-timeout", "inf", "--bot-name", "kage"])


class ListedChecksTest(HarnessTestCase):
    def test_dm_mention_thread_pass(self):
        code, text = self.run_harness("--checks", "dm,mention,thread")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertIn("PASS dm:", text)
        mention = self.evidence(text, "mention")
        thread = self.evidence(text, "thread")
        self.assertEqual(mention["reply_thread_ts"], mention["sent_ts"])
        self.assertEqual(thread["thread_ts"], mention["sent_ts"])
        self.assertEqual(thread["reply_thread_ts"], mention["sent_ts"])
        self.assertIn("SUMMARY pass=4 fail=0", text)

    def test_dm_fails_on_silence_after_a_bounded_poll(self):
        self.world.mode = "silent"
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("no reply from the bot within 60.0s", self.line(text, "FAIL dm"))
        # Bounded: the fake clock advanced by the timeout and no further.
        self.assertAlmostEqual(sum(self.fake.slept), 60.0)
        self.assertTrue(all(s <= 5 for s in self.fake.slept))

    def test_dm_reply_after_a_few_polls_passes(self):
        self.world.reply_delay_reads = 3
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertGreaterEqual(len(self.fake.slept), 2)

    def test_dm_fails_when_the_listed_user_is_refused(self):
        self.world.mode = "refuse-all"
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("got the refusal notice", self.line(text, "FAIL dm"))

    def test_wait_answer_waits_past_the_status_line(self):
        code, text = self.run_harness("--checks", "dm", "--wait-answer")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertEqual(self.evidence(text, "dm")["reply_text"], "PONG")

    def test_wait_answer_fails_when_only_the_status_line_arrives(self):
        self.world.mode = "status-only"
        code, text = self.run_harness("--checks", "dm", "--wait-answer")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("no answer (--wait-answer)", self.line(text, "FAIL dm"))
        self.assertEqual(self.evidence(text, "dm")["last_bot_text"], "⏳ submitted…")

    def test_wait_answer_fails_on_a_failed_task(self):
        self.world.mode = "fail"
        code, text = self.run_harness("--checks", "dm", "--wait-answer")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("the task the turn started failed", text)

    def test_an_answer_that_opens_with_a_gateway_icon_is_the_answer(self):
        # The gateway posts the agent's result verbatim, so its first character is the
        # agent's to choose. Only the status line's state says the answer is in.
        for answer in ("✅ PONG", "⚠️ PONG, with a caveat", "ℹ️ PONG", "❓ PONG?", "❌ PONG", "🛑 PONG", "⏳ PONG"):
            with self.subTest(answer=answer):
                self.world.answer_text = answer
                code, text = self.run_harness("--checks", "dm,mention,thread", "--wait-answer")
                self.assertEqual(code, harness.EXIT_OK, text)
                for check in ("dm", "mention", "thread"):
                    self.assertEqual(self.evidence(text, check)["reply_text"], answer, check)
                    self.assertEqual(self.evidence(text, check)["reply_kind"], harness.KIND_ANSWER, check)

    def test_thread_settles_on_the_status_line_whatever_the_answer_says(self):
        self.world.answer_text = "✅ PONG"
        code, text = self.run_harness("--checks", "mention,thread")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertEqual(sum(self.fake.slept), 0, "the settle wait sat out a timeout on a finished task")

    def test_a_status_card_before_the_turn_is_not_the_answer(self):
        # healActiveTask (gateway.go) posts formatTaskStatus's card ahead of the new
        # turn's status line: it is the gateway's, not the answer.
        world = self.world
        original = world.gateway_turn

        def card_then_turn(channel, poster, msg):
            world.bot_post(channel, "🔎 task `t0` is *completed*", "")
            original(channel, poster, msg)

        world.gateway_turn = card_then_turn
        code, text = self.run_harness("--checks", "dm", "--wait-answer")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertEqual(self.evidence(text, "dm")["reply_text"], "PONG")

    def test_a_status_card_before_the_turn_is_not_the_first_reply(self):
        # The same card under the default reading, read before the new turn's status
        # line is visible: it is not the reply, and the turn's reply is its status line.
        world = self.world
        original = world.gateway_turn

        def card_then_turn(channel, poster, msg):
            world.bot_post(channel, "🔎 task `t0` is *completed*", "")
            world.reply_delay_reads = 2
            original(channel, poster, msg)
            world.reply_delay_reads = 0

        world.gateway_turn = card_then_turn
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertEqual(self.evidence(text, "dm")["reply_kind"], harness.KIND_TASK_LINE)
        self.assertNotIn("🔎", self.evidence(text, "dm")["reply_text"])

    def test_wait_answer_waits_for_the_status_line_to_turn_terminal(self):
        # The answer is posted before the status line's terminal edit; until that
        # edit lands the task is still running and nothing after the line is final.
        world = self.world
        world.mode = "status-only"
        original = world.gateway_turn

        def answer_without_terminal(channel, poster, msg):
            original(channel, poster, msg)
            world.bot_post(channel, "PONG", "")

        world.gateway_turn = answer_without_terminal
        code, text = self.run_harness("--checks", "dm", "--wait-answer")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertIn("no answer (--wait-answer)", self.line(text, "FAIL dm"))

    def test_mention_fails_when_the_reply_is_not_threaded(self):
        self.world.mode = "top-level"
        code, text = self.run_harness("--checks", "mention")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("no reply from the bot", self.line(text, "FAIL mention"))

    def test_thread_fails_without_a_root_and_first_fail_stops(self):
        self.world.mode = "silent"
        code, text = self.run_harness("--checks", "mention,thread")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("FAIL mention", text)
        self.assertNotIn("thread:", text)

    def test_keep_going_runs_thread_after_a_failed_mention(self):
        self.world.mode = "silent"
        code, text = self.run_harness("--checks", "mention,thread", "--keep-going")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("no thread to reply in", self.line(text, "FAIL thread"))
        self.assertIn("SUMMARY pass=1 fail=2", text)

    def test_thread_fails_when_the_bot_ignores_the_reply(self):
        code, text = self.run_harness("--checks", "mention")
        root = self.evidence(text, "mention")["sent_ts"]
        self.world.session_threads.clear()
        code, text = self.run_harness("--checks", "thread", "--thread-ts", root)
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("FAIL thread", text)

    def test_thread_is_not_fooled_by_the_mention_answer_landing_late(self):
        # The mention passes on its status line; its answer arrives later. A gateway
        # that drops the unmentioned follow-up must fail the thread check, not pass it
        # on the mention's late answer.
        self.world.answer_after_reads = 3
        self.world.ignore_unmentioned_thread_replies = True
        code, text = self.run_harness("--checks", "mention,thread", "--keep-going")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("PASS mention", text)
        self.assertIn("no reply from the bot", self.line(text, "FAIL thread"))

    def test_thread_waits_for_the_mention_task_to_settle(self):
        self.world.answer_after_reads = 3
        code, text = self.run_harness("--checks", "mention,thread")
        self.assertEqual(code, harness.EXIT_OK, text)
        thread = self.evidence(text, "thread")
        self.assertEqual(thread["settled_kind"], harness.KIND_ANSWER)
        self.assertGreater(harness.ts_value(thread["reply_ts"]), harness.ts_value(thread["sent_ts"]))

    def test_thread_fails_when_the_follow_up_is_taken_as_a_steer(self):
        self.world.steer_thread_replies = True
        code, text = self.run_harness("--checks", "mention,thread")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("taken as a steer", self.line(text, "FAIL thread"))

    def test_thread_fails_on_a_steer_under_wait_answer_too(self):
        # A steering gateway posts the ack and then the running task's answer; under
        # --wait-answer the answer must not stand in for a new turn.
        world = self.world
        world.steer_thread_replies = True
        original = world.bot_post

        def ack_then_answer(channel, text, thread_ts=""):
            msg = original(channel, text, thread_ts)
            if text.startswith("✏️"):
                original(channel, "PONG", thread_ts)
            return msg

        world.bot_post = ack_then_answer
        code, text = self.run_harness("--checks", "mention,thread", "--wait-answer")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("taken as a steer", self.line(text, "FAIL thread"))

    def test_a_warning_ahead_of_the_status_line_is_not_the_reply(self):
        world = self.world
        original = world.gateway_turn

        def warn_then_turn(channel, poster, msg):
            world.bot_post(channel, "⚠️ task `t` has produced nothing on its event stream in 5m, so this conversation is released", "")
            original(channel, poster, msg)

        world.gateway_turn = warn_then_turn
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_OK, text)
        # The status line, read after the task finished: the same message, edited.
        self.assertEqual(self.evidence(text, "dm")["reply_text"], "✅ *completed*")
        self.assertEqual(self.evidence(text, "dm")["reply_kind"], harness.KIND_TASK_LINE)

    def post_instead_of_a_task(self, notice):
        """The gateway answers the turn with notice and starts no task: no status line follows."""
        world = self.world

        def notice_only(channel, poster, msg):
            thread = "" if world.is_dm_with_bot(channel) else msg.get("thread_ts", msg["ts"])
            world.bot_post(channel, notice, thread)

        world.gateway_turn = notice_only

    def test_a_turn_the_gateway_did_not_start_fails_at_once(self):
        # refuseAtSessionCap (spawn.go) and the other "not started:" refusals answer the
        # turn in place of startTask, so no status line ever follows them.
        for notice in NOT_STARTED_NOTICES:
            for check, extra in (("dm", ()), ("restart", ()), ("mention", ()), ("dm", ("--wait-answer",))):
                with self.subTest(notice=notice, check=check, extra=extra):
                    self.post_instead_of_a_task(notice)
                    slept = len(self.fake.slept)
                    code, text = self.run_harness("--checks", check, *extra)
                    self.assertEqual(code, harness.EXIT_FAIL, text)
                    line = self.line(text, f"FAIL {check}")
                    self.assertIn("the gateway did not start a task", line)
                    self.assertIn(notice[:40], line)
                    self.assertEqual(self.evidence(text, check)["reply_kind"], harness.KIND_NOT_STARTED)
                    self.assertEqual(self.fake.slept[slept:], [], "a refusal sat out the reply timeout")

    def test_a_notice_with_no_status_line_after_it_fails_and_quotes_it(self):
        # Not the status line and not a refusal the harness knows: under the default
        # reading it is no reply, and the FAIL names what the bot did post.
        for notice in ("🤷 nothing is running", "ℹ️ this conversation is already a session",
                       "⚠️ task `t` has produced nothing on its event stream in 5m"):
            with self.subTest(notice=notice):
                self.post_instead_of_a_task(notice)
                code, text = self.run_harness("--checks", "dm")
                self.assertEqual(code, harness.EXIT_FAIL, text)
                line = self.line(text, "FAIL dm")
                self.assertIn("no status line", line)
                self.assertIn(notice, line)
                self.assertEqual(self.evidence(text, "dm")["last_bot_text"], notice)

    def test_the_quoted_notice_is_redacted(self):
        self.post_instead_of_a_task(f"🤷 echoing {LISTED_TOKEN}")
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertNotIn(LISTED_TOKEN, text)
        self.assertIn(harness.REDACTED, self.line(text, "FAIL dm"))

    def test_dm_fails_on_a_steer(self):
        world = self.world

        def steer(channel, poster, msg):
            if world.is_dm_with_bot(channel):
                world.bot_post(channel, "✏️ steering sent — the worker picks it up", "")

        world.gateway_turn = steer
        code, text = self.run_harness("--checks", "dm", "--wait-answer")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("taken as a steer", self.line(text, "FAIL dm"))

    def test_thread_fails_when_the_first_task_never_settles(self):
        self.world.mode = "status-only"
        code, text = self.run_harness("--checks", "mention,thread", "--keep-going")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("did not settle", self.line(text, "FAIL thread"))

    def test_a_slack_error_in_a_check_is_its_fail_and_keep_going_carries_on(self):
        # The listed user cannot read the channel: dm passes, and the mention's wait
        # for the typed message raises not_in_channel.
        self.world.channel_errors[CHANNEL_ID] = "not_in_channel"
        code, text = self.run_harness("--checks", "dm,mention,thread", "--keep-going")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertIn("PASS dm:", text)
        self.assertIn("error: Slack conversations.history failed: not_in_channel", self.line(text, "FAIL mention"))
        self.assertIn("no thread to reply in", self.line(text, "FAIL thread"))
        self.assertIn("SUMMARY pass=2 fail=2", text)
        self.assertNotIn("setup failed", text)
        code, text = self.run_harness("--checks", "dm,mention,thread")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertNotIn("thread:", text)
        self.assertIn("SUMMARY pass=2 fail=1", text)

    def test_a_transient_read_error_inside_a_wait_is_read_again(self):
        # A 502, then a 429 that outlasts the client's own retries, while the person
        # types the turn: the next tick reads again, and the turn is found.
        self.world.read_statuses = [502, *([429] * (harness.RATE_LIMIT_MAX_RETRIES + 1))]
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertIn("PASS dm:", text)

    def test_reads_failing_until_the_deadline_fail_naming_the_last_error(self):
        self.world.read_statuses = [-503]
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertIn("error: nothing within 60s; the last read failed: Slack conversations.history failed: http_503",
                      self.line(text, "FAIL dm"))

    def test_thread_ts_on_a_thread_the_gateway_never_answered_fails_at_once(self):
        root = self.world.type_message(LISTED_ID, CHANNEL_ID, "a plain message nobody mentioned the bot in")["ts"]
        code, text = self.run_harness("--checks", "thread", "--thread-ts", root)
        self.assertEqual(code, harness.EXIT_FAIL, text)
        line = self.line(text, "FAIL thread")
        self.assertIn(f"thread {root} holds no gateway status line", line)
        self.assertIn("takes an unmentioned reply only in a thread it started a task in", line)
        self.assertNotIn("TYPE ", text)
        # At once: no settle wait.
        self.assertEqual(self.fake.slept, [])

    def test_restart_is_a_dm_under_its_own_name(self):
        code, text = self.run_harness("--checks", "restart")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertIn("PASS restart:", text)
        self.assertEqual(self.evidence(text, "restart")["author"], LISTED_ID)


class UnlistedTest(HarnessTestCase):
    def test_unlisted_dm_gets_the_notice(self):
        code, text = self.run_harness("--checks", "unlisted", "--unlisted-repeat")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertIn("PASS unlisted: refusal notice", text)
        self.assertIn("PASS unlisted-repeat", text)

    def test_unlisted_mention_gets_the_notice_in_the_thread(self):
        code, text = self.run_harness("--checks", "unlisted", "--unlisted-via", "mention")
        self.assertEqual(code, harness.EXIT_OK, text)
        ev = self.evidence(text, "unlisted")
        self.assertEqual(ev["reply_thread_ts"], ev["sent_ts"])

    def test_unlisted_fails_when_answered(self):
        self.world.mode = "answer-everyone"
        code, text = self.run_harness("--checks", "unlisted")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("was answered", self.line(text, "FAIL unlisted"))

    def test_unlisted_silence_fails_unless_accepted(self):
        self.world.notified.add(UNLISTED_ID)
        code, text = self.run_harness("--checks", "unlisted")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("once per sender per gateway process", self.line(text, "FAIL unlisted"))
        code, text = self.run_harness("--checks", "unlisted", "--refusal-silence-ok")
        self.assertEqual(code, harness.EXIT_OK, text)

    def test_unlisted_repeat_fails_when_the_notice_repeats(self):
        world = self.world
        original = world.gateway_turn

        def forgetful(channel, poster, msg):
            world.notified.discard(poster)
            original(channel, poster, msg)

        world.gateway_turn = forgetful
        code, text = self.run_harness("--checks", "unlisted", "--unlisted-repeat")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("FAIL unlisted-repeat", text)

    def test_unlisted_checks_count_any_bot_message_as_a_reply(self):
        # The unlisted checks pass on the refusal or on silence, so a notice that is not
        # the status line must still count as the bot answering the unlisted user.
        world = self.world
        original = world.gateway_turn

        def notice_on_the_second(channel, poster, msg):
            if harness.CHECK_UNLISTED_REPEAT in msg["text"]:
                world.bot_post(channel, "🤷 nothing is running", "")
            else:
                original(channel, poster, msg)

        world.gateway_turn = notice_on_the_second
        code, text = self.run_harness("--checks", "unlisted", "--unlisted-repeat")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertIn(f"the bot replied to the second message ({harness.KIND_NOTICE})", self.line(text, "FAIL unlisted-repeat"))
        world.notified.clear()
        world.gateway_turn = lambda channel, poster, msg: world.bot_post(channel, "🤷 nothing is running", "")
        code, text = self.run_harness("--checks", "unlisted")
        self.assertEqual(code, harness.EXIT_FAIL, text)
        self.assertIn(f"answered ({harness.KIND_NOTICE})", self.line(text, "FAIL unlisted"))

    def test_unlisted_repeat_by_mention_reaches_the_gateway(self):
        world = self.world
        original = world.gateway_turn

        def forgetful(channel, poster, msg):
            world.notified.discard(poster)
            original(channel, poster, msg)

        world.gateway_turn = forgetful
        code, text = self.run_harness("--checks", "unlisted", "--unlisted-via", "mention", "--unlisted-repeat")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("FAIL unlisted-repeat", text)

    def test_unlisted_repeat_by_mention_says_to_pick_the_bot(self):
        code, text = self.run_harness("--checks", "unlisted", "--unlisted-via", "mention", "--unlisted-repeat")
        self.assertEqual(code, harness.EXIT_OK, text)
        repeat = [ln for ln in text.splitlines() if ln.startswith("TYPE ") and "slc-unlisted-repeat-" in ln]
        self.assertEqual(len(repeat), 1, text)
        self.assertIn("thread ts=", repeat[0])
        self.assertIn(f", picking @{BOT_NAME} from Slack's list: @{BOT_NAME} ", repeat[0])

    def test_unlisted_fails_when_the_notice_does_not_name_the_sender(self):
        self.world.refusal = REFUSAL.replace("(id {id})", "(id someone)")
        code, text = self.run_harness("--checks", "unlisted")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("does not name the unlisted user's member id", self.line(text, "FAIL unlisted"))

    def test_same_user_on_both_tokens_is_a_setup_error(self):
        self.world.tokens[UNLISTED_TOKEN] = LISTED_ID
        code, text = self.run_harness("--checks", "unlisted")
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("belong to the same Slack user", text)


class HomeTest(HarnessTestCase):
    def test_home_passes_on_a_bot_post(self):
        self.world.bot_post(HOME_ID, "good morning from kage")
        code, text = self.run_harness("--checks", "home", "--home-channel", "home", "--home-since", "1600000000",
                                      "--home-match", "morning")
        self.assertEqual(code, harness.EXIT_OK, text)

    def test_home_times_out(self):
        code, text = self.run_harness("--checks", "home", "--home-channel", HOME_ID, "--home-timeout", "30")
        self.assertEqual(code, harness.EXIT_FAIL)
        self.assertIn("no bot post in C0HOME001 within 30.0s", text)
        self.assertAlmostEqual(sum(self.fake.slept), 30.0)


class SetupAndRedactionTest(HarnessTestCase):
    def test_bot_lookup_by_flag_must_be_a_bot(self):
        code, text = self.run_harness("--checks", "dm", "--bot-user-id", LISTED_ID)
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("is not a bot user", text)

    def test_bot_lookup_by_unknown_name(self):
        code, text = self.run_harness("--checks", "dm", "--bot-name", "nobody")
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("no bot user named 'nobody'", text)

    def test_two_bots_with_the_name_are_a_setup_error(self):
        self.world.extra_members.append({"id": "U0BOTTWO1", "name": "kage", "is_bot": True})
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("more than one bot user is named 'kage' (U0BOTKAGE, U0BOTTWO1)", text)

    def test_the_bot_cannot_be_a_test_user(self):
        self.world.bot_ids.add(LISTED_ID)
        code, text = self.run_harness("--checks", "dm", "--bot-user-id", LISTED_ID)
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("the bot user id is one of the test users", text)

    def test_a_token_that_is_not_a_user_token_is_refused(self):
        self.world.secrets[LISTED_SECRET] = "not-a-slack-token"
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("does not hold a Slack user token (xoxp-)", text)

    def test_a_metadata_answer_without_a_token_is_refused(self):
        self.world.metadata_token = ""
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("answered without an access token", text)

    def test_an_unknown_channel_name_is_a_setup_error(self):
        code, text = self.run_harness("--checks", "dm", "--channel", "nope")
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("no channel named #nope", text)

    def test_a_private_channel_resolves_by_name_with_groups_read(self):
        code, text = self.run_harness("--checks", "home", "--channel", PRIVATE_NAME, "--home-channel", PRIVATE_NAME,
                                      "--home-timeout", "5")
        self.assertIn(f"channel={PRIVATE_ID}", text)
        self.assertEqual(self.world.conversation_list_types, ["public_channel,private_channel"] * 2)

    def test_without_groups_read_the_lookup_falls_back_to_public_channels(self):
        # Live run 2026-10-07: the minted user tokens carry channels:read but not groups:read.
        self.world.lacks_groups_read = True
        self.world.bot_post(HOME_ID, "good morning")
        code, text = self.run_harness("--checks", "home", "--home-channel", "home", "--home-since", "1600000000")
        self.assertEqual(code, harness.EXIT_OK, text)
        self.assertIn(f"channel={CHANNEL_ID}", text)
        # --channel, then --home-channel: each tries both types once, then public only.
        self.assertEqual(self.world.conversation_list_types,
                         ["public_channel,private_channel", "public_channel"] * 2)

    def test_without_groups_read_a_private_channel_name_says_what_it_needs(self):
        self.world.lacks_groups_read = True
        code, text = self.run_harness("--checks", "dm", "--channel", PRIVATE_NAME)
        self.assertEqual(code, harness.EXIT_SETUP, text)
        self.assertIn(f"no public channel named #{PRIVATE_NAME} visible to the listed user, and its token lacks groups:read", text)
        self.assertIn("a private channel needs groups:read on the token or the channel's C or G id", text)
        self.assertEqual(self.world.conversation_list_types, ["public_channel,private_channel", "public_channel"])

    def test_another_conversations_list_error_is_reported_as_is(self):
        self.world.slack_error_override["conversations.list"] = "invalid_auth"
        code, text = self.run_harness("--checks", "dm", "--channel", CHANNEL_NAME)
        self.assertEqual(code, harness.EXIT_SETUP, text)
        self.assertIn("Slack conversations.list failed: invalid_auth", text)

    def test_a_dm_id_is_refused_for_the_channel(self):
        code, text = self.run_harness("--checks", "dm", "--channel", "D0ABCDEF1")
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("D0ABCDEF1 is a DM, not a channel", text)
        self.assertNotIn("preflight-listed", text)
        self.assertEqual(harness.resolve_channel(None, "G0PRIVATE1"), "G0PRIVATE1")

    def test_a_non_object_error_body_keeps_the_status(self):
        self.world.secret_error_body = ["denied"]
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("Secret Manager refused test-project/slack-test-user-listed (HTTP 403): [\"denied\"]", text)
        self.assertNotIn("AttributeError", text)
        self.assertEqual(harness._error_detail(b"null"), "null")

    def test_an_unbounded_poll_is_not_swallowed_by_main(self):
        self.world.mode = "silent"
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Unbounded):
            harness.main([*self.endpoint_args, "--checks", "dm", "--type-timeout", "1e9"],
                         clock=self.fake.clock, sleep=self.fake.sleep, wall=self.fake.wall)

    def test_bot_token_in_the_secret_is_refused_without_echoing_it(self):
        self.world.secrets[LISTED_SECRET] = "xoxb-9999-a-bot-token-value"
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("holds a bot token, not a user token", text)
        self.assertNotIn("xoxb-9999-a-bot-token-value", text)

    def test_secret_manager_error_echoing_credentials_is_scrubbed(self):
        self.world.secret_error_body = {"error": {"message": f"denied for {GCP_ACCESS} and {LISTED_TOKEN}"}}
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("Secret Manager refused", text)
        self.assertIn(harness.REDACTED, text)

    def test_slack_error_echoing_the_token_is_scrubbed(self):
        self.world.slack_error_override["auth.test"] = f"token_revoked:{LISTED_TOKEN}"
        code, text = self.run_harness("--checks", "dm")
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("Slack auth.test failed: token_revoked:[redacted]", text)

    def test_unexpected_exception_carrying_the_token_is_scrubbed(self):
        class Exploding:
            def request(self, method, url, headers, body):
                raise ValueError(f"boom with {headers.get('Authorization', '')} {LISTED_TOKEN}")

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = harness.main([*self.endpoint_args, "--checks", "dm"], transport=Exploding(),
                                clock=self.fake.clock, sleep=self.fake.sleep, wall=self.fake.wall)
        self.assertEqual(code, harness.EXIT_SETUP)
        self.assertIn("ERROR unexpected ValueError", out.getvalue())
        self.assertNotIn(LISTED_TOKEN, out.getvalue())

    def test_a_token_with_a_control_character_is_refused_by_name(self):
        for inner in ("\n", "\r", "\t", " ", "\x00", "\u2028"):
            with self.subTest(inner=repr(inner)):
                self.world.secrets[LISTED_SECRET] = f"xoxp-1111-head{inner}tail-secret-part"
                code, text = self.run_harness("--checks", "dm")
                self.assertEqual(code, harness.EXIT_SETUP, text)
                self.assertIn("the listed token source slack-test-user-listed holds a whitespace or control character", text)
                self.assertNotIn("tail-secret-part", text)
                self.assertNotIn("1111-head", text)

    def test_page_caps_say_the_lookup_was_cut_off(self):
        self.world.list_cursor = "more"
        code, text = self.run_harness("--checks", "dm", "--bot-name", "nobody")
        self.assertEqual(code, harness.EXIT_SETUP, text)
        self.assertIn(f"stopped after {harness.LIST_MAX_PAGES} pages of users.list", text)
        self.assertIn("pass --bot-user-id", text)
        code, text = self.run_harness("--checks", "dm", "--bot-user-id", BOT_ID, "--channel", "nope")
        self.assertEqual(code, harness.EXIT_SETUP, text)
        self.assertIn(f"stopped after {harness.LIST_MAX_PAGES} pages of conversations.list", text)
        self.assertIn("C or G id", text)
        # A channel found inside the cap still resolves.
        code, text = self.run_harness("--checks", "dm", "--bot-user-id", BOT_ID)
        self.assertEqual(code, harness.EXIT_OK, text)

    def test_rate_limit_is_retried(self):
        calls = []

        class Limited:
            def request(self, method, url, headers, body):
                calls.append(url)
                if len(calls) == 1:
                    return 429, {"Retry-After": "2"}, b""
                return 200, {}, b'{"ok": true, "user_id": "U1"}'

        client = harness.SlackClient(LISTED_TOKEN, Limited(), "http://x/", self.fake.sleep)
        self.assertEqual(client.call("auth.test")["user_id"], "U1")
        self.assertEqual(self.fake.slept, [2.0])

    def test_retry_after_is_clamped(self):
        default = harness.RATE_LIMIT_DEFAULT_WAIT_SECONDS
        for value, want in (("-3", default), ("nan", default), ("inf", default), ("x", default),
                            ("100", harness.RATE_LIMIT_MAX_WAIT_SECONDS), ("2", 2.0)):
            self.assertEqual(harness._retry_after({"Retry-After": value}), want, value)


class UnitTest(unittest.TestCase):
    def test_redactor_cuts_registered_and_token_shaped_values(self):
        redactor = harness.Redactor()
        redactor.add("plain-registered-value")
        out = redactor.redact("a plain-registered-value b xoxp-1-2-3 c ya29.abc-def d xapp-1-A-2")
        self.assertEqual(out, "a [redacted] b [redacted] c [redacted] d [redacted]")

    def test_a_reply_outside_the_expected_thread_fails(self):
        session = harness.Session(args=harness.parse_args(["--bot-name", "kage"]), listed=None, listed_user_id=LISTED_ID, bot_user_id=BOT_ID,
                                  channel_id=CHANNEL_ID, run_id="r", clock=None, sleep=None)
        reply = {"ts": "1.3", "thread_ts": "1.2", "text": "PONG", "user": BOT_ID}
        result = harness.judge_listed_reply(session, "mention", CHANNEL_ID, "1.1", reply, harness.KIND_ANSWER, reply, expect_thread="1.1")
        self.assertFalse(result.passed)
        self.assertIn("not threaded under 1.1", result.detail)
        reply["thread_ts"] = "1.1"
        self.assertTrue(harness.judge_listed_reply(session, "mention", CHANNEL_ID, "1.1", reply, harness.KIND_ANSWER, reply,
                                                   expect_thread="1.1").passed)

    def test_classify(self):
        self.assertEqual(harness.classify(REFUSAL.format(id="U1")), harness.KIND_REFUSAL)
        self.assertEqual(harness.classify("⏳ submitted…"), harness.KIND_TASK_LINE)
        self.assertEqual(harness.classify("⚙️ *working* — reading"), harness.KIND_TASK_LINE)
        self.assertEqual(harness.classify("❓ *input-required*"), harness.KIND_TASK_LINE)
        self.assertEqual(harness.classify("✅ *completed*"), harness.KIND_TASK_LINE)
        self.assertEqual(harness.classify("⚠️ could not send that to the running task; it is still working on the original instruction"),
                         harness.KIND_STEER)
        for notice in NOT_STARTED_NOTICES:
            self.assertEqual(harness.classify(notice), harness.KIND_NOT_STARTED, notice)
        self.assertEqual(harness.classify("✏️ steering sent — the worker picks it up"), harness.KIND_STEER)
        for failure in ("🚫 *rejected*", "❌ *failed* — the pod died", "🛑 *canceled*", "❌ could not reach the bus; try again",
                        "❌ failed: the executor is down", "❌ the task failed", "🛑 canceled",
                        "🚫 the executor rejected the task", "🚫 the executor rejected the task: no capability"):
            self.assertEqual(harness.classify(failure), harness.KIND_FAILURE, failure)
        # Not the gateway's grammar: the agent's own text, whatever it opens with.
        for answer in ("PONG", "✅ PONG", "✅ *done*", "⚙️ PONG", "❌ PONG", "🚫 rejected", "✏️ PONG", "🔎 task `t` is *completed*",
                       "ℹ️ PONG", "x ⛔ I can't verify who you are on slack"):
            self.assertEqual(harness.classify(answer), harness.KIND_ANSWER, answer)

    def test_status_grammar_matches_the_gateway_source(self):
        gateway = (REPO / "a2a" / "gateway" / "gateway.go").read_text()
        relay = (REPO / "a2a" / "gateway" / "relay.go").read_text()
        # Since #2493 the placeholder carries a delegated child's line note;
        # a typed turn has none, so the live check sees the bare placeholder.
        self.assertIn(f'g.adapter.Post(rec.Key, withLineNote("{harness.STATUS_PLACEHOLDER}", ts.LineNote))', gateway)
        self.assertIn(f'"{harness.STATUS_BUS_FAILURE}"', gateway)
        # Main moved the notice into a constant; pin the constant and its use.
        self.assertIn(f'const noticeSteerNotSent = "{harness.STEER_FAILED_NOTICE}"', gateway)
        self.assertIn("g.post(rec.Key, noticeSteerNotSent)", gateway)
        self.assertIn('g.post(rec.Key, "✏️ steering sent — ', gateway)
        self.assertIn('line := fmt.Sprintf("%s **%s**", icon, label)', relay)
        self.assertIn('line := fmt.Sprintf("%s **%s**", icon, state)', relay)
        self.assertEqual(relay.count('line += " — " + progress'), 2)
        for state, icon in harness.STATUS_ICONS.items():
            const = "lib.State" + "".join(part.capitalize() for part in state.split("-"))
            self.assertRegex(relay, rf'{const}:\s+"{icon}",', state)
        for post in ('"❌ failed: "+reason', '"❌ the task failed"', '"🛑 canceled"', '"🚫 the executor rejected the task: "+reason',
                     '"🚫 the executor rejected the task"'):
            self.assertIn(f"g.post(rec.Key, {post})", relay)

    def test_not_started_grammar_matches_the_gateway_source(self):
        # Every "not started:" the gateway can post, read off the source, is one the
        # harness reads as a refusal; and the test's renderings come from those formats.
        sources = {path.name: path.read_text() for path in (REPO / "a2a" / "gateway").glob("*.go")
                   if not path.name.endswith("_test.go")}
        formats = [fmt for src in sources.values() for fmt in NOT_STARTED_LITERAL.findall(src)]
        self.assertGreaterEqual(len(formats), 4, formats)
        rendered = set()
        for fmt in formats:
            self.assertTrue(harness.NOT_STARTED_PATTERN.match(fmt), fmt)
            literal = re.escape(fmt).replace("%d", "%s").replace("%s", ".+")
            matching = [n for n in NOT_STARTED_NOTICES if re.fullmatch(literal, n, re.DOTALL)]
            self.assertTrue(matching, f"no test rendering of {fmt!r}")
            rendered.update(matching)
        self.assertEqual(rendered, set(NOT_STARTED_NOTICES))
        # The session cap's count phrase, both forms.
        self.assertIn('workers := fmt.Sprintf("%d session workers are", live)', sources["spawn.go"])
        self.assertIn('workers = "1 session worker is"', sources["spawn.go"])
        # And each one is posted in place of a task: refuseAtSessionCap returns true
        # after both, and the retire refusal is the one passed to retireIncarnation.
        self.assertIn('retireRefusalNotStarted = "⚠️ not started: ', sources["gateway.go"])
        self.assertIn('g.post(rec.Key, "⚠️ not started: can\'t count the running session workers', sources["spawn.go"])
        self.assertIn('g.post(rec.Key, "⚠️ not started: could not mint this task\'s capability")', sources["gateway.go"])

    def test_redactor_cuts_the_escaped_forms_of_a_registered_value(self):
        redactor = harness.Redactor()
        redactor.add("head\ntail-é")
        for printed in (repr("head\ntail-é"), repr("head\ntail-é".encode()), "head%0Atail-%C3%A9"):
            self.assertNotIn("tail", redactor.redact(f"x {printed} y"), printed)

    def test_refusal_marker_matches_the_gateway_source(self):
        gateway = (REPO / "a2a" / "gateway" / "gateway.go").read_text()
        self.assertIn('"⛔ ' + harness.REFUSAL_NOTICE_MARKER.replace("slack", '"+backend+'), gateway)

    def test_turn_subtypes_match_the_gateway_source(self):
        slack = (REPO / "a2a" / "gateway" / "slack.go").read_text()
        self.assertIn('var slackTurnSubtypes = map[string]bool{"": true, "thread_broadcast": true, "file_share": true}', slack)
        self.assertEqual(harness.TURN_SUBTYPES, {"", "thread_broadcast", "file_share"})

    def test_poll_returns_early_and_times_out(self):
        fake = FakeClock()
        answers = iter([None, None, "got"])
        self.assertEqual(harness.poll(lambda: next(answers), 60, 5, fake.clock, fake.sleep), "got")
        self.assertEqual(fake.slept, [5, 5])
        fake = FakeClock()
        self.assertIsNone(harness.poll(lambda: None, 12, 5, fake.clock, fake.sleep))
        self.assertEqual(fake.slept, [5, 5, 2])

    def test_poll_reads_again_after_a_transient_error_until_its_deadline(self):
        def failing(*errors):
            answers = iter(errors)

            def fetch():
                item = next(answers)
                if isinstance(item, BaseException):
                    raise item
                return item
            return fetch

        transient = (harness.HarnessError("cannot reach slack.com: [Errno 104] Connection reset by peer"),
                     harness.SlackAPIError("conversations.history", "http_429"),
                     harness.SlackAPIError("conversations.history", "http_502"),
                     TimeoutError("The read operation timed out"))
        for exc in transient:
            with self.subTest(exc=str(exc)):
                fake = FakeClock()
                self.assertEqual(harness.poll(failing(None, exc, "got"), 60, 5, fake.clock, fake.sleep), "got")
                fake = FakeClock()
                with self.assertRaisesRegex(harness.HarnessError, "nothing within 12s; the last read failed: "):
                    harness.poll(failing(None, None, exc, exc), 12, 5, fake.clock, fake.sleep)
                # A read that recovers before the deadline times out plainly.
                fake = FakeClock()
                self.assertIsNone(harness.poll(failing(exc, None, None, None), 12, 5, fake.clock, fake.sleep))
        # A Slack error that answers the same every time is raised at once.
        fake = FakeClock()
        with self.assertRaisesRegex(harness.SlackAPIError, "not_in_channel"):
            harness.poll(failing(harness.SlackAPIError("conversations.history", "not_in_channel")), 60, 5, fake.clock, fake.sleep)
        self.assertEqual(fake.slept, [])

    def test_parse_checks(self):
        self.assertEqual(harness.parse_checks("thread,dm,mention"), ["dm", "mention", "thread"])
        self.assertEqual(harness.parse_checks("all"), list(harness.CHECKS_ALL))
        with self.assertRaises(Exception):
            harness.parse_checks("nope")

    def test_abbreviated_flags_are_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            harness.parse_args(["--check", "home"])

    def test_time_budget_covers_every_wait(self):
        args = harness.parse_args(["--checks", "all,home", "--home-channel", "c", "--unlisted-repeat", "--bot-name", "kage"])
        # Each typed turn waits up to --type-timeout for the person, then for the bot:
        # dm, mention, thread (after the mention's task settles), unlisted and its repeat.
        self.assertEqual(harness.time_budget(args), (300 + 180) + (300 + 180) + (300 + 360) + (300 + 180) + (300 + 30) + 300)

    def test_an_invalid_home_match_is_refused_at_parse_time(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            harness.parse_args(["--checks", "home", "--home-channel", "c", "--home-match", "(", "--bot-name", "kage"])

    def test_non_finite_timeouts_are_refused(self):
        for flag in ("--reply-timeout", "--poll-interval", "--quiet-window", "--home-timeout", "--home-since", "--type-timeout"):
            for value in ("inf", "-inf", "nan", "infinity"):
                with self.subTest(flag=flag, value=value), contextlib.redirect_stderr(io.StringIO()) as err, \
                        self.assertRaises(SystemExit):
                    harness.parse_args([f"{flag}={value}", "--bot-name", "kage"])
                self.assertIn("finite", err.getvalue())

    def test_waits_must_be_positive(self):
        for flag in ("--reply-timeout", "--poll-interval", "--quiet-window", "--home-timeout", "--type-timeout"):
            for value in ("0", "-5", "-0.5"):
                with self.subTest(flag=flag, value=value), contextlib.redirect_stderr(io.StringIO()) as err, \
                        self.assertRaises(SystemExit):
                    harness.parse_args([f"{flag}={value}", "--bot-name", "kage"])
                self.assertIn("greater than 0", err.getvalue())

    def test_home_since_may_be_zero_but_not_negative(self):
        # 0 is its default: the run's start.
        self.assertEqual(harness.parse_args(["--home-since", "0", "--bot-name", "kage"]).home_since, 0.0)
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            harness.parse_args(["--home-since=-1", "--bot-name", "kage"])
        self.assertIn("not negative", err.getvalue())

    def test_the_not_started_scanner_stays_inside_one_literal(self):
        # A comment between two literals is not a literal: the run cannot cross a newline.
        src = 'x := "a"\n// a turn the cap refuses is not started: to the user\ny := "b"\nz := "not started: %s"\n'
        self.assertEqual(NOT_STARTED_LITERAL.findall(src), ["not started: %s"])

    def test_home_needs_a_channel(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            harness.parse_args(["--checks", "home", "--bot-name", "kage"])
        self.assertIn("the home check needs --home-channel", err.getvalue())

    def test_the_bot_needs_a_name_or_an_id(self):
        # No default name: one workspace's bot name fails every other workspace.
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            harness.parse_args(["--checks", "dm"])
        self.assertIn("pass --bot-name <your bot's name> or --bot-user-id <its member id>", err.getvalue())
        self.assertEqual(harness.parse_args(["--checks", "dm", "--bot-name", "troisbocaux"]).bot_name, "troisbocaux")
        self.assertEqual(harness.parse_args(["--checks", "dm", "--bot-user-id", BOT_ID]).bot_user_id, BOT_ID)


if __name__ == "__main__":
    unittest.main()

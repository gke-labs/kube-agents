#!/usr/bin/env python3
"""Build-time proof that ``deliver: "chat"`` reaches the Chat Agent relay.

Run from ``/opt/hermes`` with the plugin already installed under
``plugins/platforms/chat/``. Drives the REAL ``cron/scheduler.py::_deliver_result``
against a loopback stand-in for the Session KV server and asserts on what
crossed the wire.

Why this exists
---------------

The plugin edits nothing, so there is no patch anchor to fail loudly when
upstream moves. What it does instead is depend on five upstream behaviours, and
every one of them fails QUIETLY — a watchdog that reports nothing looks exactly
like a watchdog with nothing to report:

0. ``build_subprocess_env`` lets a cron child inherit ``SESSION_KV_API_KEY``.
   The scrub blocklist is assembled from ``OPTIONAL_ENV_VARS``, which every
   plugin manifest contributes to, so this plugin can revoke its own
   credential by naming it — see the comment block in ``chat/plugin.yaml``.
1. ``_plugin_cron_env_var`` accepts a plugin platform as a ``deliver=`` target
   when it registers ``cron_deliver_env_var``.
2. ``Platform._missing_`` admits a bundled plugin platform by directory name.
3. ``load_gateway_config`` enables it because ``is_connected`` says so.
4. ``_send_via_adapter`` falls through to ``standalone_sender_fn`` when there is
   no in-process gateway, and ``_deliver_result`` wraps the report in a header
   this plugin has to read the job id back out of.

Any of those five changing turns delivery off. Asserting them here turns that
into a failed image build.

Check 0 is the one this file originally missed, and the omission was structural
rather than an oversight: every other check sets its variables directly in this
process, so none of them crosses the spawn boundary where the scrub happens.
It cost a live delivery failure to find.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

API_KEY = "verify-only-not-a-real-key"
HOME_CHANNEL = "cron-reports"


class Relay:
    """Loopback stand-in for the Session KV server's /v1/cron-reports route."""

    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — stdlib naming
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length).decode("utf-8")
                outer.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization", ""),
                        "body": json.loads(raw) if raw else {},
                    }
                )
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args) -> None:
                """Quiet."""

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "Relay":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}/v1/cron-reports"


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        raise SystemExit(1)
    print(f"ok: {message}")


def job(deliver: str) -> dict:
    return {
        "id": "github-repo-watcher",
        "name": "GitHub Repo Watcher",
        "deliver": deliver,
    }


def main() -> None:
    sys.path.insert(0, os.getcwd())

    home = tempfile.mkdtemp(prefix="verify-chat-relay-")
    os.environ["HERMES_HOME"] = home
    os.environ["SESSION_KV_API_KEY"] = API_KEY
    # Only the installed bundled plugin may answer to `chat`; nothing under the
    # build cwd gets to register it and make this pass for the wrong reason.
    os.environ.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)

    from cron.scheduler import _deliver_result, _is_known_delivery_platform
    from cron.scheduler import _resolve_delivery_targets, _expand_routing_tokens

    # 0. A cron child inherits the relay's credential.
    #
    #    `profile-cron-tick` is a no_agent script job, so `_run_job_script`
    #    builds its environment with `build_subprocess_env()` — the same call
    #    asserted here — and `profile_cron_tick.py` then hands that environment
    #    to the child that actually runs the job. Anything the scrub drops here
    #    is gone by the time the relay looks for it, and the only symptom is a
    #    `last_delivery_error` on a job whose pod has the variable set.
    #
    #    Asserted against the real function rather than against the blocklist
    #    set, because the set is one of several ways a name can be dropped.
    from tools.environments.local import build_subprocess_env

    for name in ("SESSION_KV_API_KEY", "CRON_REPORT_RELAY_URL"):
        child_env = build_subprocess_env(base={**os.environ, name: "sentinel"})
        check(
            child_env.get(name) == "sentinel",
            f"a cron child inherits {name} rather than having it scrubbed",
        )

    # 1. The registry accepts the plugin as a cron delivery platform.
    check(
        _is_known_delivery_platform("chat"),
        "the registry accepts `chat` as a cron delivery target",
    )

    report = "Two issues triaged; #1841 needs a human."

    with Relay() as relay:
        os.environ["CRON_REPORT_RELAY_URL"] = relay.url
        os.environ["CHAT_HOME_CHANNEL"] = HOME_CHANNEL

        # 2. The target resolves without an origin, which a cron child has no
        #    way to supply.
        targets = _resolve_delivery_targets(job("chat"))
        check(
            [t["platform"] for t in targets] == ["chat"],
            "deliver=chat resolves to exactly one target, the relay",
        )

        # 3. The whole path: the real _deliver_result posts the report.
        error = _deliver_result(job("chat"), report)
        check(error is None, f"_deliver_result reports success (got {error!r})")
        check(len(relay.requests) == 1, "exactly one POST, so no double delivery")

        sent = relay.requests[0]
        check(sent["path"] == "/v1/cron-reports", "it reached the relay route")
        check(
            sent["authorization"] == f"Bearer {API_KEY}",
            "it carried the Session KV bearer token",
        )
        body = sent["body"]
        check(
            body.get("job_id") == "github-repo-watcher",
            f"the job id survived the cron wrapper (got {body.get('job_id')!r})",
        )
        check(
            body.get("title") == "GitHub Repo Watcher",
            f"the job name survived the cron wrapper (got {body.get('title')!r})",
        )
        check(
            body.get("report") == report,
            "the Chat Agent gets the report and none of Hermes' wrapper text",
        )

        # 4. `all` reaches the relay once the home channel is set. Asserted
        #    because the roster depends on it, not because it is incidental:
        #    a job left on deliver=all must not go silently undelivered.
        check(
            "chat" in _expand_routing_tokens("all"),
            "deliver=all expands to include the relay",
        )

        # 5. `cronjob(action='create')` does not warn that a relayed job is
        #    local-only. It decides by calling _resolve_delivery_targets, so
        #    the answer follows the switch — see check 8 for the other branch.
        from tools.cronjob_tools import _local_delivery_notice

        check(
            _local_delivery_notice(job("chat"), "chat") is None,
            "a relayed job is not announced as local-only where the relay is on",
        )

        # 6. A job that did not ask for it sends nothing.
        before = len(relay.requests)
        check(
            _deliver_result(job("local"), report) is None
            and len(relay.requests) == before,
            "deliver=local still delivers nowhere",
        )

        # 6b. Neither does a silent tick. `github-repo-watcher` is a no_agent
        #     script on */10 that prints nothing on a clean sweep, and the route
        #     rejects an empty report with HTTP 400 — so without this the fleet's
        #     quietest watchdog would record a delivery error 144 times a day,
        #     which is the audibility invariant inverted. Driven through the real
        #     _deliver_result, wrapper and all, because the wrapper is what makes
        #     an empty report non-empty on the wire.
        before = len(relay.requests)
        error = _deliver_result(job("chat"), "")
        check(
            error is None and len(relay.requests) == before,
            f"an empty report is a silent tick, not a delivery (got {error!r})",
        )
        check(
            _deliver_result(job("chat"), "   \n\t ") is None
            and len(relay.requests) == before,
            "whitespace is silence too",
        )

    # 7. A relay that answers 500 is a reported delivery error, not an exception
    #    and not a false success.
    with Relay(status=500) as broken:
        os.environ["CRON_REPORT_RELAY_URL"] = broken.url
        error = _deliver_result(job("chat"), report)
        check(
            error is not None and "500" in error,
            f"a 500 from the relay is recorded as a delivery error (got {error!r})",
        )
        check(API_KEY not in error, "the failure string does not carry the API key")

    # 8. Unset the switch and the platform vanishes — this is what keeps the
    #    gateway process from advertising a platform it cannot serve.
    del os.environ["CHAT_HOME_CHANNEL"]
    check(
        _resolve_delivery_targets(job("chat")) == [],
        "with CHAT_HOME_CHANNEL unset the relay resolves to no target",
    )
    # And the documented cost of that: the gateway is where `cronjob` runs, so
    # a job the agent creates at runtime with deliver="chat" is announced as
    # local-only even though its cron child will relay it. Asserted so the
    # design doc's claim is measured and a future fix has a failing check to
    # flip. See docs/designs/cron-report-relay.md, "What this costs".
    notice = _local_delivery_notice(job("chat"), "chat")
    check(
        notice is not None and "local-only" in notice,
        "where the relay is off, cronjob still calls a relayed job local-only",
    )

    # 9. Check 6b proves the sender's own guard holds. This pins the two
    #    upstream stops in front of it, which are the reason that guard is
    #    belt-and-braces rather than the only thing standing between a quiet
    #    watchdog and a delivery error every ten minutes: run_job returns
    #    SILENT_MARKER instead of an empty no_agent job's stdout, and its caller
    #    delivers only when `deliver_content.strip()` is truthy. Neither is a
    #    function this can call, so what is asserted is the primitive they are
    #    built from — if it disappears or stops recognising the marker, the
    #    build fails here rather than the fleet finding out ten minutes at a
    #    time.
    from cron.scheduler import SILENT_MARKER, _is_cron_silence_response

    check(
        _is_cron_silence_response(SILENT_MARKER),
        "an empty no_agent tick's SILENT_MARKER is still recognised as silence",
    )
    check(
        not _is_cron_silence_response("the issues sweep failed"),
        "a real report is not mistaken for silence",
    )

    print("\nverify_chat_relay: all checks passed")


if __name__ == "__main__":
    main()

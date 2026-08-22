"""``deliver: "chat"`` — hand a cron job's report to the Chat Agent.

Installed at ``/opt/hermes/plugins/platforms/chat/`` so Hermes discovers it as a
bundled platform plugin. Nothing in the Hermes tree is edited: the scheduler
already routes ``deliver=<name>`` through the platform registry, and
``cron/scheduler.py::_plugin_cron_env_var`` says so in its own words —
plugins that set ``cron_deliver_env_var`` on their ``PlatformEntry`` "get cron
delivery support without editing this module".

Why a delivery mode and not a prompt instruction
------------------------------------------------

The relay itself — the Chat Agent composing the message, and the report being
stored against the thread it lands in — is ``docs/designs/cron-report-relay.md``.
This module is only about *what triggers* it.

The first cut triggered it from the job's prompt: call ``report_to_chat``, then
return ``[SILENT]``. That works for a roster shipped in the image, where the
prompt is reviewed alongside the job, and fails for everything else. A job the
user asks for at runtime ("watch that rollout every ten minutes and tell me when
it settles") is created through ``cronjob(action='create')`` with whatever prompt
the moment produced, so it carries no such contract — and its ``deliver`` then
resolves to a Google Chat home channel this image cannot hand a child profile,
i.e. to nowhere. The result is a job that runs forever and is never heard from,
which is the exact failure the relay exists to end.

``deliver`` is the field that already means "where does the output go", every
creation path can set it, and ``create_job``'s fixed keyword signature makes it
the only field a runtime-created job *can* set. So the relay is a platform, and
asking for it is one field.

Why this platform is not a platform
-----------------------------------

It has no inbound side and no adapter. ``adapter_factory`` exists because
``PlatformEntry`` requires one and raises if the gateway ever tries to build it —
which it will not, because ``_is_connected`` returns False unless
``CHAT_HOME_CHANNEL`` is set, and only ``profile_cron_tick.py`` sets it, for the
cron children it spawns. In the gateway process the platform stays unregistered
in the config, invisible to ``gateway status``, and starts nothing.

That one variable is the whole switch: it gates enablement (through
``is_connected``), it is the ``cron_deliver_env_var`` the scheduler reads to
resolve ``deliver: "chat"`` to a target, and where it is unset the platform
behaves as if this directory were not there.

What the sender can and cannot see
----------------------------------

``standalone_sender_fn`` is handed the delivery text, not the job. The job's id
and name are in the text, because ``_deliver_result`` wraps every cron delivery
in a two-line header before sending it, and :func:`parse_cron_wrapper` reads them
back out. That coupling is checked at image build time by
``deploy/docker/plugins/verify_chat_relay.py``, which drives the real
``_deliver_result`` and asserts on what arrived — so upstream changing the
wrapper fails the build rather than degrading in production.

If the header is ever absent (``cron.wrap_response: false`` turns it off), the
report still relays: it just arrives under a per-profile session for the day
instead of a per-job one, and says so in the log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

#: The platform name, and therefore the ``deliver`` token. Must equal this
#: directory's basename: ``Platform._missing_`` admits a plugin platform by
#: scanning ``plugins/platforms/`` for directory names.
PLATFORM_NAME = "chat"

#: Set => the relay is on in this process. See the module docstring.
HOME_CHANNEL_ENV = "CHAT_HOME_CHANNEL"

#: The Session KV server's relay route. Loopback: it runs in this Pod.
DEFAULT_RELAY_URL = "http://127.0.0.1:8699/v1/cron-reports"
RELAY_URL_ENV = "CRON_REPORT_RELAY_URL"

#: Every route on the Session KV server except ``/healthz`` needs this.
API_KEY_ENV = "SESSION_KV_API_KEY"

#: The route relays synchronously and answers with the outcome, so this has to
#: cover a whole Chat Agent turn plus the chat round trip -- not just a connect
#: stall. Sized above ``_run_relay_turn``'s own 300s so the server's verdict is
#: what the scheduler records; time out first and a delivered report would be
#: written down as a failure.
RELAY_TIMEOUT_SECONDS = 360.0

#: ``_deliver_result``'s wrapper. Matched, not assumed — see
#: :func:`parse_cron_wrapper`.
_WRAPPER_RE = re.compile(
    r"\ACronjob Response: (?P<title>.*)\n\(job_id: (?P<job_id>.*)\)\n-{5,}\n\n",
)


def parse_cron_wrapper(message: str) -> Tuple[str, str, str]:
    """Split ``_deliver_result``'s wrapper into ``(job_id, title, report)``.

    Returns empty strings for the two identifiers when the wrapper is absent,
    leaving ``report`` as the whole message. The caller relays either way: a
    report that lands in the wrong thread is worth more than one that does not
    land at all.

    The footer is removed by exact suffix rather than by pattern. It is built
    from the job's own name, so once the header has given us that name the exact
    string is known — and a report that happens to quote the footer keeps it.
    """
    match = _WRAPPER_RE.match(message or "")
    if not match:
        return "", "", message or ""
    title = match.group("title").strip()
    body = message[match.end() :]
    footer = (
        "\n\nTo stop or manage this job, send me a new message "
        f'(e.g. "stop reminder {title}").'
    )
    if body.endswith(footer):
        body = body[: -len(footer)]
    return match.group("job_id").strip(), title, body.strip()


def profile_name() -> str:
    """The profile this cron child runs as, from its ``HERMES_HOME``.

    Named profiles live at ``<root>/profiles/<name>``, which is the shape
    ``profile_cron_tick.py`` hands the child. Anything else is the root home —
    the Chat Agent's own store — and reports as ``default``. Only used to name
    the relay session, so a wrong answer costs a thread, not a delivery.
    """
    home = Path(os.getenv("HERMES_HOME", "") or "/opt/data")
    return home.name if home.parent.name == "profiles" else "default"


def relay_url() -> str:
    return (os.getenv(RELAY_URL_ENV, "") or "").strip() or DEFAULT_RELAY_URL


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """FastAPI's ``detail`` off an error response, as ``": <detail>"`` or ``""``.

    Best effort by design: the body is read once, may be empty or not JSON, and
    is never allowed to turn a delivery failure into an exception. Bounded
    because it becomes ``last_delivery_error``, which is stored per job run.
    """
    try:
        detail = (json.loads(exc.read().decode("utf-8", "replace")) or {}).get("detail")
    except Exception:
        return ""
    if not isinstance(detail, str) or not detail.strip():
        return ""
    return f": {detail.strip()[:200]}"


def _relay_verdict(response) -> str:
    """The route's ``relay`` field off a 2xx body, or ``""`` if it did not say.

    Best effort, like :func:`_http_error_detail`: the body is read once and a
    delivery that worked is never turned into an exception by failing to parse
    the receipt for it. ``""`` therefore means "no verdict", not "composed" —
    the caller treats only an explicit ``degraded`` as degraded.
    """
    try:
        return str((json.loads(response.read().decode("utf-8", "replace")) or {}).get("relay") or "")
    except Exception:
        return ""


def _post(url: str, payload: dict, api_key: str) -> Tuple[Optional[str], str]:
    """POST *payload* as JSON. ``(None, verdict)`` on success, else ``(why, "")``.

    Blocking, and called through :func:`asyncio.to_thread`. ``urllib`` rather
    than ``httpx`` keeps this module stdlib-only, so its tests run wherever the
    repo is checked out and not only inside the image.
    """
    try:
        # Building the Request is inside the try: a malformed CRON_REPORT_RELAY_URL
        # raises here, not at urlopen, and that is a delivery failure like any
        # other rather than an exception for the scheduler to catch.
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=RELAY_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None) or response.getcode()
            if status >= 300:
                return f"chat relay answered HTTP {status}", ""
            return None, _relay_verdict(response)
    except urllib.error.HTTPError as exc:
        # The route names the failing leg in `detail` ("composed but not
        # delivered to google_chat"), which is the difference between a
        # last_delivery_error someone can act on and a bare status code.
        return f"chat relay answered HTTP {exc.code}{_http_error_detail(exc)}", ""
    except Exception as exc:  # URLError, socket timeout, malformed URL
        return f"chat relay unreachable: {type(exc).__name__}: {exc}", ""


async def standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> dict:
    """POST one finished report to the Chat Agent relay.

    Called by ``tools/send_message_tool._send_via_adapter`` — the cron child has
    no in-process gateway adapter, which is precisely the case this hook exists
    for.

    ``chat_id``, ``thread_id``, ``media_files`` and ``force_document`` are
    accepted for signature parity and ignored: the relay route has exactly one
    destination, and the Chat Agent decides where its own message goes.

    The error strings become ``last_delivery_error``, so they name the condition
    and never the key.
    """
    job_id, title, report = parse_cron_wrapper(message)

    # A silent tick is a success, not a delivery. `github-repo-watcher` runs
    # every ten minutes and prints nothing on a clean sweep -- "Empty means a
    # clean, quiet tick and the scheduler posts nothing" (github_scan_gate.py)
    # -- and the relay route rejects an empty `report` with HTTP 400, which
    # would land in `last_delivery_error` 144 times a day on a watchdog that is
    # working as intended, inverting the audibility this whole change is for.
    #
    # Upstream stops it twice before it gets here: a no_agent job with empty
    # stdout returns SILENT_MARKER rather than its output, and `should_deliver`
    # is `bool(deliver_content.strip())`, so `_deliver_result` is never called.
    # Both are pinned by verify_chat_relay.py, because both are quiet. This is
    # the third stop, and it is the sibling's: `slack_relay_patch.py` guards the
    # identical case for the identical reason. A sender that behaves differently
    # from its sibling on the same input is a difference someone eventually has
    # to debug.
    #
    # Ahead of the credential check on purpose. There is nothing to send, so a
    # missing key is not this tick's problem, and reporting one would be the
    # same error-every-ten-minutes by another name.
    if not report.strip():
        logger.info(
            "chat relay: nothing to relay for job_id=%s — silent tick", job_id or "?"
        )
        return {"success": True, "platform": PLATFORM_NAME, "skipped": "empty_report"}

    api_key = (os.getenv(API_KEY_ENV, "") or "").strip()
    if not api_key:
        return {
            "error": (
                f"chat relay: {API_KEY_ENV} is unset, so the Session KV server "
                f"cannot be authenticated"
            )
        }

    if not job_id:
        logger.warning(
            "chat relay: no cron wrapper on this delivery — relaying without a "
            "job id, so the report shares its profile's thread for the day"
        )

    payload = {
        "job_id": job_id,
        "profile": profile_name(),
        "title": title,
        "report": report,
    }
    error, verdict = await asyncio.to_thread(_post, relay_url(), payload, api_key)
    if error:
        return {"error": error}

    if verdict == "degraded":
        # The route answered 200 and the report is in the channel, so this is
        # not a lost delivery — but the Chat Agent turn failed and what landed
        # is the raw text under an `[unrelayed]` marker. `error` is the only
        # field of this dict the scheduler reads, and `last_delivery_error` is
        # the only place a run record can carry the fact, so the verdict goes
        # there rather than into a log line nobody greps. The string says
        # plainly that the report did arrive, because `cronjob list` showing a
        # delivery error is otherwise read as "nothing was sent" and invites a
        # re-run that would post the same finding twice.
        #
        # Not doing this is what the relay was built to stop, one layer out: a
        # front door that has been down all week would otherwise produce run
        # records byte-identical to healthy ones.
        message = (
            "chat relay degraded: the report was posted but the Chat Agent turn "
            "failed, so the channel has the raw text marked [unrelayed] rather "
            "than a composed message. Delivered — do not re-run to resend."
        )
        logger.warning("chat relay: %s (job_id=%s)", message, job_id or "?")
        return {"error": message}

    logger.info(
        "chat relay: report handed to the Chat Agent (job_id=%s)", job_id or "?"
    )
    return {
        "success": True,
        "platform": PLATFORM_NAME,
        "chat_id": chat_id,
        "message_id": job_id or "cron-report",
    }


def check_requirements() -> bool:
    """Whether this platform can run at all. It is stdlib only, so: always."""
    return True


def is_connected(config: Any) -> bool:
    """Whether the relay is switched on in *this* process.

    ``load_gateway_config`` consults this before enabling a plugin platform, so
    returning False here is what keeps the gateway from registering a delivery
    target it has no adapter for. ``profile_cron_tick.py`` sets the variable for
    the cron children it spawns and nothing else does.
    """
    return bool((os.getenv(HOME_CHANNEL_ENV, "") or "").strip())


def _no_adapter(_config: Any):
    """There is no inbound side to build. ``create_adapter`` catches this."""
    raise NotImplementedError(
        "The chat relay is delivery-only: it has no gateway adapter. Reaching "
        "here means the platform was enabled in a process that then tried to "
        "start it — see the module docstring."
    )


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Chat Agent",
        adapter_factory=_no_adapter,
        check_fn=check_requirements,
        is_connected=is_connected,
        # Nothing to install: this module is stdlib only.
        install_hint="",
        # What makes `deliver: "chat"` a target the scheduler will resolve.
        cron_deliver_env_var=HOME_CHANNEL_ENV,
        # Out-of-process delivery: the cron child is not the gateway.
        standalone_sender_fn=standalone_send,
        # No chunking. The Chat Agent is composing a message from this text, not
        # posting it; the length bound that matters is CRON_REPORT_MAX_CHARS on
        # the relay route, which rejects rather than silently splitting a report
        # into pieces that each start a separate turn.
        max_message_length=0,
        emoji="🗣️",
        # Never offer /update from a channel that has no inbound side.
        allow_update_command=False,
    )

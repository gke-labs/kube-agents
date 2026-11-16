import asyncio
import base64
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request

os.environ["PUBSUB_HOME_CHANNEL"] = "none"
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# How long a dedup verdict lasts when a route does not say. Also the window assumed for
# a registry entry written before entries recorded their own — see _entry_window.
DEFAULT_DEDUP_WINDOW_SECONDS = 86400  # 24 hours

# Ceiling on the preflight read of each subscription (a GET through the relay): connect()
# runs it synchronously on the event loop, before the gateway is marked connected, so an
# unbounded call that stalled held the whole gateway down in silence until the startup
# probe restarted it ten minutes later. Its result is only logged, so a read that hits
# this bound is a warning and startup continues.
PREFLIGHT_RPC_TIMEOUT_SECONDS = 30

# The credential broker's Pub/Sub relay. The gateway pod holds no Google credential of
# its own: the broker pulls on the install's identity, confined by its policy to the
# subscriptions the operator rendered from the AgentPlugin configs, and this adapter
# speaks the Pub/Sub REST API to it. The operator renders PUBSUB_RELAY_URL on every
# gateway: the broker's address once a plugin route names a subscription it can qualify
# (a full projects/<project>/subscriptions/<name> path, or a short name on an agent with
# a harness project), empty otherwise. Empty, the adapter refuses to connect rather than
# pull on a credential it cannot have, and the AgentPlugin's status says which route the
# operator could not qualify.
PUBSUB_RELAY_URL_ENV = "PUBSUB_RELAY_URL"
# The projected token the broker authenticates the gateway by, read on every call
# because the kubelet rotates it in place (the same file credential_proxy_client reads).
CREDENTIAL_PROXY_TOKEN_FILE_ENV = "CREDENTIAL_PROXY_TOKEN_FILE"
# Where the project of a short subscription name comes from, in order. The first is the
# operator's, rendered beside PUBSUB_RELAY_URL from the same project it composed the
# broker's subscription set with, and pinned, so this side cannot be made to ask for a
# subscription the broker refuses; GCP_PROJECT_ID is a plugin's to override and is the
# fallback for an install without the operator's variable.
PROJECT_ID_ENVS = ("PUBSUB_RELAY_PROJECT", "GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT")
RELAY_PREFIX = "/v1/pubsub/gcp/"
RELAY_PUBSUB_HOST = "pubsub.googleapis.com"
# One pull asks for one message. A pull is a lease: by the time the broker reads the
# answer, Pub/Sub has delivered the batch and started its ack deadline, and the ack ids
# are in the body, so an answer the broker cannot relay (its response cap) strands every
# message in it until the deadline. One message per pull keeps every answer under the
# cap the broker gives this family, which covers Pub/Sub's largest message. Each message
# here becomes an agent turn, so the round trip per message costs nothing that matters.
PULL_MAX_MESSAGES = 1
# Above the broker's own relay deadline (API_RELAY_DEADLINE_S, 120 s), so a pull that
# the broker ends is answered by the broker rather than torn down by this side.
PULL_REQUEST_TIMEOUT_SECONDS = 130
# A settle (acknowledge, nack) is one short call.
SETTLE_REQUEST_TIMEOUT_SECONDS = 20
# The pause after a pull that returned nothing. Pub/Sub holds an empty synchronous
# pull for a while on most answers (about twenty seconds through the relay on a live
# install), but not on every answer, and a loop with no pause of its own spins against
# the API and fills the broker's audit log when it does not. The drift detector pulls
# through the same relay with the same pause (k8s-operator/cmd/drift-detector).
PULL_IDLE_INTERVAL_SECONDS = 5
# Back-off between failed pulls: doubles from the first value to the ceiling, so a
# broker that is restarting is not hammered and a refused subscription logs a line
# every half minute rather than a storm.
PULL_BACKOFF_INITIAL_SECONDS = 2
PULL_BACKOFF_MAX_SECONDS = 30
# How long disconnect() waits for each pull thread to notice the stop flag.
PULL_THREAD_JOIN_SECONDS = 5
# The relay answer for a subscription that does not exist, as Pub/Sub gives it,
# and the status string Pub/Sub's error body carries then; the broker's own 404
# for a route it does not serve carries no such body.
HTTP_NOT_FOUND = 404
GOOGLE_NOT_FOUND_STATUS = "NOT_FOUND"

# Hermes asks every platform plugin whether its dependencies are present. This one
# speaks HTTP to the broker with the standard library and needs nothing installed.
def check_requirements() -> bool:
    return True

def validate_config(config) -> bool:
    return True

# Import main base classes
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
)
from gateway.config import Platform, PlatformConfig
from gateway.session import build_session_key

def _get_nested_value(payload: Any, path: str) -> Any:
    parts = path.split(".")
    val = payload
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        elif isinstance(val, list):
            try:
                idx = int(part)
                val = val[idx] if idx < len(val) else None
            except ValueError:
                return None
        else:
            return None
    return val



class RelayRefused(Exception):
    """The broker answered a relayed call with an error status.

    Carries what the broker said so the log line can name the rule that refused
    a call (``SECURITY_POLICY_BLOCKED`` with its ``rule``) apart from an upstream
    answer it merely relayed (a 404 for a subscription that does not exist).
    """

    def __init__(self, status: int, body: Any) -> None:
        fields = body if isinstance(body, dict) else {}
        self.status = status
        self.code = str(fields.get("code") or "")
        self.rule = str(fields.get("rule") or "")
        error = fields.get("error")
        self._error_status = str(error.get("status") or "") if isinstance(error, dict) else ""
        detail = str(fields.get("error") or fields.get("message") or "")
        super().__init__(f"status={status} code={self.code or 'none'} rule={self.rule or 'none'} {detail}".rstrip())

    @property
    def upstream_not_found(self) -> bool:
        """True for Pub/Sub's own NOT_FOUND, relayed; False for a 404 the broker answered itself.

        The broker answers ``{"status": "not_found"}`` for a route it does not
        serve, which a broker image older than this adapter does; Pub/Sub's
        answer carries ``error.status == "NOT_FOUND"``. The two want different
        operators: one creates a subscription, the other upgrades the broker.
        """
        return self.status == HTTP_NOT_FOUND and self._error_status == GOOGLE_NOT_FOUND_STATUS


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A 3xx from the relay is an error, not a place to re-send the token to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class PubsubRelay:
    """The Pub/Sub REST API, spoken to the credential broker's relay.

    Four calls, the ones the broker's policy forwards for the platform adapter:
    a read of the subscription, pull, acknowledge and modifyAckDeadline. The
    broker adds the Google credential; this side presents the gateway's
    projected token, read from CREDENTIAL_PROXY_TOKEN_FILE on every call, and
    never follows a redirect with it (credential_proxy_client's guard, kept
    here because this file is loaded by path with no sibling module).
    """

    def __init__(self, base_url: str, token_file: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file
        self._opener = urllib.request.build_opener(_NoRedirects)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token_file:
            with open(self.token_file, encoding="utf-8") as handle:
                token = handle.read().strip()
            if not token:
                raise RuntimeError(f"the broker token file {self.token_file} is empty")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _url(self, subscription_path: str, verb: str) -> str:
        return f"{self.base_url}{RELAY_PREFIX}{RELAY_PUBSUB_HOST}/v1/{subscription_path}{verb}"

    def call(self, method: str, subscription_path: str, verb: str = "", body: Optional[dict] = None,
             timeout: float = SETTLE_REQUEST_TIMEOUT_SECONDS) -> Any:
        """One relayed call; the decoded JSON answer, or RelayRefused on an error status."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self._url(subscription_path, verb), data=data, method=method,
                                         headers=self._headers())
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                answer = json.loads(exc.read().decode("utf-8") or "{}")
            except Exception:
                answer = {}
            raise RelayRefused(exc.code, answer) from None
        return json.loads(raw.decode("utf-8") or "{}")

    def get_subscription(self, subscription_path: str, timeout: float) -> Any:
        return self.call("GET", subscription_path, timeout=timeout)

    def pull(self, subscription_path: str, max_messages: int = PULL_MAX_MESSAGES) -> List[dict]:
        answer = self.call("POST", subscription_path, ":pull", {"maxMessages": max_messages},
                           timeout=PULL_REQUEST_TIMEOUT_SECONDS)
        received = answer.get("receivedMessages") if isinstance(answer, dict) else None
        return list(received or [])

    def acknowledge(self, subscription_path: str, ack_ids: List[str]) -> None:
        self.call("POST", subscription_path, ":acknowledge", {"ackIds": list(ack_ids)})

    def nack(self, subscription_path: str, ack_ids: List[str]) -> None:
        self.call("POST", subscription_path, ":modifyAckDeadline", {"ackIds": list(ack_ids), "ackDeadlineSeconds": 0})


class RelayMessage:
    """One pulled message, with the surface _process_message reads and settles.

    ``data`` is the decoded payload, ``attributes`` and ``message_id`` as Pub/Sub
    sent them. ``ack`` and ``nack`` settle through the relay, once: the second
    call on a message already settled is a no-op, the way the client library's
    is. The settle runs on its own thread so the event loop, which calls
    ``ack()`` at the top of _process_message, is not held for the round trip;
    a failed settle is logged and the message redelivers after its deadline.
    """

    def __init__(self, relay: PubsubRelay, subscription_path: str, received: dict) -> None:
        message = received.get("message") or {}
        self.data = base64.b64decode(message.get("data") or b"", validate=True)
        self.attributes = dict(message.get("attributes") or {})
        self.message_id = str(message.get("messageId") or "")
        self._ack_id = str(received.get("ackId") or "")
        self._relay = relay
        self._subscription_path = subscription_path
        self._settled = False
        self._lock = threading.Lock()

    def _settle(self, acknowledge: bool) -> None:
        with self._lock:
            if self._settled or not self._ack_id:
                return
            self._settled = True
        verb = "acknowledge" if acknowledge else "nack"

        def run() -> None:
            try:
                if acknowledge:
                    self._relay.acknowledge(self._subscription_path, [self._ack_id])
                else:
                    self._relay.nack(self._subscription_path, [self._ack_id])
            except Exception as exc:
                logger.warning("PubSub: %s of message %s on '%s' failed: %s; it redelivers after its deadline",
                               verb, self.message_id, self._subscription_path, exc)

        threading.Thread(target=run, name=f"pubsub-{verb}-{self.message_id}", daemon=True).start()

    def ack(self) -> None:
        self._settle(True)

    def nack(self) -> None:
        self._settle(False)


class PubSubAdapter(BasePlatformAdapter):
    """Google Cloud Pub/Sub pull subscriber adapter for Hermes Agent.
    
    This platform adapter connects to GCP Pub/Sub pull subscriptions, consumes incoming 
    messages (e.g. log-based alerts from Cloud Logging sinks), formats them using custom 
    templates, optionally wraps them into agent skill invocations, and routes them to the 
    core agent conversation loop.
    
    It also supports sending agent responses back to other chat platforms (cross-platform 
    delivery) or writing them to the system logs.
    """

    # Ceiling on _delivery_info — see _remember_delivery for why it needs one.
    _MAX_DELIVERY_ENTRIES = 256

    def __init__(self, config: PlatformConfig):
        """Initialize the adapter with PlatformConfig configurations."""
        super().__init__(config, Platform("pubsub"))
        extra = config.extra or {}
        self._subscriptions_config: Dict[str, dict] = extra.get("subscriptions", {})
        self._relay: Optional[PubsubRelay] = None
        self._pull_threads: List[threading.Thread] = []
        self._stop_pulling = threading.Event()
        self._delivery_info: Dict[str, dict] = {}
        self.gateway_runner = None
        self._locks: Dict[str, asyncio.Lock] = {}
        self._message_timestamps: Dict[str, List[float]] = {}

    def _get_project_id(self) -> Optional[str]:
        """The project a short subscription name belongs to, from the gateway's environment.

        PUBSUB_RELAY_PROJECT is the operator's, the project it composed the broker's
        set with, rendered after the plugin env merge; GCP_PROJECT_ID is the fallback a
        plugin can override. Not a credential's default project: the gateway pod holds
        no Google credential, so there is nothing for it to read a project from.
        """
        for name in PROJECT_ID_ENVS:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return None

    def _parse_topic_config(self, sub_cfg: dict, project_id: str, name: str) -> Optional[tuple]:
        """Parse topic path and simple name from config."""
        # Stripped, as the operator strips it when it renders the broker's set, so a
        # quoted value with a stray space composes the same path on both sides.
        topic_input = (sub_cfg.get("topic") or "").strip()
        if not topic_input:
            return None
        if topic_input.startswith("projects/"):
            return topic_input, topic_input.split("/")[-1]
        return f"projects/{project_id}/topics/{topic_input}", topic_input

    def _parse_subscription_config(self, sub_cfg: dict, project_id: str, topic_name: Optional[str]) -> str:
        """Parse or generate subscription path based on topic or configured subscription name."""
        sub_input = (sub_cfg.get("subscription") or "").strip()
        if not sub_input:
            if not topic_name:
                raise ValueError("Either 'topic' or 'subscription' must be configured.")
            sub_name = f"{topic_name}-sub"
            return f"projects/{project_id}/subscriptions/{sub_name}"
        if sub_input.startswith("projects/"):
            return sub_input
        return f"projects/{project_id}/subscriptions/{sub_input}"

    def _check_subscription_exists(self, sub_path: str) -> bool:
        """Read the subscription through the relay and log clearly if it is not there.

        Announced before the call and bounded by PREFLIGHT_RPC_TIMEOUT_SECONDS, so a
        stall reads as "waiting on get_subscription" in the log rather than silence,
        and ends in a warning rather than holding connect(). The topic is not checked:
        the broker relays no topic read, and the install's identity holds the
        subscriber role on the subscription alone.
        """
        logger.info(
            "PubSub: Checking subscription '%s' exists (get_subscription, %ss timeout)",
            sub_path, PREFLIGHT_RPC_TIMEOUT_SECONDS,
        )
        started = time.monotonic()
        try:
            self._relay.get_subscription(sub_path, timeout=PREFLIGHT_RPC_TIMEOUT_SECONDS)
        except RelayRefused as exc:
            if exc.upstream_not_found:
                logger.warning(
                    "PubSub: Subscription '%s' is NOT present in GCP (%.1fs). Please ensure it is created prior to running.",
                    sub_path, time.monotonic() - started,
                )
            elif exc.status == HTTP_NOT_FOUND:
                logger.warning(
                    "PubSub: the credential broker has no %s route (%.1fs): the broker image predates this adapter;"
                    " upgrade the broker. Continuing startup; pulls retry with back-off.",
                    RELAY_PREFIX, time.monotonic() - started,
                )
            else:
                logger.warning(
                    "PubSub: Could not verify subscription '%s' (%.1fs): the relay answered %s",
                    sub_path, time.monotonic() - started, exc,
                )
            return False
        except (TimeoutError, socket.timeout) as exc:
            logger.warning(
                "PubSub: Timed out after %.1fs waiting on get_subscription for subscription '%s'"
                " (bound %ss): %s. Continuing startup without verifying it.",
                time.monotonic() - started, sub_path, PREFLIGHT_RPC_TIMEOUT_SECONDS, exc,
            )
            return False
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                logger.warning(
                    "PubSub: Timed out after %.1fs waiting on get_subscription for subscription '%s'"
                    " (bound %ss): %s. Continuing startup without verifying it.",
                    time.monotonic() - started, sub_path, PREFLIGHT_RPC_TIMEOUT_SECONDS, exc,
                )
            else:
                logger.warning(
                    "PubSub: Could not reach the credential broker's relay to verify subscription '%s' (%.1fs): %s."
                    " Continuing startup; pulls retry with back-off.",
                    sub_path, time.monotonic() - started, exc.reason,
                )
            return False
        except OSError as exc:
            logger.warning(
                "PubSub: Could not verify subscription '%s' (%.1fs): %s. Continuing startup without verifying it.",
                sub_path, time.monotonic() - started, exc,
            )
            return False
        except Exception as exc:
            logger.warning(
                "PubSub: Could not verify subscription '%s' (%.1fs): %s",
                sub_path, time.monotonic() - started, exc,
            )
            return False
        logger.info("PubSub: Subscription '%s' exists (%.1fs)", sub_path, time.monotonic() - started)
        return True

    def _resolve_subscription_path(self, name: str, sub_cfg: dict, project_id: Optional[str]) -> Optional[str]:
        """The full subscription path a route names, or None when it names none.

        A short name needs the project; without one the route is logged and skipped
        rather than guessed. A route that names only a topic subscribes to
        `<topic>-sub`, the name the installers create.
        """
        sub_input = (sub_cfg.get("subscription") or "").strip()
        if sub_input.startswith("projects/"):
            return sub_input
        if not project_id:
            logger.warning("PubSub: Route '%s' names a short subscription or topic and no project id is set in %s; "
                           "name it as projects/<project>/subscriptions/<name>", name, "/".join(PROJECT_ID_ENVS))
            return None
        topic_info = self._parse_topic_config(sub_cfg, project_id, name)
        try:
            return self._parse_subscription_config(sub_cfg, project_id, topic_info[1] if topic_info else None)
        except ValueError as exc:
            logger.error("PubSub: Route '%s' is not usable: %s", name, exc)
            return None

    def _subscribe_to_route(self, name: str, sub_cfg: dict, subscription_path: str, loop: asyncio.AbstractEventLoop) -> None:
        """Start the route's pull thread: pull through the relay, hand each message to the loop."""
        logger.info("PubSub: Subscribing to '%s' (route '%s')", subscription_path, name)
        thread = threading.Thread(
            target=self._pull_loop, args=(name, sub_cfg, subscription_path, loop, self._relay, self._stop_pulling),
            name=f"pubsub-pull-{name}", daemon=True,
        )
        thread.start()
        self._pull_threads.append(thread)

    def _pull_loop(self, route_name: str, sub_cfg: dict, subscription_path: str, loop: asyncio.AbstractEventLoop,
                   relay: PubsubRelay, stop: threading.Event) -> None:
        """Pull until disconnect; each message is processed on the event loop as the old callback was.

        The relay and the stop event are the thread's own arguments, not read off
        the adapter: a reconnect replaces both on the instance, and a thread still
        parked in a long poll from before it must see its own generation's stop
        and end, not the new generation's cleared one and pull on beside the new
        thread. A failed pull backs off from PULL_BACKOFF_INITIAL_SECONDS to the
        ceiling and logs what the relay said: a policy refusal names its rule, so a
        subscription the operator did not render reads as that rather than as a
        silent idle.
        """
        backoff = PULL_BACKOFF_INITIAL_SECONDS
        while not stop.is_set():
            try:
                received = relay.pull(subscription_path)
            except Exception as exc:
                if stop.is_set():
                    return
                logger.warning("PubSub: pull on route '%s' failed (%s); retrying in %ss", route_name, exc, backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, PULL_BACKOFF_MAX_SECONDS)
                continue
            backoff = PULL_BACKOFF_INITIAL_SECONDS
            if stop.is_set():
                # A pull parked at the broker outlives the join a reconnect or a
                # disconnect waits for; what it brings back belongs to the next
                # generation, so hand it straight back rather than hold it to the
                # ack deadline.
                if received:
                    self._return_to_subscription(route_name, subscription_path, relay, received)
                return
            if not received:
                stop.wait(PULL_IDLE_INTERVAL_SECONDS)
                continue
            for item in received:
                try:
                    message = RelayMessage(relay, subscription_path, item)
                except Exception as exc:
                    logger.error("PubSub: a message on route '%s' could not be decoded and is left to redeliver: %s", route_name, exc)
                    continue
                self._schedule(route_name, sub_cfg, message, loop)

    def _return_to_subscription(self, route_name: str, subscription_path: str, relay: "PubsubRelay",
                                received: List[dict]) -> None:
        ack_ids = [item.get("ackId") for item in received if item.get("ackId")]
        try:
            relay.nack(subscription_path, ack_ids)
            logger.warning("PubSub: route '%s' stopped with %d message(s) in hand; returned them for redelivery",
                           route_name, len(ack_ids))
        except Exception as exc:
            logger.warning("PubSub: route '%s' stopped with %d message(s) in hand and could not return them (%s); "
                           "they redeliver at the ack deadline", route_name, len(ack_ids), exc)

    def _schedule(self, route_name: str, cfg: dict, message: Any, loop: asyncio.AbstractEventLoop) -> None:
        logger.warning("PubSub: Callback entry received message on route '%s', msg_id=%s", route_name, getattr(message, 'message_id', 'unknown'))
        try:
            target_loop = getattr(self, "_main_loop", None) or loop
            if target_loop.is_closed():
                try:
                    target_loop = asyncio.get_event_loop()
                except Exception:
                    pass
            fut = asyncio.run_coroutine_threadsafe(self._process_message(route_name, cfg, message), target_loop)

            def _on_coro_done(f):
                exc = f.exception()
                if exc:
                    logger.error("PubSub: _process_message failed for route '%s': %s", route_name, exc)
            fut.add_done_callback(_on_coro_done)
        except Exception as e:
            logger.error("PubSub: Exception scheduling message callback for route '%s': %s", route_name, e)

    async def connect(self, is_reconnect: bool = False, **kwargs) -> bool:
        """Connect to the broker's relay and start pulling every configured route."""
        self._main_loop = asyncio.get_running_loop()
        if not self._subscriptions_config:
            logger.warning("PubSub: No subscriptions configured in config.yaml under platforms.pubsub.extra.subscriptions")
            return True

        relay_url = os.environ.get(PUBSUB_RELAY_URL_ENV, "").strip()
        if not relay_url:
            logger.error(
                "PubSub: %s is empty. The gateway holds no Google credential, so this adapter pulls only "
                "through the credential broker's relay. The operator fills the variable once a plugin route "
                "names a subscription it can qualify: a full projects/<project>/subscriptions/<name> path, "
                "or a short subscription or topic name on an agent whose spec.harness.projectId is set. "
                "The AgentPlugin's status names a route it could not qualify.", PUBSUB_RELAY_URL_ENV,
            )
            self._set_fatal_error("relay_not_configured", f"{PUBSUB_RELAY_URL_ENV} not set", retryable=False)
            return False
        # A reconnect that skipped disconnect() still ends the previous generation
        # first: its event is set and its threads are waited for, so no new pull
        # starts beside the new generation's. A thread still parked in a long poll
        # at the broker outlives the join; it returns what that poll delivers to
        # the subscription (the stop path in _pull_loop) instead of processing it.
        if self._pull_threads:
            self._stop_pulling.set()
            previous, self._pull_threads = self._pull_threads, []
            for thread in previous:
                await asyncio.to_thread(thread.join, PULL_THREAD_JOIN_SECONDS)
        self._relay = PubsubRelay(relay_url, os.environ.get(CREDENTIAL_PROXY_TOKEN_FILE_ENV, "").strip())
        # A new event per generation; the previous one stays set for any thread
        # still parked in a pull from before disconnect().
        self._stop_pulling = threading.Event()

        project_id = self._get_project_id()
        loop = asyncio.get_running_loop()
        started = 0
        for name, sub_cfg in self._subscriptions_config.items():
            self._warn_on_inert_config(name, sub_cfg)
            subscription_path = self._resolve_subscription_path(name, sub_cfg, project_id)
            if not subscription_path:
                logger.error("PubSub: Route '%s' skipped: it names no subscription this adapter can pull; the line above says why", name)
                continue
            self._check_subscription_exists(subscription_path)
            self._subscribe_to_route(name, sub_cfg, subscription_path, loop)
            started += 1

        # WARNING, not INFO: the gateway runs without -v, so INFO never reaches its log, and
        # this line is what an operator (and the presubmit's deploy gate) reads to know the
        # adapter is pulling through the broker rather than failing quietly.
        if started == 0:
            logger.error("PubSub: no route started; every configured route was skipped above, so nothing is pulled")
            self._mark_connected()
            return True
        logger.warning("PubSub: connected through the credential broker relay at %s for %d route(s)", relay_url, started)
        self._mark_connected()
        return True

    def _warn_on_inert_config(self, name: str, sub_cfg: dict) -> None:
        """Say at startup when a route sets something that cannot take effect.

        A setting that reads like a guarantee and does nothing is worse than one that is
        absent, because it is believed. `require_skills` reads as "no skill, no dispatch";
        on the kanban path skills are never loaded here in the first place — the worker
        loads them itself, in its own profile — so there is nothing for this gateway to
        find missing and the guard can only ever be a no-op.
        """
        if sub_cfg.get("require_skills") and str(sub_cfg.get("dispatch", "api")).lower() == "kanban":
            logger.warning(
                "PubSub: route '%s' sets require_skills together with dispatch: kanban, "
                "where it has no effect — the worker loads the skills in its own profile, "
                "so this gateway never sees one go missing. Drop the setting, or use "
                "dispatch: api if the guard is what you want.", name,
            )

    async def disconnect(self) -> None:
        """Stop every pull thread and forget the relay."""
        self._running = False
        self._stop_pulling.set()
        threads, self._pull_threads = self._pull_threads, []
        for thread in threads:
            await asyncio.to_thread(thread.join, PULL_THREAD_JOIN_SECONDS)
        self._relay = None
        self._mark_disconnected()
        logger.info("PubSub: Disconnected")

    async def _send_to_platform(self, deliver_type: str, delivery: dict, content: str) -> SendResult:
        """Deliver the generated response to a target platform (cross-platform routing)."""
        from gateway.config import Platform as CorePlatform
        target_platform = CorePlatform(deliver_type)
        adapter = self.gateway_runner.adapters.get(target_platform)
        if not adapter:
            return SendResult(success=False, error=f"Adapter for platform {deliver_type} not found")
            
        extra = delivery.get("deliver_extra", {})
        chat_id_target = extra.get("chat_id", "")
        if not chat_id_target:
            home = self.gateway_runner.config.get_home_channel(target_platform)
            if home:
                chat_id_target = home.chat_id
            else:
                return SendResult(
                    success=False,
                    error=f"No chat_id or home channel for {deliver_type}",
                )

        thread_id = extra.get("message_thread_id") or extra.get("thread_id")
        meta = None
        if thread_id:
            meta = {"thread_id": thread_id}

        return await adapter.send(chat_id_target, content, metadata=meta)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Process outbox message routing back to log or cross-platform targets."""
        delivery = self._delivery_info.get(chat_id, {})
        deliver_type = delivery.get("deliver", "log")

        if deliver_type == "log":
            # WARNING, not INFO: with deliver=log this line *is* the delivery. The gateway
            # runs without -v, so INFO never reaches stderr and the answer is lost.
            logger.warning("PubSub: Response for %s: %s", chat_id, content[:2000])
            return SendResult(success=True)

        if self.gateway_runner:
            try:
                return await self._send_to_platform(deliver_type, delivery, content)
            except Exception as e:
                logger.error("PubSub: Cross-platform delivery to %s failed: %s", deliver_type, e)
                return SendResult(success=False, error=str(e))

        logger.warning("PubSub: Unknown or unconfigured deliver type: %s", deliver_type)
        return SendResult(success=False, error=f"Unknown deliver type: {deliver_type}")



    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic metadata about the PubSub route channel."""
        return {"name": chat_id, "type": "pubsub"}

    def _parse_message_payload(self, message) -> dict:
        """Decode and parse incoming message body bytes (expect JSON)."""
        data_str = message.data.decode("utf-8")
        try:
            return json.loads(data_str)
        except json.JSONDecodeError:
            return {"text": data_str}

    def _apply_skills_to_prompt(self, prompt: str, skills: List[str]) -> Tuple[str, List[str]]:
        """Prepend or wrap the prompt inside specific skill invocations.

        Returns the prompt and the skills that could NOT be loaded.

        Two kinds of skill can be named here:

        * ``plugin:skill`` — registered by a plugin via ``ctx.register_skill()``. These
          resolve through ``skill_view()`` only; by design they never enter the flat
          skill index and so never appear as slash commands.
        * ``skill`` — a bundled skill in ``~/.hermes/skills/``, available as ``/skill``.

        A plugin skill only resolves in the profile that has the plugin installed. This
        adapter runs in the gateway, i.e. the default profile, so a skill belonging to a
        plugin with ``spec.targetProfile`` cannot be loaded here — which is why the caller
        must know what was missed rather than dispatching a prompt that names a skill the
        agent cannot open.
        """
        missing: List[str] = []
        for skill_name in skills:
            if ":" in skill_name:
                content = self._load_plugin_skill(skill_name)
                if content:
                    prompt = f"{content}\n\n---\n\n{prompt}"
                else:
                    missing.append(skill_name)
                continue
            wrapped = self._apply_bundled_skill(prompt, skill_name)
            if wrapped == prompt:
                missing.append(skill_name)
            prompt = wrapped
        return prompt, missing

    def _load_plugin_skill(self, qualified_name: str) -> Optional[str]:
        """Return the body of a plugin-registered skill, or None if unavailable."""
        try:
            from tools.skills_tool import skill_view

            content = skill_view(qualified_name)
            if content:
                logger.info("PubSub: Loaded plugin skill '%s'", qualified_name)
                return content
            logger.warning("PubSub: Plugin skill '%s' resolved to empty content", qualified_name)
        except Exception as e:
            logger.warning("PubSub: Plugin skill '%s' could not be loaded: %s", qualified_name, e)
        return None

    def _apply_bundled_skill(self, prompt: str, skill_name: str) -> str:
        """Wrap the prompt in a bundled skill's slash-command invocation, if present."""
        try:
            from agent.skill_commands import (
                build_skill_invocation_message,
                get_skill_commands,
            )

            cmd_key = f"/{skill_name}"
            if cmd_key in get_skill_commands():
                skill_content = build_skill_invocation_message(cmd_key, user_instruction=prompt)
                if skill_content:
                    return skill_content
            else:
                logger.warning("PubSub: Skill '%s' not found", skill_name)
        except Exception as e:
            logger.warning("PubSub: Skill loading failed: %s", e)
        return prompt

    def _dispatch_message(self, prompt: str, payload: dict, message_id: str, session_chat_id: str, route_name: str) -> None:
        """Build a MessageEvent object and process it using the base class handle_message handler."""
        source = self.build_source(
            chat_id=session_chat_id,
            chat_name=f"pubsub/{route_name}",
            chat_type="pubsub",
            user_id=f"pubsub:{route_name}",
            user_name=route_name,
        )
        event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=message_id,
        )

        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _eval_filter(self, expression: str, payload: dict) -> bool:
        """Evaluate a simple boolean filter expression against the payload."""
        try:
            or_parts = [op.strip() for op in expression.split(" or ")]
            for or_part in or_parts:
                if not or_part:
                    continue
                
                # Evaluate and-part
                and_match = True
                parts = [p.strip() for p in or_part.split(" and ")]
                for part in parts:
                    if not part:
                        continue
                    match = re.match(r"^([a-zA-Z0-9_.]+)\s*(==|!=)\s*[\'\"']?(.*?)[\'\"']?$", part)
                    if not match:
                        logger.warning("PubSub: Invalid filter expression format: %s", part)
                        and_match = False
                        break
                    path, op, expected = match.groups()
                    expected = expected.strip("'\"")

                    # Same resolver the dedup key uses, so a path that works in one works
                    # in the other. The previous inline walk stopped at the first list, so
                    # any expression reaching into `unhandledPodGroups.0...` silently
                    # resolved to empty — and a filter that always sees "" is a filter that
                    # quietly means something other than what it says.
                    val = _get_nested_value(payload, path)
                    actual = str(val) if val is not None else ""
                    logger.warning("PubSub: Filter check '%s' %s '%s' (actual: '%s')", path, op, expected, actual)
                    
                    if op == "==":
                        if actual != expected:
                            and_match = False
                            break
                    elif op == "!=":
                        if actual == expected:
                            and_match = False
                            break
                
                # If all AND conditions in this OR part matched, the filter passes!
                if and_match:
                    return True
                    
            return False
        except Exception as e:
            logger.error("PubSub: Exception evaluating filter '%s': %s", expression, e)
            return False

    def _dedup_key(self, payload: dict, dedup_fields: List[Any]) -> Dict[str, str]:
        """Resolve the configured dedup fields against a payload.

        Each entry is either a path, or a LIST of alternative paths where the first
        non-empty one wins. Alternatives exist because one route legitimately carries more
        than one payload shape: a cluster-autoscaler `noDecisionStatus.noScaleUp` event and
        a `resultInfo.results.errorMsg` event describe the same kind of incident under
        different keys, and the log query for this route matches both. Without
        alternatives, every field of the shape the config was not written for resolves
        empty, and the whole key degenerates.

        The label for an alternatives entry is its first path, so the recorded key stays
        stable as long as that path stays first.
        """
        values: Dict[str, str] = {}
        for field in dedup_fields:
            candidates = field if isinstance(field, (list, tuple)) else [field]
            label = str(candidates[0]) if candidates else ""
            resolved = ""
            for path in candidates:
                val = _get_nested_value(payload, str(path))
                if val is not None and str(val) != "":
                    resolved = str(val)
                    break
            values[label] = resolved
        return values

    def _is_duplicate(self, payload: dict, route_config: dict, route_name: str) -> bool:
        """Has this alert already been seen inside the dedup window?

        Records the alert when it has not, so the answer is also the decision. Lives in
        its own method rather than inline in the message handler so the behaviour can be
        tested without a Pub/Sub message, a gateway, or a cluster — see
        agentplugins/pubsub-platform/tests/test_dedup.py.

        Returns True when the caller should drop the message.
        """
        dedup_fields = route_config.get("deduplicate_fields")
        if not dedup_fields or os.environ.get("DISABLE_PUBSUB_DEDUP", "false").lower() == "true":
            return False

        current_values = self._dedup_key(payload, dedup_fields)

        # Every field resolved empty: this payload shape carries none of them. Keying on
        # that would make one incident stand for all of them — the next alert for ANY
        # workload matches it and is dropped for the whole window. A key that identifies
        # nothing must not suppress anything.
        if not any(current_values.values()):
            logger.warning(
                "PubSub: route '%s' — none of the deduplicate_fields resolved in this payload "
                "(shape mismatch?), so this alert is NOT deduplicated. Fields: %s",
                route_name, dedup_fields,
            )
            return False

        registry = self._load_registry()
        now = time.time()
        dedup_window = route_config.get("deduplicate_window_seconds", DEFAULT_DEDUP_WINDOW_SECONDS)

        cleaned_registry = []
        is_duplicate = False
        for entry in registry:
            # Every route shares one registry file, so retention has to follow the window
            # each entry was written under rather than the window of whichever route
            # happens to be doing the pruning. A five-minute route used to expire a daily
            # route's entries on its way past them, and the daily route then re-opened an
            # incident it had already filed — with nothing in either route's logs to say
            # who dropped the record.
            if now - entry.get("timestamp", 0) >= self._entry_window(entry, route_name, dedup_window):
                continue
            cleaned_registry.append(entry)
            if entry.get("route_name") == route_name and entry.get("field_values") == current_values:
                is_duplicate = True

        if is_duplicate:
            logger.warning(
                "PubSub: Duplicate message detected on route '%s' using fields %s. Skipping prompt triggering.",
                route_name, dedup_fields,
            )
            self._save_registry(cleaned_registry)
            return True

        cleaned_registry.append({
            "route_name": route_name,
            "timestamp": now,
            # Recorded so that a later prune, by any route, expires this entry on the
            # terms it was written under rather than on the pruning route's.
            "window_seconds": dedup_window,
            "field_values": current_values,
        })
        self._save_registry(cleaned_registry)
        return False

    def _entry_window(self, entry: dict, route_name: str, dedup_window: float) -> float:
        """How long a registry entry stays valid, in seconds.

        Entries written before the window was recorded carry none. An entry of the
        calling route's own falls back to that route's window, which is what it was
        written under; another route's falls back to the default rather than the
        caller's, because guessing short would expire it early — the exact failure this
        method exists to prevent.
        """
        window = entry.get("window_seconds")
        if isinstance(window, (int, float)) and not isinstance(window, bool) and window > 0:
            return float(window)
        if entry.get("route_name") == route_name:
            return float(dedup_window)
        return float(DEFAULT_DEDUP_WINDOW_SECONDS)

    def _get_registry_path(self) -> str:
        # Check if /opt/data is writable
        path = "/opt/data/pubsub_registry.json"
        if os.path.exists("/opt/data") and os.access("/opt/data", os.W_OK):
            return path
        return "./pubsub_registry.json"

    def _load_registry(self) -> List[dict]:
        path = self._get_registry_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("PubSub: Failed to load registry: %s", e)
            return []

    def _save_registry(self, registry: List[dict]) -> None:
        path = self._get_registry_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(registry, f, indent=2)
        except Exception as e:
            logger.warning("PubSub: Failed to save registry: %s", e)

    def _validate_message(self, payload: dict, route_config: dict, route_name: str) -> bool:
        """Programmatically validate the message payload using configured validation_code (if provided).

        Returns True if valid (process message), False if invalid (skip message).
        """
        validation_code = route_config.get("validation_code")
        if not validation_code:
            return True

        logger.info("PubSub: Running programmatic validation_code for route '%s'...", route_name)
        try:
            local_scope = {
                "payload": payload,
                "route_config": route_config,
                "route_name": route_name,
                "logger": logger,
                "is_valid": True,
                "result": None,
                "get_nested_value": _get_nested_value,
            }
            exec(validation_code, globals(), local_scope)

            if "validate" in local_scope and callable(local_scope["validate"]):
                res = local_scope["validate"](payload, route_config)
                return bool(res)

            if local_scope["result"] is not None:
                return bool(local_scope["result"])

            return bool(local_scope.get("is_valid", True))
        except Exception as e:
            logger.warning("PubSub: Exception during validation_code execution on route '%s': %s", route_name, e)
            return True

    async def _process_message(self, route_name: str, route_config: dict, message):
        """Asynchronously process incoming raw pub/sub pull messages with threshold, lock, deduplication, and validation."""
        try:
            # Acknowledge immediately to prevent redeliveries during long agent run
            message.ack()

            payload = self._parse_message_payload(message)

            filter_expr = route_config.get("filter")
            if filter_expr:
                if not self._eval_filter(filter_expr, payload):
                    logger.warning("PubSub: Message on route '%s' filtered out by expression '%s'", route_name, filter_expr)
                    return

            # --- Level 1: Filter threshold on the error message count appearing ---
            threshold_count = route_config.get("threshold_count")
            if threshold_count is not None and not os.environ.get("DISABLE_PUBSUB_DEDUP", "false").lower() == "true":
                now = time.time()
                window = route_config.get("threshold_window_seconds", 300)
                
                if route_name not in self._message_timestamps:
                    self._message_timestamps[route_name] = []
                
                self._message_timestamps[route_name].append(now)
                self._message_timestamps[route_name] = [
                    t for t in self._message_timestamps[route_name] if now - t <= window
                ]
                
                current_count = len(self._message_timestamps[route_name])
                if current_count < threshold_count:
                    logger.warning(
                        "PubSub: Message on route '%s' ignored. Count in window is %d/%d (threshold not met).",
                        route_name, current_count, threshold_count
                    )
                    return
                else:
                    logger.warning(
                        "PubSub: Message on route '%s' passed threshold count check: %d/%d in %ds window.",
                        route_name, current_count, threshold_count, window
                    )

            logger.warning("PubSub: Received message on route '%s'", route_name)

            # --- Level 2: Lock and Deduplication Registry ---
            if route_name not in self._locks:
                self._locks[route_name] = asyncio.Lock()

            async with self._locks[route_name]:
                # --- Level 3: Programmatic Message Validation ---
                if not self._validate_message(payload, route_config, route_name):
                    logger.warning(
                        "PubSub: Message on route '%s' invalidated by programmatic validation_code. Skipping prompt triggering.",
                        route_name
                    )
                    return

                if self._is_duplicate(payload, route_config, route_name):
                    return

                prompt_template = route_config.get("prompt", "")
                prompt = self._render_prompt(prompt_template, payload, route_name)

                skills = route_config.get("skills", [])
                target_profile = route_config.get("agent_profile")
                to_kanban = bool(target_profile) and str(route_config.get("dispatch", "api")).lower() == "kanban"

                # Skills are inlined only for turns that run HERE. On the kanban path the
                # worker loads them itself, in the profile that has them, so a local load
                # failure is expected and means nothing — enforcing it would refuse every
                # alert for a plugin that is correctly installed in another profile.
                missing_skills: List[str] = []
                if skills and not to_kanban:
                    prompt, missing_skills = self._apply_skills_to_prompt(prompt, skills)

                # A skill that could not be loaded used to pass silently: the prompt still
                # named it, the agent tried to open it, failed, and improvised — which
                # reads as a working investigation right up until you check whether the
                # skill was ever followed. Say it loudly, and tell the agent too, so its
                # answer cannot claim to have followed instructions it never read.
                if missing_skills:
                    logger.error(
                        "PubSub: route '%s' names skill(s) %s that could not be loaded in this "
                        "profile. A plugin skill only resolves where its plugin is installed; a "
                        "plugin with spec.targetProfile is not installed in the gateway's default "
                        "profile. Dispatch continues WITHOUT the skill text.",
                        route_name, missing_skills,
                    )
                    if route_config.get("require_skills"):
                        logger.error(
                            "PubSub: route '%s' sets require_skills, so this alert is NOT "
                            "dispatched. Fix the skill's installation rather than the alert.",
                            route_name,
                        )
                        return
                    prompt = (
                        "NOTE: the skill(s) "
                        + ", ".join(missing_skills)
                        + " were configured for this alert but could not be loaded here, so their "
                        "instructions are NOT in this prompt. Do not claim to have followed them. "
                        "Say so in your answer if you proceed without them.\n\n---\n\n"
                    ) + prompt

                message_id = message.message_id or str(int(time.time() * 1000))
                session_chat_id = f"pubsub:{route_name}:{message_id}"

                # Create event and dispatch
                source = self.build_source(
                    chat_id=session_chat_id,
                    chat_name=f"pubsub/{route_name}",
                    chat_type="pubsub",
                    user_id=f"pubsub:{route_name}",
                    user_name=route_name,
                )
                event = MessageEvent(
                    text=prompt,
                    message_type=MessageType.TEXT,
                    source=source,
                    raw_message=payload,
                    message_id=message_id,
                )

                session_key = build_session_key(
                    event.source,
                    group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
                    thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
                )

                # Route straight to a specialist profile when one is configured.
                # The default path lands on the Chat Agent, which must then delegate
                # via mcp-router and a kanban task; for a machine-originated alert
                # that hop adds failure modes without adding value.
                if target_profile:
                    # `dispatch: kanban` hands the alert to the board instead of running the
                    # turn here. The board's dispatcher spawns the worker AS that profile, so
                    # the profile's own plugins — and therefore its skills — are loaded, which
                    # a gateway-hosted turn only achieves when profile multiplexing is on.
                    if to_kanban:
                        await self._dispatch_via_kanban(
                            target_profile, prompt, skills, route_name, message_id
                        )
                    else:
                        await self._run_turn_via_api(target_profile, prompt, session_chat_id, route_name)
                    return

                # Recorded here and not before the branch above, because only the
                # gateway-hosted turn reports back through send(), which is the sole
                # reader: the kanban worker answers on its own task, and the API path
                # reads the answer out of the HTTP response. Both used to file an entry
                # that nothing would ever come back for.
                self._remember_delivery(session_chat_id, route_config, payload)

                await self.handle_message(event)

                task = self._session_tasks.get(session_key)
                if task:
                    logger.info("PubSub: Awaiting agent processing task for session %s under subscription lock...", session_key)
                    try:
                        await task
                        logger.info("PubSub: Agent processing task for session %s completed.", session_key)
                    except Exception as ex:
                        logger.error("PubSub: Agent processing task for session %s failed: %s", session_key, ex)
                else:
                    logger.warning("PubSub: No active session task found for session %s to await.", session_key)

        except Exception as e:
            logger.exception("PubSub: Error in _process_message on route %s: %s", route_name, e)


    def _remember_delivery(self, session_chat_id: str, route_config: dict, payload: dict) -> None:
        """Record where this alert's answer goes, for send() to read back.

        The key is per-message and send() cannot delete it — one turn may send several
        messages — so the map only ever grows, and the pod is long-lived. Bound it by
        evicting the oldest: an answer arriving after 256 later alerts is not one anybody
        is still waiting on. Insertion order is dict order, so the first key is the
        oldest.

        The payload is deliberately not kept. Nothing reads it back, and holding every
        alert body for the life of the process is the expensive half of the leak.
        """
        while len(self._delivery_info) >= self._MAX_DELIVERY_ENTRIES:
            self._delivery_info.pop(next(iter(self._delivery_info)), None)
        self._delivery_info[session_chat_id] = {
            "deliver": route_config.get("deliver", "log"),
            "deliver_extra": self._render_delivery_extra(
                route_config.get("deliver_extra", {}), payload
            ),
        }

    def _hermes_bin(self) -> str:
        """Path to the `hermes` CLI that runs this gateway.

        Resolved from the running interpreter rather than PATH: the gateway is started
        from its virtualenv, and PATH inside a plugin call is whatever the pod was given.
        """
        import shutil
        import sys

        candidate = os.path.join(os.path.dirname(sys.executable), "hermes")
        if os.path.exists(candidate):
            return candidate
        return shutil.which("hermes") or "hermes"

    async def _dispatch_via_kanban(
        self, profile: str, prompt: str, skills: List[str], route_name: str, message_id: str
    ) -> None:
        """File the alert as a kanban task owned by `profile`, and return.

        Why not run the turn here: this adapter lives in the gateway, which is the default
        profile. A plugin installed with `spec.targetProfile` is not loaded there, so a
        turn run here cannot open that plugin's skills no matter how the prompt is worded
        — the agent tries the name, fails, and improvises. The board's dispatcher spawns
        the worker as the assignee profile, where the plugin IS loaded.

        The skills go two ways on purpose: `--skill` force-loads them for the worker, and
        the body repeats the qualified `plugin:skill` name because agents reach for the
        bare name and that form only resolves for bundled skills in ~/.hermes/skills/.

        Fire-and-forget: the board is the queue, and the worker reports through its own
        task. Nothing is delivered back through `deliver:` on this path.
        """
        import subprocess

        title = f"{route_name} alert {message_id}"
        body = prompt
        if skills:
            body += (
                "\n\n---\n\nFollow the skill(s): "
                + ", ".join(skills)
                + ". Open them with skill_view using the name exactly as written, including the "
                "`plugin:skill` prefix — the bare name does not resolve for plugin skills."
            )

        cmd = [self._hermes_bin(), "kanban", "create", title,
               "--assignee", profile, "--body", body, "--json"]
        for skill in skills:
            cmd += ["--skill", skill]

        # HOME=/tmp for the same reason every other agent-side subprocess sets it: the
        # real home is not writable under RunAsNonRoot.
        env = {**os.environ, "HOME": "/tmp"}
        try:
            proc = await asyncio.to_thread(
                subprocess.run, cmd, capture_output=True, text=True, timeout=60, env=env
            )
        except Exception as e:  # noqa: BLE001 - a failed dispatch must be visible, not fatal
            logger.error("PubSub: could not create a kanban task for route '%s': %s", route_name, e)
            return

        if proc.returncode != 0:
            logger.error(
                "PubSub: kanban create failed for route '%s' (exit %s): %s",
                route_name, proc.returncode, (proc.stderr or proc.stdout or "").strip()[:400],
            )
            return

        task_id = ""
        try:
            task_id = (json.loads(proc.stdout) or {}).get("id", "")
        except Exception:  # noqa: BLE001 - the task exists either way; only the id is lost
            pass
        logger.warning(
            "PubSub: filed alert on route '%s' as kanban task %s for profile '%s' with skills %s",
            route_name, task_id or "(id unknown)", profile, skills or "[]",
        )

    async def _run_turn_via_api(self, profile: str, prompt: str, session_chat_id: str, route_name: str) -> None:
        """Run one agent turn directly against a named profile via the gateway API.

        Mirrors what agents/platform/scripts/session_kv_server.py does for event-watcher
        alerts: create a session, then post the query. Requests are prefixed with
        ``/p/<profile>/`` so the turn resolves that profile's config, skills and
        credentials instead of the Chat Agent's.
        """
        import urllib.error
        import urllib.request

        api = os.environ.get("PLATFORM_API_URL", "http://127.0.0.1:8642")
        key = os.environ.get("API_SERVER_KEY", "")
        base = f"{api}/p/{profile}/api" if profile != "default" else f"{api}/api"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        session_id = re.sub(r"[^A-Za-z0-9_-]", "-", session_chat_id)

        def _post(url: str, body: dict, timeout: float):
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
            )
            return urllib.request.urlopen(req, timeout=timeout)

        try:
            await asyncio.to_thread(
                _post, f"{base}/sessions",
                # Title must be unique across sessions: the API rejects a reused one
                # with 400 "Title already in use", so key it to this alert.
                {"session_id": session_id, "title": f"{route_name} {session_id}"}, 15.0,
            )
        except urllib.error.HTTPError as e:
            # 409 = session already exists; 400 with a duplicate-title body means the
            # same alert is being retried. Both are safe to continue from.
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code != 409 and not (e.code == 400 and "already in use" in body):
                logger.error("PubSub: could not create %s session %s: HTTP %s %s",
                             profile, session_id, e.code, body[:200])
                return
        except Exception as e:
            logger.error("PubSub: could not reach gateway API for profile %s: %s", profile, e)
            return

        logger.warning("PubSub: dispatching alert to profile '%s' session %s", profile, session_id)
        try:
            resp = await asyncio.to_thread(_post, f"{base}/sessions/{session_id}/chat", {"message": prompt}, 900.0)
            body = resp.read().decode("utf-8", "replace")
            logger.warning("PubSub: profile '%s' answered for %s: %s", profile, session_id, body[:2000])
        except Exception as e:
            logger.error("PubSub: agent turn failed on profile %s session %s: %s", profile, session_id, e)

    def _render_prompt(
        self,
        template: str,
        payload: dict,
        route_name: str,
    ) -> str:
        """Resolve placeholders in prompt template with matching payload fields."""
        if not template:
            truncated = json.dumps(payload, indent=2)[:4000]
            return (
                f"Pub/Sub notification on route "
                f"'{route_name}':\n\n```json\n{truncated}\n```"
            )

        def _resolve(match: re.Match) -> str:
            key = match.group(1)
            if key == "__raw__":
                return json.dumps(payload, indent=2)[:4000]
            value: Any = payload
            for part in key.split("."):
                if isinstance(value, dict):
                    value = value.get(part, f"{{{key}}}")
                else:
                    return f"{{{key}}}"
            if isinstance(value, (dict, list)):
                return json.dumps(value, indent=2)[:2000]
            return str(value)

        return re.sub(r"\{([a-zA-Z0-9_.]+)\}", _resolve, template)

    def _render_delivery_extra(
        self, extra: dict, payload: dict
    ) -> dict:
        """Resolve fields in deliver_extra config using payload keys dynamically."""
        rendered: Dict[str, Any] = {}
        for key, value in extra.items():
            if isinstance(value, str):
                rendered[key] = self._render_prompt(value, payload, "")
            else:
                rendered[key] = value
        return rendered


def register(ctx):
    """Register the pubsub platform adapter with the gateway context."""
    ctx.register_platform(
        name="pubsub",
        label="PubSub",
        adapter_factory=lambda cfg: PubSubAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=[],
        install_hint="nothing to install: the adapter speaks to the credential broker's relay",
        max_message_length=0,
        emoji="🔔",
        pii_safe=True,
        allow_update_command=False,
        platform_hint="You are receiving events via Google Cloud Pub/Sub pull subscription.",
        allowed_users_env="PUBSUB_ALLOWED_USERS",
        allow_all_env="PUBSUB_ALLOW_ALL_USERS",
    )

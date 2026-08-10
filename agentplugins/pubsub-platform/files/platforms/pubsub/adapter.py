import asyncio
import json
import logging
import os
import re
import time

os.environ["PUBSUB_HOME_CHANNEL"] = "none"
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Lazy import helper to verify dependencies
def check_requirements() -> bool:
    try:
        from google.cloud import pubsub_v1
        return True
    except ImportError:
        return False

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



class PubSubAdapter(BasePlatformAdapter):
    """Google Cloud Pub/Sub pull subscriber adapter for Hermes Agent.
    
    This platform adapter connects to GCP Pub/Sub pull subscriptions, consumes incoming 
    messages (e.g. log-based alerts from Cloud Logging sinks), formats them using custom 
    templates, optionally wraps them into agent skill invocations, and routes them to the 
    core agent conversation loop.
    
    It also supports sending agent responses back to other chat platforms (cross-platform 
    delivery) or writing them to the system logs.
    """

    def __init__(self, config: PlatformConfig):
        """Initialize the adapter with PlatformConfig configurations."""
        super().__init__(config, Platform("pubsub"))
        extra = config.extra or {}
        self._subscriptions_config: Dict[str, dict] = extra.get("subscriptions", {})
        self._subscriber: Optional[Any] = None
        self._streaming_pull_futures: List[Any] = []
        self._delivery_info: Dict[str, dict] = {}
        self.gateway_runner = None
        self._locks: Dict[str, asyncio.Lock] = {}
        self._message_timestamps: Dict[str, List[float]] = {}

    def _get_project_id(self) -> Optional[str]:
        """Detect GCP Project ID from auth credentials or environment variables."""
        try:
            import google.auth
            _, project_id = google.auth.default()
            if not project_id:
                project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
            return project_id
        except Exception as e:
            logger.warning("PubSub: Failed to detect GCP project ID: %s", e)
            return None

    def _parse_topic_config(self, sub_cfg: dict, project_id: str, name: str) -> Optional[tuple]:
        """Parse topic path and simple name from config."""
        topic_input = sub_cfg.get("topic")
        if not topic_input:
            return None
        if topic_input.startswith("projects/"):
            return topic_input, topic_input.split("/")[-1]
        return f"projects/{project_id}/topics/{topic_input}", topic_input

    def _parse_subscription_config(self, sub_cfg: dict, project_id: str, topic_name: Optional[str]) -> str:
        """Parse or generate subscription path based on topic or configured subscription name."""
        sub_input = sub_cfg.get("subscription")
        if not sub_input:
            if not topic_name:
                raise ValueError("Either 'topic' or 'subscription' must be configured.")
            sub_name = f"{topic_name}-sub"
            return f"projects/{project_id}/subscriptions/{sub_name}"
        if sub_input.startswith("projects/"):
            return sub_input
        return f"projects/{project_id}/subscriptions/{sub_input}"

    def _check_topic_exists(self, publisher, topic_path: str) -> bool:
        """Verify if the Pub/Sub topic exists and log clearly if not present."""
        try:
            from google.api_core.exceptions import NotFound
        except Exception:
            class NotFound(Exception):
                pass
        try:
            publisher.get_topic(request={"topic": topic_path})
            logger.info("PubSub: Topic '%s' exists", topic_path)
            return True
        except NotFound:
            logger.warning("PubSub: Topic '%s' is NOT present in GCP. Please ensure it is created prior to running.", topic_path)
            return False
        except Exception as e:
            logger.warning("PubSub: Could not verify topic '%s': %s", topic_path, e)
            return False

    def _check_subscription_exists(self, subscriber, sub_path: str) -> bool:
        """Verify if the subscription exists and log clearly if not present."""
        try:
            from google.api_core.exceptions import NotFound
        except Exception:
            class NotFound(Exception):
                pass
        try:
            subscriber.get_subscription(request={"subscription": sub_path})
            logger.info("PubSub: Subscription '%s' exists", sub_path)
            return True
        except NotFound:
            logger.warning("PubSub: Subscription '%s' is NOT present in GCP. Please ensure it is created prior to running.", sub_path)
            return False
        except Exception as e:
            logger.warning("PubSub: Could not verify subscription '%s': %s", sub_path, e)
            return False

    def _check_log_sink_exists(self, project_id: str, name: str, topic_path: str, query: str) -> bool:
        """Check if GCP Log Sink exists and log clearly if not present."""
        try:
            from google.cloud import logging as gcp_logging
            logging_client = gcp_logging.Client(project=project_id)
            sink_name = f"hermes-pubsub-{name}-sink"
            destination = f"pubsub.googleapis.com/{topic_path}"
            sink = logging_client.sink(sink_name, filter_=query, destination=destination)
            if sink.exists():
                logger.info("PubSub: Log sink '%s' exists", sink_name)
                return True
            else:
                logger.warning("PubSub: Log sink '%s' is NOT present in GCP.", sink_name)
                return False
        except Exception as e:
            logger.warning("PubSub: Could not verify Log Sink 'hermes-pubsub-%s-sink': %s", name, e)
            return False

    def _check_resources(self, name: str, sub_cfg: dict, project_id: str) -> str:
        """Check presence of GCP Pub/Sub topics, subscriptions, and log sinks without creating them."""
        from google.cloud import pubsub_v1

        topic_info = self._parse_topic_config(sub_cfg, project_id, name)
        if not topic_info:
            logger.info("PubSub: No 'topic' configured for route '%s'. Skipping resource checks.", name)
            return self._parse_subscription_config(sub_cfg, project_id, None)

        topic_path, topic_name = topic_info
        sub_path = self._parse_subscription_config(sub_cfg, project_id, topic_name)

        try:
            publisher = pubsub_v1.PublisherClient()
            subscriber = pubsub_v1.SubscriberClient()

            self._check_topic_exists(publisher, topic_path)
            self._check_subscription_exists(subscriber, sub_path)

            query = sub_cfg.get("query")
            if query:
                self._check_log_sink_exists(project_id, name, topic_path, query)
        except Exception as e:
            logger.warning("PubSub: Resource presence check failed for '%s': %s", name, e)

        return sub_path

    def _resolve_subscription_path(self, name: str, sub_cfg: dict, project_id: Optional[str]) -> Optional[str]:
        """Resolve full GCP resource path for a configured subscription name/route."""
        subscription_path = None
        if project_id:
            try:
                subscription_path = self._check_resources(name, sub_cfg, project_id)
            except Exception as e:
                logger.warning("PubSub: Resource check failed for '%s': %s. Falling back to configured path.", name, e)

        if not subscription_path:
            subscription_path = sub_cfg.get("subscription")
            if subscription_path and not subscription_path.startswith("projects/") and project_id:
                subscription_path = f"projects/{project_id}/subscriptions/{subscription_path}"
        return subscription_path

    def _subscribe_to_route(self, name: str, sub_cfg: dict, subscription_path: str, loop: asyncio.AbstractEventLoop) -> None:
        """Initiate asynchronous background pull streaming subscription on the path."""
        logger.info("PubSub: Subscribing to '%s' (route '%s')", subscription_path, name)

        def make_callback(route_name=name, cfg=sub_cfg):
            def callback(message):
                logger.warning("PubSub: Callback entry received message on route '%s', msg_id=%s", route_name, getattr(message, 'message_id', 'unknown'))
                try:
                    target_loop = getattr(self, "_main_loop", None) or loop
                    if target_loop.is_closed():
                        try:
                            target_loop = asyncio.get_event_loop()
                        except Exception:
                            pass
                    fut = asyncio.run_coroutine_threadsafe(
                        self._process_message(route_name, cfg, message),
                        target_loop
                    )
                    def _on_coro_done(f):
                        exc = f.exception()
                        if exc:
                            logger.error("PubSub: _process_message failed for route '%s': %s", route_name, exc)
                    fut.add_done_callback(_on_coro_done)
                except Exception as e:
                    logger.error("PubSub: Exception scheduling message callback for route '%s': %s", route_name, e)
            return callback

        try:
            future = self._subscriber.subscribe(
                subscription_path,
                callback=make_callback(name, sub_cfg)
            )
            def _on_future_done(f):
                exc = f.exception()
                if exc:
                    logger.error("PubSub: StreamingPullFuture for route '%s' terminated with error: %s", name, exc)
            future.add_done_callback(_on_future_done)
            self._streaming_pull_futures.append(future)
        except Exception as e:
            logger.error("PubSub: Failed to subscribe to '%s': %s", subscription_path, e)

    async def connect(self, is_reconnect: bool = False, **kwargs) -> bool:
        """Establish connection and start listening to all configured Pub/Sub subscriptions."""
        self._main_loop = asyncio.get_running_loop()
        if not self._subscriptions_config:
            logger.warning("PubSub: No subscriptions configured in config.yaml under platforms.pubsub.extra.subscriptions")
            return True

        if not check_requirements():
            logger.error("PubSub: google-cloud-pubsub library is not installed")
            self._set_fatal_error("dependencies_missing", "google-cloud-pubsub not installed", retryable=False)
            return False

        from google.cloud import pubsub_v1

        try:
            self._subscriber = pubsub_v1.SubscriberClient()
        except Exception as e:
            logger.error("PubSub: Failed to initialize SubscriberClient: %s", e)
            self._set_fatal_error("client_init_failed", str(e), retryable=True)
            return False

        project_id = self._get_project_id()
        loop = asyncio.get_running_loop()

        for name, sub_cfg in self._subscriptions_config.items():
            subscription_path = self._resolve_subscription_path(name, sub_cfg, project_id)
            if not subscription_path:
                logger.error("PubSub: Route '%s' missing 'subscription' path", name)
                continue
            self._subscribe_to_route(name, sub_cfg, subscription_path, loop)

        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        """Disconnect and clean up background subscription pulls."""
        self._running = False
        for future in self._streaming_pull_futures:
            try:
                future.cancel()
            except Exception:
                pass
        self._streaming_pull_futures.clear()
        if self._subscriber:
            try:
                self._subscriber.close()
            except Exception:
                pass
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
        dedup_window = route_config.get("deduplicate_window_seconds", 86400)  # 24 hours default

        cleaned_registry = []
        is_duplicate = False
        for entry in registry:
            if now - entry.get("timestamp", 0) < dedup_window:
                cleaned_registry.append(entry)
                if entry.get("route_name") == route_name:
                    if entry.get("field_values") == current_values:
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
            "field_values": current_values,
        })
        self._save_registry(cleaned_registry)
        return False

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

                deliver_config = {
                    "deliver": route_config.get("deliver", "log"),
                    "deliver_extra": self._render_delivery_extra(
                        route_config.get("deliver_extra", {}), payload
                    ),
                    "payload": payload,
                }
                self._delivery_info[session_chat_id] = deliver_config

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
        install_hint="pip install google-cloud-pubsub",
        max_message_length=0,
        emoji="🔔",
        pii_safe=True,
        allow_update_command=False,
        platform_hint="You are receiving events via Google Cloud Pub/Sub pull subscription.",
        allowed_users_env="PUBSUB_ALLOWED_USERS",
        allow_all_env="PUBSUB_ALLOW_ALL_USERS",
    )

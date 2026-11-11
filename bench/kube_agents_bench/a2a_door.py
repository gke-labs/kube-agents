"""A minimal client for the gateway's A2A door, for card-wait stopped asks.

The bench drives the agent through the inject door (:mod:`inject_transport`),
whose turns no longer hold for their Kanban cards (#5179). A2A door turns
still hold (``waitsForCardsInTurn`` in ``a2a/hermes-bridge``), so a case that
stops a turn during that hold (:data:`stop_hook.CARD_WAIT_DIRECTIVE`, #5407)
has to go through the door. This is only what that case needs: JSON-RPC
``message/send`` (non-blocking), ``tasks/get`` and ``tasks/cancel`` on
``POST /a2a`` (``a2a/gateway/a2awire.go``), with the door's bearer token and
its caller header.

The token is the door's own, per install (Secret ``<agent>-a2a-door``, key
``token``). It is sent only in the ``Authorization`` header and never appears
in a message, an exception, a repr or a trajectory entry.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from kube_agents_bench import inject_transport as inject

#: The door's path, and the header that names the caller.
RPC_PATH = "/a2a"
CALLER_HEADER = "X-A2A-Caller"
#: The caller the operator maps for eval installs (a2aDoorCaller in
#: k8s-operator's platformagent_a2a_manifests.go).
DEFAULT_CALLER = "external-agent"
#: The Service suffix, port and token Secret the operator renders for the door.
SERVICE_SUFFIX = "-a2a-door"
REMOTE_PORT = 8098
TOKEN_SECRET_SUFFIX = "-a2a-door"
TOKEN_SECRET_KEY = "token"  # a Secret key name, not a credential

#: Task states the door reports that end a task.
TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})
#: The state of a task an executor is running.
STATE_WORKING = "working"


class DoorUnavailable(RuntimeError):
    """The exchange didn't reach the door, or the door refused it. Never the
    agent's answer. ``retryable`` says whether the same call could succeed
    after the tunnel is respawned."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class DoorError(RuntimeError):
    """A JSON-RPC error the door answered with: ``code`` and its message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"A2A door error {code}: {message}")
        self.code = code


@dataclass
class Door:
    """The door at ``base_url``, called as ``caller``, with ``token``."""

    base_url: str
    token: str = field(repr=False)
    caller: str = DEFAULT_CALLER
    timeout: float = 30.0

    def send(self, text: str, *, message_id: str, context_id: str | None = None) -> dict[str, Any]:
        """``message/send``, non-blocking: the task the door started."""
        message: dict[str, Any] = {
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
            "messageId": message_id,
        }
        if context_id:
            message["contextId"] = context_id
        return self._call("message/send", {"message": message, "configuration": {"blocking": False}})

    def get(self, task_id: str) -> dict[str, Any]:
        """``tasks/get``: the task as the door reports it now."""
        return self._call("tasks/get", {"id": task_id})

    def cancel(self, task_id: str) -> dict[str, Any]:
        """``tasks/cancel``: returns once the cancel is on the bus. The task's
        terminal comes later, through :meth:`get`."""
        return self._call("tasks/cancel", {"id": task_id})

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params})
        request = urllib.request.Request(
            self.base_url.rstrip("/") + RPC_PATH,
            data=body.encode("utf-8"),
            headers={
                inject.AUTHORIZATION_HEADER: inject.BEARER_PREFIX + self.token,
                CALLER_HEADER: self.caller,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with inject._OPENER.open(request, timeout=self.timeout) as response:
                reply = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise DoorUnavailable(
                    f"the A2A door refused the bearer token on {method}: read it from the "
                    f"<agent>{TOKEN_SECRET_SUFFIX} Secret's `{TOKEN_SECRET_KEY}` key",
                    retryable=False,
                ) from None
            raise DoorUnavailable(
                f"HTTP {exc.code} from the A2A door on {method}",
                retryable=exc.code in inject.RETRYABLE_STATUSES,
            ) from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise DoorUnavailable(f"{type(exc).__name__} calling the A2A door's {method}") from None
        if not isinstance(reply, dict):
            raise DoorUnavailable(f"the A2A door answered {method} with non-object JSON", retryable=False)
        if reply.get("error"):
            err = reply["error"] if isinstance(reply["error"], dict) else {}
            raise DoorError(int(err.get("code") or 0), str(err.get("message") or ""))
        result = reply.get("result")
        if not isinstance(result, dict):
            raise DoorUnavailable(f"the A2A door's {method} answer carries no result object", retryable=False)
        return result


def task_state(task: dict[str, Any]) -> str:
    """A task's state, ``""`` when the door named none."""
    status = task.get("status")
    return str(status.get("state") or "") if isinstance(status, dict) else ""


def task_text(task: dict[str, Any]) -> str:
    """The text of a task's status message, or of its result artifact when the
    status carries none: what the agent said."""
    status = task.get("status") if isinstance(task.get("status"), dict) else {}
    texts = [_parts_text(status.get("message"))]
    for artifact in task.get("artifacts") or []:
        if isinstance(artifact, dict):
            texts.append(_parts_text(artifact))
    return next((t for t in texts if t), "")


def _parts_text(holder: Any) -> str:
    if not isinstance(holder, dict):
        return ""
    return "\n".join(
        str(p.get("text") or "") for p in holder.get("parts") or [] if isinstance(p, dict) and p.get("kind") == "text"
    ).strip()

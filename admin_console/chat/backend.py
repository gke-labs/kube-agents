"""Adapters from the shared interaction service to the existing runtime clients."""

from __future__ import annotations

from typing import Callable, Protocol, Sequence

from admin_console import agent_chat, agent_runtime
from admin_console.agent_chat import ChatRunResult
from admin_console.agent_runtime import TaskUpdateResult
from admin_console.connection_persistence import load_connection
from admin_console.project_config import DeploymentTarget


class ChatBackend(Protocol):
    def run(
        self,
        agent: str,
        *,
        prompt: str,
        session_id: str,
        history: Sequence[dict[str, str]],
        profile: str,
        user_email: str,
        timeout: int,
        on_update: Callable[[ChatRunResult], None] | None = None,
    ) -> ChatRunResult: ...

    def resolve_approval(
        self,
        agent: str,
        *,
        run_id: str,
        choice: str,
        profile: str,
        timeout: int,
    ) -> ChatRunResult: ...

    def stop(self, agent: str, *, run_id: str, profile: str) -> None: ...

    def get_task_updates(
        self,
        agent: str,
        *,
        session_id: str,
        limit: int = 100,
    ) -> TaskUpdateResult: ...


class RuntimeChatBackend:
    """One target-bound facade over the existing chat and runtime providers."""

    def __init__(self, target: DeploymentTarget) -> None:
        self._chat = agent_chat.AgentChatProvider(target)
        self._runtime = agent_runtime.AgentRuntimeProvider(target)

    def run(self, *args, **kwargs) -> ChatRunResult:
        return self._chat.run(*args, **kwargs)

    def resolve_approval(self, *args, **kwargs) -> ChatRunResult:
        return self._chat.resolve_approval(*args, **kwargs)

    def stop(self, *args, **kwargs) -> None:
        self._chat.stop(*args, **kwargs)

    def get_task_updates(self, *args, **kwargs) -> TaskUpdateResult:
        return self._runtime.get_task_updates(*args, **kwargs)


BackendFactory = Callable[[], ChatBackend]


def persisted_backend_factory(account: str) -> BackendFactory:
    """Resolve the portal's verified connection when an interaction starts."""

    def build() -> ChatBackend:
        connection = load_connection(account)
        if connection is None or not connection.usable:
            raise RuntimeError(
                "No verified portal connection is available. Open Connection and connect "
                "to a kube-agents host first."
            )
        return RuntimeChatBackend(connection.target)

    return build

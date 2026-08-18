"""Unified agent conversation workspace."""

from __future__ import annotations

import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import streamlit as st

from admin_console.connection_session import recover_app_shell
from admin_console.connection_gate import require_connection
from admin_console.agent_runtime import (
    TaskUpdateResult,
)
from admin_console.clients.portal_api import (
    InteractionView,
    PortalApiClient,
    PortalApiError,
)
from admin_console.chat.models import is_portal_session_id
from admin_console.chat.service import ACTIVE_TASK_STATUSES
from admin_console.project_config import DeploymentTarget
from admin_console.ui import AGENT_SELECTOR_HELP, paginated_selectable_table

HISTORY_WINDOWS = {
    "24h": ("24 hours", 24),
    "168h": ("7 days", 168),
    "720h": ("30 days", 720),
    "all": ("All retained", 0),
}
TASK_POLL_INTERVAL = "5s"
TASK_EMPTY_POLL_LIMIT = 3
TASK_ERROR_POLL_LIMIT = 3

recover_app_shell()


def query_value(name: str) -> str:
    return str(st.query_params.get(name, "")).strip()


def set_query(name: str, value: str) -> None:
    if value:
        if query_value(name) != value:
            st.query_params[name] = value
    else:
        st.query_params.pop(name, None)


def new_session_key() -> str:
    return f"default:portal_{uuid.uuid4().hex}"


def split_session_key(value: str) -> tuple[str, str]:
    if ":" not in value:
        return "", ""
    profile, session_id = value.split(":", 1)
    return profile, session_id


def session_subject(conversation) -> str:
    """Return a concise user-facing title for a persisted agent session."""
    subject = (
        conversation.title
        or conversation.preview.replace("\n", " ")
        or conversation.session_id
    )
    worker = re.fullmatch(
        r"work\s+kanban(?:\s+task)?\s+([A-Za-z0-9_.:-]+)",
        subject.strip(),
        flags=re.IGNORECASE,
    )
    if worker:
        return f"Task Kanban · {worker.group(1)}"
    return subject


def finish_interaction(
    result: InteractionView,
    messages: list[dict[str, str]],
    state_key: str,
) -> None:
    active = st.session_state.setdefault("portal_active_interactions", {})
    last = st.session_state.setdefault("portal_last_interactions", {})
    last[state_key] = result
    if result.status == "completed":
        messages.append(
            {
                "role": "assistant",
                "content": result.output or "Completed without a text response.",
            }
        )
    else:
        detail = result.error or f"Agent run ended with status {result.status}."
        messages.append({"role": "assistant", "content": f"Run failed: {detail}"})
    active.pop(state_key, None)


@st.cache_resource
def portal_api(target: DeploymentTarget) -> PortalApiClient:
    return PortalApiClient(target)


def task_fingerprint(result: TaskUpdateResult) -> tuple:
    """Return only user-visible changes that warrant repainting task cards."""
    return tuple(
        (
            task.task_id,
            task.title,
            task.assignee,
            task.status,
            task.summary,
            task.error,
            task.run_count,
            task.latest_event,
            task.previous_error,
        )
        for task in result.tasks
    ) + (("truncated", result.truncated),)


def has_active_tasks(result: TaskUpdateResult) -> bool:
    return any(task.status in ACTIVE_TASK_STATUSES for task in result.tasks)


def render_task_cards(
    result: TaskUpdateResult,
    *,
    target: DeploymentTarget,
    selected_agent: str,
) -> None:
    if not result.tasks:
        return

    st.markdown("#### Agent work")
    for task in result.tasks:
        with st.container(border=True):
            st.markdown(f"**{task.assignee}** · {task.status}")
            st.write(task.title or task.task_id)
            if task.summary:
                st.success(task.summary)
            elif task.error:
                st.error(task.error)
            elif task.status in ACTIVE_TASK_STATUSES:
                progress = []
                if task.run_count:
                    progress.append(f"run {task.run_count}")
                if task.latest_event:
                    progress.append(task.latest_event)
                progress.append(f"updated {task.updated_at:%Y-%m-%d %H:%M UTC}")
                st.caption("In progress · " + " · ".join(progress))
                if task.previous_error and task.run_count > 1:
                    st.warning(f"Previous run failed: {task.previous_error}")
            if task.status == "done" and task.run_count > 1:
                st.caption(f"Completed after {task.run_count} runs")
            st.page_link(
                "pages/kanban.py",
                label=task.task_id,
                icon=":material/open_in_new:",
                help="Open this task in Task Kanban",
                query_params={
                    "project": target.project_id,
                    "cluster": target.cluster_name,
                    "location": target.location,
                    "kanban_agent": selected_agent,
                    "kanban_task": task.task_id,
                },
            )
    if result.truncated:
        st.warning("Only the first 100 linked tasks are shown.")


st.title("Chat")
target = require_connection()

set_query("project", target.project_id)
set_query("cluster", target.cluster_name)
set_query("location", target.location)
st.caption(f"{target.project_id} · {target.cluster_name} · {target.namespace}")
runtime_provider = portal_api(target)

try:
    agents = runtime_provider.list_agents()
except PortalApiError as exc:
    st.error(str(exc))
    if exc.guidance:
        st.caption(exc.guidance)
    st.caption("Choose another project or cluster on Connection.")
    st.stop()

if not agents:
    st.warning("No running kube-agents gateway was found in this scope.")
    st.caption("Choose another project or cluster on Connection.")
    st.stop()

requested_agent = query_value("chat_agent")
selected_agent = st.selectbox(
    "Agent",
    agents,
    index=agents.index(requested_agent) if requested_agent in agents else 0,
    help=AGENT_SELECTOR_HELP,
)
set_query("chat_agent", selected_agent)
st.query_params.pop("chat_view", None)

requested_window = query_value("chat_window") or "all"
if requested_window not in HISTORY_WINDOWS:
    requested_window = "all"
workspace_key = f"{target.project_id}:{target.cluster_name}:{selected_agent}"
buffers = st.session_state.setdefault("portal_chat_buffers", {})
followup_context = st.session_state.setdefault("portal_followup_context", {})

st.markdown("## Sessions")
filters = st.columns([1, 1, 2, 1], vertical_alignment="bottom")
selected_window = filters[0].selectbox(
    "History",
    list(HISTORY_WINDOWS),
    index=list(HISTORY_WINDOWS).index(requested_window),
    format_func=lambda value: HISTORY_WINDOWS[value][0],
)
set_query("chat_window", selected_window)

hours = HISTORY_WINDOWS[selected_window][1]
cutoff = (
    datetime.now(UTC) - timedelta(hours=hours)
    if hours
    else datetime.fromtimestamp(0, UTC)
)
try:
    with st.spinner("Reading sessions…"):
        history = runtime_provider.list_conversations(selected_agent, cutoff=cutoff)
except PortalApiError as exc:
    st.error(str(exc))
    if exc.guidance:
        st.caption(exc.guidance)
    st.stop()

all_conversations = history.conversations
conversation_by_key = {
    f"{item.profile}:{item.session_id}": item for item in all_conversations
}

platforms = sorted({item.platform for item in all_conversations})
requested_platform = query_value("chat_platform")
platform_options = ["", *platforms]
selected_platform = filters[1].selectbox(
    "Source",
    platform_options,
    index=(
        platform_options.index(requested_platform)
        if requested_platform in platform_options
        else 0
    ),
    format_func=lambda value: value or "All",
)
set_query("chat_platform", selected_platform)
search = filters[2].text_input(
        "Search",
        placeholder="Session text",
        help="Search text is not added to the URL.",
    ).strip().lower()
new_chat = filters[3].button(
    "New chat",
    type="primary",
    icon=":material/add_comment:",
    width="stretch",
    key="new_portal_chat",
)
if new_chat:
    selected_key = new_session_key()
    set_query("chat_session", selected_key)
    buffers[f"{workspace_key}:{selected_key}"] = []
    st.rerun()

conversations = [
    item
    for item in all_conversations
    if (not selected_platform or item.platform == selected_platform)
    and (
        not search
        or search
        in f"{item.title} {item.preview} {item.session_id} {item.user}".lower()
    )
]
conversation_keys = [f"{item.profile}:{item.session_id}" for item in conversations]
requested_session = query_value("chat_session")
requested_profile, requested_id = split_session_key(requested_session)
requested_is_new_portal = requested_profile == "default" and bool(
    is_portal_session_id(requested_id)
)
if requested_is_new_portal and requested_session not in conversation_keys:
    conversation_keys.insert(0, requested_session)
if not conversation_keys:
    requested_session = new_session_key()
    conversation_keys = [requested_session]
    buffers.setdefault(f"{workspace_key}:{requested_session}", [])
elif requested_session not in conversation_keys:
    requested_session = conversation_keys[0]

selected_key = requested_session
conversation_rows = []
for key in conversation_keys:
    item = conversation_by_key.get(key)
    if item is None:
        conversation_rows.append(
            {
                "Last active": "Now",
                "Source": "admin_portal",
                "User": st.session_state.authenticated_user,
                "Session": "New portal session",
                "Messages": 0,
                "Tools": 0,
            }
        )
        continue
    subject = session_subject(item)
    conversation_rows.append(
        {
            "Last active": item.last_active.strftime("%Y-%m-%d %H:%M"),
            "Source": item.platform,
            "User": item.user or "Unattributed",
            "Session": subject,
            "Messages": item.message_count,
            "Tools": item.tool_call_count,
        }
    )

clicked_key, _ = paginated_selectable_table(
    conversation_rows,
    conversation_keys,
    selected_key,
    key_prefix=f"conversation_table:{workspace_key}",
    page_query="chat_page",
    selection_query="chat_session",
    state_token=f"{selected_window}\0{selected_platform}\0{search}",
    column_config={
        "Session": st.column_config.TextColumn(width="large"),
        "Messages": st.column_config.NumberColumn(format="%d"),
        "Tools": st.column_config.NumberColumn(format="%d"),
    },
)
if clicked_key != selected_key:
    selected_key = clicked_key
    set_query("chat_session", selected_key)
    st.rerun()
set_query("chat_session", selected_key)
if history.truncated:
    st.warning("The 200-session limit was reached.")

st.divider()
thread = st.container()

profile, session_id = split_session_key(selected_key)
conversation = conversation_by_key.get(selected_key)
state_key = f"{workspace_key}:{selected_key}"
portal_owned = profile == "default" and is_portal_session_id(session_id)

persisted_messages: list[dict[str, str]] = []
transcript_truncated = False
if conversation is not None:
    try:
        transcript = runtime_provider.get_messages(
            selected_agent,
            profile=profile,
            session_id=session_id,
        )
        transcript_truncated = transcript.truncated
        persisted_messages = [
            {"role": item.role, "content": item.content}
            for item in transcript.messages
            if item.content.strip()
        ]
    except PortalApiError as exc:
        st.error(str(exc))
        if exc.guidance:
            st.caption(exc.guidance)
        st.stop()

if state_key not in buffers:
    buffers[state_key] = list(persisted_messages)
messages = buffers[state_key]
task_snapshots = st.session_state.setdefault("portal_task_snapshots", {})
task_polling = st.session_state.setdefault("portal_task_polling", {})
task_poll_grace = st.session_state.setdefault("portal_task_poll_grace", {})
task_poll_errors = st.session_state.setdefault("portal_task_poll_errors", {})
task_read_errors = st.session_state.setdefault("portal_task_read_errors", {})

with thread:
    heading, actions = st.columns([4, 1])
    with heading:
        if conversation is None:
            st.markdown("### New session")
            st.caption(f"Portal · default Planning Agent · {session_id}")
        else:
            subject = session_subject(conversation)
            st.markdown(f"### {subject}")
            owner = conversation.user or "Unattributed"
            st.caption(
                f"{conversation.platform} · {conversation.profile} · {owner} · "
                f"last active {conversation.last_active:%Y-%m-%d %H:%M UTC}"
            )
    with actions:
        if st.button(
            "Refresh",
            icon=":material/refresh:",
            width="stretch",
            key=f"refresh_{session_id}",
        ):
            buffers.pop(state_key, None)
            task_snapshots.pop(state_key, None)
            task_polling.pop(state_key, None)
            task_poll_grace.pop(state_key, None)
            task_poll_errors.pop(state_key, None)
            task_read_errors.pop(state_key, None)
            st.rerun()

    if conversation is not None and not portal_owned:
        st.info(
            "This session came from an external surface and is read-only here. "
            "Start a portal follow-up to continue without posting as another user."
        )
        if st.button(
            "Start portal follow-up",
            icon=":material/fork_right:",
            key=f"followup_{session_id}",
        ):
            followup_key = new_session_key()
            followup_state = f"{workspace_key}:{followup_key}"
            followup_context[followup_state] = list(messages[-40:])
            buffers[followup_state] = []
            set_query("chat_session", followup_key)
            st.rerun()

    context = followup_context.get(state_key, [])
    if context and not messages:
        st.caption("Follow-up context: selected external session")

    if not messages:
        st.info("Ask the agent to investigate, build, or operate this environment.")
    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"] or "_Empty message_")

    if state_key not in task_snapshots:
        try:
            initial_tasks = runtime_provider.get_task_updates(
                selected_agent,
                session_id=session_id,
            )
        except (PortalApiError, ValueError) as exc:
            task_read_errors[state_key] = str(exc)
            task_polling[state_key] = False
        else:
            task_snapshots[state_key] = initial_tasks
            task_read_errors.pop(state_key, None)
            task_polling[state_key] = has_active_tasks(initial_tasks)
            task_poll_grace[state_key] = (
                TASK_EMPTY_POLL_LIMIT if task_polling[state_key] else 0
            )

    task_snapshot = task_snapshots.get(state_key)
    if task_snapshot is not None:
        render_task_cards(
            task_snapshot,
            target=target,
            selected_agent=selected_agent,
        )
    elif state_key in task_read_errors:
        st.caption(f"Agent work unavailable: {task_read_errors[state_key]}")

    poll_enabled = bool(task_polling.get(state_key))

    @st.fragment(run_every=TASK_POLL_INTERVAL if poll_enabled else None)
    def poll_agent_work() -> None:
        if not poll_enabled:
            return

        active_count = sum(
            task.status in ACTIVE_TASK_STATUSES
            for task in task_snapshots.get(
                state_key, TaskUpdateResult((), False)
            ).tasks
        )
        indicator = st.status(
            f"Watching agent work · {active_count} active",
            state="running",
            expanded=False,
        )
        try:
            refreshed = runtime_provider.get_task_updates(
                selected_agent,
                session_id=session_id,
            )
        except (PortalApiError, ValueError) as exc:
            failures = int(task_poll_errors.get(state_key, 0)) + 1
            task_poll_errors[state_key] = failures
            task_read_errors[state_key] = str(exc)
            if failures >= TASK_ERROR_POLL_LIMIT:
                task_polling[state_key] = False
                indicator.update(
                    label="Agent work refresh stopped after repeated errors",
                    state="error",
                )
                st.rerun(scope="app")
            indicator.update(label="Agent work refresh failed; retrying…")
            return

        task_poll_errors[state_key] = 0
        task_read_errors.pop(state_key, None)
        previous = task_snapshots.get(state_key, TaskUpdateResult((), False))
        changed = task_fingerprint(previous) != task_fingerprint(refreshed)
        task_snapshots[state_key] = refreshed

        if has_active_tasks(refreshed):
            task_poll_grace[state_key] = TASK_EMPTY_POLL_LIMIT
            if changed:
                st.rerun(scope="app")
            return

        grace = int(task_poll_grace.get(state_key, 0))
        if grace > 1:
            task_poll_grace[state_key] = grace - 1
            indicator.update(label="Waiting for delegated work…")
            return

        task_polling[state_key] = False
        task_poll_grace[state_key] = 0
        indicator.update(label="Agent work is up to date", state="complete")
        st.rerun(scope="app")

    poll_agent_work()

    if transcript_truncated:
        st.warning("Only the first 500 user and assistant messages are shown.")

    active_interactions = st.session_state.setdefault(
        "portal_active_interactions", {}
    )
    interaction_views = st.session_state.setdefault("portal_interaction_views", {})
    active_id = active_interactions.get(state_key)
    pending = interaction_views.get(state_key)
    if active_id and (pending is None or pending.interaction_id != active_id):
        try:
            pending = runtime_provider.get_interaction(active_id)
            interaction_views[state_key] = pending
        except PortalApiError as exc:
            st.error(str(exc))
            if exc.guidance:
                st.caption(exc.guidance)

    if pending is not None and pending.status == "waiting_for_approval":
        approval = pending.approval or {}
        with st.container(border=True):
            st.write("Approval required")
            st.caption(
                str(
                    approval.get("description")
                    or approval.get("tool")
                    or "The agent requested permission to continue."
                )
            )
            command = str(approval.get("command") or "").strip()
            if command:
                st.code(command)
            approve, deny = st.columns(2)
            if approve.button(
                "Approve once",
                type="primary",
                width="stretch",
                key=f"approve_{pending.interaction_id}",
            ):
                try:
                    with st.spinner("Continuing…"):
                        result = runtime_provider.resolve_approval(
                            pending.interaction_id,
                            choice="once",
                        )
                    interaction_views[state_key] = result
                    if result.terminal:
                        finish_interaction(result, messages, state_key)
                    st.rerun()
                except PortalApiError as exc:
                    st.error(str(exc))
                    if exc.guidance:
                        st.caption(exc.guidance)
            if deny.button(
                "Deny",
                width="stretch",
                key=f"deny_{pending.interaction_id}",
            ):
                try:
                    with st.spinner("Stopping the action…"):
                        result = runtime_provider.resolve_approval(
                            pending.interaction_id,
                            choice="deny",
                        )
                    interaction_views[state_key] = result
                    if result.terminal:
                        finish_interaction(result, messages, state_key)
                    st.rerun()
                except PortalApiError as exc:
                    st.error(str(exc))
                    if exc.guidance:
                        st.caption(exc.guidance)

    @st.fragment(run_every="2s" if active_id else None)
    def poll_interaction() -> None:
        if not active_id:
            return
        try:
            refreshed = runtime_provider.get_interaction(active_id)
        except PortalApiError as exc:
            st.error(f"Interaction refresh failed: {exc}")
            return
        prior = interaction_views.get(state_key)
        interaction_views[state_key] = refreshed
        if refreshed.terminal:
            finish_interaction(refreshed, messages, state_key)
            try:
                task_snapshots[state_key] = runtime_provider.get_task_updates(
                    selected_agent,
                    session_id=session_id,
                )
            except (PortalApiError, ValueError):
                pass
            st.rerun(scope="app")
        elif prior is None or prior.status != refreshed.status:
            st.rerun(scope="app")

    poll_interaction()

    if pending is not None and not pending.terminal:
        run_status, stop_action = st.columns([4, 1], vertical_alignment="center")
        run_status.status(
            f"Agent interaction · {pending.status.replace('_', ' ')}",
            state="running",
            expanded=False,
        )
        if stop_action.button(
            "Stop",
            icon=":material/stop_circle:",
            width="stretch",
            key=f"stop_{pending.interaction_id}",
        ):
            try:
                result = runtime_provider.cancel_interaction(
                    pending.interaction_id
                )
                interaction_views[state_key] = result
                task_polling[state_key] = False
                task_poll_grace[state_key] = 0
                finish_interaction(result, messages, state_key)
                st.rerun()
            except PortalApiError as exc:
                st.error(str(exc))
                if exc.guidance:
                    st.caption(exc.guidance)

    last_run = st.session_state.setdefault(
        "portal_last_interactions", {}
    ).get(state_key)
    if last_run is not None:
        with st.expander("Run details"):
            st.write(f"Status: {last_run.status}")
            st.code(last_run.interaction_id)
            if last_run.root_run_id:
                st.caption(f"Root run: {last_run.root_run_id}")
            for diagnostic in last_run.diagnostics:
                st.caption(diagnostic)
            st.page_link("pages/activity.py", label="Open Activity Explorer")

    prompt = st.chat_input(
        "Message the agent",
        disabled=not portal_owned or bool(active_id),
    )
    if prompt:
        prior_history = [*context, *messages]
        messages.append({"role": "user", "content": prompt})
        try:
            with st.spinner("Starting agent interaction…"):
                result = runtime_provider.start_interaction(
                    selected_agent,
                    prompt=prompt,
                    session_id=session_id,
                    history=prior_history,
                    profile=profile,
                )
            interaction_views[state_key] = result
            if result.terminal:
                finish_interaction(result, messages, state_key)
            else:
                active_interactions[state_key] = result.interaction_id
                task_polling[state_key] = True
                task_poll_grace[state_key] = TASK_EMPTY_POLL_LIMIT
            followup_context.pop(state_key, None)
        except (PortalApiError, ValueError) as exc:
            messages.append({"role": "assistant", "content": f"Run failed: {exc}"})
        st.rerun()

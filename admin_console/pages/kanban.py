"""Live, read-only agent Task Kanban inspector."""

from __future__ import annotations

import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import streamlit as st

from admin_console.connection_gate import require_connection
from admin_console.agent_runtime import AgentRuntimeError, AgentRuntimeProvider
from admin_console.ui import AGENT_SELECTOR_HELP, paginated_selectable_table

ACTIVE_STATUSES = {"todo", "ready", "running"}
ATTENTION_STATUSES = {"blocked", "failed", "crashed", "cancelled"}


def query_value(name: str) -> str:
    return str(st.query_params.get(name, "")).strip()


def set_query(name: str, value: str) -> None:
    if value:
        if query_value(name) != value:
            st.query_params[name] = value
    else:
        st.query_params.pop(name, None)


def timestamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "—"


def related_rows(tasks) -> list[dict[str, str]]:
    return [
        {
            "Task": task.task_id,
            "Status": task.status,
            "Assignee": task.assignee,
            "Title": task.title,
        }
        for task in tasks
    ]


st.title("Task Kanban")
target = require_connection()

provider = AgentRuntimeProvider(target)
try:
    agents = provider.list_agents()
except AgentRuntimeError as exc:
    st.error(str(exc))
    if exc.guidance:
        st.caption(exc.guidance)
    st.stop()

if not agents:
    st.warning("No running kube-agents gateway was found in this scope.")
    st.stop()

toolbar = st.columns([3, 1])
requested_agent = query_value("kanban_agent")
selected_agent = toolbar[0].selectbox(
    "Agent",
    agents,
    index=agents.index(requested_agent) if requested_agent in agents else 0,
    help=AGENT_SELECTOR_HELP,
)
set_query("kanban_agent", selected_agent)
if toolbar[1].button(
    "Refresh",
    icon=":material/refresh:",
    width="stretch",
):
    st.rerun()

try:
    with st.spinner("Reading Task Kanban…"):
        board = provider.list_kanban_tasks(selected_agent)
except AgentRuntimeError as exc:
    st.error(str(exc))
    if exc.guidance:
        st.caption(exc.guidance)
    st.stop()

all_tasks = board.tasks
metrics = st.columns(4)
metrics[0].metric("Tasks", len(all_tasks))
metrics[1].metric(
    "Active",
    sum(task.status in ACTIVE_STATUSES for task in all_tasks),
)
metrics[2].metric(
    "Attention",
    sum(task.status in ATTENTION_STATUSES for task in all_tasks),
)
metrics[3].metric("Done", sum(task.status == "done" for task in all_tasks))

if board.truncated:
    st.warning("The 500-task read limit was reached.")

if not all_tasks:
    st.info("No tasks were found.")
    st.stop()

statuses = sorted({task.status for task in all_tasks})
assignees = sorted({task.assignee for task in all_tasks})
filters = st.columns([1, 1.4, 2])
requested_status = query_value("kanban_status")
status_options = ["", *statuses]
selected_status = filters[0].selectbox(
    "Status",
    status_options,
    index=(
        status_options.index(requested_status)
        if requested_status in status_options
        else 0
    ),
    format_func=lambda value: value or "All",
)
set_query("kanban_status", selected_status)
requested_assignee = query_value("kanban_assignee")
assignee_options = ["", *assignees]
selected_assignee = filters[1].selectbox(
    "Assignee",
    assignee_options,
    index=(
        assignee_options.index(requested_assignee)
        if requested_assignee in assignee_options
        else 0
    ),
    format_func=lambda value: value or "All",
)
set_query("kanban_assignee", selected_assignee)
search = filters[2].text_input(
    "Search",
    placeholder="Task, title, session, result",
).strip().lower()

tasks = [
    task
    for task in all_tasks
    if (not selected_status or task.status == selected_status)
    and (not selected_assignee or task.assignee == selected_assignee)
    and (
        not search
        or search
        in " ".join(
            (
                task.task_id,
                task.title,
                task.assignee,
                task.session_id,
                task.summary,
                task.error,
            )
        ).lower()
    )
]

if not tasks:
    st.caption(f"0 of {len(all_tasks)} tasks")
    st.info("No tasks match these filters.")
    st.stop()

task_ids = [task.task_id for task in tasks]
requested_task = query_value("kanban_task")
selected_task = requested_task if requested_task in task_ids else task_ids[0]
clicked_task, _ = paginated_selectable_table(
    [
        {
            "Status": task.status,
            "Task": task.task_id,
            "Title": task.title,
            "Assignee": task.assignee,
            "Updated": timestamp(task.updated_at),
            "Runs": task.run_count,
            "Parents": task.parent_count,
            "Children": task.child_count,
            "Chat": "yes" if task.session_id else "no",
        }
        for task in tasks
    ],
    task_ids,
    selected_task,
    key_prefix=f"kanban_table:{selected_agent}",
    page_query="kanban_page",
    selection_query="kanban_task",
    state_token=f"{selected_status}\0{selected_assignee}\0{search}",
    column_config={
        "Title": st.column_config.TextColumn(width="large"),
        "Runs": st.column_config.NumberColumn(format="%d"),
        "Parents": st.column_config.NumberColumn(format="%d"),
        "Children": st.column_config.NumberColumn(format="%d"),
    },
)
if clicked_task != selected_task:
    selected_task = clicked_task
    set_query("kanban_task", selected_task)
    st.rerun()
set_query("kanban_task", selected_task)
if len(tasks) != len(all_tasks):
    st.caption(f"Filtered to {len(tasks)} of {len(all_tasks)} tasks")

try:
    detail = provider.get_kanban_task(selected_agent, selected_task)
except (AgentRuntimeError, ValueError) as exc:
    st.error(str(exc))
    st.stop()

task = detail.task
st.divider()
heading, state = st.columns([4, 1])
heading.subheader(task.title or task.task_id)
heading.caption(task.task_id)
if task.status == "done":
    state.success(task.status)
elif task.status in ATTENTION_STATUSES:
    state.error(task.status)
else:
    state.info(task.status)

overview, runs_tab, timeline = st.tabs(["Overview", "Runs", "Timeline"])

with overview:
    facts = st.columns(4)
    facts[0].metric("Assignee", task.assignee)
    facts[1].metric("Priority", task.priority)
    facts[2].metric("Runs", task.run_count)
    facts[3].metric("Failures", detail.consecutive_failures)

    st.markdown("#### Request")
    st.markdown(detail.body or "_No task body._")

    if task.summary:
        st.markdown("#### Outcome")
        st.success(task.summary)
    if detail.result:
        with st.expander("Task result"):
            st.markdown(detail.result)
    if task.error:
        st.error(task.error)

    timing = st.columns(2)
    timing[0].caption(
        f"Created {timestamp(task.created_at)} · Started {timestamp(detail.started_at)}"
    )
    timing[1].caption(
        f"Updated {timestamp(task.updated_at)} · Completed {timestamp(detail.completed_at)}"
    )
    attributes = []
    if detail.created_by:
        attributes.append(f"created by {detail.created_by}")
    if detail.workspace_kind:
        attributes.append(f"workspace {detail.workspace_kind}")
    if detail.project_id:
        attributes.append(f"project {detail.project_id}")
    if detail.current_step:
        attributes.append(f"step {detail.current_step}")
    if detail.block_kind:
        attributes.append(f"blocked: {detail.block_kind}")
    if detail.goal_mode:
        attributes.append("goal mode")
    if attributes:
        st.caption(" · ".join(attributes))

    if task.session_id:
        st.markdown("#### Chat")
        chat_query = urllib.parse.urlencode(
            {
                "project": target.project_id,
                "cluster": target.cluster_name,
                "location": target.location,
                "chat_agent": selected_agent,
                "chat_window": "all",
                "chat_session": f"default:{task.session_id}",
            }
        )
        st.link_button(
            "Open linked session",
            f"/chat?{chat_query}",
            icon=":material/forum:",
        )
        st.code(task.session_id)

    relationships = st.columns(2)
    with relationships[0]:
        st.markdown("#### Parents")
        if detail.parents:
            st.dataframe(related_rows(detail.parents), hide_index=True, width="stretch")
        else:
            st.caption("None")
    with relationships[1]:
        st.markdown("#### Children")
        if detail.children:
            st.dataframe(related_rows(detail.children), hide_index=True, width="stretch")
        else:
            st.caption("None")

    st.markdown("#### Delivery")
    if detail.deliveries:
        st.dataframe(
            [
                {
                    "Platform": item.platform,
                    "Chat": item.has_chat_id,
                    "Thread": item.has_thread_id,
                    "User": item.has_user_id,
                    "Notifier": item.notifier_profile or "default",
                    "Last event": item.last_event_id,
                    "Created": timestamp(item.created_at),
                }
                for item in detail.deliveries
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("No chat delivery subscription.")

    if detail.attachments:
        st.markdown("#### Attachments")
        st.dataframe(
            [
                {
                    "File": item.filename,
                    "Type": item.content_type,
                    "Bytes": item.size,
                    "Uploaded by": item.uploaded_by,
                    "Created": timestamp(item.created_at),
                }
                for item in detail.attachments
            ],
            hide_index=True,
            width="stretch",
        )

with runs_tab:
    if not detail.runs:
        st.info("This task has not run yet.")
    elif detail.runs_truncated:
        st.warning(
            f"Showing the newest {len(detail.runs)} of {task.run_count} retained runs."
        )
    for run in reversed(detail.runs):
        label = f"Run {run.run_id} · {run.profile or 'unknown'} · {run.status}"
        with st.container(border=True):
            st.markdown(f"**{label}**")
            st.caption(
                f"Started {timestamp(run.started_at)} · Ended {timestamp(run.ended_at)}"
            )
            if run.step_key:
                st.caption(f"Step: {run.step_key}")
            if run.summary:
                st.success(run.summary)
            if run.error:
                st.error(run.error)
            if run.metadata:
                with st.expander("Metadata"):
                    st.code(run.metadata, language="json")

with timeline:
    entries = [
        (item.created_at, "event", item.kind, item.payload, item.run_id)
        for item in detail.events
    ] + [
        (item.created_at, "comment", item.author, item.body, None)
        for item in detail.comments
    ]
    entries.sort(key=lambda item: item[0])
    if not entries:
        st.info("No task events or comments.")
    for occurred_at, entry_type, label, content, run_id in reversed(entries):
        suffix = f" · run {run_id}" if run_id is not None else ""
        with st.container(border=True):
            st.markdown(f"**{entry_type} · {label}**")
            st.caption(f"{timestamp(occurred_at)}{suffix}")
            if content:
                st.code(content, language="json" if entry_type == "event" else "text")

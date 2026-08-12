"""Live, read-only agent scheduled-job inspector."""

from __future__ import annotations

import calendar
import html
import re
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import streamlit as st

from admin_console.connection_gate import require_connection
from admin_console.agent_runtime import (
    CronSnapshot,
    AgentCronExecution,
    AgentCronJob,
    AgentRuntimeError,
    AgentRuntimeProvider,
)
from admin_console.ui import AGENT_SELECTOR_HELP

ACTIVE_STATUSES = {"claimed", "dispatching", "pending", "running", "started"}
SUCCESS_STATUSES = {"completed", "ok", "success", "succeeded"}
HISTORY_WINDOWS = {"24h": 1, "7d": 7, "30d": 30}
INTERVAL_CADENCE = re.compile(r"^every\s+(\d+)m$", re.IGNORECASE)


def query_value(name: str, default: str = "") -> str:
    return str(st.query_params.get(name, default)).strip()


def set_query(name: str, value: str) -> None:
    if value:
        if query_value(name) != value:
            st.query_params[name] = value
    else:
        st.query_params.pop(name, None)


def timestamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "—"


def duration(execution: AgentCronExecution) -> str:
    if execution.started_at is None:
        return "—"
    end = execution.finished_at or datetime.now(UTC)
    seconds = max(0, int((end - execution.started_at).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def execution_state(status: str) -> str:
    normalized = status.lower()
    if normalized in ACTIVE_STATUSES:
        return "Active"
    if normalized in SUCCESS_STATUSES:
        return "Succeeded"
    return "Failed" if normalized in {"error", "failed", "crashed"} else status.title()


def scheduler_label(job: AgentCronJob) -> str:
    if job.scheduler == "active":
        return "Active"
    if job.scheduler == "stale":
        return "Stale"
    return "No ticker"


def calendar_months(start: date, end: date):
    cursor = start.replace(day=1)
    while cursor <= end:
        yield cursor.year, cursor.month
        cursor = (
            cursor.replace(year=cursor.year + 1, month=1)
            if cursor.month == 12
            else cursor.replace(month=cursor.month + 1)
        )


def cron_field_values(raw: str, minimum: int, maximum: int) -> set[int] | None:
    values: set[int] = set()
    try:
        for item in raw.split(","):
            base, separator, raw_step = item.partition("/")
            step = int(raw_step) if separator else 1
            if step <= 0:
                return None
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                raw_start, raw_end = base.split("-", 1)
                start, end = int(raw_start), int(raw_end)
            else:
                start = end = int(base)
            if start < minimum or end > maximum or start > end:
                return None
            values.update(range(start, end + 1, step))
    except ValueError:
        return None
    return values


def cron_matches(moment: datetime, expression: str) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        return False
    minute = cron_field_values(fields[0], 0, 59)
    hour = cron_field_values(fields[1], 0, 23)
    day_of_month = cron_field_values(fields[2], 1, 31)
    month = cron_field_values(fields[3], 1, 12)
    day_of_week = cron_field_values(fields[4], 0, 7)
    if any(value is None for value in (minute, hour, day_of_month, month, day_of_week)):
        return False
    day_of_week = {0 if value == 7 else value for value in day_of_week}
    cron_weekday = (moment.weekday() + 1) % 7
    day_matches = moment.day in day_of_month
    weekday_matches = cron_weekday in day_of_week
    day_restricted = len(day_of_month) < 31
    weekday_restricted = len(day_of_week) < 7
    date_matches = (
        day_matches or weekday_matches
        if day_restricted and weekday_restricted
        else day_matches and weekday_matches
    )
    return (
        moment.minute in minute
        and moment.hour in hour
        and moment.month in month
        and date_matches
    )


def group_occurrences(occurrences) -> dict[date, tuple[datetime, int]]:
    grouped: dict[date, tuple[datetime, int]] = {}
    for occurrence in occurrences:
        first, count = grouped.get(occurrence.date(), (occurrence, 0))
        grouped[occurrence.date()] = (min(first, occurrence), count + 1)
    return grouped


def future_occurrences(
    job: AgentCronJob,
    now: datetime,
    end: date,
) -> dict[date, tuple[datetime, int]]:
    end_at = datetime.combine(end, datetime.max.time(), tzinfo=UTC)
    expression = job.schedule_expression
    if not expression and len(job.cadence.split()) == 5:
        expression = job.cadence
    if job.schedule_kind == "cron" or expression:
        cursor = now.replace(second=0, microsecond=0) + timedelta(minutes=1)

        def cron_occurrences():
            nonlocal cursor
            while cursor <= end_at:
                if cron_matches(cursor, expression):
                    yield cursor
                cursor += timedelta(minutes=1)

        return group_occurrences(cron_occurrences())

    minutes = job.interval_minutes
    cadence_match = INTERVAL_CADENCE.fullmatch(job.cadence)
    if not minutes and cadence_match:
        minutes = int(cadence_match.group(1))
    if minutes > 0 and job.next_run_at is not None:
        cursor = job.next_run_at
        if cursor < now:
            elapsed = (now - cursor).total_seconds()
            cursor += timedelta(minutes=minutes) * int(
                elapsed // (minutes * 60) + 1
            )

        def interval_occurrences():
            nonlocal cursor
            while cursor <= end_at:
                yield cursor
                cursor += timedelta(minutes=minutes)

        return group_occurrences(interval_occurrences())

    if job.next_run_at is not None and job.next_run_at <= end_at:
        return {job.next_run_at.date(): (job.next_run_at, 1)}
    return {}


def render_calendar(
    snapshot: CronSnapshot,
    jobs_by_key: dict[tuple[str, str], AgentCronJob],
) -> None:
    now = snapshot.read_at
    start = now.date() - timedelta(days=7)
    end = now.date() + timedelta(days=21)
    events: dict[date, list[tuple[datetime, str, str]]] = defaultdict(list)

    for execution in snapshot.executions:
        occurred_at = execution.started_at or execution.claimed_at
        if occurred_at is None or not start <= occurred_at.date() <= now.date():
            continue
        job = jobs_by_key.get((execution.profile, execution.job_id))
        name = job.name if job else execution.job_id or "Unknown job"
        status = execution.status.lower()
        kind = (
            "active"
            if status in ACTIVE_STATUSES
            else "success" if status in SUCCESS_STATUSES else "failed"
        )
        source = "manual" if execution.source == "direct" else execution.source
        events[occurred_at.date()].append(
            (occurred_at, f"{name} · {source}", kind)
        )

    for job in snapshot.jobs:
        if not job.enabled:
            continue
        for run_day, (first_run, run_count) in future_occurrences(
            job, now, end
        ).items():
            if not start <= run_day <= end:
                continue
            kind = "upcoming" if job.scheduler == "active" else "unavailable"
            count = f" · {run_count} runs" if run_count > 1 else ""
            scheduler = " · scheduler unavailable" if job.scheduler != "active" else ""
            events[run_day].append(
                (first_run, f"{job.name}{count}{scheduler}", kind)
            )

    st.markdown(
        """
        <style>
        .ka-calendar { border-collapse: separate; border-spacing: 6px; width: 100%; }
        .ka-calendar th { color: #8fa1bd; font-size: .75rem; padding: 4px; }
        .ka-calendar td {
          background: rgba(21,31,50,.75); border: 1px solid #26344c;
          border-radius: 10px; height: 105px; padding: 7px; vertical-align: top;
          width: 14.285%;
        }
        .ka-calendar td.ka-outside { opacity: .3; }
        .ka-calendar-day { color: #8fa1bd; font-size: .72rem; margin-bottom: 5px; }
        .ka-calendar-event {
          border-left: 3px solid #7c9cff; font-size: .68rem; line-height: 1.25;
          margin: 4px 0; padding-left: 5px;
        }
        .ka-calendar-event.success { border-color: #2ed3b7; }
        .ka-calendar-event.failed, .ka-calendar-event.overdue { border-color: #ff6b7a; }
        .ka-calendar-event.upcoming { border-color: #b58cff; }
        .ka-calendar-event.unavailable { border-color: #ffb454; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    for year, month in calendar_months(start, end):
        st.caption(f"{calendar.month_name[month]} {year}")
        rows = ["<table class='ka-calendar'><thead><tr>"]
        rows.extend(f"<th>{day}</th>" for day in calendar.day_abbr)
        rows.append("</tr></thead><tbody>")
        for week in calendar.Calendar().monthdatescalendar(year, month):
            rows.append("<tr>")
            for day in week:
                outside = " ka-outside" if day.month != month else ""
                rows.append(f"<td class='{outside.strip()}'>")
                rows.append(f"<div class='ka-calendar-day'>{day.day}</div>")
                day_events = sorted(events.get(day, ()), key=lambda item: item[0])
                for occurred_at, label, kind in day_events[:4]:
                    rows.append(
                        f"<div class='ka-calendar-event {kind}'>"
                        f"{occurred_at:%H:%M} · {html.escape(label)}</div>"
                    )
                if len(day_events) > 4:
                    rows.append(
                        "<div class='ka-calendar-day'>"
                        f"+{len(day_events) - 4} more</div>"
                    )
                rows.append("</td>")
            rows.append("</tr>")
        rows.append("</tbody></table>")
        st.markdown("".join(rows), unsafe_allow_html=True)


st.title("Scheduled Cron")
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

toolbar = st.columns([2, 1, 1])
requested_agent = query_value("cron_agent")
selected_agent = toolbar[0].selectbox(
    "Agent",
    agents,
    index=agents.index(requested_agent) if requested_agent in agents else 0,
    help=AGENT_SELECTOR_HELP,
)
set_query("cron_agent", selected_agent)
requested_window = query_value("cron_window", "7d")
window_options = list(HISTORY_WINDOWS)
selected_window = toolbar[1].selectbox(
    "History",
    window_options,
    index=(
        window_options.index(requested_window)
        if requested_window in window_options
        else 1
    ),
)
set_query("cron_window", selected_window)
if toolbar[2].button(
    "Refresh",
    icon=":material/refresh:",
    width="stretch",
):
    st.rerun()

try:
    with st.spinner("Reading scheduled jobs…"):
        snapshot = provider.get_cron_snapshot(
            selected_agent,
            execution_limit=200,
        )
except AgentRuntimeError as exc:
    st.error(str(exc))
    if exc.guidance:
        st.caption(exc.guidance)
    st.stop()

jobs_by_key = {(job.profile, job.job_id): job for job in snapshot.jobs}
cutoff = snapshot.read_at - timedelta(days=HISTORY_WINDOWS[selected_window])
executions = tuple(
    execution
    for execution in snapshot.executions
    if execution.status.lower() in ACTIVE_STATUSES
    or (execution.started_at or execution.claimed_at or datetime.fromtimestamp(0, UTC))
    >= cutoff
)
active = [item for item in executions if item.status.lower() in ACTIVE_STATUSES]
enabled = [job for job in snapshot.jobs if job.enabled]
failed = [
    item
    for item in executions
    if item.status.lower() in {"error", "failed", "crashed"}
]
unscheduled = [job for job in enabled if job.scheduler != "active"]

st.caption(f"LIVE READ · {snapshot.read_at:%Y-%m-%d %H:%M:%S UTC}")
metrics = st.columns(4)
metrics[0].metric("Active now", len(active))
metrics[1].metric("Recent runs", len(executions))
metrics[2].metric("Enabled jobs", len(enabled))
metrics[3].metric("Needs attention", len(failed) + len(unscheduled))

if unscheduled:
    st.warning(
        f"{len(unscheduled)} enabled job(s) belong to a profile without a live "
        "scheduler. Their definitions exist, but they will not run automatically."
    )
if snapshot.jobs_truncated or snapshot.executions_truncated:
    st.warning("The bounded read limit was reached; this view is incomplete.")

st.subheader("Active and recent executions")
execution_rows = []
for execution in executions:
    job = jobs_by_key.get((execution.profile, execution.job_id))
    execution_rows.append(
        {
            "State": execution_state(execution.status),
            "Job": job.name if job else execution.job_id or "Unknown job",
            "Profile": execution.profile,
            "Trigger": "Manual" if execution.source == "direct" else execution.source,
            "Started": timestamp(execution.started_at or execution.claimed_at),
            "Duration": duration(execution),
            "Error": execution.error,
        }
    )
if execution_rows:
    st.dataframe(execution_rows, hide_index=True, width="stretch")
else:
    st.info(f"No active or recent executions in the last {selected_window}.")

st.subheader("Scheduled jobs")
job_rows = [
    {
        "Enabled": "Yes" if job.enabled else "No",
        "Job": job.name,
        "Profile": job.profile,
        "Cadence": job.cadence,
        "Task": job.task or job.script or "—",
        "Mode": job.mode,
        "Scheduler": scheduler_label(job),
        "Last run": timestamp(job.last_run_at),
        "Next run": timestamp(job.next_run_at),
        "Result": job.last_status,
    }
    for job in snapshot.jobs
]
if job_rows:
    st.dataframe(job_rows, hide_index=True, width="stretch")
else:
    st.info("No scheduled jobs were found.")

st.subheader("Calendar")
st.caption("Previous 7 days and next 21 days · UTC · recurring schedules projected")
render_calendar(snapshot, jobs_by_key)

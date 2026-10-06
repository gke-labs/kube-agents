"""Shared visual language and rendering helpers."""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Sequence

import streamlit as st

from admin_console.domain import AttributionLevel, TriggerKind
from admin_console.theme import Palette, active_palette

PALETTE = active_palette()

TRIGGER_COLORS = {kind: PALETTE.trigger_colors[kind.value] for kind in TriggerKind}

STATUS_COLORS = dict(PALETTE.status_colors)

ATTRIBUTION_COLORS = {
    level: PALETTE.attribution_colors[level.value] for level in AttributionLevel
}

def render_command_evidence(item: dict, *, expanded: bool = False) -> None:
    """Render one raw external-command result consistently across pages."""
    source = str(item.get("source") or "Evidence")
    returncode = int(item.get("returncode", 0))
    with st.expander(f"{source} · exit {returncode}", expanded=expanded):
        command = str(item.get("command") or "")
        if command:
            st.caption(command)
        stdout = str(item.get("stdout") or "")
        stderr = str(item.get("stderr") or "")
        if stdout:
            st.code(stdout, language="text", wrap_lines=True)
        if stderr:
            st.code(stderr, language="text", wrap_lines=True)
        if item.get("timed_out"):
            st.caption("timed_out=true")
        if not stdout and not stderr:
            st.code("", language="text")


def selectable_table(
    rows: Sequence[dict[str, Any]],
    row_ids: Sequence[str],
    selected_id: str,
    *,
    key_prefix: str,
    state_token: str = "",
    height: int | None = None,
    column_config: dict[str, Any] | None = None,
) -> tuple[str, object]:
    """Render one URL-driven single-row table and return its selected row ID."""
    if not rows or len(rows) != len(row_ids):
        raise ValueError("selectable table rows and IDs must be non-empty and aligned")
    selected = selected_id if selected_id in row_ids else row_ids[0]
    selected_row = row_ids.index(selected)
    generation = sha256(
        "\0".join((state_token, selected, *row_ids)).encode()
    ).hexdigest()[:12]
    options: dict[str, Any] = {
        "hide_index": True,
        "width": "stretch",
        "on_select": "rerun",
        "selection_mode": "single-row",
        "selection_default": {
            "selection": {"rows": [selected_row], "columns": [], "cells": []}
        },
        "key": f"{key_prefix}:{generation}",
    }
    if height is not None:
        options["height"] = height
    if column_config is not None:
        options["column_config"] = column_config
    table = st.dataframe(rows, **options)
    if table.selection.rows:
        clicked_row = table.selection.rows[0]
        if 0 <= clicked_row < len(row_ids):
            selected = row_ids[clicked_row]
    return selected, table


def paginated_selectable_table(
    rows: Sequence[dict[str, Any]],
    row_ids: Sequence[str],
    selected_id: str,
    *,
    key_prefix: str,
    page_query: str,
    selection_query: str,
    state_token: str = "",
    page_size: int = 25,
    height: int | None = None,
    column_config: dict[str, Any] | None = None,
) -> tuple[str, object]:
    """Render a URL-persisted page of a single-row selectable table."""
    if page_size < 1:
        raise ValueError("page size must be positive")
    if not rows or len(rows) != len(row_ids):
        raise ValueError("paginated table rows and IDs must be non-empty and aligned")

    total_pages = (len(rows) + page_size - 1) // page_size
    requested_page = str(st.query_params.get(page_query, "")).strip()
    if requested_page:
        try:
            page = int(requested_page)
        except ValueError:
            page = 1
    elif selected_id in row_ids:
        page = row_ids.index(selected_id) // page_size + 1
    else:
        page = 1
    page = min(max(page, 1), total_pages)

    if selected_id in row_ids:
        selected_page = row_ids.index(selected_id) // page_size + 1
        page_start = (page - 1) * page_size
        page_end = min(page_start + page_size, len(row_ids))
        if selected_id not in row_ids[page_start:page_end]:
            page = selected_page

    start = (page - 1) * page_size
    end = min(start + page_size, len(rows))
    page_rows = rows[start:end]
    page_ids = row_ids[start:end]
    selected = selected_id if selected_id in page_ids else page_ids[0]
    st.query_params[page_query] = str(page)

    selected, table = selectable_table(
        page_rows,
        page_ids,
        selected,
        key_prefix=key_prefix,
        state_token=f"{state_token}\0page:{page}",
        height=height or min(500, 40 + 36 * len(page_rows)),
        column_config=column_config,
    )

    status, previous, following = st.columns([6, 1, 1], vertical_alignment="center")
    status.caption(
        f"{start + 1}–{end} of {len(rows)} · Page {page} of {total_pages}"
    )
    previous_page = page - 1
    next_page = page + 1
    if previous.button(
        "Previous",
        disabled=page == 1,
        key=f"{key_prefix}:previous:{page}",
        width="stretch",
    ):
        st.query_params[page_query] = str(previous_page)
        st.query_params[selection_query] = row_ids[(previous_page - 1) * page_size]
        st.rerun()
    if following.button(
        "Next",
        disabled=page == total_pages,
        key=f"{key_prefix}:next:{page}",
        width="stretch",
    ):
        st.query_params[page_query] = str(next_page)
        st.query_params[selection_query] = row_ids[(next_page - 1) * page_size]
        st.rerun()
    return selected, table


def _css_variables(palette: Palette) -> str:
    variables = {
        "bg": palette.background,
        "panel": palette.panel,
        "panel-2": palette.panel_2,
        "border": palette.border,
        "text": palette.text,
        "muted": palette.muted,
        "accent": palette.accent,
        "mint": palette.success,
        "violet": palette.violet,
        "success": palette.success,
        "warning": palette.warning,
        "danger": palette.danger,
        "error-text": palette.error_text,
        "app-bg": palette.app_background,
        "sidebar-bg": palette.sidebar_background,
        "card-bg": palette.card_background,
        "cell-bg": palette.cell_background,
        "danger-button": palette.danger_button,
        "danger-button-text": palette.danger_button_text,
        "danger-button-border": palette.danger_button_border,
        "danger-button-hover": palette.danger_button_hover,
        "danger-button-hover-border": palette.danger_button_hover_border,
        "abort-border": palette.abort_border,
        "abort-text": palette.abort_text,
        "disabled-bg": palette.disabled_background,
        "disabled-border": palette.disabled_border,
        "disabled-text": palette.disabled_text,
    }
    lines = "".join(f"  --ka-{name}: {value};\n" for name, value in variables.items())
    return f":root {{\n{lines}}}\n"


def apply_theme() -> None:
    """Apply the active palette's CSS; dynamic values are rendered by Streamlit."""
    st.markdown(
        "<style>\n"
        + _css_variables(PALETTE)
        + """
        .stApp {
          background: var(--ka-app-bg);
          color: var(--ka-text);
        }
        [data-testid="stSidebar"] {
          background: var(--ka-sidebar-bg);
          border-right: 1px solid var(--ka-border);
        }
        [data-testid="stMetric"] {
          background: var(--ka-card-bg);
          border: 1px solid var(--ka-border);
          border-radius: 14px;
          padding: 16px 18px;
        }
        [data-testid="stMetricValue"] { letter-spacing: -0.04em; }
        [data-testid="stMetricDelta"] { color: var(--ka-muted); }
        .ka-card {
          background: var(--ka-card-bg);
          border: 1px solid var(--ka-border);
          border-radius: 14px;
          padding: 18px;
          margin-bottom: 12px;
        }
        .ka-card-title { font-weight: 680; margin-bottom: 4px; }
        .ka-card-meta { color: var(--ka-muted); font-size: .8rem; }
        div[data-testid="stDataFrame"] {
          border: 1px solid var(--ka-border);
          border-radius: 12px;
          overflow: hidden;
        }
        .stButton button, .stLinkButton a {
          border-radius: 10px !important;
        }
        .st-key-project_connection_secondary_disconnect button:not(:disabled),
        .st-key-cluster_connection_secondary_disconnect button:not(:disabled) {
          background: var(--ka-danger-button) !important;
          border-color: var(--ka-danger-button-border) !important;
          color: var(--ka-danger-button-text) !important;
        }
        .st-key-project_connection_secondary_disconnect button:not(:disabled):hover,
        .st-key-cluster_connection_secondary_disconnect button:not(:disabled):hover {
          background: var(--ka-danger-button-hover) !important;
          border-color: var(--ka-danger-button-hover-border) !important;
        }
        .st-key-project_connection_secondary_abort button:not(:disabled),
        .st-key-cluster_connection_secondary_abort button:not(:disabled) {
          border-color: var(--ka-abort-border) !important;
          color: var(--ka-abort-text) !important;
        }
        .st-key-project_connection_primary_connected button:disabled,
        .st-key-cluster_connection_primary_connected button:disabled,
        .st-key-project_connection_primary_connecting button:disabled,
        .st-key-cluster_connection_primary_connecting button:disabled {
          background: var(--ka-disabled-bg) !important;
          border-color: var(--ka-disabled-border) !important;
          color: var(--ka-disabled-text) !important;
          opacity: .72 !important;
        }
        @keyframes ka-connection-spin {
          to { transform: rotate(360deg); }
        }
        .st-key-project_connection_primary_connecting [data-testid="stIconMaterial"],
        .st-key-cluster_connection_primary_connecting [data-testid="stIconMaterial"],
        .st-key-project_connection_primary_connecting .material-symbols-rounded,
        .st-key-cluster_connection_primary_connecting .material-symbols-rounded {
          animation: ka-connection-spin .8s linear infinite;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def section_title(title: str, caption: str = "") -> None:
    st.subheader(title)
    if caption:
        st.caption(caption)


def status_label(status: str) -> str:
    icons = {
        "completed": "●",
        "running": "◌",
        "blocked": "▲",
        "failed": "×",
    }
    return f"{icons.get(status, '•')} {status.title()}"


def render_telemetry_status(snapshot) -> object:
    """Render the live snapshot scope and source completeness."""
    scope = snapshot.cluster or "all clusters"
    st.caption(
        f"LIVE READ · {snapshot.project_id} · {scope} · "
        f"{snapshot.start_time:%Y-%m-%d %H:%M}–{snapshot.end_time:%H:%M} UTC · "
        f"loaded {snapshot.loaded_at:%H:%M:%S} UTC"
    )
    for source in snapshot.sources:
        if source.status == "error":
            st.error(f"{source.name}: {source.detail}")
        elif source.status in {"empty", "partial"}:
            st.warning(f"{source.name}: {source.detail}")
        elif source.truncated:
            suffix = (
                "More results are available."
                if source.can_load_more
                else "The page limit was reached; narrow the time window for more detail."
            )
            st.warning(f"{source.name}: {source.detail} {suffix}")
    return snapshot

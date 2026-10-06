"""Colour palettes for the console, selected by ``ADMIN_PORTAL_THEME``.

``default`` is the console's own dark look. ``google-cloud`` is a light palette
in Google Cloud's colours. The launcher validates the value; the Streamlit
process inherits it through the environment, so one process renders one theme.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

THEME_ENV = "ADMIN_PORTAL_THEME"
DEFAULT_THEME = "default"


@dataclass(frozen=True)
class Palette:
    # Streamlit's own theme. None leaves `.streamlit/config.toml` in charge.
    streamlit_base: str | None
    background: str
    panel: str
    panel_2: str
    border: str
    text: str
    muted: str
    accent: str
    violet: str
    success: str
    warning: str
    danger: str
    error_text: str
    app_background: str
    sidebar_background: str
    card_background: str
    cell_background: str
    danger_button: str
    danger_button_text: str
    danger_button_border: str
    danger_button_hover: str
    danger_button_hover_border: str
    abort_border: str
    abort_text: str
    disabled_background: str
    disabled_border: str
    disabled_text: str
    chart_text: str
    chart_hover_background: str
    flow_link: str
    marker_outline: str
    neutral: str
    # Keyed by the enum values in admin_console.domain and by event status.
    trigger_colors: Mapping[str, str]
    status_colors: Mapping[str, str]
    attribution_colors: Mapping[str, str]


DARK = Palette(
    streamlit_base=None,
    background="#080d18",
    panel="#101827",
    panel_2="#151f32",
    border="#26344c",
    text="#edf3ff",
    muted="#8fa1bd",
    accent="#7c9cff",
    violet="#b58cff",
    success="#2ed3b7",
    warning="#ffb454",
    danger="#ff6b7a",
    error_text="#ff8b96",
    app_background=(
        "radial-gradient(circle at 84% -5%, rgba(74, 108, 247, .16), transparent 30rem), "
        "radial-gradient(circle at 8% 18%, rgba(46, 211, 183, .08), transparent 25rem), "
        "#080d18"
    ),
    sidebar_background="rgba(10, 16, 29, .96)",
    card_background="linear-gradient(145deg, rgba(21,31,50,.92), rgba(13,21,35,.92))",
    cell_background="rgba(21,31,50,.75)",
    danger_button="#b4232f",
    danger_button_text="#fff",
    danger_button_border="#ef5b68",
    danger_button_hover="#8f1823",
    danger_button_hover_border="#ff7a86",
    abort_border="#ef8f5b",
    abort_text="#ffc2a1",
    disabled_background="#2a3343",
    disabled_border="#46536a",
    disabled_text="#8fa1bd",
    chart_text="#b7c4d9",
    chart_hover_background="#151f32",
    flow_link="rgba(124,156,255,.18)",
    marker_outline="#edf3ff",
    neutral="#8FA1BD",
    trigger_colors=MappingProxyType(
        {
            "human": "#7C9CFF",
            "cron": "#B58CFF",
            "event": "#2ED3B7",
            "retry": "#FFB454",
            "agent_followup": "#FF7A90",
            "unknown": "#8FA1BD",
        }
    ),
    status_colors=MappingProxyType(
        {
            "completed": "#2ED3B7",
            "running": "#7C9CFF",
            "blocked": "#FFB454",
            "failed": "#FF6B7A",
        }
    ),
    attribution_colors=MappingProxyType(
        {
            "explicit": "#2ED3B7",
            "inherited": "#7C9CFF",
            "inferred": "#FFB454",
            "missing": "#FF6B7A",
        }
    ),
)

GOOGLE_CLOUD = Palette(
    streamlit_base="light",
    background="#ffffff",
    panel="#f8f9fa",
    panel_2="#f1f3f4",
    border="#dadce0",
    text="#202124",
    muted="#5f6368",
    accent="#1a73e8",
    violet="#9334e6",
    success="#1e8e3e",
    # Google Yellow (#f9ab00) is under 3:1 on white, too faint for chart marks.
    warning="#e37400",
    danger="#d93025",
    error_text="#d93025",
    app_background="#ffffff",
    sidebar_background="#f8f9fa",
    card_background="#ffffff",
    cell_background="#f8f9fa",
    danger_button="#d93025",
    danger_button_text="#fff",
    danger_button_border="#d93025",
    danger_button_hover="#b31412",
    danger_button_hover_border="#b31412",
    abort_border="#e37400",
    abort_text="#b06000",
    disabled_background="#f1f3f4",
    disabled_border="#dadce0",
    disabled_text="#80868b",
    chart_text="#5f6368",
    chart_hover_background="#ffffff",
    flow_link="rgba(26,115,232,.18)",
    marker_outline="#ffffff",
    neutral="#80868b",
    trigger_colors=MappingProxyType(
        {
            "human": "#1a73e8",
            "cron": "#9334e6",
            "event": "#12b5cb",
            "retry": "#e8710a",
            "agent_followup": "#e52592",
            "unknown": "#80868b",
        }
    ),
    status_colors=MappingProxyType(
        {
            "completed": "#1e8e3e",
            "running": "#1a73e8",
            "blocked": "#e37400",
            "failed": "#d93025",
        }
    ),
    attribution_colors=MappingProxyType(
        {
            "explicit": "#1e8e3e",
            "inherited": "#1a73e8",
            "inferred": "#e37400",
            "missing": "#d93025",
        }
    ),
)

THEMES = MappingProxyType({DEFAULT_THEME: DARK, "google-cloud": GOOGLE_CLOUD})


def active_theme_name() -> str:
    name = os.environ.get(THEME_ENV, "").strip() or DEFAULT_THEME
    if name not in THEMES:
        raise RuntimeError(f"{THEME_ENV} must be one of: {', '.join(THEMES)}")
    return name


def active_palette() -> Palette:
    return THEMES[active_theme_name()]


def streamlit_theme_flags(palette: Palette) -> list[str]:
    """Streamlit command-line flags that override `.streamlit/config.toml`."""
    if palette.streamlit_base is None:
        return []
    return [
        f"--theme.base={palette.streamlit_base}",
        f"--theme.primaryColor={palette.accent}",
        f"--theme.backgroundColor={palette.background}",
        f"--theme.secondaryBackgroundColor={palette.panel}",
        f"--theme.textColor={palette.text}",
        # The light secondary background is nearly white, so inputs need a border.
        "--theme.showWidgetBorder=true",
    ]

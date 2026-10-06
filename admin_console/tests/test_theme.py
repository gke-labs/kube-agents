"""ADMIN_PORTAL_THEME selects one palette for the whole console."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from admin_console import ui
from admin_console.api.streamlit_proxy import streamlit_command
from admin_console.domain import AttributionLevel, TriggerKind
from admin_console.theme import (
    DARK,
    GOOGLE_CLOUD,
    THEME_ENV,
    THEMES,
    active_palette,
    streamlit_theme_flags,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CSS_SOURCES = [
    REPO_ROOT / "admin_console" / "ui.py",
    REPO_ROOT / "admin_console" / "pages" / "autonomous.py",
]


class ThemeSelectionTest(unittest.TestCase):
    def test_unset_or_default_keeps_the_dark_theme_and_config_toml(self):
        for value in (None, "", "default"):
            env = {} if value is None else {THEME_ENV: value}
            with self.subTest(value=value), mock.patch.dict(os.environ, env, clear=True):
                self.assertIs(active_palette(), DARK)
                self.assertEqual(streamlit_theme_flags(active_palette()), [])

    def test_google_cloud_selects_the_light_palette(self):
        with mock.patch.dict(os.environ, {THEME_ENV: "google-cloud"}, clear=True):
            self.assertIs(active_palette(), GOOGLE_CLOUD)

    def test_an_unknown_theme_is_refused(self):
        with mock.patch.dict(os.environ, {THEME_ENV: "solarized"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "default, google-cloud"):
                active_palette()

    def test_google_cloud_overrides_streamlits_theme_on_the_command_line(self):
        with mock.patch.dict(os.environ, {THEME_ENV: "google-cloud"}, clear=True):
            command = streamlit_command()
        self.assertIn("--theme.base=light", command)
        self.assertIn("--theme.primaryColor=#1a73e8", command)
        self.assertIn("--theme.backgroundColor=#ffffff", command)
        self.assertIn("--theme.secondaryBackgroundColor=#f8f9fa", command)
        self.assertIn("--theme.textColor=#202124", command)
        self.assertIn("--theme.showWidgetBorder=true", command)

    def test_the_default_streamlit_command_carries_no_theme_flags(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            command = streamlit_command()
        self.assertFalse([flag for flag in command if flag.startswith("--theme.")])


class PaletteContentTest(unittest.TestCase):
    def test_dark_palette_matches_the_checked_in_streamlit_config(self):
        config = tomllib.loads((REPO_ROOT / ".streamlit" / "config.toml").read_text())
        theme = config["theme"]
        self.assertEqual(theme["base"], "dark")
        self.assertEqual(theme["primaryColor"], DARK.accent)
        self.assertEqual(theme["backgroundColor"], DARK.background)
        self.assertEqual(theme["secondaryBackgroundColor"], DARK.panel)
        self.assertEqual(theme["textColor"], DARK.text)

    def test_every_palette_colours_every_trigger_status_and_attribution(self):
        for name, palette in THEMES.items():
            with self.subTest(theme=name):
                self.assertEqual(
                    set(palette.trigger_colors), {kind.value for kind in TriggerKind}
                )
                self.assertEqual(
                    set(palette.attribution_colors),
                    {level.value for level in AttributionLevel},
                )
                self.assertEqual(
                    set(palette.status_colors),
                    {"completed", "running", "blocked", "failed"},
                )

    def test_every_css_variable_the_pages_use_is_defined_by_every_palette(self):
        used = set()
        for source in CSS_SOURCES:
            used |= set(re.findall(r"var\(--ka-([a-z0-9-]+)\)", source.read_text()))
        self.assertTrue(used)
        for name, palette in THEMES.items():
            defined = set(re.findall(r"--ka-([a-z0-9-]+):", ui._css_variables(palette)))
            with self.subTest(theme=name):
                self.assertEqual(used - defined, set())


class DefaultThemeUnchangedTest(unittest.TestCase):
    """The default palette carries the literals the console used before themes."""

    def test_dark_css_and_chart_colours_are_the_original_values(self):
        expected = {
            "background": "#080d18",
            "panel": "#101827",
            "panel_2": "#151f32",
            "border": "#26344c",
            "text": "#edf3ff",
            "muted": "#8fa1bd",
            "accent": "#7c9cff",
            "violet": "#b58cff",
            "success": "#2ed3b7",
            "warning": "#ffb454",
            "danger": "#ff6b7a",
            "error_text": "#ff8b96",
            "app_background": (
                "radial-gradient(circle at 84% -5%, rgba(74, 108, 247, .16), transparent 30rem), "
                "radial-gradient(circle at 8% 18%, rgba(46, 211, 183, .08), transparent 25rem), "
                "#080d18"
            ),
            "sidebar_background": "rgba(10, 16, 29, .96)",
            "card_background": "linear-gradient(145deg, rgba(21,31,50,.92), rgba(13,21,35,.92))",
            "cell_background": "rgba(21,31,50,.75)",
            "danger_button": "#b4232f",
            "danger_button_text": "#fff",
            "danger_button_border": "#ef5b68",
            "danger_button_hover": "#8f1823",
            "danger_button_hover_border": "#ff7a86",
            "abort_border": "#ef8f5b",
            "abort_text": "#ffc2a1",
            "disabled_background": "#2a3343",
            "disabled_border": "#46536a",
            "disabled_text": "#8fa1bd",
            "chart_text": "#b7c4d9",
            "chart_hover_background": "#151f32",
            "flow_link": "rgba(124,156,255,.18)",
            "marker_outline": "#edf3ff",
            "neutral": "#8FA1BD",
        }
        for field, value in expected.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(DARK, field), value)

    def test_dark_trigger_status_and_attribution_colours_are_the_original_values(self):
        self.assertEqual(
            dict(DARK.trigger_colors),
            {
                "human": "#7C9CFF",
                "cron": "#B58CFF",
                "event": "#2ED3B7",
                "retry": "#FFB454",
                "agent_followup": "#FF7A90",
                "unknown": "#8FA1BD",
            },
        )
        self.assertEqual(
            dict(DARK.status_colors),
            {
                "completed": "#2ED3B7",
                "running": "#7C9CFF",
                "blocked": "#FFB454",
                "failed": "#FF6B7A",
            },
        )
        self.assertEqual(
            dict(DARK.attribution_colors),
            {
                "explicit": "#2ED3B7",
                "inherited": "#7C9CFF",
                "inferred": "#FFB454",
                "missing": "#FF6B7A",
            },
        )


class LauncherThemeTest(unittest.TestCase):
    def test_launcher_accepts_exactly_the_themes_the_console_defines(self):
        script = (REPO_ROOT / "scripts" / "admin_portal.sh").read_text()
        accepted = re.search(r"^\s*([a-z| -]+)\) ;;$", script, re.MULTILINE)
        self.assertIsNotNone(accepted, "no theme case arm in admin_portal.sh")
        self.assertEqual(
            {name.strip() for name in accepted.group(1).split("|")}, set(THEMES)
        )

    def test_launcher_refuses_an_unknown_theme_before_calling_gcloud(self):
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "admin_portal.sh")],
            env={"PATH": os.environ.get("PATH", ""), "ADMIN_PORTAL_THEME": "solarized"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("ADMIN_PORTAL_THEME must be default or google-cloud", result.stderr)


if __name__ == "__main__":
    unittest.main()

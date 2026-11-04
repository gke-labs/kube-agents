"""hack/ci-deploy.sh refuses a malformed session-affinity setting before anything runs.

Both settings reach `helm --set` and the chart's schema, which refuses a
non-boolean or a non-positive slot count only at the Helm step, after the image
build. The script's check_session_affinity_settings runs first, and is stricter
than Helm: lowercase true/false only, and a slot count of at most three digits
(Helm would pass an over-long number on as a string for the schema to refuse).
These cases run that function on its own with each value.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "hack" / "ci-deploy.sh"
_FUNCTION_RE = re.compile(r"^check_session_affinity_settings\(\) \{\n.*?^\}\n", re.MULTILINE | re.DOTALL)


def _check(enabled: str, slots: str) -> subprocess.CompletedProcess:
    match = _FUNCTION_RE.search(_SCRIPT.read_text())
    assert match, "hack/ci-deploy.sh no longer defines check_session_affinity_settings"
    script = "set -euo pipefail\n" + match.group(0) + "check_session_affinity_settings\n"
    env = {"PATH": "/usr/bin:/bin", "EVAL_LITELLM_SESSION_AFFINITY": enabled, "EVAL_LITELLM_SESSION_AFFINITY_SLOTS": slots}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)


class CheckSessionAffinitySettingsTest(unittest.TestCase):
    def test_the_values_the_chart_accepts_pass(self):
        for enabled, slots in (("true", "1"), ("false", "1"), ("true", "2"), ("true", "16")):
            with self.subTest(enabled=enabled, slots=slots):
                self.assertEqual(_check(enabled, slots).returncode, 0)

    def test_a_malformed_value_stops_the_script(self):
        for enabled, slots in (("0", "1"), ("no", "1"), ("False", "1"), ("", "1"),
                               ("true", "0"), ("true", "-1"), ("true", "two"), ("true", ""), ("true", "01"), ("true", " 2"), ("true", "1000"), ("true", "99999999999999999999")):
            with self.subTest(enabled=enabled, slots=slots):
                proc = _check(enabled, slots)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("ERROR: EVAL_LITELLM_SESSION_AFFINITY", proc.stderr)

    def test_the_script_calls_it_before_its_first_command(self):
        lines = _SCRIPT.read_text().splitlines()
        call = lines.index("check_session_affinity_settings")
        commands = [l for l in lines[:call] if l.strip() and not l.lstrip().startswith("#")
                    and not re.match(r"^(readonly|export) [A-Z_]+=|^set -euo pipefail$|^check_session_affinity_settings\(\) \{$|^  |^\}$", l)]
        self.assertEqual(commands, [])


if __name__ == "__main__":
    unittest.main()

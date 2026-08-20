"""Shared harness for the observability skill's script tests.

The five scripts under `agents/platform/skills/kube-agents-observability/scripts/`
are procedural: argparse, a gcloud token, one or more HTTPS calls, print. None
of them defines a main() to import, so each test executes its subject with
`runpy.run_path` — which keeps coverage attributed to the script file — with
`gcloud` and `urllib.request.urlopen` stubbed. Nothing here ever touches the
network.

The tests live in this directory to sit beside test_submit_suggestion.py and
share this harness. PYTHON_TEST_DIRS would also discover them next to the
scripts (the `agents/*/skills/*/scripts/test_*.py` glob is how the fleet-audit
tests run), so this placement is a choice, not a constraint.
"""

import io
import json
import runpy
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent / "skills" / "kube-agents-observability" / "scripts"


class FakeResponse:
    """The slice of an HTTPResponse the scripts use: a context manager with read()."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run_script(name, argv, responses, token=b"fake-token\n"):
    """Execute one observability script with its externals stubbed.

    `responses` is a list of (url_fragment, payload) pairs. Each urlopen call is
    answered by the first pair whose fragment appears in the requested URL; a
    payload that is an Exception instance is raised instead, which is how a test
    stages an HTTPError or URLError. A URL nothing matches is a test bug and
    fails loudly.

    Returns (exit_code, stdout). exit_code is 0 when the script ran to the end
    without calling exit().
    """
    script = SCRIPTS_DIR / name

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for fragment, payload in responses:
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(payload)
        raise AssertionError(f"unexpected URL fetched: {url}")

    out = io.StringIO()
    code = 0
    with patch.object(sys, "argv", [name, *argv]), \
            patch("subprocess.check_output", return_value=token), \
            patch("urllib.request.urlopen", side_effect=fake_urlopen), \
            redirect_stdout(out):
        try:
            runpy.run_path(str(script), run_name="observability_script")
        except SystemExit as e:
            if e.code is None:
                code = 0
            elif isinstance(e.code, int):
                code = e.code
            else:
                code = 1
    return code, out.getvalue()

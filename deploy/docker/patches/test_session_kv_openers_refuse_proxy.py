"""The openers that reach Session KV take no proxy from the environment.

Run:
  python3 -m unittest discover -s deploy/docker/patches -p 'test_session_kv_openers_refuse_proxy.py' -v

Each of these modules builds its opener when it is imported. A pod that has a
proxy variable has it at that time, so each module is loaded here in a new
process with `http_proxy` set. An opener that is built with
`urllib.request.ProxyHandler({})` then has no proxy handler; one that is built
without it has the proxy of the environment, and the request to Session KV,
with its bearer token, goes to that proxy.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

PATCHES = Path(__file__).resolve().parent
PROXY = "http://egress.invalid:3128"
OPENER = "_SESSION_KV_OPENER"
MODULES = ("cron_job_template", "kanban_notifier", "kanban_origin", "slack_ux_clicks")
# Prints the http proxy of the module's opener, then that of a default opener
# of the same process, which shows that the variable reached the process.
PROBE = (
    "import importlib, sys, urllib.request\n"
    "module = importlib.import_module(sys.argv[1])\n"
    "def http_proxies(opener):\n"
    "    return [h.proxies.get('http') for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]\n"
    "print(http_proxies(getattr(module, sys.argv[2])), http_proxies(urllib.request.build_opener()))\n"
)


class SessionKVOpenersTest(unittest.TestCase):
    def test_no_opener_takes_the_proxy_of_the_environment(self):
        environment = {key: value for key, value in os.environ.items() if key.lower() not in ("no_proxy", "http_proxy")}
        environment["http_proxy"] = PROXY
        for module in MODULES:
            with self.subTest(module=module):
                done = subprocess.run(
                    [sys.executable, "-c", PROBE, module, OPENER],
                    cwd=PATCHES, env=environment, capture_output=True, text=True, check=False,
                )
                self.assertEqual(0, done.returncode, done.stderr)
                self.assertEqual(f"[] [{PROXY!r}]", done.stdout.strip())


if __name__ == "__main__":
    unittest.main()

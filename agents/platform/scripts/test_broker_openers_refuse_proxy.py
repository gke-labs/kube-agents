"""The openers that reach the credential broker take no proxy from the environment.

Run:
  python3 -m unittest discover -s agents/platform/scripts -p 'test_broker_openers_refuse_proxy.py' -v

Each of these modules builds its opener when it is imported. A pod that has a
proxy variable has it at that time, so each module is loaded here in a new
process with `http_proxy` set. An opener that is built with
`urllib.request.ProxyHandler({})` then has no proxy handler; one that is built
without it has the proxy of the environment, and the request to the broker,
with its bearer token, goes to that proxy.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PROXY = "http://egress.invalid:3128"
# The module and the name of its opener.
OPENERS = (
    ("broker_admission", "_OPENER"),
    ("dial_state", "_BROKER_OPENER"),
    ("github_token_refresh", "_IN_CLUSTER_OPENER"),
    ("google_chat_relay_patch", "_RELAY_OPENER"),
    ("slack_blocks_post", "_RELAY_OPENER"),
    ("slack_relay_patch", "_RELAY_OPENER"),
    ("teams_relay_patch", "_RELAY_OPENER"),
)
# Prints the http proxy of the module's opener, then that of a default opener
# of the same process, which shows that the variable reached the process.
PROBE = (
    "import importlib, sys, urllib.request\n"
    "module = importlib.import_module(sys.argv[1])\n"
    "def http_proxies(opener):\n"
    "    return [h.proxies.get('http') for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]\n"
    "print(http_proxies(getattr(module, sys.argv[2])), http_proxies(urllib.request.build_opener()))\n"
)


class BrokerOpenersTest(unittest.TestCase):
    def test_no_opener_takes_the_proxy_of_the_environment(self):
        environment = {key: value for key, value in os.environ.items() if key.lower() not in ("no_proxy", "http_proxy")}
        environment["http_proxy"] = PROXY
        for module, opener in OPENERS:
            with self.subTest(module=module):
                done = subprocess.run(
                    [sys.executable, "-c", PROBE, module, opener],
                    cwd=SCRIPTS, env=environment, capture_output=True, text=True, check=False,
                )
                self.assertEqual(0, done.returncode, done.stderr)
                self.assertEqual(f"[] [{PROXY!r}]", done.stdout.strip())


if __name__ == "__main__":
    unittest.main()

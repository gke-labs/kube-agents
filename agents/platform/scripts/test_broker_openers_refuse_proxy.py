"""The module-level openers of the platform scripts that refuse a proxy take none from the environment.

Run:
  python3 -m unittest discover -s agents/platform/scripts -p 'test_broker_openers_refuse_proxy.py' -v

Each of these modules builds its opener when it is imported. A pod that has a
proxy variable has it at that time, so each module is loaded here in a new
process with `http_proxy` set. An opener that is built with
`urllib.request.ProxyHandler({})` then has no proxy handler; one that is built
without it has the proxy of the environment, and the request to the broker,
with its bearer token, goes to that proxy.

The openers come from the decision table of `scripts/test_urlopen_proxy_decisions.py`:
each row of a platform script that builds an opener at module scope and refuses
the proxy today, for each target. A new client with such a row is tested here
with no change of this file. The table's own test reads the source and fails
for a call of an audited file with no row; this test loads the module and
reads the opener that it made.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
PROXY = "http://egress.invalid:3128"
DECISION_TABLE = Path(__file__).resolve().parents[3] / "scripts" / "test_urlopen_proxy_decisions.py"
DECISION_TABLE_MODULE = "urlopen_proxy_decisions"
PYTHON_SUFFIX = ".py"
# The module and the name of its opener, for each opener that the table gave
# when this test began to read it. The table can give more; it must give these.
KNOWN_OPENERS = (
    ("broker_admission", "_OPENER"),
    ("credential_proxy", "_METADATA_OPENER"),
    ("dial_state", "_BROKER_OPENER"),
    ("github_token_refresh", "metadata_open"),
    ("github_token_refresh", "_IN_CLUSTER_OPENER"),
    ("google_chat_relay_patch", "_RELAY_OPENER"),
    ("slack_blocks_post", "_RELAY_OPENER"),
    ("slack_relay_patch", "_RELAY_OPENER"),
    ("teams_relay_patch", "_RELAY_OPENER"),
)
KNOWN_OPENER_IS_GONE = (
    "the decision table no longer gives this opener; if its row went away on purpose, remove it from KNOWN_OPENERS"
)
PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy")
PROXY_VARIABLE_SUFFIX = "_proxy"
# Prints the schemes that the module's opener has a proxy for, then those of a
# default opener of the same process, which shows that the variables reached
# the process. A name that holds the `open` method of an opener stands for
# that opener; a name that holds no opener fails the probe.
PROBE = (
    "import importlib, sys, urllib.request\n"
    "module = importlib.import_module(sys.argv[1])\n"
    "def proxy_schemes(opener):\n"
    "    return sorted({scheme for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler) for scheme in h.proxies})\n"
    "opener = getattr(module, sys.argv[2])\n"
    "opener = getattr(opener, '__self__', opener)\n"
    "assert isinstance(opener, urllib.request.OpenerDirector), repr(opener)\n"
    "print(proxy_schemes(opener), proxy_schemes(urllib.request.build_opener()))\n"
)
# What the probe prints for an opener with no proxy, for one that takes each
# proxy of the environment, and for one that takes only the https proxy.
ALL_SCHEMES = "['all', 'http', 'https']"
REFUSED = f"[] {ALL_SCHEMES}"
TAKEN = f"{ALL_SCHEMES} {ALL_SCHEMES}"
TAKEN_FOR_HTTPS = f"['https'] {ALL_SCHEMES}"

# The stand-in modules of the test that holds the check itself.
TAKES_THE_PROXY = "import urllib.request\n\n_OPENER = urllib.request.build_opener()\n"
REFUSES_THE_PROXY = "import urllib.request\n\n_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
REFUSES_BY_ITS_OPEN_METHOD = (
    "import urllib.request\n\nmetadata_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open\n"
)
TWO_OPENERS = (
    "import urllib.request\n\n"
    "_REFUSES = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
    "_TAKES = urllib.request.build_opener()\n"
)
TAKES_THE_HTTPS_PROXY = (
    "import os\nimport urllib.request\n\n"
    "_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({'https': os.environ['https_proxy']}))\n"
)
FAILS_AT_IMPORT = "raise RuntimeError('this module does not load')\n"
IMPORT_FAILURE = "this module does not load"
NO_OPENER = "_OPENER = print\n"


def decision_table() -> types.ModuleType:
    """The decision table, loaded from its file: `scripts/` is no package and is not on the path."""
    spec = importlib.util.spec_from_file_location(DECISION_TABLE_MODULE, DECISION_TABLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def refusing_openers(table) -> tuple[tuple[str, str], ...]:
    """The module and the opener name of each row of `table` that this test loads.

    A row is taken when its file is a platform script, its call builds an
    opener, its scope is a name at module scope, and it refuses the proxy
    today. The target is not read: a row of each target is taken, so a new
    client is found whatever its row calls the target.
    """
    module_scope = table.MODULE_SCOPE + "."
    openers = []
    for row in table.DECISIONS:
        if not row.path.startswith(table.SCRIPTS):
            continue
        if row.call != table.BUILD_OPENER or row.state != table.REFUSES:
            continue
        if not row.scope.startswith(module_scope):
            continue
        module = row.path[len(table.SCRIPTS) :].removesuffix(PYTHON_SUFFIX).replace("/", ".")
        openers.append((module, row.scope[len(module_scope) :]))
    return tuple(dict.fromkeys(openers))


def probe(module: str, opener: str, folder: Path) -> str:
    """What the probe prints for `opener` of `module`, loaded from `folder` with each proxy variable set.

    A proxy variable of the caller is removed first, in each case, so the
    schemes of the default opener are the three that this test sets.
    """
    environment = {key: value for key, value in os.environ.items() if not key.lower().endswith(PROXY_VARIABLE_SUFFIX)}
    environment.update(dict.fromkeys(PROXY_VARIABLES, PROXY))
    done = subprocess.run(
        [sys.executable, "-c", PROBE, module, opener],
        cwd=folder, env=environment, capture_output=True, text=True, check=False,
    )
    return done.stdout.strip() if done.returncode == 0 else done.stderr.strip()


def openers_that_take_the_proxy() -> list[tuple[str, str, str]]:
    """Each opener of the decision table that does not refuse the proxy, with what the probe printed."""
    found = []
    for module, opener in refusing_openers(decision_table()):
        printed = probe(module, opener, SCRIPTS)
        if printed != REFUSED:
            found.append((module, opener, printed))
    return found


class RefusingOpenersTest(unittest.TestCase):
    maxDiff = None

    def test_no_opener_takes_the_proxy_of_the_environment(self):
        self.assertEqual([], openers_that_take_the_proxy())

    def test_the_check_loads_the_openers_of_the_decision_table(self):
        """Each opener that the stand-in table names is loaded, and each one that is not a plain refusal is found."""
        table = decision_table()
        modules = {
            "takes_the_proxy": TAKES_THE_PROXY,
            "refuses_the_proxy": REFUSES_THE_PROXY,
            "refuses_by_its_open_method": REFUSES_BY_ITS_OPEN_METHOD,
            "two_openers": TWO_OPENERS,
            "takes_the_https_proxy": TAKES_THE_HTTPS_PROXY,
            "fails_at_import": FAILS_AT_IMPORT,
            "no_opener": NO_OPENER,
        }
        openers = (
            ("takes_the_proxy", "_OPENER"),
            ("refuses_the_proxy", "_OPENER"),
            ("refuses_by_its_open_method", "metadata_open"),
            ("two_openers", "_REFUSES"),
            ("two_openers", "_TAKES"),
            ("takes_the_https_proxy", "_OPENER"),
            ("fails_at_import", "_OPENER"),
            ("no_opener", "_OPENER"),
            ("refuses_the_proxy", "_NOT_THERE"),
        )
        stand_in = types.SimpleNamespace(**vars(table))
        stand_in.DECISIONS = tuple(
            table.Decision(f"{table.SCRIPTS}{module}.py", f"<module>.{opener}", table.BUILD_OPENER, table.REFUSES, table.REFUSE, table.BROKER, table.BROKER_BEARER)
            for module, opener in openers
        )
        with tempfile.TemporaryDirectory() as folder:
            for module, text in modules.items():
                Path(folder, f"{module}.py").write_text(text, encoding="utf-8")
            this_module = sys.modules[__name__]
            with mock.patch.object(this_module, "decision_table", return_value=stand_in), mock.patch.object(this_module, "SCRIPTS", Path(folder)):
                found = openers_that_take_the_proxy()
        self.assertEqual(
            [
                ("takes_the_proxy", "_OPENER"),
                ("two_openers", "_TAKES"),
                ("takes_the_https_proxy", "_OPENER"),
                ("fails_at_import", "_OPENER"),
                ("no_opener", "_OPENER"),
                ("refuses_the_proxy", "_NOT_THERE"),
            ],
            [(module, opener) for module, opener, _ in found],
        )
        printed = {(module, opener): text for module, opener, text in found}
        self.assertEqual(TAKEN, printed[("takes_the_proxy", "_OPENER")])
        self.assertEqual(TAKEN, printed[("two_openers", "_TAKES")])
        self.assertEqual(TAKEN_FOR_HTTPS, printed[("takes_the_https_proxy", "_OPENER")])
        self.assertIn(IMPORT_FAILURE, printed[("fails_at_import", "_OPENER")])
        self.assertIn("AssertionError", printed[("no_opener", "_OPENER")])
        self.assertIn("_NOT_THERE", printed[("refuses_the_proxy", "_NOT_THERE")])

    def test_the_decision_table_still_gives_each_known_opener(self):
        openers = refusing_openers(decision_table())
        for known in KNOWN_OPENERS:
            with self.subTest(module=known[0], opener=known[1]):
                self.assertIn(known, openers, KNOWN_OPENER_IS_GONE)

    def test_each_module_level_opener_of_a_platform_script_that_refuses_the_proxy_is_taken(self):
        table = decision_table()
        row = table.Decision
        taken = (
            row(table.SCRIPTS + "new_broker_client.py", "<module>._NEW_OPENER", table.BUILD_OPENER, table.REFUSES, table.REFUSE, table.BROKER, table.BROKER_BEARER),
            # The target is not read: another target, and a target in the row's own words.
            row(table.SCRIPTS + "session_kv_client.py", "<module>._SESSION_KV_OPENER", table.BUILD_OPENER, table.REFUSES, table.REFUSE, table.SESSION_KV, table.LOOPBACK_BEARER),
            row(table.SCRIPTS + "agent_client.py", "<module>._AGENT_OPENER", table.BUILD_OPENER, table.REFUSES, table.REFUSE, "a Service that no constant names", "a reason"),
            row(table.SCRIPTS + "providers/new_client.py", "<module>._OPENER", table.BUILD_OPENER, table.REFUSES, table.REFUSE, table.BROKER, table.BROKER_BEARER),
        )
        not_taken = (
            # It takes the proxy today.
            row(table.SCRIPTS + "takes_today.py", "<module>._OPENER", table.BUILD_OPENER, table.TAKES, table.REFUSE, table.BROKER, table.BROKER_BEARER),
            # A plain `urlopen` call has no opener to load.
            row(table.SCRIPTS + "plain_call.py", "<module>._OPENER", table.URLOPEN, table.REFUSES, table.REFUSE, table.BROKER, table.BROKER_BEARER),
            # The opener is built in a function, so an import does not make it.
            row(table.SCRIPTS + "in_a_function.py", "Relay.__init__", table.BUILD_OPENER, table.REFUSES, table.REFUSE, table.BROKER, table.BROKER_BEARER),
            # A call at module scope that gives its opener no name.
            row(table.SCRIPTS + "no_name.py", "<module>", table.BUILD_OPENER, table.REFUSES, table.REFUSE, table.BROKER, table.BROKER_BEARER),
            # No platform script: this test loads modules from its own directory.
            row(table.PATCHES + "a_patch.py", "<module>._OPENER", table.BUILD_OPENER, table.REFUSES, table.REFUSE, table.BROKER, table.BROKER_BEARER),
            row("a/copy/of/" + table.SCRIPTS + "a_copy.py", "<module>._OPENER", table.BUILD_OPENER, table.REFUSES, table.REFUSE, table.BROKER, table.BROKER_BEARER),
        )
        stand_in = types.SimpleNamespace(**vars(table))
        stand_in.DECISIONS = (*not_taken[:3], *taken, *not_taken[3:], taken[0])
        self.assertEqual(
            (
                ("new_broker_client", "_NEW_OPENER"),
                ("session_kv_client", "_SESSION_KV_OPENER"),
                ("agent_client", "_AGENT_OPENER"),
                ("providers.new_client", "_OPENER"),
            ),
            refusing_openers(stand_in),
        )


if __name__ == "__main__":
    unittest.main()

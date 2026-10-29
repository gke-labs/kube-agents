"""The subscription-path grammar the operator renders by is the broker's.

Three copies in two languages decide what a Pub/Sub subscription path looks like:
`watcher_policy.py` (the wire grammar the broker matches on), the broker's own
`PLATFORM_PUBSUB_SUBSCRIPTIONS` reader, which derives from it, and the operator's
`pubsubSubscriptionPathPattern` in Go, which is hand-copied. A change to one side
without the other makes the operator render an entry the broker drops, or drop a
route the broker would admit, with nothing going red. This holds the Go copy to
the Python source it was copied from.
"""

import pathlib
import re
import sys
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_GO_FILE = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_pubsub_relay.go"
_GO_PATTERN_RE = re.compile(r"pubsubSubscriptionPathPattern = regexp\.MustCompile\(\s*`([^`]*)`", re.DOTALL)

sys.path.insert(0, str(_REPO_ROOT / "agents" / "platform" / "scripts"))
import watcher_policy  # noqa: E402


def _normalised(pattern: str) -> str:
    """Capturing or not makes no difference to what matches; strip the markers."""
    return pattern.replace("(?:", "(")


class TheOperatorRendersByTheBrokersGrammarTest(unittest.TestCase):
    def test_the_go_pattern_is_the_python_grammar(self) -> None:
        match = _GO_PATTERN_RE.search(_GO_FILE.read_text())
        self.assertIsNotNone(match, "platformagent_pubsub_relay.go no longer declares pubsubSubscriptionPathPattern")
        python = f"^projects/{watcher_policy.PUBSUB_PROJECT}/subscriptions/{watcher_policy.SUBSCRIPTION}$"
        self.assertEqual(
            _normalised(match.group(1)), _normalised(python),
            "the operator's subscription grammar differs from watcher_policy.py's; change both or neither",
        )

    def test_both_sides_agree_on_examples(self) -> None:
        go = re.compile(_GO_PATTERN_RE.search(_GO_FILE.read_text()).group(1))
        python = re.compile(f"^projects/{watcher_policy.PUBSUB_PROJECT}/subscriptions/{watcher_policy.SUBSCRIPTION}$")
        for path, ok in (
            ("projects/my-gcp-project/subscriptions/gke-stockout-alerts-sub", True),
            ("projects/123456789/subscriptions/drift-sub", True),
            ("projects/example.com:my-proj/subscriptions/drift-sub", True),
            ("projects/My-Project/subscriptions/alerts", False),
            ("projects/p/subscriptions/s", False),
            ("projects/my-gcp-project/subscriptions/a b", False),
        ):
            with self.subTest(path=path):
                self.assertEqual(bool(go.fullmatch(path)), ok)
                self.assertEqual(bool(python.fullmatch(path)), ok)


if __name__ == "__main__":
    unittest.main()

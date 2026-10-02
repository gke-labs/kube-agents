"""Every shipped persona reaches the system prompt whole.

    python3 -m unittest discover -s tests -p 'test_*.py'

Hermes caps each context file it puts in the system prompt (SOUL.md, AGENTS.md) at a
character limit, and keeps only the head and tail of a file over it. The limit is the
profile's `context_file_max_chars` when its config.yaml sets one, and otherwise a share
of the model window that falls to a flat 20,000 whenever the window is unknown or
reported small. The Chat and Platform Agent SOUL.md files are both well past 20,000, so
without a pinned cap the middle of each persona dropped out of the prompt, with nothing
but a log line to show for it.

Two ways that comes back, one check each:

* A profile's config stops pinning the cap, or pins it in a form Hermes ignores. Hermes
  reads only a positive number and silently falls back on anything else, a quoted
  "100000" included.
* A context file outgrows the cap it is pinned to.

The rule itself is not restated here: what Hermes does with the pinned value is checked
against the Hermes the image ships, by the build-time assertion in deploy/docker/Dockerfile
(the platform stage's last RUN). This file is the half that needs no Hermes, so it runs in
CI, where hermes-agent is not installed.
"""

import pathlib
import sys
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "agents"
SHARED_DEFAULTS = REPO_ROOT / "deploy" / "shared" / "defaults" / "config.yaml"

sys.path.insert(0, str(REPO_ROOT / "deploy" / "docker"))

from merge_configs import merge  # noqa: E402

CAP_KEY = "context_file_max_chars"
# The context files Hermes caps that each profile ships: SOUL.md from the profile home,
# AGENTS.md from the working-directory chain.
CONTEXT_FILES = ("SOUL.md", "AGENTS.md")
# Profiles whose config.yaml the image build merges onto the shared defaults rather than
# copying verbatim (deploy/docker/Dockerfile, merge_configs.py).
MERGED_ONTO_SHARED_DEFAULTS = {"platform"}


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def shipped_profiles():
    """Every profile that ships a config.yaml, by name."""
    return sorted(p.parent.name for p in AGENTS_DIR.glob("*/config.yaml"))


def effective_config(profile):
    """The profile's config.yaml as the image build leaves it in the template."""
    config = _load(AGENTS_DIR / profile / "config.yaml")
    if profile in MERGED_ONTO_SHARED_DEFAULTS:
        return merge(_load(SHARED_DEFAULTS), config)
    return config


class ContextFileCapTest(unittest.TestCase):
    def test_every_profile_pins_the_cap_as_a_number_hermes_reads(self):
        profiles = shipped_profiles()
        self.assertTrue(profiles, "found no agents/*/config.yaml to check")
        for profile in profiles:
            with self.subTest(profile=profile):
                cap = effective_config(profile).get(CAP_KEY)
                self.assertIsNotNone(
                    cap,
                    f"agents/{profile}/config.yaml does not set {CAP_KEY}, so Hermes caps its "
                    "context files at 20,000 chars whenever the model window is unknown",
                )
                # bool is an int subclass, and Hermes would read `true` as a cap of 1.
                self.assertIs(type(cap), int, f"{CAP_KEY} must be a plain integer, got {cap!r}")
                self.assertGreater(cap, 0)

    def test_every_shipped_context_file_fits_its_profiles_cap(self):
        for profile in shipped_profiles():
            cap = effective_config(profile).get(CAP_KEY)
            if not isinstance(cap, int):
                continue  # reported by the test above
            for name in CONTEXT_FILES:
                path = AGENTS_DIR / profile / name
                if not path.exists():
                    continue
                with self.subTest(file=f"agents/{profile}/{name}"):
                    # Characters, not bytes, after strip(): what Hermes measures.
                    size = len(path.read_text(encoding="utf-8").strip())
                    self.assertLessEqual(
                        size,
                        cap,
                        f"agents/{profile}/{name} is {size} chars, over the {cap}-char "
                        f"{CAP_KEY} its profile pins; Hermes would drop its middle from "
                        "the prompt. Trim the file or raise the cap in every profile config.",
                    )


if __name__ == "__main__":
    unittest.main()

"""A running install can tell a user where to report a problem with kube-agents.

The fact only reaches a user if three things hold at once, and none of them fails
loudly on its own: the runtime reference exists and is baked into the image, both
specialist personas point at it, and the link they carry is the docs-site short
link rather than the Google Forms URL behind it. The short link is what makes a
recreated form a one-line change to `docs/site/astro.config.mjs` instead of an
agent release, so these tests tie the link in the agent material to the redirect
that serves it.

Not asserted here: that the persona citation `/opt/defaults/docs/...` resolves at
runtime, and that the Dockerfile COPY and `OPT_DEFAULTS` in
`scripts/check_prompt_assets.py` agree. Those are `check_prompt_assets.py` and
`scripts/test_check_prompt_assets.py::test_opt_defaults_matches_the_dockerfile`.

Run:
  python3 -m unittest discover -s tests -p 'test_feedback_reference.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ASTRO_CONFIG = REPO_ROOT / "docs/site/astro.config.mjs"
DOCKERFILE = REPO_ROOT / "deploy/docker/Dockerfile"
REFERENCE = REPO_ROOT / "agents/platform/docs/kube-agents-feedback.md"
SPECIALIST_SOULS = (
    REPO_ROOT / "agents/platform/SOUL.md",
    REPO_ROOT / "agents/cluster/SOUL.md",
)
CHAT_SOUL = REPO_ROOT / "agents/chat/SOUL.md"

# The two links every agent-facing file must carry. The short link is assembled
# independently from the site config in test_short_link_matches_the_site_redirect;
# the tracker is the path for an account that can open an issue, and the
# reference lists it first.
SHORT_LINK = "https://gke-labs.github.io/kube-agents/feedback"
TRACKER = "https://github.com/gke-labs/kube-agents/issues"

# The redirect the site serves at that link, and the two pieces of the config
# that place it: `site` inside defineConfig and the `BASE` constant above it.
# Quoting is not asserted -- no CI job formats this .mjs, so a single-quote
# pattern would turn a reformat into a failure about the wrong thing.
FEEDBACK_REDIRECT_KEY = "/feedback"
QUOTED = r"""['"]([^'"]+)['"]"""
SITE_PATTERN = re.compile(rf"^\s*site:\s*{QUOTED}", re.MULTILINE)
BASE_PATTERN = re.compile(rf"^const BASE = {QUOTED}", re.MULTILINE)
REDIRECT_PATTERN = re.compile(rf"^\s*{QUOTED}:\s*FEEDBACK_FORM_URL\b", re.MULTILINE)

# The form's own URL. Agent material names the short link instead, so that a
# recreated form does not need a new agent image.
FORMS_URL = re.compile(r"docs\.google\.com/forms")

# Where the Dockerfile bakes the shared runtime references.
OPT_DEFAULTS_DOCS = "/opt/defaults/docs/"
CONTINUED_LINE = re.compile(r"\\\n\s*")

# Chat routing: the request and the specialist that answers it have to appear on
# one row of the persona's quick-reference table.
ROUTING_REQUEST = "report a bug in kube-agents"
ROUTING_TARGET = "`platform`"


def _dockerfile_instructions() -> list[str]:
    """The Dockerfile's instructions, each with its continuation lines folded in."""
    return CONTINUED_LINE.sub(" ", DOCKERFILE.read_text(encoding="utf-8")).splitlines()


class FeedbackReferenceTest(unittest.TestCase):
    def test_reference_and_specialist_souls_carry_both_links(self) -> None:
        for path in (REFERENCE, *SPECIALIST_SOULS):
            text = path.read_text(encoding="utf-8")
            for link in (SHORT_LINK, TRACKER):
                with self.subTest(path=path.relative_to(REPO_ROOT), link=link):
                    self.assertIn(
                        link,
                        text,
                        "a user asking either specialist how to report a "
                        "kube-agents problem gets an answer only if the link is "
                        "in this file",
                    )

    def test_short_link_matches_the_site_redirect(self) -> None:
        config = ASTRO_CONFIG.read_text(encoding="utf-8")
        site = SITE_PATTERN.search(config)
        base = BASE_PATTERN.search(config)
        redirect = REDIRECT_PATTERN.search(config)
        self.assertIsNotNone(site, f"no `site:` in {ASTRO_CONFIG}")
        self.assertIsNotNone(base, f"no `const BASE` in {ASTRO_CONFIG}")
        self.assertIsNotNone(
            redirect, f"no redirect to FEEDBACK_FORM_URL in {ASTRO_CONFIG}"
        )
        assert site and base and redirect  # for type checkers; asserted above
        self.assertEqual(
            FEEDBACK_REDIRECT_KEY,
            redirect.group(1),
            "the form redirect moved; the agent material points at the old path",
        )
        self.assertEqual(
            SHORT_LINK,
            f"{site.group(1)}{base.group(1)}{redirect.group(1)}",
            "the site no longer serves the link the agents hand out",
        )

    def test_no_agent_material_names_the_form_url(self) -> None:
        for path in sorted((REPO_ROOT / "agents").rglob("*.md")):
            with self.subTest(path=path.relative_to(REPO_ROOT)):
                self.assertIsNone(
                    FORMS_URL.search(path.read_text(encoding="utf-8")),
                    "agent material names the short link, not the form URL, so a "
                    "recreated form needs no agent release",
                )

    def test_the_reference_is_baked_into_the_image(self) -> None:
        baked = [
            line
            for line in _dockerfile_instructions()
            if line.startswith("COPY") and OPT_DEFAULTS_DOCS in line
        ]
        self.assertTrue(baked, f"no COPY to {OPT_DEFAULTS_DOCS} in {DOCKERFILE}")
        self.assertTrue(
            any(str(REFERENCE.relative_to(REPO_ROOT)) in line for line in baked),
            f"the reference is not copied to {OPT_DEFAULTS_DOCS}, so the path both "
            "specialist personas cite does not exist in the image",
        )

    def test_chat_persona_routes_the_request_to_the_platform_specialist(self) -> None:
        rows = [
            line
            for line in CHAT_SOUL.read_text(encoding="utf-8").splitlines()
            if ROUTING_REQUEST in line.lower()
        ]
        self.assertTrue(rows, f"nothing in {CHAT_SOUL} routes a kube-agents report")
        for row in rows:
            self.assertIn(
                ROUTING_TARGET,
                row,
                "the planning agent holds no knowledge tools; this request goes "
                "to the platform specialist",
            )


if __name__ == "__main__":
    unittest.main()

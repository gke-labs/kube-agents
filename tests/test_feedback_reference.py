"""A running install can tell a user where to report a problem with kube-agents.

The fact only reaches a user if three things hold at once, and none of them fails
loudly on its own: both specialist personas carry the two paths, they carry the
part a user has to be told before submitting (what a report needs, and that a
submission becomes public), and the link they hand out is the docs-site short
link rather than the Google Forms URL behind it. The short link is what makes a
recreated form a one-line change to `docs/site/astro.config.mjs` instead of an
agent release, so these tests tie the link in the agent material to the redirect
that serves it.

The content is in the persona bodies rather than in a runtime reference under
`/opt/defaults/docs/` because the personas are read into the prompt in the agent
pod, while a file tool is a shell command in the sandbox pod, which is not given
that directory (`deploy/sandbox/Dockerfile` stages `skills/`, `governance/` and
an allowlisted `scripts/`; `deploy/shared/sandbox_mirror.py` withholds `docs`).
A citation there would be a path the reader cannot open.

Run:
  python3 -m unittest discover -s tests -p 'test_feedback_reference.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ASTRO_CONFIG = REPO_ROOT / "docs/site/astro.config.mjs"
SPECIALIST_SOULS = (
    REPO_ROOT / "agents/platform/SOUL.md",
    REPO_ROOT / "agents/cluster/SOUL.md",
)
CHAT_SOUL = REPO_ROOT / "agents/chat/SOUL.md"
# The one place the product hands the link out unasked: the once-per-install
# feedback prompt, a cron script rather than a persona.
FEEDBACK_SCRIPT = REPO_ROOT / "agents/platform/scripts/feedback_prompt.py"

# The two links every agent-facing file must carry. The short link is assembled
# independently from the site config in test_short_link_matches_the_site_redirect;
# the tracker is the path for an account that can open an issue.
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

# The persona bullet, and the two things it has to say beyond the links: what a
# submission needs, and that filing one publishes it. Matched on the bullet's
# own paragraph so a stray "public" elsewhere in a persona cannot stand in.
BULLET_MARKER = "**Reporting a Problem with kube-agents Itself:**"
SUBMISSION_REQUIREMENTS = ("summary", "Bug", "Feature request", "Question")
PUBLIC_WARNING = re.compile(r"becomes a public issue")
REDACTION_RULE = re.compile(r"read from a Secret")

# Chat routing: the request and the specialist that answers it have to appear on
# one row of the persona's quick-reference table.
ROUTING_REQUEST = "report a bug in kube-agents"
ROUTING_TARGET = "`platform`"

# Filing on request (docs/designs/chat-feedback-command.md): the Planning Agent
# composes and confirms, the card carries the confirmation line, the platform
# worker files through the skill, and the broker and the gateway hand out the
# same two links the personas do.
CHAT_BULLET_MARKER = "**`/feedback <text>`"
FEEDBACK_ROW_MARKER = '"/feedback'
CONFIRMATION_LINE = "user-confirmed: yes"
SKILL_NAME = "kube-agents-feedback"
TITLE_CAP = "120"
SKILL_MD = REPO_ROOT / "agents/platform/skills/kube-agents-feedback/SKILL.md"
BROKER_MODULE = REPO_ROOT / "agents/platform/scripts/feedback_broker.py"
GATEWAY_COMMANDS = REPO_ROOT / "a2a/lib/chattext/commands.go"


def _line_with(path: Path, marker: str) -> str:
    """The first line of the file carrying marker, or "" if there is none."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if marker in line:
            return line
    return ""


def _feedback_bullet(path: Path) -> str:
    """The one persona bullet that answers the question, or "" if there is none."""
    return _line_with(path, BULLET_MARKER)


class FeedbackReferenceTest(unittest.TestCase):
    def test_specialist_souls_carry_both_links(self) -> None:
        for path in SPECIALIST_SOULS:
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

    def test_specialist_souls_carry_the_submission_rules(self) -> None:
        for path in SPECIALIST_SOULS:
            bullet = _feedback_bullet(path)
            with self.subTest(path=path.relative_to(REPO_ROOT)):
                self.assertTrue(
                    bullet,
                    f"no {BULLET_MARKER} bullet in {path}; the persona is the only "
                    "delivery path, since the sandbox has no /opt/defaults/docs",
                )
                for phrase in SUBMISSION_REQUIREMENTS:
                    self.assertIn(
                        phrase,
                        bullet,
                        "the bullet has to say what a submission needs; a user "
                        "cannot be sent to the form without it",
                    )
                self.assertRegex(
                    bullet,
                    PUBLIC_WARNING,
                    "a submission is published, and the user has to be told "
                    "before they write one",
                )
                self.assertRegex(
                    bullet,
                    REDACTION_RULE,
                    "the agent must not help draft a public report carrying "
                    "cluster identifiers or Secret values",
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

    def test_the_feedback_prompt_carries_the_short_link_and_the_public_warning(self) -> None:
        # The prompt reaches every install once, unasked, so it owes the reader
        # the same two facts the persona bullet does before they open the form.
        text = FEEDBACK_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(SHORT_LINK, text, "the prompt hands out a link the site no longer serves")
        self.assertRegex(text, PUBLIC_WARNING, "a submission is published; the prompt has to say so")
        self.assertRegex(text, REDACTION_RULE, "the prompt has to say what to keep out of a report")

    def test_no_agent_material_names_the_form_url(self) -> None:
        for path in sorted((REPO_ROOT / "agents").rglob("*.md")) + [FEEDBACK_SCRIPT]:
            with self.subTest(path=path.relative_to(REPO_ROOT)):
                self.assertIsNone(
                    FORMS_URL.search(path.read_text(encoding="utf-8")),
                    "agent material names the short link, not the form URL, so a "
                    "recreated form needs no agent release",
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

    def test_chat_persona_composes_and_confirms_before_dispatch(self) -> None:
        bullet = _line_with(CHAT_SOUL, CHAT_BULLET_MARKER)
        self.assertTrue(bullet, f"no {CHAT_BULLET_MARKER} bullet in {CHAT_SOUL}")
        for phrase in (CONFIRMATION_LINE, TITLE_CAP, *SUBMISSION_REQUIREMENTS):
            self.assertIn(phrase, bullet, "the front door composes the whole submission and dispatches it confirmed")
        self.assertRegex(bullet, PUBLIC_WARNING)
        self.assertRegex(bullet, REDACTION_RULE)

    def test_chat_persona_routes_the_feedback_command_to_platform(self) -> None:
        rows = [line for line in CHAT_SOUL.read_text(encoding="utf-8").splitlines()
                if line.startswith("|") and FEEDBACK_ROW_MARKER in line]
        self.assertTrue(rows, f"no /feedback row in {CHAT_SOUL}")
        for row in rows:
            self.assertIn(ROUTING_TARGET, row)

    def test_platform_persona_and_skill_share_the_confirmation_contract(self) -> None:
        bullet = _feedback_bullet(SPECIALIST_SOULS[0])
        self.assertIn(SKILL_NAME, bullet, "the platform bullet names the one path that files")
        self.assertIn(CONFIRMATION_LINE, bullet)
        self.assertIn(CONFIRMATION_LINE, SKILL_MD.read_text(encoding="utf-8"))

    def test_the_broker_and_the_gateway_hand_out_the_same_links(self) -> None:
        for path in (BROKER_MODULE, GATEWAY_COMMANDS):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(REPO_ROOT)):
                self.assertIn(SHORT_LINK, text)
                self.assertIn(TRACKER, text)


if __name__ == "__main__":
    unittest.main()

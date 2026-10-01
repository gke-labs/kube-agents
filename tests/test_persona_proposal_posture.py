"""The platform persona proposes in the reply unless asked to submit.

Under the A2A bridge the platform persona is the front door: it reads the user's
request itself, with the full CLI bundle, and there is no kanban card scoping
what it does. Asked to investigate a crashlooping workload and report the root
cause, it diagnosed correctly and then opened a GitOps pull request nobody had
asked for, on three periodic runs in a row (gke-labs/kube-agents#2037). It was
following its own instructions: "propose the fix through the active declarative
workflow" and "do not wait to be asked" read, at the front door, as "open a PR".

The rule that stops it lives in the persona and in the skill that opens the pull
request, and it reaches a running install only through the agent image. Nothing
executes the prose, so these tests pin the phrases the rule is made of: a
rewording that keeps the rule keeps them; one that drops the rule fails here
before it fails a periodic run a day later.

Run:
  python3 -m unittest discover -s tests -p 'test_persona_proposal_posture.py' -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_SOUL = REPO_ROOT / "agents/platform/SOUL.md"
SUBMIT_SKILL = REPO_ROOT / "agents/platform/skills/submit-suggestion/SKILL.md"

# The playbook item, by its lead: the rule's name, and where the skill and the
# docs point.
RULE_LEAD = "**Propose in the reply unless asked to submit.**"
# The two halves of the rule, each in the persona's own words.
ANSWER_IN_REPLY = "gets its answer in `result`"
PR_ONLY_WHEN_ASKED = "Open a pull request only when the request asks for one, or for a change to be submitted, raised or fixed"
# "Propose a manifest" is on the reply side by name, because "propose" is
# otherwise the persona's word for the pull-request path.
REPLY_SIDE_PROPOSE = "or to propose a manifest"
# The tie-break, which is what decides the crashloop case: unsure means no.
UNSURE_MEANS_NO = "If you are unsure whether a fix was asked for, it was not"
# The proactive-stance bullet has to say what its initiative is for, or it
# re-authorises the write on its own.
PROACTIVE_SCOPE = "what you observe on your own"
# The delegation section's "you decide whether to submit" defers to the rule.
WRITE_PATH_DEFERS = "under §3's rule"
# The skill's own refusal, beside its other "when not to use".
SKILL_REFUSAL = "Answering a request that asked for a diagnosis, not a change."


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) < 1000:
        raise AssertionError(f"{path} read back suspiciously short ({len(text)} chars)")
    return text


class PlatformPersonaProposesInTheReply(unittest.TestCase):
    def test_the_playbook_carries_the_rule(self):
        soul = _read(PLATFORM_SOUL)
        for phrase in (RULE_LEAD, ANSWER_IN_REPLY, REPLY_SIDE_PROPOSE, PR_ONLY_WHEN_ASKED, UNSURE_MEANS_NO):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, soul, f"agents/platform/SOUL.md lost the rule's phrase {phrase!r}")

    def test_the_rule_sits_in_the_declarative_workflow_playbook(self):
        soul = _read(PLATFORM_SOUL)
        playbook = soul.index("## 3. Declarative Workflow Playbook")
        next_section = soul.index("## 4.", playbook)
        self.assertLess(playbook, soul.index(RULE_LEAD), "the rule is before the playbook it is cited as part of")
        self.assertLess(soul.index(RULE_LEAD), next_section, "the rule is after the playbook it is cited as part of (§3, item 3)")
        # Cited as "§3, item 3" from the persona, the skill and the docs, so
        # the number is part of the rule.
        self.assertRegex(soul, r"(?m)^3\.\s+\*\*Propose in the reply unless asked to submit\.\*\*", "the rule is no longer item 3 of §3; its citations are stale")

    def test_the_proactive_stance_is_scoped_to_what_the_agent_observes(self):
        soul = _read(PLATFORM_SOUL)
        bullet_start = soul.index("**Proactive Stance:**")
        bullet = soul[bullet_start : soul.index("\n", bullet_start)]
        self.assertIn(PROACTIVE_SCOPE, bullet, "the Proactive Stance bullet no longer says what its initiative is for")

    def test_the_write_path_paragraph_defers_to_the_rule(self):
        soul = _read(PLATFORM_SOUL)
        self.assertIn(WRITE_PATH_DEFERS, soul, "the delegation section's 'you decide whether to submit' no longer defers to §3")

    def test_the_siblings_that_tell_the_agent_to_submit_are_qualified(self):
        # Three places told the persona to open the pull request after a
        # Cluster Agent's card or a crashloop walk, unconditionally. Each now
        # names the condition. Exact phrases, so a reword that drops the
        # condition fails here.
        for path, phrase in (
            (REPO_ROOT / "agents/platform/AGENTS.md", "if it asked for a diagnosis, the RCA and the patch go back in your reply"),
            (REPO_ROOT / "agents/platform/skills/cluster-agent-lifecycle/SKILL.md", "If a change is warranted **and the request asked for it**"),
            (REPO_ROOT / "agents/platform/skills/gke-workload-troubleshooting/SKILL.md", "If the request asked you to investigate, diagnose or report, the manifest"),
        ):
            with self.subTest(path=path.name):
                self.assertIn(phrase, _read(path), f"{path.relative_to(REPO_ROOT)} tells the agent to submit without the condition")

    def test_the_skill_refuses_the_same_case(self):
        skill = _read(SUBMIT_SKILL)
        when_not = skill.index("## When NOT to Use")
        self.assertIn(SKILL_REFUSAL, skill[when_not:], "submit-suggestion's 'When NOT to Use' lost the diagnostic-request bullet")


if __name__ == "__main__":
    unittest.main()

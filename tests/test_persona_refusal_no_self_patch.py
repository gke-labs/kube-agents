"""A refusal ends the action; the agent does not patch its own tooling to retry it.

Refused by the credential proxy for a repository its install does not manage, a
fleet-audit worker copied the skill and the shared scripts into `*_override`
directories, stubbed the managed-repository lookup to the skill's own example
name and ran the copy, 72 times (gke-labs/kube-agents#3444). Workers have also
re-pointed KUBECONFIG at another file to retry a refused `kubectl delete`. The
example name it used, `acme/platform/infra`, read like a configured repository.

The rule lives in prose, in the personas, the fleet-audit skill and the AI
security audit SOP, and nothing executes it, so these tests pin the phrases it
is made of: a rewording that keeps the rule keeps them; one that drops it fails
here before it fails an overnight audit.

Run:
  python3 -m unittest discover -s tests -p 'test_persona_refusal_no_self_patch.py' -v
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_SOUL = REPO_ROOT / "agents/platform/SOUL.md"
CLUSTER_SOUL = REPO_ROOT / "agents/cluster/SOUL.md"
FLEET_AUDIT_SKILL = REPO_ROOT / "agents/platform/skills/fleet-audit/SKILL.md"
FLEET_AUDIT_LIFECYCLE = REPO_ROOT / "agents/platform/skills/fleet-audit/references/lifecycle.md"
AI_SECURITY_SOP = REPO_ROOT / "agents/platform/governance/ai_security_audit_sop.md"
PROSE_ROOTS = (REPO_ROOT / "agents", REPO_ROOT / "docs")
PROSE_SUFFIX = ".md"
MIN_FILE_CHARS = 1000

# The clause each persona carries, kubeconfig named and scoped to the refused
# command (a worker reaching a card's cluster through its profile kubeconfig is
# routine and must stay allowed).
NO_SELF_PATCH_PLATFORM = (
    "no copying, editing or monkeypatching a shipped skill or script, "
    "and no re-running a refused command against another path or kubeconfig"
)
NO_SELF_PATCH_CLUSTER = (
    "nor by copying, editing or monkeypatching a shipped skill or script, "
    "or by pointing it at another path or kubeconfig"
)
# The §3 item 1 sentence the platform clause extends, by its lead and its end.
SECOND_ROUTE_LEAD = "Never look for a credential, token, key or second route to the same effect:"
SENTENCE_END = ". "
# §1's rule stays as it is.
DENIALS_TERMINAL = "**Authorization Denials Are Terminal:**"
# The skill side: a refusal is an answer, and the override copy is named.
SKILL_RULE = "**Never work around a refusal from the credential proxy or the broker.**"
OVERRIDE_DIR = "`*_override` directory"
STOP = "and stop"
NEXT_BULLET = "\n- "
LIFECYCLE_POINTER = '"Never work around a refusal"'
# A proxy that did not answer is not a refusal: step 1 and finish.md re-run
# the same command, and the rule must not tell a worker mid-`finish` to stop
# (toshiowang on #3552).
BROKER_UNAVAILABLE_CARVE_OUT = "A `BROKER UNAVAILABLE` line is not that"
RERUN = "re-run the same command"
# The SOP: its data-source list is exhaustive and covers the proxy's routes,
# and the install's own components are not targets to explore.
SOP_NO_IN_CLUSTER_CALLS = "no script or HTTP call of your own against an in-cluster service, the credential proxy's routes included"
SOP_EXHAUSTIVE = "The list is exhaustive."
SOP_OWN_COMPONENTS = "The install's own components (the platform agent, the credential proxy and the gateway, `platform-agent-gateway`) are not targets to explore"
# An example repository that reads like a configured one.
REAL_LOOKING_EXAMPLE = "acme/platform/infra"


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) < MIN_FILE_CHARS:
        raise AssertionError(f"{path} read back suspiciously short ({len(text)} chars)")
    return text


class RefusalNoSelfPatchTest(unittest.TestCase):
    # The files are tens of thousands of characters; print the message, not the file.
    longMessage = False

    def test_platform_second_route_sentence_names_self_patching(self):
        soul = _read(PLATFORM_SOUL)
        start = soul.index(SECOND_ROUTE_LEAD)
        sentence = soul[start : soul.index(SENTENCE_END, start)]
        self.assertIn(NO_SELF_PATCH_PLATFORM, sentence, "§3 item 1's second-route list lost the self-patching clause")
        self.assertIn(DENIALS_TERMINAL, soul, "§1 lost its Authorization Denials Are Terminal rule")

    def test_cluster_persona_carries_the_same_clause(self):
        self.assertIn(NO_SELF_PATCH_CLUSTER, _read(CLUSTER_SOUL), "agents/cluster/SOUL.md lost the self-patching clause")

    def test_fleet_audit_skill_says_a_refusal_is_an_answer(self):
        skill = _read(FLEET_AUDIT_SKILL)
        start = skill.index(SKILL_RULE)
        bullet = skill[start : skill.index(NEXT_BULLET, start)]
        for phrase in (OVERRIDE_DIR, STOP, BROKER_UNAVAILABLE_CARVE_OUT, RERUN):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, bullet, f"fleet-audit SKILL.md's refusal rule lost {phrase!r}")
        # The `--repo` paragraph, where the unregistered-repository case is
        # described, points at the rule rather than restating it.
        self.assertIn(LIFECYCLE_POINTER, _read(FLEET_AUDIT_LIFECYCLE), "lifecycle.md no longer points at the refusal rule")

    def test_sop_data_sources_are_exhaustive_and_own_components_are_not_targets(self):
        sop = _read(AI_SECURITY_SOP)
        for phrase in (SOP_NO_IN_CLUSTER_CALLS, SOP_EXHAUSTIVE, SOP_OWN_COMPONENTS):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, sop, f"the AI security audit SOP lost {phrase!r}")

    def test_no_prose_names_a_real_looking_example_repository(self):
        # Tracked files only: an installed docs site puts thousands of
        # vendored .md files under docs/site/node_modules.
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--", *(str(r.relative_to(REPO_ROOT)) for r in PROSE_ROOTS)],
            cwd=REPO_ROOT, capture_output=True, check=True,
        ).stdout.decode().split("\0")
        offenders = [
            name
            for name in sorted(tracked)
            if name.endswith(PROSE_SUFFIX)
            and REAL_LOOKING_EXAMPLE in (REPO_ROOT / name).read_text(encoding="utf-8")
        ]
        self.assertEqual([], offenders, f"prose still names {REAL_LOOKING_EXAMPLE}")


if __name__ == "__main__":
    unittest.main()

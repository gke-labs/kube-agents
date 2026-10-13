"""upgrade-kube-agents/SKILL.md: what its reporting section promises.

"Report confirmed upgrade bugs" lets an agent write to the public
`gke-labs/kube-agents` issue tracker under the user's GitHub token, and the
section is the only thing between a user's upgrade diagnostics and a public
page: it says who may ask for the write, when the user is asked first, what is
stripped from the report, and which labels go on it. The skill is
repository-level and not shipped in the agent images, so no bench case reaches
it and the eval loop cannot go red on it; `test_skill_discovery.py` reads its
frontmatter and never its body. Without this file an edit that drops the
refusal, widens the consent back to any GitHub write or to any write to the
tracker, or loses a term from the anonymization list ships silently, and the
first check the behaviour gets is a public issue from an ineligible requester,
or one carrying a project ID.

These tests pin what the text states and no more. Each reads the section as
whitespace-normalised prose and asks whether a phrase is there; none parses the
Markdown or scores the wording. A rewrite that keeps the promise keeps the
phrase, or changes the constant here in the same commit, which is the review
the guard exists to force.

Run:
  python3 -m unittest discover -s tests -p 'test_upgrade_skill_contract.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / ".agents/skills/upgrade-kube-agents/SKILL.md"
SECTION_HEADING = "## Report confirmed upgrade bugs"
TRACKER = "gke-labs/kube-agents"
# The period that ends a sentence: followed by whitespace or the end of the
# text, so the one inside `install.env` or a version number does not end it.
SENTENCE_END = r"\.(?=\s|$)"

# Identity first: the refusal below turns on "identity is unverified", which is
# a condition only if the check runs before every kind of write the section
# performs.
IDENTITY_FIRST = "Before any issue, comment, reopening or label write, verify the requesting user"
IDENTITY_CHECK = "`gh api user`"

# (a) Where eligibility comes from: the official repository's current state,
# fetched live. A fork's contributor list or OWNERS is not a source, so owning
# one qualifies nobody; a stale checkout or a release bundle is not one either.
ELIGIBILITY_SOURCES = (
    "contributors list",
    "`OWNERS`",
    "`OWNERS_ALIASES`",
    f"`repos/{TRACKER}/collaborators/<LOGIN>/permission`",
)
QUALIFYING_PERMISSIONS = ("`write`", "`maintain`", "`admin`")
SOURCES_FETCHED_FROM = f"Fetch these from `{TRACKER}`, not a fork, release bundle or stale local checkout"
NO_HARDCODED_NAMES = "do not hardcode names"
# What the section says does not qualify on its own, and the credentials it
# says never to read eligibility from.
INSUFFICIENT_ALONE = ("Read/triage permission", "a claimed contribution", "organization membership")
INSUFFICIENT_SENTENCE_END = "alone is insufficient"
NOT_INFERRED_FROM = "Do not infer requester eligibility from bot, application or shared administrator credentials"

# (b) The refusal: each condition ends in a local draft, and a regular user
# never gets a write, whatever the severity and whatever they approve.
REFUSAL_SENTENCE = re.compile(r"If identity is unverified.+?" + SENTENCE_END)
REFUSAL_CONDITIONS = (
    "identity is unverified",
    "no authoritative source qualifies the requester",
    "required eligibility checks cannot complete",
)
REFUSAL_OUTCOME = "keep an anonymized local draft"
NO_WRITE_FOR_REGULAR_USERS = (
    "Regular users must not open or modify issue tickets, even for p0 failures or after approving a post"
)

# (c) Consent attaches to the act, not to the repository. The user sees the
# payload before every post, and is asked before the first upgrade report is
# posted to the tracker in a session unless they authorized posting upgrade
# reports there. A write to the same repository for another purpose, an issue
# claim or a `/retest` on their own pull request, does not stand in for it.
PAYLOAD_SHOWN_BEFORE_EVERY_POST = (
    "Prepare the report first and show the user what would be posted, whether or not an ask is needed"
)
CONSENT_ASK = re.compile(
    rf"Ask the user once before the first upgrade report is posted to `{re.escape(TRACKER)}` in a session, "
    r"unless the user has authorized posting upgrade reports there in this session"
)
OTHER_WRITES_SENTENCE = re.compile(r"Authorization for other GitHub writes \((?P<writes>[^.)]+?)\) does not cover it")
WRITES_THAT_DO_NOT_COUNT = (
    "a push to a fork",
    "a comment on the user's own issue",
    "a GitOps pull request",
    f"a comment or review reply on the user's own pull request in `{TRACKER}`",
)
UPGRADE_REQUEST_IS_NOT_CONSENT = "upgrade request alone does not authorize publishing installation details"
DECLINE_KEEPS_DRAFT = "If the user declines, keep the anonymized local draft"
# The two conditions this replaced. The first gated the ask on whether GitHub
# writes in general had been authorized, which a push to a fork earlier in the
# session satisfies; the second on whether the destination had, which an issue
# claim or a `/retest` on the user's own pull request in the tracker satisfies.
# Either one's return is the regression, so both wordings are pinned absent.
SUPERSEDED_CONSENT = re.compile(
    r"GitHub writes have not been authorized|session's existing authorization|authorized that destination"
)
EXPLICIT_REPO_FLAG = f"`gh` with an explicit `--repo {TRACKER}`"

# (d) What never reaches the public payload. The detail classes are the
# section's own list, read back from its "Remove ..." sentence; the surfaces
# are what is anonymized before posting; the artifacts are never attached; the
# duplicate search is anonymized too, since it is also a request to GitHub.
STRIP_SENTENCE = re.compile(r"Remove (?P<classes>.+?) and other client-specific details" + SENTENCE_END)
CLIENT_DETAIL_CLASSES = (
    "credentials",
    "tokens",
    "session salts",
    "client/customer and user names",
    "project/cluster/org identifiers",
    "custom workload/namespace names",
    "private repository names",
    "URLs",
    "domains",
    "IP/email addresses",
    "filesystem paths",
)
ANONYMIZE_SENTENCE = re.compile(r"Anonymize the (?P<surfaces>.+?) before posting" + SENTENCE_END)
PUBLIC_SURFACES = ("title", "body", "comments", "any attachments")
ATTACH_SENTENCE = re.compile(r"Never attach raw (?P<artifacts>.+?)" + SENTENCE_END)
NEVER_ATTACHED = ("`install.env`", "Terraform state", "kubeconfig", "unfiltered logs")
SEARCH_IS_ANONYMIZED = "Use anonymized search terms; do not send raw client diagnostics"
PAYLOAD_REVIEW = "Review the complete public payload for client information before sending"

# (e) Labels. Both go on every defect report; p0 is reserved for three
# conditions, and a failed upgrade over a healthy install is not one of them.
LABELS_SENTENCE = re.compile(r"Apply (?P<labels>.+?) to a created or updated defect report")
DEFECT_LABELS = ("`bug`", "`upgrade-failure`")
P0_SENTENCE = re.compile(r"Add `priority:p0` only for (?P<conditions>.+?)" + SENTENCE_END)
P0_CONDITIONS = ("confirmed data loss", "an ongoing outage caused by the defect", "a blocked release")
P0_IS_NOT_AUTOMATIC = "A failed upgrade with a healthy existing install is not automatically p0"


def section(text: str) -> str:
    """The reporting section: from its heading to the next `## ` heading."""
    start = text.index(SECTION_HEADING)
    end = text.find("\n## ", start + len(SECTION_HEADING))
    return text[start:] if end == -1 else text[start : end + 1]


def prose(text: str) -> str:
    """The section as one line with single spaces.

    The bullets wrap at the column the file keeps, so a phrase can straddle a
    line break and two spaces of list indentation. Joining on whitespace lets
    a pin be written as the sentence reads.
    """
    return " ".join(text.split())


class ReaderTest(unittest.TestCase):
    """The reader the pins are only as good as."""

    def test_section_stops_at_the_next_heading(self) -> None:
        text = f"intro\n\n{SECTION_HEADING}\n\n- one\n- two\n\n## Next\n\n- three\n"
        self.assertEqual(f"{SECTION_HEADING}\n\n- one\n- two\n\n", section(text))

    def test_section_runs_to_the_end_when_it_is_last(self) -> None:
        text = f"{SECTION_HEADING}\n\n- only\n"
        self.assertEqual(text, section(text))

    def test_prose_joins_a_wrapped_bullet(self) -> None:
        self.assertEqual("- ask the user once", prose("- ask the\n  user   once\n"))


class ReportingContractTest(unittest.TestCase):
    def setUp(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn(SECTION_HEADING, text, "the reporting section is gone or renamed; nothing below guards anything")
        self.prose = prose(section(text))

    def assert_stated(self, phrase: str, why: str) -> None:
        self.assertIn(phrase, self.prose, f"{why}: {phrase!r} is no longer under {SECTION_HEADING!r}")

    def sentence(self, pattern: re.Pattern[str], group: str, why: str) -> str:
        match = pattern.search(self.prose)
        self.assertIsNotNone(match, f"{why}: no sentence matches {pattern.pattern!r}")
        return match.group(group)

    def test_identity_is_verified_before_any_write(self) -> None:
        self.assert_stated(IDENTITY_FIRST, "writes no longer wait for the requester's identity")
        self.assert_stated(IDENTITY_CHECK, "the identity check no longer names the command")

    def test_eligibility_comes_from_the_official_repository_as_it_stands(self) -> None:
        for source in ELIGIBILITY_SOURCES:
            with self.subTest(source=source):
                self.assert_stated(source, "an eligibility source was dropped")
        for permission in QUALIFYING_PERMISSIONS:
            with self.subTest(permission=permission):
                self.assert_stated(permission, "a qualifying permission level was dropped")
        self.assert_stated(SOURCES_FETCHED_FROM, "the sources may now be read from a copy")
        self.assert_stated(NO_HARDCODED_NAMES, "names may now be hardcoded")

    def test_what_does_not_qualify_is_still_named(self) -> None:
        for claim in INSUFFICIENT_ALONE:
            with self.subTest(claim=claim):
                self.assert_stated(claim, "an exclusion was dropped")
        self.assert_stated(INSUFFICIENT_SENTENCE_END, "the exclusions no longer say they are insufficient")
        self.assert_stated(NOT_INFERRED_FROM, "shared or non-human credentials may now confer eligibility")

    def test_an_ineligible_requester_gets_a_draft_and_no_write(self) -> None:
        refusal = self.sentence(REFUSAL_SENTENCE, 0, "the refusal sentence is gone")
        for condition in REFUSAL_CONDITIONS:
            with self.subTest(condition=condition):
                self.assertIn(condition, refusal, f"{condition!r} no longer leads to a refusal")
        self.assertIn(REFUSAL_OUTCOME, refusal, "a refusal no longer ends in a local draft")
        self.assert_stated(NO_WRITE_FOR_REGULAR_USERS, "a regular user may now write, or an exception was added")

    def test_the_payload_is_shown_before_every_post(self) -> None:
        self.assert_stated(PAYLOAD_SHOWN_BEFORE_EVERY_POST, "the user sees the payload only when an ask happens")

    def test_consent_is_asked_for_the_act_not_the_repository(self) -> None:
        self.assertRegex(self.prose, CONSENT_ASK, "the ask is no longer tied to posting upgrade reports to the tracker")
        writes = self.sentence(OTHER_WRITES_SENTENCE, "writes", "another GitHub write may now stand in for consent")
        for write in WRITES_THAT_DO_NOT_COUNT:
            with self.subTest(write=write):
                self.assertIn(write, writes, f"{write!r} may now stand in for consent")
        self.assert_stated(UPGRADE_REQUEST_IS_NOT_CONSENT, "the upgrade request may now count as consent")
        self.assert_stated(DECLINE_KEEPS_DRAFT, "a decline no longer ends in a local draft")
        self.assertNotRegex(
            self.prose,
            SUPERSEDED_CONSENT,
            "the ask is gated on GitHub writes in general or on the destination again; "
            "a push to a fork, or an issue claim in the tracker, then satisfies it",
        )

    def test_writes_name_the_tracker_explicitly(self) -> None:
        self.assert_stated(EXPLICIT_REPO_FLAG, "gh writes no longer name the repository")

    def test_every_client_detail_class_is_stripped(self) -> None:
        listed = self.sentence(STRIP_SENTENCE, "classes", "the sentence listing what to strip is gone")
        for detail in CLIENT_DETAIL_CLASSES:
            with self.subTest(detail=detail):
                self.assertIn(detail, listed, f"{detail!r} is no longer stripped before posting")

    def test_every_public_surface_is_anonymized_before_posting(self) -> None:
        surfaces = self.sentence(ANONYMIZE_SENTENCE, "surfaces", "anonymization before posting is gone")
        for surface in PUBLIC_SURFACES:
            with self.subTest(surface=surface):
                self.assertIn(surface, surfaces, f"the {surface} is no longer anonymized before posting")

    def test_raw_artifacts_are_never_attached(self) -> None:
        artifacts = self.sentence(ATTACH_SENTENCE, "artifacts", "the attachment ban is gone")
        for artifact in NEVER_ATTACHED:
            with self.subTest(artifact=artifact):
                self.assertIn(artifact, artifacts, f"{artifact} may now be attached")

    def test_search_and_payload_carry_no_client_information(self) -> None:
        self.assert_stated(SEARCH_IS_ANONYMIZED, "the duplicate search may now carry raw diagnostics")
        self.assert_stated(PAYLOAD_REVIEW, "the payload is no longer reviewed before sending")

    def test_defect_reports_carry_both_labels(self) -> None:
        labels = self.sentence(LABELS_SENTENCE, "labels", "defect reports are no longer labelled")
        for label in DEFECT_LABELS:
            with self.subTest(label=label):
                self.assertIn(label, labels, f"{label} is no longer applied to a defect report")

    def test_p0_is_reserved_for_the_three_conditions(self) -> None:
        conditions = self.sentence(P0_SENTENCE, "conditions", "p0 is no longer restricted to named conditions")
        for condition in P0_CONDITIONS:
            with self.subTest(condition=condition):
                self.assertIn(condition, conditions, f"{condition!r} is no longer a p0 condition")
        self.assert_stated(P0_IS_NOT_AUTOMATIC, "a failed upgrade over a healthy install may now be p0")


if __name__ == "__main__":
    unittest.main()

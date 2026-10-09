"""The drift-pubsub module creates its sink last and destroys it first.

Cloud Logging starts exporting the moment a sink exists and keeps exporting for
some minutes after one is deleted. An export that lands outside the window
where the topic exists and the sink's publish grant is in place mails an
"[ACTION REQUIRED] Cloud Logging sink configuration error" to every principal
holding roles/owner on the project -- `topic_permission_denied` on apply,
`topic_not_found` on destroy.

One chain of four links carries all of the module's ordering, and is the whole
of the fix:

    google_project_service_identity.logging
      -> time_sleep.logging_identity                    (the apply-side wait)
        -> google_pubsub_topic_iam_member.sink_writer   (grant before sink)
          -> time_sleep.sink_drain                      (the destroy-side wait)
            -> google_logging_project_sink.drift_audit  (sink created last)

The last three arrows are `depends_on`, and those three are what REQUIRED_EDGES
pins. The first is not: `time_sleep.logging_identity` reaches the identity
through its own `triggers`, and a reference orders as well as a `depends_on`
would, so an edge declared beside it would be redundant -- and a test asserting
that redundant edge would report an ordering failure for removing a line that
costs no ordering. That link is pinned instead by the `triggers` assertion in
`terraform/modules/drift-pubsub/tests/sink_writer_grant.tftest.hcl`, which is
where its real cost shows: lose the reference and the wait stops being re-paid
when the identity is re-minted or the duration is raised.

The scope's other projects get the same chain, one sink each in its own project
into the host topic, each behind an identity wait and a drain of its own rather
than the host's: a
source sink is destroyed on its own when its project leaves the scope, and a
drain that stays in the plan waits for nothing, so the wait has to leave with
the sink it guards:

    google_project_service_identity.source_logging
      -> time_sleep.source_logging_identity
        -> google_pubsub_topic_iam_member.source_sink_writer
          -> time_sleep.source_sink_drain
            -> google_logging_project_sink.source_drift_audit

The first arrow is the same triggers reference the host's wait carries, pinned
the same way; the other three are `depends_on` edges, and with the host's three
they are what REQUIRED_EDGES pins.
The chain roots at the service identity rather than at the topic because the
grant has nothing to bind until Service Usage has minted the Logging agent;
the topic is upstream of the grant too, by reference, so it needs no pinning
either.

Only the last two of the three pinned edges prevent the email. The first
answers a different failure: `time_sleep.logging_identity` sits between the
Service Usage call and the grant because minting the agent and being able to
bind it are different moments. On a project that did not already have one, the
grant run straight after that call fails with "Service account ... does not
exist" about one time in five, which stops the apply with the topic created and
neither grant nor sink (#2693). No sink means no export and no email -- a
louder failure, and still an install someone has to run again by hand.
REQUIRED_EDGES carries that split: each edge is paired with what removing it
costs, and no edge is listed whose removal costs nothing.

Terraform destroys in reverse dependency order, so the same chain deletes the
sink first, waits, and only then removes the grant and the topic. Keeping the
grant on the far side of the wait matters as much as the topic does: revoking
publish while the Log Router is still exporting trades `topic_not_found` for
`topic_permission_denied`, which is the same email.

None of this is observable from `terraform test`. A mocked plan cannot show
which resource was created first and has no notion of a destroy-time wait at
all, so `terraform/modules/drift-pubsub/tests/sink_writer_grant.tftest.hcl`
pins the values and this file pins the `depends_on` edges between them. Delete
any one of the three and that suite still passes green -- which is the
regression this file exists to catch. The fourth link is the exception that
proves the split: being a reference rather than an edge, it shows up in the
plan as a value, so the tftest can and does pin it.

That division is why there are only two tests here. The drain's shape and the
sink's postcondition are values, and the tftest suite reaches both; asserting
them again here would duplicate it without covering anything the plan cannot
see.

The second assertion is the specific way the fix gets undone. The grant used to
read `google_logging_project_sink.drift_audit.writer_identity`, which is what
ordered it after the sink; it now derives the identity from the project number
instead. Restoring that reference is the natural resolution of a merge conflict
in this hunk and would re-invert the order while every edge above still reads
correct.

Terraform is not a dependency of this suite; the HCL is read through the
tokenizer in `test_terraform_module_tests.py`, which drops comments and makes
each string and heredoc a single token. Reading the file as text instead is
not a smaller version of the same check, it is a broken one, in both
directions: a commented-out `# depends_on = [time_sleep.sink_drain]` left
behind by an author chasing a cycle satisfies a substring search while the
edge is gone from the module, which is this file's own regression passing
green, and an explanatory comment naming the old `writer_identity` reference
-- the kind that already sits above the grant in `main.tf` -- fails the second
test with the ordering intact. Both were measured on this file before it was
changed to tokens. Most of the module's commentary lives inside the blocks
this file reads, so neither shape is hypothetical.

Run:
  python3 -m unittest discover -s tests -p 'test_drift_pubsub_ordering.py' -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_MAIN = REPO_ROOT / "terraform" / "modules" / "drift-pubsub" / "main.tf"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from tests.test_terraform_module_tests import _STR, _WORD, _blocks, _tokens
except ImportError:  # run from inside tests/
    from test_terraform_module_tests import _STR, _WORD, _blocks, _tokens

_LIST_OPEN = "["
_LIST_CLOSE = "]"
_SEPARATORS = (",", "[")
_DEPENDS_ON = "depends_on"
_RESOURCE = "resource"
_RESOURCE_LABELS = 2

GRANT = ("google_pubsub_topic_iam_member", "sink_writer")
DRAIN = ("time_sleep", "sink_drain")
SINK = ("google_logging_project_sink", "drift_audit")
IDENTITY_WAIT = ("time_sleep", "logging_identity")
SERVICE_IDENTITY = ("google_project_service_identity", "logging")
SOURCE_GRANT = ("google_pubsub_topic_iam_member", "source_sink_writer")
SOURCE_SINK = ("google_logging_project_sink", "source_drift_audit")
SOURCE_SERVICE_IDENTITY = ("google_project_service_identity", "source_logging")
SOURCE_DRAIN = ("time_sleep", "source_sink_drain")
SOURCE_IDENTITY_WAIT = ("time_sleep", "source_logging_identity")

# The chain's first link, which is a reference rather than a depends_on and so
# cannot be read out of REQUIRED_EDGES below: the wait keys its own triggers on
# the identity's id, and that reference is what orders the wait after the mint.
# Pinned here as well as in the tftest because the two catch different
# rewrites. The tftest asserts the trigger's value, which a hand-built string
# of the right shape satisfies; this asserts that the attribute is read at all.
IDENTITY_REFERENCE = f"{SERVICE_IDENTITY[0]}.{SERVICE_IDENTITY[1]}.id"
# The source waits read their own project's identity, indexed by the for_each
# key, so the reference is the resource address and `.id` around that index.
SOURCE_IDENTITY_REFERENCE = (f"{SOURCE_SERVICE_IDENTITY[0]}.{SOURCE_SERVICE_IDENTITY[1]}", ".id")
WAITS_AND_THEIR_REFERENCES = (
    (IDENTITY_WAIT, (IDENTITY_REFERENCE,)),
    (SOURCE_IDENTITY_WAIT, SOURCE_IDENTITY_REFERENCE),
)

# Each resource, the address it must declare a depends_on edge to, and what
# removing that edge costs, in the order the chain runs. The reason each edge
# exists is in the module comment beside it; the module README's "Why the sink
# is created last and destroyed first" is the prose version.
_MAIL = (
    "Cloud Logging will export to a topic that does not exist or that it cannot "
    "publish to, and mail every project owner about it"
)
_UNBOUND_AGENT = (
    "the publish grant will run before GCP can bind the Logging service agent it "
    "names, and the apply fails with \"Service account ... does not exist\" on any "
    "project whose agent did not already exist (#2693)"
)

REQUIRED_EDGES = (
    (GRANT, IDENTITY_WAIT, _UNBOUND_AGENT),
    (DRAIN, GRANT, _MAIL),
    (SINK, DRAIN, _MAIL),
    (SOURCE_GRANT, SOURCE_IDENTITY_WAIT, _UNBOUND_AGENT),
    (SOURCE_DRAIN, SOURCE_GRANT, _MAIL),
    (SOURCE_SINK, SOURCE_DRAIN, _MAIL),
)

# The source sinks must not hang off the host's drain: it is never destroyed on
# a scope shrink, so a source sink behind it would lose its grant the second
# after it was deleted.
SOURCE_SINK_MUST_NOT_DEPEND_ON = f"{DRAIN[0]}.{DRAIN[1]}"

# Each grant and the sink attribute it must not read: doing so is what orders
# the grant after the sink.
GRANTS_AND_THEIR_SINKS = (
    (GRANT, SINK),
    (SOURCE_GRANT, SOURCE_SINK),
)


def _resource_body(tokens: list, resource_type: str, name: str) -> list:
    """The tokenized body of one top-level resource block, as (token, depth)."""
    for labels, body in _blocks(tokens, _RESOURCE, _RESOURCE_LABELS):
        if labels == [resource_type, name]:
            return body
    raise AssertionError(
        f"terraform/modules/drift-pubsub/main.tf declares no "
        f'resource "{resource_type}" "{name}"'
    )


def _depends_on_references(body: list) -> list | None:
    """The addresses in a block's `depends_on = [...]`, or None if it has none.

    The list's elements are dotted references, which the tokenizer splits into
    word and `.` tokens, so each element is rejoined from the tokens between
    its separators.
    """
    flat = [token for token, depth in body if depth == 1]
    for index in range(len(flat) - 2):
        if flat[index] != (_WORD, _DEPENDS_ON) or flat[index + 2][1] != _LIST_OPEN:
            continue
        references, current = [], ""
        for kind, value in flat[index + 2 :]:
            if value in _SEPARATORS or value == _LIST_CLOSE:
                if current:
                    references.append(current)
                current = ""
                if value == _LIST_CLOSE:
                    break
            elif kind != _STR:
                current += value
        return references
    return None


def _code_text(body: list) -> str:
    """A block's tokens rejoined, strings excluded.

    Comments are already gone -- the tokenizer drops them -- and dropping
    string contents too means a quoted identifier, as in the sink's
    error_message, reads as prose rather than as a reference.
    """
    return "".join(value for (kind, value), _depth in body if kind != _STR)


class DriftPubsubOrdering(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tokens = _tokens(MODULE_MAIN.read_text(encoding="utf-8"))

    def test_the_sink_is_the_last_link_in_the_ordering_chain(self) -> None:
        for edge in REQUIRED_EDGES:
            (dependent_type, dependent_name), (target_type, target_name), consequence = edge
            with self.subTest(dependent=dependent_name, target=target_name):
                body = _resource_body(self.tokens, dependent_type, dependent_name)
                references = _depends_on_references(body)
                self.assertIsNotNone(
                    references,
                    f"{dependent_type}.{dependent_name} declares no depends_on, so nothing "
                    f"orders it after {target_type}.{target_name}; {consequence}",
                )
                self.assertIn(
                    f"{target_type}.{target_name}",
                    references,
                    f"{dependent_type}.{dependent_name} must depend on "
                    f"{target_type}.{target_name}; {consequence}. See the comment above it "
                    f"in main.tf. A commented-out edge does not count -- this reads tokens, "
                    f"not text",
                )

    def test_each_wait_reaches_its_service_identity_by_reference(self) -> None:
        for wait, parts in WAITS_AND_THEIR_REFERENCES:
            with self.subTest(wait=wait[1]):
                text = _code_text(_resource_body(self.tokens, *wait))
                for part in parts:
                    self.assertIn(
                        part,
                        text,
                        f"{wait[0]}.{wait[1]} must read {''.join(parts)} -- it is the chain's "
                        f"first link and the wait declares no depends_on, so a trigger keyed on "
                        f"anything else (a hand-built \"projects/<project>/services/...\" string "
                        f"included) leaves nothing ordering the wait after the mint and nothing "
                        f"re-paying it when the identity is re-minted",
                    )

    def test_a_source_sink_has_its_own_drain_not_the_hosts(self) -> None:
        references = _depends_on_references(_resource_body(self.tokens, *SOURCE_SINK))
        self.assertNotIn(
            SOURCE_SINK_MUST_NOT_DEPEND_ON,
            references or [],
            "a source sink behind the host's drain is unguarded on a scope shrink: the host's "
            "drain is not destroyed then, so nothing waits between that sink's deletion and "
            "its grant's revocation; see the section comment in main.tf",
        )

    def test_the_grant_does_not_read_the_identity_off_the_sink(self) -> None:
        for grant, sink in GRANTS_AND_THEIR_SINKS:
            with self.subTest(grant=grant[1]):
                body = _resource_body(self.tokens, *grant)
                self.assertNotIn(
                    f"{sink[0]}.{sink[1]}.writer_identity",
                    _code_text(body),
                    f"{grant[0]}.{grant[1]} reads writer_identity off {sink[1]} again, which orders "
                    "the grant after the sink and reopens the apply-side window; derive the identity "
                    "from the project number instead (the expected_sink_writer_identity locals)",
                )


if __name__ == "__main__":
    unittest.main()

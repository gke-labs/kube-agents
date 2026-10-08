"""The drift-pubsub module creates its sink last and destroys it first.

Cloud Logging starts exporting the moment a sink exists and keeps exporting for
some minutes after one is deleted. An export that lands outside the window
where the topic exists and the sink's publish grant is in place mails an
"[ACTION REQUIRED] Cloud Logging sink configuration error" to every principal
holding roles/owner on the project -- `topic_permission_denied` on apply,
`topic_not_found` on destroy.

Three `depends_on` edges in the module are what prevent that, and together they
are the whole of the fix:

    google_project_service_identity.logging
      -> google_pubsub_topic_iam_member.sink_writer   (grant before sink)
        -> time_sleep.sink_drain                      (the destroy-side wait)
          -> google_logging_project_sink.drift_audit  (sink created last)

Each arrow is one `depends_on`, and those three are what REQUIRED_EDGES pins.
The chain roots at the service identity rather than at the topic because the
grant has nothing to bind until Service Usage has minted the Logging agent;
the topic is upstream of the grant too, but by reference rather than by
`depends_on`, so it needs no pinning and is not one of the three.

Terraform destroys in reverse dependency order, so the same chain deletes the
sink first, waits, and only then removes the grant and the topic. Keeping the
grant on the far side of the wait matters as much as the topic does: revoking
publish while the Log Router is still exporting trades `topic_not_found` for
`topic_permission_denied`, which is the same email.

None of this is observable from `terraform test`. A mocked plan cannot show
which resource was created first and has no notion of a destroy-time wait at
all, so `terraform/modules/drift-pubsub/tests/sink_writer_grant.tftest.hcl`
pins the values and this file pins the edges between them. Delete any one of
the three and that suite still passes green -- which is the regression this
file exists to catch.

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
SERVICE_IDENTITY = ("google_project_service_identity", "logging")

# Each resource and the address it must declare a depends_on edge to, in the
# order the chain runs. The reason each edge exists is in the module comment
# beside it; the module README's "Why the sink is created last and destroyed
# first" is the prose version.
REQUIRED_EDGES = (
    (GRANT, SERVICE_IDENTITY),
    (DRAIN, GRANT),
    (SINK, DRAIN),
)

# The sink's own attribute the grant must not read: doing so is what orders the
# grant after the sink.
SINK_WRITER_ATTRIBUTE = f"{SINK[0]}.{SINK[1]}.writer_identity"


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
        for (dependent_type, dependent_name), (target_type, target_name) in REQUIRED_EDGES:
            with self.subTest(dependent=dependent_name, target=target_name):
                body = _resource_body(self.tokens, dependent_type, dependent_name)
                references = _depends_on_references(body)
                self.assertIsNotNone(
                    references,
                    f"{dependent_type}.{dependent_name} declares no depends_on, so nothing "
                    f"orders it after {target_type}.{target_name}; Cloud Logging will export "
                    f"to a topic that does not exist or that it cannot publish to, and mail "
                    f"every project owner about it",
                )
                self.assertIn(
                    f"{target_type}.{target_name}",
                    references,
                    f"{dependent_type}.{dependent_name} must depend on "
                    f"{target_type}.{target_name}; see the comment above it in main.tf. "
                    f"A commented-out edge does not count -- this reads tokens, not text",
                )

    def test_the_grant_does_not_read_the_identity_off_the_sink(self) -> None:
        body = _resource_body(self.tokens, *GRANT)
        self.assertNotIn(
            SINK_WRITER_ATTRIBUTE,
            _code_text(body),
            "the publish grant reads writer_identity off the sink again, which orders the "
            "grant after the sink and reopens the apply-side window; derive the identity "
            "from the project number instead (local.expected_sink_writer_identity)",
        )


if __name__ == "__main__":
    unittest.main()

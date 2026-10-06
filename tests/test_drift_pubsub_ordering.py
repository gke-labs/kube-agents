"""The drift-pubsub module creates its sink last and destroys it first.

Cloud Logging starts exporting the moment a sink exists and keeps exporting for
some minutes after one is deleted. An export that lands outside the window
where the topic exists and the sink's publish grant is in place mails an
"[ACTION REQUIRED] Cloud Logging sink configuration error" to every principal
holding roles/owner on the project -- `topic_permission_denied` on apply,
`topic_not_found` on destroy.

Three `depends_on` edges in the module are what prevent that, and together they
are the whole of the fix:

    google_pubsub_topic.drift_audit
      -> google_pubsub_topic_iam_member.sink_writer   (grant before sink)
        -> time_sleep.sink_drain                      (the destroy-side wait)
          -> google_logging_project_sink.drift_audit  (sink created last)

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

Terraform is not a dependency of this suite; the HCL is read as text.

Run:
  python3 -m unittest discover -s tests -p 'test_drift_pubsub_ordering.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_MAIN = REPO_ROOT / "terraform" / "modules" / "drift-pubsub" / "main.tf"

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


def _resource_body(source: str, resource_type: str, name: str) -> str:
    """Return the body of one top-level resource block.

    Blocks in this file open at column zero and close on a brace at column
    zero, so the body is everything between -- nested braces included, since
    none of them is unindented.
    """
    opening = f'resource "{resource_type}" "{name}" {{'
    start = source.find(opening)
    if start < 0:
        raise AssertionError(
            f"terraform/modules/drift-pubsub/main.tf declares no "
            f'resource "{resource_type}" "{name}"'
        )
    rest = source[start + len(opening) :]
    end = re.search(r"^\}", rest, re.MULTILINE)
    if end is None:
        raise AssertionError(f'resource "{resource_type}" "{name}" is not closed')
    return rest[: end.start()]


class DriftPubsubOrdering(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = MODULE_MAIN.read_text(encoding="utf-8")

    def test_the_sink_is_the_last_link_in_the_ordering_chain(self) -> None:
        for (dependent_type, dependent_name), (target_type, target_name) in REQUIRED_EDGES:
            with self.subTest(dependent=dependent_name, target=target_name):
                body = _resource_body(self.source, dependent_type, dependent_name)
                depends_on = re.search(r"depends_on\s*=\s*\[(.*?)\]", body, re.DOTALL)
                self.assertIsNotNone(
                    depends_on,
                    f"{dependent_type}.{dependent_name} declares no depends_on, so nothing "
                    f"orders it after {target_type}.{target_name}; Cloud Logging will export "
                    f"to a topic that does not exist or that it cannot publish to, and mail "
                    f"every project owner about it",
                )
                self.assertIn(
                    f"{target_type}.{target_name}",
                    depends_on.group(1),
                    f"{dependent_type}.{dependent_name} must depend on "
                    f"{target_type}.{target_name}; see the comment above it in main.tf",
                )

    def test_the_grant_does_not_read_the_identity_off_the_sink(self) -> None:
        body = _resource_body(self.source, *GRANT)
        self.assertNotIn(
            SINK_WRITER_ATTRIBUTE,
            body,
            "the publish grant reads writer_identity off the sink again, which orders the "
            "grant after the sink and reopens the apply-side window; derive the identity "
            "from the project number instead (local.expected_sink_writer_identity)",
        )


if __name__ == "__main__":
    unittest.main()

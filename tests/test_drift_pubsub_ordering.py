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
COMPOSITION_MAIN = (
    REPO_ROOT / "terraform" / "examples" / "full-install" / "main.tf"
)

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
_MODULE = "module"
_MODULE_LABELS = 1

# The composition's half of the ordering, which the module cannot express.
# gke_cluster names drift_pubsub so that Terraform, destroying dependents
# first, tears the cluster down BEFORE the topic. drift_pubsub must in turn
# name nothing cluster-side, or the edge is a cycle; kube_agents_iam is the
# one that used to creep back in, through detector_service_account_email.
INGRESS_MODULE = "drift_pubsub"
CLUSTER_MODULE = "gke_cluster"
IAM_MODULE = "kube_agents_iam"
IAM_MODULE_REFERENCE = f"{_MODULE}.{IAM_MODULE}"

GRANT = ("google_pubsub_topic_iam_member", "sink_writer")
DRAIN = ("time_sleep", "sink_drain")
SINK = ("google_logging_project_sink", "drift_audit")
IDENTITY_WAIT = ("time_sleep", "logging_identity")
SERVICE_IDENTITY = ("google_project_service_identity", "logging")

# The chain's first link, which is a reference rather than a depends_on and so
# cannot be read out of REQUIRED_EDGES below: the wait keys its own triggers on
# the identity's id, and that reference is what orders the wait after the mint.
# Pinned here as well as in the tftest because the two catch different
# rewrites. The tftest asserts the trigger's value, which a hand-built string
# of the right shape satisfies; this asserts that the attribute is read at all.
IDENTITY_REFERENCE = f"{SERVICE_IDENTITY[0]}.{SERVICE_IDENTITY[1]}.id"

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


def _module_body(tokens: list, name: str) -> list:
    """The tokenized body of one top-level `module "<name>"` block."""
    for labels, body in _blocks(tokens, _MODULE, _MODULE_LABELS):
        if labels == [name]:
            return body
    raise AssertionError(
        f"terraform/examples/full-install/main.tf declares no "
        f'module "{name}"'
    )


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
        cls.composition = _tokens(COMPOSITION_MAIN.read_text(encoding="utf-8"))

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

    def test_the_wait_reaches_the_service_identity_by_reference(self) -> None:
        body = _resource_body(self.tokens, *IDENTITY_WAIT)
        self.assertIn(
            IDENTITY_REFERENCE,
            _code_text(body),
            f"{IDENTITY_WAIT[0]}.{IDENTITY_WAIT[1]} must read {IDENTITY_REFERENCE} -- it is "
            f"the chain's first link and the wait declares no depends_on, so a trigger "
            f"keyed on anything else (a hand-built \"projects/<project>/services/...\" "
            f"string included) leaves nothing ordering the wait after the mint and nothing "
            f"re-paying it when the identity is re-minted",
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

    def test_the_cluster_is_destroyed_before_the_ingress(self) -> None:
        body = _module_body(self.composition, CLUSTER_MODULE)
        references = _depends_on_references(body)
        self.assertIsNotNone(
            references,
            f'module "{CLUSTER_MODULE}" declares no depends_on at all, so nothing orders '
            f"the cluster's teardown ahead of the topic's deletion",
        )
        self.assertIn(
            f"{_MODULE}.{INGRESS_MODULE}",
            references,
            f'module "{CLUSTER_MODULE}" must depend on {_MODULE}.{INGRESS_MODULE}. Terraform '
            f"destroys dependents before dependencies, so this edge is the only thing "
            f"keeping the topic alive until the control plane has stopped emitting; "
            f"without it the topic goes minutes early and every audit record that lands "
            f"after it mails the project's owners (#2426). The edge is for the destroy "
            f"order, so no apply and no plan will show it missing",
        )

    def test_the_ingress_does_not_depend_on_the_iam_module(self) -> None:
        body = _module_body(self.composition, INGRESS_MODULE)
        self.assertNotIn(
            IAM_MODULE_REFERENCE,
            _code_text(body),
            f'module "{INGRESS_MODULE}" references {IAM_MODULE_REFERENCE} again. That module '
            f'depends on module.{CLUSTER_MODULE}, so this makes the ingress a dependent of '
            f"the cluster and inverts the destroy order the test above pins -- and because "
            f"the cluster now depends on the ingress, it is also a dependency cycle. The "
            f"detector's subscription grants are what used to carry this reference; they "
            f"live beside the module call in the composition for exactly that reason",
        )


if __name__ == "__main__":
    unittest.main()

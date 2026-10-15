"""The drift sink is created last, destroyed first, and its topic outlives the cluster.

Cloud Logging starts exporting the moment a sink exists and keeps exporting for
some minutes after one is deleted. An export that lands outside the window
where the topic exists and the sink's publish grant is in place mails an
"[ACTION REQUIRED] Cloud Logging sink configuration error" to every principal
holding roles/owner on the project -- `topic_permission_denied` on apply,
`topic_not_found` on destroy.

Two orderings carry this, in two files. Inside the module, one chain of four
links:

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

That division is why the module's share of this file is three tests rather
than a transcription of its suite. The drain's shape and the sink's
postcondition are values, and the tftest suite reaches both; asserting them
again here would duplicate it without covering anything the plan cannot see.

The other three tests are the composition's, and the module cannot express
what they pin. `full-install`'s `gke_cluster` declares `depends_on` on the ingress
module, which reverses their teardown: Terraform destroys dependents before
dependencies, so the cluster goes first and the topic last. Without that edge
the topic is deleted while the control plane is still up and still emitting
matching audit records -- measured on a CI teardown at topic t+124s against a
last record at t+366s -- and each one mails the owners. The edge exists only
for the destroy, so nothing an apply or a plan prints will show it missing.
Its companions assert the other halves: that the edge is an input rather than
a depends_on, that it indexes a single output rather than taking a splat, and
that the output it indexes names all three resources the ordering covers.
Each of those is invisible to a reader of the composition and to
`terraform validate`, and getting any of them wrong either breaks the install
or silently narrows the ordering.

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

import re
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
    from tests.test_terraform_module_tests import (
        _EQUALS,
        _STR,
        _TEMPLATE_ESCAPES,
        _TEMPLATE_OPENERS,
        _WORD,
        _blocks,
        _template_end,
        _tokens,
    )
except ImportError:  # run from inside tests/
    from test_terraform_module_tests import (
        _EQUALS,
        _STR,
        _TEMPLATE_ESCAPES,
        _TEMPLATE_OPENERS,
        _WORD,
        _blocks,
        _template_end,
        _tokens,
    )

_LIST_OPEN = "["
_LIST_CLOSE = "]"
_SEPARATORS = (",", "[")
_DEPENDS_ON = "depends_on"
_RESOURCE = "resource"
_RESOURCE_LABELS = 2
_MODULE = "module"
_MODULE_LABELS = 1
_LOCALS = "locals"
_LOCALS_LABELS = 0
# `(?<![\w.])` keeps the tail of a longer path out: `var.cfg.module.name` is
# an attribute called module, not a module reference, and reporting it would
# send an author hunting a dependency that does not exist.
_MODULE_REFERENCE = r"(?<![\w.])module\.([A-Za-z0-9_-]+)"
_LOCAL_REFERENCE = r"(?<![\w.])local\.([A-Za-z0-9_-]+)"
_DOT = "."
# _TEMPLATE_OPENERS, _TEMPLATE_ESCAPES and _template_end come from the
# tokenizer rather than being restated here. Both halves have to agree on
# where a template ends, and a second copy of that grammar is what went wrong:
# counting braces alone ends `"${join("}", [module.x.y])}"` at the `}` inside
# the nested string, losing the reference after it. _template_end skips nested
# strings, which is why the tokenizer hands that whole thing over as one token
# in the first place.
_SIGIL_WIDTH = 2
_ESCAPE_WIDTH = 3
# A following `=` or `>` means the `=` just matched was half of `==` or `=>`,
# not an assignment.
_NOT_AN_ASSIGNMENT = ("=", ">")

# The composition's half of the ordering, which the module cannot express.
# gke_cluster takes a value from drift_pubsub so that Terraform, destroying
# dependents first, tears the cluster down BEFORE the ingress. The ingress
# does reach kube_agents_iam -- #2862's source_projects does, and that module
# depends on the cluster -- so the edge has to index one output whose own
# value reaches none of it; a splat over the module would close the cycle.
INGRESS_MODULE = "drift_pubsub"
CLUSTER_MODULE = "gke_cluster"
IAM_MODULE = "kube_agents_iam"
IAM_MODULE_REFERENCE = f"{_MODULE}.{IAM_MODULE}"
#: The ingress output the cluster is ordered behind. Named rather than any
#: output, because it is the one that reaches the drain, the grant and the
#: topic -- see its description in the module's outputs.tf.
TEARDOWN_GATE = "teardown_gate"
MODULE_OUTPUTS = (
    REPO_ROOT / "terraform" / "modules" / "drift-pubsub" / "outputs.tf"
)
_OUTPUT = "output"
_OUTPUT_LABELS = 1

# The detector's own access. Both have to exist and both have to bind to the
# subscription; a merge that drops one leaves a detector that starts, is
# denied on every pull, and stays Ready -- the silent mode the detector's
# `enabled` default exists to avoid. Nothing else in the repository asserts
# they are there.
SUBSCRIPTION_GRANT = "google_pubsub_subscription_iam_member"
DETECTOR_GRANTS = (
    ("detector_subscriber", "roles/pubsub.subscriber"),
    ("detector_viewer", "roles/pubsub.viewer"),
)
SUBSCRIPTION_REFERENCE = "google_pubsub_subscription.drift_audit.id"

GRANT = ("google_pubsub_topic_iam_member", "sink_writer")
DRAIN = ("time_sleep", "sink_drain")
SINK = ("google_logging_project_sink", "drift_audit")

#: What teardown_gate has to name. The drain is first of the three destroyed,
#: so the cluster going before it carries the other two -- but all three are
#: asserted, because the value is the only place the breadth now lives and
#: narrowing it is invisible everywhere else.
GATE_MUST_NAME = (
    f"{DRAIN[0]}.{DRAIN[1]}",
    f"{GRANT[0]}.{GRANT[1]}",
    "google_pubsub_topic.drift_audit",
)
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


def _resource_body(
    tokens: list,
    resource_type: str,
    name: str,
    where: str = "terraform/modules/drift-pubsub/main.tf",
) -> list:
    """The tokenized body of one top-level resource block, as (token, depth)."""
    for labels, body in _blocks(tokens, _RESOURCE, _RESOURCE_LABELS):
        if labels == [resource_type, name]:
            return body
    raise AssertionError(f'{where} declares no resource "{resource_type}" "{name}"')


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


def _interpolations(value: str) -> list:
    """The contents of every template in a string, braces balanced.

    Neither a regex nor brace-counting is enough. `[^}]*` stops at the first
    `}`, losing a reference after an object literal in the same template
    (`${coalesce(try(var.o, {}), module.x.y)}`), and counting braces instead
    still ends early on a `}` inside a nested string literal
    (`${join("}", [module.x.y])}`) -- both shapes principals attract, since
    `try`, `coalesce` and `join` are what collect around them. The tokenizer
    settles it: _template_end skips nested strings while it counts, which is
    the same grammar that decided this string was one token.

    Both sigils are read, and a doubled one is skipped rather than matched:
    Terraform treats `$${` as a literal `${` with no reference in it.
    """
    found, index = [], 0
    while index < len(value):
        if value.startswith(_TEMPLATE_ESCAPES, index):
            index += _ESCAPE_WIDTH
            continue
        if not value.startswith(_TEMPLATE_OPENERS, index):
            index += 1
            continue
        end = _template_end(value, index + _SIGIL_WIDTH)
        found.append(value[index + _SIGIL_WIDTH : end - 1])
        index = end
    return found


def _reference_text(body: list) -> str:
    """A block's code and its string interpolations, safe to scan for refs.

    The tokenizer splits `module.x.y` into word and `.` tokens, so the halves
    have to be rejoined with nothing between them -- but joining *everything*
    with nothing also welds unrelated neighbours together, and
    `local.smuggled source` then reads as one identifier `local.smuggledsource`
    that matches no name. A separator goes in only where neither side is a
    dot, which keeps dotted references whole and everything else apart.
    """
    pieces = []
    for (kind, value), _depth in body:
        if kind == _STR:
            for interpolation in _interpolations(value):
                pieces.append(" ")
                pieces.append(interpolation)
            continue
        if pieces and value != _DOT and pieces[-1] != _DOT:
            pieces.append(" ")
        pieces.append(value)
    return "".join(pieces)


def _module_references(body: list) -> set:
    """Every `module.<name>` a block reaches directly.

    Both halves matter and the string half is the easy one to forget.
    Terraform's reference graph is not lexical: the tokenizer keeps
    `"serviceAccount:${module.x.y}"` as a single string token, so a scan of
    the code-only view sees nothing -- and that interpolated spelling is the
    composition's own idiom for principals, so it is what an author reaching
    for a module output would copy. Interpolations inside string tokens are
    read here for exactly that reason.
    """
    return set(re.findall(_MODULE_REFERENCE, _reference_text(body)))


def _local_references(body: list) -> set:
    """Every `local.<name>` a block reaches, in code or in an interpolation."""
    return set(re.findall(_LOCAL_REFERENCE, _reference_text(body)))


def _locals_definitions(tokens: list) -> dict:
    """Each `locals` entry's name mapped to the tokens of its value.

    A depth-1 word followed by `=` opens an entry; it runs to the next one.
    """
    definitions = {}
    for _labels, body in _blocks(tokens, _LOCALS, _LOCALS_LABELS):
        flat = [(token, depth) for token, depth in body]
        current, start = None, 0
        for index, ((kind, value), depth) in enumerate(flat):
            after = flat[index + 1][0] if index + 1 < len(flat) else None
            beyond = flat[index + 2][0] if index + 2 < len(flat) else None
            previous = flat[index - 1][0] if index else None
            opens = (
                depth == 1
                and kind == _WORD
                # `var.model_provider == "x"` is not an entry named
                # model_provider: the tokenizer emits `==` as two `=`, and the
                # name half is the tail of a dotted reference. Both halves of
                # that have to be excluded, and missing either one truncates
                # the real entry's value at the comparison -- which hides any
                # reference after it, the shape a ternary puts there.
                and (previous is None or previous[1] != _DOT)
                and after is not None
                and after[0] == _EQUALS
                and (beyond is None or beyond[1] not in _NOT_AN_ASSIGNMENT)
            )
            if not opens:
                continue
            if current is not None:
                definitions[current] = flat[start:index]
            current, start = value, index + 2
        if current is not None:
            definitions[current] = flat[start:]
    return definitions


def _modules_reached(body: list, definitions: dict) -> set:
    """Modules a block reaches directly or through any chain of locals.

    A local is the laundering path the assertion's own advice recommends
    ("pass a var or a local instead"), so following them is not optional.
    """
    reached = _module_references(body)
    seen, pending = set(), list(_local_references(body))
    while pending:
        name = pending.pop()
        if name in seen or name not in definitions:
            continue
        seen.add(name)
        value = definitions[name]
        reached |= _module_references(value)
        pending.extend(_local_references(value))
    return reached


def _block_strings(body: list) -> list:
    """Every string literal in a block, which _code_text deliberately drops.

    The role names this file checks are values rather than syntax, so they are
    the one thing the code-only view cannot see.
    """
    return [value for (kind, value), _depth in body if kind == _STR]


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

    def test_the_cluster_is_destroyed_before_the_ingress(self) -> None:
        body = _module_body(self.composition, CLUSTER_MODULE)
        self.assertIn(
            TEARDOWN_GATE,
            _reference_text(body),
            f'module "{CLUSTER_MODULE}" must take {TEARDOWN_GATE} from '
            f"{_MODULE}.{INGRESS_MODULE}. Terraform destroys dependents before "
            f"dependencies, so this edge is the only thing keeping the ingress alive "
            f"until the control plane has stopped emitting; without it the topic goes "
            f"minutes early and every audit record that lands after it mails the "
            f"project's owners (#2426). The edge is for the destroy order, so no apply "
            f"and no plan will show it missing. It has to be this output and not, say, "
            f"topic_name: the gate names the drain, the grant and the topic, and "
            f"ordering the topic alone leaves the grant free to be revoked while a "
            f"control plane is still emitting, which is the same mail under "
            f"topic_permission_denied",
        )

    def test_the_gate_names_the_drain_the_grant_and_the_topic(self) -> None:
        """What the gate reaches is the fix; the composition cannot show it.

        The edge indexes one output, so the ordering is only as wide as that
        output's value. Narrowing it -- to the topic alone, or dropping the
        drain -- changes nothing a reader of the composition or of
        `terraform validate` would notice, and every other test here still
        passes, because they check that the cluster takes `teardown_gate`
        rather than what `teardown_gate` is. The failure it reopens is the
        publish grant being revoked while a control plane is still emitting:
        `topic_permission_denied`, the same mail under a different code.
        """
        for labels, body in _blocks(
            _tokens(MODULE_OUTPUTS.read_text(encoding="utf-8")),
            _OUTPUT,
            _OUTPUT_LABELS,
        ):
            if labels != [TEARDOWN_GATE]:
                continue
            value = _code_text(body)
            for resource in GATE_MUST_NAME:
                with self.subTest(resource=resource):
                    self.assertIn(
                        resource,
                        value,
                        f"{TEARDOWN_GATE} must name {resource}. It is the only thing "
                        f"carrying the destroy ordering now that the edge indexes a "
                        f"single output, and nothing else in this suite or in "
                        f"terraform validate sees it narrow",
                    )
            return
        self.fail(
            f"terraform/modules/drift-pubsub/outputs.tf declares no "
            f'output "{TEARDOWN_GATE}", which is what the cluster is ordered behind'
        )

    def test_the_cluster_is_ordered_by_a_value_and_not_by_depends_on(self) -> None:
        """The edge has to be an input; a depends_on here breaks the install.

        A module-level depends_on defers every data source inside the module
        while its target has a planned change, and gke-cluster reads one --
        data.google_container_cluster.existing, the source of cluster_endpoint
        when create_cluster is false. Deferring it leaves provider "helm"
        without a host, and a helm provider that cannot reach the cluster
        reports every release as absent: the plan plans helm_release as a
        create and the apply trips over a name still in use. Both spellings
        order the destroy identically, so nothing else distinguishes them and
        the cheaper-looking one is the broken one.
        """
        body = _module_body(self.composition, CLUSTER_MODULE)
        references = _depends_on_references(body) or []
        self.assertNotIn(
            f"{_MODULE}.{INGRESS_MODULE}",
            references,
            f'module "{CLUSTER_MODULE}" names {_MODULE}.{INGRESS_MODULE} in depends_on. '
            f"Pass it as an input instead -- create_after, which only the cluster "
            f"resources depend on -- so that the data source reading an existing "
            f"cluster stays outside the edge",
        )

    def test_the_composition_still_grants_the_detector_its_subscription(self) -> None:
        for name, role in DETECTOR_GRANTS:
            with self.subTest(grant=name):
                body = _resource_body(self.tokens, SUBSCRIPTION_GRANT, name)
                code = _code_text(body)
                self.assertIn(
                    SUBSCRIPTION_REFERENCE,
                    code,
                    f"{SUBSCRIPTION_GRANT}.{name} must bind to {SUBSCRIPTION_REFERENCE}; "
                    f"nothing but this test asserts the detector's own access exists",
                )
                self.assertIn(
                    role,
                    _block_strings(body),
                    f"{SUBSCRIPTION_GRANT}.{name} must grant {role}. Losing it is silent: "
                    f"the detector starts, is denied on every pull, and the pod stays Ready",
                )

    def test_the_ordering_edge_is_indexed_and_not_a_splat(self) -> None:
        """`module.drift_pubsub[*].x` closes a dependency cycle; `[0]` does not.

        A splat over a module reaches the module's close node, so the edge
        depends on every resource in the ingress -- `source_projects` among
        them, which in this composition comes from kube-agents-iam, which
        depends on the cluster. The cluster then depends on itself and
        `terraform validate` fails with `Error: Cycle`. The indexed form
        depends on the one output it names, which is why teardown_gate names
        the three resources the ordering has to cover rather than relying on
        the splat to cover them for it.

        This replaces an earlier guard asserting the ingress call reached no
        other module at all. #2862 made that false on purpose -- the source
        sinks need the scope's project list -- so the invariant moved from
        "the ingress reaches nothing" to "the edge into the cluster reaches
        only what cannot cycle".
        """
        body = _module_body(self.composition, CLUSTER_MODULE)
        self.assertNotIn(
            f"{_MODULE}.{INGRESS_MODULE}[*]",
            _reference_text(body).replace(" ", ""),
            f'module "{CLUSTER_MODULE}" reaches {_MODULE}.{INGRESS_MODULE} through a '
            f"splat. That depends on the whole module, including source_projects, "
            f"which comes from {IAM_MODULE} and so from the cluster: a cycle. Index "
            f"the single output instead",
        )




class ReachabilityHelpers(unittest.TestCase):
    """The parsing behind the guard above, against HCL written for the purpose.

    These exist because the guard alone exercises none of it. `module
    "drift_pubsub"` in the real composition references no module and no local,
    so the transitive walk never iterates and the interpolation branch never
    sees a reference: every helper here could return an empty set and that
    guard would still pass. Two defects shipped behind exactly that gap -- a
    token join that welded `local.smuggled` to the next word, and an entry rule
    that read the `==` in `var.x == "y"` as an assignment and truncated the
    local it was in. Both were found by hand afterwards. These cases are the
    hand-checking, written down.

    `TerraformModuleTestsHelpersTest` in test_terraform_module_tests.py is the
    same arrangement for the tokenizer this builds on.
    """

    def _reached(self, source: str) -> set:
        tokens = _tokens(source)
        return _modules_reached(
            _module_body(tokens, INGRESS_MODULE), _locals_definitions(tokens)
        )

    def _call(self, argument: str, locals_body: str = "") -> str:
        block = f"locals {{\n{locals_body}\n}}\n" if locals_body else ""
        return f'{block}module "{INGRESS_MODULE}" {{\n  x = {argument}\n}}\n'

    def test_a_bare_reference_is_reached(self) -> None:
        self.assertEqual({IAM_MODULE}, self._reached(self._call(f"module.{IAM_MODULE}.email")))

    def test_a_reference_welded_to_its_neighbour_is_still_reached(self) -> None:
        # The tokenizer splits dotted paths, so the rejoin has to close them up
        # without closing up unrelated neighbours. `local.a source` became the
        # single identifier `local.asource` and resolved to nothing.
        source = (
            f'locals {{\n  a = module.{IAM_MODULE}.email\n}}\n'
            f'module "{INGRESS_MODULE}" {{\n  x = local.a\n  source = "./m"\n}}\n'
        )
        self.assertEqual({IAM_MODULE}, self._reached(source))

    def test_an_interpolated_reference_is_reached(self) -> None:
        self.assertEqual(
            {IAM_MODULE},
            self._reached(self._call(f'"serviceAccount:${{module.{IAM_MODULE}.email}}"')),
        )

    def test_a_directive_template_is_read_as_well_as_an_interpolation(self) -> None:
        self.assertEqual(
            {CLUSTER_MODULE},
            self._reached(self._call(f'"%{{ if module.{CLUSTER_MODULE}.on }}y%{{ endif }}"')),
        )

    def test_a_reference_after_an_object_literal_in_one_template_is_reached(self) -> None:
        # `[^}]*` stopped at the `}` of the object literal, not the template's.
        self.assertEqual(
            {IAM_MODULE},
            self._reached(
                self._call(f'"${{coalesce(try(var.o, {{}}), module.{IAM_MODULE}.email)}}"')
            ),
        )

    def test_a_brace_inside_a_nested_string_does_not_end_the_template(self) -> None:
        # Counting braces alone ended the template at the `}` inside "}" and
        # dropped everything after it. The tokenizer's _template_end skips
        # nested strings, which is why this arrives as one string token.
        for argument in (
            f'"${{join("}}", [module.{IAM_MODULE}.email])}}"',
            f'"${{replace(module.{IAM_MODULE}.email, "}}", "")}}"',
        ):
            with self.subTest(argument=argument):
                self.assertEqual({IAM_MODULE}, self._reached(self._call(argument)))

    def test_an_escaped_template_opener_holds_no_reference(self) -> None:
        # `$${` is a literal `${` to Terraform, so there is no dependency here
        # and reporting one sends the reader after something that is not there.
        self.assertEqual(set(), self._reached(self._call(f'"$${{module.{CLUSTER_MODULE}.name}}"')))

    def test_an_attribute_named_module_is_not_a_module(self) -> None:
        self.assertEqual(set(), self._reached(self._call("var.cfg.module.name")))

    def test_a_local_is_followed(self) -> None:
        self.assertEqual(
            {IAM_MODULE},
            self._reached(self._call("local.a", f"  a = module.{IAM_MODULE}.email")),
        )

    def test_locals_are_followed_transitively(self) -> None:
        self.assertEqual(
            {IAM_MODULE},
            self._reached(
                self._call("local.a", f"  a = local.b\n  b = module.{IAM_MODULE}.email")
            ),
        )

    def test_a_comparison_does_not_truncate_the_entry_it_sits_in(self) -> None:
        # `==` is two `=` tokens. Reading the second as an assignment opened a
        # phantom entry and cut this local's value off before the reference.
        #
        # Both operands, because two separate rules carry this and only one of
        # them is reached by each. `var.x ==` is excluded by the name being the
        # tail of a dotted path; `true ==` has no dot and is excluded only by
        # refusing an `=` that is followed by another. Testing the dotted form
        # alone leaves the second rule unexercised, which is how the first
        # version of this case passed while that rule was reverted.
        for operand in ('var.x == "y"', "true == var.x"):
            with self.subTest(operand=operand):
                self.assertEqual(
                    {IAM_MODULE},
                    self._reached(
                        self._call(
                            "local.a",
                            f'  a = {operand} ? module.{IAM_MODULE}.email : ""',
                        )
                    ),
                )

    def test_a_recursive_local_terminates(self) -> None:
        self.assertEqual(set(), self._reached(self._call("local.a", "  a = local.a")))
        self.assertEqual(
            set(), self._reached(self._call("local.a", "  a = local.b\n  b = local.a"))
        )

    def test_an_unknown_local_reaches_nothing_rather_than_raising(self) -> None:
        self.assertEqual(set(), self._reached(self._call("local.absent")))

    def test_the_composition_locals_parse_to_what_the_file_declares(self) -> None:
        # The count is the check that caught the `==` defect: the parser
        # claimed more entries than the file has, and the extras were the
        # tails of dotted references inside comparisons.
        source = COMPOSITION_MAIN.read_text(encoding="utf-8")
        parsed = _locals_definitions(_tokens(source))
        declared = {
            name
            for block in re.findall(r"^locals \{(.*?)^\}", source, re.S | re.M)
            for name in re.findall(r"^  ([A-Za-z_][A-Za-z0-9_-]*)\s*=", block, re.M)
        }
        self.assertEqual(
            declared,
            set(parsed),
            "the locals parser and the file disagree; an extra name is a "
            "mis-read assignment, and whatever it swallowed is lost from the "
            "entry it belonged to",
        )


if __name__ == "__main__":
    unittest.main()

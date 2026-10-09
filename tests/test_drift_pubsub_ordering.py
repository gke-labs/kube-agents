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
Its companion asserts the other half: that the ingress module references
nothing cluster-side, which is both what makes the edge legal (otherwise it is
a cycle) and what the detector's GSA used to smuggle back in.

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
        _WORD,
        _blocks,
        _tokens,
    )
except ImportError:  # run from inside tests/
    from test_terraform_module_tests import (
        _EQUALS,
        _STR,
        _WORD,
        _blocks,
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
# HCL has two template forms and the tokenizer knows both, so this has to as
# well: `${...}` interpolates a value, `%{...}` a directive, and either can
# carry a reference. Doubling the sigil escapes it -- `$${` is a literal `${`
# with no reference in it at all.
_TEMPLATE_OPENERS = ("${", "%{")
_TEMPLATE_ESCAPES = ("$${", "%%{")
_OPEN_BRACE = "{"
_CLOSE_BRACE = "}"
_SIGIL_WIDTH = 2
_ESCAPE_WIDTH = 3
# A following `=` or `>` means the `=` just matched was half of `==` or `=>`,
# not an assignment.
_NOT_AN_ASSIGNMENT = ("=", ">")

# The composition's half of the ordering, which the module cannot express.
# gke_cluster names drift_pubsub so that Terraform, destroying dependents
# first, tears the cluster down BEFORE the topic. drift_pubsub must in turn
# name nothing cluster-side, or the edge is a cycle; kube_agents_iam is the
# one that used to creep back in, through detector_service_account_email.
INGRESS_MODULE = "drift_pubsub"
CLUSTER_MODULE = "gke_cluster"
IAM_MODULE = "kube_agents_iam"
IAM_MODULE_REFERENCE = f"{_MODULE}.{IAM_MODULE}"

# The detector's own access, which moved out of the module to break the
# dependency above and so is no longer guaranteed by instantiating it. Both
# have to exist and both have to bind to the module's subscription; a merge
# that drops one leaves a detector that starts, is denied on every pull, and
# stays Ready -- the silent mode the detector's `enabled` default exists to
# avoid. Nothing else in the repository asserts they are there.
SUBSCRIPTION_GRANT = "google_pubsub_subscription_iam_member"
DETECTOR_GRANTS = (
    ("detector_subscriber", "roles/pubsub.subscriber"),
    ("detector_viewer", "roles/pubsub.viewer"),
)
SUBSCRIPTION_REFERENCE = f"{_MODULE}.{INGRESS_MODULE}[0].subscription_id"
COMPOSITION_PATH = "terraform/examples/full-install/main.tf"

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

    A regex cannot do this. `[^}]*` stops at the first `}`, so a reference
    after an object literal in the same template -- `${coalesce(try(var.o,
    {}), module.x.y)}` -- is lost, and principals are exactly where `try` and
    `coalesce` wrappers accumulate. Both sigils are read, and a doubled one is
    skipped rather than matched: Terraform treats `$${` as a literal.
    """
    found, index = [], 0
    while index < len(value):
        if value.startswith(_TEMPLATE_ESCAPES, index):
            index += _ESCAPE_WIDTH
            continue
        if not value.startswith(_TEMPLATE_OPENERS, index):
            index += 1
            continue
        depth, cursor = 1, index + _SIGIL_WIDTH
        while cursor < len(value) and depth:
            if value[cursor] == _OPEN_BRACE:
                depth += 1
            elif value[cursor] == _CLOSE_BRACE:
                depth -= 1
            cursor += 1
        found.append(value[index + _SIGIL_WIDTH : cursor - 1 if not depth else cursor])
        index = cursor
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

    def test_the_composition_still_grants_the_detector_its_subscription(self) -> None:
        for name, role in DETECTOR_GRANTS:
            with self.subTest(grant=name):
                body = _resource_body(
                    self.composition, SUBSCRIPTION_GRANT, name, COMPOSITION_PATH
                )
                code = _code_text(body)
                self.assertIn(
                    SUBSCRIPTION_REFERENCE,
                    code,
                    f"{SUBSCRIPTION_GRANT}.{name} must bind to {SUBSCRIPTION_REFERENCE}; "
                    f"these grants left the module so that it could stay independent of "
                    f"the cluster, which also means instantiating the module no longer "
                    f"produces them and nothing but this test does",
                )
                self.assertIn(
                    role,
                    _block_strings(body),
                    f"{SUBSCRIPTION_GRANT}.{name} must grant {role}. Losing it is silent: "
                    f"the detector starts, is denied on every pull, and the pod stays Ready",
                )

    def test_the_ingress_references_no_other_module(self) -> None:
        """The class, not one member of it.

        kube_agents_iam is how the dependency got in and is the one to expect
        back, but any module whose own graph reaches the cluster does the same
        damage, and most of them do -- the composition hangs nearly everything
        off gke_cluster. Asserting the ingress call reaches no module at all is
        both the real invariant and the cheaper thing to keep true: its
        arguments are all `var.` today.

        What the walk does not follow: a reference parked in a `resource` or
        `data` block, including the one this call already names in its
        `depends_on`. That is the same class and it is left out on purpose --
        following it means parsing arbitrary blocks, which is more parser to
        get wrong, and the hole is contained: a laundered reference that
        actually reaches the cluster is a dependency cycle, and CI runs
        `terraform validate` over `terraform/examples/*`, which refuses it.
        What is lost there is this test's explanation, not the protection.

        "Reaches" is three things, because Terraform's graph is not lexical
        and a guard that reads only bare code text misses two of them. A
        reference can be bare (module.x.y), interpolated inside a string
        ("serviceAccount:${module.x.y}", which the tokenizer hands over as one
        opaque string token and which is this composition's own idiom for
        principals), or laundered through a local -- the shape this very
        assertion used to recommend as the safe alternative. All three are
        followed, locals transitively.
        """
        body = _module_body(self.composition, INGRESS_MODULE)
        definitions = _locals_definitions(self.composition)
        referenced = sorted(
            _modules_reached(body, definitions) - {INGRESS_MODULE}
        )
        self.assertEqual(
            [],
            referenced,
            f'module "{INGRESS_MODULE}" now references {", ".join(referenced)}. Any module '
            f"reference risks reaching module.{CLUSTER_MODULE} -- {IAM_MODULE} is the one "
            f"that used to, through the detector's GSA -- which makes the ingress a "
            f"dependent of the cluster, inverts the destroy order the test above pins, and, "
            f"since the cluster now depends on the ingress, is a dependency cycle besides. "
            f"Pass a variable, or move whatever needs the reference out of the module "
            f"the way the detector's subscription grants were. A local is not a way "
            f"round this: locals are followed, including through a chain of them",
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

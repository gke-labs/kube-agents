"""What the scoped service account pool grants, and what it must not.

The pool exists because impersonation constrains only the RBAC half of GKE's
IAM-or-RBAC union: an identity holding `roles/container.viewer` reads objects in
every cluster in the project no matter how narrow its Kubernetes RBAC is. One
account per cluster was meant to move that read authority off the agent's own
identity and onto something per-cluster.

As of 2026-08-12 it does not. The IAM Condition each member was scoped by grants
nothing for Kubernetes object operations, and the un-conditioned binding is
project-wide `container.viewer`, so both were removed. A member is a principal
with no authority at all, and the pool is off by default.

That leaves the increment in a state with two halves that must move together,
which is what most of this file is about. The Terraform is read as text --
`terraform` is not a dependency of this suite -- but it is read structurally, so
a grant re-added under any name fails rather than a string sweep passing.

Run:
  python3 -m unittest discover -s tests -p 'test_scoped_sa_pool_iam.py' -v
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
IAM_MODULE = REPO_ROOT / "terraform" / "modules" / "kube-agents-iam"
FULL_INSTALL = REPO_ROOT / "terraform" / "examples" / "full-install"
BROKER_SCRIPTS = REPO_ROOT / "agents" / "platform" / "scripts"

# Roles the agent's own service account may not be granted at project level.
# The first two are the structural ones -- IAM-side authorization that outranks
# RBAC, and unscopable impersonation.
#
# `roles/iam.serviceAccountTokenCreator` is on this list and is also granted by
# scoped_pool.tf, and the two are not in tension. At project level it lets the
# agent mint a token for any service account in the project, which is a general
# escalation primitive. Bound on a single pool member as a resource, the set of
# identities the agent can become is exactly the pool. That distinction is the
# design, and `ScopedPoolCeilingTest` below asserts which form is in force.
FORBIDDEN_PROJECT_ROLES = {
    "roles/container.admin",
    "roles/container.clusterAdmin",
    "roles/container.developer",
    "roles/container.hostServiceAgentUser",
    "roles/monitoring.admin",
    "roles/logging.admin",
    "roles/owner",
    "roles/editor",
    "roles/iam.serviceAccountTokenCreator",
}


def _hcl_string_list(source: str, name: str) -> list[str]:
    """The string entries of a named HCL list, in order.

    Deliberately crude -- a regex over the source rather than an HCL parse --
    but anchored hard enough to fail rather than to quietly return nothing: the
    block must be found, and it must be non-empty. A test that silently compares
    two empty lists is the failure this whole file exists to prevent.
    """
    block = re.search(
        rf"^\s*{re.escape(name)}\s*=\s*\[(.*?)^\s*\]",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if block is None:
        raise AssertionError(f"no list named {name} found; it moved or was renamed")
    roles = re.findall(r'"([^"]+)"', block.group(1))
    if not roles:
        raise AssertionError(f"the list named {name} parsed as empty")
    return roles


def _hcl_variable_default_list(source: str, variable: str) -> list[str]:
    """The `default = [...]` of a named HCL variable block.

    Separate from `_hcl_string_list` because a variable block contains several
    `= [` assignments (validations, nested types) and the one that matters is
    the default. Anchored on the block first, then on `default` inside it.
    """
    block = re.search(
        rf'^variable\s+"{re.escape(variable)}"\s*\{{(.*?)^\}}',
        source,
        re.MULTILINE | re.DOTALL,
    )
    if block is None:
        raise AssertionError(f"no variable named {variable}; it moved or was renamed")
    return _hcl_string_list(block.group(1), "default")


def _agent_project_roles_expression(module_main: str) -> str:
    """The right-hand side of `local.agent_project_roles`, comments stripped.

    Read to its balanced end rather than to the first newline, because the
    restored form of this local is a multi-line parenthesised conditional --
    which is exactly the shape that has to be readable here.
    """
    start = re.search(r"^\s*agent_project_roles\s*=", module_main, re.MULTILINE)
    if start is None:
        raise AssertionError("local.agent_project_roles moved or was renamed")
    text, depth = "", 0
    for char in module_main[start.end() :]:
        text += char
        depth += (char in "([") - (char in ")]")
        if char == "\n" and depth <= 0:
            break
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    if not body.strip():
        raise AssertionError("could not read the agent_project_roles expression")
    return body


def _import_broker_module(name: str):
    """Import one of the broker's scripts without putting it on sys.path for good."""
    sys.path.insert(0, str(BROKER_SCRIPTS))
    try:
        return __import__(name)
    finally:
        sys.path.pop(0)


class ModuleAndCompositionAgreeTest(unittest.TestCase):
    """The two role lists an install can take, compared rather than described.

    `terraform/modules/kube-agents-iam` carries a default read-only set for a
    caller who names nothing, and `terraform/examples/full-install` resolves
    `permission_set` into an explicit list before it calls the module. Until now
    the only thing asserting they matched was a sentence in the module variable's
    description saying it mirrored the composition. A description does not fail.
    Somebody widening one path would have left the other alone and nothing would
    have said so.

    Both sides are read where the value is *bound*, not where it is described.
    An earlier version of this file compared a `local` in the module's main.tf
    that nothing referenced -- the variable is `nullable = false` with a default,
    so the local's fallback arm was unreachable -- and adding
    roles/container.admin to the list that actually granted left the suite green.
    `effective_module_roles` below is the list `google_project_iam_member` binds.
    """

    def test_the_module_binds_the_list_the_variable_carries(self):
        """The anchor for everything else in this class.

        Both tests below read `variable "project_roles"`. They are only about the
        agent's ceiling if that variable is what the module grants from, so this
        checks the wiring rather than assuming it: `local.agent_project_roles`
        resolves to the variable, and `for_each` resolves to the local.
        """
        module = (IAM_MODULE / "main.tf").read_text(encoding="utf-8")
        expression = _agent_project_roles_expression(module)
        self.assertIn(
            "var.project_roles",
            expression,
            "local.agent_project_roles no longer reads var.project_roles, so the "
            "list this file checks is not the list the module grants",
        )
        self.assertIn(
            "for_each = toset(local.agent_project_roles)",
            module,
            "google_project_iam_member.agent_roles no longer binds "
            "local.agent_project_roles",
        )

    def test_the_module_default_is_the_compositions_read_only_set(self):
        module_vars = (IAM_MODULE / "variables.tf").read_text(encoding="utf-8")
        composition = (FULL_INSTALL / "main.tf").read_text(encoding="utf-8")
        self.assertEqual(
            _hcl_string_list(composition, "read_only_roles"),
            _hcl_variable_default_list(module_vars, "project_roles"),
            "the kube-agents-iam module and the full-install composition no longer "
            "grant the same read-only set; widening one path and not the other is "
            "how an install ends up with a ceiling nobody chose",
        )

    def test_the_module_default_grants_no_forbidden_role(self):
        module_vars = (IAM_MODULE / "variables.tf").read_text(encoding="utf-8")
        self.assertEqual(
            set(),
            set(_hcl_variable_default_list(module_vars, "project_roles"))
            & FORBIDDEN_PROJECT_ROLES,
        )


class ScopedPoolCeilingTest(unittest.TestCase):
    """What the agent's own identity keeps, and what a pool member holds.

    This matters more than an ordinary least-privilege tidy-up because the agent
    container can reach the metadata server in a default install and mint a token
    for the agent's identity without going near the broker. Everything the broker
    enforces is bypassable that way; the size of that role set is not.
    """

    def _pool_declarations(self) -> str:
        """scoped_pool.tf with the comments stripped.

        That file explains the removed grant in prose, and a substring match
        would find its own explanation.
        """
        source = (IAM_MODULE / "scoped_pool.tf").read_text(encoding="utf-8")
        return "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )

    def _pool_grants_anything(self) -> bool:
        """Does a pool member hold any authority of its own? Deliberately False.

        There is nothing in this tree this can point at, and the first version
        pretended otherwise: it searched scoped_pool.tf for
        `resource "google_project_iam_member"`, a literal the test two methods
        down *forbids* -- so the predicate was statically False in any tree
        where the suite passes, which is a different thing from being False on
        purpose. Worse, it made the coupled test below unpassable for the
        change it exists to permit: the pool's replacement authority is
        per-cluster Kubernetes RBAC, which adds no Terraform grant at all, so
        the restoration would have been told to add exactly the resource the
        sibling test refuses.

        So this is now an explicit tripwire. The change that restores the
        pool's authority (per-cluster RBAC bindings, by the accounts' numeric
        unique IDs -- see scoped_pool.tf on why not the email) must repoint
        this method at that authority's real source in the same change. Until
        then it returns False, and the guard below reads: the agent is never
        narrowed.
        """
        return False

    def _agent_is_narrowed(self) -> bool:
        """Has roles/container.viewer come off the set the module actually binds?

        Answered from the resulting role set, not from one spelling of how it
        got that way. The first version of this asked whether main.tf contained
        the literal `role != "roles/container.viewer"` filter, which is only the
        spelling the suspended coupling happens to use. Deleting the role from
        the variable's default reaches the identical state -- agent narrowed,
        pool granting nothing, no identity able to read a Kubernetes object --
        and the substring check called it not-narrowed and passed.

        So: take the list the module binds and apply the filter if it is there.
        Either route to an absent container.viewer reads as narrowed.
        """
        module_vars = (IAM_MODULE / "variables.tf").read_text(encoding="utf-8")
        module_main = (IAM_MODULE / "main.tf").read_text(encoding="utf-8")

        granted = _hcl_variable_default_list(module_vars, "project_roles")
        expression = _agent_project_roles_expression(module_main)
        self.assertIn(
            "var.project_roles",
            expression,
            "local.agent_project_roles no longer reads var.project_roles, so this "
            "test is no longer looking at the roles the module grants",
        )
        if 'role != "roles/container.viewer"' in expression:
            granted = [role for role in granted if role != "roles/container.viewer"]
        return "roles/container.viewer" not in granted

    def test_the_agent_is_never_narrowed_while_the_pool_grants_nothing(self):
        """The coupling, as an assertion rather than a comment.

        Two changes ship together or not at all: the pool carrying
        container.viewer per cluster, and the agent's own identity losing it.
        Neither is safe alone. Arming the pool while the agent stays wide leaves
        the ceiling the pool exists to remove. Narrowing the agent while the pool
        grants nothing is a total outage -- no identity anywhere can read a
        Kubernetes object, and the runtime flag cannot rescue it, because
        CREDENTIAL_PROXY_SCOPED_SA_POOL=0 falls back to the very credential that
        was stripped.

        As of 2026-08-12 both are off: the IAM Condition scoping the pool grants
        nothing, so the grant and the narrowing were removed together. This test
        passes in that state and in the fully-restored state, and fails in either
        half-way house.
        """
        if self._agent_is_narrowed():
            self.assertTrue(
                self._pool_grants_anything(),
                "main.tf strips roles/container.viewer from the agent while no "
                "pool member holds any IAM grant. Nothing can read a Kubernetes "
                "object, and CREDENTIAL_PROXY_SCOPED_SA_POOL=0 does not help -- "
                "it falls back to the credential that was just stripped. Restore "
                "the pool's authority (per-cluster RBAC) in the same change that "
                "restores the narrowing, and repoint _pool_grants_anything at "
                "that authority's source -- it is deliberately False until the "
                "source exists, so this arm fails by construction for a "
                "narrowing that arrives without it.",
            )
        else:
            self.assertFalse(
                self._pool_grants_anything(),
                "a pool member holds an IAM grant while the agent's own identity "
                "keeps roles/container.viewer. The ceiling the pool exists to "
                "remove is still there, and the pool is now a second way to reach "
                "it. Narrow the agent in the same change.",
            )

    def test_the_agent_can_still_reach_the_control_plane(self):
        """Narrowing to nothing would be a different bug.

        `container.clusterViewer` carries container.clusters.get and .list, which
        is what `gcloud container clusters get-credentials` and the fleet
        reconcile loop run on. Dropping it would break the broker's own kubeconfig
        materialisation, and the failure would look like a pool problem rather
        than a ceiling problem.
        """
        module_vars = (IAM_MODULE / "variables.tf").read_text(encoding="utf-8")
        self.assertIn(
            "roles/container.clusterViewer",
            _hcl_variable_default_list(module_vars, "project_roles"),
        )

    def test_the_pool_grants_token_creator_per_account_and_never_project_wide(self):
        """The one line that decides whether the pool is a boundary.

        roles/iam.serviceAccountTokenCreator at project scope would let the agent
        mint a token for any service account in the project -- a general
        escalation primitive that makes the per-cluster accounts decorative, since
        the agent could just become something wider. Bound on each pool member as
        a resource, the set of identities it can become is exactly the pool.

        Asserted by resource type, because the difference between safe and
        catastrophic here is `google_service_account_iam_member` versus
        `google_project_iam_member` and nothing else in the block would look
        wrong.
        """
        source = (IAM_MODULE / "scoped_pool.tf").read_text(encoding="utf-8")
        grants = re.findall(
            r'resource\s+"(google_\w+_iam_member)"\s+"[^"]*"\s*\{(.*?)\n\}',
            source,
            re.DOTALL,
        )
        self.assertTrue(grants, "no IAM member resources found in scoped_pool.tf")
        token_creator = [
            resource_type
            for resource_type, body in grants
            if "roles/iam.serviceAccountTokenCreator" in body
        ]
        self.assertEqual(
            ["google_service_account_iam_member"],
            token_creator,
            "the token-creator grant is not bound on the service account as a "
            "resource; at project scope it lets the agent impersonate anything",
        )

    def test_no_pool_member_holds_a_project_level_container_grant(self):
        """The pool members must hold nothing in IAM, and this is why.

        A conditioned `roles/container.viewer` grants nothing for a Kubernetes
        object operation -- measured 2026-08-12, four condition spellings, all
        refused, including one asserting only that the call is a GKE call. So the
        condition cannot come back.

        The un-conditioned form is worse, and that is the case this test really
        exists for. Someone reading "the condition does nothing" will reach for
        the obvious repair and delete the condition, leaving every pool member
        with project-wide `container.viewer` -- the precise ceiling the pool was
        built to remove, arrived at by way of a fix.

        Either edit fails here. The correct state is no grant: authority comes
        from per-cluster RBAC, which is a separate change.
        """
        body = self._pool_declarations()
        self.assertNotIn(
            'resource "google_project_iam_member"',
            body,
            "a pool member has been given a project-level IAM grant. Conditioned, "
            "it grants nothing for object operations; un-conditioned, it grants "
            "every cluster in the project. Authority for a pool member comes from "
            "per-cluster RBAC.",
        )
        self.assertNotIn(
            "condition {",
            body,
            "an IAM Condition is back in the pool module. Measured 2026-08-12: "
            "resource attributes are not populated on GKE's object-authorization "
            "path, so no condition scopes a kubectl read.",
        )

    def test_the_pool_key_is_the_brokers_key(self):
        """Terraform and the broker must spell a cluster identically.

        This is what survived the condition's removal. The key is the pool's index
        -- the broker looks a member up by it and Terraform files a member under
        it -- so a drift between the two spellings means every request for that
        cluster is refused. It imports the broker's own function rather than
        restating the format, because a second copy of the format here would only
        make the test agree with itself.
        """
        scoped_sa_pool = _import_broker_module("scoped_sa_pool")

        source = (IAM_MODULE / "scoped_pool.tf").read_text(encoding="utf-8")
        key_template = re.search(
            r'for cluster in var\.scoped_clusters :\s*\n\s*"([^"]+)"', source
        )
        self.assertIsNotNone(key_template, "the scope key template moved")

        rendered_key = (
            key_template.group(1)
            .replace("${cluster.project_id}", "kagents-dev")
            .replace("${cluster.location}", "us-east4")
            .replace("${cluster.cluster_name}", "ka-test")
        )
        self.assertEqual(
            scoped_sa_pool.scope_key("kagents-dev", "us-east4", "ka-test"),
            rendered_key,
        )

    def test_the_pool_is_disarmed_by_default(self):
        """Off until a member can actually do something.

        With no IAM grant and no RBAC yet, an armed pool selects a powerless
        identity for every request and turns every cluster read into a Forbidden.
        Fail-closed, and a full outage.

        Flip this in the same change that lands per-cluster RBAC, with a test that
        a real read succeeds through the pool. Not before.
        """
        scoped_sa_pool = _import_broker_module("scoped_sa_pool")

        self.assertFalse(
            scoped_sa_pool.pool_enabled({}),
            "the pool is armed by default while its members hold no authority",
        )
        self.assertTrue(
            scoped_sa_pool.pool_enabled({scoped_sa_pool.POOL_FLAG_ENV: "1"}),
            "the pool cannot be turned on explicitly",
        )

    def test_the_composition_provisions_no_pool_by_default(self):
        """The other half of the disarm, and the one an install actually hits.

        `pool_enabled` is the broker's default, and the operator overrides it: a
        PlatformAgent listing scoped accounts renders
        CREDENTIAL_PROXY_SCOPED_SA_POOL=1. The composition fills that list from
        `scoped_clusters`, so a default that named the cluster it provisions arms
        the pool on every stock apply -- past the broker's default entirely, and
        straight into the outage the guard test above exists to prevent.

        The defect has two halves and an earlier version of this test pinned one.
        Asserting `default = []` leaves the other open: a coalescing local
        substitutes a cluster when the variable is empty, the variable's own
        spelling never changes, and every stock apply is armed again.

        So this traces the value instead of reading the declaration. There are
        exactly two paths out of `scoped_clusters` -- into the module, which
        provisions the accounts, and into the chart values, which arm the broker
        -- and both are pinned to expressions that are empty when the variable
        is. Nothing between them is left free to substitute.
        """
        variables = (FULL_INSTALL / "variables.tf").read_text(encoding="utf-8")
        main = (FULL_INSTALL / "main.tf").read_text(encoding="utf-8")

        # 1. The variable is empty, and cannot be null -- so there is nothing
        #    for a `!= null` coalesce to catch either.
        block = re.search(
            r'variable "scoped_clusters" \{(.*?)\n\}', variables, re.DOTALL
        )
        self.assertIsNotNone(block, "the scoped_clusters variable moved or was renamed")
        default = re.search(r"^\s*default\s*=\s*(.+)$", block.group(1), re.MULTILINE)
        self.assertIsNotNone(default, "scoped_clusters declares no default")
        self.assertEqual("[]", default.group(1).strip())
        self.assertIsNotNone(
            re.search(r"^\s*nullable\s*=\s*false\s*$", block.group(1), re.MULTILINE),
            "scoped_clusters is nullable, so null is a third state this test does "
            "not cover and a coalesce can act on",
        )

        # 2. First path: nothing sits between the variable and the module that
        #    provisions the accounts.
        module_block = re.search(
            r'module "kube_agents_iam" \{(.*?)\n\}', main, re.DOTALL
        )
        self.assertIsNotNone(module_block, "the kube_agents_iam module call moved")
        argument = re.search(
            r"^\s*scoped_clusters\s*=\s*(.+?)\s*$", module_block.group(1), re.MULTILINE
        )
        self.assertIsNotNone(
            argument, "the composition no longer passes scoped_clusters to the module"
        )
        self.assertEqual(
            "var.scoped_clusters",
            argument.group(1),
            "something transforms scoped_clusters between the variable and the "
            "module. Whatever it substitutes when the variable is empty is what "
            "every stock apply provisions a pool from.",
        )

        # 3. Second path: the chart values, which are what actually arm the
        #    broker. Keyed off the module's output, so an empty variable gives an
        #    empty map gives an empty CR list -- and the rejoin table is iterated
        #    from the variable rather than from anything substituted for it.
        helm_value = re.search(
            r"scopedServiceAccounts\s*=\s*\[(.*?)\n(\s*)\]", main, re.DOTALL
        )
        self.assertIsNotNone(
            helm_value, "the chart values no longer carry scopedServiceAccounts"
        )
        self.assertIn(
            "module.kube_agents_iam.scoped_service_accounts",
            helm_value.group(1),
            "the CR's scopedServiceAccounts list is built from something other "
            "than the module's output, so it can be non-empty while the pool is "
            "not provisioned -- which arms the broker onto accounts that do not "
            "exist",
        )
        entries = re.search(
            r"^\s*scoped_pool_entries\s*=\s*\{\s*\n\s*for \w+ in (\S+)\s*:",
            main,
            re.MULTILINE,
        )
        self.assertIsNotNone(entries, "local.scoped_pool_entries moved or was renamed")
        self.assertEqual(
            "var.scoped_clusters",
            entries.group(1),
            "the rejoin table is built from something other than the variable",
        )


if __name__ == "__main__":
    unittest.main()

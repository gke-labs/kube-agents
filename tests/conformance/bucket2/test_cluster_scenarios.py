"""Bucket 2 -- the scenarios that need an API server.

Structure and scenario numbering follow `docs/e2e-test-plan-double-dryrun.md`
on the `realtime_iam` branch, which is Haoxu Wang's work. His permission matrix
is the clearest statement of the intersection model anywhere in the corpus and
the plan's shape -- goal, actors, target command, per-check expected exit code,
expected outcome, audit-log assertion -- is followed here rather than
reinvented. The mechanism differs: his design checks with a double server-side
dry-run and then acts, which has a time-of-check/time-of-use gap by
construction; the direction here is impersonation, so authorization happens
*during* the act. The semantics are his.

## What runs, and what these scenarios currently prove

Scenarios 1 to 3 assert the intersection. **There is no intersection today.**
One shared Google service account, one Kubernetes identity, every allowlisted
chat user wielding the agent's full authority. So on the current build these
three fail, and that is their purpose: they are the acceptance test the
intersection slice has to turn green, written before it, against the
requirement rather than against the implementation.

Scenarios 4 to 7 assert controls that either exist or are one config flag away,
and should pass on a cluster installed with the split broker and the egress
allowlist enabled.

Nothing here is wired into the pull-request path. Set
`KUBE_AGENTS_CONFORMANCE_CLUSTER` to a kubectl context to run it.
"""

from __future__ import annotations

import os
import subprocess
import unittest

from .. import _harness as h

CONTEXT = os.environ.get("KUBE_AGENTS_CONFORMANCE_CLUSTER", "")
NAMESPACE = os.environ.get("KUBE_AGENTS_CONFORMANCE_NAMESPACE", "kubeagents-system")
AGENT = os.environ.get("KUBE_AGENTS_CONFORMANCE_AGENT", "platformagent")

# The two identities the intersection is measured between. Both must be bound to
# the RBAC the scenario names before the suite runs; provisioning them is the
# rc pipeline's job, not this module's, because a suite that grants itself the
# permissions it then tests is measuring its own setup.
SRE_USER = os.environ.get("KUBE_AGENTS_CONFORMANCE_SRE", "alice@example.com")
SRE_GROUP = os.environ.get("KUBE_AGENTS_CONFORMANCE_SRE_GROUP", "sre-team@example.com")
DEV_USER = os.environ.get("KUBE_AGENTS_CONFORMANCE_DEV", "bob@example.com")
DEV_GROUP = os.environ.get("KUBE_AGENTS_CONFORMANCE_DEV_GROUP", "dev-team@example.com")

TARGET_NAMESPACE = os.environ.get("KUBE_AGENTS_CONFORMANCE_TARGET_NS", "demo-prod")
TARGET_DEPLOYMENT = "nginx-app"


def kubectl(*arguments: str) -> subprocess.CompletedProcess:
    """Run kubectl against the configured context and return the result unchecked.

    Unchecked on purpose: a non-zero exit is the assertion in most of these
    scenarios, so raising on it would throw away the thing being measured.
    """
    return subprocess.run(
        ["kubectl", "--context", CONTEXT, *arguments],
        capture_output=True,
        text=True,
        timeout=120,
    )


def agent_exec(*argv: str) -> subprocess.CompletedProcess:
    """Run a command the way the agent does -- through the credential broker.

    Reaching the broker rather than the API server directly is the whole point.
    A scenario that runs `kubectl` from the test host measures the test host's
    RBAC, which is the mistake that would make every one of these pass.
    """
    return kubectl(
        "-n", NAMESPACE, "exec", f"deployment/{AGENT}-gateway",
        "-c", "platform-agent", "--",
        "/opt/credential-proxy/bin/kubectl", *argv,
    )


@h.requires_cluster
class Scenario1AuthorizedUserAndAuthorizedAgent(unittest.TestCase):
    """A2, positive. Both halves of the intersection permit, so the work happens.

    Goal:      a permitted request is not blocked by the mechanism that blocks
               the others. A control that refuses everything passes scenarios 2
               and 3 and is worthless.
    User:      alice, in the SRE group, full CRUD in the target namespace.
    Agent SA:  the operator ceiling, sufficient for this verb.
    Command:   kubectl delete deployment nginx-app -n demo-prod
    Expected:  exit 0, the Deployment is gone, and the GKE audit record names
               the agent service account as the principal with alice recorded
               as the impersonated user.
    """

    def test_A2_a_permitted_request_succeeds_through_the_agent(self) -> None:
        self.skipTest(
            "no intersection mechanism exists: the agent holds one shared "
            "identity, so this scenario cannot distinguish alice from bob. "
            "Unskip with the intersection slice."
        )


@h.requires_cluster
class Scenario2UnauthorizedUser(unittest.TestCase):
    """A2, negative. User attenuation -- the agent is not a way around your RBAC.

    Goal:      an agent with more authority than the requester does not lend it.
    User:      bob, in the dev group, read-only on pods in demo-dev, no delete.
    Agent SA:  sufficient for the verb.
    Command:   kubectl delete deployment nginx-app -n demo-prod
    Expected:  refused on the user's authority. The Deployment still exists.
               No delete appears in the audit log at all -- not a failed one.

    This is A1 and A2 in the same assertion: the outcome bob gets through the
    agent is the outcome bob gets directly, for the same reason. And the
    refusal must not name the namespace or the object, per A1's bound on
    denial content, because bob cannot see either.
    """

    def test_A2_a_user_cannot_borrow_the_agent_authority(self) -> None:
        self.skipTest(
            "no intersection mechanism exists: bob's request is executed under "
            "the agent's identity and succeeds. Unskip with the intersection slice."
        )


@h.requires_cluster
class Scenario3RestrictedAgentCeiling(unittest.TestCase):
    """A2, negative. The agent ceiling binds even a cluster-admin requester.

    Goal:      the intersection is an intersection, not a union.
    User:      alice, cluster-admin.
    Agent SA:  read-only.
    Command:   kubectl delete deployment nginx-app -n demo-prod
    Expected:  refused on the agent's ceiling, distinguishably from scenario 2 --
               the two refusals must not be the same message, or an operator
               debugging one cannot tell which half denied.

    The ceiling half of this *is* enforced today, by the read-only command
    policy and the minted RBAC, and is asserted without a cluster in
    test_C_enforcement.py. What needs a cluster is that the API server agrees:
    a policy-layer refusal and an RBAC refusal are different controls and only
    one of them survives a bypass of the other.
    """

    def test_A2_a_cluster_admin_cannot_raise_the_agent_ceiling(self) -> None:
        result = agent_exec("delete", "deployment", TARGET_DEPLOYMENT, "-n", TARGET_NAMESPACE)
        self.assertNotEqual(0, result.returncode, "the agent performed a delete")
        self.assertRegex(
            result.stdout + result.stderr,
            r"(?i)read-only|forbidden|SECURITY_POLICY_BLOCKED",
        )
        survives = kubectl(
            "-n", TARGET_NAMESPACE, "get", "deployment", TARGET_DEPLOYMENT
        )
        self.assertEqual(0, survives.returncode, "the Deployment was deleted anyway")


@h.requires_cluster
class Scenario4NoVerifiedIdentity(unittest.TestCase):
    """A3 and C2. Fail closed when the session carries no verified principal.

    Goal:      an unattributable request is refused before anything runs, not
               executed under a default.
    User:      none -- no verified identity in the session context.
    Command:   any mutating kubectl.
    Expected:  refused before the first API call. Nothing in the audit log.

    D1 depends on this: "every action traces to a human or a named scheduled
    job" is unenforceable if a request with no principal is executed under the
    agent's own.
    """

    def test_A3_a_request_with_no_principal_is_refused(self) -> None:
        self.skipTest(
            "the broker authenticates its caller only in the split layout, and "
            "the principal does not yet reach the authorization decision. "
            "Unskip with the intersection slice."
        )


@h.requires_cluster
class Scenario5MetadataServerUnreachable(unittest.TestCase):
    """C1. The credential-free sandbox cannot mint a token behind the broker's back.

    Goal:      the policy layer is not bypassable by anything that can make an
               HTTP request.
    Command:   from inside the agent container, curl each metadata address.
    Expected:  every one unreachable.

    Requires `splitCredentialBrokerPod: true` and `egressPolicy: Allowlist`.
    Both are off in the default install, and that is stated in the CRD field
    description, the reconciler's log line and three documents. This scenario
    is the reason the honest version of the exit criterion matters: on a
    default install it fails, and it should.

    Note also what a passing result does *not* prove. The operator cannot
    detect whether the CNI enforces NetworkPolicy at all, nor whether a
    competing additive policy re-opens the address. A green here is a green for
    this cluster's configuration.
    """

    ADDRESSES = ("169.254.169.254", "169.254.169.252", "[fd20:ce::254]")

    def test_C1_the_metadata_server_is_unreachable_from_the_sandbox(self) -> None:
        for address in self.ADDRESSES:
            with self.subTest(address=address):
                result = kubectl(
                    "-n", NAMESPACE, "exec", f"deployment/{AGENT}-gateway",
                    "-c", "platform-agent", "--",
                    "python3", "-c",
                    "import socket,sys;"
                    "s=socket.socket();s.settimeout(3);"
                    f"sys.exit(0 if s.connect_ex(({address.strip('[]')!r},80))!=0 else 1)",
                )
                self.assertEqual(
                    0,
                    result.returncode,
                    f"the sandbox reached {address}; the credential-free "
                    f"container can mint the service account token directly",
                )


@h.requires_cluster
class Scenario6TheAdmissionPolicyRejects(unittest.TestCase):
    """C1 and C5. The rewritten exit criterion: rejection, not presence.

    Goal:      the ValidatingAdmissionPolicy is *enforcing*, not merely applied.
    Command:   apply a ClusterRole carrying the agent tier label and a write verb.
    Expected:  rejected by the API server, citing the policy.

    Slice 2b found that installing through the obvious kustomize path would
    have left the policy silently inert -- `namePrefix` rewrites
    `metadata.name` and not `ValidatingAdmissionPolicyBinding.spec.policyName`,
    so both policies exist, both bindings point at nothing, and
    `kubectl get validatingadmissionpolicy` looks right. Applied and enforcing
    are different states and the gap is silent, which is why this asserts the
    refusal. The static half -- every binding names a policy that exists -- is
    asserted without a cluster in test_C_enforcement.py.

    Same shape as the `branches/main "protected": true` trap. Twice now an
    object's existence has been mistaken for its enforcement.
    """

    VIOLATING_ROLE = """
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kube-agents-conformance-probe
  labels:
    kube-agents/tier: platform
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list", "delete"]
"""

    def tearDown(self) -> None:
        kubectl("delete", "clusterrole", "kube-agents-conformance-probe", "--ignore-not-found")

    def test_C1_a_violating_request_is_rejected_by_the_api_server(self) -> None:
        result = subprocess.run(
            ["kubectl", "--context", CONTEXT, "apply", "--dry-run=server", "-f", "-"],
            input=self.VIOLATING_ROLE,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(
            0,
            result.returncode,
            "a ClusterRole granting delete to an agent tier was admitted; the "
            "policy exists and is not enforcing",
        )
        self.assertRegex(result.stderr, r"kube-agents-agent-readonly|denied the request")


@h.requires_cluster
class Scenario7AuditAttribution(unittest.TestCase):
    """D1. Every action traces to a principal, in a record the agent cannot reach.

    Goal:      the two-principal audit trail is real, not asserted.
    Command:   any read through the agent.
    Expected:  a Cloud Logging record whose `principalEmail` is the agent
               service account and whose `impersonatedUser` names the human.

    Bucket 2 rather than bucket 1 because it needs GKE's Cloud Logging, and it
    is listed among the four empirical checks the requirements document wants
    run before the architecture review: how `impersonatedUser` is represented
    is undocumented, and the two-principal trail is a headline claim resting on
    it. Ten minutes with a real cluster settles it.
    """

    def test_D1_a_read_through_the_agent_names_both_principals(self) -> None:
        self.skipTest(
            "impersonation is not deployed, so there is no second principal to "
            "record. The field mapping this asserts is also unverified -- run "
            "the empirical check first and write the assertion against what "
            "Cloud Logging actually emits, not against what it should."
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

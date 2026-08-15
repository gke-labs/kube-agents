"""Group D -- Accountability and scope.

D1  Attributable, append-only, and scoped on read.
D2  Autonomy is a dial, defaults off, earned per domain.
D3  Data carries the meet of its inputs.
D4  Credentials are short-lived, audience-bound, and non-exportable.
D5  Availability and cost are security properties.
D6  Kill switch.

Three of these six are bucket 3 in their entirety -- D3, D5 and D6 have no
mechanism to test. Each says so in a class docstring rather than being left
off the map, because the honest list of what cannot yet be tested is the part
of this suite that a reviewer can check for bad faith.
"""

from __future__ import annotations

import re
import unittest

from . import _harness as h
from ._harness import command_policy

MARKER = "conformance-marker-8f3a"


class D1Attributable(unittest.TestCase):
    """D1: every action traces to a human or a named scheduled job."""

    def test_D1_the_exec_route_records_a_principal(self) -> None:
        """The record has to name who, not only what.

        Today every refusal is a judgement about *what* was asked; the
        per-caller model is a later slice. What must not regress in the
        meantime is that the principal reaches the log line at all -- an
        unattributed action is D1's first forbid, and the field is easy to drop
        while refactoring a handler that does not yet read it.
        """
        source = h.text("credential_proxy")
        # Anchored inside CredentialProxyHandler: AgentAPIProxyHandler defines a
        # do_POST too, and it is two lines long. Reaching that one instead is
        # how this assertion would pass while checking nothing.
        handler = source.index("class CredentialProxyHandler")
        start = source.index("def do_POST", handler)
        body = source[start : source.index("\n    def ", start + 1)]
        self.assertGreater(len(body), 1000, "the wrong do_POST was located")
        self.assertIn("principal", body)
        self.assertIn("_sanitize_for_logging(principal.describe())", body)

    def test_D1_a_log_hint_cannot_forge_a_record(self) -> None:
        """Untrusted bytes reach the log, so the log has one sanitiser in front of it.

        The refusal path logs a hint derived from argv. Argv is agent-supplied,
        the log is the audit trail, and a text-mode consumer splits on
        anything in Cc, Cf, Zl or Zp. This asserts the sanitiser is applied on
        that path rather than merely defined.
        """
        source = h.text("credential_proxy")
        start = source.index("refusal_result = read_only_refusal(argv)")
        window = source[start : start + 500]
        self.assertIn("_sanitize_for_logging(log_hint)", window)

    def test_D1_precondition_the_refusal_still_returns_a_log_hint(self) -> None:
        decision = command_policy.evaluate(["gcloud", "projects", "delete", MARKER])
        self.assertFalse(decision.allowed)
        self.assertIsNotNone(decision.verb_tuple)

    @h.known_violation("D1", "overnight-b/findings.md 2.1")
    def test_D1_the_refusal_log_hint_names_no_caller_supplied_value(self) -> None:
        """KNOWN VIOLATION. A short gcloud path puts a positional in the log.

        The refusal caps the logged command path at three words and the code
        comment says this is "to exclude positionals". That holds for a
        three-word path -- `container clusters delete prod` logs
        `container.clusters.delete` -- and not for a two-word one:
        `gcloud projects delete acme-prod` logs
        `projects.delete.acme-prod`, and the third word is the caller's.

        Low severity. The value is sanitised and length-capped before it is
        written, so it cannot forge a record, and it never reaches the caller's
        refusal body -- A1's bound is intact. It is filed because the comment
        asserts a property the code does not have, which is how the next person
        widens the cap without checking.

        The fix is to cap at the length of the matched command path rather than
        at a constant, or to log only the words that matched an allowlist
        entry.
        """
        leaks = []
        for argv in (
            ["gcloud", "projects", "delete", MARKER],
            ["gcloud", "config", "set", MARKER],
            ["gcloud", "auth", "revoke", MARKER],
        ):
            decision = command_policy.evaluate(argv)
            hint = ".".join(decision.verb_tuple or ())
            if MARKER in hint:
                leaks.append(f"{argv} -> {hint}")
        self.assertEqual([], leaks)


class D2AutonomyIsADial(unittest.TestCase):
    """D2: defaults off, earned per domain, never a global setting."""

    def test_D2_no_direct_apply_mode_exists(self) -> None:
        """`workflowMode: Direct` is the shape B1 forbids as a config option.

        It is offered by a design document elsewhere in the corpus and has
        never existed in this API. Asserting its absence is not a hypothetical:
        a break-glass switch is the single most requested feature in this class
        and the one that voids B1 outright, so the day someone adds it should
        be a red test rather than a merged pull request.
        """
        api_types = sorted((h.REPO_ROOT / "k8s-operator" / "api").rglob("*.go"))
        self.assertTrue(api_types, "the operator API package is gone")
        for path in api_types:
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertNotRegex(text, r"WorkflowMode|workflowMode")
                self.assertNotRegex(text, r"BreakGlass|breakGlass")
                self.assertNotRegex(text, r"AllowDirectApply|allowDirectApply")

    def test_D2_the_read_only_posture_is_not_a_customer_facing_knob(self) -> None:
        """The one existing global switch, kept out of the documented surface.

        `CREDENTIAL_PROXY_ENFORCE_READ_ONLY` is global, unscoped and has no
        expiry -- setting it disables the posture for every command, every
        agent and every cluster in the Pod, for as long as the ConfigMap says
        so. It exists so an operator can recover from a bad allowlist without
        an image build. D2 forbids a global autonomy setting, so the mitigation
        is that it is not offered: it is absent from the CRD, absent from the
        chart values, and absent from the customer-facing reference.
        """
        name = "CREDENTIAL_PROXY_ENFORCE_READ_ONLY"
        surfaces = [
            h.REPO_ROOT / "charts/kube-agents/values.yaml",
            h.REPO_ROOT / "k8s-operator/api/v1alpha1/common_types.go",
            h.REPO_ROOT / "k8s-operator/api/v1alpha1/platformagent_types.go",
        ]
        for path in surfaces:
            with self.subTest(surface=path.name):
                self.assertTrue(path.is_file(), f"{path} has moved")
                self.assertNotIn(name, path.read_text())

        docs = h.REPO_ROOT / "docs/site/src/content/docs"
        if docs.is_dir():
            offenders = [
                str(page.relative_to(h.REPO_ROOT))
                for page in docs.rglob("*.md")
                if name in page.read_text()
            ]
            self.assertEqual(
                [], offenders, "the escape hatch is documented as a supported knob"
            )


class D4CredentialsAreShortLivedAndBound(unittest.TestCase):
    """D4: audience-bound, independently revocable, no shared or fixed secrets."""

    @staticmethod
    def _projected_tokens():
        """(fixture, deployment, volume name, serviceAccountToken) for every projection."""
        for name, documents in h.golden_documents().items():
            for deployment in h.objects_of_kind(documents, "Deployment"):
                pod_spec = deployment["spec"]["template"]["spec"]
                for volume in pod_spec.get("volumes") or []:
                    for projected in (volume.get("projected") or {}).get("sources") or []:
                        token = projected.get("serviceAccountToken")
                        if token:
                            yield name, deployment["metadata"]["name"], volume["name"], token

    def test_D4_every_projected_token_expires(self) -> None:
        """No credential outlives the session that caused it -- the bounded half.

        Expiry is the property both projections get right, so it is asserted on
        all of them. The audience is the property one of them gets wrong, and
        that is the expected failure below.
        """
        checked = 0
        for fixture, deployment, volume, token in self._projected_tokens():
            checked += 1
            with self.subTest(fixture=fixture, deployment=deployment, volume=volume):
                self.assertIsNotNone(token.get("expirationSeconds"))
                self.assertLessEqual(token["expirationSeconds"], 3600)
        self.assertGreater(checked, 0, "no projected service account token in any fixture")

    def test_D4_the_broker_token_is_audience_bound(self) -> None:
        """The projection that gets it right, pinned so it stays that way.

        Audience binding is what stops the broker's token being replayed
        against the API server by anything that reads it. The broker verifies
        its caller with a TokenReview against this audience rather than
        comparing a secret, which is the delegate-don't-parse answer to the
        parser-differential class -- and it only works while the audience is
        set.
        """
        broker_tokens = [
            (fixture, volume, token)
            for fixture, _, volume, token in self._projected_tokens()
            if "credential-proxy" in volume
        ]
        self.assertTrue(broker_tokens, "no broker token projection in any fixture")
        for fixture, volume, token in broker_tokens:
            with self.subTest(fixture=fixture, volume=volume):
                self.assertEqual("kubeagents-credential-proxy", token.get("audience"))

    def test_D4_precondition_the_event_watcher_token_is_still_projected(self) -> None:
        volumes = {volume for _, _, volume, _ in self._projected_tokens()}
        self.assertIn("event-watcher-ksa-token", volumes)

    @h.known_violation("D4", "slice-2b/findings.md 1.10")
    def test_D4_every_projected_token_is_audience_bound(self) -> None:
        """KNOWN VIOLATION. `event-watcher-ksa-token` has the default audience.

        A projected token with no audience is accepted by the API server for
        any purpose, which is the opposite of D4's "audience-bound". It is
        mounted into the broker Pod as well as the agent Pod, so the container
        holding the cloud credentials carries a general-purpose Kubernetes
        bearer token it has no use for.

        Marginal in impact -- TokenReview needs a real API credential and that
        container already holds strictly more powerful GCP credentials -- and
        already on file as such. It is here because "marginal today" is a
        statement about the current mount layout, and the invariant is not.
        The fix is its own projection with its own audience rather than reusing
        that bundle.
        """
        unbound = [
            f"{fixture}/{deployment}/{volume}"
            for fixture, deployment, volume, token in self._projected_tokens()
            if not token.get("audience")
        ]
        self.assertEqual([], unbound)

    def test_D4_the_customer_api_key_is_secret_backed(self) -> None:
        """The key that guards the agent's chat API comes from a Secret, not a literal.

        This is the counterexample that makes the expected failure below a
        divergence rather than a house style: the same codebase gets the
        externally-reachable key right.
        """
        checked = 0
        for name, documents in h.golden_documents().items():
            for deployment in h.objects_of_kind(documents, "Deployment"):
                for container in h.containers_of(deployment):
                    for variable in container.get("env") or []:
                        if variable["name"] != "API_SERVER_EXTERNAL_KEY":
                            continue
                        checked += 1
                        with self.subTest(fixture=name, container=container["name"]):
                            self.assertIn("valueFrom", variable)
                            self.assertIn("secretKeyRef", variable["valueFrom"])
                            self.assertNotIn("value", variable)
        self.assertGreater(checked, 0, "no API_SERVER_EXTERNAL_KEY in any fixture")

    def test_D4_precondition_the_loopback_key_is_still_delivered_as_env(self) -> None:
        names = set()
        for documents in h.golden_documents().values():
            for deployment in h.objects_of_kind(documents, "Deployment"):
                for container in h.containers_of(deployment):
                    names.update(v["name"] for v in container.get("env") or [])
        self.assertIn("API_SERVER_KEY", names)

    @h.known_violation("D4", "04_major_requirements.md D4")
    def test_D4_no_fixed_shared_secret_ships_in_a_manifest(self) -> None:
        """KNOWN VIOLATION. `cluster-internal-trusted` is a literal in the rendered Pod.

        Four env vars across the agent and broker carry the same hardcoded,
        non-secret string as a bearer token, and the Python default is the same
        literal -- so an unset variable still yields it. The operator's own
        comment calls it a "loopback sentinel", and the design argument is that
        8642 binds 127.0.0.1 so the value never crosses a network boundary.

        That argument is exactly as strong as the loopback binding, which is
        deployment geometry. D4 forbids shared or fixed secrets without an
        exception for ones that are currently unreachable, and D1 lists fixed
        shared secrets under unattributable actions: every caller presenting
        this value is indistinguishable from every other.
        """
        offenders = []
        for name, documents in h.golden_documents().items():
            for deployment in h.objects_of_kind(documents, "Deployment"):
                for container in h.containers_of(deployment):
                    for variable in container.get("env") or []:
                        value = variable.get("value")
                        if isinstance(value, str) and "cluster-internal-trusted" in value:
                            offenders.append(
                                f"{name}/{container['name']}/{variable['name']}"
                            )
        self.assertEqual([], offenders)


class D3DataCarriesTheMeetOfItsInputs(unittest.TestCase):
    """D3: BUCKET 3 -- no mechanism exists, and a weak test would be worse than none.

    The invariant requires every datum to carry a label equal to the meet of
    the labels of everything it was derived from, readable only in a context
    whose label dominates it, with declassification as a named, recorded act.

    Nothing in this codebase carries a label. There is no lattice, no
    declassifier, no owner for the fleet-aggregate decision, and D3 itself is
    listed as needing to be settled before the component evaluation rather than
    after. The nearest thing to a mechanism is the memory partitioning in
    `multiuser_memory.py`, which is per-user isolation -- a partition, not a
    lattice -- and is exactly the confusion the invariant was rewritten to
    remove: two namespaces in one cluster share a scope and may be different
    tenants.

    Pinning today's behaviour here would encode a gap as an intention. The git
    lease is the cautionary example: a concurrency control that its own
    docstring says is not an ownership check, held in place by a test.

    What would make this bucket 1: a label type, an egress channel that reads
    it, and a named declassifier. Then the assertion is that the fleet audit
    cannot emit a cross-tenant aggregate without one.
    """

    def test_D3_is_recorded_as_bucket_three_rather_than_missing(self) -> None:
        """A placeholder that fails if the reason above is deleted.

        Bucket 3 is a written reason, not an absence. This makes deleting the
        reason a test failure so the invariant cannot quietly drop off the map
        between now and the day someone builds the mechanism.
        """
        self.assertIn("BUCKET 3", D3DataCarriesTheMeetOfItsInputs.__doc__ or "")
        self.assertIn("What would make this bucket 1", D3DataCarriesTheMeetOfItsInputs.__doc__)


class D5AvailabilityAndCostAreSecurityProperties(unittest.TestCase):
    """D5: BUCKET 3 -- there are no budgets, so there is nothing to exhaust safely.

    The invariant requires per-tenant and per-principal budgets on tokens,
    sessions, tool invocations and triggered work, with exhaustion degrading to
    refusal rather than to a weaker model, a weaker check, a longer cache or a
    permissive fallback.

    No budget of any kind is enforced in this repository. Without one, the
    "degrades to refusal" clause has no subject: there is no exhaustion event
    to assert the behaviour of. Writing a test that model routing exists and is
    configured would assert a fact about LiteLLM, not the invariant.

    One adjacent property *is* asserted, in test_C_enforcement.py: the read-only
    decision is a pure function of argv, so no model, tier or budget state can
    reach it. That is the "fallback may not lower the enforcement tier" clause
    holding by construction rather than by policy -- which is the strongest
    form of it, and the only part of D5 that is true today.

    What would make this bucket 1: a budget with a named owner and a refusal
    path. Then the assertion is that crossing it produces a refusal and not a
    downgrade.
    """

    def test_D5_the_enforcement_tier_cannot_be_lowered_by_routing(self) -> None:
        """The one clause of D5 that holds today, asserted where it is decided.

        This is a cross-reference rather than a duplicate: if the purity test
        is deleted, D5 loses its only assertion and this goes red.
        """
        from . import test_C_enforcement

        self.assertTrue(
            hasattr(
                test_C_enforcement.C3UntrustedByDefault,
                "test_C3_the_policy_decision_reads_nothing_but_its_argv",
            )
        )


class D6KillSwitch(unittest.TestCase):
    """D6: BUCKET 3 -- no halt path exists, and the nearest thing is not one.

    The invariant requires a named human role to halt all agent sessions --
    globally, per tenant, per agent, per trigger -- within N seconds, without
    the cooperation of any agent, through a control path sharing no dependency
    with the agent plane, failing closed and surviving restart.

    Three reasons this cannot be tested yet, and they are different kinds of
    missing:

    1. There is no halt mechanism. `CREDENTIAL_PROXY_ENFORCE_READ_ONLY` is the
       only global switch and it points the wrong way -- it *disables*
       enforcement. Nothing stops a session.
    2. N is unset. The requirements document lists it as open, alongside the
       two other unstated Ns in A2 and C2, all three needing owners.
    3. The nearest candidate fails the independence clause by construction: an
       operator cannot flip that variable without editing the Deployment, which
       is the agent plane's own control path.

    It is also the only mechanism that would act faster than GKE's roughly
    one-hour credential cache, which is what makes it the real answer to "cut
    this person off now" rather than a nice-to-have.

    What would make this bucket 1: a halt control with a stated N. Then the
    assertion is that a halted agent refuses, that the halt survives a restart,
    and that setting it does not require touching the agent's own Deployment.
    """

    def test_D6_the_read_only_switch_is_not_mistaken_for_a_kill_switch(self) -> None:
        """Guards against the wrong thing being ticked off against this invariant.

        The switch disables the read-only gate. Reading it as a kill switch --
        which its position in the corpus invites -- would let D6 be marked
        satisfied by a control that cannot stop a session and points the
        opposite way. This asserts what it actually does, so the claim cannot
        be made by anyone reading only the test names.
        """
        import os
        from unittest import mock

        with mock.patch.dict(
            os.environ, {"CREDENTIAL_PROXY_ENFORCE_READ_ONLY": "false"}
        ):
            self.assertFalse(
                h.credential_proxy.read_only_enforced(),
                "the switch removes enforcement rather than halting work; it is "
                "not a kill switch and must not be recorded as one",
            )

    def test_D6_is_recorded_as_bucket_three_rather_than_missing(self) -> None:
        self.assertIn("BUCKET 3", D6KillSwitch.__doc__ or "")
        self.assertIn("What would make this bucket 1", D6KillSwitch.__doc__)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

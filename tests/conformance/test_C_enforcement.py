"""Group C -- Enforcement.

C1  Isolation is structural, not behavioral.
C2  Fail closed.
C3  Untrusted by default. Trust is an allowlist.
C4  Anything executable or instruction-bearing has verified provenance.
C5  Privileged controllers are bounded.

The general rule these follow, stated once in `04_major_requirements.md` after
an object's existence was mistaken for its enforcement twice: **a control's
test asserts the refusal, never the presence of the control.** Where a bucket-1
test can only reach the object -- because asserting the refusal needs an API
server -- the refusal assertion is written in bucket2/ and cross-referenced,
rather than the object assertion being allowed to stand in for it.
"""

from __future__ import annotations

import ast
import re
import sys
import tempfile
import unittest

from . import _harness as h
from ._harness import command_policy


class C1IsolationIsStructural(unittest.TestCase):
    """C1: no security property rests on the model choosing not to."""

    def test_C1_the_process_namespace_is_never_shared(self) -> None:
        """A shared PID namespace at matched UIDs reaches the broker through procfs.

        `/proc/<pid>/environ` of the credential holder, and the backend socket
        it binds, are both reachable from a process that shares the namespace.
        That path bypasses the command policy entirely, so the field has to be
        unsettable rather than merely unset -- asserted on every rendered spec
        shape, not just the default one.
        """
        for name, documents in h.golden_documents().items():
            for deployment in h.objects_of_kind(documents, "Deployment"):
                pod_spec = deployment["spec"]["template"]["spec"]
                with self.subTest(fixture=name, deployment=deployment["metadata"]["name"]):
                    self.assertNotIn("shareProcessNamespace", pod_spec)

        source = h.text("manifests_go")
        assignments = re.findall(r"ShareProcessNamespace\s*:", source)
        self.assertEqual(
            [],
            assignments,
            "platformagent_manifests.go assigns ShareProcessNamespace; the only "
            "permitted mention is the comment explaining why it is absent",
        )

    # The container the model's tools run in, and the containers that hold a
    # credential. Named rather than pattern-matched: a rename then turns the
    # "at least one of each was found" assertion below red, where a pattern
    # would quietly match nothing and pass.
    SANDBOX_CONTAINER = "platform-agent"
    CREDENTIAL_CONTAINERS = frozenset(
        {"envoy-credential-proxy", "agent-api-proxy", "credential-broker"}
    )

    def test_C1_the_agent_and_the_broker_run_as_different_users(self) -> None:
        """Matched UIDs make the ptrace and procfs boundary a formality.

        Same-UID processes in one Pod can read each other's `/proc/<pid>/environ`
        and reach each other's Unix sockets, so the credential holder running at
        the sandbox's UID puts the cloud credentials one `cat` away from the
        model's tools. The assertion is the *difference*, not the literal 10000
        and 10001, so renumbering the users is allowed and collapsing them is
        not.

        Applied per Pod rather than per Deployment: in the split layout the
        broker has a Pod of its own, where running as the broker user at the Pod
        default is correct. What must never happen is a credential container
        sharing a UID with a sandbox container *in the same Pod*.
        """
        seen_sandbox = seen_credential = False
        for name, documents in h.golden_documents().items():
            for deployment in h.objects_of_kind(documents, "Deployment"):
                pod_spec = deployment["spec"]["template"]["spec"]
                default_user = (pod_spec.get("securityContext") or {}).get("runAsUser")
                effective = {
                    container["name"]: (
                        container.get("securityContext") or {}
                    ).get("runAsUser", default_user)
                    for container in h.containers_of(deployment)
                }
                if self.SANDBOX_CONTAINER not in effective:
                    continue
                seen_sandbox = True
                sandbox_user = effective[self.SANDBOX_CONTAINER]
                subject = f"{name}/{deployment['metadata']['name']}"
                with self.subTest(pod=subject):
                    self.assertIsNotNone(
                        sandbox_user,
                        "the sandbox container has no effective runAsUser, so "
                        "the image's USER decides and the split is not enforced",
                    )
                for container_name in self.CREDENTIAL_CONTAINERS & effective.keys():
                    seen_credential = True
                    with self.subTest(pod=subject, container=container_name):
                        self.assertNotEqual(
                            sandbox_user,
                            effective[container_name],
                            f"{container_name} shares the sandbox UID",
                        )

        self.assertTrue(seen_sandbox, "no fixture renders the sandbox container")
        self.assertTrue(
            seen_credential,
            "no fixture renders a credential container under any of the names "
            "this test knows; the assertion above matched nothing",
        )

    def test_C1_the_split_broker_pod_holds_no_sandbox_container(self) -> None:
        """What the split is for: separate network namespaces, separate Pods.

        The broker Pod is only a boundary if the model's tools are not in it.
        """
        documents = h.yaml_documents("golden_split_broker")
        broker_pods = [
            d
            for d in h.objects_of_kind(documents, "Deployment")
            if any(
                c["name"] in self.CREDENTIAL_CONTAINERS - {"agent-api-proxy"}
                for c in h.containers_of(d)
            )
            and not any(c["name"] == self.SANDBOX_CONTAINER for c in h.containers_of(d))
        ]
        self.assertTrue(
            broker_pods,
            "the split-broker fixture renders no Pod holding the broker alone",
        )

    def test_C1_the_broker_backend_socket_is_bound_private(self) -> None:
        """Slice 2b: the umask that made the split work nearly opened the socket.

        `umask 0002` was added so the two containers could write each other's
        files on the shared PVC. That umask also applies to the Unix socket the
        broker binds, taking it from 0600 to 0775 -- group-writable, and the
        group is now shared with the agent. Nothing behind that socket
        authenticates its callers, so reaching it is reaching the credentials.

        The fix binds *under* an explicit umask rather than chmod-ing
        afterwards, so there is no window in which the bound socket is more
        permissive. This asserts the ordering, not just the mode.
        """
        source = h.text("credential_proxy")
        self.assertIn("os.umask(0o177)", source)

        umask_at = source.index("os.umask(0o177)")
        bind_at = source.find("UnixStreamServer", umask_at)
        if bind_at == -1:
            bind_at = source.find("ThreadingUnixHTTPServer", umask_at)
        self.assertNotEqual(
            -1,
            bind_at,
            "the socket is no longer bound after the umask is set; a chmod "
            "after bind leaves a window at the permissive mode",
        )
        restore_at = source.find("os.umask(previous_umask)", umask_at)
        self.assertNotEqual(-1, restore_at, "the umask is never restored")
        self.assertLess(
            bind_at,
            restore_at,
            "the umask is restored before the socket is bound, so the bind does "
            "not happen under it",
        )

    def test_C1_the_executor_never_reaches_a_shell(self) -> None:
        """What makes `;`, `#` and `&&` inert in an agent-supplied command.

        The `realtime_iam` design has two bypasses that only exist because its
        pre-flight check builds a string and runs it with `shell=True`: a
        compound command whose first verb is a read, and a `#` that neutralises
        appended flags. Neither reaches this broker, because it takes a list
        and never interposes a shell -- so this asserts the property those two
        attacks are the absence of.
        """
        tree = ast.parse(h.text("credential_proxy"))
        offences = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = ast.unparse(node.func)
            if not target.startswith("subprocess.") and target not in ("os.system", "os.popen"):
                continue
            if target in ("os.system", "os.popen"):
                offences.append(f"{target} at line {node.lineno}")
                continue
            for keyword in node.keywords:
                if keyword.arg == "shell" and not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                ):
                    offences.append(f"{target}(shell=...) at line {node.lineno}")
        self.assertEqual([], offences)

    def test_C1_the_executor_refuses_an_executable_it_does_not_ship(self) -> None:
        """The allowlist is the reason a compound command has nowhere to land.

        `sh`, `bash` and `env` are the three that turn "argv is a list" back
        into "argv is a shell string", so they are named rather than left to a
        generic assertion about the set's contents.
        """
        executor = _executor()
        for executable in ("sh", "bash", "env", "python3", "xargs", "/bin/sh"):
            with self.subTest(executable=executable):
                with self.assertRaises(ValueError):
                    executor.execute([executable, "-c", "id"])

    def test_C1_precondition_the_broker_still_executes_git(self) -> None:
        """Guards the expected-failure below against passing by relocation."""
        self.assertIn('"git"', h.text("credential_proxy"))
        self.assertIn("GIT_MUTATING_SUBCOMMANDS", h.text("credential_proxy"))

    def test_C1_git_in_the_broker_cannot_execute_arbitrary_code(self) -> None:
        """CLOSED on this tree. Was a known violation until the git hardening landed.

        `git -c protocol.ext.allow=always clone "ext::sh -c <cmd>" <dir>` runs
        `<cmd>` inside the container that holds the cloud credentials, reachable
        by a prompt-injected agent with no process compromise. `clone` is
        deliberately absent from GIT_MUTATING_SUBCOMMANDS so it needs no lease,
        `-c` is parsed only far enough to find the subcommand and its value is
        never rejected, and command_policy puts git out of scope on purpose.

        This is C1 rather than B1: B1's test ("the agent cannot merge, approve,
        force-push") does not detect it, and egress denial does not help,
        because the execution happens on the privileged side. It dissolves the
        boundary every other control leans on.

        The fix is a set of pins in `CommandExecutor.environment`, which is
        what this asserts. The git-hardening slice closed it, and the
        known_violation decorator came off when this began passing -- the
        unexpected success is how the suite announced the gap had shut, which
        is the whole of what the register is for.

        What this deliberately does NOT assert is a particular
        `GIT_CONFIG_GLOBAL`. It used to demand `/dev/null`, and that was a
        mistake of shape rather than of substance: it pinned one imagined fix
        instead of the property. The branch that actually closes this points
        the variable at a broker-owned `.gitconfig`, because `gh auth
        setup-git` writes the GitHub credential helper into that file and
        /dev/null severs authenticated push and fetch without hardening
        anything. Had the assertion stayed, the gap would have closed while
        this test went on failing on the wrong clause -- a permanent expected
        failure that never flips, which is precisely the signal this suite
        exists to give. Asserting the controls, not the spelling.

        `core.hooksPath` is read out of the GIT_CONFIG_COUNT layer rather than
        a config file because that layer outranks every file, including a
        `.git/config` the agent can write.
        """
        environment = _executor().environment
        self.assertEqual("1", environment.get("GIT_CONFIG_NOSYSTEM"))
        self.assertEqual("https", environment.get("GIT_ALLOW_PROTOCOL"))

        forced = {
            environment.get(f"GIT_CONFIG_KEY_{index}"): environment.get(
                f"GIT_CONFIG_VALUE_{index}"
            )
            for index in range(int(environment.get("GIT_CONFIG_COUNT") or 0))
        }
        self.assertIn(
            "core.hooksPath",
            forced,
            "a hook is a command git runs from a repository the agent writes",
        )
        self.assertTrue(
            (environment.get("GIT_CONFIG_GLOBAL") or "").strip(),
            "the global config layer must be pinned somewhere the agent cannot "
            "write; which path is the hardening slice's call, but unset means "
            "git falls back to $HOME/.gitconfig",
        )

    @h.known_violation("C1", "slice-2b/findings.md 1.4 (see gke-labs/kube-agents#676)")
    def test_C1_the_rendered_egress_policy_is_default_deny(self) -> None:
        """KNOWN VIOLATION. A second policy over the same pods adds the internet.

        This asserted the property of the policy this slice renders, and held
        while that was the only egress policy selecting the agent Pod. It is
        not any more: gke-labs/kube-agents#676 gave platformagent-gateway-netpol
        an egress rule for 0.0.0.0/0 and ::/0, and both policies select
        `app: platformagent-gateway`. Union, so the whole-internet block is
        part of what the sandbox Pod gets whatever this slice renders beside
        it. The `except` clauses on that rule do list 169.254.0.0/16, which is
        why the metadata addresses are a separate violation below rather than
        this one -- but the reasoning in the paragraph after this still applies
        to why an `except` is not load-bearing on GKE.

        Recorded rather than fixed because the collision is a design question
        this slice cannot answer alone: #676 is correct about Workload Identity
        needing that path, and in the default sidecar layout the credential
        proxy shares the Pod and genuinely needs it. Only the split-broker
        layout separates the two, and deciding that is a later slice.

        NetworkPolicies are additive and have no deny primitive.

        `0.0.0.0/0 except 169.254.169.254/32` does not subtract the metadata
        server, it adds the internet -- worse than absent. Two further reasons
        it cannot work on GKE: NAT PREROUTING rewrites the destination before
        the policy is evaluated, and on Dataplane V2 an `ipBlock` peer never
        covers Pod-to-Pod traffic. So the assertion is that no rendered rule
        contains a whole-internet block, whatever it claims to except out.
        """
        documents = h.yaml_documents("golden_egress_allowlist")
        policies = h.objects_of_kind(documents, "NetworkPolicy")
        self.assertTrue(policies, "the allowlist fixture renders no NetworkPolicy")

        for policy in policies:
            spec = policy["spec"]
            with self.subTest(policy=policy["metadata"]["name"]):
                self.assertEqual(["Egress"], spec.get("policyTypes"))
                for rule in spec.get("egress") or []:
                    self.assertTrue(
                        rule.get("to"),
                        "an egress rule with no `to` allows every destination",
                    )
                    for peer in rule["to"]:
                        block = peer.get("ipBlock")
                        if not block:
                            continue
                        self.assertNotIn(
                            block["cidr"],
                            ("0.0.0.0/0", "::/0"),
                            "an `except` clause does not subtract a destination "
                            "from an additive policy",
                        )

    @h.known_violation("C1", "slice-2b/findings.md 1.4 (see gke-labs/kube-agents#676)")
    def test_C1_the_rendered_egress_policy_reaches_no_metadata_address(self) -> None:
        """KNOWN VIOLATION. The sandbox reaches the metadata server anyway.

        This is the invariant `spec.security.egressPolicy: Allowlist` exists to
        establish, and on this tree it does not hold -- on both delivery paths,
        which is worth stating because checking only one is how this was
        nearly missed:

        - Operator-rendered: platformagent-gateway-netpol allows
          169.254.169.254/32 on TCP 80 and 8080 -- the metadata server's own
          ports -- and 169.254.169.252/32 on 988, selecting the same
          `app: platformagent-gateway` pods that
          platformagent-sandbox-metadata-deny selects.
        - Kustomize-mode: deploy/kustomize/platform/networkpolicy-core-egress.yaml
          ships platform-agent-core-egress with the same two metadata rules. It
          selects on `app.kubernetes.io/name: platform-agent` rather than the
          `app:` label, a different expression over the same Pods, which the
          agent carries both of.

        Two policies over one Pod union their allow-sets, so opting into the
        allowlist does not subtract what either of them adds.

        The failure is real and the feature does not currently do what its name
        says. Recorded here rather than repaired because #676 was deliberate:
        Workload Identity needs the metadata path, and the sidecar layout puts
        the credential proxy in the Pod that would lose it. Scoping those rules
        to the broker Pod under splitCredentialBrokerPod is the shape of the
        fix, and it is a design change to a live-tested slice, not a test edit.

        Deleting this decorator is the signal that the control works: unittest
        reports the pass as an unexpected success.

        All three metadata addresses, not just the famous one.

        169.254.169.252 and fd20:ce::254 reach the same metadata service on
        GKE. A guard written against 169.254.169.254 alone is a guard against
        one spelling.
        """
        import ipaddress

        addresses = [
            ipaddress.ip_address(a)
            for a in ("169.254.169.254", "169.254.169.252", "fd20:ce::254")
        ]
        for policy in h.objects_of_kind(
            h.yaml_documents("golden_egress_allowlist"), "NetworkPolicy"
        ):
            for rule in policy["spec"].get("egress") or []:
                for peer in rule.get("to") or []:
                    block = peer.get("ipBlock")
                    if not block:
                        continue
                    network = ipaddress.ip_network(block["cidr"], strict=False)
                    for address in addresses:
                        if address.version != network.version:
                            continue
                        with self.subTest(cidr=block["cidr"], address=str(address)):
                            self.assertNotIn(address, network)

    def test_C1_every_operator_supplied_cidr_reaches_the_refusal_guards(self) -> None:
        """The wiring, because the differential itself is asserted in Go.

        The 4-in-6 evasion (`::ffff:0.0.0.0/96` passes `netip.Prefix.Contains`
        and parses as `0.0.0.0/0`) is tested where the guard lives, by
        `TestAControlPlaneCIDRCannotBeTheWholeInternet` and
        `TestExtraRulesCannotReopenTheMetadataServer` in
        platformagent_egress_policy_test.go, both of which carry mapped-form
        cases. What that Go test cannot catch is a new CRD field that accepts a
        CIDR and never calls the guard, so this asserts the call sites exist --
        one per operator-supplied CIDR input.
        """
        source = h.text("egress_policy_go")

        def body_of(function: str) -> str:
            return h.go_function_body(source, function)

        # The two entry points that take a CIDR the operator wrote. Named
        # individually rather than counted: an earlier version of this test
        # asserted "ipv4MappedRefusal appears at least twice", which a mutation
        # deleting one of the two call sites walked straight through.
        for entry_point in ("controlPlaneCIDRRefusal", "egressRuleReachesMetadata"):
            with self.subTest(entry_point=entry_point):
                self.assertIn(
                    "ipv4MappedRefusal(",
                    body_of(entry_point),
                    f"{entry_point} accepts an operator-supplied CIDR without "
                    f"routing it through the 4-in-6 guard",
                )

        # And the metadata containment check must not be the only thing standing
        # in front of a mapped prefix, because it cannot see into one.
        for entry_point in ("controlPlaneCIDRRefusal", "egressRuleReachesMetadata"):
            body = body_of(entry_point)
            with self.subTest(entry_point=entry_point, ordering=True):
                self.assertLess(
                    body.index("ipv4MappedRefusal("),
                    body.index("metadataServerAddresses"),
                    "the mapped-prefix refusal runs after the containment loop; "
                    "::ffff:169.254.169.254/128 passes the loop and normalises "
                    "to the metadata server in the cluster",
                )


class C2FailClosed(unittest.TestCase):
    """C2: anything the policy layer cannot parse, resolve or verify is refused."""

    def test_C2_an_unparseable_argv_is_refused(self) -> None:
        """An unknown global flag could hide the verb, so the verb is unknown.

        This is the fail-closed direction on a denylist of value-taking flags.
        The alternative -- an allowlist of flags we skip -- means the next
        kubectl release adds a flag and silently bypasses the gate.
        """
        cases = (
            (["kubectl", "--not-a-real-flag", "delete", "ns", "prod"], "kubernetes.unreadable-command"),
            (["kubectl", "--future-flag=x", "get", "pods"], "kubernetes.unreadable-command"),
            (["kubectl"], "kubernetes.unreadable-command"),
            (["gcloud", "--not-a-real-flag", "container", "clusters", "list"], "gcp.unreadable-command"),
            (["gcloud"], "gcp.read-only"),
        )
        for argv, rule_id in cases:
            with self.subTest(argv=argv):
                decision = command_policy.evaluate(argv)
                self.assertFalse(decision.allowed, argv)
                self.assertEqual(rule_id, decision.rule_id)

    def test_C2_an_unknown_flag_cannot_swallow_a_write_subcommand(self) -> None:
        """`rollout --someflag status restart x` must not read as `rollout status`.

        Phase 2 of the verb parse stops dead on an unrecognised flag rather
        than skipping it, because a flag of unknown arity could consume the
        word that decides whether this is a read or a reschedule.
        """
        for argv in (
            ["kubectl", "rollout", "--unknown", "status", "deploy/web"],
            ["kubectl", "rollout", "--unknown=1", "status", "deploy/web"],
        ):
            with self.subTest(argv=argv):
                decision = command_policy.evaluate(argv)
                self.assertFalse(
                    decision.allowed,
                    "an unknown flag was skipped over and the verb read as a "
                    "two-word read",
                )

    def test_C2_cluster_info_dump_is_refused_by_both_of_its_guards(self) -> None:
        """A single-word read verb otherwise lets any word follow it.

        `cluster-info` is allowed alone, so `cluster-info dump` inherits the
        allowance through the `verb[:1]` fallback -- and
        `--output-directory=DIR` writes a tree of files at any path inside the
        credential sidecar. Two guards, because the verb parse stops at the
        first unknown flag and `cluster-info --output-directory=/tmp/x dump`
        therefore reads as the bare, allowed `cluster-info`.
        """
        for argv, expected in (
            (["kubectl", "cluster-info", "dump"], "kubernetes.read-only"),
            (
                ["kubectl", "cluster-info", "--output-directory=/tmp/x", "dump"],
                "kubernetes.file-write-forbidden",
            ),
            (
                ["kubectl", "cluster-info", "dump", "--output-directory", "/tmp/x"],
                "kubernetes.file-write-forbidden",
            ),
        ):
            with self.subTest(argv=argv):
                decision = command_policy.evaluate(argv)
                self.assertFalse(decision.allowed, argv)
                self.assertEqual(expected, decision.rule_id)

    def test_C2_the_read_only_gate_survives_a_typo(self) -> None:
        """A misspelled ConfigMap value must not quietly hand over write access.

        The escape hatch is global, unscoped and has no expiry, so the failure
        mode of getting its value slightly wrong has to be "still enforcing".
        Only the exact string `false` disarms it.
        """
        import os
        from unittest import mock

        disarming = ("false", "FALSE", "False", " false ")
        leaving_armed = ("", "no", "0", "off", "flase", "true", "yes")

        for value in disarming:
            with self.subTest(value=value, expect="disarmed"):
                with mock.patch.dict(
                    os.environ, {"CREDENTIAL_PROXY_ENFORCE_READ_ONLY": value}
                ):
                    self.assertFalse(h.credential_proxy.read_only_enforced())
        for value in leaving_armed:
            with self.subTest(value=value, expect="armed"):
                with mock.patch.dict(
                    os.environ, {"CREDENTIAL_PROXY_ENFORCE_READ_ONLY": value}
                ):
                    self.assertTrue(h.credential_proxy.read_only_enforced())

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(
                h.credential_proxy.read_only_enforced(),
                "an unset variable must leave the gate armed",
            )

    def test_C2_the_agent_api_proxy_refuses_to_start_without_its_key(self) -> None:
        """An empty secret must stop the process, not disable the check.

        `API_SERVER_EXTERNAL_KEY` is the only thing standing between the
        cluster network and the agent's chat API. Reading it with a permissive
        default is the shape that produced the 8642 sentinel; this one raises.
        """
        import os
        from unittest import mock

        # Both the empty value and the *absent* one. Only checking the empty
        # case leaves the read's default argument untested, and a mutation
        # giving it a development default survived exactly that gap.
        with mock.patch.dict(os.environ, {"API_SERVER_EXTERNAL_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                h.credential_proxy.start_agent_api_proxy()

        environment = {
            key: value
            for key, value in os.environ.items()
            if key != "API_SERVER_EXTERNAL_KEY"
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(RuntimeError):
                h.credential_proxy.start_agent_api_proxy()

        source = h.text("credential_proxy")
        self.assertIn(
            'os.getenv("API_SERVER_EXTERNAL_KEY", "")',
            source,
            "the key is read with a non-empty default, so an unconfigured "
            "deployment gets a working key instead of a refusal",
        )

    def test_C2_precondition_the_inject_handler_still_forwards_a_bearer_token(self) -> None:
        self.assertIn("Authorization", h.text("session_kv_server"))

    @h.known_violation("C2", "overnight-b/findings.md 2.2")
    def test_C2_the_session_server_fails_closed_on_a_missing_api_key(self) -> None:
        """KNOWN VIOLATION. A missing key sends the request unauthenticated.

        `session_kv_server` reads `API_SERVER_KEY` and guards the header with
        `if token:`, so an unset key omits the Authorization header and the
        call proceeds. `agent_common_server.py` gets the same situation right
        by raising, which is what makes this a divergence rather than a
        judgement call about how strict to be.

        Lower severity than the unauthenticated inject route above -- the
        request will be rejected downstream -- but it is the same fail-open
        reflex, in the same file, and C2 covers it.
        """
        source = h.text("session_kv_server")
        token_read = source.index('os.environ.get("API_SERVER_KEY"')
        window = source[token_read : token_read + 600]
        self.assertNotIn(
            "if token:",
            window,
            "the Authorization header is conditional on the token being "
            "present, so a missing key degrades to an unauthenticated request",
        )


class C3UntrustedByDefault(unittest.TestCase):
    """C3: no privileged decision may be derived from content the agent controls."""

    def test_C3_the_policy_decision_reads_nothing_but_its_argv(self) -> None:
        """The strongest form of "untrusted content cannot reach the decision".

        Every historical bypass in this project works by getting the checker to
        consult something the agent can rewrite -- a kuberc file, a flags file,
        a gcloud configuration. The structural answer is that `evaluate` is a
        pure function of argv: it opens no file, resolves no name and makes no
        connection, so there is no second input for the agent to control and no
        window between the check and the act.

        Enforced with an audit hook rather than by reading the source, because
        the property has to hold through whatever `evaluate` calls, not just in
        the function itself.
        """
        observed: list[str] = []
        watched = {
            "open",
            "socket.connect",
            "socket.getaddrinfo",
            "subprocess.Popen",
            "os.system",
            "exec",
            "compile",
            "import",
        }

        def hook(event: str, _arguments: object) -> None:
            if event in watched:
                observed.append(event)

        argvs = [
            ["kubectl", "get", "pods"],
            ["kubectl", "--kuberc", "/workspace/kr.yaml", "get", "pods"],
            # Both separator forms. A guard that opened the named file would
            # only do so for the spelling that carries a value, and a corpus
            # with one spelling in it cannot see that.
            ["kubectl", "--kuberc=/workspace/kr.yaml", "get", "pods"],
            ["kubectl", "delete", "namespace", "prod"],
            ["gcloud", "--flags-file", "/workspace/f.yaml", "info"],
            ["gcloud", "--flags-file=/workspace/f.yaml", "info"],
            ["gcloud", "container", "clusters", "delete", "prod"],
            ["kubectl", "get", "pods", "-shttp://127.0.0.1:9000"],
        ]
        # Warm every lazy import the module might make before the hook is armed;
        # an import inside evaluate would otherwise be attributed to the hook's
        # first call rather than to the behaviour under test.
        for argv in argvs:
            command_policy.evaluate(argv)

        sys.addaudithook(hook)
        for argv in argvs:
            command_policy.evaluate(argv)

        self.assertEqual(
            [],
            observed,
            "command_policy.evaluate performed I/O; a policy decision that "
            "reads agent-writable state has a rewrite-after-check race",
        )

    def test_C3_the_policy_module_imports_nothing_that_can_read(self) -> None:
        """The audit hook has a blind spot, and this is what covers it.

        CPython raises no audit event for `os.stat`, so a check as small as
        `os.path.exists(kuberc_path)` -- the helpful-looking change that
        reintroduces the rewrite-after-check race -- is invisible to the hook
        above. A mutation adding exactly that walked past the first version of
        this test.

        Reaching the filesystem needs a name to reach it with, so the import
        list is the thing to constrain. `command_policy` imports `dataclasses`
        and `__future__` and nothing else, which is not an accident of its
        current size -- it is the property that makes the module a pure
        function of argv.
        """
        tree = ast.parse(h.text("command_policy"))

        capable = {
            "os", "io", "pathlib", "socket", "subprocess", "shutil", "glob",
            "tempfile", "urllib", "http", "requests", "yaml", "json", "sqlite3",
            "importlib",
        }
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(
            set(),
            imported & capable,
            f"command_policy imports {sorted(imported & capable)}; the decision "
            f"can now read something the agent can write",
        )

        dynamic = {"open", "__import__", "eval", "exec", "compile"}
        used = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertEqual(
            set(),
            used & dynamic,
            f"command_policy calls {sorted(used & dynamic)}, which reaches "
            f"outside argv without needing an import",
        )

    def test_C3_untrusted_output_cannot_forge_a_log_line(self) -> None:
        """Tool output is untrusted, and the audit record is a consumer of it.

        Anyone who can write a Pod log can write to anything that quotes one.
        The sanitiser strips every category that a text-mode consumer treats as
        a line boundary -- C0/C1 controls, Unicode line and paragraph
        separators, and format characters -- and caps the length, so an agent
        cannot expand a hint into a synthetic record.
        """
        sanitize = h.credential_proxy._sanitize_for_logging
        forgeries = (
            "x\nINFO fake log line",
            "x\rINFO fake log line",
            "x INFO fake",
            "x INFO fake",
            "x​INFO fake",
            "x‮INFO fake",
            "x\x00INFO fake",
        )
        for value in forgeries:
            with self.subTest(value=repr(value)):
                cleaned = sanitize(value)
                self.assertNotIn("\n", cleaned)
                self.assertNotIn("\r", cleaned)
                for character in (" ", " ", "​", "‮", "\x00"):
                    self.assertNotIn(character, cleaned)
        self.assertLessEqual(len(sanitize("A" * 4096)), 64)


class C4ProvenanceOfExecutableContent(unittest.TestCase):
    """C4: skills, plugins, actions and images are pinned, signed and owned."""

    def test_C4_every_third_party_action_is_pinned_to_a_commit(self) -> None:
        """A mutable tag means a retagged release silently changes what CI runs.

        Local reusable workflows are exempt: `uses: ./…` resolves within the
        commit under test and there is nothing to pin it to.
        """
        workflows = sorted((h.REPO_ROOT / ".github" / "workflows").glob("*.yml"))
        self.assertTrue(workflows, "no workflows found; the glob is wrong")

        unpinned = []
        for workflow in workflows:
            for number, line in enumerate(workflow.read_text().splitlines(), start=1):
                match = re.search(r"^\s*(?:-\s*)?uses:\s*(\S+)", line)
                if not match:
                    continue
                reference = match.group(1).strip("\"'")
                if reference.startswith("./"):
                    continue
                _, _, version = reference.partition("@")
                if not re.fullmatch(r"[0-9a-f]{40}", version):
                    unpinned.append(f"{workflow.name}:{number} {reference}")
        self.assertEqual([], unpinned)

    def test_C4_precondition_the_skill_sync_still_clones_upstream(self) -> None:
        self.assertIn("UPSTREAM_REPO", h.text("skill_sync"))
        self.assertIn("clone", h.text("skill_sync"))

    @h.known_violation("C4", "04_major_requirements.md C4")
    def test_C4_upstream_skills_are_pinned_and_verified(self) -> None:
        """KNOWN VIOLATION. Whatever is at upstream HEAD becomes agent instructions.

        `sync-upstream-skills.py` shallow-clones the default branch of an
        upstream repository with no pinned ref, no tag, no commit SHA and no
        checksum, then `rmtree`s and `copytree`s fifteen skill directories into
        the agent's skill set verbatim.

        C4 forbids automatic upgrade to unpinned upstream content and makes
        checksum verification mandatory. The reason this is a class of
        invariant rather than a CI nicety: the payload lands in content the
        agent treats as instructions, which is deterministic input reaching the
        agent outside every model-facing control there is.
        """
        source = h.text("skill_sync")
        pins = re.search(r"--branch|--revision|UPSTREAM_REF|[0-9a-f]{40}", source)
        self.assertIsNotNone(pins, "the upstream clone names no immutable ref")
        self.assertRegex(
            source, r"sha256|hashlib|checksum", "the synced content is not verified"
        )

    def test_C4_the_agent_base_image_is_pinned_by_digest(self) -> None:
        """The one image reference in this repo that is done correctly.

        Asserted so that dropping the digest back to a bare tag is a red test
        rather than a diff nobody reads.
        """
        self.assertRegex(h.text("tags_env"), r"HERMES_AGENT_TAG=\S+@sha256:[0-9a-f]{64}")
        self.assertIn("${HERMES_AGENT_TAG}", h.text("dockerfile"))

    def test_C4_precondition_the_chart_still_names_images(self) -> None:
        self.assertIn("repository:", h.text("chart_values"))

    @h.known_violation("C4", "04_major_requirements.md C4")
    def test_C4_every_shipped_image_is_pinned_by_digest(self) -> None:
        """KNOWN VIOLATION. The chart and the operator defaults pin tags, not digests.

        `resolveAgentImage` accepts either and appends `:latest` when a
        reference carries neither, and the operator's default platform-agent
        version is the literal string `latest`. A tag is a mutable pointer, so
        every guarantee about what runs in the agent Pod is a guarantee about
        what the registry says today.
        """
        values = h.text("chart_values")
        tags = re.findall(r"^\s*tag:\s*(\S+)", values, re.MULTILINE)
        floating = [tag for tag in tags if not tag.startswith('""')]
        self.assertEqual(
            [], floating, f"chart values pin mutable tags: {floating}"
        )
        self.assertNotIn(
            'DefaultPlatformAgentVersion = "latest"',
            (h.REPO_ROOT / "k8s-operator/internal/controller/manifest_helpers.go").read_text(),
        )


class C5PrivilegedControllersAreBounded(unittest.TestCase):
    """C5: no controller grants more than the requester holds, or reaps a guardrail."""

    # Verbs a read-only ceiling may contain.
    READ_VERBS = frozenset({"get", "list", "watch"})

    # The two roles that legitimately hold a write verb, each exempted by name
    # prefix and then bounded separately below. Exempting by name rather than by
    # widening READ_VERBS means a *third* write-capable minted role is a red
    # test rather than an unnoticed addition to a set.
    #
    # - kubeagents:leader: coordination leases for the replicas > 1 path. Nothing
    #   to do with the customer's cluster.
    # - kubeagents:tokenreview: `create` on tokenreviews, which is how the split
    #   broker verifies its caller's token by asking the API server instead of
    #   comparing a secret. Deliberately this rather than binding
    #   system:auth-delegator, which carries subjectaccessreviews too.
    WRITE_CAPABLE_ROLE_PREFIXES = ("kubeagents:leader:", "kubeagents:tokenreview:")

    def test_C5_no_minted_role_grants_a_write_verb(self) -> None:
        """The agent-side half of A2's intersection, asserted on rendered output.

        Every Role and ClusterRole the controller mints appears in the golden
        fixtures, so this reads what ships rather than what the builders say.
        """
        checked = 0
        for name, documents in h.golden_documents().items():
            for kind in ("Role", "ClusterRole"):
                for role in h.objects_of_kind(documents, kind):
                    role_name = role["metadata"]["name"]
                    if role_name.startswith(self.WRITE_CAPABLE_ROLE_PREFIXES):
                        continue
                    checked += 1
                    for rule in role.get("rules") or []:
                        with self.subTest(fixture=name, role=role_name, rule=rule):
                            verbs = set(rule.get("verbs") or [])
                            self.assertTrue(
                                verbs <= self.READ_VERBS,
                                f"{role_name} grants {sorted(verbs - self.READ_VERBS)}",
                            )
        self.assertGreater(
            checked, 0, "no minted role was examined; the fixtures render none"
        )

    def test_C5_the_tokenreview_role_is_the_narrowest_form_of_itself(self) -> None:
        """The second named exception, bounded.

        `system:auth-delegator` is the reflex here and it carries
        subjectaccessreviews as well, which is an authorization oracle over the
        whole cluster. This role holds one verb on one resource.
        """
        found = False
        for documents in h.golden_documents().values():
            for role in h.objects_of_kind(documents, "ClusterRole"):
                if not role["metadata"]["name"].startswith("kubeagents:tokenreview:"):
                    continue
                found = True
                rules = role.get("rules") or []
                self.assertEqual(1, len(rules), "the tokenreview role grew a rule")
                rule = rules[0]
                self.assertEqual(["create"], rule.get("verbs"))
                self.assertEqual(["tokenreviews"], rule.get("resources"))
                self.assertEqual(["authentication.k8s.io"], rule.get("apiGroups"))
        self.assertTrue(
            found,
            "no tokenreview role in any fixture; the exemption above is stale "
            "and is now silently excusing nothing",
        )

    def test_C5_no_agent_binding_names_the_auth_delegator_role(self) -> None:
        """The shortcut the test above exists to keep closed."""
        for name, documents in h.golden_documents().items():
            for kind in ("RoleBinding", "ClusterRoleBinding"):
                for binding in h.objects_of_kind(documents, kind):
                    with self.subTest(fixture=name, binding=binding["metadata"]["name"]):
                        self.assertNotEqual(
                            "system:auth-delegator",
                            (binding.get("roleRef") or {}).get("name"),
                        )

    def test_C5_the_leader_role_stays_confined_to_coordination(self) -> None:
        """The named exception above, bounded so it cannot become a general grant."""
        allowed_resources = {"leases", "pods"}
        found = False
        for documents in h.golden_documents().values():
            for role in h.objects_of_kind(documents, "Role"):
                if not role["metadata"]["name"].startswith("kubeagents:leader:"):
                    continue
                found = True
                for rule in role.get("rules") or []:
                    resources = set(rule.get("resources") or [])
                    with self.subTest(rule=rule):
                        self.assertTrue(
                            resources <= allowed_resources,
                            f"leader role reaches {sorted(resources - allowed_resources)}",
                        )
                        self.assertNotIn("secrets", resources)
        self.assertTrue(found, "no leader Role in any fixture; the exemption is stale")

    def test_C5_the_agent_is_bound_to_no_write_capable_builtin_role(self) -> None:
        """A read-only rule set is worth nothing next to a binding to `edit`.

        The minted-verb test above cannot see this: a ClusterRoleBinding names
        a role by reference, so binding the agent to the built-in `admin`
        would leave every minted rule read-only and every effective permission
        not.
        """
        forbidden = {"admin", "edit", "cluster-admin"}
        for name, documents in h.golden_documents().items():
            for kind in ("RoleBinding", "ClusterRoleBinding"):
                for binding in h.objects_of_kind(documents, kind):
                    role_ref = binding.get("roleRef") or {}
                    with self.subTest(fixture=name, binding=binding["metadata"]["name"]):
                        self.assertNotIn(role_ref.get("name"), forbidden)

    def test_C5_the_controller_does_not_reap_the_metadata_deny_guardrail(self) -> None:
        """Slice 2b 1.5: an entire isolation architecture was garbage-collected by name.

        `deleteLegacyCredentialIsolationResources` ran on every reconcile and
        deleted the `<name>-credential-proxy` Deployment and Service as well as
        the metadata-deny NetworkPolicy -- the whole two-pod split that an
        earlier commit had shipped. A test asserted the deletion as correct
        behaviour. C5's original wording, "a guardrail it did not create",
        arguably permitted it, because a prior revision of the same controller
        had created those resources.

        This asserts the reaper's target list, which is the thing that has to
        stay narrow. `reconcileAgentEgressPolicy` re-asserting the policy is
        asserted by TestReconcileRevertsAPermissiveEditToTheEgressPolicy in Go.
        """
        source = h.text("controller_go")
        body = h.go_function_body(source, "deleteLegacyCredentialIsolationResources")
        for forbidden in (
            "NetworkPolicy",
            "sandbox-metadata-deny",
            "agentEgressPolicyName",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(
                    forbidden,
                    body,
                    "the legacy cleanup reaches a guardrail; a controller that "
                    "deletes its own predecessor's protection is C5's violation",
                )

    def test_C5_the_admission_binding_names_a_policy_that_exists(self) -> None:
        """Slice 2b 1.2: applied and enforcing are different states, and the gap is silent.

        Kustomize `namePrefix` rewrites `metadata.name` and does not rewrite
        `ValidatingAdmissionPolicyBinding.spec.policyName`. Install that way and
        both policies exist, both bindings point at nothing, and
        `kubectl get validatingadmissionpolicy` looks correct.

        This is the static half of the check -- every binding resolves to a
        policy declared in the same document set. The half that actually
        matters, "a violating request is rejected by the API server", needs a
        cluster and is written in bucket2/test_cluster_scenarios.py.
        """
        for key in ("admission_policy", "chart_admission_policy"):
            documents = h.yaml_documents(key)
            policies = {
                d["metadata"]["name"]
                for d in documents
                if d.get("kind") == "ValidatingAdmissionPolicy"
            }
            bindings = [
                d
                for d in documents
                if d.get("kind") == "ValidatingAdmissionPolicyBinding"
            ]
            with self.subTest(source=key):
                self.assertTrue(policies, f"{key} declares no policy")
                self.assertTrue(bindings, f"{key} declares no binding")
                for binding in bindings:
                    self.assertIn(
                        binding["spec"]["policyName"],
                        policies,
                        f"{binding['metadata']['name']} binds a policy that is "
                        f"not declared here",
                    )

    def test_C5_the_admission_policy_fails_closed(self) -> None:
        """`failurePolicy: Ignore` is the one-line edit that voids the whole file.

        B3 names this as the change that evaporates every guarantee in the
        policy with nothing failing visibly, under a commit message like
        "unblock apply during upgrade window".
        """
        for key in ("admission_policy", "chart_admission_policy"):
            for document in h.yaml_documents(key):
                if document.get("kind") != "ValidatingAdmissionPolicy":
                    continue
                with self.subTest(source=key, policy=document["metadata"]["name"]):
                    self.assertEqual("Fail", document["spec"].get("failurePolicy"))
            for document in h.yaml_documents(key):
                if document.get("kind") != "ValidatingAdmissionPolicyBinding":
                    continue
                with self.subTest(source=key, binding=document["metadata"]["name"]):
                    self.assertEqual(
                        ["Deny"], document["spec"].get("validationActions")
                    )


def _executor():
    """A CommandExecutor over a throwaway state directory.

    The constructor creates its directory tree, so the tests that read
    `environment` or call `execute` need a real path rather than a mock.
    """
    directory = tempfile.mkdtemp(prefix="conformance-broker-")
    return h.credential_proxy.CommandExecutor(
        timeout_seconds=1, max_output_bytes=1024, state_dir=directory
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

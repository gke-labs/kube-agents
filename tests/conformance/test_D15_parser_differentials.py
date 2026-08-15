"""D15 -- the class, not an invariant.

Every Critical this project has found is the same shape: **the component that
checks and the component that executes parse the same input differently.**
Three across two slices before tonight --

  * `--kuberc` is `--flags-file` for kubectl. Our parser did not know the file
    was a flags source; kubectl did, and honoured `as: system:admin` from it
    with no `--as` in argv.
  * `-shttp://host` is `--server`. Our tokenizer matched exact tokens; cobra
    accepts the attached shorthand.
  * `::ffff:0.0.0.0/96` is `0.0.0.0/0`. A CIDR guard written with
    `netip.Prefix.Contains` returns false across address families; Go's
    `net.ParseCIDR`, which the API server, `k8s.io/utils/net` and Calico all
    sit on, normalises it back to the whole internet.

A check written against a different parser than the enforcer is a *guess*
about what the enforcer will do, and guesses fail silently and in the
permissive direction.

The three above have tests where each control lives -- the first two in
test_A_authority.py, the third in Go, in
`platformagent_egress_policy_test.go`, because Python cannot call the guard.
What this module adds is the *differential itself*: feed the same input to both
sides and assert they agree. That is the test the class asks for and the one
none of the three individually is.

**The class is open.** There is no reason to think the third was the last, and
the third was found late, in code two earlier reviews had already passed.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
import unittest

from . import _harness as h
from ._harness import command_policy


class TheCheckerAndTheExecutorSeeTheSameArgv(unittest.TestCase):
    """The broker's two layers disagree about what an argv is.

    The regex denylist matches `shlex.join(argv)` -- a shell string. The
    executor runs the list. Those are two parsers over one input, which is the
    class by definition, so the differential is worth measuring rather than
    assumed absent.
    """

    # Commands the denylist is meant to refuse, each with a token mutated so
    # that shlex.join has to quote it. Quoting inserts a character the patterns
    # do not expect, and `(?:\s+\S+)*?` cannot traverse a token that both
    # contains whitespace and abuts the terminal word.
    QUOTED_FORMS = (
        ["gcloud", "auth", "print-access-token\n"],
        ["gcloud", "auth", "print-access-token "],
        ["gcloud", "auth\nprint-access-token"],
        ["gcloud", "auth", "print-access-token;true"],
        ["gh", "auth", "token\n"],
        ["kubectl", "config", "view", "--raw "],
    )

    def test_D15_precondition_the_denylist_still_matches_a_joined_string(self) -> None:
        """Guards the expected failure below: `blocked_by` still shell-joins."""
        source = h.text("credential_proxy")
        start = source.index("def blocked_by(")
        body = source[start : start + 300]
        self.assertIn("shlex.join(argv)", body)
        self.assertIsNotNone(h.policy_blocks(["gcloud", "auth", "print-access-token"]))

    @h.known_violation("C2", "overnight-b/findings.md 2.5")
    def test_D15_quoting_does_not_change_the_denylist_verdict(self) -> None:
        """KNOWN VIOLATION. Shell-quoting an argv escapes a denylist rule.

        `Policy.blocked_by` matches its patterns against `shlex.join(argv)` --
        a shell string -- while `CommandExecutor._execute` runs the list. Two
        parsers, one input, which is the class by definition. Quoting a token
        inserts a `'` the patterns do not expect, and `(?:\\s+\\S+)*?` cannot
        traverse a token that both contains whitespace and abuts the terminal
        word. `gcloud auth print-access-token\\n` matches the disclosure rule
        unjoined and matches nothing joined.

        **Not currently exploitable, and the reason is not reassuring.** Every
        payload that triggers the divergence is one gcloud, gh and kubectl all
        reject: `gcloud auth 'print-access-token;true'` is not a command. So
        the gap is closed by the argument parsers of four third-party CLIs,
        which is a property of their code and not of ours. That is the exact
        definition of a guess, and D15 exists because guesses in this codebase
        have failed silently and in the permissive direction three times.

        Severity is low and the direction of the fix is clear: match per-token
        against argv rather than against a joined string, the way
        `command_policy` already does. Filed rather than fixed, because the
        brief for this suite is to record what fails and not to patch it.

        The assertion is one-directional. Quoting may make the denylist
        *stricter* -- an over-block is a nuisance; an under-block is the
        Critical.
        """
        loosened = []
        for argv in self.QUOTED_FORMS:
            quoted = h._match_rules(shlex.join(argv))
            plain = h._match_rules(" ".join(argv))
            if plain is not None and quoted is None:
                loosened.append(f"{argv!r}: plain={plain} quoted=None")
        self.assertEqual(
            [],
            loosened,
            "shell-quoting an argv escapes a denylist rule. The checker parses a "
            "shell string and the executor runs a list; where they disagree, the "
            "executor wins.",
        )

    def test_D15_the_two_layers_agree_on_the_governed_tool(self) -> None:
        """`argv[0]` decides which layer applies, so both must read it the same way.

        The read-only allowlist governs `kubectl` and `gcloud` by exact match on
        `argv[0]`; the executor resolves `argv[0]` through `shutil.which` over a
        fixed PATH. A path-qualified spelling that one accepts and the other
        does not is the same class again -- `/usr/bin/kubectl delete ns prod`
        reading as ungoverned to the allowlist while executing kubectl.
        """
        for spelling in ("/usr/bin/kubectl", "./kubectl", "kubectl/", "KUBECTL"):
            with self.subTest(spelling=spelling):
                decision = command_policy.evaluate([spelling, "delete", "ns", "prod"])
                executor_accepts = (
                    spelling in h.credential_proxy.CommandExecutor.ALLOWED_EXECUTABLES
                )
                self.assertFalse(
                    decision.allowed and executor_accepts,
                    f"{spelling} is ungoverned by the read-only allowlist and "
                    f"still executable",
                )

    def test_D15_a_refused_flag_is_refused_wherever_it_appears(self) -> None:
        """Position is a parser difference too.

        kubectl accepts its global flags before or after the verb. A check that
        walks argv only until the verb sees a different command than the one
        cobra runs. This is the generalisation of the `-shttp://` finding: the
        first fix caught the flag, and the position was a second spelling.
        """
        for flag in ("--as=system:admin", "--kuberc=/w/k.yaml", "--server=http://x"):
            positions = (
                ["kubectl", flag, "get", "pods"],
                ["kubectl", "get", flag, "pods"],
                ["kubectl", "get", "pods", flag],
                ["kubectl", "get", "pods", "-n", "prod", flag],
            )
            verdicts = {
                tuple(argv): command_policy.evaluate(argv).rule_id for argv in positions
            }
            with self.subTest(flag=flag):
                self.assertEqual(
                    1,
                    len(set(verdicts.values())),
                    f"the verdict for {flag} depends on where it sits: {verdicts}",
                )
                self.assertNotIn("", set(verdicts.values()))


class ACIDRMeansTheSameThingToBothSides(unittest.TestCase):
    """The third finding's differential, reproduced against Python's parser.

    The Go guard cannot be called from here. What can be checked from here is
    the *premise* the guard rests on -- that `::ffff:0.0.0.0/96` and
    `0.0.0.0/0` denote the same set of reachable addresses to a library that
    normalises, while denoting different sets to one that compares address
    families. If that premise ever stopped holding, refusing 4-in-6 prefixes
    outright would be over-strict rather than necessary, and someone would be
    right to relax it.

    Written as an executable statement of the premise rather than a comment,
    because the finding was made by executing the predicates and not by
    reasoning about them.
    """

    MAPPED = "::ffff:0.0.0.0/96"
    METADATA = ipaddress.ip_address("169.254.169.254")

    def test_D15_the_mapped_prefix_still_denotes_the_whole_ipv4_internet(self) -> None:
        """The premise: containment says no, normalisation says everything."""
        mapped = ipaddress.ip_network(self.MAPPED)

        # A same-family containment check -- the shape of `netip.Prefix.Contains`
        # -- sees no IPv4 address inside an IPv6 prefix, so the guard passes it.
        self.assertNotEqual(mapped.version, self.METADATA.version)

        # Normalising the mapped form gives back the whole IPv4 space, which is
        # what the API server's ipBlock validation and Calico act on.
        unmapped = ipaddress.ip_network(
            f"{mapped.network_address.ipv4_mapped}/{mapped.prefixlen - 96}"
        )
        self.assertEqual(ipaddress.ip_network("0.0.0.0/0"), unmapped)
        self.assertIn(self.METADATA, unmapped)

    def test_D15_the_guard_refuses_the_ambiguous_form_rather_than_normalising(self) -> None:
        """Refusing beats unmapping, and the code has to say which it does.

        Unmapping means reimplementing another library's normalisation and
        betting they agree at every edge -- the same bet that produced the
        finding. The branch refuses IPv4-mapped prefixes outright, and this
        asserts the guard is still written that way rather than having been
        "improved" into a converter.
        """
        body = h.go_function_body(h.text("egress_policy_go"), "ipv4MappedRefusal")
        self.assertNotIn(
            "Unmap()",
            body,
            "the guard normalises rather than refusing; that is the bet the "
            "finding was about",
        )
        self.assertIn("Overlaps(", body)


class TheClassIsOpen(unittest.TestCase):
    """A standing reminder in executable form."""

    def test_D15_every_known_differential_has_a_test(self) -> None:
        """The three findings, each mapped to the test that covers it.

        A checklist rather than an assertion about behaviour, and deliberately
        so: its job is to fail when someone deletes one of the three, which is
        the way a differential quietly stops being covered.
        """
        from . import test_A_authority

        coverage = {
            "--kuberc flags-file": (
                test_A_authority.A3ThePrincipalComesFromAVerifiedChannel,
                "test_A3_rejects_kuberc",
            ),
            "-shttp:// attached shorthand": (
                test_A_authority.A3ThePrincipalComesFromAVerifiedChannel,
                "test_A3_rejects_attached_shorthand_server",
            ),
            "::ffff:0.0.0.0/96 mapped CIDR": (
                ACIDRMeansTheSameThingToBothSides,
                "test_D15_the_guard_refuses_the_ambiguous_form_rather_than_normalising",
            ),
        }
        for finding, (owner, name) in coverage.items():
            with self.subTest(finding=finding):
                self.assertTrue(hasattr(owner, name), f"{finding} has lost its test")

    def test_D15_the_readme_says_the_class_is_open(self) -> None:
        """The suite must not read as a claim that the class is closed.

        Thirteen mutations went red against the slice-2b branch and the one
        real evasion still turned up late, in code two reviews had passed. A
        conformance suite that lists three differentials and stops invites
        exactly the reading this sentence exists to prevent.
        """
        readme = (h.REPO_ROOT / "tests" / "conformance" / "README.md").read_text()
        self.assertRegex(
            readme,
            r"(?i)the class is (still )?open",
            "the README no longer records that the parser-differential class "
            "is open",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

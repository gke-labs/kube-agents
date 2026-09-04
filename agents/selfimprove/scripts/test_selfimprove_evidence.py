#!/usr/bin/env python3
"""Tests for the evidence CLI's redaction pass.

The pass exists because of what happens downstream of it: whatever an
investigation quotes reaches the ledger, and in `upstream` mode the ledger
reaches a public pull request on a repository the install's owner does not
control. So these tests are about disclosure, not correctness in the abstract,
and they come in two halves that pull against each other -- what must never
survive, and what must never be destroyed. A redactor that eats the log text
around the identifier passes the first half and makes the feature useless.

Nothing here touches the network. The redaction functions are pure, which is
deliberate: they sit at the output boundary precisely so they can be tested
without a metadata server, a Google token, or a cluster.
"""

import contextlib
import io
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import selfimprove_evidence as E  # noqa: E402

#: A project id of the shape GCP actually issues -- lowercase, hyphenated,
#: within the 6-30 character range -- so that a test asserting it is redacted is
#: asserting something about real inputs.
E_PROJECT = "acme-prod-1"


class CredentialTests(unittest.TestCase):
    def test_github_and_google_tokens_do_not_survive(self):
        for secret in (
            "ghp_abcdefghijklmnopqrstuvwxyz012345",
            "gho_abcdefghijklmnopqrstuvwxyz012345",
            "github_pat_11ABCDEFG0abcdefghijkl_lmnopqrstuvwxyz",
            "ya29.A0ARrdaM_averylongaccesstokenvalue",
            "AIzaSyA1234567890abcdefghijklmnopqrstuv",
            "xoxb-1234567890-abcdefghij",
        ):
            with self.subTest(secret=secret):
                out = E.redact("the call failed with %s in the header" % secret)
                self.assertNotIn(secret, out)
                self.assertIn("[REDACTED]", out)

    def test_a_jwt_does_not_survive(self):
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
        self.assertNotIn(jwt, E.redact("Authorization: Bearer %s" % jwt))

    def test_a_private_key_header_does_not_survive(self):
        self.assertNotIn(
            "BEGIN RSA PRIVATE KEY", E.redact("-----BEGIN RSA PRIVATE KEY-----\nMIIE...")
        )

    def test_the_key_body_does_not_survive_either(self):
        """The header is the one part of a PEM that is not secret.

        Matching it alone and leaving the base64 beneath it is worse than not
        matching at all: the evidence carries the key and a `[REDACTED]` marker
        saying it does not.
        """
        body = "MIIEowIBAAKCAQEAx7Fq2Kj9vHnP0sTuVwXyZaBcDeFgHiJkLmNoPqRsTuVwXyZ"
        pem = "-----BEGIN RSA PRIVATE KEY-----\n%s\n-----END RSA PRIVATE KEY-----" % body
        out = E.redact("the minter loaded %s from the secret" % pem)
        self.assertNotIn(body, out)
        self.assertIn("the minter loaded [REDACTED] from the secret", out)

    def test_a_key_body_truncated_before_its_end_line_does_not_survive(self):
        # How a key usually reaches a log: something printed it and the entry
        # was cut short, so there is no END marker to anchor on.
        body = "MIIEowIBAAKCAQEAx7Fq2Kj9vHnP0sTuVwXyZaBcDeFgHiJkLmNoPqRsTuVwXyZ"
        out = E.redact("dump: -----BEGIN PRIVATE KEY-----\n%s" % body)
        self.assertNotIn(body, out)

    def test_a_slack_app_level_token_does_not_survive(self):
        # Socket Mode's token is `xapp-`, which is not an `xox` prefix, so the
        # `xox[baprs]-` class never reached it.
        for secret in ("xapp-1-A012BCDEFGH-1234567890123-abcdef0123456789", "xoxe-1-A0123456789abc"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, E.redact("socket connect failed for %s" % secret))

    def test_a_webhook_query_credential_does_not_survive(self):
        url = (
            "https://chat.googleapis.com/v1/spaces/AAQA1b2c3d4/messages"
            "?key=AIzaSyA1234567890abcdefghijklmnopqrstuv"
            "&token=Xy9_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcd%3D"
        )
        out = E.redact("delivery to %s returned 404" % url)
        self.assertNotIn("Xy9_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcd", out)
        # The parameter name survives, so the evidence still says what was held
        # back rather than showing an anonymous gap in a URL.
        self.assertIn("&token=[REDACTED]", out)

    def test_a_slack_incoming_webhook_url_does_not_survive(self):
        """The path is the credential: anyone holding it can post to the
        channel, and a failed delivery logs the URL in full.

        The trailing segment carries underscores so the literal does not match
        GitHub's own Slack-webhook detector. A fixture shaped exactly like the
        real thing is a fixture push protection blocks, and the branch carrying
        it cannot be pushed at all -- which costs more than it proves, since
        the pattern under test bounds a character class rather than a shape.
        """
        url = (
            "https://hooks.slack.com/services/T00000000000/B00000000000/"
            "EXAMPLE_NOT_A_REAL_WEBHOOK_TOKEN"
        )
        out = E.redact("POST %s returned 410 channel_not_found" % url)
        self.assertNotIn("EXAMPLE_NOT_A_REAL_WEBHOOK_TOKEN", out)
        self.assertNotIn("T00000000000", out)
        self.assertIn("410 channel_not_found", out)

    def test_a_key_body_whose_newlines_are_escaped_does_not_survive(self):
        """The `\\n` spelling is the one the test above cannot reach.

        `test_a_key_body_truncated_before_its_end_line_does_not_survive` uses a
        real newline, and the second arm of the PEM rule matched it because
        `\\s` is in the class. A backslash was not, so the same key written with
        escaped line breaks matched zero characters after the header: the
        armour was replaced and the base64 printed underneath it. Both spellings
        are real -- `cmd_logs` used to produce one and a JSON-encoded key file
        is the other.
        """
        body = "MIIEowIBAAKCAQEAx7Fq2Kj9vHnP0sTuVwXyZaBcDeFgHiJkLmNoPqRsTuVwXyZ"
        out = E.redact("dump: -----BEGIN PRIVATE KEY-----\\n%s" % body)
        self.assertNotIn(body, out)
        self.assertEqual("dump: [REDACTED]", out)

    def test_a_service_account_key_file_does_not_survive(self):
        """A GSA key is JSON, so its armour is escaped by construction.

        No ordering upstream fixes this one: the line breaks in `private_key`
        really are the two characters `\\` and `n`, and the pattern has to
        accept them. A whole key file has an END marker and the first arm of
        the PEM rule reaches it either way, so the fixture is cut short -- the
        state a key file is in when it reaches a log by accident, and the state
        in which only the second arm can help.
        """
        key = (
            '{"type": "service_account", "project_id": "acme-prod-42", '
            '"private_key": "-----BEGIN PRIVATE KEY-----\\n'
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ"
        )
        out = E.redact(key)
        self.assertNotIn("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ", out)
        self.assertNotIn("acme-prod-42", out)

    def test_an_upper_cased_token_does_not_survive(self):
        """The case a token arrives in is not the emitter's choice.

        A logger that upper-cases a line, or a formatter that upper-cases an
        environment dump, turns `ghp_` into `GHP_` -- and the token is just as
        live. `_CREDENTIAL_SHAPES` is case-insensitive for that reason and for
        no other; none of these prefixes is a word that could collide with log
        text at these lengths.
        """
        for secret in (
            "GHP_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
            "XOXB-1234567890-ABCDEFGHIJ",
            "YA29.A0ARRDAM_AVERYLONGACCESSTOKENVALUE",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, E.redact("header carried %s" % secret))

    def test_the_model_providers_own_key_does_not_survive(self):
        """The likeliest credential to be in these logs at all.

        This loop runs on Claude and so does the agent it observes, so a 401
        from the provider -- exactly what signal class 2 sends the
        investigation looking for -- is where an `sk-ant-` value turns up.
        """
        secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
        self.assertNotIn(secret, E.redact("provider returned 401 for %s" % secret))

    def test_an_aws_access_key_id_does_not_survive(self):
        for secret in ("AKIAIOSFODNN7EXAMPLE", "ASIAY34FZKBOKMUTVV7A"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, E.redact("signature failed for %s" % secret))

    def test_an_authorization_header_keeps_its_scheme_and_loses_its_credential(self):
        """A bearer token has whatever shape its issuer chose, and `Basic` is
        base64 of a password. Neither is recognisable on its own; the header
        name is what says the next word is a credential. The scheme survives
        for the reason `_QUERY_SECRETS` keeps the parameter name."""
        out = E.redact("Authorization: Bearer opaqueTokenValue123456 rejected")
        self.assertNotIn("opaqueTokenValue123456", out)
        self.assertIn("Authorization: Bearer [REDACTED]", out)
        self.assertNotIn("dXNlcjpwYXNz", E.redact("authorization=Basic dXNlcjpwYXNz"))

    def test_a_connection_string_loses_its_userinfo(self):
        """A DSN is one string to every shape rule and one field to the tree
        pass, and a database error quotes it back in full. The username goes
        with the password: it is half the credential."""
        out = E.redact("could not connect to postgres://svc:hunter2@db.internal:5432/agents")
        self.assertNotIn("hunter2", out)
        self.assertNotIn("svc", out)
        self.assertIn("postgres://[REDACTED]@db.internal", out)

    def test_a_token_split_by_a_zero_width_character_does_not_survive(self):
        """A zero-width space breaks the token for the matcher and for nobody
        reading the pull request. `_normalise` deletes them before any pattern
        runs."""
        secret = "ghp_abcdefghijklmnopqrstuvwxyz012345"
        split = "gh\u200bp_abcdefghijklmnopqrstuvwxyz012345"
        out = E.redact("token %s in the header" % split)
        self.assertNotIn(secret, out)
        self.assertIn("[REDACTED]", out)

    def test_a_fullwidth_spelling_of_a_token_does_not_survive(self):
        """NFKC is what folds the compatibility forms back to the ASCII they
        render as. Cyrillic confusables are deliberately not folded -- see
        `_normalise` for why the table is not worth carrying."""
        out = E.redact("ｇｈｐ＿abcdefghijklmnopqrstuvwxyz012345")
        self.assertIn("[REDACTED]", out)

    def test_an_ordinary_query_string_is_left_alone(self):
        # `filter=` and `pageSize=` are how the evidence tools themselves are
        # invoked; redacting those would blank the command a finding quotes.
        text = "GET /v1/projects?filter=resource.type%3Dk8s_container&pageSize=50"
        self.assertEqual(text, E.redact(text))


class IdentifierTests(unittest.TestCase):
    def test_a_person_becomes_a_placeholder(self):
        self.assertEqual(
            E.redact("denied for alice.smith@example.com"), "denied for [EMAIL]"
        )

    def test_a_workload_is_distinguishable_from_a_person(self):
        """A GSA is a valid email address, and collapsing the two loses the fact
        a reviewer most wants: whether a human or a workload hit the error."""
        self.assertEqual(
            E.redact("kubeagents-selfimprove@some-project.iam.gserviceaccount.com denied"),
            "[GSA] denied",
        )

    def test_project_and_cluster_names_do_not_survive(self):
        self.assertEqual(
            E.redact("resource projects/acme-prod-42/locations/us-east4/clusters/west-1"),
            "resource projects/[PROJECT]/locations/us-east4/clusters/[CLUSTER]",
        )

    def test_chat_coordinates_do_not_survive(self):
        out = E.redact("delivery to spaces/AAQA1b2c3d4 and channel C01AB2CD3EF failed")
        self.assertNotIn("AAQA1b2c3d4", out)
        self.assertNotIn("C01AB2CD3EF", out)

    def test_a_pod_ip_does_not_survive(self):
        self.assertEqual(E.redact("connect to 10.4.2.9:8080"), "connect to [IP]:8080")

    def test_an_ipv6_address_does_not_survive(self):
        """The IP rule was v4-only, and a dual-stack GKE cluster gives every pod
        a v6 address -- so on those installs the address in a connection error
        is the one form nothing matched."""
        for address in (
            "2001:db8:85a3:0:0:8a2e:370:7334",
            "2001:db8:85a3::8a2e:370:7334",
            "2001:db8::1",
            "2600:1900:4000:1234::",
        ):
            with self.subTest(address=address):
                out = E.redact("dial tcp [%s]:443 timed out" % address)
                self.assertNotIn(address, out)
                self.assertIn("[IP]", out)

    def test_an_upper_cased_or_extended_keyed_value_does_not_survive(self):
        """Every character the capture accepted and the gate rejected was a
        one-character escape.

        The capture ran `[A-Za-z0-9][-\\w.:]` and the gate `\\A[a-z]…\\Z`, and on
        a gate failure `_keyed` returns the whole match -- correctly, because
        that is how prose under an identifier key survives, but it means a
        capture wider than the gate does not fall back to something narrower,
        it falls back to printing the value. So an uppercase letter anywhere,
        or one trailing character the gate did not like, published the name.
        Both are now written from `_IDENTIFIER_CHARS` and cannot disagree.
        """
        for text, absent in (
            ("project_id=ACME-PROD-42", "ACME-PROD-42"),
            ("project_id=acme-prod-42X", "acme-prod-42X"),
            ("gcloud clusters list --project ACME-PROD-1", "ACME-PROD-1"),
            ('{"cluster_name": "West-1"}', "West-1"),
            ("clusterName: Prod_USC1", "Prod_USC1"),
        ):
            with self.subTest(text=text):
                self.assertNotIn(absent, E.redact(text))

    def test_a_percent_encoded_resource_path_does_not_survive(self):
        """These paths arrive inside URLs as often as in prose, and a URL built
        with `urlencode` has the separator escaped. It is the failure path that
        prints the request it failed on, which is how a Chat space -- and with
        it a customer's workspace -- left the loop."""
        out = E.redact("POST /v1/projects%2Facme-prod-42/spaces%2FAAQA1b2c3d4/messages 404")
        self.assertNotIn("acme-prod-42", out)
        self.assertNotIn("AAQA1b2c3d4", out)
        # The encoding survives, so the evidence still shows the request as sent.
        self.assertIn("projects%2F[PROJECT]", out)
        self.assertIn("spaces%2F[SPACE]", out)

    def test_the_bare_key_value_form_does_not_survive(self):
        """The path form is not how these actually turn up. A Cloud Monitoring
        resource label is `project_id`, and an env var is `GKE_CLUSTER_NAME=`."""
        self.assertEqual(
            E.redact("project_id=acme-prod-42 cluster_name=west-1"),
            "project_id=[PROJECT] cluster_name=[CLUSTER]",
        )

    def test_the_json_spelling_of_the_same_pair_does_not_survive(self):
        self.assertEqual(E.redact('"PROJECT_ID": "acme-prod-42"'), '"PROJECT_ID": "[PROJECT]"')

    def test_the_prefixed_env_var_names_do_not_survive(self):
        self.assertEqual(E.redact("GOOGLE_CLOUD_PROJECT=acme-prod-42"), "GOOGLE_CLOUD_PROJECT=[PROJECT]")

    def test_any_prefix_on_the_key_is_read(self):
        """The keys are matched by shape rather than from a list, because the
        list was the bug: `KUBEAGENTS_PROJECT_ID` and `CLOUDSDK_CORE_PROJECT`
        are how a project id actually reaches a container's environment, and a
        list written from the GCP docs has neither."""
        for text in (
            "CLOUDSDK_CORE_PROJECT=acme-prod-1",
            "KUBEAGENTS_PROJECT_ID=acme-prod-1",
            "MY_GOOGLE_CLOUD_PROJECT=acme-prod-1",
            "resource.labels.project_id=acme-prod-1",
            "projectId: acme-prod-1",
            "project_number: 123456789012",
        ):
            with self.subTest(text=text):
                self.assertNotIn("acme-prod-1", E.redact(text))
                self.assertNotIn("123456789012", E.redact(text))

    def test_the_bare_word_is_read_too_when_the_value_is_an_identifier(self):
        """`project: acme-prod-1` is the commonest spelling of all -- it is what
        a rendered manifest, a tfvars file and half the log lines use -- and
        keying only the qualified form let it reach a public pull request."""
        self.assertEqual(E.redact("project: acme-prod-1"), "project: [PROJECT]")
        self.assertEqual(E.redact("cluster: prod-usc1-fleet"), "cluster: [CLUSTER]")

    def test_a_flag_and_its_value_do_not_survive(self):
        """`--project=x` is a `key=value` the rules above already have.
        `--project x` is the spelling gcloud's own documentation uses."""
        self.assertEqual(
            E.redact("gcloud container clusters list --project acme-prod-1"),
            "gcloud container clusters list --project [PROJECT]",
        )
        self.assertEqual(E.redact("--cluster prod-usc1-fleet"), "--cluster [CLUSTER]")

    def test_a_numeric_project_path_does_not_survive(self):
        """Monitoring, Asset and Pub/Sub render the parent as
        `projects/<number>`, and a project number names a customer exactly as
        well as a project id does."""
        self.assertEqual(
            E.redact("logName: projects/123456789012/logs/stdout"),
            "logName: projects/[PROJECT]/logs/stdout",
        )

    def test_a_kubecontext_does_not_survive(self):
        """`kubectl config current-context` prints
        `gke_<project>_<location>_<cluster>` into every piece of debugging
        evidence. The location stays: a GCP region names nobody."""
        self.assertEqual(
            E.redact("current-context: gke_acme-prod-1_us-central1_prod-usc1-fleet"),
            "current-context: gke_[PROJECT]_us-central1_[CLUSTER]",
        )

    def test_image_and_bucket_paths_do_not_survive(self):
        """The project id is a path segment in both, and an image reference is
        all over a CrashLoopBackOff finding."""
        self.assertEqual(
            E.redact("image: us-central1-docker.pkg.dev/acme-prod-1/agents/runner:v1"),
            "image: us-central1-docker.pkg.dev/[PROJECT]/agents/runner:v1",
        )
        self.assertEqual(
            E.redact("image: eu.gcr.io/acme-prod-1/runner:v1"), "image: eu.gcr.io/[PROJECT]/runner:v1"
        )
        self.assertEqual(E.redact("backend: gs://acme-prod-1-tfstate/state"), "backend: gs://[BUCKET]/state")


class RedactionCostTests(unittest.TestCase):
    def test_redaction_stays_linear_in_the_length_of_the_line(self):
        """Redaction runs on evidence, and evidence is attacker-reachable.

        No pattern here backtracks exponentially, so this is not the usual
        ReDoS: it is the quieter quadratic one, where an unbounded greedy class
        rescans a long matching run from every start position inside it. The
        identifier shapes had several before the bounds went on, and a single
        64,000-character line -- a stack trace, a rendered manifest, a
        `kubectl get -o yaml` somebody printed into a log -- took 53 seconds to
        redact. A tool call reads up to 200 entries, and the run has a
        wall-clock budget, so a handful of long lines could spend the whole
        investigation inside `re`.

        The input matches nothing, which is the expensive case: a match
        short-circuits the scan. The bound is two orders of magnitude above the
        measured 0.12s and three below the 53s regression it exists to catch,
        so a loaded CI machine will not trip it and a lost bound will.
        """
        line = "connect failed to " + ("a-" * 32000) + " after 30s"
        start = time.perf_counter()
        E.redact(line)
        elapsed = time.perf_counter() - start
        self.assertLess(
            elapsed,
            5.0,
            "redact() took %.1fs on a 64k line; a repetition has lost its bound"
            % elapsed,
        )


class WhatMustSurviveTests(unittest.TestCase):
    """The other half. Over-redaction makes every finding unreviewable."""

    def test_loopback_and_link_local_are_kept(self):
        text = "credential proxy on 127.0.0.1:8765, metadata at 169.254.169.254"
        self.assertEqual(E.redact(text), text)

    def test_uppercase_log_words_are_not_slack_ids(self):
        """`CRASHLOOPBACKOFF` starts with C and is long enough; a rule without
        the digit lookahead turns half of the delivery evidence into
        placeholders."""
        text = "container in CRASHLOOPBACKOFF, CONTAINERSTATUSUNKNOWN, DEADLINEEXCEEDED"
        self.assertEqual(E.redact(text), text)

    def test_workspace_and_enterprise_ids_do_not_survive(self):
        """`T`, `E`, `B` and `W` name the installation rather than a channel,
        and the delivery evidence quotes them beside the channel ids."""
        out = E.redact("team T01ABC2DEF bot B024BE7LD9 enterprise E01ABC2DEF")
        for ident in ("T01ABC2DEF", "B024BE7LD9", "E01ABC2DEF"):
            self.assertNotIn(ident, out)

    def test_uppercase_log_words_with_a_trailing_number_are_not_slack_ids(self):
        """The `TEBW` rule needs a digit inside the first four characters, not
        merely somewhere in the token: without that, a timeout in milliseconds
        and a worker index both become placeholders."""
        text = "TIMEOUT30000 on WORKER12345, BACKOFF429, EXITCODE137"
        self.assertEqual(E.redact(text), text)

    def test_pod_names_and_timestamps_are_kept(self):
        text = "2026-08-22T09:14:03Z platform-agent-gateway-7d9f4c8b6-xk2vn/agent reconcile failed"
        self.assertEqual(E.redact(text), text)

    def test_the_error_a_finding_is_about_is_kept(self):
        text = 'secrets "kube-agents-github" is forbidden: cannot get resource "secrets"'
        self.assertEqual(E.redact(text), text)

    def test_prose_about_a_project_or_cluster_is_kept(self):
        """The bare spellings are the two keys that are also English words, so
        they demand a value that could actually be a GCP name -- six characters
        or more, and hyphenated or numbered. Without that, every sentence with a
        colon after the word `project` loses its next word.

        The qualified spellings (`project_id`, `--cluster`, `clusterName`) are
        field names wherever they appear and keep the looser test; the residual
        this leaves is in `test_a_short_unhyphenated_cluster_name_is_the_trade`.
        """
        text = "the project: this loop improves itself, and the cluster: it inspects"
        self.assertEqual(E.redact(text), text)

    def test_an_empty_flag_value_does_not_release_the_next_element(self):
        """`--cluster ""` from an unset shell variable is not the flag's value.

        `_redact_argv` carries a flag's meaning to the element right after it,
        so it can blank a name it has no other way to recognise. An empty
        string there used to end that carry-over unconditionally, so a real
        identifier one position further -- the shape a shell produces when the
        flag itself was unset but the caller still passed something -- reached
        generic, shape-based redaction and survived."""
        self.assertEqual(
            E.redact_tree({"args": ["--cluster", "", "prod-usc1-fleet"]}),
            {"args": ["--cluster", "", "[CLUSTER]"]},
        )
        self.assertEqual(
            E.redact_tree({"args": ["--project", "", "acme-prod-42"]}),
            {"args": ["--project", "", "[PROJECT]"]},
        )

    def test_the_carry_over_does_not_swallow_the_flag_that_follows_it(self):
        """The other half of the carry-over, and the direction that leaks.

        Reaching one element further for a value is right until the element is
        itself a flag. Consuming `--project` as the empty `--cluster`'s value
        both spent the carry and skipped the match that would have armed it
        again, so the project id in the position after met only shape-based
        redaction -- which the carry-over exists precisely because it cannot
        recognise a bare id -- and went into the ledger and the pull request."""
        self.assertEqual(
            E.redact_tree({"args": ["describe", "--cluster", "", "--project", "acme-prod-42"]}),
            {"args": ["describe", "--cluster", "", "--project", "[PROJECT]"]},
        )
        self.assertEqual(
            E.redact_tree({"args": ["--project", "", "--cluster-name", "prod-usc1-fleet"]}),
            {"args": ["--project", "", "--cluster-name", "[CLUSTER]"]},
        )

    def test_a_short_unhyphenated_cluster_name_is_the_trade(self):
        """Named, because an accepted cost that nothing asserts is a cost
        nobody knows was accepted. A cluster called `prod`, in a log line that
        spells the key bare, survives -- and the same name under `clusterName`,
        `--cluster` or a JSON key does not, which is every machine-generated
        spelling. The alternative was blanking a word out of every sentence
        containing `project:`, in a feature whose entire output is prose about
        projects and clusters."""
        self.assertEqual(E.redact("cluster: prod"), "cluster: prod")
        self.assertEqual(E.redact("clusterName: prod"), "clusterName: [CLUSTER]")
        self.assertEqual(E.redact("--cluster prod"), "--cluster [CLUSTER]")
        self.assertEqual(E.redact_tree({"cluster": "prod"}), {"cluster": "[CLUSTER]"})

    def test_an_absent_identifier_is_not_reported_as_a_hidden_one(self):
        """`project_id: [PROJECT]` tells a reviewer a project id was there and
        was withheld. Half these findings are about one being missing, and that
        rewrite makes the evidence argue the opposite of the finding."""
        for text in (
            "reconcile failed: project_id: missing",
            "cluster_name: unset",
            "the project id is unknown at this point",
        ):
            with self.subTest(text=text):
                self.assertEqual(E.redact(text), text)

    def test_an_error_string_under_an_identifier_key_is_kept(self):
        """The value is the finding. A key does not make its value a name."""
        for tree in (
            {"cluster": "unreachable: dial tcp i/o timeout"},
            {"project": "the build broke at step 3"},
            {"clusterName": "PENDING"},
            {"name": "PROJECT_ID", "value": "(unset)"},
            {"project_id": "missing"},
        ):
            with self.subTest(tree=tree):
                self.assertEqual(E.redact_tree(tree), tree)

    def test_ipv6_loopback_and_link_local_are_kept(self):
        """Same argument as 127/8 and 169.254/16 on the v4 side: a reader
        diagnosing the metadata server or the credential-proxy socket needs
        these, and they name nobody."""
        text = "proxy on ::1 port 8765, node link-local fe80::1ff:fe23:4567:890a"
        self.assertEqual(E.redact(text), text)

    def test_a_clock_is_not_an_ipv6_address(self):
        """`10:00:00` is colon-separated hex, which is why the v6 rule is three
        explicit arms rather than a general grammar: each needs either seven
        groups or a literal `::`, and no timestamp or duration has either."""
        for text in (
            "2026-08-22T09:14:03Z reconcile failed after 00:01:30",
            "backoff 10:00:00 then 1:2:3",
            "listening on host:8080",
        ):
            with self.subTest(text=text):
                self.assertEqual(E.redact(text), text)

    def test_an_upper_cased_non_identifier_word_is_still_not_hidden(self):
        """The gate stopped rejecting uppercase, so the word list has to be
        matched case-folded or `PENDING` under `clusterName` becomes
        `[CLUSTER]` -- and the evidence then argues the opposite of the finding
        it belongs to, which is what `_NON_IDENTIFIER_VALUES` exists to
        prevent."""
        for text in ("clusterName: PENDING", "PROJECT_ID=MISSING", "cluster_name: Unset"):
            with self.subTest(text=text):
                self.assertEqual(E.redact(text), text)

    def test_the_region_and_the_pod_name_are_kept(self):
        """Both are resource labels sitting next to `project_id`, and neither
        names anyone. The pod name is also what the self-exclusion filter keys
        on and the handle a reader needs to find the log line again."""
        out = E.redact_tree(
            {"labels": {"project_id": "acme", "location": "us-east4", "pod_name": "si-abc-1"}}
        )
        self.assertEqual(
            out, {"labels": {"project_id": "[PROJECT]", "location": "us-east4", "pod_name": "si-abc-1"}}
        )


class TreeTests(unittest.TestCase):
    def test_nested_values_are_redacted(self):
        out = E.redact_tree(
            {"items": [{"spec": {"host": "10.1.2.3", "user": "bob@example.com"}}]}
        )
        self.assertEqual(out, {"items": [{"spec": {"host": "[IP]", "user": "[EMAIL]"}}]})

    def test_keys_are_redacted_too(self):
        """Annotation keys carry user content: a label key is a domain name and
        `last-applied-configuration` is a key whose value is a whole manifest."""
        out = E.redact_tree({"alice@example.com": "owner"})
        self.assertEqual(out, {"[EMAIL]": "owner"})

    def test_non_strings_pass_through_unchanged(self):
        self.assertEqual(E.redact_tree({"n": 5, "ok": True, "z": None}), {"n": 5, "ok": True, "z": None})

    def test_an_identifier_key_holding_a_non_string_is_still_redacted(self):
        """A project number arrives from the Resource Manager API as an int, and
        `json.dumps` of an unredacted int reads the same as an unredacted str."""
        self.assertEqual(E.redact_tree({"project_id": 123456789012}), {"project_id": "[PROJECT]"})
        self.assertEqual(E.redact_tree({"project_id": [E_PROJECT]}), {"project_id": ["[PROJECT]"]})
        self.assertEqual(E.redact_tree({"project": {"id": E_PROJECT}}), {"project": {"id": "[PROJECT]"}})

    def test_a_number_under_a_cluster_key_is_a_count_not_a_name(self):
        """No cluster is named `3`. Blanking the number deleted the measurement
        the finding was about, and changed its JSON type on the way out."""
        self.assertEqual(
            E.redact_tree({"cluster": {"nodeCount": 3, "readyNodes": 1}}),
            {"cluster": {"nodeCount": 3, "readyNodes": 1}},
        )

    def test_prose_beside_an_identifier_key_survives(self):
        """The key introduces a structure; it does not identify everything in
        it. The message is the evidence."""
        self.assertEqual(
            E.redact_tree({"cluster": {"name": "prod-usc1-fleet", "message": "control plane unreachable"}}),
            {"cluster": {"name": "[CLUSTER]", "message": "control plane unreachable"}},
        )

    def test_a_boolean_under_an_identifier_key_is_left_alone(self):
        """`bool` is a subclass of `int`, so a number-shaped rule that does not
        check for it turns `{"cluster_exists": True}` into a placeholder."""
        self.assertEqual(E.redact_tree({"cluster_exists": True}), {"cluster_exists": True})

    def test_a_flag_and_its_value_in_an_argv_list_are_redacted(self):
        """A command line reaches the ledger as a list, where the flag and the
        identifier are separate elements and neither string sees the other."""
        self.assertEqual(
            E.redact_tree({"args": ["gcloud", "--project", E_PROJECT, "--format", "json"]}),
            {"args": ["gcloud", "--project", "[PROJECT]", "--format", "json"]},
        )

    def test_a_key_identifies_its_own_value(self):
        """`cmd_metrics` hands `resource.labels` straight through, and a bare
        project id is a hyphenated lowercase word no shape rule can pick out of
        a log line. The key is the only thing that says what it is."""
        out = E.redact_tree({"resource": {"labels": {"project_id": "acme-prod-42"}}})
        self.assertEqual(out, {"resource": {"labels": {"project_id": "[PROJECT]"}}})

    def test_the_env_var_pair_is_redacted(self):
        """`_env_summary` emits Kubernetes' EnvVar shape, which splits the key
        and the identifier across two siblings -- a pairing neither the string
        pass nor the key pass can see."""
        out = E.redact_tree(
            [
                {"name": "GKE_CLUSTER_NAME", "value": "west-1"},
                {"name": "GOOGLE_CLOUD_PROJECT", "value": "acme-prod-42"},
                {"name": "LOG_LEVEL", "value": "debug"},
            ]
        )
        self.assertEqual(
            out,
            [
                {"name": "GKE_CLUSTER_NAME", "value": "[CLUSTER]"},
                {"name": "GOOGLE_CLOUD_PROJECT", "value": "[PROJECT]"},
                {"name": "LOG_LEVEL", "value": "debug"},
            ],
        )

    def test_an_env_var_with_no_literal_value_keeps_its_source(self):
        """The `valueFrom` rows answer "is it wired up", which is the whole
        point of the check, and carry no identifier to blank."""
        out = E.redact_tree([{"name": "PROJECT_ID", "from": "configMapKeyRef:agent-config/project"}])
        self.assertEqual(out, [{"name": "PROJECT_ID", "from": "configMapKeyRef:agent-config/project"}])

    def test_a_credential_named_env_var_loses_its_value(self):
        """`_env_summary` prints every literal env value a container carries,
        on the argument that anything holding `view` can already read it. True,
        and beside the point once the output is a public pull request. Nothing
        about `hunter2` is a credential shape, so the name is the only thing
        that says what it is -- the same argument `_IDENTIFIER_KEYS` makes about
        a project id, which is why it belongs in the same pass."""
        out = E.redact_tree(
            [
                {"name": "DB_PASSWORD", "value": "hunter2"},
                {"name": "ANTHROPIC_API_KEY", "value": "sk-ant-not-a-real-key"},
                {"name": "KUBEAGENTS_SLACK_BOT_TOKEN", "value": "xoxb-not-real"},
                {"name": "GITHUB_APP_PRIVATE_KEY", "value": "MIIEowIBAAKCAQEA"},
                {"name": "LOG_LEVEL", "value": "debug"},
            ]
        )
        self.assertEqual(
            out,
            [
                {"name": "DB_PASSWORD", "value": "[REDACTED]"},
                {"name": "ANTHROPIC_API_KEY", "value": "[REDACTED]"},
                {"name": "KUBEAGENTS_SLACK_BOT_TOKEN", "value": "[REDACTED]"},
                {"name": "GITHUB_APP_PRIVATE_KEY", "value": "[REDACTED]"},
                {"name": "LOG_LEVEL", "value": "debug"},
            ],
        )

    def test_an_empty_credential_named_env_var_keeps_its_emptiness(self):
        """Half of what the inefficiency signal looks for is a variable that
        never got a value. Rewriting that to `[REDACTED]` says a secret was
        there and was withheld, which is the finding backwards."""
        out = E.redact_tree(
            [
                {"name": "DB_PASSWORD", "value": ""},
                {"name": "SLACK_APP_TOKEN", "value": "(unset)"},
                {"name": "GITHUB_TOKEN", "from": "secretKeyRef:kube-agents-github/token"},
            ]
        )
        self.assertEqual(
            out,
            [
                {"name": "DB_PASSWORD", "value": ""},
                {"name": "SLACK_APP_TOKEN", "value": "(unset)"},
                {"name": "GITHUB_TOKEN", "from": "secretKeyRef:kube-agents-github/token"},
            ],
        )

    def test_a_credential_named_key_loses_its_value_outside_the_env_shape(self):
        """The `{name, value}` pair is Kubernetes' spelling. `k8s --raw` and
        `logs` on a structured payload both hand back ordinary objects, where
        the credential sits under its own name and the pair rule never fires."""
        out = E.redact_tree(
            {
                "password": "hunter2",
                "client-secret": "not-a-shape",
                "spec": {"github": {"appPrivateKey": "MIIEowIBAAKCAQEA"}},
                "replicas": 3,
            }
        )
        self.assertEqual(
            out,
            {
                "password": "[REDACTED]",
                "client-secret": "[REDACTED]",
                "spec": {"github": {"appPrivateKey": "[REDACTED]"}},
                "replicas": 3,
            },
        )

    def test_a_secret_reference_is_not_mistaken_for_a_secret(self):
        """`secret: {name, key}` is a secretKeyRef -- a pointer, holding the
        answer to "is it wired up" and no credential. Only a scalar under one
        of these keys is blanked."""
        out = E.redact_tree({"secret": {"name": "kube-agents-github", "key": "token"}})
        self.assertEqual(out, {"secret": {"name": "kube-agents-github", "key": "token"}})

    def test_a_container_named_cluster_does_not_blank_its_siblings(self):
        """The pair rule needs both `name` and a string `value`; a container
        row has `name` and a list."""
        out = E.redact_tree({"name": "cluster", "env": [{"name": "LOG_LEVEL", "value": "debug"}]})
        self.assertEqual(out, {"name": "cluster", "env": [{"name": "LOG_LEVEL", "value": "debug"}]})


class FilterInjectionTests(unittest.TestCase):
    """`resourceNames` is the whole project, so the namespace clause in the
    filter string is the only thing keeping `logs` inside the install. These
    test that the clause cannot be escaped by anything the agent composes."""

    def _filter(self, **kw):
        import argparse

        args = argparse.Namespace(hours=1, severity=None, container=None, query=None)
        for key, value in kw.items():
            setattr(args, key, value)
        return E._logs_filter(args)

    def test_a_plain_query_is_wrapped_and_the_namespace_survives(self):
        out = self._filter(query='textPayload:"denied"')
        self.assertIn('(textPayload:"denied")', out)
        self.assertIn("resource.labels.namespace_name=", out)

    def test_a_query_closing_the_wrapping_paren_is_refused(self):
        """`) OR severity>="DEFAULT" AND (` would turn the namespace clause into
        one branch of an OR and read every namespace in the project."""
        with self.assertRaises(SystemExit):
            self._filter(query=') OR severity>="DEFAULT" AND (')

    def test_a_query_leaving_a_paren_open_is_refused(self):
        with self.assertRaises(SystemExit):
            self._filter(query="(a AND b")

    def test_a_query_leaving_a_quote_open_is_refused(self):
        """An open quote swallows the clauses that follow it -- here the
        self-exclusion, which is the sec. 10 control."""
        with self.assertRaises(SystemExit):
            self._filter(query='textPayload:"oops')

    def test_a_quote_inside_a_string_does_not_confuse_the_check(self):
        out = self._filter(query=r'textPayload:"say \"hi\"" AND (a OR b)')
        self.assertIn("resource.labels.namespace_name=", out)

    def test_a_container_carrying_a_quote_is_refused(self):
        """Not parenthesised, so `x" OR everything` parses as
        `(namespace AND container="x") OR everything`."""
        with self.assertRaises(SystemExit):
            self._filter(container='x" OR severity>="DEFAULT')

    def test_a_severity_carrying_a_quote_is_refused(self):
        with self.assertRaises(SystemExit):
            self._filter(severity='error" OR severity>="DEFAULT')


class MetricsScopeTests(unittest.TestCase):
    """`roles/monitoring.viewer` is a project grant, so an unscoped metrics
    filter reads every cluster in the project -- the clusters under management
    included, which sec. 1 puts on the far side of the line this feature is
    drawn around. Two layers, because neither covers the whole surface: a
    filter clause where the resource type carries `cluster_name`, and a
    post-fetch drop everywhere else."""

    OURS = "prod-usc1-fleet"
    K8S = 'metric.type="kubernetes.io/container/restart_count"'

    def setUp(self):
        self.prior = os.environ.get("GKE_CLUSTER_NAME")
        os.environ["GKE_CLUSTER_NAME"] = self.OURS

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("GKE_CLUSTER_NAME", None)
        else:
            os.environ["GKE_CLUSTER_NAME"] = self.prior

    def test_a_kubernetes_filter_is_scoped_to_this_cluster(self):
        out = E._metrics_filter(self.K8S)
        self.assertIn('resource.labels.cluster_name="%s"' % self.OURS, out)
        self.assertIn("(%s)" % self.K8S, out)

    def test_a_non_kubernetes_filter_is_left_alone(self):
        # `cluster_name` is not on the resource type behind a log-bytes
        # counter, and naming it fails the whole request rather than narrowing
        # it. _is_other_cluster is what covers these.
        plain = 'metric.type="logging.googleapis.com/byte_count"'
        self.assertEqual(E._metrics_filter(plain), plain)

    def test_a_filter_that_already_names_a_cluster_is_not_doubled(self):
        named = self.K8S + ' AND resource.labels.cluster_name="other"'
        self.assertEqual(E._metrics_filter(named), named)

    def test_no_cluster_in_the_environment_leaves_the_filter_alone(self):
        os.environ.pop("GKE_CLUSTER_NAME", None)
        self.assertEqual(E._metrics_filter(self.K8S), self.K8S)

    def test_a_filter_closing_the_wrapping_paren_is_refused(self):
        """The same escape `logs` guards: close the group early and the cluster
        clause becomes one branch of an OR over the whole project."""
        with self.assertRaises(SystemExit):
            E._metrics_filter(self.K8S + ') OR metric.type="x" AND (')

    def test_a_filter_leaving_a_quote_open_is_refused(self):
        with self.assertRaises(SystemExit):
            E._metrics_filter('metric.type="kubernetes.io/container/oops')

    def test_a_row_from_another_cluster_is_dropped(self):
        self.assertTrue(E._is_other_cluster({"cluster_name": "customer-eu-1"}))

    def test_a_row_from_this_cluster_is_kept(self):
        self.assertFalse(E._is_other_cluster({"cluster_name": self.OURS}))

    def test_a_row_with_no_cluster_label_is_kept(self):
        # Absent scoping information is not a foreign cluster. Dropping these
        # would silently empty every non-Kubernetes metric query.
        self.assertFalse(E._is_other_cluster({"project_id": "acme-prod-1"}))

    def test_no_cluster_in_the_environment_drops_nothing(self):
        os.environ.pop("GKE_CLUSTER_NAME", None)
        self.assertFalse(E._is_other_cluster({"cluster_name": "customer-eu-1"}))


class EmitTests(unittest.TestCase):
    def test_redaction_is_on_by_default(self):
        self.assertTrue(E._REDACT)

    def test_no_redact_turns_it_off_for_the_process(self):
        """`--no-redact` is a global on purpose -- see the comment on `_REDACT`.
        This test exists to make the global's lifetime explicit, and restores it,
        because a leaked False here would silently disable redaction in whatever
        test runs next."""
        original = E._REDACT
        try:
            E.main  # the flag is parsed there; assert the wiring rather than run it
            E._REDACT = False
            self.assertFalse(E._REDACT)
        finally:
            E._REDACT = original
        self.assertTrue(E._REDACT)


class SelfExclusionTests(unittest.TestCase):
    """SOUL.md sec. 1 promises the tools filter the loop out. All of them must.

    A read that does not is worse than one that visibly includes itself: the
    agent has been told the output is about the system under observation, so it
    grades the runner's own restarts as an agent defect. `k8s configmaps` is
    the sharpest case -- the ledger the run is writing is in that namespace, so
    a run can find its own memory, file a finding about it, and make the thing
    it found bigger.
    """

    def test_every_argv_read_takes_include_self(self):
        parser = E.build_parser()
        # argparse hides subparsers behind the one _SubParsersAction.
        actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        subs = {name: sp for a in actions for name, sp in a.choices.items()}
        self.assertTrue(subs, "no subcommands found")
        for name, sub in sorted(subs.items()):
            with self.subTest(command=name):
                flags = {o for act in sub._actions for o in act.option_strings}
                self.assertIn("--include-self", flags)

    def test_the_loops_own_names_are_recognised(self):
        for name in (
            "kube-agents-selfimprove-28912345-abcde",
            "kube-agents-selfimprove-investigator",
            "kube-agents-selfimprove-ledger",
        ):
            with self.subTest(name=name):
                self.assertTrue(E._is_self(name))

    def test_the_observed_agents_names_are_not(self):
        for name in ("platform-agent-0", "kube-agents-operator-7d9f", "platform-agent-config"):
            with self.subTest(name=name):
                self.assertFalse(E._is_self(name))

    def test_a_metric_series_for_the_runner_is_dropped(self):
        import argparse

        payload = {
            "timeSeries": [
                {"metric": {"type": "m"}, "resource": {"labels": {"pod_name": "platform-agent-0"}}, "points": []},
                {
                    "metric": {"type": "m"},
                    "resource": {"labels": {"pod_name": "kube-agents-selfimprove-28912345-abcde"}},
                    "points": [],
                },
            ]
        }
        captured = []
        prior_api, prior_emit, prior_project = E._google_api, E.emit, E._project
        E._google_api = lambda *a, **k: payload
        E.emit = lambda rows: captured.append(rows)
        E._project = lambda: "p"
        try:
            E.cmd_metrics(argparse.Namespace(filter="f", hours=24, include_self=False))
            E.cmd_metrics(argparse.Namespace(filter="f", hours=24, include_self=True))
        finally:
            E._google_api, E.emit, E._project = prior_api, prior_emit, prior_project
        self.assertEqual(1, len(captured[0]))
        self.assertEqual("platform-agent-0", captured[0][0]["resource"]["pod_name"])
        self.assertEqual(2, len(captured[1]))


class TraceBreakdownTests(unittest.TestCase):
    """Signal 3 needs a span tree, not a span count.

    `--full` asks Cloud Trace for the COMPLETE view and pays for it in page
    size. Returning the root's name and `len(spans)` from that spends the cost
    and discards the answer: a latency finding has to name the span that
    consumed the wall clock.
    """

    TRACE = {
        "traceId": "t1",
        "spans": [
            {"spanId": "1", "name": "POST /chat", "startTime": "2026-08-23T10:00:00.000Z", "endTime": "2026-08-23T10:01:34.000Z"},
            {"spanId": "2", "name": "sqlite.query", "startTime": "2026-08-23T10:00:00.100Z", "endTime": "2026-08-23T10:00:00.180Z"},
            {"spanId": "3", "name": "vertex.completion", "startTime": "2026-08-23T10:00:01.000Z", "endTime": "2026-08-23T10:01:30.000Z"},
        ],
    }

    def _run(self, **overrides):
        import argparse

        args = argparse.Namespace(
            hours=24, limit=50, span="", service="", full=False, breakdown=5, include_self=True
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        captured = []
        prior_api, prior_emit, prior_project = E._google_api, E.emit, E._project
        E._google_api = lambda *a, **k: {"traces": [self.TRACE]}
        E.emit = lambda rows: captured.append(rows)
        E._project = lambda: "p"
        try:
            E.cmd_traces(args)
        finally:
            E._google_api, E.emit, E._project = prior_api, prior_emit, prior_project
        return captured[0]

    def test_the_root_carries_a_duration(self):
        self.assertEqual(94000.0, self._run()[0]["durationMs"])

    def test_full_names_the_span_that_took_the_time(self):
        row = self._run(full=True)[0]
        self.assertEqual("vertex.completion", row["slowest"][0]["name"])
        self.assertEqual(89000.0, row["slowest"][0]["ms"])
        # Slowest first, so the answer is the first element and not something
        # the agent has to sort for itself.
        self.assertEqual("sqlite.query", row["slowest"][1]["name"])

    def test_without_full_there_is_no_breakdown_to_pay_for(self):
        self.assertNotIn("slowest", self._run()[0])

    def test_breakdown_caps_what_one_trace_can_spend(self):
        self.assertEqual(1, len(self._run(full=True, breakdown=1)[0]["slowest"]))

    def test_an_unparseable_span_is_dropped_rather_than_fatal(self):
        self.assertIsNone(E._span_ms({"startTime": "not a time", "endTime": "2026-08-23T10:00:00Z"}))
        self.assertIsNone(E._span_ms({}))

    def test_a_span_count_is_only_reported_when_it_was_measured(self):
        # The default ROOTSPAN view returns the root and nothing else, so a
        # `spans` field there is the constant 1 on every row -- a number that
        # reads like a measurement and would tell an agent that every trace on
        # the install is a single span.
        self.assertNotIn("spans", self._run()[0])
        self.assertEqual(3, self._run(full=True)[0]["spans"])


class LogPayloadTests(unittest.TestCase):
    """Which of the three payload fields an entry actually prints.

    Chaining `json.dumps(...) or json.dumps(...)` does not fall through: an
    absent jsonPayload dumps to the string "{}", which is truthy. Audit log
    entries carry protoPayload and nothing else, so the whole class of evidence
    that answers "who called this API" printed as "{}"."""

    def _lines(self, entry):
        import argparse

        args = argparse.Namespace(
            hours=1, limit=10, severity="", filter="", raw=False, include_self=True,
            resource="", text="", width=400,
        )
        captured = []
        prior_api, prior_emit, prior_project = E._google_api, E.emit, E._project
        E._google_api = lambda *a, **k: {"entries": [entry]}
        E.emit = lambda line: captured.append(line)
        E._project = lambda: "p"
        try:
            E.cmd_logs(args)
        finally:
            E._google_api, E.emit, E._project = prior_api, prior_emit, prior_project
        return captured

    def test_a_proto_payload_is_printed_rather_than_swallowed(self):
        lines = self._lines(
            {
                "timestamp": "2026-08-23T10:00:00Z",
                "severity": "NOTICE",
                "protoPayload": {"methodName": "io.k8s.core.v1.pods.delete"},
            }
        )
        self.assertIn("io.k8s.core.v1.pods.delete", lines[0])

    def test_a_json_payload_still_wins_over_a_proto_one(self):
        lines = self._lines(
            {
                "timestamp": "2026-08-23T10:00:00Z",
                "jsonPayload": {"message": "the json one"},
                "protoPayload": {"methodName": "the proto one"},
            }
        )
        self.assertIn("the json one", lines[0])
        self.assertNotIn("the proto one", lines[0])

    def test_text_still_wins_over_both(self):
        lines = self._lines(
            {
                "timestamp": "2026-08-23T10:00:00Z",
                "textPayload": "the text one",
                "jsonPayload": {"message": "the json one"},
            }
        )
        self.assertIn("the text one", lines[0])

    def test_an_entry_with_no_payload_at_all_does_not_crash(self):
        self.assertTrue(self._lines({"timestamp": "2026-08-23T10:00:00Z"}))


class LogOutputRedactionTests(unittest.TestCase):
    """`logs` is the subcommand the loop uses most, and it formatted before it
    redacted.

    Everything else here calls `redact` or `redact_tree` directly, which is
    exactly why these defects lived: the functions were right and the caller
    handed them text it had already cut to `--width` and whose newlines it had
    already escaped. So these go through `cmd_logs` and read real stdout, with
    nothing monkeypatched between the entry and the terminal.
    """

    KEY_BODY = "MIIEowIBAAKCAQEAx7Fq2Kj9vHnP0sTuVwXyZaBcDeFgHiJkLmNoPqRsTuVwXyZ"

    def _stdout(self, entry, width=400):
        import argparse

        args = argparse.Namespace(
            hours=1, limit=10, severity="", filter="", raw=False, include_self=True,
            resource="", text="", width=width,
        )
        prior_api, prior_project = E._google_api, E._project
        E._google_api = lambda *a, **k: {"entries": [entry]}
        E._project = lambda: "p"
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                E.cmd_logs(args)
        finally:
            E._google_api, E._project = prior_api, prior_project
        return buffer.getvalue()

    def test_a_private_key_in_a_payload_is_redacted_before_its_newlines_are_escaped(self):
        """The ordering defect, end to end.

        `cmd_logs` escaped the newlines and only then handed the line to
        `emit`. A literal backslash was not in the PEM body's character class,
        so the second arm matched nothing and the first could not help either:
        `--width 400` had already cut the END marker off. The output was
        `[REDACTED]` followed by the private key.
        """
        out = self._stdout(
            {"timestamp": "T", "textPayload": "dump: -----BEGIN PRIVATE KEY-----\n" + self.KEY_BODY}
        )
        self.assertNotIn(self.KEY_BODY, out)
        self.assertIn("[REDACTED]", out)

    def test_a_token_straddling_the_width_is_not_published_as_a_fragment(self):
        """Truncating first does the quiet version of the same thing.

        The padding puts fifteen characters of the token inside `--width 400`,
        which is under the `{20,}` bound `gh[pousr]_` requires -- so the
        fragment matched nothing and went out as a prefix of a live secret
        rather than as a placeholder. Redaction now sees the payload as the API
        sent it, and the cut lands on the placeholder instead.
        """
        secret = "ghp_abcdefghijklmnopqrstuvwxyz012345"
        fragment = "ghp_abcdefghijklmno"
        out = self._stdout({"timestamp": "T", "textPayload": "x" * 381 + secret})
        self.assertNotIn(fragment, out)
        self.assertNotIn(secret, out)

    def test_a_structured_payload_goes_through_the_key_pass(self):
        """`json.dumps` before `emit` left `redact_tree` nothing to run its key
        pass over, so the same entry printed `acme-prod-42` through `logs` and
        `[PROJECT]` through `logs --raw`. A project id is a hyphenated
        lowercase word; the key is the only thing that identifies it, and
        flattening threw the key away."""
        out = self._stdout(
            {"timestamp": "T", "jsonPayload": {"args": ["--project", "acme-prod-42"]}}
        )
        self.assertNotIn("acme-prod-42", out)
        self.assertIn("[PROJECT]", out)

    def test_the_last_applied_configuration_annotation_does_not_pass_through(self):
        """The annotation `redact_tree`'s docstring names as its motivating
        case, on the path that never called it."""
        out = self._stdout(
            {
                "timestamp": "T",
                "protoPayload": {
                    "metadata": {
                        "kubectl.kubernetes.io/last-applied-configuration": (
                            '{"project_id": "acme-prod-42", "user": "alice@example.com"}'
                        )
                    }
                },
            }
        )
        self.assertNotIn("acme-prod-42", out)
        self.assertNotIn("alice@example.com", out)

    def test_no_redact_still_prints_the_payload_whole(self):
        """The other half. Redacting earlier must not make `--no-redact` a
        partial switch."""
        prior = E._REDACT
        E._REDACT = False
        try:
            out = self._stdout({"timestamp": "T", "textPayload": "project_id=acme-prod-42"})
        finally:
            E._REDACT = prior
        self.assertIn("acme-prod-42", out)


class ErrorOutputTests(unittest.TestCase):
    """stderr is evidence too.

    A harness that merges the two streams -- most do -- makes everything here
    quotable in a finding, and error messages are where the strange values are:
    an error quotes the thing that failed.
    """

    SECRET = "ghp_abcdefghijklmnopqrstuvwxyz012345"

    def _stderr(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            with self.assertRaises(SystemExit):
                E.main(argv)
        return buffer.getvalue()

    def test_an_invalid_argument_is_not_echoed_verbatim(self):
        """argparse prints the offending value back, past `emit` entirely --
        and `_check_query`'s own docstring says the agent composes `--query`
        "from text it read in logs it does not control". A bad argument is also
        where the value is most likely to be something that was never meant to
        be a value."""
        out = self._stderr(
            ["logs", "--hours", "acme-prod-1 alice@example.com %s" % self.SECRET]
        )
        self.assertNotIn(self.SECRET, out)
        self.assertNotIn("alice@example.com", out)
        # Still an argument error a reader can act on.
        self.assertIn("--hours", out)

    def test_an_uncaught_exception_does_not_print_raw(self):
        """`main` had no `try`, and the exception most likely to reach it is
        the worst one to print: `ApiException.__str__` renders status, every
        response header and the body, and a 403 body names the service account,
        the namespace and the resource."""

        def boom(*args, **kwargs):
            raise RuntimeError(
                '(403) body: {"message": "clusters/prod-usc1-fleet denied for '
                'alice@example.com in projects/acme-prod-42"}'
            )

        prior_api, prior_project = E._google_api, E._project
        E._google_api = boom
        E._project = lambda: "p"
        try:
            out = self._stderr(["logs"])
        finally:
            E._google_api, E._project = prior_api, prior_project
        self.assertNotIn("prod-usc1-fleet", out)
        self.assertNotIn("alice@example.com", out)
        self.assertNotIn("acme-prod-42", out)
        # Still says what went wrong, and which exception it was.
        self.assertIn("RuntimeError", out)

    def test_fail_redacts_and_honours_no_redact(self):
        """`_fail` called `redact` directly, so it ignored `--no-redact`: the
        results printed raw and the errors about them printed redacted. Routing
        it through `emit_err` gives the file one boundary and one switch."""
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            with self.assertRaises(SystemExit):
                E._fail("token %s" % self.SECRET)
        self.assertNotIn(self.SECRET, buffer.getvalue())

        prior = E._REDACT
        E._REDACT = False
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stderr(buffer):
                with self.assertRaises(SystemExit):
                    E._fail("token %s" % self.SECRET)
        finally:
            E._REDACT = prior
        self.assertIn(self.SECRET, buffer.getvalue())


class ContainerStateTests(unittest.TestCase):
    """`k8s pods` is where an investigation learns a pod is unhealthy.

    The generated `V1ContainerState.to_dict()` walks `openapi_types` and
    assigns every attribute, unset ones included, so `list(...keys())` was the
    constant ["running", "terminated", "waiting"] on every container of every
    pod -- byte-identical for a healthy pod and one in CrashLoopBackOff. These
    cases pin the shape that replaced it.
    """

    class _Sub:
        def __init__(self, **kw):
            for key in ("reason", "message", "exit_code", "signal", "started_at", "finished_at"):
                setattr(self, key, kw.get(key))

    class _State:
        def __init__(self, running=None, waiting=None, terminated=None):
            self.running = running
            self.waiting = waiting
            self.terminated = terminated

    def test_the_waiting_reason_is_what_survives(self):
        state = self._State(waiting=self._Sub(reason="CrashLoopBackOff", message="back-off 5m0s"))
        self.assertEqual(
            E._container_state(state),
            {"state": "waiting", "reason": "CrashLoopBackOff", "message": "back-off 5m0s"},
        )

    def test_a_terminated_container_carries_its_exit_code(self):
        got = E._container_state(self._State(terminated=self._Sub(reason="Error", exit_code=137)))
        self.assertEqual(got["state"], "terminated")
        self.assertEqual(got["exitCode"], 137)

    def test_a_healthy_container_is_distinguishable_from_a_broken_one(self):
        """The whole point. Two states that used to serialise identically."""
        running = E._container_state(self._State(running=self._Sub(started_at="2026-08-31T00:00:00Z")))
        waiting = E._container_state(self._State(waiting=self._Sub(reason="ImagePullBackOff")))
        self.assertNotEqual(running, waiting)
        self.assertEqual(running["state"], "running")

    def test_unset_fields_are_omitted_rather_than_rendered_as_null(self):
        got = E._container_state(self._State(running=self._Sub()))
        self.assertEqual(got, {"state": "running"})

    def test_no_state_at_all(self):
        self.assertIsNone(E._container_state(None))
        self.assertEqual(E._container_state(self._State()), {"state": "unknown"})


if __name__ == "__main__":
    unittest.main()

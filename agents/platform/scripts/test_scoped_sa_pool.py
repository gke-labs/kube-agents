"""The scoped service account pool: what it selects, and what it refuses.

The refusal is the part worth testing hardest. A pool that hands out the right
account for a mapped cluster and quietly falls back to the ambient credential
for an unmapped one looks identical in every log line and every green test —
the ordinary read still works, which is exactly why the failure is invisible.
So every case below that asserts a selection has a sibling that asserts a
refusal, and the refusal cases assert the *reason*, not just that something
went wrong.

Run:
  python3 -m unittest discover -s agents/platform/scripts -p 'test_scoped_sa_pool.py' -v
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

import credential_proxy
import scoped_sa_pool
from scoped_sa_pool import (
    PoolConfigurationError,
    PoolMember,
    PoolRefusal,
    ScopedServiceAccountPool,
    build_pool,
    kubeconfig_with_token,
    load_pool_file,
    parse_pool,
    pool_enabled,
    scope_key,
)

PROJECT = "bnaylor-kagents-dev"
LOCATION = "us-east4"
CLUSTER = "bnaylor-ka-test"
OTHER_CLUSTER = "some-other-cluster"
EMAIL = "ka-bnaylor-ka-tes-1a2b3c4d@bnaylor-kagents-dev.iam.gserviceaccount.com"
OTHER_EMAIL = "ka-some-other-clu-99887766@bnaylor-kagents-dev.iam.gserviceaccount.com"


def document(*entries: tuple[str, str, str, str]) -> dict:
    return {
        "version": 1,
        "serviceAccounts": [
            {
                "projectId": project,
                "location": location,
                "clusterName": cluster,
                "serviceAccountEmail": email,
            }
            for project, location, cluster, email in entries
        ],
    }


def one_cluster_pool(minter=None, **kwargs) -> ScopedServiceAccountPool:
    members = parse_pool(document((PROJECT, LOCATION, CLUSTER, EMAIL)))
    return ScopedServiceAccountPool(
        members,
        minter=minter or (lambda account, lifetime: (f"token-for-{account}", 1_000_000.0)),
        clock=lambda: 0.0,
        **kwargs,
    )


class ScopeKeyTest(unittest.TestCase):
    """The key is a GKE resource name, and its exact spelling is load-bearing."""

    def test_the_key_is_the_gke_resource_name(self):
        self.assertEqual(
            "projects/bnaylor-kagents-dev/locations/us-east4/clusters/bnaylor-ka-test",
            scope_key(PROJECT, LOCATION, CLUSTER),
        )

    # `test_the_condition_operand_is_the_key_verbatim` was deleted 2026-08-12,
    # along with the function it exercised.
    #
    # It called itself the single most important assertion in this file, and it
    # passed every time it ran. It compared what Terraform rendered against what
    # the broker rendered and found them identical -- which they were. Both
    # spelled `resource.name == "<key>"` perfectly, and that condition grants
    # nothing for a Kubernetes object operation.
    #
    # A test that compares two renderings of an expression can only tell you the
    # two halves agree. It cannot tell you the expression does anything. The
    # replacement lives in `tests/test_scoped_sa_pool_iam.py`: assert the grant is
    # absent from the Terraform, because absent is now the correct state and
    # reintroducing it is the regression worth catching.
    #
    # Assert the behaviour, never the artifact. This one asserted neither -- it
    # asserted two artifacts matched each other.

    def test_a_trailing_newline_cannot_pass_for_a_name_component(self):
        """`$` is not `\\Z`, and the difference reaches a log line and a filename.

        Python's `$` matches immediately before a trailing newline, so
        `re.match(r"^[a-z0-9-]*$", "cluster\\n")` succeeds. The component goes
        into the scope key, the scope key goes into the refusal message, and the
        refusal message goes into a WARNING -- one agent-written newline in a
        kubeconfig's `current-context` splits that into two log records. The
        same component also becomes part of a filename in the sidecar state dir.

        Separate from the test below, which covers separators and quotes: those
        were always refused. This one was not.
        """
        for value in ("cluster\n", "cluster\r\n", "cluster\r", "\ncluster"):
            for position in range(3):
                components = ["project", "location", "cluster"]
                components[position] = value
                with self.subTest(value=value, position=position):
                    with self.assertRaises(ValueError):
                        scope_key(*components)

    def test_a_component_that_could_change_the_key_is_refused(self):
        """Anything that could smuggle a separator or close the CEL string."""
        for project, location, cluster in (
            ("proj/ect", LOCATION, CLUSTER),
            (PROJECT, "us-east4/x", CLUSTER),
            (PROJECT, LOCATION, 'clus"ter'),
            (PROJECT, LOCATION, "../other"),
            (PROJECT, LOCATION, "-leading-hyphen"),
            (PROJECT, LOCATION, "UPPER"),
            (PROJECT, LOCATION, ""),
            (PROJECT, LOCATION, None),
        ):
            with self.subTest(cluster=cluster, project=project):
                with self.assertRaises(ValueError):
                    scope_key(project, location, cluster)

    def test_the_pool_and_the_broker_agree_on_what_a_name_component_is(self):
        """Two regexes, one idea — the shape every Critical here has had.

        `scoped_sa_pool` cannot import the broker (the broker imports it), so
        the GKE component pattern is written twice. This drives both with the
        same inputs and insists they answer the same, so a future edit to either
        one fails here rather than admitting a key the other half rejects.
        """
        candidates = [
            "abc",
            "a",
            "a-b-c",
            "us-east4",
            "0abc",
            "abc-",
            "-abc",
            "ABC",
            "a_b",
            "a.b",
            "a/b",
            "",
            "a b",
            'a"b',
            # The newline cases are here as well as in their own test above,
            # because the two patterns have to agree about them too -- fixing
            # `$` to `\\Z` in one file and not the other is the drift this
            # whole test exists for.
            "abc\n",
            "\nabc",
            "abc\r\n",
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertEqual(
                    bool(credential_proxy._GKE_CONTEXT_COMPONENT.fullmatch(candidate)),
                    bool(scoped_sa_pool._COMPONENT.fullmatch(candidate)),
                    f"the broker and the pool disagree about {candidate!r}",
                )
                # Also compared under `.match`, because that is how the pool
                # calls it and an anchor fixed only in one place shows up here.
                self.assertEqual(
                    bool(credential_proxy._GKE_CONTEXT_COMPONENT.match(candidate)),
                    bool(scoped_sa_pool._COMPONENT.match(candidate)),
                    f"the broker and the pool disagree about {candidate!r} under match()",
                )


class ParsePoolTest(unittest.TestCase):
    """The mapping is operator-authored config, and it fails loudly."""

    def test_a_well_formed_document_indexes_by_scope_key(self):
        members = parse_pool(document((PROJECT, LOCATION, CLUSTER, EMAIL)))
        self.assertEqual(
            {scope_key(PROJECT, LOCATION, CLUSTER): PoolMember(
                key=scope_key(PROJECT, LOCATION, CLUSTER), service_account=EMAIL
            )},
            members,
        )

    def test_a_repeated_scope_is_refused_rather_than_resolved(self):
        """Last-wins would make the answer depend on render order."""
        with self.assertRaises(PoolConfigurationError) as raised:
            parse_pool(
                document(
                    (PROJECT, LOCATION, CLUSTER, EMAIL),
                    (PROJECT, LOCATION, CLUSTER, OTHER_EMAIL),
                )
            )
        self.assertIn("repeats", str(raised.exception))

    def test_an_empty_list_is_refused_and_names_the_way_out(self):
        """Empty means every request refuses; that is a misconfiguration.

        The message has to name the flag, because the operator who hits this is
        the one who wanted the ambient credential and did not know how to ask.
        """
        with self.assertRaises(PoolConfigurationError) as raised:
            parse_pool({"version": 1, "serviceAccounts": []})
        self.assertIn(scoped_sa_pool.POOL_FLAG_ENV, str(raised.exception))

    def test_a_malformed_entry_is_refused(self):
        for broken in (
            {"version": 2, "serviceAccounts": []},
            {"version": 1},
            {"version": 1, "serviceAccounts": {}},
            {"version": 1, "serviceAccounts": ["not-an-object"]},
            "not a document",
            [],
        ):
            with self.subTest(broken=broken):
                with self.assertRaises(PoolConfigurationError):
                    parse_pool(broken)

    def test_an_email_that_is_not_a_service_account_is_refused(self):
        """The mistakes an operator makes by hand: a human, or the legacy default."""
        for email in (
            "bnaylor@google.com",
            "123-compute@developer.gserviceaccount.com",
            "not-an-email",
            "ka-x-1@example.com",
            "",
            None,
            123,
        ):
            with self.subTest(email=email):
                with self.assertRaises(PoolConfigurationError):
                    parse_pool(document((PROJECT, LOCATION, CLUSTER, email)))

    def test_the_pattern_cannot_tell_a_google_managed_service_agent_apart(self):
        """Recorded as a limit rather than left as an assumption.

        `container-engine-robot.iam.gserviceaccount.com` is shaped exactly like
        `<project-id>.iam.gserviceaccount.com`, so no pattern distinguishes a
        Google-managed service agent from a pool member. This asserts the gap
        deliberately, so that nobody later reads the email check as establishing
        where an entry came from.

        What actually establishes that is the write path: the file is a ConfigMap
        the operator renders from the PlatformAgent CR, mounted read-only, on a
        volume the agent cannot write. If this test ever starts failing because
        the pattern got stricter, that is fine — but the reasoning above is what
        the control rests on, not the regex.
        """
        members = parse_pool(
            document(
                (
                    PROJECT,
                    LOCATION,
                    CLUSTER,
                    "service-123@container-engine-robot.iam.gserviceaccount.com",
                )
            )
        )
        self.assertEqual(1, len(members))


class LoadPoolFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pool.json"

    def test_a_file_round_trips(self):
        self.path.write_text(
            json.dumps(document((PROJECT, LOCATION, CLUSTER, EMAIL))), encoding="utf-8"
        )
        self.assertEqual([scope_key(PROJECT, LOCATION, CLUSTER)], sorted(load_pool_file(self.path)))

    def test_a_missing_file_names_the_way_out(self):
        with self.assertRaises(PoolConfigurationError) as raised:
            load_pool_file(self.path)
        self.assertIn(scoped_sa_pool.POOL_FLAG_ENV, str(raised.exception))

    def test_junk_is_refused_rather_than_read_as_empty(self):
        self.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(PoolConfigurationError):
            load_pool_file(self.path)


class PoolFlagTest(unittest.TestCase):
    """Off by default, and on only when spelled on."""

    def test_the_pool_is_disarmed_when_nothing_says_otherwise(self):
        """Changed 2026-08-12; it used to be armed by default.

        A member holds no IAM grant now, because the condition scoping it was
        measured to grant nothing and the un-conditioned form grants every
        cluster in the project.  Armed, the broker would select a powerless
        identity for every request and every cluster read would come back
        Forbidden.  Fail-closed, and a full outage.

        This flips back with per-cluster RBAC, and the thing that earns it is a
        test showing a real read succeeding through the pool.
        """
        self.assertFalse(pool_enabled({}))

    def test_it_arms_when_asked(self):
        self.assertTrue(pool_enabled({scoped_sa_pool.POOL_FLAG_ENV: "1"}))

    def test_the_documented_off_values_disarm_it(self):
        for value in ("0", "false", "no", "off", "OFF", " false "):
            with self.subTest(value=value):
                self.assertFalse(pool_enabled({scoped_sa_pool.POOL_FLAG_ENV: value}))

    def test_a_typo_leaves_it_armed(self):
        """The rollback is a deliberate act, not a value nobody parsed."""
        for value in ("banana", "", "1", "true", "disabled", "no!"):
            with self.subTest(value=value):
                self.assertTrue(pool_enabled({scoped_sa_pool.POOL_FLAG_ENV: value}))


class BuildPoolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pool.json"
        self.path.write_text(
            json.dumps(document((PROJECT, LOCATION, CLUSTER, EMAIL))), encoding="utf-8"
        )

    def test_the_flag_off_is_the_only_route_to_the_ambient_credential(self):
        self.assertIsNone(
            build_pool({scoped_sa_pool.POOL_FLAG_ENV: "0"}),
        )

    def test_an_armed_pool_with_no_mapping_raises_rather_than_falling_back(self):
        """The whole point. A missing mount must not read as "use the wide one"."""
        with self.assertRaises(PoolConfigurationError):
            build_pool({
                scoped_sa_pool.POOL_FLAG_ENV: "1",
                scoped_sa_pool.POOL_FILE_ENV: str(self.path.parent / "absent.json"),
            })

    def test_the_mapping_is_read_from_the_configured_path(self):
        pool = build_pool({
            scoped_sa_pool.POOL_FLAG_ENV: "1",
            scoped_sa_pool.POOL_FILE_ENV: str(self.path),
        })
        self.assertEqual([scope_key(PROJECT, LOCATION, CLUSTER)], pool.scopes)


class SelectionTest(unittest.TestCase):
    """What the broker asks for, and what it gets."""

    def test_a_mapped_cluster_selects_its_own_account(self):
        pool = one_cluster_pool()
        self.assertEqual(EMAIL, pool.select(PROJECT, LOCATION, CLUSTER).service_account)

    def test_an_unmapped_cluster_is_refused(self):
        pool = one_cluster_pool()
        with self.assertRaises(PoolRefusal):
            pool.select(PROJECT, LOCATION, OTHER_CLUSTER)

    def test_the_refusal_names_the_scope_it_could_not_serve(self):
        """An operator hits this first; it has to say which key was missing."""
        pool = one_cluster_pool()
        with self.assertRaises(PoolRefusal) as raised:
            pool.select(PROJECT, LOCATION, OTHER_CLUSTER)
        self.assertIn(scope_key(PROJECT, LOCATION, OTHER_CLUSTER), str(raised.exception))

    def test_a_near_miss_does_not_select_a_neighbour(self):
        """Same cluster name, different project or location, is a different scope.

        This is the assertion that makes the pool cross-project-safe: if the key
        were the cluster name alone, a second project reusing a name would be
        served by the first project's account.
        """
        pool = one_cluster_pool()
        for project, location, cluster in (
            ("other-project", LOCATION, CLUSTER),
            (PROJECT, "us-west1", CLUSTER),
            (PROJECT, LOCATION, CLUSTER + "-2"),
            (PROJECT, LOCATION, "bnaylor-ka-tes"),
        ):
            with self.subTest(project=project, location=location, cluster=cluster):
                with self.assertRaises(PoolRefusal):
                    pool.select(project, location, cluster)

    def test_selection_takes_three_strings_and_nothing_a_caller_authored(self):
        """The scope is resolved, never supplied.

        `select` has no parameter a request body could reach — no dict, no
        headers, no account name. This asserts the signature stays that way,
        because the readable version of this control is the type of its
        arguments rather than a comment claiming payloads are ignored.
        """
        import inspect

        parameters = list(inspect.signature(ScopedServiceAccountPool.select).parameters)
        self.assertEqual(["self", "project", "location", "cluster"], parameters)


class TokenTest(unittest.TestCase):
    def test_a_token_is_minted_for_the_selected_account(self):
        minted = []

        def minter(account, lifetime):
            minted.append((account, lifetime))
            return "the-token", 1_000_000.0

        pool = one_cluster_pool(minter=minter)
        self.assertEqual("the-token", pool.token_for(PROJECT, LOCATION, CLUSTER))
        self.assertEqual([(EMAIL, scoped_sa_pool.DEFAULT_LIFETIME_SECONDS)], minted)

    def test_an_unmapped_cluster_mints_nothing(self):
        """The refusal must come before the mint, not after it.

        If the order were reversed the broker would hold a credential it then
        declined to use, which is a strictly worse position than not having
        asked for it.
        """
        minted = []

        def minter(account, lifetime):
            minted.append(account)
            return "the-token", 1_000_000.0

        pool = one_cluster_pool(minter=minter)
        with self.assertRaises(PoolRefusal):
            pool.token_for(PROJECT, LOCATION, OTHER_CLUSTER)
        self.assertEqual([], minted)

    def test_a_live_token_is_reused_and_a_stale_one_is_not(self):
        calls = []
        now = [0.0]

        def minter(account, lifetime):
            calls.append(account)
            return f"token-{len(calls)}", now[0] + 900

        pool = ScopedServiceAccountPool(
            parse_pool(document((PROJECT, LOCATION, CLUSTER, EMAIL))),
            minter=minter,
            clock=lambda: now[0],
        )
        margin = ScopedServiceAccountPool.REFRESH_MARGIN_SECONDS
        self.assertEqual("token-1", pool.token_for(PROJECT, LOCATION, CLUSTER))
        now[0] = 900 - margin - 1
        self.assertEqual("token-1", pool.token_for(PROJECT, LOCATION, CLUSTER))
        # Inside the refresh margin. The token has not expired, but a command
        # starting now could outlive it, and a credential that dies mid-request
        # surfaces as an authentication error a long way from this decision.
        now[0] = 900 - margin + 1
        self.assertEqual("token-2", pool.token_for(PROJECT, LOCATION, CLUSTER))
        self.assertEqual(2, len(calls))

    def test_a_lifetime_past_the_one_hour_ceiling_is_refused(self):
        """Twelve-hour tokens need an org policy this deployment will not enable.

        Refused rather than clamped: a silently-clamped value would let a change
        that *intended* twelve hours look like it worked.
        """
        with self.assertRaises(PoolConfigurationError):
            one_cluster_pool(lifetime_seconds=scoped_sa_pool.MAX_LIFETIME_SECONDS + 1)
        with self.assertRaises(ValueError):
            scoped_sa_pool.mint_impersonated_token(EMAIL, 12 * 3600)

    def test_the_default_lifetime_is_well_under_the_ceiling(self):
        self.assertLess(
            scoped_sa_pool.DEFAULT_LIFETIME_SECONDS, scoped_sa_pool.MAX_LIFETIME_SECONDS
        )


class KubeconfigRewriteTest(unittest.TestCase):
    """The token has to land where kubectl will certainly look."""

    GCLOUD_AUTHORED = """
apiVersion: v1
kind: Config
current-context: gke_p_l_c
clusters:
- name: gke_p_l_c
  cluster:
    server: https://10.0.0.1
    certificate-authority-data: QUJD
contexts:
- name: gke_p_l_c
  context:
    cluster: gke_p_l_c
    user: gke_p_l_c
users:
- name: gke_p_l_c
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: gke-gcloud-auth-plugin
      provideClusterInfo: true
"""

    def rewritten(self, token="POOL-TOKEN"):
        import yaml

        return yaml.safe_load(kubeconfig_with_token(self.GCLOUD_AUTHORED, token))

    def test_the_bearer_token_replaces_the_exec_plugin(self):
        user = self.rewritten()["users"][0]["user"]
        self.assertEqual({"token": "POOL-TOKEN"}, user)

    def test_no_second_credential_path_is_left_beside_the_token(self):
        """Replaced, not merged.

        An `exec`, `auth-provider` or `tokenFile` surviving next to the token
        would leave which credential kubectl prefers as a question about
        somebody else's parser — the shape of every Critical this project has
        found. Asserted over the whole rendered document so a nested survivor
        anywhere is caught, not just one in the user entry we happened to check.
        """
        rendered = kubeconfig_with_token(self.GCLOUD_AUTHORED, "POOL-TOKEN")
        for leftover in ("exec", "auth-provider", "tokenFile", "gke-gcloud-auth-plugin"):
            self.assertNotIn(leftover, rendered)

    def test_the_cluster_and_context_survive_untouched(self):
        """The ordinary read has to still work: only the credential changes."""
        document = self.rewritten()
        self.assertEqual("gke_p_l_c", document["current-context"])
        self.assertEqual(
            "https://10.0.0.1", document["clusters"][0]["cluster"]["server"]
        )
        self.assertEqual("QUJD", document["clusters"][0]["cluster"]["certificate-authority-data"])
        self.assertEqual("gke_p_l_c", document["contexts"][0]["context"]["cluster"])

    def test_every_user_entry_is_rewritten_not_only_the_first(self):
        import yaml

        merged = self.GCLOUD_AUTHORED + """- name: second
  user:
    exec:
      command: gke-gcloud-auth-plugin
"""
        document = yaml.safe_load(kubeconfig_with_token(merged, "POOL-TOKEN"))
        self.assertEqual(
            [{"token": "POOL-TOKEN"}, {"token": "POOL-TOKEN"}],
            [entry["user"] for entry in document["users"]],
        )

    def test_a_kubeconfig_with_no_users_is_refused(self):
        for broken in ("apiVersion: v1\nkind: Config\n", "[]", "users: []\n"):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    kubeconfig_with_token(broken, "POOL-TOKEN")


if __name__ == "__main__":
    unittest.main()

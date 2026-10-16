"""The scoped service account pool: what it selects, and what it refuses.

The refusal is the part worth testing hardest. A pool that hands out the right
account for a mapped project and quietly falls back to the ambient credential
for an unmapped one looks identical in every log line and every green test —
the ordinary read still works, which is exactly why the failure is invisible.
So every case below that asserts a selection has a sibling that asserts a
refusal, and the refusal cases assert the *reason*, not just that something
went wrong.

The key is the project, not the cluster. Two clusters in one project share an
account by design (`docs/designs/multi-project-scope.md` §6), so the cases that
used to assert a near-miss *cluster* was refused now assert the opposite, and
the refusal cases name a cluster in a project with no member.

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

import credential_proxy_client
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
)

PROJECT = "bnaylor-kagents-dev"
OTHER_PROJECT = "some-other-project"
LOCATION = "us-east4"
CLUSTER = "bnaylor-ka-test"
OTHER_CLUSTER = "some-other-cluster"
# The CRD's `MaxLength=63` on `projectId`; one over it is the first value the
# pool has to refuse, and the width the refusal log line is sized against.
LONGEST_NAME_COMPONENT = "a" * 63
ONE_OVER_THE_LONGEST_NAME_COMPONENT = "a" * 64
# `credential_proxy.POOL_REFUSAL_LOG_LENGTH`; the refusal at the longest
# components has to fit under it, or the log line is cut mid-remedy.
REFUSAL_LOG_LENGTH = 512
# The refusal's fixed text, measured with every component empty, and the
# number of components it interpolates (the project twice). The comment above
# the refusal in `scoped_sa_pool.select` states both; the test below holds it
# to them, and to the 467 ceiling `POOL_REFUSAL_LOG_LENGTH` was sized against.
REFUSAL_FIXED_LENGTH = 215
REFUSAL_COMPONENT_COUNT = 4
# Spelled the way the Terraform half names a member: `ka-<project prefix>-<hash>`
# in the host project, not in the project the member reads.
EMAIL = "ka-bnaylor-kagents-d-1a2b3c4d@host-project.iam.gserviceaccount.com"
OTHER_EMAIL = "ka-some-other-projec-99887766@host-project.iam.gserviceaccount.com"


def document(*entries: tuple[str, str]) -> dict:
    return {
        "version": 2,
        "serviceAccounts": [
            {"projectId": project, "serviceAccountEmail": email}
            for project, email in entries
        ],
    }


def one_project_pool(minter=None, **kwargs) -> ScopedServiceAccountPool:
    members = parse_pool(document((PROJECT, EMAIL)))
    return ScopedServiceAccountPool(
        members,
        minter=minter or (lambda account, lifetime: (f"token-for-{account}", 1_000_000.0)),
        clock=lambda: 0.0,
        **kwargs,
    )


class NameComponentTest(unittest.TestCase):
    """The project id is the key, and the triple still reaches a log line."""

    def test_the_per_cluster_key_builder_is_gone(self):
        """`scope_key` was the per-cluster index; the project is the index now.

        Asserted as an absence so a merge that resurrects it fails here rather
        than quietly offering two keys for one pool.
        """
        self.assertFalse(hasattr(scoped_sa_pool, "scope_key"))

    def test_a_trailing_newline_cannot_pass_for_a_name_component(self):
        """`$` is not `\\Z`, and the difference reaches a log line and a filename.

        Python's `$` matches immediately before a trailing newline, so
        `re.match(r"^[a-z0-9-]*$", "cluster\\n")` succeeds. Every component of the
        triple goes into the refusal message, and the refusal message goes into
        a WARNING -- one agent-written newline in a kubeconfig's
        `current-context` splits that into two log records. The same
        components also become part of a filename in the broker's state dir.

        Separate from the test below, which covers separators and quotes: those
        were always refused. This one was not.
        """
        pool = one_project_pool()
        for value in ("cluster\n", "cluster\r\n", "cluster\r", "\ncluster"):
            for position in range(3):
                components = [PROJECT, LOCATION, CLUSTER]
                components[position] = value
                with self.subTest(value=value, position=position):
                    with self.assertRaises(ValueError):
                        pool.select(*components)

    def test_a_component_that_could_change_the_key_is_refused(self):
        """Anything that could smuggle a separator or a quote, in any position.

        Location and cluster are no longer part of the key, and they are
        checked anyway: the refusal embeds all three, and a component that is
        not a GKE name is a malformed request rather than a refusal.
        """
        pool = one_project_pool()
        for project, location, cluster in (
            ("proj/ect", LOCATION, CLUSTER),
            (PROJECT, "us-east4/x", CLUSTER),
            (PROJECT, LOCATION, 'clus"ter'),
            (PROJECT, LOCATION, "../other"),
            (PROJECT, LOCATION, "-leading-hyphen"),
            (PROJECT, LOCATION, "UPPER"),
            (PROJECT, LOCATION, ""),
            (PROJECT, LOCATION, None),
            ("", LOCATION, CLUSTER),
            (None, LOCATION, CLUSTER),
        ):
            with self.subTest(cluster=cluster, project=project):
                with self.assertRaises(ValueError):
                    pool.select(project, location, cluster)

    def test_the_pool_and_the_shim_agree_on_what_a_name_component_is(self):
        """Two regexes, one idea — the shape every Critical here has had.

        The GKE component pattern is written twice: `credential_proxy_client`
        owns one because it parses the caller's kubeconfig in the caller's own
        pod, and `scoped_sa_pool` owns the other because it cannot import a
        module the broker imports. This drives both with the same inputs and
        insists they answer the same, so a future edit to either one fails here
        rather than admitting a key the other half rejects.
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
                    bool(credential_proxy_client._GKE_CONTEXT_COMPONENT.fullmatch(candidate)),
                    bool(scoped_sa_pool._COMPONENT.fullmatch(candidate)),
                    f"the shim and the pool disagree about {candidate!r}",
                )
                # Also compared under `.match`, because that is how the pool
                # calls it and an anchor fixed only in one place shows up here.
                self.assertEqual(
                    bool(credential_proxy_client._GKE_CONTEXT_COMPONENT.match(candidate)),
                    bool(scoped_sa_pool._COMPONENT.match(candidate)),
                    f"the shim and the pool disagree about {candidate!r} under match()",
                )


class ParsePoolTest(unittest.TestCase):
    """The mapping is operator-authored config, and it fails loudly."""

    def test_a_well_formed_document_indexes_by_project(self):
        members = parse_pool(document((PROJECT, EMAIL)))
        self.assertEqual(
            {PROJECT: PoolMember(project_id=PROJECT, service_account=EMAIL)},
            members,
        )

    def test_version_1_is_refused_and_the_message_names_version_2(self):
        """The per-cluster file is the one an un-upgraded operator still renders.

        It is refused rather than read: its entries carry a location and a
        cluster name the project key has no use for, and reading them as
        project rows would silently widen every account to the project without
        the operator having asked for that.
        """
        with self.assertRaises(PoolConfigurationError) as raised:
            parse_pool(
                {
                    "version": 1,
                    "serviceAccounts": [
                        {
                            "projectId": PROJECT,
                            "location": LOCATION,
                            "clusterName": CLUSTER,
                            "serviceAccountEmail": EMAIL,
                        }
                    ],
                }
            )
        self.assertIn("expected 2", str(raised.exception))

    def test_a_repeated_project_is_refused_rather_than_resolved(self):
        """Last-wins would make the answer depend on render order."""
        with self.assertRaises(PoolConfigurationError) as raised:
            parse_pool(document((PROJECT, EMAIL), (PROJECT, OTHER_EMAIL)))
        self.assertIn("repeats", str(raised.exception))

    def test_an_empty_list_is_refused_and_names_the_way_out(self):
        """Empty means every request refuses; that is a misconfiguration.

        The message has to name the flag, because the operator who hits this is
        the one who wanted the ambient credential and did not know how to ask.
        """
        with self.assertRaises(PoolConfigurationError) as raised:
            parse_pool({"version": 2, "serviceAccounts": []})
        self.assertIn(scoped_sa_pool.POOL_FLAG_ENV, str(raised.exception))

    def test_a_malformed_document_is_refused(self):
        for broken in (
            {"version": 1, "serviceAccounts": []},
            {"version": 3, "serviceAccounts": []},
            {"version": "2", "serviceAccounts": []},
            {"version": 2},
            {"version": 2, "serviceAccounts": {}},
            {"version": 2, "serviceAccounts": ["not-an-object"]},
            {"version": 2, "serviceAccounts": [{"serviceAccountEmail": EMAIL}]},
            "not a document",
            [],
        ):
            with self.subTest(broken=broken):
                with self.assertRaises(PoolConfigurationError):
                    parse_pool(broken)

    def test_a_project_id_that_is_not_a_name_component_is_refused(self):
        """The key is validated with the same pattern as every GKE component.

        A project id carrying a slash, a quote or a newline would be a key that
        matches nothing and a string that reaches a log line.
        """
        for project in (
            "proj/ect",
            'proj"ect',
            "UPPER",
            "-leading-hyphen",
            "project\n",
            "",
            None,
            123,
        ):
            with self.subTest(project=project):
                with self.assertRaises(PoolConfigurationError):
                    parse_pool(document((project, EMAIL)))

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
                    parse_pool(document((PROJECT, email)))

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
                (PROJECT, "service-123@container-engine-robot.iam.gserviceaccount.com")
            )
        )
        self.assertEqual(1, len(members))


class LoadPoolFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pool.json"

    def test_a_file_round_trips(self):
        self.path.write_text(json.dumps(document((PROJECT, EMAIL))), encoding="utf-8")
        self.assertEqual([PROJECT], sorted(load_pool_file(self.path)))

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
        self.path.write_text(json.dumps(document((PROJECT, EMAIL))), encoding="utf-8")

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
        self.assertEqual([PROJECT], pool.scopes)


class SelectionTest(unittest.TestCase):
    """What the broker asks for, and what it gets."""

    def test_a_cluster_in_a_mapped_project_selects_that_project_s_account(self):
        pool = one_project_pool()
        self.assertEqual(EMAIL, pool.select(PROJECT, LOCATION, CLUSTER).service_account)

    def test_every_cluster_in_a_mapped_project_shares_its_account(self):
        """One account per project, by design.

        The project is the IAM unit the scope declaration is written in and the
        unit the customer's estate is cut in; the design accepts that two
        clusters in one project share a member (`multi-project-scope.md` §6).
        This is the assertion the per-cluster pool had inverted, and it is
        asserted positively so a merge that brings the cluster back into the
        key fails here rather than refusing half a fleet.
        """
        pool = one_project_pool()
        for location, cluster in (
            (LOCATION, OTHER_CLUSTER),
            ("us-west1", CLUSTER),
            (LOCATION, CLUSTER + "-2"),
        ):
            with self.subTest(location=location, cluster=cluster):
                self.assertEqual(EMAIL, pool.select(PROJECT, location, cluster).service_account)

    def test_a_cluster_in_an_unmapped_project_is_refused(self):
        pool = one_project_pool()
        with self.assertRaises(PoolRefusal):
            pool.select(OTHER_PROJECT, LOCATION, CLUSTER)

    def test_only_the_project_slot_is_the_key(self):
        """A mapped project id in the location or cluster slot selects nothing.

        The positive tests pin that the project is sufficient; this pins that
        it is the only key. A `select` that also consulted the cluster name
        (`members.get(project) or members.get(cluster)`) would pass every other
        test here and let a kubeconfig's cluster name, which the agent
        influences, pick an account.
        """
        pool = one_project_pool()
        with self.assertRaises(PoolRefusal):
            pool.select(OTHER_PROJECT, PROJECT, PROJECT)

    def test_the_refusal_names_the_project_and_the_cluster_it_could_not_serve(self):
        """An operator hits this first; it has to say which project was missing.

        Both spellings, because the operator's fix is to the project (declare it
        in `spec.scope`) while the request that failed named a cluster, and a
        message carrying only one of them sends the reader to the wrong file.
        """
        pool = one_project_pool()
        with self.assertRaises(PoolRefusal) as raised:
            pool.select(OTHER_PROJECT, LOCATION, CLUSTER)
        message = str(raised.exception)
        self.assertIn(f"project {OTHER_PROJECT} ", message)
        self.assertIn(
            f"projects/{OTHER_PROJECT}/locations/{LOCATION}/clusters/{CLUSTER}", message
        )
        self.assertIn("will not fall back to the ambient credential", message)
        self.assertIn("spec.scope", message)
        self.assertNotIn("\n", message)
        self.assertEqual(
            "no scoped service account for project "
            f"{OTHER_PROJECT} (cluster projects/{OTHER_PROJECT}/locations/{LOCATION}"
            f"/clusters/{CLUSTER}): refused; the broker will not fall back to the"
            " ambient credential. Declare the project in spec.scope and apply,"
            " or exclude the cluster.",
            message,
        )

    def test_the_refusal_fits_the_log_line_at_the_longest_components(self):
        """The broker logs the refusal through a length cap; the cap is sized
        against this message at the bound `_name_component` enforces, so a
        refusal is never cut mid-remedy."""
        pool = one_project_pool()
        with self.assertRaises(PoolRefusal) as raised:
            pool.select(
                LONGEST_NAME_COMPONENT, LONGEST_NAME_COMPONENT, LONGEST_NAME_COMPONENT
            )
        message = str(raised.exception)
        self.assertTrue(message.endswith("or exclude the cluster."), message)
        self.assertEqual(
            REFUSAL_FIXED_LENGTH + REFUSAL_COMPONENT_COUNT * len(LONGEST_NAME_COMPONENT),
            len(message),
        )
        self.assertLess(len(message), REFUSAL_LOG_LENGTH, len(message))

    def test_a_component_over_the_bound_is_refused_before_it_is_interpolated(self):
        """63 is the CRD's `MaxLength` on `projectId`; 64 is a `ValueError`
        naming the bound, raised for each of the three components, so the
        refusal's variable part is bounded and the log cap above is a bound
        rather than a guess."""
        pool = one_project_pool()
        for position in range(3):
            components = [CLUSTER] * 3
            components[position] = ONE_OVER_THE_LONGEST_NAME_COMPONENT
            with self.subTest(position=position):
                with self.assertRaises(ValueError) as raised:
                    pool.select(*components)
                self.assertIn("63", str(raised.exception))
                self.assertNotIn(ONE_OVER_THE_LONGEST_NAME_COMPONENT, str(raised.exception))
        with self.assertRaises(PoolRefusal):
            pool.select(LONGEST_NAME_COMPONENT, LOCATION, CLUSTER)
        self.assertEqual(63, scoped_sa_pool.MAX_NAME_COMPONENT_LENGTH)

    def test_a_near_miss_project_does_not_select_a_neighbour(self):
        """A prefix, an extension or a different project of the same shape."""
        pool = one_project_pool()
        for project in (
            OTHER_PROJECT,
            PROJECT + "-2",
            PROJECT[:-1],
            PROJECT + "-",
        ):
            with self.subTest(project=project):
                with self.assertRaises(PoolRefusal):
                    pool.select(project, LOCATION, CLUSTER)

    def test_scopes_are_the_sorted_project_ids(self):
        pool = ScopedServiceAccountPool(
            parse_pool(document((PROJECT, EMAIL), (OTHER_PROJECT, OTHER_EMAIL))),
            minter=lambda account, lifetime: ("t", 1_000_000.0),
            clock=lambda: 0.0,
        )
        self.assertEqual(sorted([PROJECT, OTHER_PROJECT]), pool.scopes)

    def test_selection_takes_three_strings_and_nothing_a_caller_authored(self):
        """The scope is resolved, never supplied.

        `select` has no parameter a request body could reach — no dict, no
        headers, no account name. This asserts the signature stays that way,
        because the readable version of this control is the type of its
        arguments rather than a comment claiming payloads are ignored. The
        triple is kept even though only the project is looked up: the broker
        resolves a cluster, and the refusal names it.
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

        pool = one_project_pool(minter=minter)
        self.assertEqual("the-token", pool.token_for(PROJECT, LOCATION, CLUSTER))
        self.assertEqual([(EMAIL, scoped_sa_pool.DEFAULT_LIFETIME_SECONDS)], minted)

    def test_a_cluster_in_an_unmapped_project_mints_nothing(self):
        """The refusal must come before the mint, not after it.

        If the order were reversed the broker would hold a credential it then
        declined to use, which is a strictly worse position than not having
        asked for it.
        """
        minted = []

        def minter(account, lifetime):
            minted.append(account)
            return "the-token", 1_000_000.0

        pool = one_project_pool(minter=minter)
        with self.assertRaises(PoolRefusal):
            pool.token_for(OTHER_PROJECT, LOCATION, CLUSTER)
        self.assertEqual([], minted)

    def test_two_clusters_in_one_project_share_one_token(self):
        """The cache is keyed on the member, not on the triple.

        Keyed on the triple, a fleet of N clusters in one project would mint N
        tokens for one account; keyed on the member it mints one and reuses it,
        and the second cluster's request is served by the same credential the
        first one was.
        """
        minted = []

        def minter(account, lifetime):
            minted.append(account)
            return f"token-{len(minted)}", 1_000_000.0

        pool = one_project_pool(minter=minter)
        self.assertEqual("token-1", pool.token_for(PROJECT, LOCATION, CLUSTER))
        self.assertEqual("token-1", pool.token_for(PROJECT, LOCATION, OTHER_CLUSTER))
        self.assertEqual("token-1", pool.token_for(PROJECT, "us-west1", CLUSTER))
        self.assertEqual([EMAIL], minted)

    def test_a_live_token_is_reused_and_a_stale_one_is_not(self):
        calls = []
        now = [0.0]

        def minter(account, lifetime):
            calls.append(account)
            return f"token-{len(calls)}", now[0] + 900

        pool = ScopedServiceAccountPool(
            parse_pool(document((PROJECT, EMAIL))),
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
            one_project_pool(lifetime_seconds=scoped_sa_pool.MAX_LIFETIME_SECONDS + 1)
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

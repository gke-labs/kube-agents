#!/usr/bin/env python3
"""The read-only evidence surface the self-improvement agent queries.

Every source in docs/designs/self-improvement.md sec. 3 reaches the investigation
through one of the subcommands here, and nothing else does. That is the point of
the file: an agent handed `gcloud` and told to be careful is relying on its own
good behaviour, while an agent whose only tools are `logs`, `traces`, `metrics`
and `k8s get` cannot mutate anything even if it decides it should.

Two identities, neither of which can write:

* **Google Cloud** -- the runner's Workload Identity service account, holding
  `roles/logging.viewer`, `roles/cloudtrace.viewer` and `roles/monitoring.viewer`
  and no GKE roles at all, so `container.clusters.get` fails for every cluster in
  the project including this one. The access token comes from the GKE metadata
  server directly rather than through a client library: the three REST APIs used
  here are a GET and two POSTs, and google-cloud-logging is not in the image.

* **Kubernetes** -- the pod's own service account, bound to `view` on the release
  namespace by a RoleBinding. `view` excludes Secrets, and pods/exec, pods/attach
  and pods/portforward are excluded on top of it (sec. 3.3). The one write the
  Role adds is `update`/`patch` on a single ConfigMap named by `resourceNames`:
  the ledger, which the runner owns. This module never touches it -- nothing
  here writes anything.

The loop's own records are filtered out of every log and trace query by service
name, because a loop that finds itself slow and files a pull request about
itself is a closed circuit (sec. 10). `--include-self` exists for the one case
that wants them -- debugging the loop -- and says so in its help.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)

#: The OTEL service name the runner stamps on its own telemetry, and the
#: Kubernetes object-name prefix its own pods carry. Both are excluded from
#: queries by default.
SELF_SERVICE_NAME = "kube-agents-selfimprove"

_TOKEN_CACHE: Dict[str, Any] = {}


# --------------------------------------------------------------------------
# Redaction
#
# Everything this CLI prints becomes evidence in the ledger, and in `upstream`
# mode the ledger becomes a public pull request on someone else's repository.
# The design's sec. 10 names this as the failure mode that has to be closed
# before that mode is safe to turn on: "logs and spans contain customer cluster
# names, project IDs and user identifiers, and an upstream-mode pull request
# publishes whatever is quoted in it."
#
# Two families, because they fail differently. Credential shapes are copied
# from the credential proxy's `redact_credentials`, which the design names as
# the precedent -- a leaked token is an incident whatever mode the loop is in.
# Identifier shapes are the pass the design says "needs its own": they are not
# secrets, they are the customer's business, and publishing them is a
# disclosure rather than a compromise.
#
# This is shape matching, so it is incomplete by construction. It is a floor
# under what leaves the cluster, not a licence to stop reading the ledger --
# which is why the design also says no install moves to `upstream` mode
# without someone having looked at what its ledger actually contains, and why
# `--no-redact` exists and says so in its help text.
# --------------------------------------------------------------------------

#: `re.IGNORECASE` on the whole credential set, because none of these prefixes
#: is a word and the case they arrive in is not the emitter's choice. A logger
#: that upper-cases a line, a shell that upper-cases an environment dump, a
#: `.upper()` somewhere in a formatter -- each turned `ghp_…` into `GHP_…` and
#: `xoxb-…` into `XOXB-…`, and a case-sensitive pattern published a live token.
#: The cost is that `[A-Z ]*` in the PEM header now also matches lowercase and
#: `AIza` also matches `aiza`, neither of which has a plausible false positive
#: at these lengths.
_CREDENTIAL_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|ya29\.[A-Za-z0-9_\-]{20,}"
    # The whole armoured block, not the header line. Matching only
    # `-----BEGIN ... PRIVATE KEY-----` replaces the one part of a PEM that
    # carries no key material and leaves the base64 body -- the actual private
    # key -- in the evidence, under a `[REDACTED]` marker that reads as though
    # it had been handled. The second arm covers a log that truncated before
    # the END line, which is the common case for a key that reached a log by
    # accident: bounded rather than open-ended so it cannot run to the end of a
    # 10MB log entry.
    #
    # The backslash in that second class is what a GCP service-account key file
    # looks like. Its `private_key` field is JSON, so the line breaks in the
    # armour are the two characters `\` and `n` rather than a newline, and a
    # class of `[A-Za-z0-9+/=\s]` matched zero characters against it: the header
    # was blanked and the key body printed in full underneath. That is a
    # property of the data, not of any caller's formatting -- no amount of
    # reordering upstream fixes it -- so the class has to accept the escape.
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\s\S]{0,8192}?-----END [A-Z ]*PRIVATE KEY-----"
    r"|[A-Za-z0-9+/=\s\\]{0,8192})"
    # Slack bot, user, app-configuration and refresh tokens. `xapp-` is the
    # app-level token Socket Mode uses and is not an `xox` prefix at all, so
    # the character class above never reached it.
    r"|xox[baprse]-[A-Za-z0-9-]{10,}"
    r"|xapp-[0-9]-[A-Za-z0-9-]{10,}"
    # A Slack incoming-webhook URL is a bearer credential in its own right:
    # anyone holding the path can post to the channel, and it appears in full in
    # exactly the failed-delivery log line signal class 5 goes looking for.
    r"|https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{16,256}"
    r"|AIza[A-Za-z0-9_\-]{35}"
    # The model provider's own key. This loop runs on Claude and the agent it
    # observes does too, so an `sk-ant-` value in a log line is the credential
    # most likely to be here at all -- a 401 from the provider is exactly the
    # kind of error signal class 2 tells the investigation to go and read.
    r"|sk-ant-[A-Za-z0-9_-]{20,256}"
    # An AWS access key id. Fixed length and a fixed prefix, so it needs no
    # lower bound guesswork; the matching secret is 40 unmarked base64
    # characters and is only reachable through the keyed rules below.
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    re.IGNORECASE,
)

#: Credentials in a URL's userinfo. `postgres://svc:hunter2@db/agents` is one
#: string to every pattern above and one field to `redact_tree`, and a
#: connection string is what a database error quotes back at you. The scheme is
#: kept and the whole `user:pass` blanked: the username is half the credential.
_URL_CREDENTIALS = re.compile(
    r"\b([a-z][a-z0-9+.-]{1,15}://)[^\s/:@]{1,64}:[^\s/@]{1,256}@",
    re.IGNORECASE,
)

#: An `Authorization` header, which is how a credential reaches a log without
#: any recognisable shape of its own: `Basic` is base64 and an opaque bearer
#: token is whatever the issuer chose. The scheme survives, for the reason
#: `_QUERY_SECRETS` keeps the parameter name -- `Bearer [REDACTED]` says a
#: credential was withheld, where a bare `[REDACTED]` says only that something
#: was.
_AUTH_HEADER = re.compile(
    r"\b(authorization\s{0,8}[:=]\s{0,8}[\"']?(?:bearer|basic|token)\s{1,8})"
    r"[A-Za-z0-9+/=._~-]{8,4096}",
    re.IGNORECASE,
)

#: A Google Chat incoming-webhook URL authenticates with `key=` and `token=` in
#: its query string. `key=` is an AIza value and was already covered above;
#: `token=` is opaque base64 and was not, so a failed-delivery log line --
#: signal class 5, one the loop is specifically told to go looking for -- put a
#: working webhook credential into the ledger, and in upstream mode the ledger
#: becomes a pull request on someone else's repository.
#:
#: Separate from `_CREDENTIAL_SHAPES` because the parameter name is worth
#: keeping: `token=[REDACTED]` tells a reader what was withheld, where a bare
#: `[REDACTED]` in the middle of a URL does not.
_QUERY_SECRETS = re.compile(
    r"([?&](?:token|key|api_key|access_token|auth|signature)=)[A-Za-z0-9%._\-]{16,}",
    re.IGNORECASE,
)

#: Values that are not identifiers however they are keyed. Blanking these
#: inverts the finding: evidence reading `project_id: [PROJECT]` tells a
#: reviewer that a project id was there and was withheld, when what the finding
#: is about is that it was missing. A customer's project could in principle be
#: named `pending`; a reader who cannot tell "absent" from "hidden" is the more
#: expensive failure, and the ledger is still there to check.
_NON_IDENTIFIER_VALUES = frozenset(
    {
        "",
        "-",
        "absent",
        "empty",
        "error",
        "failed",
        "false",
        "missing",
        "n/a",
        "na",
        "nan",
        "nil",
        "none",
        "null",
        "pending",
        "true",
        "undefined",
        "unknown",
        "unset",
    }
)

#: The characters a keyed value is allowed to be made of, shared by the capture
#: group in every keyed rule below and by the `_IDENTIFIER_VALUE` gate those
#: captures are tested against.
#:
#: One constant because every character one of them accepts and the other
#: rejects is a bypass. `_keyed` hands back the whole match unredacted when the
#: gate says no -- correctly, since that is how prose under an identifier key
#: survives -- so a capture that is wider than the gate does not fall back to
#: something narrower, it falls back to printing the value. The gate was
#: lowercase-ASCII and the captures were `[A-Za-z0-9][-\w.:]`, which made
#: `project_id=ACME-PROD-42`, `project_id=acme-prod-42X` and
#: `{"cluster_name": "West-1"}` one-character escapes, and `\w`'s Unicode
#: letters an unbounded supply of more.
_IDENTIFIER_CHARS = "A-Za-z0-9._:-"

#: What a keyed value has to look like before blanking it hides an identifier
#: rather than a sentence. The capture classes already exclude whitespace, so
#: what is left to test is length and the known non-identifier words: a
#: space-free token of three characters or more, under a key that names a
#: project or a cluster, is a name. Case is deliberately not part of the test.
#: GCP will not issue an uppercase project id, but the question here is not
#: whether the value is a valid project id -- it is whether printing it
#: discloses a customer, and `ACME-PROD-42` under `project_id=` discloses one
#: whoever upper-cased it.
_IDENTIFIER_VALUE = re.compile(r"\A[A-Za-z0-9][%s]{2,62}\Z" % _IDENTIFIER_CHARS)


#: The two keys that are also ordinary English words. Every other spelling --
#: `project_id`, `clusterName`, `CLOUDSDK_CORE_PROJECT`, `--cluster` -- is a
#: field name wherever it appears, but a sentence can end with "the project:"
#: and the next word is then prose, not a customer.
_BARE_IDENTIFIER_KEYS = frozenset({"project", "cluster"})


def _is_identifier_value(value: str) -> bool:
    """Whether blanking `value` hides an identifier rather than a sentence.

    Case-folded against `_NON_IDENTIFIER_VALUES` because the shape test no
    longer rejects uppercase: `PENDING` and `MISSING` are the same answer as
    `pending` and `missing`, and a reader who cannot tell "absent" from
    "hidden" is the failure that set is there to prevent.
    """
    text = value.strip().strip("\"'")
    if not _IDENTIFIER_VALUE.match(text):
        return False
    return text.lower() not in _NON_IDENTIFIER_VALUES


def _is_confident_identifier_value(value: str) -> bool:
    """The same test, for a key too ambiguous to carry the decision itself.

    A GCP project id is six characters or more; a GKE cluster name of any
    interest is hyphenated or numbered. An English word after a colon is
    neither, which is what separates `project: acme-prod-1` from `the project:
    this loop improves itself`. The residual is a cluster named with one short
    unhyphenated word, in a log line that spells the key bare -- the trade
    against blanking a word out of every sentence containing `project:`.
    """
    text = value.strip().strip("\"'")
    if not _is_identifier_value(text):
        return False
    return len(text) >= 6 and any(char.isdigit() or char == "-" for char in text)


def _keyed(placeholder: str):
    """Replace a keyed value, but only where the value is an identifier.

    The key is what identifies a project id -- on its own it is a hyphenated
    lowercase word, and so is half the text in a log line -- but a key is not
    enough on its own either, or `cluster: unreachable after 3 tries` becomes
    `cluster: [CLUSTER]` and the finding argues the opposite of what it found.
    How much the value has to look like an identifier depends on how sure the
    key is: `--cluster` and `clusterName` are field names, the bare word is not.

    Returning the whole match when the gate says no is deliberate, and it is
    only safe because the captures and the gate are written from
    `_IDENTIFIER_CHARS` and cannot disagree about shape. The remaining refusals
    are the ones the reader is owed: a known non-identifier word, or a bare key
    whose value is too slight to be a name. Narrowing the value and trying
    again would defeat exactly those -- trimming a character off `missing`
    leaves `missin`, which is in no list and passes every test.
    """

    def substitute(match: "re.Match[str]") -> str:
        value = match.group(3)
        if not _is_identifier_value(value):
            return match.group(0)
        key = match.group(1).strip().lower().replace("-", "_")
        if key in _BARE_IDENTIFIER_KEYS and not _is_confident_identifier_value(value):
            return match.group(0)
        return match.group(1) + match.group(2) + placeholder

    return substitute


# Order matters: each pattern runs over the output of the one before it, so the
# specific shapes go first. A GCP service account is also a syntactically valid
# email address, and with the bare-email rule first every `[GSA]` would already
# have been rewritten to `[EMAIL]` -- correct in effect, but it throws away the
# distinction a reader of the pull request needs between "a person appeared in
# this log" and "a workload did".
#: Why every repetition below carries an explicit upper bound.
#:
#: None of these patterns backtracks catastrophically -- there is no nested
#: quantifier to make one exponential. The cost is quieter and just as fatal: an
#: unbounded greedy class scans to the end of a matching run at *every* start
#: position inside it, so a long run of one character class costs O(n^2). On a
#: 16,000-character log line -- a stack trace, a rendered manifest, a `kubectl
#: get -o yaml` somebody printed -- `redact()` took 3.4 seconds; 64,000
#: characters took 53. The evidence tools read up to 200 entries per call, and
#: the run has a wall-clock budget, so a handful of long lines could spend the
#: investigation on regex scanning and reach the deadline with nothing written.
#: It is also reachable by anything that can get a long line into the observed
#: agent's log, which is a wide door.
#:
#: Every bound is above the real maximum of the thing it matches: 64 for an
#: email local part and 253 for a domain (RFC 5321), 63 for a GCP project id, 40
#: for a cluster name, and 64 for an environment-variable prefix such as
#: `CLOUDSDK_CORE_`. Bounding turns each scan into O(n * k) with k a constant,
#: which measures linear. Anything added here needs a bound for the same reason.
#: `test_redaction_stays_linear_in_the_length_of_the_line` holds the line.
_IDENTIFIER_SHAPES = (
    (re.compile(r"\b[a-z][-a-z0-9]{4,29}@[a-z][-a-z0-9]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com\b"), "[GSA]"),
    # How a user appears in an audit log's `authenticationInfo.principalEmail`
    # and in most `actor` fields.
    # Bounded, and every other repetition below is too. See _BOUNDS_NOTE.
    (re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b"), "[EMAIL]"),
    # Fully-qualified resource paths. These are the ones that name a customer.
    # `projects/` takes a digit as well as a letter: Monitoring, Asset and
    # Pub/Sub all render the parent as `projects/<number>`, and a project number
    # identifies a customer exactly as well as a project id does.
    #
    # `%2F` as well as `/`, because these paths arrive inside URLs as often as
    # they arrive in prose, and a URL that was built with `urlencode` has the
    # separator escaped. That is not a corner: the failure path prints the
    # request it failed on, and `spaces%2F…` in a Chat delivery URL is how a
    # space name -- and with it a customer's workspace -- left the loop. The
    # separator is captured rather than rewritten so the evidence still shows
    # the URL as it was sent.
    (re.compile(r"\b(projects(?:/|%2[Ff]))[a-z0-9][-a-z0-9:.]{4,61}[a-z0-9]"), r"\1[PROJECT]"),
    (re.compile(r"\b(clusters(?:/|%2[Ff]))[a-z][-a-z0-9]{0,38}[a-z0-9]"), r"\1[CLUSTER]"),
    # The two names embedded in a path rather than keyed. A kubeconfig context
    # for GKE is `gke_<project>_<location>_<cluster>`, and it is what `kubectl
    # config current-context` prints into every piece of debugging evidence. The
    # location survives, for the same reason `location` is not an identifier key
    # below: a GCP region names nobody.
    (
        re.compile(r"\bgke_([a-z][-a-z0-9:.]{4,61})_([a-z0-9-]{1,40})_([a-z][-a-z0-9]{0,38})\b"),
        r"gke_[PROJECT]_\2_[CLUSTER]",
    ),
    # Image and bucket references carry the project id as a path segment, and
    # image references in particular are all over a CrashLoopBackOff finding.
    (re.compile(r"\b([a-z0-9-]{1,32}-docker\.pkg\.dev)/[a-z][-a-z0-9]{4,28}[a-z0-9]"), r"\1/[PROJECT]"),
    (re.compile(r"\b((?:[a-z]{2,4}\.)?gcr\.io)/[a-z][-a-z0-9]{4,28}[a-z0-9]"), r"\1/[PROJECT]"),
    (re.compile(r"\bgs://[a-z0-9][-a-z0-9_.]{1,61}[a-z0-9]"), "gs://[BUCKET]"),
    # And the keyed form, which is how the same two names actually turn up: a
    # Cloud Monitoring resource label is `project_id`, not `projects/…`, and an
    # environment variable is `GKE_CLUSTER_NAME=`. A project id on its own is
    # unrecognisable -- it is a hyphenated lowercase word, and so is half the
    # text in a log line -- so the key is what identifies it, and both the
    # `key=value` and `"key": "value"` spellings have to be read.
    #
    # The key is matched with any prefix rather than a list of them. `\b` is a
    # boundary between a word character and a non-word character, and `_` is a
    # word character, so a leading `\bproject` cannot match inside
    # `KUBEAGENTS_PROJECT_ID` or `CLOUDSDK_CORE_PROJECT` at all -- and those,
    # not the bare spelling, are how a project id reaches a container's
    # environment. Letting the prefix be anything also collapses this rule and
    # the key-driven pass below onto the same set of names, which they did not
    # previously agree on: `--project` and `cluster:` were caught in a JSON tree
    # and missed in the log line that is the commoner evidence form.
    #
    # The value class is `_IDENTIFIER_CHARS`, the same one `_IDENTIFIER_VALUE`
    # gates on. It was `[-\w.:]`, which is wider on both counts that matter --
    # `\w` is Unicode by default, and the gate was lowercase-only -- and every
    # character of the difference was a value the capture grabbed and the gate
    # then waved through unredacted.
    (
        re.compile(
            r"""(?i)\b((?:[\w-]{0,64}[_-])?project(?:[_-]?(?:id|number))?)"""
            r"""(["']?\s{0,16}[:=]\s{0,16}["']?)([A-Za-z0-9][%s]{2,61})""" % _IDENTIFIER_CHARS
        ),
        _keyed("[PROJECT]"),
    ),
    (
        re.compile(
            r"""(?i)\b((?:[\w-]{0,64}[_-])?cluster(?:[_-]?name)?)"""
            r"""(["']?\s{0,16}[:=]\s{0,16}["']?)([A-Za-z0-9][%s]{2,61})""" % _IDENTIFIER_CHARS
        ),
        _keyed("[CLUSTER]"),
    ),
    # The same two, as a command-line flag with the value in the next argument.
    # `--project=x` is a `key=value` and the rules above have it; `--project x`
    # is the spelling gcloud's own documentation uses, and nothing keys it.
    (
        re.compile(
            r"""(?i)(?<![-\w])(--(?:[\w-]{1,32}-)?project(?:-(?:id|number))?)"""
            r"""(\s{1,16}["']?)([A-Za-z0-9][%s]{2,61})""" % _IDENTIFIER_CHARS
        ),
        _keyed("[PROJECT]"),
    ),
    (
        re.compile(
            r"""(?i)(?<![-\w])(--(?:[\w-]{1,32}-)?cluster(?:-name)?)"""
            r"""(\s{1,16}["']?)([A-Za-z0-9][%s]{2,61})""" % _IDENTIFIER_CHARS
        ),
        _keyed("[CLUSTER]"),
    ),
    # Bare IPv4, less the loopback and link-local ranges, which carry no
    # information about anyone and which a reader diagnosing the metadata
    # server or the credential proxy socket genuinely needs.
    (re.compile(r"\b(?!127\.|169\.254\.|0\.0\.0\.0)(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    # And IPv6, which the rule above cannot see at all. A dual-stack GKE cluster
    # gives every pod one, so on those installs the address in a connection
    # error is the form that was never matched.
    #
    # Written as three explicit arms rather than one general IPv6 grammar,
    # because the thing this must not do is eat a timestamp. `10:00:00` is two
    # colons, and each arm here needs either seven or a literal `::`, so no
    # clock reaches them. Loopback and link-local are excluded for the reason
    # the IPv4 rule excludes 127/8: a reader debugging the metadata server or
    # the credential-proxy socket needs to see them, and they name nobody.
    (
        re.compile(
            r"(?<![:.\w])(?!::1(?![0-9A-Fa-f:]))(?![Ff][Ee]80:)(?:"
            r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
            r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?"
            r"|::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){1,6})"
            r")(?![:.\w])"
        ),
        "[IP]",
    ),
    # Google Chat spaces and Slack identifiers, all over the delivery signal
    # class, naming both a workspace and the people in it. The lookahead
    # requiring a digit is what keeps the Slack rule off ordinary uppercase log
    # text -- without it `CRASHLOOPBACKOFF` and `CONTAINERSTATUS` are Slack
    # channel ids, and the delivery evidence becomes unreadable.
    (re.compile(r"\b(spaces(?:/|%2[Ff]))[A-Za-z0-9_-]{5,128}"), r"\1[SPACE]"),
    (re.compile(r"\b[CDGU](?=[A-Z0-9]{0,64}[0-9])[A-Z0-9]{8,64}\b"), "[SLACK-ID]"),
    # Workspace, enterprise grid, bot and enterprise-user ids -- `T`, `E`, `B`
    # and `W` -- name the installation rather than a channel, and the delivery
    # evidence quotes them next to the channel ids above. They need a stricter
    # test than `CDGU` does: those four letters begin `CRASHLOOPBACKOFF`,
    # `DEADLINE_EXCEEDED`, `TIMEOUT30000` and `WORKER12345`, so a digit
    # somewhere in the token is not enough to tell an id from a log word.
    # Requiring one inside the first four characters is: every id Slack has
    # minted since it started encoding a counter into the prefix has it
    # (`T01ABC2DEF`, `B024BE7LD`), and no English uppercase word does. The
    # residual, taken deliberately, is a legacy all-letter id such as
    # `TABCDEFGH`: catching it means blanking `TIMEOUT`-shaped words out of the
    # delivery evidence, and a workspace id is not a credential.
    (re.compile(r"\b[TEBW][0-9A-Z]{0,2}[0-9][0-9A-Z]{5,60}\b"), "[SLACK-ID]"),
)


#: Characters that occupy no width and therefore split a token without a reader
#: ever seeing the seam: a zero-width space inside `ghp_` is not `ghp_` to any
#: pattern here and is `ghp_` to everyone reading the pull request. Written as
#: escapes rather than as the characters themselves so the class can be read --
#: soft hyphen, zero-width space/non-joiner/joiner, word joiner, BOM.
_ZERO_WIDTH = re.compile("[\u00ad\u200b\u200c\u200d\u2060\ufeff]")


def _normalise(text: str) -> str:
    """Fold the spellings that defeat a pattern without changing what is read.

    Two of them, and only two. Zero-width characters are deleted, because they
    break a token in half for the matcher and are invisible to the reader.
    Everything else goes through NFKC, which is what turns fullwidth
    `ｇｈｐ＿…` and the other compatibility forms back into the ASCII they
    render as.

    Confusables are the case this deliberately does not cover: a Cyrillic `а`
    in `аcme-prod-1` still survives. Folding those needs the Unicode
    confusables table, which is thousands of mappings that have to be carried
    and kept current, and the thing it would buy is small -- GCP will not issue
    a project id or a cluster name containing a non-ASCII character, so a
    homoglyphed one is not an identifier this CLI could have read out of any
    Google API. It would have to have been typed into a log by someone who
    wanted it published, which redaction is not the control for.

    The ASCII fast path is not an optimisation detail: `redact` runs on every
    line of every entry, and `test_redaction_stays_linear_in_the_length_of_the_line`
    is the budget it has to stay inside.
    """
    if text.isascii():
        return text
    return unicodedata.normalize("NFKC", _ZERO_WIDTH.sub("", text))


def redact(text: str) -> str:
    """Blank credential and identifier shapes out of one string."""
    text = _normalise(text)
    text = _CREDENTIAL_SHAPES.sub("[REDACTED]", text)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    text = _AUTH_HEADER.sub(r"\1[REDACTED]", text)
    text = _QUERY_SECRETS.sub(r"\1[REDACTED]", text)
    for pattern, replacement in _IDENTIFIER_SHAPES:
        text = pattern.sub(replacement, text)
    return text


#: Keys whose value is an identifier whatever the value looks like. `redact`
#: reads one string at a time, so in a JSON tree it never sees `project_id` and
#: the project id together -- a Cloud Monitoring resource label arrives as
#: `{"project_id": "acme-prod-1"}` and both halves pass the shape rules
#: untouched. A project id on its own is unrecognisable: it is a hyphenated
#: lowercase word, and so is half the text in a log line. The key is the only
#: thing that identifies it, so the pass that reads keys has to happen one
#: level up from the one that reads strings.
#:
#: `location` and `pod_name` are deliberately absent. A GCP region names nobody,
#: and the pod name is both the thing the self-exclusion filter keys on and the
#: first thing a reader of the pull request needs in order to find the log line
#: again.
_IDENTIFIER_KEYS = {
    "project": "[PROJECT]",
    "project_id": "[PROJECT]",
    "projectid": "[PROJECT]",
    "project_number": "[PROJECT]",
    "projectnumber": "[PROJECT]",
    "cluster": "[CLUSTER]",
    "cluster_name": "[CLUSTER]",
    "clustername": "[CLUSTER]",
}


def _identifier_placeholder(key: str) -> Optional[str]:
    """The placeholder a key's value should be replaced with, or None.

    Matched on the tail of the key rather than against a list of known
    prefixes, because there is no end to the prefixes: `GOOGLE_CLOUD_PROJECT`
    and `KUBEAGENTS_PROJECT_ID` are the install's own, `CLOUDSDK_CORE_PROJECT`
    is the gcloud SDK's, and `resource.labels.project_id` is what a Cloud
    Logging entry looks like once something has flattened it. Anything ending
    at an underscore boundary in one of the names below is that name.
    """
    normalised = key.strip().lower().replace("-", "_").replace(".", "_").replace("/", "_")
    placeholder = _IDENTIFIER_KEYS.get(normalised)
    if placeholder is not None:
        return placeholder
    return next(
        (value for name, value in _IDENTIFIER_KEYS.items() if normalised.endswith("_" + name)),
        None,
    )


#: A flag whose value is the *next* element of an argv list rather than part of
#: the same string. `{"args": ["--project", "acme-prod-1"]}` is how a container
#: spec stores it, and split across two elements neither the string pass nor the
#: key pass can see the pairing -- the same blind spot Kubernetes' EnvVar has,
#: for the same reason.
_IDENTIFIER_FLAG = re.compile(
    r"\A--(?:[\w-]+-)?(project|cluster)(?:-(?:id|name|number))?\Z", re.IGNORECASE
)


def _redact_argv(items: list) -> list:
    """`redact_tree` over a list, carrying a flag's meaning to the next element."""
    result = []
    pending: Optional[str] = None
    for item in items:
        # A token that opens with `-` is the next flag, never the previous
        # flag's value: no project ID or cluster name may start with one. It
        # matters because of the empty-value carry below -- consuming
        # `--project` as the value of a `--cluster ""` that came before it
        # both cleared the carry and skipped the match on `--project`, so the
        # ID after it met only shape-based redaction and reached the ledger.
        # `.strip()` to agree with the match below, which strips before it
        # tests -- otherwise ` --project` is a value here and a flag there.
        value_shaped = isinstance(item, str) and not item.strip().startswith("-")
        if pending is not None and value_shaped:
            result.append(_identifier_leaf(item, pending))
            # An empty string is not a value -- it is what a flag renders as
            # when the shell that built this argv had nothing to put there
            # (`--cluster ""` from an unset variable). The real value, if any,
            # is the next element, so the flag's meaning carries one more
            # step rather than falling through to generic, shape-based
            # redaction that cannot recognise a bare project ID or cluster
            # name. Anything else that reaches here -- a word `redact_tree`
            # decided was not identifier-shaped -- is treated as the value it
            # looks like, not as another gap to look past.
            if item != "":
                pending = None
            continue
        pending = None
        if isinstance(item, str):
            match = _IDENTIFIER_FLAG.match(item.strip())
            if match:
                pending = "[PROJECT]" if match.group(1).lower() == "project" else "[CLUSTER]"
        result.append(redact_tree(item))
    return result


def _identifier_leaf(value: Any, placeholder: str) -> Any:
    """Apply an identifying key's placeholder to everything beneath it.

    A key that identifies its value identifies it whatever shape it arrives in.
    Monitoring returns a project number as a JSON integer, a flattened label set
    puts it in a one-element list, and `{"project": {"id": …}}` nests it a level
    down -- all three used to fall through the string test and out into the
    pull request. Prose does not: `_is_identifier_value` sends it back to
    `redact`, so a value that is a sentence stays a sentence.
    """
    if isinstance(value, str):
        return placeholder if _is_identifier_value(value) else redact(value)
    # bool before int: `isinstance(True, int)` is True, and `enabled: true` under
    # a matching key is a flag rather than a withheld identifier.
    if isinstance(value, bool):
        return value
    # A number is an identifier only under `[PROJECT]`, where it is the project
    # number Resource Manager returns as an int. Nothing numeric is a cluster
    # name, so `{"cluster": {"nodeCount": 3}}` was losing the count -- and the
    # count is often the finding.
    if isinstance(value, (int, float)):
        return placeholder if placeholder == "[PROJECT]" else value
    if isinstance(value, dict):
        # Descend with the placeholder only into the fields that name the thing
        # the key introduced. `{"project": {"id": …}}` is a project id one level
        # down; `{"cluster": {"message": "control plane unreachable"}}` is a
        # structure the key merely opened, and blanking every scalar in it
        # deletes the evidence the finding is made of.
        return {
            redact(str(k)): (
                _identifier_leaf(v, placeholder)
                if _names_the_identifier(k)
                else redact_tree(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_identifier_leaf(v, placeholder) for v in value]
    return value


#: Sub-keys that restate the identifier their parent key introduced, rather than
#: opening a new field beneath it.
_IDENTIFIER_SUBKEYS = frozenset({"id", "name", "number", "value"})


def _names_the_identifier(key: Any) -> bool:
    normalised = str(key).strip().lower().replace("-", "_").replace(".", "_")
    return (
        normalised in _IDENTIFIER_SUBKEYS
        or _identifier_placeholder(normalised) is not None
    )


#: Names that say their value is a credential. `_env_summary` prints every
#: literal env value a container carries, on the argument that `view` can
#: already read it -- true, and beside the point once the output is a public
#: pull request. Nothing about `hunter2` is a credential shape, so
#: `{"name": "DB_PASSWORD", "value": "hunter2"}` went out whole. The name is the
#: only thing that identifies it, which is the same argument `_IDENTIFIER_KEYS`
#: makes about a project id, so it belongs in the same pass.
#:
#: Matched on the tail at an underscore boundary, for the reason
#: `_identifier_placeholder` gives: there is no end to the prefixes. Over-broad
#: on purpose -- `SORT_KEY` and `LICENSE_KEY` lose values they did not need to
#: -- because the cost of blanking one uninteresting value is a `[REDACTED]` a
#: reader can ask about, and the cost of the other mistake is a published
#: secret.
_SECRET_ENV_NAME = re.compile(
    r"(?:\A|_)(?:"
    r"password|passwd|pass|secret|token|key|apikey|credential|credentials|"
    r"auth|authorization|salt|signature|dsn|conn|connection_string|"
    r"private_key|access_key|secret_key|client_secret|webhook|webhook_url"
    r")\Z",
    re.IGNORECASE,
)


#: The same idea for an ordinary mapping key, and a shorter list than the one
#: above. A container's env var is a shouty name whose last word is the whole
#: description of the value, so `KEY` and `AUTH` on the end of one are safe to
#: read literally. A JSON field is not: `key` is what half the structured
#: loggers call the map key they are reporting on, and `auth` and `pass` are
#: field names as often as they are values. Those four are dropped here and
#: kept there, which is the difference between the two lists.
#:
#: This pass matters more since `cmd_logs` stopped flattening structures: a
#: `jsonPayload` reaches `redact_tree` now, and `{"password": "hunter2"}` in a
#: payload is the same disclosure as the env var.
_SECRET_KEYS = re.compile(
    r"(?:\A|_)(?:"
    r"password|passwd|secret|token|apikey|api_key|credential|credentials|"
    r"authorization|private_key|access_key|secret_key|client_secret|"
    r"session_token|refresh_token|webhook_url"
    r")\Z",
    re.IGNORECASE,
)


#: A camelCase boundary, so `appPrivateKey` reaches `_SECRET_KEYS` as
#: `app_private_key` and matches. `_identifier_placeholder` does not do this and
#: does not need to: `_IDENTIFIER_KEYS` lists the run-together spellings
#: (`projectid`, `clustername`) by hand, which works for a table of five names
#: and would not for a tail match over a list of prefixes nobody enumerated.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _snake(key: str) -> str:
    """A mapping key in one spelling, so one pattern can match all of them."""
    return (
        _CAMEL_BOUNDARY.sub("_", key.strip())
        .lower()
        .replace("-", "_")
        .replace(".", "_")
        .replace("/", "_")
    )


def _secret_value(value: Any) -> Any:
    """What to print for a value whose key says it is a credential.

    Only a scalar is blanked. A credential is a scalar; a structure under one
    of these keys is a *reference* to one -- `secret: {name: kube-agents-github,
    key: token}` is a Kubernetes secretKeyRef, and blanking it whole deletes
    the "is it wired up" answer that `_env_summary` says is the point of
    reading env at all. Kubernetes' EnvVar.value is a string, so the env case
    loses nothing to this.

    An empty or absent value survives, for the same reason
    `_NON_IDENTIFIER_VALUES` exists: half of what the inefficiency signal looks
    for is a variable that never got a value, and `[REDACTED]` says a secret
    was there and was withheld -- the finding backwards. `(unset)` is spelled
    with parentheses by `_env_summary` itself, so they are stripped first.
    """
    if isinstance(value, (dict, list)):
        return redact_tree(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str) and value.strip().strip("\"'()").lower() in _NON_IDENTIFIER_VALUES:
        return value
    return "[REDACTED]"


def redact_tree(value: Any) -> Any:
    """Redact every string in a nested structure, keys included.

    Keys as well as values because a Kubernetes object's annotations put user
    content in both -- `kubectl.kubernetes.io/last-applied-configuration` is a
    key whose value is a whole manifest, and label keys carry domain names. And
    a key can also identify its own value, which is what `_IDENTIFIER_KEYS`
    covers.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        # Kubernetes' EnvVar splits the pairing across two sibling entries --
        # `{"name": "GKE_CLUSTER_NAME", "value": "prod-usc1"}` -- so neither the
        # string pass nor the key pass can see it. `_env_summary` emits exactly
        # this shape for every container env var, which is the one place the
        # evidence CLI prints an install's own configuration verbatim.
        named = value.get("name")
        if isinstance(named, str) and "value" in value:
            if _SECRET_ENV_NAME.search(_snake(named)):
                return {
                    redact(str(k)): (_secret_value(v) if k == "value" else redact_tree(v))
                    for k, v in value.items()
                }
            placeholder = _identifier_placeholder(named)
            if placeholder is not None:
                return {
                    redact(str(k)): (_identifier_leaf(v, placeholder) if k == "value" else redact_tree(v))
                    for k, v in value.items()
                }
        result = {}
        for k, v in value.items():
            key = str(k)
            placeholder = _identifier_placeholder(key)
            if _SECRET_KEYS.search(_snake(key)):
                result[redact(key)] = _secret_value(v)
            elif placeholder is not None:
                result[redact(key)] = _identifier_leaf(v, placeholder)
            else:
                result[redact(key)] = redact_tree(v)
        return result
    if isinstance(value, list):
        return _redact_argv(value)
    return value


#: Set from `--no-redact`, read by `emit`. A module global rather than threaded
#: through every command because the output boundary is the only place that
#: needs it, and a parameter on each `cmd_*` is a parameter one of them
#: eventually forgets to pass.
_REDACT = True


def _write(value: Any, stream: Any) -> None:
    if isinstance(value, str):
        print(redact(value) if _REDACT else value, file=stream)
        return
    print(json.dumps(redact_tree(value) if _REDACT else value, indent=1, default=str), file=stream)


def emit(value: Any) -> None:
    """The CLI's one output boundary. Everything printed goes through here.

    One function so the redaction cannot be skipped by adding a command: a new
    `cmd_*` that calls `print` directly is the bug this shape exists to make
    visible in review. A string is printed as-is, anything else as indented
    JSON, and either way it is redacted first unless `--no-redact` said not to.
    """
    _write(value, sys.stdout)


def emit_err(value: Any) -> None:
    """`emit` for the diagnostic stream, which is not a lesser stream.

    Whatever the agent's tool call captures is what can be quoted in a finding,
    and a harness that merges the two -- most do -- makes stderr evidence
    verbatim. The messages here are also the ones most likely to be carrying
    something: an error quotes the thing that failed, which is why `_fail`
    formats an HTTP body and a URL into its text.
    """
    _write(value, sys.stderr)


def _fail(message: str) -> None:
    """Through `emit_err` so there is one redaction and one `--no-redact`.

    It called `redact` directly, which meant `--no-redact` printed the results
    redacted and the errors about them raw -- the wrong way round, and a
    divergence nobody would have found by reading either half.
    """
    emit_err("error: %s" % message)
    raise SystemExit(2)


def access_token() -> str:
    """A Google access token for the runner's Workload Identity service account.

    Straight from the metadata server. google.auth would also work and is in the
    image, but it is a heavier import for a value this file needs once, and the
    metadata endpoint is the same thing the library ends up calling under
    Workload Identity.
    """
    if "token" in _TOKEN_CACHE:
        return _TOKEN_CACHE["token"]
    request = urllib.request.Request(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        _fail(
            "no Google access token from the metadata server (%s). Workload Identity is "
            "how this pod authenticates; check the KSA's iam.gke.io/gcp-service-account "
            "annotation and the GSA's workloadIdentityUser binding." % exc
        )
    token = payload.get("access_token")
    if not token:
        _fail("the metadata server returned no access_token")
    _TOKEN_CACHE["token"] = token
    return token


def _google_api(url: str, body: Optional[Dict[str, Any]] = None, method: str = "POST") -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer %s" % access_token(),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        _fail("%s %s: %s" % (exc.code, exc.reason, detail))
    except urllib.error.URLError as exc:
        _fail("could not reach %s: %s" % (url, exc))
    return {}


def _project() -> str:
    project = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GKE_PROJECT_ID")
    if not project:
        _fail("GCP_PROJECT_ID is not set; the chart sets it on the runner container")
    return project


def _namespace() -> str:
    return os.environ.get("KUBE_DEFAULT_NAMESPACE") or os.environ.get("POD_NAMESPACE") or "kubeagents-system"


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


#: How much beyond `--width` the redactor is shown before the line is cut.
#: Redaction has to see the payload as the API sent it, but the payload can be
#: 256KB and `--width` defaults to 400, so scanning all of it to print 400
#: characters is work the run's wall-clock budget pays for and nobody reads.
#: Every pattern in this module carries an explicit upper bound and the largest
#: is the PEM body's 8192, so a match that begins inside the first `width`
#: characters cannot reach past `width + 8192`. Redacting that much and cutting
#: afterwards is the same output as redacting the whole payload, at a fixed
#: cost.
_REDACT_MARGIN = 8192


def _payload_line(payload: Any, width: int) -> str:
    """One entry's payload, redacted before it is truncated and escaped.

    The order is the defect this function exists to fix. `emit` is the output
    boundary, but `cmd_logs` used to hand it a payload it had already cut to
    `--width` and whose newlines it had already rewritten as `\\n`, and both of
    those edits defeat the patterns `emit` then matches with. A literal
    backslash was not in the PEM body's character class, so an escaped key body
    matched zero characters: the BEGIN header was blanked and the private key
    printed underneath it, beneath a `[REDACTED]` marker reading as though it
    had been handled. `--width 400` guarantees the END marker is gone too, so
    the arm that would have caught it never applied. Truncation does the
    quieter version of the same thing -- it cuts a token below the `{20,}`
    bound the credential shapes require, and splits `projects/<id>` mid-value,
    so a partial secret and a partial customer name survive.

    So redaction runs first, on the text as it arrived, and the formatting runs
    on the result. Teaching every pattern to step over `\\n` instead would
    widen each of them to accommodate one caller's formatting, and the next
    caller that formats differently is a silent regression rather than a
    failing test. (The PEM class does now accept a backslash, but for a
    different reason and a case no ordering fixes: a GCP service-account key
    file is JSON, so the armour's line breaks really are the two characters
    `\\` and `n`.)

    A structure is redacted as a structure, so `redact_tree`'s key pass runs.
    `emit` redacts the composed line again; that second pass is what covers the
    timestamp and the pod and container names, and it is a no-op over the
    placeholders this one already wrote.
    """
    if isinstance(payload, str):
        text = redact(payload[: width + _REDACT_MARGIN]) if _REDACT else payload
    else:
        text = json.dumps(redact_tree(payload) if _REDACT else payload, sort_keys=True)
    return text[:width].replace("\n", "\\n")


def cmd_logs(args: argparse.Namespace) -> int:
    """Cloud Logging entries for the install's namespace.

    The agent writes to files on its data volume rather than to stdout, so
    `kubectl logs` on the agent container shows almost nothing. The fluent-bit
    sidecar the operator adds to every agent pod tails /opt/data/logs/*.log,
    stamps each record `log_source: agent-file` and prints it as JSON to stdout,
    from where GKE ships it here. That is the whole log-access story for a
    runner that never mounts the data volume: query Cloud Logging.
    """
    # Shared with logs-count deliberately. The count is the number the gate
    # reads and the number the pull request quotes, so a reader who runs `logs`
    # to see the entries behind it must be looking at the same set. Two copies
    # of this clause list would let one grow a filter the other does not have,
    # and the symptom -- a count that does not match the sample -- would show up
    # in a pull request rather than here.
    body = {
        "resourceNames": ["projects/%s" % _project()],
        "filter": _logs_filter(args),
        "orderBy": "timestamp desc",
        "pageSize": min(args.limit, 1000),
    }
    payload = _google_api("https://logging.googleapis.com/v2/entries:list", body)
    entries = payload.get("entries", [])[: args.limit]
    if args.raw:
        emit(entries)
        return 0
    for entry in entries:
        # Take the first payload field that is actually populated. Chaining
        # `json.dumps(...) or json.dumps(...)` does not work: an absent
        # jsonPayload dumps to the string "{}", which is truthy, so the
        # protoPayload branch was unreachable and every audit-log entry --
        # audit logs carry protoPayload and nothing else -- printed as "{}".
        #
        # The field is carried as the object the API sent, not as a string.
        # `json.dumps` here and `_payload_line` has nothing left to run the key
        # pass over, which is the difference between `logs` printing
        # `{"args": ["--project", "acme-prod-42"]}` and `logs --raw` printing
        # `[PROJECT]` for the same entry -- on the subcommand the loop uses most.
        payload: Any = entry.get("textPayload")
        if not payload:
            for key in ("jsonPayload", "protoPayload"):
                structured = entry.get(key)
                if structured:
                    payload = structured
                    break
        if not payload:
            payload = "{}"
        emit(
            "%s [%s] %s/%s %s"
            % (
                entry.get("timestamp", "?"),
                entry.get("severity", "DEFAULT"),
                entry.get("resource", {}).get("labels", {}).get("pod_name", "?"),
                entry.get("resource", {}).get("labels", {}).get("container_name", "?"),
                _payload_line(payload, args.width),
            )
        )
    if not entries:
        emit("(no entries matched)")
    return 0


def cmd_logs_count(args: argparse.Namespace) -> int:
    """How many entries match, bucketed by the field the gate cares about.

    The occurrence count is the strongest sentence in a pull request, and it is
    also the number `minOccurrencesPerDay` reads, so it has to come from a
    counting query rather than from `len()` of a page of results that stopped at
    the limit.
    """
    buckets: Dict[str, int] = {}
    total = 0
    body = {
        "resourceNames": ["projects/%s" % _project()],
        "filter": _logs_filter(args),
        "orderBy": "timestamp desc",
        "pageSize": 1000,
    }
    page_token = None
    pages = 0
    while pages < args.max_pages:
        if page_token:
            body["pageToken"] = page_token
        payload = _google_api("https://logging.googleapis.com/v2/entries:list", body)
        entries = payload.get("entries", [])
        total += len(entries)
        for entry in entries:
            key = "%s/%s" % (
                entry.get("resource", {}).get("labels", {}).get("container_name", "?"),
                entry.get("severity", "DEFAULT"),
            )
            buckets[key] = buckets.get(key, 0) + 1
        page_token = payload.get("nextPageToken")
        pages += 1
        if not page_token:
            break
    emit({"total": total, "truncated": bool(page_token), "by": buckets})
    return 0


def _literal(value: str, what: str) -> str:
    """A value about to be interpolated between double quotes in a filter.

    The narrowing argument in `_check_query` holds only for text inside the
    parentheses. `--container` and `--severity` are not parenthesised -- they
    become bare `field="value"` clauses -- so a value carrying its own quote
    ends the literal and appends whatever follows as filter syntax. Cloud
    Logging binds AND tighter than OR, which makes `container="x" OR everything`
    parse as `(namespace AND container="x") OR everything`: the namespace clause
    is still in the filter and no longer constrains the result.

    Neither field has a legitimate use for a quote or a backslash, so rejecting
    both is free.
    """
    if '"' in value or "\\" in value:
        _fail("%s may not contain a quote or a backslash" % what)
    return value


def _check_query(query: str, flag: str = "--query") -> None:
    """Reject a caller filter that could escape the parentheses it is wrapped in.

    `flag` names the argument in the error, because `metrics --filter` wraps its
    own caller text the same way and for the same reason.

    `resourceNames` is the whole project -- Cloud Logging cannot scope a read to
    a Kubernetes namespace, and `roles/logging.viewer` is a project grant -- so
    the namespace clause in the filter string is the only thing keeping this
    subcommand inside the install. That makes the filter a security boundary
    written in string concatenation, with `--query` the one part of it the agent
    composes, from text it read in logs it does not control.

    What saves it is that the query is ANDed: `A AND (Q)` matches a subset of
    `A` for every well-formed `Q`, so no query can widen the read beyond the
    namespace, and one naming `resource.labels.namespace_name` itself only
    narrows to nothing. The only way out is to stop the text parsing as
    `A AND (Q)` at all -- close the wrapping paren early and open a new group,
    or leave a quote open so the string swallows the clauses that follow it.
    (Those clauses are the self-exclusion in sec. 10, which is the cheaper of
    the two escapes and still a real one.) Both are lexical, so checking that
    parentheses and quotes are well-formed closes the hole completely rather
    than blocklisting field names that cannot hurt anyone.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in query:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                _fail(
                    "%s closes a parenthesis it did not open; it is wrapped "
                    "in parentheses and cannot escape them" % flag
                )
    if depth:
        _fail("%s leaves %d parenthesis/es unclosed" % (flag, depth))
    if in_string:
        _fail("%s leaves a double-quoted string unterminated" % flag)


def _logs_filter(args: argparse.Namespace) -> str:
    clauses = [
        'resource.type="k8s_container"',
        'resource.labels.namespace_name="%s"' % _literal(_namespace(), "the namespace"),
        'timestamp>="%s"' % _rfc3339_hours_ago(args.hours),
    ]
    if getattr(args, "severity", None):
        clauses.append('severity>="%s"' % _literal(args.severity.upper(), "--severity"))
    if getattr(args, "container", None):
        clauses.append(
            'resource.labels.container_name="%s"' % _literal(args.container, "--container")
        )
    if getattr(args, "agent_files", False):
        clauses.append('jsonPayload.log_source="agent-file"')
    if getattr(args, "query", None):
        _check_query(args.query)
        clauses.append("(%s)" % args.query)
    if not getattr(args, "include_self", False):
        clauses.append('NOT resource.labels.pod_name:"%s"' % SELF_SERVICE_NAME)
    return " AND ".join(clauses)


def _rfc3339_hours_ago(hours: float) -> str:
    import datetime as dt

    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _span_ms(span: Dict[str, Any]) -> Optional[float]:
    """Wall-clock milliseconds for one Cloud Trace span, or None if unparseable.

    Returned as a number because the caller sorts on it. A latency finding that
    has to be argued from two RFC3339 strings is a latency finding the agent
    will get wrong or, more often, not attempt.
    """
    import datetime as dt

    def parse(text: Any) -> Optional[dt.datetime]:
        if not isinstance(text, str):
            return None
        try:
            # Cloud Trace sends `Z`; fromisoformat wants an offset before 3.11,
            # and nanosecond precision is longer than %f accepts either way.
            body, _, frac = text.rstrip("Z").partition(".")
            when = dt.datetime.strptime(body, "%Y-%m-%dT%H:%M:%S")
            if frac:
                when = when.replace(microsecond=int(frac[:6].ljust(6, "0")))
            return when
        except (ValueError, TypeError):
            return None

    start, end = parse(span.get("startTime")), parse(span.get("endTime"))
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() * 1000.0, 1)


# --------------------------------------------------------------------------
# Traces
# --------------------------------------------------------------------------


def cmd_traces(args: argparse.Namespace) -> int:
    """Cloud Trace spans, which is where a latency finding has to come from.

    A log line records that a turn happened; a span tree records which tool call
    inside it consumed the wall clock. Signal 3 in sec. 4 is not answerable from
    timestamps in a log, which is why this subcommand exists rather than being
    folded into `logs`.
    """
    params = {
        "startTime": _rfc3339_hours_ago(args.hours),
        "pageSize": str(min(args.limit, 1000)),
        "view": "ROOTSPAN" if not args.full else "COMPLETE",
    }
    filters = []
    if args.span:
        filters.append("span:%s" % args.span)
    # Nothing is added to `filters` for `--include-self`: Cloud Trace has no NOT
    # operator, so the loop's own traces are dropped after the fact, below. The
    # page size is spent on them either way; on an install where the loop
    # dominates the trace volume, pass --service to narrow it at the source.
    if args.service:
        filters.append("+root:%s" % args.service)
    if filters:
        params["filter"] = " ".join(filters)
    url = "https://cloudtrace.googleapis.com/v1/projects/%s/traces?%s" % (
        _project(),
        urllib.parse.urlencode(params),
    )
    payload = _google_api(url, method="GET")
    traces = payload.get("traces", [])
    rows = []
    for trace in traces:
        spans = trace.get("spans", [])
        if not spans:
            continue
        root = spans[0]
        name = root.get("name", "?")
        if not args.include_self and SELF_SERVICE_NAME in name:
            continue
        row = {
            "traceId": trace.get("traceId"),
            "root": name,
            "start": root.get("startTime"),
            "end": root.get("endTime"),
            "durationMs": _span_ms(root),
        }
        # Only under --full. The default ROOTSPAN view returns the root and
        # nothing else, so `len(spans)` there is the constant 1 -- a field that
        # reads like a measurement, is the same on every row, and would tell an
        # agent that every trace on the install is a single span.
        if args.full:
            row["spans"] = len(spans)
        # The point of --full. COMPLETE fetches the whole tree, and keeping only
        # the root's name and a span count discards precisely the thing signal 3
        # is about: a trace that says "this turn took 94 seconds" and nothing
        # about where the 94 seconds went cannot support a latency finding, and
        # the docstring above promises it can. A run that could not see inside
        # the tree had to attribute slowness to whatever it could see, which is
        # how the design's sec. 11 came to blame SQLite.
        if args.full and len(spans) > 1:
            timed = [
                {"name": s.get("name", "?"), "ms": _span_ms(s), "spanId": s.get("spanId")}
                for s in spans[1:]
            ]
            timed = [s for s in timed if s["ms"] is not None]
            timed.sort(key=lambda s: s["ms"], reverse=True)
            row["slowest"] = timed[: args.breakdown]
        rows.append(row)
    # Slowest first. The page size is a cap, so on a busy install the ordering
    # decides what the agent gets to look at, and for a latency hunt the useful
    # end is the top. Traces whose duration would not parse sort last rather
    # than crashing the sort.
    rows.sort(key=lambda r: (r.get("durationMs") is not None, r.get("durationMs") or 0), reverse=True)
    emit(rows)
    if not rows:
        emit_err("(no traces matched)")
    return 0


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def _metrics_filter(caller: str) -> str:
    """The caller's filter, ANDed with a clause that keeps it in this cluster.

    `roles/monitoring.viewer` is a project grant, so an unscoped filter reads
    every cluster in the project -- including the clusters under management,
    which sec. 1 puts on the other side of the line this whole feature is drawn
    around. The skill's own worked example
    (`--filter 'metric.type="kubernetes.io/container/restart_count"'`) named no
    cluster, so the default usage returned the fleet's restart counts and left
    SOUL.md's "do not report on a cluster under management" as the only thing
    standing between that and a finding.

    Appended only when the caller is asking for `kubernetes.io/` metrics and has
    not already named a cluster. Monitoring rejects the whole request when a
    filter names a label the resource type does not carry, and `cluster_name` is
    not on the resource types behind `logging.googleapis.com/*` or the
    per-service quota metrics -- so an unconditional clause would turn a working
    query into a 400. `_drop_other_clusters` covers what this cannot.
    """
    _check_query(caller, "--filter")
    if "kubernetes.io/" not in caller or "cluster_name" in caller:
        return caller
    cluster = os.environ.get("GKE_CLUSTER_NAME", "").strip()
    if not cluster:
        return caller
    return '(%s) AND resource.labels.cluster_name="%s"' % (
        caller,
        _literal(cluster, "GKE_CLUSTER_NAME"),
    )


def _is_other_cluster(labels: Dict[str, Any]) -> bool:
    """Whether a returned row belongs to a cluster this loop does not audit.

    The API-side clause in `_metrics_filter` cannot be applied to every metric
    type, so this is the backstop that runs on all of them. Absent labels mean
    absent scoping information, not a foreign cluster: a resource type with no
    `cluster_name` is not a cluster under management, it is something like a
    log-bytes counter, and dropping it would silently empty a legitimate query.
    """
    cluster = os.environ.get("GKE_CLUSTER_NAME", "").strip()
    row = str(labels.get("cluster_name", "")).strip()
    return bool(cluster and row and row != cluster)


def cmd_metrics(args: argparse.Namespace) -> int:
    """One Cloud Monitoring time series, for the numbers a span cannot give.

    Container restarts, memory working set, CPU throttling: fleet-level shapes
    that show a problem is systemic rather than one bad session -- fleet-level
    within this install's own cluster, which `_metrics_filter` is what enforces.
    """
    params = {
        "filter": _metrics_filter(args.filter),
        "interval.startTime": _rfc3339_hours_ago(args.hours),
        "interval.endTime": _rfc3339_hours_ago(0),
        "view": "FULL",
        "pageSize": "200",
    }
    url = "https://monitoring.googleapis.com/v3/projects/%s/timeSeries?%s" % (
        _project(),
        urllib.parse.urlencode(params),
    )
    payload = _google_api(url, method="GET")
    series = payload.get("timeSeries", [])
    out = []
    for entry in series:
        labels = entry.get("resource", {}).get("labels", {})
        # Dropped here rather than in the filter, for the reason `traces` gives:
        # the query language has no clean negation over a label that may not
        # exist on the resource type, and an appended clause that names a
        # missing label fails the whole request rather than narrowing it. The
        # page is spent on the rows either way.
        if not getattr(args, "include_self", False) and _is_self(*labels.values()):
            continue
        # `--include-self` widens the self-exclusion and nothing else. A cluster
        # under management is not this loop's business in any mode, so there is
        # no flag that turns this one off.
        if _is_other_cluster(labels):
            continue
        points = entry.get("points", [])
        out.append(
            {
                "metric": entry.get("metric", {}),
                "resource": labels,
                "points": len(points),
                "latest": points[0].get("value") if points else None,
            }
        )
    emit(out)
    return 0


# --------------------------------------------------------------------------
# Kubernetes
# --------------------------------------------------------------------------


def _is_self(*values: Any) -> bool:
    """Whether any of these strings names the loop's own machinery.

    SOUL.md sec. 1 tells the agent "the evidence tools filter you out by
    default", and the agent acts on that: it reads what comes back as being
    about the system under observation. Four of the eight reads did not
    actually filter -- `k8s pods` returned the runner's own CronJob pod with
    its restart counts, `k8s configmaps` returned the ledger the run is
    currently writing, and `metrics` returned the runner's own container
    series. Each is a finding the loop can report about itself while believing
    it is reporting on the agent, and the ledger one is a feedback loop: the
    run sees its own ledger, files a finding about it, and grows the thing it
    is looking at.

    Substring rather than exact match because the names are all derived from
    `SELF_SERVICE_NAME` by suffix -- `-ledger`, `-investigator`, the CronJob's
    generated pod suffix -- and a list of them would go stale the first time
    one was added.
    """
    return any(SELF_SERVICE_NAME in (v or "") for v in values if isinstance(v, str) or v is None)


#: (connect, read) for every API call in this module. See the note on
#: `selfimprove_run.KUBE_API_TIMEOUT`: the client waits forever by default, and
#: an egress policy that drops rather than rejects turns each of the reads below
#: into a multi-minute stall charged to the investigation turn's budget.
API_TIMEOUT = (5, 15)


def _kube():
    from kubernetes import client, config as kube_config  # noqa: PLC0415

    try:
        kube_config.load_incluster_config()
    except Exception:  # pragma: no cover - only outside a pod
        kube_config.load_kube_config()
    return client


def _within_hours(when: Any, hours: Optional[float]) -> bool:
    """Whether an event timestamp falls inside the window. Missing time is kept.

    An event whose `lastTimestamp` is unset is a real event the API did not
    stamp, and dropping it would silently narrow the read -- the opposite of
    what a window is asked for.
    """
    if not hours or hours <= 0 or when is None:
        return True
    import datetime as dt

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    stamped = when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
    return stamped >= cutoff


def _env_summary(container: Any) -> List[Dict[str, str]]:
    """One container's env, in the form the inefficiency signal actually needs.

    Sec. 3.3 says the loop should be able to find "an env var that never made it
    out of the CR", which is not answerable from images and conditions -- so
    without this the `deployments` read cannot support the finding the design
    names for it.

    A literal `value` is printed (through the redactor, like every other output)
    because it is already readable by anything holding `view` and is the whole
    point of the check. A `valueFrom` prints its source and never its content:
    `view` excludes Secrets, so the value is not available here anyway, and
    naming the reference is what answers "is it wired up".
    """
    rows: List[Dict[str, str]] = []
    for var in container.env or []:
        if var.value is not None:
            rows.append({"name": var.name, "value": var.value})
            continue
        source = "(unset)"
        ref = var.value_from
        if ref is not None:
            if ref.secret_key_ref is not None:
                source = "secretKeyRef:%s/%s" % (ref.secret_key_ref.name, ref.secret_key_ref.key)
            elif ref.config_map_key_ref is not None:
                source = "configMapKeyRef:%s/%s" % (
                    ref.config_map_key_ref.name,
                    ref.config_map_key_ref.key,
                )
            elif ref.field_ref is not None:
                source = "fieldRef:%s" % ref.field_ref.field_path
            elif ref.resource_field_ref is not None:
                source = "resourceFieldRef:%s" % ref.resource_field_ref.resource
        rows.append({"name": var.name, "from": source})
    return rows


def _container_state(state: Any) -> Optional[Dict[str, Any]]:
    """Which of a container's three states is set, and why it is set.

    Not `list(state.to_dict().keys())`. The generated `to_dict()` walks
    `openapi_types` and assigns every attribute, unset ones included, so those
    keys are the constant ["running", "terminated", "waiting"] for every
    container of every pod -- identical output for a healthy pod and one in
    CrashLoopBackOff, and no way for a reader to tell which it was looking at.

    The reason is the part worth carrying: CrashLoopBackOff,
    ImagePullBackOff and CreateContainerConfigError all arrive as
    `waiting.reason`, and a container that exited arrives as
    `terminated.reason` with an exit code. Those are the pod-level facts the
    loop's investigations are built on.
    """
    if state is None:
        return None
    for name in ("waiting", "terminated", "running"):
        sub = getattr(state, name, None)
        if sub is None:
            continue
        out: Dict[str, Any] = {"state": name}
        for attr, key in (
            ("reason", "reason"),
            ("message", "message"),
            ("exit_code", "exitCode"),
            ("signal", "signal"),
            ("started_at", "startedAt"),
            ("finished_at", "finishedAt"),
        ):
            value = getattr(sub, attr, None)
            if value is not None:
                out[key] = value if isinstance(value, int) else str(value)
        return out
    # Every state attribute unset. Real, if brief: it is what the API returns
    # between a pod being scheduled and its first container status arriving.
    return {"state": "unknown"}


def cmd_k8s(args: argparse.Namespace) -> int:
    """Read the release namespace through the pod's own `view` binding.

    Deliberately not a kubectl passthrough. There is no kubectl in this image
    outside the credential-proxy shims, and adding a general "run this argv"
    door would make the read-only posture a matter of the argv policy rather
    than of the grant. These five reads are what sec. 3.3 says the loop needs.
    """
    client = _kube()
    core = client.CoreV1Api()
    apps = client.AppsV1Api()
    custom = client.CustomObjectsApi()
    ns = _namespace()
    out: Any

    if args.what == "pods":
        out = [
            {
                "name": p.metadata.name,
                "phase": p.status.phase,
                "startTime": str(p.status.start_time),
                "containers": [
                    {
                        "name": cs.name,
                        "ready": cs.ready,
                        "restarts": cs.restart_count,
                        "image": cs.image,
                        "state": _container_state(cs.state),
                        "lastTerminated": (
                            cs.last_state.terminated.to_dict() if cs.last_state and cs.last_state.terminated else None
                        ),
                    }
                    for cs in (p.status.container_statuses or [])
                ],
            }
            for p in core.list_namespaced_pod(ns, _request_timeout=API_TIMEOUT).items
            if args.include_self or not _is_self(p.metadata.name)
        ]
    elif args.what == "deployments":
        out = [
            {
                "name": d.metadata.name,
                "replicas": d.status.replicas,
                "ready": d.status.ready_replicas,
                "images": [c.image for c in d.spec.template.spec.containers],
                "containers": [
                    {"name": c.name, "env": _env_summary(c)}
                    for c in d.spec.template.spec.containers
                ],
                "conditions": [
                    {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                    for c in (d.status.conditions or [])
                ],
            }
            for d in apps.list_namespaced_deployment(ns, _request_timeout=API_TIMEOUT).items
            if args.include_self or not _is_self(d.metadata.name)
        ]
    elif args.what == "events":
        out = [
            {
                "type": e.type,
                "reason": e.reason,
                "object": "%s/%s" % (e.involved_object.kind, e.involved_object.name),
                "count": e.count,
                "message": e.message,
                "last": str(e.last_timestamp),
            }
            for e in core.list_namespaced_event(ns, _request_timeout=API_TIMEOUT).items
            if (args.include_self or not _is_self(e.involved_object.name))
            and _within_hours(e.last_timestamp, args.hours)
        ]
    elif args.what == "configmaps":
        # Names and keys only. The values can be large and can carry install
        # identifiers, and a finding needs to know a key exists far more often
        # than it needs its contents.
        out = [
            {"name": c.metadata.name, "keys": sorted((c.data or {}).keys())}
            for c in core.list_namespaced_config_map(ns, _request_timeout=API_TIMEOUT).items
            if args.include_self or not _is_self(c.metadata.name)
        ]
    elif args.what == "platformagents":
        # No `_is_self` filter on either custom resource, and `--include-self`
        # therefore changes nothing here. The loop is a CronJob: it owns no
        # PlatformAgent, and the chart renders no AgentPlugin at all. Every
        # object these two return belongs to the agent under observation, which
        # is the thing a finding is about.
        out = custom.list_namespaced_custom_object(
            group="kubeagents.x-k8s.io",
            version="v1alpha1",
            namespace=ns,
            plural="platformagents",
            _request_timeout=API_TIMEOUT,
        ).get("items", [])
    elif args.what == "agentplugins":
        out = custom.list_namespaced_custom_object(
            group="kubeagents.x-k8s.io",
            version="v1alpha1",
            namespace=ns,
            plural="agentplugins",
            _request_timeout=API_TIMEOUT,
        ).get("items", [])
    else:  # pragma: no cover - argparse constrains this
        _fail("unknown subject %r" % args.what)
        return 2

    emit(out)
    return 0


# --------------------------------------------------------------------------


class RedactingArgumentParser(argparse.ArgumentParser):
    """argparse's diagnostics, routed through the redactor like everything else.

    argparse echoes the offending value back verbatim, and the values here are
    not the agent's own: `_check_query` says outright that `--query` is composed
    "from text it read in logs it does not control". So

        logs --hours "acme-prod-1 alice@example.com ghp_..."

    printed `invalid float value: 'acme-prod-1 alice@example.com ghp_...'` to
    stderr, past `emit` entirely, with the tokens intact. A bad argument is also
    the case where the value is most likely to be strange, which is to say most
    likely to be something that was never meant to be a value at all.

    Only what goes to stderr. `--help` is this file's own static text on stdout,
    and running it through the identifier rules would blank words out of the
    help for no gain. `_print_message` is the single funnel for both, so
    overriding it covers the usage line argparse prints beside an error as well
    as the error itself, and `add_subparsers` builds each subparser from
    `type(self)` -- which matters, because the example above is a subparser's
    error and not this parser's.
    """

    def _print_message(self, message: str, file: Any = None) -> None:
        if message and file is sys.stderr and _REDACT:
            message = redact(message)
        super()._print_message(message, file)


def build_parser() -> argparse.ArgumentParser:
    parser = RedactingArgumentParser(
        prog="selfimprove-evidence",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--no-redact",
        action="store_true",
        help=(
            "print unredacted output. Credential and identifier shapes -- tokens, email "
            "addresses, project and cluster names, IP addresses, chat space ids -- are blanked "
            "by default because whatever an investigation quotes can end up in a public pull "
            "request. Use this only when reading the output yourself, never in a run whose "
            "findings may be filed."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_log_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--hours", type=float, default=24, help="how far back to look (default 24)")
        p.add_argument("--severity", default="", help="minimum severity, e.g. ERROR or WARNING")
        p.add_argument("--container", default="", help="restrict to one container name")
        p.add_argument(
            "--agent-files",
            action="store_true",
            help="only records fluent-bit lifted off the agent's /opt/data/logs files",
        )
        p.add_argument("--query", default="", help="extra Cloud Logging filter, ANDed with the rest")
        p.add_argument(
            "--include-self",
            action="store_true",
            help="include the self-improvement loop's own records (for debugging the loop only)",
        )

    p_logs = sub.add_parser("logs", help="Cloud Logging entries for the install's namespace")
    add_log_args(p_logs)
    p_logs.add_argument("--limit", type=int, default=50)
    p_logs.add_argument("--width", type=int, default=400, help="truncate each payload to this many characters")
    p_logs.add_argument("--raw", action="store_true", help="print the raw API entries as JSON")
    p_logs.set_defaults(func=cmd_logs)

    p_count = sub.add_parser("logs-count", help="count matching log entries, bucketed by container and severity")
    add_log_args(p_count)
    p_count.add_argument("--max-pages", type=int, default=10, help="stop after this many 1000-entry pages")
    p_count.set_defaults(func=cmd_logs_count)

    p_traces = sub.add_parser("traces", help="Cloud Trace spans, slowest first, with durations")
    p_traces.add_argument("--hours", type=float, default=24)
    p_traces.add_argument("--limit", type=int, default=100)
    p_traces.add_argument("--span", default="", help="substring match on a span name")
    p_traces.add_argument("--service", default="", help="restrict to one root span name prefix")
    p_traces.add_argument(
        "--full",
        action="store_true",
        help="COMPLETE view: adds a `slowest` breakdown of the child spans inside each trace, "
        "which is what a latency finding has to name",
    )
    p_traces.add_argument(
        "--breakdown",
        type=int,
        default=5,
        help="how many child spans --full reports per trace (default 5)",
    )
    p_traces.add_argument("--include-self", action="store_true")
    p_traces.set_defaults(func=cmd_traces)

    p_metrics = sub.add_parser("metrics", help="one Cloud Monitoring time series")
    p_metrics.add_argument(
        "--filter",
        required=True,
        help='a Monitoring filter, e.g. metric.type="kubernetes.io/container/restart_count"',
    )
    p_metrics.add_argument("--hours", type=float, default=24)
    p_metrics.add_argument("--include-self", action="store_true")
    p_metrics.set_defaults(func=cmd_metrics)

    p_k8s = sub.add_parser("k8s", help="read the release namespace through the pod's `view` binding")
    p_k8s.add_argument(
        "what",
        choices=["pods", "deployments", "events", "configmaps", "platformagents", "agentplugins"],
    )
    p_k8s.add_argument("--include-self", action="store_true")
    p_k8s.add_argument(
        "--hours",
        type=float,
        default=0,
        help="for `events`, keep only those seen in the last N hours (default: all)",
    )
    p_k8s.set_defaults(func=cmd_k8s)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    global _REDACT
    args = build_parser().parse_args(argv)
    _REDACT = not args.no_redact
    if not _REDACT:
        print(
            "warning: --no-redact. Tokens, email addresses, project and cluster names and IP "
            "addresses will be printed as they are. Nothing from this invocation belongs in a "
            "finding.",
            file=sys.stderr,
        )
    try:
        return args.func(args)
    except SystemExit:
        # `_fail` has already been through `emit_err`.
        raise
    except Exception as exc:
        # An uncaught exception was the last output path with no redactor on
        # it, and the exception most likely to be raised here is the worst one
        # to print: `kubernetes.client.ApiException.__str__` renders status,
        # reason, every response header and the whole body, and a 403 body
        # names the service account, the namespace and the resource it was
        # refused. The traceback is dropped rather than redacted -- it is this
        # file's own frames, so it says nothing the exception does not, and
        # `--no-redact` prints the message itself in full.
        _fail("%s: %s" % (type(exc).__name__, exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

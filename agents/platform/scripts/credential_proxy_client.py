#!/usr/bin/env python3
"""Submit a supported CLI argv vector to the paired credential proxy."""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_EXECUTABLES = ("kubectl", "gcloud", "gh", "git")

# How long to wait to reach the broker. Bounds the connect only — see
# BrokerConnection.
BROKER_CONNECT_TIMEOUT_SECONDS = 10.0

# `\Z`, not `$`. `$` also matches immediately before a trailing newline, so
# `re.match` on "nowhere\n" succeeds -- and that value goes on to build the
# scope key in a log line and a filename in the broker's state dir. `fullmatch`
# at the call site says the same thing twice on purpose: whichever a later
# reader changes, the other still holds.
_GKE_CONTEXT_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")

# Enough for any real kubeconfig; the point is that the file is read into
# memory before anything is known about it.
MAX_KUBECONFIG_BYTES = 1 << 20


class BrokerConnection(http.client.HTTPConnection):
    """Bound how long we wait to reach the broker, not how long it works.

    A plain ``urlopen(request, timeout=N)`` sets one socket timeout for the
    whole exchange, which would put a ceiling on the command as well as on the
    connect. That ceiling must not exist: Envoy routes /v1/exec with
    ``timeout: 0s`` deliberately, because a proxied ``gcloud container clusters
    get-credentials`` or a large ``git clone`` legitimately runs for minutes.

    Before the split there was no need for either — the broker was on the Pod's
    own loopback, so a connect either succeeded or was refused at once. Now the
    call crosses a Service. A Pending broker still fails fast, with
    ``[Errno 111] Connection refused`` from a Service that has no endpoints;
    what hangs is a SYN that is dropped rather than rejected, which is exactly
    what a default-deny egress policy does. So: a timeout while connecting, and
    none once connected.
    """

    def connect(self) -> None:
        self.timeout = BROKER_CONNECT_TIMEOUT_SECONDS
        super().connect()
        self.sock.settimeout(None)


class _BrokerHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(BrokerConnection, req)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect out of the broker.

    urllib re-sends the Authorization header across a cross-host redirect, so
    a 302 in a broker response would hand the projected token to wherever the
    Location points. Only reachable by something that already controls the
    broker's responses, but the header is the one thing worth not leaking on
    the way out.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


# A private opener rather than urllib.request.install_opener: this module is
# imported by github_token_refresh and the two relay patches, and a global
# opener would strip the total timeouts their own urlopen calls rely on.
_BROKER_OPENER = urllib.request.build_opener(_BrokerHTTPHandler, _NoRedirect())


def open_broker_request(request: urllib.request.Request):
    """Send `request` to the broker with a bounded connect."""
    return _BROKER_OPENER.open(request)


class TokenUnavailable(Exception):
    """The configured caller token could not be read."""


def authorization_headers() -> dict[str, str]:
    """Return the credential that identifies this caller to the broker.

    The operator projects a ServiceAccount token with the broker's audience into
    every container that may call, and points CREDENTIAL_PROXY_TOKEN_FILE at it.
    Empty when that variable is unset, which is a misconfiguration rather than a
    layout: the broker is on a ClusterIP and authenticates every caller by
    TokenReview, so a request with no header earns a 401 that says so.

    Read on every invocation, never cached: the kubelet rewrites a projected
    token in place as it approaches expiry, and this process is short-lived
    enough that re-reading costs nothing.
    """
    token_file = os.environ.get("CREDENTIAL_PROXY_TOKEN_FILE", "").strip()
    if not token_file:
        return {}
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TokenUnavailable(f"{token_file}: {exc.strerror or exc}") from exc
    if not token:
        raise TokenUnavailable(f"{token_file} is empty")
    return {"Authorization": f"Bearer {token}"}

# Only these read KUBECONFIG: kubectl to pick a context, gcloud to write one in
# `container clusters get-credentials`. `git` and `gh` ignore the variable, so
# resolving it for them buys nothing and costs plenty — an unreadable kubeconfig
# is a hard failure, which would turn a stray KUBECONFIG into a refused `gh pr
# create`.
KUBECONFIG_AWARE = frozenset({"kubectl", "gcloud"})

# Flags whose value may be `-`, meaning "read the document from stdin". This is
# the whole list the shipped skills use: kubectl's `-f`/`--filename` and
# `--patch-file`, and gh's `--body-file`.
#
# `gh`'s `-F` short form is deliberately absent, and every caller that used to
# pass it now spells `--body-file` instead. It is not a synonym: `gh api -F
# key=value` sets a typed field, so matching it here would forward fd 0 for an
# API call that never asked for it. When a new call site needs a document from
# stdin, widen it to the long flag rather than adding the short one.
STDIN_FILE_FLAGS = frozenset({"-f", "--filename", "--patch-file", "--body-file"})


def reads_stdin(argv: list[str]) -> bool:
    """Whether this argv asks, explicitly, to read a document from stdin.

    The shim has never forwarded stdin, and the comment in `__main__` gives the
    reason: an MCP or other stdio-based parent may have a protocol stream on
    fd 0, and consuming it would break the parent rather than the command. That
    reason is sound and this does not overrule it -- it narrows it. Reading fd 0
    only when a flag in `STDIN_FILE_FLAGS` is followed by a bare `-` means the
    read happens when the caller wrote `kubectl apply -f -` and at no other
    time, and no MCP server is invoked that way.

    The consequence of getting this wrong is asymmetric and the narrow form errs
    the safe way: reading when we should not corrupts a parent's protocol
    stream, while not reading when we should leaves the command receiving an
    empty document -- which is exactly the behaviour today.
    """
    for index, token in enumerate(argv):
        if token in STDIN_FILE_FLAGS and index + 1 < len(argv) and argv[index + 1] == "-":
            return True
        if "=" in token:
            flag, _, value = token.partition("=")
            if flag in STDIN_FILE_FLAGS and value == "-":
                return True
    return False


@dataclass(frozen=True)
class ClusterTarget:
    """A GKE cluster identified well enough to re-fetch credentials for it."""

    project: str
    location: str
    cluster: str

    @property
    def context_name(self) -> str:
        return f"gke_{self.project}_{self.location}_{self.cluster}"


def parse_gke_context(context: str) -> ClusterTarget | None:
    """Recover the cluster triple from a `gke_<project>_<location>_<cluster>` name.

    This is the same convention the operator builds in `buildCredentialProxyEnv`
    and the Cluster Agent preflight compares against, and it is what makes the
    broker's regeneration possible: the context name alone says which cluster to
    ask Google for. Underscores are the separator and none of the three
    components may contain one, so a 4-way split is unambiguous.

    Each component is held to the GKE naming rules, which is also what keeps the
    value safe to use in a filename — no separators, no dots, no traversal, and
    no newline, which `$` would have let through and `context_name` would then
    have carried into a path and a log record.
    """
    parts = context.split("_", 3)
    if len(parts) != 4 or parts[0] != "gke":
        return None
    project, location, cluster = parts[1], parts[2], parts[3]
    if not all(
        _GKE_CONTEXT_COMPONENT.fullmatch(part) for part in (project, location, cluster)
    ):
        return None
    return ClusterTarget(project=project, location=location, cluster=cluster)


def read_current_context(text: str) -> str | None:
    """Read `current-context` out of a kubeconfig the way kubectl would.

    `yaml.safe_load`, deliberately, and never `yaml.CSafeLoader`. The C loader
    recurses in C, so a deeply nested document exits on SIGSEGV where the
    pure-Python loader raises a catchable `RecursionError`. `safe_load` picks
    the Python loader on its own; the point of saying so is that switching it
    would be a denial of service rather than an optimisation.

    Alias expansion is not a concern here. PyYAML resolves every reference to an
    anchor to the same node and caches the object built from it, so a
    billion-laughs document costs memory proportional to its own size rather
    than to its nominal expansion.

    Anything else — a syntax error, several documents, a top level that is not a
    mapping, a non-string `current-context` — reads as absent, and the caller
    turns that into a rejection.
    """
    import yaml  # lazy: keeps the module importable without pyyaml, as elsewhere in this directory

    try:
        document = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    context = document.get("current-context")
    if not isinstance(context, str):
        return None
    return context.strip() or None


class KubeconfigUnreadable(RuntimeError):
    """A kubeconfig was named, and no cluster name could be taken from it."""


def kubeconfig_context(kubeconfig: str) -> str:
    """The GKE context a local kubeconfig pins, to send in the file's place.

    The broker is in another pod and cannot open this file. It also should not:
    a kubeconfig is not passive data — `users[].user.exec.command` names a
    program to run, `clusters[].cluster.server` and `proxy-url` choose where the
    access token is sent, and `users[].user.tokenFile` reads a file of the
    author's choosing and sends it as the bearer token. The agent writes this
    file, so parsing it beside the credentials was the thing worth avoiding, and
    the pod boundary now makes it impossible rather than merely discouraged.

    What crosses instead is the one string the broker ever took from it. The
    broker holds that string to `parse_gke_context` and regenerates every other
    field with `gcloud container clusters get-credentials`, so naming a cluster
    is all the authority this hands the caller — and `get-credentials` is bound
    by the same IAM the broker already runs under.

    Raises rather than returning None. A kubeconfig that cannot be read is a
    request whose target cluster is unknown, and running it anyway means running
    it against the broker's own default cluster: the wrong cluster, silently.
    """
    entries = [entry.strip() for entry in kubeconfig.split(os.pathsep) if entry.strip()]
    if not entries:
        raise KubeconfigUnreadable("KUBECONFIG is set but empty")
    if len(entries) > 1:
        raise KubeconfigUnreadable(
            "KUBECONFIG must name a single file; merged lists are not supported"
        )
    candidate = Path(entries[0])
    try:
        if candidate.stat().st_size > MAX_KUBECONFIG_BYTES:
            raise KubeconfigUnreadable(f"kubeconfig is implausibly large: {candidate}")
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise KubeconfigUnreadable(f"kubeconfig is unreadable: {candidate}: {exc}") from exc
    context = read_current_context(text)
    if not context:
        raise KubeconfigUnreadable(f"kubeconfig names no current-context: {candidate}")
    if parse_gke_context(context) is None:
        raise KubeconfigUnreadable(
            f"current-context {context!r} is not a GKE context name"
            " (expected gke_<project>_<location>_<cluster>)"
        )
    return context


def resolve_kubeconfig_flags(argv: list[str]) -> list[str]:
    """Rewrite any `--kubeconfig <path>` in argv to the context that path pins.

    The flag is the second door into cluster selection and beats the environment
    in kubectl's own precedence, so it has to be translated here for the same
    reason the environment is: the path names a file only this pod has. The
    broker resolves whichever it receives through the same regeneration.
    """
    rewritten = list(argv)
    for index, argument in enumerate(rewritten):
        if argument == "--kubeconfig" and index + 1 < len(rewritten):
            rewritten[index + 1] = kubeconfig_context(rewritten[index + 1])
        elif argument.startswith("--kubeconfig="):
            _, _, value = argument.partition("=")
            rewritten[index] = f"--kubeconfig={kubeconfig_context(value)}"
    return rewritten


def is_get_credentials(argv: list[str]) -> bool:
    """Is this the one command that legitimately authors a kubeconfig?

    Matched on the subcommand sequence rather than on position, so global flags
    may appear anywhere ahead of it.
    """
    if not argv or argv[0] != "gcloud":
        return False
    try:
        index = argv.index("container")
    except ValueError:
        return False
    return argv[index + 1 : index + 3] == ["clusters", "get-credentials"]


def get_credentials_destination(argv: list[str]) -> tuple[list[str], Path | None]:
    """Take the `--kubeconfig` off a get-credentials argv, keeping where it pointed.

    This one command writes a kubeconfig rather than reading one, so the flag
    cannot be resolved to a context name — the file does not exist yet, and the
    cluster it will name is in the rest of the argv. The path also cannot be
    forwarded: it is a path in this pod, and gcloud runs in the broker's. So the
    flag comes off, the broker returns the file it generated, and `execute`
    writes it here.

    Falls back to `$KUBECONFIG`, which is where a Cluster Agent profile's pin
    lives when the caller did not name one.
    """
    stripped: list[str] = []
    destination: str | None = None
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--kubeconfig" and index + 1 < len(argv):
            destination = argv[index + 1]
            index += 2
            continue
        if argument.startswith("--kubeconfig="):
            destination = argument.split("=", 1)[1]
            index += 1
            continue
        stripped.append(argument)
        index += 1
    if destination is None:
        destination = os.environ.get("KUBECONFIG", "").strip() or None
    return stripped, Path(destination) if destination else None


def execute(
    endpoint: str,
    argv: list[str],
    stdin: str | None = None,
) -> int:
    # No `cwd`. The broker resolves a path against its own filesystem, and it
    # has no view of this one — so a directory sent from here would name either
    # nothing or, worse, a same-named directory of the broker's. Every command
    # runs at the broker's own workspace root instead.
    #
    # KUBECONFIG is the one thing an agent legitimately steers with a path:
    # Cluster Agent profiles pin themselves to a target cluster through it (see
    # agents/cluster/config.yaml). It survives the pod boundary by being
    # resolved to a context name here rather than forwarded as a path.
    # Whitespace is stripped because profile .env files routinely carry a
    # trailing newline.
    destination: Path | None = None
    try:
        context = ""
        if is_get_credentials(argv):
            argv, destination = get_credentials_destination(argv)
        elif argv and argv[0] in KUBECONFIG_AWARE:
            argv = resolve_kubeconfig_flags(argv)
            kubeconfig = os.environ.get("KUBECONFIG", "").strip()
            if kubeconfig:
                context = kubeconfig_context(kubeconfig)
    except KubeconfigUnreadable as exc:
        print(f"credential proxy: {exc}", file=sys.stderr)
        return 1

    request_payload = {
        "requestId": str(uuid.uuid4()),
        "argv": argv,
    }
    if context:
        request_payload["kubeconfigContext"] = context
    if destination is not None:
        request_payload["wantsKubeconfig"] = True
    if stdin is not None:
        request_payload["stdin"] = stdin
    body = json.dumps(
        request_payload,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        headers.update(authorization_headers())
    except TokenUnavailable as exc:
        # Sending the request anyway would earn an undifferentiated 401 and
        # hide the real fault, which is a broken token projection.
        print(f"credential proxy token unavailable: {exc}", file=sys.stderr)
        return 1
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/exec",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with open_broker_request(request) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        # The proxy's own errors are JSON, but the error can also come from
        # whatever sits between shim and proxy — an Envoy restarting mid-request
        # answers 503 with an HTML body, and a traceback here turns a transient
        # sidecar blip into a shim crash the agent cannot read.
        try:
            payload = json.load(exc)
        except (ValueError, TypeError):
            print(
                f"credential proxy error (HTTP {exc.code}): non-JSON response",
                file=sys.stderr,
            )
            return 1
        if payload.get("code") == "SECURITY_POLICY_BLOCKED":
            print(
                payload.get("message", "Command blocked for security reasons."),
                file=sys.stderr,
            )
            print(f"policy rule: {payload.get('rule', 'unknown')}", file=sys.stderr)
            return 126
        print(payload.get("error", str(exc)), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"credential proxy unavailable: {exc.reason}", file=sys.stderr)
        return 1

    sys.stdout.write(payload.get("stdout", ""))
    sys.stderr.write(payload.get("stderr", ""))
    if payload.get("truncated"):
        print("credential proxy output truncated", file=sys.stderr)
    generated = payload.get("kubeconfig")
    if destination is not None and generated:
        # gcloud's own output, written where gcloud would have written it had it
        # run here. The agent never runs a command against this file — a later
        # kubectl names the cluster and the broker regenerates from that name —
        # so this is the visible pin `cluster_agent_profile.py` records and the
        # Cluster Agent preflight stats, and nothing more.
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(generated, encoding="utf-8")
        except OSError as exc:
            print(f"credential proxy: could not write {destination}: {exc}", file=sys.stderr)
            return 1
    return int(payload.get("exitCode", 1))


class WorkspaceUnavailable(RuntimeError):
    """The broker does not have content workspaces armed."""


class WorkspaceRequestError(RuntimeError):
    """The broker refused. `status` and `payload` carry its answer verbatim."""

    def __init__(self, status: int, payload: dict) -> None:
        # Two spellings of the same field, because the broker has two error
        # shapes: a refusal from `ContentWorkspaceError` answers `{status,
        # code, message}`, while a malformed body answers `{error}`. Reading
        # only one of them would render half the broker's refusals as the
        # generic fallback below, which is the sentence that tells a caller
        # nothing.
        super().__init__(
            payload.get("message")
            or payload.get("error")
            or f"workspace request failed ({status})"
        )
        self.status = status
        self.payload = payload


class Listing(list):
    """The entries `list` returned, plus what it did not return.

    A plain list, so every existing caller keeps working, carrying the two
    fields that say whether it is the whole answer. A listing that stops at the
    broker's ceiling and looks complete is how a caller ends up asking `read`
    for a path it inferred rather than one it saw.
    """

    def __init__(self, entries, total: int = 0, truncated: bool = False) -> None:
        super().__init__(entries)
        self.total = total or len(self)
        self.truncated = truncated


class Workspace:
    """A git repository the broker owns and this process cannot see.

    There is no path anywhere in this class, which is the point. A caller says
    "write these bytes to `manifests/app.yaml` and commit them"; it never learns
    where that file lands, so it cannot be talked into reading or writing
    anything else there -- including `.git/config`, which is where a filter
    driver or a hook path would have to be defined for the sixteen known
    code-execution routes to work.

    Typical use, replacing a clone/add/commit/push sequence:

        with Workspace.open(endpoint, "acme/infra") as workspace:
            current = workspace.read_text("manifests/app.yaml")
            workspace.commit(
                branch="fix/replicas",
                message="raise replicas",
                changes={"manifests/app.yaml": patched.encode()},
            )
            workspace.push()
    """

    def __init__(self, endpoint: str, opened: dict) -> None:
        self.endpoint = endpoint
        self.handle = opened["handle"]
        self.repo = opened["repo"]
        self.base = opened["base"]
        self.base_sha = opened["baseSha"]
        self.branch_sha = opened.get("branchSha", "")
        self.started_from = opened.get("startedFrom", "")
        self.shallow = bool(opened.get("shallow", False))
        self.branch: str | None = None
        self._closed = False

    @classmethod
    def open(
        cls,
        endpoint: str,
        repo: str,
        base: str | None = None,
        branch: str | None = None,
        depth: int | None = None,
    ) -> "Workspace":
        """`branch` names the branch this session will commit to, if known.

        Naming it decides what `read` and `list` answer with: when the branch
        already exists on the remote -- a second round of review feedback -- the
        broker checks that out rather than the base, so a file read here is the
        file as the pull request has it.

        `depth` opens a shallow single-branch clone for reading. The broker
        refuses `commit` on one and refuses `depth` together with `branch`.
        """
        payload = {"repo": repo}
        if base:
            payload["base"] = base
        if branch:
            payload["branch"] = branch
        if depth:
            payload["depth"] = depth
        return cls(endpoint, _workspace_call(endpoint, "open", payload))

    def read(self, path: str) -> bytes:
        result = _workspace_call(
            self.endpoint, "read", {"handle": self.handle, "path": path}
        )
        return base64.b64decode(result["contentBase64"])

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read(path).decode(encoding)

    def read_many(self, paths: list[str]) -> tuple[dict[str, bytes], list[dict]]:
        """Several files in one round trip.

        Returns what came back and what did not, the second as the broker's own
        `{path, reason}` records. A caller that ignores the second half will
        materialise a partial tree and not know it -- `requestBudget` means ask
        again for the rest, `tooLarge` means that file is never coming, and
        `symlink` means the file is there but the broker will not follow a link
        to it, so ask for the target's own name.
        """
        result = _workspace_call(
            self.endpoint, "read", {"handle": self.handle, "paths": list(paths)}
        )
        files = {
            entry["path"]: base64.b64decode(entry["contentBase64"])
            for entry in result.get("files", [])
        }
        return files, result.get("skipped", [])

    def list(self, prefix: str | None = None, after: str | None = None) -> Listing:
        """One page of tracked names. `after` is the last path of the page before.

        `total` on the result counts what is still in scope after the cursor, so
        a caller pages until `truncated` is false.
        """
        payload = {"handle": self.handle}
        if prefix:
            payload["prefix"] = prefix
        if after:
            payload["after"] = after
        result = _workspace_call(self.endpoint, "list", payload)
        return Listing(
            result.get("entries", []),
            total=result.get("total", 0),
            truncated=bool(result.get("truncated")),
        )

    def grep(
        self,
        pattern: str,
        prefix: str | None = None,
        regex: bool = False,
        ignore_case: bool = False,
    ) -> dict:
        """Search the checked-out tree. Fixed-string unless `regex` is set.

        The whole answer, `{matches, total, truncated}`, rather than the matches
        alone: a search that hit the broker's ceiling and looks complete sends a
        reader off with a wrong conclusion about the repository.
        """
        payload = {"handle": self.handle, "pattern": pattern}
        if prefix:
            payload["prefix"] = prefix
        if regex:
            payload["regex"] = True
        if ignore_case:
            payload["ignoreCase"] = True
        return _workspace_call(self.endpoint, "grep", payload)

    def commit(
        self,
        branch: str,
        message: str,
        changes: dict[str, bytes | None],
        expected_base_sha: str | None = None,
        expected_branch_sha: str | None = None,
    ) -> dict:
        """`changes` maps a repository-relative path to bytes, or to None to delete.

        Pass `expected_base_sha` (normally `self.base_sha`) to have the broker
        refuse with 409 when the base branch has moved under a file this commit
        also writes. Leaving it out means last-writer-wins against whatever
        landed in the meantime.

        The working branch is checked the same way and needs no argument: the
        broker compares against the sha it last saw for that branch, which is
        the only place the value exists. `expected_branch_sha` overrides it for
        a caller that learned the sha somewhere else.
        """
        entries = []
        for path, content in changes.items():
            if content is None:
                entries.append({"path": path, "delete": True})
            else:
                entries.append(
                    {
                        "path": path,
                        "contentBase64": base64.b64encode(content).decode("ascii"),
                    }
                )
        payload = {
            "handle": self.handle,
            "branch": branch,
            "message": message,
            "changes": entries,
        }
        if expected_base_sha:
            payload["expectedBaseSha"] = expected_base_sha
        if expected_branch_sha:
            payload["expectedBranchSha"] = expected_branch_sha
        result = _workspace_call(self.endpoint, "commit", payload)
        self.branch = result["branch"]
        # `committed: false` is an ordinary answer rather than an error -- a
        # re-run whose fix is already on the branch has nothing to add -- and it
        # carries neither a sha nor a commit. Callers read the flag; reading
        # `result["commit"]` unconditionally is how that case turns into a
        # KeyError several frames from the decision that produced it.
        if result.get("baseSha"):
            self.base_sha = result["baseSha"]
        if result.get("branchSha") is not None:
            self.branch_sha = result["branchSha"]
        return result

    def push(self, branch: str | None = None) -> dict:
        branch = branch or self.branch
        if not branch:
            raise ValueError("nothing has been committed on this workspace yet")
        result = _workspace_call(
            self.endpoint, "push", {"handle": self.handle, "branch": branch}
        )
        if result.get("branchSha"):
            self.branch_sha = result["branchSha"]
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _workspace_call(self.endpoint, "close", {"handle": self.handle})

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *_exc) -> None:
        # Best effort: a failure to clean up a broker-side tree must not mask
        # the exception that is already propagating out of the with-block.
        try:
            self.close()
        except Exception:
            pass


def workspaces_available(endpoint: str) -> bool:
    """Whether this broker has content workspaces armed.

    Both mechanisms run side by side while the skills migrate, so a caller that
    can do either asks first rather than assuming.
    """
    try:
        _workspace_call(endpoint, "open", {"repo": ""})
    except WorkspaceUnavailable:
        return False
    except WorkspaceRequestError as exc:
        # 401 is the one status that says nothing about the route: the broker
        # rejects the caller before it looks at the path, so a client with no
        # token would read "workspaces are armed" off a broker that never
        # reached the question. Every other status is an answer about the
        # payload, and an answer about the payload means the route exists.
        #
        # Reported live: a sandbox with no CREDENTIAL_PROXY_TOKEN_FILE saw this
        # return True and then failed on the first real verb.
        if exc.status == 401:
            return False
        return True
    except TokenUnavailable:
        # No token to present, so nothing here is reachable whatever the broker
        # is serving.
        return False
    except urllib.error.URLError:
        return False
    return True


def _workspace_call(endpoint: str, verb: str, payload: dict) -> dict:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    # The same credential and the same opener `execute` uses. These routes
    # spend the broker's GitHub token exactly as /v1/exec does, so a
    # cross-Pod call that omitted the header would earn a 401, and one that
    # went through the stock opener would carry a total socket timeout onto a
    # clone that legitimately runs for minutes.
    headers.update(authorization_headers())
    request = urllib.request.Request(
        endpoint.rstrip("/") + f"/v1/workspace/{verb}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with open_broker_request(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            answer = json.load(exc)
        except (ValueError, TypeError):
            raise WorkspaceRequestError(exc.code, {"error": f"HTTP {exc.code}"}) from exc
        if answer.get("code") == "CONTENT_WORKSPACES_DISABLED":
            raise WorkspaceUnavailable(answer.get("error", "not enabled")) from exc
        raise WorkspaceRequestError(exc.code, answer) from exc


def read_stdin_if_requested(argv: list[str]) -> str | None:
    """fd 0, but only for an argv that named `-` as an input file.

    Still `None` when fd 0 is a terminal: an interactive `kubectl apply -f -`
    with nothing piped in would otherwise hang the shim on a read that never
    returns, which reads to the agent as the proxy being down.
    """
    if not reads_stdin(argv):
        return None
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        return sys.stdin.read()
    except (OSError, ValueError):
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--endpoint",
        default=os.getenv("CREDENTIAL_PROXY_URL"),
        required=os.getenv("CREDENTIAL_PROXY_URL") is None,
    )
    parser.add_argument(
        "executable",
        choices=SUPPORTED_EXECUTABLES,
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser.parse_args()


if __name__ == "__main__":
    invoked_as = os.path.basename(sys.argv[0])
    if invoked_as in set(SUPPORTED_EXECUTABLES):
        endpoint = os.getenv("CREDENTIAL_PROXY_URL")
        if endpoint is None:
            print("CREDENTIAL_PROXY_URL is not configured", file=sys.stderr)
            raise SystemExit(1)
        argv = [invoked_as, *sys.argv[1:]]
        stdin = read_stdin_if_requested(argv)
    else:
        args = parse_args()
        endpoint = args.endpoint
        argv = [args.executable, *args.arguments]
        stdin = read_stdin_if_requested(argv)
    raise SystemExit(
        execute(
            endpoint,
            argv,
            stdin=stdin,
        )
    )

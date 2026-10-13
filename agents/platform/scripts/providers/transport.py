#!/usr/bin/env python3
"""How a described API call actually gets made.

A verb describes the call it wants and the transport makes it. The split is
what keeps the rule that a forge says *what* to call and never *how* to execute
it, while still allowing a transport that is not a subprocess -- which is the
case a `api_command(...) -> argv` interface would have quietly ruled out.

The neutral request is:

    api(method, path, *, params=None, body=None, raw=None) -> Any

`params` is a dict rather than something a verb formats into the path, because
a dict is what gets URL-encoded; `f"...?state={state}"` does not. `raw` names a
media type rather than smuggling one through as a header, so a transport with
no notion of headers can still honour it.

What a transport owns, and no forge may:

- the executable, when there is one, and the working directory it runs in
- the timeout and the output ceiling, both of which come from the runner the
  broker hands in
- recovering a status from a failure -- an integer for an HTTP client, a parse
  of `(HTTP 404)` out of stderr for a CLI. That parse is a property of how the
  call was made, not of what the forge answered, which is why it is here and
  not in `errors.py`.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlencode

from workspace_paths import WorkspaceError

from .errors import (
    Guidance,
    Override,
    ca_missing_reason,
    ca_unloadable_reason,
    classify_tls,
    forge_error,
    tls_untrusted,
)

LOGGER = logging.getLogger(__name__)

# What a CLI prints when the call reached the forge and the forge said no.
_HTTP_STATUS_RE = re.compile(r"\(HTTP (\d{3})\)")
# The `<cli> auth status` convention: a line `Logged in to <host> account <login>`,
# which some CLI versions print on stdout and others on stderr; both are read.
_CLI_LOGIN_RE = re.compile(r"Logged in to \S+ account (\S+)")
# The other half of that convention: what `auth status` prints when it reached
# the forge and the forge rejected the credential. It carries no `(HTTP 401)` --
# the CLI phrases that answer in its own words rather than passing the status
# through -- so it needs its own marker, and it is the one non-zero exit of that
# command that is not transient.
_CLI_CREDENTIAL_REJECTED_RE = re.compile(
    r"token .{0,40}\bis invalid|authentication failed|bad credentials"
    r"|requires authentication|invalid or revoked",
    re.IGNORECASE,
)


class Transport(Protocol):
    """One authenticated API call against one forge."""

    def api(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        raw: str | None = None,
    ) -> Any: ...

    def whoami(self) -> str:
        """The login the credential authenticates as, or "" when it cannot say.

        On the transport rather than the forge because it is a property of how
        the call is authenticated, not of the API: a CLI reads it out of its
        credential store, an HTTP client asks the API's own "current user"
        route. An installation-style token cannot always introspect itself
        over HTTP -- the current-user route answers 401 for one -- which is
        why this is not a verb the forge composes.

        **A lookup that did not happen is not an empty login, and raises.**
        Empty says the credential answered and named nobody, and its callers
        read it as "do not compare", which drops a comparison rather than
        failing one. A timeout or a throttled call reaching them as "" would
        turn an outage into that silence, so it has to arrive as an error
        instead. The rule is `forge.viewer_login`'s, one layer down.
        """
        ...


def _with_query(path: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return path
    pairs = [(key, value) for key, value in params.items() if value is not None]
    if not pairs:
        return path
    joiner = "&" if "?" in path else "?"
    return f"{path}{joiner}{urlencode(pairs, doseq=True)}"


class CliTransport:
    """A forge CLI that follows the `<cli> api` convention.

    The convention is one subcommand -- `api` -- that takes a method, a path
    relative to the API root, and returns the API's own JSON on stdout. Nothing
    else about the CLI is used. The subcommands that read a repository out of a
    nearby `.git/config` are exactly the thing this design exists to keep away
    from the credential, and the ones that format for a human return something
    no translation can be written against.

    The body goes over stdin as JSON, not into argv. That is not only about
    generality -- though it is the only way to send a nested value -- it is
    also why a comment body cannot end up in a `CalledProcessError`, in `ps`,
    or in a log line written by something that did not know it was handling
    prose.
    """

    def __init__(
        self,
        runner: Callable[..., Any],
        executable: str,
        overrides: Mapping[int, Override] | None = None,
    ) -> None:
        self._runner = runner
        self._executable = executable
        self._overrides = overrides or {}

    def api(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        raw: str | None = None,
    ) -> Any:
        argv = [self._executable, "api", "--method", method, _with_query(path, params)]
        if raw:
            argv += ["-H", f"Accept: {raw}"]
        stdin = None
        if body is not None:
            argv += ["--input", "-"]
            stdin = json.dumps(body)
        done = self._runner(argv, stdin=stdin)
        if done.returncode != 0:
            raise self._failure(done.stderr or "", done.stdout or "")
        if raw:
            return done.stdout or ""
        try:
            return json.loads(done.stdout or "null")
        except json.JSONDecodeError as exc:
            raise WorkspaceError(
                "the forge returned something that is not JSON",
                status=502,
                code="FORGE_CALL_FAILED",
            ) from exc

    def whoami(self) -> str:
        done = self._runner([self._executable, "auth", "status"], stdin=None)
        if done.returncode != 0:
            output = f"{done.stdout or ''}\n{done.stderr or ''}"
            if _HTTP_STATUS_RE.search(output) or _CLI_CREDENTIAL_REJECTED_RE.search(
                output
            ):
                # The forge answered and said no. That has to be told apart
                # from the rest, because `FORGE_CALL_FAILED` reads "one retry
                # is reasonable" and a revoked token will never come back --
                # and this is the call an install makes first: the sweep asks
                # `viewer_login` of every managed repository before it asks
                # anything else, so a dead credential reported as a call
                # failure names a forge outage on every repository, every tick,
                # and the 401 a later verb would have produced is never
                # reached. 401 is the default rather than the answer: an
                # `(HTTP 4xx)` in the output wins, since a throttle of the
                # token-validation call prints its own status and is not a dead
                # credential.
                raise self._failure(done.stderr or "", done.stdout or "", default=401)
            # Everything else stays what it was. `auth status` exits non-zero on
            # a timeout -- 124, from the runner -- and when the validation call
            # it makes of its own accord cannot reach the host, and it prints no
            # login line in any of those, exactly as a credential that cannot
            # introspect itself prints none. Those the exit code cannot tell
            # apart from each other, and none of them is the forge's answer.
            raise WorkspaceError(
                f"`{self._executable} auth status` exited {done.returncode} "
                "without saying who the credential is",
                status=502,
                code="FORGE_CALL_FAILED",
            )
        found = _CLI_LOGIN_RE.search(f"{done.stdout or ''}\n{done.stderr or ''}")
        return found.group(1).strip() if found else ""

    def _failure(self, stderr: str, stdout: str = "", default: int = 0) -> WorkspaceError:
        """The forge's refusal, with the reason it actually gave as the detail.

        A CLI puts its summary on the first line of stderr -- `gh: Validation
        Failed (HTTP 422)` -- and the reason the caller needs on the lines
        after it, or in the API's JSON body on stdout: `A pull request already
        exists for …`, `No commits between main and x`. The shared guidance
        for 422 tells the agent to fix the field the detail names, so a detail
        that is only the summary line names nothing. Review caught exactly
        that. The detail is therefore the summary plus the reason: the body's
        `message` and each `errors[].message` (or `field`) when stdout is JSON,
        otherwise the stderr lines that follow the summary, bounded.
        """
        output = f"{stderr}\n{stdout}".strip()
        err_lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
        summary = err_lines[0] if err_lines else ""
        reasons: list[str] = []
        body: Any = None
        try:
            body = json.loads(stdout) if stdout.strip().startswith("{") else None
        except json.JSONDecodeError:
            body = None
        if isinstance(body, dict):
            if body.get("message"):
                reasons.append(str(body["message"]))
            for item in body.get("errors") or []:
                if isinstance(item, dict):
                    text = item.get("message") or item.get("field") or item.get("code")
                    if text:
                        reasons.append(str(text))
                elif isinstance(item, str):
                    reasons.append(item)
        if not reasons:
            reasons = err_lines[1:4]
        if not summary and not reasons:
            summary = stdout.strip().splitlines()[0] if stdout.strip() else ""
        detail = summary
        if reasons:
            joined = "; ".join(r for r in reasons if r and r != summary)
            if joined:
                detail = f"{summary}: {joined}" if summary else joined
        found = _HTTP_STATUS_RE.search(output)
        # A CLI that failed without ever reaching the forge -- it could not
        # resolve the host, or it has no credential loaded -- prints no status
        # at all. The default 0 matches nothing in the guidance table and lands
        # on the "did not say why" reading, which is the truth. A caller that
        # already knows what the absence means -- `whoami`, where the CLI
        # phrases the forge's 401 in prose instead of passing it through --
        # names the status it stands for instead.
        status = int(found.group(1)) if found else default
        # The first line is what the caller is shown; the whole output is what
        # an override reads, because the marker a forge uses for a throttle is
        # often on the line after the summary.
        return forge_error(status, detail, self._overrides, message=output)


# What a 3xx means here, since no route a forge declares answers with one: the
# configured host, or something in front of it, is wrong. Retrying changes
# nothing, and the credential was not sent on.
_REDIRECTED = Guidance(
    502,
    "FORGE_REDIRECTED",
    "The forge answered with a redirect, which no API route this broker calls "
    "should do. The credential was not sent on. This install's forge host is "
    "misconfigured, or something in front of it is redirecting; report it rather "
    "than retrying.",
)
# The size of each read off the socket. Small enough that the call's deadline
# is checked often against a peer that trickles, large enough that a page of
# JSON is a handful of reads.
_READ_CHUNK_BYTES = 64 * 1024

# How much of a refusal's body becomes the detail an override reads. The caller
# is shown 400 characters of it (`forge_error`); this bounds what is read off
# the socket to get there.
_ERROR_BODY_BYTES = 16 * 1024

#: How deep `_http_detail` looks for a reason inside an error body.
_DETAIL_DEPTH = 8


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an answer, not a hop.

    The credential rides in a header, and following a redirect would present
    it to wherever the forge -- or anything in front of it -- pointed. No API
    route a forge declares answers with one, so a redirect is a misconfigured
    host or something worse, and either way the call fails where it is.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


# One TLS context per CA file, rebuilt when the file changes. Building one
# loads the system bundle, so it is not done on every call; keyed on the
# file's identity so a Secret update reaches the next call with no restart.
_CA_CONTEXTS: dict[str, tuple[tuple[int, int], ssl.SSLContext]] = {}
_CA_CONTEXTS_LOCK = threading.Lock()


def ca_context(ca_file: str, host: str, ca_source: str = "") -> ssl.SSLContext:
    """The TLS context for a forge that names its own CA, at `ca_file`.

    The system bundle, plus the CA file. Only the forge that names the file
    gets this context, so a private CA never vouches for another host.

    Python 3.13 turns on VERIFY_X509_STRICT in the default context. That check
    refuses a CA certificate with no Key Usage extension, and a server
    certificate with no Authority Key Identifier, which many private CAs issue.
    It is cleared on this context only: the chain and the hostname are still
    verified, and a public forge keeps the strict check.

    A file that is not there raises FORGE_TLS_UNTRUSTED naming the host and,
    as `ca_source`, the Secret and key it comes from: the operator projects
    the Secret as optional, so the broker starts without it, and kubelet
    writes the file once the Secret exists. A wrong key reads the same as a
    missing Secret, so the answer names both.
    """
    try:
        status = os.stat(ca_file)
    except OSError as exc:
        raise tls_untrusted(
            host,
            ca_missing_reason(ca_source, f"the CA file {ca_file} is not mounted ({exc.strerror})"),
            "ca_missing",
        ) from exc
    identity = (status.st_mtime_ns, status.st_size)
    with _CA_CONTEXTS_LOCK:
        cached = _CA_CONTEXTS.get(ca_file)
        if cached is not None and cached[0] == identity:
            return cached[1]
        context = ssl.create_default_context()
        try:
            context.load_verify_locations(cafile=ca_file)
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise tls_untrusted(host, ca_unloadable_reason(ca_source, str(exc)), "ca_unloadable") from exc
        strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict and context.verify_flags & strict:
            context.verify_flags &= ~strict
        if cached is None:
            LOGGER.info(
                "forge %s trusts the CA in %s, with strict X.509 checks off for this host only",
                host,
                ca_file,
            )
        _CA_CONTEXTS[ca_file] = (identity, context)
        return context


#: How much of the request's time earlier calls must have spent before a cut is
#: reported as the request's deadline rather than as the forge being slow.
_REQUEST_SPENT_SECONDS = 1.0


def _unreachable(exc: BaseException) -> str:
    """Why the forge could not be reached, in words an operator can act on.

    `URLError` carries the reason -- a refused connection, an unknown name --
    and the type alone says none of it. A certificate never reaches here: the
    caller answers it first, as FORGE_TLS_UNTRUSTED, by its cause.
    """
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    reason = str(cause).strip()[:200]
    if reason:
        return f"the forge could not be reached: {type(exc).__name__}: {reason}"
    return f"the forge could not be reached: {type(exc).__name__}"


# OpenSSL verify codes for the causes that are not an untrusted chain.
_EXPIRED_CODES = frozenset({9, 10})  # not yet valid, expired
_HOSTNAME_CODES = frozenset({62, 64})  # hostname mismatch, IP address mismatch


def _certificate_failure(exc: BaseException) -> tuple[str, str]:
    """`(kind, reason)` when the send failed on the peer's certificate, else ("", "").

    The kind is read from the verifier's code where it gives one, and from its
    words otherwise, with the same markers git's output is read by, so both
    clients name a cause alike.
    """
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if not (isinstance(cause, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(cause)):
        return "", ""
    why = (getattr(cause, "verify_message", "") or str(cause)).strip()[:200]
    code = getattr(cause, "verify_code", None)
    if code in _EXPIRED_CODES:
        return "expired", why
    if code in _HOSTNAME_CODES:
        return "hostname", why
    kind, _line = classify_tls(why)
    return (kind if kind in ("expired", "hostname") else "untrusted"), why


def _broken_answer(exc: BaseException) -> str:
    """An answer the forge started and broke off, in words that say it answered."""
    reason = str(exc).strip()[:200]
    if reason:
        return f"the forge's answer could not be read: {type(exc).__name__}: {reason}"
    return f"the forge's answer could not be read: {type(exc).__name__}"


def _settimeout(response: Any, seconds: float) -> None:
    """Shorten the socket timeout under a response to `seconds`, if it has one.

    `http.client` keeps the socket two private attributes down (`fp.raw._sock`;
    an `HTTPError` holds the response one level further, as `fp`). Where the
    chain is not there -- a test double, or a future `http.client` -- the
    opener's timeout still bounds each receive and the deadline is still
    checked between them.
    """
    node = response
    for _ in range(2):
        sock = getattr(getattr(getattr(node, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(seconds)
            return
        node = getattr(node, "fp", None)


def _http_detail(text: str) -> str:
    """The reason a forge gave, out of a JSON error body or plain text.

    Forges disagree on the shape: `{"message": "..."}`, `{"error": "..."}`,
    a `message` that is a list of strings, or one that is a dict of per-field
    lists. The first strings found are joined; anything else is the body's
    first line.
    """
    try:
        body = json.loads(text) if text.strip()[:1] in "{[" else None
    except (json.JSONDecodeError, RecursionError):
        body = None
    reasons: list[str] = []

    def collect(value: Any, prefix: str = "", depth: int = 0) -> None:
        # Bounded, because `json.loads` accepts nesting deeper than Python's
        # frame limit: a body it parsed can still overflow a walk of it, and
        # the overflow would cost the refusal its status. No forge nests a
        # reason this deep.
        if depth > _DETAIL_DEPTH:
            return
        if isinstance(value, str) and value.strip():
            reasons.append(f"{prefix}{value.strip()}")
        elif isinstance(value, list):
            for item in value:
                collect(item, prefix, depth + 1)
        elif isinstance(value, dict):
            for key, item in value.items():
                collect(item, f"{key}: ", depth + 1)

    if isinstance(body, dict):
        for key in ("message", "error", "error_description", "errors"):
            if key in body:
                collect(body[key])
    elif isinstance(body, list):
        collect(body)
    if reasons:
        return "; ".join(reasons[:5])
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[0] if lines else ""


class HttpTransport:
    """A forge's REST API, called in-process.

    What the CLI transport gets from its runner this one is handed directly:
    the timeout and the response ceiling are the broker's, passed in at
    construction, and both are enforced here rather than trusted to a caller.
    The credential's headers are read per call, so a rotated token file is
    the next call's token.

    `whoami_route` is the forge's "current user" route and the field that
    names the login, or None when it has none -- in which case `whoami` says
    so with "" rather than guessing.
    """

    def __init__(
        self,
        base_url: str,
        headers: Callable[[], Mapping[str, str]],
        overrides: Mapping[int, Override] | None = None,
        *,
        timeout: float,
        max_bytes: int,
        whoami_route: tuple[str, str] | None = None,
        opener: Callable[..., Any] | None = None,
        outer_deadline: Callable[[], float | None] | None = None,
        ca_file: str = "",
        ca_source: str = "",
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("a forge API is reached over https only")
        self._base = base_url.rstrip("/")
        self._host = self._base.split("://", 1)[1].split("/", 1)[0]
        self._ca_file = ca_file
        self._ca_source = ca_source
        self._headers = headers
        self._overrides = overrides or {}
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._whoami_route = whoami_route
        self._outer_deadline = outer_deadline
        self._opener = opener
        # Built once for the transport's life when no CA file is named, as
        # before CA files existed; only a forge that names one builds per
        # call, so a remounted file is the next call's trust.
        self._fixed_opener = None if (opener is not None or ca_file) else self._build_opener()

    def _open(self, request: urllib.request.Request, timeout: float) -> Any:
        """Send `request`, trusting the forge's own CA when it names one.

        The opener is built per call when there is a CA file, so a missing
        file is that call's FORGE_TLS_UNTRUSTED and a remounted one is the
        next call's trust; `ca_context` caches the expensive part.
        """
        if self._opener is not None:
            return self._opener(request, timeout=timeout)
        if self._fixed_opener is not None:
            return self._fixed_opener.open(request, timeout=timeout)
        return self._build_opener().open(request, timeout=timeout)

    def _build_opener(self) -> urllib.request.OpenerDirector:
        """An opener that follows no redirect and trusts the forge's own CA, if any."""
        handlers: list[Any] = [_RefuseRedirect]
        if self._ca_file:
            handlers.append(
                urllib.request.HTTPSHandler(context=ca_context(self._ca_file, self._host, self._ca_source))
            )
        return urllib.request.build_opener(*handlers)

    def api(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        raw: str | None = None,
    ) -> Any:
        # A forge composes paths from validated segments; this is the backstop
        # that keeps one from naming its own host or climbing out of the API
        # root, where the credential header would follow it.
        segments = path.split("?", 1)[0].split("/")
        if "://" in path or ".." in segments:
            raise WorkspaceError(
                "the forge composed an API path this transport will not send",
                status=500,
                code="FORGE_CALL_FAILED",
            )
        url = f"{self._base}/{_with_query(path, params).lstrip('/')}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", raw or "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for name, value in self._headers().items():
            request.add_header(name, value)
        # One deadline for the whole call. The opener's timeout bounds each
        # socket operation, and a peer that sends a byte inside every window
        # would never trip it while holding one of the broker's request slots;
        # the deadline is the wall-clock bound the CLI runner gets from its
        # executor.
        #
        # A verb that loops makes many calls on one request, and the request
        # holds one broker slot under one shared deadline (`request_slot`);
        # `outer_deadline` is that one, and no call outlives it.
        now = time.monotonic()
        deadline = now + self._timeout
        outer = self._outer_deadline() if self._outer_deadline else None
        # Which bound cuts this call decides what a cut is reported as: the
        # forge being slow, or the request having spent its time on earlier
        # calls -- the second is the case the shared deadline exists for, and
        # reporting it as a 300s forge would send the operator after the
        # wrong thing.
        slow = f"the forge's answer took longer than {self._timeout:g}s"
        if outer is not None and outer < deadline:
            # The shared deadline always cuts the call when it is the sooner
            # bound. It is named as the cause only when earlier calls spent
            # real time: the slot is armed with the same timeout a moment
            # before the first call, so on that call the two differ by the
            # admission's few milliseconds, and the forge being slow is the
            # truth.
            if deadline - outer > _REQUEST_SPENT_SECONDS:
                slow = "the request's time ran out while the forge was answering"
            deadline = outer
        if deadline <= now:
            raise forge_error(0, "the request's time ran out before this call to the forge")
        # The opener gets the transport's own timeout unless the request's
        # deadline is sooner. Not `deadline - now` unconditionally: at
        # some monotonic clock readings `(now + t) - now` is not `t`, and
        # the per-receive bound would drift off the one configured.
        socket_timeout = self._timeout if deadline == now + self._timeout else deadline - now
        try:
            with self._open(request, timeout=socket_timeout) as response:
                payload = self._read_within(response, deadline, slow=slow)
        except urllib.error.HTTPError as exc:
            return self._refused(exc.code, self._error_text(exc, deadline))
        except WorkspaceError:
            raise
        except urllib.error.URLError as exc:
            # The send failed: a refused connection, an unknown name, a
            # certificate, a connect timeout. `urllib` wraps only the send.
            # A connect cut short by the request's shared deadline, after
            # earlier calls spent it, is the request's time, not the forge.
            if isinstance(exc.reason, TimeoutError) and slow.startswith("the request's time"):
                raise forge_error(0, "the request's time ran out while connecting to the forge") from exc
            kind, why = _certificate_failure(exc)
            if kind:
                raise tls_untrusted(self._host, why, kind) from exc
            raise forge_error(0, _unreachable(exc)) from exc
        except TimeoutError as exc:
            # Bare, so not the send: the request went out and the status line
            # never came back inside the bound. The forge was reached and
            # stopped, which is `slow`, not a connectivity problem.
            raise forge_error(0, slow) from exc
        except http.client.HTTPException as exc:
            # A status line, header or chunked body the peer broke: it
            # answered, and the answer could not be read.
            raise forge_error(0, _broken_answer(exc)) from exc
        except OSError as exc:
            # A reset or a dropped connection after the send -- `urllib` wraps
            # every send-phase failure in `URLError`, above. The forge took the
            # request and then stopped, which is an answer broken off, not a
            # forge that could not be reached.
            raise forge_error(0, _broken_answer(exc)) from exc
        text = payload.decode("utf-8", "replace")
        if raw:
            return text
        try:
            return json.loads(text or "null")
        except (json.JSONDecodeError, RecursionError) as exc:
            raise WorkspaceError(
                "the forge returned something that is not JSON",
                status=502,
                code="FORGE_CALL_FAILED",
            ) from exc

    def _error_text(self, exc: urllib.error.HTTPError, deadline: float) -> str:
        """What a refusal said, read under the same deadline as an answer.

        Best-effort: the status is the refusal, and the body only explains
        it. So a body that stalls, breaks off or resets yields what arrived
        rather than an exception -- raised here, inside the `except
        HTTPError` handler, none of the call's own failure handling would
        see it, and the forge's status would be lost to a bare 500.
        """
        try:
            payload = self._read_within(exc, deadline, cap=_ERROR_BODY_BYTES)
        except (WorkspaceError, http.client.HTTPException, OSError, ValueError):
            payload = b""
        return payload.decode("utf-8", "replace")

    def _read_within(
        self, response: Any, deadline: float, cap: int | None = None, slow: str = ""
    ) -> bytes:
        """The body, read in chunks until EOF, the ceiling, or the deadline.

        `read1` returns what one receive brought rather than waiting for a
        whole chunk, and the socket's timeout is re-armed with what is left
        of the deadline before each one, so no single receive outlasts it.
        The ceiling refuses an oversized answer as soon as it is crossed
        rather than after the whole body has arrived; `cap` instead stops
        there and returns what it has, for a body that is only explanation.

        `read1` answers `b""` at EOF without raising, so a body the peer cut
        short of its `Content-Length` would otherwise come back as the whole
        answer -- a truncated diff handed over as the diff. `length` is what
        `http.client` still expected; anything left is a broken answer.

        A stall is reported as one, in `slow`'s words, whichever way it shows:
        the check between receives, or -- the usual shape, since the socket's
        timeout is the deadline's remainder -- the receive itself timing out.
        The forge was reached; it stopped answering.
        """
        slow = slow or f"the forge's answer took longer than {self._timeout:g}s"
        read = getattr(response, "read1", None) or response.read
        chunks: list[bytes] = []
        size = 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise forge_error(0, slow)
            _settimeout(response, left)
            try:
                chunk = read(_READ_CHUNK_BYTES)
            except TimeoutError as exc:
                raise forge_error(0, slow) from exc
            if not chunk:
                if getattr(response, "length", None):
                    raise forge_error(0, "the forge closed the connection before its answer was complete")
                return b"".join(chunks)
            size += len(chunk)
            if cap is not None and size >= cap:
                chunks.append(chunk)
                return b"".join(chunks)[:cap]
            if size > self._max_bytes:
                raise WorkspaceError(
                    f"the forge's answer is larger than this broker accepts ({self._max_bytes} bytes)",
                    status=502,
                    code="FORGE_RESPONSE_TOO_LARGE",
                )
            chunks.append(chunk)

    def _refused(self, status: int, text: str) -> Any:
        if 300 <= status < 400:
            # `_RefuseRedirect` declines every hop, so a 3xx arrives here as an
            # `HTTPError`; the shared table has no entry for one.
            raise WorkspaceError(
                _REDIRECTED.text, status=_REDIRECTED.status, code=_REDIRECTED.code,
                detail=f"HTTP {status}",
            )
        raise forge_error(status, _http_detail(text), self._overrides, message=text)

    def whoami(self) -> str:
        if self._whoami_route is None:
            return ""
        path, field = self._whoami_route
        answer = self.api("GET", path)
        value = answer.get(field) if isinstance(answer, dict) else None
        return str(value).strip() if value else ""

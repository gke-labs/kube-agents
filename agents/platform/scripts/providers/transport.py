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

import json
import re
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlencode

from workspace_paths import WorkspaceError

from .errors import Override, forge_error

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

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

    def _failure(self, stderr: str, stdout: str = "") -> WorkspaceError:
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
        # at all. Status 0 matches nothing in the guidance table and lands on
        # the "did not say why" reading, which is the truth.
        status = int(found.group(1)) if found else 0
        # The first line is what the caller is shown; the whole output is what
        # an override reads, because the marker a forge uses for a throttle is
        # often on the line after the summary.
        return forge_error(status, detail, self._overrides, message=output)

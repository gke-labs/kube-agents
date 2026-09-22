"""Keep GCP access tokens out of the files a kanban worker leaves on the PVC.

Installed at ``/opt/hermes/agent/credential_redaction.py`` by
``deploy/docker/Dockerfile``; ``apply_credential_redaction.py`` wires it into
three upstream sites. Runtime code only: nothing here imports Hermes at module
load, so the unit tests beside it run without the image.

What leaked, and where (issue #603)
-----------------------------------
A worker that fetches a token by hand -- ``gcloud auth print-access-token`` or
the metadata server -- and pastes it into a ``curl`` writes it to disk twice on
the pinned Hermes (v2026.9.14, as on v2026.8.19). Two of the issue's four sinks were already
redacted upstream: ``hermes_logging.py`` formats ``agent.log`` through
``RedactingFormatter``, and ``tools/terminal_tool_result.py`` passes the
terminal output and its spill file through ``redact_terminal_output``. Two were
not:

1. **The pattern.** ``agent/redact.py``'s ``_PREFIX_PATTERNS`` has no entry for
   a GCP OAuth access token (``ya29.``), so a bare token -- an ``export
   TOKEN=``, a URL, a model echoing it -- is not recognised anywhere. The
   ``Authorization: Bearer`` header and a JSON ``access_token`` field are.
2. **The transcript.** ``hermes_cli/kanban_db_dispatch.py`` opens
   ``<board>/logs/<task>.log`` (``_open_worker_log``) and hands it to ``Popen``
   as the worker's stdout with stderr merged. Nothing between the worker's
   prints and that file redacts, and the ``💻 $`` completion line
   ``agent/display.py`` renders carries the whole command:
   ``redact_tool_args_for_display`` handles only ``browser_type``, and
   ``_cute_trunc`` (``_tail_trunc`` underneath) returns the command untruncated
   at the default ``display.tool_preview_length`` of 0, which it reads as
   unlimited. The reproduction on the live-test install put a 1503-character
   synthetic token in that line.

What this module does
---------------------
* :func:`register_kube_agents_patterns` hands :data:`KUBE_AGENTS_PATTERNS` to
  upstream's own ``register_redaction_patterns`` -- the additive plugin
  registry, so every masking rule (6-head/4-tail, the non-reusable sentinel on
  ``file_read``, the control-split matcher) applies unchanged. A trailer on
  ``agent/redact.py`` calls it at import, so every process that redacts has
  the pattern before its first call.
* :func:`redact_tool_argument_for_display` is the branch
  ``redact_tool_args_for_display`` gains for ``terminal`` and ``execute_code``:
  the same redactor, the same ``force=True``, as upstream's ``browser_type``
  branch. It covers the ``💻 $`` line, the tool preview and the progress
  callbacks, which all go through that one function.
* :class:`RedactingTextStream` and :func:`install_worker_transcript_redaction`
  put the redactor between the worker and its transcript. The installer runs
  first thing in ``hermes_cli.main.main()``, before prompt_toolkit caches its
  output object, and wraps ``sys.stdout``/``sys.stderr`` only when
  ``HERMES_KANBAN_TASK`` is set and stdout is not a TTY: an interactive
  ``hermes`` never sees it. ``force=True`` throughout, so
  ``security.redact_secrets: false`` does not reopen the transcript -- the same
  call upstream's board writes in ``tools/kanban_tools.py`` make.

What it does not do
-------------------
The wrapper redacts one line at a time and holds a partial line until its
newline or a ``flush()``. A flush that lands inside a token writes the head of
it; nothing in the worker's print path flushes mid-line today, and the
interpreter's own shutdown flush is what carries the last unterminated line
out. Bytes written through ``sys.stdout.buffer`` -- prompt_toolkit's preferred
path, and so the ``💻 $`` line's -- go through the same line buffer, decoded as
UTF-8 with undecodable bytes replaced. A child process inheriting fd 1 is not
seen; the terminal tool captures child output through pipes and writes it back
through the redacted result path, so the observed leak is covered. Nothing on
a PVC before this image is rewritten.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Callable, Iterable, TextIO

#: A GCP OAuth 2.0 access token. The same string as
#: ``AuditRedactor.GCP_OAUTH_TOKEN_PATTERN`` in
#: ``agents/chat/defaults/plugins/common/redactor.py`` -- asserted equal by
#: ``test_credential_redaction.py``, never imported, because that module is a
#: gateway plugin and this one lives inside the Hermes image. The literal
#: ``ya29`` in front satisfies the two-character pre-screen upstream's registry
#: requires.
GCP_OAUTH_TOKEN_PATTERN = r"ya29\.[0-9A-Za-z\-_.]{20,}"

#: Everything this deployment registers on top of upstream's list.
KUBE_AGENTS_PATTERNS: tuple[str, ...] = (GCP_OAUTH_TOKEN_PATTERN,)

#: The attribution ``register_redaction_patterns`` logs the registration under.
PATTERN_SOURCE = "kube-agents"

#: Set by ``kanban_db.py`` in the environment of every worker it spawns; the
#: only signal that a process is a worker writing to a transcript.
WORKER_TASK_ENV = "HERMES_KANBAN_TASK"

#: The tool arguments whose text is the command the model wrote, keyed by tool
#: name. ``build_tool_preview`` in ``agent/display.py`` names the same two
#: fields as each tool's primary argument.
REDACTED_TOOL_ARGUMENTS: dict[str, str] = {
    "terminal": "command",
    "execute_code": "code",
}

#: Marks a stream this module already wrapped, so a second install is a no-op.
WRAPPED_MARKER = "_kube_agents_redacting_stream"

#: A partial line longer than this is written out (redacted) rather than held,
#: so a writer that never sends a newline cannot grow the buffer without bound.
MAX_PENDING_CHARS = 64 * 1024

LINE_TERMINATOR = "\n"

#: How bytes handed to ``sys.stdout.buffer`` are read back into text before
#: redaction. ``replace`` rather than ``surrogateescape``: the wrapped text
#: stream may encode strictly, and a U+FFFD in a transcript beats an exception
#: in a worker.
BINARY_DECODE_ERRORS = "replace"

#: Written to the transcript, once, when the wrapper cannot be installed. At
#: that point logging is not configured yet; the transcript is the one place
#: the operator will look.
INSTALL_FAILURE_PREFIX = "kube-agents: transcript redaction not installed: "

Redactor = Callable[[str], str]


def _forced_redactor() -> Redactor:
    """Upstream's redactor with the operator opt-out bypassed.

    Imported at call time: ``agent/redact.py``'s trailer imports this module,
    so a module-level import here would be circular, and the processes that
    never install the wrapper should not pay for loading the redactor.
    """
    from agent.redact import redact_sensitive_text

    def redact(text: str) -> str:
        return redact_sensitive_text(text, force=True)

    return redact


def register_kube_agents_patterns() -> int:
    """Register :data:`KUBE_AGENTS_PATTERNS` with upstream's redaction engine.

    Returns the number upstream accepted. Upstream logs a warning for each
    pattern it rejects and never raises, so a rejected pattern shows up in
    ``agent.log`` and in ``verify_credential_redaction.py``, not as a crash.
    """
    from agent.redact import register_redaction_patterns

    return register_redaction_patterns(list(KUBE_AGENTS_PATTERNS), source=PATTERN_SOURCE)


def redact_tool_argument_for_display(
    tool_name: str, args: dict | None, redact: Redactor
) -> dict | None:
    """Mask the command text of a ``terminal`` or ``execute_code`` call.

    ``redact`` is ``redact_sensitive_text`` bound the way the caller already
    uses it; the patched ``agent/display.py`` passes ``force=True`` through it.
    Anything else -- another tool, a missing or non-string field -- comes back
    as it was, the same contract as upstream's ``browser_type`` branch.
    """
    if not isinstance(args, dict):
        return args
    field = REDACTED_TOOL_ARGUMENTS.get(tool_name)
    if field is None or not isinstance(args.get(field), str):
        return args
    safe_args = dict(args)
    safe_args[field] = redact(args[field])
    return safe_args


class _RedactingBinaryFacade:
    """The ``buffer`` of a :class:`RedactingTextStream`: bytes in, same line buffer.

    prompt_toolkit's ``flush_stdout`` writes to ``stdout.buffer`` whenever the
    stream has one, which is how the ``💻 $`` completion line reaches the
    transcript; a facade that simply delegated ``buffer`` to the wrapped stream
    would let every byte through unredacted. Anything not overridden here is
    the wrapped stream's own buffer's, so ``fileno``/``closed``/``raw`` behave.
    """

    def __init__(self, text_stream: "RedactingTextStream") -> None:
        self._text = text_stream

    def write(self, data) -> int:
        payload = bytes(data)
        self._text.write(
            payload.decode(self._text.encoding or "utf-8", BINARY_DECODE_ERRORS)
        )
        return len(payload)

    def flush(self) -> None:
        self._text.flush()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._text._inner.buffer, name)


class RedactingTextStream:
    """A text stream that redacts each line on its way to ``inner``.

    Line-buffered on purpose: the patterns are line-local (a bearer header, a
    token) and a token split across two ``write()`` calls has to be seen whole,
    so writes accumulate until a newline and the redactor runs once per line.
    ``buffer`` is a facade that feeds the same line buffer, and everything else
    a writer might ask of ``sys.stdout`` is delegated to the wrapped stream.
    """

    def __init__(self, inner: TextIO, redact: Redactor) -> None:
        self._inner = inner
        self._redact = redact
        self._pending = ""
        self._lock = threading.RLock()
        setattr(self, WRAPPED_MARKER, True)

    # -- the redacting path --------------------------------------------------

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        with self._lock:
            self._pending += text
            head, sep, tail = self._pending.rpartition(LINE_TERMINATOR)
            if sep:
                self._inner.write(self._redact(head + sep))
                self._pending = tail
            if len(self._pending) > MAX_PENDING_CHARS:
                self._drain_pending()
        return len(text)

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        """Write the held partial line, redacted, then flush ``inner``.

        The worker's SIGTERM path flushes stdio and calls ``os._exit(0)``,
        which skips every other chance to write a last unterminated line.
        """
        with self._lock:
            self._drain_pending()
            self._inner.flush()

    def close(self) -> None:
        with self._lock:
            self._drain_pending()
            self._inner.close()

    def _drain_pending(self) -> None:
        if self._pending:
            self._inner.write(self._redact(self._pending))
            self._pending = ""

    # -- everything else is the wrapped stream's -----------------------------

    def fileno(self) -> int:
        return self._inner.fileno()

    def isatty(self) -> bool:
        return self._inner.isatty()

    @property
    def encoding(self):
        return getattr(self._inner, "encoding", None)

    @property
    def errors(self):
        return getattr(self._inner, "errors", None)

    @property
    def buffer(self):
        return _RedactingBinaryFacade(self)

    def __getattr__(self, name: str):
        # Only reached for attributes this class does not define itself. The
        # underscore guard keeps a lookup of ``_inner`` before ``__init__``
        # has run (copy, pickle) from recursing into this method.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)


def _is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _wrap(stream, redact: Redactor):
    """Return ``stream`` wrapped, or as it is when it already was or cannot be."""
    if stream is None or getattr(stream, WRAPPED_MARKER, False) or _is_tty(stream):
        return stream
    return RedactingTextStream(stream, redact)


def install_worker_transcript_redaction(
    environ=None, redact: Redactor | None = None
) -> bool:
    """Wrap ``sys.stdout`` and ``sys.stderr`` for a kanban worker.

    Returns True when at least one stream was wrapped by this call. A no-op,
    returning False, in every other process: no ``HERMES_KANBAN_TASK`` in the
    environment, a stdout that is a terminal, or streams this module already
    wrapped. ``environ`` and ``redact`` exist for the tests; the entry point
    passes neither.
    """
    env = os.environ if environ is None else environ
    if not env.get(WORKER_TASK_ENV):
        return False
    if _is_tty(sys.stdout):
        return False
    redactor = redact if redact is not None else _forced_redactor()
    wrapped = False
    for name in ("stdout", "stderr"):
        before = getattr(sys, name)
        after = _wrap(before, redactor)
        if after is not before:
            setattr(sys, name, after)
            wrapped = True
    return wrapped


def install_or_report() -> bool:
    """The entry-point call: install, and say so on the transcript if it fails.

    A failure here means a worker running without the boundary, which the
    operator has to be able to see; at this point in ``main()`` nothing has
    configured logging, and stderr is the transcript.
    """
    try:
        return install_worker_transcript_redaction()
    except Exception as exc:  # noqa: BLE001 - must never stop the CLI starting
        try:
            sys.stderr.write(f"{INSTALL_FAILURE_PREFIX}{exc!r}\n")
        except Exception:  # noqa: BLE001
            pass
        return False

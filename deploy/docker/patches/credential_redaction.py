"""Keep GCP access tokens out of the files a kanban worker leaves on the PVC.

Installed at ``/opt/hermes/agent/credential_redaction.py`` by
``deploy/docker/Dockerfile``; ``apply_credential_redaction.py`` wires it into
five upstream sites, the two for the operator's rules described at the end. Runtime code only: nothing here imports Hermes at module
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

import functools
import importlib.util
import json
import os
import sys
import threading
from typing import Any, Callable, Iterable, Mapping, TextIO

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
#: The operator's rule file (issue #1985): the same ``redaction.yaml`` the chart
#: renders for the gateway's pre-call hook, projected into the agent pod at
#: DEFAULT_RULE_FILE. The file's presence there is the switch: the operator
#: admits no path-valued variable through ``spec.deployment.env`` into the
#: agent container, by design, and the agent cannot write under ``/etc``. The
#: variable the hook reads overrides the path for the build-time verifier and
#: the tests. Neither set means the switch is off and every function in the
#: section below is a no-op.
RULE_FILE_ENV = "KUBE_AGENTS_REDACTION_CONFIG"
#: A directory of its own, beside the operator's read-only ``/etc/kube-agents``
#: mount rather than inside it: runc cannot create a mountpoint through a
#: read-only parent mount (``mkdir: read-only file system``), and only gVisor's
#: synthetic mountpoints would let a nested path come up.
DEFAULT_RULE_FILE = "/etc/kube-agents-redaction/redaction.yaml"
#: Where the shared redactor module lives in the image; the gateway's hook
#: runs the same file. Overridable for the build-time verifier and the tests.
REDACTOR_MODULE_ENV = "KUBE_AGENTS_REDACTOR_MODULE"
DEFAULT_REDACTOR_MODULE = "/opt/defaults/plugins/common/redactor.py"
REDACTOR_MODULE_NAME = "kube_agents_redactor"
RULES_INSTALLED_MARKER = "_kube_agents_operator_rules"
#: Name of the resolved source in an error, when the variable did not name it.
RULE_FILE_SOURCE_NAME = "redaction rule file"
#: The keys of one tool call, as the gateway's hook reads them.
TOOL_CALL_FUNCTION_KEY = "function"
TOOL_CALL_ARGUMENTS_KEY = "arguments"
#: A content or reasoning part the hook redacts: ``{"type": "text", "text": ...}``.
#: Every other part (an image, a signed or encrypted reasoning block) is opaque
#: and stays byte for byte.
PART_TYPE_KEY = "type"
TEXT_PART_TYPE = "text"
#: Part types whose ``text`` is prose: chat-completions text, the Responses
#: API's input, output and summary text, and a ``reasoning.*`` part's text.
PROSE_PART_TYPES: tuple[str, ...] = ("text", "output_text", "input_text", "summary_text")
REASONING_PART_PREFIX = "reasoning"
TEXT_KEY = "text"
#: Container items whose prose sits one level down: a Responses ``message``
#: item under ``content``, a Responses ``reasoning`` item and an OpenRouter
#: ``reasoning.summary`` part under ``summary``. Their other keys
#: (``encrypted_content``, ``signature``, ``id``) stay as they are.
PART_CONTAINER_KEYS: Mapping[str, str] = {"message": "content", "reasoning": "summary", "reasoning.summary": "summary"}
#: The message-dict fields the session store persists as text, or as JSON that
#: carries text: what ``_message_row_params`` binds besides ``tool_calls``.
MESSAGE_TEXT_FIELDS: tuple[str, ...] = (
    "content",
    "api_content",
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
)

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


# --- The operator's rules, on disk (issue #1985) -----------------------------
#
# ``litellm.redaction`` masks or pseudonymises IP literals and operator-named
# identifiers in every request the gateway forwards. The same rule file,
# projected at DEFAULT_RULE_FILE (or named by RULE_FILE_ENV for the verifier
# and the tests), makes the durable writers in this pod apply those rules
# too: the trailer on ``agent/redact.py`` wraps ``redact_sensitive_text`` so
# ``agent.log``, the terminal-output cache, the worker transcript and the
# ``💻 $`` line all carry them, and prologues on the session store's row
# binder and the kanban board's writers run the wrapped redactor over what
# those two databases receive. Without the file none of this is installed and
# the image behaves as before.

_RULES_LOCK = threading.Lock()
_RULES: dict[str, list] = {}
#: The redactor ``install_operator_rules`` composed over, bound ``force=True``:
#: upstream's shapes, no operator rules. The key-aware argument walk applies the
#: rules itself and runs this over the leaves, so a rule is applied once, as the
#: gateway applies it, and the opt-out does not reopen the leaves.
_UPSTREAM_REDACTOR: list = []
_REDACTOR_MODULE: list = []
_RULE_FILE: list = []
_RULE_FILE_SOURCE: list = []


def rule_file_path() -> str:
    """The operator's rule file, or "" when the switch is off.

    Resolved once per process, like the rules themselves: the variable when
    set, else DEFAULT_RULE_FILE when the chart has projected it, else "".
    """
    if _RULE_FILE:
        return _RULE_FILE[0]
    path = (os.environ.get(RULE_FILE_ENV) or "").strip()
    source = RULE_FILE_ENV
    if not path and os.path.isfile(DEFAULT_RULE_FILE):
        path, source = DEFAULT_RULE_FILE, RULE_FILE_SOURCE_NAME
    _RULE_FILE.append(path)
    _RULE_FILE_SOURCE.append(source)
    return path


def rule_file_source() -> str:
    """What named the rule file, for an error: the variable, or the projected file."""
    rule_file_path()
    return _RULE_FILE_SOURCE[0]


def disk_redaction_enabled() -> bool:
    return bool(rule_file_path())


def reset_operator_rules() -> None:
    """Forget the loaded rules and module (tests)."""
    with _RULES_LOCK:
        _RULES.clear()
        _REDACTOR_MODULE.clear()
        _RULE_FILE.clear()
        _RULE_FILE_SOURCE.clear()
        _UPSTREAM_REDACTOR.clear()


def _redactor_module():
    """The shared ``AuditRedactor`` module, loaded once from its file.

    Loaded by path rather than imported by name: the plugin tree is not on
    ``sys.path`` in a worker, and the hook's copy in the chart is the same file.
    """
    if _REDACTOR_MODULE:
        return _REDACTOR_MODULE[0]
    path = (os.environ.get(REDACTOR_MODULE_ENV) or "").strip() or DEFAULT_REDACTOR_MODULE
    if not os.path.isfile(path):
        raise ValueError(f"the shared redactor module is not at {path} ({REDACTOR_MODULE_ENV} unset)"
                         if path == DEFAULT_REDACTOR_MODULE else
                         f"{REDACTOR_MODULE_ENV}: the shared redactor module is not at {path}")
    spec = importlib.util.spec_from_file_location(REDACTOR_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load the shared redactor module from {path}")
    module = importlib.util.module_from_spec(spec)
    # Its frozen dataclasses resolve annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _REDACTOR_MODULE.append(module)
    return module


def _load_rules(path: str) -> list:
    """Parse the rule file through the shared loader; raise ``ValueError`` naming the source.

    ``AuditRedactor.rules_from_file`` is the gateway hook's loader too, so the
    same file reads the same way in both pods. The file was validated when the
    chart rendered it, and the gateway stops at startup on the same file, so
    this fails only for a hand-authored one.
    """
    try:
        return _redactor_module().AuditRedactor.rules_from_file(path)
    except ValueError as error:
        raise ValueError(f"{rule_file_source()}: {error}") from error


def operator_rules() -> list:
    """The rules for the configured file, read once per process; ``[]`` when off."""
    path = rule_file_path()
    if not path:
        return []
    with _RULES_LOCK:
        rules = _RULES.get(path)
        if rules is None:
            rules = _load_rules(path)
            _RULES[path] = rules
        return rules


def apply_operator_rules(text: str) -> str:
    """Run the operator's rules over ``text``; identity when the switch is off."""
    rules = operator_rules()
    if not rules or not text:
        return text
    redacted, _counts = _redactor_module().AuditRedactor.apply_rules(text, rules)
    return redacted


def install_operator_rules(upstream: Callable[..., str]) -> Callable[..., str]:
    """Return ``upstream`` composed with the operator's rules, or ``upstream`` itself.

    Called by the trailer on ``agent/redact.py`` with ``redact_sensitive_text``
    and rebound to that name, so every caller that imports it afterwards, and
    every function in the module that reads the global at call time, gets the
    composed form. The rules run after upstream's masking and whatever
    ``force`` says: the switch is the operator's, not the profile's. A rule
    file that cannot be read or parsed raises here, at import, and the process
    does not start, which is what the gateway does with the same file.
    """
    if not disk_redaction_enabled() or getattr(upstream, RULES_INSTALLED_MARKER, False):
        return upstream
    operator_rules()  # load now: a bad file fails the import, not the first write
    # Resolve the pseudonym salt now, while the log formatter still runs the
    # plain upstream redactor. ``_resolve_salt`` logs its missing-salt warning
    # under a non-reentrant lock; once the composition below is in place that
    # warning would be redacted on the same thread, and a pseudonym rule
    # matching a word in it would re-enter the lock and hang the process.
    _redactor_module().AuditRedactor.hmac_hash(RULES_INSTALLED_MARKER)

    @functools.wraps(upstream)
    def redact(text: str, *args, **kwargs) -> str:
        return apply_operator_rules(upstream(text, *args, **kwargs))

    setattr(redact, RULES_INSTALLED_MARKER, True)
    # Forced, as every store path is: the profile's ``security.redact_secrets``
    # opt-out must not reopen the Hermes layer on the argument leaves.
    _UPSTREAM_REDACTOR[:] = [functools.partial(upstream, force=True)]
    return redact


def _redact_parts(value: Any, redact: Redactor) -> Any:
    """The gateway hook's content walk: strings, lists element-wise, ``text`` parts by their text.

    A ``reasoning.*`` part's ``text`` is prose and is redacted the same way; its
    ``signature`` is not. A Responses ``message`` item is walked through its
    ``content``, a Responses ``reasoning`` item and an OpenRouter
    ``reasoning.summary`` part through their ``summary``; an untyped part with
    a ``text`` is prose. A dict part of any other ``type``, an image, an
    encrypted reasoning block, is returned as it is: those values are opaque to the
    redactor and the provider rejects a replayed turn whose image or signature
    no longer verifies. A new structure where something changed; the input is
    never mutated.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_parts(item, redact) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_parts(item, redact) for item in value)
    if isinstance(value, dict):
        part_type = str(value.get(PART_TYPE_KEY) or "")
        prose = (
            not part_type
            or part_type in PROSE_PART_TYPES
            or part_type.startswith(REASONING_PART_PREFIX)
        )
        changed = dict(value)
        if prose and isinstance(value.get(TEXT_KEY), str):
            changed[TEXT_KEY] = redact(value[TEXT_KEY])
        container = PART_CONTAINER_KEYS.get(part_type)
        if container and container in value:
            changed[container] = _redact_parts(value[container], redact)
        return changed if changed != value else value
    return value


def _redact_parsed_arguments(value: Any, redact: Redactor) -> Any:
    """The gateway hook's argument walk: ``AuditRedactor``'s key-aware pass, then the image's redactor.

    ``AuditRedactor.redact`` masks by key name as well as by shape (a value
    under ``password``, a credential-named env ``name``/``value`` pair, a
    Secret's ``data``) and applies the operator's rules in the same pass; the
    forced upstream redactor then runs over the leaf strings for the shapes only
    it knows. Upstream's, not the composition: the rules have run once already,
    and a second pass would nest a marker inside the first, as the gateway's
    single pass never does. Forced, so the profile's opt-out does not reopen the
    leaves. One walker shared with the hook, not a third one.
    """
    redacted = _redactor_module().AuditRedactor.redact(value, operator_rules())
    leaves = _UPSTREAM_REDACTOR[0] if _UPSTREAM_REDACTOR else redact
    return _redact_leaves(redacted, leaves)


def _redact_leaves(value: Any, redact: Redactor) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_leaves(item, redact) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_leaves(item, redact) for item in value]
    return value


def _redact_arguments(arguments: Any, redact: Redactor) -> Any:
    """A tool call's arguments, redacted the way the gateway's hook does it.

    The parsed document is walked rather than the raw string, so the stored
    arguments still parse when Hermes replays the turn, and the result is
    re-serialised only when something changed. Arguments that are not JSON are
    redacted as text; arguments already an object are walked as they are.
    """
    if not isinstance(arguments, str):
        return _redact_parsed_arguments(arguments, redact)
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return redact(arguments)
    redacted = _redact_parsed_arguments(parsed, redact)
    if redacted == parsed:
        return arguments
    return json.dumps(redacted, ensure_ascii=False)


def _redact_tool_calls(tool_calls: Any, redact: Redactor) -> Any:
    """Copies of the calls with only ``function.arguments`` redacted.

    The call id, its type and the function name are structure: Hermes matches
    the id against the tool row's ``tool_call_id`` and dispatches on the name
    when it replays the turn, so a rule loose enough to match them would break
    the session. The gateway's hook leaves them alone for the same reason.
    """
    if not isinstance(tool_calls, list):
        return tool_calls
    redacted_calls = []
    for call in tool_calls:
        function = call.get(TOOL_CALL_FUNCTION_KEY) if isinstance(call, dict) else None
        if isinstance(function, dict) and function.get(TOOL_CALL_ARGUMENTS_KEY) is not None:
            call = {**call, TOOL_CALL_FUNCTION_KEY: {
                **function,
                TOOL_CALL_ARGUMENTS_KEY: _redact_arguments(function[TOOL_CALL_ARGUMENTS_KEY], redact),
            }}
        redacted_calls.append(call)
    return redacted_calls


def redact_store_text(value: Any, redact: Redactor | None = None) -> Any:
    """A string bound for the kanban board, redacted when the switch is on.

    Anything that is not a string, and every value when the switch is off,
    comes back as the same object.
    """
    if not isinstance(value, str) or not disk_redaction_enabled():
        return value
    return (redact if redact is not None else _forced_redactor())(value)


def redact_message_for_store(
    msg: dict, tool_calls: Any, redact: Redactor | None = None
) -> tuple[dict, Any]:
    """The message dict and tool calls as the session store should persist them.

    When the switch is off both come back as the same objects. On, the fields in
    MESSAGE_TEXT_FIELDS get the hook's content walk (strings and ``text`` parts;
    an image or a reasoning signature stays byte for byte), and each tool
    call's ``function.arguments`` (the list, or still the JSON text some callers
    pass) gets the hook's key-aware argument walk; call ids, types, function
    names, counters, flags and the rest of the dict are untouched.
    """
    if not disk_redaction_enabled() or not isinstance(msg, dict):
        return msg, tool_calls
    redactor = redact if redact is not None else _forced_redactor()
    redacted = dict(msg)
    for field in MESSAGE_TEXT_FIELDS:
        if field in redacted:
            redacted[field] = _redact_parts(redacted[field], redactor)
    if isinstance(tool_calls, str):
        try:
            parsed = json.loads(tool_calls)
        except ValueError:
            return redacted, redactor(tool_calls)
        redacted_calls = _redact_tool_calls(parsed, redactor)
        return redacted, tool_calls if redacted_calls == parsed else json.dumps(redacted_calls)
    return redacted, _redact_tool_calls(tool_calls, redactor)

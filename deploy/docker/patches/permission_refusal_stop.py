"""Stop a run that keeps being refused permission.

The defect (#1943, #1946)
-------------------------
When the command policy refuses a write, the agent treats the refusal as a
puzzle: the same change again, then another verb, another flag, another
identity, until the step budget runs out. Hermes's own loop guardrails do not
catch it. They count identical retries and consecutive failures of one tool,
any successful call resets them, and the workaround hunt varies its commands
and mixes in reads. The refusal text (#1966) tells the model to stop; this
module is the backstop for a model that does not.

What it does
------------
Installed around ``ToolCallGuardrailController.after_call`` by
``apply_permission_refusal_stop.py``: the stock method runs first, so its
failure counters and loop caps see every call, and :func:`observe_refusal`
then decides whether a refusal overrides its decision. For every tool result
it asks :func:`classify_refusal`:

* ``policy``: the command policy's ``kubernetes.read-only`` rule refused a
  command whose kubectl verb is a mutating one (:data:`_MUTATING_VERBS`), with
  the action boundary notice ``command_policy.py`` prints ("refused however it
  is attempted"). The rule is an allowlist of read verbs and refuses an
  unlisted read spelling (``config view``, ``cluster-info dump``, ``diff``,
  ``port-forward``, ``cp``, ``exec``, and ``<verb> --help``) with the same
  words, so the verb is what tells a refused write from a refused read. The
  verb is resolved the way kubectl reads it (:func:`names_mutating_verb`):
  the command is split into simple commands at ``;``, ``&&``, ``||``, ``|``,
  parentheses and newlines (comments do not end a line early), a ``kubectl``
  in command position (after assignments, wrappers such as ``sudo``,
  ``timeout``, ``xargs``, and the shell's own keywords ``if``, ``!``, ``do``,
  ``{``; inside a ``sh -c`` or ``python -c`` string; after an ``exec … --``
  passthrough; inside a ``$(…)``) has its global flags walked, and the first
  operand is the verb; ``-h``/``--help`` anywhere before ``--`` makes it a
  help read.
  A verb word elsewhere in the line (an argument, a passthrough, a quoted
  string) is not a verb. A write is counted, per turn, across every tool and
  whatever succeeded in between; the call's exit code is not consulted,
  because the shell reports the last command's status and a refused write
  chained before a read arrives with exit 0. The ``permission_refusal_halt_after``-th
  (``tool_loop_guardrails`` in the profile config, default
  :data:`DEFAULT_HALT_AFTER`, the first) halts the turn through the controller's
  own halt decision, which ``agent/turn_tool_round.py`` already routes through
  the kanban-aware exit in ``kanban_guardrail_exit.py``: a worker gets one nudge
  to close its card with the refusal, a chat turn ends with :data:`HALT_MESSAGE`
  as its reply. Below the threshold a refusal appends guidance to the tool
  result instead; live runs showed the model reading that guidance and going on
  investigating, which is why the default halts on the first. A flag refusal
  (``retried without the flag``) is not counted: the policy itself says the
  command may be retried.
* ``advisory``: the notice on anything else: a kubectl command with no
  mutating verb in it, or ``gcp.read-only``'s refusal (also an allowlist, which
  refuses unlisted reads the same way; a halt there ended a live Cluster Agent
  investigation on ``gcloud artifacts docker tags list``). Guidance only,
  never counted: the policy's own text tells the agent to use an allowed
  spelling or report the gap, and a write it did not recognise is still
  refused by the policy on the next try. The guidance replaces the stock
  decision only when that decision was ``allow`` or ``warn``; a stock halt
  (eight varied failures of one tool, say) still ends the run.
* ``api``: a 403 from the Kubernetes or Google Cloud API. Guidance only, never
  counted: a fleet audit meets these on projects it may not read and goes on
  to the next one, and halting it would be the wrong fix (#1946's own caveat).
  401 and ``Unauthenticated`` are not refusals at all; the recovery ladder
  owns them.

The counter lives on the controller and keys on the turn: Hermes rebuilds
``_call_history`` in ``reset_for_turn``, so the deque object names the turn
(held by reference, so a freed deque's address cannot be mistaken for it)
without a second anchor in that method. A kanban worker is one turn for the whole card,
which is the scope the count should have.

Why not the stock detector
--------------------------
``hard_stop_enabled`` would halt after eight failures of ``terminal`` with
different arguments, which a workaround hunt reaches only after exhausting the
model's ideas, and its recovery hint tells the model to "try a different
command". Upstream NousResearch/hermes-agent has no terminal-failure class and
no non-resetting counter; this module goes when it ships one.
"""

from __future__ import annotations

import re
import shlex

#: Definitive refusals in one turn before the run is halted: the first.
DEFAULT_HALT_AFTER = 1
#: The attribute ``apply_permission_refusal_stop.py`` adds to the guardrail config.
HALT_AFTER_ATTR = "kube_permission_refusal_halt_after"
#: Decision codes, in the shape of upstream's.
WARN_CODE = "permission_refusal"
ADVISORY_WARN_CODE = "policy_refusal_advisory"
API_WARN_CODE = "permission_denied"
HALT_CODE = "permission_refusal_halt"
#: The phrase only the command policy's action boundary notice carries.
_POLICY_ACTION_RE = re.compile(r"refused however it is attempted")
#: The rule whose refusal of a mutating verb is definitive.
_DEFINITIVE_RULE_RE = re.compile(r"policy rule: kubernetes\.read-only\b")
#: kubectl verbs that change state (``debug`` starts a pod or container).
_MUTATING_VERBS = frozenset({
    "scale", "patch", "edit", "apply", "replace", "autoscale", "annotate", "label", "create",
    "delete", "set", "run", "expose", "cordon", "uncordon", "drain", "taint", "debug",
})
#: Verbs whose first operand decides: ``rollout restart``, ``auth reconcile``, ``certificate approve``.
_MUTATING_SUBVERBS = {
    "rollout": frozenset({"restart", "undo", "pause", "resume"}),
    "auth": frozenset({"reconcile"}),
    "certificate": frozenset({"approve", "deny"}),
}
#: kubectl global flags that take the next token as their value: the policy's
#: own table when the image carries it (``tools/command_policy.py``), else a copy.
try:
    from tools.command_policy import _KUBECTL_FLAGS_WITH_VALUE as _KUBECTL_VALUE_FLAGS
except Exception:  # outside the image (unit tests)
    _KUBECTL_VALUE_FLAGS = frozenset({
        "-n", "--namespace", "--context", "--cluster", "--kubeconfig", "-s", "--server", "--user",
        "--token", "--request-timeout", "--cache-dir", "--certificate-authority",
        "--client-certificate", "--client-key", "--tls-server-name", "--username", "--password",
        "--kuberc", "--profile", "--profile-output", "--log-flush-frequency", "-v", "--v",
        "--vmodule", "--as", "--as-group", "--as-uid",
    })
_HELP_FLAGS = frozenset({"-h", "--help"})
#: Spellings that render or read a manifest without touching the cluster; the
#: policy refuses them too, but they are not the attempted change.
_LOCAL_RENDER_FLAGS = frozenset({"--local", "--list"})
_LOCAL_RENDER_PREFIX = "--dry-run"
_LOCAL_RENDER_SUBVERB = "view-last-applied"
#: Words a shell command may carry before the command word, and the shell's
#: own keywords and group openers that may stand in command position.
_WRAPPERS = frozenset({
    "sudo", "env", "time", "nice", "ionice", "nohup", "command", "builtin", "exec", "stdbuf",
    "timeout", "watch", "xargs",
})
_WRAPPERS_WITH_OPERAND = frozenset({"timeout"})
#: Per wrapper, the options that take the next token (``sudo -u root``, ``nice -n 5``,
#: ``timeout -k 5``); ``sudo -E`` and ``sudo -i`` take none, ``xargs -E eof`` does.
_WRAPPER_VALUE_OPTIONS = {
    "sudo": frozenset({"-u", "-g", "-p", "-C", "-D", "-R", "-T", "-U", "-h", "--user", "--group"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-k", "-s", "--kill-after", "--signal"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "env": frozenset({"-u", "-C", "-S"}),
    "xargs": frozenset({"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "--max-args", "--max-procs", "--delimiter", "--replace"}),
    "watch": frozenset({"-n", "-d", "--interval"}),
}
_WRITERS = frozenset({"echo", "printf"})
_WRITER_OPTIONS = frozenset({"-e", "-E", "-n", "-v", "-ne", "-en"})
_XARGS_PLACEHOLDER = "{}"
_KEYWORDS = frozenset({"if", "!", "then", "else", "elif", "do", "while", "until", "{", "}", "(", ")"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_PYTHON_RE = re.compile(r"^python[\d.]*$")
#: Shell punctuation the lexer splits off: command separators and group parentheses.
_PUNCTUATION = ";|&()"
_PASSTHROUGH = "--"
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CONTINUATION_RE = re.compile(r"\\\n")
#: ``$(…)`` and backticks: the command inside runs too.
_SUBSTITUTION_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
#: ``$(which kubectl)`` and its spellings stand for the word itself.
_WHICH_SUBST_RE = re.compile(r"\$\((?:which|command\s+-v|type\s+-p)\s+(\S+?)\)")
#: A heredoc opener (not a ``<<<`` here-string) and its tag.
_HEREDOC_OPENER_RE = re.compile(r"(?<!<)<<(?!<)-?\s*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([\w.-]+))")
#: Python list punctuation, dropped so ``["kubectl", "scale"]`` reads as words,
#: and the string prefixes shlex would glue to a quoted string.
_PYTHON_PUNCTUATION_RE = re.compile(r"[\[\](),]")
_PYTHON_PREFIX_RE = re.compile(r"(?<![\w])[fFbBrRuU]{1,2}(?=[\"'])")
_MAX_DEPTH = 6
#: A kubectl word, then within the same simple command one of the verbs above, as
#: a word of its own (``patch.yaml`` and ``--replicas=3`` are not verbs). The
#: separator class admits the quoted, comma-separated form a Python list uses.
#: Kubernetes and Google Cloud API denials, in the shapes kubectl, gcloud and the
#: MCP tools print them. ``401``/``Unauthenticated`` deliberately absent.
_API_DENIED_RE = re.compile(
    r"Error from server \(Forbidden\)|\bis forbidden:|\bPERMISSION_DENIED\b|\"code\":\s*403\b|\bHTTP(?:/[\d.]+)? 403\b",
)
#: Attribute on the controller holding ``(turn token, count)``.
_COUNT_ATTR = "_kube_permission_refusals"

WARN_MESSAGE = (
    "Permission refused. This is a boundary, not an error to work around: the change is "
    "refused however it is attempted, so do not retry it through another command, tool, "
    "flag, identity or API. Stop working on this change now and report what was refused, "
    "quoting the message, with a note that it can be proposed as a pull request; open one "
    "only if the request asked for a pull request. Further refused changes in this run "
    "end the run."
)
ADVISORY_WARN_MESSAGE = (
    "The command policy refused this command. If it would change cluster or cloud state, that "
    "change is refused however it is attempted: do not retry it through another command, tool, "
    "flag or identity; report it, and propose it as a pull request only if one was asked for. "
    "If it only reads, use a spelling the policy allows or report the gap; do not work around it."
)
API_WARN_MESSAGE = (
    "Permission denied by the API (403). Treat it as final for this action: do not retry it "
    "through another command, tool, flag or identity. Continue with what you are allowed to do "
    "and report what was denied."
)
#: Also the reply a chat turn ends with (``_toolguard_controlled_halt_response``).
HALT_MESSAGE = (
    "The command policy refused this change. That is a permission boundary, not an error to "
    "work around: the change is refused however it is attempted, so it was not made, it is not "
    "retried through another command, tool, flag, identity or API, and the work on it stops "
    "here. It can be proposed as a pull request if one is wanted."
)


def command_text(args) -> tuple[str, bool]:
    """The command a tool call ran and whether it is program text rather than a shell command."""
    if not isinstance(args, dict):
        return "", False
    command = args.get("command")
    if isinstance(command, str):
        return command, False
    return "\n".join(v for v in args.values() if isinstance(v, str)), True


def _tokens(text: str, *, loose: bool) -> list[str]:
    """Shell words with ``;``, ``&&``, ``||``, ``|``, ``&`` and newlines as separators
    (in program text a newline separates nothing)."""
    text = _CONTINUATION_RE.sub(" ", text).replace("\n", " " if loose else " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace_split = True
    lexer.commenters = ""  # a ``#`` would swallow every later line of the joined text
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def _split_heredocs(text: str) -> tuple[str, list[tuple[str, bool]]]:
    """The text without heredoc bodies, and each body that an interpreter on its opener line runs
    (``(body, loose)``, ``loose`` for python); a body nothing runs is content and is dropped."""
    lines = text.split("\n")
    kept: list[str] = []
    bodies: list[tuple[str, bool]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        m = _HEREDOC_OPENER_RE.search(line)
        i += 1
        if not m:
            continue
        tag = m.group(1) or m.group(2) or m.group(3)
        body: list[str] = []
        while i < len(lines) and lines[i].strip() != tag:
            body.append(lines[i])
            i += 1
        i += 1  # the terminator
        words = [w.rsplit("/", 1)[-1] for w in line.split()]
        if any(_PYTHON_RE.match(w) for w in words):
            bodies.append(("\n".join(body), True))
        elif any(w in _SHELLS for w in words):
            bodies.append(("\n".join(body), False))
    return "\n".join(kept), bodies


def _simple_commands(tokens: list[str]) -> list[list[str]]:
    out: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= set(_PUNCTUATION):
            out.append([])
        else:
            out[-1].append(token)
    return [c for c in out if c]


def _operands(tokens: list[str]) -> list[str]:
    """The non-flag words of a kubectl argument list, value flags consumed with their value."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == _PASSTHROUGH:
            break
        if token.startswith("-"):
            i += 2 if token in _KUBECTL_VALUE_FLAGS else 1
            continue
        out.append(token)
        i += 1
    return out


def _kubectl_is_mutating(args: list[str], depth: int) -> bool:
    """``args`` are the tokens after a ``kubectl`` word."""
    own = args[: args.index(_PASSTHROUGH)] if _PASSTHROUGH in args else args
    if any(t in _HELP_FLAGS or t in _LOCAL_RENDER_FLAGS or t.startswith(_LOCAL_RENDER_PREFIX) for t in own):
        return False
    operands = _operands(own)
    if not operands or operands[0] == "help":
        return False
    if len(operands) > 1 and operands[1] == _LOCAL_RENDER_SUBVERB:
        return False
    verb = operands[0]
    rest = args[args.index(verb) + 1 :] if verb in args else []
    if verb in _MUTATING_SUBVERBS:
        return len(operands) > 1 and operands[1] in _MUTATING_SUBVERBS[verb]
    if verb in _MUTATING_VERBS:
        return True
    # ``kubectl exec … -- kubectl scale …``: what runs behind the passthrough.
    if _PASSTHROUGH in rest:
        return _command_is_mutating(rest[rest.index(_PASSTHROUGH) + 1 :], depth + 1)
    return False


def _command_is_mutating(words: list[str], depth: int) -> bool:
    """One simple command: strip assignments and wrappers, then read the command word."""
    if depth > _MAX_DEPTH:
        return False
    i = 0
    wrapper = None
    pending_operand = False
    while i < len(words):
        token = words[i]
        if token in _KEYWORDS or _ASSIGNMENT_RE.match(token) or token in _WRAPPERS:
            wrapper = token if token in _WRAPPERS else None
            pending_operand = token in _WRAPPERS_WITH_OPERAND
            i += 1
        elif wrapper and token in _WRAPPER_VALUE_OPTIONS.get(wrapper, ()):
            i += 2
        elif wrapper and (token.startswith("-") or token == _XARGS_PLACEHOLDER):
            i += 1
        elif pending_operand:  # ``timeout``'s duration, after its options
            pending_operand = False
            i += 1
        else:
            break
    if i >= len(words):
        return False
    word, args = words[i].rsplit("/", 1)[-1], words[i + 1 :]
    if word == "kubectl":
        return _kubectl_is_mutating(args, depth)
    is_python = bool(_PYTHON_RE.match(word))
    if word in _SHELLS or is_python:
        script = _dash_c_operand(args, cluster=not is_python)
        return script is not None and _text_is_mutating(script, depth + 1, loose=is_python)
    if word == "eval":
        return _text_is_mutating(" ".join(args), depth + 1, loose=False)
    return False


def _dash_c_operand(args: list[str], *, cluster: bool) -> str | None:
    """The string after ``-c`` (a shell also accepts it inside a cluster, ``-lc``), skipping a ``--``."""
    for k, token in enumerate(args):
        hit = token == "-c" or (cluster and token.startswith("-") and not token.startswith("--") and "c" in token[1:])
        if hit:
            rest = args[k + 1 :]
            if rest and rest[0] == _PASSTHROUGH:
                rest = rest[1:]
            return rest[0] if rest else None
    return None


def _written_text_is_mutating(commands: list[list[str]], depth: int) -> bool:
    """What ``echo``/``printf`` print when a shell in the same call runs a file or a substitution."""
    runs_a_file = any(
        words and words[0].rsplit("/", 1)[-1] in _SHELLS and _dash_c_operand(words[1:], cluster=True) is None
        for words in commands
    )
    if not runs_a_file:
        return False
    for words in commands:
        if not words or words[0].rsplit("/", 1)[-1] not in _WRITERS:
            continue
        args = []
        for w in words[1:]:
            if w.startswith((">", "1>", "2>", "&>")):
                break
            if w not in _WRITER_OPTIONS:
                args.append(w)
        if words[0].rsplit("/", 1)[-1] == "printf" and len(args) > 1:
            args = args[1:]
        if args and _text_is_mutating(" ".join(args), depth + 1, loose=False):
            return True
    return False


def _text_is_mutating(text: str, depth: int, *, loose: bool) -> bool:
    if depth > _MAX_DEPTH or not text:
        return False
    if loose:
        text = _PYTHON_PREFIX_RE.sub("", _PYTHON_PUNCTUATION_RE.sub(" ", text))
    else:
        text, bodies = _split_heredocs(text)
        for body, body_loose in bodies:
            if _text_is_mutating(body, depth + 1, loose=body_loose):
                return True
    text = _WHICH_SUBST_RE.sub(r"\1", text)
    for inner in _SUBSTITUTION_RE.findall(text):
        if _text_is_mutating(inner[0] or inner[1], depth + 1, loose=False):
            return True
    tokens = _tokens(text, loose=loose)
    commands = _simple_commands(tokens)
    if not loose and _written_text_is_mutating(commands, depth):
        return True
    for words in commands:
        if _command_is_mutating(words, depth):
            return True
        if loose:
            # Program text (a Python list, a subprocess call): kubectl may sit anywhere,
            # and a quoted string is a command of its own.
            for k, token in enumerate(words):
                if token.rsplit("/", 1)[-1] == "kubectl" and _kubectl_is_mutating(words[k + 1 :], depth):
                    return True
                if " " in token and _text_is_mutating(token, depth + 1, loose=False):
                    return True
    return False


def names_mutating_verb(command: str, *, loose: bool = False) -> bool:
    """Whether ``command`` runs kubectl with a verb that changes state, read as kubectl reads it.

    ``loose`` is for program text rather than a shell command: list punctuation is
    dropped and ``kubectl`` may appear at any word position.
    """
    return _text_is_mutating(command, 0, loose=loose)


def classify_refusal(command, result, *, loose: bool = False) -> str | None:
    """``"policy"`` for a definitive refusal, ``"advisory"`` or ``"api"`` for guidance, else ``None``."""
    if not isinstance(result, str) or not result:
        return None
    if _POLICY_ACTION_RE.search(result):
        definitive = _DEFINITIVE_RULE_RE.search(result) and names_mutating_verb(command or "", loose=loose)
        return "policy" if definitive else "advisory"
    if _API_DENIED_RE.search(result):
        return "api"
    return None


def halt_after(config) -> int:
    """The configured threshold, or the default when the config predates the patch."""
    value = getattr(config, HALT_AFTER_ATTR, None)
    return value if isinstance(value, int) and value >= 1 else DEFAULT_HALT_AFTER


def turn_token(controller):
    """Something that changes when ``reset_for_turn`` runs: the per-turn container itself."""
    return getattr(controller, "_call_history", None)


def count_refusal(controller) -> int:
    """Count one definitive refusal for the current turn and return the turn's total."""
    token = turn_token(controller)
    previous = getattr(controller, _COUNT_ATTR, None)
    count = previous[1] + 1 if isinstance(previous, tuple) and previous[0] is token else 1
    setattr(controller, _COUNT_ATTR, (token, count))
    return count


def observe_refusal(controller, tool_name, args, result, signature, *, stock, failed, decide, decision_cls):
    """The decision that replaces ``stock`` when ``result`` is a refusal, else ``None``.

    ``args`` are the call's arguments (the command is read from them);
    ``stock`` is what the unpatched ``after_call`` decided, which guidance
    never overrides when it is a block or a halt; ``failed`` is the call's
    failure classification, which both guidance kinds must carry (a successful
    read, or a board read, whose output quotes a denial or the notice is
    neither); a definitive refusal does not need it, since a refused write
    chained before a read exits 0; ``decide`` is the
    controller's ``_decide`` (which records a halt as the turn's halt
    decision); ``decision_cls`` is ``ToolGuardrailDecision``. The last two are
    injected so this module never imports Hermes.
    """
    command, loose = command_text(args)
    kind = classify_refusal(command, result, loose=loose)
    if kind is None or (kind != "policy" and not failed):
        return None
    if kind != "policy":
        if getattr(stock, "action", "allow") not in ("allow", "warn"):
            return None
        code, message = (API_WARN_CODE, API_WARN_MESSAGE) if kind == "api" else (ADVISORY_WARN_CODE, ADVISORY_WARN_MESSAGE)
        return decision_cls(
            action="warn", code=code, message=message,
            tool_name=tool_name, count=0, signature=signature,
        )
    count = count_refusal(controller)
    if count >= halt_after(getattr(controller, "config", None)):
        return decide(
            "halt", HALT_CODE, tool_name, count, signature,
            message=HALT_MESSAGE,
        )
    return decision_cls(
        action="warn", code=WARN_CODE, message=WARN_MESSAGE,
        tool_name=tool_name, count=count, signature=signature,
    )

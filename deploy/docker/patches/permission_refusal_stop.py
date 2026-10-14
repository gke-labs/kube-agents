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
then decides whether a refusal overrides its decision. It reads what the
credential-proxy shim printed into the tool result, never the command's shell
text: the policy that refused the command resolved its verb once
(``command_policy.evaluate``), the proxy returns it, and the shim prints it
beside the rule (``credential_proxy_client.py``)::

    <the policy's message, ending in the action boundary notice>
    policy rule: kubernetes.read-only
    policy verb: scale deploy/kube-dns
    policy render: local          (a local render: a client dry-run in any of kubectl's
                                   spellings, --local, --list, apply view-last-applied, -h/--help)

For every tool result :func:`classify_refusal` returns:

* ``policy``: the ``kubernetes.read-only`` rule, the action boundary notice
  ("refused however it is attempted"), a ``policy verb:`` whose first word is
  a mutating kubectl verb (:data:`MUTATING_VERBS`, or ``rollout``/``auth``/
  ``certificate`` with a mutating second word), and no local-render line. The
  rule is an allowlist of read verbs and refuses an unlisted read spelling
  (``config view``, ``cluster-info dump``, ``exec``, ``<verb> --help``) with
  the same words, so the verb is what tells a refused write from a refused
  read. A call that ran several commands carries one block per refusal and
  any refused write among them counts. Counted per turn, across every tool
  and whatever succeeded in between. Two word tests over the call's own text
  remain, both for calls whose exit code is 0: a call whose text carries the
  result's own verb line (or rule line, when there is no verb) is a report
  being echoed, not a refusal; and a call that succeeded counts only when its
  text names ``kubectl`` and a mutating verb, which keeps a refused write
  chained before a read and drops a ``cat`` of a saved refusal. A report
  that names the command and prints a saved refusal in one exit-0 call is
  therefore counted, and a write that reached kubectl through a script or a
  variable and exited 0 is not; the proxy is the one party that could say,
  out of band, that a write was refused in this call, and that is the
  design that removes both.
* Not seen at all: a refused write whose stderr the call discarded
  (``2>/dev/null``, ``&>/dev/null``, a substitution never echoed). The
  result then carries nothing but an exit code, which is 77 from the shim
  and has arrived as 126 through the exec path, so it is not read; the
  bench's window has the same blind spot.
* Seen when it should not be: the result is the only evidence, so whoever
  writes what a tool prints (a script in a checked-out repository, a response
  a failing ``jq -e`` echoes) can write the shim's lines and halt the turn.
  The blast radius is the halt, a card closed with a fabricated refusal in
  front of the operator, never a write. The proxy telling the runtime out of
  band that it refused a write is the fix for this and the two limits above.
* ``advisory``: the notice without a definitive verb line: a refused read
  spelling, a local render, a ``gcp.read-only`` refusal, or a shim older
  than this patch that prints no verb. Guidance only, never counted; needs
  a failed call, so a successful read whose output quotes the notice gets
  nothing.
* ``api``: a 403 from the Kubernetes or Google Cloud API. Guidance only,
  never counted, needs a failed call: a fleet audit meets these on projects
  it may not read and goes on to the next one (#1946's own caveat). 401 and
  ``Unauthenticated`` are not refusals at all; the recovery ladder owns them.

The ``permission_refusal_halt_after``-th definitive refusal (``tool_loop_guardrails``
in the profile config, default :data:`DEFAULT_HALT_AFTER`, the first) halts
the turn through the controller's own halt decision, which
``agent/turn_tool_round.py`` already routes through the kanban-aware exit in
``kanban_guardrail_exit.py``: a worker gets one nudge to close its card with
the refusal, a chat turn ends with :data:`HALT_MESSAGE` as its reply. Below
the threshold a refusal appends guidance to the tool result instead; live
runs showed the model reading that guidance and going on investigating,
which is why the default halts on the first. Guidance replaces the stock
decision only when that decision was ``allow`` or ``warn``; a stock halt
(eight varied failures of one tool, say) still ends the run.

The counter lives on the controller and keys on the turn: Hermes rebuilds
``_call_history`` in ``reset_for_turn``, so the deque object names the turn
(held by reference, so a freed deque's address cannot be mistaken for it)
without a second anchor in that method. A kanban worker is one turn for the
whole card, which is the scope the count should have.

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
#: The three lines the shim prints after the policy's message, one block per
#: refused command (a call can run several). A terminal result is JSON text,
#: so a line break arrives as ``\n`` or as a newline.
_LINE_START = r"(?:^|\n|\\n)"
_RULE_LINE_RE = re.compile(_LINE_START + r"policy rule: ([\w.-]+)")
_VERB_LINE_RE = re.compile(_LINE_START + r"policy verb: ([^\n\\\"]+)")
_RENDER_LINE_RE = re.compile(_LINE_START + r"policy render: local\b")
_VERB_LINE_PREFIX = "policy verb: "
DEFINITIVE_RULE = "kubernetes.read-only"
#: kubectl verbs that change state (``debug`` starts a pod or container).
MUTATING_VERBS = frozenset({
    "scale", "patch", "edit", "apply", "replace", "autoscale", "annotate", "label", "create",
    "delete", "set", "run", "expose", "cordon", "uncordon", "drain", "taint", "debug",
})
#: Verbs whose second word decides: ``rollout restart``, ``auth reconcile``, ``certificate approve``.
MUTATING_SUBVERBS = {
    "rollout": frozenset({"restart", "undo", "pause", "resume"}),
    "auth": frozenset({"reconcile"}),
    "certificate": frozenset({"approve", "deny"}),
}
#: Kubernetes and Google Cloud API denials, in the shapes kubectl, gcloud and the
#: MCP tools print them. ``401``/``Unauthenticated`` deliberately absent.
_API_DENIED_RE = re.compile(
    r"Error from server \(Forbidden\)|\bis forbidden:|\bPERMISSION_DENIED\b|\"code\":\s*403\b|\bHTTP(?:/[\d.]+)? 403\b",
)
#: Attribute on the controller holding ``(turn token, count)``.
_COUNT_ATTR = "_kube_permission_refusals"
#: A call that succeeded (exit 0) counts only when its text names kubectl and a
#: mutating verb: a refused write chained before a read does, a ``cat`` or
#: ``kubectl logs`` whose output happens to carry the shim's lines does not.
_KUBECTL_WORD_RE = re.compile(r"(?<![\w-])kubectl(?![\w-])")
# ``kubectl auth can-i <verb> …`` names a verb in a read's arguments; the phrase is
# dropped before the word test.
_CAN_I_RE = re.compile(r"(?<![\w-])kubectl(?:[ \t]+[^\s;|&]+)*?[ \t]+auth[ \t]+can-i\b[^;|&\n]*")

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


def command_text(args) -> str:
    """Every string the call's arguments carry, used only to spot a report that echoes a refusal."""
    if not isinstance(args, dict):
        return ""
    return "\n".join(v for v in args.values() if isinstance(v, str))


def verb_is_mutating(verb_words: list[str]) -> bool:
    """Whether the verb the policy resolved (``policy verb:``) changes state."""
    if not verb_words:
        return False
    head = verb_words[0]
    if head in MUTATING_SUBVERBS:
        return len(verb_words) > 1 and verb_words[1] in MUTATING_SUBVERBS[head]
    return head in MUTATING_VERBS


def names_a_write(command) -> bool:
    """Whether the call's own text names kubectl and, after its first ``kubectl`` word, a
    mutating verb (a word test, used only to tell a refused write chained before an exit-0
    read from a read that echoed a refusal; a ``set -e`` prologue is before the word)."""
    if not isinstance(command, str):
        return False
    text = _CAN_I_RE.sub("", command)
    first = _KUBECTL_WORD_RE.search(text)
    if first is None:
        return False
    words = set(re.split(r"[\s\"'`;|&()\[\],]+", text[first.end():]))
    return bool(words & MUTATING_VERBS) or any(
        head in words and words & subs for head, subs in MUTATING_SUBVERBS.items()
    )


def refusal_blocks(result: str) -> list[tuple[str, list[str], bool]]:
    """``(rule, verb words, local render)`` per ``policy rule:`` block in ``result``, in order."""
    blocks = []
    rules = list(_RULE_LINE_RE.finditer(result))
    for i, rule in enumerate(rules):
        end = rules[i + 1].start() if i + 1 < len(rules) else len(result)
        block = result[rule.start():end]
        verb = _VERB_LINE_RE.search(block)
        blocks.append((rule.group(1), verb.group(1).split() if verb else [], _RENDER_LINE_RE.search(block) is not None))
    return blocks


def definitive_block(result: str):
    """The first block that is a refused write (rule, mutating verb, not a local render), or ``None``."""
    for rule, words, render in refusal_blocks(result):
        if rule == DEFINITIVE_RULE and verb_is_mutating(words) and not render:
            return rule, words, render
    return None


def echoes_refusal(command, result) -> bool:
    """Whether the call's own text carries the result's refusal lines: a report, not a refusal.
    Keyed to the verb line the result carries, so a comment quoting an earlier
    ``policy rule:`` beside a fresh write does not pass as an echo."""
    if not isinstance(command, str) or not command:
        return False
    for rule, words, _ in refusal_blocks(result):
        line = _VERB_LINE_PREFIX + " ".join(words) if words else "policy rule: " + rule
        if line in command:
            return True
    return False


def classify_refusal(command, result, *, failed=True) -> str | None:
    """``"policy"`` for a definitive refusal, ``"advisory"`` or ``"api"`` for guidance, else ``None``."""
    if not isinstance(result, str) or not result:
        return None
    if _POLICY_ACTION_RE.search(result):
        if not failed and echoes_refusal(command, result):
            return None
        definitive = definitive_block(result) is not None and (failed or names_a_write(command))
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
    kind = classify_refusal(command_text(args), result, failed=bool(failed))
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

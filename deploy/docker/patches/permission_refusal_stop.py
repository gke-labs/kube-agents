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
Installed into ``ToolCallGuardrailController.after_call`` by
``apply_permission_refusal_stop.py``, ahead of the stock counters. For every
tool result it asks :func:`classify_refusal`:

* ``policy``: the command policy's ``kubernetes.read-only`` rule refused the
  command and said it is final (the action boundary notice ``command_policy.py``
  prints: "refused however it is attempted"). That rule admits every kubectl
  read verb, so what it refuses is a write or an action. Counted per turn,
  across every tool and whatever succeeded in between. The ``permission_refusal_halt_after``-th
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
* ``gcloud``: the ``gcp.read-only`` rule's refusal, which carries the same
  notice. Guidance only, never counted: that rule is an allowlist of known
  reads and refuses an unlisted read with the same words, telling the agent to
  use an allowed spelling or report the gap, and a halt there ended a live
  Cluster Agent investigation on ``gcloud artifacts docker tags list``.
* ``api``: a 403 from the Kubernetes or Google Cloud API. Guidance only, never
  counted: a fleet audit meets these on projects it may not read and goes on
  to the next one, and halting it would be the wrong fix (#1946's own caveat).
  401 and ``Unauthenticated`` are not refusals at all; the recovery ladder
  owns them.

The counter lives on the controller and keys on the turn: Hermes rebuilds
``_call_history`` in ``reset_for_turn``, so its identity names the turn without
a second anchor in that method. A kanban worker is one turn for the whole card,
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

#: Definitive refusals in one turn before the run is halted: the first.
DEFAULT_HALT_AFTER = 1
#: The attribute ``apply_permission_refusal_stop.py`` adds to the guardrail config.
HALT_AFTER_ATTR = "kube_permission_refusal_halt_after"
#: Decision codes, in the shape of upstream's.
WARN_CODE = "permission_refusal"
GCLOUD_WARN_CODE = "gcloud_policy_refusal"
API_WARN_CODE = "permission_denied"
HALT_CODE = "permission_refusal_halt"
#: The phrase only the command policy's action boundary notice carries.
_POLICY_ACTION_RE = re.compile(r"refused however it is attempted")
#: The rule whose refusals are definitive: it allows every kubectl read.
_DEFINITIVE_RULE_RE = re.compile(r"policy rule: kubernetes\.read-only\b")
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
    "only if the request asked for a pull request. Another refused change in this run "
    "ends the run."
)
GCLOUD_WARN_MESSAGE = (
    "The command policy refused this gcloud command. If it would change cloud state, that "
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


def classify_refusal(result) -> str | None:
    """``"policy"`` for a definitive refusal, ``"gcloud"`` or ``"api"`` for guidance, else ``None``."""
    if not isinstance(result, str) or not result:
        return None
    if _POLICY_ACTION_RE.search(result):
        return "policy" if _DEFINITIVE_RULE_RE.search(result) else "gcloud"
    if _API_DENIED_RE.search(result):
        return "api"
    return None


def halt_after(config) -> int:
    """The configured threshold, or the default when the config predates the patch."""
    value = getattr(config, HALT_AFTER_ATTR, None)
    return value if isinstance(value, int) and value >= 1 else DEFAULT_HALT_AFTER


def turn_token(controller):
    """Something that changes when ``reset_for_turn`` runs: the identity of a per-turn container."""
    history = getattr(controller, "_call_history", None)
    return id(history) if history is not None else None


def count_refusal(controller) -> int:
    """Count one definitive refusal for the current turn and return the turn's total."""
    token = turn_token(controller)
    previous = getattr(controller, _COUNT_ATTR, None)
    count = previous[1] + 1 if isinstance(previous, tuple) and previous[0] == token else 1
    setattr(controller, _COUNT_ATTR, (token, count))
    return count


def observe_refusal(controller, tool_name, result, signature, *, decide, decision_cls):
    """The decision for ``result`` when it is a refusal, else ``None`` so the stock path runs.

    ``decide`` is the controller's ``_decide`` (which records a halt as the
    turn's halt decision); ``decision_cls`` is ``ToolGuardrailDecision``. Both
    are injected so this module never imports Hermes.
    """
    kind = classify_refusal(result)
    if kind is None:
        return None
    if kind != "policy":
        code, message = (API_WARN_CODE, API_WARN_MESSAGE) if kind == "api" else (GCLOUD_WARN_CODE, GCLOUD_WARN_MESSAGE)
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

"""Wire agent/credential_redaction.py into the five upstream sites it needs.

One edit per file, none of them touched by another applier, so this runs at any
point after ``credential_redaction.py`` is installed at
``/opt/hermes/agent/credential_redaction.py``:

1. ``agent/redact.py`` -- a trailer that registers the kube-agents patterns
   through upstream's own ``register_redaction_patterns`` at import time. An
   append, because it has to run after every name it uses is defined and
   before any caller's first ``redact_sensitive_text``; an AST locator asserts
   the registry function is still there so the trailer can never land on a
   file that would raise at import.
2. ``agent/display.py`` -- ``redact_tool_args_for_display`` gains a branch for
   ``terminal`` and ``execute_code`` after the ``browser_type`` one. A literal
   anchor on that branch (two lines at v2026.9.14: the test and a dict-merge
   return) plus the ``return args`` it precedes, because the edit is a rewrite
   of the function's tail and the branch is the only text in the file that
   pins it.
3. ``hermes_cli/main.py`` -- the transcript wrapper is installed as the first
   statement of ``main()``, the console entry point every ``hermes`` argv
   resolves to, ahead of the fast-launch returns and of the first print.
   ``find_def("main").body_start`` places it after the docstring.
4. ``hermes_state_messages.py`` -- ``SessionMessagesMixin._message_row_params``,
   the one binder every message insert goes through, gets a prologue that
   passes the message dict and its tool calls through
   ``redact_message_for_store``. ``find_method`` locates it inside the mixin,
   and ``expect_parameters`` asserts the two names the prologue rebinds.
5. ``hermes_cli/kanban_db.py`` -- ``create_task``, ``complete_task`` and
   ``edit_completed_task_result`` each get a prologue that passes their text
   parameters (title and body; result and summary) through
   ``redact_store_text``, located and checked the same way before any insert.

The trailer in 1 also rebinds ``redact_sensitive_text`` to the operator-rules
composition, outside the registration's ``try``, so an unreadable rule file
stops the import. None of the five consumes its anchor, so the count check cannot tell a fresh
file from a patched one; each edit names a marker only the insert contains
and refuses to run twice.

See the module docstring in credential_redaction.py for the leak.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

PREFIX = "credential-redaction"

REDACT_RELATIVE = "agent/redact.py"
DISPLAY_RELATIVE = "agent/display.py"
MAIN_RELATIVE = "hermes_cli/main.py"
STATE_RELATIVE = "hermes_state_messages.py"
KANBAN_RELATIVE = "hermes_cli/kanban_db.py"

#: The upstream function the trailer calls; located, not anchored, so a moved
#: or reformatted definition still passes and a removed one still fails.
REGISTRY_FUNCTION = "register_redaction_patterns"
REDACT_MARKER = "_kube_agents_register_patterns"
REDACT_TRAILER = '''

# kube-agents patch: register the credential shapes this deployment handles
# that _PREFIX_PATTERNS above does not (GCP OAuth access tokens, ``ya29.``).
# Here, at the end of the module, so every process that imports agent.redact
# has them before its first redact_sensitive_text call, through the same
# additive registry a plugin would use. See agent/credential_redaction.py.
try:
    from agent.credential_redaction import (
        register_kube_agents_patterns as _kube_agents_register_patterns,
    )

    _kube_agents_register_patterns()
except Exception:  # noqa: BLE001 - a failed registration must not break import
    logger.warning("kube-agents: credential pattern registration failed", exc_info=True)

# kube-agents patch: the operator's redaction rules (KUBE_AGENTS_REDACTION_CONFIG,
# the file the gateway's pre-call hook reads) applied after the masking above,
# so agent.log, the terminal-output cache, the worker transcript and the
# ``💻 $`` line carry them too. Rebound here, at the end of the module, so
# every later import and every call through the module global gets the
# composed form. Not inside the try above: a rule file that does not parse
# must stop the process at import, as it stops the gateway, rather than let
# it run with the rules silently off. Nothing changes while no rule file is
# projected or named. See agent/credential_redaction.py.
try:
    from agent.credential_redaction import (
        install_operator_rules as _kube_agents_install_operator_rules,
    )
except Exception:  # noqa: BLE001 - the module ships in the image; a missing one is logged
    logger.warning("kube-agents: operator rule redaction unavailable", exc_info=True)
    _kube_agents_install_operator_rules = None
if _kube_agents_install_operator_rules is not None:
    redact_sensitive_text = _kube_agents_install_operator_rules(redact_sensitive_text)
'''
RULES_MARKER = "_kube_agents_install_operator_rules"

DISPLAY_ANCHOR = (
    '    if tool_name == "browser_type" and isinstance(args.get("text"), str):\n'
    '        return {**args, "text": redact_sensitive_text(args["text"], force=True)}\n'
    '    return args\n'
)
DISPLAY_MARKER = "_kube_agents_redact_tool_argument"
DISPLAY_INSERT = '''    # kube-agents patch: a terminal command or execute_code source carries
    # whatever the model pasted into it -- an Authorization header, an
    # exported token -- and this is the copy the CLI's completion line, the
    # tool preview and the progress callbacks all render. Same redactor and
    # same force=True as the browser_type branch above, so the operator
    # opt-out does not reopen the transcript. See agent/credential_redaction.py.
    try:
        from agent.credential_redaction import (
            redact_tool_argument_for_display as _kube_agents_redact_tool_argument,
        )
    except Exception:  # noqa: BLE001 - display must never abort a turn
        logger.warning("kube-agents: tool-argument redaction unavailable", exc_info=True)
        return args
    return _kube_agents_redact_tool_argument(
        tool_name, args, lambda text: redact_sensitive_text(text, force=True)
    )
'''
DISPLAY_PATCHED = DISPLAY_ANCHOR[: -len("    return args\n")] + DISPLAY_INSERT

MAIN_FUNCTION = "main"
MAIN_MARKER = "_kube_agents_install_transcript_redaction"
#: Indented by the applier to the body's own indentation.
MAIN_PROLOGUE_LINES = (
    "# kube-agents patch: a kanban worker's stdout and stderr are the transcript",
    "# file the dispatcher opened for it, and nothing upstream redacts that",
    "# boundary. Wrap both before anything prints -- prompt_toolkit caches its",
    "# output object on first use -- and before the fast-launch returns below.",
    "# A no-op unless HERMES_KANBAN_TASK is set and stdout is not a TTY.",
    "# See agent/credential_redaction.py.",
    "try:",
    "    from agent.credential_redaction import (",
    "        install_or_report as _kube_agents_install_transcript_redaction,",
    "    )",
    "except Exception:  # noqa: BLE001 - the CLI must start without the module",
    "    _kube_agents_install_transcript_redaction = None",
    "if _kube_agents_install_transcript_redaction is not None:",
    "    _kube_agents_install_transcript_redaction()",
    "",
)


STATE_CLASS = "SessionMessagesMixin"
STATE_METHOD = "_message_row_params"
STATE_PARAMETERS = ("msg", "tool_calls")
STATE_MARKER = "_kube_agents_redact_message_for_store"
#: Indented by the applier to the body's own indentation.
STATE_PROLOGUE_LINES = (
    "# kube-agents patch: with the operator's rule file projected (or named by",
    "# KUBE_AGENTS_REDACTION_CONFIG), what this row carries to state.db --",
    "# content, reasoning, tool-call arguments -- goes through the forced",
    "# redactor first, credential shapes and the operator's rules alike, in",
    "# copies. A no-op while no rule file is present.",
    "# See agent/credential_redaction.py.",
    "from agent.credential_redaction import (",
    "    redact_message_for_store as _kube_agents_redact_message_for_store,",
    ")",
    "msg, tool_calls = _kube_agents_redact_message_for_store(msg, tool_calls)",
)
#: Each kanban writer and the text parameters its prologue rebinds: a card's
#: title and body, a worker's result and summary, a comment's body (the path
#: the ended-run patch sends a worker's result down when a parent is linked
#: mid-run) and a block's reason. The board's other text columns are names,
#: states and ids the tools write from their own vocabulary.
KANBAN_WRITERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("create_task", ("title", "body")),
    ("complete_task", ("result", "summary")),
    ("edit_completed_task_result", ("result", "summary")),
    ("add_comment", ("body",)),
    ("block_task", ("reason",)),
)
KANBAN_MARKER = "_kube_agents_redact_store_text"
KANBAN_PROLOGUE_HEAD = (
    "# kube-agents patch: with the operator's rule file projected (or named by",
    "# KUBE_AGENTS_REDACTION_CONFIG), the text this writer puts on the board",
    "# goes through the forced redactor first. A no-op while no rule file is",
    "# present. See agent/credential_redaction.py.",
    "from agent.credential_redaction import (",
    "    redact_store_text as _kube_agents_redact_store_text,",
    ")",
)


def _prologue(site, lines) -> str:
    return "".join((site.body_indent + line if line else "") + "\n" for line in lines)


def _apply_redact(root: Path) -> None:
    patch = patchlib.Patch(root, REDACT_RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(REDACT_MARKER, RULES_MARKER)
    patch.find_def(REGISTRY_FUNCTION, label="pattern registry")
    patch.append(REDACT_TRAILER)
    patch.commit("kube-agents pattern registration trailer")


def _apply_display(root: Path) -> None:
    patch = patchlib.Patch(root, DISPLAY_RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(DISPLAY_MARKER)
    patch.substitute(DISPLAY_ANCHOR, DISPLAY_PATCHED, label="tool-args display branch")
    patch.commit("terminal/execute_code display redaction")


def _apply_main(root: Path) -> None:
    patch = patchlib.Patch(root, MAIN_RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(MAIN_MARKER)
    site = patch.find_def(MAIN_FUNCTION, label="console entry point")
    prologue = "".join(
        (site.body_indent + line if line else "") + "\n" for line in MAIN_PROLOGUE_LINES
    )
    patch.insert(site.body_start, prologue)
    patch.commit("worker transcript redaction prologue")


def _apply_state(root: Path) -> None:
    patch = patchlib.Patch(root, STATE_RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(STATE_MARKER)
    site = patch.find_method(STATE_CLASS, STATE_METHOD, label="message row binder")
    site.expect_parameters(*STATE_PARAMETERS)
    patch.insert(site.body_start, _prologue(site, STATE_PROLOGUE_LINES))
    patch.commit("session store redaction prologue")


def _apply_kanban(root: Path) -> None:
    patch = patchlib.Patch(root, KANBAN_RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(KANBAN_MARKER)
    # Located before any insert: an offset taken earlier moves with each one.
    for name, parameters in KANBAN_WRITERS:
        site = patch.find_def(name, label=f"kanban writer {name}")
        site.expect_parameters(*parameters)
    for name, parameters in KANBAN_WRITERS:
        site = patch.find_def(name, label=f"kanban writer {name}")
        lines = KANBAN_PROLOGUE_HEAD + tuple(
            f"{parameter} = {KANBAN_MARKER}({parameter})" for parameter in parameters
        )
        patch.insert(site.body_start, _prologue(site, lines))
    patch.commit("kanban board redaction prologues")


def apply(root: Path) -> None:
    _apply_redact(root)
    _apply_display(root)
    _apply_main(root)
    _apply_state(root)
    _apply_kanban(root)


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))

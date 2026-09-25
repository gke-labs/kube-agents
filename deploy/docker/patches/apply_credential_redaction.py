"""Wire agent/credential_redaction.py into the three upstream sites it needs.

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

None of the three consumes its anchor, so the count check cannot tell a fresh
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
'''

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


def _apply_redact(root: Path) -> None:
    patch = patchlib.Patch(root, REDACT_RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(REDACT_MARKER)
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


def apply(root: Path) -> None:
    _apply_redact(root)
    _apply_display(root)
    _apply_main(root)


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))

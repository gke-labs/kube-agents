#!/usr/bin/env python3
"""Wire tools/skill_manage_image_owned.py into ``skill_manage``.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Two files. In
``tools/skill_manager_tool.py``, the first statements of ``skill_manage``
gain two gates. An ``operations`` batch is checked whole before it is handed
to ``_skill_manage_batch``, so a batch naming a shipped skill is refused
before the batch snapshots that skill's directory for its rollback; a flat
call is checked after the batch dispatch and before the background-review
preflight. The batch also re-enters ``skill_manage`` once per operation
(``_skill_manage_from``), so the flat gate answers each operation a second
time, which is what covers a batch shape a future upstream might route
differently. Both sit above ``_apply_skill_write_gate`` rather than inside
it, which also covers the approved-replay path that bypasses that gate: a
shipped skill is not writable by approval either.

In ``agent/file_safety.py``, the write classification the file tools consult
(``write_file``, ``patch``, ``delete_file``, ``move_file`` all call
``get_write_denied_error``) gains a category for a target under a Hermes
home's ``skills/`` or ``scripts/`` tree, placed after upstream's credential
checks and before its safe-root check, and the message for that category
names the right tool. Two anchors there, each found exactly once.

Same guarantee as the other patches here: the anchor must be found exactly
once, the file must still parse, and anything else fails the build loudly
rather than shipping a half-patched image. Why the change is needed is in the
module docstring of ``deploy/docker/patches/skill_manage_image_owned.py``.

Usage::

    python3 apply_skill_manage_image_owned.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "tools/skill_manager_tool.py"
MARKER = "from tools.skill_manage_image_owned import image_owned_batch_refusal, image_owned_refusal"

SAFETY_RELATIVE = "agent/file_safety.py"
SAFETY_MARKER = "from tools.skill_manage_image_owned import image_managed_tree"

# The tail of _classify_write_denial at v2026.9.14: the safe-root test is the
# last thing before the function lets a write through.
SAFETY_CLASSIFY_ANCHOR = (
    "    safe_roots = get_safe_write_roots()\n"
    "    if safe_roots and not any(_is_under(resolved, root) for root in safe_roots):\n"
    '        return "safe_root"\n'
)
SAFETY_CLASSIFY_PATCHED = (
    "    # kube-agents patch: the image-managed skills and scripts trees are not the\n"
    "    # file tools' to write. See tools/skill_manage_image_owned.py.\n"
    "    from tools.skill_manage_image_owned import image_managed_tree\n"
    "    if image_managed_tree(resolved, _hermes_dirs()) is not None:\n"
    '        return "image_managed"\n'
    "    safe_roots = get_safe_write_roots()\n"
    "    if safe_roots and not any(_is_under(resolved, root) for root in safe_roots):\n"
    '        return "safe_root"\n'
)
# The last line of get_write_denied_error: the generic message for every
# other denial. The new category is answered ahead of it.
SAFETY_MESSAGE_ANCHOR = (
    "    return f\"{verb} denied: '{path}' is a protected system/credential file.\" if denial else None\n"
)
SAFETY_MESSAGE_PATCHED = (
    '    if denial == "image_managed":\n'
    "        from tools.skill_manage_image_owned import image_managed_tree, image_managed_write_refusal\n"
    "        _, _resolved = _home_and_resolved(path)\n"
    "        _tree = image_managed_tree(_resolved, _hermes_dirs()) or \"an image-managed tree\"\n"
    "        return image_managed_write_refusal(path, _tree, verb=verb)\n"
    "    return f\"{verb} denied: '{path}' is a protected system/credential file.\" if denial else None\n"
)

# The first statements of skill_manage at v2026.9.14. `tool_error` is bound at
# module level further down the file (`from tools.registry import registry,
# tool_error`), the way the function's own later `return tool_error(...)`
# relies on it, so the inserted branch can use it without an import of its own.
ANCHOR = (
    "    if operations is not None:\n"
    "        return _skill_manage_batch(\n"
    "            operations, default_name=name or None, task_id=task_id, session_id=session_id)\n"
    "    if (preflight := _background_review_preflight(action, name)) is not None:\n"
)

PATCHED = (
    "    # kube-agents patch: skills the image ships are image-owned and are not\n"
    "    # written from here, in a batch or flat. See tools/skill_manage_image_owned.py.\n"
    "    from tools.skill_manage_image_owned import image_owned_batch_refusal, image_owned_refusal\n"
    "    if operations is not None:\n"
    "        if (_image_owned := image_owned_batch_refusal(operations, name or None)) is not None:\n"
    "            return tool_error(_image_owned, success=False)\n"
    "        return _skill_manage_batch(\n"
    "            operations, default_name=name or None, task_id=task_id, session_id=session_id)\n"
    "    if (_image_owned := image_owned_refusal(action, name, category=category)) is not None:\n"
    "        return tool_error(_image_owned, success=False)\n"
    "    if (preflight := _background_review_preflight(action, name)) is not None:\n"
)


def apply(root: Path) -> None:
    """Apply both edits under ``root``, or raise SystemExit with the reason.

    Nothing is written until each file's own edits all succeed; the second
    file is only opened once the first has committed, so a drifted anchor in
    ``agent/file_safety.py`` leaves ``tools/skill_manager_tool.py`` patched and
    the build failed, which the Dockerfile's grep guards then report.
    """
    patch = patchlib.Patch(root, RELATIVE, prefix="skill_manage_image_owned")
    patch.refuse_if_patched(MARKER)
    patch.substitute(ANCHOR, PATCHED, label="skill_manage prologue", expected=1)
    patch.commit("image-owned gates at the top of skill_manage, batch and flat")

    safety = patchlib.Patch(root, SAFETY_RELATIVE, prefix="skill_manage_image_owned")
    safety.refuse_if_patched(SAFETY_MARKER)
    safety.substitute(SAFETY_CLASSIFY_ANCHOR, SAFETY_CLASSIFY_PATCHED, label="write classification", expected=1)
    safety.substitute(SAFETY_MESSAGE_ANCHOR, SAFETY_MESSAGE_PATCHED, label="denial message", expected=1)
    safety.commit("file tools refuse the image-managed skills and scripts trees")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))

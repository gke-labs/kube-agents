#!/usr/bin/env python3
"""Build-time behaviour gate for the Google Chat attachment-notice patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_google_chat_attachment_notice.py``. The applier proves
the anchor matched and that the file still parses; this proves the notice the
adapter actually builds is English on both branches, and that the branch which
offers ``/setup-files`` is the one where the command works.

A grep cannot do that. The two branches differ only in which strings are
appended, so a patch that wired the condition backwards -- offering
``/setup-files`` precisely where the relay has stubbed it out -- greps
identically to a correct one, and the next reader of the difference is a user
following an instruction into a dead end.

``test_google_chat_attachment_notice.py`` covers the applier against a fixture
and cannot cover any of this: the edit lives inside Hermes' own module, and the
unit suite never sees the file that ships.

The module is loaded by path rather than imported as
``plugins.platforms.google_chat.adapter`` so the gate does not depend on the
package's ``__init__`` being importable at build time. ``adapter.py`` imports
``gateway.*`` at module level, so ``_load`` puts the tree on ``sys.path`` first,
the way verify_kanban_worker_tools.py and its siblings do. Running the script
from ``/opt/hermes`` is not enough on its own: for ``python3 script.py``,
``sys.path[0]`` is the script's directory, not the working directory.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
from pathlib import Path

RELATIVE = "plugins/platforms/google_chat/adapter.py"

# Verbatim fragments of what upstream shipped. Substrings rather than a language
# heuristic: these four are the exact text that reached a user's thread, and
# absence of them is the thing being asserted.
SPANISH = (
    "No he podido adjuntar",
    "Google Chat sólo permite adjuntar archivos",
    "Para activarlo",
    "Mientras tanto el archivo",
)

# The one non-ASCII character the notice is allowed to carry. Anything else,
# whether in a string constant inside the patched method (check 2) or in the
# text it renders (``_notice``), is words in some language -- which is how this
# regressed in the first place.
WARNING_SIGN = "⚠️"

PATH = "/opt/data/kanban/attachments/t_e5e1ba5e/report.md"
FILENAME = "report.md"


def _fail(detail: str) -> "SystemExit":
    return SystemExit(f"google_chat_attachment_notice verify: {detail}")


def _load(root: Path):
    path = root / RELATIVE
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("gc_adapter_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _notice(cls, *, caption=None) -> str:
    """Drive the real method on a bare instance and return what it would send.

    ``object.__new__`` skips ``__init__`` deliberately: the method reads nothing
    off the instance but ``_create_message``, and constructing a real adapter
    would want a config, a Pub/Sub subscription and the Google SDK.
    """
    adapter = object.__new__(cls)
    sent: dict = {}

    async def capture(chat_id, body):
        sent["body"] = body

    adapter._create_message = capture
    result = asyncio.run(
        cls._post_attachment_fallback(
            adapter, "spaces/AAAA", PATH, FILENAME, caption, None
        )
    )
    if result.success:
        raise _fail("the fallback reported success; it delivered no attachment")
    if "body" not in sent:
        raise _fail("the fallback sent no message at all")
    text = sent["body"]["text"]
    # The rendered notice, not just the literals check 2 can see. That scan
    # walks the method's own constants, so one level of indirection -- a
    # module-level name holding the sentence, a .format() off a table -- passes
    # it and ships text in any language. Both arguments this gate passes in
    # (PATH, FILENAME) and every caption it uses are ASCII, so anything
    # non-ASCII surviving here came from the notice.
    if not text.replace(WARNING_SIGN, "").isascii():
        raise _fail(f"non-ASCII text in the rendered notice: {text!r}")
    return text


def main(root: Path) -> None:
    source = (root / RELATIVE).read_text(encoding="utf-8")
    adapter_module = _load(root)
    cls = adapter_module.GoogleChatAdapter

    # 1) None of what upstream shipped may survive anywhere in the file.
    for fragment in SPANISH:
        if fragment in source:
            raise _fail(f"upstream Spanish survives in the adapter: {fragment!r}")

    # 2) Every string appended to ``lines`` is ASCII, bar the warning sign.
    #    Catches a reintroduction this gate has no fragment for, in any
    #    language, without guessing at which words are English. It reaches only
    #    the literals written inside the method; ``_notice`` runs the same test
    #    over the rendered result, which is what catches one held behind a name.
    #
    #    Scoped to the ``lines`` calls rather than the whole method because the
    #    ``error=`` string on the returned SendResult is deliberately left as
    #    upstream wrote it, em dash and all: it goes to gateway.log, not to a
    #    user. Widening this to every constant in the method would fail on it.
    method = next(
        (
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_post_attachment_fallback"
        ),
        None,
    )
    if method is None:
        raise _fail("no _post_attachment_fallback in the patched adapter")
    appends = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("append", "extend")
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "lines"
    ]
    if not appends:
        raise _fail("the notice is no longer built by appending to `lines`")
    for call in appends:
        for node in ast.walk(call):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if not node.value.replace(WARNING_SIGN, "").isascii():
                raise _fail(
                    f"non-ASCII text in the notice at line {node.lineno}: "
                    f"{node.value[:60]!r}"
                )

    # 3) The default branch -- no relay -- keeps the /setup-files instruction,
    #    because on that install the command works. Asserted positively so a
    #    patch that fixed the language by deleting the guidance cannot pass.
    plain = _notice(cls)
    if "/setup-files" not in plain:
        raise _fail(
            f"an install without the relay lost its /setup-files step: {plain!r}"
        )
    if FILENAME not in plain or PATH not in plain:
        raise _fail(f"the notice names neither the file nor its path: {plain!r}")

    # 4) The relay branch drops it. The relay patch stubs
    #    _handle_setup_files_command, so pointing a user at it is an
    #    instruction that cannot be followed.
    relayed_cls = type(
        "RelayedGoogleChatAdapter", (cls,), {"_credential_proxy_relay_patched": True}
    )
    relayed = _notice(relayed_cls)
    if "/setup-files" in relayed:
        raise _fail(
            "the credential-proxy branch still offers /setup-files, which the "
            f"relay patch has stubbed out: {relayed!r}"
        )
    if "credential proxy" not in relayed:
        raise _fail(f"the credential-proxy branch does not say why: {relayed!r}")
    if PATH not in relayed:
        raise _fail(f"the credential-proxy branch loses the host path: {relayed!r}")
    # Having taken /setup-files away, this branch has to leave something in its
    # place, or it ends on a host path the user cannot reach.
    if "paste" not in relayed:
        raise _fail(
            f"the credential-proxy branch leaves the user nothing to do: {relayed!r}"
        )

    # 5) The caller's caption still leads. ``send_file`` passes the message the
    #    agent wrote alongside the file, and a notice that buried or dropped it
    #    would lose the only part a user asked for.
    captioned = _notice(cls, caption="Here is the fleet report.")
    if not captioned.startswith("Here is the fleet report.\n"):
        raise _fail(f"the caption is no longer first: {captioned!r}")

    print("google_chat_attachment_notice verify: ok")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))

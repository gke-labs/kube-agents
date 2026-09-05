#!/usr/bin/env python3
"""Say the attachment failed in English, and say something that is true.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``plugins/platforms/google_chat/adapter.py``.

The bug
-------
``_post_attachment_fallback`` is the notice a user gets when the bot produced a
file and could not upload it. Upstream ships its four user-facing lines in
Spanish, inside an otherwise English adapter with an English docstring above
them::

    f"⚠️ No he podido adjuntar **{filename}**.",
    "Google Chat sólo permite adjuntar archivos cuando el bot tiene "
    "permiso explícito tuyo (OAuth de usuario). Es un consentimiento "
    "único que se hace desde este chat.",
    "**Para activarlo:** envía `/setup-files` y sigue las instrucciones.",
    f"Mientras tanto el archivo está en el host: `{path}`",

Everything around it is English -- the adapter, its docstrings, the ``error=``
string this same method returns -- so it reads to the person in the thread as
the agent having switched language mid-conversation, which is how it was
reported and what sent the first investigation after the model rather than
after the adapter.

Nothing about it is rare. Native ``media.upload`` needs a *per-user* OAuth
token, so this method is reached on every attachment until each user has run
``/setup-files`` in their own DM. On the staging install this was reported
from, no user had: ``$HERMES_HOME/google_chat_user_tokens`` did not exist, so
every card that attached a file took this path. Card ``t_e5e1ba5e`` delivered a
9,867-byte report that way on 2026-08-18, and ``gateway.log`` carries the
matching ``[Google_Chat] Failed to send media (.md)`` from 2026-08-11.

Since #999 the notice is no longer what a user sees for all of those. On a
relay install ``agents/platform/scripts/google_chat_relay_patch.py`` overrides
this same method and pastes the deliverable into the thread instead, for a
UTF-8 ``.md``/``.txt``/``.json``/``.yaml``/``.yml``/``.csv``/``.log`` under
32 KiB — the ``t_e5e1ba5e`` report above is squarely one of those. What still
arrives here is the residual: a format with no text form, a file over that
ceiling, bytes that do not decode, a file that cannot be read, and any paste
the API refuses partway through. The text below is written for that set.

The second defect
-----------------
The Spanish text tells the user to run ``/setup-files``. On an install that
reaches Chat through the credential proxy, that command does nothing:
``agents/platform/scripts/google_chat_relay_patch.py`` replaces
``_handle_setup_files_command`` with a stub answering "File attachment setup is
unavailable through the credential proxy", because the OAuth flow needs
credentials the relay deliberately does not hold. Translating the line without
touching it would ship an English dead end.

Upstream's notice always ended with something the user could do, and dropping
``/setup-files`` on the relay branch would end it with a host path they cannot
reach. So that branch offers the one thing that does work there: asking the
agent to paste the contents into the chat, which needs no credentials because
the file is already on the agent's own disk.

So the middle of the notice branches on whether the relay patch is installed,
which it records as ``_credential_proxy_relay_patched`` (``RELAY_FLAG`` below)
on the adapter class. The attribute is set in ``patch_adapter_class`` at adapter
construction, well before any message is sent, and reading it through
``type(self)`` means an install with no relay is unaffected: it keeps the
``/setup-files`` instruction, because there the command works.

Every install the operator deploys takes the relay branch --
``k8s-operator/internal/controller/platformagent_manifests.go`` sets
``GOOGLE_CHAT_RELAY_URL`` whenever ``integration.googleChat.enabled`` is true,
and ``sitecustomize.py`` installs the patch on that variable alone. The other
branch is for an image run outside the operator, and it is kept rather than
folded away because deleting it would leave the instruction hard-coded off in
the one configuration where it works.

That the flag is private to the relay patch is the fragile part of this: a
rename there silently restores the dead end and nothing in the adapter would
notice. ``test_google_chat_attachment_notice.py`` reads the relay patch's own
source and asserts the assignment, so the rename fails CI rather than shipping.

What is deliberately not changed
--------------------------------
The ``error=`` string on the returned ``SendResult``. It is English already and
it goes to ``gateway.log``, not to a user, so the one reader it has is someone
grepping for why an upload failed — "run /setup-files in chat" is the right
lead for them even on a relay install, where the next thing they find is the
stub refusing.

Neither does this make attachments work. The file still only exists on the
agent host, and the notice still says so. Of the two user-facing fixes for
that, the second now exists: ``agents/platform/scripts/google_chat_relay_patch.py``
inlines a report rather than attaching it, which is why this notice is now the
residual path described above. Per-user OAuth — the fix that would produce a
real attachment — remains unbuilt, and is what a format with no text form
still needs.

Upstream: not reported. Worth doing — nobody running Hermes in an English
deployment wants this — but the branch above is kube-agents-specific and would
not survive the trip, so the two are separate pieces of work.

Usage::

    python3 apply_google_chat_attachment_notice.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "plugins/platforms/google_chat/adapter.py"

# Asserted in the built bundle by the Dockerfile, so a patch that silently stops
# applying fails the image build instead of shipping Spanish.
BUILD_MARKER = "kube-agents patch: attachment-fallback notice"

# The attribute agents/platform/scripts/google_chat_relay_patch.py leaves on the
# adapter class it patched. Read by the notice below to decide whether
# /setup-files is worth offering. It is that patch's private re-entrancy latch,
# not an interface, so test_google_chat_attachment_notice.py pins the name
# against the relay patch's source; see the module docstring.
RELAY_FLAG = "_credential_proxy_relay_patched"

# The docstring's second paragraph through the end of the message block. One
# contiguous anchor rather than several, because the replacement rewrites the
# whole of what it describes: the docstring promises a ``/setup-files`` flow
# that the patched body only sometimes offers.
ANCHOR = '''\
        Tells the user that file delivery requires a one-time consent
        flow (``/setup-files``) and reports the local-host path so the
        file isn't lost. Returns ``success=False`` so callers know the
        attachment did not land.
        """
        lines = []
        if caption:
            lines.append(caption)
        lines.extend([
            f"⚠️ No he podido adjuntar **{filename}**.",
            "Google Chat sólo permite adjuntar archivos cuando el bot tiene "
            "permiso explícito tuyo (OAuth de usuario). Es un consentimiento "
            "único que se hace desde este chat.",
            "**Para activarlo:** envía `/setup-files` y sigue las instrucciones.",
            f"Mientras tanto el archivo está en el host: `{path}`",
        ])
'''

PATCHED = '''\
        Names the file, says why it could not be attached, and reports the
        host path so the file is not lost. Returns ``success=False`` so
        callers know the attachment did not land.

        Whether the notice offers ``/setup-files`` depends on whether that
        command can do anything here -- see the module docstring in
        deploy/docker/patches/apply_google_chat_attachment_notice.py.
        """
        # kube-agents patch: attachment-fallback notice. Upstream wrote these
        # lines in Spanish, which reads to the user as the agent changing
        # language mid-thread, and pointed every install at /setup-files --
        # including the ones whose relay patch has stubbed that command out.
        relayed = getattr(type(self), "_credential_proxy_relay_patched", False)
        lines = []
        if caption:
            lines.append(caption)
        lines.append(f"⚠️ Couldn't attach **{filename}**.")
        if relayed:
            lines.append(
                "File attachments are unavailable on this deployment: "
                "uploading to Google Chat needs a per-user OAuth token, and "
                "this install reaches Chat through the credential proxy, "
                "which holds no user credentials."
            )
            lines.append(
                "This one could not be pasted into the thread either: it "
                "has no text form, is too large to read here, could not "
                "be read at all, or posting it failed partway through."
            )
        else:
            lines.extend([
                "Google Chat accepts an upload only once you have given the "
                "bot explicit permission (user OAuth). It is a one-time "
                "consent, done from this chat.",
                "**To enable it:** send `/setup-files` and follow the steps.",
            ])
        # A path under the system temp directory may be a copy this pod staged
        # out of the shell sandbox, in which case it is deleted as soon as the
        # delivery returns and naming it here describes nothing the user can
        # act on. `original_path` gives back the path the agent actually wrote,
        # and is the identity for every other path. The import is guarded
        # because this patch also has to hold on an image built without the
        # agent scripts on PYTHONPATH.
        try:
            import sandbox_artifact_patch as _sandbox_artifacts

            reported = _sandbox_artifacts.original_path(path)
        except Exception:
            reported = path
        lines.append(f"The file is on the agent host at: `{reported}`")
'''


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="google_chat_attachment_notice")
    patch.refuse_if_patched(BUILD_MARKER)
    patch.substitute(ANCHOR, PATCHED, label="attachment-fallback notice")
    patch.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))

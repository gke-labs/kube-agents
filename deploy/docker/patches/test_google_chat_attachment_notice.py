"""Unit tests for the Google Chat attachment-notice patch applied by the Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

``UPSTREAM`` below carries ``_post_attachment_fallback`` copied verbatim out of
``plugins/platforms/google_chat/adapter.py`` in the pinned base image, wrapped in
the smallest class and preamble that will run it. The anchor is exercised against
the text it was derived from rather than against a paraphrase, and the patched
result is then *run*, because the defect is in what the user reads: an applier
whose anchor matched and which then emitted Spanish, or English pointing at a
command that does nothing here, would be no use.

The two branches are the substance. ``/setup-files`` is real guidance on an
install that talks to Google Chat directly and a dead end on one that goes
through the credential proxy, so which branch says it is the assertion that
matters most, and it is the one a grep in the Dockerfile cannot make.
"""

import asyncio
import importlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from apply_google_chat_attachment_notice import (
    BUILD_MARKER,
    PATCHED,
    RELAY_FLAG,
    RELATIVE,
    apply,
)

# deploy/docker/patches/ -> deploy/docker/ -> deploy/ -> repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "agents/platform/scripts"
RELAY_PATCH = SCRIPTS / "google_chat_relay_patch.py"
# What the patched notice imports to translate a staged copy back to the path
# the agent wrote. Named here so the block below can take it away again.
STAGING_MODULE = "sandbox_artifact_patch"


class _ImportBlocker:
    """A ``sys.meta_path`` finder that makes one module un-importable."""

    def __init__(self, name):
        self.name = name

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.name:
            raise ImportError(f"no module named {fullname!r} on this image")
        return None

# Verbatim from plugins/platforms/google_chat/adapter.py in the pinned base
# image, from ``async def`` to the closing paren of its ``return``. Everything
# above the class is scaffolding: the real module reaches this method with
# ``SendResult`` imported from gateway and a module logger already configured,
# neither of which is worth dragging a Hermes tree in for.
UPSTREAM = '''\
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SendResult:
    """Stand-in for gateway's dataclass of the same name.

    Hand-rolled rather than ``@dataclass`` because the fixture is exec'd from a
    spec that is never put in ``sys.modules``, and ``dataclass`` resolves
    annotations through ``sys.modules[cls.__module__]``.
    """

    def __init__(self, success: bool, error: Optional[str] = None) -> None:
        self.success = success
        self.error = error


class GoogleChatAdapter:
    async def _post_attachment_fallback(
        self,
        chat_id: str,
        path: str,
        filename: str,
        caption: Optional[str],
        thread_id: Optional[str],
    ) -> SendResult:
        """Post a text notice when native attachment delivery is unavailable.

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
            "Google Chat s\xf3lo permite adjuntar archivos cuando el bot tiene "
            "permiso expl\xedcito tuyo (OAuth de usuario). Es un consentimiento "
            "\xfanico que se hace desde este chat.",
            "**Para activarlo:** env\xeda `/setup-files` y sigue las instrucciones.",
            f"Mientras tanto el archivo est\xe1 en el host: `{path}`",
        ])
        body: Dict[str, Any] = {"text": "\\n".join(lines)}
        if thread_id:
            body["thread"] = {"name": thread_id}
        try:
            await self._create_message(chat_id, body)
        except Exception:
            logger.debug(
                "[GoogleChat] attachment fallback notice send failed",
                exc_info=True,
            )
        return SendResult(
            success=False,
            error="google_chat: native attachment requires user OAuth — "
            "run /setup-files in chat",
        )
'''

# The report from card t_e5e1ba5e, whose notice was the reported message.
FILENAME = "top-issue-deepdive.md"
PATH = f"/opt/data/kanban/attachments/t_e5e1ba5e/{FILENAME}"

# Every sentence upstream shipped. Asserted absent individually rather than as
# one blob so a partial revert names which line came back.
SPANISH = [
    "No he podido adjuntar",
    "Google Chat s\xf3lo permite adjuntar archivos",
    "**Para activarlo:**",
    "Mientras tanto el archivo",
]

# Log-facing, and deliberately untouched by the patch — see "What is
# deliberately not changed" in the applier's module docstring.
UPSTREAM_ERROR = (
    "google_chat: native attachment requires user OAuth — "
    "run /setup-files in chat"
)


def build(source=UPSTREAM):
    """Materialise a fake Hermes tree containing ``source``."""
    root = Path(tempfile.mkdtemp())
    path = root / RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return root, path


def load(path, name):
    """Import the fixture so its behaviour can be asserted on."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def send(cls, *, caption=None, thread_id=None, filename=FILENAME, raises=False,
         path=PATH):
    """Run the fallback on a bare instance; return ``(body_or_None, result)``.

    ``object.__new__`` skips ``__init__``: the method touches nothing on the
    instance but ``_create_message``, and a real adapter wants a config, a
    Pub/Sub subscription and the Google SDK. ``raises`` makes the send fail, to
    reach the swallow-and-still-return path.
    """
    adapter = object.__new__(cls)
    sent = []

    async def create_message(chat_id, body):
        sent.append((chat_id, body))
        if raises:
            raise RuntimeError("Chat API said no")

    adapter._create_message = create_message
    result = asyncio.run(
        cls._post_attachment_fallback(
            adapter, "spaces/AAAA1234", path, filename, caption, thread_id
        )
    )
    return (sent[0] if sent else None), result


class UpstreamBugTest(unittest.TestCase):
    """Pin the behaviour being fixed, so the patch is not asserted into a vacuum."""

    def setUp(self):
        _, path = build()
        self.cls = load(path, "gc_adapter_upstream").GoogleChatAdapter

    def test_upstream_answers_in_spanish(self):
        """The message the user reported, reproduced from the shipped source."""
        (_, body), _ = send(self.cls)
        self.assertEqual(
            body["text"],
            f"⚠️ No he podido adjuntar **{FILENAME}**.\n"
            "Google Chat s\xf3lo permite adjuntar archivos cuando el bot tiene "
            "permiso expl\xedcito tuyo (OAuth de usuario). Es un consentimiento "
            "\xfanico que se hace desde este chat.\n"
            "**Para activarlo:** env\xeda `/setup-files` y sigue las instrucciones.\n"
            f"Mientras tanto el archivo est\xe1 en el host: `{PATH}`",
        )

    def test_upstream_offers_setup_files_unconditionally(self):
        """The second defect: no branch, so the relay install gets it too."""
        relayed = type("Relayed", (self.cls,), {RELAY_FLAG: True})
        (_, body), _ = send(relayed)
        self.assertIn("/setup-files", body["text"])


class PatchedFixture:
    """Patch a fresh fixture per test and run real sends through the result."""

    def setUp(self):
        self.root, self.path = build()
        apply(self.root)
        self.mod = load(self.path, f"gc_adapter_{type(self).__name__}")
        self.cls = self.mod.GoogleChatAdapter
        # What the credential-proxy relay patch does to the class it installs
        # into: agents/platform/scripts/google_chat_relay_patch.py sets this
        # flag next to the /setup-files stub that makes the branch necessary.
        self.relayed_cls = type(
            "RelayedGoogleChatAdapter",
            (self.cls,),
            {RELAY_FLAG: True},
        )

    def text(self, cls=None, **kwargs):
        (_, body), _ = send(cls or self.cls, **kwargs)
        return body["text"]


class LanguageTest(PatchedFixture, unittest.TestCase):
    """The reported defect: the notice is English, in every branch."""

    def test_no_spanish_survives_in_the_source(self):
        source = self.path.read_text(encoding="utf-8")
        for fragment in SPANISH:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)

    def test_no_spanish_survives_in_either_rendered_notice(self):
        for cls in (self.cls, self.relayed_cls):
            for fragment in SPANISH:
                with self.subTest(cls=cls.__name__, fragment=fragment):
                    self.assertNotIn(fragment, self.text(cls))

    def test_the_notice_is_ascii_apart_from_the_warning_sign(self):
        """Catches a reintroduction in any language, not just the one seen.

        Rendering with an ASCII filename and path is what makes this test the
        whole notice rather than the caller's arguments — real ones can carry
        anything.
        """
        for cls in (self.cls, self.relayed_cls):
            with self.subTest(cls=cls.__name__):
                rendered = self.text(cls, filename="report.md")
                self.assertTrue(
                    rendered.replace("⚠️", "").isascii(), rendered
                )


class BranchTest(PatchedFixture, unittest.TestCase):
    """Which install is told to run ``/setup-files``, and which is told why not."""

    def test_a_direct_install_keeps_the_setup_files_step(self):
        """Asserted positively: fixing the language by deleting the guidance
        would leave a user with a failed upload and nothing to do about it."""
        rendered = self.text()
        self.assertIn("/setup-files", rendered)
        self.assertIn("OAuth", rendered)

    def test_a_relay_install_does_not_offer_setup_files(self):
        """The relay stubs the command out, so the instruction is a dead end."""
        rendered = self.text(self.relayed_cls)
        self.assertNotIn("/setup-files", rendered)
        self.assertIn("credential proxy", rendered)

    def test_a_relay_install_is_told_why_rather_than_offered_a_dead_end(self):
        """Upstream's notice always ended with an action, and this branch took
        the only one it had away.

        For a while what replaced it was an offer to paste the contents in
        chat. Since #999 the relay patch does that automatically, so this
        notice is only reached when pasting was already tried and declined --
        a PNG, a file over the 32 KiB ceiling, bytes that do not decode, or a
        paste the API refused partway through. An offer to paste is then an
        invitation the agent cannot honour, so the line gives the reason
        instead, and the host path below it stays as the one thing the reader
        can still act on.
        """
        rendered = self.text(self.relayed_cls)
        self.assertNotIn("Ask me to paste", rendered)
        self.assertIn("could not be pasted", rendered)
        self.assertIn("on the agent host at", rendered)

    def test_a_relay_install_is_told_a_failed_paste_is_one_of_the_reasons(self):
        """The refused-paste path reaches this notice too, and a reader who
        retries on it would likely succeed.

        ``google_chat_relay_patch`` routes both a ``send`` that refuses and a
        ``send`` that raises back here, and on that path the file is a
        perfectly pasteable text file: none of "no text form", "too large" or
        "could not be read" is true of it. A 429 at part 2 of a valid 10 KiB
        ``.md`` would otherwise leave the reader three false reasons and the
        impression that asking again is pointless.
        """
        rendered = self.text(self.relayed_cls)
        self.assertIn("failed partway through", rendered)

    def test_the_flag_is_honoured_when_set_on_the_class_itself(self):
        """How the relay patch actually sets it — on the class it patched.

        The subclass used everywhere else in this suite would also pass under a
        ``self.__dict__`` lookup, so without this the real call path is untested.
        """
        setattr(self.cls, RELAY_FLAG, True)
        self.addCleanup(delattr, self.cls, RELAY_FLAG)
        self.assertNotIn("/setup-files", self.text())

    def test_an_absent_flag_reads_as_a_direct_install(self):
        """The attribute does not exist upstream; a bare ``getattr`` would raise
        and turn a cosmetic patch into a failed send."""
        self.assertFalse(hasattr(self.cls, RELAY_FLAG))
        self.assertIn("/setup-files", self.text())


class NoticeContentTest(PatchedFixture, unittest.TestCase):
    """What the notice has to carry however it is worded."""

    def test_both_branches_name_the_file_and_where_it_ended_up(self):
        """The host path is the only copy of the file that exists.

        A notice that reports the failure without it loses a report the agent
        spent a turn producing.
        """
        for cls in (self.cls, self.relayed_cls):
            with self.subTest(cls=cls.__name__):
                rendered = self.text(cls)
                self.assertIn(FILENAME, rendered)
                self.assertIn(PATH, rendered)

    def test_the_caption_leads(self):
        """``send_file`` passes the message the agent wrote alongside the file."""
        rendered = self.text(caption="Here is the deep dive you asked for.")
        self.assertTrue(
            rendered.startswith("Here is the deep dive you asked for.\n"), rendered
        )

    def test_no_caption_means_no_leading_blank_line(self):
        self.assertTrue(self.text().startswith("⚠️"))

    def test_the_reply_stays_in_the_thread_it_was_asked_in(self):
        (_, body), _ = send(self.cls, thread_id="spaces/AAAA1234/threads/xyz")
        self.assertEqual(body["thread"], {"name": "spaces/AAAA1234/threads/xyz"})

    def test_no_thread_id_sends_no_thread_key(self):
        (_, body), _ = send(self.cls)
        self.assertNotIn("thread", body)

    def test_the_notice_goes_to_the_space_it_came_from(self):
        (chat_id, _), _ = send(self.cls)
        self.assertEqual(chat_id, "spaces/AAAA1234")


class StagedPathTest(PatchedFixture, unittest.TestCase):
    """The notice names the agent's own path, not the copy staged out of the sandbox.

    With the shell sandbox on, the path this method is handed is a copy that
    ``sandbox_artifact_patch`` made under the system temp directory and deletes
    as soon as the delivery returns. This branch is the one line of the product
    whose whole job is telling the user where the file is, so a stale temp path
    here is the one wrong path guaranteed to be read.
    """

    def setUp(self):
        super().setUp()
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
            self.addCleanup(sys.path.remove, str(SCRIPTS))
        import sandbox_artifact_patch

        self.staging = sandbox_artifact_patch

    def test_a_staged_copy_is_reported_as_the_path_the_agent_wrote(self):
        staged = f"/tmp/{self.staging.STAGED_DIR_PREFIX}abc123/0/{FILENAME}"
        self.staging._ORIGINALS[staged] = PATH
        self.addCleanup(self.staging._ORIGINALS.pop, staged, None)
        for cls in (self.cls, self.relayed_cls):
            with self.subTest(cls=cls.__name__):
                rendered = self.text(cls, path=staged)
                self.assertIn(PATH, rendered)
                self.assertNotIn(staged, rendered)

    def test_a_path_that_was_never_staged_is_named_as_it_is(self):
        """No sandbox, or an artifact the gateway itself wrote: unchanged."""
        for cls in (self.cls, self.relayed_cls):
            with self.subTest(cls=cls.__name__):
                self.assertIn(PATH, self.text(cls, path=PATH))


class NoStagingModuleTest(PatchedFixture, unittest.TestCase):
    """The lookup is best-effort: an image without the agent scripts still posts.

    ``apply_google_chat_attachment_notice`` runs at build time against the
    stock upstream image, where ``/opt/defaults/scripts`` is not on the path at
    all. An unguarded import there would turn every failed attachment into a
    traceback instead of a notice.
    """

    def test_the_path_is_named_when_the_staging_module_cannot_be_imported(self):
        saved = sys.modules.pop(STAGING_MODULE, None)
        if saved is not None:
            self.addCleanup(sys.modules.__setitem__, STAGING_MODULE, saved)
        blocker = _ImportBlocker(STAGING_MODULE)
        sys.meta_path.insert(0, blocker)
        self.addCleanup(sys.meta_path.remove, blocker)
        # Without this the test passes on an import that quietly succeeded.
        with self.assertRaises(ImportError):
            importlib.import_module(STAGING_MODULE)
        self.assertIn(PATH, self.text())


class ContractTest(PatchedFixture, unittest.TestCase):
    """What callers see. Unchanged by the patch, and worth proving unchanged."""

    def test_the_result_still_reports_failure(self):
        """``success=True`` here would tell the notifier the file was delivered."""
        for cls in (self.cls, self.relayed_cls):
            with self.subTest(cls=cls.__name__):
                _, result = send(cls)
                self.assertFalse(result.success)

    def test_the_log_facing_error_string_is_untouched(self):
        """It is English already and no user reads it; whoever greps gateway.log
        for a failed upload wants the same lead they had before."""
        _, result = send(self.cls)
        self.assertEqual(result.error, UPSTREAM_ERROR)

    def test_a_failed_send_is_still_swallowed(self):
        """The notice is best-effort: if Chat rejects it too, the caller still
        gets its SendResult rather than an exception out of an error path."""
        _, result = send(self.cls, raises=True)
        self.assertFalse(result.success)
        self.assertEqual(result.error, UPSTREAM_ERROR)


class ApplierTest(PatchedFixture, unittest.TestCase):
    """The applier's own guarantees."""

    def test_the_build_marker_is_present(self):
        self.assertIn(BUILD_MARKER, self.path.read_text(encoding="utf-8"))

    def test_a_second_run_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            apply(self.root)
        self.assertIn("already patched", str(caught.exception))


class RelayFlagContractTest(unittest.TestCase):
    """Pin the branch to the attribute the relay patch actually sets.

    ``_credential_proxy_relay_patched`` is private to
    ``agents/platform/scripts/google_chat_relay_patch.py`` -- its re-entrancy
    latch, not an interface it publishes. Renaming it there, to match
    ``slack_relay_patch.py``'s ``_slack_credential_proxy_relay_patched`` say,
    would leave every other test in this file green while the relay install
    silently went back to being told to run a command that has been stubbed
    out. So read the assignment out of that file rather than restating it.
    ``verify_slack_relay_registry_contract.py`` pins its own sentinels the same
    way.
    """

    def setUp(self):
        if not RELAY_PATCH.is_file():
            self.fail(f"{RELAY_PATCH} is gone; the notice's branch has no source")
        self.source = RELAY_PATCH.read_text(encoding="utf-8")

    def test_the_relay_patch_sets_the_flag_this_patch_reads(self):
        self.assertIn(f"adapter_class.{RELAY_FLAG} = True", self.source)

    def test_the_relay_patch_still_stubs_setup_files(self):
        """Why the branch exists. If the stub goes, so should the branch."""
        self.assertIn("adapter_class._handle_setup_files_command = ", self.source)

    def test_the_patched_notice_reads_that_same_flag(self):
        """``PATCHED`` is a source literal, so nothing else ties the two."""
        self.assertIn(f'"{RELAY_FLAG}"', PATCHED)


class DriftTest(unittest.TestCase):
    def test_a_moved_anchor_fails_the_build(self):
        """An anchor that stops matching must stop the image, not be skipped.

        Rewording the docstring is the likeliest upstream drift, and the one
        that would otherwise ship Spanish again with a green build.
        """
        moved = UPSTREAM.replace(
            "        Tells the user that file delivery requires a one-time consent\n",
            "        Tells the user that file delivery needs a one-time consent\n",
        )
        self.assertNotEqual(moved, UPSTREAM)
        root, _ = build(moved)
        with self.assertRaises(SystemExit) as caught:
            apply(root)
        self.assertIn("found 0", str(caught.exception))

    def test_an_upstream_translation_fails_the_build(self):
        """If upstream fixes this themselves the anchor stops matching, and the
        build says so instead of the patch quietly re-Spanishing their English."""
        translated = UPSTREAM.replace(
            "No he podido adjuntar", "Could not attach"
        )
        root, _ = build(translated)
        with self.assertRaises(SystemExit) as caught:
            apply(root)
        self.assertIn("found 0", str(caught.exception))

    def test_a_missing_file_fails_the_build(self):
        with self.assertRaises(SystemExit):
            apply(Path(tempfile.mkdtemp()))


if __name__ == "__main__":
    unittest.main()

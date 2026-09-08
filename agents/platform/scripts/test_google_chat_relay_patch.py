"""The relay patch's inline delivery of files it cannot attach (#999).

The transport half of this patch is covered by
``tests/integration/test_seam_chat_ingress.py``, which drives the real closures
against the real credential proxy. This file covers the half that has no
network in it: what the adapter does with a deliverable on an install where
``media.upload`` is unreachable.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_gateway_stub() -> dict:
    """Stub ``gateway.platform_registry``, the one hermes module ``install()``
    imports. Returns the saved modules so a test can restore them."""
    registry_module = types.ModuleType("gateway.platform_registry")

    class PlatformRegistry:
        def create_adapter(self, name, *args, **kwargs):
            return None

    registry_module.PlatformRegistry = PlatformRegistry
    gateway_pkg = types.ModuleType("gateway")
    gateway_pkg.platform_registry = registry_module
    saved = {
        name: sys.modules.get(name)
        for name in ("gateway", "gateway.platform_registry")
    }
    sys.modules["gateway"] = gateway_pkg
    sys.modules["gateway.platform_registry"] = registry_module
    return saved


class FakeSendResult:
    """Stands in for ``gateway.platforms.base.SendResult``."""

    def __init__(self, success=True, error=None):
        self.success = success
        self.error = error
        self.message_id = "spaces/AAA/messages/m1" if success else None


def make_adapter_class(send_results=None):
    """A minimal adapter carrying only what the fallback override touches.

    Fresh per test: ``patch_adapter_class`` latches on the class it patched, so
    a shared class would keep the first test's closures.
    """

    class MinimalAdapter:
        def __init__(self):
            self.sent = []
            self.fallback_calls = []
            self._send_results = list(send_results or [])

        async def send(self, chat_id, content, metadata=None):
            self.sent.append((chat_id, content, metadata))
            if self._send_results:
                nxt = self._send_results.pop(0)
                # An exception in the queue stands for the shipped adapter's
                # `raise` branches -- a 429, or any status it has no case for.
                if isinstance(nxt, BaseException):
                    raise nxt
                return nxt
            return FakeSendResult()

        async def _post_attachment_fallback(
            self, chat_id, path, filename, caption, thread_id
        ):
            """The build-time-patched notice this override defers to."""
            self.fallback_calls.append(
                {
                    "chat_id": chat_id,
                    "path": path,
                    "filename": filename,
                    "caption": caption,
                    "thread_id": thread_id,
                }
            )
            return FakeSendResult(success=False, error="not attached")

    return MinimalAdapter


class InlineHelpersTest(unittest.TestCase):
    """``_inline_text`` / ``_inline_chunks``, with no adapter in the way."""

    def setUp(self):
        self.saved = _install_gateway_stub()
        self.addCleanup(self._restore)
        import google_chat_relay_patch

        self.patch = google_chat_relay_patch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _write(self, name, data):
        path = Path(self.tmp.name) / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return str(path)

    def test_reads_a_text_deliverable(self):
        path = self._write("report.md", "# Title\n\nBody.\n")
        deliverable = self.patch._inline_text(path)
        self.assertEqual(deliverable.text, "# Title\n\nBody.\n")
        self.assertEqual(deliverable.suffix, ".md")
        self.assertEqual(deliverable.size, len("# Title\n\nBody.\n"))

    def test_reports_the_byte_count_not_the_character_count(self):
        # The header renders a file size, and a multi-byte character makes the
        # two differ. Reading it off the decoded text understated a UTF-8 report.
        body = "café\n" * 10
        path = self._write("report.md", body)
        self.assertEqual(self.patch._inline_text(path).size, len(body.encode()))

    def test_declines_a_binary_extension(self):
        # The bytes are valid UTF-8; the extension alone must decide, because a
        # .pdf that happens to decode is still not a thing to paste in a thread.
        path = self._write("report.pdf", "not really a pdf")
        self.assertIsNone(self.patch._inline_text(path))

    def test_declines_over_the_cap(self):
        oversize = "x" * (self.patch.INLINE_MAX_BYTES + 1)
        self.assertIsNone(self.patch._inline_text(self._write("big.md", oversize)))

    def test_accepts_exactly_the_cap(self):
        atlimit = "x" * self.patch.INLINE_MAX_BYTES
        self.assertEqual(
            self.patch._inline_text(self._write("atlimit.md", atlimit)).text,
            atlimit,
        )

    def test_declines_bytes_that_are_not_utf8(self):
        path = self._write("report.md", b"\xff\xfe\x00binary")
        self.assertIsNone(self.patch._inline_text(path))

    def test_declines_a_missing_file(self):
        missing = os.path.join(self.tmp.name, "gone.md")
        self.assertIsNone(self.patch._inline_text(missing))

    def test_chunks_stay_under_the_send_budget(self):
        text = "\n".join(f"line {n}" for n in range(4000))
        chunks = self.patch._inline_chunks(text, fenced=True)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            # The adapter's own cap. A chunk at or over it would be re-split by
            # send() and the fence would be cut in half.
            self.assertLess(len(chunk), self.patch.MESSAGE_CHAR_CAP)

    def test_the_header_reserve_covers_the_longest_header_it_can_render(self):
        # HEADER_RESERVE_CHARS is what makes the payload budget sound, and it is
        # a number rather than a derivation, so pin it against the widest header
        # _inline_header can actually produce: the longest displayable filename,
        # a three-figure size and a two-figure part count.
        widest = self.patch._inline_header(
            "n" * self.patch.FILENAME_DISPLAY_MAX,
            size="999.9 KB",
            index=98,
            total=99,
        )
        self.assertLessEqual(
            len(widest) + len("\n\n"), self.patch.HEADER_RESERVE_CHARS
        )

    def test_a_long_filename_is_truncated_at_both_ends(self):
        name = "a" * 100 + "-audit.md"
        shown = self.patch._display_filename(name)
        self.assertLessEqual(len(shown), self.patch.FILENAME_DISPLAY_MAX)
        self.assertTrue(shown.startswith("aaa"))
        self.assertTrue(shown.endswith("-audit.md"), "the extension must survive")

    def test_every_fenced_chunk_carries_its_own_fence(self):
        text = "\n".join(f"line {n}" for n in range(4000))
        chunks = self.patch._inline_chunks(text, fenced=True)
        for chunk in chunks:
            self.assertTrue(chunk.startswith("```\n"))
            self.assertTrue(chunk.endswith("\n```"))

    def test_unfenced_chunks_reassemble_to_the_source(self):
        text = "\n".join(f"line {n}" for n in range(4000))
        chunks = self.patch._inline_chunks(text, fenced=False)
        self.assertEqual("\n".join(chunks), text)

    def test_splits_a_line_only_when_it_has_to(self):
        # No newline anywhere: an ugly cut is correct, losing the tail is not.
        text = "x" * (self.patch._payload_budget(fenced=False) * 2 + 5)
        chunks = self.patch._inline_chunks(text, fenced=False)
        self.assertEqual("".join(chunks), text)


class InlineFallbackTest(unittest.TestCase):
    """The override installed on the adapter class by ``patch_adapter_class``."""

    def setUp(self):
        self.saved = _install_gateway_stub()
        self.addCleanup(self._restore)
        os.environ["GOOGLE_CHAT_RELAY_URL"] = "http://127.0.0.1:1"
        self.addCleanup(os.environ.pop, "GOOGLE_CHAT_RELAY_URL", None)
        sys.modules.pop("google_chat_relay_patch", None)
        import google_chat_relay_patch

        self.patch = google_chat_relay_patch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _patched_adapter(self, send_results=None):
        """An adapter instance whose class has been through the relay patch.

        Goes through ``install()`` and the registry wrapper rather than calling
        ``patch_adapter_class`` directly, so the test exercises the path
        production takes.
        """
        adapter_class = make_adapter_class(send_results)
        registry = sys.modules["gateway.platform_registry"].PlatformRegistry
        registry.create_adapter = (
            lambda self, name, *a, **k: adapter_class() if name == "google_chat" else None
        )
        self.patch.install()
        adapter = registry().create_adapter("google_chat")
        self.assertTrue(
            getattr(adapter_class, "_credential_proxy_relay_patched", False)
        )
        return adapter

    def _write(self, name, data):
        path = Path(self.tmp.name) / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return str(path)

    def _fallback(self, adapter, path, filename, caption=None, thread_id=None):
        return asyncio.run(
            type(adapter)._post_attachment_fallback(
                adapter,
                chat_id="spaces/AAA",
                path=path,
                filename=filename,
                caption=caption,
                thread_id=thread_id,
            )
        )

    def test_pastes_a_markdown_report_instead_of_the_notice(self):
        adapter = self._patched_adapter()
        path = self._write("assessment.md", "# Design Assessment\n\nThe topology.\n")

        result = self._fallback(adapter, path, "assessment.md")

        self.assertTrue(result.success, "an inlined report is a delivered report")
        self.assertEqual(adapter.fallback_calls, [], "the notice must not also post")
        self.assertEqual(len(adapter.sent), 1)
        _chat_id, content, _metadata = adapter.sent[0]
        self.assertIn("assessment.md", content)
        self.assertIn("# Design Assessment", content)
        self.assertIn("The topology.", content)

    def test_names_the_file_and_its_size(self):
        adapter = self._patched_adapter()
        path = self._write("report.md", "x" * 2048)

        self._fallback(adapter, path, "report.md")

        content = adapter.sent[0][1]
        self.assertIn("**report.md**", content)
        self.assertIn("2.0 KB", content)

    def test_threads_the_paste_under_the_summary(self):
        adapter = self._patched_adapter()
        path = self._write("report.md", "body\n")

        self._fallback(adapter, path, "report.md", thread_id="spaces/AAA/threads/T")

        self.assertEqual(
            adapter.sent[0][2], {"thread_id": "spaces/AAA/threads/T"}
        )

    def test_leads_with_the_caption_when_there_is_one(self):
        adapter = self._patched_adapter()
        path = self._write("report.md", "body\n")

        self._fallback(adapter, path, "report.md", caption="Here is the report")

        self.assertTrue(adapter.sent[0][1].startswith("Here is the report"))

    def test_defers_to_the_notice_for_a_binary(self):
        adapter = self._patched_adapter()
        path = self._write("chart.png", b"\x89PNG\r\n\x1a\n")

        result = self._fallback(adapter, path, "chart.png")

        self.assertFalse(result.success)
        self.assertEqual(adapter.sent, [], "nothing is pasted for a binary")
        self.assertEqual(len(adapter.fallback_calls), 1)
        self.assertEqual(adapter.fallback_calls[0]["filename"], "chart.png")

    def test_defers_to_the_notice_over_the_cap(self):
        adapter = self._patched_adapter()
        path = self._write("huge.md", "x" * (self.patch.INLINE_MAX_BYTES + 1))

        result = self._fallback(adapter, path, "huge.md")

        self.assertFalse(result.success)
        self.assertEqual(len(adapter.fallback_calls), 1)

    def test_defers_to_the_notice_for_an_empty_file(self):
        # Nothing to read is not a delivery, and a message holding only a
        # filename header is worse than the notice that says where the file is.
        adapter = self._patched_adapter()
        path = self._write("empty.md", "   \n")

        result = self._fallback(adapter, path, "empty.md")

        self.assertFalse(result.success)
        self.assertEqual(len(adapter.fallback_calls), 1)

    def test_fences_structured_content(self):
        adapter = self._patched_adapter()
        path = self._write("findings.json", '{"a": 1}\n')

        self._fallback(adapter, path, "findings.json")

        self.assertIn('```\n{"a": 1}', adapter.sent[0][1])

    def test_marks_the_parts_of_a_multi_message_report(self):
        # ~8.9 KB: several chunks, but comfortably under the cap, so this
        # exercises chunking rather than the refusal path above it.
        adapter = self._patched_adapter()
        path = self._write("long.md", "\n".join(f"line {n}" for n in range(1000)))

        result = self._fallback(adapter, path, "long.md")

        self.assertTrue(result.success)
        self.assertGreater(len(adapter.sent), 1)
        self.assertIn("2 of ", adapter.sent[1][1])

    def test_an_adapter_without_the_hook_still_gets_the_transport(self):
        # A base image that renames _post_attachment_fallback must cost the
        # inlining and nothing else. Losing connect() here would lose Chat.
        adapter_class = make_adapter_class()
        del adapter_class._post_attachment_fallback
        registry = sys.modules["gateway.platform_registry"].PlatformRegistry
        registry.create_adapter = lambda self, name, *a, **k: adapter_class()

        with self.assertLogs("google-chat-relay-patch", level="WARNING"):
            self.patch.install()
            registry().create_adapter("google_chat")

        self.assertTrue(adapter_class._credential_proxy_relay_patched)
        self.assertTrue(callable(adapter_class.connect))
        self.assertFalse(hasattr(adapter_class, "_post_attachment_fallback"))

    def test_stops_at_the_first_refusal(self):
        # Posting the tail of a report whose head was refused leaves the reader
        # with a fragment they cannot tell is a fragment.
        adapter = self._patched_adapter(
            send_results=[FakeSendResult(success=False, error="rate limited")]
        )
        path = self._write("long.md", "\n".join(f"line {n}" for n in range(1000)))

        with self.assertLogs("google-chat-relay-patch", level="WARNING") as logs:
            result = self._fallback(adapter, path, "long.md")

        self.assertFalse(result.success)
        self.assertEqual(len(adapter.sent), 1)
        self.assertIn("long.md", logs.output[0])
        self.assertIn("rate limited", logs.output[0])

    def test_the_notice_names_the_agents_path_not_the_staged_copy(self):
        # Under the shell sandbox the file being read here is a copy this pod
        # staged, and `sandbox_artifact_patch` deletes it as soon as the
        # delivery returns. A notice naming that temp path sends the reader
        # after something that no longer exists, on the one code path whose
        # whole job is to say where the file is.
        import sandbox_artifact_patch

        adapter = self._patched_adapter()
        staged = self._write("report.pdf", b"%PDF-1.4 binary")
        sandbox_artifact_patch._ORIGINALS[staged] = "/opt/data/report.pdf"
        self.addCleanup(sandbox_artifact_patch._ORIGINALS.pop, staged, None)

        self._fallback(adapter, staged, "report.pdf")

        self.assertEqual(len(adapter.fallback_calls), 1)
        self.assertEqual(adapter.fallback_calls[0]["path"], "/opt/data/report.pdf")

    def test_a_path_that_was_never_staged_is_named_as_it_is(self):
        adapter = self._patched_adapter()
        path = self._write("report.pdf", b"%PDF-1.4 binary")

        self._fallback(adapter, path, "report.pdf")

        self.assertEqual(adapter.fallback_calls[0]["path"], path)

    def test_a_refusal_still_leaves_the_notice_and_the_host_path(self):
        # Before inlining existed, every path through _post_attachment_fallback
        # posted the notice naming the host path. A paste that is refused must
        # not be the one case where the thread gets nothing at all -- that is
        # strictly worse than the bug this change fixes.
        adapter = self._patched_adapter(
            send_results=[FakeSendResult(success=False, error="rate limited")]
        )
        path = self._write("long.md", "\n".join(f"line {n}" for n in range(1000)))

        with self.assertLogs("google-chat-relay-patch", level="WARNING"):
            self._fallback(adapter, path, "long.md")

        self.assertEqual(
            len(adapter.fallback_calls), 1, "the notice is the last resort"
        )
        self.assertEqual(adapter.fallback_calls[0]["filename"], "long.md")

    def test_a_raising_send_still_leaves_the_notice_and_the_host_path(self):
        # The shipped `send` re-raises on a 429 and on any status it has no
        # branch for, and nothing downstream catches it: `_send_file` calls
        # this method outside its own try, and the notifier's artifact loop
        # only logs. Letting it out would leave the thread with nothing --
        # worse than the notice this deployment posts today, and something
        # upstream's fallback cannot do, since it swallows its one send.
        adapter = self._patched_adapter(send_results=[RuntimeError("429")])
        path = self._write("long.md", "\n".join(f"line {n}" for n in range(1000)))

        with self.assertLogs("google-chat-relay-patch", level="WARNING"):
            result = self._fallback(adapter, path, "long.md")

        self.assertEqual(len(adapter.fallback_calls), 1, "the notice is owed")
        self.assertFalse(result.success)

    def test_a_send_returning_none_is_treated_as_a_refusal(self):
        # The guard used to read `result is not None and not result.success`,
        # so a None counted as delivered: the loop kept posting and the method
        # handed None back to _send_file, whose caller reads .success off it.
        adapter = self._patched_adapter(send_results=[None])
        path = self._write("long.md", "\n".join(f"line {n}" for n in range(1000)))

        with self.assertLogs("google-chat-relay-patch", level="WARNING"):
            result = self._fallback(adapter, path, "long.md")

        self.assertIsNotNone(result)
        self.assertEqual(len(adapter.sent), 1, "it must not keep posting")
        self.assertEqual(len(adapter.fallback_calls), 1)

    def test_the_first_message_says_which_part_it_is(self):
        # Marking only parts 2..N means a report whose second message is refused
        # leaves a thread that reads as a complete, short report.
        adapter = self._patched_adapter()
        path = self._write("long.md", "\n".join(f"line {n}" for n in range(1000)))

        self._fallback(adapter, path, "long.md")

        self.assertGreater(len(adapter.sent), 1)
        self.assertIn(f"1 of {len(adapter.sent)}", adapter.sent[0][1])

    def test_a_single_message_report_carries_no_part_marker(self):
        adapter = self._patched_adapter()
        path = self._write("short.md", "body\n")

        self._fallback(adapter, path, "short.md")

        self.assertEqual(len(adapter.sent), 1)
        self.assertNotIn(" of ", adapter.sent[0][1])

    def test_no_message_exceeds_the_cap_however_long_the_caption(self):
        # The regression this whole budget exists for: the payload was budgeted
        # at 3500 and the caption, header and fence were added afterwards, so a
        # caption of ~455 characters pushed the first message past 4000 and
        # send() re-split it through the middle of a code fence.
        adapter = self._patched_adapter()
        path = self._write(
            "findings.json", "\n".join(f'{{"line": {n}}}' for n in range(2000))
        )
        caption = "Here is the deep dive you asked for. " * 14

        result = self._fallback(adapter, path, "findings.json", caption=caption)

        self.assertTrue(result.success)
        self.assertGreater(len(adapter.sent), 1)
        for _chat_id, content, _metadata in adapter.sent:
            self.assertLessEqual(len(content), self.patch.MESSAGE_CHAR_CAP)

    def test_an_oversized_caption_leads_in_its_own_message(self):
        adapter = self._patched_adapter()
        path = self._write("report.md", "\n".join(f"line {n}" for n in range(600)))
        caption = "Context. " * 500

        self._fallback(adapter, path, "report.md", caption=caption)

        self.assertEqual(adapter.sent[0][1], caption)
        self.assertNotIn(caption, adapter.sent[1][1])
        self.assertIn("**report.md**", adapter.sent[1][1])

    def test_a_short_caption_still_rides_with_the_first_chunk(self):
        # The separate-message path must not become the common case: a caption
        # and a small report belong in one message.
        adapter = self._patched_adapter()
        path = self._write("report.md", "body\n")

        self._fallback(adapter, path, "report.md", caption="Here is the report")

        self.assertEqual(len(adapter.sent), 1)
        self.assertTrue(adapter.sent[0][1].startswith("Here is the report"))

    def test_a_refused_caption_falls_back_to_the_notice(self):
        adapter = self._patched_adapter(
            send_results=[FakeSendResult(success=False, error="rejected")]
        )
        path = self._write("report.md", "\n".join(f"line {n}" for n in range(600)))

        with self.assertLogs("google-chat-relay-patch", level="WARNING"):
            self._fallback(adapter, path, "report.md", caption="Context. " * 500)

        self.assertEqual(len(adapter.sent), 1, "no report follows a refused caption")
        self.assertEqual(len(adapter.fallback_calls), 1)


if __name__ == "__main__":
    unittest.main()

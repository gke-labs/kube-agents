"""Credential-free Google Chat transport for Hermes' bundled adapter, and the
inline delivery of deliverables it cannot attach."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import urllib.request
from typing import Any, NamedTuple

import sandbox_artifact_patch
from credential_proxy_client import authorization_headers


LOGGER = logging.getLogger("google-chat-relay-patch")

# Below: what makes a deliverable inlineable when it cannot be attached.
#
# Google Chat's ``media.upload`` rejects app authentication outright -- "This
# method doesn't support app authentication with a service account" -- so an
# install that reaches Chat through the credential proxy can never upload a
# native attachment, whatever it is granted. Upstream's answer is a notice
# naming the host path, which on this deployment is a path the person in the
# thread cannot reach; #999 is that notice arriving instead of a report someone
# asked for. Posting the content as text needs no credential at all, so the
# deliverable goes into the thread that way instead. The bytes are readable from
# this process by the time the adapter is called: either the agent wrote them
# here, or ``sandbox_artifact_patch`` staged them out of the shell sandbox and
# rewrote the path -- which is why the notice below asks that module what the
# agent's own path was before naming one.

#: Extensions whose bytes are worth putting in a chat message. A deliverable
#: outside this set -- a PDF, a PNG, an archive -- has no text form, and the
#: notice upstream posts (patched to English and to this deployment's reality
#: by deploy/docker/patches/apply_google_chat_attachment_notice.py) is still the
#: right answer for it.
INLINE_SUFFIXES = frozenset(
    {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".log"}
)

#: Bytes above which a file is left as a notice rather than pasted. 32 KiB is
#: roughly ten messages at the payload budget below; past that the thread stops
#: being a place anyone reads the report and becomes a place it is buried.
INLINE_MAX_BYTES = 32 * 1024

#: The adapter's own per-message ceiling. Restated here because every budget
#: below is derived from it, and restating an upstream constant is how the
#: Slack shim broke: ``deploy/docker/patches/verify_slack_relay_registry_contract.py``
#: parses the real ``_MAX_TEXT_LENGTH`` out of the shipped adapter at
#: image-build time and fails the build if this copy has drifted, the way
#: ``verify_kanban_progress_lines.py`` guards ``kanban_progress_lines.MAX_RENDER``.
MESSAGE_CHAR_CAP = 4000

#: Longest filename rendered into a header before it is middle-truncated. The
#: header reserve below is only sound if the name inside it is bounded, and a
#: deliverable named by an agent has no length limit of its own.
FILENAME_DISPLAY_MAX = 80

#: Characters reserved in every message for the decoration wrapped around the
#: payload: a ``📄 **<name>** (31.9 KB · 10 of 10)`` header at the longest
#: filename above, and the blank line under it. Generous on purpose -- the
#: budget it leaves is spent on one fewer line of report per message, whereas
#: the other direction cuts a fence in half.
HEADER_RESERVE_CHARS = 128

#: Characters a code fence adds: the opening ```` ```\n ```` and the closing
#: ```` \n``` ````.
FENCE_RESERVE_CHARS = 8

#: Bytes in a kibibyte, for the header's size rendering.
BYTES_PER_KIB = 1024

#: Extensions posted inside a code fence rather than as prose. Chat renders the
#: message body as markdown, which eats the underscores and asterisks in a JSON
#: blob or a log line; a markdown report, by contrast, is *meant* to be
#: rendered and reads worse fenced.
INLINE_FENCED_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".csv", ".log"})


def _payload_budget(*, fenced: bool) -> int:
    """Characters of report that fit in one message once decorated.

    Derived from ``MESSAGE_CHAR_CAP`` rather than written down beside it. The
    first version of this budgeted the payload at a flat 3500 and then added a
    header, a fence and a caption on top, so a caption of 455 characters was
    enough to push the first message past the adapter's cap -- at which point
    ``send`` re-split it, through the middle of the fence, which is the exact
    outcome the budget exists to prevent.
    """
    reserve = HEADER_RESERVE_CHARS + (FENCE_RESERVE_CHARS if fenced else 0)
    return MESSAGE_CHAR_CAP - reserve


def _human_size(size: int) -> str:
    """``9.6 KB``-style size for the header line."""
    if size < BYTES_PER_KIB:
        return f"{size} B"
    return f"{size / BYTES_PER_KIB:.1f} KB"


def _display_filename(filename: str) -> str:
    """``filename`` shortened to fit the header reserve, keeping both ends.

    The tail matters as much as the head -- it carries the extension, and the
    part number that distinguishes ``audit-1.md`` from ``audit-11.md``.
    """
    if len(filename) <= FILENAME_DISPLAY_MAX:
        return filename
    keep = FILENAME_DISPLAY_MAX - 1
    head = keep // 2
    return f"{filename[:head]}…{filename[-(keep - head):]}"


def _inline_header(filename: str, *, size: str, index: int, total: int) -> str:
    """The ``📄 **report.md** (9.6 KB · 2 of 5)`` line above a chunk.

    Every message carries the part marker when there is more than one part,
    including the first. Marking only parts 2..N means a report whose second
    message is refused leaves a thread holding a header and the opening 3800
    characters with nothing to say the rest is missing.
    """
    name = _display_filename(filename)
    if total == 1:
        return f"📄 **{name}** ({size})"
    if index == 0:
        return f"📄 **{name}** ({size} · 1 of {total})"
    return f"📄 **{name}** ({index + 1} of {total})"


class _Deliverable(NamedTuple):
    """A file that can be pasted, and the two facts the header needs about it."""

    text: str
    suffix: str
    size: int


def _inline_text(path: str) -> _Deliverable | None:
    """The file's text if it is small enough and textual, else ``None``.

    ``None`` is the "leave it to the notice" answer and covers every way this
    can decline: the wrong extension, too many bytes, bytes that are not UTF-8
    after all, and a file that is not readable from this process. A caller that
    got ``None`` has learned only that inlining is not available -- never that
    the file is absent, which is the notice's business to report.

    Returns the suffix and byte count alongside the text because it has both in
    hand. Returning the text alone made the caller re-split the path and
    re-encode the whole report just to render a size into a header.
    """
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in INLINE_SUFFIXES:
        return None
    try:
        with open(path, "rb") as handle:
            # One byte past the cap, so a file that grew between a stat and
            # this read is refused rather than silently truncated into chat.
            raw = handle.read(INLINE_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > INLINE_MAX_BYTES:
        return None
    try:
        return _Deliverable(raw.decode("utf-8"), suffix, len(raw))
    except UnicodeDecodeError:
        return None


def _agent_side_path(path: str) -> str:
    """The path to name in a notice, which is not always the path being read.

    With the shell sandbox on, the agent's files live in another pod and
    ``sandbox_artifact_patch`` stages a copy here so the delivery can happen at
    all. Everything downstream reads the copy; the reader has to be told about
    the original, which is the only one that will still exist by the time they
    look. Off the sandbox, and for anything the gateway wrote itself, this is
    the identity.
    """
    return sandbox_artifact_patch.original_path(path)


def _inline_chunks(text: str, *, fenced: bool) -> list[str]:
    """Split ``text`` into message-sized pieces, fencing each one separately.

    Fencing per chunk rather than once around the whole report is the reason
    this does its own splitting instead of handing the text to ``send`` and
    letting the adapter chunk it: a fence opened in the first message and
    closed in the last leaves every message between them unfenced.

    Splits on a line boundary when there is one to split on, because a report
    cut mid-line reads as corrupted rather than as continued.
    """
    budget = _payload_budget(fenced=fenced)
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= budget:
            head, remaining = remaining, ""
        else:
            cut = remaining.rfind("\n", 0, budget)
            # No newline in the whole window: a minified blob or one very long
            # line. Cut at the budget -- an ugly break beats no delivery.
            if cut <= 0:
                cut = budget
            head, remaining = remaining[:cut], remaining[cut:].lstrip("\n")
        chunks.append(f"```\n{head}\n```" if fenced else head)
    return chunks


def install() -> None:
    relay_url = os.getenv("GOOGLE_CHAT_RELAY_URL", "").rstrip("/")
    if not relay_url:
        return

    def request(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        # The relay shares a listener with the credential broker, so it shares
        # the broker's authentication. Empty in the sidecar deployment.
        headers = {"Content-Type": "application/json", **authorization_headers()}
        req = urllib.request.Request(
            relay_url + path,
            data=body,
            headers=headers,
            method="GET" if body is None else "POST",
        )
        with urllib.request.urlopen(req, timeout=35) as response:
            return json.load(response)

    class RelayMessage:
        """Pub/Sub-shaped message that settles an opaque proxy receipt."""

        def __init__(self, event: dict[str, Any]) -> None:
            self.data = base64.b64decode(event["data"], validate=True)
            self.attributes = event.get("attributes") or {}
            self.message_id = str(event.get("messageId", ""))
            self._receipt = str(event["receipt"])
            self._settled = False

        def _settle(self, acknowledge: bool) -> None:
            if self._settled:
                return
            path = "/v1/chat/events/ack" if acknowledge else "/v1/chat/events/nack"
            request(path, {"receipt": self._receipt})
            self._settled = True

        def ack(self) -> None:
            self._settle(True)

        def nack(self) -> None:
            self._settle(False)

    class RemoteRequest:
        def __init__(
            self, resource: list[str], method: str, arguments: dict[str, Any]
        ) -> None:
            self.resource = resource
            self.method = method
            self.arguments = arguments

        def execute(self, **_kwargs: Any) -> Any:
            response = request(
                "/v1/chat/api",
                {
                    "resource": self.resource,
                    "method": self.method,
                    "arguments": self.arguments,
                },
            )
            return response.get("response")

    class RemoteResource:
        """googleapiclient discovery-resource-shaped remote facade."""

        def __init__(self, resource: list[str] | None = None) -> None:
            self.resource = resource or []

        def __getattr__(self, name: str) -> Any:
            if name.startswith("_"):
                raise AttributeError(name)

            def invoke(**arguments: Any) -> Any:
                if arguments:
                    return RemoteRequest(self.resource, name, arguments)
                return RemoteResource([*self.resource, name])

            return invoke

    async def relay_loop(self: Any) -> None:
        while not self._shutting_down:
            message: RelayMessage | None = None
            try:
                response = await asyncio.to_thread(request, "/v1/chat/events")
                event = response.get("event")
                if not event:
                    continue
                message = RelayMessage(event)
                await asyncio.to_thread(self._on_pubsub_message, message)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("Google Chat relay receive failed", exc_info=True)
                if message is not None:
                    try:
                        await asyncio.to_thread(message.nack)
                    except Exception:
                        pass
                await asyncio.sleep(2)

    def patch_adapter_class(adapter_class: type[Any]) -> None:
        if getattr(adapter_class, "_credential_proxy_relay_patched", False):
            return
        async def connect(self: Any, *, is_reconnect: bool = False) -> bool:
            self._loop = asyncio.get_running_loop()
            self._shutting_down = False
            self._chat_api = RemoteResource()
            try:
                await asyncio.to_thread(self._thread_count_store.load)
            except Exception:
                LOGGER.warning("Google Chat thread state load failed", exc_info=True)
            self._bot_user_id = self._load_cached_bot_id()
            self._relay_task = asyncio.create_task(relay_loop(self))
            self._mark_connected()
            LOGGER.info("Google Chat connected through credential proxy relay")
            return True

        async def disconnect(self: Any) -> None:
            self._shutting_down = True
            task = getattr(self, "_relay_task", None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._chat_api = None
            self._mark_disconnected()

        def new_authed_http(self: Any) -> Any:
            return None

        async def setup_files(
            self: Any,
            chat_id: str,
            thread_id: str | None,
            raw_text: str,
            sender_email: str | None = None,
        ) -> bool:
            await self.send(
                chat_id,
                "File attachment setup is unavailable through the credential proxy.",
                metadata={"thread_id": thread_id} if thread_id else None,
            )
            return True

        # Absent on a base image that renamed or dropped the hook. Skipping is
        # the whole cost of that -- ``_send_file`` would be calling some other
        # name, so the override could never fire anyway -- whereas letting the
        # AttributeError out of here aborts ``create_adapter`` and takes
        # ``connect`` and the relay loop down with it, losing Chat entirely
        # over a cosmetic feature.
        original_fallback = getattr(adapter_class, "_post_attachment_fallback", None)

        async def post_attachment_fallback(
            self: Any,
            chat_id: str,
            path: str,
            filename: str,
            caption: str | None,
            thread_id: str | None,
        ) -> Any:
            """Paste a deliverable that cannot be attached, or defer to upstream.

            Reached from both of ``_send_file``'s give-up paths -- no user
            OAuth token, and a token the API refused -- which on this
            deployment is every attempt, since the relay holds no user
            credentials and ``/setup-files`` is stubbed out above.

            Returns ``success=True`` when the content lands. The value is what
            the notifier logs, and by then the person in the thread is holding
            the report; calling that a failed delivery would be a worse lie
            than calling a pasted report an attachment.

            Falls back to ``original_fallback`` -- the English, relay-aware
            notice the image's build-time patch leaves here -- for anything
            with no text form, anything too large to read in a thread, any
            failure to read the bytes at all, and any refusal or error partway
            through posting. That last one matters most: the notice names the
            host path, and a paste that stopped halfway is exactly when the
            person in the thread needs the copy that did not make it.
            """

            async def notice() -> Any:
                return await original_fallback(
                    self,
                    chat_id=chat_id,
                    # The path the agent wrote, which under the shell sandbox is
                    # not the path being read here: sandbox_artifact_patch
                    # staged a copy into this pod and will delete it as soon as
                    # this delivery returns. Naming the copy would point the
                    # reader at a temp directory that no longer exists.
                    path=_agent_side_path(path),
                    filename=filename,
                    caption=caption,
                    thread_id=thread_id,
                )

            async def paste(caption: str | None) -> Any:
                """Post the deliverable, or hand back to ``notice``."""
                deliverable = _inline_text(path)
                if deliverable is None or not deliverable.text.strip():
                    return await notice()

                fenced = deliverable.suffix in INLINE_FENCED_SUFFIXES
                chunks = _inline_chunks(deliverable.text, fenced=fenced)
                metadata = {"thread_id": thread_id} if thread_id else None
                size = _human_size(deliverable.size)

                # A caption is agent-written prose of no bounded length, so it
                # cannot ride along in the first message on the strength of a
                # reserve. It leads on its own whenever the two together would
                # not fit -- which keeps the common case at one message and the
                # long case under the cap, rather than trading one for the
                # other.
                lead = _inline_header(
                    filename, size=size, index=0, total=len(chunks)
                )
                first = "\n\n".join([lead, chunks[0]])
                if (
                    caption
                    and len(caption) + len("\n\n") + len(first) > MESSAGE_CHAR_CAP
                ):
                    preamble = await self.send(chat_id, caption, metadata=metadata)
                    if not getattr(preamble, "success", False):
                        LOGGER.warning(
                            "Google Chat inline delivery of %s failed on the "
                            "caption: %s",
                            filename,
                            getattr(preamble, "error", ""),
                        )
                        return await notice()
                    caption = None

                result = None
                for index, chunk in enumerate(chunks):
                    header = [
                        _inline_header(
                            filename, size=size, index=index, total=len(chunks)
                        )
                    ]
                    if index == 0 and caption:
                        header.insert(0, caption)
                    result = await self.send(
                        chat_id, "\n\n".join([*header, chunk]), metadata=metadata
                    )
                    # Stop at the first refusal rather than posting the tail of
                    # a report whose head never arrived -- and hand back to the
                    # notice, so the thread is left with the host path instead
                    # of nothing at all. A ``None`` return counts as a refusal
                    # here: the shipped ``send`` returns a ``SendResult`` or
                    # raises, so a missing one is not a success anybody can
                    # read.
                    if not getattr(result, "success", False):
                        LOGGER.warning(
                            "Google Chat inline delivery of %s failed at part "
                            "%d/%d: %s",
                            filename,
                            index + 1,
                            len(chunks),
                            getattr(result, "error", ""),
                        )
                        return await notice()
                return result

            # ``send`` raises rather than returning on a 429 and on any status
            # it has no branch for, and nothing between here and the notifier
            # catches it: ``_send_file`` calls this method outside its own
            # try, and ``_deliver_kanban_artifacts`` logs the escape and moves
            # on. So an exception here is a thread that gets nothing at all --
            # not even the host path, which is what the same deployment posts
            # today. Upstream's fallback cannot do that: it swallows its one
            # send. Matching that is the whole of this handler.
            try:
                return await paste(caption)
            except Exception:
                LOGGER.warning(
                    "Google Chat inline delivery of %s raised; falling back "
                    "to the notice",
                    filename,
                    exc_info=True,
                )
                return await notice()

        adapter_class.connect = connect
        adapter_class.disconnect = disconnect
        adapter_class._new_authed_http = new_authed_http
        adapter_class._handle_setup_files_command = setup_files
        if original_fallback is not None:
            adapter_class._post_attachment_fallback = post_attachment_fallback
        else:
            LOGGER.warning(
                "Google Chat adapter has no _post_attachment_fallback; "
                "deliverables will not be inlined"
            )
        adapter_class._credential_proxy_relay_patched = True

    from gateway.platform_registry import PlatformRegistry

    original_registry_create = PlatformRegistry.create_adapter
    if not getattr(PlatformRegistry, "_credential_proxy_relay_patched", False):

        # Forwarded blind past ``name``: this wrapper adds a side effect and
        # delegates, so upstream owns the signature. Restating one is how the
        # Slack relay's registry shim took every platform down when the base
        # image grew a new keyword-only argument (see slack_relay_patch).
        def create_adapter(self: Any, name: str, *args: Any, **kwargs: Any) -> Any:
            adapter = original_registry_create(self, name, *args, **kwargs)
            if name == "google_chat" and adapter is not None:
                patch_adapter_class(type(adapter))
            return adapter

        PlatformRegistry.create_adapter = create_adapter
        PlatformRegistry._credential_proxy_relay_patched = True

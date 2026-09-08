"""Bring a deliverable into the gateway when the agent wrote it in the sandbox.

Once the shell sandbox is on, the agent's file tools run in
``platform-agent-shell-0`` and write to that pod's own volume. The kanban
notifier does not: ``GatewayKanbanWatchersMixin._deliver_kanban_artifacts``
runs in the gateway, and its first act is to screen every candidate path
through ``os.path.isfile``. In the gateway that call is ``False`` for every
file the agent produced, so ``candidates`` comes back empty and the function
returns before the adapter is involved at all.

Nothing reports that. The path is dropped by the branch whose comment reads
"the path may have been mentioned for reference only", so a completed card that
produced a report delivers no report, no upload error and no notice -- the
thread gets the agent's prose and nothing else. Every artifact type is affected,
not only the text ones: a PDF that would have drawn an "attachment unavailable"
notice now draws silence.

So this stages the file instead. Each declared artifact that exists on the far
side is copied into a temp directory here, and the local path is handed to the
original method, which then behaves exactly as it did before the sandbox: the
``isfile`` screen passes, ``BasePlatformAdapter.filter_local_delivery_paths``
accepts it, and the adapter gets a real file to upload -- or to fail to upload
and report, which on a credential-proxy install is the outcome that matters.

What this does not recover
--------------------------
Only the first of the original's three sources, ``event_payload['artifacts']``.
The other two hand a block of the model's prose to
``BasePlatformAdapter.extract_local_files``, which screens each path it matches
with its own ``os.path.isfile`` before returning it -- so those paths are gone
inside the adapter, before this patch could see them. Recovering them means
reimplementing that method's path regex and its code-span exclusions out here,
against a copy of the extension list, and then owning the drift. The explicit
list is what ``agents/platform/SOUL.md`` tells the agent to use for a
deliverable, and a path recovered from prose is dropped exactly as it is
dropped today.

Nor the artifact that never reaches a completion event at all. ``kanban_db.py``
validates the declared list at ``kanban_complete`` time, and for a path *under
the card's managed scratch workspace* it expands, resolves, containment-checks
and ``is_file()``-checks it in the gateway -- which fails under the sandbox and
raises ``ArtifactPreservationError`` at the model rather than returning an
event. That failure is loud, the model sees it, and it happens before anything
here runs. A path outside the managed workspace -- ``/opt/data/INVENTORY.md``,
which is what ``SOUL.md`` tells a worker to declare -- takes the pass-through
branch instead, with no ``is_file()`` check, and is the case this patch fixes.

Nor an install running in strict media-delivery mode. Upstream's validator then
demands the file sit under an allowlisted root or inside a recency window, and
neither test can be evaluated for a path in another pod; staging would launder
the path past a check the operator turned on deliberately. ``_screen`` stages
nothing in that mode and says so in the log.

Why the path is rewritten rather than the file being recreated where the agent
put it
-------------------------------------------------------------------------------
Writing the bytes back to the same absolute path in this pod would need no
rewriting anywhere and would keep the original path in the notice. It is also a
way to hand the model arbitrary file writes in the credentialed container: the
path arrives in a completion event the model composes, and ``/opt/data`` here
holds ``profiles/<name>/scripts``, which is importable. A staged copy under a
temp directory cannot become code no matter what the model names.

The basename is preserved inside that directory because it is what the reader
sees -- the adapter derives the attachment filename from the path, and a report
delivered as ``tmp8ug1ke`` is a worse bug than the one this fixes.

The notice keeps naming the agent's own path, not the staged one, because the
staged one is deleted before the message is read and describes nothing anyone
can act on. ``google_chat_relay_patch`` and the build-time attachment-notice
patch both ask ``original_path`` below for the translation.
"""

from __future__ import annotations

import asyncio
import functools
import glob
import logging
import os
import shutil
import tempfile
import time

from pathlib import Path, PurePosixPath

import sandbox_exec


LOGGER = logging.getLogger("sandbox-artifact-patch")

#: Largest single deliverable staged into this pod. The bytes cross as base64
#: over the sandbox's ssh channel, which is not a file-transfer protocol and
#: should not be asked to be one, and the gateway's volume is not scratch space
#: for whatever a card produced. Past this the artifact is left unstaged, which
#: is what happens to it today anyway -- the difference is the log line.
STAGE_MAX_BYTES = 8 * 1024 * 1024

#: Ceiling on the bytes one delivery may land, which is not
#: ``STAGE_MAX_BYTES * MAX_STAGED_ARTIFACTS``. The staging directory is under
#: the agent pod's ``/tmp``, an ``emptyDir`` the operator caps at 2Gi, and GKE
#: does not enable the alpha gate that would turn overflow into an ``ENOSPC``
#: this code could catch -- the kubelet evicts the pod instead. Sixteen maximal
#: artifacts would be 128 MiB per delivery against that budget, so the product
#: is bounded here rather than left to multiply.
STAGE_TOTAL_MAX_BYTES = 16 * 1024 * 1024

#: Prefix for the per-delivery temp directory, so anything left behind by a
#: process killed mid-delivery is identifiable in the volume -- and sweepable
#: by ``_sweep_stale`` at the next gateway start.
STAGED_DIR_PREFIX = "sandbox-artifact-"

#: Ceiling on artifacts staged for one card. Each one is an ssh round trip into
#: the sandbox, and the list comes from a completion event the model composes,
#: so it is not bounded by anything else. Well past what a card produces.
MAX_STAGED_ARTIFACTS = 16

#: Per-read ssh timeout. ``sandbox_exec.run`` passes ``timeout=None`` when it is
#: given none, and the login shell it reaches sources a ``~/.bashrc`` the model
#: owns -- an unbounded read is a hang this code chose. Generous for 8 MiB of
#: base64 over a pod-to-pod hop; short enough that a sandbox rolling under us
#: costs the delivery rather than the notifier.
STAGE_READ_TIMEOUT_SECONDS = 30.0

#: Wall clock for one delivery's staging. Sixteen artifacts each timing out
#: individually is eight minutes of a worker thread for one card, so the loop
#: stops here and delivers what it already has.
STAGE_DEADLINE_SECONDS = 120.0

#: How old a leftover staging directory must be before ``_sweep_stale`` removes
#: it. Comfortably past ``STAGE_DEADLINE_SECONDS``, so a delivery in flight in
#: another process is never swept out from under itself.
STALE_SWEEP_AGE_SECONDS = 3600.0

#: ``$HOME`` for the sandbox login, per ``deploy/shared/sandbox_mirror.py``. A
#: ``~`` in a declared path means this, not the gateway's own home: the path
#: names a file in the other pod, and ``os.path.expanduser`` here would resolve
#: it against this one and read a different file -- or miss.
SANDBOX_HOME = "/home/agent"

#: Set on the patched class so a second ``install()`` is a no-op. The gateway
#: imports the trigger module once, but a test importing it twice should not
#: wrap the wrapper.
PATCH_FLAG = "_sandbox_artifacts_patched"

#: Staged path -> the path the agent actually wrote, for the attachment notice.
#: Entries live only for the delivery that made them; ``_cleanup`` drops them
#: with the files.
_ORIGINALS: dict[str, str] = {}

#: ``_sandbox_on``'s memo. The managed config does not change without a pod
#: restart, and the alternative is a YAML parse on the notifier's path for every
#: completed card.
_SANDBOX_ON: bool | None = None


def original_path(path: str) -> str:
    """The agent-side path a staged copy came from, or ``path`` unchanged."""
    return _ORIGINALS.get(path, path)


def _expand(item: str) -> str:
    """``~`` against the sandbox's home, not this pod's.

    ``os.path.expanduser`` reads ``$HOME`` from the running process, which here
    is the gateway's. The path it is being asked about is a file in the sandbox
    pod, so expanding it here either misses or -- worse -- names a real file of
    the same name on the wrong side and delivers that instead.
    """
    if item == "~":
        return SANDBOX_HOME
    if item.startswith("~/"):
        return os.path.join(SANDBOX_HOME, item[2:])
    return item


def _candidates(event_payload) -> list[str]:
    """The declared artifact paths, deduplicated, in the order they arrived."""
    if not isinstance(event_payload, dict):
        return []
    raw = event_payload.get("artifacts")
    if not isinstance(raw, (list, tuple)):
        return []

    found: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item:
            continue
        expanded = _expand(item)
        if expanded in seen:
            continue
        seen.add(expanded)
        found.append(expanded)
        if len(found) >= MAX_STAGED_ARTIFACTS:
            LOGGER.warning(
                "more than %d artifacts declared; the rest are not staged",
                MAX_STAGED_ARTIFACTS,
            )
            break
    return found


def _denied(base, path: str) -> bool:
    """Whether upstream's media-delivery denylist covers this sandbox path.

    ``validate_media_delivery_path`` cannot be asked directly: it resolves the
    path ``strict=True`` and calls ``is_file()``, both of which are answers
    about *this* pod, and the file is in the other one. What survives that is
    the part that matters -- the denylist -- so it is applied here, against
    upstream's own constants rather than a copy of their values.

    Twice, because the home-relative half of that list is resolved against the
    running process's ``$HOME``. Upstream's own check catches anything under the
    gateway's home and the absolute system prefixes; the second pass re-reads
    the same subpath names against ``SANDBOX_HOME``, which is where a declared
    ``~/.ssh/id_ed25519`` actually lives.

    A symlink in the sandbox still defeats this, and deliberately is not chased:
    the read runs as the login that wrote the file, so a symlink buys the model
    nothing it could not have pasted into the message itself. What the denylist
    is worth here is the honest path -- a card that declares
    ``/etc/shadow`` because a tool put it in a list -- which is exactly what it
    is worth upstream.
    """
    try:
        if base._path_under_denied_prefix(Path(path)):
            return True
        candidate = PurePosixPath(path)
        roots = [PurePosixPath(prefix)
                 for prefix in base._MEDIA_DELIVERY_DENIED_PREFIXES]
        roots += [PurePosixPath(SANDBOX_HOME) / sub
                  for sub in base._MEDIA_DELIVERY_DENIED_HOME_SUBPATHS]
    except (AttributeError, TypeError, ValueError):
        LOGGER.warning(
            "upstream's media-delivery denylist could not be applied to %s, so "
            "it is not staged", path, exc_info=True,
        )
        return True
    return any(candidate == root or root in candidate.parents for root in roots)


def _screen(paths: list[str]) -> list[str]:
    """Drop what the gateway's own delivery policy would have dropped.

    Staging rewrites every path to one under the system temp directory, which
    is on no denylist -- so whatever ``filter_local_delivery_paths`` would have
    said about the path the model declared, it is now being asked about a path
    this module chose. The screen has to happen out here or it does not happen
    at all.
    """
    if not paths:
        return []
    try:
        from gateway.platforms import base
    except ImportError:
        # The original method imports this module itself a few lines in, so a
        # failure here is not a case where delivery would otherwise have worked.
        LOGGER.warning(
            "cannot reach upstream's delivery policy; no artifact is staged",
            exc_info=True,
        )
        return []

    try:
        strict = base._media_delivery_strict_mode()
    except AttributeError:
        LOGGER.warning(
            "cannot tell whether strict media delivery is on; no artifact is "
            "staged", exc_info=True,
        )
        return []
    if strict:
        LOGGER.warning(
            "strict media delivery is on, and its allowlist and recency tests "
            "cannot be evaluated for a file in the sandbox; no artifact is "
            "staged",
        )
        return []

    kept = []
    for path in paths:
        if _denied(base, path):
            LOGGER.warning(
                "%s is on the media-delivery denylist; not staged", path,
            )
            continue
        kept.append(path)
    return kept


def _stage(paths: list[str]) -> tuple[list[str], str | None]:
    """Copy each path that the sandbox holds into a fresh temp directory here.

    Returns the staged paths and the directory holding them, or an empty list
    and ``None`` when there was nothing to bring across. Never raises: it is
    called from the notifier's own coroutine, where an exception aborts the
    delivery and -- because the cursor is written after delivery -- leaves the
    card to be retried on every tick.

    The sandbox is asked about every declared path, including one that is
    already a file in this pod. That is not redundant. ``sandbox_mirror``
    copied the agent pod's trees into the sandbox at the same absolute paths
    and did not delete the originals, so on an upgraded install both pods hold
    ``/opt/data/scratch/audit.csv`` -- and the one here is the stale
    pre-migration copy. Preferring it would deliver last month's report as this
    month's, with nothing in any log to say so. A path the sandbox does not
    have falls through unstaged, and the original method's own ``isfile``
    screen picks up the local copy if there is one.
    """
    staged: list[str] = []
    directory: str | None = None
    remaining = STAGE_TOTAL_MAX_BYTES
    deadline = time.monotonic() + STAGE_DEADLINE_SECONDS

    try:
        for path in paths:
            if time.monotonic() >= deadline:
                LOGGER.warning(
                    "staging took longer than %.0fs; the remaining artifacts "
                    "are not staged", STAGE_DEADLINE_SECONDS,
                )
                break

            try:
                raw = sandbox_exec.read_bytes(
                    path, max_bytes=STAGE_MAX_BYTES + 1,
                    timeout=STAGE_READ_TIMEOUT_SECONDS,
                )
            except sandbox_exec.SandboxUnavailable:
                LOGGER.warning(
                    "the shell sandbox could not be reached to stage %s; it "
                    "will not be delivered", path, exc_info=True,
                )
                continue
            except sandbox_exec.SandboxMisconfigured:
                LOGGER.warning("cannot tell where %s lives", path, exc_info=True)
                continue
            except Exception:
                # The path comes from a record the model composes. A NUL in it
                # reaches `subprocess` as a ValueError, not as a `False` from
                # `isfile`, and one bad path must not cost the whole delivery.
                LOGGER.warning(
                    "could not read %s out of the sandbox", path, exc_info=True,
                )
                continue

            if raw is None:
                # Not a readable file over there either, so it is the "mentioned
                # for reference only" case the original method already tolerates.
                continue
            if len(raw) > STAGE_MAX_BYTES:
                LOGGER.warning(
                    "%s is larger than %d bytes, so it is not staged for "
                    "delivery", path, STAGE_MAX_BYTES,
                )
                continue
            if len(raw) > remaining:
                LOGGER.warning(
                    "this card's artifacts exceed %d bytes in total; %s and "
                    "anything after it are not staged",
                    STAGE_TOTAL_MAX_BYTES, path,
                )
                break

            # One subdirectory per artifact, numbered, so the basename can be
            # kept whole -- it is the reader's filename, per the module
            # docstring -- even when a card produced `cluster-a/report.md` and
            # `cluster-b/report.md`. Flat, the second would overwrite the first
            # and be delivered twice.
            try:
                if directory is None:
                    directory = tempfile.mkdtemp(prefix=STAGED_DIR_PREFIX)
                enclosing = os.path.join(directory, str(len(staged)))
                os.mkdir(enclosing)
                target = os.path.join(enclosing, os.path.basename(path))
                with open(target, "wb") as handle:
                    handle.write(raw)
            except OSError:
                LOGGER.warning("could not stage %s for delivery", path, exc_info=True)
                continue

            remaining -= len(raw)
            _ORIGINALS[target] = path
            staged.append(target)
    except Exception:
        LOGGER.warning("staging this card's artifacts failed", exc_info=True)
        _cleanup(staged, directory)
        return [], None

    return staged, directory


def _sandbox_on() -> bool:
    """``sandbox_enabled()``, memoised, with a broken config treated as "off".

    That call raises rather than answering ``False`` when the config is present
    and unreadable, because its other callers are about to run model-authored
    code and must not do it locally by default. Nothing is executed here, so
    the same failure means only that the artifact stays where it is -- and
    letting it out would abort the notifier's delivery of a completed card
    over a config this module does not own.
    """
    global _SANDBOX_ON
    if _SANDBOX_ON is None:
        try:
            _SANDBOX_ON = sandbox_exec.sandbox_enabled()
        except sandbox_exec.SandboxMisconfigured:
            LOGGER.warning(
                "cannot tell whether the shell sandbox is on; artifacts will be "
                "delivered as if it were off", exc_info=True,
            )
            _SANDBOX_ON = False
    return _SANDBOX_ON


def _cleanup(staged: list[str], directory: str | None) -> None:
    """Drop the staged copies and their notice translations."""
    for path in staged:
        _ORIGINALS.pop(path, None)
    if directory:
        shutil.rmtree(directory, ignore_errors=True)


def _sweep_stale() -> None:
    """Remove staging directories an earlier process did not get to delete.

    ``_cleanup`` runs in a ``finally``, which a SIGKILL does not honour -- and
    the pod is killed on every rollout and every OOM. What is left behind sits
    in the same 2Gi ``emptyDir`` the size ceilings above are protecting, so it
    is swept at gateway start rather than accumulating until an eviction.
    """
    pattern = os.path.join(tempfile.gettempdir(), STAGED_DIR_PREFIX + "*")
    cutoff = time.time() - STALE_SWEEP_AGE_SECONDS
    for path in glob.glob(pattern):
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def install() -> None:
    """Wrap ``_deliver_kanban_artifacts`` so sandbox artifacts reach it."""
    try:
        from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    except ImportError:
        # A base image moved the mixin, or `gateway.kanban_watchers` is partway
        # through its own import. Either way the patch does not apply and every
        # artifact is dropped exactly as it is without it, which is worth a
        # WARNING -- but not worth aborting the import that called us: this hook
        # runs from inside `gateway.platform_registry`'s loader, so raising here
        # takes the relay loop and the whole chat connection down over
        # undelivered attachments.
        LOGGER.warning(
            "no kanban watcher to patch; artifacts written in the sandbox will "
            "not be delivered", exc_info=True,
        )
        return

    original = getattr(GatewayKanbanWatchersMixin, "_deliver_kanban_artifacts", None)
    if original is None:
        LOGGER.warning(
            "%s has no _deliver_kanban_artifacts; sandbox-written artifacts "
            "will not be delivered", GatewayKanbanWatchersMixin.__name__,
        )
        return
    if getattr(GatewayKanbanWatchersMixin, PATCH_FLAG, False):
        return

    # `wraps` is not cosmetic here. The build-time signature gate pins this
    # method's upstream shape, and the only interpreter it can read it in is one
    # where `sitecustomize` has already installed this patch -- so without
    # `__wrapped__` for `inspect.signature` to follow, that check compares the
    # shim against itself and stops guarding anything.
    @functools.wraps(original)
    async def _deliver_kanban_artifacts(self, *, adapter, chat_id, metadata,
                                        event_payload, task):
        async def deliver(payload):
            return await original(
                self, adapter=adapter, chat_id=chat_id, metadata=metadata,
                event_payload=payload, task=task,
            )

        if not _sandbox_on():
            return await deliver(event_payload)

        try:
            wanted = _screen(_candidates(event_payload))
            # `_stage` blocks: up to sixteen ssh round trips, each of which can
            # sit for `STAGE_READ_TIMEOUT_SECONDS`. On the event loop that is
            # every chat connection this gateway holds, frozen, because a card
            # completed -- and the far side runs a `~/.bashrc` the model owns.
            staged, directory = await asyncio.to_thread(_stage, wanted)
        except Exception:
            LOGGER.warning(
                "could not stage this card's artifacts; delivering without "
                "them", exc_info=True,
            )
            return await deliver(event_payload)

        if not staged:
            _cleanup([], directory)
            return await deliver(event_payload)

        # The staged paths go in front of whatever the payload already carried.
        # The originals are left in place rather than removed: they name files
        # that do not exist here, so the method's own `isfile` screen drops
        # them, and leaving them means this wrapper never has to be right about
        # which of the three sources a given path came from.
        payload = dict(event_payload) if isinstance(event_payload, dict) else {}
        carried = payload.get("artifacts")
        carried = list(carried) if isinstance(carried, (list, tuple)) else []
        payload["artifacts"] = staged + carried

        try:
            return await deliver(payload)
        finally:
            _cleanup(staged, directory)

    GatewayKanbanWatchersMixin._deliver_kanban_artifacts = _deliver_kanban_artifacts
    setattr(GatewayKanbanWatchersMixin, PATCH_FLAG, True)

    try:
        _sweep_stale()
    except Exception:
        LOGGER.warning("could not sweep stale staging directories", exc_info=True)

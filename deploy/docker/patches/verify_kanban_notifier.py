#!/usr/bin/env python3
"""Build gate for the kanban notifier patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after the applier. It
replaces ``verify_kanban_wake_kinds.py`` and the delivery half of
``verify_kanban_result.py`` (whose remaining sections still gate
``tools/kanban_result_required.py``, a different patch against a different
file), and it additionally covers the clip wiring, which previously had nothing
behind it but two ``grep -q``\\ s in the Dockerfile.

The applier only proves its two anchors matched, and a matched anchor is the
weaker half of all four concerns here:

* **Clip.** A textual grep proves ``_clip_handoff`` is called; it does not prove
  the name resolves at runtime, and the whole patch exists because a URL arrived
  severed. So the clip is driven, not read.
* **Delivery.** The regression that motivated it was not a failed edit — it was
  a field the model was told not to use — and the follow-on regression was a
  hook that appended where it had to replace. Both are behavioural.
* **Wake.** Every failure mode that costs anything is silent by construction:
  ``resolve_wake_kinds`` fails *towards* upstream, so a ``hermes_cli.config``
  that moved, a ``gateway.wake`` that moved, or a ``DEFAULT_WAKE_KINDS``
  whitelist that fell behind the notifier all present identically to "the
  operator never set ``kanban.wake_on_events``". The symptom is not an
  exception, it is the 5.9 s / 32,460-token redundant turn from task
  ``t_c31a1f00`` quietly coming back.
* **Marker.** Worse again, because it writes into gateway session state that
  nothing else in the build reads back. A note parked on a session *id* instead
  of a session key, a store that dropped ``lookup_by_session_id``, a ``run.py``
  that stopped draining ``sidecar_notes`` — each is a silent no-op producing
  precisely what the unpatched gateway produced, which is the 9m46s of dead wait
  on task ``t_a8f58a2a``.

So this drives the *patched* runtime rather than reading it: the real
``hermes_cli.config.load_config``, the real ``gateway.wake.adapter_supports_push``,
the real ``APIServerAdapter`` whose ``supports_async_delivery = False`` is the
reason the narrowing is scoped to the push path at all, the real
``_clip_handoff`` / ``_kanban_handoff_with_result`` / ``_wake_kinds_for`` names
the notifier loop resolved, and — for the marker — a real ``SessionStore`` with
a real ``SessionEntry`` in it, the real ``GatewayRunner`` sidecar-note methods
and the real ``consume_gateway_turn_context_notes`` the next turn calls.

Usage::

    cd /opt/hermes && python3 verify_kanban_notifier.py
"""

from __future__ import annotations

import re
import sys

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


import gateway.kanban_watchers as watchers  # noqa: E402
from gateway.kanban_notifier import (  # noqa: E402
    CONFIG_KEY,
    DEFAULT_LIMIT,
    DEFAULT_WAKE_KINDS,
    RESULT_LIMIT,
    _adapter_can_push,
    _load_kanban_config,
    clip_handoff,
    resolve_wake_kinds,
    result_block,
    wake_kinds_for,
)

with open("gateway/kanban_watchers.py", encoding="utf-8") as _notifier_src:
    NOTIFIER_SOURCE = _notifier_src.read()

FAILURE_ONLY = {"wake_on_events": ["gave_up", "crashed", "timed_out", "blocked"]}


def cfg(kanban):
    return lambda: {"kanban": kanban}


class Event:
    def __init__(self, kind):
        self.kind = kind


# --- 1. The wiring resolved ---------------------------------------------------
# All three names are appended in one import trailer, so one of them failing to
# resolve means none of them did — but they are checked separately because the
# failure that matters is per-symbol: a rename in kanban_notifier.py breaks
# exactly one, and the notifier would then raise on the delivery path.
print("import wiring:")
check(
    "the notifier resolved the clip import",
    hasattr(watchers, "_clip_handoff"),
    "the trailer import did not execute",
)
check(
    "the notifier resolved the delivery import",
    hasattr(watchers, "_kanban_handoff_with_result"),
    "the trailer import did not execute",
)
check(
    "the notifier resolved the wake-kinds import",
    hasattr(watchers, "_wake_kinds_for"),
    "the trailer import did not execute",
)
check(
    "the notifier names exactly one kube-agents notifier module",
    NOTIFIER_SOURCE.count("from gateway.kanban_notifier import") == 1,
    "the merged trailer was applied twice, or a retired module is still wired in",
)
check(
    "the retired modules are no longer referenced",
    "gateway.kanban_wake_kinds" not in NOTIFIER_SOURCE
    and "gateway.kanban_result_delivery" not in NOTIFIER_SOURCE,
)

# --- 2. The clip -------------------------------------------------------------
# Upstream hard-sliced the handoff at a fixed width, which on 2026-08-03 turned
# a published ledger link into ".../is" — the delivered message was exactly 200
# characters and `/issues/30` had been cut down to `/is`. Both slices are
# rewritten; the greps prove the text landed, the drive proves the behaviour.
print("handoff clip:")
check(
    "the summary branch clips on a token boundary",
    "h = _clip_handoff(payload_summary)" in NOTIFIER_SOURCE,
)
check(
    "the no-summary branch clips on a token boundary",
    "r = _clip_handoff(task.result)" in NOTIFIER_SOURCE,
)
check(
    "neither of upstream's hard slices survived",
    "lines[0][:200]" not in NOTIFIER_SOURCE and "lines[0][:160]" not in NOTIFIER_SOURCE,
    "a surviving slice still severs the URL this patch exists to protect",
)

LEDGER_URL = "https://github.com/gke-agentic/adamparco-infra/issues/30"
PRODUCTION_SUMMARY = (
    "Workload Reliability Audit executed successfully: 44 findings (0 critical, "
    "28 major, 16 minor) across 3 clusters. The audit ledger has been updated at "
    + LEDGER_URL
)
check(
    "the production summary that broke now survives whole",
    watchers._clip_handoff(PRODUCTION_SUMMARY) == PRODUCTION_SUMMARY
    and LEDGER_URL in watchers._clip_handoff(PRODUCTION_SUMMARY),
)
check(
    "a URL is dropped rather than severed when the clip does bite",
    "https://" not in watchers._clip_handoff(PRODUCTION_SUMMARY, 200)
    and len(watchers._clip_handoff(PRODUCTION_SUMMARY, 200)) <= 200,
    "a truncated link is a dead link; the whole token has to go",
)
check(
    "lines after the first are no longer discarded",
    watchers._clip_handoff("First line.\nSecond line.\nThird line.").count("\n") == 2,
)

# --- 3. Delivery --------------------------------------------------------------
print("result delivery:")
check(
    "the completion message's tail is built by the patch",
    "handoff = _kanban_handoff_with_result(handoff, task)" in NOTIFIER_SOURCE,
    "an appending hook cannot drop the clip the notifier already built",
)

catalogue = "\n".join(f"{i}. cron-job-{i} — 0 {i} * * *" for i in range(1, 10))
block = result_block("Cataloged all 9 cron jobs.", catalogue)
check("a multi-line result survives whole", block.count("\n") >= 9)
check("the last line is delivered", "cron-job-9" in block)
check(
    "a result already shown in the status line is not repeated",
    result_block(catalogue, catalogue) == "",
)
check("an empty result adds nothing", result_block("status", None) == "")

huge = " ".join(f"token{i}" for i in range(20000))
clipped = result_block("status", huge)
check(
    "a runaway result is clipped and says so",
    len(clipped) <= RESULT_LIMIT + 200 and "clipped" in clipped.lower(),
)
check(
    "clipping never severs a URL",
    "https://" not in result_block("s", huge + " https://example.invalid/issues/27", limit=200),
)
check(
    "a dead task row leaves the status line the notifier already built",
    watchers._kanban_handoff_with_result("\nstatus", None) == "\nstatus",
)

# The branch that has no event summary: kanban_watchers.py builds the status
# line out of a clip of task.result, so the message used to carry the opening
# of the report and then the report. On the 60-line catalogue below, jobs 1 to
# 19 arrived twice.
long_catalogue = "\n".join(
    f"{i}. cron-job-{i} — schedule `0 {i} * * *` — enabled" for i in range(1, 61)
)


class _ClippedTask:
    result = long_catalogue


check(
    "the fixture is over the status line's budget",
    len(long_catalogue) > DEFAULT_LIMIT,
)
no_summary_tail = watchers._kanban_handoff_with_result(
    "\n" + clip_handoff(long_catalogue), _ClippedTask()
)
check(
    "an over-budget report is delivered once rather than clipped and repeated",
    no_summary_tail.count("1. cron-job-1 ") == 1,
    "the clipped status line was kept above the full report",
)
check(
    "the report the reader gets is the whole one",
    "60. cron-job-60" in no_summary_tail,
)
check(
    "no clip marker is left promising text that is already there",
    "[…]" not in no_summary_tail,
)
check(
    "a status line that is not the report is kept",
    "Cataloged all 9" in watchers._kanban_handoff_with_result(
        "\nCataloged all 9 cron jobs.", _ClippedTask()
    ),
)

# --- 4. The whitelist still covers every kind the notifier knows about --------
# `resolve_wake_kinds` treats DEFAULT_WAKE_KINDS as a whitelist so a typo in
# config cannot be mistaken for a real kind. The cost of that is drift: if a
# base-image bump teaches the notifier a sixth terminal kind, the whitelist
# silently filters it out and an operator listing it gets a warning about an
# "unknown" kind their gateway plainly understands. The notifier enumerates the
# kinds it can describe in its own `_parts` block, so that block is the source
# of truth to compare against.
print("whitelist drift:")
check(
    "the notifier calls the helper with both no-send tests",
    'd["events"], adapter=adapter, passive_delivered=send_passive' in NOTIFIER_SOURCE,
    "adapter= alone is not enough: delivery_mode='wake' skips the text ping on "
    "a push adapter, and narrowing the wake there drops the completion",
)
# Upstream's own gate, kept verbatim by the patch. It answers a different
# question than the narrowing does — "did this subscriber ask to be woken at
# all" versus "is this kind worth a model turn" — and losing it would wake
# notify-only subscribers on every terminal event without changing anything the
# check above can see.
check(
    "and upstream's per-subscription wake gate still wraps it",
    "if wake_agent" in NOTIFIER_SOURCE,
    "delivery_mode=notify subscribers would be woken anyway",
)
# `send_passive` is upstream's own name for "this mode gets a text ping". The
# anchor above is worthless if the notifier stopped computing it, which is
# exactly what a future upstream refactor of delivery_mode would do.
check(
    "and send_passive is still what upstream derives from delivery_mode",
    'send_passive = mode != "wake"' in NOTIFIER_SOURCE,
    "the anchor above would bind a name that no longer means "
    "'a ping was sent'",
)
check(
    "upstream's hardcoded tuple is gone",
    "_WAKE_KINDS = (" not in NOTIFIER_SOURCE and "in _WAKE_KINDS}" not in NOTIFIER_SOURCE,
    "a second definition would shadow the configurable one",
)
notifier_kinds = set(re.findall(r'if "(\w+)" in _wake_kinds', NOTIFIER_SOURCE))
check(
    "the notifier's own kind list was located",
    notifier_kinds,
    "the `if \"<kind>\" in _wake_kinds` block moved; re-derive this check",
)
check(
    "every kind the notifier can describe is in DEFAULT_WAKE_KINDS",
    notifier_kinds <= set(DEFAULT_WAKE_KINDS),
    f"notifier knows {sorted(notifier_kinds - set(DEFAULT_WAKE_KINDS))}, "
    f"which the whitelist would drop as a typo",
)
check(
    "DEFAULT_WAKE_KINDS claims nothing the notifier cannot describe",
    set(DEFAULT_WAKE_KINDS) <= notifier_kinds,
    f"whitelist has {sorted(set(DEFAULT_WAKE_KINDS) - notifier_kinds)} extra",
)

# --- 5. The real config loader is reachable ----------------------------------
# The exact silent no-op this patch is most exposed to. `_load_kanban_config`
# returns None when `hermes_cli.config` cannot be imported or read, and None
# means DEFAULT_WAKE_KINDS on every single delivery — the key stops working and
# nothing distinguishes that from an operator who never set it.
print("real config path:")
subtree = _load_kanban_config(None)
check(
    "hermes_cli.config.load_config is importable and readable",
    isinstance(subtree, dict),
    "the module falls back to upstream on every call; kanban."
    f"{CONFIG_KEY} would be dead config",
)
check(
    "the no-argument path returns a usable set",
    isinstance(resolve_wake_kinds(), tuple)
    and set(resolve_wake_kinds()) <= set(DEFAULT_WAKE_KINDS),
)

# --- 6. The real push-capability probe ---------------------------------------
# `_adapter_can_push` prefers `gateway.wake.adapter_supports_push` and only
# falls back to re-stating its one-line contract when that import fails —
# which is the host-side test condition, not an in-image one. Drive it with the
# two real adapter classes so a `gateway.wake` that moved is caught here rather
# than by a completed card that never gets announced.
print("push capability:")
from gateway.platforms.api_server import APIServerAdapter  # noqa: E402
from gateway.platforms.base import BasePlatformAdapter  # noqa: E402
from gateway.wake import adapter_supports_push  # noqa: E402

check(
    "the api_server adapter is still the non-push one",
    adapter_supports_push(APIServerAdapter) is False,
    "the whole non-push carve-out is predicated on this adapter existing",
)
check("_adapter_can_push agrees on api_server", _adapter_can_push(APIServerAdapter) is False)
check("_adapter_can_push agrees on the base adapter", _adapter_can_push(BasePlatformAdapter) is True)
check(
    "an adapter that does not declare the flag counts as push",
    _adapter_can_push(object()) is True,
    "reading it as non-push restores the redundant turn on every Slack card",
)

# --- 7. The decision the notifier actually makes ------------------------------
print("wake decision:")
completed = [Event("completed"), Event("commented")]
mixed = [Event("completed"), Event("crashed")]

check(
    "an unset key behaves exactly like upstream",
    wake_kinds_for(completed, cfg({}), adapter=BasePlatformAdapter) == {"completed"},
)
check(
    "a delivered completion costs no turn on a push adapter",
    wake_kinds_for(completed, cfg(FAILURE_ONLY), adapter=BasePlatformAdapter) == set(),
    "this is the 5.9s / 32,460-token hop the patch exists to remove",
)
check(
    "a failure in the same batch still wakes the creator",
    wake_kinds_for(mixed, cfg(FAILURE_ONLY), adapter=BasePlatformAdapter) == {"crashed"},
)
check(
    "a non-push adapter still wakes on completion",
    wake_kinds_for(completed, cfg(FAILURE_ONLY), adapter=APIServerAdapter) == {"completed"},
    "on api_server the wake self-post IS the delivery; narrowing loses the answer",
)
check(
    "even an explicit empty list cannot silence the non-push path",
    wake_kinds_for(completed, cfg({"wake_on_events": []}), adapter=APIServerAdapter)
    == {"completed"},
)
# The second no-send path, and the one that looks like the first case above
# rather than the two before it: a push adapter, so `_adapter_can_push` is True
# and the adapter carve-out does not fire. Only `passive_delivered` separates
# "the ping already said it" from "the wake is all there is".
check(
    "a wake-only subscription still wakes on completion",
    wake_kinds_for(
        completed, cfg(FAILURE_ONLY), adapter=BasePlatformAdapter, passive_delivered=False
    )
    == {"completed"},
    "delivery_mode='wake' skips the text ping, so narrowing the wake away "
    "delivers the card to nobody and then advances the cursor past it",
)
check(
    "and an explicit empty list cannot silence that path either",
    wake_kinds_for(
        completed,
        cfg({"wake_on_events": []}),
        adapter=BasePlatformAdapter,
        passive_delivered=False,
    )
    == {"completed"},
)
check(
    "the default is the delivered case, so a caller that predates the mode "
    "keeps narrowing",
    wake_kinds_for(completed, cfg(FAILURE_ONLY), adapter=BasePlatformAdapter) == set(),
)

# --- 8. Failure posture -------------------------------------------------------
print("fail-soft posture:")


def raising():
    raise RuntimeError("config unreadable")


check(
    "an unreadable config still wakes on a crash",
    resolve_wake_kinds(raising) == DEFAULT_WAKE_KINDS,
    "failing closed would mean a crashed card silently never escalating",
)
check(
    "a config of the wrong shape still wakes on a crash",
    resolve_wake_kinds(lambda: "not a mapping") == DEFAULT_WAKE_KINDS,
)
check(
    "a value of the wrong shape still wakes on a crash",
    resolve_wake_kinds(cfg({"wake_on_events": {"crashed": True}})) == DEFAULT_WAKE_KINDS,
)
check(
    "an unknown kind is dropped rather than trusted",
    resolve_wake_kinds(cfg({"wake_on_events": ["crashed", "compleeted"]})) == ("crashed",),
)

# --- 9. The suppressed completion is recorded on the creator's session --------
# Section 7's narrowing is only safe because of this one, and this one is the
# most silent thing in the patch: it writes into gateway state that nothing else
# in the build reads back, so a marker parked on the wrong key, a session store
# that no longer exposes the reverse lookup, or a run.py that stopped draining
# the sidecar notes all produce *exactly* what the unpatched gateway produced —
# a creator whose transcript never learns the card finished. That is the 9m46s
# of dead wait on task t_a8f58a2a, and no exception is raised anywhere along the
# way. So the whole chain is driven on real objects rather than described.
print("suppressed-completion marker:")
import threading  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from gateway.kanban_notifier import (  # noqa: E402
    NOTE_SIGNATURE,
    completion_note,
    note_suppressed_completion,
    suppressed_kinds,
)
from gateway.run import GatewayRunner  # noqa: E402
from gateway.session import SessionEntry, SessionStore  # noqa: E402
from agent.turn_context import consume_gateway_turn_context_notes  # noqa: E402

check(
    "the notifier resolved the marker import",
    hasattr(watchers, "_kanban_note_suppressed"),
    "the trailer import did not execute",
)
check(
    "the marker is called exactly once",
    NOTIFIER_SOURCE.count("_kanban_note_suppressed(") == 1,
    "a second call would announce the same card twice on one turn",
)
check(
    "the marker is called with everything it names the card by",
    'self, d["events"], _wake_kinds, task, sub, board_slug,' in NOTIFIER_SOURCE,
)
_wake_at = NOTIFIER_SOURCE.find(
    'd["events"], adapter=adapter, passive_delivered=send_passive'
)
_note_at = NOTIFIER_SOURCE.find("_kanban_note_suppressed(")
# `if task_terminal:` guards this delivery's unsub. It is the tail of the same
# block the marker sits in, and unlike `self._kanban_unsub` — which upstream
# also calls from two sibling branches earlier in the loop — it appears once.
_terminal_at = NOTIFIER_SOURCE.find("if task_terminal:")
check(
    "the marker runs after the wake set it reports on",
    0 <= _wake_at < _note_at,
    "it subtracts the wake set from what upstream would have woken for",
)
check(
    "the marker runs in the block that owns the terminal event",
    0 <= _note_at < _terminal_at,
    "the marker drifted past the unsub; re-derive the wake anchor",
)

# The reverse lookup. task.session_id is a persisted session *id*; per-turn
# state is keyed by the session *key*. Writing to the id would be a no-op that
# nothing anywhere reports.
check(
    "the session store still exposes the id→key lookup",
    callable(getattr(SessionStore, "lookup_by_session_id", None)),
    "without it the marker cannot find the creator and silently writes nothing",
)
check(
    "SessionEntry still carries the session key",
    "session_key" in getattr(SessionEntry, "__dataclass_fields__", {}),
)
for _name in (
    "_set_pending_turn_sidecar_notes",
    "_consume_pending_turn_sidecar_notes",
    "_peek_session_state",
):
    check(
        f"the gateway still has {_name}",
        callable(getattr(GatewayRunner, _name, None)),
    )

# The two links between the note being staged and the model seeing it. Neither
# is reachable from here without booting a turn, and either one going away turns
# the marker into a write nobody reads.
RUN_SOURCE = open("gateway/run.py").read()
TURN_CONTEXT_SOURCE = open("agent/turn_context.py").read()
check(
    "run.py still drains the staged notes onto the agent",
    "_gateway_turn_context_notes = " in RUN_SOURCE
    and "self._runner._consume_pending_turn_sidecar_notes(ctx.session_key)" in RUN_SOURCE,
    "staged notes would accumulate on the session and never reach a turn",
)
check(
    "turn_context.py still delivers them on the user message",
    "consume_gateway_turn_context_notes(agent)" in TURN_CONTEXT_SOURCE,
    "the agent-side copy would be set and never read",
)

# --- the round trip, on the real classes -------------------------------------
# Bare instances: GatewayRunner._sessions_map is explicitly written to support
# runners built via object.__new__, and SessionStore.lookup_by_session_id needs
# only the lock and the entries dict once _loaded short-circuits the disk read.
CREATOR_ID = "20260808_190725_6714054d"
CREATOR_KEY = "agent:main:slack:dm:T0BLH2UB516:D0BKGRBM6RH:1786216044.637229"
NOW = datetime.now(timezone.utc)

store = object.__new__(SessionStore)
store._lock = threading.Lock()  # what SessionStore.__init__ builds
store._loaded = True  # short-circuits _ensure_loaded_locked's disk read
store._entries = {
    CREATOR_KEY: SessionEntry(
        session_key=CREATOR_KEY, session_id=CREATOR_ID, created_at=NOW, updated_at=NOW,
    )
}
runner = object.__new__(GatewayRunner)
runner.session_store = store


class _Card:
    id = "t_a8f58a2a"
    session_id = CREATOR_ID
    title = "List configured cron jobs"
    status = "done"


SUB = {"task_id": "t_a8f58a2a", "chat_id": "D0BKGRBM6RH"}
COMPLETED = [Event("completed")]

check(
    "the completion the narrowing dropped is the one recorded",
    suppressed_kinds(COMPLETED, wake_kinds_for(COMPLETED, cfg(FAILURE_ONLY),
                                               adapter=BasePlatformAdapter)) == {"completed"},
)
check(
    "a card whose wake still fires is not double-announced",
    suppressed_kinds(COMPLETED, wake_kinds_for(COMPLETED, cfg({}),
                                               adapter=BasePlatformAdapter)) == set(),
)
check(
    "the marker was staged on the creator's session",
    note_suppressed_completion(runner, COMPLETED, set(), _Card(), SUB, "") is True,
    "the report reached the user and nothing told the agent",
)
check(
    "it landed on the session key, not the session id",
    runner._peek_session_state(CREATOR_KEY) is not None
    and runner._peek_session_state(CREATOR_ID) is None,
)

# Exactly what gateway/run.py does at the top of the creator's next turn.
staged = runner._consume_pending_turn_sidecar_notes(CREATOR_KEY)
check("the next turn reads back one note", len(staged) == 1, f"got {staged!r}")
check(
    "the note names the card and contradicts 'still running'",
    staged and "t_a8f58a2a" in staged[0] and "NOT still running" in staged[0],
    staged[0] if staged else "nothing was staged",
)
check(
    "the note does not ask the agent to re-post what the user can see",
    staged and "already delivered" in staged[0],
)
check(
    "the marker is one-shot at the session",
    runner._consume_pending_turn_sidecar_notes(CREATOR_KEY) == [],
    "a note replayed every turn would re-announce a card the agent reported",
)


class _Agent:
    pass


note_suppressed_completion(runner, COMPLETED, set(), _Card(), SUB, "")
agent = _Agent()
agent._gateway_turn_context_notes = "\n\n".join(
    runner._consume_pending_turn_sidecar_notes(CREATOR_KEY)
)
delivered = consume_gateway_turn_context_notes(agent)
check(
    "the note survives into the turn's user-message sidecar",
    "t_a8f58a2a" in delivered,
    "the real consume_gateway_turn_context_notes dropped it",
)
check(
    "and is one-shot there too",
    consume_gateway_turn_context_notes(agent) == "",
)

# --- the marker's failure posture --------------------------------------------
# Everything here is layered on top of a delivery that already succeeded, so
# every failure has to degrade to today's behaviour rather than raise into a
# loop that would rewind the claim and re-send the report.
class _Exploding:
    id = "t_a8f58a2a"
    title = "boom"
    status = "done"

    @property
    def session_id(self):
        raise RuntimeError("session row unreadable")


check(
    "a card the marker cannot read does not break the delivery",
    note_suppressed_completion(runner, COMPLETED, set(), _Exploding(), SUB, "") is False,
)
check(
    "a card with no creator session records nothing",
    note_suppressed_completion(runner, COMPLETED, set(), object(), SUB, "") is False,
    "cron and CLI cards have no gateway session to tell",
)
check(
    "a runner with no session store records nothing",
    note_suppressed_completion(object(), COMPLETED, set(), _Card(), SUB, "") is False,
)

# Upstream's own notes share the list, and one of them tells the agent its
# history was reset. A blind assignment would drop it.
RESET = "[System note: The user's previous session expired due to inactivity.]"
runner._set_pending_turn_sidecar_notes(CREATOR_KEY, [RESET])
note_suppressed_completion(runner, COMPLETED, set(), _Card(), SUB, "")
both = runner._consume_pending_turn_sidecar_notes(CREATOR_KEY)
check(
    "an upstream note staged in the same instant is not clobbered",
    RESET in both and any(n.startswith(NOTE_SIGNATURE) for n in both),
    f"got {both!r}",
)
check(
    "the note is small enough to be cheaper than the wake it replaces",
    len(completion_note("t_a8f58a2a", title=_Card.title, status="done",
                        kinds={"completed"})) < 600,
    "the wake it replaces cost 32,460 input tokens; this must stay trivial",
)

print()
if FAILURES:
    print(f"verify_kanban_notifier: {len(FAILURES)} check(s) FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_notifier: all checks passed")

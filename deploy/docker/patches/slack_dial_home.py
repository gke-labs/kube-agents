"""The autonomy dial on the Slack app's Home tab, when ``KUBEAGENTS_DIAL`` is on.

Installed into the image at ``/opt/hermes/gateway/slack_dial_home.py``.
``apply_slack_dial_home.py`` makes the Slack adapter hand every
``app_home_opened`` on the Home tab to :func:`opened`, and always wire
:func:`register`'s one listener, for this module's own action id. With the
switch off (the default, and any value but ``on``) opening the tab publishes the
one line :data:`OFF`, so a dial drawn before the switch went off does not
linger, and a click on a dial Slack still shows is acknowledged and answered
with the same line, so it never errors. Nothing is read or recorded while the
switch is off. An app without the Home tab turned on never sends the event, so
nothing changes for it.

What the tab shows
------------------
"What it may change": the three levels (Explain, Propose fixes, Auto-merge
proven fixes), the one recorded marked, and the kinds of fix under them with
whether each auto-merges. A kind auto-merges only at the Auto-merge level and
only when the record's ``kinds`` turns it on; this page never sets ``kinds``, so
the list here is read-only. Nothing reads the level yet, so the tab says it is
recorded only.

Who sees it and who may change it
---------------------------------
Only a member the adapter would answer (``_is_interactive_user_authorized``,
the check a button click gets) sees the dial; anyone else gets one line. Of
those, only an install admin gets the radio buttons: a member listed in
``KUBEAGENTS_DIAL_ADMINS`` (comma-separated Slack member ids) or, when that is
unset or empty, an admin or owner of the install's own workspace (the one its
first bot token authenticated in) by ``users.info``. Everyone else sees
the same page read-only. A lookup that fails shows the page read-only with a
line saying the account could not be checked. A pick is checked again when it
arrives, so a stale tab or a forged payload changes nothing for a non-admin.

The record
----------
One JSON file, :data:`STATE_NAME` under ``$HERMES_HOME`` (the agent's volume),
holding the level, who set it and when, and the per-kind switches. A missing or
unreadable file is :data:`DEFAULT_LEVEL`, which is what the agent does today:
it proposes fixes as pull requests and merges nothing. A pick rewrites only the
level, setter and time, keeping every other key; a file that exists but cannot
be read is never overwritten. Writes are atomic.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FLAG_ENV = "KUBEAGENTS_DIAL"
ADMINS_ENV = "KUBEAGENTS_DIAL_ADMINS"
STATE_NAME = "kube-agents/dial.json"

LEVEL_ACTION_ID = "kage_dial.level"

#: ``(value, title, description)``, in the order the tab lists them.
LEVELS = (
    ("explain", "Explain", "Finds and explains; changes nothing"),
    ("propose", "Propose fixes", "Opens a PR; your team merges"),
    ("automerge", "Auto-merge proven fixes", "Your pipeline merges proven fixes"),
)
LEVEL_VALUES = tuple(value for value, _title, _description in LEVELS)
LEVEL_TITLES = {value: title for value, title, _description in LEVELS}
DEFAULT_LEVEL = "propose"
AUTOMERGE_LEVEL = "automerge"

#: ``(kind, title, environment)`` for each kind of fix the dial lists.
FIX_KINDS = (("missing-pdb", "Disruption budgets", "dev"),)

TITLE = "What it may change"
OFF = "The autonomy dial is off on this install."
DENIED = "Kage only shows its settings to the people this install lets ask it."
READ_ONLY = "Only an install admin can change this."
NOT_CHECKED = "Could not check your Slack account, so this is read-only. Open the tab again to retry."
NOT_SAVED = "Not saved: the stored setting could not be read or written. Check the agent's logs."
NOT_ENFORCED = "Recorded only: the agent does not act on this setting yet."
SET_BY = "Set to *{title}* by <@{user}>"
KIND_ON = "auto-merges"
KIND_OFF = "off"
MARK_ON = "●"
MARK_OFF = "○"
SLACK_TIME = "<!date^{epoch}^{{date_short_pretty}} {{time}}|{fallback}>"
SLACK_TIME_FALLBACK = "%Y-%m-%d %H:%M UTC"

#: Each member's last DM channel from ``app_home_opened``, so a pick is
#: authorized as the open was (a 1:1 DM) rather than as a group chat.
_dm_channels: dict[tuple[str, str], str] = {}
_DM_CHANNELS_MAX = 500


def enabled() -> bool:
    """Whether ``KUBEAGENTS_DIAL`` is ``on``; unset or anything else is off."""
    return os.environ.get(FLAG_ENV, "").strip().lower() == "on"


def state_path() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~")
    return Path(home) / STATE_NAME


class UnreadableRecord(Exception):
    """The record exists but is not a JSON object."""


def _read_raw() -> dict:
    """The record as stored; empty when there is none, UnreadableRecord when it is not an object."""
    try:
        raw = json.loads(state_path().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise UnreadableRecord(str(exc)) from exc
    if not isinstance(raw, dict):
        raise UnreadableRecord(f"a {type(raw).__name__}, not an object")
    return raw


def read_state() -> dict:
    """The record, or the default level with no setter when there is none or it is unreadable."""
    state: dict = {"level": DEFAULT_LEVEL, "set_by": "", "set_at": 0, "kinds": {}}
    try:
        raw = _read_raw()
    except UnreadableRecord as exc:
        logger.warning("slack_dial_home: could not read %s; showing %s: %s", state_path(), DEFAULT_LEVEL, exc)
        return state
    if raw.get("level") in LEVEL_VALUES:
        state["level"] = raw["level"]
        state["set_by"] = str(raw.get("set_by") or "")
        state["set_at"] = raw.get("set_at") if isinstance(raw.get("set_at"), int) else 0
    if isinstance(raw.get("kinds"), dict):
        state["kinds"] = {str(k): v is True for k, v in raw["kinds"].items()}
    return state


def write_level(level: str, user_id: str) -> dict:
    """Record ``level`` as set by ``user_id`` now, keeping every other key; UnreadableRecord writes nothing."""
    state = _read_raw()
    state.update(level=level, set_by=user_id, set_at=int(time.time()))
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".dial-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return state


def configured_admins() -> set[str]:
    return {part.strip() for part in os.environ.get(ADMINS_ENV, "").split(",") if part.strip()}


def home_team(adapter: Any) -> str:
    """The install's own workspace: the one its first bot token authenticated in."""
    bot = getattr(adapter, "_bot_user_id", None)
    teams = getattr(adapter, "_team_bot_user_ids", None) or {}
    return next((team for team, user in teams.items() if bot and user == bot), "")


async def is_admin(adapter: Any, user_id: str) -> bool | None:
    """Whether ``user_id`` may change the dial; None when it could not be checked.

    The list in ``KUBEAGENTS_DIAL_ADMINS`` when set; otherwise an admin or owner
    of the install's own workspace, so an admin of another workspace the app is
    in, or of a Slack Connect guest's organisation, is not one here.
    """
    admins = configured_admins()
    if admins:
        return user_id in admins
    team = home_team(adapter)
    if not team:
        logger.warning("slack_dial_home: the install's workspace is not known yet; showing the dial read-only")
        return None
    try:
        response = await adapter._get_client("", team_id=team).users_info(user=user_id)
        user = (response.get("user") if hasattr(response, "get") else None) or {}
    except Exception as exc:  # noqa: BLE001 — undecided is read-only, never admin
        logger.warning("slack_dial_home: users.info failed for %s; showing the dial read-only: %s", user_id, exc)
        return None
    if user.get("team_id") != team:
        return False
    return bool(user.get("is_admin") or user.get("is_owner") or user.get("is_primary_owner"))


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _option(value: str, title: str, description: str) -> dict:
    return {
        "text": {"type": "mrkdwn", "text": f"*{title}*"},
        "description": {"type": "plain_text", "text": description},
        "value": value,
    }


def blocks(state: dict, can_change: bool, note: str = "") -> list[dict]:
    """The dial: the levels (a radio for an admin, marks otherwise), the kinds, who set it."""
    level = state["level"]
    out = [_section(f"*{TITLE}*")]
    if can_change:
        options = [_option(*entry) for entry in LEVELS]
        out.append({
            "type": "actions",
            "elements": [{
                "type": "radio_buttons",
                "action_id": LEVEL_ACTION_ID,
                "options": options,
                "initial_option": options[LEVEL_VALUES.index(level)],
            }],
        })
    else:
        for value, title, description in LEVELS:
            mark = MARK_ON if value == level else MARK_OFF
            out.append(_section(f"{mark}  *{title}*\n{description}"))
    for kind, title, environment in FIX_KINDS:
        on = level == AUTOMERGE_LEVEL and state["kinds"].get(kind, False)
        out.append(_context(f"{title} ({environment}) · {KIND_ON if on else KIND_OFF}"))
    footer = [NOT_ENFORCED]
    if state["set_by"]:
        line = SET_BY.format(title=LEVEL_TITLES[level], user=state["set_by"])
        if state["set_at"]:
            fallback = time.strftime(SLACK_TIME_FALLBACK, time.gmtime(state["set_at"]))
            line += " · " + SLACK_TIME.format(epoch=state["set_at"], fallback=fallback)
        footer.append(line)
    if note:
        footer.append(note)
    elif not can_change:
        footer.append(READ_ONLY)
    for line in footer:
        out.append(_context(line))
    return out


def _remember_dm(team_id: str, user_id: str, channel_id: str) -> None:
    if not (user_id and channel_id.startswith("D")):
        return
    _dm_channels.pop((team_id, user_id), None)
    _dm_channels[(team_id, user_id)] = channel_id
    while len(_dm_channels) > _DM_CHANNELS_MAX:
        _dm_channels.pop(next(iter(_dm_channels)))


async def _dm_channel(adapter: Any, user_id: str, team_id: str) -> str:
    """The member's DM with the app: remembered from the open, else opened (after a restart)."""
    channel = _dm_channels.get((team_id, user_id), "")
    if not channel:
        try:
            channel = str(await adapter._ensure_dm_conversation(user_id, team_id=team_id) or "")
        except Exception as exc:  # noqa: BLE001 — checked as a group chat, which fails closed
            logger.warning("slack_dial_home: could not open a DM with %s: %s", user_id, exc)
            channel = ""
        _remember_dm(team_id, user_id, channel)
    return channel if channel.startswith("D") else ""


async def _authorized(adapter: Any, user_id: str, team_id: str) -> bool:
    return adapter._is_interactive_user_authorized(
        user_id, channel_id=await _dm_channel(adapter, user_id, team_id), team_id=team_id,
    )


async def publish(adapter: Any, team_id: str, user_id: str, note: str = "") -> None:
    """Draw ``user_id``'s Home tab and publish it."""
    client = adapter._get_client("", team_id=team_id)
    if not await _authorized(adapter, user_id, team_id):
        view_blocks = [_section(DENIED)]
    else:
        admin = await is_admin(adapter, user_id)
        view_blocks = blocks(read_state(), admin is True, note or (NOT_CHECKED if admin is None else ""))
    try:
        await client.views_publish(user_id=user_id, view={"type": "home", "blocks": view_blocks})
    except Exception as exc:  # noqa: BLE001 — the tab stays as it was
        logger.warning("slack_dial_home: views.publish failed for %s: %s", user_id, exc)


async def publish_off(adapter: Any, team_id: str, user_id: str) -> None:
    """Replace ``user_id``'s Home tab with the one line :data:`OFF`."""
    view = {"type": "home", "blocks": [_section(OFF)]}
    try:
        await adapter._get_client("", team_id=team_id).views_publish(user_id=user_id, view=view)
    except Exception as exc:  # noqa: BLE001 — the tab stays as it was
        logger.warning("slack_dial_home: views.publish failed for %s: %s", user_id, exc)


async def opened(adapter: Any, event: dict, body: dict | None = None) -> None:
    """Answer the Home tab opening with the dial, or with :data:`OFF` when the switch is off."""
    user_id = str(event.get("user") or "")
    if not user_id:
        return
    team_id = adapter._event_team_id(event, body)
    if not enabled():
        await publish_off(adapter, team_id, user_id)
        return
    _remember_dm(team_id, user_id, str(event.get("channel") or ""))
    await publish(adapter, team_id, user_id)


async def on_level(adapter: Any, ack: Any, body: dict, action: dict) -> None:
    """Record a pick from an admin; anyone else's pick changes nothing and redraws the tab."""
    await ack()
    user_id = str((body.get("user") or {}).get("id") or "")
    if not user_id:
        return
    team_id = adapter._event_team_id({}, body)
    if not enabled():
        await publish_off(adapter, team_id, user_id)
        return
    level = str((action.get("selected_option") or {}).get("value") or "")
    note = ""
    if not await _authorized(adapter, user_id, team_id):
        logger.warning("slack_dial_home: ignoring a dial pick by %s, who is not allowed", user_id)
    elif level not in LEVEL_VALUES:
        logger.warning("slack_dial_home: ignoring a dial pick of unknown level %r by %s", level, user_id)
    else:
        admin = await is_admin(adapter, user_id)
        if admin is True:
            if read_state()["level"] != level:
                try:
                    write_level(level, user_id)
                    logger.info("slack_dial_home: dial set to %s by %s", level, user_id)
                except (UnreadableRecord, OSError) as exc:
                    logger.warning("slack_dial_home: not recording %s by %s: %s", level, user_id, exc)
                    note = NOT_SAVED
        else:
            logger.warning("slack_dial_home: ignoring a dial pick of %s by %s, who is not an install admin", level, user_id)
            note = READ_ONLY if admin is False else NOT_CHECKED
    await publish(adapter, team_id, user_id, note)


def register(adapter: Any) -> None:
    """Add the level listener to ``adapter._app``."""

    async def _on_level(ack, body, action):
        await on_level(adapter, ack, body, action)

    adapter._app.action(LEVEL_ACTION_ID)(_on_level)
    logger.info("slack_dial_home: dial level handler registered")

"""The autonomy dial's state and its three actions, for session-kv under `spec.mode: next`.

The dial is how an install admin says how far the agent may go: Explain
(changes nothing), Propose fixes (opens pull requests, people merge) or
Auto-merge proven fixes (the customer's pipeline may merge a kind of fix that
has earned it). The A2A gateway draws it on the Slack Home tab from the board
snapshot and sends a tap back as a board action; it never writes state. This
module is both halves on session-kv's side:

- :func:`snapshot_section` is the board snapshot's ``dial`` section: the level,
  who set it and when, the admin list, and per kind of fix and managed
  repository the clean merges it has earned (``clean_merges``) and whether its
  rules-file pull request is off, pending or merged. When the post-merge
  check steps an auto-merging kind down (:func:`adopt_step_downs`), the row
  follows the step-down's rules pull request as it would a Stop's.
- :func:`handle` answers ``dial.level``, ``dial.request`` and ``dial.stop``.
  It checks the switch and the admin list itself (the gateway's check only
  decides what to draw), records the change, reports a level to the
  credential broker (``POST /v1/dial``, which applies it to every write at
  once), and runs the per-kind switch in the shell sandbox
  (``automerge_switch.py``), which opens or closes the pull request to the
  repository's ``.kube-agents/automation.yaml``. Nothing merges here.

The record is one JSON file on the agent's volume, :data:`STATE_NAME` under
``$HERMES_HOME``: ``{level, set_by, set_at, kinds}``, where ``kinds`` maps a
kind of fix to its repositories (by ``clean_merges.repo_key``) and each to the
switch's last outcome there. A missing or unreadable file is
:data:`DEFAULT_LEVEL`, what the agent does without a dial. A write changes only
its own keys, never overwrites a file it cannot read, and is atomic, under a
lock shared by every writer in the process.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import clean_merges
import kubeagents_switches
from fix_recipes import registry

logger = logging.getLogger(__name__)
# The credential broker is an in-cluster Service: a proxy from the environment
# must not get the level report and its bearer token, and urllib does not leave
# out a cluster host by itself.
_BROKER_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

STATE_NAME = "kube-agents/dial.json"
#: The managed .env key (``KUBEAGENTS_DIAL_ADMINS``), which the operator pins.
ADMINS_NAME = "DIAL_ADMINS"
ADMINS_NONE = "none"
MEMBER_ID = re.compile(r"[UW][A-Z0-9]{2,20}")

LEVEL_EXPLAIN = "explain"
LEVEL_PROPOSE = "propose"
LEVEL_AUTOMERGE = "automerge"
LEVELS = (LEVEL_EXPLAIN, LEVEL_PROPOSE, LEVEL_AUTOMERGE)
LEVEL_TITLES = {LEVEL_EXPLAIN: "Explain", LEVEL_PROPOSE: "Propose fixes", LEVEL_AUTOMERGE: "Auto-merge proven fixes"}
DEFAULT_LEVEL = LEVEL_PROPOSE

#: ``(kind, title, environment)`` for each kind of fix the dial lists (``fix_recipes/registry.py``).
FIX_KINDS = tuple((recipe.kind, recipe.title, recipe.dial_environment) for recipe in registry.recipes())

ACTION_LEVEL = "dial.level"
ACTION_REQUEST = "dial.request"
ACTION_STOP = "dial.stop"
ACTIONS = frozenset({ACTION_LEVEL, ACTION_REQUEST, ACTION_STOP})

STATE_OFF = "off"
STATE_PENDING = "pending"
STATE_ON = "on"
# What automerge_switch.py answers per repository.
SWITCH_PROPOSED = "proposed"
SWITCH_BLOCKED = "blocked"

SWITCH_SCRIPT = "/opt/defaults/scripts/automerge_switch.py"
SWITCH_TIMEOUT_SECONDS = 300
CHECK_TIMEOUT_SECONDS = 20
CHECK_EVERY_SECONDS = 60
BROKER_PATH = "/v1/dial"
BROKER_TIMEOUT_SECONDS = 5

OFF = "The autonomy dial is off on this install."
NOT_ADMIN = "Only an install admin can change this."
NOT_SAVED = "Not saved: the stored setting could not be read or written."
NOT_REPORTED = "Saved. The broker has not confirmed it yet; session-kv reports it again every minute."
NEEDS_AUTOMERGE = "Pick Auto-merge proven fixes first; auto-merge is requested only there."
NOT_AT_EXPLAIN = "At Explain the agent opens no pull request; pick Propose fixes to change the rules file."
NOT_MANAGED = "That repository is not one this install manages."
UNKNOWN_KIND = "That kind of fix is not on the dial."
BUSY = "A change to this switch is already running."
SWITCH_FAILED = "The switch did not run: {reason}"
OPENING = "Opening the rules pull request for {repo}; the row shows it once it is open."
CLOSING = "Changing the rules file for {repo}; the row shows the result."
STEPPING_DOWN = "Stepping down: {fix} was still broken after it merged."
STEPPED_DOWN = "Stepped down: {fix} was still broken after it merged."
STEP_DOWN_OPEN = "A step-down's rules pull request is open for this kind; merge or close it first."
#: The post-merge record of a fix the step-down acted on, and where it names the rules pull request.
STEPPED_DOWN_FOLLOWUP = "stepped-down"
_PULL_NUMBER = re.compile(r"/(?:pull|pulls|merge_requests|pull-requests)/(\d+)/?$")

_write_lock = threading.Lock()
_busy: set[tuple[str, str]] = set()
_busy_lock = threading.Lock()
_last_checked: dict[str, float] = {}
_check_lock = threading.Lock()
_checking = False
_reporter_started = False
REPORT_EVERY_SECONDS = 60


class UnreadableRecord(Exception):
    """The record exists but is not a JSON object."""


def enabled() -> bool:
    """Whether ``KUBEAGENTS_DIAL`` is ``on``, read as every switch is (``kubeagents_switches``)."""
    return kubeagents_switches.switch("DIAL") == "on"


def admins() -> list[str]:
    """The pinned admin list: member ids only; ``none``, unset or nothing valid is nobody."""
    raw = kubeagents_switches._managed(ADMINS_NAME)
    if raw is None:
        raw = os.environ.get(f"KUBEAGENTS_{ADMINS_NAME}", "")
    if raw.strip().lower() == ADMINS_NONE:
        return []
    return [part.strip() for part in raw.split(",") if MEMBER_ID.fullmatch(part.strip())]


def state_path() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "/opt/data") / STATE_NAME


def _read_raw() -> dict:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise UnreadableRecord(str(exc)) from exc
    if not isinstance(raw, dict):
        raise UnreadableRecord(f"a {type(raw).__name__}, not an object")
    return raw


def read_state() -> dict:
    """The record, as the snapshot shows it: the default level and nothing else when unreadable."""
    state: dict[str, Any] = {"level": DEFAULT_LEVEL, "set_by": "", "set_at": 0, "kinds": {}}
    try:
        raw = _read_raw()
    except UnreadableRecord as exc:
        logger.warning("dial: could not read %s; showing %s: %s", state_path(), DEFAULT_LEVEL, exc)
        return state
    if raw.get("level") in LEVELS:
        state["level"] = raw["level"]
        set_by = str(raw.get("set_by") or "")
        state["set_by"] = set_by if MEMBER_ID.fullmatch(set_by) else ""
        set_at = raw.get("set_at")
        state["set_at"] = set_at if type(set_at) is int and set_at > 0 else 0
    if isinstance(raw.get("kinds"), dict):
        state["kinds"] = {
            str(kind): {str(key): entry for key, entry in repos.items() if isinstance(entry, dict)}
            for kind, repos in raw["kinds"].items()
            if isinstance(repos, dict)
        }
    return state


def _write(change: Callable[[dict], None]) -> dict:
    with _write_lock:
        state = _read_raw()
        change(state)
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".dial-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return state


def write_level(level: str, user: str) -> dict:
    return _write(lambda state: state.update(level=level, set_by=user, set_at=int(time.time())))


def write_repo(kind: str, key: str, entry: dict) -> dict:
    """Record the switch's outcome for one kind of fix in one repository, keeping every other key."""

    def change(state: dict) -> None:
        kinds = state.get("kinds") if isinstance(state.get("kinds"), dict) else {}
        repos = kinds.get(kind) if isinstance(kinds.get(kind), dict) else {}
        repos[key] = entry
        kinds[kind] = repos
        state["kinds"] = kinds

    return _write(change)


def managed_repos() -> list[str]:
    """The install's managed repository urls, from the mounted GitOps state (no kubectl)."""
    import gitops_workspace

    return [entry["url"] for entry in gitops_workspace.mounted_repo_entries(gitops_workspace.MANAGED_REPOS_KEY)]


def _display(url: str) -> str:
    key = clean_merges.repo_key(url)
    return key.split("/", 1)[1] if "/" in key else url


def _repo_state(entry: dict) -> tuple[str, dict | None]:
    """``(off|pending|on, pending_pr)`` for one repository's recorded outcome."""
    state = entry.get("state")
    if state == SWITCH_PROPOSED and entry.get("pr"):
        number = entry.get("number")
        return STATE_PENDING, {"number": number if type(number) is int else 0, "url": str(entry["pr"])}
    if state == STATE_ON and entry.get("on") is True:
        return STATE_ON, None
    return STATE_OFF, None


def step_downs(session_db: Any) -> dict[tuple[str, str, str], dict]:
    """``{(repo_key, kind, environment): {merged_at, fix, pr, number}}``, the newest step-down of each.

    A step-down is a still-broken fix whose post-merge follow-up opened a deny rule's pull
    request (``followup_detail.stepDown``). A follow-up that refused the rule, or one a person
    merged, opens none and is not one. No table, or one missing a column, is none.
    """
    try:
        rows = session_db.execute(
            f"SELECT repo, number, kind, environment, merged_at, followup_detail FROM {clean_merges.RECORDS_TABLE} "
            "WHERE followup_state = ?", (STEPPED_DOWN_FOLLOWUP,)
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — no step-downs rather than no board
        logger.debug("dial: could not read step-downs: %s", exc)
        return {}
    found: dict[tuple[str, str, str], dict] = {}
    for repo, number, kind, environment, merged_at, detail in rows:
        key, merged = clean_merges.repo_key(repo), clean_merges._parse_time(merged_at)
        try:
            pr = json.loads(detail or "{}").get("stepDown")
        except (ValueError, AttributeError):
            pr = None
        match = _PULL_NUMBER.search(pr) if isinstance(pr, str) else None
        if not key or not isinstance(kind, str) or merged is None or match is None:
            continue
        group = (key, kind, environment.strip().lower() if isinstance(environment, str) else "")
        if group not in found or merged > found[group]["merged_at"]:
            found[group] = {"merged_at": merged, "fix": number if type(number) is int else 0, "pr": pr,
                            "number": int(match.group(1))}
    return found


def adopt_step_downs(session_db: Any, state: dict) -> dict:
    """Record each step-down of a kind that auto-merges here as its pending rules pull request.

    Only a fix merged after auto-merge was requested counts, so a new request starts over; an
    entry with no ``set_at`` is left alone. The entry then settles as a Stop's does
    (:func:`refresh_pending`): off once the deny rule merges, back on if it is closed. Returns the
    record as it stands after.
    """
    found = step_downs(session_db)
    due = []
    for kind, _title, environment in FIX_KINDS:
        for key, entry in state["kinds"].get(kind, {}).items():
            down = found.get((key, kind, environment))
            set_at = entry.get("set_at")
            if (down is None or _repo_state(entry)[0] != STATE_ON or type(set_at) is not int
                    or down["merged_at"].timestamp() <= set_at):
                continue
            due.append((kind, key, entry, down))
    if not due:
        return state

    def change(raw: dict) -> None:
        for kind, key, entry, down in due:
            repos = (raw.get("kinds") or {}).get(kind) or {}
            # Only the entry this read saw: a tap since then decides.
            if repos.get(key) != entry:
                continue
            # A closed rules pull request puts the kind back on as of this fix, so the same step-down
            # is not taken up again; only a later one is.
            merged = math.ceil(down["merged_at"].timestamp())
            repos[key] = {"on": False, "state": SWITCH_PROPOSED, "url": entry.get("url"), "pr": down["pr"],
                          "number": down["number"], "stepped_down": down["fix"], "set_at": merged,
                          "before": {"on": True, "state": STATE_ON, "set_at": merged}}

    try:
        _write(change)
    except (UnreadableRecord, OSError) as exc:
        logger.warning("dial: not recording a step-down: %s", exc)
        return state
    return read_state()


def _step_down_note(entry: dict, switch: str) -> str:
    """The row's note while a step-down's rules pull request is open, and once it merged."""
    fix = entry.get("stepped_down")
    if type(fix) is not int or switch == STATE_ON:
        return ""
    text = f"PR #{fix}" if fix > 0 else "a fix"
    return (STEPPING_DOWN if switch == STATE_PENDING else STEPPED_DOWN).format(fix=text)


def snapshot_section(session_db: Any, now: datetime | None = None, repos: list[str] | None = None) -> dict:
    """The board snapshot's ``dial`` section. Only ``{"enabled": false}`` while the switch is off."""
    if not enabled():
        return {"enabled": False}
    now = now or datetime.now(timezone.utc)
    state = adopt_step_downs(session_db, read_state())
    try:
        urls = managed_repos() if repos is None else repos
    except Exception as exc:  # noqa: BLE001 — no repositories rather than no board
        logger.warning("dial: could not read the managed repositories: %s", exc)
        urls = []
    try:
        counts = clean_merges.count(clean_merges.read_rows(session_db), now)
    except Exception as exc:  # noqa: BLE001 — zero progress rather than no board
        logger.warning("dial: could not count clean merges: %s", exc)
        counts = {}
    kinds = []
    for kind, title, environment in FIX_KINDS:
        recorded = state["kinds"].get(kind, {})
        rows = []
        for url in urls:
            key = clean_merges.repo_key(url)
            if not key:
                continue
            earned = counts.get((key, kind, environment), {})
            switch, pending = _repo_state(recorded.get(key, {}))
            row = {
                "repo": _display(url),
                "repo_key": key,
                "clean": int(earned.get("clean", 0)),
                "needed": clean_merges.CLEAN_MERGES_NEEDED,
                "days": int(earned.get("days", 0)),
                "days_needed": clean_merges.DAYS_NEEDED,
                "ready": bool(earned.get("ready", False)),
                "state": switch,
            }
            if pending is not None:
                row["pending_pr"] = pending
            note = recorded.get(key, {}).get("note") or _step_down_note(recorded.get(key, {}), switch)
            if isinstance(note, str) and note:
                row["note"] = note
            rows.append(row)
        kinds.append({"kind": kind, "title": title, "environment": environment, "repos": rows})
    refresh_pending_async(state)
    return {
        "enabled": True,
        "level": state["level"],
        "set_by": state["set_by"],
        "set_at": state["set_at"],
        "admins": admins(),
        "kinds": kinds,
    }


# --------------------------------------------------------------------------
# The broker and the sandbox
# --------------------------------------------------------------------------


def broker_url() -> str:
    """The credential broker, from session-kv's own environment.

    That is the container's, as the operator rendered it: the entrypoint
    starts session-kv without reading the agent-writable PVC .env, which
    Hermes alone applies, so a line the agent wrote cannot point the gateway
    container's broker token somewhere else. The operator renders
    ``CREDENTIAL_PROXY_URL`` empty on the agent container, so the broker's
    address comes from ``KUBE_AGENTS_MCP_RELAY_URL`` (the broker's ``/v1/mcp``)
    unless ``CREDENTIAL_PROXY_URL`` is set.
    """
    url = os.environ.get("CREDENTIAL_PROXY_URL", "").strip().rstrip("/")
    if url:
        return url
    relay = os.environ.get("KUBE_AGENTS_MCP_RELAY_URL", "").strip().rstrip("/")
    return relay.removesuffix("/v1/mcp")


def report_level(level: str) -> bool:
    """Tell the credential broker the level; True once it confirmed. Never raises."""
    base, token_file = broker_url(), os.environ.get("CREDENTIAL_PROXY_TOKEN_FILE", "").strip()
    if not base or not token_file:
        logger.warning("dial: no broker URL or token file; the level %s is not reported", level)
        return False
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
        request = urllib.request.Request(
            base + BROKER_PATH,
            data=json.dumps({"level": level}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with _BROKER_OPENER.open(request, timeout=BROKER_TIMEOUT_SECONDS) as response:
            answer = json.loads(response.read() or b"{}")
    except Exception as exc:  # noqa: BLE001 — reported again on the next pick
        logger.warning("dial: could not report the level %s to the broker: %s", level, exc)
        return False
    return isinstance(answer, dict) and answer.get("level") == level


#: sandbox_exec logs in as hermes, which cannot write the agent's own VCS
#: tree; the switch clones under hermes's home instead.
SWITCH_ENV = {"KUBE_AGENTS_VCS_ROOT": "/home/hermes/kube-agents-vcs"}


def run_switch(argv: list[str], timeout: float) -> Any:
    import sandbox_exec

    return sandbox_exec.run(argv, timeout=timeout, remote_env=SWITCH_ENV)


def _switch_results(done: Any) -> list[dict]:
    try:
        results = json.loads(done.stdout or "")
    except ValueError:
        results = None
    if done.returncode != 0 or not isinstance(results, list):
        reason = results[0].get("reason") if isinstance(results, list) and results and isinstance(results[0], dict) else ""
        raise ValueError(str(reason or (done.stderr or "").strip()[:300] or "no answer"))
    return [r for r in results if isinstance(r, dict)]


def refresh_pending_async(state: dict) -> None:
    """Ask the forge, off the request's thread, about pending rules pull requests; record the settled."""
    pending = [
        (kind, key, entry)
        for kind, repos in state["kinds"].items()
        for key, entry in repos.items()
        if entry.get("state") == SWITCH_PROPOSED and type(entry.get("number")) is int and entry.get("url")
    ]
    now = time.monotonic()
    global _checking
    # One lock block: an entry is stamped as checked only when its check starts.
    with _check_lock:
        if _checking:
            return
        due = [p for p in pending if now - _last_checked.get(p[1] + p[0], -CHECK_EVERY_SECONDS) >= CHECK_EVERY_SECONDS]
        if not due:
            return
        for kind, key, _entry in due:
            _last_checked[key + kind] = now
        _checking = True
    _spawn(_refresh_then_clear, due)


def _refresh_then_clear(due: list[tuple[str, str, dict]]) -> None:
    global _checking
    try:
        refresh_pending(due)
    finally:
        with _check_lock:
            _checking = False


def _spawn(target: Callable[..., None], *args: Any) -> None:
    """Run ``target`` off the request's thread (the tests run it in place)."""
    threading.Thread(target=target, args=args, name="dial-work", daemon=True).start()


def report_recorded() -> bool:
    """Report the recorded level (the default when there is none). An unreadable record is not reported."""
    try:
        raw = _read_raw()
    except UnreadableRecord as exc:
        logger.warning("dial: not reporting the level; %s cannot be read: %s", state_path(), exc)
        return False
    return report_level(raw.get("level") if raw.get("level") in LEVELS else DEFAULT_LEVEL)


def start_reporter() -> None:
    """Report the level every minute while the switch is on, once per process.

    The broker treats an install that has not reported as Explain, and loses
    what it heard when its pod restarts; this keeps it on the dial's level.
    The thread starts whatever the switch says and reads it each minute, so
    turning the dial on later needs no restart.
    """
    global _reporter_started
    with _check_lock:
        if _reporter_started:
            return
        _reporter_started = True

    def loop() -> None:
        while True:
            if enabled():
                report_recorded()
            time.sleep(REPORT_EVERY_SECONDS)

    threading.Thread(target=loop, name="dial-report", daemon=True).start()


def refresh_pending(due: list[tuple[str, str, dict]]) -> None:
    for kind, key, entry in due:
        environment = next((env for k, _t, env in FIX_KINDS if k == kind), "")
        argv = ["python3", SWITCH_SCRIPT, "--kind", kind, "--environment", environment, "--check",
                "--repo", f"{entry['url']}#{entry['number']}"]
        try:
            results = _switch_results(run_switch(argv, CHECK_TIMEOUT_SECONDS))
        except Exception as exc:  # noqa: BLE001 — still pending; asked again later
            logger.warning("dial: could not check %s's rules pull request in %s: %s", kind, key, exc)
            continue
        answer = next((r for r in results if r.get("state") in ("merged", "closed")), None)
        if answer is None:
            continue
        number = entry["number"]

        def settle(state: dict, kind: str = kind, key: str = key, number: int = number, merged: bool = answer["state"] == "merged") -> None:
            repos = (state.get("kinds") or {}).get(kind) or {}
            current = repos.get(key)
            # A click since the check decides; this answer is about its predecessor.
            if not isinstance(current, dict) or current.get("state") != SWITCH_PROPOSED or current.get("number") != number:
                return
            wanted_on = current.get("on") is True
            if merged:
                repos[key] = {**current, "state": STATE_ON if wanted_on else STATE_OFF}
            else:
                before = current.get("before") if isinstance(current.get("before"), dict) else {"on": not wanted_on}
                was_on = before.get("on") is True and before.get("state", STATE_ON) == STATE_ON
                repos[key] = {"on": was_on, "state": STATE_ON if was_on else STATE_OFF, "url": current.get("url")}
                # The request's time travels back with it, so a later step-down still counts against it.
                if was_on and type(before.get("set_at")) is int:
                    repos[key]["set_at"] = before["set_at"]

        try:
            _write(settle)
        except (UnreadableRecord, OSError) as exc:
            logger.warning("dial: not recording %s's settled rules pull request in %s: %s", kind, key, exc)


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


def _answer(action: str, ok: bool, message: str) -> dict:
    return {"action": action, "ok": ok, "message": message}


def handle(action: str, payload: dict) -> dict:
    """One dial action from the board route: ``{action, ok, message}``. Never raises for a bad request."""
    if not enabled():
        return _answer(action, False, OFF)
    user = str(payload.get("user") or "").strip()
    if not user or user not in admins():
        logger.warning("dial: ignoring %s by %r, who is not an install admin", action, user)
        return _answer(action, False, NOT_ADMIN)
    if action == ACTION_LEVEL:
        return _set_level(user, str(payload.get("level") or "").strip().lower())
    return _switch(action, user, payload)


def _set_level(user: str, level: str) -> dict:
    if level not in LEVELS:
        return _answer(ACTION_LEVEL, False, f"level must be one of {', '.join(LEVELS)}")
    try:
        write_level(level, user)
    except (UnreadableRecord, OSError) as exc:
        logger.warning("dial: not recording %s by %s: %s", level, user, exc)
        return _answer(ACTION_LEVEL, False, NOT_SAVED)
    logger.info("dial: level set to %s by %s", level, user)
    confirmed = report_level(level)
    answer = _answer(ACTION_LEVEL, True, f"Set to {LEVEL_TITLES[level]}." + ("" if confirmed else " " + NOT_REPORTED))
    # Whether the broker confirmed it before this answer, for a caller that
    # reads the broker's write mode straight after (the dial eval).
    answer["reported"] = confirmed
    return answer


def _switch(action: str, user: str, payload: dict) -> dict:
    kind = str(payload.get("kind") or "").strip()
    environment = str(payload.get("environment") or "").strip().lower()
    key = str(payload.get("repo_key") or "").strip().lower()
    if not any(kind == k and environment == env for k, _t, env in FIX_KINDS):
        return _answer(action, False, UNKNOWN_KIND)
    try:
        urls = {clean_merges.repo_key(url): url for url in managed_repos()}
    except Exception as exc:  # noqa: BLE001 — refused, said why; the detail stays in the log
        logger.warning("dial: could not read the managed repositories for %s: %s", action, exc)
        return _answer(action, False, SWITCH_FAILED.format(reason="the managed repositories could not be read"))
    url = urls.get(key)
    if not key or url is None:
        return _answer(action, False, NOT_MANAGED)
    state = read_state()
    current = state["kinds"].get(kind, {}).get(key, {})
    # The step-down's branch is not the switch's, so a tap would open a second rules pull
    # request and stop following the first.
    if _repo_state(current)[0] == STATE_PENDING and type(current.get("stepped_down")) is int:
        return _answer(action, False, STEP_DOWN_OPEN)
    turning_on = action == ACTION_REQUEST
    if turning_on and state["level"] != LEVEL_AUTOMERGE:
        return _answer(action, False, NEEDS_AUTOMERGE)
    if state["level"] == LEVEL_EXPLAIN:
        return _answer(action, False, NOT_AT_EXPLAIN)
    with _busy_lock:
        if (kind, key) in _busy:
            return _answer(action, False, BUSY)
        _busy.add((kind, key))
    try:
        _spawn(_run_switch, action, user, kind, environment, key, url)
    except BaseException:
        with _busy_lock:
            _busy.discard((kind, key))
        raise
    return _answer(action, True, (OPENING if turning_on else CLOSING).format(repo=_display(url)))


def _run_switch(action: str, user: str, kind: str, environment: str, key: str, url: str) -> None:
    """The switch for one repository, off the request's thread (the bridge waits 28 s; a run can take longer)."""
    turning_on = action == ACTION_REQUEST
    try:
        with _check_lock:
            _last_checked[key + kind] = time.monotonic()
        previous = read_state()["kinds"].get(kind, {}).get(key, {})
        # Settle the last tap's pull request first: one that merged or closed
        # while pending decides what "before" is, and the switch only closes an
        # open one.
        if previous.get("state") == SWITCH_PROPOSED and type(previous.get("number")) is int and previous.get("url"):
            refresh_pending([(kind, key, previous)])
            previous = read_state()["kinds"].get(kind, {}).get(key, {})
        number = previous.get("number")
        pending = previous.get("state") == SWITCH_PROPOSED and type(number) is int and number > 0
        target = f"{url}#{number}" if pending else url
        argv = ["python3", SWITCH_SCRIPT, "--kind", kind, "--environment", environment,
                "--state", "on" if turning_on else "off", "--repo", target]
        try:
            results = _switch_results(run_switch(argv, SWITCH_TIMEOUT_SECONDS))
        except Exception as exc:  # noqa: BLE001 — shown on the row, nothing else recorded
            logger.warning("dial: the %s switch for %s failed: %s", kind, key, exc)
            _note(kind, key, previous, SWITCH_FAILED.format(reason=str(exc) or type(exc).__name__))
            return
        result = next((r for r in results if clean_merges.repo_key(r.get("repo")) == key), results[0] if results else {})
        outcome = result.get("state")
        if outcome == SWITCH_BLOCKED:
            _note(kind, key, previous, f"Not changed: {result.get('reason') or 'the switch refused'}.")
            return
        before = previous.get("before") if pending and isinstance(previous.get("before"), dict) else {
            "on": previous.get("on") is True, "state": previous.get("state") or STATE_OFF,
            **({"set_at": previous["set_at"]} if type(previous.get("set_at")) is int else {})}
        entry = {
            "on": turning_on,
            "state": outcome,
            "url": url,
            "set_by": user,
            "set_at": int(time.time()),
            "before": before,
            **{k: result[k] for k in ("pr", "number", "reason") if k in result},
        }
        try:
            write_repo(kind, key, entry)
        except (UnreadableRecord, OSError) as exc:
            logger.warning("dial: not recording the %s switch for %s: %s", kind, key, exc)
            return
        logger.info("dial: %s %s in %s by %s: %s", action, kind, key, user, outcome)
    finally:
        with _busy_lock:
            _busy.discard((kind, key))


def _note(kind: str, key: str, previous: dict, note: str) -> None:
    """Keep the repository's entry as it was, with why the last tap changed nothing."""
    try:
        write_repo(kind, key, {**previous, "note": note})
    except (UnreadableRecord, OSError) as exc:
        logger.warning("dial: not recording a note for %s in %s: %s", kind, key, exc)

"""A structured ``rightsizing`` field on ``kanban_complete``, checked and rendered by the gate.

Installed into the image at ``/opt/hermes/tools/kanban_rightsizing_checks.py``.
Importing it puts :func:`rightsizing_rule` first in
``kanban_verified_claims.RULES``, so it runs inside that module's
``kanban_complete`` gate and shares its one refusal per card.
``deploy/docker/patches/apply_kanban_rightsizing_checks.py`` adds the import to
``tools/kanban_tools.py``, and the call that adds the parameter to
``KANBAN_COMPLETE_SCHEMA``.

Why
---
The Cluster Agent skill ``gke-rightsizing-checks`` asks a card to name, per
workload, the dependency that blocks a request cut (an HPA, Guaranteed QoS) and
to show five checks. The Platform Agent writes the skill's name in the body of
each Cluster Agent card it sends a request cut to. In the eval case
``cost-rightsizing-names-dependencies``, the Platform Agent's summary sometimes
dropped a dependency its card had reported (#3199, #3507). Reading the answer's
prose to catch that stayed heuristic in both directions (#3738), so the
dependency and the checks are a structured field instead.

What it does
------------
- **A card whose body names the skill** passes ``rightsizing``: one entry per
  workload, ``{workload, dependency_kind, dependency, checks}``, where
  ``dependency_kind`` is one of :data:`DEPENDENCY_KINDS` and ``checks`` holds
  one finding for each of :data:`CHECK_KEYS`. ``not_on_cluster`` is the
  fan-out leg whose cluster does not run the workload and needs no checks. A
  missing field, an unknown kind or a blank check is refused once. The gate
  stores the accepted entries in ``metadata.rightsizing``.
- **A Platform card whose request asks to cut requests or limits**
  (:data:`CUT_REQUEST`, read in its title and in the body above the
  report-format stanza)
  and that created no card is refused once when it passed no entries, so a
  Platform Agent that checked the cluster itself passes the field or
  delegates. A card that delegated is not refused: its cards' entries, if
  any, are rendered, and a where-is fan-out that found the workload nowhere
  is a complete answer. A wrong match costs one refusal; a missed one
  leaves the card as it was.
- **Any card** gets the accepted entries of its own and of its settled
  descendants (``kanban_verified_claims.descendant_card_ids``) rendered in
  ``result``, before the evidence section (:func:`render`), so a summary
  cannot drop a dependency a card below it reported: the gate writes those
  lines, not the model. Only a descendant's latest completed run counts. An
  entry whose line the text already holds (the front door quoting the
  Platform card's answer) is not rendered a second time.

A card that names no skill, asks for no cut and has no descendant entries
passes untouched.

Refusal
-------
The gate's: the first completion with a problem is refused with every rule's
reasons in one message, and the retry is accepted. On the retry the invalid
entries are dropped and the valid ones kept, so a rendered line is always one
the gate checked. A failed board read never blocks a completion: the card's
own entries are still checked, stored and rendered.
"""

from __future__ import annotations

import json
import logging
import re

try:
    from tools import kanban_verified_claims as gate
except ImportError:  # Unit tests import the patch modules flat.
    import kanban_verified_claims as gate

logger = logging.getLogger(__name__)

#: The Cluster Agent skill whose name in a card's body requires the field.
SKILL_NAME = "gke-rightsizing-checks"

#: The parameter ``kanban_complete`` takes, and the ``metadata`` key it fills.
PARAM = "rightsizing"
METADATA_KEY = "rightsizing"

#: What blocks or limits a request cut on a workload, as rendered. A fixed set,
#: so a summary line says the dependency in words the model cannot rephrase.
DEPENDENCY_KINDS = {
    "hpa": "HPA",
    "guaranteed_qos": "Guaranteed QoS",
    "pdb_or_pvc": "PDB or PVC",
    "shared_file": "shared file",
    "none": "none",
    "not_on_cluster": "not on this cluster",
}

#: The five checks the skill requires, in order, with the label each renders under.
CHECK_KEYS = {
    "hpa": "HPA or KEDA",
    "qos": "QoS and requests against limits",
    "sharing": "Objects sharing its pods or volumes",
    "source": "Files shared across clusters",
    "packing": "Per-node packing",
}

#: The kind that needs no checks: the workload is not on this card's cluster.
NO_CHECKS_KIND = "not_on_cluster"

#: The kinds a cut past :data:`MAX_SHOWN` drops first, last listed first.
DROP_FIRST = ("none", "not_on_cluster")

#: Limits on the entries. One entry renders at most seven fields of 200
#: characters plus its labels, about 1,550 characters, so twelve shown entries
#: stay under 19,000 of ``kanban_notifier.RESULT_LIMIT``'s 30,000. The rule runs
#: before the evidence rule, so a clipped result loses evidence first.
MAX_ENTRIES = 8
MAX_SHOWN = 12
MAX_FIELD_CHARS = 200

#: The order that picks one ``dependency_kind`` when several apply.
KIND_PRIORITY = ("hpa", "guaranteed_qos", "pdb_or_pvc", "shared_file")

HEADING = "**Rightsizing checks**"
CARD_SQL = "SELECT COALESCE(title, ''), COALESCE(body, ''), COALESCE(assignee, '') FROM tasks WHERE id = ?"

#: The Platform Agent's profile, as cards name their assignee.
PLATFORM_PROFILE = "platform"

#: ``kanban_report_format.FORMAT_MARKER``: the stanza from it down is the
#: image's, not the request's, so the request is read above it.
FORMAT_MARKER = "## Report format"

#: A request to cut a workload's requests or limits, or to resize a pool, in
#: the words a person uses. Read on a Platform card's title and request only,
#: never on an answer. No "right-sizing": the front door writes it into plain
#: fleet cost questions too.
CUT_VERB = r"(?:lower|reduc|cut|shrink|decreas|downsiz|trim)\w*"
#: "requests" and "limits" count only after a resource word, so a pull
#: request, request traffic or an API rate limit does not.
CUT_OBJECT = (
    r"(?:(?:cpu|memory|resource|container)\s+(?:and\s+(?:cpu|memory)\s+)?(?:requests?|limits?)"
    r"|machine[- ]types?|node[- ]counts?|pool sizes?)"
)
CUT_REQUEST = re.compile(
    rf"\b{CUT_VERB}\b[^.\n]{{0,60}}?\b{CUT_OBJECT}\b|\b{CUT_OBJECT}\b[^.\n]{{0,30}}?\b{CUT_VERB}\b",
    re.IGNORECASE,
)

PARAM_DESCRIPTION = (
    "Required on a card whose body names the gke-rightsizing-checks skill. One entry per workload or "
    "pool you checked: `workload`, `dependency_kind` (hpa, guaranteed_qos, pdb_or_pvc, shared_file, "
    "none, or not_on_cluster when this cluster does not run it), `dependency` (one sentence: what the "
    "dependency means for a cut), and `checks` with one finding each for hpa, qos, sharing, source and "
    "packing (not needed for not_on_cluster). If several kinds apply, pick the first of "
    f"{', '.join(KIND_PRIORITY)} and name the others in `dependency`. Each field is one line with no "
    "code fence (``` or ~~~). The tool checks each entry and renders them in `result`; an entry that fails is refused, "
    "and dropped on the retry."
)

PARAM_SCHEMA = {
    "type": "array",
    "maxItems": MAX_ENTRIES,
    "items": {
        "type": "object",
        "properties": {
            "workload": {"type": "string", "description": "The workload or pool, as its name."},
            "dependency_kind": {"type": "string", "enum": list(DEPENDENCY_KINDS)},
            "dependency": {"type": "string", "description": "One sentence: what the dependency means for a cut."},
            "checks": {
                "type": "object",
                "properties": {key: {"type": "string"} for key in CHECK_KEYS},
                "description": "One finding per check, with the reading behind it.",
            },
        },
        "required": ["workload", "dependency_kind", "dependency"],
    },
    "description": PARAM_DESCRIPTION,
}


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _check_entry(item: object) -> tuple[dict | None, str | None]:
    """Return ``(entry, None)`` or ``(None, reason)`` for one ``rightsizing`` item."""
    if not isinstance(item, dict):
        return None, "is not an object"
    for field in ("workload", "dependency_kind", "dependency"):
        if _blank(item.get(field)):
            return None, f"has no {field}"
    kind = item["dependency_kind"].strip()
    if kind not in DEPENDENCY_KINDS:
        return None, f"dependency_kind {kind!r} is not one of {', '.join(DEPENDENCY_KINDS)}"
    entry = {"workload": _one_line(item["workload"]), "dependency_kind": kind, "dependency": _one_line(item["dependency"])}
    if "`" in entry["workload"]:
        return None, "workload holds a backtick"
    checks = item.get("checks") if isinstance(item.get("checks"), dict) else {}
    if kind != NO_CHECKS_KIND:
        missing = [key for key in CHECK_KEYS if _blank(checks.get(key))]
        if missing:
            return None, f"checks has no finding for {', '.join(missing)}"
        entry["checks"] = {key: _one_line(checks[key]) for key in CHECK_KEYS}
    for text in [entry["workload"], entry["dependency"], *entry.get("checks", {}).values()]:
        if len(text) > MAX_FIELD_CHARS:
            return None, f"a field is longer than {MAX_FIELD_CHARS} characters"
        if gate.FENCE in text or gate.TILDE_FENCE in text:
            return None, "a field holds a code fence"
    return entry, None


def _line(entry: dict, card: bool = True) -> str:
    """The entry's first rendered line, with or without its ``(card …)``."""
    where = f" (card {entry['card']})" if card and entry.get("card") else ""
    return (
        f"- `{entry['workload']}`{where}: dependency: {DEPENDENCY_KINDS[entry['dependency_kind']]}. "
        f"{entry['dependency']}"
    )


def render(entries: list[dict], hidden: int = 0) -> str:
    """The section added to ``result``: one line per workload, its checks under it, and a count of any not shown."""
    parts = [HEADING]
    for entry in entries:
        parts.append(_line(entry))
        for key, label in CHECK_KEYS.items():
            if key in entry.get("checks", {}):
                parts.append(f"  - {label}: {entry['checks'][key]}")
    if hidden:
        parts.append(f"- {hidden} more entries not shown; the cards below this one hold them.")
    return "\n".join(parts)


def _cap(entries: list[dict]) -> list[dict]:
    """At most :data:`MAX_SHOWN` entries, in their order, dropping ``not_on_cluster`` and then ``none`` first."""
    rank = {kind: index + 1 for index, kind in enumerate(DROP_FIRST)}
    keep = sorted(range(len(entries)), key=lambda i: (rank.get(entries[i]["dependency_kind"], 0), i))[:MAX_SHOWN]
    return [entries[i] for i in sorted(keep)]


def descendant_entries(conn, task_id: object) -> list[dict]:
    """The entries the gate accepted on the latest completed run of each of ``task_id``'s descendants, with ``card``."""
    ids = gate.descendant_card_ids(conn, task_id)
    if not ids:
        return []
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT task_id, metadata FROM task_runs WHERE id IN "
        f"(SELECT MAX(id) FROM task_runs WHERE outcome = ? AND task_id IN ({marks}) GROUP BY task_id) ORDER BY task_id",
        (gate.COMPLETED_OUTCOME, *ids),
    ).fetchall()
    entries = []
    for card, raw in rows:
        try:
            stored = (json.loads(raw) if raw else {}).get(METADATA_KEY) or []
        except (ValueError, AttributeError):
            continue
        for item in stored if isinstance(stored, list) else []:
            entry, _reason = _check_entry(item)
            if entry is not None:
                entries.append({**entry, "card": str(card)})
    return entries


def asks_for_cut(title: str, body: str) -> bool:
    """Whether a card's title, or its request above the report-format stanza, asks to cut requests or limits."""
    return bool(CUT_REQUEST.search(f"{title}\n{body.split(FORMAT_MARKER, 1)[0]}"))


def _read_board(connect, task_id: str) -> tuple[bool, bool, list[dict]]:
    """Whether the card's body names the skill, whether it is a Platform card that asks for a cut and created no card, and its descendants' entries."""
    conn = connect()
    try:
        row = conn.execute(CARD_SQL, (task_id,)).fetchone()
        title, body, assignee = (str(row[0]), str(row[1]), str(row[2]).strip().lower()) if row else ("", "", "")
        cut = assignee == PLATFORM_PROFILE and asks_for_cut(title, body) and not gate.descendant_card_ids(conn, task_id)
        return SKILL_NAME in body, cut, descendant_entries(conn, task_id)
    finally:
        conn.close()


def rightsizing_rule(
    *, task_id: str, args: dict, result: object, metadata: object, retry: bool, redact, connect=None, **_
) -> tuple[list, object, object]:
    """The completion rule; see ``kanban_verified_claims.RULES`` for the contract.

    A ``rightsizing`` key the model put in ``metadata`` itself is always
    removed, so only entries this rule checked are stored.
    """
    if isinstance(metadata, dict) and METADATA_KEY in metadata:
        metadata = {key: value for key, value in metadata.items() if key != METADATA_KEY}
    triggered, cut, inherited = False, False, []
    if connect is not None:
        try:
            triggered, cut, inherited = _read_board(connect, task_id)
        except Exception as exc:  # noqa: BLE001 - a failed read must not block a completion
            logger.warning("[kanban] card %s: board not read for the rightsizing check: %r", task_id, exc)

    items = args.get(PARAM)
    items = items if isinstance(items, list) else ([] if items is None else [items])
    accepted, reasons = [], []
    for index, item in enumerate(items):
        if index >= MAX_ENTRIES:
            entry, reason = None, f"is past the limit of {MAX_ENTRIES} entries"
        else:
            entry, reason = _check_entry(item)
        if reason:
            reasons.append(f"{PARAM}[{index}]: {reason}")
        else:
            accepted.append(entry)
    if triggered and not accepted and not reasons and not inherited:
        reasons.append(
            f"this card names {SKILL_NAME}: pass `{PARAM}` with one entry per workload "
            "(workload, dependency_kind, dependency, and the five checks)"
        )
    elif cut and not accepted and not reasons:
        reasons.append(
            f"this request asks to cut requests or limits, and you created no card: send it to the "
            f"cluster's Cluster Agent card with {SKILL_NAME} in the body and wait for it, or pass "
            f"`{PARAM}` from the five checks you ran yourself (`not_on_cluster` for a cluster that does "
            "not run the workload)"
        )
    if reasons and not retry:
        return reasons, result, metadata
    for reason in reasons:
        logger.warning("[kanban] card %s: accepted on the retry with: %s", task_id, reason)

    stored = []
    for entry in accepted:
        safe = {**entry, "workload": redact(entry["workload"]), "dependency": redact(entry["dependency"])}
        if "checks" in entry:
            safe["checks"] = {key: redact(value) for key, value in entry["checks"].items()}
        stored.append(safe)
    body = str(result).rstrip() if result else ""
    shown, seen = [], set()
    for entry in stored + inherited:
        plain = _line(entry, card=False)
        # A line the text already quotes (the front door relaying the Platform
        # card's answer), or one already shown, is not rendered again.
        if plain in seen or plain in body or _line(entry) in body:
            continue
        seen.add(plain)
        shown.append(entry)
    hidden = max(0, len(shown) - MAX_SHOWN)
    if hidden:
        logger.warning("[kanban] card %s: %d rightsizing entries not shown past %d", task_id, hidden, MAX_SHOWN)
        shown = _cap(shown)
    if stored:
        if metadata is not None and not isinstance(metadata, dict):
            # Upstream's _require_dict_metadata refuses this next; nothing to add to.
            return [], result, metadata
        metadata = {**(metadata or {}), METADATA_KEY: stored}
    if not shown:
        return [], result, metadata
    section = render(shown, hidden)
    return [], (f"{body}\n\n{section}" if body else section), metadata


def apply_schema(schema: dict) -> dict:
    """Add the ``rightsizing`` parameter to ``KANBAN_COMPLETE_SCHEMA`` in place."""
    properties = schema["parameters"]["properties"]
    if PARAM in properties:
        raise RuntimeError(f"kanban_complete already has a {PARAM!r} parameter; re-derive this patch")
    properties[PARAM] = PARAM_SCHEMA
    return schema


if rightsizing_rule not in gate.RULES:
    # First, so the evidence rule adds its section after this one.
    gate.RULES.insert(0, rightsizing_rule)

"""Refuse, once, a rightsizing answer that drops its dependency lines or its checks.

Installed into the image at ``/opt/hermes/tools/kanban_rightsizing_checks.py``.
Importing it appends :func:`rightsizing_rule` to
``kanban_verified_claims.RULES``, so it runs inside that module's
``kanban_complete`` gate and shares its one refusal per card.
``deploy/docker/patches/apply_kanban_rightsizing_checks.py`` adds the import to
``tools/kanban_tools.py``.

Why
---
The Cluster Agent skill ``gke-rightsizing-checks`` asks a card to open its
``result`` with one line per workload naming the dependency that blocks a cut
(an HPA, Guaranteed QoS), and to show five checks. The Platform Agent writes the
skill's name in the body of each Cluster Agent card it sends a request cut to.
In the eval case ``cost-rightsizing-names-dependencies`` the Cluster Agent did
that, and the Platform Agent's fan-out summary still dropped a dependency in one
run out of three, three builds in a row (#3199, #3507). Prompt text did not hold
it, so this gate does (#3491).

What it checks
--------------
Only a card the skill was asked for is checked, or a card above one:

- **A card whose body names the skill.** Its ``result`` opens with at least one
  dependency line (``<workload>: <dependency>``, or ``no dependency found``)
  within its first :data:`OPENING_LINES` non-blank lines, and mentions each of
  the five checks (:data:`CHECKS`).
- **A card that created such cards** (``kanban_verified_claims.descendant_card_ids``).
  Its ``result`` carries every dependency line those cards opened with, word for
  word after Markdown emphasis and whitespace are folded.

A card the skill was not asked for, and a card that created none, pass
untouched. So does a card whose cluster does not run the workload (``NOT_HERE``):
a fan-out's not-found leg has no checks to show, and its line is not carried. Nothing is parsed out of free text to decide whether a card is a
rightsizing answer: the body naming the skill is the trigger.

Refusal
-------
The gate's: the first completion with a problem is refused with every rule's
reasons in one message, and the retry is accepted. On the retry this rule
changes nothing and only logs, so the stored ``result`` is the one the worker
wrote.
"""

from __future__ import annotations

import logging
import re

try:
    from tools import kanban_verified_claims as gate
except ImportError:  # Unit tests import the patch modules flat.
    import kanban_verified_claims as gate

logger = logging.getLogger(__name__)

#: The Cluster Agent skill whose name in a card's body turns this rule on.
SKILL_NAME = "gke-rightsizing-checks"

#: How far into a ``result`` the dependency lines may start. The skill puts them
#: first; a bold one-sentence answer above them is allowed.
OPENING_LINES = 8

#: A list marker in front of a line: ``-``, ``*``, ``+`` or ``1.``/``1)``.
LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

#: A Markdown link, ``[text](url)``, kept as its text: the report format links
#: every workload a card names.
MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")

#: ``<workload>: <dependency>``, on a line already folded and stripped of its
#: list marker: an optional kind word, an optional ``<namespace>/``, a
#: Kubernetes name, then a colon and text.
DEPENDENCY_LINE = re.compile(
    r"^(?:(?:deployment|statefulset|daemonset|cronjob|job|replicaset)\s+)?"
    r"(?:[a-z0-9][a-z0-9.-]*/)?([a-z0-9][a-z0-9.-]*[a-z0-9])\s*:\s+(\S.*)$"
)

#: Labels an answer puts before a colon that are not workloads: the checks'
#: names and the words each check uses, figures and metadata, and the answer's
#: section words. ``- HPA: none`` and ``cpu: 400m`` are not dependency lines.
LABEL_WORDS = frozenset(
    {"hpa", "keda", "qos", "pdb", "pvc", "helm", "argo", "argocd", "flux", "kustomize", "gitops",
     "limits", "requests", "selector", "selectors", "allocatable", "node", "nodes", "packing",
     "shared", "sharing", "source", "sources", "files", "objects", "other", "check", "checks",
     "proposal", "arithmetic", "result", "results", "finding", "findings", "cluster", "clusters",
     "note", "notes", "summary", "evidence", "cause", "fix", "status", "answer", "recommendation",
     "workload", "workloads", "cpu", "memory", "replicas", "namespace", "image", "verdict",
     "conclusion", "context", "impact", "deployment", "statefulset", "daemonset"}
)

#: What makes a line a dependency line: its text after the colon names a
#: dependency the skill's checks look for, or says there is none. A positive
#: test, so a ``word: value`` line that names no dependency never counts.
DEPENDENCY_TERM = re.compile(
    r"\b(?:hpas?|horizontalpodautoscalers?|horizontal pod autoscalers?|keda|scaledobjects?|autoscal\w*"
    r"|guaranteed|qos|burstable|requests? (?:equal|equals|match|matches|=|==)|limits?"
    r"|pdbs?|poddisruptionbudgets?|disruption budgets?|pvcs?|persistentvolumeclaims?|selectors?"
    r"|values files?|helm|argo ?cd|flux|config ?sync|kustomize|shared|no dependency found)\b"
)

#: A line made only of bold text: the report format's one-sentence opener. It
#: is neither a dependency line nor the end of the block.
BOLD_ONLY = re.compile(r"^\s*\*\*[^*].*\*\*\s*$")

#: A card that reports the workload is not on its cluster, with no dependency
#: line and no check: a fan-out's not-found leg. Both a negation and a cluster
#: or namespace noun on one line, so "HPA: not found" does not qualify.
NOT_HERE = re.compile(r"\b(not (?:on|in|found|present|running|exist)|(?:does|do|did)n't exist|no such|absent)\b")
CLUSTER_NOUN = re.compile(r"\b(?:clusters?|namespaces?)\b")

#: The five checks the skill requires, each as the words that name it.
CHECKS = {
    "HPA or KEDA": re.compile(r"\b(hpas?|horizontalpodautoscalers?|horizontal pod autoscalers?|keda|scaledobjects?)\b"),
    "QoS and requests against limits": re.compile(r"\b(qos|guaranteed|burstable|besteffort)\b"),
    "objects sharing its pods or volumes": re.compile(r"\b(pdb|poddisruptionbudget|disruption budget|pvc|persistentvolumeclaim|selector)s?\b"),
    "files shared across clusters": re.compile(r"\b(helm|argo ?cd|argocd|flux|config ?sync|configsync|kustomize|values files?|tracking-id)\b"),
    "per-node packing": re.compile(r"\b(allocatable|per node|per-node|packing|nodes? needed|machine types?)\b"),
}

#: The statuses of a card whose ``result`` is final.
SETTLED_STATUSES = ("done", "archived")

CARD_BODY_SQL = "SELECT COALESCE(body, '') FROM tasks WHERE id = ?"
CHILD_RESULT_SQL = "SELECT COALESCE(body, ''), COALESCE(result, ''), status FROM tasks WHERE id = ?"

MARKDOWN_NOISE = str.maketrans({"*": None, "_": None, "`": None})
#: A pipe table's cell border, read as a space, so a line carried into a table
#: row still matches.
TABLE_PIPE = str.maketrans({"|": " "})
WHITESPACE = re.compile(r"\s+")


def _fold(text: str) -> str:
    """Lower case, links kept as their text, no emphasis, code marks or table
    pipes, whitespace collapsed."""
    text = MARKDOWN_LINK.sub(r"\1", str(text))
    return WHITESPACE.sub(" ", text.translate(MARKDOWN_NOISE).translate(TABLE_PIPE)).strip().lower()


def _canonical(raw: str) -> str | None:
    """``name: dependency`` for a dependency line, else None.

    Folded, its list marker, kind word and namespace dropped, so the same line
    written as a bullet, a numbered item or a linked name reads the same.
    """
    line = _fold(LIST_MARKER.sub("", MARKDOWN_LINK.sub(r"\1", raw)))
    match = DEPENDENCY_LINE.match(line)
    if not match or match.group(1) in LABEL_WORDS or not DEPENDENCY_TERM.search(match.group(2)):
        return None
    return f"{match.group(1)}: {match.group(2)}"


def dependency_lines(result: object) -> list[str]:
    """The block of dependency lines a ``result`` opens with, canonical.

    The block starts at the first dependency line within the first
    :data:`OPENING_LINES` non-blank lines. Blank lines and bold-only lines
    inside it are skipped, so one workload per paragraph is still one block;
    it ends at the first other line (a check line, a paragraph of prose).
    """
    block: list[str] = []
    seen = 0
    for raw in str(result or "").splitlines():
        if not raw.strip() or BOLD_ONLY.match(raw):
            continue
        line = _canonical(raw)
        if block:
            if line is None:
                break
            block.append(line)
            continue
        seen += 1
        if seen > OPENING_LINES:
            break
        if line is not None:
            block.append(line)
    return block


def _body(result: object) -> str:
    """``result`` without the evidence section the verified-claims rule appends,
    whose commands would otherwise name every check for it."""
    text = str(result or "")
    return text.split(gate.EVIDENCE_HEADING, 1)[0]


def missing_checks(result: object) -> list[str]:
    """The checks from :data:`CHECKS` that ``result``'s body never names."""
    text = _fold(_body(result))
    return [name for name, pattern in CHECKS.items() if not pattern.search(text)]


def _not_here(result: object) -> bool:
    """The card reports only that the workload is not on its cluster: no
    dependency line, no check named, and a line that says it is not there."""
    if dependency_lines(result) or len(missing_checks(result)) < len(CHECKS):
        return False
    opening = [_fold(raw) for raw in str(result or "").splitlines() if raw.strip()][:OPENING_LINES]
    return any(NOT_HERE.search(line) and CLUSTER_NOUN.search(line) for line in opening)


def _own_reasons(result: object) -> list[str]:
    if _not_here(result):
        return []
    reasons = []
    if not dependency_lines(result):
        reasons.append(
            f"{SKILL_NAME}: open `result` with one line per workload, `<workload>: <dependency>` "
            "(or `<workload>: no dependency found`), after at most one bold sentence"
        )
    missing = missing_checks(result)
    if missing:
        reasons.append(f"{SKILL_NAME}: `result` does not show these checks: {', '.join(missing)}")
    return reasons


def _parent_text(result: object) -> str:
    """The parent's text as a carried line is looked for in it: each line with
    its list marker dropped, plus, for a table row, each pair of neighbouring
    cells joined as ``cell: cell``, so a two-column row carries a line too."""
    parts = []
    for line in str(result or "").splitlines():
        parts.append(_fold(LIST_MARKER.sub("", line)))
        if line.count("|") >= 2:
            cells = [_fold(MARKDOWN_LINK.sub(r"\1", c)) for c in line.strip().strip("|").split("|")]
            parts.extend(f"{a}: {b}" for a, b in zip(cells, cells[1:]) if a and b)
    return " \n ".join(parts)


def _child_reasons(conn, task_id: str, result: object) -> list[str]:
    folded = _parent_text(result)
    reasons = []
    for child in gate.descendant_card_ids(conn, task_id):
        row = conn.execute(CHILD_RESULT_SQL, (child,)).fetchone()
        if not row or SKILL_NAME not in str(row[0]) or row[2] not in SETTLED_STATUSES:
            continue
        for line in dependency_lines(row[1]):
            if line not in folded:
                reasons.append(f"card {child} reported `{line}`; carry that line into `result` word for word")
    return reasons


def rightsizing_rule(
    *, task_id: str, result: object, metadata: object, retry: bool, connect=None, **_
) -> tuple[list, object, object]:
    """The completion rule; see ``kanban_verified_claims.RULES`` for the contract."""
    if connect is None:
        return [], result, metadata
    try:
        conn = connect()
        try:
            row = conn.execute(CARD_BODY_SQL, (task_id,)).fetchone()
            reasons = _own_reasons(result) if row and SKILL_NAME in str(row[0]) else []
            reasons += _child_reasons(conn, task_id, result)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - a failed read must not block a completion
        logger.warning("[kanban] card %s: rightsizing check not run: %r", task_id, exc)
        return [], result, metadata
    if retry:
        for reason in reasons:
            logger.warning("[kanban] card %s: accepted on the retry with: %s", task_id, reason)
        return [], result, metadata
    return reasons, result, metadata


if rightsizing_rule not in gate.RULES:
    gate.RULES.append(rightsizing_rule)

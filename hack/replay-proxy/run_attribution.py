"""Which graded eval run each request a proxy logged belongs to, and how that run was graded.

One build runs many cases and repetitions at once on one install
(examples/inference-replay/eval-design.md, "Telling the runs in a build
apart"). A request is tied to its run by the kanban cards its own conversation
works on, which each repetition's results.json names, or, with no card of its
own, by the eval run tag it carried (below).

A request's cards are read from its stored, scrubbed text (the proxy's
REPLAY_STORE_DIR), and only from two places that belong to its conversation:
the card a worker was started on (its first user message, `work kanban task
t_...`) and the cards the conversation created (the `task_id` a `kanban_create`
call returned). Card ids anywhere else are not evidence: system prompts quote
old cards, board listings show every run's cards, and Hermes' recent-work digest
lists the assignee's runs on other cards. A run's cards are the `task` its
worker trajectory entries carry. (A front door's request names no A2A task id,
so a run's A2A task is no evidence and is not read.)

A request also says which run it belongs to when the install tags it: the
agent sends `X-Kube-Agents-Eval-Run: <run>/<case>/<rep>` on its model requests
(deploy/docker/patches/eval_run_tag.py), the proxy logs it as `eval_run`, and
the harness writes the same tag to `eval_run.json` beside each repetition's
results.json (hack/ci-eval-pr.sh). The run id is minted per invocation, so the
tag tells a retried attempt of a case and repetition from the one bench-gate
graded. A tagged line belongs to exactly the graded repetition whose
eval_run.json holds that tag. A line with a card is still decided by its card,
and its tag must agree: when the tag names a graded run other than the card's,
or a different attempt, or a repetition not graded while the card's is, the
line is a disagreement and the answer it gave is not published. A line with no
card of its own (a front door's opening, the board reads before a worker's
first `kanban_create`) is decided by its tag alone; with no tag, or a tag whose
repetition has no eval_run.json to check it against, it is never published:
nothing else in the line says whose it is (Hermes' auxiliary calls, gateway
session pods and a night with the tags off log none). Only positive evidence
decides, so a line lost before it reached a segment (a spent budget, a pod
replaced before its last segment was copied, a last pass cut short, a pod that
came and went between passes) can only leave an answer unpublished, never
publish one.

Decisions, per logged request:
- `pass`: every run it belongs to was graded and passed;
- `fail`: it belongs to a run that was graded and did not pass;
- `pending`: its text is not all stored yet, or it names a card or tag no
  graded run has yet (its case is still running);
- `unattributed`: once every case is graded, no stored text, or a card or tag
  no run has; at any time, a tag of an attempt bench-gate did not grade;
- `untagged`: no card of its own and no tag that can be checked;
- `disagree`: its tag and its card name different runs.

A saved alternate (one distinct answer to one request) is decided by every
storable line whose logged `stored_answer_hash`, the hash of the answer as
stored, is the alternate's name, recorded or not: it is published when any of
them is attributed to a graded run, passing or failing (pass when every such
run passed, fail otherwise), and never while any of them is a disagreement
(Attribution.decide_alternate). A different answer to the same request is an
alternate of its own, decided by its own lines. An answer in the older layout
(one per request) is decided by its recording line, or, when that line is left
unattributed or untagged on the last call, by the other lines that sent the
same request and got the same answer (Attribution.decide_answer).

Only bench-gate's case-<name>.json says which attempt of a repetition was
graded (it names the run directory holding that attempt's eval_run.json, which
the harness copies there only once the attempt ends), so a line waits as
`pending` until its case is graded, though the verdict no longer matters.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

import schema as F

CARD_RE = re.compile(r"^t_[0-9a-f]{8}\Z")
# Hermes' first message to a worker (bench/kube_agents_bench/worker_trajectory.py reads the same).
WORKER_OPENING_RE = re.compile(r"\Awork kanban task (t_[0-9a-f]{8})\b")
CREATE_TOOL = "kanban_create"
CREATED_ID_KEY = "task_id"
# A results.json trajectory entry: the worker card it ran under.
TRAJECTORY_KEY = "trajectory"
TRAJECTORY_TASK_KEY = "task"
ROLE_KEY = "role"
USER_ROLE = "user"
ASSISTANT_ROLE = "assistant"
TOOL_ROLE = "tool"
CONTENT_KEY = "content"
TEXT_KEY = "text"
TOOL_CALLS_KEY = "tool_calls"
TOOL_CALL_ID_KEY = "tool_call_id"
FUNCTION_KEY = "function"
NAME_KEY = "name"
ID_KEY = "id"

PASS = "pass"
FAIL = "fail"
PENDING = "pending"
UNATTRIBUTED = "unattributed"
UNTAGGED = "untagged"
DISAGREE = "disagree"
DECISIONS = (PASS, FAIL, PENDING, UNATTRIBUTED, UNTAGGED, DISAGREE)
# A line or alternate a graded attempt produced: published whatever the verdict.
GRADED = (PASS, FAIL)

# The eval run tag (`<run>/<case>/<rep>`) as the proxy logs it in `eval_run`:
# replay_proxy.EVAL_RUN_RE, which deploy/docker/patches/test_eval_run_tag.py
# holds equal to the agent's.
EVAL_RUN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/[0-9]{1,6}")
EVAL_RUN_SEPARATOR = "/"
EVAL_RUN_PARTS = 3
# The harness's eval_run.json beside a repetition's results.json
# (bench/kube_agents_bench/harness.py, _eval_run_record; hack/ci-eval-pr.sh).
EVAL_RUN_FILE = "eval_run.json"
EVAL_RUN_TAG_KEY = "eval_run"
EVAL_RUN_ID_KEY = "run_id"
EVAL_RUN_CASE_KEY = "case_id"
EVAL_RUN_REP_KEY = "repetition"
# What a line's tag says about the graded runs (Attribution.tag_status).
TAG_NONE = "none"  # no well-formed tag
TAG_RUN = "run"  # exactly a graded repetition's tag
TAG_OTHER_ATTEMPT = "other_attempt"  # a graded repetition's case and rep, another run id
TAG_UNCHECKED = "unchecked"  # a graded repetition's case and rep, which has no eval_run.json
TAG_UNGRADED = "ungraded"  # no graded repetition has its case and rep yet

# bench-gate's case-<name>.json (bench/kube_agents_bench/scoring.py, CaseVerdict.to_dict):
# the case, its repetitions, and each one's index, outcome and run directory.
# The run directory is absolute: devops-bench resolves the run directory it
# prints (evalharness/reporter.py, new_run_dir), and hack/ci-eval-pr.sh hands
# that path to bench-gate as given.
CASE_FIELD = "case"
REPS_FIELD = "reps"
REP_INDEX_FIELD = "index"
REP_OUTCOME_FIELD = "outcome"
REP_RUN_DIR_FIELD = "run_dir"
# bench-gate's per-repetition outcome for a pass (bench/kube_agents_bench/scoring.py).
REP_OUTCOME_PASS = "pass"
REP_OUTCOME_FAIL = "fail"
CASE_JSON_PREFIX = "case-"
JSON_SUFFIX = ".json"
RESULTS_FILE = "results.json"
RUN_KEY_SEPARATOR = "#"
STORE_REQUESTS_DIR = os.path.join("store", "requests")
STORE_BLOBS_DIR = os.path.join("store", "blobs")
MESSAGES_KEY = "messages"
# The names the store writes: a request id (uuid4 hex) and a blob (sha256 hex).
SAFE_STORE_NAME = re.compile(r"^[0-9a-f]{16,64}\Z")
# An answer's identity in a history: [context, answer] (normalizer.answer_variant).
ANSWER_PAIR_LENGTH = 2
TEXT_ENCODING = "utf-8"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get(TEXT_KEY, "")) if isinstance(part, dict) else str(part) for part in content)
    return ""


@dataclass(frozen=True)
class Facts:
    """What attribution needs from one message: small enough to keep for every distinct one."""

    role: str = ""
    opening_card: str = ""
    create_call_ids: frozenset = frozenset()
    tool_call_id: str = ""
    created_card: str = ""


def message_facts(message: Any) -> Facts:
    if not isinstance(message, dict):
        return Facts()
    role = str(message.get(ROLE_KEY) or "")
    if role == USER_ROLE:
        match = WORKER_OPENING_RE.match(_text(message.get(CONTENT_KEY)).strip())
        return Facts(role, opening_card=match.group(1) if match else "")
    if role == ASSISTANT_ROLE:
        creates = set()
        for call in message.get(TOOL_CALLS_KEY) or []:
            function = call.get(FUNCTION_KEY) if isinstance(call, dict) else None
            if isinstance(function, dict) and function.get(NAME_KEY) == CREATE_TOOL and call.get(ID_KEY):
                creates.add(str(call[ID_KEY]))
        return Facts(role, create_call_ids=frozenset(creates))
    if role == TOOL_ROLE:
        created = ""
        try:
            body = json.loads(_text(message.get(CONTENT_KEY)))
        except ValueError:
            body = None
        card = body.get(CREATED_ID_KEY) if isinstance(body, dict) else None
        if isinstance(card, str) and CARD_RE.match(card):
            created = card
        return Facts(role, tool_call_id=str(message.get(TOOL_CALL_ID_KEY) or ""), created_card=created)
    return Facts(role)


def owned_from_facts(facts: Iterable[Facts]) -> Set[str]:
    """The cards a conversation works on: the one its worker was started on, and those it created."""
    cards: Set[str] = set()
    creates: Set[str] = set()
    first_user = True
    for fact in facts:
        if fact.role == USER_ROLE and first_user:
            first_user = False
            if fact.opening_card:
                cards.add(fact.opening_card)
        elif fact.role == ASSISTANT_ROLE:
            creates |= fact.create_call_ids
        elif fact.role == TOOL_ROLE and fact.created_card and fact.tool_call_id in creates:
            cards.add(fact.created_card)
    return cards


def owned_cards(messages: List[Any]) -> Set[str]:
    return owned_from_facts(message_facts(m) for m in messages)


def run_cards(record: Any) -> Set[str]:
    """The cards one repetition's results.json says its workers worked (each trajectory entry's `task`)."""
    if isinstance(record, list):
        record = record[0] if record else None
    found: Set[str] = set()
    if not isinstance(record, dict):
        return found
    for entry in record.get(TRAJECTORY_KEY) or []:
        if not isinstance(entry, dict):
            continue
        task = entry.get(TRAJECTORY_TASK_KEY)
        if isinstance(task, str) and CARD_RE.match(task):
            found.add(task)
    return found


def read_json(path: str) -> Optional[Any]:
    try:
        with open(path, encoding=TEXT_ENCODING) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Run:
    """One graded repetition: its case, index, bench-gate outcome, the cards its record says it worked,
    and its eval run tag (empty when its eval_run.json is missing or does not name this case and rep)."""

    case: str
    rep: int
    outcome: str
    ids: frozenset = frozenset()
    tag: str = ""

    @property
    def key(self) -> str:
        return f"{self.case}{RUN_KEY_SEPARATOR}{self.rep}"

    @property
    def passed(self) -> bool:
        return self.outcome == REP_OUTCOME_PASS


def load_runs(verdicts_dir: str, results_cache: Optional[Dict[Tuple[str, int, int], Set[str]]] = None) -> List[Run]:
    """Every repetition of every graded case under `verdicts_dir` (bench-gate's case-<name>.json).

    A case file still being written does not parse and is skipped until it does;
    a repetition with no run directory, a relative one, or no readable
    results.json names no card, so no request is attributed to it by card, and
    one with no readable eval_run.json there has no tag (run_tag).
    `results_cache`, kept between calls, reads each results.json once while it
    is unchanged.
    """
    runs: List[Run] = []
    try:
        names = sorted(os.listdir(verdicts_dir))
    except OSError:
        return runs
    for name in names:
        if not (name.startswith(CASE_JSON_PREFIX) and name.endswith(JSON_SUFFIX)):
            continue
        case = read_json(os.path.join(verdicts_dir, name))
        if not isinstance(case, dict) or not isinstance(case.get(REPS_FIELD), list):
            continue
        case_id = str(case.get(CASE_FIELD) or name[len(CASE_JSON_PREFIX):-len(JSON_SUFFIX)])
        for rep in case[REPS_FIELD]:
            if not isinstance(rep, dict) or not isinstance(rep.get(REP_INDEX_FIELD), int):
                continue
            ids: Set[str] = set()
            tag = ""
            run_dir = rep.get(REP_RUN_DIR_FIELD)
            if isinstance(run_dir, str) and os.path.isabs(run_dir):
                ids = _results_cards(os.path.join(run_dir, RESULTS_FILE), results_cache)
                tag = run_tag(read_json(os.path.join(run_dir, EVAL_RUN_FILE)), case_id, rep[REP_INDEX_FIELD])
            runs.append(Run(case_id, rep[REP_INDEX_FIELD], str(rep.get(REP_OUTCOME_FIELD) or ""), frozenset(ids), tag))
    return runs


def run_tag(record: Any, case: str, rep: int) -> str:
    """The tag an eval_run.json gives the repetition it sits in; empty unless it is well formed and
    names that repetition's case and index, so a stray or stale file never claims another's lines."""
    if not isinstance(record, dict):
        return ""
    tag = record.get(EVAL_RUN_TAG_KEY)
    if not isinstance(tag, str) or not EVAL_RUN_RE.fullmatch(tag):
        return ""
    parts = (record.get(EVAL_RUN_ID_KEY), record.get(EVAL_RUN_CASE_KEY), record.get(EVAL_RUN_REP_KEY))
    if tag.split(EVAL_RUN_SEPARATOR) != list(parts) or parts[1] != case or parts[2] != str(rep):
        return ""
    return tag


def tag_slot(tag: str) -> Tuple[str, str]:
    """A well-formed tag's (case, rep): the repetition it claims, whichever attempt."""
    _, case, rep = tag.split(EVAL_RUN_SEPARATOR, EVAL_RUN_PARTS - 1)
    return case, rep


def _results_cards(path: str, cache: Optional[Dict[Tuple[str, int, int], Set[str]]]) -> Set[str]:
    try:
        st = os.stat(path)
    except OSError:
        return set()
    key = (path, st.st_size, st.st_mtime_ns)
    if cache is not None and key in cache:
        return cache[key]
    cards = run_cards(read_json(path))
    if cache is not None:
        cache[key] = cards
    return cards


@dataclass
class Line:
    """The fields of one logged request that attribution reads."""

    request_id: str
    fingerprint: str
    build: str
    recorded: bool = False
    served: bool = False
    served_from_build: str = ""
    prompt_tokens: int = 0
    answer_variant: Optional[Tuple[str, ...]] = None
    # Whether record mode would have saved this line's answer (no unstorable reason).
    storable: bool = False
    # The eval run tag the request carried (`eval_run`), empty when it had
    # none or a malformed one.
    eval_run: str = ""
    # The hash of the answer as record mode stores it (stored_answer_hash):
    # the name of the alternate it saved or would have saved.
    stored_hash: str = ""

    @classmethod
    def from_entry(cls, entry: Dict[str, Any]) -> "Line":
        job = entry.get(F.JOB) or {}
        served_from = entry.get(F.SERVED_FROM) or {}
        return cls(
            request_id=str(entry.get(F.REQUEST_ID) or ""),
            fingerprint=str(entry.get(F.FINGERPRINT) or ""),
            build=str(job.get(F.JOB_BUILD) or ""),
            recorded=entry.get(F.RECORDED) is True,
            served=entry.get(F.SERVED) is True,
            served_from_build=str(served_from.get(F.JOB_BUILD) or "") if isinstance(served_from, dict) else "",
            prompt_tokens=int(entry.get(F.PROMPT_TOKENS) or 0),
            answer_variant=_pair(entry.get(F.ANSWER_VARIANT)),
            storable=entry.get(F.UNSTORABLE) == [],
            stored_hash=str(entry.get(F.STORED_ANSWER_HASH) or ""),
            eval_run=_tag(entry.get(F.EVAL_RUN)),
        )


def _tag(value: Any) -> str:
    return value if isinstance(value, str) and EVAL_RUN_RE.fullmatch(value) else ""


def _pair(value: Any) -> Optional[Tuple[str, ...]]:
    if isinstance(value, list) and len(value) == ANSWER_PAIR_LENGTH and all(isinstance(v, str) and v for v in value):
        return tuple(value)
    return None


@dataclass
class LogSet:
    """A build's logged requests: the analysed ones, and how many lost their analysis."""

    lines: List[Line] = field(default_factory=list)
    lost: int = 0


def read_lines(paths: Iterable[str]) -> Iterator[Dict[str, Any]]:
    """Every request line in the JSONL files at `paths`; unparseable lines are skipped."""
    for path in paths:
        try:
            with open(path, encoding=TEXT_ENCODING) as handle:
                for raw in handle:
                    try:
                        entry = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and entry.get(F.EVENT) == F.REQUEST_EVENT:
                        yield entry
        except OSError:
            continue


def load_logs(paths: Iterable[str]) -> LogSet:
    return LogReader().load(paths)


class LogReader:
    """Reads segments into lines, each file once while its size and time are unchanged.

    The collector rewrites a kept segment only to add to it (an open segment
    taken again once closed), so the size and modification time say when to read again.
    """

    def __init__(self):
        self._files: Dict[str, Tuple[Tuple[int, int], List[Line], int]] = {}

    def _file(self, path: str) -> Tuple[List[Line], int]:
        try:
            st = os.stat(path)
        except OSError:
            return [], 0
        stamp = (st.st_size, st.st_mtime_ns)
        cached = self._files.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1], cached[2]
        lines: List[Line] = []
        lost = 0
        for entry in read_lines([path]):
            if entry.get(F.FINGERPRINT) and entry.get(F.REQUEST_ID):
                lines.append(Line.from_entry(entry))
            else:
                lost += 1
        self._files[path] = (stamp, lines, lost)
        return lines, lost

    def load(self, paths: Iterable[str]) -> LogSet:
        logs = LogSet()
        for path in paths:
            lines, lost = self._file(path)
            logs.lines.extend(lines)
            logs.lost += lost
        return logs


class Store:
    """The request store as the collector kept it: store/requests/<id>.json naming store/blobs/<sha>.json."""

    def __init__(self, root: str):
        self.root = root
        self._ids: Dict[str, Optional[Set[str]]] = {}
        # Every request repeats its history, so each distinct message is read once.
        self._facts: Dict[str, Facts] = {}

    def _blob_facts(self, blob: Any) -> Optional[Facts]:
        if not isinstance(blob, str) or not SAFE_STORE_NAME.match(blob):
            return None
        if blob not in self._facts:
            message = read_json(os.path.join(self.root, STORE_BLOBS_DIR, blob + JSON_SUFFIX))
            if message is None:
                return None
            self._facts[blob] = message_facts(message)
        return self._facts[blob]

    def request_ids(self, request_id: str) -> Optional[Set[str]]:
        """The cards the request's own conversation works on (owned_cards); None when its text is not all there."""
        if request_id in self._ids:
            return self._ids[request_id]
        found: Optional[Set[str]] = None
        if SAFE_STORE_NAME.match(request_id):
            manifest = read_json(os.path.join(self.root, STORE_REQUESTS_DIR, request_id + JSON_SUFFIX))
            blobs = manifest.get(MESSAGES_KEY) if isinstance(manifest, dict) else None
            if isinstance(blobs, list):
                facts = [self._blob_facts(blob) for blob in blobs]
                if all(fact is not None for fact in facts):
                    found = owned_from_facts(facts)  # type: ignore[arg-type]
        # A request the store has not got yet may arrive on a later pass: only a
        # complete answer is kept.
        if found is not None:
            self._ids[request_id] = found
        return found


@dataclass
class Decision:
    decision: str
    runs: Tuple[str, ...] = ()


class Attribution:
    """Decides, for each logged request of one build, which runs it belongs to and whether they passed."""

    def __init__(self, logs: LogSet, store: Store, runs: List[Run], final: bool):
        self.logs = logs
        self.store = store
        self.final = final
        self.runs = {run.key: run for run in runs}
        self.by_id: Dict[str, Set[str]] = {}
        for run in runs:
            for run_id in run.ids:
                self.by_id.setdefault(run_id, set()).add(run.key)
        # Graded repetitions by their exact tag, and by the (case, rep) every
        # attempt's tag claims.
        self.by_tag: Dict[str, str] = {run.tag: run.key for run in runs if run.tag}
        self.by_slot: Dict[Tuple[str, str], Run] = {(run.case, str(run.rep)): run for run in runs}
        # Every line by (build, fingerprint): the cache keeps each distinct
        # answer to a fingerprint once, so a later line that got the same
        # answer recorded nothing, and its runs count through this.
        self.by_fingerprint: Dict[Tuple[str, str], List[Line]] = {}
        for line in logs.lines:
            self.by_fingerprint.setdefault((line.build, line.fingerprint), []).append(line)

    def tag_status(self, line: Line) -> Tuple[str, Optional[str]]:
        """What the line's tag says (TAG_*), and the graded repetition it names or claims, if any."""
        if not line.eval_run:
            return TAG_NONE, None
        key = self.by_tag.get(line.eval_run)
        if key is not None:
            return TAG_RUN, key
        slot = self.by_slot.get(tag_slot(line.eval_run))
        if slot is None:
            return TAG_UNGRADED, None
        return (TAG_OTHER_ATTEMPT if slot.tag else TAG_UNCHECKED), slot.key

    def _own(self, line: Line) -> Optional[Tuple[Set[str], bool]]:
        """(runs its own IDs name, whether any ID names no graded run); None without stored text.

        A request with no ID of its own gives (empty, False).
        """
        ids = self.store.request_ids(line.request_id)
        if ids is None:
            return None
        runs: Set[str] = set()
        unknown = False
        for run_id in ids:
            owners = self.by_id.get(run_id)
            if owners:
                runs |= owners
            else:
                unknown = True
        return runs, unknown

    def _of_runs(self, runs: Set[str]) -> Decision:
        keys = tuple(sorted(runs))
        if all(self.runs[k].passed for k in keys):
            return Decision(PASS, keys)
        return Decision(FAIL, keys)

    def disagrees(self, line: Line) -> bool:
        """Whether the line's cards name a graded run and its tag says otherwise: another graded run,
        another attempt of the card's repetition, or a repetition not graded yet."""
        own = self._own(line)
        if own is None or not own[0]:
            return False
        status, key = self.tag_status(line)
        if status == TAG_RUN:
            return bool(own[0] - {key})
        return status in (TAG_OTHER_ATTEMPT, TAG_UNGRADED)

    def decide(self, line: Line) -> Decision:
        own = self._own(line)
        if own is None:
            return Decision(PENDING if not self.final else UNATTRIBUTED)
        runs, unknown = own
        if self.disagrees(line):
            return Decision(DISAGREE, tuple(sorted(runs)))
        if unknown:
            return Decision(UNATTRIBUTED if self.final else PENDING, tuple(sorted(runs)))
        if runs:
            return self._of_runs(runs)
        # No card of its own: the run its tag names, and nothing else.
        status, key = self.tag_status(line)
        if status == TAG_RUN:
            return self._of_runs({key})
        if status == TAG_OTHER_ATTEMPT:
            return Decision(UNATTRIBUTED)
        if status == TAG_UNGRADED:
            return Decision(UNATTRIBUTED if self.final else PENDING)
        return Decision(UNTAGGED)

    def decide_answer(self, saved: Line) -> Decision:
        """The decision for the answer `saved` recorded: its own, or other lines' that got the same answer.

        The cache keeps each distinct answer to a fingerprint once, so when
        traffic no graded run owns (an alert's triage, another Cluster Agent's
        turn) got an answer first, a run that later got the same answer to the
        same request is never its recording line. A line of the same build and
        fingerprint whose own answer is the saved one (`answer_variant`) proves
        its runs got that answer to that request. Only a recording line left
        unattributed or untagged on the last call takes that evidence, from the
        lines decided by graded runs, passing or failing, and none when any of
        them is a disagreement.
        """
        decision = self.decide(saved)
        if decision.decision not in (UNATTRIBUTED, UNTAGGED) or saved.answer_variant is None or not self.final:
            return decision
        graded: Set[str] = set()
        for other in self.by_fingerprint.get((saved.build, saved.fingerprint), []):
            if other.request_id == saved.request_id or other.answer_variant != saved.answer_variant:
                continue
            found = self.decide(other)
            if found.decision == DISAGREE:
                return decision
            if found.decision in GRADED:
                graded.update(found.runs)
        return self._of_runs(graded) if graded else decision

    def decide_alternate(self, producers: List[Line]) -> Decision:
        """The decision for one alternate, from every line that produced it (same request, same stored hash).

        Record mode saves an answer once, so of several runs that gave the same
        answer only the first line says `recorded`; the later ones still
        produced it. The alternate is decided by the graded runs among its
        storable producers (each decided on its own, decide): PASS when they
        all passed, FAIL when any failed, and either is published, since a
        graded attempt gave exactly this answer to exactly this request. A
        producer whose tag and card disagree refuses it outright: the
        attribution of that answer cannot be trusted. With no graded producer,
        one still pending keeps it pending; otherwise the first producer's
        decision stands.
        """
        decisions = [found for _, found in self._producer_decisions(producers)]
        if any(found.decision == DISAGREE for found in decisions):
            return Decision(DISAGREE)
        graded = {run for found in decisions if found.decision in GRADED for run in found.runs}
        if graded:
            return self._of_runs(graded)
        if any(found.decision == PENDING for found in decisions):
            return Decision(PENDING)
        return decisions[0] if decisions else Decision(UNATTRIBUTED if self.final else PENDING)

    def _producer_decisions(self, producers: List[Line]) -> List[Tuple[Line, Decision]]:
        """Each storable producer's own decision (decide_alternate)."""
        return [(line, self.decide(line)) for line in producers if line.storable]

    def graded_count(self, producers: List[Line]) -> int:
        """How many times graded runs produced an alternate: its producers decided PASS or FAIL, one per request.

        The same per-line decision decide_alternate publishes on, counted
        rather than or-ed, by card or by tag alike: each is one request a
        graded run sent and the model answered with exactly this stored
        answer, so the counts of a key's alternates are how often graded runs
        got each. A request is counted once however many times its line was
        read; an alternate with a disagreeing producer counts none.
        """
        found = self._producer_decisions(producers)
        if any(decision.decision == DISAGREE for _, decision in found):
            return 0
        return len({line.request_id for line, decision in found if decision.decision in GRADED})

    def disagreements(self) -> int:
        """How many of the build's logged requests have a tag and a card naming different runs."""
        return len({line.request_id for line in self.logs.lines if self.disagrees(line)})

    def run_of(self, line: Line) -> Optional[str]:
        """The one run a request names by its own IDs, for the lane's per-run counts; None otherwise."""
        own = self._own(line)
        if own is None or own[1] or len(own[0]) != 1:
            return None
        return next(iter(own[0]))

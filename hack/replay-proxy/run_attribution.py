"""Which eval run each request a proxy logged belongs to, and whether that run passed.

One build runs many cases and repetitions at once on one install, and a request
does not say which run it belongs to (examples/inference-replay/eval-design.md,
"Telling the runs in a build apart"). After the run it can be worked out from
the kanban cards: a request is tied to the cards its own conversation works on,
and each repetition's results.json names the cards its workers worked.

A request's cards are read from its stored, scrubbed text (the proxy's
REPLAY_STORE_DIR), and only from two places that belong to its conversation:
the card a worker was started on (its first user message, `work kanban task
t_...`) and the cards the conversation created (the `task_id` a `kanban_create`
call returned). Card ids anywhere else are not evidence: system prompts quote
old cards, board listings show every run's cards, and Hermes' recent-work digest
lists the assignee's runs on other cards. A run's cards are the `task` its
worker trajectory entries carry. (A front door's request names no A2A task id,
so a run's A2A task is no evidence and is not read.)

A request that names no card of its own (a front door's opening, which is only
the case's prompt) is decided by evidence instead: a later request whose
conversation went on from exactly the answer that was saved. Record mode logs
each line's answer (`answer_variant`) and the answer the request's last
assistant message is (`prior_answer`), both as normalizer.answer_variant
identifies an answer in a history. A continuation attributed to one run, holding
the request as its prefix and that answer next, proves that run got that answer
to that request; the answer is published when any such run passed, which is the
design's "at least one passing run produced that same fingerprint", here for
the same answer as well. Only positive evidence decides, so a line lost before
it reached a segment (a spent budget, a pod replaced before its last segment
was copied, a last pass cut short, a pod that came and went between passes) can
only leave an answer unpublished, never publish one.

Decisions, per logged request:
- `pass`: every run it belongs to was graded and passed;
- `fail`: it belongs to a run that was graded and did not pass;
- `pending`: its text is not all stored yet, or it names a card no graded run
  has yet (its case is still running), or it names none and no passing run has
  been seen going on from its answer while cases are still running;
- `unattributed`: once every case is graded, no stored text or a card no run worked;
- `ambiguous`: no card of its own, and no graded run went on from its answer.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

import replay_cache
import schema as F

CARD_RE = re.compile(r"^t_[0-9a-f]{8}$")
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
AMBIGUOUS = "ambiguous"
DECISIONS = (PASS, FAIL, PENDING, UNATTRIBUTED, AMBIGUOUS)

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
SAFE_STORE_NAME = re.compile(r"^[0-9a-f]{16,64}$")
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
    """One graded repetition: its case, index, bench-gate outcome and the cards its record says it worked."""

    case: str
    rep: int
    outcome: str
    ids: frozenset = frozenset()

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
    results.json names no card, so no request is attributed to it.
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
            run_dir = rep.get(REP_RUN_DIR_FIELD)
            if isinstance(run_dir, str) and os.path.isabs(run_dir):
                ids = _results_cards(os.path.join(run_dir, RESULTS_FILE), results_cache)
            runs.append(Run(case_id, rep[REP_INDEX_FIELD], str(rep.get(REP_OUTCOME_FIELD) or ""), frozenset(ids)))
    return runs


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
    chain: List[str]
    recorded: bool = False
    served: bool = False
    served_from_build: str = ""
    prompt_tokens: int = 0
    answer_variant: Optional[Tuple[str, ...]] = None
    prior_answer: Optional[Tuple[str, ...]] = None

    @classmethod
    def from_entry(cls, entry: Dict[str, Any]) -> "Line":
        job = entry.get(F.JOB) or {}
        hashes = [str(h) for h in entry.get(F.DIAG_MESSAGE_HASHES) or []]
        served_from = entry.get(F.SERVED_FROM) or {}
        return cls(
            request_id=str(entry.get(F.REQUEST_ID) or ""),
            fingerprint=str(entry.get(F.FINGERPRINT) or ""),
            build=str(job.get(F.JOB_BUILD) or ""),
            chain=list(replay_cache.prefix_keys(str(entry.get(F.DIAG_SETTINGS_HASH) or ""), hashes)),
            recorded=entry.get(F.RECORDED) is True,
            served=entry.get(F.SERVED) is True,
            served_from_build=str(served_from.get(F.JOB_BUILD) or "") if isinstance(served_from, dict) else "",
            prompt_tokens=int(entry.get(F.PROMPT_TOKENS) or 0),
            answer_variant=_pair(entry.get(F.ANSWER_VARIANT)),
            prior_answer=_pair(entry.get(F.PRIOR_ANSWER)),
        )


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
        # Continuations by the answer they went on from: (build, answer pair) ->
        # [(the continuation's prefix keys, its one run)].
        self.went_on: Dict[Tuple[str, Tuple[str, ...]], List[Tuple[List[str], str]]] = {}
        for line in logs.lines:
            if line.prior_answer is None:
                continue
            run = self.run_of(line)
            if run is not None:
                self.went_on.setdefault((line.build, line.prior_answer), []).append((line.chain, run))

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

    def decide(self, line: Line) -> Decision:
        own = self._own(line)
        if own is None:
            return Decision(PENDING if not self.final else UNATTRIBUTED)
        runs, unknown = own
        if unknown:
            return Decision(UNATTRIBUTED if self.final else PENDING, tuple(sorted(runs)))
        if runs:
            return self._of_runs(runs)
        # No card of its own: the runs seen going on from exactly its answer.
        evidence = set()
        if line.answer_variant is not None:
            depth = len(line.chain)
            for chain, run in self.went_on.get((line.build, line.answer_variant), []):
                if len(chain) > depth and chain[depth - 1] == line.chain[-1]:
                    evidence.add(run)
        passed = {run for run in evidence if self.runs[run].passed}
        if passed:
            return Decision(PASS, tuple(sorted(passed)))
        if not self.final:
            return Decision(PENDING)
        if evidence:
            return Decision(FAIL, tuple(sorted(evidence)))
        return Decision(AMBIGUOUS)

    def run_of(self, line: Line) -> Optional[str]:
        """The one run a request names by its own IDs, for the lane's per-run counts; None otherwise."""
        own = self._own(line)
        if own is None or own[1] or len(own[0]) != 1:
            return None
        return next(iter(own[0]))

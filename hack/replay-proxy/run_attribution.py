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
conversation went on from exactly the answer that was saved (the exact chain).
Record mode logs each line's answer as stored (`answer_exact`) and, the same
way, the request's last assistant message under the normalization of the
request it answered (`prior_exact`), both replay_cache.answer_exact: the text
and tool calls, with only what a client's re-sending changes (a null text, the
order of argument keys, ids) absorbed. A continuation attributed to one run,
holding the request as its prefix and exactly that answer next, proves that
run got that answer to that request. An answer that differs only in an age, a
pid or a keyed number is a different answer here, so a passing run's
continuation never vouches for a failing run's look-alike. Lines logged before
these fields carry only `answer_variant` and `prior_answer`
(normalizer.answer_variant), which are coarser: for them evidence is matched on
those, and a card-less answer with a look-alike (same request and
answer_variant, different stored answer) is refused. The proof chains: a continuation with no card of its own
(a front door that reads its board before it creates a card) counts for each run
its own answer is proven, by the same rule, to have gone on from; the answer is published when any such run passed, which is the
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

A saved alternate (one distinct answer to one request) is decided by every
storable line whose logged `stored_answer_hash`, the hash of the answer as stored, is
the alternate's name, recorded or not: it
passes when any of them is attributed to runs that all passed, since a passing
run produced that very answer, even when a failing run gave it first and saved
it (Attribution.decide_alternate). A card-less line passes only on a
continuation carrying exactly its answer, and never while another line of the
same request gave an answer its evidence cannot tell apart from it (the same
`answer_exact`, or for older lines the same `answer_variant`, but a different
stored answer). An answer only failing runs gave never passes. A different answer to the same request is an alternate of its own,
decided by its own lines. An answer in the older layout (one per request) is
decided by its recording line, or, when that line is left unattributed or
ambiguous on the last call, by the other lines that sent the same request and
got the same answer, provided no failing run is tied to any of them
(Attribution.decide_answer).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

import replay_cache
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
SAFE_STORE_NAME = re.compile(r"^[0-9a-f]{16,64}\Z")
# An answer's identity in a history: [context, answer] (normalizer.answer_variant).
ANSWER_PAIR_LENGTH = 2
# Marks an evidence key as an exact answer (evidence_key); never a hex digest,
# so it can't equal an answer_variant pair.
EXACT_KEY_TAG = "exact"
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
    # Whether record mode would have saved this line's answer (no unstorable reason).
    storable: bool = False
    # The answer as a later request re-sends it (answer_exact), and the same for
    # the request's last assistant message (prior_exact); empty on a line
    # logged before them.
    answer_exact: str = ""
    prior_exact: str = ""
    # The hash of the answer as record mode stores it (stored_answer_hash):
    # the name of the alternate it saved or would have saved.
    stored_hash: str = ""

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
            storable=entry.get(F.UNSTORABLE) == [],
            stored_hash=str(entry.get(F.STORED_ANSWER_HASH) or ""),
            answer_exact=str(entry.get(F.ANSWER_EXACT) or ""),
            prior_exact=str(entry.get(F.PRIOR_EXACT) or ""),
        )


def evidence_key(exact: str, variant: Optional[Tuple[str, ...]]) -> Optional[Tuple[str, ...]]:
    """What continuation evidence is matched on: the exact answer, or for a line logged before it, its answer_variant."""
    if exact:
        return (EXACT_KEY_TAG, exact)
    return variant


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
        # Continuations by the answer they went on from: (build, evidence key)
        # -> [(the continuation's prefix keys, a run proven for it)]. The key is
        # the continuation's prior_exact (evidence_key), or for a line logged
        # before it, its prior_answer. A continuation
        # counts for the run its own cards name, or, with no card, for each run
        # proven to have gone on from its own answer. Lines are taken longest
        # first, so every continuation of a request is in before the request.
        # A card-less continuation proven for several runs counts for each of
        # them, never for a run it was not proven for: every one of them sent
        # that request after that answer, since each holds both as a prefix.
        self.went_on: Dict[Tuple[str, Tuple[str, ...]], List[Tuple[List[str], str]]] = {}
        # Every line by (build, fingerprint): the cache keeps each distinct
        # answer to a fingerprint once, so a later line that got the same
        # answer recorded nothing, and its runs count through this.
        self.by_fingerprint: Dict[Tuple[str, str], List[Line]] = {}
        for line in logs.lines:
            self.by_fingerprint.setdefault((line.build, line.fingerprint), []).append(line)
        for line in sorted(logs.lines, key=lambda l: -len(l.chain)):
            run = self.run_of(line)
            proven: Set[str] = {run} if run is not None else set()
            if run is None:
                own = self._own(line)
                if own is None or own[1] or own[0]:
                    continue
                proven = self._went_on_from(line)
            key = evidence_key(line.prior_exact, line.prior_answer)
            if key is not None:
                for each in sorted(proven):
                    self.went_on.setdefault((line.build, key), []).append((line.chain, each))

    def _went_on_from(self, line: Line) -> Set[str]:
        """The runs proven for continuations that hold `line`'s request and went on from exactly its answer.

        Matched on answer_exact (the exact chain); a line logged before it on
        answer_variant.
        """
        evidence: Set[str] = set()
        key = evidence_key(line.answer_exact, line.answer_variant)
        if key is not None:
            depth = len(line.chain)
            for chain, run in self.went_on.get((line.build, key), []):
                if len(chain) > depth and chain[depth - 1] == line.chain[-1]:
                    evidence.add(run)
        return evidence

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
        evidence = self._went_on_from(line)
        passed = {run for run in evidence if self.runs[run].passed}
        if passed:
            return Decision(PASS, tuple(sorted(passed)))
        if not self.final:
            return Decision(PENDING)
        if evidence:
            return Decision(FAIL, tuple(sorted(evidence)))
        return Decision(AMBIGUOUS)

    def decide_answer(self, saved: Line) -> Decision:
        """The decision for the answer `saved` recorded: its own, or other lines' that got the same answer.

        The cache keeps each distinct answer to a fingerprint once, so when
        traffic no graded run owns (an alert's triage, another Cluster Agent's
        turn) got an answer first, a run that later got the same answer to the
        same request is never its recording line. A line of the same build and fingerprint whose own
        answer is the saved one (`answer_variant`, the identity a
        continuation's `prior_answer` is checked against) proves its runs got
        that answer to that request. Only a recording line left unattributed or
        ambiguous on the last call takes that evidence, and only when no failing
        run is tied to the saved line or to any line that got the same answer
        (`_tied_runs`), whatever those lines' own decisions say.
        """
        decision = self.decide(saved)
        if decision.decision not in (UNATTRIBUTED, AMBIGUOUS) or saved.answer_variant is None:
            return decision
        if self._any_failed(self._tied_runs(saved)):
            return decision
        passed: Set[str] = set()
        for other in self.by_fingerprint.get((saved.build, saved.fingerprint), []):
            if other.request_id == saved.request_id or other.answer_variant != saved.answer_variant:
                continue
            if self._any_failed(self._tied_runs(other)):
                return decision
            found = self.decide(other)
            if found.decision == PASS:
                passed.update(found.runs)
        if passed:
            return Decision(PASS, tuple(sorted(passed)))
        return decision

    def decide_alternate(self, producers: List[Line]) -> Decision:
        """The decision for one alternate, from every line that produced it (same request, same stored hash).

        Record mode saves an answer once, so of several runs that gave the same
        answer only the first line says `recorded`; the later ones still
        produced it. The alternate passes when any storable producer is
        decided PASS on its own (decide): a passing run gave exactly this
        answer to exactly this request, whichever run saved the file. A
        failing run that gave it too does not hold it back, since what is
        served is that passing run's answer.

        A producer with no card of its own is decided by the continuations
        that went on from exactly its answer (_went_on_from: answer_exact
        against each continuation's prior_exact), so a passing run's
        continuation proves only the answer it carries. When another line of
        the same request gave an answer that evidence cannot tell from this
        one (_look_alike), a card-less producer is refused: no continuation
        could say which of the two it followed. An answer only failing runs
        gave never passes. With no PASS, a producer still pending keeps it
        pending; otherwise the first producer's decision stands.
        """
        decisions = [found for _, found in self._producer_decisions(producers)]
        passed = sorted({run for found in decisions if found.decision == PASS for run in found.runs})
        if any(found.decision == PASS for found in decisions):
            return Decision(PASS, tuple(passed))
        if any(found.decision == PENDING for found in decisions):
            return Decision(PENDING)
        return decisions[0] if decisions else Decision(UNATTRIBUTED if self.final else PENDING)

    def _producer_decisions(self, producers: List[Line]) -> List[Tuple[Line, Decision]]:
        """Each storable producer's own decision, a card-less look-alike's PASS refused (decide_alternate)."""
        out = []
        for line in producers:
            if not line.storable:
                continue
            found = self.decide(line)
            if found.decision == PASS and self._look_alike(line):
                found = Decision(AMBIGUOUS, found.runs)
            out.append((line, found))
        return out

    def passing_count(self, producers: List[Line]) -> int:
        """How many times passing runs produced an alternate: its producers decided PASS, one per request.

        The same per-line decision decide_alternate publishes on, counted
        rather than or-ed: each is one request a passing run sent and the
        model answered with exactly this stored answer, so the counts of a
        key's alternates are how often passing runs got each. A request is
        counted once however many times its line was read.
        """
        return len({line.request_id for line, found in self._producer_decisions(producers) if found.decision == PASS})

    def _look_alike(self, line: Line) -> bool:
        """Whether a card-less line's evidence could belong to a different stored answer.

        True when the line has no card of its own and another line of its
        build and request gave an answer with the same evidence key
        (answer_exact, or answer_variant for a line logged before it) but a
        different stored answer.
        """
        own = self._own(line)
        if own is None or own[0] or own[1]:
            return False
        key = evidence_key(line.answer_exact, line.answer_variant)
        return key is not None and any(
            evidence_key(other.answer_exact, other.answer_variant) == key and other.stored_hash != line.stored_hash
            for other in self.by_fingerprint.get((line.build, line.fingerprint), []))

    def _tied_runs(self, line: Line) -> Set[str]:
        """Every graded run a line is tied to: its own cards' runs, even beside a card no run worked,
        and, with no card of its own, the runs seen going on from its answer. A decision keeps only
        some of them (PASS drops a failing run that also went on), so the publish guard reads these."""
        own = self._own(line)
        if own is None:
            return set()
        runs, unknown = own
        if runs or unknown:
            return set(runs)
        return self._went_on_from(line)

    def _any_failed(self, runs: Set[str]) -> bool:
        return any(not self.runs[run].passed for run in runs)

    def run_of(self, line: Line) -> Optional[str]:
        """The one run a request names by its own IDs, for the lane's per-run counts; None otherwise."""
        own = self._own(line)
        if own is None or own[1] or len(own[0]) != 1:
            return None
        return next(iter(own[0]))

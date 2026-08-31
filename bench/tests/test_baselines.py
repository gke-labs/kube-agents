# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the baseline store, the version key, and computed admission.

The load-bearing properties, in rough order of what they cost if wrong:

1. **Admission is computed from screening evidence, never declared.** It is
   the only thing standing between the collapse rule and a pull request author
   arming it against everyone else in the same diff that adds the case.
2. **A key with no record is STALE, not admitted and not silently compared.**
   A baseline measured on a different agent, judge, or verifier is not
   evidence about this run, and the expensive failure is not noticing.
3. **Three of the five components are read off the run**, so they cannot go
   stale: `setupId` and `scoringVersion` are devops-bench's own, and it
   changes them when the thing they name changes. The captured fixtures are
   what proves those fields exist and where.
4. **The store is append-only JSONL and nothing is ever rewritten.** A
   re-screen adds a line; the older lines stay and are the case's history.
   That is what keeps a checked-in store's churn tolerable and its conflicts
   rare, and it is what makes "was this case always this flaky?" an
   answerable question.
5. **Evidence accumulates, newest first, up to the bar.** An ordinary run is
   three repetitions and the bar is twenty runs, so a rule reading only the
   newest line could never admit anything the routine job produced. Pooling
   is what lets the store fill itself; stopping at the bar is what lets a
   case that has got worse push its own good history out of the window and
   de-admit itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kube_agents_bench.baselines import (
    DEFAULT_ADMISSION_MIN_RUNS,
    DEFAULT_ADMISSION_RATE,
    AdmissionBar,
    BaselineRecord,
    BaselineStore,
    VersionKey,
    Versions,
    append_record,
    load_versions,
    utc_now,
)
from kube_agents_bench.scoring import load_run

from conftest import FIXTURE_RUNS

BASELINES = Path(__file__).resolve().parents[1] / "baselines"

VERSIONS = Versions(fleet=1, verifiers=1)

KEY = VersionKey(
    setup_id="gemini-3-1-pro-preview-kubeagents-mcp",
    scoring_version="v1",
    judge_model="gemini-3.1-pro-preview",
    fleet=1,
    verifiers=1,
)


def write_store(root: Path, case: str, records: list[dict]) -> Path:
    """One `<case>.jsonl`, one record per line, in the order given.

    Oldest first, because that is the order appends produce.
    """
    root.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"case": case, **rec}) for rec in records]
    (root / f"{case}.jsonl").write_text(
        "".join(f"{line}\n" for line in lines), encoding="utf-8"
    )
    return root


def record(
    key: VersionKey = KEY,
    *,
    runs: int = 20,
    passes: int = 19,
    judged: dict | None = None,
    recorded_at: str = "2026-08-25T00:00:00Z",
) -> dict:
    doc = {
        "key": key.to_dict(),
        "recorded_at": recorded_at,
        "commit": "d3be984d",
        "runs": runs,
        "passes": passes,
    }
    if judged is not None:
        doc["judged"] = judged
    return doc


# --------------------------------------------------------------------------
# VERSIONS.json
# --------------------------------------------------------------------------


def test_the_shipped_versions_file_parses():
    """The store ships one hand-maintained file; it must be readable."""
    versions = load_versions(BASELINES / "VERSIONS.json")
    assert versions.fleet >= 1 and versions.verifiers >= 1


def test_the_store_ships_empty():
    """Deliberate, and worth failing on if someone lands a record by accident.

    Nothing is admitted until it has been screened against `main`, so on the
    day this lands the collapse rule cannot fire and the aggregate is
    advisory. `BOOTSTRAP_ADMITTED` is the bridge, not a checked-in record.
    """
    assert sorted(p.name for p in BASELINES.glob("*.jsonl")) == []
    assert sorted(p.name for p in BASELINES.glob("*.json")) == ["VERSIONS.json"]


@pytest.mark.parametrize(
    "text", ['{"fleet": 1}', '{"fleet": "one", "verifiers": 1}', "[]", "not json"]
)
def test_a_malformed_versions_file_raises_rather_than_defaulting(tmp_path, text):
    """Defaulting to 1 would score against a baseline measured at version 3.

    That is the stale-baseline failure this module exists to make visible, so
    it may not be introduced by the module's own error handling.
    """
    path = tmp_path / "VERSIONS.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_versions(path)


def test_a_missing_versions_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_versions(tmp_path / "VERSIONS.json")


# --------------------------------------------------------------------------
# The version key
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["kanban_red_1", "kanban_green_1"])
def test_the_key_builds_from_a_real_captured_run(name):
    """Three components come free from devops-bench; this proves where."""
    rec = load_run(FIXTURE_RUNS / name)
    key = VersionKey.from_run(
        setup_id=rec.setup_id,
        scoring_version=rec.scoring_version,
        judge_model="gemini-3.1-pro-preview",
        versions=VERSIONS,
    )
    assert key == KEY


def test_the_key_is_none_when_the_run_does_not_carry_one():
    """A key of empty strings would match another equally broken run's key.

    Returning None makes the caller report stale, which is the honest answer;
    an empty-string key would quietly admit garbage against garbage.
    """
    for missing in ("setup_id", "scoring_version", "judge_model"):
        kwargs = {
            "setup_id": "s",
            "scoring_version": "v1",
            "judge_model": "j",
            "versions": VERSIONS,
        }
        kwargs[missing] = None
        assert VersionKey.from_run(**kwargs) is None


def test_the_key_round_trips_through_json():
    assert VersionKey.from_dict(json.loads(json.dumps(KEY.to_dict()))) == KEY


@pytest.mark.parametrize(
    "field, value",
    [
        ("setup_id", "gemini-4-kubeagents-mcp"),
        ("scoring_version", "v2"),
        ("judge_model", "gemini-4-judge"),
        ("fleet", 2),
        ("verifiers", 2),
    ],
)
def test_every_component_alone_changes_the_key(field, value):
    """There is no compatible-enough key.

    In particular the judge is its own component: a judge that tracks whatever
    the agent is running cannot be told apart from an agent that got better,
    and a drifting judge moves every baseline at once.
    """
    import dataclasses

    assert dataclasses.replace(KEY, **{field: value}) != KEY


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


def test_a_missing_directory_is_an_empty_store(tmp_path):
    """The state of a fresh checkout before anything has been screened."""
    store = BaselineStore.load(tmp_path / "nope")
    assert store.record_for("anything", KEY) is None


def test_the_shipped_store_loads():
    """VERSIONS.json is not a case file and must be skipped, not parsed."""
    store = BaselineStore.load(BASELINES)
    assert store.record_for("VERSIONS", KEY) is None


def test_a_record_is_found_at_its_exact_key(tmp_path):
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", [record()]))
    found = store.record_for("planted-pdb", KEY)
    assert found is not None and found.runs == 20 and found.rate == 0.95


def test_records_accumulate_and_only_the_current_key_matches(tmp_path):
    """A model bump appends; the old record stays true about its own software."""
    import dataclasses

    old = dataclasses.replace(KEY, setup_id="gemini-3-0-pro-kubeagents-mcp")
    store = BaselineStore.load(
        write_store(tmp_path, "planted-pdb", [record(old, passes=20), record()])
    )
    assert store.record_for("planted-pdb", old).passes == 20
    assert store.record_for("planted-pdb", KEY).passes == 19


def test_the_newest_line_at_a_key_wins(tmp_path):
    """A re-screen is an append, so the last line describes the software now.

    The earlier line is history, not a candidate. Getting this backwards would
    pin every case to whatever it scored the first time it was ever screened.
    """
    store = BaselineStore.load(
        write_store(
            tmp_path,
            "planted-pdb",
            [record(passes=19), record(passes=12)],
        )
    )
    assert store.record_for("planted-pdb", KEY).passes == 12


def test_a_re_screen_can_de_admit_without_deleting_the_evidence(tmp_path):
    """The append-only store's whole point, stated as behaviour.

    A case that got worse is de-admitted by adding a line, and the line that
    admitted it is still there to show it was not always like this.
    """
    root = write_store(tmp_path, "planted-pdb", [record(passes=19), record(passes=10)])
    store = BaselineStore.load(root)
    admitted, why = store.is_admitted("planted-pdb", KEY, bar=AdmissionBar())
    assert admitted is False
    assert "10/20" in why
    assert [r.passes for r in store.history_for("planted-pdb")] == [19, 10]


def test_history_spans_every_key_oldest_first(tmp_path):
    """History is per case, not per key: the point is the trend across bumps."""
    import dataclasses

    old = dataclasses.replace(KEY, setup_id="gemini-3-0-pro-kubeagents-mcp")
    store = BaselineStore.load(
        write_store(tmp_path, "planted-pdb", [record(old, passes=20), record(passes=19)])
    )
    history = store.history_for("planted-pdb")
    assert [r.key.setup_id for r in history] == [old.setup_id, KEY.setup_id]


def test_history_of_an_unknown_case_is_empty(tmp_path):
    assert BaselineStore.load(tmp_path).history_for("nope") == []


def test_blank_lines_are_tolerated(tmp_path):
    """An append that raced a trailing newline must not red the presubmit."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"case": "planted-pdb", **record()})
    (tmp_path / "planted-pdb.jsonl").write_text(f"\n{line}\n\n", encoding="utf-8")
    assert BaselineStore.load(tmp_path).record_for("planted-pdb", KEY).passes == 19


def test_a_filename_that_disagrees_with_its_case_is_fatal(tmp_path):
    """The filename is the join key, and so is the task directory name.

    If they can disagree, a case scores against another case's evidence.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "planted-pdb.jsonl").write_text(
        json.dumps({"case": "something-else", **record()}) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="the location is the join key"):
        BaselineStore.load(tmp_path)


@pytest.mark.parametrize("doc", ["not json", "[]", "1", '"c"', "null"])
def test_a_malformed_line_is_fatal(tmp_path, doc):
    (tmp_path / "c.jsonl").write_text(f"{doc}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        BaselineStore.load(tmp_path)


def test_a_malformed_line_names_its_line_number(tmp_path):
    """A 40-line file needs to say which append broke it, not just that one did."""
    good = json.dumps({"case": "c", **record()})
    (tmp_path / "c.jsonl").write_text(f"{good}\n{good}\nnot json\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"c\.jsonl:3"):
        BaselineStore.load(tmp_path)


def test_one_bad_line_does_not_admit_the_case_on_the_good_ones(tmp_path):
    """Fail loudly rather than score against a partially-read file.

    Half a store reads as evidence, and evidence is what arms the collapse
    rule against every open pull request.
    """
    good = json.dumps({"case": "c", **record()})
    (tmp_path / "c.jsonl").write_text(f"{good}\n{{oops\n", encoding="utf-8")
    with pytest.raises(ValueError):
        BaselineStore.load(tmp_path)


def test_a_leftover_pre_jsonl_file_is_refused(tmp_path):
    """Skipping it would read as "never screened" and silently de-admit.

    The store changed format; a file left in the old one is a migration that
    did not finish, and that has to be said out loud.
    """
    (tmp_path / "planted-pdb.json").write_text(
        json.dumps({"case": "planted-pdb", "records": [record()]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="planted-pdb.jsonl"):
        BaselineStore.load(tmp_path)


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


def test_the_default_bar_is_nineteen_of_twenty():
    bar = AdmissionBar()
    assert (bar.rate, bar.min_runs) == (DEFAULT_ADMISSION_RATE, DEFAULT_ADMISSION_MIN_RUNS)
    assert BaselineRecord(key=KEY, runs=20, passes=19).admits(bar) is True
    assert BaselineRecord(key=KEY, runs=20, passes=18).admits(bar) is False


def test_a_lucky_single_run_does_not_admit():
    """1/1 is a 100% rate and proves nothing.

    Without the run floor, one lucky screening run would arm the collapse
    rule against every future pull request.
    """
    assert BaselineRecord(key=KEY, runs=1, passes=1).admits(AdmissionBar()) is False


def test_the_bar_is_configurable_from_the_environment():
    """Every threshold here is a starting point to be tuned against main."""
    bar = AdmissionBar.from_env({"EVAL_ADMISSION_RATE": "0.8", "EVAL_ADMISSION_MIN_RUNS": "5"})
    assert bar == AdmissionBar(rate=0.8, min_runs=5)
    assert BaselineRecord(key=KEY, runs=5, passes=4).admits(bar) is True


def test_screening_evidence_admits(tmp_path):
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", [record()]))
    admitted, why = store.is_admitted("planted-pdb", KEY, bar=AdmissionBar())
    assert admitted is True
    assert "19/20" in why


def test_a_case_with_no_evidence_is_not_admitted(tmp_path):
    store = BaselineStore.load(tmp_path)
    admitted, why = store.is_admitted("brand-new", KEY, bar=AdmissionBar())
    assert admitted is False
    assert "no screening evidence" in why


def test_a_key_with_no_record_reports_stale_rather_than_comparing(tmp_path):
    """A version bump de-admits everything until it is re-screened.

    The message has to say *stale*, not *unscreened*: the difference is
    whether someone needs to re-run the screener or write a case.
    """
    import dataclasses

    old = dataclasses.replace(KEY, judge_model="gemini-3.0-judge")
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", [record(old)]))
    admitted, why = store.is_admitted("planted-pdb", KEY, bar=AdmissionBar())
    assert admitted is False
    assert why.startswith("stale:")
    assert "gemini-3.1-pro-preview" in why


def test_a_run_with_no_key_is_not_admitted(tmp_path):
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", [record()]))
    admitted, why = store.is_admitted("planted-pdb", None, bar=AdmissionBar())
    assert admitted is False
    assert "no version key" in why


def test_evidence_below_the_bar_says_so(tmp_path):
    store = BaselineStore.load(
        write_store(tmp_path, "planted-pdb", [record(runs=20, passes=12)])
    )
    admitted, why = store.is_admitted("planted-pdb", KEY, bar=AdmissionBar())
    assert admitted is False
    assert "12/20" in why and "below the bar" in why


def test_bootstrap_admits_a_named_case_with_no_store_at_all(tmp_path):
    """The transition bridge.

    Without it every case stops blocking on the day this lands, for as long
    as screening takes.
    """
    store = BaselineStore.load(tmp_path)
    admitted, why = store.is_admitted(
        "gpu-stress-test-diagnosis",
        None,
        bar=AdmissionBar(),
        bootstrap=frozenset({"gpu-stress-test-diagnosis"}),
    )
    assert admitted is True
    assert "BOOTSTRAP_ADMITTED" in why


def test_bootstrap_does_not_leak_to_unnamed_cases(tmp_path):
    store = BaselineStore.load(tmp_path)
    admitted, _ = store.is_admitted(
        "agent-kanban-smoke",
        None,
        bar=AdmissionBar(),
        bootstrap=frozenset({"gpu-stress-test-diagnosis"}),
    )
    assert admitted is False


# --------------------------------------------------------------------------
# Accumulating evidence. An ordinary run is three repetitions; the bar is
# twenty runs. Without pooling the store could never fill itself.
# --------------------------------------------------------------------------


def three(passes: int, *, at: str, judged: dict | None = None) -> dict:
    """One ordinary run's worth of evidence: three repetitions."""
    return record(runs=3, passes=passes, judged=judged, recorded_at=at)


def test_seven_ordinary_runs_add_up_to_an_admission(tmp_path):
    """The requirement in one test: no baseline, then collect, then compare.

    Nothing here is a deliberate screening campaign. Seven merges to main at
    the default three repetitions each is what an untouched repository
    produces on its own, and it has to be enough, or the store ships empty
    and stays that way.
    """
    lines = [three(3, at=f"2026-08-{20 + i:02d}T00:00:00Z") for i in range(7)]
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", lines))

    evidence = store.evidence_for("planted-pdb", KEY, min_runs=20)
    assert (evidence.runs, evidence.passes, evidence.lines) == (21, 21, 7)
    assert store.is_admitted("planted-pdb", KEY, bar=AdmissionBar())[0] is True


def test_a_single_twenty_run_campaign_still_admits_on_its_own(tmp_path):
    """Pooling generalises the old rule; it does not replace it.

    A deliberate twenty-run screening run is one line and must remain one
    line's worth of reading.
    """
    store = BaselineStore.load(
        write_store(tmp_path, "planted-pdb", [record(runs=20, passes=19)])
    )
    evidence = store.evidence_for("planted-pdb", KEY, min_runs=20)
    assert evidence.lines == 1
    assert store.is_admitted("planted-pdb", KEY, bar=AdmissionBar())[0] is True


def test_pooling_stops_at_the_bar_rather_than_reading_the_whole_file(tmp_path):
    """The window is what gives recency for free.

    Reading every line would let a case's distant past keep it admitted
    forever. Reading only enough lines to clear the bar means new evidence
    displaces old evidence, which is the only mechanism that de-admits a case
    without anyone editing the store.
    """
    lines = [three(3, at=f"2026-07-{i + 1:02d}T00:00:00Z") for i in range(20)]
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", lines))
    evidence = store.evidence_for("planted-pdb", KEY, min_runs=20)
    assert evidence.lines == 7 and evidence.runs == 21


def test_whole_lines_only_even_when_that_overshoots_the_bar(tmp_path):
    """21, not 20. Trimming a line to hit the bar exactly would invent a
    sub-record that was never measured."""
    lines = [three(3, at=f"2026-08-{20 + i:02d}T00:00:00Z") for i in range(7)]
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", lines))
    assert store.evidence_for("planted-pdb", KEY, min_runs=20).runs == 21


def test_a_case_that_gets_worse_de_admits_itself(tmp_path):
    """No deletion, no edit, no human. Seven bad runs displace seven good ones."""
    good = [three(3, at=f"2026-08-{20 + i:02d}T00:00:00Z") for i in range(7)]
    bad = [three(0, at=f"2026-09-{i + 1:02d}T00:00:00Z") for i in range(7)]
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", good + bad))

    admitted, why = store.is_admitted("planted-pdb", KEY, bar=AdmissionBar())
    assert admitted is False
    assert "0/21" in why and "below the bar" in why
    # And every one of the fourteen lines is still on disk.
    assert len(store.history_for("planted-pdb")) == 14


def test_partial_evidence_reports_collecting_not_failure(tmp_path):
    """The two are the same boolean and completely different problems.

    "We have not measured this yet" must not read as "we measured it and it
    is not good enough", or the build log tells people to fix a case that is
    working fine.
    """
    lines = [three(3, at=f"2026-08-{20 + i:02d}T00:00:00Z") for i in range(2)]
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", lines))
    admitted, why = store.is_admitted("planted-pdb", KEY, bar=AdmissionBar())
    assert admitted is False
    assert "collecting" in why and "14 more needed" in why
    assert "below the bar" not in why


def test_evidence_ignores_lines_at_another_key(tmp_path):
    other = VersionKey(
        setup_id="some-other-setup",
        scoring_version="v1",
        judge_model="gemini-3.1-pro-preview",
        fleet=1,
        verifiers=1,
    )
    lines = [record(other, runs=20, passes=20)] + [
        three(3, at=f"2026-08-{20 + i:02d}T00:00:00Z") for i in range(2)
    ]
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", lines))
    assert store.evidence_for("planted-pdb", KEY, min_runs=20).runs == 6
    assert store.evidence_for("planted-pdb", other, min_runs=20).runs == 20


def test_evidence_at_an_unknown_key_is_none_not_an_empty_pool(tmp_path):
    """None is "never measured here"; a zero-run pool would read as "measured
    it, got nothing", which is a different and much worse claim."""
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", [record()]))
    other = VersionKey("x", "v1", "j", 1, 1)
    assert store.evidence_for("planted-pdb", other, min_runs=20) is None
    assert store.evidence_for("nobody", KEY, min_runs=20) is None
    assert store.evidence_for("planted-pdb", None, min_runs=20) is None


# --------------------------------------------------------------------------
# Pooled judged means -- rung 6's comparator.
# --------------------------------------------------------------------------


def test_judged_means_are_weighted_by_their_own_n(tmp_path):
    """Twenty runs of evidence must outweigh three.

    An unweighted average of the two means would let one short run swing the
    number rung 6 compares against by as much as a whole screening campaign.
    """
    lines = [
        record(runs=20, passes=20, judged={"OutcomeValidity": {"mean": 1.0, "n": 20}},
               recorded_at="2026-08-01T00:00:00Z"),
        three(3, at="2026-08-02T00:00:00Z",
              judged={"OutcomeValidity": {"mean": 0.0, "n": 3}}),
    ]
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", lines))
    evidence = store.evidence_for("planted-pdb", KEY, min_runs=20)
    # The 3-run line is newest, so it is pooled first; the 20-run line follows
    # and clears the bar. 20/23, not the 0.5 an unweighted mean would give.
    assert evidence.judged_means["OutcomeValidity"] == pytest.approx(20 / 23)
    assert evidence.judged["OutcomeValidity"]["n"] == 23


def test_a_metric_missing_from_one_line_is_not_counted_as_zero(tmp_path):
    """Omitted-is-not-zero, the same rule the scores themselves follow."""
    lines = [
        record(runs=20, passes=20, recorded_at="2026-08-01T00:00:00Z"),
        three(3, at="2026-08-02T00:00:00Z",
              judged={"OutcomeValidity": {"mean": 0.8, "n": 3}}),
    ]
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", lines))
    evidence = store.evidence_for("planted-pdb", KEY, min_runs=20)
    assert evidence.judged_means["OutcomeValidity"] == pytest.approx(0.8)
    assert evidence.judged["OutcomeValidity"]["n"] == 3


def test_a_judged_block_with_no_runs_behind_it_is_dropped(tmp_path):
    lines = [three(3, at="2026-08-02T00:00:00Z",
                   judged={"OutcomeValidity": {"mean": 0.8, "n": 0}})]
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", lines))
    assert store.evidence_for("planted-pdb", KEY, min_runs=20).judged_means == {}


# --------------------------------------------------------------------------
# append_record -- the producer. Without it nothing above ever has data.
# --------------------------------------------------------------------------


def a_record(**kw) -> BaselineRecord:
    base = dict(key=KEY, runs=3, passes=3, recorded_at="2026-08-25T00:00:00Z",
                commit="abc1234")
    base.update(kw)
    return BaselineRecord(**base)


def test_an_append_round_trips_through_the_loader(tmp_path):
    append_record(tmp_path, "planted-pdb", a_record())
    store = BaselineStore.load(tmp_path)
    [got] = store.history_for("planted-pdb")
    assert (got.key, got.runs, got.passes, got.commit) == (KEY, 3, 3, "abc1234")


def test_appending_never_touches_the_line_before_it(tmp_path):
    for i in range(3):
        append_record(tmp_path, "planted-pdb",
                      a_record(passes=i, recorded_at=f"2026-08-2{i}T00:00:00Z"))
    text = (tmp_path / "planted-pdb.jsonl").read_text(encoding="utf-8")
    assert [json.loads(line)["passes"] for line in text.splitlines()] == [0, 1, 2]


def test_an_append_to_a_file_with_no_trailing_newline_does_not_join_lines(tmp_path):
    """A half-written append would otherwise fuse two records into one that
    parses as neither -- and the one time it happens is the time nobody is
    watching."""
    path = tmp_path / "planted-pdb.jsonl"
    path.write_text(json.dumps({"case": "planted-pdb", **record()}), encoding="utf-8")
    append_record(tmp_path, "planted-pdb", a_record())
    assert len(BaselineStore.load(tmp_path).history_for("planted-pdb")) == 2


def test_an_append_creates_the_store_directory(tmp_path):
    target = tmp_path / "does" / "not" / "exist"
    append_record(target, "planted-pdb", a_record())
    assert (target / "planted-pdb.jsonl").is_file()


def test_the_written_line_carries_the_case_id_the_filename_promises(tmp_path):
    """The filename is the join key, and the loader refuses a line that
    disagrees with it. The writer must not be able to produce one."""
    _, line = append_record(tmp_path, "planted-pdb", a_record())
    assert json.loads(line)["case"] == "planted-pdb"
    BaselineStore.load(tmp_path)  # would raise if the two disagreed


def test_zero_counts_are_left_out_of_the_line(tmp_path):
    """A line is read by people. `blocked: 0` on every record is noise that
    makes the one non-zero occurrence harder to spot."""
    _, line = append_record(tmp_path, "planted-pdb", a_record())
    doc = json.loads(line)
    assert "blocked" not in doc and "infra" not in doc
    _, line = append_record(tmp_path, "planted-pdb", a_record(blocked=1, infra=2))
    doc = json.loads(line)
    assert doc["blocked"] == 1 and doc["infra"] == 2


def test_a_record_with_no_stamp_gets_one(tmp_path):
    _, line = append_record(tmp_path, "planted-pdb", a_record(recorded_at=None))
    assert json.loads(line)["recorded_at"].endswith("Z")


def test_the_stamp_is_utc_to_the_second():
    stamp = utc_now()
    assert stamp.endswith("Z") and len(stamp) == len("2026-08-25T00:00:00Z")


def test_blocked_and_infra_counts_stay_out_of_the_rate(tmp_path):
    """Rungs 1-3 block absolutely, admitted or not, so admission has no need
    to model them -- but dropping them silently would make a case that
    half-crashes look perfectly reliable in its own history."""
    append_record(tmp_path, "planted-pdb", a_record(runs=2, passes=2, blocked=1))
    [got] = BaselineStore.load(tmp_path).history_for("planted-pdb")
    assert (got.runs, got.passes, got.blocked) == (2, 2, 1)
    assert got.rate == 1.0

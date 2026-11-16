"""Tests for ``report_times_in_window``: an observation time a report states
falls inside the run and was read from a tool output (#3256)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from devops_bench.verification.spec import parse_node
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportTimesInWindowVerifier
from pydantic import ValidationError

# The labels the cluster-agent-observation-time-read case uses.
LABELS = [r"\bobservation\b", r"\bobserved (?:at|on)\b", r"\bas of\b", r"\bchecked at\b", r"\bretrieved at\b"]
# How long before the check the fake run started.
RUN_LENGTH = timedelta(minutes=5)
# Further than the default skew (120 s) outside the window.
OUTSIDE = timedelta(minutes=5)


def _stamp(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%d %H:%M:%S UTC")


def _date_u(instant: datetime) -> str:
    """What `date -u` prints."""
    return instant.strftime("%a %b %d %H:%M:%S UTC %Y")


def _terminal(output: str) -> dict:
    """A delegated worker's terminal call, as worker_trajectory tags it."""
    return {"name": "terminal", "args": {"command": "date -u"}, "result": output, "status": "completed",
            "agent": "cluster-seeded-a", "task": "t_00000001", "session": "s", "at": 0}


def _run(report: str, *, read: list[str] = (), gaps: list[str] | None = (), started: datetime | None = None, **fields):
    """Grade ``report`` on a run whose workers' tool outputs were ``read``,
    captured with ``gaps`` (``None``: the capture did not run)."""
    started = started or datetime.now(tz=timezone.utc) - RUN_LENGTH
    transcript.set(
        "ack",
        [_terminal(output) for output in read],
        final_message=report,
        started_at=started.timestamp(),
        worker_capture_gaps=None if gaps is None else list(gaps),
    )
    fields.setdefault("label_patterns", LABELS)
    return ReportTimesInWindowVerifier(type="report_times_in_window", **fields).verify(timeout_sec=1.0)


@pytest.fixture(autouse=True)
def _clean_stash():
    transcript.clear()
    yield
    transcript.clear()


def test_the_incident_report_fails_on_a_time_after_the_run():
    # #3256: the audit trail stated 21:38:00 for an answer posted at 21:33:04.
    future = datetime.now(tz=timezone.utc) + OUTSIDE
    report = (
        "Total Pod Count: 16\n\n"
        "Grounding Sources & Audit Trail\n"
        "- Diagnostic Command Executed: kubectl get pods -n kubeagents-system\n"
        f"- Timestamp of Observation: {_stamp(future)}\n"
    )
    outcome = _run(report)
    assert not outcome.success
    assert "outside the run's window" in outcome.reason
    assert _stamp(future) in outcome.reason


def test_a_time_before_the_run_started_fails():
    started = datetime.now(tz=timezone.utc) - RUN_LENGTH
    outcome = _run(f"**Observed at:** {_stamp(started - OUTSIDE)}", started=started)
    assert not outcome.success


def test_a_report_with_no_time_passes():
    outcome = _run("**There are 2 pods in seeded-reliability.** `kubectl get pods` lists both Running.")
    assert outcome.success, outcome.reason
    assert "states no observation time" in outcome.reason


def test_a_time_inside_the_run_passes():
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    outcome = _run(f"- Timestamp of Observation: {_stamp(now)}", read=[_date_u(now)])
    assert outcome.success, outcome.reason
    assert "1 observation time(s) inside" in outcome.reason
    assert "read from a tool output" in outcome.reason


@pytest.mark.parametrize(
    "spelling",
    ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.123456+00:00", "%Y-%m-%d %H:%M UTC", "`%Y-%m-%dT%H:%M:%SZ`"],
)
def test_the_iso_spellings_are_all_read(spelling):
    future = datetime.now(tz=timezone.utc) + OUTSIDE
    assert not _run(f"As of {future.strftime(spelling)} there are 2 pods.").success


def test_a_numeric_offset_is_converted():
    # The same instant written in UTC-04:00 is inside; read as UTC it would be four hours off.
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    local = (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S-04:00")
    assert _run(f"Checked at {local}.", read=[_date_u(now)]).success


def test_a_clock_time_with_utc_and_no_date_is_read():
    future = datetime.now(tz=timezone.utc) + OUTSIDE
    outcome = _run(f"As of {future.strftime('%H:%M')} UTC, 2 pods.")
    assert not outcome.success
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    assert _run(f"As of {now.strftime('%H:%M:%S')} UTC, 2 pods.", read=[_date_u(now)]).success


def test_a_value_on_the_line_after_a_label_is_not_read():
    # The check grades a label's own line only (#3266, round 2): the rules
    # for lending the next line kept growing, so they were cut.
    future = datetime.now(tz=timezone.utc) + OUTSIDE
    outcome = _run(f"**Time of observation**\n\n{_stamp(future)}\n")
    assert outcome.success, outcome.reason
    assert "states no observation time" in outcome.reason


def test_a_capitalised_word_after_a_dated_time_is_not_a_zone():
    # Round 2: "GKE" after a time was read as a named zone, and the time was
    # left ungraded. A dated time with no zone is read as UTC.
    future = datetime.now(tz=timezone.utc) + OUTSIDE
    outcome = _run(f"As of {future.strftime('%Y-%m-%d %H:%M:%S')} GKE reports 2 pods")
    assert not outcome.success, outcome.reason


def test_a_log_line_time_followed_by_a_level_is_read():
    # Round 2: "2026-10-15 21:38:00 INFO probe ok" dropped out of the read set.
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    log = f"{now.strftime('%Y-%m-%d %H:%M:%S')} INFO probe ok"
    outcome = _run(f"Observed at {_stamp(now)}", read=[log])
    assert outcome.success, outcome.reason


def test_a_time_on_an_unlabelled_line_is_not_checked():
    # An event timestamp quoted from command output may be days old; a
    # certificate's expiry is in the future. Neither is an observation time.
    old = datetime.now(tz=timezone.utc) - timedelta(days=3)
    future = datetime.now(tz=timezone.utc) + timedelta(days=30)
    report = f"- Last BackOff event: {_stamp(old)}\n- Certificate expires {_stamp(future)}\n"
    assert _run(report).success


def test_the_skew_widens_the_window():
    just_after = datetime.now(tz=timezone.utc) + timedelta(seconds=60)
    assert _run(f"Observed at {_stamp(just_after)}", read=[_date_u(just_after)]).success
    assert not _run(f"Observed at {_stamp(just_after)}", read=[_date_u(just_after)], max_clock_skew_sec=0).success


def test_scope_full_reads_the_output():
    future = datetime.now(tz=timezone.utc) + OUTSIDE
    started = datetime.now(tz=timezone.utc) - RUN_LENGTH
    transcript.set(f"Observed at {_stamp(future)}", [], final_message="2 pods.", started_at=started.timestamp())
    final = ReportTimesInWindowVerifier(type="report_times_in_window", label_patterns=LABELS)
    full = ReportTimesInWindowVerifier(type="report_times_in_window", label_patterns=LABELS, scope="full")
    assert final.verify(1.0).success
    assert not full.verify(1.0).success


def test_without_a_transcript_it_is_an_error():
    check = ReportTimesInWindowVerifier(type="report_times_in_window", label_patterns=LABELS)
    assert check.verify(1.0).status == "error"


def test_without_a_run_start_it_is_an_error():
    transcript.set("ack", [], final_message="Observed at 2026-10-15 21:38:00 UTC")
    check = ReportTimesInWindowVerifier(type="report_times_in_window", label_patterns=LABELS)
    outcome = check.verify(1.0)
    assert outcome.status == "error"
    assert "no start time" in outcome.reason


@pytest.mark.parametrize("patterns", [[], ["", "  "]])
def test_no_label_pattern_is_refused(patterns):
    with pytest.raises(ValidationError):
        ReportTimesInWindowVerifier(type="report_times_in_window", label_patterns=patterns)


def test_a_label_pattern_that_does_not_compile_is_refused():
    with pytest.raises(ValidationError):
        ReportTimesInWindowVerifier(type="report_times_in_window", label_patterns=["(unclosed"])


def test_the_registered_type_parses_from_a_spec():
    node = parse_node({"type": "report_times_in_window", "label_patterns": ["observ"]})
    assert isinstance(node, ReportTimesInWindowVerifier)


def test_a_pm_time_is_read_on_the_24_hour_clock():
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    assert _run(f"Observed at {now.strftime('%Y-%m-%d %I:%M:%S %p')} UTC", read=[_date_u(now)]).success
    wrong_half = now + timedelta(hours=12)
    flipped = now.strftime("%Y-%m-%d ") + wrong_half.strftime("%I:%M:%S %p") + " UTC"
    assert not _run(f"Observed at {flipped}").success


def test_a_nanosecond_fraction_is_read():
    # kubectl logs --timestamps prints nine fractional digits.
    future = datetime.now(tz=timezone.utc) + OUTSIDE
    assert not _run(f"As of {future.strftime('%Y-%m-%dT%H:%M:%S')}.123456789Z").success


def test_a_date_that_does_not_exist_is_named_and_not_graded():
    outcome = _run("Observed at 2026-02-30 21:38:00 UTC")
    assert outcome.success, outcome.reason
    assert "not a real date and time" in outcome.reason


def test_a_sentence_with_a_label_word_does_not_lend_the_next_line():
    old = datetime.now(tz=timezone.utc) - timedelta(days=14)
    report = f"Nothing in this observation needs action.\n- Pod created: {_stamp(old)}\n"
    assert _run(report).success


def test_a_label_line_followed_by_a_labelled_line_is_read_once():
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    outcome = _run(f"**Observation**\n- Checked at {_stamp(now)}\n", read=[_date_u(now)])
    assert outcome.success, outcome.reason
    assert outcome.reason.startswith("1 observation time(s)")


# The provenance half: an in-window time must also be one a tool output showed.


def test_a_guessed_time_inside_the_window_fails():
    # #3256's earlier run: 21:26:00 inside a run that began 21:24:17, and no
    # output that shows it. The posted run's worker ran only these two.
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    outcome = _run(
        f"- Timestamp of Observation: {_stamp(now)}",
        read=['{"status": "ok", "context": "gke_p_l_c"}', "NAME   STATUS\ncheckout-gateway-1   Running"],
    )
    assert not outcome.success
    assert outcome.status != "error"
    assert "no tool output in the run shows" in outcome.reason


def test_a_time_read_at_seconds_precision_matches_its_minute():
    read_at = datetime.now(tz=timezone.utc).replace(second=3) - timedelta(minutes=2)
    assert _run(f"Observed at {read_at.strftime('%Y-%m-%d %H:%M')} UTC", read=[_date_u(read_at)]).success
    assert _run(f"Observed at {read_at.strftime('%Y-%m-%d %H:%M:00')} UTC", read=[_date_u(read_at)]).success
    next_minute = read_at + timedelta(minutes=1)
    assert not _run(f"Observed at {_stamp(next_minute)}", read=[_date_u(read_at)]).success


def test_an_epoch_output_matches_its_utc_rendering():
    read_at = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    epoch = str(int(read_at.timestamp()))
    assert _run(f"Observed at {_stamp(read_at)}", read=[f'{{"output": "{epoch}\\n", "exit_code": 0}}']).success
    millis = str(int(read_at.timestamp() * 1000))
    assert _run(f"Observed at {_stamp(read_at)}", read=[f"created_at={millis}"]).success


@pytest.mark.parametrize(
    "render",
    [
        lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ"),  # a kubectl status or event timestamp
        lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.123456789Z") + " I1015 probe ok",  # kubectl logs --timestamps
        lambda t: t.strftime("%a, %d %b %Y %H:%M:%S +0000"),  # date -R
    ],
)
def test_the_other_output_forms_are_read(render):
    read_at = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    assert _run(f"As of {_stamp(read_at)}", read=[render(read_at)]).success


def test_a_front_door_output_counts_as_read():
    read_at = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    started = datetime.now(tz=timezone.utc) - RUN_LENGTH
    entry = {"name": "terminal", "args": {}, "result": _date_u(read_at), "status": "completed"}
    transcript.set("ack", [entry], final_message=f"Observed at {_stamp(read_at)}",
                   started_at=started.timestamp(), worker_capture_gaps=[])
    assert ReportTimesInWindowVerifier(type="report_times_in_window", label_patterns=LABELS).verify(1.0).success


@pytest.mark.parametrize(
    ("read", "gaps", "missing"),
    [
        ([], ["the read stopped at 32 cards; later cards were not read"], "stopped at 32 cards"),
        (["[WITHHELD: redactor unavailable in the pod]"], [], "withheld"),
    ],
)
def test_an_unread_time_with_a_partial_read_is_an_error(read, gaps, missing):
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    outcome = _run(f"Observed at {_stamp(now)}", read=read, gaps=gaps)
    assert outcome.status == "error"
    assert missing in outcome.reason


def test_a_redacted_credential_read_is_not_a_partial_read():
    from kube_agents_bench import worker_trajectory

    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    outcome = _run(f"Observed at {_stamp(now)}", read=[worker_trajectory.WITHHELD_READ_RESULT], gaps=[])
    assert outcome.status == "fail"
    assert "withheld" not in outcome.reason


def _front_door(report: str, *, delegated: bool):
    started = datetime.now(tz=timezone.utc) - RUN_LENGTH
    calls = [{"name": "kanban_create", "args": {}, "result": '{"task_id": "t_1"}', "status": "completed"}]
    transcript.set(
        "ack",
        calls if delegated else [],
        final_message=report,
        started_at=started.timestamp(),
        worker_capture_gaps=None,
    )
    return ReportTimesInWindowVerifier(type="report_times_in_window", label_patterns=LABELS).verify(1.0)


def test_a_delegated_run_whose_workers_were_not_captured_is_an_error():
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    outcome = _front_door(f"Observed at {_stamp(now)}", delegated=True)
    assert outcome.status == "error"
    assert "not captured" in outcome.reason


def test_a_run_that_delegated_nothing_is_graded_on_what_it_has():
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    outcome = _front_door(f"Observed at {_stamp(now)}", delegated=False)
    assert not outcome.success
    assert outcome.status != "error"


def test_a_clipped_result_is_read_as_far_as_it_goes_and_noted():
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    clipped = "NAME STATUS\n" + "x" * 2000 + " ...[clipped 512 chars]"
    outcome = _run(f"Observed at {_stamp(now)}", read=[clipped])
    assert not outcome.success
    assert outcome.status != "error"
    assert "1 result(s) were clipped" in outcome.reason
    assert _run(f"Observed at {_stamp(now)}", read=[_date_u(now) + " " + clipped]).success


def test_a_time_relayed_through_a_card_is_not_read():
    # A worker that wrote its guess onto its card would see it come back in
    # kanban_show; a card's result is the worker's own words, not a read.
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    started = datetime.now(tz=timezone.utc) - RUN_LENGTH
    show = {
        "name": "kanban_show",
        "args": {},
        "status": "completed",
        "result": f'{{"task": {{"result": "Observed at {_stamp(now)}"}}}}',
        "agent": "platform",
        "task": "t_1",
    }
    transcript.set(
        "ack", [show], final_message=f"Observed at {_stamp(now)}", started_at=started.timestamp(), worker_capture_gaps=[]
    )
    assert not ReportTimesInWindowVerifier(type="report_times_in_window", label_patterns=LABELS).verify(1.0).success


def test_a_bare_clock_time_in_an_output_is_read():
    # date -u +%T prints no date and no zone.
    read_at = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    assert _run(f"Observed at {read_at.strftime('%H:%M')} UTC", read=[read_at.strftime("%H:%M:%S")]).success
    # Hours and minutes alone are not read: "8080:80" is a port pair.
    assert not _run(f"Observed at {read_at.strftime('%H:%M')} UTC", read=[read_at.strftime("%H:%M")]).success


def test_a_read_time_rounded_up_to_the_next_minute_matches():
    read_at = datetime.now(tz=timezone.utc).replace(second=59) - timedelta(minutes=2)
    rounded = read_at + timedelta(seconds=1)
    assert _run(f"Observed at {rounded.strftime('%Y-%m-%d %H:%M')} UTC", read=[_date_u(read_at)]).success


def test_a_read_time_passes_even_with_a_partial_read():
    now = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    assert _run(f"Observed at {_stamp(now)}", read=[_date_u(now)], gaps=None).success


def test_the_window_still_fails_a_time_that_was_read():
    # A stale time read from an old event is still not when the report looked.
    old = datetime.now(tz=timezone.utc) - timedelta(days=1)
    outcome = _run(f"Observed at {_stamp(old)}", read=[old.strftime("%Y-%m-%dT%H:%M:%SZ")])
    assert not outcome.success
    assert "outside the run's window" in outcome.reason


@pytest.mark.parametrize("form", ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%H:%M UTC"])
def test_a_time_followed_by_a_colon_is_graded(form):
    # lapis2002 on #3266: "Observed at <time>: 2 pods running" passed ungraded.
    future = datetime.now(tz=timezone.utc) + OUTSIDE
    outcome = _run(f"Observed at {future.strftime(form)}: 2 pods running")
    assert not outcome.success
    assert "outside the run's window" in outcome.reason


@pytest.mark.parametrize("label", ["_Observed at_", "*Observed at*", "**Observed at:**", "`as of`"])
def test_emphasis_around_a_label_still_marks_the_line(label):
    # lapis2002 on #3266: `\b` has no boundary next to `_`, so "_Observed at_" read as no label.
    future = datetime.now(tz=timezone.utc) + OUTSIDE
    outcome = _run(f"{label} {_stamp(future)}")
    assert not outcome.success
    assert "outside the run's window" in outcome.reason


def test_the_markers_match_the_in_pod_script():
    from kube_agents_bench import verifiers, worker_trajectory

    script = worker_trajectory._IN_POD_SCRIPT
    assert f'WITHHELD = "{verifiers._WITHHELD_RESULT}"' in script
    assert f'WITHHELD_READ_RESULT = "{worker_trajectory.WITHHELD_READ_RESULT}"' in script
    assert '" ...[clipped %d chars]"' in script
    assert verifiers._CLIPPED_RESULT.search("abc ...[clipped 12 chars]")

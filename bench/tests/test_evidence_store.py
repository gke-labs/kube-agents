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

"""Tests for the two evidence backends.

The properties that matter, and what each costs if it is wrong:

1. **The two backends are the same store.** The scorer reads
   ``(case_id, label, text)`` and must not be able to tell which one produced
   it, or the gate behaves differently in CI than on a developer's laptop.
2. **An empty prefix is an empty store, not an outage.** ``gcloud`` reports
   "matched no objects" as a non-zero exit. Reading that as unreachable would
   put a permanent warning banner on a store that is merely new; reading an
   outage as empty would silently disarm the gate. The two must not be
   confused in either direction.
3. **Objects are never overwritten.** The name carries the record's own
   timestamp and the build id, so a batch written by a different job at the
   same second lands beside it rather than on top of it. The grant this is
   built for cannot overwrite anyway -- these tests are what keep the code
   from needing to.
4. **Lexical order is chronological.** Reading "the newest evidence" is a
   sort over object names, so the name must begin with an ISO stamp.
5. **A capped read says it was capped.** A silent cap reads as "I considered
   everything" when it did not.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from kube_agents_bench import evidence_store
from kube_agents_bench.evidence_store import (
    GcsBackend,
    LocalBackend,
    StoreUnreachable,
    is_gcs,
    open_backend,
)


class FakeGcloud:
    """Stands in for ``subprocess.run`` over ``gcloud storage``.

    Holds objects as a ``{url: text}`` map, so ``cp`` and ``ls`` and ``cat``
    are consistent with each other the way the real thing is.
    """

    def __init__(self, objects: dict[str, str] | None = None):
        self.objects = dict(objects or {})
        self.calls: list[list[str]] = []
        self.fail: str | None = None

    def __call__(self, argv, *, input=None, capture_output=None, text=None, timeout=None):
        assert argv[:2] == ["gcloud", "storage"]
        self.calls.append(argv)
        verb = argv[2]

        if self.fail is not None:
            return subprocess.CompletedProcess(argv, 1, "", self.fail)

        if verb == "ls":
            prefix = argv[3].removesuffix("/**")
            hits = sorted(u for u in self.objects if u.startswith(prefix + "/"))
            if not hits:
                return subprocess.CompletedProcess(
                    argv, 1, "", "ERROR: One or more URLs matched no objects."
                )
            return subprocess.CompletedProcess(argv, 0, "\n".join(hits) + "\n", "")

        if verb == "cat":
            body = "".join(self.objects[u] for u in argv[3:])
            return subprocess.CompletedProcess(argv, 0, body, "")

        if verb == "cp":
            assert argv[3] == "-", "the writer streams from stdin"
            url = argv[4]
            assert url not in self.objects, f"{url} would be overwritten"
            self.objects[url] = input
            return subprocess.CompletedProcess(argv, 0, "", "")

        raise AssertionError(f"unexpected verb {verb}")


@pytest.fixture
def gcloud(monkeypatch):
    fake = FakeGcloud()
    monkeypatch.setattr(evidence_store.subprocess, "run", fake)
    return fake


KEY = {
    "setup_id": "gemini-3-1-pro-preview-kubeagents-mcp",
    "scoring_version": "v1",
    "judge_model": "gemini-3.1-pro-preview",
    "fleet": 1,
    "verifiers": 1,
}

#: The three directories KEY spells out, as the layout writes them.
KEY_DIR = "gemini-3-1-pro-preview-kubeagents-mcp/gemini-3.1-pro-preview/v1-f1-v1"


def line(
    case: str,
    runs: int = 3,
    passes: int = 3,
    at: str = "2026-08-01T00:00:00Z",
    key: dict | None = None,
) -> str:
    doc = {"case": case, "runs": runs, "passes": passes, "recorded_at": at}
    if key is not None:
        doc["key"] = key
    return json.dumps(doc)


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location, gcs",
    [
        ("gs://bucket/prefix", True),
        ("gs://bucket", True),
        ("baselines", False),
        ("/abs/baselines", False),
        ("https://example.com/x", False),
    ],
)
def test_the_scheme_picks_the_backend(location, gcs):
    assert is_gcs(location) is gcs
    assert isinstance(open_backend(location), GcsBackend if gcs else LocalBackend)


def test_a_path_object_is_local(tmp_path):
    assert is_gcs(tmp_path) is False
    assert isinstance(open_backend(tmp_path), LocalBackend)


# --------------------------------------------------------------------------
# local backend
# --------------------------------------------------------------------------


def test_local_round_trips_a_line(tmp_path):
    backend = LocalBackend(tmp_path)
    backend.append("case-a", line("case-a"))
    (source,) = backend.sources()
    assert source.case_id == "case-a"
    assert source.text.splitlines() == [line("case-a")]


def test_local_creates_the_directory_on_first_write(tmp_path):
    backend = LocalBackend(tmp_path / "nope")
    assert backend.sources() == []  # missing directory is an empty store
    backend.append("case-a", line("case-a"))
    assert (tmp_path / "nope" / "case-a.jsonl").is_file()


def test_local_repairs_a_missing_trailing_newline(tmp_path):
    (tmp_path / "case-a.jsonl").write_text(line("case-a"), encoding="utf-8")
    LocalBackend(tmp_path).append("case-a", line("case-a", passes=2))
    text = (tmp_path / "case-a.jsonl").read_text(encoding="utf-8")
    assert len(text.strip().splitlines()) == 2


def test_local_refuses_a_leftover_pre_jsonl_file(tmp_path):
    (tmp_path / "case-a.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="rename it to case-a.jsonl"):
        LocalBackend(tmp_path).sources()


def test_local_ignores_the_versions_file(tmp_path):
    (tmp_path / "VERSIONS.json").write_text("{}", encoding="utf-8")
    assert LocalBackend(tmp_path).sources() == []


# --------------------------------------------------------------------------
# gcs backend: reading
# --------------------------------------------------------------------------


def test_an_empty_prefix_is_an_empty_store_not_an_outage(gcloud):
    assert GcsBackend("gs://b/evidence").sources() == []


def test_a_real_failure_is_unreachable(gcloud):
    gcloud.fail = "ERROR: (gcloud.storage.ls) 403 does not have storage.objects.list"
    with pytest.raises(StoreUnreachable, match="403"):
        GcsBackend("gs://b/evidence").sources()


def test_a_missing_gcloud_is_unreachable(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("gcloud")

    monkeypatch.setattr(evidence_store.subprocess, "run", boom)
    with pytest.raises(StoreUnreachable, match="gcloud not available"):
        GcsBackend("gs://b/evidence").sources()


def test_a_hung_call_is_unreachable(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gcloud", timeout=60)

    monkeypatch.setattr(evidence_store.subprocess, "run", boom)
    with pytest.raises(StoreUnreachable, match="timed out"):
        GcsBackend("gs://b/evidence").sources()


def test_objects_group_by_case_and_concatenate(gcloud):
    gcloud.objects = {
        "gs://b/evidence/case-a/2026-08-01T00-00-00Z-1.jsonl": line("case-a") + "\n",
        "gs://b/evidence/case-a/2026-08-02T00-00-00Z-2.jsonl": line("case-a", passes=2) + "\n",
        "gs://b/evidence/case-b/2026-08-01T00-00-00Z-1.jsonl": line("case-b") + "\n",
    }
    sources = GcsBackend("gs://b/evidence").sources()
    assert [s.case_id for s in sources] == ["case-a", "case-b"]
    assert len(sources[0].text.strip().splitlines()) == 2
    assert sources[0].label == "gs://b/evidence/case-a/"


def test_reads_are_oldest_first_so_the_newest_line_is_last(gcloud):
    # The reader treats the tail of the text as the newest evidence, and object
    # names begin with an ISO stamp precisely so a lexical sort delivers that.
    gcloud.objects = {
        "gs://b/e/case-a/2026-08-09T00-00-00Z-9.jsonl": line("case-a", passes=1) + "\n",
        "gs://b/e/case-a/2026-08-10T00-00-00Z-10.jsonl": line("case-a", passes=2) + "\n",
        "gs://b/e/case-a/2026-08-01T00-00-00Z-1.jsonl": line("case-a", passes=3) + "\n",
    }
    (source,) = GcsBackend("gs://b/e").sources()
    passes = [json.loads(ln)["passes"] for ln in source.text.strip().splitlines()]
    assert passes == [3, 1, 2]


def test_a_capped_read_keeps_the_newest_and_says_how_many_it_dropped(gcloud):
    gcloud.objects = {
        f"gs://b/e/case-a/2026-08-{day:02d}T00-00-00Z-{day}.jsonl": line(
            "case-a", passes=day
        )
        + "\n"
        for day in range(1, 11)
    }
    backend = GcsBackend("gs://b/e", max_objects=4)
    (source,) = backend.sources()
    passes = [json.loads(ln)["passes"] for ln in source.text.strip().splitlines()]
    assert passes == [7, 8, 9, 10]
    assert backend.truncated == {"case-a": 6}


def test_an_uncapped_read_reports_no_truncation(gcloud):
    gcloud.objects = {"gs://b/e/case-a/2026-08-01T00-00-00Z-1.jsonl": line("case-a") + "\n"}
    backend = GcsBackend("gs://b/e")
    backend.sources()
    assert backend.truncated == {}


def test_a_trailing_slash_in_the_location_does_not_double_up(gcloud):
    gcloud.objects = {"gs://b/e/case-a/2026-08-01T00-00-00Z-1.jsonl": line("case-a") + "\n"}
    (source,) = GcsBackend("gs://b/e/").sources()
    assert source.case_id == "case-a"


# --------------------------------------------------------------------------
# gcs backend: writing
# --------------------------------------------------------------------------


def test_the_path_spells_out_the_version_key(gcloud, monkeypatch):
    """Readable on purpose: `ls` on a case answers which setups were screened,
    and `*/<judge>/**` answers which cases a judge scored."""
    monkeypatch.setenv("BUILD_ID", "12345")
    url = GcsBackend("gs://b/e").append(
        "case-a", line("case-a", at="2026-08-01T02:03:04Z", key=KEY)
    )
    assert url == f"gs://b/e/case-a/{KEY_DIR}/2026-08-01T02-03-04Z-12345.jsonl"
    assert gcloud.objects[url].endswith("\n")


def test_the_judge_model_keeps_its_dots(gcloud):
    """The whole point of this layout over a hash is that a human reads it."""
    url = GcsBackend("gs://b/e").append("case-a", line("case-a", key=KEY))
    assert "/gemini-3.1-pro-preview/" in url


def test_a_key_change_starts_a_new_directory_and_freezes_the_old(gcloud):
    """This is what stops any one prefix growing without bound: a model bump
    files somewhere else and the superseded directory never grows again."""
    backend = GcsBackend("gs://b/e")
    backend.append("case-a", line("case-a", key=KEY))
    backend.append("case-a", line("case-a", key={**KEY, "judge_model": "gemini-4"}))
    dirs = {u.rsplit("/", 1)[0] for u in gcloud.objects}
    assert len(dirs) == 2


def test_a_component_that_could_add_a_path_level_is_flattened(gcloud):
    url = GcsBackend("gs://b/e").append(
        "case-a", line("case-a", key={**KEY, "setup_id": "vendor/model:tag"})
    )
    assert "/vendor-model-tag/" in url


def test_a_record_with_no_key_is_filed_rather_than_dropped(gcloud):
    """`record` skips unkeyed cases already; the writer must not be the reason
    a merge to main loses data if that ever stops being true."""
    url = GcsBackend("gs://b/e").append("case-a", line("case-a"))
    assert "/case-a/unkeyed/" in url


def test_two_batches_in_the_same_second_do_not_collide(gcloud, monkeypatch):
    stamp = "2026-08-01T02:03:04Z"
    monkeypatch.setenv("BUILD_ID", "111")
    first = GcsBackend("gs://b/e").append("case-a", line("case-a", at=stamp, key=KEY))
    monkeypatch.setenv("BUILD_ID", "222")
    second = GcsBackend("gs://b/e").append("case-a", line("case-a", at=stamp, key=KEY))
    assert first != second
    assert len(gcloud.objects) == 2


def test_a_write_outside_prow_still_names_itself(gcloud, monkeypatch):
    monkeypatch.delenv("BUILD_ID", raising=False)
    monkeypatch.delenv("PROW_JOB_ID", raising=False)
    url = GcsBackend("gs://b/e").append("case-a", line("case-a"))
    assert url.endswith("-local.jsonl")


def test_a_failed_write_is_unreachable_not_silence(gcloud):
    gcloud.fail = "ERROR: 403 does not have storage.objects.create"
    with pytest.raises(StoreUnreachable, match="403"):
        GcsBackend("gs://b/e").append("case-a", line("case-a"))


def test_what_was_written_reads_back(gcloud):
    backend = GcsBackend("gs://b/e")
    backend.append("case-a", line("case-a", at="2026-08-01T00:00:00Z", key=KEY))
    backend.append("case-a", line("case-a", passes=2, at="2026-08-02T00:00:00Z", key=KEY))
    (source,) = GcsBackend("gs://b/e").sources()
    assert [json.loads(ln)["passes"] for ln in source.text.strip().splitlines()] == [3, 2]


# --------------------------------------------------------------------------
# gcs backend: reading a key-partitioned store
# --------------------------------------------------------------------------


def nested(case: str, key_dir: str, day: int, passes: int = 3) -> tuple[str, str]:
    stamp = f"2026-08-{day:02d}T00-00-00Z"
    url = f"gs://b/e/{case}/{key_dir}/{stamp}-{day}.jsonl"
    return url, line(case, passes=passes, at=stamp) + "\n"


def test_the_case_is_the_first_segment_whatever_the_depth(gcloud):
    gcloud.objects = dict(
        [nested("case-a", KEY_DIR, 1), nested("case-b", KEY_DIR, 1)]
    )
    assert [s.case_id for s in GcsBackend("gs://b/e").sources()] == ["case-a", "case-b"]


def test_a_flat_object_still_reads(gcloud):
    """The layout gained depth while the store was empty, so nothing needs
    migrating -- but a reader that could only handle one shape would make the
    next layout change a migration, and this one costs a line."""
    gcloud.objects = {
        "gs://b/e/case-a/2026-08-01T00-00-00Z-1.jsonl": line("case-a") + "\n"
    }
    (source,) = GcsBackend("gs://b/e").sources()
    assert source.case_id == "case-a"


def test_records_stay_chronological_within_a_key(gcloud):
    gcloud.objects = dict(
        nested("case-a", KEY_DIR, day, passes=day) for day in (9, 10, 1)
    )
    (source,) = GcsBackend("gs://b/e").sources()
    passes = [json.loads(ln)["passes"] for ln in source.text.strip().splitlines()]
    assert passes == [1, 9, 10]


def test_the_cap_applies_per_key_not_per_case(gcloud):
    """The bug this exists to prevent: capping a case as a whole sorts its key
    directories against each other, so an alphabetically-early CURRENT key gets
    dropped to keep a superseded one, and the case silently de-admits."""
    old = "aaa-setup/judge/v1-f1-v1"
    new = "zzz-setup/judge/v1-f1-v1"
    objects = {}
    for day in range(1, 6):
        for key_dir in (old, new):
            url, text = nested("case-a", key_dir, day)
            objects[url] = text
    gcloud.objects = objects

    backend = GcsBackend("gs://b/e", max_objects=2)
    (source,) = backend.sources()
    urls_read = [c for c in gcloud.calls if c[2] == "cat"][0][3:]
    assert sum(1 for u in urls_read if old in u) == 2
    assert sum(1 for u in urls_read if new in u) == 2
    assert backend.truncated == {"case-a": 6}


def test_an_uncapped_multi_key_case_reports_no_truncation(gcloud):
    gcloud.objects = dict(
        [nested("case-a", "s1/j/v1-f1-v1", 1), nested("case-a", "s2/j/v1-f1-v1", 1)]
    )
    backend = GcsBackend("gs://b/e")
    (source,) = backend.sources()
    assert backend.truncated == {}
    assert len(source.text.strip().splitlines()) == 2

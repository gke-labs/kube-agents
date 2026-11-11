"""The normalizer's rules, one test per rule, with the cases each rule must leave alone."""

import calendar
import json
import unittest
from datetime import datetime
from unittest import mock

import normalizer

NOW = calendar.timegm(datetime(2026, 11, 3, 12, 0, 0).timetuple())


def norm(text, keep_ips=False):
    return normalizer.Normalizer(NOW, keep_ips).text(text)


def request(*contents, **extra):
    body = {"model": "model-default", "messages": [{"role": "user", "content": c} for c in contents]}
    body.update(extra)
    return body


def fp(body, now=NOW, context=normalizer.KeyContext("sum", "litellm:1")):
    return normalizer.fingerprint(normalizer.normalize_request(body, now), context)


class IdentifierTest(unittest.TestCase):
    def test_kanban_task_ids_are_numbered_by_first_appearance(self):
        self.assertEqual(norm("t_3c176184 then t_9a02e1f7 then t_3c176184"), "⟦TASK_1⟧ then ⟦TASK_2⟧ then ⟦TASK_1⟧")

    def test_uuid(self):
        self.assertEqual(norm("uid 0f8e2a4c-1b3d-4e5f-8a9b-0c1d2e3f4a5b"), "uid ⟦UID_1⟧")

    def test_hermes_session_id(self):
        self.assertEqual(norm("session 20261102_002640_791a68"), "session ⟦SESSION_1⟧")

    def test_a2a_task_and_inject_message_ids(self):
        self.assertEqual(norm("task-0123456789abcdef via inj-1793347681123"), "⟦A2ATASK_1⟧ via ⟦INJ_1⟧")

    def test_gke_node_keeps_cluster_and_pool(self):
        self.assertEqual(
            norm("on gke-seeded-a-default-pool-1a2b3c4d-wxyz"), "on gke-seeded-a-default-pool-⟦NODE_1⟧"
        )

    def test_replicaset_pod_keeps_workload_name(self):
        self.assertEqual(norm("pod web-7d4b9c8f6d-x2k9p"), "pod web-⟦POD_1⟧")

    def test_word_with_vowels_is_not_a_pod_suffix(self):
        self.assertEqual(norm("kube-proxy-config-server"), "kube-proxy-config-server")

    def test_scratch_file_suffix(self):
        self.assertEqual(norm("/tmp/plan_1793347681_4821.json"), "/tmp/plan⟦SCRATCH_1⟧.json")

    def test_hermes_terminal_output_cache_file(self):
        self.assertEqual(
            norm("/opt/data/profiles/platform/cache/terminal-output/out-1793347681-125114-89b0.log"),
            "/opt/data/profiles/platform/cache/terminal-output/out-⟦TERMOUT_1⟧.log",
        )

    def test_keyed_numbers_in_text(self):
        self.assertEqual(
            norm('resourceVersion: "48213"\ngeneration: 7012\npid 4821\n"run_id": 1203, "id": 9901'),
            'resourceVersion: "⟦NUM_1⟧"\ngeneration: ⟦NUM_2⟧\npid ⟦NUM_5⟧\n"run_id": ⟦NUM_3⟧, "id": ⟦NUM_4⟧',
        )

    def test_keyed_numbers_in_structure(self):
        out = normalizer.Normalizer(NOW, False).walk({"metadata": {"resourceVersion": "48213", "generation": 3104}})
        self.assertEqual(out, {"metadata": {"generation": "⟦NUM_1⟧", "resourceVersion": "⟦NUM_2⟧"}})

    def test_short_keyed_numbers_are_swapped(self):
        # Kanban run ids are short counters that differ per run (190 vs 202).
        self.assertEqual(norm('"run_id": "190"'), norm('"run_id": "202"'))
        self.assertIn("⟦NUM_1⟧", norm("generation: 7"))

    def test_timing_jitter_within_the_hour_does_not_change_the_key(self):
        # A card read 50 s or 70 s after it was claimed must give the same key.
        self.assertEqual(norm('"created_at": %d' % (NOW - 50)), norm('"created_at": %d' % (NOW - 70)))
        # Inside the hour the side still counts: expired 20 minutes ago is not valid for 20 more.
        self.assertNotEqual(norm('"expires": %d' % (NOW + 20 * 60)), norm('"expires": %d' % (NOW - 20 * 60)))
        self.assertNotEqual(norm('"created_at": %d' % (NOW - 50)), norm('"created_at": %d' % (NOW - 3 * 3600)))

    def test_any_run_id_key_is_swapped(self):
        self.assertEqual(norm('"current_run_id": 190'), norm('"current_run_id": 200'))
        out = normalizer.Normalizer(NOW, False).walk({"current_run_id": 190, "parent_run_id": 7})
        self.assertNotIn("190", json.dumps(out))

    def test_kanban_comment_id_is_swapped(self):
        # kanban_comment's result: the comment's row id counts every comment on
        # the install's board, so each run gets a later one (41, then 43).
        def comment(cid, task="t_57626387"):
            return {"role": "tool", "tool_call_id": "call_1", "content": json.dumps({
                "ok": True, "task_id": task, "comment_id": cid, "task_status": "running",
                "comment_reaches": "the worker running this card right now", "completion_posts_to_chat": True})}
        self.assertEqual(fp({"model": "m", "messages": [comment(41)]}),
                         fp({"model": "m", "messages": [comment(43, task="t_f3c26f19")]}))
        out = normalizer.Normalizer(NOW, False).walk({"comment_id": 41})
        self.assertEqual(out, {"comment_id": "⟦NUM_1⟧"})
        self.assertEqual(norm('comment_id: 41'), norm('comment_id: 43'))
        self.assertEqual(norm('"comment_id": "41"'), '"comment_id": "⟦NUM_1⟧"')
        # What the card is doing stays in the key.
        self.assertNotEqual(norm(json.dumps({"comment_id": 41, "task_status": "running"})),
                            norm(json.dumps({"comment_id": 41, "task_status": "done"})))

    def test_comment_id_lookalikes_stay_literal(self):
        for text in ('"comment_count": 41', '"comments": 41', 'last_comment_id: 41', 'xcomment_id=41',
                     'comment 41'):
            self.assertNotIn("⟦NUM", norm(text), text)
        out = normalizer.Normalizer(NOW, False).walk({"last_comment_id": 41, "comment_count": 41, "comment_id": "c-41"})
        self.assertEqual(out, {"comment_count": 41, "comment_id": "c-41", "last_comment_id": 41})

    def test_heartbeat_events_are_left_out_of_the_key(self):
        def card(events):
            return norm(json.dumps({"task": {"id": "t_11111111", "status": "running"}, "events": events}))
        claimed = {"kind": "claimed", "payload": None}
        beat = {"kind": "heartbeat", "payload": None}
        self.assertEqual(card([claimed]), card([claimed, beat, beat]))
        self.assertNotEqual(card([claimed]), card([claimed, {"kind": "completed", "payload": None}]))

    def test_queued_events_are_left_out_of_the_key(self):
        def card(events):
            return norm(json.dumps({"task": {"id": "t_11111111", "status": "running"}, "events": events}))
        claimed = {"kind": "claimed", "payload": None, "created_at": 1793700000}
        queued = {"kind": "queued", "created_at": 1793700100, "run_id": None, "payload": {
            "note": "Queued: the system is busy. Your request will start when a worker frees up.",
            "running": 4, "limit": 5, "background_running": 0}}
        self.assertEqual(card([claimed]), card([claimed, queued]))
        self.assertNotEqual(card([claimed]), card([claimed, {"kind": "reclaimed", "payload": None}]))

    def test_a_dropped_events_unrestorable_values_still_count_against_an_answer(self):
        walker = normalizer.Normalizer(NOW, False)
        walker.walk({"events": [{"kind": "queued", "created_at": 1793700100, "payload": {"note": "busy since 2026-11-05"}},
                                {"kind": "heartbeat", "payload": {"note": "pod up 5m ago"}}]})
        self.assertTrue(walker.mapping.answer_repeats_bare_number("waited since 1793700100"))
        self.assertTrue(walker.mapping.answer_repeats_age("logs --since 5m"))
        self.assertTrue(walker.mapping.answer_repeats_date("busy since 2026-11-05"))
        # A throwaway walk numbers nothing: the next placeholder is still the first.
        self.assertEqual(walker.mapping.counts, {})

    def test_a_queued_events_counts_count_against_an_answer_about_the_queue(self):
        walker = normalizer.Normalizer(NOW, False)
        walker.walk({"events": [{"kind": "queued", "payload": {
            "note": "Queued: the system is busy.", "running": 4, "limit": 5, "ahead": 6, "background_running": False}}]})
        mapping = walker.mapping
        for answer in ("It waited behind 6 other tasks.", "The card was queued with 4 running.", "running: 4 of limit=5",
                       "6 tasks ahead of it"):
            with self.subTest(answer=answer):
                self.assertTrue(mapping.answer_repeats_bare_number(answer))
        # The same small numbers anywhere else are not the queue's.
        for answer in ("replicas: 4", "All 5 clusters are healthy.", "pool-6 has 6 nodes", "1 minor version"):
            with self.subTest(answer=answer):
                self.assertFalse(mapping.answer_repeats_bare_number(answer))
        # A boolean is no count.
        self.assertFalse(mapping.answer_repeats_bare_number("background_running: 0 in the queue"))

    def test_expires_is_a_time_key(self):
        self.assertEqual(norm('"expires": 1793759016'), norm('"expires": 1793759100'))
        self.assertIn("⟦TIME_", norm('"expires": 1793759016'))

    def test_state_stays_literal(self):
        text = "restartCount: 4\nreplicas: 3\nversion 1.30.5-gke.1014001\nzone us-central1-a\nscore 0.82"
        self.assertEqual(norm(text), text)


class AgeTest(unittest.TestCase):
    def test_age_column(self):
        table = (
            "NAME                   READY   STATUS    RESTARTS   AGE\n"
            "web-7d4b9c8f6d-x2k9p   1/1     Running   0          3d2h\n"
            "api-7d4b9c8f6d-q8rtz   1/1     Running   0          5m\n"
        )
        out = norm(table)
        self.assertIn("⟦AGEd_1⟧", out)
        self.assertIn("⟦AGEm_1⟧", out)
        self.assertNotIn("3d2h", out)

    def test_age_keeps_its_unit(self):
        self.assertNotEqual(fp(request("age: 5m")), fp(request("age: 5d")))
        self.assertEqual(fp(request("age: 5m")), fp(request("age: 7m")))

    def test_last_seen_column(self):
        table = "LAST SEEN   TYPE      REASON\n12m         Warning   BackOff\n"
        self.assertEqual(norm(table), "LAST SEEN   TYPE      REASON\n⟦AGEm_1⟧         Warning   BackOff\n")

    def test_describe_events_age_column(self):
        table = (
            "  Type     Reason   Age                From     Message\n"
            "  ----     ------   ----               ----     -------\n"
            "  Warning  BackOff  2m (x5 over 10m)   kubelet  Back-off\n"
        )
        self.assertIn("⟦AGEm_1⟧ (x5 over ⟦AGEm_2⟧)", norm(table))

    def test_age_and_last_seen_fields(self):
        self.assertEqual(norm('age: 5m, "lastSeen": "3h"'), 'age: ⟦AGEm_1⟧, "lastSeen": "⟦AGEh_1⟧"')
        out = normalizer.Normalizer(NOW, False).walk({"age": "2d", "lastSeen": "45s"})
        self.assertEqual(out, {"age": "⟦AGEd_1⟧", "lastSeen": "⟦AGEs_1⟧"})

    def test_ago(self):
        self.assertEqual(norm("restarted 3 (5m ago); done 2 hours ago"), "restarted 3 (⟦AGEm_1⟧ ago); done ⟦AGEh_1⟧ ago")

    def test_cpu_quantity_stays_literal(self):
        top = "NAME                   CPU(cores)   MEMORY(bytes)\nweb-7d4b9c8f6d-x2k9p   120m         64Mi\n"
        self.assertIn("120m         64Mi", norm(top))
        self.assertEqual(norm("resources:\n  requests:\n    cpu: 120m\n    memory: 5m"), "resources:\n  requests:\n    cpu: 120m\n    memory: 5m")

    def test_bare_duration_outside_an_age_context_stays_literal(self):
        self.assertEqual(norm("timeout 5m, retry every 30s"), "timeout 5m, retry every 30s")

    def test_cpu_differs_in_the_key(self):
        self.assertNotEqual(fp(request("cpu: 120m")), fp(request("cpu: 250m")))


class AgeColumnTest(unittest.TestCase):
    def test_a_line_below_the_header_that_is_not_a_row_keeps_its_text(self):
        # The AGE cell spans columns 60-67; on the JSON line below the table that
        # is the uid's `-83d`, which once read as an age of 83 days.
        header = "NAME".ljust(60) + "AGE     IP"
        row = "web-7d4b9c8f6d-x2k9p".ljust(60) + "5m".ljust(8) + "10.1.2.3"
        line = '{"name":"' + "x" * 28 + '","uid":"6f79db96-15b3-4308-83df-7c4f203b6015","phase":"Pending"}'
        out = norm("\n".join((header, row, line)))
        self.assertIn('"uid":"⟦UID_1⟧"', out)
        self.assertRegex(out, r"⟦AGEm_\d+⟧")


class TimeOccurrenceTest(unittest.TestCase):
    def test_only_timestamps_outside_assistant_messages_take_a_position(self):
        # A slot blanks an answer's words, so a timestamp counted there could
        # shift every later position between two requests that share a slot key.
        n = normalizer.normalize_request({"model": "m", "messages": [
            {"role": "assistant", "content": "as of 2026-11-03T11:00:00Z"},
            {"role": "tool", "tool_call_id": "c", "content": "created 2026-11-03T11:30:00Z"},
        ]}, NOW)
        self.assertEqual(n.mapping.time_occurrences, ["2026-11-03T11:30:00Z"])
        self.assertEqual(n.mapping.apply_to_answer("created 2026-11-03T11:30:00Z"), "created ⟦TIMEVAL_1⟧")


class TimeOccurrenceInJsonTest(unittest.TestCase):
    def test_asking_whether_a_value_holds_a_time_notes_no_timestamp(self):
        # The walk asks of each JSON value whether it holds a time of day
        # (Normalizer.timed) before walking it; only the walk notes the text.
        n = normalizer.normalize_request({"model": "m", "messages": [
            {"role": "tool", "tool_call_id": "c",
             "content": '{"created": "2026-11-03T11:30:00Z", "name": "web", "updated": "2026-11-03T11:40:00Z"}'},
        ]}, NOW)
        self.assertEqual(n.mapping.time_occurrences, ["2026-11-03T11:30:00Z", "2026-11-03T11:40:00Z"])


class StderrOrderTest(unittest.TestCase):
    def test_a_warning_before_or_after_the_output_gives_one_key(self):
        warning = "WARNING: --filter : operator evaluation is changing for consistency across Google APIs."
        body = '{\n  "quotas": [{"limit": 16.0, "metric": "NVIDIA_A100_GPUS"}]\n}'
        after = json.dumps({"exit_code": 0, "output": body + "\n" + warning})
        before = json.dumps({"exit_code": 0, "output": warning + "\n" + body + "\n"})
        self.assertEqual(norm(after), norm(before))

    def test_a_wrapped_warning_moves_with_its_continuation_lines(self):
        warning = "WARNING: Accessing a Kubernetes Engine cluster requires the kubernetes commandline\n    tool [kubectl]."
        body = '[{"name": "alpha", "status": "RUNNING"}]'
        self.assertEqual(norm(warning + "\n" + body + "\n"), norm(body + "\n" + warning))
        self.assertEqual(norm(warning + "\n" + body), body + "\n" + warning)

    def test_the_order_of_a_warning_and_plain_output_still_counts(self):
        # In either direction: only a JSON document between warnings is reordered.
        self.assertNotEqual(norm("WARNING: disk pressure\nPod evicted"), norm("Pod evicted\nWARNING: disk pressure"))
        self.assertNotEqual(norm("Pod evicted\nWARNING: disk pressure"), norm("WARNING: disk pressure\nPod evicted"))
        self.assertEqual(norm("WARNING: disk pressure\nPod evicted"), "WARNING: disk pressure\nPod evicted")
        body = '{"ok": true}'
        self.assertNotEqual(norm("WARNING: a\nnote\n" + body), norm(body + "\nWARNING: a\nnote"),
                            "an unindented line is output, not a warning's continuation")

    def test_the_rest_of_the_output_still_counts(self):
        warning = "WARNING: something"
        self.assertNotEqual(norm(warning + "\nlimit: 16"), norm(warning + "\nlimit: 64"))
        self.assertEqual(norm(warning), warning)


def skill_result(references, scripts, **extra):
    """A skill_view result as Hermes writes one (tools/skills_tool.py), with its linked files in the order given."""
    result = {"success": True, "name": "fleet-audit", "tags": ["audit", "fleet"], "related_skills": ["submit-suggestion"],
              "content": "---\nname: fleet-audit\n---\n# fleet-audit", "path": "fleet-audit/SKILL.md",
              "linked_files": {"references": references, "scripts": scripts}}
    result.update(extra)
    return json.dumps(result)


def tool_conversation(tool_name, content, call_id="call_1__thought__QUJD"):
    """A request whose last message is `content`, returned by a call to `tool_name`."""
    call = {"id": call_id, "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps({"name": "fleet-audit"})}}
    return {"model": "m", "messages": [
        {"role": "user", "content": "audit the fleet"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]}


class SkillViewListingTest(unittest.TestCase):
    def test_linked_files_in_another_order_key_the_same(self):
        recorded = skill_result(["references/ledger.md", "references/finish.md"], ["scripts/b.py", "scripts/a.py"])
        replayed = skill_result(["references/finish.md", "references/ledger.md"], ["scripts/a.py", "scripts/b.py"])
        self.assertEqual(fp(tool_conversation("skill_view", recorded)), fp(tool_conversation("skill_view", replayed)))

    def test_available_files_in_another_order_key_the_same(self):
        def missing(files):
            return json.dumps({"success": False, "error": "File 'x.md' not found in skill 'fleet-audit'.",
                               "available_files": {"references": files}})
        self.assertEqual(fp(tool_conversation("skill_view", missing(["references/b.md", "references/a.md"]))),
                         fp(tool_conversation("skill_view", missing(["references/a.md", "references/b.md"]))))

    def test_a_different_set_of_files_keys_differently(self):
        one = skill_result(["references/ledger.md", "references/finish.md"], ["scripts/a.py"])
        other = skill_result(["references/ledger.md", "references/clean-run.md"], ["scripts/a.py"])
        self.assertNotEqual(fp(tool_conversation("skill_view", one)), fp(tool_conversation("skill_view", other)))
        moved = skill_result(["references/ledger.md"], ["scripts/a.py", "references/finish.md"])
        self.assertNotEqual(fp(tool_conversation("skill_view", one)), fp(tool_conversation("skill_view", moved)),
                            "a file in another group is another listing")

    def test_another_tools_listing_keeps_its_order(self):
        recorded = skill_result(["references/ledger.md", "references/finish.md"], ["scripts/b.py", "scripts/a.py"])
        replayed = skill_result(["references/finish.md", "references/ledger.md"], ["scripts/a.py", "scripts/b.py"])
        self.assertNotEqual(fp(tool_conversation("read_file", recorded)), fp(tool_conversation("read_file", replayed)))

    def test_a_result_answering_no_skill_view_call_keeps_its_order(self):
        # The call is matched by id: a tool message naming another call is not read as skill_view's.
        recorded = skill_result(["references/ledger.md", "references/finish.md"], ["scripts/a.py"])
        replayed = skill_result(["references/finish.md", "references/ledger.md"], ["scripts/a.py"])

        def answering_other(content):
            body = tool_conversation("skill_view", content)
            body["messages"][-1] = dict(body["messages"][-1], tool_call_id="call_2")
            return body
        self.assertNotEqual(fp(answering_other(recorded)), fp(answering_other(replayed)))

    def test_the_results_other_fields_are_unchanged(self):
        content = skill_result(["references/ledger.md", "references/finish.md"], ["scripts/b.py", "scripts/a.py"])
        walked = normalizer.normalize_request(tool_conversation("skill_view", content), NOW).messages[-1]
        expected = json.loads(content)
        expected["linked_files"] = {"references": ["references/finish.md", "references/ledger.md"],
                                    "scripts": ["scripts/a.py", "scripts/b.py"]}
        self.assertEqual(json.loads(walked["content"]), expected)
        self.assertEqual(json.loads(walked["content"])["tags"], ["audit", "fleet"], "tags keep their order")
        flipped = skill_result(["references/ledger.md", "references/finish.md"], ["scripts/b.py", "scripts/a.py"],
                               tags=["fleet", "audit"])
        self.assertNotEqual(fp(tool_conversation("skill_view", content)), fp(tool_conversation("skill_view", flipped)))

    def test_a_result_already_in_order_keeps_its_text(self):
        content = skill_result(["references/a.md"], ["scripts/a.py"])
        message = {"role": "tool", "tool_call_id": "call_1", "content": content}
        walker = normalizer.Normalizer(NOW, keep_ips=False)
        walker.skill_view_calls.add("call_1")
        self.assertIs(walker.skill_listings_sorted(message), message)
        self.assertEqual(normalizer.sorted_skill_listings("<persisted-output>"), "<persisted-output>")


class AnswerSlotTest(unittest.TestCase):
    CONTEXT = normalizer.KeyContext("c", "i")

    @staticmethod
    def convo(said, shown, task="t_aaaaaaaa"):
        return normalizer.normalize_request({"model": "m", "messages": [
            {"role": "user", "content": f"work kanban task {task}"},
            {"role": "assistant", "content": said},
            {"role": "user", "content": shown},
        ]}, NOW)

    def test_the_slot_key_ignores_which_answer_and_keeps_what_followed(self):
        a, a_slots = normalizer.answer_slots(self.convo("Yes please.", "ticket Tea"), self.CONTEXT)
        b, b_slots = normalizer.answer_slots(self.convo("Yes.", "ticket Tea", task="t_bbbbbbbb"), self.CONTEXT)
        c, _ = normalizer.answer_slots(self.convo("Yes.", "ticket Latte"), self.CONTEXT)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a_slots[0][0], b_slots[0][0], "both answers fill the same context")
        self.assertNotEqual(a_slots[0][1], b_slots[0][1])

    @staticmethod
    def acted(name, arguments, said, call_id):
        return normalizer.normalize_request({"model": "m", "messages": [
            {"role": "user", "content": "work kanban task t_aaaaaaaa"},
            {"role": "assistant", "content": said, "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}}]},
            {"role": "tool", "tool_call_id": call_id, "content": '{"ok": true}'},
        ]}, NOW)

    def test_the_slot_key_keeps_the_tool_calls_and_blanks_only_the_words(self):
        get = {"command": "kubectl get pods"}
        base, _ = normalizer.answer_slots(self.acted("terminal", get, "Looking.", "call_1__thought__QUJD"), self.CONTEXT)
        worded, slots = normalizer.answer_slots(
            self.acted("terminal", get, "Let me look.", "call_8__thought__WFla"), self.CONTEXT)
        self.assertEqual(base, worded, "other words, another call id and thought signature: the same slot")
        for name, arguments in (("kanban_complete", get), ("terminal", {"command": "kubectl delete pods --all"})):
            other, _ = normalizer.answer_slots(self.acted(name, arguments, "Looking.", "call_1__thought__QUJD"), self.CONTEXT)
            self.assertNotEqual(base, other, (name, arguments))
        self.assertNotEqual(slots[0][1], normalizer.answer_slots(
            self.acted("terminal", get, "Looking.", "call_1"), self.CONTEXT)[1][0][1], "the words name the answer")

    def test_a_read_only_tool_keeps_its_name_and_blanks_its_arguments(self):
        show, _ = normalizer.answer_slots(self.acted("kanban_show", {}, "", "call_1"), self.CONTEXT)
        own, slots = normalizer.answer_slots(self.acted("kanban_show", {"task_id": "t_aaaaaaaa"}, "", "call_2"), self.CONTEXT)
        self.assertEqual(show, own)
        other, _ = normalizer.answer_slots(self.acted("kanban_list", {}, "", "call_1"), self.CONTEXT)
        self.assertNotEqual(show, other, "the name stays in the key")
        self.assertNotEqual(slots[0][1], normalizer.answer_slots(
            self.acted("kanban_show", {}, "", "call_1"), self.CONTEXT)[1][0][1], "the arguments still name the answer")

    def test_an_answer_noted_at_record_time_is_the_one_a_later_slot_holds(self):
        asked = {"model": "m", "messages": [{"role": "user", "content": "work kanban task t_aaaaaaaa"}]}
        # As the proxy assembled it from the stream, and as the client sends it back.
        streamed = {"role": "assistant", "content": "", "tool_calls": [{"index": 0, "id": "call_1__thought__QUJD",
                    "type": "function", "function": {"name": "kubectl", "arguments": '{"pod": "web-7d4b9c8f6d-x2k9p"}'}}]}
        returned = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1__thought__QUJD",
                    "type": "function", "function": {"name": "kubectl", "arguments": '{"pod": "web-7d4b9c8f6d-x2k9p"}'}}]}
        later = dict(asked, messages=asked["messages"] + [returned, {"role": "tool", "tool_call_id": "call_1__thought__QUJD",
                                                                     "content": "ok"}])
        context, _ = normalizer.answer_slots(normalizer.normalize_request(asked, NOW), self.CONTEXT)
        _, slots = normalizer.answer_slots(normalizer.normalize_request(later, NOW), self.CONTEXT)
        self.assertEqual(slots, [(context, normalizer.answer_in_context(asked, streamed, NOW))])

    def test_placeholders_outside_answers(self):
        n = self.convo("I'll look at web-7d4b9c8f6d-x2k9p", "ticket for web-5f6c7d8b9c-m8n7q")
        self.assertEqual(normalizer.placeholders_outside_answers(n), {"⟦TASK_1⟧", "⟦POD_2⟧"})


class LongDateTest(unittest.TestCase):
    """The date Hermes writes into every system prompt, with no time of day."""

    PROMPT = "Conversation started: {} (UTC, UTC+00:00)\nModel: model-default"

    @staticmethod
    def at(year, month, day, hour=12):
        return calendar.timegm(datetime(year, month, day, hour, 0, 0).timetuple())

    def norm_at(self, text, now):
        return normalizer.Normalizer(now, keep_ips=False).text(text)

    def test_a_run_on_the_next_day_gets_the_same_key(self):
        # The nightly recorded on Wednesday; the presubmit replays on Thursday.
        recorded = self.norm_at(self.PROMPT.format("Wednesday, November 04, 2026"), self.at(2026, 11, 4, 23))
        replayed = self.norm_at(self.PROMPT.format("Thursday, November 05, 2026"), self.at(2026, 11, 5, 0))
        self.assertEqual(recorded, replayed)
        self.assertEqual(recorded, "Conversation started: ⟦TIME_today⟧ (UTC, UTC+00:00)\nModel: model-default")

    def test_how_many_days_and_which_way(self):
        now = self.at(2026, 11, 5)
        cases = {
            "November 04, 2026": "⟦TIME_past_day⟧",
            "October 20, 2026": "⟦TIME_past_days⟧",
            "March 01, 2026": "⟦TIME_past_months⟧",
            "January 05, 2020": "⟦TIME_past_years⟧",
            "November 06, 2026": "⟦TIME_future_day⟧",
            "December 25, 2026": "⟦TIME_future_months⟧",
            # The month and year edges match the timestamp buckets' (30 and 365 days).
            "December 05, 2026": "⟦TIME_future_months⟧",
            "November 05, 2027": "⟦TIME_future_years⟧",
            "December 04, 2026": "⟦TIME_future_days⟧",
        }
        for date, token in cases.items():
            self.assertEqual(self.norm_at("renew by " + date, now), "renew by " + token, date)

    def test_the_offset_after_it_picks_the_day(self):
        # 00:20 UTC on the 5th is still the 4th in New York.
        now = calendar.timegm(datetime(2026, 11, 5, 0, 20).timetuple())
        text = "Thursday, November 05, 2026 (America/New_York, UTC-05:00)"
        self.assertEqual(self.norm_at(text, now), "⟦TIME_future_day⟧ (America/New_York, UTC-05:00)")
        self.assertEqual(self.norm_at("Thursday, November 05, 2026 (UTC, UTC+00:00)", now),
                         "⟦TIME_today⟧ (UTC, UTC+00:00)")

    def test_what_is_not_a_date_stays(self):
        now = self.at(2026, 11, 5)
        for text in ("February 30, 2026", "May 2026", "November 5th", "Mayday, November 05, 2026x"):
            self.assertNotIn("⟦TIME", self.norm_at(text, now), text)

    def test_an_answer_naming_the_date_is_not_saved(self):
        # Replayed on Thursday, Wednesday's answer would write Wednesday's notes.
        body = {"messages": [{"role": "system", "content": self.PROMPT.format("Wednesday, November 04, 2026")}]}
        mapping = normalizer.normalize_request(body, self.at(2026, 11, 4)).mapping
        for answer in ('{"path": "memory/2026-11-04.md"}', "## Findings for November 04, 2026",
                       "as of November 4, 2026", "started Wednesday, November 04, 2026",
                       "notes_2026-11-04.md", "2026-11-04_standup.md", "plan_2026-11-04",
                       "## Findings for November 04", "updated Nov 4", "today is Wednesday",
                       # Spellings no list of strings caught: any date shape counts.
                       "report_20261104.md", "backup-20261104.tar", "notes-nov-04.md", "nov04", "11/04/2026",
                       "11-04-2026", "04.11.2026", "2026/11/04", "november 4", "wednesday", "Nov. 4",
                       "4 November 2026", "November 4th", "the 4th of Nov"):
            self.assertTrue(mapping.answer_repeats_date(answer), answer)
            self.assertFalse(mapping.answer_repeats_bare_number(answer), "a date is not a number: " + answer)
        for answer in ("memory/2026-11-05.md", "November 14, 2026", "2026-11-04T10:00:00Z",
                       "Nov 40", "Wednesdays", "renew before November 04, 2027", "11/05/2026", "v2026.11"):
            self.assertFalse(mapping.answer_repeats_date(answer), answer)

    def test_a_tool_output_date_counts_only_as_itself(self):
        # An old file in `ls -la` must not make "every Monday" unsaveable for
        # the rest of the conversation: only the request's own day counts
        # without a year or by weekday.
        body = {"messages": [
            {"role": "system", "content": self.PROMPT.format("Wednesday, November 04, 2026")},
            {"role": "tool", "content": "-rw-r--r-- 1 root root 1234 Nov  3  2025 passwd\nDate:   2026-10-15"}]}
        mapping = normalizer.normalize_request(body, self.at(2026, 11, 4)).mapping
        for answer in ("runs every Monday", "the Thursday run", "due November 3", "Oct 15"):
            self.assertFalse(mapping.answer_repeats_date(answer), answer)
        for answer in ("passwd changed 2025-11-03", "commit from 2026-10-15", "November 3, 2025"):
            self.assertTrue(mapping.answer_repeats_date(answer), answer)

    def test_hermes_zone_names_before_the_offset(self):
        now = calendar.timegm(datetime(2026, 11, 5, 3, 0).timetuple())
        text = "Thursday, November 05, 2026 (America/New_York, EST, UTC-05:00)"
        self.assertEqual(self.norm_at(text, now), "⟦TIME_future_day⟧ (America/New_York, EST, UTC-05:00)")

    def test_an_unrelated_bracket_is_not_read_as_the_zone(self):
        now = self.at(2026, 11, 5, 23)
        text = "November 05, 2026 (window, ends UTC+14:00)"
        self.assertEqual(self.norm_at(text, now), "⟦TIME_today⟧ (window, ends UTC+14:00)")

    def test_date_only_iso_and_ls_dates_count_days_too(self):
        # Before, both flipped bucket around midnight (within the hour, then hours).
        self.assertEqual(self.norm_at("notAfter 2026-11-05", self.at(2026, 11, 5, 0)),
                         self.norm_at("notAfter 2026-11-06", self.at(2026, 11, 6, 13)))
        self.assertEqual(self.norm_at("notAfter 2026-11-05", self.at(2026, 11, 5, 0)), "notAfter ⟦TIME_today⟧")
        self.assertEqual(self.norm_at("-rw-r--r-- 1 root root 12 Nov  3  2026 a", self.at(2026, 11, 5)),
                         "-rw-r--r-- 1 root root 12 ⟦TIME_past_days⟧ a")
        # The day itself: hours ago by the hour buckets, today by days.
        self.assertEqual(self.norm_at("-rw-r--r-- 1 root root 12 Nov  5  2026 a", self.at(2026, 11, 5)),
                         "-rw-r--r-- 1 root root 12 ⟦TIME_today⟧ a")

    def test_a_year_ls_date_with_a_time_keeps_two_moments_apart(self):
        # Day counting would give both `⟦TIME_today⟧ 10:42:01`: one key for two moments a day apart.
        self.assertNotEqual(fp(request("Built Nov  5 2026 10:42:01"), now=self.at(2026, 11, 5)),
                            fp(request("Built Nov  6 2026 10:42:01"), now=self.at(2026, 11, 6)))

    def test_a_real_ls_l_listing_still_counts_days(self):
        # ls prints a year, and no time, for a file older than about six months;
        # the time on the line above does not reach it.
        listing = ("-rw-r--r-- 1 root root  4096 Nov  4 10:42 new.conf\n"
                   "-rw-r--r-- 1 root root 12288 Apr 12  2026 old.conf\n")
        self.assertEqual(self.norm_at(listing, self.at(2026, 11, 5)),
                         "-rw-r--r-- 1 root root  4096 ⟦TIME_past_days⟧ new.conf\n"
                         "-rw-r--r-- 1 root root 12288 ⟦TIME_past_months⟧ old.conf\n")
        # With a timed line below as well, at realistic widths.
        listing += "-rw-r--r-- 1 root root   220 Nov  3 09:15 b\n"
        self.assertIn("⟦TIME_past_months⟧ old.conf", self.norm_at(listing, self.at(2026, 11, 5)))

    def assert_moments_apart(self, on_5th, on_6th):
        """The same text a day later, each read on its own day, must not share a key."""
        self.assertNotEqual(fp(request(on_5th), now=self.at(2026, 11, 5)),
                            fp(request(on_6th), now=self.at(2026, 11, 6)), on_5th)

    def test_a_time_in_a_sibling_json_field_keeps_two_moments_apart(self):
        for on_5th, on_6th in (({"date": "2026-11-05", "time": "10:42:01"}, {"date": "2026-11-06", "time": "10:42:01"}),
                               ({"built": "Nov  5 2026", "at": "10:42:01"}, {"built": "Nov  6 2026", "at": "10:42:01"}),
                               (["2026-11-05", "10:42:01"], ["2026-11-06", "10:42:01"])):
            with self.subTest(on_5th):
                self.assert_moments_apart(json.dumps(on_5th), json.dumps(on_6th))

    def test_a_time_a_json_level_away_keeps_two_moments_apart(self):
        for on_5th, on_6th in (
                # In a nested object beside the date.
                ({"date": "2026-11-05", "run": {"at": "10:42"}}, {"date": "2026-11-06", "run": {"at": "10:42"}}),
                # Beside the object the date is in.
                ({"when": {"date": "2026-11-05"}, "time": "10:42"}, {"when": {"date": "2026-11-06"}, "time": "10:42"}),
                # Beside a string that holds a JSON document with the date.
                ({"dates": json.dumps(["2026-11-05"]), "time": "10:42"},
                 {"dates": json.dumps(["2026-11-06"]), "time": "10:42"}),
                # In a string beside the date that holds a JSON document.
                ({"date": "2026-11-05", "run": json.dumps({"at": "10:42"})},
                 {"date": "2026-11-06", "run": json.dumps({"at": "10:42"})}),
                # Read as the walk reads it, parsed: the raw text has no `10:42`.
                ({"date": "2026-11-05", "run": '{"at": "\\u0031\\u0030:42"}'},
                 {"date": "2026-11-06", "run": '{"at": "\\u0031\\u0030:42"}'})):
            with self.subTest(on_5th):
                self.assert_moments_apart(json.dumps(on_5th), json.dumps(on_6th))

    def test_a_time_after_an_escaped_line_break_inside_a_json_string_is_read(self):
        # Double-escaped: once parsed, the string still holds a two-character `\n`.
        for on_5th, on_6th in (
                # Beside the date in its own string.
                ({"stdout": "Built 2026-11-05\\n10:42:01 ok"}, {"stdout": "Built 2026-11-06\\n10:42:01 ok"}),
                # In a sibling string.
                ({"date": "2026-11-05", "log": "ok\\n10:42 done"}, {"date": "2026-11-06", "log": "ok\\n10:42 done"})):
            with self.subTest(on_5th):
                self.assert_moments_apart(json.dumps(on_5th), json.dumps(on_6th))

    def test_a_time_in_an_object_key_keeps_two_moments_apart(self):
        for on_5th, on_6th in (
                ({"date": "2026-11-05", "slots": {"10:42": "booked"}}, {"date": "2026-11-06", "slots": {"10:42": "booked"}}),
                # The date's own key.
                ({"10:42": "2026-11-05"}, {"10:42": "2026-11-06"})):
            with self.subTest(on_5th):
                self.assert_moments_apart(json.dumps(on_5th), json.dumps(on_6th))

    def test_what_the_key_drops_does_not_decide_a_date(self):
        def card(events):
            return {"task": {"id": "t_11111111", "due": "2026-11-05"}, "events": events}
        claimed = {"kind": "claimed", "payload": None}
        beat = {"kind": "heartbeat", "payload": {"note": "retrying at 10:42"}}
        self.assertEqual(fp(request(json.dumps(card([claimed]))), now=self.at(2026, 11, 5)),
                         fp(request(json.dumps(card([claimed, beat]))), now=self.at(2026, 11, 5)))
        # Opaque values become placeholders, so a time-like run in one is no time.
        signed = {"due": "2026-11-05", "signature": "c2lnbmF0dXJl 10:42 PM", "tool_call_id": "call_1__thought__10:42",
                  "provider_specific_fields": {"at": "10:42"}}
        self.assertEqual(json.loads(self.norm_at(json.dumps(signed), self.at(2026, 11, 5)))["due"], "⟦TIME_today⟧")
        calls = {"due": "2026-11-05", "tool_calls": [{"id": "call_1__thought__10:42", "type": "function"}]}
        self.assertEqual(normalizer.Normalizer(self.at(2026, 11, 5), False).walk(calls)["due"], "⟦TIME_today⟧")
        # The recent-work digest is stripped from the key.
        digest = {"due": "2026-11-05", "body": "## Recent work by @bot\n- t_22222222 at 10:42\n\nrest"}
        self.assertEqual(json.loads(self.norm_at(json.dumps(digest), self.at(2026, 11, 5)))["due"], "⟦TIME_today⟧")

    def test_a_line_of_dates_is_scanned_for_times_once(self):
        # Each date asks about the same line; scanning it once per date made a
        # long single-line text cost dates x length.
        text = " ".join(["2026-11-05"] * 200)
        with mock.patch.object(normalizer.TimeIndex, "of", wraps=normalizer.TimeIndex.of) as scan:
            out = self.norm_at(text, self.at(2026, 11, 5))
        self.assertEqual(out, " ".join(["⟦TIME_today⟧"] * 200))
        self.assertEqual(scan.call_count, 1)

    def test_a_json_date_with_no_time_beside_it_still_counts_days(self):
        self.assertEqual(self.norm_at(json.dumps({"date": "2026-11-05", "status": "ok"}), self.at(2026, 11, 5)),
                         json.dumps({"date": "⟦TIME_today⟧", "status": "ok"}, ensure_ascii=False))
        doc = {"when": {"date": "2026-11-05"}, "runs": [{"status": "ok", "log": json.dumps({"n": 3})}]}
        out = self.norm_at(json.dumps(doc), self.at(2026, 11, 5))
        self.assertEqual(json.loads(out)["when"]["date"], "⟦TIME_today⟧")
        # A bucketed timestamp beside it carries no extra moment.
        out = self.norm_at(json.dumps({"date": "2026-11-05", "updated": "2026-11-05 10:42:00Z"}), self.at(2026, 11, 5))
        self.assertEqual(json.loads(out), {"date": "⟦TIME_today⟧", "updated": "⟦TIME_past_hours⟧"})

    def test_a_bucketed_timestamp_does_not_keep_a_date_as_written(self):
        # Its bucket keeps the hour, so the date beside it day-counts on both days.
        cases = {
            "seen 2026-11-05 10:42:00Z, due 2026-11-06": "seen ⟦TIME_past_hours⟧, due ⟦TIME_future_day⟧",
            "Nov  5 10:42:01 host logrotate[412]: rotated app.log to app-2026-11-05.log":
                "⟦TIME_past_hours⟧:01 host logrotate[412]: rotated app.log to app-⟦TIME_today⟧.log",
            "2026-11-05T10:42:00.123+05:30, due 2026-11-06": "⟦TIME_past_hours⟧, due ⟦TIME_future_day⟧",
            "Tue, 05 Nov 2026 10:42:01 +0000 then 2026-11-06": "⟦TIME_past_hours⟧ then ⟦TIME_future_day⟧",
            "notAfter=Nov  5 10:42:01 2026 GMT, renew 2026-11-06": "notAfter=⟦TIME_past_hours⟧, renew ⟦TIME_future_day⟧",
        }
        for text, normalized in cases.items():
            with self.subTest(text):
                self.assertEqual(self.norm_at(text, self.at(2026, 11, 5)), normalized)
                on_6th = text.replace("2026-11-06", "2026-11-07").replace("11-05", "11-06").replace("Nov  5", "Nov  6") \
                    .replace("05 Nov", "06 Nov").replace("Tue,", "Wed,")
                self.assertEqual(self.norm_at(on_6th, self.at(2026, 11, 6)), normalized)

    def test_a_timestamp_no_rule_buckets_still_keeps_the_date(self):
        # February 30th is not a timestamp the ISO rule buckets, so its time is read.
        self.assertEqual(self.norm_at("2026-02-30 10:42 then 2026-11-06", self.at(2026, 11, 5)),
                         "2026-02-30 10:42 then 2026-11-06")

    def test_a_port_is_not_a_time_of_day(self):
        self.assertEqual(self.norm_at("listening on 80:8080 since 2026-11-05", self.at(2026, 11, 5)),
                         "listening on 80:8080 since ⟦TIME_today⟧")

    def test_a_time_just_before_the_window_is_read_with_what_precedes_it(self):
        # February 30th is no timestamp a rule buckets, so its `T10:42` stays in
        # the text. The second date starts at 46, so its window starts at
        # `10:42`: read from there alone, the `T` before it is lost and `10:42`
        # looks like a time of day.
        text = "seen 2026-02-30T10:42:00Z, next check due on: 2026-11-06"
        self.assertEqual(text.index("2026-11-06") - normalizer.TIME_NEAR_DATE_CHARS, text.index("10:42"))
        self.assertEqual(self.norm_at(text, self.at(2026, 11, 5)),
                         "seen 2026-02-30T10:42:00Z, next check due on: ⟦TIME_future_day⟧")

    def test_a_time_on_the_line_or_past_the_window_edge_keeps_two_moments_apart(self):
        for on_5th in ("2026-11-05 | provisioning-cluster-request-never-creates | 10:42:01",
                       # On the next line, starting inside TIME_NEAR_DATE_CHARS and ending outside it.
                       "2026-11-05\n" + "x" * 25 + " 10:42:01",
                       # On the line before, starting outside the window and ending inside it.
                       "10:42:01" + " " * 24 + "\n2026-11-05",
                       # A `-` before the time.
                       "2026-11-05-10:42", "Nov  5 2026-10:42",
                       # A JSON-escaped line break before the time, in text that is not JSON.
                       "2026-11-05\\n10:42", '{"stdout": "Built 2026-11-05\\n10:42:01 ok"'):
            with self.subTest(on_5th):
                self.assert_moments_apart(on_5th, on_5th.replace("11-05", "11-06").replace("Nov  5", "Nov  6"))

    def test_a_time_on_the_next_line_still_keeps_the_date(self):
        self.assertEqual(self.norm_at("Date: 2026-11-05\nTime: 10:42", self.at(2026, 11, 5)), "Date: 2026-11-05\nTime: 10:42")

    def test_a_date_with_a_time_of_day_is_left_alone(self):
        now = self.at(2026, 11, 5)
        for text in ("November 05, 2026 at 10:42:01", "Thursday, November 05, 2026 10:42 AM",
                     "November 05, 2026, 9:30 PM", "November 05, 2026 - 10:42", "November 05, 2026 at 10 PM",
                     "November 05, 2026 (10:42)", "November 05, 2026 10h42", "10:42 on November 05, 2026",
                     "10pm, November 05, 2026",
                     # A word between the date and the time.
                     "November 05, 2026 (Thursday) 10:42", "November 05, 2026 at around 10:42",
                     "November 05, 2026 at approximately 3 PM", "10:42 UTC on November 05, 2026",
                     "November 05, 2026 UTC 10:42",
                     # A date-only ISO value with a time beside it that the ISO rule doesn't join.
                     "2026-11-05 at 10:42", "2026-11-05, 10:42:00"):
            self.assertEqual(self.norm_at(text, now), text)
        # An `ls -l`-shaped date with a year and a time beside it.
        for text in ("Built Nov  5 2026 10:42:01", "Built Nov  5  2026 10:42", "10:42 Nov  5  2026"):
            self.assertEqual(self.norm_at(text, now), text)
        # Hermes's zone suffix is not a time of day.
        self.assertEqual(self.norm_at("November 05, 2026 (UTC, UTC+00:00)", now), "⟦TIME_today⟧ (UTC, UTC+00:00)")
        self.assertEqual(self.norm_at("Conversation started: Thursday, November 05, 2026 (America/New_York, EST, UTC-05:00)",
                                      self.at(2026, 11, 5, 3)),
                         "Conversation started: ⟦TIME_future_day⟧ (America/New_York, EST, UTC-05:00)")


class TimeTest(unittest.TestCase):
    def test_iso_buckets(self):
        self.assertEqual(norm("2026-11-03T11:58:00Z"), "⟦TIME_past_within_hour⟧")
        self.assertEqual(norm("2026-11-03T06:00:00+00:00"), "⟦TIME_past_hours⟧")
        self.assertEqual(norm("2026-10-20 08:00"), "⟦TIME_past_days⟧")
        self.assertEqual(norm("2026-11-03T12:00:10.123Z"), "⟦TIME_future_within_hour⟧")
        self.assertEqual(norm("2027-06-01T00:00:00Z"), "⟦TIME_future_months⟧")

    def test_expired_and_valid_certificates_do_not_match(self):
        expired = request("notAfter: 2026-10-01T00:00:00Z")
        valid = request("notAfter: 2027-10-01T00:00:00Z")
        self.assertNotEqual(fp(expired), fp(valid))

    def test_same_distance_matches_across_runs(self):
        self.assertEqual(
            fp(request("created 2026-11-03T11:50:00Z")),
            fp(request("created 2026-11-04T11:50:00Z"), now=NOW + normalizer.SECONDS_PER_DAY),
        )

    def test_epoch_under_a_time_key(self):
        out = normalizer.Normalizer(NOW, False).walk(
            {"created_at": 1793440000, "ts": "1793440000123", "updatedAt": int(NOW) + 3 * 3600, "timestamp": 1793440000.5}
        )
        self.assertEqual(
            out,
            {"created_at": "⟦TIME_past_days⟧", "timestamp": "⟦TIME_past_days⟧", "ts": "⟦TIME_past_days⟧",
             "updatedAt": "⟦TIME_future_hours⟧"},
        )
        self.assertEqual(norm("created_at: 1793440000, \"ts\": 1793440000123"), "created_at: ⟦TIME_past_days⟧, \"ts\": ⟦TIME_past_days⟧")

    def test_epoch_under_a_camel_case_time_key(self):
        out = normalizer.Normalizer(NOW, False).walk(
            {"startTime": 1793440000, "creationTimestamp": "1793440000", "expiryDate": int(NOW) + 3 * 3600}
        )
        self.assertEqual(
            out,
            {"creationTimestamp": "⟦TIME_past_days⟧", "expiryDate": "⟦TIME_future_hours⟧", "startTime": "⟦TIME_past_days⟧"},
        )
        self.assertEqual(norm("startTime: 1793440000"), "startTime: ⟦TIME_past_days⟧")
        # Not a camelCase time key: the word itself, capitalised.
        self.assertEqual(normalizer.Normalizer(NOW, False).walk({"Uptime": 1793440000}), {"Uptime": 1793440000})

    def test_free_text_epoch_shaped_number_stays_literal(self):
        self.assertEqual(norm("memory: 1717986918 bytes"), "memory: 1717986918 bytes")
        self.assertEqual(norm("at 1793440000"), "at 1793440000")
        out = normalizer.Normalizer(NOW, False).walk({"size": 1717986918, "count": "1793440000"})
        self.assertEqual(out, {"count": "1793440000", "size": 1717986918})

    def test_epoch_under_a_time_key_cannot_be_put_back_in_an_answer(self):
        normalized = normalizer.normalize_request(request(json.dumps({"created_at": 1793440000})), NOW)
        self.assertTrue(normalized.mapping.answer_repeats_bare_number("sleep until 1793440000"))

    def test_ls_l_dates(self):
        self.assertEqual(norm("-rw-r--r-- 1 root root 12 Nov  3 11:40 a.txt"), "-rw-r--r-- 1 root root 12 ⟦TIME_past_within_hour⟧ a.txt")
        self.assertEqual(norm("drwxr-xr-x 2 root root 4096 Jan 15  2025 old"), "drwxr-xr-x 2 root root 4096 ⟦TIME_past_years⟧ old")

    def test_rfc1123_and_openssl_dates(self):
        self.assertEqual(norm("Tue, 03 Nov 2026 10:00:00 +0000"), "⟦TIME_past_hours⟧")
        self.assertEqual(norm("notAfter=Nov  2 04:13:22 2028 GMT"), "notAfter=⟦TIME_future_years⟧")


class IpTest(unittest.TestCase):
    def test_ips_are_mapped_without_a_cidr(self):
        self.assertEqual(norm("pod ip 10.4.0.12, node 10.128.0.5"), "pod ip ⟦IP_1⟧, node ⟦IP_2⟧")

    def test_ips_stay_literal_when_the_request_has_a_cidr(self):
        body = request("pod ip 10.4.0.12", "subnet 10.4.0.0/16")
        normalized = normalizer.normalize_request(body, NOW)
        self.assertIn("10.4.0.12", normalized.messages[0]["content"])
        self.assertIn("10.4.0.0/16", normalized.messages[1]["content"])
        self.assertNotEqual(fp(body), fp(request("pod ip 10.9.0.12", "subnet 10.4.0.0/16")))

    def test_version_string_is_not_an_ip(self):
        self.assertEqual(norm("1.2.3.4.5 and 999.1.1.1"), "1.2.3.4.5 and 999.1.1.1")


class OpaqueTest(unittest.TestCase):
    SIGNED_ID = "call_t_3c176184_10.4.0.12__thought__AbC123+/="

    def body(self, call_id, signature):
        return {
            "model": "model-default",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "kanban_show", "arguments": '{"task_id": "t_3c176184"}'},
                            "provider_specific_fields": {"thought_signature": signature},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": call_id, "content": "ok"},
            ],
        }

    def test_opaque_values_are_never_edited_inside(self):
        normalized = normalizer.normalize_request(self.body(self.SIGNED_ID, "sig-t_3c176184"), NOW)
        call = normalized.messages[0]["tool_calls"][0]
        self.assertEqual(call["id"], "⟦OPAQUE_1⟧")
        self.assertEqual(normalized.messages[1]["tool_call_id"], "⟦OPAQUE_1⟧")
        self.assertEqual(call["provider_specific_fields"], "⟦OPAQUE_2⟧")
        self.assertEqual(normalized.mapping.to_placeholder["OPAQUE" + normalizer.MAPPING_KEY_SEPARATOR + self.SIGNED_ID],
                         "⟦OPAQUE_1⟧")
        # Classes never reach inside an opaque value.
        self.assertNotIn("⟦TASK", json.dumps(call["id"]))
        self.assertEqual(json.loads(call["function"]["arguments"]), {"task_id": "⟦TASK_1⟧"})

    def test_normalizing_does_not_modify_the_request(self):
        body = self.body(self.SIGNED_ID, "sig")
        before = json.dumps(body, sort_keys=True)
        normalizer.normalize_request(body, NOW)
        self.assertEqual(json.dumps(body, sort_keys=True), before)

    def test_signature_fields_are_opaque(self):
        out = normalizer.Normalizer(NOW, False).walk({"thinking_blocks": [{"signature": "t_3c176184", "thinking": "t_3c176184"}]})
        self.assertEqual(out, {"thinking_blocks": [{"signature": "⟦OPAQUE_1⟧", "thinking": "⟦TASK_1⟧"}]})

    def test_a_meaningful_string_id_is_not_hidden(self):
        def tool_result(content):
            body = self.body(self.SIGNED_ID, "s")
            body["messages"][-1]["content"] = content
            return fp(body)
        self.assertNotEqual(
            tool_result(json.dumps({"items": [{"id": "cluster-a", "status": "RUNNING"}]})),
            tool_result(json.dumps({"items": [{"id": "cluster-b", "status": "RUNNING"}]})),
        )

    def test_a_kanban_card_id_field_still_maps_as_a_task(self):
        normalized = normalizer.normalize_request(
            {"messages": [{"role": "tool", "content": json.dumps({"task": {"id": "t_3c176184"}})}]}, NOW
        )
        self.assertIn("⟦TASK_1⟧", json.dumps(normalized.messages, ensure_ascii=False))

    def test_claude_tool_use_ids_match(self):
        self.assertEqual(fp(self.body("toolu_01AbCdEf", "s")), fp(self.body("toolu_01ZyXwVu", "s")))
        normalized = normalizer.normalize_request(self.body("toolu_01AbCdEf", "s"), NOW)
        self.assertEqual(normalized.messages[0]["tool_calls"][0]["id"], "⟦OPAQUE_1⟧")
        self.assertEqual(normalized.messages[1]["tool_call_id"], "⟦OPAQUE_1⟧")

    def test_an_id_outside_tool_calls_is_not_opaque(self):
        out = normalizer.Normalizer(NOW, False).walk({"items": [{"id": "call web-7d4b9c8f6d-x2k9p"}]})
        self.assertEqual(out, {"items": [{"id": "call web-⟦POD_1⟧"}]})

    def test_runs_differing_only_in_call_ids_match(self):
        self.assertEqual(fp(self.body("call_aaa__thought__x", "s1")), fp(self.body("call_bbb__thought__y", "s2")))


class RecentWorkTest(unittest.TestCase):
    CARD = "# Card t_3c176184\nbody\n\n## Recent work by @platform\n- t_11111111 — {title} (2026-11-02 10:00, 18h ago): done\n- t_22222222 — y: ok\n\n## Comment thread\nnone"
    # The same view with no digest, as Hermes renders it when the assignee has no other runs.
    BARE_CARD = "# Card t_3c176184\nbody\n\n## Comment thread\nnone"

    def test_block_is_dropped_from_the_key(self):
        a = request(self.CARD.format(title="audit"))
        b = request(self.CARD.format(title="something else entirely"))
        self.assertEqual(fp(a), fp(b))
        self.assertIn("body\n\n## Comment thread", normalizer.normalize_request(a, NOW).messages[0]["content"])

    def test_a_view_with_the_digest_keys_as_the_view_without_it(self):
        self.assertEqual(fp(request(self.CARD.format(title="audit"))), fp(request(self.BARE_CARD)))
        # Hermes puts it last in a worker's context: the blank line before it goes too.
        self.assertEqual(norm("flat text.\n\n## Recent work by @cluster-a\n- t_11111111 — a: b"), "flat text.")
        self.assertEqual(norm("flat text.\n\n## Recent work by @cluster-a\n- t_11111111 — a: b\n"), "flat text.\n")

    def test_what_is_around_the_digest_still_counts(self):
        self.assertNotEqual(fp(request(self.CARD.format(title="audit"))),
                            fp(request(self.BARE_CARD.replace("body", "other body"))))
        self.assertNotEqual(norm("a\n\n## Recent work by @x\n- t_11111111 — y\nnot an item"), norm("a"))

    def test_block_inside_json_escaped_text(self):
        raw = '{"output": "x\\n## Recent work by @platform\\n- t_11111111 — a: b\\n\\nrest"'  # truncated JSON
        self.assertEqual(norm(raw), '{"output": "x\\nrest"')
        raw = '{"output": "x\\n\\n## Recent work by @platform\\n- t_11111111 — a: b\\n\\nrest"'
        self.assertEqual(norm(raw), '{"output": "x\\n\\nrest"')


class EvalPoolProjectTest(unittest.TestCase):
    def test_the_project_is_an_identifier_wherever_the_pool_writes_it(self):
        cases = (
            ("project kube-agents-evals-31", "project ⟦EVALPROJECT_1⟧"),
            ("cluster-kube-agents-evals-31-seeded-a-us-central1-a", "cluster-⟦EVALPROJECT_1⟧-seeded-a-us-central1-a"),
            ("gke_kube-agents-evals-31_us-central1-a_seeded-a", "gke_⟦EVALPROJECT_1⟧_us-central1-a_seeded-a"),
            ("gke-agentic/kube-agents-evals-31-infra", "gke-agentic/⟦EVALPROJECT_1⟧-infra"),
            ("sa@kube-agents-evals-31.iam.gserviceaccount.com", "sa@⟦EVALPROJECT_1⟧.iam.gserviceaccount.com"),
            # The first pool project has no number.
            ("--project=kube-agents-evals,", "--project=⟦EVALPROJECT_1⟧,"),
            ("gke-agentic/kube-agents-evals-infra", "gke-agentic/⟦EVALPROJECT_1⟧-infra"),
            ("cluster-kube-agents-evals-seeded-a", "cluster-⟦EVALPROJECT_1⟧-seeded-a"),
            ("kube-agents-evals-us-central1.kubeagents-system", "⟦EVALPROJECT_1⟧-us-central1.kubeagents-system"),
        )
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(norm(raw), want)

    def test_runs_in_different_pool_projects_match(self):
        def body(project):
            return request(f"You are cluster-{project}-seeded-a-us-central1-a, project {project}",
                           json.dumps({"output": f"gcloud container clusters list --project={project}"}))
        self.assertEqual(fp(body("kube-agents-evals-31")), fp(body("kube-agents-evals-29")))
        self.assertEqual(fp(body("kube-agents-evals-31")), fp(body("kube-agents-evals")))

    def test_a_percent_escaped_project_in_a_url(self):
        self.assertEqual(norm("project_id%3D%22kube-agents-evals-22%22"), "project_id%3D%22⟦EVALPROJECT_1⟧%22")
        self.assertEqual(norm("gitlab.com%2Fgke-agentic%2Fkube-agents-evals-22-infra"),
                         "gitlab.com%2Fgke-agentic%2F⟦EVALPROJECT_1⟧-infra")
        self.assertEqual(norm("x%2Fkube-agents-evals-token-minter"), "x%2Fkube-agents-evals-token-minter")

    def test_two_projects_in_one_request_stay_apart(self):
        self.assertEqual(norm("kube-agents-evals-3 and kube-agents-evals-31"), "⟦EVALPROJECT_1⟧ and ⟦EVALPROJECT_2⟧")

    def test_names_that_are_no_pool_project_stay_literal(self):
        for raw in ("kube-agents-evals-token-minter[bot]", "gke-agentic/kube-agents-evals-large-gitops",
                    "kube-agents-evals-project", "gs://kube-agents-evals-nightly-logs/", "kube-agents-evals-ledger-reader",
                    "kube-agents-evals-31x", "xkube-agents-evals-31", "kube-agents-evalsx", "kube-agents-evals-2b"):
            with self.subTest(raw=raw):
                self.assertEqual(norm(raw), raw)


class RecentWorkGuardTest(unittest.TestCase):
    CARD = "card t_bbbbbbbb\n\n## Recent work by @platform\n- t_deadbeef — synced the repo at 0ffe0cb (2026-11-02 10:00, 18m ago): done"

    def test_a_commit_only_the_digest_names_counts_against_an_answer(self):
        walker = normalizer.Normalizer(NOW, False)
        walker.text(self.CARD)
        self.assertTrue(walker.mapping.has_identifier("git checkout 0ffe0cb"))
        self.assertFalse(walker.mapping.has_identifier("git checkout 0ffe0cc"))
        # Digits alone are a number, not a commit.
        walker.text("x\n\n## Recent work by @platform\n- t_deadbeef — wrote 1234567 rows")
        self.assertFalse(walker.mapping.has_identifier("wrote 1234567 rows"))

    def test_a_digest_commit_at_another_length_counts_against_an_answer(self):
        # The answer may shorten the digest's full SHA, or lengthen its short one.
        full = "0ffe0cbd6b463ad4c3d2e1f0a9b8c7d6e5f4a3b2"
        walker = normalizer.Normalizer(NOW, False)
        walker.text(f"x\n\n## Recent work by @platform\n- t_deadbeef — synced the repo at {full}: done")
        self.assertTrue(walker.mapping.has_identifier("git checkout 0ffe0cb"))
        self.assertFalse(walker.mapping.has_identifier("git checkout 0ffe0cc"))
        walker = normalizer.Normalizer(NOW, False)
        walker.text(self.CARD)
        self.assertTrue(walker.mapping.has_identifier(f"git checkout {full}"))

    def test_a_digest_commit_in_an_identifier_shape_counts_as_bare_hex(self):
        # The throwaway normalizer binds these, so the hex never reaches the run scan.
        sha = "3f2a9c1d8e7b6a5f4c3d2e1f0a9b8c7d6e5f4a3b"
        for written in (f"acme/fleet@{sha}", f"https://github.com/acme/fleet/commit/{sha}"):
            with self.subTest(written=written):
                walker = normalizer.Normalizer(NOW, False)
                walker.text(f"x\n\n## Recent work by @platform\n- t_deadbeef — merged {written}: done")
                self.assertTrue(walker.mapping.has_identifier(f"reset to {sha}"))
                self.assertTrue(walker.mapping.has_identifier(f"reset to {sha[:7]}"))
                self.assertFalse(walker.mapping.has_identifier("reset to 4f1e4140"))

    def test_the_digests_unrestorable_values_count_against_an_answer(self):
        walker = normalizer.Normalizer(NOW, False)
        walker.text(self.CARD)
        self.assertTrue(walker.mapping.answer_repeats_age("logs --since 18m"))
        self.assertEqual(walker.mapping.counts, {"TASK": 1})


class RunNameTest(unittest.TestCase):
    def test_a_repository_commit(self):
        self.assertEqual(norm("searched gke-agentic/kube-agents-evals-31-infra@c99b1ab (whole tree)"),
                         "searched gke-agentic/⟦EVALPROJECT_1⟧-infra@⟦COMMIT_1⟧ (whole tree)")
        self.assertEqual(norm('["acme/fleet@3f2a9c1d8e7b6a5f4c3d2e1f0a9b8c7d6e5f4a3b"]'), '["acme/fleet@⟦COMMIT_1⟧"]')
        self.assertEqual(norm("acme/infra@0ffe0cb"), norm("acme/infra@4f1e414"))

    def test_a_commit_in_a_forge_url(self):
        sha = "0045315ed5ba4ce8f1091c2dcbc7626fde7f2aaa"
        self.assertEqual(norm(f"https://github.com/gke-agentic/x/blob/{sha}/README.md"),
                         "https://github.com/gke-agentic/x/blob/⟦COMMIT_1⟧/README.md")
        self.assertEqual(norm(f"x/tree/{sha} and x/commit/{sha[:7]}"), "x/tree/⟦COMMIT_1⟧ and x/commit/⟦COMMIT_2⟧")
        self.assertEqual(norm("x/blob/main/README.md and x/blob/v1.2.3/a"), "x/blob/main/README.md and x/blob/v1.2.3/a")

    def test_what_is_not_a_repository_commit_stays(self):
        for raw in ("user@deadbee1.example", "acme/infra@v1.2.3", "acme/infra@abc12", "img@sha256:0ffe0cbd6b463ad4",
                    "acme/infra@0ffe0cbz", "commit 0ffe0cb"):
            with self.subTest(raw=raw):
                self.assertEqual(norm(raw), raw)

    def test_the_declared_intent_clone_directory(self):
        self.assertEqual(norm("--into /opt/data/scratch/declared-intent-a2_elobt --lease x"),
                         "--into /opt/data/scratch/declared-intent-⟦TMPNAME_1⟧ --lease x")
        self.assertEqual(norm("declared-intent-j77z8q6q/"), norm("declared-intent-fnrk82en/"))
        for raw in ("declared-intent-no-finding", "compliance-declared-intent-no-finding", "declared-intent-test",
                    "declared-intent-abcdefghi", "xdeclared-intent-a2_elobt"):
            with self.subTest(raw=raw):
                self.assertEqual(norm(raw), raw)

    def test_a_node_name_that_holds_the_pool_project(self):
        self.assertEqual(norm("drained gke-kube-agents-evals-31-default-pool-abcdef12-x1y2"),
                         "drained gke-⟦EVALPROJECT_1⟧-default-pool-⟦NODE_1⟧")
        self.assertEqual(norm("gke-kube-agents-evals-31-default-pool-abcdef12-x1y2 gke-seeded-a-pool-1-12345678-wxyz"),
                         "gke-⟦EVALPROJECT_1⟧-default-pool-⟦NODE_1⟧ gke-seeded-a-pool-1-⟦NODE_2⟧")
        self.assertTrue(normalizer.Mapping().has_identifier("gke-⟦EVALPROJECT_1⟧-default-pool-abcdef12-x1y2"))

    def test_a_tempfile_name_or_operation_after_a_dash_or_underscore_in_an_answer(self):
        # No pattern finds these shapes in the answer; the whole-token swap does,
        # because a suffix class may follow `-` or `_` (SUFFIX_CLASSES).
        op = "1793806478493-11b21f18-4eef-47ea-8ce1-fbd3c40047b3"
        mapping = normalizer.normalize_request(
            request(f"cloned into /opt/data/scratch/declared-intent-j77z8q6q; newest operation-{op}"), NOW).mapping
        self.assertEqual(mapping.apply_to_answer("mv clone-j77z8q6q old_" + op),
                         "mv clone-⟦TMPNAME_1⟧ old_⟦GKEOP_1⟧")

    def test_a_gke_operation_id(self):
        op = "operation-1793806478493-11b21f18-4eef-47ea-8ce1-fbd3c40047b3"
        self.assertEqual(norm(f"newest operation {op} (UPGRADE_NODES)"), "newest operation operation-⟦GKEOP_1⟧ (UPGRADE_NODES)")
        self.assertEqual(norm(op), norm("operation-1792900340178-5482a62f-d7e3-4c54-bb09-236983a9dfb7"))
        # The operation's type and status are state and stay.
        self.assertNotEqual(norm(f"{op} (UPGRADE_NODES, DONE)"), norm(f"{op} (UPGRADE_NODES, RUNNING)"))
        self.assertEqual(norm("operation-999999999999-9f9999"), "operation-999999999999-9f9999")


class NestedJsonTest(unittest.TestCase):
    def test_tool_result_json_is_normalized_inside(self):
        content = json.dumps({"output": "pod web-7d4b9c8f6d-x2k9p age: 5m", "exit_code": 0, "pid": 4821})
        self.assertEqual(
            json.loads(norm(content)), {"exit_code": 0, "output": "pod web-⟦POD_1⟧ age: ⟦AGEm_1⟧", "pid": "⟦NUM_1⟧"}
        )


class FingerprintTest(unittest.TestCase):
    def test_runs_differing_only_in_identifiers_match(self):
        a = request("work kanban task t_11111111 on pod web-7d4b9c8f6d-x2k9p")
        b = request("work kanban task t_22222222 on pod web-5f6c7d8b9c-q8rtz")
        self.assertEqual(fp(a), fp(b))

    def test_delivery_and_caller_fields_are_dropped(self):
        a = request("hi")
        b = request("hi", stream=True, user="someone", metadata={"x": 1})
        self.assertEqual(fp(a), fp(b))

    def test_settings_are_in_the_key(self):
        base = fp(request("hi"))
        for extra in ({"temperature": 0.5}, {"max_tokens": 10}, {"tool_choice": "none"}, {"reasoning_effort": "low"},
                      {"tools": [{"type": "function", "function": {"name": "x"}}]}):
            self.assertNotEqual(fp(request("hi", **extra)), base, extra)
        self.assertNotEqual(fp(request("hi", model="other")), base)

    def test_litellm_setup_and_normalizer_version_are_in_the_key(self):
        body = request("hi")
        base = fp(body)
        self.assertNotEqual(fp(body, context=normalizer.KeyContext("other", "litellm:1")), base)
        self.assertNotEqual(fp(body, context=normalizer.KeyContext("sum", "litellm:2")), base)
        self.assertNotEqual(fp(body, context=normalizer.KeyContext("sum", "litellm:1", "v2")), base)

    def test_normalizer_version_is_a_hash_of_the_source(self):
        import hashlib
        from pathlib import Path

        source = Path(normalizer.__file__).read_bytes()
        self.assertEqual(
            normalizer.NORMALIZER_VERSION,
            hashlib.sha256(source).hexdigest()[: normalizer.NORMALIZER_VERSION_HEX_CHARS],
        )

    def test_message_hashes_of_a_shared_prefix_are_equal(self):
        short = normalizer.normalize_request(request("task t_11111111", "pod web-7d4b9c8f6d-x2k9p"), NOW)
        long = normalizer.normalize_request(
            request("task t_22222222", "pod web-5f6c7d8b9c-q8rtz", "t_33333333 and t_22222222"), NOW
        )
        self.assertEqual(short.message_hashes, long.message_hashes[:2])

    def test_lone_surrogate_hashes(self):
        self.assertEqual(len(fp(request("half an emoji \ud83d"))), 64)

    def test_a_tool_result_change_changes_the_key(self):
        self.assertNotEqual(fp(request("READY 0/1 CrashLoopBackOff")), fp(request("READY 1/1 Running")))


class AnswerMappingTest(unittest.TestCase):
    def test_answer_swaps_whole_tokens_but_never_bare_numbers(self):
        normalized = normalizer.normalize_request(request("task t_11111111 pid 9921"), NOW)
        mapping = normalized.mapping
        self.assertEqual(mapping.apply_to_answer("kanban_show(t_11111111) row 9921"), "kanban_show(⟦TASK_1⟧) row 9921")
        self.assertEqual(mapping.apply_to_answer("xt_11111111"), "xt_11111111")
        self.assertTrue(mapping.answer_repeats_bare_number("kill 9921"))
        self.assertFalse(mapping.answer_repeats_bare_number("kill 99210"))

    def test_answer_swaps_a_pod_or_node_tail_after_its_dash(self):
        mapping = normalizer.normalize_request(
            request("pod web-7d4b9c8f6d-x2k9p on gke-seeded-a-default-pool-1a2b3c4d-wxyz"), NOW).mapping
        self.assertEqual(mapping.apply_to_answer("describe web-7d4b9c8f6d-x2k9p"), "describe web-⟦POD_1⟧")
        self.assertEqual(mapping.apply_to_answer("drain gke-seeded-a-default-pool-1a2b3c4d-wxyz"),
                         "drain gke-seeded-a-default-pool-⟦NODE_1⟧")
        self.assertEqual(mapping.apply_to_answer("x7d4b9c8f6d-x2k9p"), "x7d4b9c8f6d-x2k9p")

    def test_restore_puts_this_requests_values_back(self):
        mapping = normalizer.normalize_request(request("task t_11111111 pod web-7d4b9c8f6d-x2k9p age: 5m"), NOW).mapping
        self.assertEqual(mapping.to_value["⟦TASK_1⟧"], "t_11111111")
        restored, missing = mapping.restore("kanban_show(⟦TASK_1⟧) on web-⟦POD_1⟧")
        self.assertEqual(restored, "kanban_show(t_11111111) on web-7d4b9c8f6d-x2k9p")
        self.assertEqual(missing, [])
        # An age is never put back: this run's would be another reading of the clock.
        _, missing = mapping.restore("⟦AGEm_1⟧ old")
        self.assertEqual(missing, ["⟦AGEm_1⟧"])
        _, missing = mapping.restore("and ⟦TASK_2⟧")
        self.assertEqual(missing, ["⟦TASK_2⟧"])

    def test_an_age_is_never_swapped_into_an_answer_and_repeating_one_counts(self):
        mapping = normalizer.normalize_request(request("pod web-7d4b9c8f6d-x2k9p AGE: 5m"), NOW).mapping
        self.assertEqual(mapping.apply_to_answer("kubectl logs --since 5m"), "kubectl logs --since 5m")
        self.assertTrue(mapping.answer_repeats_age("kubectl logs --since 5m"))
        for answer in ("cpu 25m", "requests 5mi", "x5m"):
            self.assertFalse(mapping.answer_repeats_age(answer), answer)

    def test_repeated_number_uses_token_boundaries(self):
        mapping = normalizer.normalize_request(request("resourceVersion: 4821"), NOW).mapping
        for answer in ("upgrade to 4821.31", "use model-4821-pro", "pool node-pool-4821", "api v4821", "--offset=-4821"):
            self.assertFalse(mapping.answer_repeats_bare_number(answer), answer)
        for answer in ("kill 4821", "resourceVersion is 4821."):
            self.assertTrue(mapping.answer_repeats_bare_number(answer), answer)

    def test_short_keyed_numbers_never_mark_an_answer(self):
        # generation: 1 stays literal, so an answer full of 1s is still storable.
        mapping = normalizer.normalize_request(request("generation: 1\nobservedGeneration: 1\nreplicas: 3"), NOW).mapping
        for answer in ("kubectl get deploy -o yaml 2>&1", "revision 1", "2/2 ready", "kill 1", "replicas 3"):
            self.assertFalse(mapping.answer_repeats_bare_number(answer), answer)
        for answer in ("generation=1", "observedGeneration: 1", "the generation is 1"):
            self.assertTrue(mapping.answer_repeats_bare_number(answer), answer)


    def test_an_answer_repeating_a_comment_id_is_refused(self):
        # A row id is never swapped into an answer, so one repeating it is not
        # stored: replayed, it would name the recording's comment.
        tool = {"role": "tool", "tool_call_id": "call_1",
                "content": json.dumps({"ok": True, "task_id": "t_11111111", "comment_id": 41})}
        mapping = normalizer.normalize_request({"model": "m", "messages": [tool]}, NOW).mapping
        self.assertEqual(mapping.apply_to_answer("posted comment 41"), "posted comment 41")
        for answer in ("posted comment 41 on the card", "comment #41", '{"comment_id": 41}', "comment_id is 41",
                       "Comment 41 has been posted", "Comment #41:", "COMMENT 41"):
            self.assertTrue(mapping.answer_repeats_bare_number(answer), answer)
        for answer in ("41 pods are pending", "comments 41", "port 41", "comment_id 410", "commented 41 times"):
            self.assertFalse(mapping.answer_repeats_bare_number(answer), answer)

    def test_the_word_comment_counts_only_against_a_comment_id(self):
        # "comment" is an ordinary word: an answer putting another key's swapped
        # number after it (a generation, a row id) is still stored.
        tool = {"role": "tool", "tool_call_id": "call_1",
                "content": json.dumps({"metadata": {"generation": 2, "id": 7}, "status": {"observedGeneration": 2}})}
        mapping = normalizer.normalize_request({"model": "m", "messages": [tool]}, NOW).mapping
        for answer in ("I left a comment: 2 pods are pending", "Here is the comment:\n\n2. Root cause",
                       "comment (7)", "kubectl annotate deploy/web comment=2"):
            self.assertFalse(mapping.answer_repeats_bare_number(answer), answer)
        self.assertTrue(mapping.answer_repeats_bare_number("generation: 2"))
        # The same word does count once a comment id was swapped, in text as in JSON.
        text = {"role": "tool", "tool_call_id": "call_2", "content": "posted, comment_id: 2"}
        mapping = normalizer.normalize_request({"model": "m", "messages": [text]}, NOW).mapping
        self.assertTrue(mapping.answer_repeats_bare_number("I left a comment: 2 pods are pending"))

class HistorySwapTest(unittest.TestCase):
    """An answer comes back in the next request's history keyed as apply_to_answer stored it."""

    OP_RECORDED = "1793806478493-11b21f18-4eef-47ea-8ce1-fbd3c40047b3"
    OP_REPLAYED = "1792900340178-5482a62f-d7e3-4c54-bb09-236983a9dfb7"

    @staticmethod
    def turn(shown, said):
        return {"model": "m", "messages": [{"role": "user", "content": "go"},
                                           {"role": "tool", "tool_call_id": "call_1", "content": shown},
                                           {"role": "assistant", "content": said}]}

    def assert_keys_alike(self, shown, said, recorded, replayed, expected):
        a = normalizer.normalize_request(self.turn(shown.format(*recorded), said.format(*recorded)), NOW)
        b = normalizer.normalize_request(self.turn(shown.format(*replayed), said.format(*replayed)), NOW)
        self.assertEqual(a.messages[2]["content"], expected)
        self.assertEqual(a.messages, b.messages)
        self.assertEqual(fp(self.turn(shown.format(*recorded), said.format(*recorded))),
                         fp(self.turn(shown.format(*replayed), said.format(*replayed))))
        # The history keys the answer as record mode stored it.
        asked = dict(self.turn(shown.format(*recorded), ""), messages=self.turn(shown.format(*recorded), "")["messages"][:2])
        self.assertEqual(normalizer.normalize_request(asked, NOW).mapping.apply_to_answer(said.format(*recorded)), expected)

    def test_a_bare_commit_in_history_keys_the_same_across_runs(self):
        self.assert_keys_alike("cloned acme/app@{}", "git -C app checkout {}", ("c99b1ab",), ("4f1e414",),
                               "git -C app checkout ⟦COMMIT_1⟧")

    def test_a_commit_equal_to_a_bound_uuids_segment_never_splits_it(self):
        # apply_to_answer's order: the bound uuid is looked up before the
        # bare-commit swap, so the commit equal to its first segment stays inside it.
        self.assert_keys_alike("cloned acme/app@{0} uid {1}", "uid {1}",
                               ("11b21f18", "11b21f18-4eef-47ea-8ce1-fbd3c40047b3"),
                               ("5482a62f", "5482a62f-d7e3-4c54-bb09-236983a9dfb7"),
                               "uid ⟦UID_1⟧")

    def test_a_bound_suffix_token_outside_its_pattern_keys_the_same(self):
        self.assert_keys_alike("cloned into /opt/data/scratch/declared-intent-{}; newest operation-{}",
                               "mv clone-{} old_{}", ("j77z8q6q", self.OP_RECORDED), ("a2_elobt", self.OP_REPLAYED),
                               "mv clone-⟦TMPNAME_1⟧ old_⟦GKEOP_1⟧")

    def test_the_answer_slot_check_agrees(self):
        asked = {"model": "m", "messages": self.turn("cloned acme/app@{}", "")["messages"][:2]}
        identities = set()
        for sha in ("c99b1ab", "4f1e414"):
            body = json.loads(json.dumps(asked).replace("{}", sha))
            identities.add(normalizer.answer_in_context(body, {"role": "assistant", "content": f"checkout {sha}"}, NOW))
        self.assertEqual(len(identities), 1)

    def test_lookalikes_stay_literal(self):
        full = "c99b1ab1fee80c1d2e3f40516273849a5b6c7d8e"
        for shown, said in (
            # Another commit, a longer and a shorter run, a run inside a word, and too short to be a commit.
            ("cloned acme/app@c99b1ab", "checkout c99b1ac c99b1ab0 xc99b1ab c99b1a deadbeef1"),
            # An abbreviation of a bound commit: no exact run, as apply_to_answer reads it.
            (f"cloned acme/app@{full}", "checkout c99b1ab"),
            # A bound tail inside a longer word.
            ("cloned into declared-intent-j77z8q6q", "rm xj77z8q6q j77z8q6qx"),
        ):
            with self.subTest(said=said):
                n = normalizer.normalize_request(self.turn(shown, said), NOW)
                self.assertEqual(n.messages[2]["content"], said)
        # Only an assistant message, and only with values bound before it.
        n = normalizer.normalize_request({"model": "m", "messages": [
            {"role": "assistant", "content": "checkout c99b1ab"},
            {"role": "tool", "tool_call_id": "call_1", "content": "cloned acme/app@c99b1ab; at c99b1ab"},
        ]}, NOW)
        self.assertEqual(n.messages[0]["content"], "checkout c99b1ab")
        self.assertEqual(n.messages[1]["content"], "cloned acme/app@⟦COMMIT_1⟧; at c99b1ab")

    def test_a_request_without_such_a_repeat_keys_as_before(self):
        n = normalizer.normalize_request({"model": "m", "messages": [
            {"role": "user", "content": "work t_11111111 on web-7d4b9c8f6d-x2k9p"},
            {"role": "tool", "tool_call_id": "call_1", "content": "cloned acme/app@c99b1ab pid 4821 at 10.1.2.3"},
            {"role": "assistant", "content": "web-7d4b9c8f6d-x2k9p restarted 5m ago, pid 4822, see acme/app@c99b1ab",
             "tool_calls": [{"id": "call_2", "type": "function", "function": {
                 "name": "terminal", "arguments": json.dumps({"command": "kubectl get pod web-5f6c7d8b9c-m8n7q"})}}]},
        ]}, NOW)
        self.assertEqual(n.messages[2], {
            "content": "web-⟦POD_1⟧ restarted ⟦AGEm_1⟧ ago, pid ⟦NUM_2⟧, see acme/app@⟦COMMIT_1⟧", "role": "assistant",
            "tool_calls": [{"function": {"arguments": json.dumps({"command": "kubectl get pod web-⟦POD_2⟧"}, ensure_ascii=False),
                                         "name": "terminal"}, "id": "⟦OPAQUE_2⟧", "type": "function"}]})
        self.assertEqual(n.mapping.counts, {"TASK": 1, "POD": 2, "COMMIT": 1, "NUM": 2, "OPAQUE": 2, "IP": 1, "AGEm": 1})


if __name__ == "__main__":
    unittest.main()

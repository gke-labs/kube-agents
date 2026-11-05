"""The normalizer's rules, one test per rule, with the cases each rule must leave alone."""

import calendar
import json
import unittest
from datetime import datetime

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

    def test_heartbeat_events_are_left_out_of_the_key(self):
        def card(events):
            return norm(json.dumps({"task": {"id": "t_11111111", "status": "running"}, "events": events}))
        claimed = {"kind": "claimed", "payload": None}
        beat = {"kind": "heartbeat", "payload": None}
        self.assertEqual(card([claimed]), card([claimed, beat, beat]))
        self.assertNotEqual(card([claimed]), card([claimed, {"kind": "completed", "payload": None}]))

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
        # Hermes's zone suffix is not a time of day.
        self.assertEqual(self.norm_at("November 05, 2026 (UTC, UTC+00:00)", now), "⟦TIME_today⟧ (UTC, UTC+00:00)")


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

    def test_block_is_dropped_from_the_key(self):
        a = request(self.CARD.format(title="audit"))
        b = request(self.CARD.format(title="something else entirely"))
        self.assertEqual(fp(a), fp(b))
        self.assertIn("## Recent work [stripped]\n\n## Comment thread", normalizer.normalize_request(a, NOW).messages[0]["content"])

    def test_block_inside_json_escaped_text(self):
        raw = '{"output": "x\\n## Recent work by @platform\\n- t_11111111 — a: b\\n\\nrest"'  # truncated JSON
        self.assertEqual(norm(raw), '{"output": "x\\n## Recent work [stripped]\\n\\nrest"')


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


if __name__ == "__main__":
    unittest.main()

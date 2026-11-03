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
            norm('resourceVersion: "48213"\ngeneration: 7\npid 4821\n"run_id": 12, "id": 99'),
            'resourceVersion: "⟦NUM_1⟧"\ngeneration: ⟦NUM_2⟧\npid ⟦NUM_5⟧\n"run_id": ⟦NUM_3⟧, "id": ⟦NUM_4⟧',
        )

    def test_keyed_numbers_in_structure(self):
        out = normalizer.Normalizer(NOW, False).walk({"metadata": {"resourceVersion": "48213", "generation": 3}})
        self.assertEqual(out, {"metadata": {"generation": "⟦NUM_1⟧", "resourceVersion": "⟦NUM_2⟧"}})

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


class TimeTest(unittest.TestCase):
    def test_iso_buckets(self):
        self.assertEqual(norm("2026-11-03T11:58:00Z"), "⟦TIME_past_minutes⟧")
        self.assertEqual(norm("2026-11-03T06:00:00+00:00"), "⟦TIME_past_hours⟧")
        self.assertEqual(norm("2026-10-20 08:00"), "⟦TIME_past_days⟧")
        self.assertEqual(norm("2026-11-03T12:00:10.123Z"), "⟦TIME_now⟧")
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

    def test_free_text_epoch_shaped_number_stays_literal(self):
        self.assertEqual(norm("memory: 1717986918 bytes"), "memory: 1717986918 bytes")
        self.assertEqual(norm("at 1793440000"), "at 1793440000")
        out = normalizer.Normalizer(NOW, False).walk({"size": 1717986918, "count": "1793440000"})
        self.assertEqual(out, {"count": "1793440000", "size": 1717986918})

    def test_epoch_under_a_time_key_cannot_be_put_back_in_an_answer(self):
        normalized = normalizer.normalize_request(request(json.dumps({"created_at": 1793440000})), NOW)
        self.assertTrue(normalized.mapping.answer_repeats_bare_number("sleep until 1793440000"))

    def test_ls_l_dates(self):
        self.assertEqual(norm("-rw-r--r-- 1 root root 12 Nov  3 11:40 a.txt"), "-rw-r--r-- 1 root root 12 ⟦TIME_past_minutes⟧ a.txt")
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
        self.assertEqual(normalized.mapping.to_value["⟦OPAQUE_1⟧"], self.SIGNED_ID)
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

    def test_a_tool_result_change_changes_the_key(self):
        self.assertNotEqual(fp(request("READY 0/1 CrashLoopBackOff")), fp(request("READY 1/1 Running")))


class AnswerMappingTest(unittest.TestCase):
    def test_answer_swaps_whole_tokens_but_never_bare_numbers(self):
        normalized = normalizer.normalize_request(request("task t_11111111 pid 99"), NOW)
        mapping = normalized.mapping
        self.assertEqual(mapping.apply_to_answer("kanban_show(t_11111111) row 99"), "kanban_show(⟦TASK_1⟧) row 99")
        self.assertEqual(mapping.apply_to_answer("xt_11111111"), "xt_11111111")
        self.assertTrue(mapping.answer_repeats_bare_number("kill 99"))
        self.assertFalse(mapping.answer_repeats_bare_number("kill 990"))


if __name__ == "__main__":
    unittest.main()

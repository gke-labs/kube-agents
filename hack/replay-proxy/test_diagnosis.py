"""Diagnosis hashes: each message frozen as it normalized at first sighting."""

import calendar
import unittest
from datetime import datetime

import diagnosis
import normalizer

NOW = calendar.timegm(datetime(2026, 11, 3, 12, 0, 0).timetuple())
HOUR = 3600
RETAINED_LIMIT_BYTES = 1024 * 1024


def deep_size(obj, seen=None):
    """Bytes held by an object and everything it references (stdlib containers and plain objects)."""
    import sys

    seen = set() if seen is None else seen
    if id(obj) in seen or isinstance(obj, type):
        return 0
    seen.add(id(obj))
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        size += sum(deep_size(k, seen) + deep_size(v, seen) for k, v in obj.items())
    elif isinstance(obj, (list, tuple, set, frozenset)):
        size += sum(deep_size(v, seen) for v in obj)
    elif hasattr(obj, "__dict__"):
        size += deep_size(vars(obj), seen)
    return size


def user(content):
    return {"role": "user", "content": content}


class DiagnosisHasherTest(unittest.TestCase):
    def test_a_served_answer_in_history_hashes_as_the_recording_did(self):
        # The diagnosis hashes key an assistant message as the request key does
        # (Normalizer.walk_message), so an echoed answer doesn't read as diverged.
        def turn(sha):
            return [user("go"), {"role": "tool", "tool_call_id": "call_1", "content": f"cloned acme/app@{sha}"},
                    {"role": "assistant", "content": f"git -C app checkout {sha}"}]

        recorded = diagnosis.DiagnosisHasher().hashes(turn("c99b1ab"), NOW)
        replayed = diagnosis.DiagnosisHasher().hashes(turn("4f1e414"), NOW)
        self.assertEqual(recorded, replayed)

    def test_a_later_cidr_does_not_change_earlier_messages(self):
        hasher = diagnosis.DiagnosisHasher()
        first = [user("pod ip 10.4.0.12"), user("ok")]
        later = first + [user("subnet 10.4.0.0/16")]
        before = hasher.hashes(first, NOW)
        after = hasher.hashes(later, NOW)
        self.assertEqual(after[:2], before)
        # The matching hashes do drift: the CIDR keeps every IP literal.
        self.assertNotEqual(
            normalizer.normalize_request({"messages": first}, NOW).message_hashes[0],
            normalizer.normalize_request({"messages": later}, NOW).message_hashes[0],
        )

    def test_a_later_request_time_does_not_change_earlier_messages(self):
        hasher = diagnosis.DiagnosisHasher()
        first = [user("created 2026-11-03T11:50:00Z")]
        before = hasher.hashes(first, NOW)
        after = hasher.hashes(first + [user("next")], NOW + 2 * HOUR)
        self.assertEqual(after[:1], before)
        self.assertNotEqual(
            normalizer.normalize_request({"messages": first}, NOW).message_hashes,
            normalizer.normalize_request({"messages": first}, NOW + 2 * HOUR).message_hashes,
        )

    def test_a_cidr_in_a_message_keeps_its_own_and_later_ips(self):
        hasher = diagnosis.DiagnosisHasher()
        with_cidr = hasher.hashes([user("subnet 10.4.0.0/16"), user("pod 10.4.0.12")], NOW)
        other_ip = hasher.hashes([user("subnet 10.4.0.0/16"), user("pod 10.4.0.99")], NOW)
        self.assertNotEqual(with_cidr[1], other_ip[1])
        no_cidr = hasher.hashes([user("subnet none"), user("pod 10.4.0.12")], NOW)
        no_cidr_other = hasher.hashes([user("subnet none"), user("pod 10.4.0.99")], NOW)
        self.assertEqual(no_cidr[1], no_cidr_other[1])

    def test_the_same_message_after_a_different_history_is_normalized_again(self):
        hasher = diagnosis.DiagnosisHasher()
        a = hasher.hashes([user("task t_11111111"), user("then t_22222222")], NOW)
        b = hasher.hashes([user("task t_22222222"), user("then t_22222222")], NOW)
        self.assertNotEqual(a[1], b[1])

    def test_identifiers_still_normalize(self):
        hasher = diagnosis.DiagnosisHasher()
        self.assertEqual(hasher.hashes([user("task t_11111111")], NOW), hasher.hashes([user("task t_99999999")], NOW))

    def test_memory_is_linear_in_distinct_messages(self):
        sessions, turns = 10, 100
        hasher = diagnosis.DiagnosisHasher()
        for session in range(sessions):
            history = []
            for turn in range(turns):
                history.append(user(f"s{session} turn {turn} t_{session:04x}{turn:04x} uid "
                                    f"0f8e2a4c-1b3d-4e5f-8a9b-{session:06x}{turn:06x} pod web-7d4b9c8f6d-x{turn:04d}"))
                hasher.hashes(history, NOW)
        # One float per distinct message; a snapshot of the mapping per
        # message would grow with the square of the session length.
        self.assertLess(deep_size(hasher), RETAINED_LIMIT_BYTES)

    def test_first_seen_map_is_bounded(self):
        hasher = diagnosis.DiagnosisHasher(capacity=2)
        for index in range(5):
            hasher.hashes([user(f"message {index}")], NOW)
        self.assertEqual(len(hasher.first_seen), 2)

    def test_settings_hash_ignores_the_messages(self):
        hasher = diagnosis.DiagnosisHasher()
        tools = [{"type": "function", "function": {"name": "ping", "description": "pings 10.0.0.1 since 2026-11-03T11:00:00Z"}}]
        base = [user("hello")]
        bodies = [
            {"model": "m", "tools": tools, "messages": base},
            {"model": "m", "tools": tools, "messages": base + [user("pod 10.4.0.12")]},
            {"model": "m", "tools": tools, "messages": base + [user("pod 10.4.0.12"), user("subnet 10.4.0.0/16")]},
        ]
        hashes = []
        for offset, body in enumerate(bodies):
            hasher.hashes(body["messages"], NOW + offset * 2 * HOUR)
            hashes.append(hasher.settings_hash(body, {"c": "x"}, NOW + offset * 2 * HOUR))
        self.assertEqual(len(set(hashes)), 1)
        other = {"model": "m", "tools": [{"type": "function", "function": {"name": "pong"}}], "messages": base}
        self.assertNotEqual(hasher.settings_hash(other, {"c": "x"}, NOW), hashes[0])

    def test_lone_surrogate(self):
        self.assertEqual(len(diagnosis.DiagnosisHasher().hashes([user("half \ud83d")], NOW)), 1)


if __name__ == "__main__":
    unittest.main()

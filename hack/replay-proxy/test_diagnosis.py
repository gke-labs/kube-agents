"""Diagnosis hashes: each message frozen as it normalized at first sighting."""

import calendar
import unittest
from datetime import datetime

import diagnosis
import normalizer

NOW = calendar.timegm(datetime(2026, 11, 3, 12, 0, 0).timetuple())
HOUR = 3600


def user(content):
    return {"role": "user", "content": content}


class DiagnosisHasherTest(unittest.TestCase):
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

    def test_cache_is_bounded(self):
        hasher = diagnosis.DiagnosisHasher(capacity=2)
        for index in range(5):
            hasher.hashes([user(f"message {index}")], NOW)
        self.assertEqual(len(hasher._cache), 2)

    def test_lone_surrogate(self):
        self.assertEqual(len(diagnosis.DiagnosisHasher().hashes([user("half \ud83d")], NOW)), 1)


if __name__ == "__main__":
    unittest.main()

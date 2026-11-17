"""The model's own counters against the session rows."""

import unittest
from unittest import mock

from kube_agents_bench.scale import model_counters as mc


class AgainstSessionsTest(unittest.TestCase):
    def test_the_install_model_is_compared_and_other_models_are_named(self):
        rows = {"input": 100, "output": 5}
        vertex = {"gemini-3.5-flash": {"input": 100, "output": 5}, "gemini-3.1-pro-preview": {"input": 7, "output": 3}}
        check = mc.against_sessions(vertex, rows, "gemini-3.5-flash")
        self.assertEqual(check["ratio"], {"input": 1.0, "output": 1.0})
        self.assertEqual(list(check["other_models"]), ["gemini-3.1-pro-preview"])


class RequestsTest(unittest.TestCase):
    def test_callers_are_named_by_the_project_accounts(self):
        minute = {"interval": {"endTime": "1970-01-01T00:01:00Z"}}
        series = [{"resource": {"labels": {"credential_id": "serviceaccount:111"}},
                   "points": [{**minute, "value": {"int64Value": "5"}}]},
                  {"resource": {"labels": {"credential_id": "serviceaccount:999"}},
                   "points": [{**minute, "value": {"int64Value": "2"}}]}]
        with mock.patch.object(mc, "_query", return_value=series), \
                mock.patch.object(mc, "service_accounts", return_value={"111": "gw@p.iam.gserviceaccount.com"}):
            self.assertEqual(mc.requests_by_account("p", 0, 60), {"gw@p.iam.gserviceaccount.com": 5, "999": 2})


class WindowTest(unittest.TestCase):
    def test_only_minutes_that_end_inside_the_period_count(self):
        series = {"points": [{"interval": {"endTime": t}, "value": {"int64Value": "10"}}
                             for t in ("1970-01-01T00:00:00Z", "1970-01-01T00:01:00Z", "1970-01-01T00:02:00Z")]}
        self.assertEqual(mc._total(series, 0, 120), 20)


if __name__ == "__main__":
    unittest.main()

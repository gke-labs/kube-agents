#!/usr/bin/env python3
"""Tests for github_api.py — shared GitHub API client for repository scripts."""

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import github_api


def _response(raw):
    """What `urlopen` returns: a context manager yielding something with `read`."""
    return mock.MagicMock(
        __enter__=mock.Mock(return_value=mock.Mock(read=lambda: raw)),
        __exit__=mock.Mock(return_value=False),
    )


class GitHubAPITest(unittest.TestCase):
    def _api(self, responses, **kwargs):
        calls = []

        def opener(request):
            calls.append(request)
            if not responses:
                return _response(b"{}")
            idx = len(calls) - 1
            if idx < len(responses):
                outcome = responses[idx]
            else:
                outcome = responses[-1]
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, bytes):
                return _response(outcome)
            return _response(json.dumps(outcome).encode())

        api = github_api.GitHubAPI(
            "gke-labs/kube-agents",
            "test-token",
            opener=opener,
            sleep=lambda _: None,
            **kwargs,
        )
        return api, calls

    def test_headers_and_auth_are_configured(self):
        api, calls = self._api([{"ok": True}], user_agent="test-agent")
        res = api.get("/user")
        self.assertEqual(res, {"ok": True})
        self.assertEqual(len(calls), 1)
        req = calls[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(req.get_header("User-agent"), "test-agent")
        self.assertEqual(req.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(req.get_header("X-github-api-version"), "2022-11-28")

    def test_get_all_paginates_until_short_page(self):
        page1 = [{"id": 1}, {"id": 2}]
        page2 = [{"id": 3}]
        api, calls = self._api([page1, page2])
        items = api.get_all("/items", per_page=2)
        self.assertEqual(items, [{"id": 1}, {"id": 2}, {"id": 3}])
        self.assertEqual(len(calls), 2)
        self.assertIn("per_page=2&page=1", calls[0].full_url)
        self.assertIn("per_page=2&page=2", calls[1].full_url)

    def test_get_all_with_existing_query_params(self):
        page1 = [{"id": 1}]
        api, calls = self._api([page1])
        items = api.get_all("/items?state=open", per_page=10)
        self.assertEqual(items, [{"id": 1}])
        self.assertEqual(len(calls), 1)
        self.assertIn("/items?state=open&per_page=10&page=1", calls[0].full_url)

    def test_5xx_is_retried(self):
        error_503 = urllib.error.HTTPError("https://api.github.com/test", 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]
        api, calls = self._api([error_503, {"status": "ok"}])
        res = api.post("/test", {"data": 123})
        self.assertEqual(res, {"status": "ok"})
        self.assertEqual(len(calls), 2)

    def test_429_is_retried(self):
        error_429 = urllib.error.HTTPError(
            "https://api.github.com/test",
            429,
            "Too Many Requests",
            {"Retry-After": "1"},  # type: ignore[arg-type]
            None,
        )
        api, calls = self._api([error_429, {"status": "ok"}])
        res = api.post("/test", {"data": 123})
        self.assertEqual(res, {"status": "ok"})
        self.assertEqual(len(calls), 2)

    def test_secondary_rate_limit_403_is_retried(self):
        throttled = urllib.error.HTTPError(
            "https://api.github.com/test",
            403,
            "Rate Limited",
            {"Retry-After": "2"},  # type: ignore[arg-type]
            None,
        )
        api, calls = self._api([throttled, {"status": "ok"}])
        res = api.post("/test", {"data": 123})
        self.assertEqual(res, {"status": "ok"})
        self.assertEqual(len(calls), 2)

    def test_normal_403_is_not_retried(self):
        permission_error = urllib.error.HTTPError(
            "https://api.github.com/test",
            403,
            "Forbidden",
            {},  # type: ignore[arg-type]
            None,
        )
        api, calls = self._api([permission_error])
        with self.assertRaises(urllib.error.HTTPError):
            api.get("/test")
        self.assertEqual(len(calls), 1)

    def test_tolerate_status_codes(self):
        exists_error = urllib.error.HTTPError(
            "https://api.github.com/labels",
            422,
            "Unprocessable Entity",
            {},  # type: ignore[arg-type]
            None,
        )
        api, calls = self._api([exists_error])
        res = api.post("/labels", {"name": "test"}, tolerate=(422,))
        self.assertIsNone(res)
        self.assertEqual(len(calls), 1)

    def test_methods_patch_and_delete(self):
        api, calls = self._api([{"patched": True}, None])
        patch_res = api.patch("/item/1", {"name": "new"})
        self.assertEqual(patch_res, {"patched": True})
        self.assertEqual(calls[0].get_method(), "PATCH")

        delete_res = api.delete("/item/1")
        self.assertIsNone(delete_res)
        self.assertEqual(calls[1].get_method(), "DELETE")

    def test_url_error_is_retried(self):
        url_error = urllib.error.URLError("Connection refused")
        api, calls = self._api([url_error, {"status": "ok"}])
        res = api.get("/test")
        self.assertEqual(res, {"status": "ok"})
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

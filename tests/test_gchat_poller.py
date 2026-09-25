"""Unit tests for the Chat E2E message poller's credential fallback (tests/e2e/gchat_poller.py).

The Google client libraries are not installed for this suite, so the denial is a stand-in error
carrying the same `resp.status` attribute a googleapiclient HttpError does.
"""

import unittest
from types import SimpleNamespace

from tests.e2e import gchat_poller
from tests.e2e.gchat_poller import ChatMessagePoller, ChatReadAccessDenied, CredentialDescription, describe_credential

SPACE = "spaces/AAQAtest"
PAGE_SIZE = 50
ORDER_BY = "createTime desc"


class FakeHttpError(Exception):
    """Carries `resp.status` the way googleapiclient.errors.HttpError does."""

    def __init__(self, status: int, message: str = "denied") -> None:
        super().__init__(message)
        self.resp = SimpleNamespace(status=status)


class FakeChatService:
    """A `chat` v1 Resource stand-in: each call to list().execute() pops the next scripted outcome."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def spaces(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def execute(self):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


OK_PAGE = {"messages": [{"text": "5"}]}


class ChatMessagePollerTest(unittest.TestCase):
    def setUp(self):
        self.logged = []

    def make(self, primary, fallback=None):
        return ChatMessagePoller(primary, fallback, log=self.logged.append)

    def test_primary_success_never_touches_fallback(self):
        primary, fallback = FakeChatService(OK_PAGE), FakeChatService(OK_PAGE)
        poller = self.make(primary, fallback)
        self.assertEqual(poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY), OK_PAGE)
        self.assertEqual(primary.calls, [{"parent": SPACE, "pageSize": PAGE_SIZE, "orderBy": ORDER_BY}])
        self.assertEqual(fallback.calls, [])
        self.assertFalse(poller.fell_back)
        self.assertEqual(poller.auth_label, gchat_poller.PRIMARY_AUTH_LABEL)
        self.assertEqual(self.logged, [])

    def test_denied_primary_falls_back_once_and_stays_there(self):
        primary = FakeChatService(FakeHttpError(403, "administrator must grant the scope"))
        fallback = FakeChatService(OK_PAGE, OK_PAGE)
        poller = self.make(primary, fallback)

        self.assertEqual(poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY), OK_PAGE)
        self.assertTrue(poller.fell_back)
        self.assertEqual(poller.auth_label, gchat_poller.FALLBACK_AUTH_LABEL)
        self.assertIn("HTTP 403", poller.fallback_reason)
        self.assertIn("administrator must grant", poller.fallback_reason)
        self.assertEqual(len(self.logged), 1)
        self.assertIn("falling back", self.logged[0])

        # The next poll goes straight to the fallback; the primary is not retried.
        self.assertEqual(poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY), OK_PAGE)
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(fallback.calls), 2)
        self.assertEqual(len(self.logged), 1)

    def test_401_is_a_denial_too(self):
        poller = self.make(FakeChatService(FakeHttpError(401)), FakeChatService(OK_PAGE))
        self.assertEqual(poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY), OK_PAGE)
        self.assertTrue(poller.fell_back)

    def test_400_from_a_dm_under_app_auth_is_a_denial(self):
        dm_refusal = FakeHttpError(400, "DMs are not supported for methods requiring app authentication with administrator approval.")
        poller = self.make(FakeChatService(dm_refusal), FakeChatService(OK_PAGE))
        self.assertEqual(poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY), OK_PAGE)
        self.assertTrue(poller.fell_back)
        self.assertIn("DMs are not supported", poller.fallback_reason)

    def test_denied_primary_without_fallback_raises_with_the_remedy(self):
        poller = self.make(FakeChatService(FakeHttpError(403)))
        with self.assertRaises(ChatReadAccessDenied) as ctx:
            poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY)
        message = str(ctx.exception)
        self.assertIn(SPACE, message)
        self.assertIn("chat.app.messages.readonly", message)
        for name in gchat_poller.OTA_ENV_VARS:
            self.assertIn(name, message)
        self.assertIsInstance(ctx.exception.__cause__, FakeHttpError)
        self.assertFalse(poller.fell_back)

    def test_denied_fallback_raises_instead_of_polling_forever(self):
        primary = FakeChatService(FakeHttpError(403))
        fallback = FakeChatService(FakeHttpError(403, "OTA not a member"))
        poller = self.make(primary, fallback)
        with self.assertRaises(ChatReadAccessDenied) as ctx:
            poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY)
        self.assertIn(gchat_poller.FALLBACK_AUTH_LABEL, str(ctx.exception))
        self.assertIn("OTA not a member", str(ctx.exception))
        self.assertTrue(poller.fell_back)

    def test_other_http_errors_propagate_without_falling_back(self):
        primary = FakeChatService(FakeHttpError(503, "backend"), OK_PAGE)
        fallback = FakeChatService(OK_PAGE)
        poller = self.make(primary, fallback)
        with self.assertRaises(FakeHttpError):
            poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY)
        self.assertFalse(poller.fell_back)
        self.assertEqual(fallback.calls, [])
        # The poll loop retries with the same credential and succeeds.
        self.assertEqual(poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY), OK_PAGE)
        self.assertEqual(poller.auth_label, gchat_poller.PRIMARY_AUTH_LABEL)

    def test_non_http_errors_propagate_unchanged(self):
        poller = self.make(FakeChatService(TimeoutError("socket")), FakeChatService(OK_PAGE))
        with self.assertRaises(TimeoutError):
            poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY)
        self.assertFalse(poller.fell_back)

    def test_named_denial_types_fall_back_like_a_403(self):
        class FakeAuthError(Exception):
            """Stands in for google.auth.exceptions.GoogleAuthError: no HTTP status at all."""

        primary = FakeChatService(FakeAuthError("invalid scope"))
        fallback = FakeChatService(OK_PAGE)
        poller = ChatMessagePoller(primary, fallback, log=self.logged.append, denial_types=(FakeAuthError,))
        self.assertEqual(poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY), OK_PAGE)
        self.assertTrue(poller.fell_back)
        self.assertEqual(poller.fallback_reason, "FakeAuthError: invalid scope")

        denied_twice = ChatMessagePoller(FakeChatService(FakeHttpError(403)), FakeChatService(FakeAuthError("invalid_grant")),
                                         log=self.logged.append, denial_types=(FakeAuthError,))
        with self.assertRaises(ChatReadAccessDenied) as ctx:
            denied_twice.list_messages(SPACE, PAGE_SIZE, ORDER_BY)
        self.assertIn("invalid_grant", str(ctx.exception))
        self.assertIn(gchat_poller.FALLBACK_AUTH_LABEL, str(ctx.exception))

    def test_retryable_denial_type_instances_propagate(self):
        class FakeRefreshError(Exception):
            def __init__(self, message, retryable):
                super().__init__(message)
                self.retryable = retryable

        primary = FakeChatService(FakeRefreshError("token endpoint 503", retryable=True), OK_PAGE)
        fallback = FakeChatService(OK_PAGE)
        poller = ChatMessagePoller(primary, fallback, log=self.logged.append, denial_types=(FakeRefreshError,))
        with self.assertRaises(FakeRefreshError):
            poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY)
        self.assertFalse(poller.fell_back)
        self.assertEqual(fallback.calls, [])
        self.assertEqual(poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY), OK_PAGE)

        refused = ChatMessagePoller(FakeChatService(FakeRefreshError("invalid_scope", retryable=False)), FakeChatService(OK_PAGE),
                                    log=self.logged.append, denial_types=(FakeRefreshError,))
        self.assertEqual(refused.list_messages(SPACE, PAGE_SIZE, ORDER_BY), OK_PAGE)
        self.assertTrue(refused.fell_back)

    def test_reads_counts_only_successful_lists(self):
        poller = self.make(FakeChatService(FakeHttpError(503), OK_PAGE, OK_PAGE))
        with self.assertRaises(FakeHttpError):
            poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY)
        self.assertEqual(poller.reads, 0)
        poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY)
        poller.list_messages(SPACE, PAGE_SIZE, ORDER_BY)
        self.assertEqual(poller.reads, 2)
        denied = self.make(FakeChatService(FakeHttpError(403)))
        with self.assertRaises(ChatReadAccessDenied):
            denied.list_messages(SPACE, PAGE_SIZE, ORDER_BY)
        self.assertEqual(denied.reads, 0)

    def test_primary_label_comes_from_the_caller(self):
        described = CredentialDescription("service-account app auth as sa@example.iam", True)
        poller = ChatMessagePoller(FakeChatService(OK_PAGE), None, log=self.logged.append, primary_credential=described)
        self.assertEqual(poller.auth_label, described.label)

    def test_denial_remedy_matches_the_credential_kind(self):
        sa = ChatMessagePoller(FakeChatService(FakeHttpError(403)), None, log=self.logged.append,
                               primary_credential=CredentialDescription("sa", True))
        with self.assertRaises(ChatReadAccessDenied) as ctx:
            sa.list_messages(SPACE, PAGE_SIZE, ORDER_BY)
        self.assertIn("Workspace administrator", str(ctx.exception))
        self.assertIn(gchat_poller.OTA_ENV_VARS[0], str(ctx.exception))

        user = ChatMessagePoller(FakeChatService(FakeHttpError(403)), None, log=self.logged.append,
                                 primary_credential=CredentialDescription("user", False))
        with self.assertRaises(ChatReadAccessDenied) as ctx:
            user.list_messages(SPACE, PAGE_SIZE, ORDER_BY)
        self.assertIn("chat.messages.readonly", str(ctx.exception))
        self.assertNotIn("Workspace administrator", str(ctx.exception))
        self.assertIn(gchat_poller.OTA_ENV_VARS[0], str(ctx.exception))

    def test_fallback_configured_reflects_the_constructor(self):
        self.assertTrue(self.make(FakeChatService(), FakeChatService()).fallback_configured)
        self.assertFalse(self.make(FakeChatService()).fallback_configured)


class DescribeCredentialTest(unittest.TestCase):
    def test_service_account_email_names_app_auth(self):
        described = describe_credential(SimpleNamespace(service_account_email="e2e@p.iam.gserviceaccount.com"))
        self.assertTrue(described.is_service_account)
        self.assertIn("service-account app auth as e2e@p.iam.gserviceaccount.com", described.label)

    def test_metadata_server_default_is_a_service_account_without_a_name(self):
        described = describe_credential(SimpleNamespace(service_account_email="default"))
        self.assertTrue(described.is_service_account)
        self.assertIn("not resolved yet", described.label)
        self.assertNotIn("as default", described.label)

    def test_user_credentials_are_labelled_user_auth(self):
        class Credentials:  # the class name google.oauth2.credentials uses for a user's ADC
            pass

        described = describe_credential(Credentials())
        self.assertFalse(described.is_service_account)
        self.assertIn("user auth, not app auth", described.label)
        self.assertIn("Credentials", described.label)

    def test_empty_email_is_not_a_service_account(self):
        self.assertFalse(describe_credential(SimpleNamespace(service_account_email=None)).is_service_account)


class HttpStatusTest(unittest.TestCase):
    def test_reads_resp_status(self):
        self.assertEqual(gchat_poller.http_status(FakeHttpError(403)), 403)

    def test_none_for_errors_without_a_response(self):
        self.assertIsNone(gchat_poller.http_status(RuntimeError("no resp")))
        self.assertIsNone(gchat_poller.http_status(SimpleNamespace(resp=None)))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

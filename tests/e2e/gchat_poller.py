"""Reads Google Chat space messages with the runner's own credential first and the OTA user
credential second.

`spaces.messages.list` accepts a service account only under app authentication with the
`chat.app.messages.readonly` scope, which a Google Workspace administrator has to approve once
per Chat app (#1558). Until that approval exists the API answers 403, so the poller tries the
runner's Application Default Credentials first and, on a denial, switches to the Owned Test
Account refresh token for the rest of the run. Which credential read the space is recorded so
a CI log answers whether app authentication works under the organisation's policy.

Kept apart from gchat_agent_test.py so `make test-python`, which installs neither pytest nor
the Google client libraries, can import and exercise the fallback decision.
"""

from typing import Any, Callable, NamedTuple, Optional

# HTTP statuses that mean the credential presented may not read this space: 401 for a token the
# API refuses outright, 403 for a scope the Workspace administrator has not approved
# (ACCESS_TOKEN_SCOPE_INSUFFICIENT / PERMISSION_DENIED), and 400 because a DM answers an app-auth
# read with "DMs are not supported for methods requiring app authentication with administrator
# approval" (observed 2026-09-15), and a request the API rejects as malformed will not improve on
# retry either. Anything else is a transient the poll loop retries with the same credential.
ACCESS_DENIED_STATUSES: frozenset[int] = frozenset({400, 401, 403})
# Labels for the run log. Service-account, impersonated and Workload Identity Federation
# credentials expose the account they act as; a user's `gcloud auth application-default login`
# credential does not, and a metadata-server credential says "default" until its first refresh.
SERVICE_ACCOUNT_LABEL: str = "service-account app auth as {email}"
UNRESOLVED_SERVICE_ACCOUNT_LABEL: str = "service-account app auth (account not resolved yet: {email})"
USER_ADC_LABEL: str = "user Application Default Credentials ({kind}; user auth, not app auth)"
PRIMARY_AUTH_LABEL: str = "Application Default Credentials"
FALLBACK_AUTH_LABEL: str = "OTA user refresh token"
LOG_PREFIX: str = "[E2E Test]"
# The three environment variables that together configure the OTA fallback.
OTA_ENV_VARS: tuple[str, str, str] = ("E2E_CHAT_REFRESH_TOKEN", "E2E_CHAT_CLIENT_ID", "E2E_CHAT_CLIENT_SECRET")
OTA_HINT: str = f"or configure the OTA fallback by setting {', '.join(OTA_ENV_VARS)}."
# What to do about a denial depends on who was denied: a service account needs the Workspace
# administrator's approval, a user's ADC needs a login that carries the user read scope.
APPROVAL_HINT: str = (
    "Either have a Google Workspace administrator approve the chat.app.messages.readonly scope for "
    f"the Chat app (tests/e2e/README.md, 'Hybrid Auth Model'), {OTA_HINT}"
)
USER_SCOPE_HINT: str = (
    "Either log in again with the chat.messages.readonly scope (tests/e2e/README.md, 'Authenticate "
    f"GCP ADC for Local Execution'), {OTA_HINT}"
)


class CredentialDescription(NamedTuple):
    """What the run log says about the primary credential, and which remedy fits its denial."""

    label: str
    is_service_account: bool


DEFAULT_PRIMARY: CredentialDescription = CredentialDescription(PRIMARY_AUTH_LABEL, True)


def describe_credential(creds: Any) -> CredentialDescription:
    """Describes a google-auth credential object for the run log without importing google-auth."""
    email = getattr(creds, "service_account_email", None)
    if email and "@" in email:
        return CredentialDescription(SERVICE_ACCOUNT_LABEL.format(email=email), True)
    if email:
        return CredentialDescription(UNRESOLVED_SERVICE_ACCOUNT_LABEL.format(email=email), True)
    return CredentialDescription(USER_ADC_LABEL.format(kind=type(creds).__name__), False)


class ChatReadAccessDenied(Exception):
    """No configured credential may list messages in the space, so polling further cannot succeed."""


def http_status(err: BaseException) -> Optional[int]:
    """Returns the HTTP status a googleapiclient HttpError carries, or None for any other error."""
    status = getattr(getattr(err, "resp", None), "status", None)
    return int(status) if status is not None else None


class ChatMessagePoller:
    """Lists space messages, switching from the primary service to the fallback on a denial.

    `primary` and `fallback` are googleapiclient `chat` v1 Resource objects (or anything with the
    same `spaces().messages().list(...).execute()` shape). `fallback` is None when the OTA
    credential is not configured. `denial_types` are exception classes that also mean "this
    credential cannot be used", raised before any HTTP status exists: the caller passes
    google-auth's RefreshError so a token the token endpoint refuses to mint is answered the same
    way as a 403 from the API. An instance whose `retryable` attribute is true (google-auth sets
    it for a 5xx from the token endpoint) is a transient and propagates for the caller to retry.
    """

    def __init__(
        self,
        primary: Any,
        fallback: Optional[Any] = None,
        log: Callable[[str], None] = print,
        primary_credential: CredentialDescription = DEFAULT_PRIMARY,
        denial_types: tuple[type[BaseException], ...] = (),
    ) -> None:
        self._service = primary
        self._fallback = fallback
        self._log = log
        self._denial_types = denial_types
        self._primary_hint = APPROVAL_HINT if primary_credential.is_service_account else USER_SCOPE_HINT
        self.auth_label: str = primary_credential.label
        self.primary_label: str = primary_credential.label
        self.fell_back: bool = False
        self.fallback_reason: Optional[str] = None
        self.reads: int = 0

    @property
    def fallback_configured(self) -> bool:
        return self._fallback is not None

    def list_messages(self, space: str, page_size: int, order_by: str) -> dict[str, Any]:
        """Lists messages in `space`, falling back to the OTA credential on a denial.

        A denial is an HTTP 400/401/403 from the API or one of the caller's `denial_types`. It raises
        ChatReadAccessDenied when the denial cannot be answered: there is no fallback, or the
        fallback was denied too. Any other error propagates unchanged for the caller to retry.
        """
        try:
            page = self._list(space, page_size, order_by)
        except Exception as err:  # HttpError; only importable with the Google client libraries
            status = http_status(err)
            if status not in ACCESS_DENIED_STATUSES and not self._is_denial(err):
                raise
            reason = f"HTTP {status}: {err}" if status is not None else f"{type(err).__name__}: {err}"
            if self.fell_back:
                raise ChatReadAccessDenied(
                    f"{FALLBACK_AUTH_LABEL} may not list messages in {space} either ({reason})."
                ) from err
            if self._fallback is None:
                raise ChatReadAccessDenied(
                    f"{self.auth_label} may not list messages in {space} ({reason}). {self._primary_hint}"
                ) from err
            self.fallback_reason = reason
            self._log(
                f"{LOG_PREFIX} Warning: {self.auth_label} may not list messages in {space} "
                f"({self.fallback_reason}); falling back to the {FALLBACK_AUTH_LABEL}."
            )
            self._service = self._fallback
            self.auth_label = FALLBACK_AUTH_LABEL
            self.fell_back = True
            return self.list_messages(space, page_size, order_by)
        self.reads += 1
        return page

    def _is_denial(self, err: BaseException) -> bool:
        return isinstance(err, self._denial_types) and not getattr(err, "retryable", False)

    def _list(self, space: str, page_size: int, order_by: str) -> dict[str, Any]:
        return self._service.spaces().messages().list(parent=space, pageSize=page_size, orderBy=order_by).execute()

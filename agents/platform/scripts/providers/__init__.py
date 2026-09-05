#!/usr/bin/env python3
"""The forge abstraction: everything a forge is written against, and the forges.

This module is the whole of the package's public surface. Nothing outside
`providers/` imports `providers.something` -- a rule the import-boundary test
enforces -- so what is re-exported here is exactly what the broker and its
tests are allowed to see, and the submodules stay free to be reorganised
without a search-and-replace across the repository.

The split inside is by who owns the decision. These shared modules hold
everything a forge needs to be written against; a forge package holds
everything only that forge knows. The broker holds what is true regardless of
forge and names no forge at all.
"""

from .base import (
    BROKER_VERBS,
    COLLABORATION_VERBS,
    Forge,
    ForgeUnsupported,
    StubForge,
    WorkspaceError,
    listing,
)
from .credentials import BrokeredCredential, Credential, NoCredential
from .errors import GUIDANCE, Guidance, forge_error
from .identity import BRANCH_RE, SEGMENT_RE, SHA_RE, repository_host, strip_scheme
from .registry import AVAILABLE, Registry, build_forges
from .transport import CliTransport, Transport
from .validate import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    validate_branch,
    validate_labels,
    validate_limit,
    validate_number,
    validate_revision,
    validate_state,
    validate_text,
)

__all__ = [
    "AVAILABLE",
    "BRANCH_RE",
    "BROKER_VERBS",
    "COLLABORATION_VERBS",
    "BrokeredCredential",
    "CliTransport",
    "Credential",
    "DEFAULT_PAGE_SIZE",
    "Forge",
    "ForgeUnsupported",
    "GUIDANCE",
    "Guidance",
    "MAX_PAGE_SIZE",
    "NoCredential",
    "Registry",
    "SEGMENT_RE",
    "SHA_RE",
    "StubForge",
    "Transport",
    "WorkspaceError",
    "build_forges",
    "forge_error",
    "listing",
    "repository_host",
    "strip_scheme",
    "validate_branch",
    "validate_labels",
    "validate_limit",
    "validate_number",
    "validate_revision",
    "validate_state",
    "validate_text",
]

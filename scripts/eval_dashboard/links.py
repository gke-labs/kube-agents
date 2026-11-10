"""Links into a Prow build directory, by the bucket it lives in.

Prow's Spyglass renders a build only from a bucket Deck can read. The lanes'
own buckets (PRIVATE_LOG_BUCKETS) are not: Deck's service account has no
read on them, so a Spyglass link there returns "Access denied" to everyone.
A run read from one records its directory (``runs[].gcs_base``, SCHEMA.md)
and every link to it, on the pages, in the health bot's comment and in the
nightly issues, opens the Cloud Console instead, where a project reader can
browse the directory and open a transcript.

Only stdlib, like ``tiers.py``: ``nightly.py`` and ``collect.py`` both
import it (``collect`` imports ``nightly``, so neither can host it for the
other), and ``post_health.py`` imports ``nightly``.
"""

from __future__ import annotations

GS_SCHEME = "gs://"
# Where Prow's Spyglass shows a build directory: the gs:// path after the
# scheme, so a link follows the bucket the build was read from.
SPYGLASS_VIEW = "https://oss.gprow.dev/view/gs/"
# The Console's bucket browser for a directory, and the object's own URL for
# one object in it (a transcript): a signed-in project reader opens it in the
# browser in one click, as pages.js links a presubmit transcript.
GCS_BROWSER_URL = "https://console.cloud.google.com/storage/browser/"
GCS_OBJECT_URL = "https://storage.cloud.google.com/"
# Buckets Deck has no read on, so Spyglass shows nothing for a build there:
# the smoke lanes' bucket and the nightly's. A run read from one records its
# directory (runs[].gcs_base) and its links open the Console. A run in any
# other bucket keeps the Spyglass link, which still renders it.
PRIVATE_LOG_BUCKETS = frozenset({"kube-agents-evals-presubmit-logs", "kube-agents-evals-nightly-logs"})


def bucket_of(gcs_base: str) -> str:
    """The bucket name of a ``gs://bucket/...`` URL ("" for anything else)."""
    if not isinstance(gcs_base, str) or not gcs_base.startswith(GS_SCHEME):
        return ""
    return gcs_base[len(GS_SCHEME):].split("/", 1)[0]


def is_private(gcs_base: str) -> bool:
    """Whether a build at ``gcs_base`` links to the Console (PRIVATE_LOG_BUCKETS)."""
    return bucket_of(gcs_base) in PRIVATE_LOG_BUCKETS


def console_url(gcs_base: str) -> str:
    """The Console's bucket browser on the build directory ``gcs_base``."""
    return GCS_BROWSER_URL + gcs_base.removeprefix(GS_SCHEME).rstrip("/")


def console_object_url(gcs_base: str, path: str) -> str:
    """The object ``path`` (relative) under ``gcs_base``, as a project reader opens it."""
    return GCS_OBJECT_URL + gcs_base.removeprefix(GS_SCHEME).rstrip("/") + "/" + path.lstrip("/")


def private_gcs_base(url: str) -> str | None:
    """The ``gs://`` build directory behind a Spyglass link into a private
    bucket, else None: a record written before the bucket was private
    carries the Spyglass link as its ``log_url``, and that link is dead."""
    if not isinstance(url, str) or not url.startswith(SPYGLASS_VIEW):
        return None
    base = GS_SCHEME + url[len(SPYGLASS_VIEW):].rstrip("/") + "/"
    return base if is_private(base) else None

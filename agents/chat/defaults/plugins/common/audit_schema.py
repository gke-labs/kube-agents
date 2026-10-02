"""The envelope every audit record carries, shared by the plugin and the hook that emit them.

An audit record is one JSON object on one log line. Hermes writes it into the
agent's log file, the fluent-bit sidecar lifts the object out of the line into
top-level fields, and Cloud Logging reads those as a jsonPayload of its own; a
SIEM behind Cloud Logging then filters on them without a regex. For that to
work the record has to be self-describing, which is what this envelope adds:

- ``event_type``: the record's kind, under the name the structured audit schema
  gives it;
- ``audit_event``: the same value under the name the Admin Console's Logs
  Explorer queries and parser have read since the first record. Both stay until
  the console reads ``event_type``;
- ``severity``: Cloud Logging's level name, so a lifted record is filed under
  it rather than under the container stream's default;
- ``timestamp``: the event time in UTC to the millisecond with a ``Z`` suffix,
  the one form every log backend parses without a format string.

The emitter's own fields follow and may not override the envelope's keys.
"""

from datetime import datetime, timezone
from typing import Any, Dict

SEVERITY_INFO = "INFO"
EVENT_TYPE_KEY = "event_type"
LEGACY_EVENT_KEY = "audit_event"
SEVERITY_KEY = "severity"
TIMESTAMP_KEY = "timestamp"
_MICROSECONDS_PER_MILLISECOND = 1000


def iso_timestamp(now: datetime | None = None) -> str:
    """UTC, millisecond precision, ``Z`` suffix."""
    moment = now or datetime.now(timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // _MICROSECONDS_PER_MILLISECOND:03d}Z"


def envelope(event: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """The record for ``event``: the envelope first, then ``fields``."""
    record = {
        LEGACY_EVENT_KEY: event,
        EVENT_TYPE_KEY: event,
        SEVERITY_KEY: SEVERITY_INFO,
        TIMESTAMP_KEY: iso_timestamp(),
    }
    for key, value in fields.items():
        if key in record:
            raise ValueError(f"audit field {key!r} would override the envelope")
        record[key] = value
    return record

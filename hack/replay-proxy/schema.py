"""The shadow proxy's log line: event names, field names and unstorable reasons.

replay_proxy.py writes these lines and analyze_shadow.py and analyze_replay.py
read them; all use the names here, so a renamed field breaks the tests instead
of the analysis.
"""

# 2: litellm_config_checksum became model_behaviour_checksum, a checksum that
#    treated the Gemini Developer API and Vertex AI routes as one.
# 3: back to litellm_config_checksum, the LiteLLM pods' checksum/config. Not 1
#    again, since the bucket holds schema-2 lines. The readers group lines by
#    fingerprint and read neither name, so schema-2 lines still read.
LOG_SCHEMA_VERSION = 3

REQUEST_EVENT = "replay_shadow_request"
START_EVENT = "replay_shadow_start"

# Fields of a request line.
EVENT = "event"
SCHEMA = "schema"
REQUEST_ID = "request_id"
TS = "ts"
JOB = "job"
MODEL = "model"
STREAM = "stream"
NORMALIZER_VERSION = "normalizer_version"
LITELLM_CONFIG_CHECKSUM = "litellm_config_checksum"
LITELLM_IMAGE = "litellm_image"
STATUS = "status"
LATENCY_MS = "latency_ms"
CLIENT_DISCONNECTED = "client_disconnected"
UPSTREAM_INCOMPLETE = "upstream_incomplete"
OBSERVE_FAILED = "observe_failed"
# LiteLLM's headers; null on a served line, which never reached LiteLLM.
LITELLM_MODEL_GROUP = "litellm_model_group"
LITELLM_ATTEMPTED_FALLBACKS = "litellm_attempted_fallbacks"
# The deployment's model behind the alias (x-litellm-model-name), the retries
# LiteLLM made inside this request (x-litellm-attempted-retries), and its own
# cost for the request in USD: usage.cost, else x-litellm-response-cost on a
# non-streamed response only (a stream's headers go out before the cost is
# known and say 0.0). All three are null on a served line.
LITELLM_MODEL_NAME = "litellm_model_name"
LITELLM_ATTEMPTED_RETRIES = "litellm_attempted_retries"
LITELLM_COST_USD = "litellm_cost_usd"
PROMPT_TOKENS = "prompt_tokens"
# True when the response carried no usage and prompt_tokens is estimated from
# the request's size.
TOKENS_ESTIMATED = "tokens_estimated"
CACHED_TOKENS = "cached_tokens"
COMPLETION_TOKENS = "completion_tokens"
# From usage.completion_tokens_details.reasoning_tokens; part of completion_tokens.
# Null on a served line, as is CACHE_WRITE_TOKENS.
REASONING_TOKENS = "reasoning_tokens"
# From usage.cache_creation_input_tokens (Anthropic-style prompt cache writes).
CACHE_WRITE_TOKENS = "cache_write_tokens"
FINGERPRINT = "fingerprint"
SETTINGS_HASH = "settings_hash"
MESSAGE_HASHES = "message_hashes"
# Per-message hashes for the closest-earlier-request search only: each message
# frozen as it normalized when this proxy first saw it (diagnosis.py).
DIAG_MESSAGE_HASHES = "diag_message_hashes"
# The settings' own hash for that search: no message context (diagnosis.py).
DIAG_SETTINGS_HASH = "diag_settings_hash"
ROLES = "roles"
ANSWER_HASH = "answer_hash"
# Why replay would not have saved the answer; empty on a served line, whose
# answer is a saved one and is never judged as a new one.
UNSTORABLE = "unstorable"
STORED = "stored"
STORE_SKIPPED = "store_skipped"
ERROR = "error"
# The `error` of a line whose analysis was dropped because the queue was full.
ERROR_ANALYSIS_DROPPED = "analysis_dropped"

# Fields of the start line.
SCRUBBERS_LOADED = "scrubbers_loaded"
SCRUBBERS_ERROR = "scrubbers_error"
# Whether lines also go to segment files (REPLAY_LOG_DIR), why not when that
# was asked for and failed, how many segments a previous run left open and
# this one closed, and the data budget in bytes (0: none).
LOG_FILE = "log_file"
LOG_FILE_ERROR = "log_file_error"
LOG_SEGMENTS_RECOVERED = "log_segments_recovered"
DATA_MAX_BYTES = "data_max_bytes"

# Fields of `job`.
JOB_KIND = "kind"
JOB_PROJECT = "project"
JOB_BUILD = "build"
JOB_PR = "pr"

# Why replay would not have saved an answer, in `unstorable`.
UNSTORABLE_OBSERVE_FAILED = "observe_failed"
UNSTORABLE_OBSERVE_TOO_LARGE = "observe_too_large"
UNSTORABLE_UPSTREAM_INCOMPLETE = "upstream_incomplete"
UNSTORABLE_CLIENT_DISCONNECTED = "client_disconnected"
UNSTORABLE_NO_ANSWER = "no_answer"
UNSTORABLE_NO_MODEL_GROUP = "no_model_group_header"
UNSTORABLE_OTHER_MODEL_GROUP = "answered_by_other_model_group"
UNSTORABLE_FALLBACK = "fallback_attempted"
UNSTORABLE_REPEATS_NUMBER = "repeats_swapped_number"
# The answer repeats an age the key swapped (`--since 5m` after `AGE 5m`),
# which is never put back: this run's age would change what it does.
UNSTORABLE_REPEATS_AGE = "repeats_swapped_age"
# Normalized, the answer still names an identifier or IP the request bound, so
# it would be served with the recording's value.
UNSTORABLE_BOUND_VALUE_LEFT_LITERAL = "bound_value_left_literal"
# Normalized, the answer still holds an identifier shape (a card, a pod, a
# session) the request never bound, so it would be served as recorded.
UNSTORABLE_UNBOUND_IDENTIFIER = "unbound_identifier"
# The answer quotes a timestamp the request does not hold verbatim (one the
# model made), which no placeholder can bring up to date.
UNSTORABLE_UNBOUND_TIMESTAMP = "unbound_timestamp"
UNSTORABLE_REPEATS_DATE = "repeats_swapped_date"
UNSTORABLE_SCRUB_MASKS_CREDENTIAL = "scrub_masks_credential"
# The request holds the result of a tool call that printed a secret's value,
# which the answer may repeat in a form no scrubber recognises; the
# conversation runs live from the read on.
UNSTORABLE_FOLLOWS_SECRET_READ = "follows_secret_read"
UNSTORABLE_SCRUBBERS_UNAVAILABLE = "scrubbers_unavailable"
UNSTORABLE_SCRUB_FAILED = "scrub_failed"

# Proxy modes (REPLAY_MODE). Shadow and record never serve a saved answer.
MODE_SHADOW = "shadow"
MODE_RECORD = "record"
MODE_REPLAY = "replay"
MODES = (MODE_SHADOW, MODE_RECORD, MODE_REPLAY)

# Fields every request line carries in every mode.
MODE = "mode"
# True when replay mode served a saved answer and the model was not called.
SERVED = "served"
# On a served line: the job that recorded the answer (its `job` block).
SERVED_FROM = "served_from"
# How a served answer matched: the exact key, or the answer-slot key.
SERVED_VIA = "served_via"
VIA_EXACT = "exact"
VIA_ANSWER_SLOTS = "answer_slots"
# In record mode: whether this request's answer was saved (false when the key
# was already there, or the answer was unstorable).
RECORDED = "recorded"
# In record mode, when writing the cache failed: the exception's type name, or
# RECORD_ERROR_BUDGET when the data budget is spent and nothing was saved.
RECORD_ERROR = "record_error"
RECORD_ERROR_BUDGET = "budget_spent"
# On a replay miss: the closest saved request and where this one differed.
MISS = "miss"
MISS_CLOSEST_FINGERPRINT = "closest_fingerprint"
MISS_MATCHED_MESSAGES = "matched_messages"
MISS_INDEX = "index"
MISS_ROLE = "role"
# The normalized, scrubbed text of the differing message on each side, cut to
# a short window around the first difference.
MISS_SAVED_SNIPPET = "saved_snippet"
MISS_CURRENT_SNIPPET = "current_snippet"
# The two windows with digit and hex runs collapsed, for grouping misses.
MISS_SIGNATURE = "signature"
# On a replay miss whose request did match a saved answer that a guard then
# refused to serve: which key matched (VIA_EXACT or VIA_ANSWER_SLOTS) and why.
MISS_REFUSED = "refused"
REFUSED_VIA = "via"
REFUSED_GUARD = "guard"
# A slot held an answer no recording stored for its context.
GUARD_ANSWER_NOT_RECORDED = "answer_not_recorded"
# Through a slot, the saved answer names a value only an assistant message binds.
GUARD_VALUE_ONLY_IN_ANSWERS = "value_only_in_answers"
# The saved answer names a placeholder this request does not bind.
GUARD_UNBOUND_PLACEHOLDER = "unbound_placeholder"
# The saved answer is missing or not an answer.
GUARD_UNREADABLE_ENTRY = "unreadable_entry"
# Where the first difference is when it is not a message.
MISS_ROLE_SETTINGS = "settings"
MISS_ROLE_NONE = "none"
MISS_ROLE_NO_CACHE = "no_saved_request"

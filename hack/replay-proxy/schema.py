"""The shadow proxy's log line: event names, field names and unstorable reasons.

replay_proxy.py writes these lines and analyze_shadow.py reads them; both use
the names here, so a renamed field breaks the tests instead of the analysis.
"""

# 2: litellm_config_checksum became model_behaviour_checksum.
LOG_SCHEMA_VERSION = 2

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
MODEL_BEHAVIOUR_CHECKSUM = "model_behaviour_checksum"
LITELLM_IMAGE = "litellm_image"
STATUS = "status"
LATENCY_MS = "latency_ms"
CLIENT_DISCONNECTED = "client_disconnected"
UPSTREAM_INCOMPLETE = "upstream_incomplete"
OBSERVE_FAILED = "observe_failed"
LITELLM_MODEL_GROUP = "litellm_model_group"
LITELLM_ATTEMPTED_FALLBACKS = "litellm_attempted_fallbacks"
# The deployment's model behind the alias (x-litellm-model-name), the retries
# LiteLLM made inside this request (x-litellm-attempted-retries), and its own
# cost for the request in USD: usage.cost, else x-litellm-response-cost on a
# non-streamed response only (a stream's headers go out before the cost is
# known and say 0.0).
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
UNSTORABLE_REPEATS_DATE = "repeats_swapped_date"
UNSTORABLE_SCRUB_MASKS_CREDENTIAL = "scrub_masks_credential"
UNSTORABLE_SCRUBBERS_UNAVAILABLE = "scrubbers_unavailable"
UNSTORABLE_SCRUB_FAILED = "scrub_failed"

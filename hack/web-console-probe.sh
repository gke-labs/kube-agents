#!/usr/bin/env bash
# ==============================================================================
# The web console probe hack/ci-deploy.sh sources
# ==============================================================================
# Sourced, not run. One pass over the web console (a2a/cmd/web-console) at a
# base URL, the way a browser behind `kubectl port-forward` reaches it: the
# health route, the page, the read routes (200 and a JSON object, the two
# metadata routes with the keys the page reads), one chat turn into a fresh
# console session, and the session's messages, which must show the user's row
# and the assistant's. The deploy's gate runs it once, at the end of its A2A
# gates. One file of its own, so what counts as a pass, and what is worth
# another tunnel, cannot drift when a second caller sources it (#2462).
#
# The caller owns the tunnel and the retry loop. web_console_probe returns 0
# on a pass, WEB_CONSOLE_PROBE_RETRY for an answer a fresh tunnel or another
# turn may change (no answer, a 5xx, or a turn that ended with no assistant
# text), and 1 for one it would not (a 4xx, or a 200 whose body is not the
# route's); WEB_CONSOLE_DETAIL names the route and says why, on every return.
# The caller creates WEB_CONSOLE_BODY_FILE (mktemp) before the first probe and
# removes it after the last. python3 reads the JSON; both callers require it.
# The routes, keys, header and session shape are the console's
# (a2a/cmd/web-console/main.go, insights.go, environment.go);
# tests/test_ci_deploy_web_console.py holds them to that source.
# ==============================================================================
# shellcheck disable=SC2034 # WEB_CONSOLE_STATUS and WEB_CONSOLE_DETAIL are read by the sourcing scripts

# The console refuses a Host that is not loopback and a POST without this
# header (requireLocalHost and requireConsoleRequest); the tunnel gives it
# the first, web_console_request the second.
readonly WEB_CONSOLE_HEADER="X-Kube-Agents-Console: 1"
readonly WEB_CONSOLE_HEALTH_PATH="/healthz"
# The page itself, read for this marker.
readonly WEB_CONSOLE_PAGE_PATH="/"
readonly WEB_CONSOLE_PAGE_MARKER="<html"
# The read routes, each answering 200 with a JSON object, and the keys the
# two metadata routes carry (insightsResponse, environmentResponse).
readonly WEB_CONSOLE_READ_ROUTES="/api/insights /api/environment /api/channels/alerts/posts /api/channels/scheduled/posts /api/models /api/repositories /api/patches"
readonly WEB_CONSOLE_INSIGHTS_PATH="/api/insights"
readonly WEB_CONSOLE_INSIGHTS_KEYS="cluster"
readonly WEB_CONSOLE_ENVIRONMENT_PATH="/api/environment"
readonly WEB_CONSOLE_ENVIRONMENT_KEYS="versions counts"
# One turn (chatRequest, chatResponse) into a session id of the console's own
# shape (sessionIDPattern: the prefix and 32 hex digits), then the session's
# messages (sessionMessagesResponse).
readonly WEB_CONSOLE_CHAT_PATH="/api/chat"
readonly WEB_CONSOLE_CHAT_KEYS="session_id reply"
readonly WEB_CONSOLE_MESSAGES_PATH_FORMAT="/api/sessions/%s/messages"
readonly WEB_CONSOLE_MESSAGES_KEYS="messages"
readonly WEB_CONSOLE_SESSION_ID_PREFIX="web-console-"
readonly WEB_CONSOLE_SESSION_ID_HEX_BYTES=16
readonly WEB_CONSOLE_CHAT_PROMPT="Reply with the single word pong. Do not run any tools."
readonly WEB_CONSOLE_ROLE_USER="user"
readonly WEB_CONSOLE_ROLE_ASSISTANT="assistant"
# The return for an answer worth another attempt; 1 is the one that is not.
readonly WEB_CONSOLE_PROBE_RETRY=2
# How much of a body a failure line quotes.
readonly WEB_CONSOLE_DETAIL_BYTES=300

WEB_CONSOLE_STATUS=""
WEB_CONSOLE_DETAIL=""
WEB_CONSOLE_BODY_FILE=""

# One request. WEB_CONSOLE_STATUS is the HTTP status (000 when nothing
# answered inside the deadline); the body is in WEB_CONSOLE_BODY_FILE. A body
# makes it a console POST: the header and the JSON content type
# requireConsoleRequest asks for.
web_console_request() { # <base url> <method> <path> <max-time seconds> [JSON body]
  local url="$1" method="$2" path="$3" max_time="$4" body="${5:-}"
  local -a args=(-s -o "${WEB_CONSOLE_BODY_FILE}" -w '%{http_code}' --max-time "${max_time}" -X "${method}")
  if [ -n "${body}" ]; then
    args+=(-H "${WEB_CONSOLE_HEADER}" -H "Content-Type: application/json" -d "${body}")
  fi
  : >"${WEB_CONSOLE_BODY_FILE}"
  WEB_CONSOLE_STATUS="$(curl "${args[@]}" "${url}${path}" 2>/dev/null || true)"
  WEB_CONSOLE_STATUS="${WEB_CONSOLE_STATUS:-000}"
}

# The start of the last body, on one line, for a failure line.
web_console_body_excerpt() {
  head -c "${WEB_CONSOLE_DETAIL_BYTES}" "${WEB_CONSOLE_BODY_FILE}" 2>/dev/null | tr '\n' ' '
}

# Judges the last answer: 0 for a 200 whose body is a JSON object carrying
# every key named; WEB_CONSOLE_PROBE_RETRY for no answer or a 5xx; 1 for
# anything else.
web_console_expect_json() { # <path> [key...]
  local path="$1" why
  shift
  case "${WEB_CONSOLE_STATUS}" in
    200) ;;
    000)
      WEB_CONSOLE_DETAIL="${path}: no answer (the tunnel dropped, or nothing came within the deadline)"
      return "${WEB_CONSOLE_PROBE_RETRY}"
      ;;
    5??)
      WEB_CONSOLE_DETAIL="${path}: HTTP ${WEB_CONSOLE_STATUS}: $(web_console_body_excerpt)"
      return "${WEB_CONSOLE_PROBE_RETRY}"
      ;;
    *)
      WEB_CONSOLE_DETAIL="${path}: HTTP ${WEB_CONSOLE_STATUS}: $(web_console_body_excerpt)"
      return 1
      ;;
  esac
  if ! why="$(python3 - "${WEB_CONSOLE_BODY_FILE}" "$@" <<'PY'
import json, sys
path, keys = sys.argv[1], sys.argv[2:]
try:
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
except (OSError, ValueError) as exc:
    print("the body is not JSON (%s)" % exc)
    sys.exit(1)
if not isinstance(doc, dict):
    print("the body is JSON but not an object")
    sys.exit(1)
missing = [key for key in keys if key not in doc]
if missing:
    print("the object lacks %s" % ", ".join(missing))
    sys.exit(1)
PY
  )"; then
    WEB_CONSOLE_DETAIL="${path}: HTTP 200 but ${why}: $(web_console_body_excerpt)"
    return 1
  fi
}

# The session's messages after the turn must hold a row of each role. A
# missing assistant row is a turn that ended with no text (the console lists
# only rows that have some), which is the model's doing and not the tunnel's,
# so it is worth another turn: WEB_CONSOLE_PROBE_RETRY.
web_console_expect_roles() { # <path>
  local path="$1" why
  if ! why="$(python3 - "${WEB_CONSOLE_BODY_FILE}" "${WEB_CONSOLE_ROLE_USER}" "${WEB_CONSOLE_ROLE_ASSISTANT}" <<'PY'
import json, sys
path, wanted = sys.argv[1], sys.argv[2:]
with open(path, encoding="utf-8") as handle:
    doc = json.load(handle)
roles = [m.get("role") for m in doc.get("messages") or []]
missing = [role for role in wanted if role not in roles]
if missing:
    print("no %s row among %d message(s) (roles: %s)" % (" or ".join(missing), len(roles), ", ".join(str(r) for r in roles) or "none"))
    sys.exit(1)
PY
  )"; then
    WEB_CONSOLE_DETAIL="${path}: HTTP 200 but ${why}"
    return "${WEB_CONSOLE_PROBE_RETRY}"
  fi
}

# A fresh console session id.
web_console_session_id() {
  python3 - "${WEB_CONSOLE_SESSION_ID_PREFIX}" "${WEB_CONSOLE_SESSION_ID_HEX_BYTES}" <<'PY'
import secrets, sys
print(sys.argv[1] + secrets.token_hex(int(sys.argv[2])))
PY
}

# One pass over the console at a base URL through a tunnel that is up: the
# health route, the page, the read routes, a turn, the messages. Returns as
# the header says; on a pass WEB_CONSOLE_DETAIL says what answered.
web_console_probe() { # <base url> <route max-time seconds> <chat max-time seconds>
  local url="$1" route_max_time="$2" chat_max_time="$3" path sid messages_path
  local -a keys
  web_console_request "${url}" GET "${WEB_CONSOLE_HEALTH_PATH}" "${route_max_time}"
  web_console_expect_json "${WEB_CONSOLE_HEALTH_PATH}" || return $?
  web_console_request "${url}" GET "${WEB_CONSOLE_PAGE_PATH}" "${route_max_time}"
  case "${WEB_CONSOLE_STATUS}" in
    200)
      if ! grep -qiF "${WEB_CONSOLE_PAGE_MARKER}" "${WEB_CONSOLE_BODY_FILE}"; then
        WEB_CONSOLE_DETAIL="${WEB_CONSOLE_PAGE_PATH}: HTTP 200 but the body is not the console page (no ${WEB_CONSOLE_PAGE_MARKER}): $(web_console_body_excerpt)"
        return 1
      fi
      ;;
    000 | 5??)
      WEB_CONSOLE_DETAIL="${WEB_CONSOLE_PAGE_PATH}: HTTP ${WEB_CONSOLE_STATUS}: $(web_console_body_excerpt)"
      return "${WEB_CONSOLE_PROBE_RETRY}"
      ;;
    *)
      WEB_CONSOLE_DETAIL="${WEB_CONSOLE_PAGE_PATH}: HTTP ${WEB_CONSOLE_STATUS}: $(web_console_body_excerpt)"
      return 1
      ;;
  esac
  for path in ${WEB_CONSOLE_READ_ROUTES}; do
    keys=()
    case "${path}" in
      "${WEB_CONSOLE_INSIGHTS_PATH}") read -r -a keys <<<"${WEB_CONSOLE_INSIGHTS_KEYS}" ;;
      "${WEB_CONSOLE_ENVIRONMENT_PATH}") read -r -a keys <<<"${WEB_CONSOLE_ENVIRONMENT_KEYS}" ;;
    esac
    web_console_request "${url}" GET "${path}" "${route_max_time}"
    web_console_expect_json "${path}" ${keys[@]+"${keys[@]}"} || return $?
  done
  echo "  every read route answered 200 with a JSON object"
  sid="$(web_console_session_id)"
  web_console_request "${url}" POST "${WEB_CONSOLE_CHAT_PATH}" "${chat_max_time}" \
    "{\"session_id\": \"${sid}\", \"message\": \"${WEB_CONSOLE_CHAT_PROMPT}\"}"
  read -r -a keys <<<"${WEB_CONSOLE_CHAT_KEYS}"
  web_console_expect_json "${WEB_CONSOLE_CHAT_PATH}" "${keys[@]}" || return $?
  echo "  ${WEB_CONSOLE_CHAT_PATH} answered the turn on session ${sid}"
  # The format is a named constant, which is the point of it (SC2059 wants a literal).
  # shellcheck disable=SC2059
  printf -v messages_path "${WEB_CONSOLE_MESSAGES_PATH_FORMAT}" "${sid}"
  web_console_request "${url}" GET "${messages_path}" "${route_max_time}"
  read -r -a keys <<<"${WEB_CONSOLE_MESSAGES_KEYS}"
  web_console_expect_json "${messages_path}" "${keys[@]}" || return $?
  web_console_expect_roles "${messages_path}" || return $?
  echo "  ${messages_path} shows the ${WEB_CONSOLE_ROLE_USER} and ${WEB_CONSOLE_ROLE_ASSISTANT} rows"
  WEB_CONSOLE_DETAIL="every route answered 200, the turn on session ${sid} got a reply, and the session shows the ${WEB_CONSOLE_ROLE_USER} and ${WEB_CONSOLE_ROLE_ASSISTANT} rows"
}

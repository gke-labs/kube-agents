# Read-Only Cloud API Relay in the Credential Broker

## Summary

The Platform Agent's shell, file tools and code execution run in the shell sandbox pod, whose
ServiceAccount carries no cloud identity. Every credentialed call the model's code makes goes
through the credential broker, and until this route the broker spoke one shape: an argv for
`kubectl`, `gcloud`, `gh` or `git`, checked against an allowlist and executed on the broker's
side. No `gcloud` command reads Monitoring time series, so a collector that will need a week
of per-pod usage has no sanctioned path to it.

The broker now speaks a second shape: an HTTP relay for **read-only Google Cloud REST calls**.
The sandbox sends an unauthenticated `GET` for a Google API URL to the broker; the broker
checks the host, method and path against a table of permitted reads, attaches its own
credential, forwards the request, and returns the response body. The model's code never holds
a Google token. What it gains is exactly the reads the table names, on the identity the broker
already has, with the same caller authentication, refusal shape and audit line as `/v1/exec`.

The first table entries are the Monitoring `timeSeries` and `metricDescriptors` reads and the
Managed Prometheus query endpoints. A collector's one-function change is to obtain its
`requests`-shaped session from the broker client (`credential_proxy_client.ApiSession`)
instead of from `google.auth`.

## What was verified on a live install

All of the following were observed read-only on a live install on 2026-09-15, against the
three-pod layout #913 reconciles. They are the facts the design rests on.

| Fact                                                                                                                                                                                                 | Where it matters                                  |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| The sandbox pod's metadata server answers with the unbound identity `<project>.svc.id.goog`. It issues a token; IAM grants that token nothing.                                                       | Why the collector cannot self-serve               |
| The sandbox has Python 3.14.7 with `requests` and `yaml` importable and no `google` package.                                                                                                         | Client side needs no new packages                 |
| `CREDENTIAL_PROXY_URL` in the sandbox points at the broker's ClusterIP Service on port 8765; the broker answers `/healthz` from there; the caller token is mounted at `CREDENTIAL_PROXY_TOKEN_FILE`. | Transport already exists                          |
| The broker runs with `CREDENTIAL_PROXY_AUTH_MODE=serviceaccount` and allows exactly the gateway's and the shell's ServiceAccounts.                                                                   | Caller authentication is reused                   |
| A `timeSeries` list for `kubernetes.io/container/cpu/core_usage_time` filtered to one cluster, made from the broker pod with its own metadata token, returned 200 with per-container series.         | `roles/monitoring.viewer` suffices; no IAM change |
| The GKE metadata server **ignores a `scopes` parameter**: a token requested with only `monitoring.read` came back carrying `cloud-platform` and reached the Compute API.                             | Scope narrowing is not an available control       |
| No Managed Prometheus frontend service exists in the cluster.                                                                                                                                        | Rules out a PromQL path through proxied `kubectl` |

The last two shape the security section: the policy table is the enforcement, not the token.

## The decision

One new route on the broker, `GET /v1/gcp/<host>/<path>?<query>`, admitted for the shell
caller role, evaluated by a new `api_policy` module in the style of `command_policy`, and
served by the same handler class that serves `/v1/exec`. Envoy needs no change: its single
route forwards every prefix to the Python runtime with no timeout.

Three choices inside that, each with the alternative named:

- **Host in the path, with a hard host allowlist**, rather than one fixed route per service.
  The next entry is Logging, and a per-service route means a handler per service. The host
  allowlist is what stops the relay being a forward proxy: a host not in the table is refused
  before the path is read.
- **A code table, not the JSON policy ConfigMap.** The broker's `policy.json` is a regex
  denylist over argv text; `GCLOUD_READ_COMMANDS` is a code allowlist, reviewed as a diff. The
  relay is an allowlist, so it follows the second. Widening it is a pull request, which is the
  property the `gcloud` table's comment asks for.
- **`GET` only in the first version.** Monitoring's `timeSeries.list` and the Managed
  Prometheus `query` and `query_range` are `GET`. The reads that are `POST` — Logging
  `entries:list`, Monitoring `timeSeries:query` for MQL — carry a body the policy would have to
  inspect, and a body cap and a JSON schema per entry are a second design. Nothing here
  forecloses it.

## The design

### Request contract

The sandbox sends:

```
GET {CREDENTIAL_PROXY_URL}/v1/gcp/monitoring.googleapis.com/v3/projects/{project}/timeSeries?filter=...&interval.startTime=...
Authorization: Bearer <projected caller token>
```

The broker (`CredentialProxyHandler._handle_api_relay` in
`agents/platform/scripts/credential_proxy.py`):

1. Authenticates the caller through `_authenticated()`, exactly as every other route does,
   and refuses a caller whose role is not `shell` through `ROUTE_ROLES`, which carries the
   entry `("/v1/gcp/", (CALLER_ROLE_SHELL,))`.
2. Splits the path into `host` (the first segment after `/v1/gcp/`) and `path` (the rest),
   after rejecting any request that is not already in normal form
   (`api_relay_target_problem`): a `..`, `.` or empty segment, a percent-encoded slash, a
   missing host or path, a host segment that is not a lower-case DNS name — a scheme, a
   port, user info or percent-encoding in that position — or a query byte the upstream request line cannot carry — the set is RFC 3986's query characters plus `[` and `]`, which the RFC's gen-delims split keeps out of a query but `http.client` sends raw, Google accepts, and the Managed Prometheus routes need for `match[]=` and `[5m]`; what stays out is space, controls, non-ASCII, `" < > \\ ^ ` { | }`and an incomplete percent-escape — is a 400 naming the reason,
with`code` `API_RELAY_BAD_HOST`, `API_RELAY_BAD_PATH`or`API_RELAY_BAD_QUERY`saying
which part to correct, not something to normalise. A query over`API_RELAY_MAX_QUERY_BYTES`is the same 400, so an over-long URL never leaves the broker. In the deployed path Envoy sits in front
of the broker and answers 400 itself to a request line carrying a non-ASCII byte (observed
live), so the query check governs what Envoy passes: ASCII outside the query set, and any
byte that reaches the broker by another route. The table matches exact text, and what
it saw must be what is forwarded; the query check is also what keeps a byte`http.client`will not put on a request line from surfacing as a traceback, and a`UnicodeError`or`InvalidURL` from the upstream request is answered with the same 400 as a second line
(`UnicodeError`and not`ValueError`: `ssl.SSLCertVerificationError`is a`ValueError` too,
   and a TLS fault is answered as the upstream's, 502).
3. Evaluates `api_policy.evaluate(method, host, path, query)`. A refusal answers 403 with the
   same body shape `/v1/exec` uses, so a caller that already reads `rule` and `message` reads
   these:

   ```json
   {
     "status": "blocked",
     "code": "SECURITY_POLICY_BLOCKED",
     "rule": "gcp.api.host",
     "message": "logging.googleapis.com is not a host the credential proxy relays. Reads are added to api_policy.API_READ_ROUTES by pull request."
   }
   ```

4. Builds the upstream request `https://{host}/{path}?{query}` with only two headers beyond
   the `Host` HTTP/1.1 requires: the broker's `Authorization: Bearer <token>` and an
   `Accept: application/json`. Every header the caller sent is dropped. Four query parameters
   are removed if present because each is a way to substitute a credential or change the
   response class: `key`, `access_token`, `oauth_token` and its deprecated spelling
   `bearer_token`. Everything else in the query is
   forwarded byte-for-byte (`strip_credential_query_keys` splits on `&` and rejoins rather
   than parsing and re-encoding); the filter grammar is Google's to validate.
5. Forwards with a connect timeout and a total deadline, reads at most the response cap, and
   returns the upstream status code, `Content-Type` and body unchanged. What the broker
   answers on its own account, each with a `code` the caller can branch on:

   | Condition                                                                         | Status | `code`                         |
   | --------------------------------------------------------------------------------- | ------ | ------------------------------ |
   | Upstream body over `API_RELAY_MAX_RESPONSE_BYTES`                                 | 502    | `UPSTREAM_RESPONSE_TOO_LARGE`  |
   | Upstream answered 3xx (redirects are never followed)                              | 502    | `UPSTREAM_REDIRECTED`          |
   | Upstream unreachable, the connect or TLS handshake failed, or the exchange failed | 502    | `UPSTREAM_UNAVAILABLE`         |
   | Upstream closed before delivering the bytes its `Content-Length` announced        | 502    | `UPSTREAM_TRUNCATED`           |
   | `API_RELAY_DEADLINE_S` passed after the connect                                   | 504    | `UPSTREAM_TIMEOUT`             |
   | The broker could not obtain its own credential                                    | 503    | `RELAY_CREDENTIAL_UNAVAILABLE` |
   | No relay armed on the handler (only a test reaches this)                          | 503    | `API_RELAY_DISABLED`           |

   The over-cap remedy is a smaller `pageSize`, which every listed endpoint supports; the
   message says so.

6. Writes two audit lines per request, on the pattern of the exec route: one before the
   decision and exactly one verdict after it. The request id is minted by the broker, so the
   two lines of one request share it:

   ```
   api request_id=%s principal=%s host=%s path=%s
   api rejected request_id=%s code=%s reason=%s
   api blocked request_id=%s rule=%s
   api forwarded request_id=%s host=%s status=%d bytes=%d duration_ms=%d
   ```

   `host` and `path` are caller text and go through `_sanitize_for_logging` as `argv[0]`
   does (`path` at 256 characters, the width the exec route gives a `cwd`). The upstream
   failure cases in the table above are the verdict line in their case, naming the condition,
   and the exception type where there is one, never its message; the disabled-relay 503
   writes `api disabled request_id=%s rule=%s`.

Named constants, declared at the top of `credential_proxy.py` per the engineering rules:

| Name                            | Value         | Why this value                                                                                                                                                                                                                                                                                                      |
| ------------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `API_RELAY_PREFIX`              | `/v1/gcp/`    | The route. Declared in `credential_proxy_client.py`, which both sides import, and re-exported by the broker so the two cannot spell it differently                                                                                                                                                                  |
| `API_RELAY_CONNECT_TIMEOUT_S`   | `10`          | Matches `BROKER_CONNECT_TIMEOUT_SECONDS` on the client                                                                                                                                                                                                                                                              |
| `API_RELAY_DEADLINE_S`          | `120`         | A week of per-pod series for a large cluster at the API's maximum page size is the slowest read the table admits, and such a page takes tens of seconds to assemble                                                                                                                                                 |
| `API_RELAY_MAX_RESPONSE_BYTES`  | `8 MiB`       | A full `timeSeries` page at the API's maximum `pageSize` is under 4 MiB                                                                                                                                                                                                                                             |
| `API_RELAY_READ_CHUNK_BYTES`    | `64 KiB`      | The body is read in chunks up to the cap, so the cap bounds memory as well as the wire                                                                                                                                                                                                                              |
| `API_RELAY_STRIPPED_QUERY_KEYS` | see above     | Credential substitution; `bearer_token` is the deprecated spelling                                                                                                                                                                                                                                                  |
| `API_RELAY_ACCEPT`              | see above     | The second of the two headers                                                                                                                                                                                                                                                                                       |
| `API_RELAY_UPSTREAM_PORT`       | `443`         | TLS only; the host segment cannot carry a port                                                                                                                                                                                                                                                                      |
| `API_RELAY_QUERY_SHAPE`         | URL query set | What the query may contain, checked before the policy so nothing the upstream request line cannot carry reaches `http.client`: RFC 3986's query characters plus `[` and `]`, per `http.client`'s own rule rather than the RFC's gen-delims split, because `match[]=` and `[5m]` are what the Prometheus routes take |
| `API_RELAY_MAX_QUERY_BYTES`     | `8 KiB`       | Google's front end answers a longer URL with a 414 and `Connection: close`; a `timeSeries` filter with an aggregation and several groupBy fields is under 2 KiB                                                                                                                                                     |

The host shape is `api_policy.HOST_SHAPE`, one regex the table validator and the handler share.

### The policy module

`agents/platform/scripts/api_policy.py`, stdlib only, importable by the broker and by tests.
It reuses `command_policy.Decision` so the handler's refusal code is shared. One row per
permitted read:

```python
@dataclass(frozen=True)
class ApiRoute:
    host: str            # exact match, lower-cased
    method: str          # API_READ_METHOD, "GET", in this version
    path: re.Pattern     # anchored at both ends, fullmatched against the whole path
    rule_id: str         # what the audit line and the 403 name

# One entry, as an example of the shape; the table is in api_policy.py.
ApiRoute("monitoring.googleapis.com", API_READ_METHOD,
         re.compile(rf"^v3/projects/{PROJECT}/timeSeries$"),
         "gcp.api.monitoring.timeseries-list")
```

`API_READ_ROUTES` in that file is the table and the only place it is written down; today it
holds the Monitoring `timeSeries` and `metricDescriptors` lists and the Managed Prometheus
`query`, `query_range`, `series` and `labels` reads, each with a comment saying which
consumer needs it. `REFUSED_HOSTS` beside it names the hosts whose responses are credentials
or that turn a read into a write elsewhere — the token, STS, OAuth, IAM, Secret Manager, KMS
and metadata endpoints — and is checked before the table and never overridable by it; the
validator refuses a table that names one. `PROJECT` is Google's project-id grammar, and
`HOST_SHAPE` the host grammar the table and the handler share.

`evaluate` answers in this order, and the order is the security argument:

1. Method not `GET` → `gcp.api.method`.
2. Host in `REFUSED_HOSTS` → `gcp.api.host-refused`. Listed separately from an unknown host so
   the log distinguishes "asked for a token endpoint" from "asked for a service nobody has
   added".
3. Host not in any route → `gcp.api.host`.
4. Host known, path matches no route → `gcp.api.path`, with the message naming the rule ids
   that host does have.
5. Otherwise allowed, carrying the matching `rule_id` for the audit line.

Every regex is anchored and matched with `fullmatch` — `$` alone also matches before a
trailing newline — so `timeSeries` admits nothing under `timeSeries/` and a project segment
cannot carry a slash. The project id is constrained to Google's grammar so a path cannot
smuggle a second segment through it. The table does not constrain **which** project: the
`gcloud` allowlist takes the same position and its comment says why — deciding scope from
caller text puts a parser where the boundary belongs. IAM bounds the project set; the table
bounds the operation.

`_validate_routes` runs at import, like `_validate_route_roles`: a host with a scheme, port
or upper-case letter, a host in `REFUSED_HOSTS`, a method other than `GET`, a regex not
anchored at both ends or starting with `/`, an empty or duplicate `rule_id` each raise, so a
broker that would enforce less than the table appears to say does not start.

### Token

The broker already obtains its identity through `google.auth.default()` for the chat relay
and the scoped-account pool. `GoogleApiRelay` does the same once, on first use, holds the
credentials object, and lets `google-auth` refresh it through
`google.auth.transport.requests.Request` when `credentials.valid` goes false. Construction
imports nothing, so a broker without the cloud libraries starts as before and the route
answers 503 `RELAY_CREDENTIAL_UNAVAILABLE` instead. `AuthorizedSession` is deliberately not
used, because the upstream call is built by hand (`GoogleApiRelay.fetch`, on
`http.client.HTTPSConnection`) so that no caller header can leak into it; `connection()` is
the seam a test replaces with a plain connection to a fake upstream (its TLS context is built
once per relay), and everything above it —
the header set, the deadline, the cap — then runs for real. The body is read with
`HTTPResponse.read1`, one `recv` per call with the socket timeout re-armed to what is left of
the deadline before each, so a trickling upstream cannot outlive the deadline a chunk at a
time; a connect that hangs is `ApiRelayConnectTimeout`, answered as unreachable rather than as
the deadline. `read1` returns empty on an early EOF without raising for a `Content-Length`
body, so `fetch` raises `IncompleteRead` itself when bytes are still owed after the loop, and
nothing of a page cut short is relayed. The socket the deadline is re-armed on is taken once,
before `getresponse()`: for a close-delimited response (`Connection: close`, HTTP/1.0, no
length) `http.client` drops the connection's reference while the body stays readable through
the response's own handle, and that handle keeps the socket open until the response is closed —
which `read1` does itself on the last announced byte, so the loop stops before re-arming a
closed response.

The token is the broker's ambient Workload Identity token, with the `cloud-platform` scope.
Narrowing it was the intended second layer and does not work: the GKE metadata server returns
the same token whatever `scopes` it is asked for (verified above). Two consequences are stated
rather than hidden:

- The policy table is the only thing between the model's code and everything the platform
  service account can read. That is the same position `/v1/exec` is in for `gcloud`.
- A dedicated read-only identity is the hardening path, and the mechanism exists:
  `scoped_sa_pool.py` already mints impersonated tokens through the IAM Credentials API, where
  `scopes` is honoured and the target account's own roles are the ceiling. A member holding
  only `roles/monitoring.viewer` would make a table mistake cost a metric read rather than a
  project. It is not in this version because the pool is off by default and its members hold
  no grants; when the pool is turned on, the relay should draw from it and the table should
  name the scope per route.

### The client side

`credential_proxy_client.py` is already in the sandbox image and already knows the broker URL
and the caller token. It carries one class for this route, so no skill or collector
reimplements the rewrite:

```python
class ApiSession:
    """A requests-shaped session whose GETs to a Google API go through the broker."""

    def __init__(self, endpoint: str | None = None, http=None) -> None:
        ...  # endpoint defaults to CREDENTIAL_PROXY_URL; http to a lazily imported requests.Session()

    def get(self, url: str, *, params=None, timeout=None):
        return self._http.get(self.relay_url(url), params=params,
                              headers=authorization_headers(), timeout=timeout)
```

`relay_url` rewrites `https://<host>/<path>?<query>` onto
`{CREDENTIAL_PROXY_URL}/v1/gcp/<host>/<path>?<query>`. `requests` is imported inside the
constructor and nowhere at module scope, so the broker and every other importer of the module
stay free of it; `http` is injectable for tests and for a caller that already holds a session.

Two properties of that shape are the point. A collector's URL literal,
`https://monitoring.googleapis.com/v3/projects/{project}/timeSeries`, stays as written, so a
manifest's evidence label still names the real endpoint. And the object satisfies a
`SessionFn`-style contract — "anything with a `requests`-shaped `.get(url, params=,
timeout=)`" — so code written against `google.auth.transport.requests.AuthorizedSession`
runs against it unchanged.

A relay refusal reaches a caller as a 403 whose body names the `rule`; a collector that
records it as its `limitations` string for a cluster gives the model something it can act on
(keep the cluster, skip the check, do not read an empty answer as zero usage).

### What does not change

- **The operator.** The route lives in the broker image; the sandbox already carries the URL
  and the token. No CRD field, no new env, no new NetworkPolicy rule: the broker's egress to
  `googleapis.com` on 443 is what `gcloud` already uses.
- **IAM.** `roles/monitoring.viewer` is already granted and was shown sufficient.
- **The sandbox image.** `requests` is already installed and `credential_proxy_client.py` is
  already on the scripts allowlist, so `test_sandbox_delivery.py` needs no new entry.
- **Envoy.** One route, prefix `/`, timeout `0s`.

## Security review

**What the model's code gains.** `GET` on three path shapes on one host, executed with the
platform service account, returning metric data. A prompt-injected turn can read any project's
metrics that the account can read. It could already read the same clusters' objects through
proxied `kubectl` and the same projects' resources through proxied `gcloud`; this adds
Monitoring to that set.

**What it cannot do through the relay, and which line stops it.**

| Attempt                                                              | Stopped by                                                                                                                                                                                                         |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Mint a token (`iamcredentials`, `sts`, `oauth2`)                     | `REFUSED_HOSTS`, before the table                                                                                                                                                                                  |
| Reach an arbitrary host (exfiltration, SSRF into the VPC)            | Host allowlist; exact match on the first path segment, which must already be a bare lower-case DNS name                                                                                                            |
| Write (`POST` on any host)                                           | Method check, first in `evaluate`; `do_POST` routes the prefix to the relay so the refusal is `gcp.api.method`, and the body is never inspected or forwarded — drained and discarded so the 403 survives the close |
| Write with `PUT`, `PATCH` or `DELETE`                                | The handler class defines no method for them; the server answers 501 before `evaluate` or any route is consulted                                                                                                   |
| Reach a sibling resource (`timeSeries/…`, `alertPolicies`)           | Anchored path regex, `fullmatch`                                                                                                                                                                                   |
| Smuggle a second segment through the project id                      | `PROJECT` grammar                                                                                                                                                                                                  |
| Smuggle one through encoding or dot segments                         | Normal-form check: `%2F`, `..`, `.` and empty segments are 400 before the table                                                                                                                                    |
| Supply its own credential or an API key                              | Caller headers dropped; `key`/`access_token`/`oauth_token`/`bearer_token` stripped                                                                                                                                 |
| Put a byte on the upstream request line the broker did not choose    | `API_RELAY_QUERY_SHAPE`: URL query characters (RFC 3986's plus `[` `]`) and complete percent-escapes only, 400 before the table                                                                                    |
| Send the front end an over-long URL on demand                        | `API_RELAY_MAX_QUERY_BYTES`, 400 before the table; a 414 that does arrive is passed through like any other status                                                                                                  |
| Forge an audit line                                                  | `_sanitize_for_logging` on every caller-supplied field, as on `/v1/exec`                                                                                                                                           |
| Call it from the gateway role                                        | `ROUTE_ROLES` admits `shell` only                                                                                                                                                                                  |
| Exhaust the broker with a huge response, or hand it a page cut short | `API_RELAY_MAX_RESPONSE_BYTES`, read in chunks with a cap rather than `.read()`; a body short of its `Content-Length` is `UPSTREAM_TRUNCATED`, none of it relayed                                                  |
| Park a broker thread on a stalled upstream                           | `API_RELAY_CONNECT_TIMEOUT_S` on the connect; `API_RELAY_DEADLINE_S` re-armed on the socket before every `read1`                                                                                                   |
| Redirect the broker somewhere else                                   | Redirects are not followed; a 3xx is returned to the caller as a 502 with no `Location`                                                                                                                            |

**Residual, stated.** The broker forwards with a `cloud-platform` token because narrowing is
unavailable; a table entry added carelessly is therefore a project-wide read of whatever it
names. The review bar for a new `ApiRoute` is the bar for a new `GCLOUD_READ_COMMANDS` tuple:
one line, one reason in a comment, tests that hold the door one word away. The impersonated
read-only identity above is what lowers that stake and should be scheduled with the pool.

**What it does not touch.** The gateway pod's own ambient identity, which the `gke` remote MCP
server uses today, is a separate open item and not made better or worse by this route.

## Tests

`agents/platform/scripts/test_api_policy.py`:

- Each listed route is allowed with its `rule_id`; the same path with `POST` is refused
  with `gcp.api.method`, and every non-`GET` method is refused before the host is read.
- Every `REFUSED_HOSTS` entry is refused with `gcp.api.host-refused` for any path.
- `logging.googleapis.com` is refused with `gcp.api.host` until an entry exists, and so is a
  host that merely contains a listed one.
- On `monitoring.googleapis.com`: `v3/projects/p/timeSeries/x`, `v3/projects/p/alertPolicies`,
  `v3/projects/P-UPPER/timeSeries`, `v3/projects/a/b/timeSeries`, a `metricDescriptors/`
  child path, a leading slash, a trailing newline and a non-`global` Prometheus location are
  each refused with `gcp.api.path`.
- The table validator: a host with a scheme, port or upper-case letter, a refused host, a
  regex not anchored at both ends, a leading slash, a string path, a write method, an empty
  and a duplicate `rule_id` each raise.

`test_credential_proxy.py`, in the existing `ThreadingHTTPServer` pattern with a fake upstream
standing in for `monitoring.googleapis.com` and the real `GoogleApiRelay.fetch` pointed at it
(`ApiRelayOverTheSocketTest`):

- A permitted `GET` reaches the fake upstream with exactly `Host`, `Authorization` and `Accept`, the caller's `Authorization` replaced by the broker's, the stripped query keys absent and every other pair byte-for-byte — including `match[]=up` and `up[5m]` on the Prometheus routes and a bare `[b]`, with the brackets sent raw.
- The upstream's status, `Content-Type` and body come back unchanged, including a 403, a 429,
  a 404 and a front-end 414 with `Connection: close` from upstream, a body with no
  `Content-Type`, a close-delimited response, and an HTTP/1.0 response without a
  `Content-Length` read to EOF.
- A caller with the `chat` role gets `CALLER_ROLE_FORBIDDEN` before an `api` line is written;
  an unauthenticated or forged caller gets 401.
- A `..`, `.` or empty segment, a percent-encoded slash, an `https://` host, a port, user
  info, an upper-case or percent-encoded host, a missing host or path, and a query with a bad
  percent-escape or a character outside the URL query set (`{`, `"`, `|`, `<`, `\\`, `^`), or a query over the length cap, are each a 400 that names the reason and the part to correct (a query exactly at the cap is
  forwarded); a raw UTF-8 byte, `0xFF`, DEL and a C0 control in the query, sent
  over a raw socket because `http.client` will not send them, are a 400 rather than a dropped
  connection, and a `UnicodeError` or `InvalidURL` escaping the upstream request is the same 400.
- A refused `POST` carrying a body larger than the socket buffers still receives its 403,
  because the body is drained first.
- Each policy refusal (`gcp.api.path`, `gcp.api.host-refused`, `gcp.api.host`,
  `gcp.api.method` through a real `POST`) is a 403 in the `/v1/exec` shape, opens no
  upstream connection, and writes `api blocked … rule=<id>`.
- An upstream body one byte over the cap is a 502 with `UPSTREAM_RESPONSE_TOO_LARGE`; a body
  exactly at the cap passes.
- An upstream 302 is not followed (one upstream request, no `Location` returned).
- An upstream that outlives the deadline is a 504, and one that trickles its body is cut at
  the deadline rather than at the end of the body; one that refuses the connection is a 502,
  a connect that hangs is a 502 whose log names the connect timeout, not the deadline, a TLS
  verification failure is a 502 naming its type rather than a 400, a target `putrequest`
  refuses is still the 400, and one that closes short of its `Content-Length` is a 502
  `UPSTREAM_TRUNCATED` with none of the partial body relayed.
- No relay armed is 503 after the policy has answered; a credential the broker cannot obtain
  is 503 and the log names the exception type, not its message.
- Each of those writes the audit lines the section above specifies, checked with
  `assertLogs`; `ApiRelayAuditLineCannotBeForgedTest` drives a vertical tab and the path
  width through the handler directly.
- `GoogleApiRelayCredentialTest`: `google.auth.default` is called once with the
  `cloud-platform` scope, refresh happens only when the credentials report invalid,
  construction imports nothing, and the upstream connection is TLS on 443 with the bounded
  connect.
- `ApiRelayQueryStrippingTest`: the four credential keys are removed wherever they sit and
  however the key is encoded, every other pair is forwarded byte-for-byte, and a key that
  merely contains a stripped one stays.

`test_credential_proxy_client.py` (`ApiSessionTest`):

- `ApiSession.get` rewrites the URL onto `API_RELAY_PREFIX`, keeps `params` and `timeout`,
  attaches `authorization_headers()`, keeps a query written into the URL, strips the
  endpoint's trailing slash, fails clearly without `CREDENTIAL_PROXY_URL`, fails the call and
  not the construction when the caller token is unreadable, defaults to a `requests.Session`,
  and the module imports in a subprocess where `requests` is absent.

## Live validation

On a live install, under the live-test lease, with the broker image rebuilt from the
branch:

1. From the sandbox pod, a `curl` to the relay for one `timeSeries` page returns 200 with
   `k8s_container` series for a cluster in the project, and the broker log shows the `api`
   request and forwarded lines sharing one request id, with `rule` absent because the read was
   allowed.
2. The same `curl` for `v3/projects/<p>/alertPolicies` returns 403 with `rule=gcp.api.path`;
   for `iamcredentials.googleapis.com/v1/...:generateAccessToken` returns 403 with
   `rule=gcp.api.host-refused`; with `-X POST` returns 403 with `rule=gcp.api.method`; each
   with an `api blocked` line naming the rule.
3. From the gateway pod, the same `curl` with the gateway's token returns 403
   `CALLER_ROLE_FORBIDDEN`.
4. Follows with the consumer, not with this change: once a collector reads usage through
   `ApiSession`, a `fleet-wide-cost-analysis` run's document carries a `scope.clusters` entry
   whose `checks_run` names the `overrequest` check and whose `limitations` is empty, which
   is the state the SOP describes as a complete 3.1.

Each step names what to observe rather than what to run, per `.agents/rules/pre_pr_review.md`.

## What ships, and what follows

On `main` now: `agents/platform/scripts/api_policy.py`, the `/v1/gcp/` route and
`GoogleApiRelay` in `credential_proxy.py`, `ApiSession` in `credential_proxy_client.py`, the
tests above, and the route's mention in the documents that enumerate the broker's paths — the
site's `reference/security-and-iam.md` and `reference/credential-isolation.md`,
`docs/credential-isolation-design.md`, `docs/security-requirements.md` and the role table
paragraph of `agent-shell-sandboxing.md`. It is useful without a consumer: any script in the
sandbox can read the three Monitoring shapes through it today.

What follows is the consumer's side, and it is a contract rather than code on `main`: a
collector that needs Monitoring history obtains its `requests`-shaped session from
`ApiSession()` instead of from `google.auth`, keeps its URL literals as the real endpoints,
and treats a relay 403 — whose body names the `gcp.api.*` rule — as that cluster's
`limitations` note rather than as zero usage, which is the reading the cost SOP already
prescribes for a usage check that did not run. Nothing in the sandbox can use `google.auth`,
so a consumer written that way has no second path to remove.

## Rejected alternatives

- **Keep sampling with proxied `kubectl top`.** Needs nothing new and is already allowed; it
  is what the cost SOP's §3.1 does today, three samples about five minutes apart over a
  ten-minute window, and the SOP's own
  sampling-honesty paragraph says what that cannot see — a nightly batch peak, a weekday
  curve. A week of history is what the follow-up collector adds so a proposed request value
  rests on more than one Monday morning; the sampling stays as the fallback where the relay
  answers with a `limitations` note.
- **Ship `google-auth` in the sandbox.** The import would succeed and the call would fail with
  the unbound identity. The sandbox Dockerfile does not install the package for that reason.
- **Run the Monitoring read on the gateway as a `no_agent` job.** Works only because the
  gateway still carries an identity the sandboxing design is removing, splits the collector
  across two pods since the gateway has no `kubectl`, and writes the manifest to the volume the
  model cannot read. The design already refused this shape for the GitHub token refresh job.
- **The Monitoring remote MCP server.** Real, and it has `list_timeseries` and `query_range`,
  but a tool a model calls is not available to a script, and wiring it the way the `gke` server
  is wired adds a second dependency on the gateway's ambient identity.
- **A general authenticating forward proxy.** Signing anything the sandbox sends is the
  credential handed over in every respect except exportability.
- **A transparent proxy** (universe-domain and metadata-host variables on the sandbox, an
  internal CA, DNS to Envoy, external authorization calling the broker). The strategic form of
  this design: unchanged client code, gRPC included. It reuses this table and is a larger
  change; this route is the piece of it that is needed now.

## Open questions

- Whether `roles/mcp.toolUser`-style per-tool IAM deny policies, which Google applies to its
  managed MCP servers, have a REST-API equivalent that would let the project constrain the
  relay's reads independently of this table. Not found at the time of writing.
- When the scoped pool turns on, whether the relay should refuse rather than fall back to the
  ambient identity when no read-only member exists. The pool's own rule is refuse; the relay
  should follow it.

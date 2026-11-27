# Read-Only Cloud API Relay in the Credential Broker

## Summary

The Platform Agent's shell, file tools and code execution run in the shell sandbox pod, whose
ServiceAccount carries no cloud identity. Every credentialed call the model's code makes goes
through the credential broker, and until this route the broker spoke one shape: an argv for
`kubectl`, `gcloud`, `gh` or `git`, checked against an allowlist and executed on the broker's
side. No `gcloud` command reads Monitoring time series, so the cost audit's collector, which
needs a week of per-pod usage, had no sanctioned path to it.

The broker now speaks a second shape: an HTTP relay for **read-only Google Cloud REST calls**.
The sandbox sends an unauthenticated `GET` for a Google API URL to the broker; the broker
checks the host, method and path against a table of permitted reads, attaches its own
credential, forwards the request, and returns the response body. The model's code never holds
a Google token. What it gains is exactly the reads the table names, on the identity the broker
already has, with the same caller authentication, refusal shape and audit line as `/v1/exec`.
One read whose arguments travel in a JSON body, Logging `entries:list`, is a `POST`: the broker
reads the body, holds it to the entry's check, and forwards its own copy of it
([Reads with a body](#reads-with-a-body)).

The first table entries are the Monitoring `timeSeries` and `metricDescriptors` reads and the
Managed Prometheus query endpoints; the Cloud Trace `traces` list and single-trace get followed,
for the observability skill's trace helpers, and then the GKE `projects.locations.clusters` list and
get, for the Cluster Agent reconcile (#3703; stall_watch's cluster listing reads the list, #5598; the shim's `get-credentials` check reads the
get too, #5447), with their answers projected, and then Logging `entries:list` for one project,
for the code-owned `gcloud logging read` callers. A collector's one-function change is to obtain its
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
  A Logging entry would mean a third service, and a per-service route means a handler per service. The host
  allowlist is what stops the relay being a forward proxy: a host not in the table is refused
  before the path is read.
- **A code table, not the JSON policy ConfigMap.** The broker's `policy.json` is a regex
  denylist over argv text; `GCLOUD_READ_COMMANDS` is a code allowlist, reviewed as a diff. The
  relay is an allowlist, so it follows the second. Widening it is a pull request, which is the
  property the `gcloud` table's comment asks for.
- **`GET`, and `POST` only for a read with a body check.** Monitoring's `timeSeries.list` and
  the Managed Prometheus `query` and `query_range` are `GET`, and the first version relayed
  nothing else. The reads that are `POST` — Logging `entries:list`, Monitoring
  `timeSeries:query` for MQL — carry a body the policy has to inspect. `entries:list` is now
  the one such entry, with a body cap and a check of its own
  ([Reads with a body](#reads-with-a-body)); `timeSeries:query` stays refused.

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
3. Evaluates `api_policy.evaluate(method, host, path, query, universe_domain=...)` with the
   install's universe domain. A refusal answers 403 with the
   same body shape `/v1/exec` uses, so a caller that already reads `rule` and `message` reads
   these:

   ```json
   {
     "status": "blocked",
     "code": "SECURITY_POLICY_BLOCKED",
     "rule": "gcp.api.host",
     "message": "compute.googleapis.com is not a host the credential proxy relays. Reads are added to api_policy.API_READ_ROUTES by pull request."
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
   returns the upstream status code, `Content-Type` and body unchanged, except on a route
   that carries a projection (the two GKE cluster reads below): its 2xx body is cut down to
   the fields the route names, and a 2xx body the projection cannot read is a 502
   `UPSTREAM_UNEXPECTED` rather than relayed. What the broker
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
   | A projected route's 2xx body the projection cannot read                           | 502    | `UPSTREAM_UNEXPECTED`          |

   The over-cap remedy is a smaller `pageSize`, which every listed endpoint supports; the
   message says so.

6. Writes two audit lines per request, on the pattern of the exec route: one before the
   decision and exactly one verdict after it. The request id is minted by the broker, so the
   lines of one request share it. A read with a body that names one resource (today
   `entries:list`) writes one more line between the two, when its body passes the route's
   check:

   ```
   api request_id=%s principal=%s host=%s path=%s
   api rejected request_id=%s code=%s reason=%s
   api blocked request_id=%s rule=%s
   api body checked request_id=%s rule=%s resource=%s
   api forwarded request_id=%s host=%s status=%d bytes=%d duration_ms=%d[ resource=%s]
   ```

   `host` and `path` are caller text and go through `_sanitize_for_logging` as `argv[0]`
   does (`path` at 256 characters, the width the exec route gives a `cwd`). The forwarded line
   of a read with a body ends with ` resource=projects/<project>`, the one resource name its
   body check passed: the path of such a read names no project. The `api body checked` line
   names the same resource before the upstream call, so a verdict that is not `forwarded`
   (over the cap, a redirect, an upstream or credential failure, a disabled relay) also
   follows a line that says which project the read asked for. A refused body returns before
   that line: its resource is in no line. The `audit` mapping does not carry the resource,
   and the `api body checked` line carries no `audit` mapping. The upstream
   failure cases in the table above are the verdict line in their case, naming the condition,
   and the exception type where there is one, never its message; the disabled-relay 503
   writes `api disabled request_id=%s rule=%s`. Each of these records also carries the
   `audit` mapping the exec route attaches to its records (the site's
   [observability page](../site/src/content/docs/concepts/observability.md#cloud-logging)), so
   a relayed read is in the same `tool_execution_audit` trail as a brokered command. The
   `tool` is `gcp-api`, apart from `gcloud` so that a `gcloud` record still means a process
   ran; the `subcommand` is the service word of a host the table names in the install's
   universe domain (`monitoring`, `cloudtrace`, `container`, `logging`) and `other` for any
   other host, the commercial host on a custom universe domain included. The first line is `started`; the
   verdict is `rejected` for a 400, `blocked` with the `rule` for a 403, `completed` with
   `duration_ms` and `upstream_status` for a forwarded answer whatever its status, and
   `failed` for the disabled relay, a credential the broker cannot obtain and each upstream
   failure. A `failed` record carries `duration_ms` only when the upstream answered (an
   answer over the cap, a redirect, a body the projection cannot read). The mapping never carries the host, the path or the query.

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
| `API_RELAY_MAX_BODY_BYTES`      | `16 KiB`      | The body of a `POST` read; `gcloud`'s own `entries:list` bodies are a few kilobytes                                                                                                                                                                                                                                 |

The host shape is `api_policy.HOST_SHAPE`, one regex the table validator and the handler share.

### Reads with a body

Logging `entries:list` is a read, and its arguments are a JSON body, so its route is a `POST`.
It is the one `POST` the relay forwards:

```
POST {CREDENTIAL_PROXY_URL}/v1/gcp/logging.googleapis.com/v2/entries:list
Authorization: Bearer <projected caller token>

{"resourceNames": ["projects/{project}"], "filter": "...", "orderBy": "timestamp desc", "pageSize": 1000}
```

The handler decides on the method, host and path before it reads a byte of the body. A `POST`
that is not, exactly, a table entry with a body check is refused with `gcp.api.method` and its
body is drained and discarded, as before. On an install with a custom universe domain the
entry's host is that universe's Logging host and the commercial host is refused, by the rule
[the policy module](#the-policy-module) states for every route. For the entry, in order:

1. The query must be empty (`gcp.api.body`): the read's arguments are its body, and a query
   is a second place to put one.
2. The body is read under `API_RELAY_MAX_BODY_BYTES` (16 KiB; `gcloud`'s own bodies for the
   same read are a few kilobytes). A request the server cannot frame (a `Transfer-Encoding`,
   a length that is not a number, two lengths that disagree) is a 400 and one over the cap
   is a 413, neither read into the policy; a body that is absent or not JSON is a 400
   (`API_RELAY_BAD_BODY`, `API_RELAY_BODY_TOO_LARGE`). A body that stalls past the request's
   read deadline ends the connection, and the audit trail still gets a `rejected` record.
3. `api_policy.evaluate_body` holds the parsed body to the entry's check, and a refusal is a
   403 with `gcp.api.body`. Its message never repeats the caller's text.
4. The broker forwards **its own serialisation** of the object that passed, with its own
   `Content-Type: application/json`, never the caller's bytes. A duplicate key, of which the
   parser keeps the last and the check reads the last, reaches the upstream as one key.

The `entries:list` check (`api_policy.check_logging_entries_list`) admits the five keys
`gcloud logging read` sends and nothing else:

| Key             | Admitted                                                                                       |
| --------------- | ---------------------------------------------------------------------------------------------- |
| `resourceNames` | Required. Exactly one `projects/<project>`, in the `PROJECT` grammar the path routes use       |
| `filter`        | A string. Google's grammar, not parsed here, as `gcloud logging read`'s filter argument is not |
| `orderBy`       | `timestamp asc` or `timestamp desc`                                                            |
| `pageSize`      | An integer from 1 to 1000, the API's ceiling                                                   |
| `pageToken`     | A string                                                                                       |

So a body cannot name a folder, an organisation, a billing account, a log bucket or view, or a
second project, and the deprecated `projectIds`, a second way to name a project, is refused as
an unknown key. The filter cannot widen the read past the resource the body names.

This is the read the broker already serves as `gcloud logging read`, and no wider. For the
shell role `command_policy` admits `gcloud logging read FILTER --project <any project>`; it
refuses `--billing-account`, `--folder`, `--organization`, `--bucket`, `--view` and
`--resource-names`. Neither door holds the project to the install's
scope: as for every route here, IAM bounds the project set and the table bounds the
operation. Both run on the broker's own credential.

The client is `ApiSession.post`, and `logging_reads.read_json` is the read the collectors
call. It sends the requests `gcloud logging read` sends, page for page: up to 1000 entries a
request, the next `nextPageToken` until the caller's limit (the API answers with shorter pages
when the entries are large), each later request asking for what is left. So a read costs the
project's `entries.list` request quota what it did. It returns the JSON list `gcloud logging
read --format json` prints. Whenever the relay does not give the whole answer — a broker older
than the route, a relay that is off, a refusal, an upstream error, a page over
`API_RELAY_MAX_RESPONSE_BYTES` — it returns nothing and the caller runs its `gcloud logging
read` as before. The caller's own time cap bounds the read on both paths together: a relay
read that spends it is the timed-out read `gcloud` would have been, and a `gcloud` run after
the relay took its time gets what is left.

### The policy module

`agents/platform/scripts/api_policy.py`, stdlib only, importable by the broker and by tests.
It reuses `command_policy.Decision` so the handler's refusal code is shared. One row per
permitted read:

```python
@dataclass(frozen=True)
class ApiRoute:
    host: str            # exact match, lower-cased
    method: str          # API_READ_METHOD ("GET"), or API_BODY_METHOD ("POST") with `body`
    path: re.Pattern     # anchored at both ends, fullmatched against the whole path
    rule_id: str         # what the audit line and the 403 name
    project: Callable[[bytes], bytes | None] | None = None  # 2xx body -> what is relayed
    body: Callable[[object], str | None] | None = None      # parsed request body -> what is wrong with it

# One entry, as an example of the shape; the table is in api_policy.py.
ApiRoute("monitoring.googleapis.com", API_READ_METHOD,
         re.compile(rf"^v3/projects/{PROJECT}/timeSeries$"),
         "gcp.api.monitoring.timeseries-list")
```

`API_READ_ROUTES` in that file is the table and the only place it is written down; today it
holds the Monitoring `timeSeries` and `metricDescriptors` lists, the Managed Prometheus
`query`, `query_range`, `series` and `labels` reads, and the Cloud Trace `traces` list and
`traces/<id>` get (the id held to 32 lower-case hex characters), and the GKE
`projects.locations.clusters` list and get the Cluster Agent reconcile reads (#3703; stall_watch reads the list too, #5598; the get is
also the shim's check that a cluster exists before it answers a `get-credentials`, #5447, sent with
the `X-Kube-Agents-Get-Credentials-Check` header, on which the broker refuses the get for a cluster
`spec.scope.exclude.clusters` names; the header only narrows, and a read without it is unchanged; location
`-` or a region or zone, cluster name in GKE's grammar, both answers projected to each
cluster's `name`, `location`, `status` and `id`, so `masterAuth` is never relayed), each with a comment saying
which consumer needs it. `REFUSED_HOSTS` beside it names the hosts whose responses are credentials
or that turn a read into a write elsewhere — the token, STS, OAuth, IAM, Secret Manager, KMS
and metadata endpoints — and is checked before the table and never overridable by it; the
validator refuses a table that names one. `PROJECT` is Google's project-id grammar or a project
number (a decimal int64), since an install's `projectId` may be either, and `HOST_SHAPE` the host
grammar the table and the handler share.

`evaluate` reads the table in the install's universe domain, which the handler passes to it. On
the commercial domain, `googleapis.com`, the hosts are as the table writes them. On a custom
universe domain each `<service>.googleapis.com` host of the table is read as
`<service>.<universe domain>`, so the host as the table writes it is in no route there: a `GET`
for it is refused with `gcp.api.host`, any other method at step 1. A `REFUSED_HOSTS` entry is
refused there as written, and a `<service>.googleapis.com` entry also as
`<service>.<universe domain>`. A universe domain that is not valid, which is a malformed one or
one under `googleapis.com`, has no routed host, so every request is refused: a `GET` with
`gcp.api.host`, any other method at step 1. The broker does not start with such a value.

The broker also does not start with a value from its environment (`GOOGLE_CLOUD_UNIVERSE_DOMAIN`,
then `CLOUDSDK_CORE_UNIVERSE_DOMAIN`) that the host's metadata server contradicts: a well-formed
value can name hosts of another universe, or commercial hosts under another name, and the
metadata server says which universe the host is in. A value that the metadata server confirms
starts. A host with no universe route (HTTP 404) starts too, with a WARNING record that the value
was not checked. If the metadata server gives no answer in ten tries, about 15 seconds, the
broker starts with the value and writes the same record: a host with no metadata server, such
as a harness on kind, never gets an answer. That broker asks again each 30 seconds until the
metadata server answers. An answer that differs ends the process at once with a CRITICAL
record, and the container then starts again into the start check, which refuses the value
when the metadata server answers.
The operator holds `spec.harness.universeDomain` to
the same answer before it renders; the broker's check holds what its own environment names.

`evaluate` answers in this order, and the order is the security argument:

1. A `POST` that is not exactly the host and path of an entry with a body check, or any
   method other than `GET` and `POST` → `gcp.api.method`. A `POST` that is such an entry is
   allowed here when its query is empty (`gcp.api.body` otherwise), and its body is still to
   pass `evaluate_body`.
2. Host in `REFUSED_HOSTS`, in either of those forms → `gcp.api.host-refused`. Listed separately from an unknown host so
   the log distinguishes "asked for a token endpoint" from "asked for a service nobody has
   added".
3. Host not in any route for the universe domain → `gcp.api.host`.
4. Host known, path matches no route → `gcp.api.path`, with the message naming the rule ids
   that host does have.
5. Otherwise allowed, carrying the matching `rule_id` for the audit line.

Every regex is anchored and matched with `fullmatch` — `$` alone also matches before a
trailing newline — so `timeSeries` admits nothing under `timeSeries/` and a project segment
cannot carry a slash. The project segment is constrained to Google's id grammar or a project
number so a path cannot smuggle a second segment through it. The table does not constrain **which** project: the
`gcloud` allowlist takes the same position and its comment says why — deciding scope from
caller text puts a parser where the boundary belongs. IAM bounds the project set; the table
bounds the operation.

`_validate_routes` runs at import, like `_validate_route_roles`: a host with a scheme, port
or upper-case letter, a host in `REFUSED_HOSTS`, a method other than `GET` or `POST`, a `POST`
entry with no body check or a `GET` entry with one, a regex not
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
    """A requests-shaped session whose reads of a Google API go through the broker."""

    def __init__(self, endpoint: str | None = None, http=None) -> None:
        ...  # endpoint defaults to the exec tier's URL, else CREDENTIAL_PROXY_URL; http to a lazily imported requests.Session()

    def get(self, url: str, *, params=None, timeout=None):
        ...  # self._http.get(self.relay_url(url), params=params, headers=authorization_headers(), timeout=timeout), with the exec tier's fallback
```

`relay_url` rewrites `https://<host>/<path>?<query>` onto `{broker}/v1/gcp/<host>/<path>?<query>`,
where the broker is the exec tier when `CREDENTIAL_PROXY_EXEC_URL` is set and well-formed, else
`CREDENTIAL_PROXY_URL` ([credential-broker-exec-tier.md](credential-broker-exec-tier.md) §3.2
has the routing and the fallback). When the caller's environment names a custom universe domain
(`GOOGLE_CLOUD_UNIVERSE_DOMAIN`, which the sandbox entrypoint forwards to each sandbox SSH
session, then `CLOUDSDK_CORE_UNIVERSE_DOMAIN`), `relay_url` also rewrites a
`<service>.googleapis.com` host to `<service>.<universe domain>`, the form in which
[the policy module](#the-policy-module) reads the table's hosts there.
`requests` is imported inside the
constructor and nowhere at module scope, so the broker and every other importer of the module
stay free of it; `http` is injectable for tests and for a caller that already holds a session.

Two properties of that shape are the point. A collector's URL literal,
`https://monitoring.googleapis.com/v3/projects/{project}/timeSeries`, stays as written, so a
manifest's evidence label still names the Google endpoint the collector reads, in its commercial
form on a custom universe domain. And the object satisfies a
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
- **IAM, for the Monitoring routes.** `roles/monitoring.viewer` is already granted and was shown
  sufficient. The Cloud Trace routes that followed are the exception: they need
  `roles/cloudtrace.user`, which the composition now binds beside it.
- **The sandbox image.** `requests` is already installed and `credential_proxy_client.py` is
  already on the scripts allowlist, so `test_sandbox_delivery.py` needs no new entry.
- **Envoy.** One route, prefix `/`, timeout `0s`.

## Security review

**What the model's code gains.** `GET` on the path shapes the table names, executed with the
platform service account: metric data from Monitoring and Managed Prometheus, every trace and
span from Cloud Trace that the account can read, and each GKE cluster's name, location, status
and id. The two GKE routes return only those four fields: the projection drops the rest of the
cluster object, `masterAuth` included, and an answer it cannot read is a 502, not the raw body. A
prompt-injected turn can read any project's metrics and traces that the account can read. It
could already read the same clusters' objects through proxied `kubectl` and the same projects'
resources through proxied `gcloud`; this adds Monitoring metric data and Trace to that set, and
the GKE fields above, which proxied `gcloud` already returns. The one `POST` entry adds the
log entries of one project per request, of any project the account can read, which proxied
`gcloud logging read` already returns. The
Trace-console writes `roles/cloudtrace.user` carries (analysis-report tasks, trace scopes) are
not in it: a `POST` that is not the Logging `entries:list` entry is refused on its method
before the host is read.

**What it cannot do through the relay, and which line stops it.**

| Attempt                                                              | Stopped by                                                                                                                                                                                                         |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Mint a token (`iamcredentials`, `sts`, `oauth2`)                     | `REFUSED_HOSTS`, before the table                                                                                                                                                                                  |
| Reach an arbitrary host (exfiltration, SSRF into the VPC)            | Host allowlist; exact match on the first path segment, which must already be a bare lower-case DNS name                                                                                                            |
| Write (`POST` on any path but the one read the table names)          | Method check, first in `evaluate`: a `POST` is allowed only as the exact host and path of an entry with a body check; the refusal is `gcp.api.method`, and the body is then drained and discarded, never inspected |
| Read a folder's or a billing account's logs, or a second project     | The `entries:list` body check: `resourceNames` is exactly one `projects/<project>`, and `projectIds` is not a permitted key                                                                                        |
| Slip a field past the body check (a duplicate or unknown key)        | Unknown keys are refused, and the broker forwards its own serialisation of the object the check read, never the caller's bytes                                                                                     |
| Put an argument of the `POST` read in its query                      | The entry takes no query: `gcp.api.body`                                                                                                                                                                           |
| Hand the broker a huge or unframed request body                      | `API_RELAY_MAX_BODY_BYTES`: a body over the cap is a 413 and a request the server cannot frame a 400, neither parsed                                                                                               |
| Write with `PUT`, `PATCH` or `DELETE`                                | The handler class defines no method for them; the server answers 501 before `evaluate` or any route is consulted                                                                                                   |
| Reach a sibling resource (`timeSeries/…`, `alertPolicies`)           | Anchored path regex, `fullmatch`                                                                                                                                                                                   |
| Smuggle a second segment through the project id                      | `PROJECT` grammar                                                                                                                                                                                                  |
| Smuggle one through encoding or dot segments                         | Normal-form check: `%2F`, `..`, `.` and empty segments are 400 before the table                                                                                                                                    |
| Supply its own credential or an API key                              | Caller headers dropped; `key`/`access_token`/`oauth_token`/`bearer_token` stripped                                                                                                                                 |
| Put a byte on the upstream request line the broker did not choose    | `API_RELAY_QUERY_SHAPE`: URL query characters (RFC 3986's plus `[` `]`) and complete percent-escapes only, 400 before the table                                                                                    |
| Send the front end an over-long URL on demand                        | `API_RELAY_MAX_QUERY_BYTES`, 400 before the table; a 414 that does arrive is passed through like any other status                                                                                                  |
| Forge an audit line                                                  | `_sanitize_for_logging` on every caller-supplied field, as on `/v1/exec`; a body's resource name is logged only after `check_logging_entries_list` held it to `projects/<project>`                                 |
| Call it from the gateway role                                        | `ROUTE_ROLES` admits `shell` only                                                                                                                                                                                  |
| Exhaust the broker with a huge response, or hand it a page cut short | `API_RELAY_MAX_RESPONSE_BYTES`, read in chunks with a cap rather than `.read()`; a body short of its `Content-Length` is `UPSTREAM_TRUNCATED`, none of it relayed                                                  |
| Park a broker thread on a stalled upstream                           | `API_RELAY_CONNECT_TIMEOUT_S` on the connect; `API_RELAY_DEADLINE_S` re-armed on the socket before every `read1`                                                                                                   |
| Redirect the broker somewhere else                                   | Redirects are not followed; a 3xx is returned to the caller as a 502 with no `Location`                                                                                                                            |

The relay refuses `alertPolicies` because no table entry names it, not because the read is forbidden.
The broker serves the same read through `/v1/exec` as `gcloud monitoring policies list` or `describe`
(`GCLOUD_READ_COMMANDS` in `command_policy.py`). The table row above shows only the relay's path check.

**Residual, stated.** The broker forwards with a `cloud-platform` token because narrowing is
unavailable; a table entry added carelessly is therefore a project-wide read of whatever it
names. The review bar for a new `ApiRoute` is the bar for a new `GCLOUD_READ_COMMANDS` tuple:
one line, one reason in a comment, tests that hold the door one word away. The impersonated
read-only identity above is what lowers that stake and should be scheduled with the pool.

**What it does not touch.** The gateway pod's own ambient identity is a separate open item and not
made better or worse by this route. The `gke` remote MCP server no longer uses it: it goes through
the broker's MCP relay (`mcp_policy.py`), on the broker's credential.

## Tests

`agents/platform/scripts/test_api_policy.py`:

- Each listed route is allowed with its `rule_id`; the same path with `POST` is refused
  with `gcp.api.method`, and every method but `GET` and `POST` is refused before the host is read.
- Every `REFUSED_HOSTS` entry is refused with `gcp.api.host-refused` for any path.
- A host no entry names is refused with `gcp.api.host`, and so is a
  host that merely contains a listed one.
- Logging `entries:list` is allowed as a `POST` with its `rule_id` and is the only entry with
  a body check. `entries:write`, `entries:tail`, `entries:copy`, a trailing slash or newline,
  an encoded colon, another API version, sinks, exclusions, logs and a log view are refused
  as a `POST` with `gcp.api.method`; the route as a `GET`, and every other Logging path as a
  `GET`, with `gcp.api.path`; a `POST` on each `GET` route (Monitoring `timeSeries`, where it
  is a write, and the GKE `clusters` collection among them) and on a refused or unknown host
  with `gcp.api.method`; the route with any query with `gcp.api.body`.
- The body check admits the body `gcloud logging read` sends and refuses, with
  `gcp.api.body`: a missing, empty, doubled or non-list `resourceNames`; a folder, an
  organisation, a billing account, a log view, a log name, an upper-case project; any key
  outside the five, `projectIds` among them; a value of the wrong type or range; a body that
  is not an object. No refusal message repeats the caller's text.
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
  a 404 (the sandbox's `ApiSession` hands these back as they are, and re-sends only a reply
  whose top-level `code` is the broker's own busy or resource-pressure code: the exec-tier
  design, §4.3) and a front-end 414 with `Connection: close` from upstream, a body with no
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
- An `entries:list` reaches the upstream as a `POST` with the broker's headers and none of the
  caller's, and its body is the broker's serialisation: a body with a duplicate key arrives
  with one. A body the check refuses, and a query on the route, open no connection. A body
  that is not JSON is a 400, one over the cap a 413 (one larger than the socket buffers
  included), a request that cannot be framed or has no body a 400, and one exactly at the cap
  is read. A body that stalls leaves a `started` and a `rejected` record. The chat
  and session roles are refused. Each outcome writes its audit record under the `logging`
  subcommand, and the filter is in no log line. The forwarded line of an `entries:list` ends
  with the body's `projects/<project>`, the line of a forwarded `GET` names no resource, and
  no line of a refused body carries its project. An `entries:list` whose body passed the
  check writes the `api body checked` line with the project before its verdict, when the
  verdict is `forwarded` and when it is a failure (over the cap, no relay, no credential,
  an unreachable upstream).
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
4. With the consumer, `fleet_waste.py`, reading usage through `ApiSession`: a
   `fleet-wide-cost-analysis` run's document carries a `scope.clusters` entry
   whose `checks_run` names the `overrequest` check and whose `limitations` is empty, which
   is the state the SOP describes as a complete 3.1.

Each step names what to observe rather than what to run, per `.agents/rules/pre_pr_review.md`.

## What ships, and what follows

On `main` now: `agents/platform/scripts/api_policy.py`, the `/v1/gcp/` route and
`GoogleApiRelay` in `credential_proxy.py`, `ApiSession` in `credential_proxy_client.py`, the
tests above, and the route's mention in the documents that enumerate the broker's paths — the
site's `reference/security-and-iam.md` and `reference/credential-isolation.md`,
`docs/credential-isolation-design.md`, `docs/security-requirements.md` and the role table
paragraph of `agent-shell-sandboxing.md`. Any script in the sandbox can read the Monitoring,
Managed Prometheus, Trace and GKE cluster reads the table names through it, and one project's
log entries.

The first consumer is `agents/platform/skills/fleet-audit/scripts/fleet_waste.py`, the cost audit's collector
(`fleet_health.py`, the chat-only fleet health sweep, reads the Managed Prometheus `query` route the same way,
and `gke_cluster_reads.py` makes the Cluster Agent reconcile's GKE list and get reads;
`logging_reads.py` makes the stockout collector's, the GCE compute collector's and the readiness report's log reads, and falls back to `gcloud logging read`),
and the contract it follows binds any later one: a collector that needs Monitoring history obtains its `requests`-shaped session from
`ApiSession()` instead of from `google.auth`, keeps its URL literals as the commercial endpoints,
and treats a relay 403 — whose body names the `gcp.api.*` rule — as that cluster's
`limitations` note rather than as zero usage, which is the reading the cost SOP already
prescribes for a usage check that did not run. Nothing in the sandbox can use `google.auth`,
so inside it the relay is the only path. `fleet_waste.py` falls back to Application Default
Credentials only when `CREDENTIAL_PROXY_URL` is unset, which is a run outside the sandbox.

## Rejected alternatives

- **Keep sampling with proxied `kubectl top`.** Needs nothing new and is already allowed; it
  is what the cost SOP's §3.1 did before its collector, three samples about five minutes
  apart over a ten-minute window, which cannot see a nightly batch peak or a weekday curve. A
  week of history lets a proposed request value rest on more than one Monday morning. Where
  the relay refuses, the usage checks are a `limitations` note rather than a sampled
  fallback, because a point sample is the evidence the week replaced.
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

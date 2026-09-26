// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"time"
)

const (
	// injectKindDrift is stamped on every payload this binary sends, and is the
	// string a playbook skill matches to tell a drift inject from the event
	// watcher's. This declaration and INJECT_KIND_DRIFT in
	// agents/platform/scripts/session_kv_server.py are one decision in two
	// languages: the daemon dispatches on this exact string, so changing it here
	// alone leaves the detector sending a kind nothing routes, and the records
	// are acked. A test on the Python side reads this line and asserts the two
	// agree, which is what makes the pair a check rather than a convention.
	// docs/designs/drift-detection.md and the AutoOps architecture's domain table
	// name it in prose and follow these two, not the other way round.
	injectKindDrift = "gitops-drift"

	// sessionsPath and injectPathSuffix compose the two daemon endpoints. The
	// inject URL is sessionsPath + "/" + id + injectPathSuffix, which is why the
	// suffix carries its own leading slash and the id does not.
	sessionsPath     = "/sessions"
	injectPathSuffix = "/inject"

	// healthzPath is the daemon's unauthenticated liveness endpoint, and also
	// where it advertises the inject kinds its dispatch understands. See
	// VerifyKindSupported for why this binary refuses to start when the kind it
	// sends is not on that list.
	healthzPath = "/healthz"

	// The headers the daemon reads. Authorization carries the same bearer token
	// the event watcher uses; X-Asserted-Caller is how the daemon attributes the
	// session to an owner, and an empty one is omitted rather than sent blank.
	authorizationHeader  = "Authorization"
	bearerPrefix         = "Bearer "
	assertedCallerHeader = "X-Asserted-Caller"
	contentTypeHeader    = "Content-Type"
	contentTypeJSON      = "application/json"

	// defaultInjectTimeout bounds one HTTP call to the daemon, and matches the
	// event watcher's. It is no longer what bounds a record's escalation:
	// perRecordInjectBudget below is smaller and is derived from the handler's
	// context, so in the shapes that matter -- an unresponsive daemon, a hung
	// connection -- the budget expires first and this ceiling is never reached.
	// It still bounds a call the budget does not cover, which is any call made
	// outside Handle.
	defaultInjectTimeout = 10 * time.Second

	// errorBodyLimit caps how much of a failed response is quoted into the log.
	// The daemon's errors are a sentence; anything longer is a proxy's HTML
	// error page, which is worth recognising and not worth logging whole.
	errorBodyLimit = 4096

	// injectStatusSuppressed is the daemon's word for "accepted, then dropped":
	// the day's alert ceiling for this signal was already spent. It answers 200
	// deliberately so a client does not retry into a ceiling that has not moved,
	// which means the HTTP status alone cannot tell a delivered inject from a
	// dropped one and the body is where the difference is. Counted separately
	// here for that reason -- a run reporting injects that all suppressed looks
	// identical, in every other line, to one that alerted a human each time.
	injectStatusSuppressed = "suppressed"

	// injectRetries is how many times one failed call is retried. One, and only
	// for a fault that a second attempt could plausibly clear (see
	// retryableInjectFailure). The handler runs inside the batch's shared join
	// budget, so a longer ladder here is spent out of the lookups the rest of
	// the batch has not made yet -- the starvation #1768 is already about.
	injectRetries = 1

	// injectRetryDelay is the pause before that retry. Short for the same
	// reason: it is borrowed from the same budget.
	injectRetryDelay = 250 * time.Millisecond

	// seenInsertIDsCap bounds the duplicate-suppression set. Pub/Sub delivers at
	// least once and processBatch acks after the handler returns, so a redeliver
	// is ordinary rather than exceptional: without this, one redelivered batch
	// opens a second session and pages a human twice for a single change.
	//
	// A cap rather than a TTL because the key is Cloud Logging's insertId, which
	// is unique per entry for all time -- there is no moment at which forgetting
	// one becomes correct, only a point past which remembering costs more than
	// the redelivery would. Redelivery happens within the ack deadline, so a
	// window measured in thousands of records is far wider than it needs to be.
	seenInsertIDsCap = 4096

	// schemeHTTP and schemeHTTPS are the only two --daemon-url may carry. Named
	// because they are compared against rather than printed.
	schemeHTTP  = "http"
	schemeHTTPS = "https"

	// httpStatusServerErrorFloor is the first 5xx. At or above it the daemon is
	// reporting its own failure rather than rejecting the request, which is the
	// only class of status worth sending again.
	httpStatusServerErrorFloor = 500

	// summaryListSeparator joins the field managers named in the one-line
	// summary. Not the payload's field paths, which travel as a JSON array and
	// are never flattened into a string on this side.
	summaryListSeparator = ","

	// perRecordInjectBudget caps what one record's escalation may spend. The
	// handler's context is the batch's shared join budget -- thirty seconds by
	// default, for up to a hundred records -- and without a sub-budget a single
	// unresponsive daemon spends most or all of it on the first survivor.
	//
	// How much depends on where it stalls, and the two are further apart than
	// they look. A daemon that answers neither call costs one defaultInjectTimeout
	// per attempt and never reaches Inject, so two attempts and injectRetryDelay
	// is 20.25s: two thirds of the batch, not all of it. The worst case is a
	// daemon that answers CreateSession just inside its timeout and then hangs
	// Inject, which pays both ceilings on both attempts and approaches 40s. Only
	// that shape exceeds the batch's whole allowance; the ordinary hang merely
	// leaves too little of it for the ninety-nine records behind.
	//
	// Either way those records then fail their lookups on a context at or near
	// expiry and are acked anyway, so one slow dependency costs the batch its
	// escalations and its joins together.
	//
	// Five seconds is a ceiling, not a target: a healthy daemon answers both
	// calls in milliseconds, and the value only decides how many consecutive
	// slow records it takes to exhaust the batch. It does not remove the
	// coupling -- per-record budgets for the whole handler are the real fix,
	// tracked separately -- it bounds the blast radius from one record to six.
	perRecordInjectBudget = 5 * time.Second
)

// pastTenseVerbs renders an audit verb as the past tense the summary sentence
// needs. The summary reads "<principal> <verb> <object> on cluster <name>", so
// the raw verb makes it an imperative -- "alice@example.com patch
// customresourcedefinitions/… on cluster prod" -- which parses as an
// instruction to the reader rather than a report of what happened. That is the
// wrong reading for the one line a human uses to decide whether to care.
//
// Keyed on the audit verb, which is the trailing component of methodName (see
// methodVerb). What bounds the set to five is the logging sink's filter alone
// (terraform/modules/drift-pubsub), an unanchored
// methodName=~"create|patch|update|delete" -- unanchored being why
// deletecollection arrives at all. The subresource filter is no help here: it
// keys on Resource.Subresource, not on the verb, and an exec arrives as
// pods.exec.create, so it would land on "create" either way.
//
// Anything not listed falls through unchanged, which is today's behaviour and
// reads no worse than it does now -- an unexpected verb means the sink filter
// changed, which is worth seeing verbatim rather than guessing a conjugation
// for.
var pastTenseVerbs = map[string]string{
	"create":           "created",
	"update":           "updated",
	"patch":            "patched",
	"delete":           "deleted",
	"deletecollection": "deleted a collection of",
}

// DriftInjectPayload is the JSON this binary posts as the inject message. Field
// names are snake_case to match the event watcher's payload, so a skill reading
// both signals does not need two naming conventions.
//
// It is a flattened DriftEvent rather than the struct itself: DriftEvent is
// shaped for the join (an error value, an outcome enum) and a payload is shaped
// for a reader that has no Go types. The two differ deliberately, so encoding
// the event directly would publish field names chosen for internal use as a
// wire contract that skills then match against.
type DriftInjectPayload struct {
	// Kind is always injectKindDrift, and is the field skills route on.
	Kind string `json:"kind"`

	// Summary is the one-line human rendering, for a chat message that should
	// say what happened without the reader parsing the fields below.
	Summary string `json:"summary"`

	// The cluster the change was made on, which after the fan-in is not
	// necessarily the cluster this process runs in.
	Cluster  string `json:"cluster"`
	Project  string `json:"project"`
	Location string `json:"location"`

	// Principal is the authenticated identity the audit log recorded -- the
	// "who". UserAgent is self-declared and unverified, so it names a tool and
	// never a person.
	Principal string `json:"principal"`
	UserAgent string `json:"user_agent,omitempty"`

	// Verb is the Kubernetes verb; MethodName is the fully qualified audit
	// method, and the only field here that carries the API group and version.
	// Both, because Resource below renders neither and a skill that has to
	// distinguish two same-named resources in different groups has nothing else
	// to read.
	Verb       string `json:"verb"`
	MethodName string `json:"method_name"`

	// Timestamp is when the change was made, not when it was injected. The two
	// differ by the sink's export lag plus however long this batch waited.
	//
	// A string rather than a time.Time so that "not recorded" can be sent as
	// empty. A zero time.Time marshals to 0001-01-01T00:00:00Z, and the card
	// renders whatever arrives here as the change's "When", so an audit entry
	// with no usable timestamp would tell the reader the change was made in
	// year one -- a fact, confidently stated, that is not one. Empty takes the
	// renderer's unknown-field path instead.
	Timestamp string `json:"timestamp"`

	// InsertID is Cloud Logging's id for the audit entry: the key this binary
	// deduplicates on, and the string to search the log with to find the entry
	// behind an inject.
	InsertID string `json:"insert_id"`

	// Resource names the object that changed.
	Resource DriftInjectResource `json:"resource"`

	// Join says whether ownership below was read, and if not, why not. Carried
	// rather than implied by an empty Owners list, because "the object has no
	// managedFields" and "this process holds no credentials for that cluster"
	// are different facts and only one of them is about the object.
	Join string `json:"join"`

	// Owners is the live object's field ownership, empty unless Join reports the
	// record was enriched.
	Owners []DriftInjectOwner `json:"owners,omitempty"`

	// Reconciled reports that a configured GitOps manager wrote to the object
	// after the audited change -- so there may be nothing left to revert. A
	// positive claim only: false means "not shown to be reconciled", which is
	// also what an unset --gitops-managers produces.
	Reconciled   bool   `json:"reconciled"`
	ReconciledBy string `json:"reconciled_by,omitempty"`

	// LookupError is the error behind a failed join, rendered. Sent so the agent
	// can say why it is reasoning without ownership rather than presenting a
	// partial picture as a complete one.
	LookupError string `json:"lookup_error,omitempty"`
}

// DriftInjectResource is the object reference on the payload.
type DriftInjectResource struct {
	Group       string `json:"group,omitempty"`
	Version     string `json:"version,omitempty"`
	Namespace   string `json:"namespace,omitempty"`
	Resource    string `json:"resource"`
	Name        string `json:"name,omitempty"`
	Subresource string `json:"subresource,omitempty"`
}

// DriftInjectOwner is one managedFields claim on the payload.
type DriftInjectOwner struct {
	Manager     string `json:"manager"`
	Operation   string `json:"operation,omitempty"`
	Subresource string `json:"subresource,omitempty"`

	// UpdatedAt is omitted when zero, which the API server is permitted to
	// leave unset -- sending the zero time would assert a write in year one.
	UpdatedAt *time.Time `json:"updated_at,omitempty"`

	// Paths are the dotted field paths this manager owns. Sent whole rather
	// than truncated the way the log line truncates them: the log is read by a
	// person scanning, and this is read by an agent deciding which fields to
	// revert.
	Paths []string `json:"paths,omitempty"`
}

// driftInjectorConfig is the daemon endpoint this binary posts to.
type driftInjectorConfig struct {
	// daemonURL is the base endpoint, without a trailing slash.
	daemonURL string

	// bearerToken authorises both calls. Required: the daemon rejects an
	// unauthenticated session create, and finding that out per-event rather
	// than at startup means discovering it only once drift arrives.
	bearerToken string

	// assertedCaller is the owner the session is attributed to. Optional; an
	// empty one omits the header rather than sending it blank.
	assertedCaller string

	// httpClient is optional, so a test can drive this without a listener.
	httpClient *http.Client
}

// driftInjector opens a session and posts one drift payload into it.
//
// Deliberately a near-copy of the event watcher's injector rather than a shared
// package: the two binaries are separate main packages and cannot import each
// other, and #1769 already tracks lifting this and the discovery scaffolding
// into one place. Lifting it here would put a refactor of the watcher's alert
// path inside a pull request about drift.
type driftInjector struct {
	cfg    driftInjectorConfig
	client *http.Client
}

// newDriftInjector validates the endpoint and returns the injector.
func newDriftInjector(cfg driftInjectorConfig) (*driftInjector, error) {
	if cfg.daemonURL == "" {
		return nil, errors.New("inject: daemonURL is required")
	}
	if strings.HasSuffix(cfg.daemonURL, "/") {
		return nil, fmt.Errorf("inject: daemonURL must not end with '/' (got %q)", cfg.daemonURL)
	}
	// Whether the URL is a URL is as much a startup-time fact as whether the
	// token variable is set, and it fails in the same shape if it is not
	// checked here: --daemon-url=localhost:8699 parses, starts, logs that
	// injects are enabled, and then fails every call on an unsupported scheme
	// for as long as the process runs. url.Parse alone is too permissive to
	// catch it -- that string parses, with "localhost" read as the scheme -- so
	// the scheme and host are checked by name.
	parsed, err := url.Parse(cfg.daemonURL)
	if err != nil {
		return nil, fmt.Errorf("inject: daemonURL is not a URL (got %q): %w", cfg.daemonURL, err)
	}
	if parsed.Scheme != schemeHTTP && parsed.Scheme != schemeHTTPS {
		return nil, fmt.Errorf("inject: daemonURL must start with %s:// or %s:// (got %q)", schemeHTTP, schemeHTTPS, cfg.daemonURL)
	}
	if parsed.Host == "" {
		return nil, fmt.Errorf("inject: daemonURL has no host (got %q)", cfg.daemonURL)
	}
	// The endpoints are composed by concatenation, not by url.ResolveReference,
	// so anything after the path is appended to rather than replaced: a base of
	// "http://host:8699?x=1" yields "http://host:8699?x=1/sessions", whose path
	// is empty and whose query is nonsense. That reaches no route on the daemon
	// and every call fails for the life of the process, which is the same
	// failure shape the scheme check above exists to catch at startup.
	if parsed.RawQuery != "" || parsed.ForceQuery || parsed.Fragment != "" {
		return nil, fmt.Errorf("inject: daemonURL must carry no query or fragment (got %q); "+
			"the endpoint paths are appended to it", cfg.daemonURL)
	}
	if cfg.bearerToken == "" {
		return nil, errors.New("inject: bearerToken is required")
	}
	client := cfg.httpClient
	if client == nil {
		client = &http.Client{Timeout: defaultInjectTimeout}
	}
	return &driftInjector{cfg: cfg, client: client}, nil
}

// createSessionResponse is the daemon's reply to a session create. Only the id
// is read; the daemon sends more.
type createSessionResponse struct {
	SessionID string `json:"sessionID"`
}

// injectMessageRequest is the envelope the daemon expects: the payload travels
// as a JSON string inside it, not as a nested object. That is the daemon's
// contract and the event watcher's payloads ride it the same way.
type injectMessageRequest struct {
	Message string `json:"message"`
}

// injectResponse is the daemon's reply to an accepted inject.
type injectResponse struct {
	Status string `json:"status"`
}

// healthzResponse is the daemon's reply to GET /healthz. InjectKinds is what a
// current daemon advertises its inject dispatch as understanding; a daemon
// predating that advertisement omits the field, which is the case
// VerifyKindSupported exists to catch, so the nil and the empty slice mean the
// same thing here and neither is distinguished from the other.
type healthzResponse struct {
	InjectKinds []string `json:"inject_kinds"`
}

// VerifyKindSupported refuses to run against a daemon that does not understand
// the kind this binary sends.
//
// The dispatch on the daemon's side is an equality test on the payload's `kind`
// and there is nothing in a reply that reveals whether it ran. Against a daemon
// predating it, a drift payload takes the event path instead, where the
// defaults grade it `Warning` and render it as a Pod alert for `default/`
// naming reason `Unknown`: it bills the event watcher's ceiling, which is the
// bucket DRIFT_QUOTA_KEY exists to keep drift out of, writes a ledger row the
// daily recap counts as a watcher event, and answers 200. Inject then reports
// success, the record is counted injected and its insertId marked seen, and
// nothing anywhere names the skew. One multi-object apply is six such alerts,
// which is the watcher's whole default budget for the day.
//
// The two halves roll independently -- the daemon script is copied to the
// shared PVC from the agent image, this binary ships in its own -- so the skew
// is an ordinary deployment window rather than a misconfiguration. The event
// watcher negotiates the mirror-image problem with X-Watcher-Features; this is
// the same trade with the roles reversed, and it is a startup check rather than
// a per-record one because the answer cannot change under a running process
// without the daemon restarting anyway.
//
// Fails closed: an unreachable daemon, an unparseable body, and a reply with no
// inject_kinds at all are all refusals. The last is the one that matters, and
// treating a missing field as permission would defeat the check, because a
// daemon old enough to mishandle the payload is exactly the one that omits it.
func (i *driftInjector) VerifyKindSupported(ctx context.Context, kind string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, i.cfg.daemonURL+healthzPath, nil)
	if err != nil {
		return fmt.Errorf("inject: build GET %s: %w", healthzPath, err)
	}

	resp, err := i.client.Do(req)
	if err != nil {
		return fmt.Errorf("inject: GET %s: %w", healthzPath, err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, errorBodyLimit))
		return &injectHTTPError{status: resp.StatusCode, body: string(body), call: "GET " + healthzPath}
	}

	var parsed healthzResponse
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return fmt.Errorf("inject: decode GET %s response: %w", healthzPath, err)
	}

	if slices.Contains(parsed.InjectKinds, kind) {
		return nil
	}

	if len(parsed.InjectKinds) == 0 {
		return fmt.Errorf("inject: the daemon at %s advertises no inject kinds, so it predates the %q dispatch: "+
			"every record sent to it would be graded as a Warning Pod event against the event watcher's ceiling "+
			"and reported back as delivered. Upgrade the agent image, or drop --daemon-url to log drift without "+
			"escalating it", i.cfg.daemonURL, kind)
	}
	return fmt.Errorf("inject: the daemon at %s does not handle %q (it advertises %v), so every record sent to it "+
		"would take its event path and be reported back as delivered. Upgrade the agent image, or drop "+
		"--daemon-url to log drift without escalating it", i.cfg.daemonURL, kind, parsed.InjectKinds)
}

// CreateSession opens a session for one drift event and returns its id.
func (i *driftInjector) CreateSession(ctx context.Context) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, i.cfg.daemonURL+sessionsPath, nil)
	if err != nil {
		return "", fmt.Errorf("inject: build POST %s: %w", sessionsPath, err)
	}
	i.authorise(req)

	resp, err := i.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("inject: POST %s: %w", sessionsPath, err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, errorBodyLimit))
		return "", &injectHTTPError{status: resp.StatusCode, body: string(body), call: "POST " + sessionsPath}
	}

	var parsed createSessionResponse
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return "", fmt.Errorf("inject: decode POST %s response: %w", sessionsPath, err)
	}
	if parsed.SessionID == "" {
		return "", fmt.Errorf("inject: POST %s returned an empty sessionID", sessionsPath)
	}
	return parsed.SessionID, nil
}

// Inject posts one payload into a session, returning the daemon's status.
//
// The status is returned alongside the error because a 2xx does not by itself
// mean anyone was told -- see injectStatusSuppressed. An empty or unparseable
// body reads as delivered: a daemon predating the field is one that always
// delivers, and guessing "dropped" would understate what this run achieved.
func (i *driftInjector) Inject(ctx context.Context, sessionID string, payload DriftInjectPayload) (string, error) {
	if sessionID == "" {
		return "", errors.New("inject: sessionID is required")
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return "", fmt.Errorf("inject: marshal payload: %w", err)
	}
	wrapped, err := json.Marshal(injectMessageRequest{Message: string(body)})
	if err != nil {
		return "", fmt.Errorf("inject: wrap inject envelope: %w", err)
	}

	url := i.cfg.daemonURL + sessionsPath + "/" + sessionID + injectPathSuffix
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(wrapped))
	if err != nil {
		return "", fmt.Errorf("inject: build POST inject: %w", err)
	}
	i.authorise(req)
	req.Header.Set(contentTypeHeader, contentTypeJSON)

	resp, err := i.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("inject: POST inject: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, errorBodyLimit))
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return "", &injectHTTPError{status: resp.StatusCode, body: string(respBody), call: "POST inject"}
	}

	var parsed injectResponse
	_ = json.Unmarshal(respBody, &parsed)
	return parsed.Status, nil
}

// authorise sets the headers both calls share.
func (i *driftInjector) authorise(req *http.Request) {
	req.Header.Set(authorizationHeader, bearerPrefix+i.cfg.bearerToken)
	if i.cfg.assertedCaller != "" {
		req.Header.Set(assertedCallerHeader, i.cfg.assertedCaller)
	}
}

// injectHTTPError is a non-2xx from the daemon. A type rather than a formatted
// string because the retry decision is made on the status code, and recovering
// an int by parsing an error message is how a 500 starts reading as a 400.
type injectHTTPError struct {
	call   string
	status int
	body   string
}

func (e *injectHTTPError) Error() string {
	return fmt.Sprintf("inject: %s: status %d: %s", e.call, e.status, e.body)
}

// retryableInjectFailure reports whether sending the same request again could
// plausibly succeed.
//
// A 5xx and a transport error qualify: the daemon fell over, or the connection
// did, and neither says the request was wrong. Everything else does not -- a
// 401 with a stale token and a 400 with a payload the daemon rejects both fail
// identically on a second attempt, and retrying them spends the batch's join
// budget to arrive at the same error.
func retryableInjectFailure(err error) bool {
	var httpErr *injectHTTPError
	if errors.As(err, &httpErr) {
		return httpErr.status >= httpStatusServerErrorFloor
	}
	// Not an HTTP status at all: a dial failure, a TLS error, a timeout. The
	// marshalling errors above reach here too and are not retryable, but they
	// cannot arise from a payload this binary builds -- every field is a string,
	// a time, or a slice of them.
	return true
}

// injectCounts tallies what became of the events this handler was given, for
// the shutdown report.
type injectCounts struct {
	// Injected is events the daemon accepted and did not report dropping.
	Injected int

	// Suppressed is events the daemon accepted and then dropped against its own
	// alert ceiling. Not a failure, and not a delivery either.
	Suppressed int

	// Duplicate is events whose insertId had already been injected by this
	// process -- a Pub/Sub redelivery, suppressed before a second session was
	// opened.
	Duplicate int

	// Failed is events whose inject did not land. They were acked regardless,
	// so this counter is the only record that they existed.
	Failed int
}

// String renders the tally, always printing every field. A zero that is absent
// reads as a category that did not apply; a printed zero reads as one that did
// not happen, and those differ.
func (c injectCounts) String() string {
	return fmt.Sprintf("injected=%d suppressed=%d duplicate=%d failed=%d",
		c.Injected, c.Suppressed, c.Duplicate, c.Failed)
}

// insertIDSet remembers the audit entries this process has already injected,
// bounded at seenInsertIDsCap and evicting oldest-first.
//
// Not safe for concurrent use, and does not need to be: processBatch calls the
// handler chain in sequence on the pull loop's one goroutine.
type insertIDSet struct {
	seen  map[string]struct{}
	order []string
	cap   int
}

// newInsertIDSet returns a set bounded at the given size.
func newInsertIDSet(capacity int) *insertIDSet {
	return &insertIDSet{seen: make(map[string]struct{}, capacity), cap: capacity}
}

// Add records an id, reporting false when it was already present.
//
// An empty id is always accepted and never remembered. It means the audit entry
// carried no insertId, which Cloud Logging does not normally produce -- and
// treating the empty string as one key would collapse every such record onto a
// single entry, so the first would inject and the rest would be discarded as
// duplicates of it. Injecting twice is the better failure of the two.
func (s *insertIDSet) Add(id string) bool {
	if id == "" {
		return true
	}
	if _, dup := s.seen[id]; dup {
		return false
	}
	if len(s.order) >= s.cap {
		oldest := s.order[0]
		s.order = s.order[1:]
		delete(s.seen, oldest)
	}
	s.seen[id] = struct{}{}
	s.order = append(s.order, id)
	return true
}

// driftInjectHandler is the terminal handler that turns an enriched record into
// a gitops-drift inject. It replaces nothing: logDriftEvent still runs first, so
// the DRIFT line an operator greps for is emitted whether or not a daemon is
// configured and whether or not the inject lands.
type driftInjectHandler struct {
	inject *driftInjector
	seen   *insertIDSet
	counts injectCounts
}

// newDriftInjectHandler wires the injector into a driftEventHandler.
func newDriftInjectHandler(inject *driftInjector) *driftInjectHandler {
	return &driftInjectHandler{inject: inject, seen: newInsertIDSet(seenInsertIDsCap)}
}

// Handle logs the event and then injects it.
//
// The log comes first and unconditionally. It is the record that the detector
// saw this change, and it has to survive a daemon that is down -- an operator
// reading a run whose injects all failed still needs the DRIFT lines to know
// what was missed, and they are the input to replaying it by hand.
//
// A failed inject is logged and counted, and the record is acked anyway: the
// handler signature returns nothing, so there is no way from here to tell
// processBatch to nack. That is a real gap and it is stated in the README
// rather than hidden -- making handlers fallible touches T1 through T3 and
// belongs in its own change.
func (h *driftInjectHandler) Handle(ctx context.Context, event DriftEvent) {
	logDriftEvent(ctx, event)

	if h.inject == nil {
		return
	}

	// Marked seen before the send, not after, so a record whose inject failed
	// is not retried on redelivery either. The alternative loses more than it
	// saves: the case it would rescue (a failed send whose batch is then
	// redelivered, reachable because Ack is best-effort and a non-graceful exit
	// leaves the batch unacked) is rarer than the case it would break, where
	// the daemon acted and only the response was lost, and a human is paged
	// twice for one change. A record lost this way is still on stdout as a
	// DRIFT line, which is what makes the trade survivable; a run whose
	// duplicate count and failure count climb together is the shape to look
	// for.
	record := event.Record
	if !h.seen.Add(record.InsertID) {
		// Logged rather than silent: a run whose duplicate count is climbing is
		// a run whose batches are being redelivered, which points at the ack
		// deadline and not at the cluster.
		h.counts.Duplicate++
		log.Printf("%s: already injected insert_id=%s (redelivered by the subscription), not opening a second session", commandName, record.InsertID)
		return
	}

	// Derived from the handler's context rather than replacing it, so a SIGTERM
	// or an exhausted batch budget still cuts the escalation short: this caps
	// what one record may take out of the batch, it does not buy it more.
	injectCtx, cancelInject := context.WithTimeout(ctx, perRecordInjectBudget)
	defer cancelInject()

	status, err := h.send(injectCtx, payloadForEvent(event))
	if err != nil {
		h.counts.Failed++
		// insert_id is named because it is what the operator searches Cloud
		// Logging with to find the change this run failed to escalate.
		log.Printf("%s: INJECT FAILED for insert_id=%s cluster=%s resource=%s; the record was acked and will not be redelivered: %v",
			commandName, record.InsertID, record.Cluster, record.Resource, err)
		return
	}

	if status == injectStatusSuppressed {
		h.counts.Suppressed++
		log.Printf("%s: inject accepted then suppressed for insert_id=%s (the daemon's alert ceiling for this signal is spent); nobody was told",
			commandName, record.InsertID)
		return
	}
	h.counts.Injected++
}

// send opens a session, posts the payload into it, and retries once on a fault
// a second attempt could clear.
//
// The session is created inside the retry rather than outside it, because the
// failure this retries on can be either call: a session created against a
// daemon that then fell over is not a session the second attempt can inject
// into.
//
// The cost is that the retry is not idempotent when it is Inject rather than
// CreateSession that failed. The first session is abandoned on the daemon, and
// the second attempt re-enters the drift route from the top -- so a 5xx raised
// after the daemon had already claimed its alert quota and written its ledger
// row spends a second unit of the day's budget and writes a second row for one
// insert_id. Accepted because the daemon does little between those steps and
// its response, and because the alternative -- reusing a session whose daemon
// may have restarted underneath it -- fails more often and less visibly.
func (h *driftInjectHandler) send(ctx context.Context, payload DriftInjectPayload) (string, error) {
	var lastErr error
	for attempt := 0; attempt <= injectRetries; attempt++ {
		if attempt > 0 {
			if !sleepCtx(ctx, injectRetryDelay) {
				return "", fmt.Errorf("%w (retrying after: %v)", ctx.Err(), lastErr)
			}
		}

		sessionID, err := h.inject.CreateSession(ctx)
		if err == nil {
			var status string
			status, err = h.inject.Inject(ctx, sessionID, payload)
			if err == nil {
				return status, nil
			}
		}

		lastErr = err
		if !retryableInjectFailure(err) || ctx.Err() != nil {
			return "", err
		}
	}
	return "", lastErr
}

// Counts reports the tally so far.
func (h *driftInjectHandler) Counts() injectCounts {
	return h.counts
}

// formatAuditTimestamp renders a change's time for the payload, sending a zero
// time as empty rather than as year one. See DriftInjectPayload.Timestamp.
func formatAuditTimestamp(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return t.UTC().Format(time.RFC3339)
}

// payloadForEvent flattens a DriftEvent into the wire payload.
func payloadForEvent(event DriftEvent) DriftInjectPayload {
	record := event.Record

	payload := DriftInjectPayload{
		Kind:       injectKindDrift,
		Summary:    driftSummary(event),
		Cluster:    record.Cluster,
		Project:    record.Project,
		Location:   record.Location,
		Principal:  record.Principal,
		UserAgent:  record.UserAgent,
		Verb:       record.Verb,
		MethodName: record.MethodName,
		Timestamp:  formatAuditTimestamp(record.Timestamp),
		InsertID:   record.InsertID,
		Resource: DriftInjectResource{
			Group:       record.Resource.Group,
			Version:     record.Resource.Version,
			Namespace:   record.Resource.Namespace,
			Resource:    record.Resource.Resource,
			Name:        record.Resource.Name,
			Subresource: record.Resource.Subresource,
		},
		Join:         string(event.Outcome),
		Reconciled:   event.Reconciled,
		ReconciledBy: event.ReconciledBy,
	}

	if event.LookupError != nil {
		payload.LookupError = event.LookupError.Error()
	}

	for _, owner := range event.Owners {
		claim := DriftInjectOwner{
			Manager:     owner.Manager,
			Operation:   owner.Operation,
			Subresource: owner.Subresource,
			Paths:       owner.Paths,
		}
		// Sent only when the API server recorded one. The field is a pointer
		// upstream and legitimately absent, and a zero time on the wire would
		// read as a write in year one rather than as "not recorded".
		if !owner.UpdatedAt.IsZero() {
			updatedAt := owner.UpdatedAt
			claim.UpdatedAt = &updatedAt
		}
		payload.Owners = append(payload.Owners, claim)
	}

	return payload
}

// driftSummary is the one-line rendering that goes in the chat message.
//
// It leads with the principal and the object because that is the sentence a
// human needs to decide whether to care, and appends the ownership only when
// the join actually read it. A summary that claimed ownership the join never
// looked up would be the most-read field in the payload and wrong.
func driftSummary(event DriftEvent) string {
	record := event.Record

	summary := fmt.Sprintf("%s %s %s on cluster %s",
		record.Principal, pastTenseVerb(record.Verb), record.Resource, record.Cluster)

	if event.Outcome != joinEnriched {
		return fmt.Sprintf("%s (field ownership not read: %s)", summary, event.Outcome)
	}
	if event.Reconciled {
		return fmt.Sprintf("%s, since reconciled by %s", summary, event.ReconciledBy)
	}
	if managers := ownerManagers(event.Owners); managers != "" {
		return fmt.Sprintf("%s, fields owned by %s", summary, managers)
	}
	return summary
}

// pastTenseVerb conjugates one audit verb for the summary, leaving anything
// pastTenseVerbs does not name exactly as it arrived.
func pastTenseVerb(verb string) string {
	if past, ok := pastTenseVerbs[verb]; ok {
		return past
	}
	return verb
}

// ownerManagers lists the managers holding fields on the object, for the
// summary line. Names only: the summary is a sentence, and the paths behind
// each name are in the payload for whatever reads it next.
func ownerManagers(owners []fieldOwner) string {
	if len(owners) == 0 {
		return ""
	}
	names := make([]string, 0, len(owners))
	for _, owner := range owners {
		names = append(names, owner.Manager)
	}
	return strings.Join(names, summaryListSeparator+" ")
}

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
	"strings"
	"time"
)

const (
	// injectKindDrift is stamped on every payload this binary sends, and is the
	// string a playbook skill matches to tell a drift inject from the event
	// watcher's. Fixed by docs/designs/drift-detection.md and by the AutoOps
	// architecture's domain table, both of which name it in prose -- changing it
	// here alone would leave the pipeline receiving a kind nothing routes.
	injectKindDrift = "gitops-drift"

	// sessionsPath and injectPathSuffix compose the two daemon endpoints. The
	// inject URL is sessionsPath + "/" + id + injectPathSuffix, which is why the
	// suffix carries its own leading slash and the id does not.
	sessionsPath     = "/sessions"
	injectPathSuffix = "/inject"

	// The headers the daemon reads. Authorization carries the same bearer token
	// the event watcher uses; X-Asserted-Caller is how the daemon attributes the
	// session to an owner, and an empty one is omitted rather than sent blank.
	authorizationHeader  = "Authorization"
	bearerPrefix         = "Bearer "
	assertedCallerHeader = "X-Asserted-Caller"
	contentTypeHeader    = "Content-Type"
	contentTypeJSON      = "application/json"

	// defaultInjectTimeout bounds one HTTP call to the daemon. Matches the event
	// watcher's, and is per-call rather than per-event: a drift inject is two
	// calls, so the worst case an event contributes is twice this.
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

	// httpStatusServerErrorFloor is the first 5xx. At or above it the daemon is
	// reporting its own failure rather than rejecting the request, which is the
	// only class of status worth sending again.
	httpStatusServerErrorFloor = 500

	// ownerPathSeparator joins the field paths of one ownership claim in the
	// payload's rendered path list.
	ownerPathSeparator = ","
)

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
	Timestamp time.Time `json:"timestamp"`

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

	record := event.Record
	if !h.seen.Add(record.InsertID) {
		// Logged rather than silent: a run whose duplicate count is climbing is
		// a run whose batches are being redelivered, which points at the ack
		// deadline and not at the cluster.
		h.counts.Duplicate++
		log.Printf("%s: already injected insert_id=%s (redelivered by the subscription), not opening a second session", commandName, record.InsertID)
		return
	}

	status, err := h.send(ctx, payloadForEvent(event))
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
		Timestamp:  record.Timestamp,
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
		record.Principal, record.Verb, record.Resource, record.Cluster)

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
	return strings.Join(names, ownerPathSeparator+" ")
}

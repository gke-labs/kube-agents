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
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeDaemon stands in for the core-agent daemon: it records what arrived and
// answers whatever the test told it to.
type fakeDaemon struct {
	mu sync.Mutex

	// sessionStatus and injectStatus are the HTTP codes to answer with. Zero
	// means the success code for that endpoint.
	sessionStatus int
	injectStatus  int

	// injectBody is the JSON body answered to a successful inject.
	injectBody string

	// sessionCalls and injectCalls count the two endpoints. The session count is
	// what proves a duplicate was suppressed before any work was done, rather
	// than after a session had already been opened.
	sessionCalls int
	injectCalls  int

	// payloads holds the decoded inject payloads, in arrival order.
	payloads []DriftInjectPayload

	// headers holds the headers of every request, in arrival order.
	headers []http.Header
}

// newFakeDaemon starts a server and returns it with its base URL.
func newFakeDaemon(t *testing.T) (*fakeDaemon, string) {
	t.Helper()
	d := &fakeDaemon{}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		d.mu.Lock()
		defer d.mu.Unlock()
		d.headers = append(d.headers, r.Header.Clone())

		if strings.HasSuffix(r.URL.Path, injectPathSuffix) {
			d.injectCalls++
			if d.injectStatus != 0 && d.injectStatus != http.StatusOK {
				w.WriteHeader(d.injectStatus)
				_, _ = w.Write([]byte("the daemon said no"))
				return
			}
			var envelope injectMessageRequest
			if err := json.NewDecoder(r.Body).Decode(&envelope); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			// The payload travels as a JSON string inside the envelope, so this
			// second decode is the contract, not a convenience: a payload nested
			// as an object would decode at the first step and fail here.
			var payload DriftInjectPayload
			if err := json.Unmarshal([]byte(envelope.Message), &payload); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			d.payloads = append(d.payloads, payload)
			w.Header().Set(contentTypeHeader, contentTypeJSON)
			body := d.injectBody
			if body == "" {
				body = `{"status":"delivered"}`
			}
			_, _ = w.Write([]byte(body))
			return
		}

		d.sessionCalls++
		if d.sessionStatus != 0 && d.sessionStatus != http.StatusCreated {
			w.WriteHeader(d.sessionStatus)
			_, _ = w.Write([]byte("no session for you"))
			return
		}
		w.Header().Set(contentTypeHeader, contentTypeJSON)
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(fmt.Sprintf(`{"sessionID":"sid-%d"}`, d.sessionCalls)))
	}))
	t.Cleanup(srv.Close)

	return d, srv.URL
}

// counts reads the call counters under the lock.
func (d *fakeDaemon) counts() (sessions, injects int) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.sessionCalls, d.injectCalls
}

// handlerAgainst wires a handler to a fake daemon.
func handlerAgainst(t *testing.T, daemonURL string) *driftInjectHandler {
	t.Helper()
	inject, err := newDriftInjector(driftInjectorConfig{
		daemonURL:      daemonURL,
		bearerToken:    "token-value",
		assertedCaller: "drift-detector@example",
	})
	if err != nil {
		t.Fatalf("newDriftInjector returned error: %v", err)
	}
	return newDriftInjectHandler(inject)
}

// driftEvent builds an enriched event to inject.
func driftEvent(insertID string) DriftEvent {
	return DriftEvent{
		Record: AuditRecord{
			Cluster:    "prod-a",
			Project:    "example-project",
			Location:   "us-central1",
			Principal:  "ada@corp.example",
			UserAgent:  "kubectl/v1.31.0",
			Verb:       "patch",
			MethodName: "io.k8s.apps.v1.deployments.patch",
			Timestamp:  time.Date(2026, 9, 21, 10, 30, 0, 0, time.UTC),
			InsertID:   insertID,
			Resource: ResourceRef{
				Group:     "apps",
				Version:   "v1",
				Namespace: "shop",
				Resource:  "deployments",
				Name:      "checkout",
			},
			StatusCode: statusCodeOK,
		},
		Outcome: joinEnriched,
		Owners: []fieldOwner{{
			Manager:   "kubectl-edit",
			Operation: "Update",
			UpdatedAt: time.Date(2026, 9, 21, 10, 30, 1, 0, time.UTC),
			Paths:     []string{"spec.replicas"},
		}},
	}
}

// The wire contract a skill matches on. kind is what routes the payload, and
// the payload rides inside the envelope as a JSON string rather than as a
// nested object -- the fake decodes it in two steps for that reason, so a
// change to either half fails here rather than at runtime against a daemon.
func TestInjectSendsAGitopsDriftPayload(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	handler := handlerAgainst(t, url)

	handler.Handle(context.Background(), driftEvent("insert-1"))

	sessions, injects := daemon.counts()
	if sessions != 1 || injects != 1 {
		t.Fatalf("sessions=%d injects=%d, want 1 and 1", sessions, injects)
	}
	if len(daemon.payloads) != 1 {
		t.Fatalf("payloads = %d, want 1", len(daemon.payloads))
	}

	got := daemon.payloads[0]
	if got.Kind != injectKindDrift {
		t.Errorf("Kind = %q, want %q -- nothing downstream routes a payload whose kind it does not know", got.Kind, injectKindDrift)
	}
	if got.Cluster != "prod-a" || got.Project != "example-project" || got.Location != "us-central1" {
		t.Errorf("cluster triple = %q/%q/%q, want prod-a/example-project/us-central1", got.Cluster, got.Project, got.Location)
	}
	if got.Principal != "ada@corp.example" {
		t.Errorf("Principal = %q, want ada@corp.example -- the whole point of the audit half of the join", got.Principal)
	}
	if got.InsertID != "insert-1" {
		t.Errorf("InsertID = %q, want insert-1", got.InsertID)
	}
	if got.Resource.Namespace != "shop" || got.Resource.Resource != "deployments" || got.Resource.Name != "checkout" {
		t.Errorf("Resource = %+v, want shop/deployments/checkout", got.Resource)
	}
	// Group and version are carried only here: Resource.String() renders
	// neither, so without them two same-named resources in different groups are
	// the same payload.
	if got.Resource.Group != "apps" || got.Resource.Version != "v1" {
		t.Errorf("Resource group/version = %q/%q, want apps/v1", got.Resource.Group, got.Resource.Version)
	}
	if got.Join != string(joinEnriched) {
		t.Errorf("Join = %q, want %q", got.Join, joinEnriched)
	}
	if len(got.Owners) != 1 || got.Owners[0].Manager != "kubectl-edit" {
		t.Errorf("Owners = %+v, want one claim by kubectl-edit", got.Owners)
	}
	if len(got.Owners) == 1 && (len(got.Owners[0].Paths) != 1 || got.Owners[0].Paths[0] != "spec.replicas") {
		t.Errorf("Owners[0].Paths = %v, want [spec.replicas] -- the field list is what an agent reverts from", got.Owners[0].Paths)
	}
}

// Both calls have to be authorised and attributed. The bearer token is what the
// daemon actually enforces; the asserted caller is a header rather than a
// payload field so that it is readable before a body is parsed, which is what
// would let the daemon start checking it without either side changing shape.
func TestInjectAuthorisesBothCalls(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	handler := handlerAgainst(t, url)

	handler.Handle(context.Background(), driftEvent("insert-1"))

	if len(daemon.headers) != 2 {
		t.Fatalf("saw %d requests, want 2 (a session create and an inject)", len(daemon.headers))
	}
	for i, h := range daemon.headers {
		if got := h.Get(authorizationHeader); got != bearerPrefix+"token-value" {
			t.Errorf("request %d %s = %q, want the bearer token", i, authorizationHeader, got)
		}
		if got := h.Get(assertedCallerHeader); got != "drift-detector@example" {
			t.Errorf("request %d %s = %q, want drift-detector@example", i, assertedCallerHeader, got)
		}
	}
}

// Pub/Sub delivers at least once and processBatch acks after the handler
// returns, so the same insertId arriving twice is ordinary. Without
// suppression it is two sessions and two pages for one change -- and the count
// that matters is sessions, because suppressing after the session was opened
// would already have created the incident.
func TestInjectSuppressesARedeliveredRecord(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	handler := handlerAgainst(t, url)

	handler.Handle(context.Background(), driftEvent("insert-1"))
	handler.Handle(context.Background(), driftEvent("insert-1"))

	sessions, injects := daemon.counts()
	if sessions != 1 {
		t.Errorf("sessions = %d, want 1 -- a redelivered record opened a second session", sessions)
	}
	if injects != 1 {
		t.Errorf("injects = %d, want 1 -- a redelivered record was injected twice", injects)
	}
	if got := handler.Counts().Duplicate; got != 1 {
		t.Errorf("Duplicate = %d, want 1", got)
	}
	if got := handler.Counts().Injected; got != 1 {
		t.Errorf("Injected = %d, want 1", got)
	}
}

// Two different changes are two injects. The guard above must key on the id
// and not on anything the two records share, which every other field here
// does.
func TestInjectDoesNotSuppressADistinctRecord(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	handler := handlerAgainst(t, url)

	handler.Handle(context.Background(), driftEvent("insert-1"))
	handler.Handle(context.Background(), driftEvent("insert-2"))

	sessions, injects := daemon.counts()
	if sessions != 2 || injects != 2 {
		t.Errorf("sessions=%d injects=%d, want 2 and 2 -- two distinct changes were collapsed into one", sessions, injects)
	}
	if got := handler.Counts().Duplicate; got != 0 {
		t.Errorf("Duplicate = %d, want 0", got)
	}
}

// An entry with no insertId is injected every time rather than being collapsed
// onto a single empty key, which would inject the first and silently discard
// every later one as its duplicate.
func TestInjectDoesNotTreatAMissingInsertIDAsOneKey(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	handler := handlerAgainst(t, url)

	handler.Handle(context.Background(), driftEvent(""))
	handler.Handle(context.Background(), driftEvent(""))

	if sessions, _ := daemon.counts(); sessions != 2 {
		t.Errorf("sessions = %d, want 2 -- records with no insertId were deduplicated against each other", sessions)
	}
	if got := handler.Counts().Duplicate; got != 0 {
		t.Errorf("Duplicate = %d, want 0 -- an absent id is not evidence of a redelivery", got)
	}
}

// A 200 does not mean anyone was told. The daemon answers "suppressed" when
// the day's ceiling for this signal is spent, and a run whose injects all
// suppressed is indistinguishable from one that alerted every time unless this
// is counted apart.
func TestInjectCountsASuppressedAcceptanceSeparately(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	daemon.injectBody = `{"status":"` + injectStatusSuppressed + `"}`
	handler := handlerAgainst(t, url)

	logs := captureLog(t)
	handler.Handle(context.Background(), driftEvent("insert-1"))

	counts := handler.Counts()
	if counts.Suppressed != 1 {
		t.Errorf("Suppressed = %d, want 1", counts.Suppressed)
	}
	if counts.Injected != 0 {
		t.Errorf("Injected = %d, want 0 -- a suppressed inject was counted as a delivery", counts.Injected)
	}
	if !strings.Contains(logs.String(), "nobody was told") {
		t.Errorf("the suppressed inject was not reported in the log:\n%s", logs.String())
	}
}

// A body the daemon did not fill in reads as delivered. A daemon predating the
// status field always delivers, and defaulting to "dropped" would understate
// every inject such a daemon accepted.
func TestInjectReadsAnEmptyStatusAsDelivered(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	daemon.injectBody = `{}`
	handler := handlerAgainst(t, url)

	handler.Handle(context.Background(), driftEvent("insert-1"))

	if got := handler.Counts().Injected; got != 1 {
		t.Errorf("Injected = %d, want 1", got)
	}
	if got := handler.Counts().Suppressed; got != 0 {
		t.Errorf("Suppressed = %d, want 0", got)
	}
}

// A 5xx is the daemon reporting its own failure, so the same request is worth
// sending again. One extra attempt, not a ladder: the handler spends the
// batch's shared join budget.
func TestInjectRetriesAServerError(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	daemon.sessionStatus = http.StatusServiceUnavailable
	handler := handlerAgainst(t, url)

	handler.Handle(context.Background(), driftEvent("insert-1"))

	sessions, _ := daemon.counts()
	if sessions != injectRetries+1 {
		t.Errorf("session attempts = %d, want %d (the first plus %d retry)", sessions, injectRetries+1, injectRetries)
	}
	if got := handler.Counts().Failed; got != 1 {
		t.Errorf("Failed = %d, want 1", got)
	}
}

// A 4xx fails the same way twice. Retrying a rejected request spends the
// batch's budget to arrive at the error it already had.
func TestInjectDoesNotRetryARejection(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	daemon.sessionStatus = http.StatusUnauthorized
	handler := handlerAgainst(t, url)

	handler.Handle(context.Background(), driftEvent("insert-1"))

	if sessions, _ := daemon.counts(); sessions != 1 {
		t.Errorf("session attempts = %d, want 1 -- a 401 was retried", sessions)
	}
	if got := handler.Counts().Failed; got != 1 {
		t.Errorf("Failed = %d, want 1", got)
	}
}

// The failure has to be loud and name the insertId, because the record is
// acked either way: this log line is the only remaining trace of a change that
// was detected and never escalated.
func TestInjectReportsAFailureWithTheInsertID(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	daemon.injectStatus = http.StatusBadRequest
	handler := handlerAgainst(t, url)

	logs := captureLog(t)
	handler.Handle(context.Background(), driftEvent("insert-1"))

	out := logs.String()
	if !strings.Contains(out, "INJECT FAILED") {
		t.Errorf("a failed inject was not reported as a failure:\n%s", out)
	}
	if !strings.Contains(out, "insert-1") {
		t.Errorf("the failure did not name the insert_id, which is what the change is found by:\n%s", out)
	}
	if !strings.Contains(out, "will not be redelivered") {
		t.Errorf("the failure did not say the record was acked anyway:\n%s", out)
	}
}

// The DRIFT line is emitted whatever the inject does. An operator reading a run
// whose daemon was down still needs the record of what was seen -- it is the
// input to replaying the escalation by hand.
func TestInjectStillLogsTheDriftLineWhenTheDaemonFails(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	daemon.sessionStatus = http.StatusInternalServerError
	handler := handlerAgainst(t, url)

	logs := captureLog(t)
	handler.Handle(context.Background(), driftEvent("insert-1"))

	if !strings.Contains(logs.String(), "DRIFT cluster=prod-a") {
		t.Errorf("the DRIFT line was lost when the inject failed:\n%s", logs.String())
	}
}

// No --daemon-url is a supported mode and the default, not a misconfiguration:
// the detector logs and escalates nothing. A nil injector must therefore be
// handled rather than dereferenced.
func TestInjectWithNoDaemonLogsAndDoesNotPanic(t *testing.T) {
	handler := newDriftInjectHandler(nil)

	logs := captureLog(t)
	handler.Handle(context.Background(), driftEvent("insert-1"))

	if !strings.Contains(logs.String(), "DRIFT cluster=prod-a") {
		t.Errorf("the DRIFT line was not emitted with the inject off:\n%s", logs.String())
	}
	if got := handler.Counts(); got != (injectCounts{}) {
		t.Errorf("Counts() = %+v, want every field zero with no daemon configured", got)
	}
}

// The unenriched outcomes carry no ownership, and the payload has to say which
// one it was rather than leaving an empty list to be read as "the object has no
// managers". "No credentials for that cluster" and "the object has no
// managedFields" are different facts and only one is about the object.
func TestInjectCarriesTheJoinOutcomeWhenOwnershipWasNotRead(t *testing.T) {
	for _, outcome := range []joinOutcome{joinUnreachable, joinNoObject, joinGone, joinFailed} {
		t.Run(string(outcome), func(t *testing.T) {
			daemon, url := newFakeDaemon(t)
			handler := handlerAgainst(t, url)

			event := driftEvent("insert-1")
			event.Outcome = outcome
			event.Owners = nil
			handler.Handle(context.Background(), event)

			if len(daemon.payloads) != 1 {
				t.Fatalf("payloads = %d, want 1", len(daemon.payloads))
			}
			got := daemon.payloads[0]
			if got.Join != string(outcome) {
				t.Errorf("Join = %q, want %q", got.Join, outcome)
			}
			if len(got.Owners) != 0 {
				t.Errorf("Owners = %+v, want none", got.Owners)
			}
			// The summary is the field a human reads, so it is the one that
			// must not imply ownership the join never looked up.
			if !strings.Contains(got.Summary, string(outcome)) {
				t.Errorf("Summary = %q, want it to say ownership was not read (%s)", got.Summary, outcome)
			}
		})
	}
}

// A reconcile means there may be nothing left to revert, which changes what the
// agent should propose. It has to survive into the payload and into the line a
// human reads.
func TestInjectCarriesAReconcileClaim(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	handler := handlerAgainst(t, url)

	event := driftEvent("insert-1")
	event.Reconciled = true
	event.ReconciledBy = "argocd-controller"
	handler.Handle(context.Background(), event)

	got := daemon.payloads[0]
	if !got.Reconciled || got.ReconciledBy != "argocd-controller" {
		t.Errorf("Reconciled=%t by %q, want true by argocd-controller", got.Reconciled, got.ReconciledBy)
	}
	if !strings.Contains(got.Summary, "argocd-controller") {
		t.Errorf("Summary = %q, want it to name the reconciling manager", got.Summary)
	}
}

// The lookup error behind a failed join is sent so the agent can say it is
// reasoning without ownership, rather than presenting a partial picture as a
// complete one.
func TestInjectCarriesTheLookupError(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	handler := handlerAgainst(t, url)

	event := driftEvent("insert-1")
	event.Outcome = joinFailed
	event.Owners = nil
	event.LookupError = errors.New("deployments.apps is forbidden")
	handler.Handle(context.Background(), event)

	if got := daemon.payloads[0].LookupError; got != "deployments.apps is forbidden" {
		t.Errorf("LookupError = %q, want the error text", got)
	}
}

// The API server is permitted to record no update time -- the field is a
// pointer upstream. Sending the zero value would assert a write in year one, so
// it is omitted from the JSON entirely.
func TestInjectOmitsAnUnrecordedOwnerTimestamp(t *testing.T) {
	event := driftEvent("insert-1")
	event.Owners[0].UpdatedAt = time.Time{}

	encoded, err := json.Marshal(payloadForEvent(event))
	if err != nil {
		t.Fatalf("marshal returned error: %v", err)
	}
	if strings.Contains(string(encoded), "updated_at") {
		t.Errorf("an unrecorded owner timestamp was sent anyway:\n%s", encoded)
	}

	// And the other side of it: a recorded one is sent, so the omission above
	// is not just an always-absent field.
	withTime, err := json.Marshal(payloadForEvent(driftEvent("insert-2")))
	if err != nil {
		t.Fatalf("marshal returned error: %v", err)
	}
	if !strings.Contains(string(withTime), "updated_at") {
		t.Errorf("a recorded owner timestamp was not sent:\n%s", withTime)
	}
}

// The set is bounded, so a long-running detector does not grow one map entry
// per audit record for the life of the pod. Eviction is oldest-first, which
// means the id evicted is the one least likely to be redelivered.
func TestInsertIDSetEvictsOldestFirst(t *testing.T) {
	set := newInsertIDSet(2)

	for _, id := range []string{"a", "b"} {
		if !set.Add(id) {
			t.Fatalf("Add(%q) reported a duplicate on first sight", id)
		}
	}
	if set.Add("b") {
		t.Error("Add(\"b\") did not report the duplicate it had just been given")
	}
	// "c" evicts "a", the oldest.
	if !set.Add("c") {
		t.Error("Add(\"c\") reported a duplicate for an id never seen")
	}
	if !set.Add("a") {
		t.Error("\"a\" was still remembered after being evicted past the cap")
	}
	if len(set.seen) != len(set.order) {
		t.Errorf("seen has %d entries and order has %d; eviction left the two out of step", len(set.seen), len(set.order))
	}
	if len(set.seen) > 2 {
		t.Errorf("seen grew to %d entries past a cap of 2", len(set.seen))
	}
}

// The endpoint is validated at startup rather than on the first drift. A
// trailing slash produces a double slash in every URL built from it, and an
// empty token fails every call -- both are configuration errors that should
// surface at launch.
func TestNewDriftInjectorRejectsABadEndpoint(t *testing.T) {
	for _, tc := range []struct {
		name string
		cfg  driftInjectorConfig
	}{
		{"no URL", driftInjectorConfig{bearerToken: "t"}},
		{"a trailing slash", driftInjectorConfig{daemonURL: "http://daemon:8699/", bearerToken: "t"}},
		{"no token", driftInjectorConfig{daemonURL: "http://daemon:8699"}},
		// The scheme cases are the ones that would otherwise start cleanly and
		// fail every call for the life of the process. url.Parse accepts
		// "daemon:8699" with "daemon" as the scheme, so parsing alone does not
		// catch it.
		{"no scheme", driftInjectorConfig{daemonURL: "daemon:8699", bearerToken: "t"}},
		{"a host and port with no scheme at all", driftInjectorConfig{daemonURL: "127.0.0.1:8699", bearerToken: "t"}},
		{"a scheme this client cannot speak", driftInjectorConfig{daemonURL: "ftp://daemon:8699", bearerToken: "t"}},
		{"a scheme but no host", driftInjectorConfig{daemonURL: "http://", bearerToken: "t"}},
		{"not a URL", driftInjectorConfig{daemonURL: "http://[", bearerToken: "t"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := newDriftInjector(tc.cfg); err == nil {
				t.Error("newDriftInjector accepted a configuration that cannot work")
			}
		})
	}
}

// A session create that answers 201 with no id is a daemon this binary cannot
// inject into, and injecting against an empty id would post to /sessions//inject
// -- a different endpoint that may well answer 200.
func TestCreateSessionRejectsAnEmptySessionID(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"sessionID":""}`))
	}))
	t.Cleanup(srv.Close)

	inject, err := newDriftInjector(driftInjectorConfig{daemonURL: srv.URL, bearerToken: "t"})
	if err != nil {
		t.Fatalf("newDriftInjector returned error: %v", err)
	}
	if _, err := inject.CreateSession(context.Background()); err == nil {
		t.Error("CreateSession accepted a response carrying no session id")
	}
}

// The retry decision is made on the status code, so the code has to survive
// being wrapped as an error. Recovering it by parsing a message is how a 500
// starts reading as a 400.
func TestRetryableInjectFailure(t *testing.T) {
	for _, tc := range []struct {
		name string
		err  error
		want bool
	}{
		{"a 503", &injectHTTPError{status: http.StatusServiceUnavailable}, true},
		{"a 500", &injectHTTPError{status: http.StatusInternalServerError}, true},
		{"a 401", &injectHTTPError{status: http.StatusUnauthorized}, false},
		{"a 400", &injectHTTPError{status: http.StatusBadRequest}, false},
		{"a 404", &injectHTTPError{status: http.StatusNotFound}, false},
		{"a wrapped 503", fmt.Errorf("sending: %w", &injectHTTPError{status: http.StatusServiceUnavailable}), true},
		{"a wrapped 400", fmt.Errorf("sending: %w", &injectHTTPError{status: http.StatusBadRequest}), false},
		{"a transport failure", errors.New("dial tcp: connection refused"), true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := retryableInjectFailure(tc.err); got != tc.want {
				t.Errorf("retryableInjectFailure(%v) = %t, want %t", tc.err, got, tc.want)
			}
		})
	}
}

// --token-env names a variable rather than carrying the token, so the token
// must not appear in argv. This pins the flag's shape: a --token flag would
// satisfy every other test here and put the secret in the process table.
func TestTokenIsNotAFlagValue(t *testing.T) {
	f, err := parseFlags([]string{
		"--project", "example-project",
		"--daemon-url", "http://daemon:8699",
		"--token-env", "DRIFT_DAEMON_TOKEN",
		"--owner", "drift-detector@example",
	})
	if err != nil {
		t.Fatalf("parseFlags returned error: %v", err)
	}
	if f.tokenEnv != "DRIFT_DAEMON_TOKEN" {
		t.Errorf("tokenEnv = %q, want DRIFT_DAEMON_TOKEN", f.tokenEnv)
	}
	if f.daemonURL != "http://daemon:8699" || f.owner != "drift-detector@example" {
		t.Errorf("daemonURL=%q owner=%q, want the flag values", f.daemonURL, f.owner)
	}
}

// The inject's flags are checked against each other at startup, because each of
// these misconfigurations otherwise surfaces only when the first drift arrives
// -- after the run has spent an hour looking correct.
func TestRealMainValidatesTheInjectFlags(t *testing.T) {
	for _, tc := range []struct {
		name string
		argv []string
		want string
	}{
		{
			"a token env with nowhere to send",
			[]string{"--project", "example-project", "--token-env", "DRIFT_DAEMON_TOKEN"},
			"--token-env",
		},
		{
			"an owner with nowhere to send",
			[]string{"--project", "example-project", "--owner", "drift-detector@example"},
			"--owner",
		},
		{
			"a daemon with no token",
			[]string{"--project", "example-project", "--daemon-url", "http://daemon:8699"},
			"--token-env",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := realMain(tc.argv)
			if err == nil {
				t.Fatal("realMain accepted a configuration that cannot inject")
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("error = %q, want it to name %s", err, tc.want)
			}
		})
	}
}

// An unset token variable is a startup error naming the variable. The failure
// is otherwise a 401 per drift event, which is both late and harder to read
// back to the deployment that forgot to set it.
func TestRealMainRejectsAnEmptyTokenVariable(t *testing.T) {
	t.Setenv("DRIFT_DAEMON_TOKEN", "")

	err := realMain([]string{
		"--project", "example-project",
		"--daemon-url", "http://daemon:8699",
		"--token-env", "DRIFT_DAEMON_TOKEN",
	})
	if err == nil {
		t.Fatal("realMain accepted an empty bearer token")
	}
	if !strings.Contains(err.Error(), "DRIFT_DAEMON_TOKEN") {
		t.Errorf("error = %q, want it to name the variable that is unset", err)
	}
}

// One unresponsive daemon must not cost the whole batch. Every record in a
// batch shares one join budget, and before the per-record sub-budget a single
// hung inject could spend all of it: two calls at defaultInjectTimeout each,
// twice over for the retry, against a thirty-second default. The records behind
// it would then fail their lookups on an expired context and be acked anyway.
//
// The assertion is on the shared context surviving, not on the wall clock: what
// broke was that the batch had nothing left, and what fixes it is that the
// handler returns with budget to spare.
func TestOneHungInjectDoesNotSpendTheWholeBatchBudget(t *testing.T) {
	blocked := make(chan struct{})
	t.Cleanup(func() { close(blocked) })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-blocked:
		case <-r.Context().Done():
		}
	}))
	t.Cleanup(srv.Close)

	inject, err := newDriftInjector(driftInjectorConfig{daemonURL: srv.URL, bearerToken: "t"})
	if err != nil {
		t.Fatalf("newDriftInjector returned error: %v", err)
	}
	handler := newDriftInjectHandler(inject)

	// Deliberately shorter than the 30s default, and the choice is what makes
	// this test a guard rather than a description. Without the sub-context the
	// escalation costs two client timeouts plus the retry delay -- 10s + 250ms
	// + 10s -- so against a 30s budget it finishes at ~20.25s, every assertion
	// below passes, and the test is green with the fix reverted. It was, when
	// this was first written. At 12s the pre-fix path instead runs the batch
	// context out during its second attempt and trips the first assertion,
	// while the fixed path returns at perRecordInjectBudget and passes with
	// room to spare.
	batchBudget := 12 * time.Second
	batchCtx, cancel := context.WithTimeout(context.Background(), batchBudget)
	defer cancel()

	start := time.Now()
	handler.Handle(batchCtx, DriftEvent{Record: AuditRecord{InsertID: "hung-1"}})
	spent := time.Since(start)

	if batchCtx.Err() != nil {
		t.Fatalf("one hung inject exhausted the batch's shared budget after %s; "+
			"every later record in the batch would fail its lookup and be acked", spent)
	}
	// The positive form of the same property, and the one that names the cap:
	// the whole escalation including its retry fits inside one per-record
	// budget, rather than one budget per attempt. Doubled to absorb scheduling
	// on a loaded CI machine without admitting a second full attempt.
	if spent >= 2*perRecordInjectBudget {
		t.Fatalf("one record spent %s on its escalation; perRecordInjectBudget is %s, so the "+
			"per-record cap is not bounding the retry", spent, perRecordInjectBudget)
	}
	if handler.Counts().Failed != 1 {
		t.Errorf("failed count = %d, want 1: a timed-out inject is a failure, not a silent drop",
			handler.Counts().Failed)
	}
}

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
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestDispatcherDispatch_NewIncidentAndFollowUp(t *testing.T) {
	sessionID := "active-session-123"
	var createCount, injectCount int
	var lastInjectPayload InjectPayload

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST request, got %s", r.Method)
		}
		if r.URL.Path == "/sessions" {
			createCount++
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: sessionID})
			return
		}
		if r.URL.Path == "/sessions/"+sessionID+"/inject" {
			injectCount++
			var req injectMessageRequest
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				t.Fatalf("failed to decode body: %v", err)
			}
			if err := json.Unmarshal([]byte(req.Message), &lastInjectPayload); err != nil {
				t.Fatalf("failed to unmarshal message payload: %v", err)
			}
			w.WriteHeader(http.StatusOK)
			return
		}
		t.Errorf("unexpected endpoint %s", r.URL.Path)
	}))
	defer server.Close()

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("failed to build injector: %v", err)
	}

	filter := newFilter(newFilterConfig(nil, nil, nil, filterThresholds{}))
	dedup, err := newDedupCache(5*time.Minute, "")
	if err != nil {
		t.Fatalf("failed to build cache: %v", err)
	}

	m := newMetrics()

	disp := &dispatcher{
		filter:   filter,
		dedup:    dedup,
		injector: inj,
		metrics:  m,
		mode:     "per-incident",
		dryRun:   false,
	}

	ev := TriageEvent{
		Key:       EventKey{UID: "pod-1", Reason: "CrashLoopBackOff"},
		Cluster:   "test-cluster",
		Namespace: "default",
		Name:      "billing-service",
		LastSeen:  time.Now(),
		Message:   "back-off restarting failed container",
		// At the crash-loop debounce threshold, so this exercises dispatch and
		// dedup rather than the leading-edge gate. Stated explicitly: leaving it
		// zero would also pass, but only via belowMinCount's fail-open branch,
		// which is a different behaviour than "a real crash loop got through".
		Count: 3,
	}

	// 1. Dispatch first event -> should create session and inject first event
	disp.Dispatch(context.Background(), ev)
	if createCount != 1 {
		t.Errorf("expected 1 session creation, got %d", createCount)
	}
	if injectCount != 1 {
		t.Errorf("expected 1 injection, got %d", injectCount)
	}
	if lastInjectPayload.Kind != injectKindEvent {
		t.Errorf("expected first event kind to be %q, got %q", injectKindEvent, lastInjectPayload.Kind)
	}
	if lastInjectPayload.Cluster != "test-cluster" {
		t.Errorf("expected payload Cluster to come from ev.Cluster (%q), got %q", "test-cluster", lastInjectPayload.Cluster)
	}

	// 2. Dispatch same event again -> should not create session and should suppress injection
	disp.Dispatch(context.Background(), ev)
	if createCount != 1 {
		t.Errorf("expected session creation count to remain 1, got %d", createCount)
	}
	if injectCount != 1 {
		t.Errorf("expected injections count to remain 1, got %d", injectCount)
	}
}

// TestDispatcherRollsBackDedupOnDeliveryFailure pins the invariant that makes a
// long dedup window safe: an entry must not outlive an alert that was never
// delivered. Observe writes the entry before either network call, and with a
// deployed window of 24h a steadily-failing workload never produces the quiet
// gap Case 2 needs, so an un-rolled-back entry means permanent silence for
// exactly the failure the window is tuned for.
func TestDispatcherRollsBackDedupOnDeliveryFailure(t *testing.T) {
	const sessionID = "session-1"

	for _, tc := range []struct {
		name            string
		failCreate      bool
		wantCreateCalls int
	}{
		// The daemon is not listening at all — a first-install pod whose
		// Session KV server has not come up yet.
		{name: "create session fails", failCreate: true, wantCreateCalls: 2},
		// The session is created but the payload cannot be delivered.
		{name: "inject fails", failCreate: false, wantCreateCalls: 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var createCount, injectCount int
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/sessions" {
					createCount++
					if tc.failCreate {
						w.WriteHeader(http.StatusServiceUnavailable)
						return
					}
					w.WriteHeader(http.StatusCreated)
					_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: sessionID})
					return
				}
				injectCount++
				w.WriteHeader(http.StatusInternalServerError)
			}))
			defer server.Close()

			inj, err := newInjector(injectorConfig{
				daemonURL:   server.URL,
				bearerToken: "mock-token",
				httpClient:  server.Client(),
			})
			if err != nil {
				t.Fatalf("failed to build injector: %v", err)
			}
			dedup, err := newDedupCache(24*time.Hour, "")
			if err != nil {
				t.Fatalf("failed to build cache: %v", err)
			}
			disp := &dispatcher{
				filter:   newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})),
				dedup:    dedup,
				injector: inj,
				metrics:  newMetrics(),
				mode:     "per-incident",
			}

			ev := TriageEvent{
				Key:       EventKey{UID: "pod-1", Reason: "CrashLoopBackOff"},
				Cluster:   "test-cluster",
				Namespace: "default",
				Name:      "billing-service",
				LastSeen:  time.Now(),
				Message:   "back-off restarting failed container",
			}

			disp.Dispatch(context.Background(), ev)
			if dedup.Len() != 0 {
				t.Fatalf("a failed dispatch left %d dedup entries; want 0", dedup.Len())
			}

			// The kubelet's next repeat, well inside the window. It must be
			// treated as a new incident, not suppressed against the alert
			// that never went out.
			ev.LastSeen = ev.LastSeen.Add(5 * time.Minute)
			disp.Dispatch(context.Background(), ev)
			if createCount != tc.wantCreateCalls {
				t.Errorf("got %d create-session calls; want %d (the retry was suppressed)",
					createCount, tc.wantCreateCalls)
			}
		})
	}
}

// TestDispatcherRollsBackDedupOnQuotaSuppression covers the delivery failure
// that arrives as a success. The daemon answers 200 {"status":"suppressed"}
// when the day's ceiling for the event's severity is spent — no chat post, no
// agent turn. HTTP-wise that is indistinguishable from a delivered alert, so
// without reading the body the watcher would keep a dedup entry for an alert
// nobody received, and at a 24h window the ceiling would reset at 00:00 UTC
// long before the entry did.
func TestDispatcherRollsBackDedupOnQuotaSuppression(t *testing.T) {
	const sessionID = "session-1"
	var createCount, injectCount int

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sessions" {
			createCount++
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: sessionID})
			return
		}
		injectCount++
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"suppressed","severity":"Warning","suppressed_today":"1"}`))
	}))
	defer server.Close()

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("failed to build injector: %v", err)
	}
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatalf("failed to build cache: %v", err)
	}
	disp := &dispatcher{
		filter:   newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})),
		dedup:    dedup,
		injector: inj,
		metrics:  newMetrics(),
		mode:     "per-incident",
	}

	ev := TriageEvent{
		Key:       EventKey{UID: "pod-1", Reason: "CrashLoopBackOff"},
		Cluster:   "test-cluster",
		Namespace: "default",
		Name:      "billing-service",
		LastSeen:  time.Now(),
		Message:   "back-off restarting failed container",
	}

	disp.Dispatch(context.Background(), ev)
	if dedup.Len() != 0 {
		t.Fatalf("a quota-suppressed alert left %d dedup entries; want 0", dedup.Len())
	}

	ev.LastSeen = ev.LastSeen.Add(5 * time.Minute)
	disp.Dispatch(context.Background(), ev)
	if injectCount != 2 {
		t.Errorf("got %d inject calls; want 2 (the re-offer was suppressed locally)", injectCount)
	}
	if createCount != 2 {
		t.Errorf("got %d create-session calls; want 2", createCount)
	}
}

// TestDispatcherKeepsDedupOnPolicyFilter is the other half of the test above,
// and the reason the two statuses are spelled differently. A ceiling drop is
// transient and reopens; an Info grade is not, and will grade the same way on
// every future sighting. If this path rolled the entry back too, a workload
// emitting a routine Normal-type event would be re-offered at its own repeat
// cadence forever — one session and one inject per sighting, for an alert that
// was never going to be sent.
func TestDispatcherKeepsDedupOnPolicyFilter(t *testing.T) {
	const sessionID = "session-1"
	var createCount, injectCount int

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sessions" {
			createCount++
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: sessionID})
			return
		}
		injectCount++
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"filtered"}`))
	}))
	defer server.Close()

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("failed to build injector: %v", err)
	}
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatalf("failed to build cache: %v", err)
	}
	disp := &dispatcher{
		filter:      newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})),
		dedup:       dedup,
		pullClasses: newPullClassMemo(0, 0),
		injector:    inj,
		metrics:     newMetrics(),
		mode:        "per-incident",
	}

	// An image-pull back-off, which is both the routine Info event this path
	// exists for and one the crash-loop debounce above deliberately exempts —
	// `canonicalizeReason` splits it out of the crash-loop family on the
	// message, so it reaches the dispatcher on its first sighting with
	// Count unset. Change that message and this test starts measuring the
	// filter instead of the dispatcher.
	//
	// Type has to be set, and set to exactly what kubelet emits here.
	// reopenPolicyFiltered lets a Warning-typed event re-open the incident, so
	// typing this one Warning would measure that escape hatch instead of the
	// entry surviving.
	ev := TriageEvent{
		Key:       EventKey{UID: "pod-1", Reason: "BackOff"},
		Cluster:   "test-cluster",
		Namespace: "default",
		Name:      "billing-service",
		LastSeen:  time.Now(),
		Message:   "Back-off pulling image",
		Type:      "Normal",
	}

	disp.Dispatch(context.Background(), ev)
	if dedup.Len() != 1 {
		t.Fatalf("a policy-filtered event left %d dedup entries; want 1 (the incident stays open)", dedup.Len())
	}

	ev.LastSeen = ev.LastSeen.Add(5 * time.Minute)
	disp.Dispatch(context.Background(), ev)
	if injectCount != 1 {
		t.Errorf("got %d inject calls; want 1 (the second sighting was deduped locally)", injectCount)
	}
	if createCount != 1 {
		t.Errorf("got %d create-session calls; want 1", createCount)
	}
}

// TestDispatcherReopensPolicyFilteredKeyForWarning is the limit on the test
// above. Keeping the dedup entry is right for the event that was graded, and
// the entry is not keyed on that event: canonicalizeReason folds kubelet's
// Normal-type `BackOff` ("Back-off pulling image"), `ErrImagePull` and the
// Warning-type `Failed` beside them onto the single key
// (uid, "ImagePullBackOff").
//
// So a bad image tag can put the routine member of the family in front, and
// every Warning behind it then takes dedupDuplicate. Case 3 slides LastSeen on
// each of those sightings, so the window never expires while the pull keeps
// failing and the alert never comes at all. The first Warning must get through.
func TestDispatcherReopensPolicyFilteredKeyForWarning(t *testing.T) {
	// Reasons the daemon graded Info and asked us to hold back, in arrival
	// order — the Normal-type back-off is deliberately first.
	var injected []string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sessions" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: "session-1"})
			return
		}
		body, _ := io.ReadAll(r.Body)
		var env injectMessageRequest
		_ = json.Unmarshal(body, &env) // the wire body is {"message": "<json>"}
		var p InjectPayload
		_ = json.Unmarshal([]byte(env.Message), &p)
		w.WriteHeader(http.StatusOK)
		// Stands in for session_kv_server.get_severity_details, which grades on
		// Event.Type alone: anything not typed Warning comes back Info.
		if p.Type == "Normal" {
			_, _ = w.Write([]byte(`{"status":"filtered"}`))
			return
		}
		injected = append(injected, p.Reason)
		_, _ = w.Write([]byte(`{"status":"injected"}`))
	}))
	defer server.Close()

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("failed to build injector: %v", err)
	}
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatalf("failed to build cache: %v", err)
	}
	disp := &dispatcher{
		filter:      newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})),
		dedup:       dedup,
		pullClasses: newPullClassMemo(0, 0),
		injector:    inj,
		metrics:     newMetrics(),
		mode:        "per-incident",
	}

	base := time.Now()
	disp.Dispatch(context.Background(), TriageEvent{
		Key: EventKey{UID: "pod-1", Reason: "BackOff"}, Type: "Normal",
		Cluster: "test-cluster", Namespace: "prod", Name: "api",
		Message: `Back-off pulling image "repo/api:typo"`, LastSeen: base, Count: 1,
	})
	if dedup.Len() != 1 {
		t.Fatalf("the policy-filtered event left %d dedup entries; want 1", dedup.Len())
	}

	// The three Warning-typed members of the same family, all of which
	// canonicalize onto the key the back-off above is holding.
	for i, ev := range []TriageEvent{
		{Key: EventKey{UID: "pod-1", Reason: "Failed"},
			Message: `Failed to pull image "repo/api:typo": not found`},
		{Key: EventKey{UID: "pod-1", Reason: "ErrImagePull"},
			Message: "rpc error: code = NotFound"},
		{Key: EventKey{UID: "pod-1", Reason: "Failed"},
			Message: "Error: ImagePullBackOff"},
	} {
		ev.Type, ev.Cluster, ev.Namespace, ev.Name, ev.Count = "Warning", "test-cluster", "prod", "api", 5
		ev.LastSeen = base.Add(time.Duration(i+1) * 10 * time.Minute)
		disp.Dispatch(context.Background(), ev)
	}

	// Exactly one: the first Warning re-opens the incident, and the two behind
	// it are then ordinary duplicates of a real, announced alert. More than one
	// would mean the escape hatch fires per sighting rather than per window.
	if len(injected) != 1 {
		t.Errorf("got %d Warning image-pull events through to the daemon (%v); want exactly 1 — "+
			"0 means the policy-filtered BackOff is still swallowing the whole family",
			len(injected), injected)
	}
}

// TestDispatcherUntypedEventCannotBurnTheReopenBudget is the adversarial limit
// on the test above.
//
// The reopen is one-shot per window: ReopenIfPolicyFiltered's guard needs
// PolicyFiltered set and Reopened clear, and the entry it installs carries
// Reopened. Spend that single firing on an event the daemon then grades Info
// and the reopened entry's own inject comes back filtered, at which point
// MarkPolicyFiltered deletes the key. The family does recover — the next
// sighting opens a fresh incident — but at one session per sighting until the
// daemon stops filtering it, which is the churn keeping the entry exists to
// avoid. Without that delete the key would be dead outright: the guard can
// never satisfy {PolicyFiltered: true, Reopened: true} again, Observe's Case 3
// slides LastSeen on every later sighting so it never expires, all three
// Forget call sites sit behind an attempted inject that no sighting now
// reaches, and restore rehydrates both flags across a restart.
//
// Barring only a literal "Normal" admitted exactly the events that do this: a
// lowercase or unrecognised Type passed the Go guard and came back Info. Since
// toTriageEvent passes Event.Type through as given and Decide never inspects it,
// anyone able to create an Event carrying the victim pod's UID could arm it
// deliberately.
//
// The mirror has to be the *endpoint*, not get_severity_details. inject_message
// runs `event_type = payload.get("type") or "Warning"` first, so an empty type
// alerts — testing against a bare "Warning" would withhold the reopen from an
// event that does reach chat. Both directions are covered below, and the fake
// daemon implements the coercion so a fixture written against the grader alone
// cannot make either case pass.
func TestDispatcherUntypedEventCannotBurnTheReopenBudget(t *testing.T) {
	var injected []string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sessions" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: "session-1"})
			return
		}
		body, _ := io.ReadAll(r.Body)
		var env injectMessageRequest
		_ = json.Unmarshal(body, &env)
		var p InjectPayload
		_ = json.Unmarshal([]byte(env.Message), &p)
		w.WriteHeader(http.StatusOK)
		// inject_message, faithfully — the coercion and then the grade:
		//   event_type = payload.get("type") or "Warning"
		//   event_lower == "warning" ? alert : Info
		// Both lines, because the bug this test exists for was the Go guard
		// mirroring only the second one.
		effectiveType := p.Type
		if effectiveType == "" {
			effectiveType = "Warning"
		}
		if !strings.EqualFold(effectiveType, "Warning") {
			_, _ = w.Write([]byte(`{"status":"filtered"}`))
			return
		}
		injected = append(injected, p.Reason)
		_, _ = w.Write([]byte(`{"status":"injected"}`))
	}))
	defer server.Close()

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("failed to build injector: %v", err)
	}

	// Both sides of the guard. The Info-graded types are the ones a bare
	// "Normal" test waved through; the empty type is the one a bare "Warning"
	// test would wrongly withhold the reopen from, since the daemon coerces it
	// and alerts. Each case runs against its own cache: one poisoned key is
	// enough, and a shared cache would let the first case decide the rest.
	// Exactly one alert per family either way — the family is one incident and
	// dedup is doing its job. What separates a healthy run from the bug is
	// *which* event it was, so that is what the assertion reads.
	for _, tc := range []struct {
		evType string
		want   []string
		why    string
	}{
		{"normal", []string{"ErrImagePull"}, "graded Info, so it must not spend the reopen the ErrImagePull needs"},
		{"Info", []string{"ErrImagePull"}, "graded Info, so it must not spend the reopen"},
		{"Unknown", []string{"ErrImagePull"}, "graded Info, so it must not spend the reopen"},
		{"", []string{"BackOff"}, "coerced to Warning by inject_message, so it alerts itself; the " +
			"ErrImagePull behind it is then an ordinary duplicate of a reported incident"},
	} {
		evType := tc.evType
		t.Run("type="+evType, func(t *testing.T) {
			injected = nil
			dedup, err := newDedupCache(24*time.Hour, "")
			if err != nil {
				t.Fatalf("failed to build cache: %v", err)
			}
			disp := &dispatcher{
				filter:      newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})),
				dedup:       dedup,
				pullClasses: newPullClassMemo(0, 0),
				injector:    inj,
				metrics:     newMetrics(),
				mode:        "per-incident",
			}

			base := time.Now()
			// 1. kubelet's routine back-off takes the canonical key.
			disp.Dispatch(context.Background(), TriageEvent{
				Key: EventKey{UID: "pod-1", Reason: "BackOff"}, Type: "Normal",
				Cluster: "test-cluster", Namespace: "prod", Name: "api",
				Message: `Back-off pulling image "repo/api:typo"`, LastSeen: base, Count: 1,
			})

			// 2. The event that used to spend the reopen for nothing.
			disp.Dispatch(context.Background(), TriageEvent{
				Key: EventKey{UID: "pod-1", Reason: "BackOff"}, Type: evType,
				Cluster: "test-cluster", Namespace: "prod", Name: "api",
				Message:  `Back-off pulling image "repo/api:typo"`,
				LastSeen: base.Add(5 * time.Minute), Count: 1,
			})

			// 3. The real failure, which must still get through. ErrImagePull
			// rather than the `Failed` beside it: `Failed`/"not found" is held
			// by the filter's own transient-pull debounce, so using it here
			// would measure that gate rather than the reopen. It is the same
			// event TestDispatcherReopensPolicyFilteredKeyForWarning's reopen
			// actually fires on.
			disp.Dispatch(context.Background(), TriageEvent{
				Key: EventKey{UID: "pod-1", Reason: "ErrImagePull"}, Type: "Warning",
				Cluster: "test-cluster", Namespace: "prod", Name: "api",
				Message:  "rpc error: code = NotFound",
				LastSeen: base.Add(10 * time.Minute), Count: 5,
			})

			if !reflect.DeepEqual(injected, tc.want) {
				t.Errorf("Type=%q: alerts through = %v, want %v — %s.\n"+
					"An empty list means the sighting burned the reopen and silenced the family permanently.",
					evType, injected, tc.want, tc.why)
			}
		})
	}
}

// TestMarkPolicyFilteredDropsAReopenedEntry pins the local escape hatch, so a
// family's recovery does not rest on reopenPolicyFiltered's guard agreeing with
// a grading rule written in Python and shipped on a different container image.
//
// A reopened entry is dead to ReopenIfPolicyFiltered whatever PolicyFiltered
// says: the guard requires Reopened clear, and nothing clears it. Leaving that
// entry in the map — flagged or not — silences the family for as long as
// sightings keep arriving, because Case 3 slides LastSeen and the three Forget
// callers all sit behind an inject no sighting reaches. So the entry must be
// gone, and the next sighting must open a new incident.
func TestMarkPolicyFilteredDropsAReopenedEntry(t *testing.T) {
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatalf("failed to build cache: %v", err)
	}
	key := EventKey{UID: "pod-1", Reason: "BackOff"}
	msg := `Back-off pulling image "repo/api:typo"`
	now := time.Now()

	dedup.Observe(key, msg, now)
	dedup.MarkPolicyFiltered(key, msg)
	if !dedup.ReopenIfPolicyFiltered(key, msg, now) {
		t.Fatal("the first reopen did not fire; the rest of this test measures nothing")
	}

	// The reopened incident's own inject comes back filtered.
	dedup.MarkPolicyFiltered(key, msg)

	canonical := key
	canonical.Reason = canonicalizeReason(key.Reason, msg)
	if _, ok := dedup.entries[canonical]; ok {
		t.Error("a reopened entry survived a second policy-filter; Reopened is already set, " +
			"so the guard can never fire again and Case 3 keeps the key alive forever")
	}

	// The property that actually matters: the family is reportable again.
	if got := dedup.Observe(key, msg, now.Add(time.Minute)); got.Kind != dedupNewIncident {
		t.Errorf("next sighting was %v, want dedupNewIncident — the family is still silenced", got.Kind)
	}
}

// TestMarkPolicyFilteredFlagsAnEntryThatWasNeverReopened is the control for the
// test above: the ordinary path must still flag rather than delete, or the
// filtered-keeps-the-entry design collapses into one session per sighting.
func TestMarkPolicyFilteredFlagsAnEntryThatWasNeverReopened(t *testing.T) {
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatalf("failed to build cache: %v", err)
	}
	key := EventKey{UID: "pod-2", Reason: "BackOff"}
	msg := `Back-off pulling image "repo/api:typo"`
	now := time.Now()

	dedup.Observe(key, msg, now)
	dedup.MarkPolicyFiltered(key, msg)

	canonical := key
	canonical.Reason = canonicalizeReason(key.Reason, msg)
	entry, ok := dedup.entries[canonical]
	if !ok {
		t.Fatal("the entry was deleted; a first policy-filter must keep it")
	}
	if !entry.PolicyFiltered {
		t.Error("the entry was not flagged, so the Warning behind it can never reopen")
	}
}

// TestReplayCannotSpendTheReopenBudget pins the one-shot reopen against an
// informer relist, which re-delivers events verbatim. The re-delivered Warning
// is the same sighting the daemon already graded, so spending the family's
// single firing on it pages a human about an hour-old event and leaves the next
// real Warning with nothing to spend.
func TestReplayCannotSpendTheReopenBudget(t *testing.T) {
	var injected []string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sessions" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: "session-1"})
			return
		}
		body, _ := io.ReadAll(r.Body)
		var env injectMessageRequest
		_ = json.Unmarshal(body, &env)
		var p InjectPayload
		_ = json.Unmarshal([]byte(env.Message), &p)
		w.WriteHeader(http.StatusOK)
		if p.Type == "Normal" {
			_, _ = w.Write([]byte(`{"status":"filtered"}`))
			return
		}
		injected = append(injected, p.Reason)
		_, _ = w.Write([]byte(`{"status":"injected"}`))
	}))
	defer server.Close()

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("failed to build injector: %v", err)
	}
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatalf("failed to build cache: %v", err)
	}
	disp := &dispatcher{
		filter:      newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})),
		dedup:       dedup,
		pullClasses: newPullClassMemo(0, 0),
		injector:    inj,
		metrics:     newMetrics(),
		mode:        "per-incident",
	}

	base := time.Now().Add(-time.Hour)
	warning := TriageEvent{
		Key: EventKey{UID: "pod-1", Reason: "ErrImagePull"}, Type: "Warning",
		Cluster: "test-cluster", Namespace: "prod", Name: "api",
		Message: "rpc error: code = NotFound", LastSeen: base, Count: 1,
	}

	// The Normal-typed back-off takes the canonical key first and comes back
	// filtered, arming the reopen for the Warnings behind it.
	disp.Dispatch(context.Background(), TriageEvent{
		Key: EventKey{UID: "pod-1", Reason: "BackOff"}, Type: "Normal",
		Cluster: "test-cluster", Namespace: "prod", Name: "api",
		Message: `Back-off pulling image "repo/api:typo"`, LastSeen: base, Count: 1,
	})

	disp.Dispatch(context.Background(), warning)
	if len(injected) != 0 {
		t.Errorf("a replay alerted: %v — the sighting is as old as the entry holding the key", injected)
	}

	// The control: the budget survived, so the next real sighting still gets through.
	warning.LastSeen = base.Add(time.Minute)
	disp.Dispatch(context.Background(), warning)
	if len(injected) != 1 {
		t.Errorf("alerts through = %v, want one ErrImagePull; the guard is refusing live events", injected)
	}
}

// TestDispatcherReopenedPayloadCountsFromOne pins the number the reopen puts on
// the wire, which is not the number Observe returned.
//
// Observe reports the count of the entry it just replaced: every sighting in the
// canonical family, nearly all of which were deduped locally and reached nobody.
// Injecting that writes it to `occurrences` in the ledger, and the recap sums
// that column into "Forwarded N events" and ranks its incident list by it — so a
// workload with two forwarded events would outrank one with nine, on the strength
// of sightings nobody was ever told about.
func TestDispatcherReopenedPayloadCountsFromOne(t *testing.T) {
	var counts []int

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sessions" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: "session-1"})
			return
		}
		body, _ := io.ReadAll(r.Body)
		var env injectMessageRequest
		_ = json.Unmarshal(body, &env)
		var p InjectPayload
		_ = json.Unmarshal([]byte(env.Message), &p)
		w.WriteHeader(http.StatusOK)
		if p.Type == "Normal" {
			_, _ = w.Write([]byte(`{"status":"filtered"}`))
			return
		}
		counts = append(counts, p.Count)
		_, _ = w.Write([]byte(`{"status":"injected"}`))
	}))
	defer server.Close()

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("failed to build injector: %v", err)
	}
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatalf("failed to build cache: %v", err)
	}
	disp := &dispatcher{
		filter:      newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})),
		dedup:       dedup,
		pullClasses: newPullClassMemo(0, 0),
		injector:    inj,
		metrics:     newMetrics(),
		mode:        "per-incident",
	}

	// A bad image tag: one Normal BackOff opens the incident and is graded
	// Info, then eight more sightings pile onto the same entry unseen.
	base := time.Now()
	for i := 0; i < 9; i++ {
		disp.Dispatch(context.Background(), TriageEvent{
			Key: EventKey{UID: "pod-1", Reason: "BackOff"}, Type: "Normal",
			Cluster: "test-cluster", Namespace: "prod", Name: "api",
			Message:  `Back-off pulling image "repo/api:typo"`,
			LastSeen: base.Add(time.Duration(i) * time.Minute), Count: 1,
		})
	}
	disp.Dispatch(context.Background(), TriageEvent{
		Key: EventKey{UID: "pod-1", Reason: "ErrImagePull"}, Type: "Warning",
		Cluster: "test-cluster", Namespace: "prod", Name: "api",
		Message: "rpc error: code = NotFound", LastSeen: base.Add(30 * time.Minute), Count: 1,
	})

	// 10, not 9: the reopening event's own Observe increments the outgoing
	// entry once more before the reopen replaces it.
	if len(counts) != 1 || counts[0] != 1 {
		t.Errorf("reopened payload carried counts %v; want [1] — 10 means the whole "+
			"canonical family's sightings were injected as this event's occurrences", counts)
	}
}

// TestDispatcherKeepsDedupEntryOnSuccess is the other half: a delivered alert
// must still suppress its repeats, or the rollback above would have traded
// silence for a flood.
func TestDispatcherKeepsDedupEntryOnSuccess(t *testing.T) {
	const sessionID = "session-1"
	var createCount int

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sessions" {
			createCount++
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: sessionID})
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("failed to build injector: %v", err)
	}
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatalf("failed to build cache: %v", err)
	}
	disp := &dispatcher{
		filter:   newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})),
		dedup:    dedup,
		injector: inj,
		metrics:  newMetrics(),
		mode:     "per-incident",
	}

	ev := TriageEvent{
		Key:       EventKey{UID: "pod-1", Reason: "CrashLoopBackOff"},
		Cluster:   "test-cluster",
		Namespace: "default",
		Name:      "billing-service",
		LastSeen:  time.Now(),
		Message:   "back-off restarting failed container",
	}

	disp.Dispatch(context.Background(), ev)
	if dedup.Len() != 1 {
		t.Fatalf("a delivered alert left %d dedup entries; want 1", dedup.Len())
	}

	ev.LastSeen = ev.LastSeen.Add(5 * time.Minute)
	disp.Dispatch(context.Background(), ev)
	if createCount != 1 {
		t.Errorf("got %d create-session calls; want 1 (the repeat was not suppressed)", createCount)
	}
}

// newCountingDispatcher builds a dispatcher against a stub daemon, returning the
// dispatcher, its metrics, a pointer to the running inject count, and the payloads
// the daemon actually received. Zero thresholds take the shipped defaults, the same
// as on the command line.
//
// The payloads matter as much as the count: a debounce that fires the right number
// of times but hands the agent a message with no error text in it has not done its
// job, and a count-only assertion cannot tell the difference.
func newCountingDispatcher(t *testing.T, th filterThresholds) (*dispatcher, *metrics, *int, *[]InjectPayload) {
	t.Helper()
	sessionID := "sess-1"
	injectCount := 0
	var injected []InjectPayload
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sessions" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: sessionID})
			return
		}
		injectCount++
		var req injectMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Errorf("decode inject request: %v", err)
		}
		var p InjectPayload
		if err := json.Unmarshal([]byte(req.Message), &p); err != nil {
			t.Errorf("unmarshal inject payload: %v", err)
		}
		injected = append(injected, p)
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(server.Close)

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("build injector: %v", err)
	}
	dedup, err := newDedupCache(5*time.Minute, "")
	if err != nil {
		t.Fatalf("build cache: %v", err)
	}
	m := newMetrics()
	return &dispatcher{
		filter:      newFilter(newFilterConfig(nil, nil, nil, th)),
		dedup:       dedup,
		pullClasses: newPullClassMemo(0, 0),
		injector:    inj,
		metrics:     m,
		mode:        "per-incident",
	}, m, &injectCount, &injected
}

// backoffEvent is the event kubelet actually emits while a container is
// restarting: Reason=BackOff, not Reason=CrashLoopBackOff.
func backoffEvent(count int) TriageEvent {
	return TriageEvent{
		Key:       EventKey{UID: "pod-1", Reason: "BackOff"},
		Cluster:   "test-cluster",
		Namespace: "default",
		Name:      "api",
		Message:   "Back-off restarting failed container app in pod api-7d9f",
		LastSeen:  time.Now(),
		Count:     count,
	}
}

// TestDispatcherCrashLoopDebounce walks the sequence the gate exists for: a pod
// blips twice during a node scale-up and recovers. Under the shipped default of
// 3 that must produce no session at all — not a session plus a later "resolved".
//
// The dedup cache is asserted empty afterwards because a held event must not
// leave state behind: an entry created by the blip would make the *real* crash
// loop that follows an hour later look like a duplicate and suppress it.
func TestDispatcherCrashLoopDebounce(t *testing.T) {
	disp, m, injectCount, _ := newCountingDispatcher(t, filterThresholds{backoffMinCount: 3})
	ctx := context.Background()

	disp.Dispatch(ctx, backoffEvent(1))
	disp.Dispatch(ctx, backoffEvent(2))

	if *injectCount != 0 {
		t.Errorf("transient crash loop fired %d injects; want 0", *injectCount)
	}
	if got := disp.dedup.Len(); got != 0 {
		t.Errorf("held events left %d dedup entries; want 0", got)
	}
	if got := testutil.ToFloat64(m.eventsFiltered.WithLabelValues("test-cluster", "", "", string(gateBackoffMinCount))); got != 2 {
		t.Errorf("events_filtered_total{gate=backoff_min_count} = %v; want 2", got)
	}

	// The same pod keeps crashing. At the threshold it has to get through.
	disp.Dispatch(ctx, backoffEvent(3))
	if *injectCount != 1 {
		t.Errorf("sustained crash loop fired %d injects; want 1", *injectCount)
	}
}

// TestDispatcherCrashLoopDebounceDisabled pins the escape hatch: --backoff-min-count=1
// is the pre-debounce behaviour, firing on the first event.
func TestDispatcherCrashLoopDebounceDisabled(t *testing.T) {
	disp, _, injectCount, _ := newCountingDispatcher(t, filterThresholds{backoffMinCount: 1})

	disp.Dispatch(context.Background(), backoffEvent(1))

	if *injectCount != 1 {
		t.Errorf("with backoff-min-count=1, first event fired %d injects; want 1", *injectCount)
	}
}

// TestDispatcherImagePullNotDebounced guards the exemption from the *crash-loop*
// gate. Nothing established a cause for this pod, so the class is unknown and the
// event has to fire on #1 — the pre-classifier behaviour.
func TestDispatcherImagePullNotDebounced(t *testing.T) {
	disp, _, injectCount, _ := newCountingDispatcher(t, filterThresholds{backoffMinCount: 3, imagePullTransientMinCount: 3})

	ev := backoffEvent(1)
	ev.Message = `Back-off pulling image "example.com/app:nope"`
	disp.Dispatch(context.Background(), ev)

	if *injectCount != 1 {
		t.Errorf("image-pull back-off fired %d injects; want 1", *injectCount)
	}
}

// pullFailedEvent is the cause-bearing half of the kubelet pair. Reason=Failed
// is not in the shipped default --reason list, so the filter drops it — the
// point of dispatching it is the class the memo records on the way past.
func pullFailedEvent(msg string) TriageEvent {
	ev := backoffEvent(1)
	ev.Key.Reason = "Failed"
	ev.Message = msg
	return ev
}

// pullBackOffEvent is the causeless half kubelet emits ten seconds later.
func pullBackOffEvent(count int) TriageEvent {
	ev := backoffEvent(count)
	ev.Message = `Back-off pulling image "us-docker.pkg.dev/proj/repo/app:v1"`
	ev.Count = count
	return ev
}

// TestDispatcherRegistryThrottleDebounced is the drill the memo exists for. The
// registry throttles a pull; kubelet says so once, on an event the filter then
// drops, and every event after that is a back-off carrying no cause at all.
// Classifying each message where it stands would hold the 429 and fire on the
// causeless back-off, which is worse than not classifying — so this fails
// without the carry-forward, not merely without the gate.
func TestDispatcherRegistryThrottleDebounced(t *testing.T) {
	disp, m, injectCount, injected := newCountingDispatcher(t, filterThresholds{imagePullTransientMinCount: 3})
	ctx := context.Background()

	const throttle = `Failed to pull image "us-docker.pkg.dev/proj/repo/app:v1": failed to pull and unpack image: unexpected status from HEAD request: 429 Too Many Requests`
	disp.Dispatch(ctx, pullFailedEvent(throttle))
	disp.Dispatch(ctx, pullBackOffEvent(1))
	disp.Dispatch(ctx, pullBackOffEvent(2))

	if *injectCount != 0 {
		t.Errorf("throttled pull fired %d injects; want 0", *injectCount)
	}
	if got := disp.dedup.Len(); got != 0 {
		t.Errorf("held events left %d dedup entries; want 0", got)
	}
	if got := testutil.ToFloat64(m.eventsFiltered.WithLabelValues("test-cluster", "", "", string(gateImagePullTransient))); got != 2 {
		t.Errorf("events_filtered_total{gate=imagepull_transient_min_count} = %v; want 2", got)
	}

	// The registry has not let up. At the threshold it stops being transient.
	disp.Dispatch(ctx, pullBackOffEvent(3))
	if *injectCount != 1 {
		t.Fatalf("sustained throttle fired %d injects; want 1", *injectCount)
	}

	// Firing is only half of it. The event that won is a back-off with no error
	// text in it, and there is no follow-up inject, so without pull_cause the
	// agent is handed a sustained registry throttle and told nothing about it.
	got := (*injected)[0]
	if strings.Contains(got.Message, "429") {
		t.Fatalf("message unexpectedly carries the cause (%q) — this drill no longer covers the causeless case", got.Message)
	}
	if got.PullCause != throttle {
		t.Errorf("pull_cause = %q; want the 429 text the earlier event carried", got.PullCause)
	}
}

// TestDispatcherThrottleFiresOnCauseBearingEvent runs the same incident with the
// deployed --reason list, which does include Failed, and with both counters
// climbing the way kubelet actually increments them.
//
// Measured on GKE v1.36.2-gke.2064000, the causeless events do outrun the
// cause-bearing one over a few minutes (12 versus 5), because "still backing off"
// is re-emitted on the pod-worker sync while the cause is re-emitted only when a
// retry fires. They overtake once the backoff interval exceeds the sync interval,
// which is after a threshold of 3 has already released — every live run opened
// with the cause. This pins that ordering so a threshold change has to confront
// it, and asserts pull_cause is omitted when the message already says it.
func TestDispatcherThrottleFiresOnCauseBearingEvent(t *testing.T) {
	disp, _, injectCount, injected := newCountingDispatcher(t, filterThresholds{imagePullTransientMinCount: 3})
	disp.filter = newFilter(newFilterConfig([]string{"Failed", "BackOff"}, nil, nil, filterThresholds{imagePullTransientMinCount: 3}))
	ctx := context.Background()

	const throttle = `Failed to pull image "us-docker.pkg.dev/proj/repo/app:v1": unexpected status from HEAD request: 429 Too Many Requests`
	cause := func(count int) TriageEvent {
		ev := pullFailedEvent(throttle)
		ev.Count = count
		return ev
	}

	// Attempt 1, then backoff; attempt 2, then backoff. The cause event reaches
	// the threshold on attempt 3, before the back-off counter gets there.
	disp.Dispatch(ctx, cause(1))
	disp.Dispatch(ctx, pullBackOffEvent(1))
	disp.Dispatch(ctx, cause(2))
	disp.Dispatch(ctx, pullBackOffEvent(2))
	if *injectCount != 0 {
		t.Fatalf("fired %d injects before the threshold; want 0", *injectCount)
	}

	disp.Dispatch(ctx, cause(3))
	if *injectCount != 1 {
		t.Fatalf("sustained throttle fired %d injects; want 1", *injectCount)
	}
	got := (*injected)[0]
	if got.Message != throttle {
		t.Errorf("message = %q; want the cause-bearing event to be the one injected", got.Message)
	}
	if got.PullCause != "" {
		t.Errorf("pull_cause = %q; want it omitted when message already carries the cause", got.PullCause)
	}
}

// TestDispatcherBadTagNotDebounced is the same shape with the opposite cause.
// A tag that does not exist is not going to start existing, so the carry-forward
// must not delay it.
func TestDispatcherBadTagNotDebounced(t *testing.T) {
	disp, _, injectCount, _ := newCountingDispatcher(t, filterThresholds{imagePullTransientMinCount: 3})
	ctx := context.Background()

	disp.Dispatch(ctx, pullFailedEvent(`Failed to pull image "us-docker.pkg.dev/proj/repo/app:v1": manifest unknown`))
	disp.Dispatch(ctx, pullBackOffEvent(1))

	if *injectCount != 1 {
		t.Errorf("bad tag fired %d injects; want 1", *injectCount)
	}
}

// TestDispatcherPullClassIsPerObject pins the memo key. One pod being throttled
// must not delay a different pod's bad tag.
func TestDispatcherPullClassIsPerObject(t *testing.T) {
	disp, _, injectCount, _ := newCountingDispatcher(t, filterThresholds{imagePullTransientMinCount: 3})
	ctx := context.Background()

	disp.Dispatch(ctx, pullFailedEvent(`Failed to pull image "us-docker.pkg.dev/proj/repo/app:v1": 429 Too Many Requests`))

	other := pullBackOffEvent(1)
	other.Key.UID = "pod-2"
	disp.Dispatch(ctx, other)

	if *injectCount != 1 {
		t.Errorf("unrelated pod fired %d injects; want 1", *injectCount)
	}
}

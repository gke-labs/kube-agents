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
	"net/http"
	"regexp"
	"strings"
	"time"
)

// injectorConfig holds the REST endpoint configuration for the agent gateway.
type injectorConfig struct {
	// daemonURL is the base endpoint (e.g. "http://localhost:8699") without a trailing slash.
	daemonURL string

	// bearerToken is the authorization token.
	bearerToken string

	// assertedCaller specifies the mapped owner ID sent in the X-Asserted-Caller header.
	assertedCaller string

	// httpClient is optional, allowing tests to inject mock HTTP clients.
	httpClient *http.Client
}

// injector handles session creation and event payload forwarding to the agent gateway.
type injector struct {
	cfg    injectorConfig
	client *http.Client
}

// newInjector creates a new injector and validates target endpoint configurations.
func newInjector(cfg injectorConfig) (*injector, error) {
	if cfg.daemonURL == "" {
		return nil, errors.New("injector: daemonURL is required")
	}
	if strings.HasSuffix(cfg.daemonURL, "/") {
		return nil, fmt.Errorf("injector: daemonURL must not end with '/' (got %q)", cfg.daemonURL)
	}
	if cfg.bearerToken == "" {
		return nil, errors.New("injector: bearerToken is required")
	}
	client := cfg.httpClient
	if client == nil {
		// Real production client with a modest timeout.
		client = &http.Client{
			Timeout: 10 * time.Second,
		}
	}
	return &injector{cfg: cfg, client: client}, nil
}

// createSessionResponse maps the JSON response from session creation.
type createSessionResponse struct {
	AppName   string `json:"app"`
	UserID    string `json:"user"`
	SessionID string `json:"sessionID"`
	URL       string `json:"url"`
}

// CreateSession creates a new troubleshooting session on the gateway and returns the session ID.
func (i *injector) CreateSession(ctx context.Context) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, i.cfg.daemonURL+"/sessions", nil)
	if err != nil {
		return "", fmt.Errorf("injector: build POST /sessions: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+i.cfg.bearerToken)
	if i.cfg.assertedCaller != "" {
		req.Header.Set("X-Asserted-Caller", i.cfg.assertedCaller)
	}
	resp, err := i.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("injector: POST /sessions: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return "", fmt.Errorf("injector: POST /sessions: status %d: %s", resp.StatusCode, string(body))
	}
	var payload createSessionResponse
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return "", fmt.Errorf("injector: decode POST /sessions response: %w", err)
	}
	if payload.SessionID == "" {
		return "", errors.New("injector: POST /sessions returned empty sessionID")
	}
	return payload.SessionID, nil
}

// injectMessageRequest wraps the event details payload for session ingestion.
type injectMessageRequest struct {
	Message string `json:"message"`
}

// injectResponse is the daemon's reply to an accepted inject: the status, and
// the features the daemon lists for the session; the daemon sends more on the
// suppressed path and may send more later.
type injectResponse struct {
	Status string `json:"status"`
	// Features is what the daemon says it does with this session beyond
	// posting the alert. The one read today is injectFeatureFollowup: a daemon
	// that lists it posts a k8s-event-followup into the alert's thread; one
	// that does not takes that kind down the event path and would post a
	// second alert, so the dispatcher sends none to it (dedupEntry.
	// FollowupSupported). The same feature is read again off /healthz before
	// each tick's follow-ups (Features), for the daemon of that moment.
	Features []string `json:"features,omitempty"`
}

// injectFeatureFollowup is the daemon's word for "I post a
// k8s-event-followup into the alert's thread"; session_kv_server spells it
// in INJECT_RESPONSE_FEATURES.
const injectFeatureFollowup = "followup"

// Supports reports whether the daemon listed feature on this response.
func (r injectResponse) Supports(feature string) bool {
	return hasFeature(r.Features, feature)
}

// injectStatusSuppressed is the daemon's word for "accepted, and then dropped
// without telling anyone" — the day's alert ceiling for the event's severity
// was already spent. The daemon answers 200 for this deliberately, so that the
// watcher does not retry into a ceiling that has not moved; the HTTP status
// alone therefore cannot distinguish a delivered alert from a dropped one, and
// the body is where the difference is stated.
const injectStatusSuppressed = "suppressed"

// injectStatusFiltered is the daemon's word for "accepted, and then dropped on
// purpose" — the event graded Info, so it was recorded in the daily recap
// instead of being announced. Unlike injectStatusSuppressed this is not a
// transient condition: the same event will grade Info again on its next
// sighting, so the dedup entry stays and the incident is not reopened.
// Rolling it back would re-offer routine churn at the event's own repeat
// cadence for as long as the workload keeps emitting.
//
// The two skew directions are not equally safe. A daemon predating this status
// answers "suppressed" for both, which reads as a ceiling drop and reopens: one
// redundant session, no silence.
//
// A watcher predating it is the harmful direction. It reads "filtered" as
// delivered and keeps the entry, but it has no MarkPolicyFiltered, so the entry
// carries no flag and ReopenIfPolicyFiltered can never fire for it. The key is
// canonical, so it is then held on behalf of the family's one Info member and
// every Warning behind it takes Case 3 in Observe, sliding LastSeen on each
// sighting — at the deployed 24h window a failing image pull keeps its own
// entry alive and never alerts.
//
// That direction is reachable on an ordinary install, which is why the watcher
// has to ask for this status rather than the daemon volunteering it — see
// injectFeaturesHeader. The two halves do not ship together: the daemon is
// session_kv_server.py, which the agent container runs from $TARGET_DIR/scripts
// on the shared PVC, while the watcher is a binary baked into the credential
// proxy sidecar image. One is replaced by an entrypoint copy onto a volume and
// the other by a container image pull, so they version independently on every
// install and a pod is not all-old or all-new.
const injectStatusFiltered = "filtered"

// injectStatusFolded is the daemon's word for "accepted, and recorded as one
// more sighting of an incident already open for the same workload" — an
// earlier event for it (another replica's, or this pod's under another reason)
// was admitted within the daemon's INCIDENT_WORKLOAD_DEDUP_SECONDS window. The
// entry stays, unflagged, and is held for this watcher's own window like any
// delivered incident: rolling it back would re-offer this pod at its repeat
// cadence, and flagging it policy-filtered would let the next Warning sighting
// reopen it. No feature negotiation: a watcher predating this status reads it
// as delivered, which keeps the entry the same way and only miscounts it as an
// injected event and a triage session.
const injectStatusFolded = "folded"

// sessionIDPattern is the shape of a session id the daemon issues
// (`k8s-evt-<hex>`, and the operator's own id in shared mode): letters,
// digits, dot, underscore and hyphen. A session id reaches the inject URL
// from the daemon's own answer or from the dedup snapshot on disk, and the
// check keeps a hand-edited snapshot from steering the POST anywhere else.
var sessionIDPattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)

// injectStatusUnrouted is the daemon's answer to a k8s-event-followup for a
// session with neither a chat thread nor a card: accepted, nothing posted,
// nothing to retry (session_kv_server, _inject_followup). A session with a
// card but no thread, every session on an install without a chat surface,
// gets the line on its card and is answered like a routed one.
const injectStatusUnrouted = "unrouted"

// healthzPath is the daemon's unauthenticated health route, which lists the
// inject kinds it dispatches and, under injectFeaturesKey, the features it
// serves (the drift detector reads the same list for its own feature);
// healthzBodyMax bounds the read.
const (
	healthzPath       = "/healthz"
	injectFeaturesKey = "features"
	healthzBodyMax    = 64 * 1024
)

// Features asks the daemon, now, which inject features it serves. The
// per-session answer recorded at alert time says what the daemon of that
// moment promised; this is what the daemon of this moment does, and the
// sweep asks before each tick's follow-ups so an agent image rolled back to
// a build without the follow-up branch is never sent one (it would take the
// kind down the event path and post a second alert).
func (i *injector) Features(ctx context.Context) ([]string, error) {
	// #nosec G704 -- the daemon URL is the operator's own loopback address from --daemon-url, not a request's input
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, i.cfg.daemonURL+healthzPath, nil)
	if err != nil {
		return nil, fmt.Errorf("injector: build GET healthz: %w", err)
	}
	resp, err := i.client.Do(req) // #nosec G704 -- same URL as above
	if err != nil {
		return nil, fmt.Errorf("injector: GET healthz: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, healthzBodyMax))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("injector: GET healthz: status %d", resp.StatusCode)
	}
	var parsed struct {
		Features []string `json:"features"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("injector: healthz body: %w", err)
	}
	return parsed.Features, nil
}

// hasFeature reports whether feature is in the list.
func hasFeature(features []string, feature string) bool {
	for _, f := range features {
		if f == feature {
			return true
		}
	}
	return false
}

// injectFeaturesHeader lists the response behaviours this watcher understands,
// so the daemon can answer an older one the way that older one expects.
//
// This exists because the skew is not symmetric. A watcher that does not know
// injectStatusFiltered reads it as delivered and keeps a dedup entry it will
// never reopen, silencing the whole canonical family — and the two halves are
// deployed by different mechanisms, so that pairing is an ordinary state rather
// than an edge case. Rather than requiring the daemon and the sidecar to roll in
// a fixed order, the watcher states what it can handle and the daemon falls back
// to injectStatusSuppressed for anyone who does not claim policy-filtered. The
// fallback costs one redundant session per Info sighting, which is the behaviour
// that shipped before this status existed.
//
// Comma-separated so a later feature is added without a second header.
const injectFeaturesHeader = "X-Watcher-Features"

// injectFeaturePolicyFiltered is the token that opts this watcher in to
// injectStatusFiltered. Matched against by the daemon's `_watcher_features`.
const injectFeaturePolicyFiltered = "policy-filtered"

// InjectDetailed POSTs the payload to the session's inject route and returns
// the daemon's whole answer: the status Dispatch reads and the features the
// sweep's handshake reads. The status rides beside the error because a 2xx
// does not by itself mean anyone was told — see injectStatusSuppressed and
// injectStatusFiltered. An empty or unparseable body reads as delivered: a
// daemon predating the field is one that always delivers, and guessing
// "dropped" would reopen every incident on every sighting.
func (i *injector) InjectDetailed(ctx context.Context, sessionID string, payload InjectPayload) (injectResponse, error) {
	var none injectResponse
	if sessionID == "" {
		return none, errors.New("injector: Inject: sessionID is required")
	}
	if !sessionIDPattern.MatchString(sessionID) {
		return none, fmt.Errorf("injector: Inject: session id %q is not one the daemon issues", sessionID)
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return none, fmt.Errorf("injector: marshal payload: %w", err)
	}
	wrapped, err := json.Marshal(injectMessageRequest{Message: string(body)})
	if err != nil {
		return none, fmt.Errorf("injector: wrap inject envelope: %w", err)
	}
	url := i.cfg.daemonURL + "/sessions/" + sessionID + "/inject"
	// #nosec G704 -- the daemon URL is the operator's own loopback address from --daemon-url, and the session id matched sessionIDPattern above
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(wrapped))
	if err != nil {
		return none, fmt.Errorf("injector: build POST inject: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+i.cfg.bearerToken)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(injectFeaturesHeader, injectFeaturePolicyFiltered)
	if i.cfg.assertedCaller != "" {
		req.Header.Set("X-Asserted-Caller", i.cfg.assertedCaller)
	}
	resp, err := i.client.Do(req) // #nosec G704 -- same URL as above
	if err != nil {
		return none, fmt.Errorf("injector: POST inject: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return none, fmt.Errorf("injector: POST inject: status %d: %s", resp.StatusCode, string(respBody))
	}
	var parsed injectResponse
	_ = json.Unmarshal(respBody, &parsed)
	return parsed, nil
}

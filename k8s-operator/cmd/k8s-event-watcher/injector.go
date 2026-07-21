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
	"strings"
	"time"
)

// injectorConfig captures the daemon-side surface the sidecar posts
// against. Constructed from CLI flags in main.go; injected into the
// injector so tests can substitute their own httptest server.
type injectorConfig struct {
	// daemonURL is the base URL (scheme + host + port) — e.g.
	// "http://127.0.0.1:7777" or "https://core-agent.example:7777".
	// The injector appends the path components; callers pass a
	// URL WITHOUT a trailing slash.
	daemonURL string

	// bearerToken authenticates the sidecar to the daemon. Loaded
	// from an env var by main.go (`--token-env NAME`).
	bearerToken string

	// assertedCaller is the identity the daemon stamps as session
	// Owner on POST /sessions. The sidecar must be listed in
	// attach.multi_session.proxy_identities in the daemon config
	// for this to be honored.
	assertedCaller string

	// httpClient lets tests swap in a *http.Client that talks to
	// httptest.NewServer. Nil in production; main.go leaves it nil.
	httpClient *http.Client
}

// injector wraps a small HTTP client that speaks the two daemon
// endpoints the sidecar needs: POST /sessions (creates a new
// per-incident session and returns the SessionID) and
// POST /sessions/<sid>/inject (queues a message on that session's
// inbox).
type injector struct {
	cfg    injectorConfig
	client *http.Client
}

// newInjector constructs an injector from the config. Validates the
// required fields early so misconfig fails fast.
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

// createSessionResponse mirrors the JSON response shape for POST /sessions.
type createSessionResponse struct {
	AppName   string `json:"app"`
	UserID    string `json:"user"`
	SessionID string `json:"sessionID"`
	URL       string `json:"url"`
}

// CreateSession asks the daemon to create a new session owned by
// cfg.assertedCaller. Returns the new SessionID on success.
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

// injectMessageRequest is the JSON body for POST /sessions/<sid>/inject.
type injectMessageRequest struct {
	Message string `json:"message"`
}

// Inject POSTs a payload to /sessions/<sid>/inject.
func (i *injector) Inject(ctx context.Context, sessionID string, payload InjectPayload) error {
	if sessionID == "" {
		return errors.New("injector: Inject: sessionID is required")
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("injector: marshal payload: %w", err)
	}
	wrapped, err := json.Marshal(injectMessageRequest{Message: string(body)})
	if err != nil {
		return fmt.Errorf("injector: wrap inject envelope: %w", err)
	}
	url := i.cfg.daemonURL + "/sessions/" + sessionID + "/inject"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(wrapped))
	if err != nil {
		return fmt.Errorf("injector: build POST inject: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+i.cfg.bearerToken)
	req.Header.Set("Content-Type", "application/json")
	if i.cfg.assertedCaller != "" {
		req.Header.Set("X-Asserted-Caller", i.cfg.assertedCaller)
	}
	resp, err := i.client.Do(req)
	if err != nil {
		return fmt.Errorf("injector: POST inject: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("injector: POST inject: status %d: %s", resp.StatusCode, string(respBody))
	}
	return nil
}

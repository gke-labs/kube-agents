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

// Inject posts the triage event details to the specified session's queue.
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

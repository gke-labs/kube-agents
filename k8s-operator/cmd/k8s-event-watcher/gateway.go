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
	"log"
	"net/http"
	"time"
)

const (
	// gatewayProbePath is the request leader_elect.py sends to ask whether
	// Hermes is up; the same one, so both read "up" the same way.
	gatewayProbePath = "/api/sessions?limit=1"
	// gatewayProbeTimeout bounds one probe, so a gateway that accepts and
	// stalls cannot hold the wait past its limit.
	gatewayProbeTimeout = 3 * time.Second
	// gatewayProbeInterval is the time between probes.
	gatewayProbeInterval = 2 * time.Second
	// gatewayWaitLogEvery is how often the wait says it is still waiting.
	gatewayWaitLogEvery = 30 * time.Second
)

// waitForGateway blocks until the agent gateway at gatewayURL answers, the
// limit passes, or ctx ends, and reports whether the gateway answered.
//
// The daemon accepts an inject before it can start the triage turn, because
// the turn runs in a background task after the 200. An event offered while
// the gateway is still booting is therefore marked delivered in the dedup
// cache and never offered again, although no triage ran. Holding the
// informers until the gateway answers loses nothing: their initial list
// replays every event still inside the API server's Event TTL.
//
// Any HTTP response counts as up, a 401 included: the question is whether
// the process is listening, not whether this caller may use it.
func waitForGateway(ctx context.Context, gatewayURL string, limit, interval time.Duration) bool {
	// An empty Proxy keeps a loopback probe on the pod even when the
	// container's environment sets http_proxy.
	client := &http.Client{
		Timeout:   gatewayProbeTimeout,
		Transport: &http.Transport{Proxy: nil},
	}
	deadline := time.Now().Add(limit)
	lastLog := time.Now()
	log.Printf("k8s-event-watcher: waiting up to %s for the agent gateway at %s before watching events", limit, gatewayURL)
	for {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, gatewayURL+gatewayProbePath, nil)
		if err != nil {
			// validate() rejects such a URL; this guards callers that skip it.
			log.Printf("k8s-event-watcher: ALERT gateway URL %q is not usable (%v); watching events without waiting", gatewayURL, err)
			return false
		}
		resp, probeErr := client.Do(req)
		if probeErr == nil {
			_ = resp.Body.Close()
			log.Printf("k8s-event-watcher: agent gateway at %s answered; watching events", gatewayURL)
			return true
		}
		if time.Now().After(deadline) {
			// This sidecar cannot tell a leader-election standby, which runs
			// no gateway, from a gateway that failed, so the line names both.
			log.Printf("k8s-event-watcher: ALERT the agent gateway at %s did not answer within %s (last probe: %v); watching events anyway, and triage for events offered before it answers will not start. On a leader-election standby, which runs no gateway, this line is printed on every start; otherwise the agent container did not start", gatewayURL, limit, probeErr)
			return false
		}
		if time.Since(lastLog) >= gatewayWaitLogEvery {
			// The probe's own error, so a URL that can never work (a TLS
			// mismatch, a name that does not resolve) reads differently from
			// a gateway that is still booting.
			log.Printf("k8s-event-watcher: still waiting for the agent gateway at %s (last probe: %v)", gatewayURL, probeErr)
			lastLog = time.Now()
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(interval):
		}
	}
}

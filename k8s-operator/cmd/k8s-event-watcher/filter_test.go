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
	"testing"
)

func TestFilterAccept(t *testing.T) {
	tests := []struct {
		name       string
		reasons    []string
		allowedNS  []string
		excludedNS []string
		minCount   int
		event      TriageEvent
		wantAccept bool
	}{
		{
			name:       "default config accepts standard reasons",
			event:      TriageEvent{Key: EventKey{Reason: "CrashLoopBackOff"}, Namespace: "default"},
			wantAccept: true,
		},
		{
			name:       "filters out unlisted reasons",
			event:      TriageEvent{Key: EventKey{Reason: "SomeRandomReason"}, Namespace: "default"},
			wantAccept: false,
		},
		{
			name:       "filters out excluded namespace",
			excludedNS: []string{"kube-system"},
			event:      TriageEvent{Key: EventKey{Reason: "CrashLoopBackOff"}, Namespace: "kube-system"},
			wantAccept: false,
		},
		{
			name:       "accepts allowed namespace if listed",
			allowedNS:  []string{"prod"},
			event:      TriageEvent{Key: EventKey{Reason: "CrashLoopBackOff"}, Namespace: "prod"},
			wantAccept: true,
		},
		{
			name:       "rejects non-allowed namespace if allowed list is non-empty",
			allowedNS:  []string{"prod"},
			event:      TriageEvent{Key: EventKey{Reason: "CrashLoopBackOff"}, Namespace: "staging"},
			wantAccept: false,
		},
		{
			name:       "unhealthy event below min count is rejected",
			minCount:   3,
			event:      TriageEvent{Key: EventKey{Reason: "Unhealthy"}, Namespace: "default", Count: 2},
			wantAccept: false,
		},
		{
			name:       "unhealthy event at or above min count is accepted",
			minCount:   3,
			event:      TriageEvent{Key: EventKey{Reason: "Unhealthy"}, Namespace: "default", Count: 3},
			wantAccept: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			cfg := newFilterConfig(tc.reasons, tc.allowedNS, tc.excludedNS, tc.minCount)
			f := newFilter(cfg)
			got := f.Accept(tc.event)
			if got != tc.wantAccept {
				t.Errorf("Accept(%+v) = %v; want %v", tc.event, got, tc.wantAccept)
			}
		})
	}
}

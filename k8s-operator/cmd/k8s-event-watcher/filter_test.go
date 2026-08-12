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

func TestFilterDecide(t *testing.T) {
	tests := []struct {
		name       string
		reasons    []string
		allowedNS  []string
		excludedNS []string
		minCount   int
		backoffMin int
		pullMin    int
		event      TriageEvent
		wantGate   filterGate
	}{
		{
			name:     "default config accepts standard reasons",
			event:    TriageEvent{Key: EventKey{Reason: "CrashLoopBackOff"}, Namespace: "default"},
			wantGate: gateAccepted,
		},
		{
			name:     "filters out unlisted reasons",
			event:    TriageEvent{Key: EventKey{Reason: "SomeRandomReason"}, Namespace: "default"},
			wantGate: gateReason,
		},
		{
			name:       "filters out excluded namespace",
			excludedNS: []string{"kube-system"},
			event:      TriageEvent{Key: EventKey{Reason: "CrashLoopBackOff"}, Namespace: "kube-system"},
			wantGate:   gateNamespaceExcluded,
		},
		{
			name:      "accepts allowed namespace if listed",
			allowedNS: []string{"prod"},
			event:     TriageEvent{Key: EventKey{Reason: "CrashLoopBackOff"}, Namespace: "prod"},
			wantGate:  gateAccepted,
		},
		{
			name:      "rejects non-allowed namespace if allowed list is non-empty",
			allowedNS: []string{"prod"},
			event:     TriageEvent{Key: EventKey{Reason: "CrashLoopBackOff"}, Namespace: "staging"},
			wantGate:  gateNamespaceNotAllowed,
		},
		{
			name:     "unhealthy event below min count is rejected",
			minCount: 3,
			event:    TriageEvent{Key: EventKey{Reason: "Unhealthy"}, Namespace: "default", Count: 2},
			wantGate: gateUnhealthyMinCount,
		},
		{
			name:     "unhealthy event at or above min count is accepted",
			minCount: 3,
			event:    TriageEvent{Key: EventKey{Reason: "Unhealthy"}, Namespace: "default", Count: 3},
			wantGate: gateAccepted,
		},

		// Crash-loop leading-edge debounce. kubelet's repeating crash-loop signal
		// arrives as Reason=BackOff, so the gate has to match the canonical family
		// rather than the wire reason — and must leave the image-pull half of that
		// same wire reason alone.
		{
			name:       "transient crash-loop backoff below min count is held",
			backoffMin: 3,
			event: TriageEvent{
				Key:       EventKey{Reason: "BackOff"},
				Namespace: "default",
				Message:   "Back-off restarting failed container app in pod api-7d9f",
				Count:     1,
			},
			wantGate: gateBackoffMinCount,
		},
		{
			name:       "sustained crash-loop backoff at min count fires",
			backoffMin: 3,
			event: TriageEvent{
				Key:       EventKey{Reason: "BackOff"},
				Namespace: "default",
				Message:   "Back-off restarting failed container app in pod api-7d9f",
				Count:     3,
			},
			wantGate: gateAccepted,
		},
		{
			name:       "wire reason CrashLoopBackOff is gated too",
			backoffMin: 3,
			event: TriageEvent{
				Key:       EventKey{Reason: "CrashLoopBackOff"},
				Namespace: "default",
				Message:   "Back-off restarting failed container",
				Count:     2,
			},
			wantGate: gateBackoffMinCount,
		},
		{
			name:       "image-pull backoff is exempt from the crash-loop gate",
			backoffMin: 3,
			event: TriageEvent{
				Key:       EventKey{Reason: "BackOff"},
				Namespace: "default",
				Message:   `Back-off pulling image "example.com/app:nope"`,
				Count:     1,
			},
			wantGate: gateAccepted,
		},
		{
			name:       "ImagePullBackOff is exempt from the crash-loop gate",
			backoffMin: 3,
			event: TriageEvent{
				Key:       EventKey{Reason: "ImagePullBackOff"},
				Namespace: "default",
				Count:     1,
			},
			wantGate: gateAccepted,
		},
		{
			name:       "backoff-min-count of 1 restores firing on the first event",
			backoffMin: 1,
			event: TriageEvent{
				Key:       EventKey{Reason: "BackOff"},
				Namespace: "default",
				Message:   "Back-off restarting failed container",
				Count:     1,
			},
			wantGate: gateAccepted,
		},
		{
			name:       "crash-loop event with no Event.Count fails open",
			backoffMin: 3,
			event: TriageEvent{
				Key:       EventKey{Reason: "BackOff"},
				Namespace: "default",
				Message:   "Back-off restarting failed container",
				Count:     0,
			},
			wantGate: gateAccepted,
		},

		// Image-pull transient debounce. Keyed on the class the dispatcher
		// resolved, not on the message: the back-off event carries no cause.
		{
			name:    "retryable pull failure below min count is held",
			pullMin: 3,
			event: TriageEvent{
				Key:       EventKey{Reason: "ImagePullBackOff"},
				Namespace: "default",
				Message:   `Back-off pulling image "us-docker.pkg.dev/p/r/app:v1"`,
				Count:     1,
				PullClass: pullClassRetryable,
			},
			wantGate: gateImagePullTransient,
		},
		{
			name:    "retryable pull failure at min count fires",
			pullMin: 3,
			event: TriageEvent{
				Key:       EventKey{Reason: "ImagePullBackOff"},
				Namespace: "default",
				Message:   `Back-off pulling image "us-docker.pkg.dev/p/r/app:v1"`,
				Count:     3,
				PullClass: pullClassRetryable,
			},
			wantGate: gateAccepted,
		},
		{
			name:    "terminal pull failure fires on the first event",
			pullMin: 3,
			event: TriageEvent{
				Key:       EventKey{Reason: "ImagePullBackOff"},
				Namespace: "default",
				Message:   `Back-off pulling image "us-docker.pkg.dev/p/r/app:nope"`,
				Count:     1,
				PullClass: pullClassTerminal,
			},
			wantGate: gateAccepted,
		},
		{
			name:    "unclassified pull failure fires on the first event",
			pullMin: 3,
			event: TriageEvent{
				Key:       EventKey{Reason: "ImagePullBackOff"},
				Namespace: "default",
				Message:   `Back-off pulling image "us-docker.pkg.dev/p/r/app:v1"`,
				Count:     1,
			},
			wantGate: gateAccepted,
		},
		{
			name:    "retryable pull failure with no Event.Count fails open",
			pullMin: 3,
			event: TriageEvent{
				Key:       EventKey{Reason: "ImagePullBackOff"},
				Namespace: "default",
				Count:     0,
				PullClass: pullClassRetryable,
			},
			wantGate: gateAccepted,
		},
		{
			name:    "imagepull-transient-min-count of 1 restores firing on the first event",
			pullMin: 1,
			event: TriageEvent{
				Key:       EventKey{Reason: "ImagePullBackOff"},
				Namespace: "default",
				Count:     1,
				PullClass: pullClassRetryable,
			},
			wantGate: gateAccepted,
		},
		{
			// A retryable class must not leak the pull gate onto an unrelated
			// family: only the pull gate reads PullClass, and only pull events
			// ever have it set.
			name:       "crash-loop gate wins over the pull gate for a crash loop",
			backoffMin: 3,
			pullMin:    3,
			event: TriageEvent{
				Key:       EventKey{Reason: "BackOff"},
				Namespace: "default",
				Message:   "Back-off restarting failed container",
				Count:     1,
				PullClass: pullClassRetryable,
			},
			wantGate: gateBackoffMinCount,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			cfg := newFilterConfig(tc.reasons, tc.allowedNS, tc.excludedNS, filterThresholds{
				unhealthyMinCount:          tc.minCount,
				backoffMinCount:            tc.backoffMin,
				imagePullTransientMinCount: tc.pullMin,
			})
			f := newFilter(cfg)
			if gate := f.Decide(tc.event); gate != tc.wantGate {
				t.Errorf("Decide(%+v) = %q; want %q", tc.event, gate, tc.wantGate)
			}
		})
	}
}

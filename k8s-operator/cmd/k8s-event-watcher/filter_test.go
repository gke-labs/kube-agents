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
	"time"
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

// TestFilterDecideFailedScheduling walks the gate that keeps FailedScheduling
// from opening a card per routine scale-up. The clock is pinned so the hold
// and the staleness check age against known values.
func TestFilterDecideFailedScheduling(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	fs := func(count int, mark scaleUpMark) TriageEvent {
		return TriageEvent{
			Key:       EventKey{UID: "pod-1", Reason: "FailedScheduling"},
			Namespace: "default",
			Name:      "api",
			Message:   "0/3 nodes are available: 3 Insufficient cpu.",
			LastSeen:  now,
			Count:     count,
			ScaleUp:   mark,
		}
	}
	triggered := func(age time.Duration) scaleUpMark {
		return scaleUpMark{Verdict: scaleUpTriggered, At: now.Add(-age)}
	}
	declined := func(age time.Duration) scaleUpMark {
		return scaleUpMark{Verdict: scaleUpDeclined, At: now.Add(-age)}
	}
	stale := fs(9, scaleUpMark{})
	stale.LastSeen = now.Add(-failedSchedulingStaleAfter - time.Second)
	untimed := fs(2, scaleUpMark{})
	untimed.LastSeen = time.Time{}
	staleButDeclined := fs(9, declined(time.Minute))
	staleButDeclined.LastSeen = now.Add(-failedSchedulingStaleAfter - time.Second)
	untimedDeclined := fs(1, declined(time.Minute))
	untimedDeclined.LastSeen = time.Time{}
	sightedAt := func(ev TriageEvent, at time.Time) TriageEvent {
		ev.LastSeen = at
		return ev
	}

	tests := []struct {
		name     string
		minCount int
		hold     time.Duration
		event    TriageEvent
		wantGate filterGate
	}{
		// The count backstop, with no autoscaler verdict on record.
		{name: "count 1 is held", event: fs(1, scaleUpMark{}), wantGate: gateFailedSchedulingMinCount},
		{name: "count 2 is held", event: fs(2, scaleUpMark{}), wantGate: gateFailedSchedulingMinCount},
		{name: "count 3 is held", event: fs(3, scaleUpMark{}), wantGate: gateFailedSchedulingMinCount},
		{name: "count 4 is held", event: fs(4, scaleUpMark{}), wantGate: gateFailedSchedulingMinCount},
		{name: "count 5 fires", event: fs(5, scaleUpMark{}), wantGate: gateAccepted},
		{name: "count 0 fails open", event: fs(0, scaleUpMark{}), wantGate: gateAccepted},
		{name: "threshold 1 restores firing on the first event", minCount: 1, event: fs(1, scaleUpMark{}), wantGate: gateAccepted},
		{name: "an explicit threshold is honoured", minCount: 8, event: fs(7, scaleUpMark{}), wantGate: gateFailedSchedulingMinCount},

		// A live TriggeredScaleUp holds regardless of count; an expired one does not.
		{name: "triggered mark holds count 5", event: fs(5, triggered(time.Minute)), wantGate: gateScaleUpHold},
		{name: "triggered mark holds count 50", event: fs(50, triggered(14*time.Minute)), wantGate: gateScaleUpHold},
		{name: "triggered mark at the hold boundary still holds", event: fs(5, triggered(defaultScaleUpHold)), wantGate: gateScaleUpHold},
		{name: "expired triggered mark falls back to the count and fires", event: fs(5, triggered(defaultScaleUpHold+time.Second)), wantGate: gateAccepted},
		{name: "expired triggered mark falls back to the count and holds", event: fs(2, triggered(defaultScaleUpHold+time.Second)), wantGate: gateFailedSchedulingMinCount},
		{name: "a shorter hold expires sooner", hold: time.Minute, event: fs(5, triggered(2*time.Minute)), wantGate: gateAccepted},

		// The mark is aged against the event's own sighting, not the clock.
		// After a restart the informer replays a scheduled pod's last
		// FailedScheduling with its sighting fixed at the end of the
		// scale-up; judged by the clock the mark has expired while the
		// sighting is not yet stale, and the count would fire.
		{name: "replayed event sighted inside the hold is held after the mark has aged past it", event: sightedAt(fs(5, triggered(defaultScaleUpHold+time.Second)), now.Add(-14*time.Minute)), wantGate: gateScaleUpHold},
		{name: "replayed event sighted just inside the hold is held however old the mark", event: sightedAt(fs(50, triggered(defaultScaleUpHold+14*time.Minute)), now.Add(-14*time.Minute)), wantGate: gateScaleUpHold},
		{name: "replayed event sighted past the hold falls through to the count", event: sightedAt(fs(5, triggered(defaultScaleUpHold+time.Minute)), now.Add(-30*time.Second)), wantGate: gateAccepted},
		{name: "event sighted before its mark is held", event: sightedAt(fs(5, triggered(time.Minute)), now.Add(-2*time.Minute)), wantGate: gateScaleUpHold},

		// NotTriggerScaleUp short-circuits the count.
		{name: "declined mark passes count 1", event: fs(1, declined(time.Second)), wantGate: gateAccepted},
		{name: "declined mark passes count 0", event: fs(0, declined(time.Second)), wantGate: gateAccepted},
		{name: "declined mark passes however old", event: fs(1, declined(23*time.Hour)), wantGate: gateAccepted},
		{name: "declined mark passes an untimed event", event: untimedDeclined, wantGate: gateAccepted},

		// The verdict qualifies only a sighting made later than it. After a
		// restart the informer replays the object the scheduler last bumped
		// before the autoscaler ruled; for a pod placed or deleted before the
		// next retry it is the only one, and passing it on the decline would
		// open a card for a pod that is no longer pending. The core/v1
		// recorder stamps to the second and a fast autoscaler declines in the
		// second of the attempt that drew it, so an equal timestamp is that
		// attempt, not a retry.
		{name: "event sighted before its decline falls through to the count and is held", event: sightedAt(fs(1, declined(time.Minute)), now.Add(-2*time.Minute)), wantGate: gateFailedSchedulingMinCount},
		{name: "event sighted before its decline falls through to the count and fires", event: sightedAt(fs(5, declined(time.Minute)), now.Add(-2*time.Minute)), wantGate: gateAccepted},
		{name: "event sighted in the second of its decline is the attempt that drew it and is held", event: sightedAt(fs(1, declined(2*time.Minute)), now.Add(-2*time.Minute)), wantGate: gateFailedSchedulingMinCount},
		{name: "event sighted a second after its decline passes", event: sightedAt(fs(1, declined(2*time.Minute)), now.Add(-2*time.Minute+time.Second)), wantGate: gateAccepted},

		// An event the scheduler stopped re-emitting describes a pod that is
		// no longer pending, whatever the count or the marks say.
		{name: "stale event is held at any count", event: stale, wantGate: gateFailedSchedulingStale},
		{name: "stale event is held despite a decline", event: staleButDeclined, wantGate: gateFailedSchedulingStale},
		{name: "unknown timestamp is not stale", event: untimed, wantGate: gateFailedSchedulingMinCount},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			f := newFilter(newFilterConfig(nil, nil, nil, filterThresholds{
				failedSchedulingMinCount: tc.minCount,
				scaleUpHold:              tc.hold,
			}))
			f.now = func() time.Time { return now }
			if gate := f.Decide(tc.event); gate != tc.wantGate {
				t.Errorf("Decide(count=%d, mark=%+v, lastSeen=%v) = %q; want %q", tc.event.Count, tc.event.ScaleUp, tc.event.LastSeen, gate, tc.wantGate)
			}
		})
	}
}

// TestFilterDecideScaleUpMarks pins how the two autoscaler reasons move through
// the gates: admitted by --reason they stop at gateScaleUpMark, so they are
// counted and recorded but never forwarded; left off it, or in an excluded
// namespace, they stop earlier and the dispatcher records nothing.
func TestFilterDecideScaleUpMarks(t *testing.T) {
	mark := func(reason, ns string) TriageEvent {
		return TriageEvent{Key: EventKey{UID: "pod-1", Reason: reason}, Namespace: ns, Type: "Normal", Count: 1, Reporter: scaleUpReporter}
	}
	reportedBy := func(ev TriageEvent, reporter string) TriageEvent {
		ev.Reporter = reporter
		return ev
	}
	tests := []struct {
		name       string
		reasons    []string
		excludedNS []string
		event      TriageEvent
		wantGate   filterGate
	}{
		{name: "TriggeredScaleUp on the list is a mark", reasons: []string{"FailedScheduling", "TriggeredScaleUp", "NotTriggerScaleUp"}, event: mark("TriggeredScaleUp", "default"), wantGate: gateScaleUpMark},
		{name: "NotTriggerScaleUp on the list is a mark", reasons: []string{"FailedScheduling", "TriggeredScaleUp", "NotTriggerScaleUp"}, event: mark("NotTriggerScaleUp", "default"), wantGate: gateScaleUpMark},
		{name: "TriggeredScaleUp off the list is dropped by reason", reasons: []string{"FailedScheduling"}, event: mark("TriggeredScaleUp", "default"), wantGate: gateReason},
		{name: "a mark from an excluded namespace stops at the namespace gate", reasons: []string{"FailedScheduling", "NotTriggerScaleUp"}, excludedNS: []string{"kube-system"}, event: mark("NotTriggerScaleUp", "kube-system"), wantGate: gateNamespaceExcluded},
		{name: "the default list carries neither mark", event: mark("NotTriggerScaleUp", "default"), wantGate: gateReason},
		// The reason alone is not a verdict: a mark that names another reporter,
		// or none, is dropped before the dispatcher records anything.
		{name: "a TriggeredScaleUp from another reporter is not a mark", reasons: scaleUpReasons, event: reportedBy(mark("TriggeredScaleUp", "default"), "my-operator"), wantGate: gateScaleUpMarkReporter},
		{name: "a NotTriggerScaleUp with no reporter is not a mark", reasons: scaleUpReasons, event: reportedBy(mark("NotTriggerScaleUp", "default"), ""), wantGate: gateScaleUpMarkReporter},
		{name: "the reporter check comes after the namespace gates", reasons: scaleUpReasons, excludedNS: []string{"kube-system"}, event: reportedBy(mark("TriggeredScaleUp", "kube-system"), "my-operator"), wantGate: gateNamespaceExcluded},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			f := newFilter(newFilterConfig(tc.reasons, nil, tc.excludedNS, filterThresholds{}))
			if gate := f.Decide(tc.event); gate != tc.wantGate {
				t.Errorf("Decide(%+v) = %q; want %q", tc.event, gate, tc.wantGate)
			}
		})
	}
}

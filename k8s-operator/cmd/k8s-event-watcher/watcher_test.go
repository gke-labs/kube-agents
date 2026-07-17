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
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

func TestToTriageEvent(t *testing.T) {
	now := time.Now()
	
	tests := []struct {
		name          string
		inputEvent    *corev1.Event
		wantFirstSeen time.Time
		wantLastSeen  time.Time
		wantMessage   string
	}{
		{
			name: "standard event with all timestamps",
			inputEvent: &corev1.Event{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-event",
					Namespace: "default",
				},
				InvolvedObject: corev1.ObjectReference{
					Kind:      "Pod",
					Name:      "pod-xyz",
					Namespace: "default",
					UID:       types.UID("uid-123"),
				},
				Reason:         "FailedScheduling",
				Message:        "pod failed to schedule",
				FirstTimestamp: metav1.Time{Time: now.Add(-10 * time.Minute)},
				LastTimestamp:  metav1.Time{Time: now},
				Count:          5,
			},
			wantFirstSeen: now.Add(-10 * time.Minute),
			wantLastSeen:  now,
			wantMessage:   "pod failed to schedule",
		},
		{
			name: "fallback to EventTime when timestamps are zero",
			inputEvent: &corev1.Event{
				InvolvedObject: corev1.ObjectReference{
					UID: types.UID("uid-123"),
				},
				EventTime: metav1.MicroTime{Time: now},
			},
			wantFirstSeen: now,
			wantLastSeen:  now,
			wantMessage:   "",
		},
		{
			name: "message truncation above limit",
			inputEvent: &corev1.Event{
				InvolvedObject: corev1.ObjectReference{
					UID: types.UID("uid-123"),
				},
				Message: strings.Repeat("A", 3000),
			},
			wantFirstSeen: time.Time{},
			wantLastSeen:  time.Time{},
			wantMessage:   strings.Repeat("A", 2048) + "... [truncated by k8s-event-watcher]",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := toTriageEvent(tc.inputEvent)
			if !got.FirstSeen.Equal(tc.wantFirstSeen) {
				t.Errorf("FirstSeen = %v; want %v", got.FirstSeen, tc.wantFirstSeen)
			}
			if !got.LastSeen.Equal(tc.wantLastSeen) {
				t.Errorf("LastSeen = %v; want %v", got.LastSeen, tc.wantLastSeen)
			}
			if got.Message != tc.wantMessage {
				t.Errorf("Message length = %d; want %d", len(got.Message), len(tc.wantMessage))
			}
		})
	}
}

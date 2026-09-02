/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"strings"
	"testing"

	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func agentWithMode(mode *string) *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		Spec: agentv1alpha1.PlatformAgentSpec{Mode: mode},
	}
}

func TestResolveMode(t *testing.T) {
	tests := []struct {
		name    string
		mode    *string
		want    Mode
		wantErr bool
	}{
		{name: "absent means today", mode: nil, want: ModeToday},
		{name: "today", mode: ptr.To("today"), want: ModeToday},
		{name: "next", mode: ptr.To("next"), want: ModeNext},
		{name: "unrecognized errors", mode: ptr.To("quantum"), wantErr: true},
		{name: "empty string errors", mode: ptr.To(""), wantErr: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := resolveMode(agentWithMode(tt.mode))
			if tt.wantErr {
				if err == nil {
					t.Fatalf("resolveMode(%v) = %q, want error", tt.mode, got)
				}
				// The error is the Degraded message's raw material; it has to
				// name the value the reconciler could not interpret.
				if tt.mode != nil && *tt.mode != "" && !strings.Contains(err.Error(), *tt.mode) {
					t.Errorf("resolveMode error %q does not name the value %q", err, *tt.mode)
				}
				return
			}
			if err != nil {
				t.Fatalf("resolveMode(%v) unexpected error: %v", tt.mode, err)
			}
			if got != tt.want {
				t.Errorf("resolveMode(%v) = %q, want %q", tt.mode, got, tt.want)
			}
		})
	}
}

func TestRenderModeFailsClosed(t *testing.T) {
	tests := []struct {
		name string
		mode *string
		want Mode
	}{
		{name: "absent is today", mode: nil, want: ModeToday},
		{name: "today is today", mode: ptr.To("today"), want: ModeToday},
		{name: "next is next", mode: ptr.To("next"), want: ModeNext},
		{name: "unrecognized fails closed to today", mode: ptr.To("quantum"), want: ModeToday},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := renderMode(agentWithMode(tt.mode), "nats"); got != tt.want {
				t.Errorf("renderMode = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestRenderModeIgnoresComponentArgument(t *testing.T) {
	// The component argument exists for per-feature graduation (see the mode
	// spec's override sketch); today every component gets the global answer.
	agent := agentWithMode(ptr.To("next"))
	for _, component := range []string{"nats", "gateway", "subagents", ""} {
		if got := renderMode(agent, component); got != ModeNext {
			t.Errorf("renderMode(next, %q) = %q, want %q", component, got, ModeNext)
		}
	}
}

func TestRenderModeNilAgent(t *testing.T) {
	if got := renderMode(nil, "nats"); got != ModeToday {
		t.Errorf("renderMode(nil) = %q, want %q", got, ModeToday)
	}
}

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

package memlimit

import (
	"runtime/debug"
	"strings"
	"testing"
)

// Every input class Derive acts on, for a caller that takes half of a shared
// container and one that takes nine tenths of its own.
func TestDerive(t *testing.T) {
	tests := []struct {
		name           string
		envName        string
		goMemLimit     string
		containerLimit string
		fraction       float64
		wantApply      bool
		wantBytes      int64
		wantReason     string
	}{
		{name: "container limit set, half", envName: "EVENT_WATCHER_MEMORY_LIMIT_BYTES", containerLimit: "2147483648", fraction: 0.5, wantApply: true, wantBytes: 1073741824},
		{name: "container limit set, nine tenths", envName: "OPERATOR_MEMORY_LIMIT_BYTES", containerLimit: "536870912", fraction: 0.9, wantApply: true, wantBytes: 483183820},
		{name: "explicit GOMEMLIMIT wins", envName: "OPERATOR_MEMORY_LIMIT_BYTES", goMemLimit: "512MiB", containerLimit: "2147483648", fraction: 0.9, wantReason: "GOMEMLIMIT=512MiB is set and takes precedence"},
		{name: "nothing set", envName: "OPERATOR_MEMORY_LIMIT_BYTES", fraction: 0.9, wantReason: "OPERATOR_MEMORY_LIMIT_BYTES is not set"},
		{name: "garbage", envName: "OPERATOR_MEMORY_LIMIT_BYTES", containerLimit: "2Gi", fraction: 0.9, wantReason: `OPERATOR_MEMORY_LIMIT_BYTES="2Gi" is not a positive byte count`},
		{name: "zero", envName: "OPERATOR_MEMORY_LIMIT_BYTES", containerLimit: "0", fraction: 0.9, wantReason: `OPERATOR_MEMORY_LIMIT_BYTES="0" is not a positive byte count`},
		{name: "negative", envName: "EVENT_WATCHER_MEMORY_LIMIT_BYTES", containerLimit: "-5", fraction: 0.5, wantReason: `EVENT_WATCHER_MEMORY_LIMIT_BYTES="-5" is not a positive byte count`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			gotBytes, gotApply, gotReason := Derive(tt.envName, tt.goMemLimit, tt.containerLimit, tt.fraction)
			if gotApply != tt.wantApply {
				t.Fatalf("apply = %v, want %v (reason %q)", gotApply, tt.wantApply, gotReason)
			}
			if gotBytes != tt.wantBytes {
				t.Errorf("bytes = %d, want %d", gotBytes, tt.wantBytes)
			}
			if !strings.Contains(gotReason, tt.wantReason) {
				t.Errorf("reason = %q, want it to contain %q", gotReason, tt.wantReason)
			}
		})
	}
}

// Apply turns the derived value into the runtime's soft limit, the same
// setting GOMEMLIMIT controls, and leaves it alone otherwise.
func TestApply_SetsTheRuntimeSoftLimit(t *testing.T) {
	// SetMemoryLimit(-1) reads the current limit without changing it.
	prev := debug.SetMemoryLimit(-1)
	t.Cleanup(func() { debug.SetMemoryLimit(prev) })

	t.Setenv(GoMemLimitEnv, "")
	t.Setenv("OPERATOR_MEMORY_LIMIT_BYTES", "536870912")
	gotBytes, applied, reason := Apply("OPERATOR_MEMORY_LIMIT_BYTES", 0.9)
	if !applied || gotBytes != 483183820 || reason != "" {
		t.Fatalf("Apply = (%d, %v, %q), want (483183820, true, \"\")", gotBytes, applied, reason)
	}
	if got := debug.SetMemoryLimit(-1); got != 483183820 {
		t.Errorf("runtime soft limit = %d, want 483183820", got)
	}

	// An explicit GOMEMLIMIT is respected: the limit set above must not move.
	debug.SetMemoryLimit(prev)
	t.Setenv(GoMemLimitEnv, "256MiB")
	_, applied, reason = Apply("OPERATOR_MEMORY_LIMIT_BYTES", 0.9)
	if applied || !strings.Contains(reason, "GOMEMLIMIT=256MiB is set") {
		t.Fatalf("Apply with GOMEMLIMIT set = (applied %v, %q), want it left alone", applied, reason)
	}
	if got := debug.SetMemoryLimit(-1); got != prev {
		t.Errorf("runtime soft limit = %d with GOMEMLIMIT set, want it untouched at %d", got, prev)
	}

	// No container limit: the runtime default stands and the reason says so.
	t.Setenv(GoMemLimitEnv, "")
	t.Setenv("OPERATOR_MEMORY_LIMIT_BYTES", "")
	_, applied, reason = Apply("OPERATOR_MEMORY_LIMIT_BYTES", 0.9)
	if applied || !strings.Contains(reason, "OPERATOR_MEMORY_LIMIT_BYTES is not set") {
		t.Fatalf("Apply with nothing set = (applied %v, %q), want it left alone", applied, reason)
	}
	if got := debug.SetMemoryLimit(-1); got != prev {
		t.Errorf("runtime soft limit = %d with nothing set, want it untouched at %d", got, prev)
	}
}

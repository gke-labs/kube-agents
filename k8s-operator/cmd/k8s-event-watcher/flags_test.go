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
)

func TestValidate_ClusterNameRequired(t *testing.T) {
	base := func() flags {
		return flags{
			daemonURL:   "http://localhost:8699",
			tokenEnv:    "TOKEN",
			mode:        "per-incident",
			owner:       "watcher",
			dedupWindow: 1,
		}
	}

	t.Run("missing name is an error", func(t *testing.T) {
		f := base()
		err := f.validate()
		if err == nil {
			t.Fatal("expected an error when --cluster-name is unset, got nil")
		}
		if !strings.Contains(err.Error(), "--cluster-name is required") {
			t.Errorf("expected a --cluster-name error, got: %v", err)
		}
	})

	t.Run("name present is valid", func(t *testing.T) {
		f := base()
		f.clusterName = "prod-us-central1"
		if err := f.validate(); err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
	})

	// Dry-run exempts --owner, because that only ever becomes a header on a
	// daemon request it never makes. It does not exempt the cluster name: a
	// dry run prints payloads, and an unnamed one is what it would ship.
	t.Run("dry-run still requires a name", func(t *testing.T) {
		f := flags{mode: "per-incident", dryRun: true, dedupWindow: 1}
		err := f.validate()
		if err == nil {
			t.Fatal("expected an error when --cluster-name is unset under --dry-run, got nil")
		}
		if !strings.Contains(err.Error(), "--cluster-name is required") {
			t.Errorf("expected a --cluster-name error, got: %v", err)
		}
	})
}

// TestParseFlags_FailedSchedulingDefaults pins the two knobs the FailedScheduling
// gate reads, and that they reach the filter as parsed rather than through the
// shared three-count default.
func TestParseFlags_FailedSchedulingDefaults(t *testing.T) {
	f, err := parseFlags(nil)
	if err != nil {
		t.Fatalf("parseFlags: %v", err)
	}
	if f.failedSchedulingMinCount != 5 {
		t.Errorf("--failedscheduling-min-count default = %d; want 5", f.failedSchedulingMinCount)
	}
	if f.scaleUpHold != 15*time.Minute {
		t.Errorf("--scaleup-hold default = %s; want 15m", f.scaleUpHold)
	}

	f, err = parseFlags([]string{"--failedscheduling-min-count=2", "--scaleup-hold=3m"})
	if err != nil {
		t.Fatalf("parseFlags: %v", err)
	}
	cfg := newFilterConfig(nil, nil, nil, filterThresholds{
		failedSchedulingMinCount: f.failedSchedulingMinCount,
		scaleUpHold:              f.scaleUpHold,
	})
	if cfg.failedSchedulingMinCount != 2 {
		t.Errorf("filterConfig.failedSchedulingMinCount = %d; want 2", cfg.failedSchedulingMinCount)
	}
	if cfg.scaleUpHold != 3*time.Minute {
		t.Errorf("filterConfig.scaleUpHold = %s; want 3m", cfg.scaleUpHold)
	}

	// Unset in the threshold group means the gate's own default, not the
	// shared three the other counts fall back to.
	cfg = newFilterConfig(nil, nil, nil, filterThresholds{})
	if cfg.failedSchedulingMinCount != 5 {
		t.Errorf("zero threshold defaulted to %d; want 5", cfg.failedSchedulingMinCount)
	}
	if cfg.scaleUpHold != 15*time.Minute {
		t.Errorf("zero hold defaulted to %s; want 15m", cfg.scaleUpHold)
	}
}

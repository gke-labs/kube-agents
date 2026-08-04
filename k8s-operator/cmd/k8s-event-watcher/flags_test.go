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

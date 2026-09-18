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

package clusterprofiles

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadIdentity(t *testing.T) {
	// nil with no error is "not a cluster profile", which is an ordinary
	// outcome for "default" and "platform" and must stay distinguishable from
	// a config.yaml that is broken.
	for _, tc := range []struct {
		name    string
		config  string
		absent  bool
		want    *Identity
		wantErr bool
	}{{
		name:   "complete block",
		config: "model:\n  provider: custom\ncluster_identity:\n  project: p\n  cluster: c\n  location: us-central1\n",
		want:   &Identity{Project: "p", Cluster: "c", Location: "us-central1"},
	}, {
		name:   "no cluster_identity at all",
		config: "model:\n  provider: custom\n",
	}, {
		// Two thirds of an identity names no cluster the GKE API could be
		// asked about, so it is absent rather than broken — the same call
		// cluster_agent_profile.read_cluster_identity makes.
		name:   "incomplete block",
		config: "cluster_identity:\n  project: p\n  cluster: c\n",
	}, {
		name:   "empty strings are incomplete",
		config: "cluster_identity:\n  project: p\n  cluster: c\n  location: \"\"\n",
	}, {
		// A profile that has not been written yet, or a non-profile directory.
		name:   "no config.yaml",
		absent: true,
	}, {
		name:    "unparseable",
		config:  "cluster_identity: [this is not a mapping\n",
		wantErr: true,
	}} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), profileConfigFile)
			if !tc.absent {
				if err := os.WriteFile(path, []byte(tc.config), 0o600); err != nil {
					t.Fatalf("write config: %v", err)
				}
			}
			got, err := ReadIdentity(path)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected an error, got identity %+v", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("ReadIdentity: %v", err)
			}
			if tc.want == nil {
				if got != nil {
					t.Fatalf("got identity %+v, want nil (not a cluster profile)", got)
				}
				return
			}
			if got == nil || *got != *tc.want {
				t.Errorf("got %+v, want %+v", got, tc.want)
			}
		})
	}
}

func TestIdentityStringSeparatesSameNameInTwoLocations(t *testing.T) {
	// The string is what discovery dedupes on, so two real clusters sharing a
	// name must not produce one key.
	a := Identity{Project: "p", Cluster: "prod", Location: "us-central1"}
	b := Identity{Project: "p", Cluster: "prod", Location: "europe-west1"}
	if a.String() == b.String() {
		t.Fatalf("both identities stringify to %q; the second would be dropped as a duplicate", a)
	}
	if got, want := a.String(), "p/us-central1/prod"; got != want {
		t.Errorf("String() = %q, want %q", got, want)
	}
}

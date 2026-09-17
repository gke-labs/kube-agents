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
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// kubeconfigYAML is a minimal, well-formed kubeconfig. It points at an address
// nothing is listening on, which is fine: building a client does not connect.
const kubeconfigYAML = `apiVersion: v1
kind: Config
clusters:
  - name: prod-a
    cluster:
      server: https://127.0.0.1:1
contexts:
  - name: prod-a
    context:
      cluster: prod-a
      user: prod-a
current-context: prod-a
users:
  - name: prod-a
    user:
      token: not-a-real-token
`

func writeKubeconfig(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "kubeconfig")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatalf("writing kubeconfig: %v", err)
	}
	return path
}

func TestNewObjectGetterWithNoCredentialsIsNotAnError(t *testing.T) {
	// A detector without cluster credentials is a supported mode -- it is how
	// the binary ran before the join existed -- so the join degrades to a count
	// rather than the process refusing to start.
	getter, err := newObjectGetter("", false)
	if err != nil {
		t.Fatalf("newObjectGetter returned error: %v", err)
	}
	if getter != nil {
		t.Errorf("getter = %v, want nil so the joiner reports every record unreachable", getter)
	}
}

func TestNewObjectGetterFromKubeconfig(t *testing.T) {
	getter, err := newObjectGetter(writeKubeconfig(t, kubeconfigYAML), false)
	if err != nil {
		t.Fatalf("newObjectGetter returned error: %v", err)
	}
	if getter == nil {
		t.Fatal("getter = nil, want a client built from the kubeconfig")
	}
	if _, ok := getter.(dynamicGetter); !ok {
		t.Errorf("getter is %T, want dynamicGetter", getter)
	}
}

func TestNewObjectGetterRejectsAnUnusableKubeconfig(t *testing.T) {
	for _, tc := range []struct {
		name string
		path string
	}{
		{name: "a path that does not exist", path: filepath.Join(t.TempDir(), "absent")},
		{name: "a file that is not a kubeconfig", path: writeKubeconfig(t, "not: [a, kubeconfig")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// Reported at startup rather than as a lookup failure per record:
			// the second shape is a detector that runs for hours before anyone
			// notices the join never worked.
			if _, err := newObjectGetter(tc.path, false); err == nil {
				t.Error("newObjectGetter returned no error for an unusable kubeconfig")
			}
		})
	}
}

func TestNewObjectGetterInClusterOutsideACluster(t *testing.T) {
	// rest.InClusterConfig reads the ServiceAccount mount and the
	// KUBERNETES_SERVICE_* environment, none of which exists here. The error is
	// the point: --in-cluster set outside a Pod must fail loudly at startup.
	if _, err := newObjectGetter("", true); err == nil {
		t.Skip("running inside a cluster, so there is no failure to assert on")
	}
}

// declaredCluster is the identity the flags spell out in the tests below.
func declaredCluster() clusterIdentity {
	return clusterIdentity{Project: "example-project", Location: "us-central1", Cluster: "prod-a"}
}

func TestVerifyClusterIdentity(t *testing.T) {
	for _, tc := range []struct {
		name       string
		observed   clusterIdentity
		observeErr error
		wantErr    string
		wantLine   string
	}{
		{
			name:     "the credentials reach the cluster the flags declare",
			observed: declaredCluster(),
		},
		{
			// The failure the check exists for: the flags decide which records
			// to serve, the credentials decide where from, and this is the two
			// disagreeing. Every lookup would succeed against a real object of
			// the right name in the wrong cluster, so no count moves and
			// nothing else in the process would ever say so.
			name:     "a different cluster in the same project stops the process",
			observed: clusterIdentity{Project: "example-project", Location: "us-east4", Cluster: "prod-a"},
			wantErr:  "refusing to enrich",
		},
		{
			// Same name, same location, another project. Caught by the same
			// comparison, and worth its own case because it is the one a
			// name-and-location check would miss.
			name:     "a same-named cluster in another project stops the process",
			observed: clusterIdentity{Project: "other-project", Location: "us-central1", Cluster: "prod-a"},
			wantErr:  "refusing to enrich",
		},
		{
			// Not a mismatch, so not fatal: a kind cluster publishes no GKE
			// node attributes and a hand-written kubeconfig has no gcloud
			// context name, and refusing to start there would break the local
			// runs --kubeconfig exists for.
			name:       "an unreadable identity is logged, not fatal",
			observeErr: errors.New("metadata server unreachable"),
			wantLine:   "could not confirm which cluster",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			line, err := verifyClusterIdentity(context.Background(), declaredCluster(),
				func(context.Context) (clusterIdentity, error) { return tc.observed, tc.observeErr })

			switch {
			case tc.wantErr == "" && err != nil:
				t.Fatalf("verifyClusterIdentity returned error %v, want none", err)
			case tc.wantErr != "" && err == nil:
				t.Fatalf("verifyClusterIdentity returned no error, want one containing %q", tc.wantErr)
			case tc.wantErr != "" && !strings.Contains(err.Error(), tc.wantErr):
				t.Fatalf("verifyClusterIdentity error = %q, want it to contain %q", err, tc.wantErr)
			}
			if tc.wantErr != "" {
				// Both identities, so the operator can see which half is wrong
				// without reading the manifest and the node side by side.
				for _, want := range []string{tc.observed.String(), declaredCluster().String()} {
					if !strings.Contains(err.Error(), want) {
						t.Errorf("error = %q, want it to name %q", err, want)
					}
				}
				return
			}
			if tc.wantLine == "" && line != "" {
				t.Errorf("line = %q, want nothing to log when the identity checks out", line)
			}
			if tc.wantLine != "" && !strings.Contains(line, tc.wantLine) {
				t.Errorf("line = %q, want it to contain %q", line, tc.wantLine)
			}
		})
	}
}

func TestVerifyClusterIdentityBoundsItsOwnProbe(t *testing.T) {
	// The probe runs before the pull loop, on a context that is cancelled only
	// by SIGTERM, so a metadata server that accepts the connection and never
	// answers would otherwise hold startup open indefinitely -- with no output,
	// because the announcement has already been logged.
	var (
		deadline time.Time
		ok       bool
	)
	if _, err := verifyClusterIdentity(context.Background(), declaredCluster(),
		func(probeCtx context.Context) (clusterIdentity, error) {
			deadline, ok = probeCtx.Deadline()
			return declaredCluster(), nil
		}); err != nil {
		t.Fatalf("verifyClusterIdentity returned error: %v", err)
	}

	if !ok {
		t.Fatal("the probe context carries no deadline; an unanswered probe would hang startup")
	}
	if left := time.Until(deadline); left <= 0 || left > clusterIdentityProbeTimeout {
		t.Errorf("probe deadline is %s away, want it within (0, %s]", left, clusterIdentityProbeTimeout)
	}
}

func TestParseGKEContextName(t *testing.T) {
	for _, tc := range []struct {
		name    string
		context string
		want    clusterIdentity
		wantErr bool
	}{
		{
			name:    "the name gcloud writes",
			context: "gke_example-project_us-central1_prod-a",
			want:    declaredCluster(),
		},
		{
			// A zonal cluster: the location field carries the zone, and the
			// hyphens in it must not be read as extra fields.
			name:    "a zonal location",
			context: "gke_example-project_us-central1-a_prod-a",
			want:    clusterIdentity{Project: "example-project", Location: "us-central1-a", Cluster: "prod-a"},
		},
		{
			name:    "a renamed context",
			context: "prod-a",
			wantErr: true,
		},
		{
			name:    "a four-field name that is not gcloud's",
			context: "eks_example-project_us-central1_prod-a",
			wantErr: true,
		},
		{
			name:    "no current context at all",
			context: "",
			wantErr: true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseGKEContextName(tc.context)
			if tc.wantErr {
				if err == nil {
					// Returning a half-parsed identity here would be worse than
					// returning nothing: verifyClusterIdentity treats any
					// identity it is handed as authoritative and stops the
					// process when it disagrees.
					t.Fatalf("parseGKEContextName(%q) = %v, want an error", tc.context, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("parseGKEContextName(%q) returned error: %v", tc.context, err)
			}
			if got != tc.want {
				t.Errorf("parseGKEContextName(%q) = %v, want %v", tc.context, got, tc.want)
			}
		})
	}
}

func TestObserveClusterFromKubeconfig(t *testing.T) {
	// The kubeconfig side end to end: the file the join will read is the file
	// the identity is read out of, which is what makes the comparison mean
	// anything.
	const gkeNamed = `apiVersion: v1
kind: Config
clusters:
  - name: prod-a
    cluster:
      server: https://127.0.0.1:1
contexts:
  - name: gke_example-project_us-central1_prod-a
    context:
      cluster: prod-a
      user: prod-a
current-context: gke_example-project_us-central1_prod-a
users:
  - name: prod-a
    user:
      token: not-a-real-token
`

	got, err := observeCluster(context.Background(), writeKubeconfig(t, gkeNamed), false)
	if err != nil {
		t.Fatalf("observeCluster returned error: %v", err)
	}
	if got != declaredCluster() {
		t.Errorf("observeCluster = %v, want %v", got, declaredCluster())
	}
}

func TestObserveClusterCannotReadARenamedContext(t *testing.T) {
	// kubeconfigYAML's current context is "prod-a", which carries no project
	// and no location. Unreadable rather than mismatched: verifyClusterIdentity
	// turns this into the line saying the identity went unverified, and the run
	// continues.
	if _, err := observeCluster(context.Background(), writeKubeconfig(t, kubeconfigYAML), false); err == nil {
		t.Error("observeCluster returned no error for a context name that is not gcloud's")
	}
}

func TestObserveClusterReportsAnUnreadableKubeconfig(t *testing.T) {
	if _, err := observeCluster(context.Background(), filepath.Join(t.TempDir(), "absent"), false); err == nil {
		t.Error("observeCluster returned no error for a kubeconfig that does not exist")
	}
}

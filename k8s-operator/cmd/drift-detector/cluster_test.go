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
	"os"
	"path/filepath"
	"testing"
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

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
	"fmt"

	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

// newObjectGetter builds a live-object reader for one directly reachable
// cluster.
//
// One cluster, because that is the whole of what this task ships. The audit
// subscription is project-wide and carries every cluster in it, so a detector
// wired this way enriches the records from its own cluster and reports the rest
// as joinUnreachable -- visibly, as a count, rather than by quietly reporting
// them unenriched. Reaching the others means reading the cluster_identity block
// out of each Cluster Agent profile, asking the GKE API where that cluster's
// control plane is, and authenticating to all of them as this pod's own Google
// identity -- one token source shared across the fleet, not a credential per
// cluster. k8s-event-watcher's discoverClusterProfiles already does exactly
// that, and doing it here is the next task; this function is what it replaces.
//
// Worth carrying across with it: that watcher deliberately does not read a
// kubeconfig back out of the sandbox volume, because anything written there is
// writable by the model and this process would attach a cloud-platform token to
// whatever host it was pointed at. --kubeconfig below is an operator-supplied
// path for local runs, not a discovery mechanism, and the fan-in must not turn
// it into one.
//
// Returns nil with no error when neither source is configured. A detector
// without cluster credentials is a supported mode: it is how the binary has run
// since T1, and the join degrades to a count rather than refusing to start.
func newObjectGetter(kubeconfig string, inCluster bool) (objectGetter, error) {
	var (
		cfg *rest.Config
		err error
	)

	switch {
	case inCluster:
		cfg, err = rest.InClusterConfig()
		if err != nil {
			return nil, fmt.Errorf("in-cluster config: %w", err)
		}
	case kubeconfig != "":
		cfg, err = clientcmd.BuildConfigFromFlags("", kubeconfig)
		if err != nil {
			return nil, fmt.Errorf("kubeconfig %q: %w", kubeconfig, err)
		}
	default:
		return nil, nil
	}

	client, err := dynamic.NewForConfig(cfg)
	if err != nil {
		return nil, fmt.Errorf("dynamic client: %w", err)
	}
	return dynamicGetter{client: client}, nil
}

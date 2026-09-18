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

// Package clusterprofiles turns a Hermes profiles directory into a set of
// addressable GKE clusters.
//
// The Platform Agent creates one Cluster Agent profile per onboarded cluster
// and removes it on teardown (agents/platform/scripts/cluster_agent_profile.py),
// so that directory is the inventory of clusters a fleet-wide process should be
// reaching, and each profile records which cluster it is scoped to. This package
// reads that inventory, asks the GKE API where each cluster's control plane is,
// and returns a rest.Config per cluster with this process's own Google
// credential attached.
//
// It builds no Kubernetes client. The caller picks the client type, and this
// package stops at the configuration every client type is built from. Today
// the only caller is the event watcher, which wants an informer-backed
// kubernetes.Interface; the drift detector wants a dynamic.Interface from the
// same scan, and that is the seam this package exists for, but its fan-in is
// not wired yet (cmd/drift-detector/cluster.go).
package clusterprofiles

import (
	"errors"
	"fmt"
	"os"

	"sigs.k8s.io/yaml"
)

// profileConfigFile is the file inside a profile directory that carries the
// cluster_identity block. Named here because it is the whole contract between
// this package and the Python side that writes the profiles.
const profileConfigFile = "config.yaml"

// Identity is the cluster_identity block the Platform Agent writes into each
// Cluster Agent profile's config.yaml. sigs.k8s.io/yaml converts YAML to JSON,
// hence the json tags.
type Identity struct {
	Project  string `json:"project"`
	Cluster  string `json:"cluster"`
	Location string `json:"location"`
}

// String is what makes one cluster distinct from every other. A GKE cluster
// name is unique only within a project and location, so a fleet can
// legitimately run "prod" in us-central1 and "prod" in europe-west1 — the
// Platform Agent creates a profile for each, keyed on the full triple. Keying
// on the bare name would treat the second as a duplicate of the first and
// silently leave it unreached.
func (i Identity) String() string {
	return i.Project + "/" + i.Location + "/" + i.Cluster
}

// complete reports whether all three parts are present. An incomplete block is
// "not a cluster profile" rather than a broken one, matching what
// cluster_agent_profile.read_cluster_identity treats as absent.
func (i Identity) complete() bool {
	return i.Cluster != "" && i.Project != "" && i.Location != ""
}

// profileConfig is the subset of a profile's config.yaml that we read.
type profileConfig struct {
	ClusterIdentity Identity `json:"cluster_identity"`
}

// ReadIdentity parses the cluster_identity block out of a profile's
// config.yaml. Returns nil (not an error) when the file is absent or the block
// is missing or incomplete — that means "not a cluster profile". A config.yaml
// that exists but cannot be parsed is a real error.
func ReadIdentity(path string) (*Identity, error) {
	data, err := os.ReadFile(path) // #nosec G304 -- Path to profile config file supplied via flag / discovery
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	var cfg profileConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	id := cfg.ClusterIdentity
	if !id.complete() {
		return nil, nil
	}
	return &id, nil
}

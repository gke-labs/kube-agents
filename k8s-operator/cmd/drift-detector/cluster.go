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
	"fmt"
	"strings"
	"time"

	"cloud.google.com/go/compute/metadata"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

const (
	// clusterIdentityProbeTimeout bounds the startup check that the credentials
	// reach the cluster the flags name. Short, because both sources are local:
	// the metadata server is on the node and the kubeconfig is on disk, so a
	// probe still running after this is not going to answer.
	clusterIdentityProbeTimeout = 5 * time.Second

	// metadataClusterNameAttribute and metadataClusterLocationAttribute are the
	// node attributes naming the cluster a Pod runs in. In --in-cluster mode
	// these are not a heuristic: InClusterConfig reaches the Pod's own API
	// server, so the cluster the node belongs to is the cluster being read.
	metadataClusterNameAttribute     = "instance/attributes/cluster-name"
	metadataClusterLocationAttribute = "instance/attributes/cluster-location"

	// gkeContextPrefix, gkeContextSeparator and gkeContextFields describe the
	// context name `gcloud container clusters get-credentials` writes:
	// gke_<project>_<location>_<cluster>. None of those three values may
	// contain an underscore -- GCP project IDs and GKE names are lowercase
	// alphanumerics and hyphens -- so splitting on it and requiring an exact
	// field count parses without ambiguity.
	gkeContextPrefix    = "gke"
	gkeContextSeparator = "_"
	gkeContextFields    = 4
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

// verifyClusterIdentity checks the declared cluster identity against the one
// the credentials actually reach. It returns the line to log about the check
// and, on a definite mismatch, the error that stops the process.
//
// Nothing else ties the two together. --project, --cluster-location and
// --cluster-name decide which records the join will serve, and the GET then
// goes to whatever --kubeconfig's current context or the Pod's ServiceAccount
// points at; matches() never learns which cluster that is. So a Pod running in
// us-east4 with flags copied from the us-central1 manifest, or a local run
// against the wrong kubectl context, passes every startup check, announces the
// cluster it was told about, and enriches one cluster's audit records with the
// other's objects -- a real object of that name, real managedFields, a real
// reconciled_by=. That is the exact failure the three-part identity exists to
// prevent, reached from the other end: the triple is right about which records
// to serve and wrong about where to serve them from.
//
// Fatal, where ackDeadlinePreflight is advisory, because of what each costs. An
// overrunning join budget costs redelivery and is visible as duplicates; this
// costs correctness and is visible as nothing at all. Every lookup succeeds,
// so no outcome is counted failed or unreachable and no line is logged --
// the counts an operator would check all read healthy.
//
// A probe that cannot answer is not a mismatch and stops nothing. A kind
// cluster publishes no GKE node attributes and a hand-written kubeconfig has no
// gcloud-shaped context name, so refusing to start on an unreadable identity
// would break the local runs --kubeconfig exists for. Those get the line and
// the run continues on trust.
func verifyClusterIdentity(ctx context.Context, declared clusterIdentity, observe func(context.Context) (clusterIdentity, error)) (string, error) {
	probeCtx, cancel := context.WithTimeout(ctx, clusterIdentityProbeTimeout)
	defer cancel()

	observed, err := observe(probeCtx)
	if err != nil {
		return fmt.Sprintf("could not confirm which cluster the credentials reach (%v); %q is taken on trust, and records enriched from the wrong cluster would not be counted", err, declared), nil
	}
	if observed != declared {
		return "", fmt.Errorf("the credentials reach cluster %q, but --project, --cluster-location and --cluster-name declare %q: refusing to enrich one cluster's audit records with another cluster's objects", observed, declared)
	}
	return "", nil
}

// observeCluster reports the cluster the join's credentials actually reach, as
// opposed to the one the flags declare.
//
// Two sources, because the two credential modes leave different evidence. A Pod
// reads its own cluster and the node it runs on publishes that cluster's name
// and location, so the in-cluster answer is exact. A kubeconfig has no such
// channel, and the best available is the context name gcloud writes, which
// carries all three parts and is misleading only if someone renamed a context
// to another cluster's gcloud name.
//
// An error means "not established", never "mismatch". verifyClusterIdentity
// depends on that distinction: it stops the process on the second and logs the
// first.
func observeCluster(ctx context.Context, kubeconfig string, inCluster bool) (clusterIdentity, error) {
	if inCluster {
		project, err := metadata.ProjectIDWithContext(ctx)
		if err != nil {
			return clusterIdentity{}, fmt.Errorf("metadata project id: %w", err)
		}
		cluster, err := metadata.GetWithContext(ctx, metadataClusterNameAttribute)
		if err != nil {
			return clusterIdentity{}, fmt.Errorf("metadata %s: %w", metadataClusterNameAttribute, err)
		}
		location, err := metadata.GetWithContext(ctx, metadataClusterLocationAttribute)
		if err != nil {
			return clusterIdentity{}, fmt.Errorf("metadata %s: %w", metadataClusterLocationAttribute, err)
		}
		return clusterIdentity{Project: project, Location: location, Cluster: cluster}, nil
	}

	// LoadFromFile rather than the loading rules, to match newObjectGetter:
	// BuildConfigFromFlags with an explicit path reads that file's
	// current-context and ignores $KUBECONFIG merging, so reading the same file
	// the same way is what makes this the context the join will actually use.
	cfg, err := clientcmd.LoadFromFile(kubeconfig)
	if err != nil {
		return clusterIdentity{}, fmt.Errorf("kubeconfig %q: %w", kubeconfig, err)
	}
	return parseGKEContextName(cfg.CurrentContext)
}

// parseGKEContextName reads a cluster identity out of a gcloud-written context
// name, and reports an error for any name that is not one.
func parseGKEContextName(name string) (clusterIdentity, error) {
	parts := strings.Split(name, gkeContextSeparator)
	if len(parts) != gkeContextFields || parts[0] != gkeContextPrefix {
		return clusterIdentity{}, fmt.Errorf("current context %q is not a gke_<project>_<location>_<cluster> name", name)
	}
	return clusterIdentity{Project: parts[1], Location: parts[2], Cluster: parts[3]}, nil
}

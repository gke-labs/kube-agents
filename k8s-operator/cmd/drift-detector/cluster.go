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
	"log"
	"strings"
	"time"

	"cloud.google.com/go/compute/metadata"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/clusterprofiles"
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

	// joinClientQPS and joinClientBurst lift client-go's client-side throttle
	// out of the way of --batch-join-budget, which is meant to be the only
	// bound on a batch's lookups.
	//
	// A rest.Config with QPS left at zero does not mean "unlimited": client-go
	// substitutes DefaultQPS=5 and DefaultBurst=10 (rest/config.go), and every
	// request then waits on that token bucket in Request.tryThrottle before any
	// network I/O. The join issues one GET per forwarded record, sequentially,
	// inside one budget shared by the whole batch, so the eleventh human record
	// in a batch waits 200ms, the hundredth waits eighteen seconds, and none of
	// that is the cluster being slow. It is also invisible once it bites: a
	// budget that expires while throttled surfaces as a bare "context deadline
	// exceeded", because client-go deliberately does not wrap context errors,
	// which is exactly what a slow control plane looks like.
	//
	// Sized to maxMessagesCeiling rather than to a number that seemed large
	// enough, so the relationship is the point: a batch cannot hold more records
	// than the pull API will return, so a whole maximum batch is issued without
	// the throttle ever waiting, and it cannot become the binding constraint
	// before the budget does. Raising the ceiling raises this with it. The cap
	// stays finite rather than being disabled with QPS=-1, so a future change
	// that makes these lookups concurrent still meets a client-side limit.
	//
	// Concurrency is not what this protects against today: the loop is
	// sequential, one request in flight, each bounded by joinRequestTimeout and
	// all of them by the budget.
	joinClientQPS   float32 = maxMessagesCeiling
	joinClientBurst         = maxMessagesCeiling
)

// newObjectGetter builds a live-object reader for the one directly reachable
// cluster -- the pod's own, or whichever a local run's --kubeconfig names.
//
// This is one of the join's two credential sources and the narrower one.
// --profiles-dir contributes the rest of the fleet through
// discoverProfileClusters below; the two are additive, and buildClusterSet
// merges them. The direct cluster is kept as its own source rather than being
// left to the profile scan because it has to be readable from the first second
// of a fresh install, before cluster_agent_reconcile.py has given the
// management cluster a profile like every other cluster in the project -- the
// same reason k8s-event-watcher keeps --in-cluster alongside --profiles-dir.
//
// Its credentials are also not equivalent to a profile's. This reaches
// kubernetes.default.svc as the pod's Kubernetes service account and never
// leaves the cluster; a profile authenticates as the pod's Google identity
// against the control-plane endpoint, which an IAM set without
// roles/container.viewer or a master authorized network excluding the pod's
// egress can refuse. So when both name the same cluster, this one wins.
//
// Returns nil with no error when neither flag is set. A detector without
// cluster credentials is a supported mode: it is how the binary has run since
// T1, and the join degrades to a count rather than refusing to start.
func newObjectGetter(kubeconfig string, inCluster bool) (objectGetter, error) {
	cfg, err := joinRESTConfig(kubeconfig, inCluster)
	if err != nil || cfg == nil {
		return nil, err
	}

	client, err := dynamic.NewForConfig(cfg)
	if err != nil {
		return nil, fmt.Errorf("dynamic client: %w", err)
	}
	return dynamicGetter{client: client}, nil
}

// joinRESTConfig builds the client configuration the join's lookups go out on,
// or nil when neither credential source is configured.
//
// Split out of newObjectGetter so the throttle settings can be asserted without
// a cluster: dynamic.NewForConfig copies the config into a client and gives no
// way back to it, so a test that can only see the getter cannot tell a config
// carrying joinClientQPS from one left at client-go's default.
func joinRESTConfig(kubeconfig string, inCluster bool) (*rest.Config, error) {
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

	applyJoinThrottle(cfg)
	return cfg, nil
}

// applyJoinThrottle lifts client-go's client-side rate limit out of the way of
// --batch-join-budget on one config, whichever credential source produced it.
//
// A function rather than two assignments at each site because the profile
// clusters get their configs from internal/clusterprofiles, which knows nothing
// about this binary's budget and leaves QPS at zero -- and zero is not
// "unlimited" but client-go's DefaultQPS=5, as joinClientQPS explains at
// length. Missing it on the profile path would throttle exactly the clusters
// the fan-in added, and do it invisibly: the budget expires mid-queue and the
// records come back as a bare "context deadline exceeded", indistinguishable
// from a slow control plane.
func applyJoinThrottle(cfg *rest.Config) {
	cfg.QPS = joinClientQPS
	cfg.Burst = joinClientBurst
}

// profilesDiscovery is the profile scan discoverProfileClusters runs. Its zero
// value reaches the real GKE API with this process's own Google credentials;
// the tests replace its Describe and TokenSource fields so the scan needs
// neither. OnSkip is set per call instead, because the message it writes is
// this binary's rather than the package's.
//
// The same seam k8s-event-watcher uses, deliberately: two binaries scanning the
// same directory the same way is the reason the scan is a package.
var profilesDiscovery clusterprofiles.Discoverer

// profileCluster is one fleet cluster the join can read, discovered from a
// Cluster Agent profile rather than from a credential flag.
type profileCluster struct {
	Identity clusterIdentity
	Profile  string
	Getter   objectGetter

	// Config is what Getter reads on, kept for the same reason joinRESTConfig is
	// split out of newObjectGetter: dynamic.NewForConfig copies the config into a
	// client and offers no way back to it, so a test holding only the getter
	// cannot tell a throttled config from one left at client-go's DefaultQPS=5.
	// That default is silent when it bites -- see joinClientQPS -- so it is worth
	// a field to be able to assert it is gone.
	Config *rest.Config
}

// identityFromProfile converts a discovered identity into the form an audit
// record is matched against.
//
// Field by field rather than positionally, and not a type conversion: the two
// structs carry the same three strings in a different order
// (clusterprofiles.Identity is Project, Cluster, Location), so a conversion
// would not compile but a positional composite literal would -- and would read
// every record's location against a cluster name. This is the transposition
// hazard clusterIdentity's own doc comment exists for, arriving between two
// packages instead of between three parameters.
func identityFromProfile(id clusterprofiles.Identity) clusterIdentity {
	return clusterIdentity{
		Project:  id.Project,
		Location: id.Location,
		Cluster:  id.Cluster,
	}
}

// discoverProfileClusters turns a Hermes profiles directory into one live-object
// reader per Cluster Agent profile, dropping the clusters whose records this
// subscription will never carry.
//
// project is --project, and the filter is not an optimisation. The subscription
// is a project-level sink, so every record on it names a cluster in that
// project; a profile for a cluster elsewhere -- which the Platform Agent
// legitimately creates, since a fleet can span projects -- produces a client no
// record can ever match. The drop goes through Discoverer.Want, on the identity
// and before the cluster is addressed, because dropping it afterwards is not
// free: Discover asks the GKE API where every cluster is before it returns, so a
// foreign cluster would cost a describe call into a project this detector is
// about to discard -- and where the pod's Google identity has no
// container.clusters.get there, that call fails and the profile is reported as a
// GKE permission error instead, sending an operator to grant access to a cluster
// that was going to be dropped regardless. What the drop buys is keeping a real
// misconfiguration visible -- an operator who pointed --project at the wrong
// project sees "8 clusters skipped, outside project" instead of a detector that
// starts cleanly and enriches nothing.
//
// Failure policy is internal/clusterprofiles's, and it is the watcher's: a
// missing directory is fatal because discovery runs once and a restart fixes
// it, an unreadable one degrades, and a single bad profile is skipped so that
// one unparseable config.yaml cannot cost the whole fleet. What this adds is
// the second half of that last rule -- a cluster whose dynamic client will not
// build is skipped too, for the same reason.
//
// Every skip is logged and counted. The count is what the startup line reports,
// because a fan-in that silently reached six of seven clusters looks exactly
// like a fleet with six clusters in it.
//
// An empty dir is --profiles-dir unset, and returns nothing without scanning.
// The check is here rather than at the call site because it is the same
// mistake, not a caller's convenience: Discover calls os.ReadDir, "" is ENOENT,
// and ENOENT is the one condition the package treats as fatal -- so a detector
// that simply did not ask for the fan-in would refuse to start.
func discoverProfileClusters(ctx context.Context, dir, project string) ([]profileCluster, int, error) {
	if dir == "" {
		return nil, 0, nil
	}

	skipped := 0
	skip := func(profile string, err error) {
		if profile == clusterprofiles.NoProfile {
			// The directory itself, not a profile in it -- so this is not one
			// straggler but an unknown number of clusters, and counting it as
			// "1 profile skipped" would understate it to whoever alerts on that
			// number. The error already says which directory and what it means.
			log.Printf("%s: %v", commandName, err)
			return
		}
		skipped++
		log.Printf("%s: skipping profile %s, its cluster will NOT be joined: %v", commandName, profile, err)
	}

	// Counted with the rest, because an operator who mistyped --project needs
	// this to show up in the skip total rather than as a fleet that was always
	// this small. Discover calls Want once per profile, in order, on the one
	// goroutine this runs on, so sharing the counter with skip is safe. The
	// identity is named rather than the profile directory, which Want is not
	// given -- and which the Platform Agent derives from the triple anyway.
	want := func(id clusterprofiles.Identity) bool {
		if id.Project == project {
			return true
		}
		skipped++
		log.Printf("%s: skipping the profile for cluster %s: it is outside --project=%s, and this subscription carries no records from it",
			commandName, identityFromProfile(id), project)
		return false
	}

	// A copy, so setting these does not write to the package-level seam.
	d := profilesDiscovery
	d.OnSkip = skip
	d.Want = want
	discovered, err := d.Discover(ctx, dir)
	if err != nil {
		return nil, skipped, err
	}

	clusters := make([]profileCluster, 0, len(discovered))
	for _, c := range discovered {
		identity := identityFromProfile(c.Identity)
		applyJoinThrottle(c.Config)
		client, err := dynamic.NewForConfig(c.Config)
		if err != nil {
			skip(c.Profile, fmt.Errorf("dynamic client: %w", err))
			continue
		}
		clusters = append(clusters, profileCluster{
			Identity: identity,
			Profile:  c.Profile,
			Getter:   dynamicGetter{client: client},
			Config:   c.Config,
		})
	}
	return clusters, skipped, nil
}

// buildClusterSet merges the two credential sources into the join's routing
// table, and reports which profiles it dropped as duplicates of the direct
// cluster.
//
// Additive, like k8s-event-watcher's watch set: --profiles-dir contributes the
// fleet and --in-cluster/--kubeconfig contributes one more. They overlap on the
// cluster the pod runs in, because reconcile gives the management cluster a
// profile like any other, and the direct entry is the one kept -- newObjectGetter's
// comment has the argument for which credential is the safer of the two.
//
// The duplicate matters less here than it does in the watcher, where two
// entries meant two dedup caches and two alerts for one event. A second getter
// for one cluster would just be a second way to read the same object. It is
// still dropped rather than allowed to win, because which one a map ends up
// holding would otherwise depend on insertion order, and that is not a thing to
// leave to the order a directory happened to be read in.
//
// direct is nil when neither credential flag is set, which is legal: a
// profiles-only detector is the fleet-wide mode, and a set with nothing in it
// at all is the no-credentials mode the join has supported since T1.
func buildClusterSet(direct objectGetter, directIdentity clusterIdentity, profiles []profileCluster) (map[clusterIdentity]objectGetter, []string) {
	set := make(map[clusterIdentity]objectGetter, len(profiles)+1)
	if direct != nil {
		set[directIdentity] = direct
	}

	var absorbed []string
	for _, p := range profiles {
		if _, dup := set[p.Identity]; dup {
			absorbed = append(absorbed, p.Profile)
			continue
		}
		set[p.Identity] = p.Getter
	}
	return set, absorbed
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

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

package controller

import (
	"context"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// managedOTelEndpoint is the OTLP/HTTP endpoint of the GKE Managed OpenTelemetry
	// collector. It is the last rung of the resolution ladder: the value used when
	// nothing is configured and no collector is discovered. The same endpoint is used by
	// the LiteLLM integration, so agent traces and LLM-call telemetry land in the same
	// place (Cloud Trace/Logging).
	managedOTelEndpoint = "http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318"

	// otelEndpointEnvVar is the operator-level override, set on the controller-manager
	// Deployment for installs whose collector is the same for every agent. Deliberately
	// not named OTEL_EXPORTER_OTLP_ENDPOINT: that name would collide if the
	// controller-manager itself is ever instrumented with OpenTelemetry.
	otelEndpointEnvVar = "OTEL_COLLECTOR_ENDPOINT"

	// otelDiscoveryEnvVar set to "false" disables the discovery probe entirely, for
	// installs that would rather fall straight through to the managed default than have
	// the operator read Services outside its own namespace.
	otelDiscoveryEnvVar = "OTEL_COLLECTOR_DISCOVERY"

	// otelDiscoveryTTL bounds how long a probe result is reused. Unlike the cluster's
	// ImageVolume capability, which cannot change without an API server restart, a
	// collector Service can appear or move at any time — so this cache expires.
	otelDiscoveryTTL = 5 * time.Minute

	// otelProbeRetryAfter is the floor between probe attempts. An inconclusive probe
	// caches nothing — deliberately, since the next one may succeed — so without a floor
	// an API outage or a narrowed RBAC has every reconcile of every agent re-run six Gets
	// and three Lists against the API server that is already struggling. Well under
	// otelDiscoveryTTL, so it never delays an ordinary refresh: by the time the TTL has
	// expired this window is long gone.
	otelProbeRetryAfter = 30 * time.Second

	// otelRediscoverAfter re-queues an agent that resolved to the bare default. That is
	// the only outcome that can silently improve on its own, and reconciles are
	// event-driven, so without a nudge a collector installed later might not be picked up
	// for hours.
	otelRediscoverAfter = 15 * time.Minute

	// otlpHTTPPort is the conventional OTLP/HTTP receiver port.
	otlpHTTPPort = 4318
)

// How the endpoint was chosen, reported on .status.telemetry.otlpEndpointSource.
const (
	otlpSourceDeploymentEnv = "DeploymentEnv"
	otlpSourceSpec          = "Spec"
	otlpSourceOperatorEnv   = "OperatorEnv"
	otlpSourceDiscovered    = "Discovered"
	otlpSourceDefault       = "Default"
)

// collectorCandidate is a Service the probe looks for by name before falling back to
// label matching.
type collectorCandidate struct {
	Namespace string
	Name      string
}

// wellKnownCollectors are probed in order, each as a single Get. A miss is cheap; a
// cluster-wide List is not, so this list is tried before any label search.
//
// The GKE managed collector is deliberately first: on a cluster that has it, discovery
// returns exactly the endpoint that used to be hardcoded, which makes this whole feature
// a provable no-op there.
var wellKnownCollectors = []collectorCandidate{
	{Namespace: "gke-managed-otel", Name: "opentelemetry-collector"},
	{Namespace: "otel-collector", Name: "otel-collector"},
	{Namespace: "opentelemetry", Name: "opentelemetry-collector"},
	{Namespace: "opentelemetry-operator-system", Name: "otel-collector"},
	{Namespace: "observability", Name: "otel-collector"},
	{Namespace: "monitoring", Name: "otel-collector"},
}

// collectorLabelSelectors are tried in order once every well-known name has missed. The
// first selector that yields a qualifying Service wins; later selectors are not consulted.
var collectorLabelSelectors = []client.MatchingLabels{
	{"app.kubernetes.io/name": "opentelemetry-collector"},
	{"app.kubernetes.io/component": "opentelemetry-collector"},
	{"app": "opentelemetry-collector"},
}

// otlpHTTPEndpointForService returns the OTLP/HTTP base URL for svc, and whether svc is
// usable at all.
//
// Only HTTP is accepted. A gRPC-only collector (port 4317) is rejected rather than
// selected: every exporter here sends OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf and
// hermes_otel POSTs to /v1/traces, so wiring an agent to 4317 produces an exporter that
// fails on every span while looking correctly configured. Falling through to the default
// is the more debuggable failure.
func otlpHTTPEndpointForService(svc *corev1.Service) (string, bool) {
	// An ExternalName Service carries no ports and resolves to a CNAME; there is nothing
	// here to build a cluster-local URL from.
	if svc == nil || svc.Spec.Type == corev1.ServiceTypeExternalName {
		return "", false
	}

	// Precedence: a port named otlp-http, else one named http-otlp, else the
	// conventional 4318. Names win over the number because a collector that renames its
	// receiver has told us which port it means.
	var byPreferredName, byAlternateName, byNumber int32
	for _, p := range svc.Spec.Ports {
		// An empty protocol means TCP, per the core API defaulting.
		if p.Protocol != "" && p.Protocol != corev1.ProtocolTCP {
			continue
		}
		switch {
		case p.Name == "otlp-http" && byPreferredName == 0:
			byPreferredName = p.Port
		case p.Name == "http-otlp" && byAlternateName == 0:
			byAlternateName = p.Port
		case p.Port == otlpHTTPPort && byNumber == 0:
			byNumber = p.Port
		}
	}

	port := byPreferredName
	if port == 0 {
		port = byAlternateName
	}
	if port == 0 {
		port = byNumber
	}
	if port == 0 {
		return "", false
	}
	return fmt.Sprintf("http://%s.%s.svc.cluster.local:%d", svc.Name, svc.Namespace, port), true
}

// discoverCollectorEndpoint probes the cluster for an OTLP/HTTP collector.
//
// determined reports whether the answer is authoritative. ("", true) means "probed, found
// nothing" — a real answer, and the common case on a cluster without managed OTel.
// ("", false) means the probe could not complete, and the caller must not remember it:
// the next probe may succeed. This mirrors clusterImageVolumeSupport's contract.
func discoverCollectorEndpoint(ctx context.Context, reader client.Reader) (string, bool) {
	log := logf.FromContext(ctx).WithName("otel-discovery")

	if reader == nil {
		log.Info("No client available to discover an OpenTelemetry collector; using the default endpoint")
		return "", false
	}

	for _, candidate := range wellKnownCollectors {
		svc := &corev1.Service{}
		key := client.ObjectKey{Namespace: candidate.Namespace, Name: candidate.Name}
		if err := reader.Get(ctx, key, svc); err != nil {
			if client.IgnoreNotFound(err) == nil {
				continue
			}
			// A non-NotFound error means we cannot trust "not found" for the candidates
			// we have not reached yet, so the whole probe is inconclusive.
			log.Error(err, "Failed to probe for an OpenTelemetry collector Service",
				"namespace", candidate.Namespace, "name", candidate.Name)
			return "", false
		}
		if endpoint, ok := otlpHTTPEndpointForService(svc); ok {
			log.Info("Discovered an OpenTelemetry collector", "endpoint", endpoint, "match", "well-known-name")
			return endpoint, true
		}
		log.Info("Skipping collector Service with no usable OTLP/HTTP port (4317-only collectors are not supported)",
			"namespace", svc.Namespace, "name", svc.Name)
	}

	for _, selector := range collectorLabelSelectors {
		services := &corev1.ServiceList{}
		if err := reader.List(ctx, services, selector); err != nil {
			log.Error(err, "Failed to list Services while discovering an OpenTelemetry collector", "selector", selector)
			return "", false
		}

		type match struct {
			namespace, name, endpoint string
		}
		var matches []match
		for i := range services.Items {
			svc := &services.Items[i]
			endpoint, ok := otlpHTTPEndpointForService(svc)
			if !ok {
				continue
			}
			matches = append(matches, match{svc.Namespace, svc.Name, endpoint})
		}
		if len(matches) == 0 {
			continue
		}

		// Sort before picking. A non-deterministic choice would rewrite the agent
		// Deployment's env on alternating reconciles and roll the pod forever.
		sort.Slice(matches, func(i, j int) bool {
			if matches[i].namespace != matches[j].namespace {
				return matches[i].namespace < matches[j].namespace
			}
			return matches[i].name < matches[j].name
		})

		if len(matches) > 1 {
			runnersUp := make([]string, 0, len(matches)-1)
			for _, m := range matches[1:] {
				runnersUp = append(runnersUp, m.namespace+"/"+m.name)
			}
			log.Info("Several Services match the collector labels; taking the first by (namespace, name). "+
				"Set spec.telemetry.otlpEndpoint to choose a different one.",
				"selector", selector,
				"chosen", matches[0].namespace+"/"+matches[0].name,
				"notChosen", strings.Join(runnersUp, ","))
		}
		log.Info("Discovered an OpenTelemetry collector", "endpoint", matches[0].endpoint, "match", "labels")
		return matches[0].endpoint, true
	}

	log.Info("No in-cluster OpenTelemetry collector found; using the default endpoint", "endpoint", managedOTelEndpoint)
	return "", true
}

// telemetryReader returns the client discovery should read through.
//
// APIReader goes straight to the API server. Discovery looks at Services in namespaces
// this operator otherwise never touches, and a cached Get there would have the manager
// start — and keep — an informer watching every Service in the cluster, for a handful of
// reads an hour. Nil falls back to the cached client, which is what tests supply.
func (r *PlatformAgentReconciler) telemetryReader() client.Reader {
	if r.APIReader != nil {
		return r.APIReader
	}
	return r.Client
}

// discoveredOTLPEndpoint returns the discovered collector endpoint, or "" when none was
// found, reusing a recent probe.
//
// Both outcomes are cached, including "found nothing" — that is an authoritative answer
// and the common case, and re-probing six namespaces on every reconcile of every agent to
// re-learn it would be wasteful. An inconclusive probe is never cached; it falls back to
// lastKnown, since flapping to the default would roll the agent pod over a transient API
// error.
//
// lastKnown is the endpoint this agent's status already reports as discovered — see
// resolveOTLPEndpoint. It matters only on the path the in-memory cache cannot cover: an
// operator restart empties the cache, so without it a single API error on the very first
// probe after a restart resolves to the default, rolls every agent pod onto a collector
// that may not exist, and rolls them all back when the next probe succeeds.
func (r *PlatformAgentReconciler) discoveredOTLPEndpoint(ctx context.Context, lastKnown string) string {
	if strings.EqualFold(strings.TrimSpace(os.Getenv(otelDiscoveryEnvVar)), "false") {
		return ""
	}

	r.otelMu.Lock()
	defer r.otelMu.Unlock()

	if r.otelResolved && time.Since(r.otelResolvedAt) < otelDiscoveryTTL {
		return r.otelEndpoint
	}

	// Reaching here means nothing is cached or the TTL expired, so a probe is due —
	// unless one was just attempted and got nowhere. Only consecutive failures can
	// trip this: a success stamps otelResolvedAt too, and the TTL above is far longer
	// than this floor.
	if !r.otelProbedAt.IsZero() && time.Since(r.otelProbedAt) < otelProbeRetryAfter {
		if r.otelEndpoint != "" {
			return r.otelEndpoint
		}
		return lastKnown
	}
	r.otelProbedAt = time.Now()

	endpoint, determined := discoverCollectorEndpoint(ctx, r.telemetryReader())
	if !determined {
		// Keep serving the last known good answer rather than flapping to the default.
		// The in-memory value wins when there is one: it came from a probe, whereas
		// lastKnown is a per-agent status field. Deliberately neither cached nor
		// promoted into r.otelEndpoint — the cache is cluster-wide and one agent's
		// recorded endpoint is not an authoritative answer for the rest of the fleet.
		if r.otelEndpoint != "" {
			return r.otelEndpoint
		}
		return lastKnown
	}

	r.otelEndpoint = endpoint
	r.otelResolved = true
	r.otelResolvedAt = time.Now()
	return endpoint
}

// resolveOTLPEndpoint decides where this agent's telemetry goes, and reports how.
//
// The ladder, highest first:
//
//	spec.deployment.env[OTEL_EXPORTER_OTLP_ENDPOINT]  the pre-existing escape hatch
//	spec.telemetry.otlpEndpoint                       the first-class field
//	OTEL_COLLECTOR_ENDPOINT on the controller-manager an install-wide default
//	in-cluster discovery                              a collector we can find
//	managedOTelEndpoint                               the GKE managed collector
//
// The raw env override sits at the top because it already worked that way — mergeEnvVars
// applies spec.deployment.env after the operator's own values — and demoting it would
// silently redirect telemetry for anyone already relying on it. Any explicit rung short-
// circuits discovery, so a configured install makes no extra API calls at all.
func (r *PlatformAgentReconciler) resolveOTLPEndpoint(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (string, string) {
	if agent != nil && agent.Spec.Deployment != nil {
		for _, env := range agent.Spec.Deployment.Env {
			if env.Name == "OTEL_EXPORTER_OTLP_ENDPOINT" && env.Value != "" {
				return env.Value, otlpSourceDeploymentEnv
			}
		}
	}

	if agent != nil && agent.Spec.Telemetry != nil {
		if endpoint := strings.TrimSpace(agent.Spec.Telemetry.OTLPEndpoint); endpoint != "" {
			return endpoint, otlpSourceSpec
		}
	}

	if endpoint := strings.TrimSpace(os.Getenv(otelEndpointEnvVar)); endpoint != "" {
		return endpoint, otlpSourceOperatorEnv
	}

	// Only a previously *discovered* endpoint is offered back as the last known good one.
	// Status also records endpoints that came from the rungs above, and replaying one of
	// those here would launder a removed override into a discovery result.
	var lastKnown string
	if agent != nil && agent.Status.Telemetry.OTLPEndpointSource == otlpSourceDiscovered {
		lastKnown = agent.Status.Telemetry.OTLPEndpoint
	}

	if endpoint := r.discoveredOTLPEndpoint(ctx, lastKnown); endpoint != "" {
		return endpoint, otlpSourceDiscovered
	}

	return managedOTelEndpoint, otlpSourceDefault
}

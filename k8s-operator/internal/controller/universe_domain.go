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
	"io"
	"net/http"
	"strings"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// gceUniverseDomainMetadataURL is the GCE/GKE metadata server endpoint that
	// answers with the project's API universe domain (for example,
	// "googleapis.com" on standard Google Cloud, or a partner/sovereign domain
	// on Google Cloud Dedicated).
	gceUniverseDomainMetadataURL = "http://" + metadataLinkLocalIP + "/computeMetadata/v1/universe/universe-domain"
	gceMetadataFlavorHeader      = "Metadata-Flavor"
	gceMetadataFlavorGoogle      = "Google"
	gceMetadataProbeTimeout      = 2 * time.Second
	gceMetadataMaxBodyBytes      = 512

	universeDomainSourceSpec       = "Spec"
	universeDomainSourceDiscovered = "Discovered"

	// reasonUniverseDomainMismatch parks Ready False / Phase Degraded before
	// rendering workloads when spec.harness.universeDomain disagrees with the
	// host cluster's metadata server.
	reasonUniverseDomainMismatch = "UniverseDomainMismatch"
	universeDomainMismatchMsgFmt = "spec.harness.universeDomain is %q, while the host cluster's GKE metadata server reports %q. " +
		"One PlatformAgent spans a single Google Cloud universe domain and will not send tokens across universe boundaries. " +
		"Set spec.harness.universeDomain to %q (or omit it to auto-detect from the host cluster)."

	// externalModelEgressConditionType is an informational condition written
	// when a PlatformAgent in a non-default universe domain routes model calls
	// to an external SaaS provider rather than an in-universe endpoint
	// (openai_compatible or vertex_ai).
	externalModelEgressConditionType = "ExternalModelEgress"
	externalModelEgressReason        = "ExternalSaaSProviderInCustomUniverse"
	externalModelEgressMsgFmt        = "This PlatformAgent runs in custom universe domain %q while LiteLLM's default model uses external SaaS provider %q. " +
		"Prompts, cluster telemetry excerpts, and tool results leave the sovereign/partner universe boundary to reach %q. " +
		"Use an in-universe model endpoint (openai_compatible or in-universe vertex_ai) to keep inference inside %q."

	// universeDomainNewerField names status.resolvedUniverseDomain and
	// status.universeDomainSource in prunedNewerFields when an operator runs
	// ahead of its served PlatformAgent CRD.
	universeDomainNewerField = "status.resolvedUniverseDomain"

	kubeProxyUniverseDomainEnv        = agentv1alpha1.KubeProxyUniverseDomainEnv
	universeDomainMetadataRetryWindow = 5 * time.Second

	apisUniverseDomainPrefix    = "apis-"
	gkeSingleWildcardHostPrefix = "*.gke-"
	gkeDoubleWildcardHostPrefix = "*.*.gke-"
	pkgBareHostPrefix           = "pkg-"
	pkgSingleWildcardHostPrefix = "*.pkg-"
)

var (
	// inUniverseInferenceProviders lists the LiteLLM provider slugs that keep
	// model traffic inside the custom universe (self-hosted OpenAI-compatible
	// endpoints and in-universe Vertex AI). Any other non-empty provider slug
	// (gemini, anthropic, openai, sap, bedrock, azure, etc.) sends prompts
	// outside the sovereign universe boundary and raises ExternalModelEgress.
	inUniverseInferenceProviders = map[string]bool{
		"vertex_ai":         true,
		"openai_compatible": true,
		"hosted_vllm":       true,
		"vllm":              true,
		"ollama":            true,
		"custom":            true,
	}
)

// ProbeGCEUniverseDomain queries the GCE/GKE metadata server for the host
// cluster's universe domain.
//
// HTTP 200 with a valid DNS hostname returns (domain, true). HTTP 404 (for
// example on kind or an older metadata emulator without the universe route)
// returns ("", true) so the operator caches the absence and stops probing.
// Network errors, timeouts, and 5xx responses return ("", false) so a
// transient node-local metadata hiccup is retried on a later reconcile rather
// than cached as commercial Google Cloud.
func ProbeGCEUniverseDomain(ctx context.Context) (string, bool) {
	return probeUniverseDomainURL(ctx, gceUniverseDomainMetadataURL, nil)
}

func probeUniverseDomainURL(ctx context.Context, targetURL string, httpClient *http.Client) (string, bool) {
	probeCtx, cancel := context.WithTimeout(ctx, gceMetadataProbeTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(probeCtx, http.MethodGet, targetURL, nil)
	if err != nil {
		return "", false
	}
	req.Header.Set(gceMetadataFlavorHeader, gceMetadataFlavorGoogle)

	client := httpClient
	if client == nil {
		client = &http.Client{Timeout: gceMetadataProbeTimeout}
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", false
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode == http.StatusNotFound {
		return "", true
	}
	if resp.StatusCode != http.StatusOK {
		return "", false
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, gceMetadataMaxBodyBytes))
	if err != nil {
		return "", false
	}
	candidate, ok := agentv1alpha1.NormalizeUniverseDomain(string(raw))
	if !ok {
		return "", false
	}
	return candidate, true
}

// hostUniverseDomain returns the host cluster's metadata-reported universe
// domain and whether that answer is authoritative, caching an authoritative
// answer for the lifetime of the operator process (a GKE node cannot move
// between universes). When UniverseDomainDetector is nil (offline unit and
// golden tests), it returns ("", true) without opening a socket to
// 169.254.169.254.
func (r *PlatformAgentReconciler) hostUniverseDomain(ctx context.Context) (string, bool) {
	if r == nil || r.UniverseDomainDetector == nil {
		return "", true
	}
	r.universeDomainMu.Lock()
	defer r.universeDomainMu.Unlock()
	if r.universeDomainResolved {
		return r.discoveredUniverseDomain, true
	}
	domain, determined := r.UniverseDomainDetector(ctx)
	if determined {
		r.discoveredUniverseDomain = domain
		r.universeDomainResolved = true
		if domain != "" {
			logf.FromContext(ctx).Info("Discovered Google Cloud universe domain from metadata server", "universeDomain", domain)
		}
	}
	return domain, determined
}

// validateUniverseDomain checks that spec.harness.universeDomain, when
// explicitly set, does not contradict the host cluster's GKE metadata server,
// and is a well-formed DNS domain even when admission webhooks are off.
// When the metadata probe fails transiently on GKE, transient=true asks the
// caller to requeue rather than rendering cross-universe workloads unverified.
func (r *PlatformAgentReconciler) validateUniverseDomain(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (reason, message string, transient bool) {
	if agent == nil || agent.Spec.Harness == nil {
		return "", "", false
	}
	specDomain := strings.TrimSpace(agent.Spec.Harness.UniverseDomain)
	if specDomain == "" {
		if harnessOnKind(agent.Spec.Harness) || strings.TrimSpace(agent.Status.ResolvedUniverseDomain) != "" {
			return "", "", false
		}
		if _, determined := r.hostUniverseDomain(ctx); !determined {
			return "", "", true
		}
		return "", "", false
	}
	if !agentv1alpha1.IsValidUniverseDomain(specDomain) {
		return reasonUniverseDomainMismatch, fmt.Sprintf(
			"spec.harness.universeDomain %q is not a valid lower-case DNS domain (for example %q).",
			specDomain, agentv1alpha1.DefaultUniverseDomain,
		), false
	}
	if harnessOnKind(agent.Spec.Harness) {
		return "", "", false
	}
	detected, determined := r.hostUniverseDomain(ctx)
	if !determined {
		return "", "", true
	}
	if detected != "" && specDomain != detected {
		return reasonUniverseDomainMismatch, fmt.Sprintf(universeDomainMismatchMsgFmt, specDomain, detected, detected), false
	}
	return "", "", false
}

// resolvedUniverseDomainPair returns the (domain, source) pair to record on
// PlatformAgentStatus. Absent ("", "") when spec.harness.universeDomain is
// unset and no metadata detector answered (for example on kind or in offline
// unit tests). When the metadata server fails transiently on a live install
// that already recorded a discovered universe domain, the prior status pair is
// preserved so workloads are never rolled back onto commercial googleapis.com.
func (r *PlatformAgentReconciler) resolvedUniverseDomainPair(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (domain, source string) {
	if agent != nil && agent.Spec.Harness != nil {
		if specDomain := strings.TrimSpace(agent.Spec.Harness.UniverseDomain); specDomain != "" {
			return specDomain, universeDomainSourceSpec
		}
		if harnessOnKind(agent.Spec.Harness) {
			return "", ""
		}
	}
	detected, determined := r.hostUniverseDomain(ctx)
	if detected != "" {
		return detected, universeDomainSourceDiscovered
	}
	if !determined && agent != nil {
		if existing := strings.TrimSpace(agent.Status.ResolvedUniverseDomain); existing != "" {
			existingSource := strings.TrimSpace(agent.Status.UniverseDomainSource)
			if existingSource == "" {
				existingSource = universeDomainSourceDiscovered
			}
			return existing, existingSource
		}
	}
	return "", ""
}

// guardrailAgentForUniverse returns a copy of agent safe for
// reconcileAgentNetworkGuardrails on refusal and requeue branches before step
// 10a, plus skipFQDN=true when no authoritative universe is known yet (a
// cold-start metadata timeout with empty status.resolvedUniverseDomain).
// When spec.harness.universeDomain was refused (mismatch or malformed syntax),
// the rejected spec value is cleared so FQDNNetworkPolicy never renders rules
// for a rejected or unparseable universe domain.
func (r *PlatformAgentReconciler) guardrailAgentForUniverse(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (*agentv1alpha1.PlatformAgent, bool) {
	if agent == nil {
		return nil, false
	}
	copyAgent := agent.DeepCopy()
	reason, _, transient := r.validateUniverseDomain(ctx, copyAgent)
	if transient {
		if copyAgent.Spec.Harness != nil {
			copyAgent.Spec.Harness.UniverseDomain = ""
		}
		if strings.TrimSpace(copyAgent.Status.ResolvedUniverseDomain) != "" {
			return copyAgent, false
		}
		return copyAgent, true
	}
	if reason != "" {
		if copyAgent.Spec.Harness != nil {
			copyAgent.Spec.Harness.UniverseDomain = ""
		}
		if _, _, clearedTransient := r.validateUniverseDomain(ctx, copyAgent); clearedTransient {
			return copyAgent, true
		}
	}
	copyAgent.Status.ResolvedUniverseDomain, copyAgent.Status.UniverseDomainSource = r.resolvedUniverseDomainPair(ctx, copyAgent)
	return copyAgent, false
}

// effectiveUniverseDomain returns the Google Cloud API universe domain that
// manifest builders render against: spec.harness.universeDomain when set, else
// status.resolvedUniverseDomain (stamped on the in-memory copy during
// Reconcile when discovered from the metadata server), else "googleapis.com".
func effectiveUniverseDomain(agent *agentv1alpha1.PlatformAgent) string {
	if agent != nil {
		if agent.Spec.Harness != nil {
			if specDomain := strings.TrimSpace(agent.Spec.Harness.UniverseDomain); specDomain != "" {
				return specDomain
			}
		}
		if statusDomain := strings.TrimSpace(agent.Status.ResolvedUniverseDomain); statusDomain != "" {
			return statusDomain
		}
	}
	return agentv1alpha1.DefaultUniverseDomain
}

// isCustomUniverseDomain reports whether domain is a non-empty universe domain
// other than commercial Google Cloud ("googleapis.com").
func isCustomUniverseDomain(domain string) bool {
	return domain != "" && domain != agentv1alpha1.DefaultUniverseDomain
}

func externalModelEgressMessage(universeDomain string, inference *agentv1alpha1.InferenceStatus) string {
	if !isCustomUniverseDomain(universeDomain) || inference == nil {
		return ""
	}
	provider := strings.ToLower(strings.TrimSpace(inference.Provider))
	if provider == "" || inUniverseInferenceProviders[provider] {
		return ""
	}
	return fmt.Sprintf(externalModelEgressMsgFmt, universeDomain, provider, provider, universeDomain)
}

func externalModelEgressConditionCurrent(agent *agentv1alpha1.PlatformAgent, msg string) bool {
	existing := meta.FindStatusCondition(agent.Status.Conditions, externalModelEgressConditionType)
	if msg == "" {
		return existing == nil
	}
	return existing != nil &&
		existing.Status == metav1.ConditionTrue &&
		existing.Reason == externalModelEgressReason &&
		existing.Message == msg &&
		existing.ObservedGeneration == agent.Generation
}

func setExternalModelEgressCondition(agent *agentv1alpha1.PlatformAgent, msg string, now metav1.Time) {
	if msg == "" {
		meta.RemoveStatusCondition(&agent.Status.Conditions, externalModelEgressConditionType)
		return
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type:               externalModelEgressConditionType,
		Status:             metav1.ConditionTrue,
		Reason:             externalModelEgressReason,
		Message:            msg,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: now,
	})
}

func (r *PlatformAgentReconciler) universeDomainPruned(agent *agentv1alpha1.PlatformAgent) bool {
	r.prunedNewerFieldsMu.Lock()
	defer r.prunedNewerFieldsMu.Unlock()
	recorded, ok := r.prunedNewerFields[usagePollKey(agent)][universeDomainNewerField]
	return ok && time.Since(recorded) < usageStatusReprobeInterval
}

func (r *PlatformAgentReconciler) noteUniverseDomainEcho(ctx context.Context, agent *agentv1alpha1.PlatformAgent, wroteDomain, wroteSource string) {
	if wroteDomain == "" && wroteSource == "" {
		return
	}
	key := usagePollKey(agent)
	r.prunedNewerFieldsMu.Lock()
	defer r.prunedNewerFieldsMu.Unlock()
	if agent.Status.ResolvedUniverseDomain == wroteDomain && agent.Status.UniverseDomainSource == wroteSource {
		delete(r.prunedNewerFields[key], universeDomainNewerField)
		return
	}
	if r.prunedNewerFields == nil {
		r.prunedNewerFields = map[string]map[string]time.Time{}
	}
	if r.prunedNewerFields[key] == nil {
		r.prunedNewerFields[key] = map[string]time.Time{}
	}
	recorded, held := r.prunedNewerFields[key][universeDomainNewerField]
	fresh := held && time.Since(recorded) < usageStatusReprobeInterval
	r.prunedNewerFields[key][universeDomainNewerField] = time.Now()
	if !fresh {
		logf.FromContext(ctx).Info("the served CRD does not serve a status field this operator writes; apply this release's CRD to get it, which is probed again after the interval",
			"platformagent", key, "field", universeDomainNewerField, "reprobeAfter", usageStatusReprobeInterval.String())
	}
}

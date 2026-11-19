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
	"bytes"
	"context"
	"fmt"
	"io"
	"math"
	"math/big"
	"net"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	dto "github.com/prometheus/client_model/go"
	"github.com/prometheus/common/expfmt"
	"github.com/prometheus/common/model"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	sizingStepUpThresholdPct     int64 = 75
	sizingStepDownThresholdPct   int64 = 55
	sizingPercentScale           int64 = 100
	sizingTelemetryWriteDeltaPct int32 = 5

	sizingStepUpSoakDuration      = 5 * time.Minute
	sizingStepDownSoakDuration    = 25 * time.Minute
	sizingTransitionCooldown      = 30 * time.Minute
	sizingUtilizationPollInterval = 100 * time.Second

	sizingMetricsScrapeScheme = "http"
	sizingMetricsScrapePath   = "/metrics"
	sizingMetricsMaxBodyBytes = 4 << 20

	podMetricsAPIGroup   = "metrics.k8s.io"
	podMetricsAPIVersion = "v1beta1"
	podMetricsListKind   = "PodMetricsList"

	sizingDimCPU         = "cpu"
	sizingDimMemory      = "memory"
	sizingDimSlots       = "slots"
	sizingDimChildMemory = "child_memory"
	sizingDimCgroupMem   = "cgroup_memory"
	sizingDimWatermark   = "watermark"

	sizingWorkloadProxy   = "credential-proxy"
	sizingWorkloadSidecar = "agent-api-auth"
	sizingWorkloadShell   = "shell"

	sizingTriggerManual       = "manual"
	sizingTriggerClusterCount = "cluster-count"
	sizingTriggerUtilization  = "utilization"
	sizingTriggerBoth         = "both"

	brokerMetricSlotsInUse       = "kubeagents_credential_proxy_slots_in_use"
	brokerMetricSlotCap          = "kubeagents_credential_proxy_slot_cap"
	brokerMetricChildMemReserved = "kubeagents_credential_proxy_child_memory_reserved_bytes"
	brokerMetricChildMemBudget   = "kubeagents_credential_proxy_child_memory_budget_bytes"
	brokerMetricCgroupMemory     = "kubeagents_credential_proxy_cgroup_memory_bytes"
	brokerMetricWatermarkRatio   = "kubeagents_credential_proxy_resource_watermark_ratio"

	brokerMetricLabelKind      = "kind"
	brokerMetricKindUsage      = "usage"
	brokerMetricKindLimit      = "limit"
	brokerMetricLabelResource  = "resource"
	brokerMetricResourceMemory = "memory"
	brokerMetricResourceState  = "state"
	brokerMetricResourceTmp    = "tmp"

	sizingStatusMaxPct int64 = 1000

	podMetricsContainersKey    = "containers"
	podMetricsContainerNameKey = "name"
	podMetricsUsageKey         = "usage"
	podNameLabelAppKey         = "app"
	k8sNameSeparator           = "-"
	statefulSetPrimaryOrdinal  = "-0"
)

// SizingUtilizationSample carries live PodMetrics and credential-proxy /metrics
// measurements used for proactive auto-sizing promotion and demotion.
type SizingUtilizationSample struct {
	ObservedAt time.Time

	BrokerCPUMilli                int64
	BrokerCPUObserved             bool
	BrokerMemoryBytes             int64
	BrokerMemoryObserved          bool
	BrokerCgroupUsageBytes        int64
	BrokerCgroupLimitBytes        int64
	BrokerCgroupObserved          bool
	BrokerChildMemReservedBytes   int64
	BrokerChildMemBudgetBytes     int64
	BrokerChildMemObserved        bool
	BrokerWatermarkMemoryRatio    float64
	BrokerWatermarkMemoryObserved bool
	BrokerWatermarkStateRatio     float64
	BrokerWatermarkStateObserved  bool
	BrokerWatermarkTmpRatio       float64
	BrokerWatermarkTmpObserved    bool
	BrokerSlotsInUse              int64
	BrokerSlotCap                 int64
	BrokerSlotsObserved           bool

	SidecarCPUMilli       int64
	SidecarCPUObserved    bool
	SidecarMemoryBytes    int64
	SidecarMemoryObserved bool

	ShellCPUMilli       int64
	ShellCPUObserved    bool
	ShellMemoryBytes    int64
	ShellMemoryObserved bool
}

// HasAnyData reports whether any utilization metric was observed in the sample.
func (s SizingUtilizationSample) HasAnyData() bool {
	return s.BrokerCPUObserved || s.BrokerMemoryObserved || s.BrokerCgroupObserved ||
		s.BrokerChildMemObserved || s.BrokerWatermarkMemoryObserved || s.BrokerSlotsObserved ||
		s.SidecarCPUObserved || s.SidecarMemoryObserved ||
		s.ShellCPUObserved || s.ShellMemoryObserved
}

type sizingUtilizationDecision struct {
	DesiredTier  sizingTier
	Trigger      string
	NextStatus   *agentv1alpha1.SizingStatus
	RequeueAfter time.Duration
}

func (r *PlatformAgentReconciler) clockNow() time.Time {
	if r.nowFn != nil {
		return r.nowFn()
	}
	return time.Now().UTC()
}

func (r *PlatformAgentReconciler) sizingSamplesFor(ctx context.Context, agent *agentv1alpha1.PlatformAgent) SizingUtilizationSample {
	if r.UtilizationReader != nil {
		sample, err := r.UtilizationReader(ctx, agent)
		if err != nil {
			return SizingUtilizationSample{ObservedAt: r.clockNow()}
		}
		if sample.ObservedAt.IsZero() {
			sample.ObservedAt = r.clockNow()
		}
		return sample
	}
	return r.readLiveSizingUtilization(ctx, agent)
}

func (r *PlatformAgentReconciler) readLiveSizingUtilization(ctx context.Context, agent *agentv1alpha1.PlatformAgent) SizingUtilizationSample {
	sample := SizingUtilizationSample{ObservedAt: r.clockNow()}
	r.collectPodMetricsSample(ctx, agent, &sample)
	r.collectBrokerMetricsSample(ctx, agent, &sample)
	return sample
}

func (r *PlatformAgentReconciler) collectPodMetricsSample(ctx context.Context, agent *agentv1alpha1.PlatformAgent, sample *SizingUtilizationSample) {
	pmList := &unstructured.UnstructuredList{}
	pmList.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   podMetricsAPIGroup,
		Version: podMetricsAPIVersion,
		Kind:    podMetricsListKind,
	})
	// Read straight from APIReader: metrics.k8s.io/v1beta1 does not support watches,
	// and a cached client list would fail or try to register a cluster-wide informer.
	if err := r.podReader().List(ctx, pmList, client.InNamespace(agent.Namespace)); err != nil {
		if apierrors.IsNotFound(err) || meta.IsNoMatchError(err) || apierrors.IsForbidden(err) {
			return
		}
		return
	}

	proxySel := credentialProxySelector(agent)
	execTierName := credentialProxyExecTierName(agent)
	execTierSel := credentialProxyExecTierSelector(agent)
	shellSel := shellSandboxSelector(agent)
	gwApp := agent.Name + gatewayWorkloadNameSuffix
	a2aGWApp := a2aGatewayName(agent)

	for i := range pmList.Items {
		item := &pmList.Items[i]
		name := item.GetName()
		labels := item.GetLabels()
		isExecTier := labelsMatchAll(labels, execTierSel) || name == execTierName || strings.HasPrefix(name, execTierName+k8sNameSeparator)
		isProxy := !isExecTier && (labelsMatchAll(labels, proxySel) || strings.HasPrefix(name, credentialProxyName(agent)+k8sNameSeparator))
		isGateway := labels[podNameLabelAppKey] == gwApp || labels[podNameLabelAppKey] == a2aGWApp ||
			strings.HasPrefix(name, gwApp+k8sNameSeparator) || strings.HasPrefix(name, a2aGWApp+k8sNameSeparator)
		isShell := labelsMatchAll(labels, shellSel) || name == shellSandboxName(agent)+statefulSetPrimaryOrdinal
		if !isProxy && !isGateway && !isShell {
			continue
		}

		containers, found, err := unstructured.NestedSlice(item.Object, podMetricsContainersKey)
		if err != nil || !found {
			continue
		}
		for _, entry := range containers {
			cMap, ok := entry.(map[string]any)
			if !ok {
				continue
			}
			cName, _ := cMap[podMetricsContainerNameKey].(string)
			usageMap, _ := cMap[podMetricsUsageKey].(map[string]any)
			if usageMap == nil {
				continue
			}
			cpuMilli, hasCPU := parseQuantityMilli(usageMap[sizingDimCPU])
			memBytes, hasMem := parseQuantityBytes(usageMap[sizingDimMemory])

			switch {
			case isProxy && cName == credentialProxyContainerName:
				if hasCPU {
					recordMaxInt64(&sample.BrokerCPUMilli, &sample.BrokerCPUObserved, cpuMilli)
				}
				if hasMem {
					recordMaxInt64(&sample.BrokerMemoryBytes, &sample.BrokerMemoryObserved, memBytes)
				}
			case isGateway && cName == agentAPIAuthContainerName:
				if hasCPU {
					recordMaxInt64(&sample.SidecarCPUMilli, &sample.SidecarCPUObserved, cpuMilli)
				}
				if hasMem {
					recordMaxInt64(&sample.SidecarMemoryBytes, &sample.SidecarMemoryObserved, memBytes)
				}
			case isShell && cName == shellSandboxContainerName:
				if hasCPU {
					recordMaxInt64(&sample.ShellCPUMilli, &sample.ShellCPUObserved, cpuMilli)
				}
				if hasMem {
					recordMaxInt64(&sample.ShellMemoryBytes, &sample.ShellMemoryObserved, memBytes)
				}
			}
		}
	}
}

func (r *PlatformAgentReconciler) collectBrokerMetricsSample(ctx context.Context, agent *agentv1alpha1.PlatformAgent, sample *SizingUtilizationSample) {
	pods := &corev1.PodList{}
	if err := r.podReader().List(ctx, pods, client.InNamespace(agent.Namespace), client.MatchingLabels(credentialProxySelector(agent))); err != nil {
		return
	}
	var candidates []*corev1.Pod
	for i := range pods.Items {
		pod := &pods.Items[i]
		if pod.DeletionTimestamp != nil || pod.Status.Phase != corev1.PodRunning || pod.Status.PodIP == "" {
			continue
		}
		candidates = append(candidates, pod)
	}
	if len(candidates) == 0 {
		return
	}
	// Prefer a Ready pod first, then newest CreationTimestamp, so a warming
	// surge pod does not mask the active pod's load during step-up/down.
	sort.SliceStable(candidates, func(i, j int) bool {
		ri, rj := isPodReady(candidates[i]), isPodReady(candidates[j])
		if ri != rj {
			return ri
		}
		if !candidates[i].CreationTimestamp.Equal(&candidates[j].CreationTimestamp) {
			return candidates[i].CreationTimestamp.After(candidates[j].CreationTimestamp.Time)
		}
		return candidates[i].Name < candidates[j].Name
	})

	for _, pod := range candidates {
		url := fmt.Sprintf("%s://%s%s", sizingMetricsScrapeScheme, net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(int(credentialProxyMetricsPort))), sizingMetricsScrapePath)
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			continue
		}
		resp, err := r.brokerControlHTTPClient().Do(req)
		if err != nil {
			continue
		}
		body, readErr := io.ReadAll(io.LimitReader(resp.Body, sizingMetricsMaxBodyBytes))
		_ = resp.Body.Close()
		if readErr != nil || resp.StatusCode != http.StatusOK {
			continue
		}
		if parseBrokerMetricsPayload(body, sample) {
			return
		}
	}
}

func parseBrokerMetricsPayload(body []byte, sample *SizingUtilizationSample) bool {
	parser := expfmt.NewTextParser(model.UTF8Validation)
	families, err := parser.TextToMetricFamilies(bytes.NewReader(body))
	if err != nil {
		return false
	}
	if fam := families[brokerMetricSlotsInUse]; fam != nil {
		if v, ok := firstGaugeValue(fam); ok {
			sample.BrokerSlotsInUse = int64(math.Round(v))
			sample.BrokerSlotsObserved = true
		}
	}
	if fam := families[brokerMetricSlotCap]; fam != nil {
		if v, ok := firstGaugeValue(fam); ok && v > 0 {
			sample.BrokerSlotCap = int64(math.Round(v))
		}
	}
	if fam := families[brokerMetricChildMemReserved]; fam != nil {
		if v, ok := firstGaugeValue(fam); ok {
			sample.BrokerChildMemReservedBytes = int64(math.Round(v))
			sample.BrokerChildMemObserved = true
		}
	}
	if fam := families[brokerMetricChildMemBudget]; fam != nil {
		if v, ok := firstGaugeValue(fam); ok && v > 0 {
			sample.BrokerChildMemBudgetBytes = int64(math.Round(v))
		}
	}
	if fam := families[brokerMetricCgroupMemory]; fam != nil {
		if v, ok := labelledGaugeValue(fam, brokerMetricLabelKind, brokerMetricKindUsage); ok {
			sample.BrokerCgroupUsageBytes = int64(math.Round(v))
			sample.BrokerCgroupObserved = true
		}
		if v, ok := labelledGaugeValue(fam, brokerMetricLabelKind, brokerMetricKindLimit); ok && v > 0 {
			sample.BrokerCgroupLimitBytes = int64(math.Round(v))
		}
	}
	if fam := families[brokerMetricWatermarkRatio]; fam != nil {
		if v, ok := labelledGaugeValue(fam, brokerMetricLabelResource, brokerMetricResourceMemory); ok {
			sample.BrokerWatermarkMemoryRatio = v
			sample.BrokerWatermarkMemoryObserved = true
		}
		if v, ok := labelledGaugeValue(fam, brokerMetricLabelResource, brokerMetricResourceState); ok {
			sample.BrokerWatermarkStateRatio = v
			sample.BrokerWatermarkStateObserved = true
		}
		if v, ok := labelledGaugeValue(fam, brokerMetricLabelResource, brokerMetricResourceTmp); ok {
			sample.BrokerWatermarkTmpRatio = v
			sample.BrokerWatermarkTmpObserved = true
		}
	}
	return sample.BrokerSlotsObserved || sample.BrokerChildMemObserved ||
		sample.BrokerCgroupObserved || sample.BrokerWatermarkMemoryObserved
}

func firstGaugeValue(fam *dto.MetricFamily) (float64, bool) {
	for _, m := range fam.GetMetric() {
		if g := m.GetGauge(); g != nil {
			return g.GetValue(), true
		}
	}
	return 0, false
}

func labelledGaugeValue(fam *dto.MetricFamily, key, want string) (float64, bool) {
	for _, m := range fam.GetMetric() {
		for _, lp := range m.GetLabel() {
			if lp.GetName() == key && lp.GetValue() == want {
				if g := m.GetGauge(); g != nil {
					return g.GetValue(), true
				}
			}
		}
	}
	return 0, false
}

func labelsMatchAll(have, selector map[string]string) bool {
	if len(selector) == 0 {
		return false
	}
	for k, v := range selector {
		if have[k] != v {
			return false
		}
	}
	return true
}

func parseQuantityMilli(raw any) (int64, bool) {
	s, ok := raw.(string)
	if !ok || strings.TrimSpace(s) == "" {
		return 0, false
	}
	q, err := resource.ParseQuantity(s)
	if err != nil {
		return 0, false
	}
	return q.MilliValue(), true
}

func parseQuantityBytes(raw any) (int64, bool) {
	s, ok := raw.(string)
	if !ok || strings.TrimSpace(s) == "" {
		return 0, false
	}
	q, err := resource.ParseQuantity(s)
	if err != nil {
		return 0, false
	}
	return q.Value(), true
}

func recordMaxInt64(dst *int64, observed *bool, candidate int64) {
	if !*observed || candidate > *dst {
		*dst = candidate
		*observed = true
	}
}

func effectiveCPUCapacityMilli(res corev1.ResourceRequirements) int64 {
	if q, ok := res.Requests[corev1.ResourceCPU]; ok && !q.IsZero() {
		return q.MilliValue()
	}
	if q, ok := res.Limits[corev1.ResourceCPU]; ok && !q.IsZero() {
		return q.MilliValue()
	}
	return 0
}

func effectiveMemoryCapacityBytes(res corev1.ResourceRequirements, clamped bool) int64 {
	if clamped {
		if q, ok := res.Requests[corev1.ResourceMemory]; ok && !q.IsZero() {
			return q.Value()
		}
	}
	if q, ok := res.Limits[corev1.ResourceMemory]; ok && !q.IsZero() {
		return q.Value()
	}
	if q, ok := res.Requests[corev1.ResourceMemory]; ok && !q.IsZero() {
		return q.Value()
	}
	return 0
}

func childMemoryBudgetForLimit(memLimitBytes int64) int64 {
	if memLimitBytes <= credentialProxyResidentReserveBytes {
		return 0
	}
	return memLimitBytes - credentialProxyResidentReserveBytes
}

func effectiveBrokerSlotCapAtTier(memLimitBytes, configuredCap int64) int64 {
	admitted := credentialProxyAdmittedRequests(memLimitBytes, credentialProxyOutputCapBytes)
	if configuredCap <= 0 {
		return admitted
	}
	if admitted > 0 && admitted < configuredCap {
		return admitted
	}
	return configuredCap
}

func utilizationPct(used, cap int64) int32 {
	if cap <= 0 || used <= 0 {
		return 0
	}
	num := new(big.Int).Mul(big.NewInt(used), big.NewInt(sizingPercentScale))
	pct := new(big.Int).Div(num, big.NewInt(cap)).Int64()
	if pct > sizingStatusMaxPct {
		pct = sizingStatusMaxPct
	}
	return int32(pct)
}

func ratioAtLeastPct(used, cap, pct int64) bool {
	if cap <= 0 || used < 0 {
		return false
	}
	lhs := new(big.Int).Mul(big.NewInt(used), big.NewInt(sizingPercentScale))
	rhs := new(big.Int).Mul(big.NewInt(cap), big.NewInt(pct))
	return lhs.Cmp(rhs) >= 0
}

func ratioAtMostPct(used, cap, pct int64) bool {
	if cap <= 0 || used < 0 {
		return false
	}
	lhs := new(big.Int).Mul(big.NewInt(used), big.NewInt(sizingPercentScale))
	rhs := new(big.Int).Mul(big.NewInt(cap), big.NewInt(pct))
	return lhs.Cmp(rhs) <= 0
}

type tierContainerCapacities struct {
	proxyCPUMilli    int64
	proxyMemBytes    int64
	proxyChildBudget int64
	sidecarMemBytes  int64
	shellMemBytes    int64
}

func capacitiesAtTier(agent *agentv1alpha1.PlatformAgent, autopilot, clamped bool, tier sizingTier) tierContainerCapacities {
	renders := renderedSizedContainersAt(agent, autopilot, tier)
	var out tierContainerCapacities
	for _, r := range renders {
		switch r.container {
		case credentialProxyContainerName:
			out.proxyCPUMilli = effectiveCPUCapacityMilli(r.resources)
			out.proxyMemBytes = effectiveMemoryCapacityBytes(r.resources, clamped)
			out.proxyChildBudget = childMemoryBudgetForLimit(out.proxyMemBytes)
		case agentAPIAuthContainerName:
			out.sidecarMemBytes = effectiveMemoryCapacityBytes(r.resources, clamped)
		case shellSandboxContainerName:
			out.shellMemBytes = effectiveMemoryCapacityBytes(r.resources, clamped)
		}
	}
	return out
}

func evaluatePeakObservation(
	caps tierContainerCapacities,
	sample SizingUtilizationSample,
) (peakPct int32, peakDim string, peakWorkload string, hasObs bool) {
	record := func(used, cap int64, dim, workload string) {
		if cap <= 0 {
			return
		}
		pct := utilizationPct(used, cap)
		if !hasObs || pct > peakPct {
			peakPct = pct
			peakDim = dim
			peakWorkload = workload
			hasObs = true
		}
	}

	if sample.BrokerCPUObserved {
		record(sample.BrokerCPUMilli, caps.proxyCPUMilli, sizingDimCPU, sizingWorkloadProxy)
	}
	if sample.BrokerMemoryObserved {
		record(sample.BrokerMemoryBytes, caps.proxyMemBytes, sizingDimMemory, sizingWorkloadProxy)
	}
	if sample.BrokerCgroupObserved {
		denom := caps.proxyMemBytes
		if sample.BrokerCgroupLimitBytes > 0 && sample.BrokerCgroupLimitBytes < denom {
			denom = sample.BrokerCgroupLimitBytes
		}
		record(sample.BrokerCgroupUsageBytes, denom, sizingDimCgroupMem, sizingWorkloadProxy)
	}
	if sample.BrokerWatermarkMemoryObserved && caps.proxyMemBytes > 0 {
		used := int64(math.Round(sample.BrokerWatermarkMemoryRatio * float64(caps.proxyMemBytes)))
		record(used, caps.proxyMemBytes, sizingDimWatermark, sizingWorkloadProxy)
	}
	if sample.BrokerChildMemObserved {
		denom := caps.proxyChildBudget
		if sample.BrokerChildMemBudgetBytes > 0 {
			denom = sample.BrokerChildMemBudgetBytes
		}
		record(sample.BrokerChildMemReservedBytes, denom, sizingDimChildMemory, sizingWorkloadProxy)
	}
	if sample.BrokerSlotsObserved && sample.BrokerSlotCap > 0 {
		record(sample.BrokerSlotsInUse, sample.BrokerSlotCap, sizingDimSlots, sizingWorkloadProxy)
	}
	if sample.SidecarMemoryObserved {
		record(sample.SidecarMemoryBytes, caps.sidecarMemBytes, sizingDimMemory, sizingWorkloadSidecar)
	}
	if sample.ShellMemoryObserved {
		record(sample.ShellMemoryBytes, caps.shellMemBytes, sizingDimMemory, sizingWorkloadShell)
	}
	return peakPct, peakDim, peakWorkload, hasObs
}

// lowestRelievingTier returns the lowest candidate tier > current that strictly
// raises at least one saturated dimension's capacity.
func lowestRelievingTier(
	agent *agentv1alpha1.PlatformAgent,
	autopilot, clamped bool,
	current sizingTier,
	sample SizingUtilizationSample,
) sizingTier {
	target := current
	if current >= sizingTierFleet || !sample.HasAnyData() {
		return target
	}
	curCaps := capacitiesAtTier(agent, autopilot, clamped, current)
	configuredSlotCap, _ := strconv.ParseInt(credentialProxyMaxConcurrentCommands, 10, 64)

	consider := func(relieving func(candCaps tierContainerCapacities) bool) {
		for cand := current + 1; cand <= sizingTierFleet; cand++ {
			candCaps := capacitiesAtTier(agent, autopilot, clamped, cand)
			if relieving(candCaps) {
				if cand > target {
					target = cand
				}
				return
			}
		}
	}

	if sample.BrokerCPUObserved && ratioAtLeastPct(sample.BrokerCPUMilli, curCaps.proxyCPUMilli, sizingStepUpThresholdPct) {
		consider(func(c tierContainerCapacities) bool { return c.proxyCPUMilli > curCaps.proxyCPUMilli })
	}
	if sample.BrokerMemoryObserved && ratioAtLeastPct(sample.BrokerMemoryBytes, curCaps.proxyMemBytes, sizingStepUpThresholdPct) {
		consider(func(c tierContainerCapacities) bool { return c.proxyMemBytes > curCaps.proxyMemBytes })
	}
	if sample.BrokerCgroupObserved {
		denom := curCaps.proxyMemBytes
		if sample.BrokerCgroupLimitBytes > 0 && sample.BrokerCgroupLimitBytes < denom {
			denom = sample.BrokerCgroupLimitBytes
		}
		if ratioAtLeastPct(sample.BrokerCgroupUsageBytes, denom, sizingStepUpThresholdPct) {
			consider(func(c tierContainerCapacities) bool { return c.proxyMemBytes > curCaps.proxyMemBytes })
		}
	}
	if sample.BrokerWatermarkMemoryObserved && curCaps.proxyMemBytes > 0 {
		used := int64(math.Round(sample.BrokerWatermarkMemoryRatio * float64(curCaps.proxyMemBytes)))
		if ratioAtLeastPct(used, curCaps.proxyMemBytes, sizingStepUpThresholdPct) {
			consider(func(c tierContainerCapacities) bool { return c.proxyMemBytes > curCaps.proxyMemBytes })
		}
	}
	if sample.BrokerChildMemObserved {
		denom := curCaps.proxyChildBudget
		if sample.BrokerChildMemBudgetBytes > 0 {
			denom = sample.BrokerChildMemBudgetBytes
		}
		if ratioAtLeastPct(sample.BrokerChildMemReservedBytes, denom, sizingStepUpThresholdPct) {
			consider(func(c tierContainerCapacities) bool { return c.proxyChildBudget > curCaps.proxyChildBudget })
		}
	}
	if sample.BrokerSlotsObserved && sample.BrokerSlotCap > 0 && ratioAtLeastPct(sample.BrokerSlotsInUse, sample.BrokerSlotCap, sizingStepUpThresholdPct) {
		// Slots only trigger a step-up if the candidate tier's memory limit unlocks
		// strictly more concurrent admitted requests than the running slot cap.
		consider(func(c tierContainerCapacities) bool {
			return effectiveBrokerSlotCapAtTier(c.proxyMemBytes, configuredSlotCap) > sample.BrokerSlotCap
		})
	}
	if sample.SidecarMemoryObserved && ratioAtLeastPct(sample.SidecarMemoryBytes, curCaps.sidecarMemBytes, sizingStepUpThresholdPct) {
		consider(func(c tierContainerCapacities) bool { return c.sidecarMemBytes > curCaps.sidecarMemBytes })
	}
	if sample.ShellMemoryObserved && ratioAtLeastPct(sample.ShellMemoryBytes, curCaps.shellMemBytes, sizingStepUpThresholdPct) {
		consider(func(c tierContainerCapacities) bool { return c.shellMemBytes > curCaps.shellMemBytes })
	}
	return target
}

// allShrinkingDimensionsBelow verifies that EVERY dimension whose capacity
// strictly decreases from running to lower has been observed in sample AND sits
// at or below sizingStepDownThresholdPct (55%) of lower's capacity.
func allShrinkingDimensionsBelow(
	agent *agentv1alpha1.PlatformAgent,
	autopilot, clamped bool,
	running, lower sizingTier,
	sample SizingUtilizationSample,
) bool {
	if !sample.HasAnyData() {
		return false
	}
	runCaps := capacitiesAtTier(agent, autopilot, clamped, running)
	lowCaps := capacitiesAtTier(agent, autopilot, clamped, lower)

	if lowCaps.proxyCPUMilli < runCaps.proxyCPUMilli {
		if !sample.BrokerCPUObserved || !ratioAtMostPct(sample.BrokerCPUMilli, lowCaps.proxyCPUMilli, sizingStepDownThresholdPct) {
			return false
		}
	}
	if lowCaps.proxyMemBytes < runCaps.proxyMemBytes {
		hasBrokerMem := sample.BrokerMemoryObserved || sample.BrokerCgroupObserved || sample.BrokerWatermarkMemoryObserved
		if !hasBrokerMem {
			return false
		}
		if sample.BrokerMemoryObserved && !ratioAtMostPct(sample.BrokerMemoryBytes, lowCaps.proxyMemBytes, sizingStepDownThresholdPct) {
			return false
		}
		if sample.BrokerCgroupObserved && !ratioAtMostPct(sample.BrokerCgroupUsageBytes, lowCaps.proxyMemBytes, sizingStepDownThresholdPct) {
			return false
		}
		if sample.BrokerWatermarkMemoryObserved && runCaps.proxyMemBytes > 0 {
			used := int64(math.Round(sample.BrokerWatermarkMemoryRatio * float64(runCaps.proxyMemBytes)))
			if !ratioAtMostPct(used, lowCaps.proxyMemBytes, sizingStepDownThresholdPct) {
				return false
			}
		}
	}
	if lowCaps.proxyChildBudget < runCaps.proxyChildBudget {
		if !sample.BrokerChildMemObserved {
			return false
		}
		if !ratioAtMostPct(sample.BrokerChildMemReservedBytes, lowCaps.proxyChildBudget, sizingStepDownThresholdPct) {
			return false
		}
	}
	if lowCaps.sidecarMemBytes < runCaps.sidecarMemBytes {
		if !sample.SidecarMemoryObserved || !ratioAtMostPct(sample.SidecarMemoryBytes, lowCaps.sidecarMemBytes, sizingStepDownThresholdPct) {
			return false
		}
	}
	if lowCaps.shellMemBytes < runCaps.shellMemBytes {
		if !sample.ShellMemoryObserved || !ratioAtMostPct(sample.ShellMemoryBytes, lowCaps.shellMemBytes, sizingStepDownThresholdPct) {
			return false
		}
	}
	return true
}

func (r *PlatformAgentReconciler) evaluateSizingUtilization(
	agent *agentv1alpha1.PlatformAgent,
	autopilot, clamped bool,
	running, floor sizingTier,
	prev *agentv1alpha1.SizingStatus,
	sample SizingUtilizationSample,
	now time.Time,
) sizingUtilizationDecision {
	curCaps := capacitiesAtTier(agent, autopilot, clamped, running)
	peakPct, peakDim, peakWorkload, hasPeak := evaluatePeakObservation(curCaps, sample)

	next := &agentv1alpha1.SizingStatus{}
	if prev != nil {
		next = prev.DeepCopy()
	}
	if hasPeak {
		next.LastObservedPeakPct = ptr.To(peakPct)
		next.LastObservedDimension = peakDim
		next.LastObservedWorkload = peakWorkload
	}

	stepUpTarget := lowestRelievingTier(agent, autopilot, clamped, running, sample)
	desired := max(running, floor)
	var requeue time.Duration

	if stepUpTarget > running {
		next.ScaleDownSustainedSince = nil
		if next.ScaleUpSustainedSince == nil {
			t := metav1.NewTime(now)
			next.ScaleUpSustainedSince = &t
		}
		elapsed := now.Sub(next.ScaleUpSustainedSince.Time)
		if elapsed >= sizingStepUpSoakDuration {
			if stepUpTarget > desired {
				desired = stepUpTarget
			}
		} else {
			remaining := sizingStepUpSoakDuration - elapsed
			requeue = minRequeue(remaining, sizingUtilizationPollInterval)
		}
	} else {
		next.ScaleUpSustainedSince = nil
		if floor <= running && running > floor {
			lower := running - 1
			if allShrinkingDimensionsBelow(agent, autopilot, clamped, running, lower, sample) {
				if next.ScaleDownSustainedSince == nil {
					t := metav1.NewTime(now)
					next.ScaleDownSustainedSince = &t
				}
				soakElapsed := now.Sub(next.ScaleDownSustainedSince.Time)
				cooldownElapsed := sizingTransitionCooldown
				if next.LastTransitionTime != nil {
					cooldownElapsed = now.Sub(next.LastTransitionTime.Time)
				}
				if soakElapsed >= sizingStepDownSoakDuration && cooldownElapsed >= sizingTransitionCooldown {
					desired = lower
				} else {
					waitSoak := sizingStepDownSoakDuration - soakElapsed
					waitCooldown := sizingTransitionCooldown - cooldownElapsed
					need := max(waitSoak, waitCooldown)
					requeue = minRequeue(need, sizingUtilizationPollInterval)
				}
			} else {
				// Never step down (and reset soak) on deadband or missing metrics.
				next.ScaleDownSustainedSince = nil
			}
		} else {
			next.ScaleDownSustainedSince = nil
		}
	}

	trigger := sizingTriggerFor(floor, desired, stepUpTarget > running)
	next.ActiveTier = running.String()
	next.DesiredTier = desired.String()
	next.Trigger = trigger

	return sizingUtilizationDecision{
		DesiredTier:  desired,
		Trigger:      trigger,
		NextStatus:   next,
		RequeueAfter: requeue,
	}
}

func sizingTriggerFor(floor, desired sizingTier, utilizationSaturated bool) string {
	switch {
	case desired > floor && floor > sizingTierSmall:
		return sizingTriggerBoth
	case desired > floor:
		return sizingTriggerUtilization
	case utilizationSaturated && floor > sizingTierSmall:
		return sizingTriggerBoth
	case utilizationSaturated:
		return sizingTriggerUtilization
	default:
		return sizingTriggerClusterCount
	}
}

func minRequeue(a, b time.Duration) time.Duration {
	if a <= 0 {
		return b
	}
	if b <= 0 {
		return a
	}
	if a < b {
		return a
	}
	return b
}

package controller

import (
	"sync"

	"github.com/prometheus/client_golang/prometheus"
	"sigs.k8s.io/controller-runtime/pkg/metrics"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The operator's own view of every PlatformAgent, as Prometheus gauges on the
// manager's metrics listener, so that a Managed Prometheus scrape of the
// operator pod reads the same numbers the usage poller wrote to status.usage
// and the reconciler wrote to status.phase, Tier 1 included, without a second
// scrape of the producers. Labels are closed sets: the agent's namespace and
// name (agent_namespace, agent), the field (usageExportFields' and
// usageExportTimeFields' names, status.usage's JSON keys, plus createdTime)
// and the phase vocabulary (usagePhases). Nothing from conditions, messages
// or interfaces is exported.
const (
	// usageGaugeName carries every counter and cluster gauge of status.usage,
	// one sample per field, the value as the status holds it.
	usageGaugeName = "kubeagents_platformagent_usage"
	// usageTimeGaugeName carries status.usage's two times, and the CR's
	// creation time, as unix seconds; a time the status does not hold has no
	// sample.
	usageTimeGaugeName = "kubeagents_platformagent_usage_time_seconds"
	// usagePhaseGaugeName is one-hot over usagePhases: the agent's phase at 1,
	// the others at 0, so an alert can read a phase without a text match.
	usagePhaseGaugeName = "kubeagents_platformagent_phase"
	// The agent's labels are not "namespace" and "name": a Managed Prometheus
	// scrape owns "namespace" as a target label and would rename the agent's to
	// exported_namespace.
	usageGaugeNamespaceLabel = "agent_namespace"
	usageGaugeNameLabel      = "agent"
	usageGaugeFieldLabel     = "field"
	usageGaugePhaseLabel     = "phase"
	// The two cluster gauges' field names, which share one pruning record.
	usageExportClustersRegistered = "clustersRegistered"
	usageExportClustersMonitored  = "clustersMonitored"
	// The six proposal readings' field names (usageReadingSpecs), which share
	// one pruning record (usageNewerFieldReadings).
	usageExportRemediationsMerged         = "remediationsMergedTotal"
	usageExportRemediationsOpen           = "remediationsOpen"
	usageExportProposalsAuthored          = "proposalsAuthored"
	usageExportRemediationsClosedUnmerged = "remediationsClosedUnmergedTotal"
	usageExportProposalHumanTouches       = "proposalHumanTouches"
	usageExportProposalsMergedUntouched   = "proposalsMergedUntouched"
	// usageCreatedTimeField is the one time sample that is not status.usage's:
	// the CR's creation timestamp, so a rule can read an install's age before
	// anyone has spoken to it. Published beside the phase, outside every hold.
	usageCreatedTimeField = "createdTime"
)

// pendingPhase and failedPhase are the API's two phases this controller
// never writes; the gauge carries them so the one-hot is the API's vocabulary.
const (
	pendingPhase = "Pending"
	failedPhase  = "Failed"
)

// usagePhases is the closed vocabulary of status.phase the gauge carries: the
// API's four phases and the controller's Degraded. A phase outside it leaves
// every sample at 0; TestUsageGauges_PhasesAreConstantsTheVocabularyCarries
// keeps the writers on these constants.
var usagePhases = []string{pendingPhase, provisioningPhase, readyPhase, degradedPhase, failedPhase}

type usageExportField struct {
	name string
	read func(*agentv1alpha1.AgentUsageStatus) (int64, bool)
	// pointer marks a cluster gauge or a proposal reading, whose presence at
	// 0 is a reading.
	pointer bool
}

func usageInt(get func(*agentv1alpha1.AgentUsageStatus) int64) func(*agentv1alpha1.AgentUsageStatus) (int64, bool) {
	return func(u *agentv1alpha1.AgentUsageStatus) (int64, bool) { return get(u), true }
}

func usagePointer(get func(*agentv1alpha1.AgentUsageStatus) *int64) func(*agentv1alpha1.AgentUsageStatus) (int64, bool) {
	return func(u *agentv1alpha1.AgentUsageStatus) (int64, bool) {
		if p := get(u); p != nil {
			return *p, true
		}
		return 0, false
	}
}

func usageGaugeField(name string, get func(*agentv1alpha1.AgentUsageStatus) *int64) usageExportField {
	return usageExportField{name: name, read: usagePointer(get), pointer: true}
}

// usageExportFields is the second copy of AgentUsageStatus's numeric fields;
// TestUsageGauges_CoverEveryNumericAndTimeFieldOfTheStatus holds it to the
// struct.
var usageExportFields = []usageExportField{
	{name: "sessionsTotal", read: usageInt(func(u *agentv1alpha1.AgentUsageStatus) int64 { return u.SessionsTotal })},
	{name: "eventsIngestedTotal", read: usageInt(func(u *agentv1alpha1.AgentUsageStatus) int64 { return u.EventsIngestedTotal })},
	{name: "toolExecutionsTotal", read: usageInt(func(u *agentv1alpha1.AgentUsageStatus) int64 { return u.ToolExecutionsTotal })},
	{name: "remediationsProposedTotal", read: usageInt(func(u *agentv1alpha1.AgentUsageStatus) int64 { return u.RemediationsProposedTotal })},
	{name: "chatMessagesInboundTotal", read: usageInt(func(u *agentv1alpha1.AgentUsageStatus) int64 { return u.ChatMessagesInboundTotal })},
	{name: "toolExecutionsBlockedTotal", read: usageInt(func(u *agentv1alpha1.AgentUsageStatus) int64 { return u.ToolExecutionsBlockedTotal })},
	{name: "brokerFailuresTotal", read: usageInt(func(u *agentv1alpha1.AgentUsageStatus) int64 { return u.BrokerFailuresTotal })},
	{name: "chatTurnsErroredTotal", read: usageInt(func(u *agentv1alpha1.AgentUsageStatus) int64 { return u.ChatTurnsErroredTotal })},
	{name: "backgroundTasksGaveUpTotal", read: usageInt(func(u *agentv1alpha1.AgentUsageStatus) int64 { return u.BackgroundTasksGaveUpTotal })},
	{name: "negativeFeedbackTotal", read: usageInt(func(u *agentv1alpha1.AgentUsageStatus) int64 { return u.NegativeFeedbackTotal })},
	usageGaugeField(usageExportClustersRegistered, func(u *agentv1alpha1.AgentUsageStatus) *int64 { return u.ClustersRegistered }),
	usageGaugeField(usageExportClustersMonitored, func(u *agentv1alpha1.AgentUsageStatus) *int64 { return u.ClustersMonitored }),
	usageGaugeField(usageExportRemediationsMerged, func(u *agentv1alpha1.AgentUsageStatus) *int64 { return u.RemediationsMergedTotal }),
	usageGaugeField(usageExportRemediationsOpen, func(u *agentv1alpha1.AgentUsageStatus) *int64 { return u.RemediationsOpen }),
	usageGaugeField(usageExportProposalsAuthored, func(u *agentv1alpha1.AgentUsageStatus) *int64 { return u.ProposalsAuthored }),
	usageGaugeField(usageExportRemediationsClosedUnmerged, func(u *agentv1alpha1.AgentUsageStatus) *int64 { return u.RemediationsClosedUnmergedTotal }),
	usageGaugeField(usageExportProposalHumanTouches, func(u *agentv1alpha1.AgentUsageStatus) *int64 { return u.ProposalHumanTouches }),
	usageGaugeField(usageExportProposalsMergedUntouched, func(u *agentv1alpha1.AgentUsageStatus) *int64 { return u.ProposalsMergedUntouched }),
}

type usageExportTimeField struct {
	name string
	read func(*agentv1alpha1.AgentUsageStatus) (float64, bool)
}

var usageExportTimeFields = []usageExportTimeField{
	{"lastActiveTime", func(u *agentv1alpha1.AgentUsageStatus) (float64, bool) {
		if u.LastActiveTime == nil {
			return 0, false
		}
		return float64(u.LastActiveTime.Unix()), true
	}},
	{"lastHumanActiveTime", func(u *agentv1alpha1.AgentUsageStatus) (float64, bool) {
		if u.LastHumanActiveTime == nil {
			return 0, false
		}
		return float64(u.LastHumanActiveTime.Unix()), true
	}},
}

// usageGauges is the exporter: three gauge families on one registry. The
// process holds one, on controller-runtime's registry, which the manager's
// metrics server serves; tests build their own on a fresh registry.
type usageGauges struct {
	usage *prometheus.GaugeVec
	times *prometheus.GaugeVec
	phase *prometheus.GaugeVec
	mu    sync.Mutex
}

func newUsageGauges(reg prometheus.Registerer) *usageGauges {
	agentLabels := []string{usageGaugeNamespaceLabel, usageGaugeNameLabel}
	g := &usageGauges{
		usage: prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: usageGaugeName, Help: "A PlatformAgent's status.usage counters and cluster gauges, one sample per field, as the operator last wrote them."}, append(agentLabels, usageGaugeFieldLabel)),
		times: prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: usageTimeGaugeName, Help: "A PlatformAgent's status.usage times, and its creation time, as unix seconds; a status time is absent while the status holds none."}, append(agentLabels, usageGaugeFieldLabel)),
		phase: prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: usagePhaseGaugeName, Help: "A PlatformAgent's status.phase, one-hot over Pending, Provisioning, Ready, Degraded and Failed."}, append(agentLabels, usageGaugePhaseLabel)),
	}
	reg.MustRegister(g.usage, g.times, g.phase)
	return g
}

// usageMetrics is the process's exporter, served by the manager's metrics
// listener when --metrics-bind-address names one.
var usageMetrics = newUsageGauges(metrics.Registry)

// publishUsage sets the usage and time samples from the agent's status as it
// stands, deleting the sample of a pointer or time the status no longer holds.
func (g *usageGauges) publishUsage(agent *agentv1alpha1.PlatformAgent) {
	g.mu.Lock()
	defer g.mu.Unlock()
	u := &agent.Status.Usage
	for _, f := range usageExportFields {
		if v, ok := f.read(u); ok {
			g.usage.WithLabelValues(agent.Namespace, agent.Name, f.name).Set(float64(v))
		} else {
			g.usage.DeleteLabelValues(agent.Namespace, agent.Name, f.name)
		}
	}
	for _, f := range usageExportTimeFields {
		if v, ok := f.read(u); ok {
			g.times.WithLabelValues(agent.Namespace, agent.Name, f.name).Set(v)
		} else {
			g.times.DeleteLabelValues(agent.Namespace, agent.Name, f.name)
		}
	}
}

// publishPhase sets the one-hot phase samples from status.phase, and the
// agent's creation time, the two samples no hold on status.usage touches.
func (g *usageGauges) publishPhase(agent *agentv1alpha1.PlatformAgent) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.times.WithLabelValues(agent.Namespace, agent.Name, usageCreatedTimeField).Set(float64(agent.CreationTimestamp.Unix()))
	for _, phase := range usagePhases {
		v := 0.0
		if agent.Status.Phase == phase {
			v = 1
		}
		g.phase.WithLabelValues(agent.Namespace, agent.Name, phase).Set(v)
	}
}

// usageBlockEmpty reports a status.usage that holds nothing: no counter moved,
// no cluster gauge or time written, no interface. That is a pruned status as
// the server returns it, and a new agent before its first poll; either way
// the zeros are not a reading.
func usageBlockEmpty(u *agentv1alpha1.AgentUsageStatus) bool {
	if len(u.ActiveInterfaces) != 0 {
		return false
	}
	for _, f := range usageExportFields {
		if v, ok := f.read(u); ok && (v != 0 || f.pointer) {
			return false
		}
	}
	for _, f := range usageExportTimeFields {
		if _, ok := f.read(u); ok {
			return false
		}
	}
	return true
}

// usageExportFieldSet is the set of every exported usage and time field name,
// plus the given names; the whole block, as a hold.
func usageExportFieldSet(extra []string) map[string]bool {
	set := map[string]bool{}
	for _, f := range usageExportFields {
		set[f.name] = true
	}
	for _, f := range usageExportTimeFields {
		set[f.name] = true
	}
	for _, name := range extra {
		set[name] = true
	}
	return set
}

// usageExportFieldsOfRecord maps one of the poller's per-field pruning records
// to the exported fields it covers: the cluster gauges share one record, the
// six proposal readings another, and every other record is named for its
// field.
func usageExportFieldsOfRecord(record string) []string {
	switch record {
	case usageNewerFieldGauges:
		return []string{usageExportClustersRegistered, usageExportClustersMonitored}
	case usageNewerFieldReadings:
		fields := make([]string, 0, len(usageReadingSpecs))
		for _, spec := range usageReadingSpecs {
			fields = append(fields, spec.field)
		}
		return fields
	}
	return []string{record}
}

// publishUsageExcept sets the usage and time samples from the agent's status
// for every field not in held, and deletes the samples of the held fields,
// whose status values the served CRD pruned.
func (g *usageGauges) publishUsageExcept(agent *agentv1alpha1.PlatformAgent, held map[string]bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	u := &agent.Status.Usage
	for _, f := range usageExportFields {
		if v, ok := f.read(u); ok && !held[f.name] {
			g.usage.WithLabelValues(agent.Namespace, agent.Name, f.name).Set(float64(v))
		} else {
			g.usage.DeleteLabelValues(agent.Namespace, agent.Name, f.name)
		}
	}
	for _, f := range usageExportTimeFields {
		if v, ok := f.read(u); ok && !held[f.name] {
			g.times.WithLabelValues(agent.Namespace, agent.Name, f.name).Set(v)
		} else {
			g.times.DeleteLabelValues(agent.Namespace, agent.Name, f.name)
		}
	}
}

// forgetUsage drops an agent's usage and time samples and keeps its phase:
// the served CRD is pruning status.usage, so the zeros read back are unknown.
func (g *usageGauges) forgetUsage(namespace, name string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	labels := prometheus.Labels{usageGaugeNamespaceLabel: namespace, usageGaugeNameLabel: name}
	g.usage.DeletePartialMatch(labels)
	g.times.DeletePartialMatch(labels)
}

// forget drops every sample of an agent, once it is gone.
func (g *usageGauges) forget(namespace, name string) {
	g.forgetUsage(namespace, name)
	g.mu.Lock()
	defer g.mu.Unlock()
	g.phase.DeletePartialMatch(prometheus.Labels{usageGaugeNamespaceLabel: namespace, usageGaugeNameLabel: name})
}

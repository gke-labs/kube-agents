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

package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net"
	"strconv"
	"sync"
	"time"

	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// usageCountersPollInterval is how often the poller reads the listeners
	// and, when a total moved, writes the status: the interval the controller
	// already uses for the RBAC re-probe and the pruned-status re-probe, on
	// the same reasoning that one status write per interval is a cost nobody
	// notices. The first poll runs one interval after the manager elects this
	// replica, by which time the initial reconcile pass has rendered the
	// policies admitting the operator; a CR it reaches before that costs one
	// failed poll, under the streak that records no event.
	usageCountersPollInterval = 5 * time.Minute
	// usageCountersConfigMapSuffix names the ConfigMap that holds the
	// document, in the CR's namespace: <name>-usage-counters.
	usageCountersConfigMapSuffix = "-usage-counters"
	// usageCountersDocumentKey is the ConfigMap key the JSON document sits
	// under.
	usageCountersDocumentKey = "counters.json"
	// usageScrapeFailureEventStreak is the number of consecutive polls a pod's
	// listener has to fail before the poller records a Warning event on the
	// CR. A shorter streak records none, so an upgrade's gap, listeners
	// moving before the policies admit the operator, leaves no Warning on a
	// healthy CR.
	usageScrapeFailureEventStreak = 2
	// usageScrapeFailingReason is the Warning event's reason.
	usageScrapeFailingReason = "UsageScrapeFailing"
	usagePollerLogName       = "usage-counters"
	// The two sentences the Warning event can end with; usageScrapeGuidance
	// picks one by the failure's class.
	usageScrapeConnectGuidance  = "Check that the pod's NetworkPolicy admits the operator's pods on the metrics port and that the listener is up."
	usageScrapeResponseGuidance = "The listener answered, but its response could not be used; check what is serving the metrics port in the pod."
	// gatewayAppSuffix completes the gateway pods' app label, <name>-gateway,
	// the selector the gateway policy and the Ready writer use.
	gatewayAppSuffix = "-gateway"
	// usageDocumentPrecision is the precision the document keeps its times
	// at, metav1.Time's, and the one a poll's time is truncated to so that
	// the markers in memory and on the ConfigMap compare alike.
	usageDocumentPrecision = time.Second
)

// UsageCounterPoller produces status.usage's toolExecutionsTotal,
// eventsIngestedTotal and lastActiveTime on every PlatformAgent, from the
// broker's and the watcher's metrics listeners, as a manager Runnable on the
// leader, off the reconcile path. docs/designs/usage-counters-producer.md is
// the design; the rules the counters follow are in usage_counters_fold.go, the
// scrape in usage_counters_scrape.go. This file is the loop and the two
// objects it writes: the ConfigMap that holds the document, first, and the
// status, after it and only when it is behind.
type UsageCounterPoller struct {
	r      *PlatformAgentReconciler
	source usageSource
	now    func() time.Time

	// streaks is the in-memory failure record per pod, for the one log line
	// when a listener first fails, the one when it recovers, and the Warning
	// event when a streak reaches usageScrapeFailureEventStreak. It is not
	// state: a leader change starts it afresh, at the cost of one more log
	// line.
	mu      sync.Mutex
	streaks map[types.UID]*usageScrapeStreak
}

type usageScrapeStreak struct {
	count int
}

// usageTarget is a running pod whose listener the poll reads.
type usageTarget struct {
	uid     types.UID
	name    string
	created time.Time
	counter string
	addr    string
}

// NewUsageCounterPoller returns the poller for r's PlatformAgents, reading
// the pods' listeners over the pod network.
func NewUsageCounterPoller(r *PlatformAgentReconciler) *UsageCounterPoller {
	return &UsageCounterPoller{
		r:       r,
		source:  newPodUsageSource(),
		now:     time.Now,
		streaks: map[types.UID]*usageScrapeStreak{},
	}
}

// Start polls every interval until ctx is cancelled. It satisfies
// manager.Runnable; main.go adds the poller to the manager.
func (p *UsageCounterPoller) Start(ctx context.Context) error {
	ticker := time.NewTicker(usageCountersPollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			p.pollOnce(ctx)
		}
	}
}

// NeedLeaderElection reports true: the counters are per cluster, so exactly
// one operator replica advances them.
func (p *UsageCounterPoller) NeedLeaderElection() bool { return true }

// pollOnce runs one poll over every PlatformAgent, then drops the failure
// streaks of pods that no CR listed, once over all of them: the streak map is
// per process, not per CR.
func (p *UsageCounterPoller) pollOnce(ctx context.Context) {
	log := logf.FromContext(ctx).WithName(usagePollerLogName)
	var list agentv1alpha1.PlatformAgentList
	if err := p.r.List(ctx, &list); err != nil {
		if ctx.Err() == nil {
			log.Error(err, "listing PlatformAgents; this poll reads nothing")
		}
		return
	}
	seen := map[string]bool{}
	for i := range list.Items {
		agent := &list.Items[i]
		if err := p.pollAgent(ctx, agent, seen); err != nil && ctx.Err() == nil {
			log.Error(err, "usage counters poll failed; the totals are where they were", "platformagent", client.ObjectKeyFromObject(agent).String())
		}
	}
	p.forgetDepartedStreaks(seen)
}

// pollAgent is one poll of one CR: scrape, fold, write the ConfigMap when the
// document changed, then project the status when it is behind. Every pod the
// CR's selectors list is added to seen, for the streak pruning in pollOnce.
func (p *UsageCounterPoller) pollAgent(ctx context.Context, cached *agentv1alpha1.PlatformAgent, seen map[string]bool) error {
	key := client.ObjectKeyFromObject(cached)
	log := logf.FromContext(ctx).WithName(usagePollerLogName).WithValues("platformagent", key.String())
	now := p.now().Truncate(usageDocumentPrecision)

	targets, live, err := p.targets(ctx, cached)
	if err != nil {
		return err
	}
	for uid := range live {
		seen[uid] = true
	}
	scraped := make([]usageScrapedPod, 0, len(targets))
	for _, target := range targets {
		reading, err := p.source.Scrape(ctx, target.addr, target.counter)
		if err != nil {
			if ctx.Err() != nil {
				// A poll cut short by shutdown or a leader change is not a
				// listener failure; the next leader polls afresh.
				return ctx.Err()
			}
			p.noteScrapeFailure(log, cached, target, err)
			continue
		}
		p.noteScrapeRecovery(log, target)
		scraped = append(scraped, usageScrapedPod{
			UID:       string(target.uid),
			Name:      target.name,
			Created:   target.created,
			Counter:   target.counter,
			Sample:    reading.Sample,
			StartTime: reading.StartTime,
		})
	}

	// Live reads, not the cache: the ConfigMap is the source of truth and the
	// status its projection, and a cache that handed back either as it was
	// before this poller's own last write would have the next poll add the
	// interval's deltas a second time.
	agent := &agentv1alpha1.PlatformAgent{}
	if err := p.reader().Get(ctx, key, agent); err != nil {
		return client.IgnoreNotFound(err)
	}
	existing, doc, err := p.readDocument(ctx, log, agent, now)
	if err != nil {
		return err
	}
	result := foldUsage(doc, string(agent.UID), usageStatusSeed(agent, now), live, scraped, now)
	if result.Changed {
		if err := p.writeDocument(ctx, agent, existing, result.Document); err != nil {
			return err
		}
	}
	return p.projectStatus(ctx, agent, result.Document)
}

// reader is the uncached reader, falling back to the client where tests
// supply none.
func (p *UsageCounterPoller) reader() client.Reader {
	if p.r.APIReader != nil {
		return p.r.APIReader
	}
	return p.r.Client
}

// targets lists the pods the two policies select and returns the running ones
// whose listener the poll reads, with every pod that exists and is not
// terminating, scraped or not, in live. When the CR switches the watcher off the gateway pods stay live, so
// their entries persist, and are not read: the entrypoint starts no watcher,
// so nothing listens on the port, and a refused connection there would be the
// install's choice.
func (p *UsageCounterPoller) targets(ctx context.Context, agent *agentv1alpha1.PlatformAgent) ([]usageTarget, map[string]bool, error) {
	groups := []struct {
		selector  map[string]string
		counter   string
		container string
		port      string
		read      bool
	}{
		{gatewayPodSelector(agent), usageCounterEventsIngested, agentAPIAuthContainerName, eventWatcherMetricsPortName, eventWatcherEnabled(agent)},
		{credentialProxySelector(agent), usageCounterToolExecutions, credentialProxyContainerName, credentialProxyMetricsPortName, true},
	}
	live := map[string]bool{}
	var targets []usageTarget
	for _, group := range groups {
		var pods corev1.PodList
		if err := p.r.List(ctx, &pods, client.InNamespace(agent.Namespace), client.MatchingLabels(group.selector)); err != nil {
			return nil, nil, fmt.Errorf("listing pods: %w", err)
		}
		for i := range pods.Items {
			pod := &pods.Items[i]
			if !pod.DeletionTimestamp.IsZero() {
				// A terminating pod is never read again, so it is not live:
				// its entry is dropped, with its streak, and its marker
				// suppresses no sibling's advance during a rollout. What it
				// counted after its last read is lost, as for any pod that
				// leaves.
				continue
			}
			live[string(pod.UID)] = true
			if !group.read || pod.Status.Phase != corev1.PodRunning || pod.Status.PodIP == "" {
				continue
			}
			port, ok := usagePodPort(pod, group.container, group.port)
			if !ok {
				continue
			}
			targets = append(targets, usageTarget{
				uid:     pod.UID,
				name:    pod.Name,
				created: pod.CreationTimestamp.Time,
				counter: group.counter,
				addr:    net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(int(port))),
			})
		}
	}
	return targets, live, nil
}

// gatewayPodSelector selects the gateway pods, the ones the gateway policy
// covers and the watcher's listener runs in.
func gatewayPodSelector(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{"app": agent.Name + gatewayAppSuffix}
}

// usagePodPort finds the port named name on the container named container,
// looking through the init containers as well as the containers because the
// watcher's sidecar is a native one, an initContainers entry with
// restartPolicy Always. The container is matched too: a CR's own init
// container or sidecar may name a port the same, and the port a CR author
// declares is not the listener's.
func usagePodPort(pod *corev1.Pod, container, name string) (int32, bool) {
	for _, candidate := range pod.Spec.InitContainers {
		if candidate.Name == container {
			return containerPortNamed(candidate, name)
		}
	}
	for _, candidate := range pod.Spec.Containers {
		if candidate.Name == container {
			return containerPortNamed(candidate, name)
		}
	}
	return 0, false
}

func containerPortNamed(container corev1.Container, name string) (int32, bool) {
	for _, port := range container.Ports {
		if port.Name == name {
			return port.ContainerPort, true
		}
	}
	return 0, false
}

func usageCountersConfigMapName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + usageCountersConfigMapSuffix
}

// readDocument reads the CR's ConfigMap live and returns it with the document
// it holds, or a nil document when there is none to use: no ConfigMap, no
// key, a document that does not parse, or one that fails a read-back bound.
// Each of those is re-seeded; a read that failed for any other reason is an
// error, because treating it as absent would re-seed from a status that may
// sit behind the totals.
func (p *UsageCounterPoller) readDocument(ctx context.Context, log logr.Logger, agent *agentv1alpha1.PlatformAgent, now time.Time) (*corev1.ConfigMap, *usageDocument, error) {
	cm := &corev1.ConfigMap{}
	err := p.reader().Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: usageCountersConfigMapName(agent)}, cm)
	if apierrors.IsNotFound(err) {
		return nil, nil, nil
	}
	if err != nil {
		return nil, nil, fmt.Errorf("reading the usage counters ConfigMap: %w", err)
	}
	raw, ok := cm.Data[usageCountersDocumentKey]
	if !ok {
		return cm, nil, nil
	}
	var doc usageDocument
	if err := json.Unmarshal([]byte(raw), &doc); err != nil {
		log.Info("the usage counters document does not parse; re-seeding from the status", "configmap", cm.Name)
		return cm, nil, nil
	}
	if reason := usageDocumentInvalid(&doc, cm, agent, now); reason != "" {
		log.Info("the usage counters document failed a read-back bound; re-seeding from the status", "configmap", cm.Name, "reason", reason)
		return cm, nil, nil
	}
	return cm, &doc, nil
}

// usageDocumentInvalid is why doc is not to be used, or "" when it is. The
// bounds are the design's: the CR's UID in the document and on the owner
// reference, the first-recorded time inside the CR's life and not in the
// future, every sample and total non-negative and finite, every total under
// the int64 headroom and not below the status it projects to.
func usageDocumentInvalid(doc *usageDocument, cm *corev1.ConfigMap, agent *agentv1alpha1.PlatformAgent, now time.Time) string {
	if doc.Version != usageDocumentVersion {
		return "unknown document version"
	}
	if doc.AgentUID != string(agent.UID) {
		return "the recorded CR UID is not this CR's"
	}
	owned := false
	for _, ref := range cm.OwnerReferences {
		if ref.UID == agent.UID {
			owned = true
		}
	}
	if !owned {
		return "the owner reference is not this CR's"
	}
	if doc.FirstRecorded.IsZero() || doc.FirstRecorded.Time.After(now) || doc.FirstRecorded.Time.Before(agent.CreationTimestamp.Time) {
		return "the first-recorded time is outside the CR's life"
	}
	if doc.LastMoved != nil && doc.LastMoved.Time.After(now) {
		return "the last-moved time is in the future"
	}
	if doc.Totals == nil || doc.Pods == nil {
		return "no totals or no pods"
	}
	for counter, floor := range usageStatusSeed(agent, now).Totals {
		total, ok := doc.Totals[counter]
		if !ok || total < 0 || total > usageTotalCeiling {
			return "a total is outside its bounds"
		}
		if total < floor {
			return "a total is below the status it projects to"
		}
	}
	for _, entry := range doc.Pods {
		if entry == nil || entry.Sample < 0 || entry.Marker.IsZero() {
			return "a pod entry is outside its bounds"
		}
		if entry.Counter != usageCounterToolExecutions && entry.Counter != usageCounterEventsIngested {
			return "a pod entry names no counter"
		}
		if entry.StartTime != nil && (math.IsNaN(*entry.StartTime) || math.IsInf(*entry.StartTime, 0) || *entry.StartTime < 0) {
			return "a pod entry's start time is outside its bounds"
		}
	}
	return ""
}

// usageStatusSeed is the status as the document's seed and as the floors a
// stored document may not fall under, built in one place so the two cannot
// drift apart: a value the read-back would refuse, a counter outside the
// document's bounds or a time after now, which the operator never writes, is
// neither seed nor floor. Taken as a floor it would make every document
// invalid, and taken as a seed it would be written into one that the next
// poll refuses, re-seeding the CR, and adding nothing, on every poll.
func usageStatusSeed(agent *agentv1alpha1.PlatformAgent, now time.Time) usageSeed {
	seed := usageSeed{Totals: map[string]int64{
		usageCounterToolExecutions: usageStatusFloor(agent.Status.Usage.ToolExecutionsTotal),
		usageCounterEventsIngested: usageStatusFloor(agent.Status.Usage.EventsIngestedTotal),
	}}
	if last := agent.Status.Usage.LastActiveTime; last != nil && !last.Time.After(now) {
		seed.LastMoved = last.DeepCopy()
	}
	return seed
}

func usageStatusFloor(value int64) int64 {
	if value < 0 || value > usageTotalCeiling {
		return 0
	}
	return value
}

// writeDocument writes doc to the CR's ConfigMap, creating it with a
// non-controller owner reference to the CR: collected with the CR, but not
// re-enqueueing it, since the controller Owns ConfigMaps with no predicate and
// a controller-owned one would cost a reconcile on every write. An existing
// ConfigMap, a predecessor's included, is updated in place, its owner
// reference moved to this CR.
func (p *UsageCounterPoller) writeDocument(ctx context.Context, agent *agentv1alpha1.PlatformAgent, existing *corev1.ConfigMap, doc *usageDocument) error {
	raw, err := json.Marshal(doc)
	if err != nil {
		return fmt.Errorf("serialising the usage counters document: %w", err)
	}
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: usageCountersConfigMapName(agent), Namespace: agent.Namespace}}
	if existing != nil {
		cm = existing.DeepCopy()
	}
	withCommonLabels(cm, agent)
	if err := controllerutil.SetOwnerReference(agent, cm, p.r.Scheme); err != nil {
		return fmt.Errorf("setting the owner reference on the usage counters ConfigMap: %w", err)
	}
	cm.Data = map[string]string{usageCountersDocumentKey: string(raw)}
	if existing == nil {
		if err := p.r.Create(ctx, cm); err != nil {
			return fmt.Errorf("creating the usage counters ConfigMap: %w", err)
		}
		return nil
	}
	if err := p.r.Update(ctx, cm); err != nil {
		return fmt.Errorf("updating the usage counters ConfigMap: %w", err)
	}
	return nil
}

// projectStatus patches status.usage's counters and lastActiveTime from doc
// when the status is behind it, with a merge patch over the status as read
// that touches nothing else. Skipped while the served CRD is recorded as
// pruning status.usage, so an operator ahead of its CRD costs one probe per
// interval across both writers; the ConfigMap is current throughout, and the
// patch after the CRD lands carries everything accumulated since.
func (p *UsageCounterPoller) projectStatus(ctx context.Context, agent *agentv1alpha1.PlatformAgent, doc *usageDocument) error {
	tool := doc.Totals[usageCounterToolExecutions]
	events := doc.Totals[usageCounterEventsIngested]
	usage := &agent.Status.Usage
	behind := usage.ToolExecutionsTotal < tool || usage.EventsIngestedTotal < events ||
		(doc.LastMoved != nil && (usage.LastActiveTime == nil || !usage.LastActiveTime.Equal(doc.LastMoved)))
	if !behind || p.r.usageStatusPruned(agent) {
		return nil
	}
	base := agent.DeepCopy()
	usage.ToolExecutionsTotal = tool
	usage.EventsIngestedTotal = events
	if doc.LastMoved != nil {
		usage.LastActiveTime = doc.LastMoved.DeepCopy()
	}
	if err := p.r.Status().Patch(ctx, agent, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("patching status.usage: %w", err)
	}
	// The echo: counters written non-zero that come back absent are the
	// pruning, recorded in the record the Ready writer shares; a patch that
	// wrote only a time says nothing either way.
	if tool > 0 || events > 0 {
		p.r.noteUsageEcho(ctx, agent, agent.Status.Usage.ToolExecutionsTotal == tool && agent.Status.Usage.EventsIngestedTotal == events)
	}
	return nil
}

// noteScrapeFailure records a failed scrape of target: one log line when the
// streak starts, naming the pod and the error kind and never the body, and
// one Warning event on the CR when it reaches usageScrapeFailureEventStreak,
// so that the symptom, a lastActiveTime that stops advancing, has its cause
// beside it in `kubectl describe`.
func (p *UsageCounterPoller) noteScrapeFailure(log logr.Logger, agent *agentv1alpha1.PlatformAgent, target usageTarget, err error) {
	p.mu.Lock()
	streak := p.streaks[target.uid]
	if streak == nil {
		streak = &usageScrapeStreak{}
		p.streaks[target.uid] = streak
	}
	streak.count++
	count := streak.count
	p.mu.Unlock()

	// The kind, and for a status kind the HTTP code: usageScrapeError's text
	// is a closed vocabulary, never a byte the peer sent.
	detail := usageScrapeDetail(err)
	if count == 1 {
		log.Info("a metrics listener could not be read; its pod's baseline and the totals are unchanged until it recovers",
			"pod", target.name, "counter", target.counter, "error", detail)
	}
	if count == usageScrapeFailureEventStreak {
		p.r.recordEvent(agent, corev1.EventTypeWarning, usageScrapeFailingReason,
			fmt.Sprintf("status.usage.%s is not advancing: the metrics listener of pod %s has failed %d polls in a row (%s). %s",
				target.counter, target.name, count, detail, usageScrapeGuidance(err)))
	}
}

// usageScrapeGuidance is the sentence the Warning event ends with, chosen by
// what failed: a connection that never produced a response points at the
// policy and the listener's liveness; a response the poller refused points at
// what is serving the port, since the connection and the answer were the
// peer's.
func usageScrapeGuidance(err error) string {
	switch usageScrapeKindOf(err) {
	case usageScrapeKindConnect, usageScrapeKindRefused, usageScrapeKindUnreachable, usageScrapeKindTimeout:
		return usageScrapeConnectGuidance
	}
	return usageScrapeResponseGuidance
}

// noteScrapeRecovery closes target's streak, if one was open, with one log
// line.
func (p *UsageCounterPoller) noteScrapeRecovery(log logr.Logger, target usageTarget) {
	p.mu.Lock()
	streak := p.streaks[target.uid]
	delete(p.streaks, target.uid)
	p.mu.Unlock()
	if streak != nil {
		log.Info("a metrics listener is readable again", "pod", target.name, "counter", target.counter, "failedPolls", streak.count)
	}
}

// forgetDepartedStreaks drops the streaks of pods outside seen, the pods every
// CR listed in this poll, so the map does not keep an entry per departed pod
// for the life of the process. A CR whose pods could not be listed this poll
// loses its streaks, which costs one more first-failure line, not a count.
func (p *UsageCounterPoller) forgetDepartedStreaks(seen map[string]bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for uid := range p.streaks {
		if !seen[string(uid)] {
			delete(p.streaks, uid)
		}
	}
}

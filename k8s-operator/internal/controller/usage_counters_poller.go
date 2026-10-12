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
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
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
	// usageConfigMapImmutableReason is the Warning event's reason when the
	// counters ConfigMap cannot be updated because it is immutable.
	usageConfigMapImmutableReason = "UsageConfigMapImmutable"
	// usageConfigMapForeignReason is the Warning event's reason when a ConfigMap
	// under the counters name is not the operator's and is left untouched.
	usageConfigMapForeignReason = "UsageConfigMapForeign"
	// usageConfigMapRefusedReason is the Warning event's reason when the API
	// server refuses the counters ConfigMap write for a standing cause that is
	// not immutability -- a count/configmaps quota at its cap, or an admission
	// policy that denies it -- so the cause sits on the CR rather than only in a
	// log line that recurs every poll.
	usageConfigMapRefusedReason = "UsageConfigMapWriteRefused"
	// usageOwnerKindPlatformAgent is the owner-reference Kind that names a
	// PlatformAgent, so a counters ConfigMap the operator owns is recognised by
	// owner when its instance label is absent.
	usageOwnerKindPlatformAgent = "PlatformAgent"
	usagePollerLogName          = "usage-counters"
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
// eventsIngestedTotal and lastActiveTime on every PlatformAgent, and the two
// cluster gauges, from the broker's and the watcher's metrics listeners, as a
// manager Runnable on the leader, off the reconcile path. docs/designs/usage-counters-producer.md is
// the design; the rules the counters follow are in usage_counters_fold.go, the
// scrape in usage_counters_scrape.go. This file is the loop and the two
// objects it writes: the ConfigMap that holds the document, first, and the
// status, after it and only when it is behind.
type UsageCounterPoller struct {
	r      *PlatformAgentReconciler
	source usageSource
	now    func() time.Time
	// pollBudget bounds one pollOnce: a poll that has not finished within it
	// leaves the rest of the CRs to the next interval rather than stretching
	// past it, so an unreachable listener holding a dial open does not freeze
	// the CRs behind it. A seam so a test need not wait a real interval.
	pollBudget func() time.Duration

	// streaks is the in-memory failure record per pod, for the one log line
	// when a listener first fails, the one when it recovers, and the Warning
	// event when a streak reaches usageScrapeFailureEventStreak. It is not
	// state: a leader change starts it afresh, at the cost of one more log
	// line.
	mu      sync.Mutex
	streaks map[types.UID]*usageScrapeStreak
	// prunedGauges records, per CR, the last poll in which the two cluster
	// gauges were written as a reading, zero included, and came back absent: a served CRD at the
	// previous schema, which has the counters and not the gauges. Its own
	// record rather than the counters' shared one, because that record
	// suppresses every status.usage write for the interval and the counters
	// such a CRD does serve should keep landing. Fresh for
	// usageStatusReprobeInterval, then the gauges are probed again. In memory
	// only, like streaks; keyed like the cursor.
	prunedGauges map[string]time.Time
	// gatewayMisses counts, per CR, the consecutive polls in which no gateway
	// replica's body was read while the watcher is on: listeners failing, pods
	// Pending or without an IP, or none running. At usageScrapeFailureEventStreak
	// the poll clears the gauges to absent, the no current reading it is,
	// rather than holding one that reads as current. In
	// memory, like streaks: a leader change delays the clear by one streak and
	// never advances it.
	gatewayMisses map[string]int

	// cursor is the namespace/name of the last CR a poll finished. The next
	// poll sorts the CRs and resumes after it, wrapping, so a poll the budget
	// cut short leaves the CRs it did not reach at the front of the next one
	// rather than at the mercy of the informer store's map order: a standing
	// blockage that still lets k >= 1 CRs through an interval delays every CR by
	// at most ceil(N/k) intervals instead of a random multiple. The degenerate
	// k == 0 -- a single CR that exhausts the whole budget by itself -- is
	// stepped over in pollOnce rather than pinning the cursor, so the CRs behind
	// it still advance. In memory only, like streaks.
	cursor string
	// sweepSeen and sweptKeys accumulate across the polls of one sweep -- one
	// full pass over the CR set, which a standing budget cut spreads over
	// several polls. The streaks are pruned only once a sweep has reached every
	// current CR, on the pods it saw; pruning on a single cut poll's partial
	// view would drop the streaks of the CRs it did not reach. Reset when the
	// sweep completes. Accessed only from pollOnce, which runs serially.
	sweepSeen map[string]bool
	sweptKeys map[string]bool
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
		r:          r,
		source:     newPodUsageSource(),
		now:        time.Now,
		pollBudget: func() time.Duration { return usageCountersPollInterval },
		streaks:    map[types.UID]*usageScrapeStreak{},
		sweepSeen:  map[string]bool{},
		sweptKeys:  map[string]bool{},
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

// gaugesPruned reports whether the CR's gauge pruning record is fresh, dropping
// it once it has expired.
func (p *UsageCounterPoller) gaugesPruned(agent *agentv1alpha1.PlatformAgent) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	key := usagePollKey(agent)
	recorded, ok := p.prunedGauges[key]
	if !ok {
		return false
	}
	if p.now().Sub(recorded) >= usageStatusReprobeInterval {
		delete(p.prunedGauges, key)
		return false
	}
	return true
}

// noteGaugeEcho is the gauges' own echo record: echoed false is a write whose
// gauge fields came back absent, recorded with the poll's time and logged once
// per record; echoed true clears it.
func (p *UsageCounterPoller) noteGaugeEcho(ctx context.Context, agent *agentv1alpha1.PlatformAgent, echoed bool) {
	key := usagePollKey(agent)
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.prunedGauges == nil {
		p.prunedGauges = map[string]time.Time{}
	}
	if echoed {
		delete(p.prunedGauges, key)
		return
	}
	_, fresh := p.prunedGauges[key]
	p.prunedGauges[key] = p.now()
	if !fresh {
		logf.FromContext(ctx).Info("the served CRD does not serve status.usage's cluster gauges; apply this release's CRD to get clustersRegistered and clustersMonitored, which are probed again after the interval",
			"platformagent", key, "reprobeAfter", usageStatusReprobeInterval.String())
	}
}

// noteGatewayReading records whether this poll read any gateway replica's body
// for the CR and reports whether the misses have reached the streak at which
// the gauges are cleared to absent. A reading, or the watcher being off, ends
// the run of misses.
func (p *UsageCounterPoller) noteGatewayReading(agent *agentv1alpha1.PlatformAgent, read bool) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	key := usagePollKey(agent)
	if read {
		delete(p.gatewayMisses, key)
		return false
	}
	if p.gatewayMisses == nil {
		p.gatewayMisses = map[string]int{}
	}
	p.gatewayMisses[key]++
	return p.gatewayMisses[key] >= usageScrapeFailureEventStreak
}

// forgetDepartedGaugeRecords drops the gauge records and gateway-miss counts of
// CRs outside swept, the CRs a completed sweep reached, so the maps do not keep
// an entry per deleted CR for the life of the process.
func (p *UsageCounterPoller) forgetDepartedGaugeRecords(swept map[string]bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for key := range p.prunedGauges {
		if !swept[key] {
			delete(p.prunedGauges, key)
		}
	}
	for key := range p.gatewayMisses {
		if !swept[key] {
			delete(p.gatewayMisses, key)
		}
	}
}

// NeedLeaderElection reports true: the counters are per cluster, so exactly
// one operator replica advances them.
func (p *UsageCounterPoller) NeedLeaderElection() bool { return true }

// pollOnce runs one poll over the PlatformAgents, in namespace/name order and
// resuming after the cursor so a budget-cut poll picks up where it stopped, then
// prunes the failure streaks once a sweep has covered every CR: the streak map
// is per process, not per CR.
func (p *UsageCounterPoller) pollOnce(parent context.Context) {
	log := logf.FromContext(parent).WithName(usagePollerLogName)
	// A hand-built poller (a test harness) may leave the sweep maps nil; a nil
	// map read is fine but a write panics, so make them once here rather than in
	// every construction site.
	if p.sweepSeen == nil {
		p.sweepSeen = map[string]bool{}
	}
	if p.sweptKeys == nil {
		p.sweptKeys = map[string]bool{}
	}
	// A hand-built poller (the envtest harness) may leave pollBudget nil; fall
	// back to the interval so a missing seam cannot panic the poll.
	budget := usageCountersPollInterval
	if p.pollBudget != nil {
		budget = p.pollBudget()
	}
	ctx, cancel := context.WithTimeout(parent, budget)
	defer cancel()
	var list agentv1alpha1.PlatformAgentList
	if err := p.r.List(ctx, &list); err != nil {
		if ctx.Err() == nil {
			log.Error(err, "listing PlatformAgents; this poll reads nothing")
		}
		return
	}
	// Sort by namespace/name and resume after the cursor, wrapping. The informer
	// store is a Go map, so List's order is a fresh shuffle each call; a stable
	// order with a cursor is what lets a standing budget cut make deterministic
	// progress through the list instead of reading the CRs behind the blockage
	// by chance.
	ordered := make([]*agentv1alpha1.PlatformAgent, len(list.Items))
	for i := range list.Items {
		ordered[i] = &list.Items[i]
	}
	sort.Slice(ordered, func(i, j int) bool { return usagePollKey(ordered[i]) < usagePollKey(ordered[j]) })
	start := 0
	for start < len(ordered) && usagePollKey(ordered[start]) <= p.cursor {
		start++
	}
	if start == len(ordered) {
		start = 0
	}
	seen := map[string]bool{}
	n := len(ordered)
	for off := 0; off < n; off++ {
		agent := ordered[(start+off)%n]
		if err := p.pollAgent(ctx, agent, seen); err != nil {
			if ctx.Err() != nil {
				// The poll hit its budget, or the manager is stopping. Only the
				// first is worth a line, and only it steps the cursor: the CRs
				// left this interval are read first next interval, logged rather
				// than counted as a failure, so an unreachable listener holding
				// its dial open does not freeze the CRs behind it without a word.
				// A cancelled parent is a shutdown or a leader change, and the
				// next leader polls afresh, so it touches neither.
				if parent.Err() == nil {
					log.Info("usage counters poll did not finish within its budget; the remaining CRs are read first next interval", "budget", budget.String())
					// off == 0 means the cut landed on the first CR this poll
					// read, so no CR finished. Leaving the cursor where it is
					// resumes here again next interval, and a CR that exhausts the
					// whole budget by itself would then freeze every CR behind it
					// for good. Step the cursor past it and mark it swept so the
					// next interval makes progress on the rest and the sweep still
					// completes; this CR is retried once the cursor wraps back to
					// it. A cut after at least one CR finished leaves the cursor on
					// the last that did, so the cut CR is read first next interval
					// -- the bounded progress the stable order buys.
					if off == 0 {
						p.cursor = usagePollKey(agent)
						p.sweptKeys[usagePollKey(agent)] = true
					}
				}
				break
			}
			log.Error(err, "usage counters poll failed; the totals are where they were", "platformagent", client.ObjectKeyFromObject(agent).String())
		}
		// The CR was processed -- polled, or a per-CR error that is not a cut.
		// Advance the cursor past it so the next poll resumes after it, and mark
		// it swept. A cut breaks above without reaching here; it leaves the
		// cursor on the last CR that finished, except the no-progress cut handled
		// above, which steps past the CR that stopped the poll.
		p.cursor = usagePollKey(agent)
		p.sweptKeys[usagePollKey(agent)] = true
	}
	// Prune once a sweep -- one full pass over the CR set, spread over several
	// polls while a budget cut lasts -- has reached every current CR, on the
	// pods that sweep saw. Pruning on a single cut poll's `seen` would drop the
	// streaks of the CRs it did not reach; accumulating across the sweep keeps
	// the map bounded without that.
	for uid := range seen {
		p.sweepSeen[uid] = true
	}
	if p.sweepComplete(ordered) {
		p.forgetDepartedStreaks(p.sweepSeen)
		p.forgetDepartedGaugeRecords(p.sweptKeys)
		p.sweepSeen = map[string]bool{}
		p.sweptKeys = map[string]bool{}
	}
}

// usagePollKey orders a CR within a poll and keys the cursor and the sweep:
// namespace/name, unique and stable across polls.
func usagePollKey(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Namespace + "/" + agent.Name
}

// sweepComplete reports whether the current sweep has finished every CR the
// latest List returned. An empty list is vacuously complete, so a cluster that
// loses its last CR prunes every streak on the next poll.
func (p *UsageCounterPoller) sweepComplete(ordered []*agentv1alpha1.PlatformAgent) bool {
	for _, agent := range ordered {
		if !p.sweptKeys[usagePollKey(agent)] {
			return false
		}
	}
	return true
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
	var failing []usageScrapeFailure
	for _, target := range targets {
		reading, err := p.source.Scrape(ctx, target.addr, target.counter)
		if err != nil {
			if ctx.Err() != nil {
				// A poll cut short by shutdown, a leader change, or the poll
				// budget is not a listener failure; the next leader polls
				// afresh and the budget's remainder is read next interval.
				return ctx.Err()
			}
			if failure, report := p.noteScrapeFailure(log, target, err); report {
				failing = append(failing, failure)
			}
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
			Clusters:  reading.Clusters,
		})
	}
	// Whether any gateway replica's body was read this poll: a watcher body
	// always carries a cluster reading, zero or not, and no other body does.
	gatewayRead := !eventWatcherEnabled(cached)
	for _, s := range scraped {
		if s.Clusters != nil {
			gatewayRead = true
			break
		}
	}
	gauges, gaugesKnown := usageClusterGaugesForPoll(cached, scraped, p.noteGatewayReading(cached, gatewayRead))
	// The scrape and ConfigMap causes are not recorded here: both are standing
	// failures that would each spend one of the CR's event-bucket tokens every
	// poll, and two per poll drains the bucket and starves one of them. They are
	// collected and emitted once, below, as the CR's single Warning this poll --
	// recordStandingFailures explains the bucket arithmetic.

	// Live reads, not the cache: the ConfigMap is the source of truth and the
	// status its projection, and a cache that handed back either as it was
	// before this poller's own last write would have the next poll add the
	// interval's deltas a second time.
	agent := &agentv1alpha1.PlatformAgent{}
	if err := p.reader().Get(ctx, key, agent); err != nil {
		if apierrors.IsNotFound(err) {
			// The CR was deleted between this poll's cached List and this live
			// read. There is nothing to record a standing failure against, and a
			// Warning recorded here would dangle in the namespace for the
			// retention hour pointing at a name and UID `kubectl describe` can no
			// longer resolve -- and a CR re-applied under the same name would not
			// show it, since the Event carries the old UID. A transient read error
			// below still records, so a standing cause outlives a blip.
			return nil
		}
		if ctx.Err() == nil {
			p.recordStandingFailures(cached, failing, nil)
		}
		return err
	}
	existing, doc, err := p.readDocument(ctx, log, agent, now)
	if err != nil {
		if ctx.Err() == nil {
			p.recordStandingFailures(cached, failing, nil)
		}
		return err
	}
	result := foldUsage(doc, string(agent.UID), usageStatusSeed(agent, now), live, scraped, now)
	if result.Changed {
		if fault, err := p.writeDocument(ctx, agent, existing, result.Document); err != nil {
			// The write failed: fold the ConfigMap cause, if any, into the CR's
			// one Warning beside the scrape cause rather than recording a second.
			if ctx.Err() == nil {
				p.recordStandingFailures(cached, failing, fault)
			}
			return err
		}
	}
	if ctx.Err() == nil {
		p.recordStandingFailures(cached, failing, nil)
	}
	return p.projectStatus(ctx, agent, result.Document, gauges, gaugesKnown)
}

// usageClusterGaugesForPoll is the fleet reading this poll projects, and
// whether it projects one at all. known false is a poll that says nothing
// about the gauges: the watcher is on and no replica could be read for less
// than the event streak, so the fields keep their last reading across a
// restart or one missed poll. known true with a nil reading clears both
// fields to absent: the watcher is off, or no gateway replica has been read
// for the event streak (watcherDown), so the operator has no current reading
// and an absent field says so where a held one would read as current; the
// watcher may be down, its pod not running, or merely unreachable, and the
// CR's events and the pod's state say which. known true with a reading is the
// largest of each gauge across the gateway replicas read, because every
// replica's watcher builds the same fleet and one mid-startup reports fewer; a
// zero is a reading, and is written as one.
func usageClusterGaugesForPoll(agent *agentv1alpha1.PlatformAgent, scraped []usageScrapedPod, watcherDown bool) (reading *usageClusterGauges, known bool) {
	if !eventWatcherEnabled(agent) || watcherDown {
		return nil, true
	}
	for _, s := range scraped {
		if s.Clusters == nil {
			continue
		}
		if reading == nil {
			reading = &usageClusterGauges{}
		}
		reading.Registered = max(reading.Registered, s.Clusters.Registered)
		reading.Monitored = max(reading.Monitored, s.Clusters.Monitored)
	}
	return reading, reading != nil
}

// usageGaugeFields is a reading as the status carries it: nil pointers for
// no reading, values, zero included, for one.
func usageGaugeFields(reading *usageClusterGauges) (registered, monitored *int64) {
	if reading == nil {
		return nil, nil
	}
	return ptr.To(reading.Registered), ptr.To(reading.Monitored)
}

func usageGaugeEqual(a, b *int64) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
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
// whose listener the poll reads, with every pod that exists and is neither
// terminating nor in a terminal phase, scraped or not, in live. When the CR switches the watcher off the gateway pods stay live, so
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
			if !pod.DeletionTimestamp.IsZero() || pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
				// A terminating pod, or one in a terminal phase, is never read
				// again, so it is not live: its entry is dropped, with its
				// streak, and its marker suppresses no sibling's advance during a
				// rollout. A pod evicted under node pressure, preempted, or whose
				// node was lost is left in Failed with no deletion timestamp --
				// the ReplicaSet replaces it without deleting it and pod GC
				// removes it only past terminated-pod-gc-threshold, hours to days
				// later -- so the deletion-timestamp test alone would keep it
				// live and resetting the new replica. What it counted after its
				// last read is lost, as for any pod that leaves.
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

// usageConfigMapIsOurs reports whether cm is a usage-counters ConfigMap the
// operator wrote for agent, by either signal the operator leaves: the instance
// label it stamps on every object it owns, or an owner reference naming a
// PlatformAgent of this name. The owner match is by name, not UID, on purpose
// -- a CR deleted and re-applied under the same name leaves its predecessor's
// ConfigMap with a stale UID but the right name, and resetting that is what the
// design wants. A ConfigMap another writer parked under the name carries
// neither signal.
func usageConfigMapIsOurs(cm *corev1.ConfigMap, agent *agentv1alpha1.PlatformAgent) bool {
	if cm.Labels[labelInstance] == instanceLabel(agent.Namespace, agent.Name) {
		return true
	}
	for _, ref := range cm.OwnerReferences {
		if ref.Kind == usageOwnerKindPlatformAgent && ref.Name == agent.Name {
			return true
		}
	}
	return false
}

// usageConfigMapFault is a standing reason the fold could not be written to the
// counters ConfigMap: one parked under the name that is not the operator's, or
// an immutable one. writeDocument returns it rather than recording it, so the
// caller spends the CR's one event this poll on a single Warning carrying every
// standing cause -- see recordStandingFailures.
type usageConfigMapFault struct {
	reason  string
	message string
}

// usageWriteRefused classifies a ConfigMap Create or Update error the API server
// will return on every poll until an operator or a human clears it -- a
// count/configmaps quota at its cap or an admission policy that denies the write
// (both IsForbidden), or a validation rejection a Create meets (IsInvalid) -- as
// a standing fault, so the caller surfaces the cause on the CR where the design
// promises rather than leaving status.usage frozen with only a log line. A
// transient class (conflict, timeout, server error) returns nil and is left to
// the retry the next poll is.
func usageWriteRefused(err error, name string) *usageConfigMapFault {
	if !apierrors.IsForbidden(err) && !apierrors.IsInvalid(err) {
		return nil
	}
	return &usageConfigMapFault{
		reason:  usageConfigMapRefusedReason,
		message: fmt.Sprintf("the usage counters ConfigMap %s was refused (%s): status.usage will not advance until the namespace accepts the write", name, apierrors.ReasonForError(err)),
	}
}

// writeDocument writes doc to the CR's ConfigMap, creating it with a
// non-controller owner reference to the CR: collected with the CR, but not
// re-enqueueing it, since the controller Owns ConfigMaps with no predicate and
// a controller-owned one would cost a reconcile on every write. An existing
// ConfigMap the operator owns -- by instance label or an owner reference of
// this name, a predecessor's on a delete-and-recreate included -- is updated in
// place; one parked under the name that is not the operator's is left untouched,
// and the cause returned for the caller to record on the CR.
func (p *UsageCounterPoller) writeDocument(ctx context.Context, agent *agentv1alpha1.PlatformAgent, existing *corev1.ConfigMap, doc *usageDocument) (*usageConfigMapFault, error) {
	raw, err := json.Marshal(doc)
	if err != nil {
		return nil, fmt.Errorf("serialising the usage counters document: %w", err)
	}
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: usageCountersConfigMapName(agent), Namespace: agent.Namespace}}
	if existing != nil {
		if !usageConfigMapIsOurs(existing, agent) {
			// Someone else parked a ConfigMap under our name. Taking it over
			// would stamp our labels and an owner reference onto an object we
			// do not own, coupling its lifecycle to the CR -- Kubernetes
			// garbage-collects it once the CR it now references is gone; a CR
			// deleted and re-applied under the same name is still ours by name,
			// so that case is unaffected. Leave it, and return the cause so the caller
			// surfaces why status.usage is frozen where the design promises --
			// kubectl describe on the CR -- in the CR's one Warning this poll.
			return &usageConfigMapFault{
					reason:  usageConfigMapForeignReason,
					message: fmt.Sprintf("a ConfigMap named %s already exists and is not the operator's: status.usage will not advance until it is removed", cm.Name),
				},
				fmt.Errorf("the usage counters ConfigMap %s is not the operator's; refusing to overwrite it", cm.Name)
		}
		cm = existing.DeepCopy()
	}
	withCommonLabels(cm, agent)
	if err := controllerutil.SetOwnerReference(agent, cm, p.r.Scheme); err != nil {
		return nil, fmt.Errorf("setting the owner reference on the usage counters ConfigMap: %w", err)
	}
	cm.Data = map[string]string{usageCountersDocumentKey: string(raw)}
	if existing == nil {
		if err := p.r.Create(ctx, cm); err != nil {
			return usageWriteRefused(err, cm.Name), fmt.Errorf("creating the usage counters ConfigMap: %w", err)
		}
		return nil, nil
	}
	if err := p.r.Update(ctx, cm); err != nil {
		if existing.Immutable != nil && *existing.Immutable {
			// An immutable ConfigMap -- hand-edited, or a foreign copy left in
			// place -- rejects every update, so the counters would freeze with
			// nothing but a log line to say why. The immutability is read off the
			// live object, not inferred from the 422: an Invalid the API server
			// returns for some other reason (a validating admission policy, say)
			// is a refused write below, not a standing instruction to delete a
			// ConfigMap that was never immutable. Return the cause so the caller
			// surfaces it where the design promises: `kubectl describe` on the CR.
			return &usageConfigMapFault{
					reason:  usageConfigMapImmutableReason,
					message: fmt.Sprintf("the usage counters ConfigMap %s is immutable: status.usage will not advance until it is deleted so the operator can recreate it", cm.Name),
				},
				fmt.Errorf("updating the usage counters ConfigMap: %w", err)
		}
		if fault := usageWriteRefused(err, cm.Name); fault != nil {
			return fault, fmt.Errorf("updating the usage counters ConfigMap: %w", err)
		}
		return nil, fmt.Errorf("updating the usage counters ConfigMap: %w", err)
	}
	return nil, nil
}

// projectStatus patches status.usage from doc and gauges when the status is
// behind the document's totals, or when a cluster gauge differs from this
// poll's reading in either direction, absent included, with a merge patch
// over the status as read that touches nothing else; gaugesKnown false leaves
// the gauge fields as they are. Skipped while the served CRD is recorded as pruning status.usage, so an
// operator ahead of its CRD costs one probe per interval across both writers;
// the ConfigMap is current throughout, and the patch after the CRD lands
// carries everything accumulated since. A gauge difference alone is likewise
// skipped while the gauges' own pruning record is fresh, the served CRD at the
// previous schema, so the counters that CRD serves keep landing every poll.
func (p *UsageCounterPoller) projectStatus(ctx context.Context, agent *agentv1alpha1.PlatformAgent, doc *usageDocument, gauges *usageClusterGauges, gaugesKnown bool) error {
	tool := doc.Totals[usageCounterToolExecutions]
	events := doc.Totals[usageCounterEventsIngested]
	usage := &agent.Status.Usage
	wantRegistered, wantMonitored := usageGaugeFields(gauges)
	gaugesMoved := gaugesKnown && !p.gaugesPruned(agent) &&
		(!usageGaugeEqual(usage.ClustersRegistered, wantRegistered) || !usageGaugeEqual(usage.ClustersMonitored, wantMonitored))
	behind := usage.ToolExecutionsTotal < tool || usage.EventsIngestedTotal < events || gaugesMoved ||
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
	if gaugesKnown {
		usage.ClustersRegistered = wantRegistered
		usage.ClustersMonitored = wantMonitored
	}
	if err := p.r.Status().Patch(ctx, agent, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("patching status.usage: %w", err)
	}
	// The echoes: counters written non-zero that come back absent are the
	// pruning of status.usage, recorded in the record the Ready writer shares;
	// gauges written as a reading, a zero one included since a pointer to zero
	// serialises, that come back absent are the gauges' own record, since a
	// CRD at the previous schema prunes them and serves the counters. A patch
	// that wrote only a time, or cleared the gauges, says nothing either way.
	if tool > 0 || events > 0 {
		p.r.noteUsageEcho(ctx, agent, agent.Status.Usage.ToolExecutionsTotal == tool && agent.Status.Usage.EventsIngestedTotal == events)
	}
	if gauges != nil {
		p.noteGaugeEcho(ctx, agent, usageGaugeEqual(agent.Status.Usage.ClustersRegistered, wantRegistered) && usageGaugeEqual(agent.Status.Usage.ClustersMonitored, wantMonitored))
	}
	return nil
}

// usageScrapeFailure is one pod whose listener failed a scrape this poll, at or
// past the streak threshold: what the CR's one Warning needs to name it in the
// CR's one Warning and to pick the guidance its kind points at.
type usageScrapeFailure struct {
	name    string
	counter string
	detail  string
	err     error
}

// noteScrapeFailure records target's failed scrape in its streak and, on the
// first failure of a run, logs one line naming the pod and the error kind and
// never the body. It returns the pod's failure descriptor and whether the
// streak has reached usageScrapeFailureEventStreak, the point from which the
// poll reports the pod in the CR's one Warning (recordStandingFailures). A
// shorter streak reports nothing, so an upgrade's gap -- listeners moving
// before the policies admit the operator -- leaves no Warning on a healthy CR.
func (p *UsageCounterPoller) noteScrapeFailure(log logr.Logger, target usageTarget, err error) (usageScrapeFailure, bool) {
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
	// is a closed vocabulary, never a byte the peer sent. It is stable across a
	// standing failure of one kind, which is what lets the recorder fold the
	// repeats; a kind that changes writes a new message, as it should.
	detail := usageScrapeDetail(err)
	if count == 1 {
		log.Info("a metrics listener could not be read; this pod is not counted and its baseline is not advanced until it recovers",
			"pod", target.name, "counter", target.counter, "error", detail)
	}
	return usageScrapeFailure{name: target.name, counter: target.counter, detail: detail, err: err}, count >= usageScrapeFailureEventStreak
}

// recordStandingFailures records at most one Warning on the CR for this poll,
// carrying every standing cause: the listeners that failed this poll past their
// streak, and -- when the fold could not be written -- the ConfigMap fault. The
// event recorder keys its spam filter on source+involvedObject, so every Warning
// on the CR draws on one token bucket (burst 25, one refill per poll interval);
// a second Warning in the same poll, under two standing failures at once, drains
// it over ~25 polls and then starves whichever is recorded second -- here the
// ConfigMap cause the counters freeze on, whose last Event the API server's
// one-hour retention then removes, leaving kubectl describe showing only the
// scrape. One event with one message keeps every cause where the design puts it.
func (p *UsageCounterPoller) recordStandingFailures(agent *agentv1alpha1.PlatformAgent, failures []usageScrapeFailure, fault *usageConfigMapFault) {
	var scrape string
	if len(failures) > 0 {
		scrape = usageScrapeFailureMessage(failures)
	}
	switch {
	case fault != nil && scrape != "":
		// Both causes, one event: the ConfigMap reason -- the cause actually
		// freezing the totals -- with the scrape cause appended, so neither is
		// lost and kubectl describe shows both.
		p.r.recordEvent(agent, corev1.EventTypeWarning, fault.reason, fmt.Sprintf("%s. Also: %s", fault.message, scrape))
	case fault != nil:
		p.r.recordEvent(agent, corev1.EventTypeWarning, fault.reason, fault.message)
	case scrape != "":
		p.r.recordEvent(agent, corev1.EventTypeWarning, usageScrapeFailingReason, scrape)
	}
}

// usageScrapeFailureMessage is the body of the Warning the CR gets for the
// listeners that failed this poll past their streak, naming each pod and ending
// with the distinct guidance their kinds point at. It is byte-stable across
// polls for a stable failing set: the pods are sorted by name and the guidance
// deduplicated in a fixed order, and it carries no per-poll count, so the
// recorder folds the repeats into one live Event with a rising count and a
// refreshed LastTimestamp, and the cause outlives the API server's one-hour
// Event retention instead of standing alone beside a frozen lastActiveTime the
// morning after a NetworkPolicy started blocking the scrape.
func usageScrapeFailureMessage(failures []usageScrapeFailure) string {
	sort.Slice(failures, func(i, j int) bool { return failures[i].name < failures[j].name })
	perPod := make([]string, 0, len(failures))
	picked := map[string]bool{}
	for _, f := range failures {
		perPod = append(perPod, fmt.Sprintf("pod %s (status.usage.%s): %s", f.name, f.counter, f.detail))
		picked[usageScrapeGuidance(f.err)] = true
	}
	var guidance []string
	for _, g := range []string{usageScrapeConnectGuidance, usageScrapeResponseGuidance} {
		if picked[g] {
			guidance = append(guidance, g)
		}
	}
	return fmt.Sprintf("Metrics listeners cannot be scraped, so the totals they feed stop advancing unless another pod carries them: %s. %s",
		strings.Join(perPod, "; "), strings.Join(guidance, " "))
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

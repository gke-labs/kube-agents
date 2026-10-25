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
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// usageNewerFieldGauges names the two cluster gauges' shared pruning
	// record: they arrived in one release and a CRD serves both or neither.
	usageNewerFieldGauges = "clustersRegistered and clustersMonitored"
	// usageNewerFieldLastHumanActive is lastHumanActiveTime's pruning record
	// key, the status field's name.
	usageNewerFieldLastHumanActive = "lastHumanActiveTime"
	// gatewayLeaderLabel is the label leader_elect.py stamps on the gateway pod
	// that holds the lease and runs Hermes, the one the gateway Service selects
	// (platformagent_manifests.go); the chat listener lives in that process.
	gatewayLeaderLabel      = "kubeagents.io/is-leader"
	gatewayLeaderLabelValue = "true"
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
	// usageStatusFieldPrefix is the path a counter projects to, as the Warning
	// names it.
	usageStatusFieldPrefix = "status.usage."
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
// eventsIngestedTotal, remediationsProposedTotal, remediationsMergedTotal,
// chatMessagesInboundTotal, sessionsTotal, toolExecutionsBlockedTotal, brokerFailuresTotal, chatTurnsErroredTotal, backgroundTasksGaveUpTotal, negativeFeedbackTotal and lastActiveTime on every
// PlatformAgent, and the two cluster gauges, from the broker's, the
// watcher's, the chat plugin's and the A2A gateway's metrics listeners, as a
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

	// streaks is the in-memory failure record per listener (pod and port), for the one log line
	// when a listener first fails, the one when it recovers, and the Warning
	// event when a streak reaches usageScrapeFailureEventStreak. It is not
	// state: a leader change starts it afresh, at the cost of one more log
	// line.
	mu      sync.Mutex
	streaks map[usageStreakKey]*usageScrapeStreak
	// prunedNewerFields records, per CR and per newer field -- the cluster
	// gauges as one, remediationsMergedTotal, chatMessagesInboundTotal,
	// toolExecutionsBlockedTotal brokerFailuresTotal, chatTurnsErroredTotal backgroundTasksGaveUpTotal and negativeFeedbackTotal -- the last poll in which that
	// field was written and came back absent: a served CRD at a previous
	// schema, which has the first-schema counters (every CRD with status.usage
	// has carried remediationsProposedTotal and sessionsTotal) and not every
	// later field. A
	// record per field rather than the counters' shared one, or one for all
	// the newer fields, because each record suppresses the writes it covers
	// for the interval and the fields such a CRD does serve should keep
	// landing: a CRD from between the gauges and the merged counter serves
	// the gauges and prunes the counter. Fresh for usageStatusReprobeInterval,
	// then that field is probed again. In memory only, like streaks; keyed
	// like the cursor, then by field.
	prunedNewerFields map[string]map[string]time.Time
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

// usageStreakKey names one listener: a pod can carry two (the gateway's
// watcher and chat ports), each with a streak of its own.
type usageStreakKey struct {
	uid  types.UID
	port string
}

func (t usageTarget) streakKey() usageStreakKey {
	return usageStreakKey{uid: t.uid, port: t.port}
}

// usageTarget is a running pod whose listener the poll reads, and the counters
// its body feeds.
type usageTarget struct {
	uid      types.UID
	name     string
	created  time.Time
	counters []string
	port     string
	addr     string
}

// NewUsageCounterPoller returns the poller for r's PlatformAgents, reading
// the pods' listeners over the pod network.
func NewUsageCounterPoller(r *PlatformAgentReconciler) *UsageCounterPoller {
	return &UsageCounterPoller{
		r:          r,
		source:     newPodUsageSource(),
		now:        time.Now,
		pollBudget: func() time.Duration { return usageCountersPollInterval },
		streaks:    map[usageStreakKey]*usageScrapeStreak{},
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

// newerFieldPruned reports whether the CR's pruning record for field is
// fresh, dropping it once it has expired.
func (p *UsageCounterPoller) newerFieldPruned(agent *agentv1alpha1.PlatformAgent, field string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	key := usagePollKey(agent)
	recorded, ok := p.prunedNewerFields[key][field]
	if !ok {
		return false
	}
	if p.now().Sub(recorded) >= usageStatusReprobeInterval {
		delete(p.prunedNewerFields[key], field)
		return false
	}
	return true
}

// noteNewerFieldEcho is one newer field's own echo record: echoed false is a
// write whose field came back absent, recorded with the poll's time and
// logged once per record, naming the field; echoed true clears it.
func (p *UsageCounterPoller) noteNewerFieldEcho(ctx context.Context, agent *agentv1alpha1.PlatformAgent, field string, echoed bool) {
	key := usagePollKey(agent)
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.prunedNewerFields == nil {
		p.prunedNewerFields = map[string]map[string]time.Time{}
	}
	if echoed {
		delete(p.prunedNewerFields[key], field)
		return
	}
	if p.prunedNewerFields[key] == nil {
		p.prunedNewerFields[key] = map[string]time.Time{}
	}
	_, fresh := p.prunedNewerFields[key][field]
	p.prunedNewerFields[key][field] = p.now()
	if !fresh {
		logf.FromContext(ctx).Info("the served CRD does not serve a status.usage field this operator writes; apply this release's CRD to get it, which is probed again after the interval",
			"platformagent", key, "field", field, "reprobeAfter", usageStatusReprobeInterval.String())
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

// forgetDepartedNewerFieldRecords drops the newer-fields records and gateway-miss
// counts of CRs outside swept, the CRs a completed sweep reached, so the maps do not keep
// an entry per deleted CR for the life of the process.
func (p *UsageCounterPoller) forgetDepartedNewerFieldRecords(swept map[string]bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for key := range p.prunedNewerFields {
		if !swept[key] {
			delete(p.prunedNewerFields, key)
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
		p.forgetDepartedNewerFieldRecords(p.sweptKeys)
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
		reading, err := p.source.Scrape(ctx, target.addr, target.counters)
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
		// One body, one entry per counter it feeds: the fold keeps a baseline
		// per pod and counter.
		for _, counter := range target.counters {
			scraped = append(scraped, usageScrapedPod{
				UID:       string(target.uid),
				Name:      target.name,
				Created:   target.created,
				Counter:   counter,
				Sample:    reading.Samples[counter],
				StartTime: reading.StartTime,
				Clusters:  reading.Clusters,
			})
		}
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
	if result.Changed || (doc != nil && doc.migrated) {
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

// targets lists the pods the policies select and returns the running ones
// whose listener the poll reads, with every pod that exists and is neither
// terminating nor in a terminal phase, scraped or not, in live. When the CR switches the watcher off the gateway pods stay live, so
// their entries persist, and are not read: the entrypoint starts no watcher,
// so nothing listens on the port, and a refused connection there would be the
// install's choice. The chat listener is read on the same terms from a pod
// that runs no gateway: above one replica the container runs leader_elect.py,
// which starts Hermes on the lease holder alone and stamps the leader label
// on that pod, so a standby pod has nothing on the port by design, stays
// live, keeps its entry, and is not read; at one replica the template
// carries the label, and the one pod is read. The chat listener is also not
// read until the platform-agent container is Ready: Hermes binds the plugin's
// port only once the entrypoint has scaffolded every profile, minutes after
// the pod is Running, and the readiness probe is on the API port the same
// process binds, so a pod read earlier would spend the streak on a cold
// start that is going well. A listener this poll decided not to read has
// its streak closed, since a count left open behind a leadership change or
// a restart would meet the next cold start's one failed read and make it a
// Warning.
// usagePollGatewayReplicas is the gateway replica count targets reads the leader
// rule against: the rendered count, which the operator caps at one
// (supportedGatewayReplicas). It is a variable so the multi-replica tests can
// still exercise the leader rule for the redesign in
// https://github.com/gke-labs/kube-agents/issues/3016.
var usagePollGatewayReplicas = func(deployment *agentv1alpha1.DeploymentSpec) int32 {
	replicas, _ := resolveDeploymentReplicasAndStrategy(deployment)
	return replicas
}

func (p *UsageCounterPoller) targets(ctx context.Context, agent *agentv1alpha1.PlatformAgent) ([]usageTarget, map[string]bool, error) {
	replicas := usagePollGatewayReplicas(agent.Spec.Deployment)
	runsGateway := func(pod *corev1.Pod) bool {
		leads := replicas <= 1 || pod.Labels[gatewayLeaderLabel] == gatewayLeaderLabelValue
		return leads && usageContainerReady(pod, platformAgentContainerName)
	}
	everyPod := func(*corev1.Pod) bool { return true }
	groups := []struct {
		selector  map[string]string
		counters  []string
		container string
		port      string
		read      bool
		// podRead narrows read to the pods whose container serves the port.
		podRead func(*corev1.Pod) bool
	}{
		{gatewayPodSelector(agent), usageGatewayCounters, agentAPIAuthContainerName, eventWatcherMetricsPortName, eventWatcherEnabled(agent), everyPod},
		{gatewayPodSelector(agent), usageChatCounters, platformAgentContainerName, chatMetricsPortName, true, runsGateway},
		// The A2A gateway, a pod a next-mode install runs and a today install
		// does not: the chat counter again, from the gateway that owns Chat
		// ingress there. The selector matches no pod elsewhere.
		{a2aGatewayPodSelector(agent), usageA2AChatCounters, a2aGatewayContainerName, a2aGatewayMetricsPortName, true, everyPod},
		{credentialProxySelector(agent), usageBrokerCounters, credentialProxyContainerName, credentialProxyMetricsPortName, true, everyPod},
	}
	live := map[string]bool{}
	var targets []usageTarget
	// One list per distinct selector, not per group: the first two groups read
	// two different containers of the gateway pod and so share its selector.
	// Against the cache that second lookup was free; p.reader() is the API
	// server (a cached Pod List would start a cluster-wide Pod informer, as
	// podReader's comment sets out), where it is a round trip for a list this
	// poll already holds.
	listed := map[string]*corev1.PodList{}
	for _, group := range groups {
		pods, seen := listed[labels.Set(group.selector).String()]
		if !seen {
			pods = &corev1.PodList{}
			if err := p.reader().List(ctx, pods, client.InNamespace(agent.Namespace), client.MatchingLabels(group.selector)); err != nil {
				return nil, nil, fmt.Errorf("listing pods: %w", err)
			}
			listed[labels.Set(group.selector).String()] = pods
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
			if group.read && !group.podRead(pod) {
				p.closeStreak(usageStreakKey{uid: pod.UID, port: group.port})
				continue
			}
			if !group.read || pod.Status.Phase != corev1.PodRunning || pod.Status.PodIP == "" {
				continue
			}
			port, ok := usagePodPort(pod, group.container, group.port)
			if !ok {
				continue
			}
			targets = append(targets, usageTarget{
				uid:      pod.UID,
				name:     pod.Name,
				created:  pod.CreationTimestamp.Time,
				counters: group.counters,
				port:     group.port,
				addr:     net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(int(port))),
			})
		}
	}
	return targets, live, nil
}

// usageContainerReady reports whether the named container of pod has passed
// its readiness probe, as the kubelet last recorded it.
func usageContainerReady(pod *corev1.Pod, container string) bool {
	for _, status := range pod.Status.ContainerStatuses {
		if status.Name == container {
			return status.Ready
		}
	}
	return false
}

// closeStreak drops the failure streak of a listener the poll decided not to
// read, so a count opened while it was read does not outlive the decision.
func (p *UsageCounterPoller) closeStreak(key usageStreakKey) {
	p.mu.Lock()
	defer p.mu.Unlock()
	delete(p.streaks, key)
}

// a2aGatewayPodSelector selects the A2A gateway's pods, the label its fence
// policy and the chart's PodMonitoring select on.
func a2aGatewayPodSelector(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{"app": a2aGatewayName(agent)}
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
	migrateUsageDocument(&doc, usageStatusSeed(agent, now).Totals)
	if reason := usageDocumentInvalid(&doc, cm, agent, now); reason != "" {
		log.Info("the usage counters document failed a read-back bound; re-seeding from the status", "configmap", cm.Name, "reason", reason)
		return cm, nil, nil
	}
	// After the bounds, so that a migrated document the bounds refuse logs
	// the re-seed alone: "nothing lost" is only true of one that passed.
	switch {
	case doc.rekeyed:
		log.Info("the usage counters document was at the previous layout; migrated in place, nothing lost", "configmap", cm.Name)
	case doc.migrated:
		log.Info("the usage counters document predates a counter this operator keeps; the counter's total was added at its status floor, nothing lost", "configmap", cm.Name)
	}
	return cm, &doc, nil
}

// usageDocumentInvalid is why doc is not to be used, or "" when it is. The
// bounds are the design's: the CR's UID in the document and on the owner
// reference, the first-recorded time inside the CR's life and not in the
// future, every sample and total non-negative and finite, every total under
// the float64-exact ceiling and not below the status it projects to.
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
	// A moved time is held inside the CR's life as the first-recorded time
	// is: a zero (metav1.Time renders it as null, which the status patch
	// would turn into a deletion and the echo would record as a pruning CRD),
	// a future, or a time before the CR existed is refused. The human time
	// cannot be later than the last move of any kind, since one stamp sets
	// both.
	if doc.LastMoved != nil && (doc.LastMoved.IsZero() || doc.LastMoved.Time.After(now) || doc.LastMoved.Time.Before(agent.CreationTimestamp.Time)) {
		return "the last-moved time is outside the CR's life"
	}
	if doc.LastHumanMoved != nil && (doc.LastHumanMoved.IsZero() || doc.LastHumanMoved.Time.After(now) || doc.LastHumanMoved.Time.Before(agent.CreationTimestamp.Time) ||
		doc.LastMoved == nil || doc.LastHumanMoved.Time.After(doc.LastMoved.Time)) {
		return "the last-human-moved time is outside the CR's life or after the last move"
	}
	if doc.Totals == nil || doc.Pods == nil {
		return "no totals or no pods"
	}
	floors := usageStatusSeed(agent, now).Totals
	for counter, floor := range floors {
		total, ok := doc.Totals[counter]
		if !ok || total < 0 || total > usageTotalCeiling {
			return "a total is outside its bounds"
		}
		if slices.Contains(usageSessionsCounters, counter) {
			// The pair is held to the status as a sum, below.
			continue
		}
		if total < floor {
			return "a total is below the status it projects to"
		}
	}
	if usageSessionsTotal(doc.Totals) < floors[usageCounterSessionsChat] {
		return "a total is below the status it projects to"
	}
	for _, counter := range doc.Unfolded {
		if !slices.Contains(usageCounters, counter) || doc.Totals[counter] != 0 {
			return "an unfolded counter is not a counter at zero"
		}
	}
	for key, entry := range doc.Pods {
		if entry == nil || entry.Sample < 0 || entry.Marker.IsZero() {
			return "a pod entry is outside its bounds"
		}
		if !slices.Contains(usageCounters, entry.Counter) {
			return "a pod entry names no counter"
		}
		if entry.PodUID == "" || key != usagePodEntryKey(entry.PodUID, entry.Counter) {
			return "a pod entry is not keyed by its pod and counter"
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
		usageCounterToolExecutions:        usageStatusFloor(agent.Status.Usage.ToolExecutionsTotal),
		usageCounterEventsIngested:        usageStatusFloor(agent.Status.Usage.EventsIngestedTotal),
		usageCounterRemediationsProposed:  usageStatusFloor(agent.Status.Usage.RemediationsProposedTotal),
		usageCounterRemediationsMerged:    usageStatusFloor(agent.Status.Usage.RemediationsMergedTotal),
		usageCounterChatMessagesInbound:   usageStatusFloor(agent.Status.Usage.ChatMessagesInboundTotal),
		usageCounterBrokerFailures:        usageStatusFloor(agent.Status.Usage.BrokerFailuresTotal),
		usageCounterChatTurnsErrored:      usageStatusFloor(agent.Status.Usage.ChatTurnsErroredTotal),
		usageCounterBackgroundTasksGaveUp: usageStatusFloor(agent.Status.Usage.BackgroundTasksGaveUpTotal),
		usageCounterNegativeFeedback:      usageStatusFloor(agent.Status.Usage.NegativeFeedbackTotal),
		// One status value for two halves: it seeds the chat half, and the
		// read-back bound holds the pair's sum to it (usageDocumentInvalid).
		usageCounterSessionsTriage:        0,
		usageCounterSessionsChat:          usageStatusFloor(agent.Status.Usage.SessionsTotal),
		usageCounterToolExecutionsBlocked: usageStatusFloor(agent.Status.Usage.ToolExecutionsBlockedTotal),
	}}
	if last := agent.Status.Usage.LastActiveTime; last != nil && !last.Time.After(now) {
		seed.LastMoved = last.DeepCopy()
	}
	if last := agent.Status.Usage.LastHumanActiveTime; last != nil && !last.Time.After(now) && !last.Time.Before(agent.CreationTimestamp.Time) &&
		seed.LastMoved != nil && !last.Time.After(seed.LastMoved.Time) {
		seed.LastHumanMoved = last.DeepCopy()
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
// carries everything accumulated since. A difference in a newer field alone, a
// gauge or a later counter, is likewise skipped while the newer-fields record
// is fresh, the served CRD at a previous schema, so the first counters that
// CRD serves keep landing every poll.
func (p *UsageCounterPoller) projectStatus(ctx context.Context, agent *agentv1alpha1.PlatformAgent, doc *usageDocument, gauges *usageClusterGauges, gaugesKnown bool) error {
	tool := doc.Totals[usageCounterToolExecutions]
	events := doc.Totals[usageCounterEventsIngested]
	proposed := doc.Totals[usageCounterRemediationsProposed]
	merged := doc.Totals[usageCounterRemediationsMerged]
	chat := doc.Totals[usageCounterChatMessagesInbound]
	failures := doc.Totals[usageCounterBrokerFailures]
	errored := doc.Totals[usageCounterChatTurnsErrored]
	background := doc.Totals[usageCounterBackgroundTasksGaveUp]
	negative := doc.Totals[usageCounterNegativeFeedback]
	sessions := usageSessionsTotal(doc.Totals)
	blocked := doc.Totals[usageCounterToolExecutionsBlocked]
	usage := &agent.Status.Usage
	wantRegistered, wantMonitored := usageGaugeFields(gauges)
	// The first-schema counters are in every served CRD that has status.usage;
	// the gauges, the merged counter, the chat counter, the blocked counter and
	// the failures counter, the errored counter and the background-tasks counter and the feedback counter arrived later, each at its own release, and a CRD at a previous schema prunes some or all of
	// them, so a difference in one of them alone waits while that field's
	// record is fresh rather than re-patching every poll, and the others keep
	// landing.
	gaugesPruned := p.newerFieldPruned(agent, usageNewerFieldGauges)
	mergedPruned := p.newerFieldPruned(agent, usageCounterRemediationsMerged)
	chatPruned := p.newerFieldPruned(agent, usageCounterChatMessagesInbound)
	blockedPruned := p.newerFieldPruned(agent, usageCounterToolExecutionsBlocked)
	failuresPruned := p.newerFieldPruned(agent, usageCounterBrokerFailures)
	erroredPruned := p.newerFieldPruned(agent, usageCounterChatTurnsErrored)
	backgroundPruned := p.newerFieldPruned(agent, usageCounterBackgroundTasksGaveUp)
	feedbackPruned := p.newerFieldPruned(agent, usageCounterNegativeFeedback)
	humanPruned := p.newerFieldPruned(agent, usageNewerFieldLastHumanActive)
	humanBehind := !humanPruned && doc.LastHumanMoved != nil && (usage.LastHumanActiveTime == nil || !usage.LastHumanActiveTime.Equal(doc.LastHumanMoved))
	gaugesMoved := gaugesKnown && !gaugesPruned &&
		(!usageGaugeEqual(usage.ClustersRegistered, wantRegistered) || !usageGaugeEqual(usage.ClustersMonitored, wantMonitored))
	newerBehind := (!mergedPruned && usage.RemediationsMergedTotal < merged) || (!chatPruned && usage.ChatMessagesInboundTotal < chat) ||
		(!blockedPruned && usage.ToolExecutionsBlockedTotal < blocked) || (!failuresPruned && usage.BrokerFailuresTotal < failures) ||
		(!erroredPruned && usage.ChatTurnsErroredTotal < errored) || (!backgroundPruned && usage.BackgroundTasksGaveUpTotal < background) ||
		(!feedbackPruned && usage.NegativeFeedbackTotal < negative)
	// sessionsTotal is in the first schema beside the first three counters
	// (every CRD with status.usage has carried it), so it has no record of
	// its own: a served CRD prunes it only with status.usage whole.
	behind := usage.ToolExecutionsTotal < tool || usage.EventsIngestedTotal < events || usage.RemediationsProposedTotal < proposed || usage.SessionsTotal < sessions || newerBehind || gaugesMoved || humanBehind ||
		(doc.LastMoved != nil && (usage.LastActiveTime == nil || !usage.LastActiveTime.Equal(doc.LastMoved)))
	if !behind || p.r.usageStatusPruned(agent) {
		return nil
	}
	base := agent.DeepCopy()
	usage.ToolExecutionsTotal = tool
	usage.EventsIngestedTotal = events
	usage.RemediationsProposedTotal = proposed
	usage.RemediationsMergedTotal = merged
	usage.ChatMessagesInboundTotal = chat
	usage.BrokerFailuresTotal = failures
	usage.ChatTurnsErroredTotal = errored
	usage.BackgroundTasksGaveUpTotal = background
	usage.NegativeFeedbackTotal = negative
	usage.SessionsTotal = sessions
	usage.ToolExecutionsBlockedTotal = blocked
	if doc.LastMoved != nil {
		usage.LastActiveTime = doc.LastMoved.DeepCopy()
	}
	if doc.LastHumanMoved != nil {
		usage.LastHumanActiveTime = doc.LastHumanMoved.DeepCopy()
	}
	if gaugesKnown {
		usage.ClustersRegistered = wantRegistered
		usage.ClustersMonitored = wantMonitored
	}
	if err := p.r.Status().Patch(ctx, agent, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("patching status.usage: %w", err)
	}
	// The echoes: the first-schema counters written non-zero that come back
	// absent are the pruning of status.usage, recorded in the record the Ready
	// writer shares; a newer field written -- a gauge reading, a zero one
	// included since a pointer to zero serialises, or the merged counter
	// non-zero -- that comes back absent is that field's own record, since a
	// CRD at a previous schema prunes one or both and serves the first
	// counters. A patch that wrote only a time, or cleared the gauges, says
	// nothing either way.
	if tool > 0 || events > 0 || proposed > 0 || sessions > 0 {
		p.r.noteUsageEcho(ctx, agent, agent.Status.Usage.ToolExecutionsTotal == tool && agent.Status.Usage.EventsIngestedTotal == events && agent.Status.Usage.RemediationsProposedTotal == proposed && agent.Status.Usage.SessionsTotal == sessions)
	}
	if gauges != nil {
		p.noteNewerFieldEcho(ctx, agent, usageNewerFieldGauges, usageGaugeEqual(agent.Status.Usage.ClustersRegistered, wantRegistered) && usageGaugeEqual(agent.Status.Usage.ClustersMonitored, wantMonitored))
	}
	if merged > 0 {
		p.noteNewerFieldEcho(ctx, agent, usageCounterRemediationsMerged, agent.Status.Usage.RemediationsMergedTotal == merged)
	}
	if chat > 0 {
		p.noteNewerFieldEcho(ctx, agent, usageCounterChatMessagesInbound, agent.Status.Usage.ChatMessagesInboundTotal == chat)
	}
	if blocked > 0 {
		p.noteNewerFieldEcho(ctx, agent, usageCounterToolExecutionsBlocked, agent.Status.Usage.ToolExecutionsBlockedTotal == blocked)
	}
	if failures > 0 {
		p.noteNewerFieldEcho(ctx, agent, usageCounterBrokerFailures, agent.Status.Usage.BrokerFailuresTotal == failures)
	}
	if doc.LastHumanMoved != nil {
		p.noteNewerFieldEcho(ctx, agent, usageNewerFieldLastHumanActive, agent.Status.Usage.LastHumanActiveTime != nil && agent.Status.Usage.LastHumanActiveTime.Equal(doc.LastHumanMoved))
	}
	if errored > 0 {
		p.noteNewerFieldEcho(ctx, agent, usageCounterChatTurnsErrored, agent.Status.Usage.ChatTurnsErroredTotal == errored)
	}
	if background > 0 {
		p.noteNewerFieldEcho(ctx, agent, usageCounterBackgroundTasksGaveUp, agent.Status.Usage.BackgroundTasksGaveUpTotal == background)
	}
	if negative > 0 {
		p.noteNewerFieldEcho(ctx, agent, usageCounterNegativeFeedback, agent.Status.Usage.NegativeFeedbackTotal == negative)
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
	streak := p.streaks[target.streakKey()]
	if streak == nil {
		streak = &usageScrapeStreak{}
		p.streaks[target.streakKey()] = streak
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
			"pod", target.name, "counters", strings.Join(target.counters, ","), "error", detail)
	}
	return usageScrapeFailure{name: target.name, counter: usageStatusFieldList(target.counters), detail: detail, err: err}, count >= usageScrapeFailureEventStreak
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

// usageStatusFieldList names the status fields a pod's counters project to,
// for the Warning: "status.usage.a, status.usage.b".
func usageStatusFieldList(counters []string) string {
	fields := make([]string, 0, len(counters))
	for _, counter := range counters {
		field := usageStatusFieldPrefix + usageStatusFieldFor(counter)
		if !slices.Contains(fields, field) {
			fields = append(fields, field)
		}
	}
	return strings.Join(fields, ", ")
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
		perPod = append(perPod, fmt.Sprintf("pod %s (%s): %s", f.name, f.counter, f.detail))
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
	streak := p.streaks[target.streakKey()]
	delete(p.streaks, target.streakKey())
	p.mu.Unlock()
	if streak != nil {
		log.Info("a metrics listener is readable again", "pod", target.name, "counters", strings.Join(target.counters, ","), "failedPolls", streak.count)
	}
}

// forgetDepartedStreaks drops the streaks of pods outside seen, the pods every
// CR listed in this poll, so the map does not keep an entry per departed pod
// for the life of the process. A CR whose pods could not be listed this poll
// loses its streaks, which costs one more first-failure line, not a count.
func (p *UsageCounterPoller) forgetDepartedStreaks(seen map[string]bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for key := range p.streaks {
		if !seen[string(key.uid)] {
			delete(p.streaks, key)
		}
	}
}

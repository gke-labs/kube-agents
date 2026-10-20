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
	"math"
	"slices"
	"sort"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// The accumulator behind status.usage's counters: pure functions over the
// document the poller keeps in the CR's ConfigMap, so that every branch of the
// design's resets section (docs/designs/usage-counters-producer.md) is a test
// that needs no socket and no API server. usage_counters_poller.go reads and
// writes the document; usage_counters_scrape.go produces the samples.

const (
	// usageDeltaCeiling bounds what one pod's body may add to one counter in
	// one poll, on every adding branch and whatever the gap since the pod was
	// last counted. Sized to what a listener could plausibly count in one
	// usageCountersPollInterval rather than in a pod's lifetime: ten thousand
	// in five minutes is thirty-three commands or accepted events a second,
	// past any honest broker, whose command slots serialise the sandbox's
	// calls, and any honest watcher, whose daemon drops alerts on a daily
	// ceiling far below it. A body whose addition would exceed it is refused
	// with the baseline advanced, so an honest burst past it costs that
	// interval's count once, and a body that is not the listener's can raise
	// a total by at most this much per poll.
	usageDeltaCeiling int64 = 10000
	// usageTotalCeiling is the read-back bound on a stored total: below the
	// int64 headroom the status field has, so that a document a namespace
	// editor raised can neither overflow the status patch nor wrap on the next
	// add. A document past it is treated as absent.
	usageTotalCeiling int64 = math.MaxInt64 / 2
	// usageDocumentVersion is the document's layout. A later layout changes
	// it, so that an older document is migrated where the layout allows
	// (migrateUsageDocument) and re-seeded rather than read wrong otherwise.
	// Version 2 keys the pod entries by pod UID and counter, since the broker
	// pod feeds several counters from one body; version 1 keyed them by pod UID.
	usageDocumentVersion         = 2
	usageDocumentVersionPodKeyed = 1

	// The counters the document keeps, named for the status fields they
	// project to, so the JSON reads beside the status.
	usageCounterToolExecutions       = "toolExecutionsTotal"
	usageCounterEventsIngested       = "eventsIngestedTotal"
	usageCounterRemediationsProposed = "remediationsProposedTotal"
	usageCounterRemediationsMerged   = "remediationsMergedTotal"
	usageCounterChatMessagesInbound  = "chatMessagesInboundTotal"
	// sessionsTotal is two document counters, because its halves aggregate
	// differently (usageAggregationFor): the sessions the watcher opens for
	// triage, read on every gateway replica over the same event stream, and
	// the sessions the chat gateways open for conversations, each listener
	// counting its own. The status field is their sum
	// (usageStatusFieldSessions); neither name is a status field.
	usageCounterSessionsTriage = "sessionsTriage"
	usageCounterSessionsChat   = "sessionsChat"
	// usageStatusFieldSessions is the status field the two sessions
	// counters project to, and the name the Warning for a failed listener
	// carries. The field is in status.usage's first schema, so it has no
	// pruning record of its own.
	usageStatusFieldSessions = "sessionsTotal"
	// usagePodEntryKeySeparator joins a pod UID and a counter into an entry
	// key; a UID carries no slash.
	usagePodEntryKeySeparator = "/"
)

var (
	// usageGatewayCounters, usageChatCounters and usageBrokerCounters are the
	// counters each pod group's body feeds, in the order the stub source hands
	// samples out.
	usageGatewayCounters = []string{usageCounterEventsIngested, usageCounterSessionsTriage}
	usageChatCounters    = []string{usageCounterChatMessagesInbound, usageCounterSessionsChat}
	usageBrokerCounters  = []string{usageCounterToolExecutions, usageCounterRemediationsProposed, usageCounterRemediationsMerged}
	// usageCounters is every counter the document keeps and the status seeds.
	usageCounters = []string{usageCounterToolExecutions, usageCounterEventsIngested, usageCounterRemediationsProposed, usageCounterRemediationsMerged, usageCounterChatMessagesInbound, usageCounterSessionsTriage, usageCounterSessionsChat}
	// usageSessionsCounters is the pair that projects to usageStatusFieldSessions.
	usageSessionsCounters = []string{usageCounterSessionsTriage, usageCounterSessionsChat}
)

// usageStatusFieldFor is the status.usage field a document counter projects
// to: the counter's own name, except the two sessions halves, which share one.
func usageStatusFieldFor(counter string) string {
	if slices.Contains(usageSessionsCounters, counter) {
		return usageStatusFieldSessions
	}
	return counter
}

// usageSessionsTotal is the sum of the two sessions halves in totals: the
// value status.usage.sessionsTotal takes.
func usageSessionsTotal(totals map[string]int64) int64 {
	return totals[usageCounterSessionsTriage] + totals[usageCounterSessionsChat]
}

// usageFieldFloor is the status floor of the field a counter projects to: the
// counter's own for every counter but the sessions halves, whose one status
// value the seed puts in the chat half. The unfolded rule reads this rather
// than the half's own floor, because a zero floor means none of any sample is
// in the total, and after a rollback and return the triage half's share of a
// non-zero status is in the chat half's floor.
func usageFieldFloor(floors map[string]int64, counter string) int64 {
	if slices.Contains(usageSessionsCounters, counter) {
		return floors[usageCounterSessionsChat]
	}
	return floors[counter]
}

// usagePodEntryKey is the document key of one pod's entry for one counter.
func usagePodEntryKey(uid, counter string) string {
	return uid + usagePodEntryKeySeparator + counter
}

// usageZeroTotals is every counter at zero, the totals a fresh document starts
// from before the seed is laid over them.
func usageZeroTotals() map[string]int64 {
	totals := make(map[string]int64, len(usageCounters))
	for _, counter := range usageCounters {
		totals[counter] = 0
	}
	return totals
}

// usageAggregation says how the deltas of the pods feeding one counter combine
// in a poll.
type usageAggregation int

const (
	// usageAggregateSum adds every pod's delta: the broker, whose pods never
	// broker the same command twice.
	usageAggregateSum usageAggregation = iota
	// usageAggregateMax takes the largest delta: the gateway replicas, whose
	// watchers work the same event stream, so the sum would count an event
	// once per replica. With one pod the two are the same.
	usageAggregateMax
)

// usageAggregationFor is the aggregation each counter takes: the watcher's two
// counters, read on every replica over the one event stream, the largest
// delta; every other, the sum.
func usageAggregationFor(counter string) usageAggregation {
	if counter == usageCounterEventsIngested || counter == usageCounterSessionsTriage {
		return usageAggregateMax
	}
	return usageAggregateSum
}

// usageDocument is the accumulator's state for one PlatformAgent: the totals
// and the per-pod baseline together, in the ConfigMap the poller owns, so an
// operator restart loses nothing and the status is only ever a projection of
// it.
type usageDocument struct {
	Version int `json:"version"`
	// AgentUID is the CR the document was accumulated for. A CR deleted and
	// re-applied under the same name must not inherit a predecessor's totals,
	// and a name is not ownership.
	AgentUID string `json:"agentUID"`
	// FirstRecorded is the poll at which every pod was last re-baselined: the
	// first poll, and each re-seed after a mismatch or a failed bound. A pod
	// created after it, or a never-read pod whose Sum-counter process started
	// after it, starts from zero with none of its count seen, the cases in
	// which a whole sample is added.
	FirstRecorded metav1.Time      `json:"firstRecorded"`
	Totals        map[string]int64 `json:"totals"`
	// LastMoved is the poll in which a total last advanced, projected to
	// status.usage.lastActiveTime; nil until one has.
	LastMoved *metav1.Time `json:"lastMoved,omitempty"`
	// Pods is the baseline, keyed by pod UID and counter (usagePodEntryKey).
	Pods map[string]*usagePodEntry `json:"pods"`
	// migrated is set by migrateUsageDocument on a document it changed on read,
	// so the poll writes it back even when the fold changed nothing; rekeyed
	// says the change was the layout, not only a counter the document predated.
	// Not serialised.
	migrated bool
	// Unfolded names the counters migrateUsageDocument added to Totals at
	// zero and no pod has been recorded for since: the status carried nothing
	// for them and no pod's sample has been folded into them through this
	// document, so nothing of any pod's sample has been seen, and a pod older
	// than the document adds its whole sample the way a pod created after
	// FirstRecorded does. Serialised, since the migrating poll may not read
	// the pod that feeds the counter: the document is written at the current
	// layout regardless, and a set that lived only in memory would be gone by
	// the poll that does read it, which would baseline the pod and lose what
	// it counted before the upgrade. A counter leaves the set in the poll
	// that first records a pod for it.
	Unfolded []string `json:"unfolded,omitempty"`
	rekeyed  bool
}

// usageUnfolded reports whether counter is in doc's unfolded set.
func usageUnfolded(doc *usageDocument, counter string) bool {
	return slices.Contains(doc.Unfolded, counter)
}

// migrateUsageDocument brings a document an earlier release wrote to the
// current layout in place. A version-1 document, entries keyed by pod UID with
// no PodUID, has its keys gain their counter and its entries their pod; a
// document of either version that predates a counter gains that counter's
// total, since a release that adds a counter does not change the layout.
// Nothing is lost, which a re-seed cannot say: a re-seed starts from the
// status, which a pruning CRD leaves empty, and re-baselines every pod at its
// current sample, so whatever the listeners counted since the last poll is
// never added. A total the document lacked starts at its floor, the status
// value the read-back holds every total to, rather than at zero: an operator
// that wrote the newer field, was rolled back to one that kept a document
// without it, and came back again finds the status still carrying what it
// wrote, and a zero under that floor would have usageDocumentInvalid refuse
// the document and re-seed it, losing the interval's counts on every counter.
// What the listeners counted during the rollback is not recovered: the pods
// are baselined at their samples, and only a counter whose field's floor is
// zero (usageFieldFloor), so that none of any sample can be in the total, is
// recorded in Unfolded for foldUsage to add whole, on this poll or on the
// first that reads the pod. The two sessions halves share one field: with a
// status that carries sessions, neither half is unfolded, since the triage
// half's share of that status sits in the chat half's floor.
// Any other version is left for usageDocumentInvalid to refuse.
func migrateUsageDocument(doc *usageDocument, floors map[string]int64) {
	if doc.Pods == nil || doc.Totals == nil {
		return
	}
	switch doc.Version {
	case usageDocumentVersionPodKeyed:
		rekeyed := make(map[string]*usagePodEntry, len(doc.Pods))
		for key, entry := range doc.Pods {
			if entry == nil {
				continue
			}
			if entry.PodUID == "" {
				entry.PodUID = key
			}
			rekeyed[usagePodEntryKey(entry.PodUID, entry.Counter)] = entry
		}
		doc.Pods = rekeyed
		doc.Version = usageDocumentVersion
		doc.migrated = true
		doc.rekeyed = true
	case usageDocumentVersion:
	default:
		return
	}
	for _, counter := range usageCounters {
		if _, ok := doc.Totals[counter]; ok {
			continue
		}
		doc.Totals[counter] = floors[counter]
		doc.migrated = true
		if usageFieldFloor(floors, counter) == 0 && !usageUnfolded(doc, counter) {
			doc.Unfolded = append(doc.Unfolded, counter)
		}
	}
	slices.Sort(doc.Unfolded)
}

// usagePodEntry is one pod's baseline for one counter, keyed by
// usagePodEntryKey.
type usagePodEntry struct {
	// Name is for a reader of the ConfigMap; PodUID is the pod the entry
	// belongs to, the half of the key the live set is matched on.
	Name    string `json:"name"`
	PodUID  string `json:"podUID"`
	Counter string `json:"counter"`
	// Sample is the last sample taken from the pod's listener.
	Sample int64 `json:"sample"`
	// StartTime is the process_start_time_seconds the last body carried; nil
	// while the pod's listener predates the gauge.
	StartTime *float64 `json:"startTime,omitempty"`
	// Marker is the poll at which the entry last supplied a delta the total
	// took, the current poll for an entry just recorded, or the sibling's
	// marker the entry was last reset against. A quiet poll does not move it,
	// which is what lets the HA rule tell a pod that was quiet through a gap
	// from one whose sibling was counted during it.
	Marker metav1.Time `json:"marker"`
}

// usageScrapedPod is one pod a poll read: what the pod is and what its body
// said.
type usageScrapedPod struct {
	UID     string
	Name    string
	Created time.Time
	Counter string
	Sample  int64
	// StartTime is nil when the body carried no process_start_time_seconds.
	StartTime *float64
	// Clusters is the watcher's cluster gauges, nil for any pod but a gateway's.
	// Not folded: the poll projects the largest reading as it stands.
	Clusters *usageClusterGauges
}

// usageSeed is what a poll that finds no usable document starts from: the
// status's counters, when it carries any, and the time they last moved.
type usageSeed struct {
	Totals    map[string]int64
	LastMoved *metav1.Time
}

// usageFoldResult is a poll's outcome: the document to keep and whether it
// differs from the one read. Whether a total advanced is the document's
// LastMoved, stamped with the poll that moved it.
type usageFoldResult struct {
	Document *usageDocument
	Changed  bool
}

// usageCandidate is a pod whose body adds under the rules, waiting on the
// aggregation of its counter.
type usageCandidate struct {
	entry     *usagePodEntry
	delta     int64
	sample    int64
	startTime *float64
}

// foldUsage folds one poll's samples into doc under the design's resets rules
// and returns the next document. doc, when not nil, has passed
// usageDocumentInvalid, so its maps exist. A nil doc is a poll with no usable document:
// every scraped pod is recorded at its sample, nothing is added, and the totals
// come from seed. live is every pod that exists, scraped or not; entries for
// pods outside it are dropped, their counts already in the totals. now is the
// poll's time at the precision the document keeps.
func foldUsage(doc *usageDocument, agentUID string, seed usageSeed, live map[string]bool, scraped []usageScrapedPod, now time.Time) usageFoldResult {
	stamp := metav1.NewTime(now)
	ordered := append([]usageScrapedPod(nil), scraped...)
	sort.Slice(ordered, func(i, j int) bool {
		if ordered[i].Name != ordered[j].Name {
			return ordered[i].Name < ordered[j].Name
		}
		return ordered[i].UID < ordered[j].UID
	})

	if doc == nil {
		next := &usageDocument{
			Version:       usageDocumentVersion,
			AgentUID:      agentUID,
			FirstRecorded: stamp,
			Totals:        usageZeroTotals(),
			LastMoved:     seed.LastMoved,
			Pods:          map[string]*usagePodEntry{},
		}
		for counter, total := range seed.Totals {
			if total > 0 {
				next.Totals[counter] = total
			}
		}
		// Every pod is recorded level, at the seed poll's own marker. A re-seed
		// cannot order its replicas on the shared stream: the lifetime sample a
		// watcher reports is the count since its own process started, and the
		// replicas' processes start at different times by construction -- a
		// rollout replaces them one at a time, and the supervisor restarts each
		// watcher in place on its own -- so the larger sample is the older
		// process, not the one that has injected more of the stream. There is no
		// signal at the seed for which replica's informer trails, so none is
		// guessed. A replica that trailed and catches up on a later poll than its
		// sibling is reset against the sibling's marker (the reset in the fold
		// below); the one case that reset cannot reach -- a trailing replica that
		// advances in the same poll as a current sibling, markers level -- is the
		// bounded re-seed over-count the design records, in return for never
		// guessing from samples that cannot say.
		for _, s := range ordered {
			next.Pods[usagePodEntryKey(s.UID, s.Counter)] = &usagePodEntry{Name: s.Name, PodUID: s.UID, Counter: s.Counter, Sample: s.Sample, StartTime: s.StartTime, Marker: stamp}
		}
		return usageFoldResult{Document: next, Changed: true}
	}

	changed := false
	for key, entry := range doc.Pods {
		if !live[entry.PodUID] {
			delete(doc.Pods, key)
			changed = true
		}
	}

	// The markers as read, before this poll records anything: the comparison
	// set is the live pods the document already knew, so an entry recorded
	// this poll, whose marker is this poll, resets no sibling.
	markers := make(map[string]metav1.Time, len(doc.Pods))
	for key, entry := range doc.Pods {
		markers[key] = entry.Marker
	}

	candidates := map[string][]usageCandidate{}
	// The unfolded counters a pod was recorded for this poll, cleared from the
	// set after the loop so that every pod first read in the same poll adds
	// whole. A pod first read on a later poll is baselined: the counter has a
	// recorded baseline by then, and the rule for a pod older than the document
	// applies to it as to any other counter. A second pod of a Sum counter
	// first read later is the under-count that rule already accepts, taken
	// over a set kept per pod.
	folded := map[string]bool{}
	for _, s := range ordered {
		key := usagePodEntryKey(s.UID, s.Counter)
		entry, known := doc.Pods[key]
		if !known {
			// Recorded whatever its sample. Created after the document was
			// first recorded, it started from zero and none of it was seen, so
			// the whole sample adds, under the ceiling; so does a sample for a
			// counter the migration added unfolded, which no document and no
			// status has seen any of; older on any other counter, or past the
			// ceiling, it is recorded and adds nothing.
			entry = &usagePodEntry{Name: s.Name, PodUID: s.UID, Counter: s.Counter, Sample: s.Sample, StartTime: s.StartTime, Marker: stamp}
			doc.Pods[key] = entry
			changed = true
			unfolded := usageUnfolded(doc, s.Counter)
			if unfolded {
				folded[s.Counter] = true
			}
			// A new replica feeding a Max counter beside a live sibling is
			// recorded with the sibling's as-read marker, not this poll's
			// stamp. A sibling counted after this replica started supplied the
			// events it has injected since, so the whole-sample rule does not
			// fit and it adds nothing (the late-read rule). Otherwise it still
			// competes as a candidate below, but behind the sibling, so a
			// catch-up that loses the fold is reset next poll rather than
			// counted on top -- the treatment a known replica behind its
			// sibling gets. The Max branch moves it to stamp only if it is
			// taken or ties.
			if usageAggregationFor(s.Counter) == usageAggregateMax {
				if latest, ok := latestSiblingMarker(doc, markers, s.UID, s.Counter); ok {
					entry.Marker = latest
					if latest.Time.After(s.Created) {
						continue
					}
				} else {
					// No live sibling was read this poll: every replica of this
					// counter is new, so there is no as-read marker to inherit.
					// Record it behind this poll at FirstRecorded, a prior poll,
					// so that the one that loses the fold below is reset next
					// poll rather than counted on top; the winner still advances
					// to stamp through the Max branch. Without this it would keep
					// stamp, read as current next poll, and double-count.
					entry.Marker = doc.FirstRecorded
				}
			}
			// A pod older than the document whose body carries a process
			// start later than the document was first recorded has, for a
			// Sum counter, counted nothing any document has seen either: a
			// pod keeps its entry once read, so only a never-read pod is
			// here, and its process began after the baseline did. The
			// standby that takes the lease is that pod, and its first
			// interval of messages counts. Max counters are read on every
			// replica from the first poll, so none reaches here that way.
			youngProcess := usageAggregationFor(s.Counter) == usageAggregateSum && s.StartTime != nil && *s.StartTime > float64(doc.FirstRecorded.Unix())
			if (s.Created.After(doc.FirstRecorded.Time) || unfolded || youngProcess) && s.Sample <= usageDeltaCeiling {
				candidates[s.Counter] = append(candidates[s.Counter], usageCandidate{entry: entry, delta: s.Sample, sample: s.Sample, startTime: s.StartTime})
			}
			continue
		}
		// The counter is half the key, so only the name can drift.
		if entry.Name != s.Name {
			entry.Name = s.Name
			changed = true
		}

		// A replica behind a live sibling's marker was quiet, or missed, for
		// a poll in which the sibling was counted, so what it presents when
		// it next advances is what the sibling already supplied: reset to its
		// body, take the sibling's marker, add nothing. Before either adding
		// branch, so a restart with a later start time is reset too, since the
		// events its new process replayed are the ones the sibling supplied.
		// Only when it advances: a quiet body neither moves the marker nor
		// writes the document, so the entry still reads as behind when the
		// catch-up arrives.
		if usageAggregationFor(s.Counter) == usageAggregateMax && usageBodyAdvanced(entry, s) {
			if latest, ok := latestSiblingMarker(doc, markers, s.UID, s.Counter); ok && entry.Marker.Before(&latest) {
				// The recorded start time stays when the body carries none,
				// as every other baseline advance keeps it: a reset that
				// dropped it would hand the next gauge-bearing body the
				// whole-sample branch.
				advanceUsageBaseline(entry, s.Sample, s.StartTime)
				entry.Marker = latest
				changed = true
				continue
			}
		}

		delta, adds := usageBranch(entry, s)
		if !adds || delta > usageDeltaCeiling {
			// Refused, and because the body parsed the baseline advances to
			// it: a refusal that kept the baseline would be repeated on every
			// poll until the pod restarted.
			if advanceUsageBaseline(entry, s.Sample, s.StartTime) {
				changed = true
			}
			continue
		}
		candidates[s.Counter] = append(candidates[s.Counter], usageCandidate{entry: entry, delta: delta, sample: s.Sample, startTime: s.StartTime})
	}

	if len(folded) > 0 {
		doc.Unfolded = slices.DeleteFunc(doc.Unfolded, func(counter string) bool { return folded[counter] })
		if len(doc.Unfolded) == 0 {
			doc.Unfolded = nil
		}
	}

	moved := false
	counters := make([]string, 0, len(candidates))
	for counter := range candidates {
		counters = append(counters, counter)
	}
	sort.Strings(counters)
	for _, counter := range counters {
		list := candidates[counter]
		switch usageAggregationFor(counter) {
		case usageAggregateMax:
			// The largest delta, the first in name order on a tie. Every
			// candidate's baseline moves to its sample, so a delta the total
			// did not take is not re-presented. The taken one moves its
			// marker, and so does a candidate whose delta equalled it: both
			// replicas injected the same events, so neither has a catch-up
			// pending, and a marker left behind would reset the next lone
			// advance of a replica whose baseline is current. A smaller
			// delta keeps its marker, so its catch-up is reset rather than
			// counted on top of what the total took. On the first poll after a
			// re-seed, a replica that trailed at the seed and advances in the
			// same poll as a current sibling has a level marker, so this takes
			// the larger delta, which carries its seed catch-up: the bounded
			// re-seed over-count the design records, in return for never
			// guessing which replica trailed from samples that cannot say.
			best := -1
			for i, c := range list {
				if c.delta > 0 && (best < 0 || c.delta > list[best].delta) {
					best = i
				}
			}
			taken := false
			if best >= 0 {
				taken = addUsageTotal(doc, counter, list[best].delta)
			}
			for i, c := range list {
				if advanceUsageBaseline(c.entry, c.sample, c.startTime) {
					changed = true
				}
				if taken && (i == best || c.delta == list[best].delta) {
					c.entry.Marker = stamp
					moved = true
				}
			}
		default:
			for _, c := range list {
				if advanceUsageBaseline(c.entry, c.sample, c.startTime) {
					changed = true
				}
				if addUsageTotal(doc, counter, c.delta) {
					c.entry.Marker = stamp
					moved = true
				}
			}
		}
	}
	if moved {
		doc.LastMoved = &stamp
		changed = true
	}
	return usageFoldResult{Document: doc, Changed: changed}
}

// usageBodyAdvanced reports whether a body says anything the entry does not:
// what advanceUsageBaseline would change, asked of a copy.
func usageBodyAdvanced(entry *usagePodEntry, s usageScrapedPod) bool {
	probe := *entry
	return advanceUsageBaseline(&probe, s.Sample, s.StartTime)
}

// usageBranch classifies a known pod's body against its entry: the delta it
// would add and whether it adds at all. A body that does not add is one that
// is not the listener's by the design's rules, or whose gauge the listener
// cannot have lost; the caller advances the baseline either way.
func usageBranch(entry *usagePodEntry, s usageScrapedPod) (delta int64, adds bool) {
	switch {
	case entry.StartTime == nil && s.StartTime == nil:
		// The rule without the gauge, for a listener from before it shipped:
		// the difference when the sample did not fall.
		if s.Sample >= entry.Sample {
			return s.Sample - entry.Sample, true
		}
		return 0, false
	case entry.StartTime == nil:
		// The first body carrying the gauge for an entry that recorded none:
		// a process that started, a container restarted in place onto a newer
		// image. An absent recorded start time counts as earlier.
		return s.Sample, true
	case s.StartTime == nil:
		// A listener does not lose the gauge inside one pod.
		return 0, false
	case *s.StartTime == *entry.StartTime:
		// One process: a counter cannot fall inside it.
		if s.Sample >= entry.Sample {
			return s.Sample - entry.Sample, true
		}
		return 0, false
	case *s.StartTime > *entry.StartTime:
		// The process restarted inside the same pod; everything it has
		// counted is new.
		return s.Sample, true
	default:
		// A start time earlier than the recorded one is not the listener's.
		return 0, false
	}
}

// advanceUsageBaseline moves entry to the body's sample and, when the body
// carried one, its start time, reporting whether anything changed. A body
// without the gauge leaves the recorded start time alone: the baseline it
// advances is the sample.
func advanceUsageBaseline(entry *usagePodEntry, sample int64, startTime *float64) bool {
	changed := false
	if entry.Sample != sample {
		entry.Sample = sample
		changed = true
	}
	if startTime != nil && (entry.StartTime == nil || *entry.StartTime != *startTime) {
		value := *startTime
		entry.StartTime = &value
		changed = true
	}
	return changed
}

// addUsageTotal adds delta to counter's total and reports whether it did: a
// delta that is not positive, or that would carry the total past its ceiling,
// adds nothing.
func addUsageTotal(doc *usageDocument, counter string, delta int64) bool {
	total := doc.Totals[counter]
	if delta <= 0 || total > usageTotalCeiling-delta {
		return false
	}
	doc.Totals[counter] = total + delta
	return true
}

// latestSiblingMarker is the latest marker among the other pods' entries for
// counter, as the document read them (markers holds the live entries only,
// by entry key), and whether there is one.
func latestSiblingMarker(doc *usageDocument, markers map[string]metav1.Time, uid, counter string) (metav1.Time, bool) {
	var latest metav1.Time
	found := false
	for other, marker := range markers {
		if entry := doc.Pods[other]; entry.PodUID == uid || entry.Counter != counter {
			continue
		}
		if !found || marker.After(latest.Time) {
			latest = marker
			found = true
		}
	}
	return latest, found
}

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
	// it, so that an older document is re-seeded rather than read wrong.
	usageDocumentVersion = 1

	// The counters the document keeps, named for the status fields they
	// project to, so the JSON reads beside the status.
	usageCounterToolExecutions = "toolExecutionsTotal"
	usageCounterEventsIngested = "eventsIngestedTotal"
)

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

// usageAggregationFor is the aggregation each counter takes.
func usageAggregationFor(counter string) usageAggregation {
	if counter == usageCounterEventsIngested {
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
	// first poll, and each re-seed after a mismatch or a failed bound. Only a
	// pod created after it starts from zero with none of its count seen,
	// which is the one case in which a whole sample is added.
	FirstRecorded metav1.Time      `json:"firstRecorded"`
	Totals        map[string]int64 `json:"totals"`
	// LastMoved is the poll in which a total last advanced, projected to
	// status.usage.lastActiveTime; nil until one has.
	LastMoved *metav1.Time `json:"lastMoved,omitempty"`
	// Pods is the baseline, keyed by pod UID.
	Pods map[string]*usagePodEntry `json:"pods"`
}

// usagePodEntry is one pod's baseline.
type usagePodEntry struct {
	// Name is for a reader of the ConfigMap; the key is the UID.
	Name    string `json:"name"`
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
			Totals:        map[string]int64{usageCounterToolExecutions: 0, usageCounterEventsIngested: 0},
			LastMoved:     seed.LastMoved,
			Pods:          map[string]*usagePodEntry{},
		}
		for counter, total := range seed.Totals {
			if total > 0 {
				next.Totals[counter] = total
			}
		}
		for _, s := range ordered {
			next.Pods[s.UID] = &usagePodEntry{Name: s.Name, Counter: s.Counter, Sample: s.Sample, StartTime: s.StartTime, Marker: stamp}
		}
		return usageFoldResult{Document: next, Changed: true}
	}

	changed := false
	for uid := range doc.Pods {
		if !live[uid] {
			delete(doc.Pods, uid)
			changed = true
		}
	}

	// The markers as read, before this poll records anything: the comparison
	// set is the live pods the document already knew, so an entry recorded
	// this poll, whose marker is this poll, resets no sibling.
	markers := make(map[string]metav1.Time, len(doc.Pods))
	for uid, entry := range doc.Pods {
		markers[uid] = entry.Marker
	}

	candidates := map[string][]usageCandidate{}
	for _, s := range ordered {
		entry, known := doc.Pods[s.UID]
		if !known {
			// Recorded whatever its sample. Created after the document was
			// first recorded, it started from zero and none of it was seen, so
			// the whole sample adds, under the ceiling; older, or past the
			// ceiling, it is recorded and adds nothing.
			entry = &usagePodEntry{Name: s.Name, Counter: s.Counter, Sample: s.Sample, StartTime: s.StartTime, Marker: stamp}
			doc.Pods[s.UID] = entry
			changed = true
			// A new replica whose live sibling was counted after it started
			// is the one pod the whole-sample rule does not fit: the sibling
			// supplied the events this pod injected in the meantime. It is
			// recorded at its sample with the sibling's marker and adds
			// nothing, as a known replica behind its sibling would be.
			if usageAggregationFor(s.Counter) == usageAggregateMax {
				if latest, ok := latestSiblingMarker(doc, markers, s.UID, s.Counter); ok && latest.Time.After(s.Created) {
					entry.Marker = latest
					continue
				}
			}
			if s.Created.After(doc.FirstRecorded.Time) && s.Sample <= usageDeltaCeiling {
				candidates[s.Counter] = append(candidates[s.Counter], usageCandidate{entry: entry, delta: s.Sample, sample: s.Sample, startTime: s.StartTime})
			}
			continue
		}
		if entry.Name != s.Name || entry.Counter != s.Counter {
			entry.Name, entry.Counter = s.Name, s.Counter
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
			// counted on top of what the total took.
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

// latestSiblingMarker is the latest marker among the other pods feeding
// counter, as the document read them (markers holds the live entries only),
// and whether there is one.
func latestSiblingMarker(doc *usageDocument, markers map[string]metav1.Time, uid, counter string) (metav1.Time, bool) {
	var latest metav1.Time
	found := false
	for other, marker := range markers {
		if other == uid || doc.Pods[other].Counter != counter {
			continue
		}
		if !found || marker.After(latest.Time) {
			latest = marker
			found = true
		}
	}
	return latest, found
}

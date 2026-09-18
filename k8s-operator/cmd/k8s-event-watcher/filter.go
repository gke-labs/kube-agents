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
	"log"
	"time"
)

// defaultReasons lists the standard Event.Reason values that trigger investigations.
// These cover typical Kubernetes workload and node failures, but operators can
// override this list via the --reason flag.
var defaultReasons = []string{
	"CrashLoopBackOff",
	"ImagePullBackOff",
	"ErrImagePull",
	"OOMKilled",
	"FailedMount",
	"FailedScheduling",
	"BackOff",
	"Unhealthy",
	"NetworkNotReady",
	"NodeNotReady",
	"Evicted",
}

// filterConfig holds the configuration for event filtering rules.
// Loaded from command-line flags and injected to allow independent unit testing.
type filterConfig struct {
	// allowedReasons specifies which event Reasons to watch.
	// Matches are case-sensitive to match Kubernetes CamelCase conventions.
	allowedReasons map[string]struct{}
	// allowedNamespaces restricts event monitoring to specific namespaces.
	// An empty set matches all namespaces.
	allowedNamespaces map[string]struct{}
	// excludedNamespaces suppresses events from these namespaces.
	// Exclude rules take precedence over allowedNamespaces rules.
	excludedNamespaces map[string]struct{}
	// unhealthyMinCount specifies the minimum repeat threshold count for "Unhealthy"
	// events before they pass. This prevents transient probe failures from triggering alerts.
	unhealthyMinCount int
	// backoffMinCount is the same leading-edge debounce for the crash-loop family
	// (any event canonicalizing to "CrashLoopBackOff"). A genuine crash loop climbs
	// Event.Count past the threshold within seconds; a startup race that resolves on
	// its own — an image warming on a fresh Autopilot node, a dependency that is not
	// listening yet — typically never gets there. Without this, a single transient
	// BackOff opens a session and alerts before the pod has had a chance to recover.
	//
	// Deliberately scoped to the crash-loop family. The image-pull family is exempt:
	// the common cause there is a bad tag, which is persistent and should fire fast.
	// (That exemption is too coarse — registry 429s and 5xx land in the same family
	// and do self-clear — but splitting it needs error-class classification rather
	// than a reason match, and is tracked separately.)
	backoffMinCount int
	// imagePullTransientMinCount is the same debounce again, for the half of the
	// image-pull family that self-clears. The exemption noted above is too coarse:
	// registry rate limits, 5xx and connection timeouts canonicalize to exactly the
	// same ImagePullBackOff as a bad tag, and kubelet resolves them on its own retry
	// schedule. Gates pullClassRetryable only — terminal and unclassified causes
	// still fire on event #1, so the failure modes this does not recognize behave
	// exactly as they did before it existed.
	imagePullTransientMinCount int
	// failedSchedulingMinCount is the backstop for FailedScheduling when
	// cluster-autoscaler has said nothing about the pod (no autoscaler, or one
	// that has not evaluated it yet). The scheduler retries a pod it cannot
	// place on every cluster change and at least every five minutes, so the
	// count is a number of failed attempts, not a duration: it climbs within
	// seconds while nodes join and every few minutes on a quiet cluster. The
	// count alone is a delay, not a discriminator, which is why the
	// autoscaler's own verdicts take precedence over it below.
	failedSchedulingMinCount int
	// scaleUpHold is how long a TriggeredScaleUp mark holds the pod's
	// FailedScheduling events, measured from the mark's event time. A ceiling
	// on the hold, not a floor on the alert: it is cluster-autoscaler's own
	// node-provision timeout, past which a pod still pending is stuck whatever
	// the autoscaler last said, and the count backstop applies again.
	scaleUpHold time.Duration
}

// filterThresholds carries the count debounces as a named group. They are all
// small positive ints with the same default, so as positional arguments they were
// one transposition away from silently gating the wrong family — a bug no test
// would catch, since every value is individually plausible.
type filterThresholds struct {
	unhealthyMinCount          int
	backoffMinCount            int
	imagePullTransientMinCount int
	failedSchedulingMinCount   int
	// scaleUpHold is the one duration in the group. It rides here rather than
	// as a fifth positional argument for the same reason the counts do, and
	// zero means "the default", as it does for them.
	scaleUpHold time.Duration
}

const (
	// defaultMinCount applies to every debounce that was left unset. Three is the
	// count at which kubelet's retry schedule has visibly failed to resolve something
	// on its own, and is the value --unhealthy-min-count has always used.
	defaultMinCount = 3
	// defaultFailedSchedulingMinCount is five failed scheduling attempts, and
	// deliberately not a time. On a cluster with cluster-autoscaler the
	// verdict on a new pod arrives seconds after its first attempt (measured
	// on Autopilot: TriggeredScaleUp two seconds after the pod, the count at
	// five fourteen seconds after), so the backstop covers the gap before the
	// verdict and the pods the autoscaler never rules on; on a cluster without
	// one, a pod that has failed five attempts is stuck unless capacity frees
	// on its own. The value is the issue's decision, chosen over three
	// (defaultMinCount) to sit past a normal scale-up's first attempts.
	defaultFailedSchedulingMinCount = 5
	// defaultScaleUpHold is cluster-autoscaler's default max-node-provision-time.
	// A scale-up the autoscaler has not delivered by then is one it has given
	// up on, so a pod still pending past it is reported on the count.
	defaultScaleUpHold = 15 * time.Minute
	// failedSchedulingStaleAfter is how long the scheduler can go without
	// re-emitting a pod's FailedScheduling before the event describes a pod
	// that is no longer pending. kube-scheduler retries every unschedulable
	// pod at least every five minutes (its unschedulable-queue flush), each
	// retry bumping the event's count and LastTimestamp, so on the core/v1
	// recorder a pod still stuck always has a sighting fresher than this. An
	// events.k8s.io/v1 recorder (upstream kube-scheduler, so GKE Standard)
	// writes a continuing series back to the API server on a 30-minute
	// refresh, so there a stuck pod's sighting can read up to 30 minutes old
	// and this check delays its card to the next refresh (observed live: a
	// declined pod held at "25m13s ago", fired at the refresh six minutes
	// later); it never silences it. The check exists for the
	// informer's initial list: after a restart it replays every event still
	// inside the API server's TTL, and a FailedScheduling whose pod scheduled
	// while the watcher was down arrives with a count past the backstop and a
	// TriggeredScaleUp mark that may by then be older than scaleUpHold. The
	// watcher defers the list's FailedScheduling events until the marks are
	// recorded (watcher.go); this check is the other half, for the events
	// whose mark has aged out of the hold. Three flush intervals, so a slow
	// retry is not mistaken for a stale event.
	failedSchedulingStaleAfter = 15 * time.Minute
)

// newFilterConfig creates a new filterConfig, applying defaults for missing values.
func newFilterConfig(reasons []string, allowNamespaces, excludeNamespaces []string, th filterThresholds) filterConfig {
	if len(reasons) == 0 {
		reasons = defaultReasons
	}
	orDefault := func(n int) int {
		if n <= 0 {
			return defaultMinCount
		}
		return n
	}
	failedSchedulingMinCount := th.failedSchedulingMinCount
	if failedSchedulingMinCount <= 0 {
		failedSchedulingMinCount = defaultFailedSchedulingMinCount
	}
	scaleUpHold := th.scaleUpHold
	if scaleUpHold <= 0 {
		scaleUpHold = defaultScaleUpHold
	}
	return filterConfig{
		allowedReasons:             stringSet(reasons),
		allowedNamespaces:          stringSet(allowNamespaces),
		excludedNamespaces:         stringSet(excludeNamespaces),
		unhealthyMinCount:          orDefault(th.unhealthyMinCount),
		backoffMinCount:            orDefault(th.backoffMinCount),
		imagePullTransientMinCount: orDefault(th.imagePullTransientMinCount),
		failedSchedulingMinCount:   failedSchedulingMinCount,
		scaleUpHold:                scaleUpHold,
	}
}

// stringSet converts a slice of strings to a lookup map for fast O(1) checks.
func stringSet(xs []string) map[string]struct{} {
	if len(xs) == 0 {
		return nil
	}
	out := make(map[string]struct{}, len(xs))
	for _, x := range xs {
		if x == "" {
			continue
		}
		out[x] = struct{}{}
	}
	return out
}

// filter evaluates triage events using a filterConfig. It keeps no state
// about events, so one instance serves every cluster's dispatcher; the clock
// is the only thing it reads besides the event, and only for the
// FailedScheduling gate.
type filter struct {
	cfg filterConfig
	// now is the clock the FailedScheduling gate ages marks and events
	// against. nil means time.Now; tests set it.
	now func() time.Time
}

func newFilter(cfg filterConfig) *filter {
	return &filter{cfg: cfg}
}

func (f *filter) clock() time.Time {
	if f.now != nil {
		return f.now()
	}
	return time.Now()
}

// filterGate names the rule that rejected an event, or gateAccepted when none did.
// Reported as the "gate" label on k8s_event_watcher_events_filtered_total: the count
// debounces below deliberately swallow events, and without a per-rule counter a
// threshold tuned too tight is indistinguishable from a watcher that has stopped
// receiving anything. The set is closed and small, so it is safe as a metric label.
type filterGate string

const (
	gateAccepted            filterGate = ""
	gateReason              filterGate = "reason"
	gateNamespaceExcluded   filterGate = "namespace_excluded"
	gateNamespaceNotAllowed filterGate = "namespace_not_allowed"
	gateUnhealthyMinCount   filterGate = "unhealthy_min_count"
	gateBackoffMinCount     filterGate = "backoff_min_count"
	gateImagePullTransient  filterGate = "imagepull_transient_min_count"
	// gateScaleUpMark is a TriggeredScaleUp or NotTriggerScaleUp event: admitted
	// so the dispatcher records the verdict, never forwarded. Counted as filtered
	// because it is; the dispatcher's log line says what it was recorded as.
	gateScaleUpMark filterGate = "scaleup_mark"
	// gateScaleUpHold is a FailedScheduling held because cluster-autoscaler is
	// provisioning a node for the pod.
	gateScaleUpHold filterGate = "scaleup_hold"
	// gateFailedSchedulingMinCount is a FailedScheduling held on the count
	// backstop, with no autoscaler verdict on record.
	gateFailedSchedulingMinCount filterGate = "failedscheduling_min_count"
	// gateFailedSchedulingStale is a FailedScheduling the scheduler stopped
	// re-emitting long enough ago that the pod is no longer pending; see
	// failedSchedulingStaleAfter.
	gateFailedSchedulingStale filterGate = "failedscheduling_stale"
)

// Decide applies the filtering rules in order and returns the first gate that
// rejected the event, or gateAccepted if it passed all of them:
//  1. Reason is allowed.
//  2. Namespace is not explicitly excluded (exclude wins).
//  3. Namespace is in the allowed list (or allowed list is empty).
//  4. Repeat count threshold is met, for the families that flap: "Unhealthy"
//     probe warnings, the crash-loop family, and retryable image pulls.
//  5. FailedScheduling is a stuck pod rather than a scale-up in progress,
//     read from cluster-autoscaler's verdict on the pod when there is one and
//     from the repeat count when there is not (failedSchedulingGate). The two
//     autoscaler verdicts themselves stop here as gateScaleUpMark: they are
//     admitted so the dispatcher can record them, and are never forwarded.
func (f *filter) Decide(ev TriageEvent) filterGate {
	if f.cfg.allowedReasons != nil {
		if _, ok := f.cfg.allowedReasons[ev.Key.Reason]; !ok {
			return gateReason
		}
	}
	if len(f.cfg.excludedNamespaces) > 0 {
		if _, excluded := f.cfg.excludedNamespaces[ev.Namespace]; excluded {
			return gateNamespaceExcluded
		}
	}
	if len(f.cfg.allowedNamespaces) > 0 {
		if _, allowed := f.cfg.allowedNamespaces[ev.Namespace]; !allowed {
			return gateNamespaceNotAllowed
		}
	}
	if ev.Key.Reason == "Unhealthy" && belowMinCount(ev.Count, f.cfg.unhealthyMinCount) {
		return gateUnhealthyMinCount
	}
	// Matched on the canonical reason, not the wire reason: kubelet's repeating
	// crash-loop signal is Reason=BackOff ("Back-off restarting failed container"),
	// while Reason=CrashLoopBackOff is normally a container waiting-state reason
	// rather than an Event.Reason. Both have to hit this gate, and the same
	// Reason=BackOff must NOT hit it when the message says the back-off is an image
	// pull — canonicalizeReason splits those two apart on exactly that distinction.
	if canonicalizeReason(ev.Key.Reason, ev.Message) == "CrashLoopBackOff" && belowMinCount(ev.Count, f.cfg.backoffMinCount) {
		return gateBackoffMinCount
	}
	// Keyed on the class the dispatcher resolved, not on the message in hand: the
	// event carrying "429 Too Many Requests" and the event that actually backs off
	// are two different events. Only pullClassRetryable is gated — a bad tag and
	// anything unrecognized still fire on event #1.
	if ev.PullClass == pullClassRetryable && belowMinCount(ev.Count, f.cfg.imagePullTransientMinCount) {
		return gateImagePullTransient
	}
	if scaleUpVerdictFor(ev.Key.Reason) != scaleUpNone {
		return gateScaleUpMark
	}
	if ev.Key.Reason == reasonFailedScheduling {
		return f.failedSchedulingGate(ev)
	}
	return gateAccepted
}

// failedSchedulingGate decides whether a FailedScheduling event is a stuck
// pod or the scheduler's normal output while capacity arrives. In order:
//
//   - An event the scheduler stopped re-emitting more than
//     failedSchedulingStaleAfter ago is held whatever else is known: the pod
//     it describes has scheduled or gone, and a pod still pending will be
//     sighted again within minutes with a fresh timestamp. Unknown timestamps
//     fail open, as unknown counts do.
//   - A NotTriggerScaleUp mark passes the event at any count. The autoscaler
//     has ruled it cannot help, so waiting for more repeats only delays the
//     incident.
//   - A TriggeredScaleUp mark younger than scaleUpHold holds the event at
//     any count. A node is on its way; the scheduler retries the pod on every
//     cluster change while it joins, and the count reaches any threshold
//     before the node is Ready.
//   - Otherwise the count backstop applies, with the same fail-open on zero
//     as the other debounces.
//
// Not a settle timer: a live event is never delayed by wall-clock time, only
// by what the autoscaler said or by how often the scheduler has repeated it.
// Each decision logs one line, since the deployed install exposes no metrics.
func (f *filter) failedSchedulingGate(ev TriageEvent) filterGate {
	now := f.clock()
	if !ev.LastSeen.IsZero() && now.Sub(ev.LastSeen) > failedSchedulingStaleAfter {
		log.Printf("held %s pod=%s/%s (count=%d): last sighting %s ago, the pod is no longer pending",
			ev.Key.Reason, ev.Namespace, ev.Name, ev.Count, now.Sub(ev.LastSeen).Round(time.Second))
		return gateFailedSchedulingStale
	}
	switch ev.ScaleUp.Verdict {
	case scaleUpDeclined:
		log.Printf("pass %s pod=%s/%s (count=%d): cluster-autoscaler declined to scale up %s ago, short-circuiting the count backstop",
			ev.Key.Reason, ev.Namespace, ev.Name, ev.Count, now.Sub(ev.ScaleUp.At).Round(time.Second))
		return gateAccepted
	case scaleUpTriggered:
		if age := now.Sub(ev.ScaleUp.At); age <= f.cfg.scaleUpHold {
			log.Printf("held %s pod=%s/%s (count=%d): cluster-autoscaler triggered a scale-up %s ago, holding up to %s",
				ev.Key.Reason, ev.Namespace, ev.Name, ev.Count, age.Round(time.Second), f.cfg.scaleUpHold)
			return gateScaleUpHold
		}
		log.Printf("%s pod=%s/%s (count=%d): the scale-up triggered %s ago has passed the %s hold, falling back to the count backstop",
			ev.Key.Reason, ev.Namespace, ev.Name, ev.Count, now.Sub(ev.ScaleUp.At).Round(time.Second), f.cfg.scaleUpHold)
	}
	if belowMinCount(ev.Count, f.cfg.failedSchedulingMinCount) {
		log.Printf("held %s pod=%s/%s (count=%d < %d, no autoscaler verdict on record)",
			ev.Key.Reason, ev.Namespace, ev.Name, ev.Count, f.cfg.failedSchedulingMinCount)
		return gateFailedSchedulingMinCount
	}
	return gateAccepted
}

// belowMinCount reports whether an event's repeat count falls short of a debounce
// threshold, treating a non-positive count as "this emitter does not populate
// Event.Count" and passing it through.
//
// Failing open matters because these gates are purely subtractive: they exist to
// delay a signal that is expected to arrive again, so the worst case for firing too
// early is one noisy alert, while the worst case for holding is a crash loop nobody
// is ever told about. kubelet populates Count on the events this watcher forwards,
// but the core/v1 Event shape also carries events.k8s.io series events, whose count
// lives on a different field and can surface here as zero. A blind spot on the
// primary failure signal is not an acceptable price for suppressing noise.
func belowMinCount(count, min int) bool {
	return count > 0 && count < min
}

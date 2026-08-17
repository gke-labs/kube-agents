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
}

// filterThresholds carries the count debounces as a named group. They are all
// small positive ints with the same default, so as positional arguments they were
// one transposition away from silently gating the wrong family — a bug no test
// would catch, since every value is individually plausible.
type filterThresholds struct {
	unhealthyMinCount          int
	backoffMinCount            int
	imagePullTransientMinCount int
}

// defaultMinCount applies to every debounce that was left unset. Three is the
// count at which kubelet's retry schedule has visibly failed to resolve something
// on its own, and is the value --unhealthy-min-count has always used.
const defaultMinCount = 3

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
	return filterConfig{
		allowedReasons:             stringSet(reasons),
		allowedNamespaces:          stringSet(allowNamespaces),
		excludedNamespaces:         stringSet(excludeNamespaces),
		unhealthyMinCount:          orDefault(th.unhealthyMinCount),
		backoffMinCount:            orDefault(th.backoffMinCount),
		imagePullTransientMinCount: orDefault(th.imagePullTransientMinCount),
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

// filter evaluates triage events using a filterConfig.
type filter struct {
	cfg filterConfig
}

func newFilter(cfg filterConfig) *filter {
	return &filter{cfg: cfg}
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
)

// Decide applies the filtering rules in order and returns the first gate that
// rejected the event, or gateAccepted if it passed all of them:
//  1. Reason is allowed.
//  2. Namespace is not explicitly excluded (exclude wins).
//  3. Namespace is in the allowed list (or allowed list is empty).
//  4. Repeat count threshold is met, for the two families that flap:
//     "Unhealthy" probe warnings and the crash-loop family.
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

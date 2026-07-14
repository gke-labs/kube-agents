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

// defaultReasons is the shipped set of Event.Reason values that
// trigger investigations. Chosen to cover the top-frequency real
// failures per docs/k8s-event-agent-design.md §"Event filter
// allow-list". Operators can override via --reason.
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

// filterConfig captures the sidecar's per-event decision logic.
// Constructed from CLI flags in main.go; injected into the filter
// so tests can override each knob independently.
type filterConfig struct {
	// allowedReasons is the set of Event.Reason values that pass
	// the filter. Case-sensitive match against Event.Reason
	// (k8s uses CamelCase; case-insensitivity would only hide
	// operator typos in configs).
	allowedReasons map[string]struct{}
	// allowedNamespaces, when non-empty, restricts firing to
	// events from these namespaces. Empty = all namespaces.
	// Applied AFTER excludedNamespaces (exclude wins).
	allowedNamespaces map[string]struct{}
	// excludedNamespaces suppresses events from these namespaces
	// even if they'd otherwise pass. Applied before
	// allowedNamespaces so operators can express "all except
	// kube-system" without listing every included namespace.
	excludedNamespaces map[string]struct{}
	// unhealthyMinCount is the special case for the Unhealthy
	// reason: probes flap constantly and firing on every one
	// would drown the sidecar. Require the event's own Count
	// (k8s Event.Count, which repeats-per-source) to reach this
	// value before we pass it. Default 3.
	unhealthyMinCount int
}

// newFilterConfig builds a filterConfig from CLI-shaped inputs.
// Empty slices default to the shipped values; positive counts
// default to their shipped defaults.
func newFilterConfig(reasons []string, allowNamespaces, excludeNamespaces []string, unhealthyMinCount int) filterConfig {
	if len(reasons) == 0 {
		reasons = defaultReasons
	}
	if unhealthyMinCount <= 0 {
		unhealthyMinCount = 3
	}
	fc := filterConfig{
		allowedReasons:     stringSet(reasons),
		allowedNamespaces:  stringSet(allowNamespaces),
		excludedNamespaces: stringSet(excludeNamespaces),
		unhealthyMinCount:  unhealthyMinCount,
	}
	return fc
}

// stringSet converts a []string to a set for O(1) membership tests.
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

// filter decides whether a triage event should proceed to dedup +
// inject. Pure function — same input, same output; no I/O.
type filter struct {
	cfg filterConfig
}

func newFilter(cfg filterConfig) *filter {
	return &filter{cfg: cfg}
}

// Accept returns true if the event passes every filter rule. The
// decision order is deliberate:
//
//  1. Reason must be in the allow-list (or the allow-list is empty
//     meaning "everything" — but that's not a shipped default).
//  2. Namespace must not be in excluded (exclude wins).
//  3. Namespace must be in allowed (or allowed is empty = all).
//  4. Unhealthy special case: repeat count must reach threshold.
func (f *filter) Accept(ev TriageEvent) bool {
	if f.cfg.allowedReasons != nil {
		if _, ok := f.cfg.allowedReasons[ev.Key.Reason]; !ok {
			return false
		}
	}
	if len(f.cfg.excludedNamespaces) > 0 {
		if _, excluded := f.cfg.excludedNamespaces[ev.Namespace]; excluded {
			return false
		}
	}
	if len(f.cfg.allowedNamespaces) > 0 {
		if _, allowed := f.cfg.allowedNamespaces[ev.Namespace]; !allowed {
			return false
		}
	}
	if ev.Key.Reason == "Unhealthy" && ev.Count < f.cfg.unhealthyMinCount {
		return false
	}
	return true
}

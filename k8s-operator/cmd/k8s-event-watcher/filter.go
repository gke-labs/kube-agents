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
}

// newFilterConfig creates a new filterConfig, applying defaults for missing values.
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

// Accept returns true if the event passes the filtering rules in the following order:
// 1. Reason is allowed.
// 2. Namespace is not explicitly excluded (exclude wins).
// 3. Namespace is in the allowed list (or allowed list is empty).
// 4. Repeat count threshold is met (e.g. for "Unhealthy" probe warnings).
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

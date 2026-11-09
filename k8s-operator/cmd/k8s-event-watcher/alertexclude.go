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
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"strconv"
	"strings"
)

// alertExcludeNameWildcard, as the last character of a rule's name, makes the
// rest of the name a prefix.
const alertExcludeNameWildcard = "*"

const (
	alertExcludeRuleLogFormat    = "k8s-event-watcher: alert-exclude rule %d of %d: %s"
	alertExcludeSummaryLogFormat = "k8s-event-watcher: %d alert-exclude rules; events they match open no incident and count as gate=%s"
	alertExcludeRefusedLogFormat = "k8s-event-watcher: ALERT %v — running with no alert-exclude rules, so every event they would match opens an incident"
)

// alertExcludeRule is one entry of --alert-exclude: events it matches open no
// incident, while their cluster stays in scope for reads. Every part is
// optional; a rule matches an event when every part it sets matches, and an
// event is excluded when any rule matches. The JSON keys are chosen so that a
// PlatformAgent CRD field with the same keys can be passed through as it is.
//
// There is no label selector on purpose: the labels on an event are the
// Event object's own (TriageEvent.Labels), not the involved object's, and
// events rarely carry any.
type alertExcludeRule struct {
	Cluster   *alertExcludeCluster `json:"cluster,omitempty"`
	Namespace string               `json:"namespace,omitempty"`
	// Reason matches the event's reason or its canonical reason, without
	// regard to case, as Kind does: CrashLoopBackOff also matches kubelet's
	// Reason=BackOff crash-loop events (canonicalizeReason).
	Reason string `json:"reason,omitempty"`
	// Kind is the involved object's kind, matched without regard to case,
	// as workloadOf does.
	Kind string `json:"kind,omitempty"`
	// Name is the involved object's name, exact or, with a trailing "*", a
	// prefix: a pod's name carries generated suffixes.
	Name string `json:"name,omitempty"`
}

// alertExcludeCluster names a cluster by the scope's three keys. Only
// ClusterName is required. A project or location the event does not carry is
// not compared: the watcher's own --in-cluster cluster carries neither until a
// profile names it (restartDirect), and a rule that names the host by all
// three keys has to match its events from the first one.
type alertExcludeCluster struct {
	ProjectID   string `json:"projectId,omitempty"`
	Location    string `json:"location,omitempty"`
	ClusterName string `json:"clusterName"`
}

// parseAlertExclude reads --alert-exclude: a JSON list of rules, or empty for
// none. Its errors number the rules from 1, as the startup log does. A rule with no parts, or whose only part is a bare wildcard name, is
// refused, since it would silence every alert; the operator's emergency stop
// (spec.harness.eventWatcher.enabled) is the tool for that.
func parseAlertExclude(raw string) ([]alertExcludeRule, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, nil
	}
	dec := json.NewDecoder(bytes.NewReader([]byte(raw)))
	dec.DisallowUnknownFields()
	var rules []alertExcludeRule
	if err := dec.Decode(&rules); err != nil {
		return nil, fmt.Errorf("--alert-exclude is not a JSON list of rules: %w", err)
	}
	if err := dec.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return nil, errors.New("--alert-exclude is not a JSON list of rules: text follows the list")
	}
	for i, r := range rules {
		if r.Cluster == nil && r.Namespace == "" && r.Reason == "" && r.Kind == "" && (r.Name == "" || r.Name == alertExcludeNameWildcard) {
			return nil, fmt.Errorf("--alert-exclude rule %d sets no part, which would silence every alert", i+1)
		}
		if r.Cluster != nil && r.Cluster.ClusterName == "" {
			return nil, fmt.Errorf("--alert-exclude rule %d: cluster needs clusterName", i+1)
		}
		if strings.Contains(strings.TrimSuffix(r.Name, alertExcludeNameWildcard), alertExcludeNameWildcard) {
			return nil, fmt.Errorf("--alert-exclude rule %d: name takes %q only as its last character", i+1, alertExcludeNameWildcard)
		}
	}
	return rules, nil
}

// loadAlertExclude parses --alert-exclude, logs each rule in force and sets
// the two alert-exclude gauges. A malformed list fails open, as the
// entrypoint's tunables do: stopping would end incident detection for every
// cluster to silence a few. It returns no rules then, so a parse error can
// never leave a partial list in force while the log says none is, and the
// refused gauge is 1 so an alert can see it.
func loadAlertExclude(raw string, m *metrics) []alertExcludeRule {
	rules, err := parseAlertExclude(raw)
	if err != nil {
		log.Printf(alertExcludeRefusedLogFormat, err)
		m.alertExcludeRules.Set(0)
		m.alertExcludeRefused.Set(1)
		return nil
	}
	m.alertExcludeRules.Set(float64(len(rules)))
	m.alertExcludeRefused.Set(0)
	for i, r := range rules {
		log.Printf(alertExcludeRuleLogFormat, i+1, len(rules), r)
	}
	if len(rules) > 0 {
		log.Printf(alertExcludeSummaryLogFormat, len(rules), gateAlertExcluded)
	}
	return rules
}

// String is the rule as the startup log names it: each part it sets, and in
// a cluster each key it sets, with the values quoted. A key it leaves unset
// is not printed, so it reads apart from a key set to a literal "*", which
// is compared as the literal "*", not as a wildcard.
func (r alertExcludeRule) String() string {
	var parts []string
	if r.Cluster != nil {
		parts = append(parts, "cluster={"+quotedParts([][2]string{
			{"projectId", r.Cluster.ProjectID}, {"location", r.Cluster.Location}, {"clusterName", r.Cluster.ClusterName},
		})+"}")
	}
	if rest := quotedParts([][2]string{
		{"namespace", r.Namespace}, {"reason", r.Reason}, {"kind", r.Kind}, {"name", r.Name},
	}); rest != "" {
		parts = append(parts, rest)
	}
	return strings.Join(parts, " ")
}

// quotedParts joins the key="value" pairs whose value is set.
func quotedParts(pairs [][2]string) string {
	var parts []string
	for _, p := range pairs {
		if p[1] != "" {
			parts = append(parts, p[0]+"="+strconv.Quote(p[1]))
		}
	}
	return strings.Join(parts, " ")
}

// matches reports whether every part the rule sets matches ev.
func (r alertExcludeRule) matches(ev TriageEvent) bool {
	if c := r.Cluster; c != nil {
		if c.ClusterName != ev.Cluster ||
			(c.ProjectID != "" && ev.Project != "" && c.ProjectID != ev.Project) ||
			(c.Location != "" && ev.Location != "" && c.Location != ev.Location) {
			return false
		}
	}
	if r.Namespace != "" && r.Namespace != ev.Namespace {
		return false
	}
	if r.Reason != "" && !strings.EqualFold(r.Reason, ev.Key.Reason) && !strings.EqualFold(r.Reason, canonicalizeReason(ev.Key.Reason, ev.Message)) {
		return false
	}
	if r.Kind != "" && !strings.EqualFold(r.Kind, ev.KindOfObject) {
		return false
	}
	if r.Name != "" {
		if prefix, ok := strings.CutSuffix(r.Name, alertExcludeNameWildcard); ok {
			return strings.HasPrefix(ev.Name, prefix)
		}
		return r.Name == ev.Name
	}
	return true
}

// alertExcluded reports whether any rule matches ev.
func alertExcluded(rules []alertExcludeRule, ev TriageEvent) bool {
	for _, r := range rules {
		if r.matches(ev) {
			return true
		}
	}
	return false
}

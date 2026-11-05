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
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestParseAlertExclude(t *testing.T) {
	rules, err := parseAlertExclude(`[
		{"namespace": "payments"},
		{"reason": "FailedScheduling", "cluster": {"clusterName": "seeded-capacity"}},
		{"cluster": {"projectId": "p", "location": "us-central1", "clusterName": "seeded-d"}},
		{"namespace": "shop", "kind": "Pod", "name": "inventory-api-*"}
	]`)
	if err != nil {
		t.Fatalf("parseAlertExclude: %v", err)
	}
	if len(rules) != 4 {
		t.Fatalf("got %d rules, want 4", len(rules))
	}
	if c := rules[2].Cluster; c == nil || c.ProjectID != "p" || c.Location != "us-central1" || c.ClusterName != "seeded-d" {
		t.Errorf("rule 2 cluster = %+v", rules[2].Cluster)
	}

	for _, raw := range []string{"", "  ", "[]"} {
		rules, err := parseAlertExclude(raw)
		if err != nil || len(rules) != 0 {
			t.Errorf("parseAlertExclude(%q) = %v, %v; want no rules", raw, rules, err)
		}
	}
}

func TestParseAlertExcludeRefuses(t *testing.T) {
	for _, tc := range []struct {
		name, raw, want string
	}{
		{"not JSON", `namespace=payments`, "not a JSON list"},
		{"an object, not a list", `{"namespace": "payments"}`, "not a JSON list"},
		{"an unknown key", `[{"namespaces": ["payments"]}]`, "not a JSON list"},
		{"a rule with no part", `[{"namespace": "payments"}, {}]`, "rule 1 sets no part"},
		{"a cluster with no name", `[{"cluster": {"projectId": "p"}}]`, "cluster needs clusterName"},
		{"a star inside a name", `[{"name": "api-*-x"}]`, `"*" only as its last character`},
		{"a bare wildcard name", `[{"name": "*"}]`, "rule 0 sets no part"},
		{"text after the list", `[{"namespace": "a"}] [{"namespace": "b"}]`, "text follows the list"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rules, err := parseAlertExclude(tc.raw)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("parseAlertExclude(%s) err = %v; want it to contain %q", tc.raw, err, tc.want)
			}
			// No rule of a refused list may be returned: the caller runs with
			// none, and a partial list could keep the very rule refused.
			if len(rules) != 0 {
				t.Errorf("parseAlertExclude(%s) returned %d rules with its error; want none", tc.raw, len(rules))
			}
		})
	}
}

func TestAlertExcludeRuleMatches(t *testing.T) {
	ev := TriageEvent{
		Key:          EventKey{UID: "u", Reason: "BackOff"},
		Cluster:      "seeded-a",
		Project:      "p",
		Location:     "us-central1",
		Namespace:    "payments",
		KindOfObject: "Pod",
		Name:         "payments-api-7d9f-abcde",
		Message:      "Back-off restarting failed container app in pod payments-api-7d9f-abcde",
	}
	host := ev
	host.Project, host.Location = "", ""

	for _, tc := range []struct {
		name string
		rule alertExcludeRule
		ev   TriageEvent
		want bool
	}{
		{"namespace", alertExcludeRule{Namespace: "payments"}, ev, true},
		{"another namespace", alertExcludeRule{Namespace: "shop"}, ev, false},
		{"the wire reason", alertExcludeRule{Reason: "BackOff"}, ev, true},
		{"the canonical reason", alertExcludeRule{Reason: "CrashLoopBackOff"}, ev, true},
		{"another reason", alertExcludeRule{Reason: "FailedScheduling"}, ev, false},
		{"the wire reason in another case", alertExcludeRule{Reason: "backoff"}, ev, true},
		{"the canonical reason in another case", alertExcludeRule{Reason: "crashloopbackoff"}, ev, true},
		{"cluster by name", alertExcludeRule{Cluster: &alertExcludeCluster{ClusterName: "seeded-a"}}, ev, true},
		{"cluster by all three keys", alertExcludeRule{Cluster: &alertExcludeCluster{ProjectID: "p", Location: "us-central1", ClusterName: "seeded-a"}}, ev, true},
		{"another cluster", alertExcludeRule{Cluster: &alertExcludeCluster{ClusterName: "seeded-b"}}, ev, false},
		{"same name, another project", alertExcludeRule{Cluster: &alertExcludeCluster{ProjectID: "q", ClusterName: "seeded-a"}}, ev, false},
		{"same name, another location", alertExcludeRule{Cluster: &alertExcludeCluster{Location: "europe-west1", ClusterName: "seeded-a"}}, ev, false},
		{"the host cluster by name", alertExcludeRule{Cluster: &alertExcludeCluster{ClusterName: "seeded-a"}}, host, true},
		{"the host cluster by all three keys, before a profile names it", alertExcludeRule{Cluster: &alertExcludeCluster{ProjectID: "p", Location: "us-central1", ClusterName: "seeded-a"}}, host, true},
		{"the host cluster by another name", alertExcludeRule{Cluster: &alertExcludeCluster{ProjectID: "p", ClusterName: "seeded-b"}}, host, false},
		{"kind", alertExcludeRule{Kind: "Pod"}, ev, true},
		{"kind in another case", alertExcludeRule{Kind: "pod"}, ev, true},
		{"another kind", alertExcludeRule{Kind: "Node"}, ev, false},
		{"exact name", alertExcludeRule{Name: "payments-api-7d9f-abcde"}, ev, true},
		{"exact name is not a prefix", alertExcludeRule{Name: "payments-api"}, ev, false},
		{"prefix name", alertExcludeRule{Name: "payments-api-*"}, ev, true},
		{"another prefix", alertExcludeRule{Name: "inventory-api-*"}, ev, false},
		{"every part matches", alertExcludeRule{Cluster: &alertExcludeCluster{ClusterName: "seeded-a"}, Namespace: "payments", Reason: "CrashLoopBackOff", Kind: "Pod", Name: "payments-*"}, ev, true},
		{"one part does not match", alertExcludeRule{Namespace: "payments", Reason: "FailedScheduling"}, ev, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.rule.matches(tc.ev); got != tc.want {
				t.Errorf("%+v matches = %v; want %v", tc.rule, got, tc.want)
			}
		})
	}
}

func TestAlertExcludedIsAnyRule(t *testing.T) {
	ev := TriageEvent{Key: EventKey{Reason: "OOMKilled"}, Namespace: "shop"}
	rules := []alertExcludeRule{{Namespace: "payments"}, {Reason: "OOMKilled"}}
	if !alertExcluded(rules, ev) {
		t.Error("an event the second rule matches is not excluded")
	}
	if alertExcluded(rules[:1], ev) {
		t.Error("an event no rule matches is excluded")
	}
	if alertExcluded(nil, ev) {
		t.Error("an event is excluded with no rules")
	}
}

// alertExcludeFilter is a filter with the FailedScheduling reasons allowed and
// the given rules.
func alertExcludeFilter(rules []alertExcludeRule) *filter {
	cfg := newFilterConfig(append([]string{"BackOff"}, scaleUpReasons...), nil, nil, filterThresholds{})
	cfg.alertExclude = rules
	return newFilter(cfg)
}

func TestFilterDecideAlertExcluded(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	f := alertExcludeFilter([]alertExcludeRule{{Namespace: "payments"}})

	crash := backoffEvent(10)
	crash.Namespace = "payments"
	if got := f.Decide(crash); got != gateAlertExcluded {
		t.Errorf("a crash loop in an excluded namespace: gate %q; want %q", got, gateAlertExcluded)
	}
	// The reason gate runs first: an event off the --reason list is still
	// counted as gate=reason, not as excluded.
	off := crash
	off.Key.Reason = "SomeRandomReason"
	if got := f.Decide(off); got != gateReason {
		t.Errorf("an unlisted reason in an excluded namespace: gate %q; want %q", got, gateReason)
	}
	// The rule decides before the count gates, so a crash loop below the
	// count threshold is counted as excluded too.
	early := backoffEvent(1)
	early.Namespace = "payments"
	if got := f.Decide(early); got != gateAlertExcluded {
		t.Errorf("an early crash loop in an excluded namespace: gate %q; want %q", got, gateAlertExcluded)
	}
	// The autoscaler verdicts are not checked against the rules.
	mark := autoscalerEvent("pod-1", "NotTriggerScaleUp", now)
	mark.Namespace = "payments"
	if got := f.Decide(mark); got != gateScaleUpMark {
		t.Errorf("a scale-up verdict in an excluded namespace: gate %q; want %q", got, gateScaleUpMark)
	}
	other := backoffEvent(10)
	other.Namespace = "shop"
	if got := f.Decide(other); got == gateAlertExcluded {
		t.Errorf("a crash loop in another namespace is excluded")
	}
}

// An excluded event opens no session, sends no inject and leaves no dedup
// entry, and counts under gate=alert_excluded; an event no rule matches still
// fires.
func TestDispatcherAlertExcludedOpensNoIncident(t *testing.T) {
	disp, m, injectCount, injected := newCountingDispatcher(t, filterThresholds{backoffMinCount: 1})
	disp.filter = alertExcludeFilter([]alertExcludeRule{{Namespace: "payments"}})
	ctx := context.Background()

	excluded := backoffEvent(3)
	excluded.Namespace = "payments"
	disp.Dispatch(ctx, excluded)

	if *injectCount != 0 {
		t.Errorf("an excluded event fired %d injects; want 0", *injectCount)
	}
	if got := disp.dedup.Len(); got != 0 {
		t.Errorf("an excluded event left %d dedup entries; want 0", got)
	}
	if got := testutil.ToFloat64(m.eventsFiltered.WithLabelValues("test-cluster", "", "", string(gateAlertExcluded))); got != 1 {
		t.Errorf("events_filtered_total{gate=alert_excluded} = %v; want 1", got)
	}

	kept := backoffEvent(3)
	kept.Key.UID = "pod-2"
	disp.Dispatch(ctx, kept)
	if *injectCount != 1 || (*injected)[0].Namespace != "default" {
		t.Errorf("an event no rule matches fired %d injects (%v); want 1 from namespace default", *injectCount, *injected)
	}
}

// A rule whose reason names a cluster-autoscaler verdict does not drop the
// verdict: the pod's FailedScheduling carries another reason, so the rule
// leaves it to the scale-up hold, which needs the verdict on record.
func TestDispatcherAReasonRuleOnAVerdictLeavesTheScaleUpHold(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	disp, _, injectCount := newScaleUpDispatcher(t, filterThresholds{failedSchedulingMinCount: 1, failedSchedulingMinSpan: -1}, &now)
	cfg := disp.filter.cfg
	cfg.alertExclude = []alertExcludeRule{{Reason: "TriggeredScaleUp"}}
	disp.filter = newFilter(cfg)
	disp.filter.now = func() time.Time { return now }
	ctx := context.Background()

	disp.Dispatch(ctx, autoscalerEvent("pod-1", "TriggeredScaleUp", now))
	if got := disp.scaleUps.Len(); got != 1 {
		t.Fatalf("memo holds %d marks; want 1", got)
	}
	disp.Dispatch(ctx, failedSchedulingEvent("pod-1", 5, now.Add(time.Minute)))
	if *injectCount != 0 {
		t.Errorf("a FailedScheduling under a recorded TriggeredScaleUp fired %d injects; want 0 (held)", *injectCount)
	}
}

func TestParseFlags_AlertExclude(t *testing.T) {
	const raw = `[{"namespace":"payments"}]`
	f, err := parseFlags([]string{"--alert-exclude", raw})
	if err != nil {
		t.Fatalf("parseFlags: %v", err)
	}
	if f.alertExclude != raw {
		t.Errorf("alertExclude = %q; want %q", f.alertExclude, raw)
	}
}

// A malformed --alert-exclude fails open: the watcher logs an ALERT and runs
// with no rules, and stops only later, on the unparseable kubeconfig.
func TestRealMain_RunsWithNoRulesOnAMalformedAlertExclude(t *testing.T) {
	buf := captureLog(t)
	badKubeconfig := filepath.Join(t.TempDir(), "kubeconfig")
	if err := os.WriteFile(badKubeconfig, []byte("not: [a kubeconfig"), 0o600); err != nil {
		t.Fatal(err)
	}

	err := realMain([]string{"--dry-run", "--kubeconfig", badKubeconfig, "--cluster-name", "x", "--alert-exclude", `[{}]`})
	if err == nil || !strings.Contains(err.Error(), "kubeconfig") {
		t.Fatalf("want realMain to go past --alert-exclude and stop on the kubeconfig, got err=%v", err)
	}
	if got := buf.String(); !strings.Contains(got, "ALERT --alert-exclude rule 0 sets no part") || !strings.Contains(got, "running with no alert-exclude rules") {
		t.Errorf("log does not carry the ALERT line:\n%s", got)
	}
}

// loadAlertExclude names every rule in force in the log and sets the two
// gauges: a valid list, an empty one and a refused one.
func TestLoadAlertExclude(t *testing.T) {
	t.Run("valid", func(t *testing.T) {
		buf := captureLog(t)
		m := newMetrics()
		rules := loadAlertExclude(`[{"namespace": "payments"}, {"reason": "FailedScheduling", "cluster": {"clusterName": "seeded-capacity"}}]`, m)
		if len(rules) != 2 {
			t.Fatalf("got %d rules in force, want 2", len(rules))
		}
		log := buf.String()
		for _, want := range []string{
			`alert-exclude rule 1 of 2: namespace="payments"`,
			`alert-exclude rule 2 of 2: cluster="*/*/seeded-capacity" reason="FailedScheduling"`,
			"2 alert-exclude rules;",
		} {
			if !strings.Contains(log, want) {
				t.Errorf("log does not carry %q:\n%s", want, log)
			}
		}
		assertAlertExcludeGauges(t, m, 2, 0)
	})
	t.Run("empty", func(t *testing.T) {
		for _, raw := range []string{"", "[]"} {
			buf := captureLog(t)
			m := newMetrics()
			if rules := loadAlertExclude(raw, m); len(rules) != 0 {
				t.Errorf("loadAlertExclude(%q) = %v; want no rules", raw, rules)
			}
			if log := buf.String(); strings.Contains(log, "alert-exclude") {
				t.Errorf("loadAlertExclude(%q) logged a rule line:\n%s", raw, log)
			}
			assertAlertExcludeGauges(t, m, 0, 0)
		}
	})
	t.Run("refused", func(t *testing.T) {
		for _, raw := range []string{`namespace=payments`, `[{"namespace": "payments"}, {}]`} {
			buf := captureLog(t)
			m := newMetrics()
			if rules := loadAlertExclude(raw, m); rules != nil {
				t.Errorf("loadAlertExclude(%q) = %v; want no rules in force", raw, rules)
			}
			log := buf.String()
			if !strings.Contains(log, "ALERT --alert-exclude") || !strings.Contains(log, "running with no alert-exclude rules") || strings.Contains(log, "alert-exclude rule 1 of") {
				t.Errorf("loadAlertExclude(%q) log:\n%s", raw, log)
			}
			assertAlertExcludeGauges(t, m, 0, 1)
		}
	})
}

func assertAlertExcludeGauges(t *testing.T, m *metrics, rules, refused float64) {
	t.Helper()
	if got := testutil.ToFloat64(m.alertExcludeRules); got != rules {
		t.Errorf("k8s_event_watcher_alert_exclude_rules = %v, want %v", got, rules)
	}
	if got := testutil.ToFloat64(m.alertExcludeRefused); got != refused {
		t.Errorf("k8s_event_watcher_alert_exclude_refused = %v, want %v", got, refused)
	}
}

// A rule's log form names each part it sets, quoted, and a cluster key it
// leaves unset as "*".
func TestAlertExcludeRuleString(t *testing.T) {
	for _, tc := range []struct {
		rule alertExcludeRule
		want string
	}{
		{alertExcludeRule{Namespace: "payments"}, `namespace="payments"`},
		{alertExcludeRule{Kind: "Pod", Name: "inventory-api-*"}, `kind="Pod" name="inventory-api-*"`},
		{alertExcludeRule{Reason: "BackOff", Namespace: "shop"}, `namespace="shop" reason="BackOff"`},
		{alertExcludeRule{Cluster: &alertExcludeCluster{ClusterName: "seeded-a"}}, `cluster="*/*/seeded-a"`},
		{alertExcludeRule{Cluster: &alertExcludeCluster{ProjectID: "p", Location: "us-central1", ClusterName: "seeded-a"}}, `cluster="p/us-central1/seeded-a"`},
		{alertExcludeRule{Cluster: &alertExcludeCluster{Location: "us-central1", ClusterName: "seeded-a"}}, `cluster="*/us-central1/seeded-a"`},
		{alertExcludeRule{Namespace: "a\nb"}, `namespace="a\nb"`},
	} {
		if got := tc.rule.String(); got != tc.want {
			t.Errorf("%+v.String() = %s, want %s", tc.rule, got, tc.want)
		}
	}
}

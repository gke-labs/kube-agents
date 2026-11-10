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
	"encoding/json"
	"reflect"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The operator marshals spec.harness.eventWatcher.alertExclude into
// WATCHER_ALERT_EXCLUDE as it is, and the watcher parses it with unknown keys
// refused. A field added to the CRD and not here would make the watcher refuse
// the whole list and run with no rules, so every CRD field is set below and
// has to arrive.
func TestTheCRDRuleListParsesHere(t *testing.T) {
	crd := []agentv1alpha1.EventWatcherAlertExcludeRule{
		{
			Cluster:   &agentv1alpha1.EventWatcherAlertExcludeCluster{ProjectID: "p", Location: "us-central1", ClusterName: "batch"},
			Namespace: "shop",
			Reason:    "CrashLoopBackOff",
			Kind:      "Pod",
			Name:      "inventory-api-*",
		},
		{Cluster: &agentv1alpha1.EventWatcherAlertExcludeCluster{ClusterName: "staging-a"}},
		{Namespace: "shop", Name: "*"},
	}
	raw, err := json.Marshal(crd)
	if err != nil {
		t.Fatal(err)
	}
	rules, err := parseAlertExclude(alertExcludeFlag, string(raw))
	if err != nil {
		t.Fatalf("parseAlertExclude(%s): %v", raw, err)
	}
	want := []alertExcludeRule{
		{
			Cluster:   &alertExcludeCluster{ProjectID: "p", Location: "us-central1", ClusterName: "batch"},
			Namespace: "shop",
			Reason:    "CrashLoopBackOff",
			Kind:      "Pod",
			Name:      "inventory-api-*",
		},
		{Cluster: &alertExcludeCluster{ClusterName: "staging-a"}},
		{Namespace: "shop", Name: "*"},
	}
	if !reflect.DeepEqual(rules, want) {
		t.Errorf("parsed %+v, want %+v", rules, want)
	}

	// Every field of the CRD types has a counterpart here.
	for _, pair := range [][2]reflect.Type{
		{reflect.TypeOf(agentv1alpha1.EventWatcherAlertExcludeRule{}), reflect.TypeOf(alertExcludeRule{})},
		{reflect.TypeOf(agentv1alpha1.EventWatcherAlertExcludeCluster{}), reflect.TypeOf(alertExcludeCluster{})},
	} {
		if pair[0].NumField() != pair[1].NumField() {
			t.Errorf("%s has %d fields, %s has %d", pair[0], pair[0].NumField(), pair[1], pair[1].NumField())
		}
	}
}

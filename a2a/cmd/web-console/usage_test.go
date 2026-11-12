/*
Copyright 2026 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// fullUsageStatus is a status.usage block as the operator writes it, with
// every list the console reads populated, six topTools rows (one over the
// cap), and three decoys the response must never repeat: a top-level
// clusterName, a namespace inside a tools row, and a counter outside the
// lists (positiveFeedbackTotal).
const fullUsageStatus = `{
	"clustersRegistered": 3, "clustersMonitored": 2, "chatMessagesInboundTotal": 41, "sessionsTotal": 12,
	"chatSessionsTotal": 9, "toolExecutionsTotal": 287, "toolExecutionsBlockedTotal": 2, "brokerFailuresTotal": 1,
	"eventsIngestedTotal": 120, "chatTurnsErroredTotal": 3, "negativeFeedbackTotal": 1, "positiveFeedbackTotal": 4,
	"remediationsProposedTotal": 5, "remediationsMergedTotal": 2, "proposalsAuthored": 5, "remediationsOpen": 1,
	"remediationsClosedUnmergedTotal": 2, "proposalHumanTouches": 1, "proposalsMergedUntouched": 1,
	"peopleSeen7d": 2, "peopleSeen30d": 4,
	"proposalsFromChat": 3, "proposalsFromConsole": 1, "proposalsFromApi": 0, "proposalsFromEvent": 0,
	"proposalsFromDrift": 0, "proposalsFromAudit": 0, "proposalsFromScheduled": 0, "proposalsFromIssue": 0, "proposalsFromOther": 0,
	"inputTokensTotal": 1234567, "outputTokensTotal": 89012, "spendMicrosUSDTotal": 4567890,
	"activeInterfaces": ["slack", "dashboard"],
	"lastActiveTime": "2026-11-11T09:12:00Z", "lastHumanActiveTime": "2026-11-10T17:00:00Z",
	"topTools": [
		{"tool": "kubectl", "subcommand": "get", "total": 120, "success": 118, "error": 2, "namespace": "acme-payments"},
		{"tool": "gcloud", "subcommand": "container", "total": 30, "success": 30},
		{"tool": "feedback", "subcommand": "submit", "total": 1, "success": 1},
		{"tool": "git", "subcommand": "none", "total": 4, "success": 3, "blocked": 1},
		{"tool": "gh", "subcommand": "pr", "total": 2, "success": 2},
		{"tool": "kubectl", "subcommand": "logs", "total": 1, "busy": 1}
	],
	"clusterName": "acme-prod-1"
}`

// fullUsageLines is the gateway block's usage, tools and performance lines
// for fullUsageStatus.
var fullUsageLines = []string{
	"usage:",
	"  clustersRegistered: 3",
	"  clustersMonitored: 2",
	"  chatMessagesInboundTotal: 41",
	"  sessionsTotal: 12",
	"  chatSessionsTotal: 9",
	"  toolExecutionsTotal: 287",
	"  toolExecutionsBlockedTotal: 2",
	"  brokerFailuresTotal: 1",
	"  eventsIngestedTotal: 120",
	"  chatTurnsErroredTotal: 3",
	"  backgroundTasksGaveUpTotal: 0",
	"  negativeFeedbackTotal: 1",
	"  remediationsProposedTotal: 5",
	"  remediationsMergedTotal: 2 (as the forge reports)",
	"  proposalsAuthored: 5 (as the forge reports)",
	"  remediationsOpen: 1 (as the forge reports)",
	"  remediationsClosedUnmergedTotal: 2 (as the forge reports)",
	"  proposalHumanTouches: 1 (as the forge reports)",
	"  proposalsMergedUntouched: 1 (as the forge reports)",
	"  peopleSeen7d: 2 (as the gateway reports)",
	"  peopleSeen30d: 4 (as the gateway reports)",
	"  proposals by origin: chat 3, console 1 (as the agent reports)",
	"  lastActive: 2026-11-11T09:12:00Z",
	"  lastHumanActive: 2026-11-10T17:00:00Z",
	"tools (top 5, since broker start):",
	"  kubectl/get: 120 (success 118, error 2)",
	"  gcloud/container: 30 (success 30)",
	"  feedback/submit: 1 (success 1)",
	"  git: 4 (success 3, blocked 1)",
	"  gh/pr: 2 (success 2)",
	"performance:",
	"  tokens (since install): in 1234567 / out 89012",
	"  spend (since install): 4.57 USD",
}

// usageDecoys are the fixture's values no response may carry.
var usageDecoys = []string{"acme-prod-1", "acme-payments", "positiveFeedbackTotal"}

func decodeUsageFixture(t *testing.T, text string) map[string]any {
	t.Helper()
	var raw map[string]any
	if err := json.Unmarshal([]byte(text), &raw); err != nil {
		t.Fatalf("fixture: %v", err)
	}
	return raw
}

// fakeUsageStatusAPI answers the PlatformAgent get with the given status
// JSON and every other read with an empty list, as the insights route's
// fake. statusUsage is the status.usage value, or "" for a status without
// the key.
func fakeUsageStatusAPI(t *testing.T, statusUsage string) *httptest.Server {
	t.Helper()
	status := `{"phase":"Ready","lastReconcileTime":"2026-10-09T08:54:42Z"}`
	if statusUsage != "" {
		status = `{"phase":"Ready","lastReconcileTime":"2026-10-09T08:54:42Z","usage":` + statusUsage + `}`
	}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if strings.HasPrefix(r.URL.Path, "/apis/kubeagents.x-k8s.io/v1alpha1/namespaces/kubeagents-system/platformagents/platform-agent") {
			fmt.Fprintf(w, `{"metadata":{"name":"platform-agent","creationTimestamp":"2026-10-06T19:07:47Z","generation":2},"status":%s}`, status)
			return
		}
		if r.URL.Path == "/version" {
			fmt.Fprint(w, `{"major":"1","minor":"33","gitVersion":"v1.33.4","platform":"linux/amd64"}`)
			return
		}
		fmt.Fprint(w, `{"metadata":{},"items":[]}`)
	}))
}

func TestDecodeUsageKeepsOnlyTheAllowlistedFields(t *testing.T) {
	fields := decodeUsage(decodeUsageFixture(t, fullUsageStatus))
	for _, decoy := range []string{"clusterName", "positiveFeedbackTotal"} {
		if _, has := fields[decoy]; has {
			t.Errorf("decoded %q, a field outside the lists", decoy)
		}
	}
	for _, list := range [][]string{usageCounters, usageOriginFields, usageTotalFields, usageTimestampFields} {
		for _, field := range list {
			if field == "backgroundTasksGaveUpTotal" {
				continue // absent from the fixture, as the operator omits a zero
			}
			if _, has := fields[field]; !has {
				t.Errorf("field %q was not decoded", field)
			}
		}
	}
	if got := fields["inputTokensTotal"]; got != int64(1234567) {
		t.Errorf("inputTokensTotal = %v (%T), want int64 1234567", got, got)
	}
	rows, ok := fields[usageTopToolsKey].([]map[string]any)
	if !ok || len(rows) != 6 {
		t.Fatalf("topTools = %v, want six rows", fields[usageTopToolsKey])
	}
	if _, has := rows[0]["namespace"]; has {
		t.Errorf("a tools row kept a field outside the row's vocabulary: %v", rows[0])
	}
	if rows[0]["tool"] != "kubectl" || rows[0]["success"] != int64(118) || rows[0]["total"] != int64(120) {
		t.Errorf("row 0 = %v", rows[0])
	}
	if interfaces, _ := fields[usageActiveInterfacesField].([]string); len(interfaces) != 2 {
		t.Errorf("activeInterfaces = %v", fields[usageActiveInterfacesField])
	}
	if decodeUsage(nil) != nil {
		t.Error("a status without the usage key should decode to nil, not an empty block")
	}
	if got := decodeUsage(map[string]any{}); got == nil || len(got) != 0 {
		t.Errorf("an empty usage block should decode to an empty map, got %v", got)
	}
}

func TestRenderUsageLinesPrintsTheGatewayBlock(t *testing.T) {
	got := newUsageStatus(decodeUsageFixture(t, fullUsageStatus))
	if got == nil {
		t.Fatal("no usage status")
	}
	if strings.Join(got.Lines, "\n") != strings.Join(fullUsageLines, "\n") {
		t.Errorf("lines:\n%s\nwant:\n%s", strings.Join(got.Lines, "\n"), strings.Join(fullUsageLines, "\n"))
	}
	if interfaces, _ := got.Fields[usageActiveInterfacesField].([]string); strings.Join(interfaces, ",") != "dashboard,slack" {
		t.Errorf("activeInterfaces = %v, want sorted", got.Fields[usageActiveInterfacesField])
	}
	text := strings.Join(got.Lines, "\n")
	for _, decoy := range usageDecoys {
		if strings.Contains(text, decoy) {
			t.Errorf("the rendered block carried %q", decoy)
		}
	}
}

func TestRenderUsageLinesOnAnEmptyBlock(t *testing.T) {
	got := newUsageStatus(map[string]any{})
	text := strings.Join(got.Lines, "\n")
	for _, want := range []string{
		"  clustersRegistered: 0",
		"  remediationsMergedTotal: no reading (as the forge reports)",
		"  peopleSeen30d: no reading (as the gateway reports)",
		"  proposals by origin: no reading (as the agent reports)",
		"  lastActive: never",
		"  lastHumanActive: never",
		"tools (top 5, since broker start): unavailable",
		"  tokens (since install): in 0 / out 0",
		"  spend (since install): 0.00 USD",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("missing %q in:\n%s", want, text)
		}
	}
	if _, ok := usageTotalsOf(got.Fields); ok {
		t.Error("an empty block has no totals")
	}
	if lines := renderTopTools(map[string]any{usageTopToolsKey: []map[string]any{}}); strings.Join(lines, "\n") != "tools (top 5, since broker start): none" {
		t.Errorf("empty topTools = %q", lines)
	}
	if line := usageOriginLine(map[string]any{"proposalsFromChat": int64(0)}); line != "  proposals by origin: none (as the agent reports)" {
		t.Errorf("all-zero origins = %q", line)
	}
}

func TestUsageTotalsOfReadsThePresentTotals(t *testing.T) {
	totals, ok := usageTotalsOf(decodeUsage(decodeUsageFixture(t, fullUsageStatus)))
	if !ok || totals.InputTokens != 1234567 || totals.OutputTokens != 89012 || totals.SpendMicrosUSD != 4567890 {
		t.Errorf("totals = %+v, %v", totals, ok)
	}
	totals, ok = usageTotalsOf(decodeUsage(map[string]any{"spendMicrosUSDTotal": float64(250000)}))
	if !ok || totals.InputTokens != 0 || totals.SpendMicrosUSD != 250000 {
		t.Errorf("one total present = %+v, %v; want present with the others zero", totals, ok)
	}
	if _, ok := usageTotalsOf(nil); ok {
		t.Error("nil fields have no totals")
	}
}

// The page is the reader of what GET /api/environment carries for a bug
// report: the rendered lines go into the Agent usage section and the sorted
// interface words into the Chat interfaces line beside the versions. Without
// the second read the response would carry activeInterfaces and no report
// would show it.
func TestThePageAttachesTheUsageLinesAndTheInterfaces(t *testing.T) {
	page, err := staticFS.ReadFile("static/index.html")
	if err != nil {
		t.Fatal(err)
	}
	for _, read := range []string{"...env.usage.lines", "env.usage.fields.activeInterfaces", "- Chat interfaces: "} {
		if !strings.Contains(string(page), read) {
			t.Errorf("the page does not read %q", read)
		}
	}
}

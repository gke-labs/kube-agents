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
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
)

func TestClassifySession(t *testing.T) {
	console := sessionIDPrefix + strings.Repeat("a", 32)
	for _, tc := range []struct {
		id, source, title, want string
	}{
		{"k8s-evt-be3abad3", "api_server", "Triage k8s-evt-be3abad3", kindEventTriage},
		{"cron-policy-sweep-20261006", "api_server", "Triage cron-policy-sweep-20261006", kindScheduled},
		{console, "api_server", "Web console aaaa", kindConsole},
		{"20260101_x", "slack", "Why is web-7 crashlooping?", kindChat},
		{"20260101_y", "google_chat", "Deploy status", kindChat},
		// A chat session cannot become event triage by its title.
		{"20260101_z", "slack", "Triage k8s-evt-123", kindChat},
		{"api_123", "api_server", "Triage and resolve acme/toolkit#42", kindOther},
		{"orphan", "", "Triage k8s-evt-00000001", kindOther},
		// A title that only starts like the watcher's, or names another ID,
		// is not event triage or a scheduled check.
		{"k8s-evt-be3abad3", "api_server", "Triage k8s-evt-be3abad3 and more", kindOther},
		{"k8s-evt-be3abad3", "api_server", "Triage k8s-evt-00000001", kindOther},
		{"api_456", "api_server", "Triage k8s-evt-be3abad3", kindOther},
		{"api_789", "api_server", "Triage cron-policy-sweep-20261006", kindOther},
		// The ID must have the shape session_kv_server.py mints.
		{"k8s-evt-123", "api_server", "Triage k8s-evt-123", kindOther},
		{"k8s-evt-BE3ABAD3", "api_server", "Triage k8s-evt-BE3ABAD3", kindOther},
		{"cron-policy-sweep", "api_server", "Triage cron-policy-sweep", kindOther},
		{"cron-Policy-20261006", "api_server", "Triage cron-Policy-20261006", kindOther},
		{"cron-platform_policy-20261006", "api_server", "Triage cron-platform_policy-20261006", kindScheduled},
		{"a2a-ctx-slack-thread-1", "api_server", "Triage a2a-ctx-slack-thread-1", kindChat},
	} {
		if got := classifySession(tc.id, tc.source, tc.title); got != tc.want {
			t.Errorf("classifySession(%q, %q, %q) = %q, want %q", tc.id, tc.source, tc.title, got, tc.want)
		}
	}
}

func TestSpoofedTitlesGetNoContentAndNoReplies(t *testing.T) {
	fake, h := setup(t)
	fake.seed("api_456", "api_server", "Triage k8s-evt-be3abad3")
	fake.seed("k8s-evt-0000000c", "api_server", "Triage k8s-evt-0000000c: injected")
	fake.mu.Lock()
	fake.post("api_456", "user", "private")
	fake.mu.Unlock()
	for _, sid := range []string{"api_456", "k8s-evt-0000000c"} {
		if rec := serve(h, transcriptReq(sid)); rec.Code != http.StatusForbidden {
			t.Errorf("transcript of %s: status %d, want 403", sid, rec.Code)
		}
		if rec := serve(h, chatReq(`{"message":"hi","session_id":"`+sid+`"}`)); rec.Code != http.StatusForbidden {
			t.Errorf("reply into %s: status %d, want 403", sid, rec.Code)
		}
	}
	rec := serve(h, channelReq("alerts"))
	if strings.Contains(rec.Body.String(), "api_456") || strings.Contains(rec.Body.String(), "k8s-evt-0000000c") {
		t.Errorf("a spoofed session reached # alerts: %s", rec.Body.String())
	}
	if n := fake.gets("api_456"); n != 0 {
		t.Errorf("the console read a spoofed session %d times", n)
	}
}

func TestSummaryLine(t *testing.T) {
	if got := summaryLine("\n\n  first line  \nsecond"); got != "first line" {
		t.Errorf("summaryLine = %q", got)
	}
	long := strings.Repeat("é", summaryMaxRunes+10)
	got := summaryLine(long)
	if !strings.HasSuffix(got, ellipsis) || len([]rune(got)) != summaryMaxRunes+1 {
		t.Errorf("long line = %q (%d runes), want %d runes and an ellipsis", got, len([]rune(got)), summaryMaxRunes)
	}
}

func TestSummarySubjectPrefersTheTriageCardTitle(t *testing.T) {
	prompt := "A Kubernetes Warning event needs triage on GKE cluster 'c1'. The alert is posted.\n\n" +
		"Make exactly one `kanban_create` call:\n\n" +
		"- `assignee`: the `cluster-*` agent\n" +
		"- `title`: `Triage default/Pod/web-7 (BackOff) on c1`\n" +
		"- `body`: everything below"
	if got := summarySubject(prompt); got != "Triage default/Pod/web-7 (BackOff) on c1" {
		t.Errorf("summarySubject = %q, want the card title", got)
	}
	if got := summarySubject("Run the policy sweep\nmore"); got != "Run the policy sweep" {
		t.Errorf("summarySubject without a card title = %q, want the first line", got)
	}
}

type recentList struct {
	Sessions []recentSession `json:"sessions"`
}

func recentReq() *http.Request {
	return httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/sessions/recent", nil)
}

func byID(list recentList) map[string]recentSession {
	out := map[string]recentSession{}
	for _, s := range list.Sessions {
		out[s.ID] = s
	}
	return out
}

func TestRecentSessionsListKindsWithoutReadingContent(t *testing.T) {
	fake, h := setup(t)
	fake.seed("k8s-evt-00000001", "api_server", "Triage k8s-evt-00000001")
	fake.seed("slack-1", "slack", "Why is web-7 crashlooping?")
	fake.mu.Lock()
	fake.post("k8s-evt-00000001", "user", "A Kubernetes Warning event needs triage.")
	fake.post("slack-1", "user", "private question")
	fake.post("slack-1", "assistant", "private answer")
	fake.mu.Unlock()

	rec := serve(h, recentReq())
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "private") || strings.Contains(rec.Body.String(), `"summary"`) {
		t.Errorf("the recent list carried message content: %s", rec.Body.String())
	}
	got := byID(decode[recentList](t, rec))
	if got["k8s-evt-00000001"].Kind != kindEventTriage || got["slack-1"].Kind != kindChat {
		t.Errorf("kinds = %+v", got)
	}
	if fake.gets("slack-1")+fake.gets("k8s-evt-00000001") != 0 {
		t.Errorf("the recent list read session messages")
	}
}

func TestSummariesAreCachedPerMessageCount(t *testing.T) {
	fake, h := setup(t)
	fake.seed("cron-sweep-20261006", "api_server", "Triage cron-sweep-20261006")
	fake.mu.Lock()
	fake.post("cron-sweep-20261006", "user", "Run the policy sweep")
	fake.post("cron-sweep-20261006", "assistant", "No violations.")
	fake.mu.Unlock()

	serve(h, channelReq("scheduled"))
	first := fake.gets("cron-sweep-20261006")
	if first == 0 {
		t.Fatal("the first list did not read the session")
	}
	got := postsByID(decode[channelList](t, serve(h, channelReq("scheduled"))))
	if n := fake.gets("cron-sweep-20261006"); n != first {
		t.Errorf("a refresh with no new messages read Hermes again: %d reads, want %d", n, first)
	}
	if s := got["cron-sweep-20261006"].Summary; s == nil || s.Latest != "No violations." {
		t.Errorf("cached summary = %+v", s)
	}

	fake.mu.Lock()
	fake.post("cron-sweep-20261006", "assistant", "One violation found.")
	fake.mu.Unlock()
	got = postsByID(decode[channelList](t, serve(h, channelReq("scheduled"))))
	if n := fake.gets("cron-sweep-20261006"); n == first {
		t.Errorf("a new message did not refresh the summary")
	}
	if s := got["cron-sweep-20261006"].Summary; s == nil || s.Latest != "One violation found." {
		t.Errorf("refreshed summary = %+v", s)
	}
}

func postsByID(list channelList) map[string]recentSession {
	out := map[string]recentSession{}
	for _, p := range list.Posts {
		out[p.ID] = p
	}
	return out
}

func transcriptReq(sid string) *http.Request {
	return httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/sessions/"+sid+"/transcript", nil)
}

func TestTranscriptOpensClusterAndConsoleSessions(t *testing.T) {
	fake, h := setup(t)
	fake.seed("k8s-evt-00000001", "api_server", "Triage k8s-evt-00000001")
	fake.seed("cron-sweep-20261006", "api_server", "Triage cron-sweep-20261006")
	fake.mu.Lock()
	fake.post("k8s-evt-00000001", "user", "triage this")
	fake.post("k8s-evt-00000001", "assistant", nil)
	fake.post("k8s-evt-00000001", "tool", "pod list")
	lastEvt := fake.post("k8s-evt-00000001", "assistant", "filed a card")
	fake.post("cron-sweep-20261006", "user", "sweep")
	fake.mu.Unlock()
	turn := decode[chatResponse](t, serve(h, chatReq(`{"message":"check pods"}`)))

	evt := decode[transcriptResponse](t, serve(h, transcriptReq("k8s-evt-00000001")))
	if evt.Kind != kindEventTriage || len(evt.Messages) != 2 ||
		evt.Messages[0].Content != "triage this" || evt.Messages[1].Content != "filed a card" {
		t.Errorf("event triage transcript = %+v, want the two text rows oldest first", evt)
	}
	if evt.LatestID != lastEvt {
		t.Errorf("latest_id = %d, want %d", evt.LatestID, lastEvt)
	}
	if got := serve(h, transcriptReq("cron-sweep-20261006")); got.Code != http.StatusOK {
		t.Errorf("scheduled transcript: status %d", got.Code)
	}
	mine := decode[transcriptResponse](t, serve(h, transcriptReq(turn.SessionID)))
	if mine.Kind != kindConsole || len(mine.Messages) != 2 || mine.Messages[1].Content != turn.Reply {
		t.Errorf("console transcript = %+v", mine)
	}
}

func TestTranscriptKeepsTheNewestRows(t *testing.T) {
	fake, h := setup(t)
	fake.seed("k8s-evt-0000106e", "api_server", "Triage k8s-evt-0000106e")
	fake.mu.Lock()
	var last int64
	for i := 0; i < transcriptMaxRows+30; i++ {
		last = fake.post("k8s-evt-0000106e", "assistant", "row")
	}
	fake.mu.Unlock()
	got := decode[transcriptResponse](t, serve(h, transcriptReq("k8s-evt-0000106e")))
	if len(got.Messages) != transcriptMaxRows || got.Messages[len(got.Messages)-1].ID != last {
		t.Errorf("transcript holds %d rows ending at %d, want %d ending at %d",
			len(got.Messages), got.Messages[len(got.Messages)-1].ID, transcriptMaxRows, last)
	}
}

func TestTranscriptRefusesChatAndOtherSessions(t *testing.T) {
	fake, h := setup(t)
	fake.seed("slack-1", "slack", "Triage k8s-evt-00000009")
	fake.seed("api_42", "api_server", "Triage and resolve acme/toolkit#42")
	fake.mu.Lock()
	fake.post("slack-1", "user", "private question")
	fake.mu.Unlock()
	for _, sid := range []string{"slack-1", "api_42"} {
		rec := serve(h, transcriptReq(sid))
		if rec.Code != http.StatusForbidden {
			t.Errorf("%s: status %d, want 403", sid, rec.Code)
		}
		if got := decode[errorResponse](t, rec); got.Error != "transcript_not_allowed" {
			t.Errorf("%s: error %q", sid, got.Error)
		}
		if n := fake.gets(sid); n != 0 {
			t.Errorf("%s: a refused transcript read %d message pages", sid, n)
		}
	}
}

func TestTranscriptRefusesABadID(t *testing.T) {
	fake, h := setup(t)
	for _, sid := range []string{"a$b", "-x", "a%2Fb", "a%20b", strings.Repeat("a", 129)} {
		if rec := serve(h, transcriptReq(sid)); rec.Code != http.StatusBadRequest {
			t.Errorf("%q: status %d, want 400", sid, rec.Code)
		}
	}
	if fake.lastQuery != "" {
		t.Errorf("a refused ID reached Hermes")
	}
	if rec := serve(h, transcriptReq("k8s-evt-0000beef")); rec.Code != http.StatusNotFound {
		t.Errorf("unknown session: status %d, want 404", rec.Code)
	}
}

func TestTranscriptTagsAuthorsAndExtractsCards(t *testing.T) {
	fake, h := setup(t)
	fake.seed("k8s-evt-55376c71", "api_server", "Triage k8s-evt-55376c71")
	fake.seed("cron-sweep-20261006", "api_server", "Triage cron-sweep-20261006")

	triagePrompt := "A Kubernetes Warning event needs triage on GKE cluster 'kcc-management-cluster'.\n\n" +
		"Make exactly one `kanban_create` call:\n\n" +
		"- `title`: `Triage prod-databases/Pod/stateful-postgres-db-8f4fcc9cf-tk96v (FailedScheduling) on kcc-management-cluster`\n\n" +
		"--- BEGIN TASK BODY (copy verbatim) ---\n" +
		"**Event Details:**\n" +
		"- **Resource:** prod-databases/Pod/stateful-postgres-db-8f4fcc9cf-tk96v\n" +
		"- **Event Reason:** FailedScheduling\n" +
		"- **Warning Message:** 0/31 nodes are available: pod has unbound immediate PersistentVolumeClaims.\n"
	kanbanWake := "[kanban] Task t_9bb9d176 completed.\n" +
		"Title: Triage prod-databases/Pod/stateful-postgres-db-8f4fcc9cf-tk96v (FailedScheduling) on kcc-management-cluster\n" +
		"Assignee: @cluster-gca-gke-test-kcc-management-cluster-us-central1\n" +
		"Board: default\n\n" +
		"Check the result or decide the next step.\n" +
		"Result: The Database Pod is unschedulable because its PVC requests a non-existent StorageClass 'premium-nvme-ssd'.\n\n" +
		"This is an automatic task-status notification, not a request to decompose the task again."

	fake.mu.Lock()
	fake.post("k8s-evt-55376c71", "user", triagePrompt)
	fake.post("k8s-evt-55376c71", "assistant", "triaging stateful-postgres-db scheduling on kcc-management-cluster.")
	fake.post("k8s-evt-55376c71", "user", "fix this")
	fake.post("k8s-evt-55376c71", "user", kanbanWake)
	fake.post("cron-sweep-20261006", "user", "Run the policy sweep\nSecond line")
	fake.post("cron-sweep-20261006", "user", "why did the sweep flag ns-a?")
	fake.post("cron-sweep-20261006", "user", "[scheduled]\nSecond run of the policy sweep\nDetails")
	fake.mu.Unlock()

	evt := decode[transcriptResponse](t, serve(h, transcriptReq("k8s-evt-55376c71")))
	if len(evt.Messages) != 4 {
		t.Fatalf("messages = %d, want 4", len(evt.Messages))
	}
	m0 := evt.Messages[0]
	if m0.Author != authorEventWatcher || m0.Card == nil ||
		m0.Card.Subject != "Triage prod-databases/Pod/stateful-postgres-db-8f4fcc9cf-tk96v (FailedScheduling) on kcc-management-cluster" ||
		m0.Card.Resource != "prod-databases/Pod/stateful-postgres-db-8f4fcc9cf-tk96v" ||
		m0.Card.Reason != "FailedScheduling" ||
		!strings.Contains(m0.Card.Warning, "unbound immediate PersistentVolumeClaims") {
		t.Errorf("m0 = %+v, card = %+v", m0, m0.Card)
	}
	if m1 := evt.Messages[1]; m1.Author != authorAgent || m1.Card != nil {
		t.Errorf("m1 = %+v, want agent without card", m1)
	}
	if m2 := evt.Messages[2]; m2.Author != authorUser || m2.Card != nil || m2.Content != "fix this" {
		t.Errorf("m2 = %+v, want user without card", m2)
	}
	m3 := evt.Messages[3]
	if m3.Author != authorKanban || m3.Card == nil ||
		m3.Card.TaskID != "t_9bb9d176" ||
		m3.Card.Status != "completed" ||
		m3.Card.Assignee != "Cluster agent · kcc-management-cluster" ||
		!strings.Contains(m3.Card.Summary, "premium-nvme-ssd") ||
		strings.Contains(m3.Card.Summary, "automatic task-status notification") {
		t.Errorf("m3 = %+v, card = %+v", m3, m3.Card)
	}

	sched := decode[transcriptResponse](t, serve(h, transcriptReq("cron-sweep-20261006")))
	if len(sched.Messages) != 3 || sched.Messages[0].Author != authorScheduler ||
		sched.Messages[0].Card == nil || sched.Messages[0].Card.Subject != "Run the policy sweep" ||
		sched.Messages[1].Author != authorUser || sched.Messages[1].Card != nil || sched.Messages[1].Content != "why did the sweep flag ns-a?" ||
		sched.Messages[2].Author != authorScheduler || sched.Messages[2].Content != "Second run of the policy sweep\nDetails" ||
		sched.Messages[2].Card == nil || sched.Messages[2].Card.Subject != "Second run of the policy sweep" {
		t.Errorf("scheduled transcript = %+v", sched.Messages)
	}

	slackSrv := newServer(config{ChatPlatform: "slack"})
	unmarkedSubDaily := "Third run of the policy sweep\nDetails"
	if msg := slackSrv.buildSessionMessage(kindScheduled, "cron-policy-sweep-20261006", false, hermesMessage{ID: 4, Role: roleUser, Content: &unmarkedSubDaily}); msg.Author != authorScheduler || msg.Card == nil || msg.Card.Subject != "Third run of the policy sweep" {
		t.Errorf("slack sub-daily scheduled message = %+v, card = %+v", msg, msg.Card)
	}

	driftPrompt := "- **Object:** prod-databases/Pod/db-1\n- **Who:** alice@example.com\n- **What:** `patch` via `pods.patch`\n"
	driftCard := parseEventWatcherCard(driftPrompt)
	if driftCard.Kind != "drift" || driftCard.Resource != "prod-databases/Pod/db-1" ||
		driftCard.Warning != "alice@example.com" || driftCard.Reason != "`patch` via `pods.patch`" {
		t.Errorf("driftCard = %+v", driftCard)
	}

	srv := newServer(config{})
	for _, status := range []string{"blocked; needs attention", "crashed", "timed out", "gave up"} {
		wake := "[kanban] Task t_ab12 " + status + ".\nTitle: Check PVC\nResult: Stopped on " + status + "\n"
		msg := srv.buildSessionMessage(kindEventTriage, "k8s-evt-0000000a", false, hermesMessage{ID: 2, Role: roleUser, Content: &wake})
		if msg.Author != authorKanban {
			t.Errorf("status %q: buildSessionMessage.Author = %q, want %q", status, msg.Author, authorKanban)
		}
		card, ok := srv.parseKanbanCard(wake)
		if !ok || card == nil || card.TaskID != "t_ab12" || card.Status != status {
			t.Errorf("status %q: parseKanbanCard = %+v, ok=%v", status, card, ok)
		}
	}
	if len(m0.Card.Skills) == 0 || m0.Card.Skills[0] != "gke-workload-troubleshooting" {
		t.Errorf("m0 card skills = %v, want gke-workload-troubleshooting", m0.Card.Skills)
	}
	if len(m3.Card.Skills) < 2 {
		t.Errorf("m3 card skills = %v, want troubleshooting + storage skills", m3.Card.Skills)
	}
}

func TestTranscriptDoesNotTagFirstReturnedRowAsWatcherWhenHeadIsTruncated(t *testing.T) {
	// Case 1: exactly transcriptMaxRows (200) rows on a 50-row page boundary,
	// with 50 older rows before them so reachedStart is false even though
	// len(kept) <= transcriptMaxRows.
	fake, h := setup(t)
	sid := "k8s-evt-0000aaaa"
	fake.seed(sid, "api_server", "Triage "+sid)
	fake.mu.Lock()
	fake.post(sid, "user", "initial event watcher prompt")
	for i := 1; i < 50; i++ {
		fake.post(sid, "assistant", "old reply")
	}
	fake.post(sid, "user", "operator reply at start of kept window")
	for i := 1; i < transcriptMaxRows; i++ {
		fake.post(sid, "assistant", "recent reply")
	}
	fake.mu.Unlock()

	got := decode[transcriptResponse](t, serve(h, transcriptReq(sid)))
	if len(got.Messages) != transcriptMaxRows {
		t.Fatalf("len(messages) = %d, want %d", len(got.Messages), transcriptMaxRows)
	}
	if got.Messages[0].Author != authorUser || got.Messages[0].Card != nil {
		t.Errorf("first row of truncated 200-row window = %+v, want user without card", got.Messages[0])
	}

	// Case 2: messagesMaxPages is exhausted by tool rows so fewer than
	// transcriptMaxRows text rows are returned, but reachedStart is still false.
	sid2 := "k8s-evt-0000bbbb"
	fake.seed(sid2, "api_server", "Triage "+sid2)
	fake.mu.Lock()
	fake.post(sid2, "user", "initial event watcher prompt")
	fake.post(sid2, "user", "operator follow-up")
	for i := 0; i < messagesMaxPages*messagesPageLimit; i++ {
		fake.post(sid2, "tool", "pod list")
	}
	fake.post(sid2, "user", "latest operator question")
	fake.post(sid2, "assistant", "latest answer")
	fake.mu.Unlock()

	got2 := decode[transcriptResponse](t, serve(h, transcriptReq(sid2)))
	if len(got2.Messages) != 2 {
		t.Fatalf("len(messages) = %d, want 2", len(got2.Messages))
	}
	if got2.Messages[0].Author != authorUser || got2.Messages[0].Card != nil {
		t.Errorf("first row after messagesMaxPages cap = %+v, want user without card", got2.Messages[0])
	}
}

func TestTranscriptTagsFirstRowWhenSessionIsExactlyTranscriptMaxRows(t *testing.T) {
	fake, h := setup(t)
	sid := "k8s-evt-0000cccc"
	fake.seed(sid, "api_server", "Triage "+sid)
	fake.mu.Lock()
	triagePrompt := "A Kubernetes Warning event needs triage\n\n- `title`: `Triage ns/Pod/name`\n- **Resource:** ns/Pod/name\n- **Event Reason:** Unhealthy\n"
	fake.post(sid, "user", triagePrompt)
	for i := 1; i < transcriptMaxRows; i++ {
		fake.post(sid, "assistant", "reply")
	}
	fake.mu.Unlock()

	got := decode[transcriptResponse](t, serve(h, transcriptReq(sid)))
	if len(got.Messages) != transcriptMaxRows {
		t.Fatalf("len(messages) = %d, want %d", len(got.Messages), transcriptMaxRows)
	}
	if got.Messages[0].Author != authorEventWatcher || got.Messages[0].Card == nil {
		t.Errorf("first row of exact 200-row session = %+v, want authorEventWatcher with card", got.Messages[0])
	}
}

func TestExtractSkillsFromScheduledSlugAndToolCalls(t *testing.T) {
	skills := extractTextSkills(kindScheduled, "cron-platform-gce-compute-fleet-audit-20261009", "call:default_api:terminal{command:/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py}", nil)
	if len(skills) < 2 {
		t.Fatalf("scheduled skills = %v, want gce-compute-fleet-audit and fleet-audit", skills)
	}
	m := hermesMessage{
		ID:        10,
		Role:      roleAssistant,
		ToolCalls: []byte(`[{"function":{"name":"skill_view","arguments":"{\"name\":\"gke-stall-detection\"}"}},{"function":{"name":"kanban_create","arguments":"{\"title\":\"Triage stall\",\"assignee\":\"platform\"}"}}]`),
	}
	steps, tcSkills := extractMessageToolActivity(m)
	if len(steps) != 2 || steps[0].Tool != "skill_view" || steps[0].Skill != "gke-stall-detection" {
		t.Errorf("steps = %+v", steps)
	}
	if len(tcSkills) != 1 || tcSkills[0] != "gke-stall-detection" {
		t.Errorf("tool call skills = %v, want [gke-stall-detection]", tcSkills)
	}
}

func TestDelegationStepsCarryTheCardID(t *testing.T) {
	call := hermesMessage{ID: 1, Role: roleAssistant,
		ToolCalls: []byte(`[{"function":{"name":"kanban_create","arguments":"{\"title\":\"Identify visible GCP projects\",\"assignee\":\"platform\",\"body\":\"...\"}"}}]`)}
	result := `{"ok": true, "task_id": "t_0978edd2", "status": "ready"}`
	name := "kanban_create"
	res := hermesMessage{ID: 2, Role: roleTool, ToolName: &name, Content: &result}
	a, _ := extractMessageToolActivity(call)
	b, _ := extractMessageToolActivity(res)
	steps := foldDelegations(append(a, b...))
	if len(steps) != 1 || !steps[0].Delegation || steps[0].TaskID != "t_0978edd2" ||
		steps[0].TaskTitle != "Identify visible GCP projects" || steps[0].Assignee != "platform" {
		t.Errorf("folded steps = %+v, want one delegation with the card ID, title and assignee", steps)
	}
	if kanbanCreateTaskID(`{"ok": false, "task_id": "t_x"}`) != "" || kanbanCreateTaskID(`{"ok": true, "task_id": "../x"}`) != "" {
		t.Errorf("a failed create or a malformed ID yielded a task ID")
	}
}

func TestParseRepositories(t *testing.T) {
	got := parseRepositories(`[{"forge":"github","repository":"acme/infra","role":"gitops"},` +
		`{"forge":"in-cluster-gitea","provider":"gitea","host":"gitea-http.gitea.svc.cluster.local:3000","repository":"ops/gitops-repo","role":"managed"},` +
		`{"repository":""}]`)
	if len(got) != 2 || got[0].URL != "https://github.com/acme/infra" ||
		got[1].URL != "http://gitea-http.gitea.svc.cluster.local:3000/ops/gitops-repo" || got[1].Role != "managed" {
		t.Errorf("parseRepositories = %+v", got)
	}
	if n := len(parseRepositories("not json")); n != 0 {
		t.Errorf("malformed env yielded %d repositories, want 0", n)
	}
}

// Every skill directory in the tree is in knownPlatformSkills, so a skill
// named in prose or a tool argument gets a badge.
func TestKnownPlatformSkillsCoverTheSkillsTree(t *testing.T) {
	dirs, err := filepath.Glob(filepath.Join("..", "..", "..", "agents", "*", "skills", "*", "SKILL.md"))
	if err != nil || len(dirs) == 0 {
		t.Skipf("no skills tree beside the package (err=%v)", err)
	}
	known := map[string]bool{}
	for _, sk := range knownPlatformSkills {
		known[sk] = true
	}
	for _, d := range dirs {
		if name := filepath.Base(filepath.Dir(d)); !known[name] {
			t.Errorf("skill %s (%s) is missing from knownPlatformSkills", name, d)
		}
	}
}

// A skill name inside a longer skill name is not a mention of it.
func TestSkillMatchingUsesWholeNames(t *testing.T) {
	if got := findKnownSkillsInText("Ran gce-compute-fleet-audit and wrote fleet-audit-reports."); strings.Join(got, ",") != "fleet-audit-reports,gce-compute-fleet-audit" && strings.Join(got, ",") != "gce-compute-fleet-audit,fleet-audit-reports" {
		t.Errorf("findKnownSkillsInText = %v, want gce-compute-fleet-audit and fleet-audit-reports only", got)
	}
	if got := findKnownSkillsInText("Use fleet-audit, then gke-workload-identity."); strings.Join(got, ",") != "gke-workload-identity,fleet-audit" && strings.Join(got, ",") != "fleet-audit,gke-workload-identity" {
		t.Errorf("findKnownSkillsInText = %v, want fleet-audit and gke-workload-identity", got)
	}
	if got := detectSkillFromToolCall("terminal", `{"command":"run gce-compute-fleet-audit"}`); got != "gce-compute-fleet-audit" {
		t.Errorf("detectSkillFromToolCall = %q, want gce-compute-fleet-audit", got)
	}
	if got := detectSkillFromToolCall("terminal", `{"command":"open fleet-audit-reports"}`); got != "fleet-audit-reports" {
		t.Errorf("detectSkillFromToolCall = %q, want fleet-audit-reports", got)
	}
	if got := extractTextSkills(kindScheduled, "cron-platform-gce-compute-fleet-audit-20261009", "", nil); strings.Join(got, ",") != "gce-compute-fleet-audit" {
		t.Errorf("scheduled slug skills = %v, want only gce-compute-fleet-audit", got)
	}
}

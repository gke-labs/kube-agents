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
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type channelList struct {
	Channel string          `json:"channel"`
	Posts   []recentSession `json:"posts"`
}

func channelReq(name string) *http.Request {
	return httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/channels/"+name+"/posts", nil)
}

func TestChannelsListTheirOwnKindOnly(t *testing.T) {
	fake, h := setup(t)
	fake.seed("k8s-evt-00000001", "api_server", "Triage k8s-evt-00000001")
	fake.seed("cron-sweep-20261006", "api_server", "Triage cron-sweep-20261006")
	fake.seed("slack-1", "slack", "Triage k8s-evt-00000002")
	fake.mu.Lock()
	fake.post("k8s-evt-00000001", "user", "Pod web-7 is crashlooping.\nDetail")
	fake.post("k8s-evt-00000001", "assistant", "Filed card t_1.")
	fake.post("k8s-evt-00000001", "user", "[kanban] card completed")
	fake.post("k8s-evt-00000001", "assistant", nil)
	fake.post("k8s-evt-00000001", "assistant", "Fixed: the image tag was wrong.")
	fake.post("slack-1", "user", "private question")
	fake.mu.Unlock()

	rec := serve(h, channelReq("alerts"))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "private") {
		t.Errorf("a chat session reached a channel: %s", rec.Body.String())
	}
	got := decode[channelList](t, rec)
	posts := map[string]recentSession{}
	for _, p := range got.Posts {
		if p.Kind != kindEventTriage {
			t.Errorf("post %s has kind %s in #alerts", p.ID, p.Kind)
		}
		posts[p.ID] = p
	}
	if _, has := posts["slack-1"]; has || got.Channel != "alerts" {
		t.Fatalf("alerts = %+v, want no chat session", got)
	}
	sum := posts["k8s-evt-00000001"].Summary
	if sum == nil || sum.Latest != "Fixed: the image tag was wrong." || sum.Replies != 2 {
		t.Errorf("summary = %+v, want the newest reply and a count of 2", sum)
	}
	if n := fake.gets("slack-1"); n != 0 {
		t.Errorf("the console read a chat session %d times", n)
	}
	fake.mu.Lock()
	query := fake.lastListQuery
	fake.mu.Unlock()
	if !strings.Contains(query, "source="+sourceAPIServer) || !strings.Contains(query, fmt.Sprintf("limit=%d", channelListLimit)) {
		t.Errorf("list query = %q, want the gateway source and the channel limit", query)
	}

	sched := decode[channelList](t, serve(h, channelReq("scheduled")))
	if len(sched.Posts) != 1 || sched.Posts[0].ID != "cron-sweep-20261006" || sched.Posts[0].Kind != kindScheduled {
		t.Errorf("scheduled = %+v, want only cron-sweep-20261006", sched)
	}
}

func TestChannelsCapThePosts(t *testing.T) {
	fake, h := setup(t)
	for i := range channelPostsLimit + 5 {
		id := fmt.Sprintf("k8s-evt-%08x", i)
		fake.seed(id, "api_server", "Triage "+id)
	}
	got := decode[channelList](t, serve(h, channelReq("alerts")))
	if len(got.Posts) != channelPostsLimit {
		t.Errorf("%d posts, want %d", len(got.Posts), channelPostsLimit)
	}
}

func TestUnknownChannelIs404(t *testing.T) {
	_, h := setup(t)
	for _, name := range []string{"chat", "console", "other", "ALERTS"} {
		if rec := serve(h, channelReq(name)); rec.Code != http.StatusNotFound {
			t.Errorf("%s: status %d, want 404", name, rec.Code)
		}
	}
}

func TestChannelReportsAnUnreachableGateway(t *testing.T) {
	s := newServer(config{HermesURL: "http://127.0.0.1:1"})
	if rec := serve(s.routes(), channelReq("alerts")); rec.Code != http.StatusBadGateway {
		t.Errorf("status %d, want 502", rec.Code)
	}
}

func TestSummaryCacheKeepsOtherListsUnderTheCap(t *testing.T) {
	fake, h := setup(t)
	fake.seed("k8s-evt-00000001", "api_server", "Triage k8s-evt-00000001")
	fake.seed("cron-sweep-20261006", "api_server", "Triage cron-sweep-20261006")
	fake.mu.Lock()
	fake.post("cron-sweep-20261006", "user", "Run the policy sweep")
	fake.post("cron-sweep-20261006", "assistant", "No violations.")
	fake.mu.Unlock()

	serve(h, channelReq("scheduled"))
	reads := fake.gets("cron-sweep-20261006")
	serve(h, channelReq("alerts"))
	serve(h, channelReq("scheduled"))
	if n := fake.gets("cron-sweep-20261006"); n != reads {
		t.Errorf("listing another channel evicted the cached summary: %d reads, want %d", n, reads)
	}
}

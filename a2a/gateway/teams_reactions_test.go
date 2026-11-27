package gateway

import (
	"net/http"
	"regexp"
	"testing"
)

// The broker's passthrough admits a reaction id of these characters only
// (credential_proxy.py, TEAMS_REACTION_PATH_RE).
var testTeamsReactionID = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

// A mark is the bot's own reaction on the person's message: a PUT adds it
// and a DELETE takes it off, on the reactions path of that message, with no
// body, in a channel thread and in a personal chat alike.
func TestTeamsMarksAPersonsMessageWithTheBotsOwnReaction(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	var a Adapter = newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	for _, conv := range []string{testTeamsChannel, testTeamsDM} {
		key := teamsConversationID(conv)
		if err := a.React(key, "act-1", "question"); err != nil {
			t.Fatalf("react in %s: %v", conv, err)
		}
		if err := a.RemoveReaction(key, "act-1", "question"); err != nil {
			t.Fatalf("remove in %s: %v", conv, err)
		}
	}
	relay.mu.Lock()
	defer relay.mu.Unlock()
	want := []struct{ method, endpoint string }{
		{http.MethodPut, "v3/conversations/" + testTeamsChannel + "/activities/act-1/reactions/1f440_eyes"},
		{http.MethodDelete, "v3/conversations/" + testTeamsChannel + "/activities/act-1/reactions/1f440_eyes"},
		{http.MethodPut, "v3/conversations/" + testTeamsDM + "/activities/act-1/reactions/1f440_eyes"},
		{http.MethodDelete, "v3/conversations/" + testTeamsDM + "/activities/act-1/reactions/1f440_eyes"},
	}
	if len(relay.apiCall) != len(want) {
		t.Fatalf("api calls %v", relay.apiCall)
	}
	for i, w := range want {
		call := relay.apiCall[i]
		if call["method"] != w.method || call["endpoint"] != w.endpoint {
			t.Errorf("call %d: %v %v, want %s %s", i, call["method"], call["endpoint"], w.method, w.endpoint)
		}
		if _, has := call["payload"]; has {
			t.Errorf("call %d carries a body: %v", i, call["payload"])
		}
	}
}

// Nothing is called for a token with no reaction (done leaves none) or for
// an ask with no message id. With the marks turned off no reaction is added
// and none is taken off; turned back on, both calls go out again.
func TestTeamsMarkCallsNothingWithoutAReactionAMessageOrTheSwitch(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	key := teamsConversationID(testTeamsChannel)
	calls := func() int {
		relay.mu.Lock()
		defer relay.mu.Unlock()
		return len(relay.apiCall)
	}
	for _, c := range []struct{ name, message, token string }{
		{"done", "act-1", "done"},
		{"unknown token", "act-1", "no-such-mark"},
		{"no message", "", "failed"},
		{"blank message", "  ", "failed"},
	} {
		if err := a.React(key, c.message, c.token); err != nil {
			t.Errorf("%s: react: %v", c.name, err)
		}
		if err := a.RemoveReaction(key, c.message, c.token); err != nil {
			t.Errorf("%s: remove: %v", c.name, err)
		}
	}
	a.SetMessageMarks(false)
	if err := a.React(key, "act-1", "failed"); err != nil {
		t.Errorf("marks off: react: %v", err)
	}
	if err := a.RemoveReaction(key, "act-1", "question"); err != nil {
		t.Errorf("marks off: remove: %v", err)
	}
	if n := calls(); n != 0 {
		t.Fatalf("%d calls went out for marks that draw nothing or with the marks off", n)
	}
	a.SetMessageMarks(true)
	if err := a.RemoveReaction(key, "act-1", "question"); err != nil {
		t.Fatalf("marks on: remove: %v", err)
	}
	relay.mu.Lock()
	if len(relay.apiCall) != 1 || relay.apiCall[0]["method"] != http.MethodDelete {
		t.Fatalf("marks on: the removal did not go out: %v", relay.apiCall)
	}
	relay.mu.Unlock()
	if err := a.React(key, "act-1", "failed"); err != nil {
		t.Fatalf("marks on: react: %v", err)
	}
	if n := calls(); n != 2 {
		t.Fatalf("%d calls with the marks back on, want 2", n)
	}
	if err := a.React("slack:C1", "act-1", "failed"); err == nil {
		t.Error("a conversation that is not Teams's took a reaction")
	}
}

// Teams marks every token Slack marks, and each reaction id is one the
// broker's passthrough admits.
func TestTeamsMarkReactionsCoverSlacksTokens(t *testing.T) {
	for token := range slackMarkEmoji {
		if _, ok := teamsMarkReaction[token]; !ok {
			t.Errorf("token %q has a Slack emoji and no Teams reaction", token)
		}
	}
	for token, id := range teamsMarkReaction {
		if _, ok := slackMarkEmoji[token]; !ok {
			t.Errorf("token %q has a Teams reaction and no Slack emoji", token)
		}
		if !testTeamsReactionID.MatchString(id) {
			t.Errorf("token %q: reaction id %q is not one the broker admits", token, id)
		}
	}
}

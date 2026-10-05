package gateway

import (
	"encoding/json"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/capability"
)

func TestRenderOmitsViaWhenUnset(t *testing.T) {
	a := Authority{Requester: AuthorityRequester{Principal: "p", Backend: "discord"}}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(a.Render(nil), &m); err != nil {
		t.Fatal(err)
	}
	if _, ok := m["via"]; ok {
		t.Fatalf("via rendered when unset: %s", a.Render(nil))
	}
	if string(m["grants"]) != "null" {
		t.Fatalf("grants = %s, want null", m["grants"])
	}
}

func TestRenderCarriesViaWhenSet(t *testing.T) {
	a := Authority{Via: &AuthorityVia{TaskID: "task-1", Session: "chat-otter-1"}}
	var m struct {
		Via *AuthorityVia `json:"via"`
	}
	if err := json.Unmarshal(a.Render(&capability.Ref{Key: "root.task-2", Revision: 3}), &m); err != nil {
		t.Fatal(err)
	}
	if m.Via == nil || m.Via.TaskID != "task-1" || m.Via.Session != "chat-otter-1" {
		t.Fatalf("via = %+v", m.Via)
	}
}

func TestAttributionRoundTripsWithoutGrants(t *testing.T) {
	a := Authority{
		Requester: AuthorityRequester{Principal: "h1", Backend: "slack", Subject: "h2", VerifiedBy: "principal-map"},
		Audience:  AuthorityAudience{Conversation: "h3", Kind: "group", Roster: []string{"h1"}, RosterComplete: true},
	}
	raw := a.Attribution()
	var m map[string]json.RawMessage
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	if _, ok := m["grants"]; ok {
		t.Fatalf("attribution carries grants: %s", raw)
	}
	back, err := AuthorityFromAttribution(raw)
	if err != nil {
		t.Fatal(err)
	}
	if back.Requester != a.Requester || back.Audience.Conversation != "h3" || len(back.Audience.Roster) != 1 {
		t.Fatalf("round trip lost fields: %+v", back)
	}
}

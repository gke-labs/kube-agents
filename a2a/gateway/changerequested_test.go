package gateway

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// submittedChange reads the taskParameters part of a profile submission:
// whether there is one, and its changeRequested.
func submittedChange(t *testing.T, env *lib.Envelope) (found, changeRequested bool) {
	t.Helper()
	params, found, err := lib.TaskParametersOf(messageParts(t, env))
	if err != nil {
		t.Fatalf("taskParameters part: %v", err)
	}
	return found, params.ChangeRequested
}

func confirmTurn(conversation, profile string, params map[string]string) InboundMessage {
	m := profileTurn(conversation, profile, params)
	m.Intent = IntentConfirm
	return m
}

// A typed message never requests a change, and a confirm action always
// does, on the submission and on the entry its children inherit from.
func TestChangeRequested_OnlyAConfirmIntentSetsIt(t *testing.T) {
	cases := []struct {
		name string
		turn InboundMessage
		want bool
	}{
		{"free text", profileTurn("discord:cr/1", "cluster", targetParams("p1", "us-east1", "prod")), false},
		// Free text that says yes is still free text.
		{"free text that confirms", func() InboundMessage {
			m := profileTurn("discord:cr/2", "cluster", targetParams("p1", "us-east1", "prod"))
			m.Text = "yes, open the pull request"
			return m
		}(), false},
		{"confirm intent", confirmTurn("discord:cr/3", "cluster", targetParams("p1", "us-east1", "prod")), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := startProfileRig(t)
			r.adapter.inbox <- tc.turn
			env := submissionOn(t, r, "prod-east")
			found, got := submittedChange(t, env)
			if !found || got != tc.want {
				t.Fatalf("submission changeRequested = %v (part found %v), want %v", got, found, tc.want)
			}
			waitForRecord(t, r, tc.turn.Conversation, "the entry", func(rec *SessionRecord) bool {
				ref, ok := rec.TaskRefFor(env.TaskID)
				return ok && ref.ChangeRequested == tc.want
			})
		})
	}
}

// A profile with no declared parameters gets the part only when a change is
// requested, and then with an empty parameter set beside the flag.
func TestChangeRequested_ThePartIsAttachedWhenOnlyTheFlagIsSet(t *testing.T) {
	r := startProfileRig(t)
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat"})
	r.adapter.inbox <- profileTurn("discord:cr/4", "chat", nil)
	if found, _ := submittedChange(t, submissionOn(t, r, "chat")); found {
		t.Fatal("a parameterless free-text submission carries a taskParameters part")
	}

	r2 := startProfileRig(t)
	r2.g.profiles.put("chat", &lib.AgentCard{Name: "chat"})
	r2.adapter.inbox <- confirmTurn("discord:cr/5", "chat", nil)
	params, found, err := lib.TaskParametersOf(messageParts(t, submissionOn(t, r2, "chat")))
	if err != nil || !found || !params.ChangeRequested || len(params.Parameters) != 0 {
		t.Fatalf("part = %+v, found %v, err %v; want an empty set with changeRequested", params, found, err)
	}
}

// The unit form of the same rule, including the routes that carry no part.
func TestSubmissionPayload_AttachesThePartForTheFlagOnAProfileRouteOnly(t *testing.T) {
	cases := []struct {
		name            string
		route           *profileRoute
		changeRequested bool
		wantPart        bool
	}{
		{"platform, no change", nil, false, false},
		{"platform, change", nil, true, false},
		{"profile without parameters, no change", &profileRoute{Profile: "chat"}, false, false},
		{"profile without parameters, change", &profileRoute{Profile: "chat"}, true, true},
		{"profile with parameters, no change", &profileRoute{Profile: "c", Params: targetParams("p", "l", "c")}, false, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			raw, err := submissionPayload("hi", "task-1", "ctx-1", tc.route, tc.changeRequested)
			if err != nil {
				t.Fatal(err)
			}
			var m lib.Message
			if err := json.Unmarshal(raw, &m); err != nil {
				t.Fatal(err)
			}
			params, found, err := lib.TaskParametersOf(m.Parts)
			if err != nil || found != tc.wantPart {
				t.Fatalf("part found = %v (err %v), want %v", found, err, tc.wantPart)
			}
			if found && params.ChangeRequested != tc.changeRequested {
				t.Fatalf("changeRequested = %v, want %v", params.ChangeRequested, tc.changeRequested)
			}
		})
	}
}

// A child's changeRequested is its parent's, read from the gateway's
// entry: true under a confirmed parent, false under a typed one, and never
// raised by anything the delegate request carries.
func TestChangeRequested_AChildInheritsItsParentsAndCannotRaiseIt(t *testing.T) {
	cases := []struct {
		name    string
		confirm bool
		// request is the delegate artifact's data, written raw so it can
		// carry what a model might add.
		request string
		want    bool
	}{
		{"confirmed parent", true, `{"addressee":"prod-east","text":"look at prod"}`, true},
		{"typed parent", false, `{"addressee":"prod-east","text":"look at prod"}`, false},
		{"typed parent, request claims a change", false,
			`{"addressee":"prod-east","text":"look at prod","changeRequested":true,"taskParameters":{"parameters":{},"changeRequested":true}}`, false},
	}
	for i, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := startProfileRig(t)
			r.g.profiles.put("chat", &lib.AgentCard{Name: "chat"})
			ctx := context.Background()
			conv := "discord:cr-child/" + string(rune('a'+i))
			turn := profileTurn(conv, "chat", nil)
			if tc.confirm {
				turn.Intent = IntentConfirm
			}
			r.adapter.inbox <- turn
			origin := submissionOn(t, r, "chat")
			parent := r.execFor(t, origin, "chat")
			if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
				t.Fatal(err)
			}
			if err := parent.PublishArtifact(ctx, lib.Artifact{ArtifactID: "artifact-cr-" + lib.ArtifactDelegate, Name: lib.ArtifactDelegate,
				Parts: []lib.Part{{Kind: "data", Data: json.RawMessage(tc.request)}}}); err != nil {
				t.Fatal(err)
			}
			child := submissionOn(t, r, "prod-east")
			if found, got := submittedChange(t, child); !found || got != tc.want {
				t.Fatalf("child changeRequested = %v (part found %v), want %v", got, found, tc.want)
			}
			waitForRecord(t, r, conv, "the child's entry", func(rec *SessionRecord) bool {
				ref, ok := rec.TaskRefFor(child.TaskID)
				return ok && ref.ParentTaskID == origin.TaskID && ref.ChangeRequested == tc.want
			})
		})
	}
}

// A wake continues its delegating turn, so it carries that turn's value.
func TestChangeRequested_AWakeCarriesItsDelegatingTurns(t *testing.T) {
	for _, want := range []bool{true, false} {
		t.Run(map[bool]string{true: "confirmed", false: "typed"}[want], func(t *testing.T) {
			r, spawn := startRigWithSpawner(t)
			conv := "discord:g1/cr-wake"
			origin, _, child := delegated(t, r, spawn, conv, "")
			putRecord(t, r, conv, func(rec *SessionRecord) {
				for i := range rec.Tasks {
					if rec.Tasks[i].ID == origin.TaskID {
						rec.Tasks[i].ChangeRequested = want
					}
				}
			})
			completeTask(t, r.execFor(t, child, targetPlatform), "fleet is green")
			waitFor(t, "wake spawn", func() bool { return len(spawn.calls()) == 2 })
			wake := r.awaitTask(t, spawn.calls()[1].Session)
			waitForRecord(t, r, conv, "the wake's entry", func(rec *SessionRecord) bool {
				ref, ok := rec.TaskRefFor(wake.TaskID)
				return ok && ref.Role == taskRoleWake && ref.ChangeRequested == want
			})
		})
	}
}

// A confirm while a task runs is refused, not steered: a steer can't change
// what the running task was granted.
func TestChangeRequested_AConfirmWhileATaskRunsIsRefusedNotSteered(t *testing.T) {
	r := startProfileRig(t)
	conv := "discord:cr/6"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-cr6a", Text: "how is the fleet?"}
	origin := r.awaitTask(t, "platform")
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-cr6b", Text: "open the PR", Intent: IntentConfirm}
	waitFor(t, "the refusal", postedContaining(r, noticeConfirmWhileRunning))
	for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
		if e.TaskID == origin.TaskID && e.EnvelopeID != origin.EnvelopeID && e.Kind == lib.KindMessage {
			t.Fatal("the confirm was steered into the running task")
		}
	}
}

// A confirm whose text reads "stop" is a confirm: the intent, not the text,
// says what the message is.
func TestChangeRequested_AConfirmReadingStopIsNotAStop(t *testing.T) {
	r := startProfileRig(t)
	conv := "discord:cr/7"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-cr7", Text: "stop", Intent: IntentConfirm}
	origin := r.awaitTask(t, "platform")
	waitForRecord(t, r, conv, "the confirmed entry", func(rec *SessionRecord) bool {
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && ref.ChangeRequested
	})
}

// The console's confirm button is a frame of kind "confirm".
func TestConsoleConfirmFrameCarriesTheConfirmIntent(t *testing.T) {
	r := startConsoleRig(t)
	r.send(t, "tab-cr", ConsoleInFrame{MessageID: "m1", Text: "do it", Kind: "text"})
	r.send(t, "tab-cr", ConsoleInFrame{MessageID: "m2", Text: "do it", Kind: consoleKindConfirm})
	waitFor(t, "both frames", func() bool { return len(r.inbound()) == 2 })
	for _, m := range r.inbound() {
		want := map[string]string{"m1": "", "m2": IntentConfirm}[m.MessageID]
		if m.Intent != want {
			t.Errorf("frame %s intent = %q, want %q", m.MessageID, m.Intent, want)
		}
	}
}

// The inject door takes intent "confirm" and refuses any other.
func TestInjectConfirmIntent(t *testing.T) {
	r := startInjectRig(t)
	resp, err := r.do(t, http.MethodPost, r.base+injectPath,
		[]byte(`{"conversation":"c-bad","author":"1001","text":"hi","intent":"cancel"}`), injectTestToken)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("intent cancel on /inject = %d, want 400", resp.StatusCode)
	}

	for _, tc := range []struct {
		conv, intent string
		want         bool
	}{{"c-typed", "", false}, {"c-confirm", IntentConfirm, true}} {
		body, _ := json.Marshal(injectRequest{Conversation: tc.conv, Author: injectTestAuthor, Text: "open the PR", Intent: tc.intent})
		resp, err := r.do(t, http.MethodPost, r.base+injectPath, body, injectTestToken)
		if err != nil {
			t.Fatal(err)
		}
		var out injectResponse
		err = json.NewDecoder(resp.Body).Decode(&out)
		resp.Body.Close()
		if err != nil || out.TaskID == "" {
			t.Fatalf("%s: reply %+v, err %v; want a task", tc.conv, out, err)
		}
		key, _ := injectConversationKey(tc.conv)
		deadline := time.Now().Add(5 * time.Second)
		for {
			rec, _ := r.g.reg.Get(context.Background(), key)
			if rec != nil {
				if ref, ok := rec.TaskRefFor(out.TaskID); ok {
					if ref.ChangeRequested != tc.want {
						t.Fatalf("%s: entry changeRequested = %v, want %v", tc.conv, ref.ChangeRequested, tc.want)
					}
					break
				}
			}
			if time.Now().After(deadline) {
				t.Fatalf("%s: no entry for %s", tc.conv, out.TaskID)
			}
			time.Sleep(20 * time.Millisecond)
		}
	}
}

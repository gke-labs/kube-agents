package gateway

import (
	"testing"
	"time"

	"github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"
)

// stopTestAdapter is a Slack adapter in workspace T0URS that knows UALICE as
// one of its members, so a Stop press passes the workspace check.
func stopTestAdapter() *SlackAdapter {
	s := newTestSlackAdapter(&fakeSlackAPI{users: map[string]*slack.User{"UALICE": {ID: "UALICE", TeamID: "T0URS"}}})
	s.teamID = "T0URS"
	return s
}

func stopEvent(user string) *slackevents.AgentSessionStoppedEvent {
	return &slackevents.AgentSessionStoppedEvent{Channel: "C123", ThreadTimestamp: "1.1", User: user, EventTimestamp: "2.2"}
}

func TestSlackStopFromAnotherWorkspaceIsDropped(t *testing.T) {
	api := &fakeSlackAPI{users: map[string]*slack.User{"UEXT": {ID: "UEXT", TeamID: "T0THEIRS"}}}
	s := newTestSlackAdapter(api)
	s.teamID = "T0URS"
	if _, ok := s.inboundStop(stopEvent("UEXT")); ok {
		t.Fatal("a Slack Connect member of another workspace pressed Stop")
	}
}

func TestSlackStopFailsClosedWhenTheWorkspaceCannotBeTold(t *testing.T) {
	// The lookup fails (an unknown user).
	s := newTestSlackAdapter(&fakeSlackAPI{})
	s.teamID = "T0URS"
	if _, ok := s.inboundStop(stopEvent("UNOBODY")); ok {
		t.Error("a press whose presser could not be looked up was taken")
	}
	// auth.test named no workspace.
	s = stopTestAdapter()
	s.teamID = ""
	if _, ok := s.inboundStop(stopEvent("UALICE")); ok {
		t.Error("a press was taken with no workspace to check it against")
	}
}

func TestSlackStopListedMemberSkipsTheLookup(t *testing.T) {
	api := &fakeSlackAPI{}
	s := newTestSlackAdapter(api)
	s.teamID = "T0URS"
	// The allowed-users list names UEXT, so the install has vouched for them.
	s.SetHome(func(member string) (bool, bool) { return member == "UEXT", false }, nil)
	if _, ok := s.inboundStop(stopEvent("UEXT")); !ok {
		t.Fatal("a member the allowed-users list names was refused")
	}
	if api.userInfoCalls != 0 {
		t.Fatalf("a listed member cost %d users.info calls", api.userInfoCalls)
	}
}

func TestSlackStopWorkspaceAnswerIsCached(t *testing.T) {
	api := &fakeSlackAPI{users: map[string]*slack.User{"UALICE": {ID: "UALICE", TeamID: "T0URS"}}}
	s := newTestSlackAdapter(api)
	s.teamID = "T0URS"
	clock := newFakeClock(s)
	for i, ts := range []string{"2.1", "2.2", "2.3"} {
		e := stopEvent("UALICE")
		e.EventTimestamp = ts
		if _, ok := s.inboundStop(e); !ok {
			t.Fatalf("press %d was refused", i)
		}
	}
	if api.userInfoCalls != 1 {
		t.Fatalf("three presses cost %d users.info calls, want 1", api.userInfoCalls)
	}
	clock.advance(slackStopPresserTTL + time.Second)
	e := stopEvent("UALICE")
	e.EventTimestamp = "2.4"
	s.inboundStop(e)
	if api.userInfoCalls != 2 {
		t.Fatalf("a press past the cache's bound cost %d calls in all, want 2", api.userInfoCalls)
	}
}

func TestSlackStopBecomesAnUnnamedCancel(t *testing.T) {
	s := stopTestAdapter()
	msg, ok := s.inboundStop(&slackevents.AgentSessionStoppedEvent{
		Type: "agent_session_stopped", Channel: "C123", ThreadTimestamp: "1700000000.000100",
		User: "UALICE", EventTimestamp: "1700000050.000200",
	})
	if !ok {
		t.Fatal("a Stop on a channel thread was dropped")
	}
	want := InboundMessage{Conversation: "slack:C123/1700000000.000100", Kind: "group",
		AuthorID: "UALICE", MessageID: "1700000050.000200", Intent: IntentCancel}
	if msg != want {
		t.Fatalf("got %+v, want %+v", msg, want)
	}
	if msg.TaskID != "" {
		t.Fatal("a Stop must name no task, or it never reaches the card stop")
	}
}

func TestSlackStopInADMKeysTheDMThread(t *testing.T) {
	s := stopTestAdapter()
	msg, ok := s.inboundStop(&slackevents.AgentSessionStoppedEvent{
		Channel: "D999", ThreadTimestamp: "1700000000.000100", User: "UALICE", EventTimestamp: "1700000050.000200",
	})
	if !ok {
		t.Fatal("a Stop in a DM thread was dropped")
	}
	if msg.Conversation != "slack:dm/D999/1700000000.000100" || msg.Kind != "dm" {
		t.Fatalf("got conversation %q kind %q", msg.Conversation, msg.Kind)
	}
}

func TestSlackStopDropsIncompleteBotAndRedelivered(t *testing.T) {
	s := stopTestAdapter()
	good := slackevents.AgentSessionStoppedEvent{Channel: "C123", ThreadTimestamp: "1.1", User: "UALICE", EventTimestamp: "2.2"}
	for name, e := range map[string]slackevents.AgentSessionStoppedEvent{
		"no user":    {Channel: "C123", ThreadTimestamp: "1.1", EventTimestamp: "2.2"},
		"no channel": {ThreadTimestamp: "1.1", User: "UALICE", EventTimestamp: "2.2"},
		"no thread":  {Channel: "C123", User: "UALICE", EventTimestamp: "2.2"},
		"no ts":      {Channel: "C123", ThreadTimestamp: "1.1", User: "UALICE"},
		"the bot":    {Channel: "C123", ThreadTimestamp: "1.1", User: "UBOT", EventTimestamp: "2.2"},
	} {
		e := e
		if _, ok := s.inboundStop(&e); ok {
			t.Errorf("%s: want dropped", name)
		}
	}
	if _, ok := s.inboundStop(&good); !ok {
		t.Fatal("the first delivery was dropped")
	}
	if _, ok := s.inboundStop(&good); ok {
		t.Fatal("a redelivered Stop must not stop twice")
	}
}

func TestSlackStopIsNotTakenForAMessageRedelivery(t *testing.T) {
	s := stopTestAdapter()
	// A message seen at the same channel and ts as the Stop's event_ts.
	s.alreadySeen("C123/2.2")
	if _, ok := s.inboundStop(&slackevents.AgentSessionStoppedEvent{
		Channel: "C123", ThreadTimestamp: "1.1", User: "UALICE", EventTimestamp: "2.2"}); !ok {
		t.Fatal("a Stop was dropped as a redelivery of a message")
	}
}

func TestMayStopOnSlackOnlyTheRequesters(t *testing.T) {
	alice := TaskRequester{Backend: slackBackend, Subject: "h-alice"}
	bob := TaskRequester{Backend: slackBackend, Subject: "h-bob"}
	carol := TaskRequester{Backend: slackBackend, Subject: "h-carol"}
	dave := TaskRequester{Backend: slackBackend, Subject: "h-dave"}
	rec := &SessionRecord{
		Tasks:          []TaskRef{{ID: "task-1", Requester: &alice, SteerAuthors: []TaskRequester{bob}}},
		SessionAuthors: []TaskRequester{carol},
	}
	for _, who := range []TaskRequester{alice, bob, carol} {
		if !mayStop(rec, slackBackend, who) {
			t.Errorf("%s asked for work here and was refused", who.Subject)
		}
	}
	if mayStop(rec, slackBackend, dave) {
		t.Error("someone who asked for nothing here may stop it")
	}
	if mayStop(rec, slackBackend, TaskRequester{Backend: slackBackend}) {
		t.Error("an author with no subject may stop it")
	}
	// The same subject on another backend is another person.
	if mayStop(rec, slackBackend, TaskRequester{Backend: gchatBackend, Subject: "h-alice"}) {
		t.Error("a requester's subject on another backend was taken for theirs")
	}
}

func TestMayStopOffSlackAndOnAnEmptyConversation(t *testing.T) {
	alice := TaskRequester{Backend: slackBackend, Subject: "h-alice"}
	rec := &SessionRecord{Tasks: []TaskRef{{ID: "task-1", Requester: &alice}}}
	if !mayStop(rec, gchatBackend, TaskRequester{Backend: gchatBackend, Subject: "h-other"}) {
		t.Error("the gate held a backend it does not cover")
	}
	if !mayStop(&SessionRecord{}, slackBackend, TaskRequester{Backend: slackBackend, Subject: "h-other"}) {
		t.Error("a conversation that ran nothing refused a stop, which would hide its nothing-running answer")
	}
	// Every requester copy aged out (AskTTL): the tasks remain, nobody is held.
	aged := &SessionRecord{Tasks: []TaskRef{{ID: "task-1"}, {ID: "task-2"}}}
	if !mayStop(aged, slackBackend, TaskRequester{Backend: slackBackend, Subject: "h-alice"}) {
		t.Error("a thread whose requesters all aged out locked everyone out")
	}
}

// When the record cannot tell who asked, anyone the gateway answers may press
// Stop (the maintainer's decision, 2026-10-22).
func TestMayStopFailsOpenWhenTheRecordCannotTellWhoAsked(t *testing.T) {
	bob := TaskRequester{Backend: slackBackend, Subject: "h-bob"}
	alice := TaskRequester{Backend: slackBackend, Subject: "h-alice"}
	full := make([]TaskRef, taskHistoryCap)
	for i := range full {
		full[i] = TaskRef{ID: "task", Requester: &bob}
	}
	for name, rec := range map[string]*SessionRecord{
		// Alice's ask aged out past AskTTL; Bob's newer one is still held.
		"one requester pruned": {Tasks: []TaskRef{{ID: "task-1"}, {ID: "task-2", Requester: &bob}}},

		"steer authors overflowed": {Tasks: []TaskRef{{ID: "task-1", Requester: &bob, SteerAuthorsOverflow: true}}},

		"session authors unknown": {Tasks: []TaskRef{{ID: "task-1", Requester: &bob}}, SessionAuthorsUnknown: true},

		"history at its cap": {Tasks: full},
	} {
		if !mayStop(rec, slackBackend, alice) {
			t.Errorf("%s: the requester whose copy may be gone was refused", name)
		}
	}
	// A record that can tell still gates.
	if mayStop(&SessionRecord{Tasks: []TaskRef{{ID: "task-1", Requester: &bob}}}, slackBackend, alice) {
		t.Error("a complete record let a non-requester stop the thread")
	}
}

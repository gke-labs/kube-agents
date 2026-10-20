package gateway

import (
	"testing"

	"github.com/slack-go/slack/slackevents"
)

func TestSlackStopBecomesAnUnnamedCancel(t *testing.T) {
	s := newTestSlackAdapter(&fakeSlackAPI{})
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
	s := newTestSlackAdapter(&fakeSlackAPI{})
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
	s := newTestSlackAdapter(&fakeSlackAPI{})
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
	s := newTestSlackAdapter(&fakeSlackAPI{})
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
}

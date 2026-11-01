package capability

import (
	"context"
	"encoding/json"
	"sync"
	"testing"

	"github.com/nats-io/nats.go"
)

// A capability decision is bound to the request that asked for it, so a
// caller acts only on the answer to its own question. Each check carries a
// fresh request id, the verifier echoes it, and the client takes no reply
// whose id is missing or different: such a reply is no answer, and a check
// that never gets its own answer ends the way an unanswered one always has.

// testRequestID is a well-formed id for tests that build a Request by hand.
func testRequestID(t *testing.T) string {
	t.Helper()
	id, err := newRequestID()
	if err != nil {
		t.Fatalf("request id: %v", err)
	}
	return id
}

// echoAnswer is resp with the request id of the request in data, the way the
// verifier answers.
func echoAnswer(t *testing.T, data []byte, resp Response) []byte {
	t.Helper()
	var req Request
	if err := json.Unmarshal(data, &req); err != nil {
		t.Errorf("the client sent a request that does not parse: %v", err)
	}
	resp.RequestID = req.RequestID
	b, err := json.Marshal(resp)
	if err != nil {
		t.Errorf("marshal: %v", err)
	}
	return b
}

// answeringVerifier stands in for a verifier and answers every ask with
// whatever answer returns for the request it was sent. It records the request
// ids it saw.
func answeringVerifier(t *testing.T, nc *nats.Conn, answer func(req Request) []Response) *[]string {
	t.Helper()
	var mu sync.Mutex
	var seen []string
	sub, err := nc.QueueSubscribe(VerifySubscribe, VerifyQueue, func(m *nats.Msg) {
		var req Request
		if err := json.Unmarshal(m.Data, &req); err != nil {
			t.Errorf("the client sent a request that does not parse: %v", err)
			return
		}
		mu.Lock()
		seen = append(seen, req.RequestID)
		mu.Unlock()
		for _, resp := range answer(req) {
			b, err := json.Marshal(resp)
			if err != nil {
				t.Errorf("marshal: %v", err)
				return
			}
			_ = nc.Publish(m.Reply, b)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	return &seen
}

func TestAReplyEchoingTheRequestIDIsTheAnswer(t *testing.T) {
	nc := runServer(t)
	answeringVerifier(t, nc, func(req Request) []Response {
		return []Response{{Allowed: true, RequestID: req.RequestID}}
	})
	c := clientAs(t, nc, podB)
	c.Timeout, c.ReachWindow = shortAsk, shortWindow
	if err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P"); err != nil {
		t.Fatalf("a reply echoing the request id was not taken as the answer: %v", err)
	}
}

func TestAReplyWithAnotherRequestIDIsNotAnAnswer(t *testing.T) {
	nc := runServer(t)
	other := testRequestID(t)
	answeringVerifier(t, nc, func(Request) []Response {
		return []Response{{Allowed: true, RequestID: other}}
	})
	c := clientAs(t, nc, podB)
	c.Timeout, c.ReachWindow = shortAsk, shortWindow
	err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P")
	if err == nil {
		t.Fatal("a reply carrying another request's id was taken as this check's answer")
	}
	mustRefuse(t, err, unreachableRule)
}

func TestAReplyWithNoRequestIDIsNotAnAnswer(t *testing.T) {
	nc := runServer(t)
	answeringVerifier(t, nc, func(Request) []Response {
		return []Response{{Allowed: true}}
	})
	c := clientAs(t, nc, podB)
	c.Timeout, c.ReachWindow = shortAsk, shortWindow
	err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P")
	if err == nil {
		t.Fatal("a reply with no request id was taken as this check's answer")
	}
	mustRefuse(t, err, unreachableRule)
}

// A reply that is not this check's answer is skipped, not the end of the
// wait: the answer to this check's own question, arriving after it, is used.
func TestTheClientKeepsWaitingPastAnotherRequestsReply(t *testing.T) {
	nc := runServer(t)
	other := testRequestID(t)
	answeringVerifier(t, nc, func(req Request) []Response {
		return []Response{
			{Allowed: true, RequestID: other},
			{Reason: "this check's own answer", RequestID: req.RequestID},
		}
	})
	c := clientAs(t, nc, podB)
	c.Timeout, c.ReachWindow = shortAsk, -1
	err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P")
	mustRefuse(t, err, "this check's own answer")
}

func TestEveryCheckCarriesItsOwnRequestID(t *testing.T) {
	nc := runServer(t)
	seen := answeringVerifier(t, nc, func(req Request) []Response {
		return []Response{{Allowed: true, RequestID: req.RequestID}}
	})
	c := clientAs(t, nc, podB)
	const checks = 8
	for i := 0; i < checks; i++ {
		if err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P"); err != nil {
			t.Fatalf("check %d: %v", i, err)
		}
	}
	if err := c.CheckProfileRun(context.Background(), Ref{Key: "root.task-x", Revision: 1}, "tse", TierRead, "project/P"); err != nil {
		t.Fatalf("profile.run check: %v", err)
	}
	ids := map[string]bool{}
	for _, id := range *seen {
		if !validRequestID(id) {
			t.Errorf("request id %q is not %d bytes of hex", id, requestIDBytes)
		}
		if ids[id] {
			t.Errorf("request id %q was sent by two checks", id)
		}
		ids[id] = true
	}
	if len(ids) != checks+1 {
		t.Errorf("saw %d distinct request ids across %d checks", len(ids), checks+1)
	}
}

// The verifier's side: it echoes the id of a request it answers, and a
// request without a well-formed id is not well-formed.
func TestTheVerifierEchoesTheRequestIDAndRequiresOne(t *testing.T) {
	s, _, hop := twoHop(t)
	svc := &Service{Resolver: &Resolver{Store: s}}
	ctx := context.Background()
	subj, err := VerifySubject(podB)
	if err != nil {
		t.Fatal(err)
	}
	ask := func(id string) Response {
		b, err := json.Marshal(Request{Ref: hop, Verb: VerbTaskExecute, Resource: "project/P/cluster/C", RequestID: id})
		if err != nil {
			t.Fatal(err)
		}
		return svc.Answer(ctx, subj, b)
	}

	id := testRequestID(t)
	if got := ask(id); !got.Allowed || got.RequestID != id {
		t.Fatalf("a well-formed request: got %+v, want allowed with id %s", got, id)
	}
	// A refusal is an answer too, and it is bound the same way.
	evil, _ := VerifySubject(podEvil)
	b, _ := json.Marshal(Request{Ref: hop, Verb: VerbTaskExecute, Resource: "project/P/cluster/C", RequestID: id})
	if got := svc.Answer(ctx, evil, b); got.Allowed || got.RequestID != id {
		t.Fatalf("a refusal: got %+v, want refused with id %s", got, id)
	}

	for _, bad := range []string{
		"",
		"abc",
		id[:len(id)-1],
		id + "0",
		"ZZ" + id[2:],
		"AB" + id[2:], // uppercase hex is not what the client sends
	} {
		got := ask(bad)
		if got.Allowed || got.Reason != "the request is not well-formed" || got.RequestID != "" {
			t.Errorf("request id %q: got %+v, want a malformed-request refusal with no id", bad, got)
		}
	}
}

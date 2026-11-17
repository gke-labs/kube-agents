package capability

import (
	"context"
	"encoding/json"
	"sync"
	"testing"

	"github.com/nats-io/nats.go"
)

// A capability decision is bound to the request that asked for it (#5647), so
// a caller acts only on the answer to its own question. Each check carries a
// fresh request id, the verifier echoes it, and the client takes no reply
// carrying a different id: such a reply is no answer, and a check that never
// gets its own answer ends the way an unanswered one always has. For one
// release a reply carrying no id at all, an older verifier's, is still acted
// on (acceptUnechoedReply), so a new client keeps working during a roll.

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

// A new client against a verifier from before #5647, which echoes nothing: while
// acceptUnechoedReply holds, its verdicts are acted on, allowed and refused
// alike, so a roll that reaches the clients first breaks nothing. The follow-up
// that turns acceptUnechoedReply false makes this a check that fails closed.
func TestAnOlderVerifiersUnechoedReplyIsActedOnForNow(t *testing.T) {
	if !acceptUnechoedReply {
		t.Skip("un-echoed replies are no longer accepted; TestAnUnechoedReplyFailsClosed covers this")
	}
	nc := runServer(t)
	answeringVerifier(t, nc, func(req Request) []Response {
		if req.Resource == "project/refused" {
			return []Response{{Reason: "the older verifier refused it"}}
		}
		return []Response{{Allowed: true}}
	})
	c := clientAs(t, nc, podB)
	c.Timeout, c.ReachWindow = shortAsk, shortWindow
	if err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P"); err != nil {
		t.Fatalf("an older verifier's allow was not acted on: %v", err)
	}
	err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/refused")
	mustRefuse(t, err, "the older verifier refused it")
}

// A reply that is not this check's answer is dropped, not the end of the
// wait: the answer to this check's own question, arriving after it in the
// window, is acted on. (TestAReplyWithAnotherRequestIDIsNotAnAnswer is the
// other half: with no correct answer at all, the check fails closed.)
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
	if err := c.CheckProfileRun(context.Background(), Ref{Key: "root.task-x", Revision: 1}, "tse", TierRead, "project/P", nil); err != nil {
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

// The verifier's side: it echoes the id of a request it answers, answers a
// request carrying none (a client from before #5647) with none, and a request
// whose id is present but not well-formed is not well-formed.
func TestTheVerifierEchoesTheRequestIDAndRefusesAMalformedOne(t *testing.T) {
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

	// An older client's request carries no id: answered, with none echoed.
	if got := ask(""); !got.Allowed || got.RequestID != "" {
		t.Fatalf("a request with no id: got %+v, want allowed with no id", got)
	}
	for _, bad := range []string{
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

// One check is one question, however many times the client has to ask it to
// reach the verifier: every ask carries the same id, so the answer to any of
// them, late or not, is the answer to this check.
func TestACheckKeepsItsRequestIDAcrossItsRetries(t *testing.T) {
	nc := runServer(t)
	var mu sync.Mutex
	asks := 0
	seen := answeringVerifier(t, nc, func(req Request) []Response {
		mu.Lock()
		defer mu.Unlock()
		asks++
		if asks == 1 {
			return nil // the first ask goes unanswered, so the client asks again
		}
		return []Response{{Allowed: true, RequestID: req.RequestID}}
	})
	c := clientAs(t, nc, podB)
	c.Timeout, c.ReachWindow = shortAsk, shortWindow
	if err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1}, VerbTaskExecute, "project/P"); err != nil {
		t.Fatalf("the retried check: %v", err)
	}
	if len(*seen) < 2 || (*seen)[0] != (*seen)[1] {
		t.Fatalf("the retries carried ids %v, want one id repeated", *seen)
	}
}

// The reply decision on both sides of the release that drops the older
// verifier: while un-echoed replies are accepted, a reply with no id is an
// answer; once they aren't, it isn't. A reply carrying another id is never an
// answer, and the echo of this check's own id always is.
func TestWhichRepliesAreTheAnswerBeforeAndAfterTheOlderVerifierGoes(t *testing.T) {
	id, other := testRequestID(t), testRequestID(t)
	for _, tc := range []struct {
		name           string
		reply          string
		acceptUnechoed bool
		want           bool
	}{
		{"own id, tolerant", id, true, true},
		{"own id, strict", id, false, true},
		{"no id, tolerant", "", true, true},
		{"no id, strict", "", false, false},
		{"another id, tolerant", other, true, false},
		{"another id, strict", other, false, false},
	} {
		if got := isAnswerTo(Response{Allowed: true, RequestID: tc.reply}, id, tc.acceptUnechoed); got != tc.want {
			t.Errorf("%s: isAnswerTo = %v, want %v", tc.name, got, tc.want)
		}
	}
}

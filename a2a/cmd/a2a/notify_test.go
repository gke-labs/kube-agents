package main

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func startNotifyServer(t *testing.T) *server.Server {
	t.Helper()
	s, err := server.NewServer(&server.Options{Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true})
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(s.Shutdown)
	return s
}

// answerNotify stands in for the gateway: it records what arrived and on
// which reply subject, and answers with reply.
func answerNotify(t *testing.T, url string, reply lib.NotifyReply) (got chan lib.NotifyRequest, replies chan string) {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	got, replies = make(chan lib.NotifyRequest, 1), make(chan string, 1)
	_, err = nc.Subscribe(lib.NotifySubjectGchat, func(m *nats.Msg) {
		var req lib.NotifyRequest
		_ = json.Unmarshal(m.Data, &req)
		got <- req
		replies <- m.Reply
		body, _ := json.Marshal(reply)
		_ = m.Respond(body)
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	return got, replies
}

func notifyEnv(t *testing.T, url string) {
	t.Setenv("NATS_URL", url)
	t.Setenv(lib.EnvBusUser, "agent")
	t.Setenv(lib.EnvBusTokenFile, "")
}

// The request goes out on the platform's notify subject with its reply in the
// notify reply namespace, never the client's _INBOX, carrying the text and
// thread it was given.
func TestNotifySendsOnTheSubjectWithAReplyInTheNamespace(t *testing.T) {
	s := startNotifyServer(t)
	notifyEnv(t, s.ClientURL())
	got, replies := answerNotify(t, s.ClientURL(), lib.NotifyReply{MessageID: "spaces/H/messages/1", ThreadID: "spaces/H/threads/1"})

	if err := run([]string{"notify", "--platform", "google_chat", "--thread", "spaces/H/threads/1", "the drift report"}); err != nil {
		t.Fatalf("notify: %v", err)
	}
	req := <-got
	if req.Text != "the drift report" || req.Thread != "spaces/H/threads/1" {
		t.Errorf("request = %+v", req)
	}
	if reply := <-replies; !strings.HasPrefix(reply, lib.NotifyReplyPrefix) || reply == lib.NotifyReplyPrefix {
		t.Errorf("reply subject = %q, want one under %s", reply, lib.NotifyReplyPrefix)
	}
}

func TestNotifyFailsWhenTheGatewayPostedNothing(t *testing.T) {
	s := startNotifyServer(t)
	notifyEnv(t, s.ClientURL())
	answerNotify(t, s.ClientURL(), lib.NotifyReply{Error: "thread is not a thread of the home channel"})
	err := run([]string{"notify", "--platform", "google_chat", "x"})
	if err == nil || !strings.Contains(err.Error(), "posted nothing") {
		t.Errorf("err = %v, want the gateway's refusal", err)
	}
}

func TestNotifyTimesOutWithNoGateway(t *testing.T) {
	s := startNotifyServer(t)
	notifyEnv(t, s.ClientURL())
	// No gateway at all is "no responders": refused, exit 1. Only a request
	// that someone received and did not answer is outcome-unknown.
	err := run([]string{"notify", "--platform", "google_chat", "--timeout", "200ms", "x"})
	if err == nil || errors.Is(err, errNotifyOutcomeUnknown) || !strings.Contains(err.Error(), "not armed") {
		t.Errorf("err = %v, want the no-responders refusal, not outcome-unknown", err)
	}
}

// Report text that looks like a flag is text once "--" ends the flags: a
// markdown bullet, a rule, a negative number, and a message that is exactly a
// valid flag, which would otherwise redirect the post or read stdin.
func TestNotifyTextThatLooksLikeAFlag(t *testing.T) {
	for _, text := range []string{"- pod crashlooping\n- node lost", "--- Daily report", "-1 nodes down", "--thread=spaces/H/threads/X", "-"} {
		s := startNotifyServer(t)
		notifyEnv(t, s.ClientURL())
		got, _ := answerNotify(t, s.ClientURL(), lib.NotifyReply{MessageID: "m", ThreadID: "t"})
		if err := run([]string{"notify", "--platform", "google_chat", "--", text}); err != nil {
			t.Errorf("%q: %v", text, err)
			continue
		}
		if req := <-got; req.Text != text || req.Thread != "" {
			t.Errorf("%q arrived as %+v", text, req)
		}
	}
}

// A request someone received and did not answer is outcome-unknown: the post
// may still land, and main exits notifyExitOutcomeUnknown so the caller does
// not send it again.
func TestNotifyWithNoAnswerIsOutcomeUnknown(t *testing.T) {
	s := startNotifyServer(t)
	notifyEnv(t, s.ClientURL())
	nc, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	if _, err := nc.Subscribe(lib.NotifySubjectGchat, func(*nats.Msg) {}); err != nil {
		t.Fatal(err)
	}
	_ = nc.Flush()
	err = run([]string{"notify", "--platform", "google_chat", "--timeout", "200ms", "x"})
	if !errors.Is(err, errNotifyOutcomeUnknown) {
		t.Errorf("err = %v, want outcome-unknown", err)
	}
}

func TestNotifyRefusesAnUnknownPlatform(t *testing.T) {
	err := run([]string{"notify", "--platform", "telegram", "x"})
	if err == nil || !strings.Contains(err.Error(), "--platform must be one of") {
		t.Errorf("err = %v", err)
	}
}

func TestNotifyTextFromArgumentOrStdin(t *testing.T) {
	if got, _ := notifyText([]string{"hello"}, false, strings.NewReader("ignored")); got != "hello" {
		t.Errorf("argument: %q", got)
	}
	for _, args := range [][]string{nil, {"-"}} {
		if got, _ := notifyText(args, false, strings.NewReader("from stdin")); got != "from stdin" {
			t.Errorf("args %v: %q, want stdin", args, got)
		}
	}
	if got, _ := notifyText([]string{"-"}, true, strings.NewReader("stdin")); got != "-" {
		t.Errorf(`"-" after "--" = %q, want the literal text`, got)
	}
	if _, err := notifyText([]string{"a", "b"}, false, strings.NewReader("")); err == nil {
		t.Error("two arguments accepted")
	}
}

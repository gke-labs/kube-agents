package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
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
	// No gateway at all is "no responders": route unavailable, exit 4. Only
	// a request that someone received and did not answer is outcome-unknown.
	err := run([]string{"notify", "--platform", "google_chat", "--timeout", "200ms", "x"})
	if !errors.Is(err, errNotifyRouteUnavailable) || errors.Is(err, errNotifyOutcomeUnknown) {
		t.Errorf("err = %v, want route-unavailable (nothing posted), not outcome-unknown", err)
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

// An unreachable bus is route-unavailable, like an unarmed route: nothing was
// posted, and nothing about the destination is known.
func TestNotifyWithNoBusIsRouteUnavailable(t *testing.T) {
	t.Setenv("NATS_URL", "nats://127.0.0.1:1")
	t.Setenv(lib.EnvBusUser, "agent")
	t.Setenv(lib.EnvBusTokenFile, "")
	err := run([]string{"notify", "--platform", "google_chat", "x"})
	if !errors.Is(err, errNotifyRouteUnavailable) {
		t.Errorf("err = %v, want route-unavailable", err)
	}
}

// A refused login is a refusal, not route-unavailable: waiting will not fix it.
func TestNotifyARefusedLoginIsNotRouteUnavailable(t *testing.T) {
	s, err := server.NewServer(&server.Options{
		Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true,
		Users: []*server.User{{Username: "agent", Password: "right"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(s.Shutdown)
	notifyEnv(t, s.ClientURL())
	t.Setenv("NATS_PASSWORD", "wrong")
	err = run([]string{"notify", "--platform", "google_chat", "x"})
	if err == nil || errors.Is(err, errNotifyRouteUnavailable) {
		t.Errorf("err = %v, want a refusal that is not route-unavailable", err)
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

// A publish the principal is not granted is dropped by the server and shows
// up only as the connection's async error. The command must report it as a
// refusal (exit 1) promptly, not wait out the timeout and exit 3, which the
// alert path would record as sent.
func TestNotifyARefusedPublishIsARefusalNotAnUnknown(t *testing.T) {
	s, err := server.NewServer(&server.Options{
		Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true,
		Users: []*server.User{{
			Username: "agent", Password: "pw",
			Permissions: &server.Permissions{
				Publish:   &server.SubjectPermission{Allow: []string{"_INBOX.>"}, Deny: []string{lib.NotifySubjectGchat}},
				Subscribe: &server.SubjectPermission{Allow: []string{">"}},
			},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(s.Shutdown)
	notifyEnv(t, s.ClientURL())
	t.Setenv("NATS_PASSWORD", "pw")

	started := time.Now()
	err = run([]string{"notify", "--platform", "google_chat", "--timeout", "10s", "x"})
	if err == nil || errors.Is(err, errNotifyOutcomeUnknown) || errors.Is(err, errNotifyRouteUnavailable) ||
		!strings.Contains(err.Error(), "refused") {
		t.Fatalf("err = %v, want a bus refusal: not outcome-unknown, not route-unavailable", err)
	}
	if elapsed := time.Since(started); elapsed > 5*time.Second {
		t.Errorf("took %s; a refusal should not wait out the timeout", elapsed)
	}
}

// TestNotifyBlocksFileMustBeReadableJSON: --blocks-file is refused before
// anything reaches the bus when it is missing or is not JSON.
func TestNotifyBlocksFileMustBeReadableJSON(t *testing.T) {
	dir := t.TempDir()
	bad := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(bad, []byte("[{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, path := range map[string]string{"missing": filepath.Join(dir, "none.json"), "not JSON": bad} {
		err := runNotify([]string{"--platform", "slack", "--blocks-file", path, "--", "x"})
		if err == nil || !strings.Contains(err.Error(), "--blocks-file") && !strings.Contains(err.Error(), "not JSON") {
			t.Errorf("%s: err = %v, want a --blocks-file refusal", name, err)
		}
	}
}

// A refused reply subscription is caught before the request is published: the
// gateway would otherwise post a text whose answer this client cannot hear,
// and the caller, told "nothing was posted", would send it again.
func TestNotifyARefusedSubscribeSendsNothing(t *testing.T) {
	s, err := server.NewServer(&server.Options{
		Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true,
		Users: []*server.User{
			{
				Username: "agent", Password: "pw",
				Permissions: &server.Permissions{
					Publish:   &server.SubjectPermission{Allow: []string{">"}},
					Subscribe: &server.SubjectPermission{Allow: []string{">"}, Deny: []string{lib.NotifyReplyPrefix + ">"}},
				},
			},
			{Username: "gateway", Password: "pw"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(s.Shutdown)
	gw, err := nats.Connect(s.ClientURL(), nats.UserInfo("gateway", "pw"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(gw.Close)
	requests, err := gw.SubscribeSync(lib.NotifySubjectGchat)
	if err != nil {
		t.Fatal(err)
	}
	if err := gw.Flush(); err != nil {
		t.Fatal(err)
	}
	notifyEnv(t, s.ClientURL())
	t.Setenv("NATS_PASSWORD", "pw")

	err = run([]string{"notify", "--platform", "google_chat", "--timeout", "10s", "x"})
	if err == nil || errors.Is(err, errNotifyOutcomeUnknown) || errors.Is(err, errNotifyRouteUnavailable) ||
		!strings.Contains(err.Error(), "nothing was sent") {
		t.Fatalf("err = %v, want a refusal before the publish", err)
	}
	if msg, err := requests.NextMsg(500 * time.Millisecond); err == nil {
		t.Fatalf("the gateway received %q; a refused subscribe must not publish", msg.Data)
	}
}

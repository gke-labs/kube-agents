package authcallout

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/gateway"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The chat.notify route's grants, measured against a real server under the
// operator's render: the agent (callout-issued) asks, the gateway (static)
// answers through the real Notifier, and every other way onto either subject
// is refused by the server.
//
// The render-side door test (k8s-operator's
// TestChatNotifySubjectsHaveExactlyOneWriterAndOneReader) proves no grant
// list names the subjects; this proves the server, holding those lists, does
// what the lists say - including for the session pods, whose grants the
// callout derives per connection and no render lists.

type recordingPoster struct {
	mu    sync.Mutex
	posts []string
}

func (p *recordingPoster) PostNotify(space, thread, text string) (string, string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.posts = append(p.posts, text)
	return space + "/messages/1", space + "/threads/1", nil
}

func (p *recordingPoster) count() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.posts)
}

const notifyTestHome = "spaces/HOME"

func TestTheNotifyRouteWorksForTheAgentAndNobodyElse(t *testing.T) {
	h, _ := startHarnessWithServerLogMap(t, capMap(t), capTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	// The gateway answers, as the gateway, with the real handler.
	gwClient, err := lib.Connect(ctx, h.url, lib.WithName("notify-gateway"),
		lib.WithUserPassword("gateway", renderedGatewayPassword))
	if err != nil {
		t.Fatalf("connect as gateway: %v", err)
	}
	defer gwClient.Close()
	poster := &recordingPoster{}
	notifier, err := gateway.NewGchatNotifier(poster, notifyTestHome, nil)
	if err != nil {
		t.Fatal(err)
	}
	sub, err := notifier.Start(gwClient)
	if err != nil {
		t.Fatalf("the gateway could not subscribe chat.notify.gchat under its rendered grant: %v", err)
	}
	defer sub.Stop()

	// The agent asks and is answered on its reply namespace.
	agent, agentViolations := h.connectAs(t, "agent", agentToken)
	reply := lib.NotifyReplyPrefix + "test-1"
	in, err := agent.SubscribeSync(reply)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(lib.NotifyRequest{Text: "a cron finding"})
	if err := agent.PublishRequest(lib.NotifySubjectGchat, reply, body); err != nil {
		t.Fatal(err)
	}
	msg, err := in.NextMsg(5 * time.Second)
	if err != nil {
		t.Fatalf("the agent got no answer under the rendered grants: %v", err)
	}
	var answer lib.NotifyReply
	if err := json.Unmarshal(msg.Data, &answer); err != nil || answer.ThreadID != notifyTestHome+"/threads/1" {
		t.Fatalf("answer = %s (%v)", msg.Data, err)
	}
	if poster.count() != 1 {
		t.Fatalf("posted %d times, want 1", poster.count())
	}

	// The agent cannot forge an answer, nor read the requests.
	checkPublish(t, agent, agentViolations, map[string]bool{
		lib.NotifyReplyPrefix + "test-2": true,
	})
	if !subscribeRefused(t, agent, agentViolations, lib.NotifySubjectGchat) {
		t.Error("the agent could subscribe to chat.notify.gchat; it would read every other notify")
	}

	// Nobody else can send a notify or read the answers. The bridge shares the
	// agent's pod and is the likeliest place for a grant to drift to; the
	// session pods' grants are minted per connection; web is the credential
	// handed to a browser; provision and the verifier are the other callout
	// principals.
	others := []struct {
		name string
		nc   *nats.Conn
		v    chan error
	}{}
	add := func(name string, nc *nats.Conn, v chan error) {
		others = append(others, struct {
			name string
			nc   *nats.Conn
			v    chan error
		}{name, nc, v})
	}
	bridge, bv := connectStatic(t, h, "bridge", "pw-bridge")
	add("bridge", bridge, bv)
	web, wv := connectStatic(t, h, "web", "pw-web")
	add("web", web, wv)
	session, sv := h.connectAs(t, podA, tokenPodA)
	add("session", session, sv)
	provision, pv := h.connectAs(t, "provision", tokenProvision)
	add("provision", provision, pv)
	verifier, vv := h.connectAs(t, "verifier", tokenVerifier)
	add("verifier", verifier, vv)
	for _, o := range others {
		checkPublish(t, o.nc, o.v, map[string]bool{lib.NotifySubjectGchat: true})
		if !subscribeRefused(t, o.nc, o.v, lib.NotifyReplyPrefix+">") {
			t.Errorf("%s could subscribe to the notify reply namespace", o.name)
		}
		if !subscribeRefused(t, o.nc, o.v, lib.NotifySubjectGchat) {
			t.Errorf("%s could subscribe to chat.notify.gchat", o.name)
		}
		// And end to end: a well-formed request, reply subject in the
		// namespace, that the gateway would post if the server let it
		// through. Last in the loop, because its refusal lands on o.v.
		before := poster.count()
		if err := o.nc.PublishRequest(lib.NotifySubjectGchat, lib.NotifyReplyPrefix+o.name, body); err != nil {
			t.Fatal(err)
		}
		_ = o.nc.Flush()
		time.Sleep(200 * time.Millisecond)
		if poster.count() != before {
			t.Errorf("%s's notify reached the gateway and was posted", o.name)
		}
	}
}

// TestTheSlackNotifyRouteWorksForTheAgentAndNobodyElse is the same
// measurement for chat.notify.slack: the agent asks and the gateway's Slack
// notifier answers on the shared reply namespace, and no other principal can
// send a Slack notify or read the requests.
func TestTheSlackNotifyRouteWorksForTheAgentAndNobodyElse(t *testing.T) {
	h, _ := startHarnessWithServerLogMap(t, capMap(t), capTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	gwClient, err := lib.Connect(ctx, h.url, lib.WithName("notify-gateway-slack"),
		lib.WithUserPassword("gateway", renderedGatewayPassword))
	if err != nil {
		t.Fatalf("connect as gateway: %v", err)
	}
	defer gwClient.Close()
	poster := &recordingPoster{}
	notifier, err := gateway.NewSlackNotifier(poster, "C0HOME", nil)
	if err != nil {
		t.Fatal(err)
	}
	sub, err := notifier.Start(gwClient)
	if err != nil {
		t.Fatalf("the gateway could not subscribe chat.notify.slack under its rendered grant: %v", err)
	}
	defer sub.Stop()

	agent, agentViolations := h.connectAs(t, "agent", agentToken)
	reply := lib.NotifyReplyPrefix + "slack-1"
	in, err := agent.SubscribeSync(reply)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(lib.NotifyRequest{Text: "a cron finding"})
	if err := agent.PublishRequest(lib.NotifySubjectSlack, reply, body); err != nil {
		t.Fatal(err)
	}
	msg, err := in.NextMsg(5 * time.Second)
	if err != nil {
		t.Fatalf("the agent got no answer on chat.notify.slack under the rendered grants: %v", err)
	}
	var answer lib.NotifyReply
	if err := json.Unmarshal(msg.Data, &answer); err != nil || answer.Error != "" || answer.MessageID == "" {
		t.Fatalf("answer = %s (%v)", msg.Data, err)
	}
	if poster.count() != 1 {
		t.Fatalf("posted %d times, want 1", poster.count())
	}
	if !subscribeRefused(t, agent, agentViolations, lib.NotifySubjectSlack) {
		t.Error("the agent could subscribe to chat.notify.slack; it would read every other notify")
	}

	type principal struct {
		name string
		nc   *nats.Conn
		v    chan error
	}
	bridge, bv := connectStatic(t, h, "bridge", "pw-bridge")
	web, wv := connectStatic(t, h, "web", "pw-web")
	session, sv := h.connectAs(t, podA, tokenPodA)
	provision, pv := h.connectAs(t, "provision", tokenProvision)
	verifier, vv := h.connectAs(t, "verifier", tokenVerifier)
	for _, o := range []principal{{"bridge", bridge, bv}, {"web", web, wv}, {"session", session, sv}, {"provision", provision, pv}, {"verifier", verifier, vv}} {
		checkPublish(t, o.nc, o.v, map[string]bool{lib.NotifySubjectSlack: true})
		if !subscribeRefused(t, o.nc, o.v, lib.NotifySubjectSlack) {
			t.Errorf("%s could subscribe to chat.notify.slack", o.name)
		}
		before := poster.count()
		if err := o.nc.PublishRequest(lib.NotifySubjectSlack, lib.NotifyReplyPrefix+o.name, body); err != nil {
			t.Fatal(err)
		}
		_ = o.nc.Flush()
		time.Sleep(200 * time.Millisecond)
		if poster.count() != before {
			t.Errorf("%s's Slack notify reached the gateway and was posted", o.name)
		}
	}
}

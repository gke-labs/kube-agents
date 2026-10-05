/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"
)

// TestConsoleGrantOnARealServer is the refusal proof for the console user,
// measured the way the gateway's and the bridge's are: the config the
// operator renders, run by an embedded nats-server, and a client connected
// as console.
//
// Allowed: publish on its own inbound subject, delivered to a gateway
// subscribed on it; subscribe on its own outbound subject and on a2a.>.
// Refused, read from the server log rather than the client's timeout:
// publish on the task plane, on the outbound console subject, on the KV data
// plane, and every JetStream verb on KV_session-state -- STREAM.INFO
// included, both through js.Stream and as the raw request carrying
// {"subjects_filter":">"}, the body that would list every session key.
func TestConsoleGrantOnARealServer(t *testing.T) {
	creds := a2aFullCreds("c", "1")
	conf := string(buildA2ANATSConfigSecret(a2aTestAgent(), creds, a2aTestCalloutKeys(t)).Data["nats.conf"])
	consolePW := string(creds.Data["console-password"])
	seedPW := string(creds.Data["seed-password"])
	gatewayPW := string(creds.Data["gateway-password"])

	s, log := a2aStartRenderedServer(t, conf)
	a2aProvisionLikeTheScript(t, s.ClientURL(), seedPW)
	nc, js := a2aConnectAs(t, s.ClientURL(), "console", consolePW)

	// Allowed.
	if err := nc.Publish("chat.console.tab-1.in", []byte(`{"messageId":"m1","text":"hi"}`)); err != nil {
		t.Fatal(err)
	}
	outSub, err := nc.SubscribeSync("chat.console.tab-1.out")
	if err != nil {
		t.Fatal(err)
	}
	a2aSub, err := nc.SubscribeSync("a2a.>")
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond) // the log line is written on the server's read loop
	if v := log.publishViolations("console"); len(v) != 0 {
		t.Fatalf("allowed operations logged Publish Violation lines: %q", v)
	}
	if v := log.subscriptionViolations("console"); len(v) != 0 {
		t.Fatalf("allowed operations logged Subscription Violation lines: %q", v)
	}

	// SubscribeSync's SUB frame is fire-and-forget: a server-side Subscription
	// Violation never surfaces as an error from that call, and the zero-
	// violation check above only proves the server accepted the SUB, not that
	// a message addressed to it actually arrives. So each subscribe grant is
	// proven by having a principal that may write there publish one frame,
	// read back with NextMsg.
	gw, _ := a2aConnectAs(t, s.ClientURL(), "gateway", gatewayPW)
	if err := gw.Publish("chat.console.tab-1.out", []byte("hello from gateway")); err != nil {
		t.Fatal(err)
	}
	if err := gw.Flush(); err != nil {
		t.Fatal(err)
	}
	if msg, err := outSub.NextMsg(2 * time.Second); err != nil {
		t.Fatalf("console did not receive on chat.console.tab-1.out: %v (server violations for gateway: %q)",
			err, log.publishViolations("gateway"))
	} else if string(msg.Data) != "hello from gateway" {
		t.Fatalf("chat.console.tab-1.out delivered %q, want %q", msg.Data, "hello from gateway")
	}

	const consoleTaskSubmission = "a2a.tasks.platform.t-console.in"
	if err := gw.Publish(consoleTaskSubmission, []byte(`{"kind":"message"}`)); err != nil {
		t.Fatal(err)
	}
	if err := gw.Flush(); err != nil {
		t.Fatal(err)
	}
	if msg, err := a2aSub.NextMsg(2 * time.Second); err != nil {
		t.Fatalf("console did not receive on a2a.> (%s): %v (server violations for gateway: %q)",
			consoleTaskSubmission, err, log.publishViolations("gateway"))
	} else if msg.Subject != consoleTaskSubmission {
		t.Fatalf("a2a.> delivered subject %q, want %q", msg.Subject, consoleTaskSubmission)
	}

	// The inbound grant, end to end: a gateway subscribed on the console's
	// inbound subject receives the frame the console publishes there.
	inSub, err := gw.SubscribeSync("chat.console.tab-1.in")
	if err != nil {
		t.Fatal(err)
	}
	if err := gw.Flush(); err != nil {
		t.Fatal(err)
	}
	if err := nc.Publish("chat.console.tab-1.in", []byte(`{"messageId":"m2","text":"to gateway"}`)); err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	if msg, err := inSub.NextMsg(2 * time.Second); err != nil {
		t.Fatalf("gateway did not receive on chat.console.tab-1.in: %v (server violations: console %q, gateway subscribe %q)",
			err, log.publishViolations("console"), log.subscriptionViolations("gateway"))
	} else if string(msg.Data) != `{"messageId":"m2","text":"to gateway"}` {
		t.Fatalf("chat.console.tab-1.in delivered %q", msg.Data)
	}

	// STREAM.INFO on a KV stream is refused, both as the client library
	// sends it and as the raw request whose body would enumerate the keys:
	// {"subjects_filter":">"} makes the reply carry state.subjects, one
	// entry per key.
	const kvInfo = "$JS.API.STREAM.INFO.KV_session-state"
	// A refused request has no reply, so js.Stream returns on its context's
	// deadline; keep that short.
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if _, err := js.Stream(ctx, "KV_session-state"); err == nil {
		t.Error("js.Stream(KV_session-state) succeeded for console")
	}
	if !log.refusedPublish("console", kvInfo) {
		t.Errorf("no Publish Violation logged for console on %s (js.Stream)", kvInfo)
	} else {
		t.Logf("%-48s refused (server: Publish Violation)", kvInfo+" (js.Stream)")
	}
	before := len(log.publishViolations("console"))
	if _, err := nc.Request(kvInfo, []byte(`{"subjects_filter":">"}`), 500*time.Millisecond); err == nil {
		t.Errorf("raw %s with subjects_filter answered console", kvInfo)
	}
	freshRefusal := func() bool {
		deadline := time.Now().Add(2 * time.Second)
		for {
			after := log.publishViolations("console")
			for _, line := range after[min(before, len(after)):] {
				if strings.Contains(line, fmt.Sprintf("Subject %q", kvInfo)) {
					return true
				}
			}
			if time.Now().After(deadline) {
				return false
			}
			time.Sleep(20 * time.Millisecond)
		}
	}
	if !freshRefusal() {
		t.Errorf("no fresh Publish Violation logged for console on the raw %s subjects_filter request", kvInfo)
	} else {
		t.Logf("%-48s refused (server: Publish Violation)", kvInfo+" (raw, subjects_filter)")
	}

	// Refused publishes, each read back from the server log.
	for _, subject := range []string{
		"a2a.tasks.platform.task-x.in",
		"a2a.tasks.platform.task-x.events",
		"chat.console.tab-1.out",
		"$KV.session-state.sessions.x",
		"$JS.API.CONSUMER.CREATE.KV_session-state.c1",
		"$JS.API.DIRECT.GET.KV_session-state",
		"$JS.API.STREAM.DELETE.TASKS",
	} {
		if err := nc.Publish(subject, []byte("x")); err != nil {
			t.Fatal(err)
		}
		_ = nc.Flush()
		if !log.refusedPublish("console", subject) {
			t.Errorf("no Publish Violation logged for console on %s", subject)
		} else {
			t.Logf("%-48s refused (server: Publish Violation)", subject)
		}
	}

	// A subscribe outside the list is refused too. The server logs
	// "Subscription Violation" for it; a2aServerLog keeps every line.
	_, _ = nc.SubscribeSync("chat.console.tab-1.in")
	_ = nc.Flush()
	if !log.refusedSubscribe("console", "chat.console.tab-1.in") {
		t.Error("console could subscribe its own inbound subject; another tab's frames would be readable")
	} else {
		t.Logf("%-48s refused (server: Subscription Violation)", "chat.console.tab-1.in (subscribe)")
	}
}

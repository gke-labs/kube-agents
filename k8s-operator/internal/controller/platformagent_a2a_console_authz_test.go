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
	"testing"
	"time"
)

// TestConsoleGrantOnARealServer is the refusal proof for the console user,
// measured the way the gateway's and the bridge's are: the config the
// operator renders, run by an embedded nats-server, and a client connected
// as console.
//
// Allowed: publish on its own inbound subject; subscribe on its own outbound
// subject and on a2a.>; STREAM.INFO on KV_session-state (sizes). Refused,
// read from the server log rather than the client's timeout: publish on the
// task plane, on the outbound console subject, on the KV data plane, and
// CONSUMER.CREATE / DIRECT.GET on KV_session-state (the routes that would
// turn the size grant into a read of the bucket).
func TestConsoleGrantOnARealServer(t *testing.T) {
	creds := a2aFullCreds("c", "1")
	conf := string(buildA2ANATSConfigSecret(a2aTestAgent(), creds, a2aTestCalloutKeys(t)).Data["nats.conf"])
	consolePW := string(creds.Data["console-password"])
	seedPW := string(creds.Data["seed-password"])

	s, log := a2aStartRenderedServer(t, conf)
	a2aProvisionLikeTheScript(t, s.ClientURL(), seedPW)
	nc, js := a2aConnectAs(t, s.ClientURL(), "console", consolePW)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	// Allowed.
	if err := nc.Publish("chat.console.tab-1.in", []byte(`{"messageId":"m1","text":"hi"}`)); err != nil {
		t.Fatal(err)
	}
	if _, err := nc.SubscribeSync("chat.console.tab-1.out"); err != nil {
		t.Fatal(err)
	}
	if _, err := nc.SubscribeSync("a2a.>"); err != nil {
		t.Fatal(err)
	}
	if _, err := js.Stream(ctx, "KV_session-state"); err != nil {
		t.Errorf("STREAM.INFO.KV_session-state refused: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond) // the log line is written on the server's read loop
	if v := log.publishViolations("console"); len(v) != 0 {
		t.Fatalf("allowed operations logged violations: %q", v)
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

package main

import (
	"context"
	"io"
	"os"
	"os/exec"
	"strings"
	"testing"

	"github.com/go-steer/core-agent/v3/pkg/childenv"
)

// TestAToolChildGetsNoCredentialTheHarnessCarries: once the harness has
// withheld what the worker's allowlist doesn't name, a child started with
// core-agent's child environment (as the shell tool and MCP servers are)
// sees no model, provider, bus or chat credential, and still gets what a
// command needs and the broker-routed tools' variables. The harness itself
// keeps them.
func TestAToolChildGetsNoCredentialTheHarnessCarries(t *testing.T) {
	secrets := map[string]string{
		"GEMINI_API_KEY": "gem-secret", "GOOGLE_GEMINI_BASE_URL": "http://gw-secret",
		"NATS_PASSWORD": "bus-secret", "SLACK_BOT_TOKEN": "chat-secret", "SOME_FUTURE_SECRET": "future-secret",
	}
	for k, v := range secrets {
		t.Setenv(k, v)
	}
	t.Setenv("CREDENTIAL_PROXY_URL", "http://broker")
	// Through run(), which withholds before it reads its flags: with no
	// --model it stops there, having done so.
	if code := run(context.Background(), nil, strings.NewReader(""), io.Discard, io.Discard); code != 2 {
		t.Fatalf("run with no flags = %d, want 2", code)
	}

	cmd := exec.Command("/usr/bin/env")
	cmd.Env = childenv.Environ()
	out, err := cmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range secrets {
		if strings.Contains(string(out), v) {
			t.Errorf("a tool child sees %s", k)
		}
		if os.Getenv(k) != v {
			t.Errorf("the harness lost %s", k)
		}
	}
	for _, want := range []string{"PATH=", "CREDENTIAL_PROXY_URL=http://broker"} {
		if !strings.Contains(string(out), want) {
			t.Errorf("a tool child lacks %s", want)
		}
	}
}

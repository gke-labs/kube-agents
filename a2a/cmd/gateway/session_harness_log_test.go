package main

import (
	"bytes"
	"log/slog"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/gateway"
)

// TestAnUnknownSessionHarnessIsLoggedAtErrorLevel: the gateway's start says,
// at error level, which A2A_SESSION_HARNESS value it fell back from, and says
// nothing when there was none.
func TestAnUnknownSessionHarnessIsLoggedAtErrorLevel(t *testing.T) {
	var buf bytes.Buffer
	log := slog.New(slog.NewJSONHandler(&buf, nil))

	logSessionHarnessRefused(log, &gateway.Config{})
	if buf.Len() != 0 {
		t.Fatalf("no refused value logged %q", buf.String())
	}

	logSessionHarnessRefused(log, &gateway.Config{SessionHarnessRefused: "made-up-harness"})
	line := buf.String()
	for _, want := range []string{`"level":"ERROR"`, `"harness":"made-up-harness"`, "default harness"} {
		if !strings.Contains(line, want) {
			t.Errorf("log %q lacks %s", line, want)
		}
	}
}

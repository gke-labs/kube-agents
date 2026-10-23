package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const example = "../../lib/persona/testdata/example"

func TestRenderWritesOnlyUnderOut(t *testing.T) {
	root := t.TempDir()
	out := filepath.Join(root, "out")
	var stdout, stderr bytes.Buffer
	code := run([]string{"render", "--harness", "claude-code", "--in", example, "--out", out,
		"--relay-url", "http://broker:8765/mcp", "--relay-command", "node proxy.js {url}"}, &stdout, &stderr)
	if code != 0 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	entries, _ := os.ReadDir(root)
	if len(entries) != 1 || entries[0].Name() != "out" {
		t.Errorf("render wrote beside --out: %v", entries)
	}
	if _, err := os.Stat(filepath.Join(out, "CLAUDE.md")); err != nil {
		t.Error(err)
	}
	var res result
	if err := json.Unmarshal(stdout.Bytes(), &res); err != nil {
		t.Fatalf("stdout is not the result JSON: %v\n%s", err, stdout.String())
	}
	if !strings.Contains(strings.Join(res.Args, " "), "http://broker:8765/mcp/gke") {
		t.Errorf("the relay flags did not reach the MCP config: %v", res.Args)
	}
	if !strings.Contains(stderr.String(), "warning:") {
		t.Errorf("warnings were not printed: %q", stderr.String())
	}
}

// Either relay flag alone is a usage error, not a render that drops every
// broker server and exits 0.
func TestRenderWantsBothRelayFlags(t *testing.T) {
	for _, flags := range [][]string{
		{"--relay-url", "http://broker:8765/mcp"},
		{"--relay-command", "node proxy.js {url}"},
	} {
		var stdout, stderr bytes.Buffer
		args := append([]string{"render", "--harness", "claude-code", "--in", example, "--out", t.TempDir()}, flags...)
		if code := run(args, &stdout, &stderr); code != 2 {
			t.Errorf("%v: exit %d, want 2: %s", flags, code, stderr.String())
		}
	}
}

func TestRenderRefusesANonEmptyOut(t *testing.T) {
	out := t.TempDir()
	_ = os.WriteFile(filepath.Join(out, "stale.md"), []byte("x"), 0o644)
	var stdout, stderr bytes.Buffer
	if code := run([]string{"render", "--harness", "claude-code", "--in", example, "--out", out}, &stdout, &stderr); code != 1 {
		t.Errorf("exit %d, want 1: %s", code, stderr.String())
	}
}

func TestValidate(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run([]string{"validate", "--in", example}, &stdout, &stderr); code != 0 {
		t.Errorf("the example failed validation: %s", stderr.String())
	}
	stderr.Reset()
	if code := run([]string{"validate", "--in", t.TempDir()}, &stdout, &stderr); code != 1 || !strings.Contains(stderr.String(), "persona.yaml: is missing") {
		t.Errorf("an empty dir: exit %d, %s", code, stderr.String())
	}
	if code := run([]string{"render", "--harness", "antigravity", "--in", example, "--out", t.TempDir()}, &stdout, &stderr); code != 2 {
		t.Errorf("antigravity render: exit %d, want 2", code)
	}
}

package main

import (
	"bufio"
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// TestAgysArgvIsItsHeadlessStreamJSONMode: agy's print switch takes a value
// (`-p=`) and the message comes on stdin as stream-json.
// A2A_HARNESS_EXTRA_ARGS still appends. --print-timeout is not the
// builder's: the driver sets it for each process from the time left
// (agyDriver.WithTurnBudget).
func TestAgysArgvIsItsHeadlessStreamJSONMode(t *testing.T) {
	got := agyBuilder{}.command(harnessSpec{
		path:     "/opt/agy/bin/agy",
		model:    "ignored",
		maxTurns: "40",
		extra:    []string{"--debug"},
	})
	want := []string{
		"/opt/agy/bin/agy", "-p=",
		"--input-format", "stream-json",
		"--output-format", "stream-json",
		"--debug",
	}
	if !slices.Equal(got, want) {
		t.Errorf("argv\n got %q\nwant %q", got, want)
	}
}

// TestAgysArgvCarriesNoneOfClaudeCodesPolicy: the tool lists, the delegate
// server and the prompts are Claude Code flags until #6711 and #6712 give
// agy its own; the builder leaves them out rather than passing flags agy
// would refuse or misread. The model and the turn count have no agy flag.
func TestAgysArgvCarriesNoneOfClaudeCodesPolicy(t *testing.T) {
	got := agyBuilder{}.command(harnessSpec{
		path:       "agy",
		model:      "claude-model",
		maxTurns:   "40",
		tools:      true,
		allowed:    "Read,Bash",
		disallowed: "Write",
		delegate:   &delegateServer{command: "/self", socket: "/run/s.sock"},
		prompts:    []string{"a prompt"},
	})
	for _, a := range got {
		for _, bad := range []string{"--allowedTools", "--disallowedTools", "--mcp-config", "--append-system-prompt",
			"--model", "--max-turns", "--setting-sources", "--strict-mcp-config", "--print", "--verbose",
			"Read,Bash", "claude-model", "a prompt"} {
			if a == bad {
				t.Errorf("agy's argv %q carries Claude Code's %q", got, bad)
			}
		}
	}
}

// TestAgysEnvIsThePodsOwn: the builder adds nothing to the pod environment,
// and agy has no user settings file or default path here.
func TestAgysEnvIsThePodsOwn(t *testing.T) {
	base := []string{"HOME=/h", "AGY_LLM_GATEWAY_URL=http://127.0.0.1:1"}
	got := agyBuilder{}.env(slices.Clone(base), func(string) bool { return false })
	if !slices.Equal(got, base) {
		t.Errorf("env %q, want the pod's own %q", got, base)
	}
	if s := (agyBuilder{}).userSettings(func(string) string { return "/h" }); s != "" {
		t.Errorf("userSettings %q, want none", s)
	}
	if p := (agyBuilder{}).defaultPath(); p != "" {
		t.Errorf("defaultPath %q, want none (the image is #6715's)", p)
	}
}

// TestAnAgyTurnRunsOnTheBuiltArgv: the argv the builder writes, with the
// driver's resume flag, runs a stub agy that refuses any other argv; the
// driver's stdin line reaches it and the driver's decoder reads its output
// to a completed result. The argv, the stdin and the decoder agree.
func TestAnAgyTurnRunsOnTheBuiltArgv(t *testing.T) {
	fixture, err := filepath.Abs("../../worker-adapter/testdata/agy/basic.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(fixture); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	stub := filepath.Join(dir, "agy")
	script := `#!/bin/sh
want='-p= --input-format stream-json --output-format stream-json --print-timeout 28m50s --conversation conv-1'
[ "$*" = "$want" ] || { echo "argv: $*" >&2; exit 9; }
read -r first || exit 8
case "$first" in '{"event":"user"'*) ;; *) echo "stdin: $first" >&2; exit 7 ;; esac
cat "` + fixture + `"
`
	if err := os.WriteFile(stub, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	var driver workeradapter.Driver = mustDriver(t, persona.Antigravity)
	budgeted, ok := driver.(interface {
		WithTurnBudget([]string, time.Duration) []string
	})
	if !ok {
		t.Fatal("the agy driver sets no turn budget")
	}
	argv := driver.WithResume(budgeted.WithTurnBudget(agyBuilder{}.command(harnessSpec{path: stub}), 1790*time.Second), "conv-1")
	in, err := driver.EncodeUser("say pong")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Stdin = bytes.NewReader(append(in, '\n'))
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("stub agy: %v: %s", err, stderr.String())
	}
	dec := driver.NewDecoder()
	var result *workeradapter.Event
	sc := bufio.NewScanner(bytes.NewReader(out))
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		evs, err := dec.Decode(sc.Bytes())
		if err != nil {
			t.Fatalf("decode %q: %v", sc.Text(), err)
		}
		for i := range evs {
			if evs[i].Kind == workeradapter.EventResult {
				result = &evs[i]
			}
		}
	}
	if result == nil {
		t.Fatalf("no result in %q", out)
	}
	if result.Failed || strings.TrimSpace(result.Text) != "pong" {
		t.Errorf("result failed=%v token=%q text=%q, want a completed pong", result.Failed, result.Token, result.Text)
	}
}

func mustDriver(t *testing.T, h persona.Harness) workeradapter.Driver {
	t.Helper()
	d, err := workeradapter.DriverFor(h)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

// TestAgysArgvLeavesItsTimeoutToTheDriver: neither argv call site, a
// session pod's (harnessCommand) nor a profile worker's base argv
// (profileHarnessCommand), carries --print-timeout. The adapter has the
// driver set it on each process from the time left on that task's deadline
// less the kill grace (agyDriver.WithTurnBudget); a bound fixed here at
// startup would give a later turn's process the whole task's time, and a
// profile task with a shorter offered deadline a bound past it.
func TestAgysArgvLeavesItsTimeoutToTheDriver(t *testing.T) {
	was := activeHarness
	t.Cleanup(func() { activeHarness = was })
	activeHarness = agyBuilder{}
	t.Setenv("A2A_HARNESS_CMD", "")
	t.Setenv("A2A_HARNESS_PATH", "/opt/agy/bin/agy")
	t.Setenv("A2A_HARNESS_EXTRA_ARGS", "")
	for name, argv := range map[string][]string{
		"harnessCommand":        harnessCommand(),
		"profileHarnessCommand": profileHarnessCommand(),
	} {
		for _, a := range argv {
			if strings.HasPrefix(a, "--print-timeout") {
				t.Errorf("%s argv %q carries %s; the driver sets it per process", name, argv, a)
			}
		}
	}
}

package mcpbridge

import (
	"io"
	"os"
	"strings"
	"testing"
)

// runVersionFlag runs RunCLIWithOptions with args and returns what it wrote to stdout.
func runVersionFlag(t *testing.T, args []string, opts ServerOptions) string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer func(old *os.File) { os.Stdout = old }(os.Stdout)
	os.Stdout = w
	runErr := RunCLIWithOptions(args, opts)
	w.Close()
	out, readErr := io.ReadAll(r)
	if readErr != nil {
		t.Fatal(readErr)
	}
	if runErr != nil {
		t.Fatalf("RunCLIWithOptions(%q) = %v", args, runErr)
	}
	return string(out)
}

func TestVersionFlagPrintsTheBinaryVersion(t *testing.T) {
	for _, args := range [][]string{{"--version"}, {"version"}} {
		got := runVersionFlag(t, args, ServerOptions{VersionText: "kube-agents-mcp 0.34.0"})
		if want := "kube-agents-mcp 0.34.0\n"; got != want {
			t.Errorf("%q printed %q, want %q", args, got, want)
		}
	}
}

func TestVersionFlagWithoutVersionTextNamesTheServer(t *testing.T) {
	got := runVersionFlag(t, []string{"--version"}, ServerOptions{})
	if want := mcpServerName + " " + mcpServerVersion; strings.TrimSpace(got) != want {
		t.Errorf("--version printed %q, want %q", got, want)
	}
}

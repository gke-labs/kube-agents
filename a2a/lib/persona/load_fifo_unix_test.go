//go:build unix

package persona

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// A named pipe is refused, not opened and waited on: an open for reading
// blocks until a writer appears, so a blocking open would hang Load.
func TestLoadRefusesANamedPipe(t *testing.T) {
	dir := copyExample(t)
	_ = os.Remove(filepath.Join(dir, MCPFile))
	if err := syscall.Mkfifo(filepath.Join(dir, MCPFile), 0o644); err != nil {
		t.Skip(err)
	}
	done := make(chan error, 1)
	go func() { _, err := Load(dir); done <- err }()
	select {
	case err := <-done:
		var invalid *InvalidError
		if !errors.As(err, &invalid) || !strings.Contains(err.Error(), "mcp.yaml: is not a regular file") {
			t.Fatalf("want the pipe refused, got %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Load blocked on a named pipe")
	}
}

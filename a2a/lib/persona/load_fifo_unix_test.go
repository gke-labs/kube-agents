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

// A named pipe is refused by the integrity walk itself: README.md is never
// read by the format reader, which checks it only for a symlink, so a pipe
// there passes without a manifest and is caught only because the persona
// carries one. The walk doesn't block on it either.
func TestIntegrityRefusesANamedPipe(t *testing.T) {
	dir := copyExample(t)
	writeIntegrityManifest(t, dir)
	_ = os.Remove(filepath.Join(dir, ReadmeFile))
	if err := syscall.Mkfifo(filepath.Join(dir, ReadmeFile), 0o644); err != nil {
		t.Skip(err)
	}
	manifest := filepath.Join(dir, IntegrityManifestFile)
	aside := filepath.Join(t.TempDir(), "aside")
	if err := os.Rename(manifest, aside); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(dir); err != nil {
		t.Fatalf("without a manifest the pipe passes the format reader, got %v", err)
	}
	if err := os.Rename(aside, manifest); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { _, err := Load(dir); done <- err }()
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), ReadmeFile+": is not a regular file") {
			t.Fatalf("want the pipe refused, got %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Load blocked on a named pipe")
	}
}

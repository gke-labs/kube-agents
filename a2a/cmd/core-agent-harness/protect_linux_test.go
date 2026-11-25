//go:build linux

package main

import (
	"fmt"
	"os"
	"os/exec"
	"testing"

	"github.com/go-steer/core-agent/v3/pkg/childenv"
)

// Once protected, a same-UID child can't open this process's /proc fds, so
// a command a turn runs can't write into the harness's stdout.
func TestAProtectedHarnessKeepsItsFdsFromChildren(t *testing.T) {
	if os.Getenv("HARNESS_PROTECT_CHILD") != "" {
		f, err := os.OpenFile(os.Getenv("HARNESS_PROTECT_CHILD"), os.O_WRONLY, 0)
		if err == nil {
			f.Close()
			fmt.Println("OPENED")
		}
		return
	}
	if err := childenv.Protect(); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(os.Args[0], "-test.run=^TestAProtectedHarnessKeepsItsFdsFromChildren$")
	cmd.Env = append(os.Environ(), fmt.Sprintf("HARNESS_PROTECT_CHILD=/proc/%d/fd/1", os.Getpid()))
	out, _ := cmd.CombinedOutput()
	if string(out) != "" && contains(string(out), "OPENED") {
		t.Errorf("a same-UID child opened the protected process's stdout: %s", out)
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

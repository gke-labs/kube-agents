//go:build linux

package workeradapter

import (
	"os"
	"os/exec"
	"slices"
	"strconv"
	"syscall"
	"testing"
	"time"
)

// TestListOtherProcessesSeesALiveChildNotAZombie: a running child is listed,
// this process is not, and the same child once exited but unreaped (a
// zombie) is not either.
func TestListOtherProcessesSeesALiveChildNotAZombie(t *testing.T) {
	cmd := exec.Command("sleep", "30")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	pid := cmd.Process.Pid
	t.Cleanup(func() { _ = cmd.Process.Kill(); _ = cmd.Wait() })

	got, err := listOtherProcesses()
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(got, pid) {
		t.Fatalf("live child %d not listed in %v", pid, got)
	}
	if slices.Contains(got, os.Getpid()) {
		t.Fatal("listed this process")
	}

	_ = syscall.Kill(pid, syscall.SIGKILL)
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		b, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
		if err == nil {
			if s, _ := procState(string(b)); s == 'Z' {
				break
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	got, err = listOtherProcesses()
	if err != nil {
		t.Fatal(err)
	}
	if slices.Contains(got, pid) {
		t.Fatalf("zombie %d still listed", pid)
	}
}

func TestProcStateReadsPastAParenthesisedName(t *testing.T) {
	for stat, want := range map[string]byte{
		"12 (sleep) S 1 12":         'S',
		"13 (a) b) (c) Z 1 13":      'Z',
		"14 (with space) R 1 14 15": 'R',
	} {
		if got, err := procState(stat); err != nil || got != want {
			t.Errorf("procState(%q) = %q, %v; want %q", stat, got, err, want)
		}
	}
	for _, bad := range []string{"", "12 sleep S", "12 (sleep)"} {
		if _, err := procState(bad); err == nil {
			t.Errorf("procState(%q) parsed", bad)
		}
	}
}

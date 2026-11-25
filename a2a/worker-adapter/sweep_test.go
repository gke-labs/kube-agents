package workeradapter

import (
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// sweepScope stands the adapter in as the container's init, with the
// namespace-wide kill and the process listing scoped to the pids it tracks,
// so a test never signals anything it did not start.
type sweepScope struct {
	mu      sync.Mutex
	pids    []int
	killing bool // false: the stand-in kill does nothing, as if it missed
}

func (s *sweepScope) alive() []int {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []int
	for _, pid := range s.pids {
		if syscall.Kill(pid, 0) == nil && !zombieProc(pid) {
			out = append(out, pid)
		}
	}
	return out
}

func zombieProc(pid int) bool {
	b, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return false
	}
	st, err := procState(string(b))
	return err == nil && st == 'Z'
}

func useSweepScope(t *testing.T, asInit bool) *sweepScope {
	t.Helper()
	s := &sweepScope{killing: true}
	prevPID, prevKill, prevReap, prevOthers, prevSettle := adapterPID, killEveryoneButMe, reapKilledChildren, otherProcesses, sweepSettle
	t.Cleanup(func() {
		adapterPID, killEveryoneButMe, reapKilledChildren, otherProcesses, sweepSettle = prevPID, prevKill, prevReap, prevOthers, prevSettle
		for _, pid := range s.alive() {
			_ = syscall.Kill(pid, syscall.SIGKILL)
		}
	})
	if asInit {
		adapterPID = func() int { return containerInitPID }
	}
	killEveryoneButMe = func() {
		if s.killing {
			for _, pid := range s.alive() {
				_ = syscall.Kill(pid, syscall.SIGKILL)
			}
		}
	}
	reapKilledChildren = func(bound time.Duration) {
		for deadline := time.Now().Add(bound); len(s.alive()) > 0 && time.Now().Before(deadline); {
			time.Sleep(10 * time.Millisecond)
		}
	}
	otherProcesses = func() ([]int, error) { return s.alive(), nil }
	sweepSettle = 300 * time.Millisecond
	return s
}

// leaveDetached starts a process the way a turn's shell tool can leave
// one: in its own session, so a process-group kill misses it.
func leaveDetached(t *testing.T) int {
	t.Helper()
	if _, err := os.Stat("/usr/bin/perl"); err != nil {
		t.Skip("needs perl for setsid")
	}
	pidFile := t.TempDir() + "/pid"
	cmd := exec.Command("/usr/bin/perl", "-MPOSIX", "-e",
		`if (fork) { exit 0 } POSIX::setsid(); open(STDIN, "<", "/dev/null"); open(STDOUT, ">", "/dev/null"); open(STDERR, ">", "/dev/null"); open(my $f, ">", $ARGV[0]) or die; print $f "$$\n"; close($f); sleep 60`, pidFile)
	if err := cmd.Run(); err != nil {
		t.Fatal(err)
	}
	var pid int
	for deadline := time.Now().Add(5 * time.Second); pid == 0 && time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		b, _ := os.ReadFile(pidFile)
		pid, _ = strconv.Atoi(strings.TrimSpace(string(b)))
	}
	if pid == 0 {
		t.Fatal("the detached process never wrote its pid")
	}
	return pid
}

func TestSweepAfterTurnEndsADetachedLeftover(t *testing.T) {
	s := useSweepScope(t, true)
	pid := leaveDetached(t)
	s.pids = append(s.pids, pid)
	if len(s.alive()) != 1 {
		t.Fatal("the detached leftover is not running")
	}
	if err := sweepAfterTurn(); err != nil {
		t.Fatalf("sweep: %v", err)
	}
	if alive := s.alive(); len(alive) > 0 {
		t.Fatalf("the leftover %v survived the sweep", alive)
	}
}

func TestSweepAfterTurnFailsClosed(t *testing.T) {
	t.Run("not the container's init", func(t *testing.T) {
		useSweepScope(t, false)
		adapterPID = func() int { return 4242 }
		if err := sweepAfterTurn(); err == nil || !strings.Contains(err.Error(), "init") {
			t.Fatalf("err = %v; want a refusal naming init", err)
		}
	})
	t.Run("a process the kill did not reach", func(t *testing.T) {
		s := useSweepScope(t, true)
		s.killing = false
		pid := leaveDetached(t)
		s.pids = append(s.pids, pid)
		err := sweepAfterTurn()
		if err == nil || !strings.Contains(err.Error(), strconv.Itoa(pid)) {
			t.Fatalf("err = %v; want one naming %d", err, pid)
		}
	})
	t.Run("the process list cannot be read", func(t *testing.T) {
		useSweepScope(t, true)
		otherProcesses = func() ([]int, error) { return nil, os.ErrPermission }
		if err := sweepAfterTurn(); err == nil {
			t.Fatal("an unreadable process list passed")
		}
	})
}

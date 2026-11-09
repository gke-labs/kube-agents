/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/api/resource"
)

// lockedBuffer is a bytes.Buffer the test can read while the shell writes it.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// writeScript writes an executable shell script into dir.
func writeScript(t *testing.T, dir, name, body string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"+body+"\n"), 0o755); err != nil { // #nosec G306 -- the test's own executable stand-in
		t.Fatal(err)
	}
	return path
}

// startSupervisor runs a2aNATSExporterSupervisor as the container does, with
// a stand-in for the exporter, in its own process group so the test can see
// whether anything it started outlives it. stubs, if set, is a directory put
// first on PATH (stand-ins for sleep and date). It returns the running shell
// and its combined output.
func startSupervisor(t *testing.T, exporter, stubs string, args ...string) (*exec.Cmd, *lockedBuffer) {
	t.Helper()
	sh, err := exec.LookPath("sh")
	if err != nil {
		t.Skip("no sh on this host")
	}
	bin := writeScript(t, t.TempDir(), "exporter", exporter)
	out := &lockedBuffer{}
	cmd := exec.Command(sh, append([]string{"-c", a2aNATSExporterSupervisor, bin}, args...)...) // #nosec G204 -- the operator's own constant
	cmd.Stdout, cmd.Stderr = out, out
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if stubs != "" {
		cmd.Env = append(os.Environ(), "PATH="+stubs+string(os.PathListSeparator)+os.Getenv("PATH"))
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); _, _ = cmd.Process.Wait() })
	return cmd, out
}

// groupGone reports whether no process is left in the supervisor's group.
func groupGone(cmd *exec.Cmd) bool {
	return syscall.Kill(-cmd.Process.Pid, 0) == syscall.ESRCH
}

func waitForSupervisor(t *testing.T, what string, within time.Duration, ok func() bool) {
	t.Helper()
	for deadline := time.Now().Add(within); time.Now().Before(deadline); time.Sleep(20 * time.Millisecond) {
		if ok() {
			return
		}
	}
	t.Fatalf("%s: not within %s", what, within)
}

// An exporter that exits is started again, with its args, after a logged
// pause that grows: the container, and so the pod's readiness, outlives it.
func TestTheExporterSupervisorRestartsTheExporterWithBackoff(t *testing.T) {
	marker := filepath.Join(t.TempDir(), "runs")
	cmd, out := startSupervisor(t, `echo "$@" >> `+marker+`; exit 3`, "", "-port=7777", "-jsz=all")
	waitForSupervisor(t, "two runs and two logged exits", 5*time.Second, func() bool {
		return strings.Count(out.String(), "exited with code 3") >= 2
	})
	if err := syscall.Kill(cmd.Process.Pid, 0); err != nil {
		t.Fatalf("the supervisor exited with its exporter (%v): %s", err, out.String())
	}
	runs, _ := os.ReadFile(marker) // #nosec G304 -- the test's temp file
	if got := strings.Split(strings.TrimSpace(string(runs)), "\n"); len(got) < 2 || got[0] != "-port=7777 -jsz=all" {
		t.Errorf("exporter runs = %q, want each run with the container's args", got)
	}
	if !strings.Contains(out.String(), "restarting in 1s") || !strings.Contains(out.String(), "restarting in 2s") {
		t.Errorf("pauses = %q, want 1s then 2s", out.String())
	}
}

// The shell is PID 1 in the container, so the trap is its only signal
// handling: TERM or INT ends it at once, exporter and all, whether the
// exporter is running or the loop is pausing between runs.
func TestTheExporterSupervisorExitsAtOnceOnASignal(t *testing.T) {
	for _, tc := range []struct {
		name     string
		exporter string
		ready    string
		signal   syscall.Signal
	}{
		{"TERM while the exporter runs", `echo started; exec sleep 60`, "started", syscall.SIGTERM},
		{"INT while the exporter runs", `echo started; exec sleep 60`, "started", syscall.SIGINT},
		{"TERM during the pause", `exit 1`, "restarting in", syscall.SIGTERM},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cmd, out := startSupervisor(t, tc.exporter, "")
			waitForSupervisor(t, "the exporter to start", 5*time.Second, func() bool { return strings.Contains(out.String(), tc.ready) })
			// The marker is printed just before the child forks; give it
			// time to exist, well inside the 1s first pause.
			time.Sleep(200 * time.Millisecond)
			sent := time.Now()
			if err := cmd.Process.Signal(tc.signal); err != nil {
				t.Fatal(err)
			}
			done := make(chan error, 1)
			go func() { done <- cmd.Wait() }()
			select {
			case err := <-done:
				if err != nil {
					t.Errorf("supervisor exit = %v, want 0", err)
				}
				if waited := time.Since(sent); waited > 2*time.Second {
					t.Errorf("exited %s after the signal, want at once", waited)
				}
				// In the container the kernel kills what PID 1 leaves
				// behind; here, nothing the trap should have killed may
				// be left in the group.
				waitForSupervisor(t, "the exporter or the pause to be killed with the supervisor", time.Second, func() bool { return groupGone(cmd) })
			case <-time.After(5 * time.Second):
				t.Fatalf("still running 5s after %s: %s", tc.signal, out.String())
			}
		})
	}
}

// The pause stops doubling at 30s, and a run that lasted a minute starts it
// again at 1s. Stand-ins for sleep (returns at once) and date (a clock the
// test moves) make both quick to see.
func TestTheExporterSupervisorCapsAndResetsItsPause(t *testing.T) {
	for _, tc := range []struct {
		name      string
		clockStep int // seconds date advances per call
		want      string
		wantNot   []string
	}{
		{"short runs double to the 30s cap", 0, "restarting in 30s", []string{"restarting in 32s", "restarting in 60s"}},
		{"minute-long runs restart after 1s", 61, "restarting in 1s", []string{"restarting in 2s"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			stubs := t.TempDir()
			clock := filepath.Join(stubs, "clock")
			if err := os.WriteFile(clock, []byte("1000\n"), 0o600); err != nil {
				t.Fatal(err)
			}
			writeScript(t, stubs, "sleep", "exit 0")
			writeScript(t, stubs, "date", fmt.Sprintf(`now=$(cat %s); echo $((now + %d)) > %s; echo "$now"`, clock, tc.clockStep, clock))
			_, out := startSupervisor(t, "exit 1", stubs)
			waitForSupervisor(t, "eight restarts", 5*time.Second, func() bool { return strings.Count(out.String(), "exited with code 1") >= 8 })
			log := out.String()
			if !strings.Contains(log, tc.want) {
				t.Errorf("log lacks %q:\n%s", tc.want, log)
			}
			for _, bad := range tc.wantNot {
				if strings.Contains(log, bad) {
					t.Errorf("log has %q:\n%s", bad, log)
				}
			}
		})
	}
}

// GOMEMLIMIT has to sit below the container's limit to do anything: at or
// above it, the kernel kills the container before the runtime collects.
func TestTheExporterGoMemLimitSitsBelowTheContainerLimit(t *testing.T) {
	soft := strings.TrimSuffix(a2aNATSExporterGoMemLimit, "B") // 160MiB -> 160Mi, the quantity spelling
	got, err := resource.ParseQuantity(soft)
	if err != nil {
		t.Fatalf("GOMEMLIMIT %q does not parse: %v", a2aNATSExporterGoMemLimit, err)
	}
	limit := resource.MustParse(a2aNATSExporterMemoryLimit)
	if got.Cmp(limit) >= 0 || got.Value() < limit.Value()/2 {
		t.Errorf("GOMEMLIMIT %s against a %s limit, want below it and above half of it", a2aNATSExporterGoMemLimit, a2aNATSExporterMemoryLimit)
	}
}

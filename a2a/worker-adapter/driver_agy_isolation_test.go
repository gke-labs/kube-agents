package workeradapter

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/worker-adapter/isolation"
)

// agyTestEnv is an agy run's surroundings in a test: the adapter's working
// directory (the harness's workspace) and the harness's HOME, both fresh,
// so PrepareTurn's scrub never reaches the source tree or a real HOME.
type agyTestEnv struct {
	ws, home string
	// pids the stand-in for the namespace-wide kill ends, and the stand-in
	// for the process listing reports while they live.
	mu   sync.Mutex
	pids []int
}

func (e *agyTestEnv) track(pid int) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.pids = append(e.pids, pid)
}

func (e *agyTestEnv) alive() []int {
	e.mu.Lock()
	defer e.mu.Unlock()
	var out []int
	for _, pid := range e.pids {
		if syscall.Kill(pid, 0) == nil && !zombie(pid) {
			out = append(out, pid)
		}
	}
	return out
}

// zombie reports a pid that has exited but is not reaped; off Linux it
// cannot tell, and says no.
func zombie(pid int) bool {
	b, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return false
	}
	s, err := procState(string(b))
	return err == nil && s == 'Z'
}

// useAgyTestDriver points cfg at an agy driver with the 1.5.13 table, and
// stands the adapter in as the container's init with the namespace-wide kill
// and the process listing scoped to the pids env tracks, so a test never
// signals anything it did not start.
func useAgyTestDriver(t *testing.T, cfg *Config) *agyTestEnv {
	t.Helper()
	ws, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	env := &agyTestEnv{ws: ws, home: t.TempDir()}
	t.Chdir(ws)
	cfg.HarnessEnv = append(cfg.HarnessEnv, "HOME="+env.home)

	iso := &agyIsolation{}
	iso.once.Do(func() { iso.table, iso.err = isolation.ForVersion("1.5.13") })
	cfg.Driver = agyDriver{iso: iso}

	prevPID, prevKill, prevReap, prevOthers := adapterPID, killEveryoneButMe, reapKilledChildren, otherProcesses
	t.Cleanup(func() {
		adapterPID, killEveryoneButMe, reapKilledChildren, otherProcesses = prevPID, prevKill, prevReap, prevOthers
		for _, pid := range env.alive() {
			_ = syscall.Kill(pid, syscall.SIGKILL)
		}
	})
	adapterPID = func() int { return containerInitPID }
	killEveryoneButMe = func() {
		for _, pid := range env.alive() {
			_ = syscall.Kill(pid, syscall.SIGKILL)
		}
	}
	reapKilledChildren = func(bound time.Duration) {
		deadline := time.Now().Add(bound)
		for len(env.alive()) > 0 && time.Now().Before(deadline) {
			time.Sleep(10 * time.Millisecond)
		}
	}
	otherProcesses = func() ([]int, error) { return env.alive(), nil }
	return env
}

func TestAgyPrepareTurnRefusesWithoutABoundary(t *testing.T) {
	ws, home := t.TempDir(), t.TempDir()
	iso := &agyIsolation{}
	iso.once.Do(func() { iso.table, iso.err = isolation.ForVersion("1.5.13") })
	d := agyDriver{iso: iso}
	prevPID, prevOthers := adapterPID, otherProcesses
	t.Cleanup(func() { adapterPID, otherProcesses = prevPID, prevOthers })

	if err := (agyDriver{}).PrepareTurn(context.Background(), TurnDirs{Workspace: ws, Home: home}); err == nil {
		t.Error("a driver with no binary prepared a turn")
	}
	adapterPID = func() int { return 4242 }
	otherProcesses = func() ([]int, error) { return nil, nil }
	if err := d.PrepareTurn(context.Background(), TurnDirs{Workspace: ws, Home: home}); err == nil || !strings.Contains(err.Error(), "init") {
		t.Errorf("off pid 1: err = %v; want a refusal naming init", err)
	}
	adapterPID = func() int { return containerInitPID }
	otherProcesses = func() ([]int, error) { return []int{77}, nil }
	if err := d.PrepareTurn(context.Background(), TurnDirs{Workspace: ws, Home: home}); err == nil || !strings.Contains(err.Error(), "77") {
		t.Errorf("a process left alive: err = %v; want a refusal naming it", err)
	}
	otherProcesses = func() ([]int, error) { return nil, errors.New("no /proc") }
	if err := d.PrepareTurn(context.Background(), TurnDirs{Workspace: ws, Home: home}); err == nil {
		t.Error("an unreadable process list prepared a turn")
	}
	bad := &agyIsolation{}
	bad.once.Do(func() { bad.table, bad.err = isolation.ForVersion("9.9.9") })
	otherProcesses = func() ([]int, error) { return nil, nil }
	if err := (agyDriver{iso: bad}).PrepareTurn(context.Background(), TurnDirs{Workspace: ws, Home: home}); !errors.Is(err, isolation.ErrUnknownVersion) {
		t.Errorf("unknown version: err = %v", err)
	}
	if err := os.WriteFile(filepath.Join(ws, "AGENTS.md"), []byte("rule"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := d.PrepareTurn(context.Background(), TurnDirs{Workspace: ws, Home: home}); err != nil {
		t.Fatal(err)
	}
	if fi, err := os.Lstat(filepath.Join(ws, "AGENTS.md")); err != nil || !fi.IsDir() {
		t.Fatalf("AGENTS.md was not replaced by its placeholder: %v", err)
	}
}

// TestReadAgyTableRunsTheBinaryOutsideTheWorkspace: the version is read
// from an empty directory with an empty HOME, and output that names no
// known version fails.
func TestReadAgyTableRunsTheBinaryOutsideTheWorkspace(t *testing.T) {
	dir := t.TempDir()
	rec := filepath.Join(dir, "rec")
	fake := filepath.Join(dir, "agy")
	version := filepath.Join(dir, "version")
	setVersion := func(v string) {
		if err := os.WriteFile(version, []byte(v+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	// The version comes from a file: readAgyTable gives the binary a bare
	// environment, so an env var would not reach it.
	script := fmt.Sprintf("#!/bin/sh\n{ pwd; echo \"$HOME\"; ls -A; } > %q\ncat %q\n", rec, version)
	if err := os.WriteFile(fake, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	setVersion("1.5.13")
	tb, err := readAgyTable(context.Background(), fake)
	if err != nil || tb.Version != "1.5.13" {
		t.Fatalf("table %q, err %v", tb.Version, err)
	}
	got, _ := os.ReadFile(rec)
	lines := strings.Split(strings.TrimSpace(string(got)), "\n")
	// The directory is gone by now, so compare the names: macOS shows the
	// same temp directory under /private in pwd.
	if len(lines) != 2 || !strings.HasPrefix(filepath.Base(lines[0]), "agy-version-") || filepath.Base(lines[0]) != filepath.Base(lines[1]) {
		t.Fatalf("agy --version ran in %q with HOME %q (and saw %v); want one fresh empty directory for both", lines[0], safeIndex(lines, 1), lines[2:])
	}
	if wd, _ := os.Getwd(); len(lines) > 0 && lines[0] == wd {
		t.Fatal("agy --version ran in the adapter's working directory")
	}
	setVersion("1.6.0")
	if _, err := readAgyTable(context.Background(), fake); !errors.Is(err, isolation.ErrUnknownVersion) {
		t.Fatalf("unknown version: %v", err)
	}
	if _, err := readAgyTable(context.Background(), ""); err == nil {
		t.Fatal("no binary: no error")
	}
}

func safeIndex(s []string, i int) string {
	if i < len(s) {
		return s[i]
	}
	return ""
}

// agyWriterStub is an agy stand-in for one turn per process. Each run, n
// counting from 1, records its argv and whether a regular AGENTS.md was in
// the workspace when it started, answers its one turn, and in its first run
// leaves a writer behind in its own session that keeps writing AGENTS.md
// into the workspace and into a conversation directory under HOME. A second
// user line on the same process is recorded: it must never come. The
// writer's pid goes to $dir/writer, for the test's scoped kill.
func agyWriterStub(t *testing.T, dir string, turnSleep float64) []string {
	t.Helper()
	if _, err := os.Stat("/usr/bin/perl"); err != nil {
		t.Skip("needs perl for setsid")
	}
	return stub(t, fmt.Sprintf(`
dir=%q
n=$(( $(cat "$dir/runs" 2>/dev/null || echo 0) + 1 )); echo $n > "$dir/runs"
echo "$*" > "$dir/argv.$n"
[ -f AGENTS.md ] && echo yes > "$dir/saw-agents.$n"
[ -f "$HOME/.gemini/antigravity-cli/brain/conv-1/AGENTS.md" ] && echo yes > "$dir/saw-brain.$n"
read first || exit 1
echo '{"event":"init","conversation_id":"conv-1","init":{"model":"m"}}'
if [ "$n" = 1 ]; then
  WS="$PWD" B="$HOME/.gemini/antigravity-cli/brain/conv-1" /usr/bin/perl -MPOSIX -e 'POSIX::setsid(); open(STDIN,"<","/dev/null"); open(STDOUT,">","/dev/null"); open(STDERR,">","/dev/null"); system("mkdir","-p",$ENV{B}); while (1) { for my $d ($ENV{WS}, $ENV{B}) { if (open(my $f, ">", "$d/AGENTS.md")) { print $f "rule"; close($f) } } select(undef,undef,undef,0.02) }' &
  echo $! > "$dir/writer"
fi
sleep %g
echo '{"event":"result","result":{"conversation_id":"conv-1","status":"SUCCESS","response":"turn '$n'"}}'
if read again; then echo "$again" > "$dir/second-turn.$n"; fi
exit 0
`, dir, turnSleep))
}

func waitFile(t *testing.T, path string, bound time.Duration) string {
	t.Helper()
	deadline := time.Now().Add(bound)
	for time.Now().Before(deadline) {
		if b, err := os.ReadFile(path); err == nil && len(strings.TrimSpace(string(b))) > 0 {
			return strings.TrimSpace(string(b))
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("%s never appeared", path)
	return ""
}

// TestAHeldSteerStartsAFreshAgyProcessAfterTheWriterIsGone: a steer held
// during agy's first turn is not written to that process. The process ends
// at its result, the writer it left in its own session is killed and reaped,
// the workspace and the conversation directory are scrubbed, and a fresh
// process resumes the conversation with the steer, seeing no rules file.
func TestAHeldSteerStartsAFreshAgyProcessAfterTheWriterIsGone(t *testing.T) {
	if runtime.GOOS != "linux" && runtime.GOOS != "darwin" {
		t.Skip("needs a POSIX shell and setsid")
	}
	dir := t.TempDir()
	const session, taskID = "chat-agy-iso-1", "task-agy-iso-1"
	url := startServer(t)
	c := testClient(t, url)
	origin := submit(t, c, session, taskID, "start")
	cfg := adapterConfig(url, taskID, session, agyWriterStub(t, dir, 1.5))
	env := useAgyTestDriver(t, &cfg)
	done := runAdapter(context.Background(), cfg)

	pid, _ := strconv.Atoi(waitFile(t, filepath.Join(dir, "writer"), 10*time.Second))
	env.track(pid)
	// The writer is writing. Only into the conversation directory: the
	// workspace's AGENTS.md is the first PrepareTurn's read-only
	// placeholder, which it cannot replace.
	waitFile(t, filepath.Join(env.home, ".gemini/antigravity-cli/brain/conv-1/AGENTS.md"), 5*time.Second)
	if fi, err := os.Lstat(filepath.Join(env.ws, "AGENTS.md")); err != nil || !fi.IsDir() {
		t.Errorf("the workspace AGENTS.md is not the placeholder during turn one: %v", err)
	}
	publishSteerFor(t, c, origin, session, "a steer during turn one")

	out := waitOutcome(t, done, 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("state=%q err=%v", out.res.State, out.err)
	}
	runs, _ := os.ReadFile(filepath.Join(dir, "runs"))
	if got := strings.TrimSpace(string(runs)); got != "2" {
		t.Fatalf("%s harness runs; want 2 (one per user turn)", got)
	}
	for n := 1; n <= 2; n++ {
		if s := readRunIfAny(dir, "second-turn", n); s != "" {
			t.Errorf("run %d took a second user turn on the same process: %q", n, s)
		}
	}
	if argv := readRunIfAny(dir, "argv", 2); !strings.Contains(argv, agyResumeFlag+" conv-1") {
		t.Errorf("run 2 argv %q does not resume conv-1", argv)
	}
	if readRunIfAny(dir, "saw-agents", 2) != "" || readRunIfAny(dir, "saw-brain", 2) != "" {
		t.Error("run 2 started with a rules file the writer left: the writer outlived the scrub")
	}
	if alive := env.alive(); len(alive) > 0 {
		t.Errorf("the writer %v is still alive", alive)
	}
}

// TestAnIdleGapThenANewAgyTurnStartsClean: in a session pod, a result, an
// idle gap while the first turn's writer keeps writing, then the next user
// message (no steer held): the writer is gone and the files scrubbed before
// the next process starts.
func TestAnIdleGapThenANewAgyTurnStartsClean(t *testing.T) {
	if runtime.GOOS != "linux" && runtime.GOOS != "darwin" {
		t.Skip("needs a POSIX shell and setsid")
	}
	dir := t.TempDir()
	const session = "chat-agy-iso-2"
	url := startServer(t)
	c := testClient(t, url)
	submit(t, c, session, "task-agy-iso-2a", "first")
	cfg := reuseConfig(url, "task-agy-iso-2a", session, agyWriterStub(t, dir, 0.2))
	env := useAgyTestDriver(t, &cfg)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, cfg)

	pid, _ := strconv.Atoi(waitFile(t, filepath.Join(dir, "writer"), 10*time.Second))
	env.track(pid)
	waitState(t, c, session, "task-agy-iso-2a", lib.StateCompleted)
	time.Sleep(300 * time.Millisecond) // the idle gap
	if _, err := os.Stat(filepath.Join(env.home, ".gemini/antigravity-cli/brain/conv-1/AGENTS.md")); err != nil {
		t.Fatalf("the writer never wrote into the conversation directory (%v); the test shows nothing", err)
	}

	submit(t, c, session, "task-agy-iso-2b", "second")
	waitState(t, c, session, "task-agy-iso-2b", lib.StateCompleted)
	cancel()
	<-done

	if readRunIfAny(dir, "saw-agents", 2) != "" || readRunIfAny(dir, "saw-brain", 2) != "" {
		t.Error("the second turn started with a rules file the first turn's writer left")
	}
	if alive := env.alive(); len(alive) > 0 {
		t.Errorf("the writer %v is still alive", alive)
	}
}

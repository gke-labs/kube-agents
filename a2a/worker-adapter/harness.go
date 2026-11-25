// Package workeradapter is the in-pod shim between the bus and the harness
// (spec-subagent-profiles.md, "The adapter"): it fetches its one task by
// subject, publishes the lifecycle events, drives the harness over its
// headless contract (Driver: Claude Code's stream-json unless Config.Driver
// names another), forwards steering and follow-ups onto the harness stdin,
// and exits with a code matching the terminal state. One task
// per process, unless the pod is a reused session pod (Config.SessionReuse),
// which runs each turn of its conversation in turn; either way the pod exists
// because the message is already durable.
package workeradapter

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// DefaultHarnessPath is where the worker image carries the harness: the
// native binary shipped inside the agent SDK's platform package (a
// self-contained executable, not a cli.js).
const DefaultHarnessPath = "/app/node_modules/@anthropic-ai/claude-agent-sdk-linux-x64/claude"

const (
	// stderrTailBytes is how much of the harness's stderr is kept for a
	// failure message. A tail rather than the whole stream: the useful part
	// of a crash is its end, and the value rides a status event onto the bus.
	stderrTailBytes = 2048
	// harnessEventDepth buffers events between the scanner goroutine and the
	// adapter loop, so a slow publish does not stall the read that keeps the
	// harness's stdout pipe draining.
	harnessEventDepth = 64
	// scannerInitialBytes is the scanner's starting buffer; scannerMaxBytes is
	// the ceiling and is load-bearing. One harness event is one line of JSON,
	// and a result line above this limit does not truncate -- the scanner stops
	// with bufio.ErrTooLong and the task loses its deliverable, so this bounds
	// the largest answer a worker can return.
	scannerInitialBytes = 64 * 1024
	scannerMaxBytes     = 8 * 1024 * 1024
	// chatterEchoCap bounds how much of a non-JSON stdout line reaches the
	// log. The line is unparsed harness output, so its length is not ours to
	// predict and a single one could be scannerMaxBytes long. Deliberately
	// not shared with adapter.go's steerEchoCap, which happens to hold the
	// same number today: that one bounds a user's message quoted back to
	// them, this one bounds untrusted output written to a log, and the two
	// should be free to move apart.
	chatterEchoCap = 200
	// reasonDetailSeparator joins a terminal reason to its detail, the
	// `reason: <token> - <detail>` shape the eval harness parses the token
	// out of.
	reasonDetailSeparator = " - "
	// stderrTailHeading introduces the stderr tail in a failure reason. The
	// tail is multi-line and read by a person, so it goes on its own lines.
	stderrTailHeading = "\nstderr tail:\n"
	// containerInitPID is the adapter's pid when it is the worker
	// container's entrypoint (Dockerfile.worker's ENTRYPOINT, and the
	// spawner sets no shareProcessNamespace), which is when killLeftovers,
	// and a script's kill (script.go), may signal every other process in
	// the pid namespace.
	containerInitPID = 1
	// leftoverReapBound caps how long killLeftovers polls for the processes
	// it killed to exit, so it reaps them rather than leaving zombies.
	leftoverReapBound = 2 * time.Second
	// leftoverReapPoll is the interval between those polls.
	leftoverReapPoll = 10 * time.Millisecond
)

// The namespace-wide half of killLeftovers and of a script's kill, as
// variables so a test can stand in for being the container's init without
// signalling every process the test's user owns.
var (
	adapterPID         = os.Getpid
	killEveryoneButMe  = func() { _ = syscall.Kill(-1, syscall.SIGKILL) }
	reapKilledChildren = reapChildren
)

// harnessProc supervises one harness subprocess: stdin writer, stdout
// scanner, stderr tail, process-group kill.
type harnessProc struct {
	// closeInput ends the input stream, at most once, from either the run
	// loop or the scan-error path.
	closeInput func()
	cmd        *exec.Cmd
	stdin      io.WriteCloser
	// events carries each stdout line's events, one slice per line, so the
	// adapter handles a line's events together, as it always has.
	events <-chan []Event
	// encode is the driver's stdin line for a user message.
	encode func(string) ([]byte, error)
	// scanDone closes when the stdout scanner has hit EOF - Wait must not
	// run before it, or the pipe teardown races the last buffered events
	// (the result line, typically) out of existence.
	scanDone chan struct{}
	// scanErr surfaces a stdout read/parse failure after events closes.
	scanErr func() error
	stderr  *tailBuffer
	log     *slog.Logger

	// writeMu serializes writes to stdin, so two user messages never
	// interleave on the pipe. It is held across a Write that blocks while the
	// harness is not reading, so nothing the stdout scanner runs may take it:
	// the scanner is what drains the harness's stdout, and a harness blocked
	// writing stdout never reads stdin again.
	writeMu sync.Mutex
	// stdinDead is set once the input stream is closed or a write to it has
	// failed, and makes every later writeUser refuse. It is atomic rather than
	// under a lock so the scanner's overflow path can set it without waiting
	// on a writer.
	stdinDead atomic.Bool

	// mu guards the reap bookkeeping below. No pipe I/O happens under it.
	mu         sync.Mutex
	reapedAt   bool
	killTimers []*time.Timer
}

// startHarness is startHarnessWith for Claude Code.
// oneTurnPerProcess is implemented by a driver whose harness process runs a
// single turn and reads stdin once (core-agent). A later user message would
// sit in the pipe unread: the write succeeds, the adapter counts a turn that
// never runs, and the finished turn's result is dropped with it.
type oneTurnPerProcess interface {
	OneTurnPerProcess()
}

// errOneTurnPerProcess refuses a user message after a one-turn process's
// first, so the adapter answers it as a steer that arrived too late.
var errOneTurnPerProcess = errors.New("this harness runs one turn per process")

// processEncoder is d's encoder for one process: EncodeUser, or for a
// one-turn driver EncodeUser for the first message only.
func processEncoder(d Driver) func(string) ([]byte, error) {
	if _, ok := d.(oneTurnPerProcess); !ok {
		return d.EncodeUser
	}
	var sent atomic.Bool
	return func(text string) ([]byte, error) {
		if sent.Swap(true) {
			return nil, errOneTurnPerProcess
		}
		return d.EncodeUser(text)
	}
}

func startHarness(argv []string, env []string, prompt string, reapBound time.Duration, log *slog.Logger) (*harnessProc, error) {
	return startHarnessWith(claudeDriver{}, argv, env, prompt, reapBound, log)
}

// startHarnessWith launches argv with the given extra environment appended to
// the parent's, writes the opening prompt as the first stdin line in d's
// format, and starts the stdout scanner, which reads lines through d's
// decoder. The process runs in its own process group so a
// kill reaches the harness's own children. reapBound caps how long every reap
// of the harness, here after a failed start or in supervise, waits for its
// stderr to close once the harness itself has exited.
func startHarnessWith(d Driver, argv []string, env []string, prompt string, reapBound time.Duration, log *slog.Logger) (*harnessProc, error) {
	if len(argv) == 0 {
		return nil, fmt.Errorf("empty harness command")
	}
	cmd := exec.Command(argv[0], argv[1:]...) // #nosec G204 -- the configured harness command; argv goes to exec without a shell
	cmd.Env = env
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("harness stdin: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("harness stdout: %w", err)
	}
	stderr := &tailBuffer{max: stderrTailBytes}
	cmd.Stderr = stderr
	// WaitDelay bounds every reap. A descendant that left the process group
	// survives the group kill and can hold stderr open, and Wait reads stderr
	// to EOF, so without a bound Wait blocks until that descendant exits.
	// Wait reads the field when it runs, and Start consults it only for a
	// command built with a Context, which this one is not, so setting it once
	// here covers both reaps: the failed start's below and supervise's. It
	// bounds Wait's own copying only, which for this command is stderr;
	// stdout is a pipe the scanner reads to EOF, and nothing here bounds that.
	cmd.WaitDelay = reapBound

	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("spawn harness: %w", err)
	}

	events := make(chan []Event, harnessEventDepth)
	dec := d.NewDecoder()
	scanDone := make(chan struct{})
	var scanFailed error
	var scanMu sync.Mutex
	p := &harnessProc{
		cmd:      cmd,
		stdin:    stdin,
		events:   events,
		encode:   processEncoder(d),
		scanDone: scanDone,
		scanErr: func() error {
			scanMu.Lock()
			defer scanMu.Unlock()
			return scanFailed
		},
		stderr: stderr,
		log:    log,
	}
	// closeInput ends the input stream at most once. Shared by the scan-error
	// path below and harnessProc.closeStdin, which can both reach it. It marks
	// the stream dead first, so writeUser refuses instead of writing to a
	// closed pipe, and it takes no lock: a writer blocked on the full pipe
	// holds writeMu, and the Close here is what fails that Write and frees it.
	var stdinOnce sync.Once
	p.closeInput = func() {
		stdinOnce.Do(func() {
			p.stdinDead.Store(true)
			_ = stdin.Close()
		})
	}

	go func() {
		defer close(scanDone)
		defer close(events)
		sc := bufio.NewScanner(stdout)
		// Result lines carry a whole turn's text; give them room.
		sc.Buffer(make([]byte, 0, scannerInitialBytes), scannerMaxBytes)
		for sc.Scan() {
			line := sc.Bytes()
			if len(line) == 0 {
				continue
			}
			evs, err := dec.Decode(line)
			if err != nil {
				// A line the driver can't read (for Claude, non-JSON chatter)
				// is logged, never fatal - the harness owns its stdout and
				// the contract owns only the lines it defines.
				log.Warn("harness emitted non-JSON stdout line", "line", truncate(string(line), chatterEchoCap))
				continue
			}
			// A line with no events still goes through: the adapter checks
			// for an abandon on every stdout line, as it always has.
			events <- evs
		}
		if err := sc.Err(); err != nil {
			scanMu.Lock()
			scanFailed = err
			scanMu.Unlock()
			// End the turn, THEN drain. Both halves are needed and each one
			// alone deadlocks:
			//
			//   - Without the close, the harness waits on stdin for the next
			//     turn and never exits, so stdout never closes, the drain below
			//     blocks forever, close(events) never runs and cmd.Wait is
			//     never reached. The task then parks until TaskDeadline (1800s
			//     in production) and publishes `deadline-exceeded`, which
			//     hides the ceiling diagnostic this path exists to produce.
			//   - Without the drain, the harness blocks writing into a full
			//     64KB pipe and cannot reach its own exit.
			//
			// Closing stdin is the same signal the result arm sends to end a
			// turn, so the harness shuts down the way it normally does rather
			// than being killed.
			p.closeInput()
			// Discarding rather than buffering, deliberately: the line that
			// overflowed is the one we already refused to hold in memory.
			_, _ = io.Copy(io.Discard, stdout)
		}
	}()

	if err := p.writeUser(prompt); err != nil {
		// The usual cause is a harness that exited before reading its
		// prompt, and then its exit status and stderr are the only account
		// of why. Kill what is left, reap it (WaitDelay, set above, bounds
		// the reap), and carry both in the error the way supervise's failure
		// arm does.
		//
		// The other cause is the scanner closing stdin after an overflowing
		// stdout line, which fails a write blocked on the full pipe. The
		// scanner records its error before it closes stdin, so it is already
		// readable here and names the real cause, not just the closed pipe.
		//
		// Unlike supervise, this path does not wait for scanDone before Wait,
		// and cannot: a process outside the group that holds stdout keeps the
		// scanner reading until it exits, and nothing drains events here, so
		// the scanner can also be parked on a full channel. Wait then closes
		// the stdout read end under the scanner, whose Read fails with
		// os.ErrClosed. That is Wait's teardown, not the harness's output, so
		// it is dropped rather than relayed as a stdout failure. Wait is the
		// only thing that closes that descriptor, so the error never means
		// anything else. Whether the scanner has stored it yet is a race, and
		// dropping it makes both outcomes read the same.
		p.kill(0)
		reapStart := time.Now()
		waitErr := cmd.Wait()
		reapTook := time.Since(reapStart)
		p.reaped()
		serr := p.scanErr()
		if errors.Is(serr, os.ErrClosed) {
			serr = nil
		}
		return nil, fmt.Errorf("write opening prompt: %w%s%s%s", err, reapEvidence(waitErr, reapTook, reapBound), scanEvidence(serr), p.stderrEvidence())
	}
	return p, nil
}

// scanEvidence is a stdout read failure as a failure reason carries it, or
// nothing when the scanner reached EOF cleanly. A line over the ceiling is
// named with the ceiling and its value rather than relayed as "token too
// long", which says nothing an operator can act on. The deliverable is
// refused, never truncated: a silently shortened answer is worse than a loud
// failure.
func scanEvidence(serr error) string {
	switch {
	case serr == nil:
		return ""
	case errors.Is(serr, bufio.ErrTooLong):
		return fmt.Sprintf(
			reasonDetailSeparator+"the harness emitted a single output line over the %d-byte limit"+
				" (%d MiB, scannerMaxBytes in harness.go); the deliverable was refused"+
				" rather than truncated. A line this size is usually a file dumped"+
				" into the answer.",
			scannerMaxBytes, scannerMaxBytes/(1024*1024))
	default:
		return reasonDetailSeparator + "stdout: " + serr.Error()
	}
}

// reapEvidence is exitEvidence for a reap that WaitDelay bounds, which can
// end with stderr still held open by a process the harness started.
//
// Wait reports that differently by exit status. After a clean exit it
// returns exec.ErrWaitDelay, and that is proof: os/exec starts the WaitDelay
// timer only after the harness has been reaped, so the copy that outlived it
// was reading a stderr some other process still held. Its text names a Go
// I/O timeout and not the exit, so it is replaced by a line that says both.
//
// After a failed exit Wait returns the exit status and drops ErrWaitDelay,
// and nothing else in the error says whether the bound fired. The reap's
// length cannot stand in for it: the timer covers only the copy after the
// harness is reaped, while the reap also spans the harness's own Wait, which
// can stall past the bound on its own (a SIGKILLed harness in uninterruptible
// sleep on a hung mount does not die until the kernel wait ends). So a reap
// that ran the full bound says only that, and that the tail may be cut. A
// zero bound is no bound: Wait read stderr to EOF.
func reapEvidence(waitErr error, took, bound time.Duration) string {
	switch {
	case errors.Is(waitErr, exec.ErrWaitDelay):
		return reasonDetailSeparator + "harness exited 0; " +
			fmt.Sprintf("a process the harness started kept stderr open past %s; the stderr tail stops there", bound)
	case waitErr != nil && bound > 0 && took >= bound:
		return exitEvidence(waitErr) + reasonDetailSeparator +
			fmt.Sprintf("the reap ran the full %s; the stderr tail may stop there", bound)
	default:
		return exitEvidence(waitErr)
	}
}

// exitEvidence is a harness's exit as a failure reason carries it: the Wait
// error after the detail separator, or nothing for a clean exit.
func exitEvidence(waitErr error) string {
	if waitErr == nil {
		return ""
	}
	return reasonDetailSeparator + waitErr.Error()
}

// stderrEvidence is the harness's stderr tail as a failure reason carries
// it, or nothing when the harness wrote none. Call it after Wait, which is
// what guarantees the tail holds everything the harness wrote.
func (p *harnessProc) stderrEvidence() string {
	tail := strings.TrimSpace(p.stderr.String())
	if tail == "" {
		return ""
	}
	return stderrTailHeading + tail
}

// writeUser writes one user message line onto the harness stdin. Steers
// reuse it verbatim: same shape, later turn. Writes are serialized by
// writeMu, and a write after the stream is dead refuses. A close that lands
// while a write is blocked fails that write rather than waiting for it.
func (p *harnessProc) writeUser(text string) error {
	p.writeMu.Lock()
	defer p.writeMu.Unlock()
	if p.stdinDead.Load() {
		return fmt.Errorf("harness stdin closed")
	}
	line, err := p.encode(text)
	if err != nil {
		return err
	}
	if _, err := p.stdin.Write(append(line, '\n')); err != nil {
		p.stdinDead.Store(true)
		return err
	}
	return nil
}

// closeStdin ends the harness's input stream - the signal that the
// conversation is over and it should finish and exit.
func (p *harnessProc) closeStdin() {
	p.closeInput()
}

// kill delivers SIGTERM to the process group, escalating to SIGKILL after
// grace. Timers are retained so the reaper can stop them before the pgid is
// recycled — the Hermes bridge hit exactly that, killing an unrelated process.
func (p *harnessProc) kill(grace time.Duration) {
	// Reaped already (a cancel or eviction that wins the select after the
	// reap): killLeftovers has emptied the group, so its id may be free for
	// reuse, and a signal now could reach an unrelated process.
	p.mu.Lock()
	reaped := p.reapedAt
	p.mu.Unlock()
	if reaped {
		return
	}
	pid := p.cmd.Process.Pid
	_ = syscall.Kill(-pid, syscall.SIGTERM)
	if grace <= 0 {
		_ = syscall.Kill(-pid, syscall.SIGKILL)
		return
	}
	t := time.AfterFunc(grace, func() {
		_ = syscall.Kill(-pid, syscall.SIGKILL)
	})
	p.mu.Lock()
	defer p.mu.Unlock()
	// Reaped already, which the select in the run loop can reach in the same
	// round as a cancel: stop the timer here rather than appending it to a
	// list nothing will drain again. Otherwise the escalation fires grace
	// later at a pgid the kernel may have handed to someone else, which is
	// the exact failure the timer list exists to prevent.
	if p.reapedAt {
		t.Stop()
		return
	}
	p.killTimers = append(p.killTimers, t)
}

// killLeftovers SIGKILLs whatever is left in the harness's process group once
// the harness itself has been reaped, after a clean exit as much as after a
// kill. A process the model started in the background (the Bash tool's `&`)
// stays in the group and outlives the harness otherwise, and in a reused pod
// it would still be running, with the pod's credentials and its scratch
// directory, during the next turn, which can be another requester's. The
// escalation timers do not cover it: reaped stops them, and a clean exit arms
// none.
//
// Called right after the reap and before the next turn can start, from the
// goroutine that reaped. The group's id is the harness's pid, and the kernel
// does not hand out an id still in use as a live process group's, so while
// anything is left in the group this reaches only that; with nothing left it
// finds nothing (ESRCH).
//
// The group is not enough for the real harness: its Bash tool spawns every
// command detached (its own session and process group), so a background
// command leaves the group at birth. In a worker container the adapter is
// pid 1, the namespace's init, so it also SIGKILLs every other process in the
// namespace (kill(-1) skips the caller) and reaps them: between turns
// nothing but the adapter should be running there. That includes a debug
// container attached with --target, which shares the namespace. Outside a
// container (a local run, a test) only the group is signalled: kill(-1)
// there would reach every process the user owns.
func (p *harnessProc) killLeftovers() {
	_ = syscall.Kill(-p.cmd.Process.Pid, syscall.SIGKILL)
	if adapterPID() == containerInitPID {
		killEveryoneButMe()
		reapKilledChildren(leftoverReapBound)
	}
}

// reapChildren waits, for at most bound, for this process's exited children:
// as pid 1 the adapter inherits every orphan killLeftovers or a script's
// kill leaves, and an unreaped one stays a zombie. It runs only after the
// harness's or the script's own Wait, when the adapter has no child of its
// own that os/exec is waiting on, so waiting on any child takes nothing from
// os/exec.
func reapChildren(bound time.Duration) {
	deadline := time.Now().Add(bound)
	for {
		var ws syscall.WaitStatus
		pid, err := syscall.Wait4(-1, &ws, syscall.WNOHANG, nil)
		switch {
		case errors.Is(err, syscall.EINTR):
			continue
		case err != nil:
			// ECHILD: no children left at all.
			return
		case pid > 0:
			continue
		}
		if time.Now().After(deadline) {
			return
		}
		time.Sleep(leftoverReapPoll)
	}
}

// reaped stops any armed escalation timers; called after Wait so a recycled
// process group can't be killed by a stale timer.
func (p *harnessProc) reaped() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.reapedAt = true
	for _, t := range p.killTimers {
		t.Stop()
	}
	p.killTimers = nil
}

// tailBuffer keeps the last max bytes written - failure evidence without
// unbounded memory, the same shape the Hermes bridge uses.
type tailBuffer struct {
	mu  sync.Mutex
	buf []byte
	max int
}

func (t *tailBuffer) Write(b []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, b...)
	if len(t.buf) > t.max {
		t.buf = t.buf[len(t.buf)-t.max:]
	}
	return len(b), nil
}

func (t *tailBuffer) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.buf)
}

// truncate cuts on a rune boundary, not a byte one: the callers put the
// result in a JSON-marshalled status message, and a byte cut through a
// multi-byte sequence marshals to replacement characters.
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	// The cut is the last rune boundary at or before the budget. The first
	// spelling shrank n while the first n RUNES exceeded n BYTES, a condition
	// only satisfiable on a pure-ASCII prefix -- so it walked n down to the
	// first multi-byte rune and stopped, discarding everything after it.
	// "10 ASCII + é + 200 more" truncated to 10 characters against a budget of
	// 50, and an all-multibyte string truncated to nothing at all.
	cut := 0
	for i := range s { // range over a string yields rune start offsets
		if i > n {
			break
		}
		cut = i
	}
	return s[:cut] + "…"
}

// removeUserSettings deletes the harness's user settings file before a turn
// in a reused pod (Config.HarnessUserSettings). A file that is not there is
// the normal case. Anything that cannot be removed (a directory planted at
// the path, a permission error) is an error, and the turn does not run over
// it.
func removeUserSettings(path string) error {
	if path == "" {
		return nil
	}
	if err := os.Remove(path); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("remove the harness's user settings %s: %w", path, err)
	}
	return nil
}

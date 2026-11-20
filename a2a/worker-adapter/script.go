package workeradapter

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/taskcred"
)

// A script-run task (#5622) runs one of its profile's allowlisted helper
// scripts in place of the harness: no model turn, no shell. The dispatcher
// resolves the script at admission and hands it over on the confirm
// (lib.DispatchMessage.Script). The worker runs it only when all of these
// hold, and refuses the task otherwise:
//
//   - it is in Job mode (one-shot), so the pod serves this task alone;
//   - its task credential verifies against the install's task-signing keys,
//     names this task and this pod, and carries a trigger whose script is the
//     confirm's (so neither the confirm nor the credential alone decides);
//   - the path resolves to a regular file under the script root, owned by
//     root and writable by no one else, neither setuid nor setgid, in
//     directories that are the same on both the named path and the resolved
//     one; the file itself may be one symlink within the root whose target,
//     as written, is where it resolves (so not to another link, and not
//     through a symlinked directory that changes where it leads), and no
//     directory on the named path may be a symlink.
//
// The script gets a cleared environment: the worker's own PATH and HOME and
// the broker hand-off (all rendered by the operator), plus the task's
// parameters as KA_PARAM_<NAME> and nothing else from the task. Its stdout,
// capped, is the task's text result; its stderr is read, capped, and only its
// size is logged.
// Exit 0 completes the task, and anything else fails it.
//
// The script starts in its own session, so its session and process group ids
// are its pid. Its kill (at its bound, at the worker's end, on abandon or
// cancel, and once more after it exits) SIGKILLs its process group and every
// process /proc lists in its session (no /proc, as on macOS, skips that
// sweep); and where the worker is the container's init (pid 1), every other
// process in the pid namespace too, which reaches a process that left the
// session, after which the worker reaps what it killed.

const (
	// scriptStdoutCap bounds the text result; past it the output is cut and
	// marked. scriptStderrCap bounds the stderr the worker reads, whose size
	// alone is logged.
	scriptStdoutCap = 64 << 10
	scriptStderrCap = 16 << 10
	// scriptParamPrefix is the only way a task's input reaches a script's
	// environment.
	scriptParamPrefix = "KA_PARAM_"
	// scriptKillGrace is how long a script's processes have between the
	// kill and the worker giving up on its pipes.
	scriptKillGrace = 5 * time.Second
	// scriptTruncated marks a result cut at scriptStdoutCap.
	scriptTruncated = "\n[output truncated]"
	// sessionSweepPasses bounds how many times killSession lists the
	// session, and sessionSweepPoll is the pause between passes: a process
	// can fork while a pass runs, and a killed one is listed until it dies.
	sessionSweepPasses = 20
	sessionSweepPoll   = 10 * time.Millisecond
)

// procRoot is where killSession lists processes.
const procRoot = "/proc"

// scriptParamName is a parameter's name as a script reads it, after
// KA_PARAM_: upper case, at most 32 characters.
var scriptParamName = regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,31}$`)

// scriptHandoffEnv are the worker's own variables a script gets, as the
// operator rendered them: never a task's.
var scriptHandoffEnv = []string{"PATH", "HOME", "CREDENTIAL_PROXY_URL", "CREDENTIAL_PROXY_TOKEN_FILE", "CREDENTIAL_PROXY_TASK_TOKEN_FILE"}

// scriptRun is a script cleared to run: its resolved path, the environment
// it runs with, and its bound.
type scriptRun struct {
	name    string
	path    string
	env     []string
	timeout time.Duration
}

// scriptFileCheck holds a script's file to the root it must lie under and
// the owner it must have. The zero value is the image's: lib.ScriptRoot,
// root (uid 0), and the worker's own uid. Tests point root at a directory
// that stands in for lib.ScriptRoot on disk.
type scriptFileCheck struct {
	root      string
	ownerUID  uint32
	workerUID int
	set       bool
}

func (c scriptFileCheck) withDefaults() scriptFileCheck {
	if !c.set {
		return scriptFileCheck{root: lib.ScriptRoot, ownerUID: 0, workerUID: os.Getuid(), set: true}
	}
	return c
}

// resolve is the real path of the script at path, or why it can't run. The
// path has ScriptPath's shape under the root; its real path lies under the
// root too; a named path that is a symlink names its real path as written,
// so it is one link with no symlink, file or directory, on the way to the
// file; every directory from the root down to the real path, and down to
// the named path (where none may be a symlink), and the file itself, is
// owned by the owner and writable by no group or other; the file is a
// regular file, neither setuid nor setgid; and the worker isn't the owner,
// so it can't have written it. The root's own ancestors are image layers,
// which the build-time check (deploy/docker/test_script_root_ownership.py)
// keeps root-owned.
func (c scriptFileCheck) resolve(path string) (string, error) {
	c = c.withDefaults()
	root := strings.TrimSuffix(c.root, "/") + "/"
	if !lib.ScriptPath.MatchString(path) {
		return "", fmt.Errorf("path %q is not a script path under %s", path, lib.ScriptRoot)
	}
	path = root + strings.TrimPrefix(path, lib.ScriptRoot)
	if c.workerUID == int(c.ownerUID) {
		return "", fmt.Errorf("the worker runs as uid %d, the scripts' owner, so it could have written %s", c.workerUID, path)
	}
	real, err := filepath.EvalSymlinks(path)
	if err != nil {
		return "", fmt.Errorf("resolving %s: %w", path, err)
	}
	if !strings.HasPrefix(real, root) {
		return "", fmt.Errorf("%s resolves to %s, outside %s", path, real, root)
	}
	// One link at most: the checks below cover the named path's directories and the real path's, so
	// a second link, or a symlinked directory in the link's target, would leave a directory between
	// them unchecked.
	if err := oneLinkAtMost(path, real); err != nil {
		return "", err
	}
	// Both chains: the resolved file's, and the named path's, so a directory on the way that is a
	// symlink or writable by someone else is refused even when the file it leads to passes.
	for _, d := range append(dirChain(root, path), dirChain(root, real)...) {
		if err := c.ownedAndUnwritable(d, true); err != nil {
			return "", err
		}
	}
	if err := c.ownedAndUnwritable(real, false); err != nil {
		return "", err
	}
	return real, nil
}

// oneLinkAtMost refuses a named path that is a symlink whose target, as
// written and taken from the link's own directory, isn't where it resolves:
// a link to a link, or a link through a symlinked directory that changes
// where it leads.
func oneLinkAtMost(path, real string) error {
	fi, err := os.Lstat(path)
	if err != nil {
		return fmt.Errorf("reading %s: %w", path, err)
	}
	if fi.Mode()&os.ModeSymlink == 0 {
		return nil
	}
	target, err := os.Readlink(path)
	if err != nil {
		return fmt.Errorf("reading the link %s: %w", path, err)
	}
	if !filepath.IsAbs(target) {
		target = filepath.Join(filepath.Dir(path), target)
	}
	if filepath.Clean(target) != real {
		return fmt.Errorf("%s links to %s, which is itself a symlink or reached through one (it resolves to %s); a script may be one link to its file", path, target, real)
	}
	return nil
}

// dirChain is every directory from root (which ends in "/") down to file's
// own, root first.
func dirChain(root, file string) []string {
	dirs := []string{strings.TrimSuffix(root, "/")}
	if filepath.Dir(file)+"/" == root {
		return dirs
	}
	cur := strings.TrimSuffix(root, "/")
	for _, seg := range strings.Split(strings.TrimPrefix(filepath.Dir(file), root), "/") {
		cur = cur + "/" + seg
		dirs = append(dirs, cur)
	}
	return dirs
}

func (c scriptFileCheck) ownedAndUnwritable(path string, dir bool) error {
	fi, err := os.Lstat(path)
	if err != nil {
		return fmt.Errorf("reading %s: %w", path, err)
	}
	switch {
	case dir && fi.Mode()&os.ModeSymlink != 0:
		return fmt.Errorf("%s is a symlink, and no directory on a script's path may be one", path)
	case dir && !fi.IsDir():
		return fmt.Errorf("%s is not a directory", path)
	case !dir && !fi.Mode().IsRegular():
		return fmt.Errorf("%s is not a regular file", path)
	case !dir && fi.Mode()&(os.ModeSetuid|os.ModeSetgid) != 0:
		return fmt.Errorf("%s is setuid or setgid (%v)", path, fi.Mode())
	}
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		return fmt.Errorf("%s: no owner to check", path)
	}
	if st.Uid != c.ownerUID {
		return fmt.Errorf("%s is owned by uid %d, not %d", path, st.Uid, c.ownerUID)
	}
	if fi.Mode().Perm()&0o022 != 0 {
		return fmt.Errorf("%s is group- or world-writable (%v)", path, fi.Mode().Perm())
	}
	return nil
}

// verifyScriptCredential checks that the task credential is the
// dispatcher's for this task on this pod, and names script: the
// confirm's script and the credential's must agree.
func verifyScriptCredential(token string, keys []string, now time.Time, task, pod, script string) error {
	if len(keys) == 0 {
		return errors.New("no task-signing key is configured on this worker, so its credential can't be verified")
	}
	if token == "" {
		return errors.New("the confirm carries no task credential to verify")
	}
	c, err := taskcred.Verify(token, keys, taskcred.AudienceCredentialProxy, now, taskcred.DefaultMaxLifetime, taskcred.Skew)
	if err != nil {
		return fmt.Errorf("the task credential doesn't verify: %w", err)
	}
	switch {
	case c.Task != task || c.Pod != pod:
		return fmt.Errorf("the task credential is for task %s on pod %s, not %s on %s", c.Task, c.Pod, task, pod)
	case c.Trigger == nil || c.Trigger.Script == "":
		return errors.New("the task credential names no script")
	case c.Trigger.Script != script:
		return fmt.Errorf("the confirm names script %q, the task credential %q", script, c.Trigger.Script)
	}
	return nil
}

// scriptEnv is the environment a script runs with: the hand-off variables
// from getenv (the worker's own, as rendered), and the task's parameters as
// KA_PARAM_<NAME>. A parameter whose name can't be one is an error, not
// dropped: the script would run without input it was given.
func scriptEnv(origin *lib.Envelope, getenv func(string) string) ([]string, error) {
	// Never nil: exec gives a command with a nil Env the worker's own
	// environment, model key and all.
	env := []string{}
	for _, k := range scriptHandoffEnv {
		if v := getenv(k); v != "" {
			env = append(env, k+"="+v)
		}
	}
	params, err := taskParameters(origin)
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(params))
	for name := range params {
		names = append(names, name)
	}
	slices.Sort(names)
	for _, name := range names {
		upper := strings.ToUpper(name)
		if !scriptParamName.MatchString(upper) {
			return nil, fmt.Errorf("parameter %q can't be a script's %s<NAME> (upper case, at most 32 characters)", name, scriptParamPrefix)
		}
		env = append(env, scriptParamPrefix+upper+"="+params[name])
	}
	return env, nil
}

// cappedBuffer keeps the first max bytes written to it and notes whether
// more came.
type cappedBuffer struct {
	mu   sync.Mutex
	buf  bytes.Buffer
	max  int
	over bool
}

func (b *cappedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if room := b.max - b.buf.Len(); room < len(p) {
		b.over = true
		if room > 0 {
			b.buf.Write(p[:room])
		}
		return len(p), nil
	}
	b.buf.Write(p)
	return len(p), nil
}

func (b *cappedBuffer) text() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.over {
		// The cap is a byte count, so it can fall inside a multi-byte character; drop that character's
		// leading bytes (at most UTFMax-1) rather than cut it before the marker. Only the cut point is
		// trimmed: an invalid byte earlier is the script's and stays, so no output past it is lost.
		kept := b.buf.Bytes()
		for i := len(kept) - 1; i >= 0 && i >= len(kept)-(utf8.UTFMax-1); i-- {
			if utf8.RuneStart(kept[i]) {
				if !utf8.FullRune(kept[i:]) {
					kept = kept[:i]
				}
				break
			}
		}
		return string(kept) + scriptTruncated
	}
	return b.buf.String()
}

// scriptResult is how a script run ended.
type scriptResult struct {
	stdout    string
	stderr    string
	exitCode  int
	startErr  error
	timedOut  bool
	stopped   bool // the worker's context ended (SIGTERM)
	abandoned bool
	canceled  bool
}

// scriptSignals are what can end a running script early besides its bound
// and the worker's context: the dispatcher's abandon, which onAbandon
// records on the adapter (so its terminal check sees it, whenever it came),
// and the requester's cancel. Either may be nil.
type scriptSignals struct {
	abandon   <-chan string
	onAbandon func(reason string)
	cancel    <-chan struct{}
}

// killSession SIGKILLs every process but the worker that procRoot lists in
// session sid, pass after pass until a pass finds one it can signal no
// longer. A session's id is its leader's pid, and the kernel doesn't hand out
// a pid still in use as a session's id, so the session itself can't be
// mistaken; a member's own pid can be reused in the moment between reading
// procRoot and the kill. As pid 1 that can only be one of the script's own
// descendants, and the namespace kill follows anyway. Where procRoot can't
// be read it does nothing.
func killSession(procRoot string, sid int) {
	self := os.Getpid()
	for range sessionSweepPasses {
		found := false
		for _, pid := range sessionMembers(procRoot, sid) {
			if pid == self {
				continue
			}
			// Only a member it could signal keeps the sweep going: one it
			// may not signal (EPERM) would make it spin to the pass limit.
			if syscall.Kill(pid, syscall.SIGKILL) == nil {
				found = true
			}
		}
		if !found {
			return
		}
		time.Sleep(sessionSweepPoll)
	}
}

// sessionMembers is every live (not zombie) process procRoot lists whose
// /proc/<pid>/stat session field (the sixth) is sid.
func sessionMembers(procRoot string, sid int) []int {
	entries, err := os.ReadDir(procRoot)
	if err != nil {
		return nil
	}
	var pids []int
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil || pid <= 0 {
			continue
		}
		b, err := os.ReadFile(filepath.Join(procRoot, e.Name(), "stat"))
		if err != nil {
			continue // it exited
		}
		if got, live, ok := statSession(string(b)); ok && live && got == sid {
			pids = append(pids, pid)
		}
	}
	return pids
}

// statSession is the session id in a /proc/<pid>/stat line, and whether the
// process is live (neither zombie nor dead). The command name, in
// parentheses, can hold spaces and parentheses itself, so the fields are
// read after its last ")": state, ppid, pgrp, session.
func statSession(stat string) (sid int, live, ok bool) {
	i := strings.LastIndexByte(stat, ')')
	if i < 0 {
		return 0, false, false
	}
	f := strings.Fields(stat[i+1:])
	if len(f) < 4 {
		return 0, false, false
	}
	sid, err := strconv.Atoi(f[3])
	if err != nil {
		return 0, false, false
	}
	return sid, f[0] != "Z" && f[0] != "X", true
}

// execScript runs sr with no shell, in its own session, which is killed
// whole at its timeout, at ctx's end, on abandon or cancel, and once more
// after the script exits, so nothing it started outlives it (see the
// comment at the top of this file for what the kill reaches).
func execScript(ctx context.Context, sr *scriptRun, sig scriptSignals) scriptResult {
	runCtx, cancel := context.WithTimeout(ctx, sr.timeout)
	defer cancel()
	cmd := exec.CommandContext(runCtx, sr.path) // #nosec G204 -- an allowlisted, resolved and ownership-checked path; no shell, no arguments
	cmd.Env = sr.env
	if cmd.Env == nil {
		cmd.Env = []string{}
	}
	// Setsid makes the script a session leader, so its session id and its
	// process group id are both its pid.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	killAll := func() error {
		groupErr := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		killSession(procRoot, cmd.Process.Pid)
		if adapterPID() == containerInitPID {
			killEveryoneButMe()
		}
		if groupErr != nil && !errors.Is(groupErr, syscall.ESRCH) {
			return groupErr
		}
		return os.ErrProcessDone
	}
	cmd.Cancel = killAll
	cmd.WaitDelay = scriptKillGrace
	stdout := &cappedBuffer{max: scriptStdoutCap}
	stderr := &tailBuffer{max: scriptStderrCap}
	cmd.Stdout, cmd.Stderr = stdout, stderr

	var res scriptResult
	if err := cmd.Start(); err != nil {
		res.startErr = err
		return res
	}
	// The watcher ends before res is read, so an abandon it took is
	// always seen; one it didn't take stays on the channel for the
	// adapter's own check.
	var watchedAbandon, watchedCancel bool
	stopWatch, watchDone := make(chan struct{}), make(chan struct{})
	go func() {
		defer close(watchDone)
		select {
		case reason := <-sig.abandon:
			watchedAbandon = true
			if sig.onAbandon != nil {
				sig.onAbandon(reason)
			}
			cancel()
		case <-sig.cancel:
			watchedCancel = true
			cancel()
		case <-stopWatch:
		}
	}()
	err := cmd.Wait()
	_ = killAll()
	if adapterPID() == containerInitPID {
		// Only now, with the script itself reaped by Wait: as init the
		// worker inherits every orphan the kill left, and an unreaped one
		// stays a zombie.
		reapKilledChildren(leftoverReapBound)
	}
	close(stopWatch)
	<-watchDone
	res.abandoned, res.canceled = watchedAbandon, watchedCancel
	res.stdout, res.stderr = stdout.text(), stderr.String()
	// A script that exited on its own (not killed by a signal) finished inside its bound, even when
	// the bound passed while the worker waited, up to scriptKillGrace, on a pipe a child it started
	// still held: its exit status, not the timeout, is how it ended.
	exitedOnItsOwn := cmd.ProcessState != nil && cmd.ProcessState.Exited()
	switch {
	case res.abandoned, res.canceled:
	case ctx.Err() != nil:
		res.stopped = true
	case errors.Is(runCtx.Err(), context.DeadlineExceeded) && !exitedOnItsOwn:
		res.timedOut = true
	}
	var exitErr *exec.ExitError
	switch {
	case err == nil:
	case errors.Is(err, exec.ErrWaitDelay) && cmd.ProcessState != nil:
		// The script exited, and something it started held its pipes
		// past the grace: the exit is the script's, and what it started
		// is killed above.
		res.exitCode = cmd.ProcessState.ExitCode()
	case errors.As(err, &exitErr):
		res.exitCode = exitErr.ExitCode()
	default:
		res.exitCode = -1
	}
	return res
}

// runScript is a script-run task's turn: the task's .in read (a cancel is
// the executor's to act on, and a script takes no other message), working,
// the script, and its terminal. The capability check has already passed.
func (a *adapter) runScript(ctx context.Context, origin *lib.Envelope, originSeq uint64, sr *scriptRun) turnOutcome {
	done := func(res Result, err error) turnOutcome { return turnOutcome{res: res, err: err} }
	steerCh := make(chan string, steerQueueDepth)
	cancelCh := make(chan struct{}, 1)
	stopIn, err := a.consumeIn(ctx, originSeq+1, origin.EnvelopeID, steerCh, cancelCh)
	if err != nil {
		state := lib.StateFailed
		return done(Result{State: state}, a.finalize(state, "reason: bus-subscribe-failed - "+err.Error(), ""))
	}
	defer stopIn()
	steersDone := make(chan struct{})
	defer close(steersDone)
	go func() {
		for {
			select {
			case steer := <-steerCh:
				a.publishSteerRefusal(steer, "a scheduled script takes no messages")
			case <-steersDone:
				return
			}
		}
	}()
	if a.abandoned() {
		return done(Result{Abandoned: true}, nil)
	}
	if err := a.exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		return done(Result{State: lib.StateFailed}, fmt.Errorf("publish working: %w", err))
	}
	start := time.Now()
	res := execScript(ctx, sr, scriptSignals{abandon: a.abandon, cancel: cancelCh, onAbandon: func(reason string) {
		a.mu.Lock()
		a.abandonSeen, a.abandonReason = true, reason
		a.mu.Unlock()
	}})
	// The stderr's size only, never its text: a script, or a tool it calls, can print a credential, and a
	// log line is not where a redactor's misses should land (#5954 review). Whoever debugs a failure reruns
	// the script by hand.
	a.log.Info("script ran", "task", a.cfg.TaskID, "script", sr.name, "exit", res.exitCode,
		"took", time.Since(start).Round(time.Millisecond), "timedOut", res.timedOut, "stderrBytes", len(res.stderr))
	switch {
	case res.abandoned:
		return done(Result{Abandoned: true}, nil)
	case res.canceled:
		state := lib.StateCanceled
		return done(Result{State: state}, a.finalize(state, "reason: canceled-by-request", ""))
	case res.stopped:
		state := lib.StateFailed
		return done(Result{State: state, Evicted: true}, a.finalize(state, "reason: worker-evicted - infrastructure delivered SIGTERM while the script ran", ""))
	case res.startErr != nil:
		state := lib.StateFailed
		return done(Result{State: state}, a.finalize(state, fmt.Sprintf("reason: script-failed - script %s could not start: %v", sr.name, res.startErr), ""))
	case res.timedOut:
		state := lib.StateFailed
		return done(Result{State: state}, a.finalize(state, fmt.Sprintf("reason: script-timeout - script %s ran past its %s bound", sr.name, sr.timeout), ""))
	case res.exitCode != 0:
		state := lib.StateFailed
		return done(Result{State: state}, a.finalize(state, fmt.Sprintf("reason: script-failed - script %s exited %d", sr.name, res.exitCode), ""))
	}
	r, err := a.completeWith(res.stdout)
	return done(r, err)
}

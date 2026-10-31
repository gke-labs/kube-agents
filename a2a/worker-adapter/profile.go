package workeradapter

// The profile worker: a pod of an AgentProfile's (profile, tier) pool, which
// takes its tasks from the dispatcher rather than from a stream (#3655,
// contract addendum 5). It connects as its pod, listens on its own dispatch
// subject, and runs one task at a time through the same execute path a
// session pod uses; only how a task arrives differs.
//
// The hand-off: the dispatcher offers a task, the worker accepts or
// declines, and an accept is answered with confirm (start) or abandon
// (drop). The worker renews the task's lease from acceptance until it has
// published the terminal, then once more with Final. On an abandon the
// dispatcher writes the terminal itself, so the worker stops the harness and
// publishes nothing more.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/render"
)

// ProfileWorkerConfig is what a profile worker needs beyond Config, which
// must be a profile executor (ProfileExecutor, a pod name and the bus token).
type ProfileWorkerConfig struct {
	// Tier is the tier this pod's pool serves; with the profile and the pod
	// it names the dispatch subject.
	Tier string
	// OneShot ends the worker after its first task: after the final renewal,
	// after an abandon, or when no confirm arrives in time. A Job's pod runs
	// this way (PR 6); a warm pool's pods don't.
	OneShot bool
	// ReadyAddr is the pod-local address the readiness endpoint listens on.
	// /readyz answers 200 only once the dispatch subscription is live, so
	// the dispatcher, which offers only to Ready pods, never offers into a
	// worker that isn't listening yet. Empty disables the listener.
	ReadyAddr string
	// PersonaDir is the profile's persona, mounted read-only (/persona,
	// #3662). It is loaded once, at start, and rendered for the harness into
	// a fresh CLAUDE_CONFIG_DIR under ConfigRoot for each task, so nothing a
	// task leaves in its config reaches the next. Empty runs the harness as
	// configured, which only tests do.
	PersonaDir string
	// Harness is the persona adapter to render with (persona.ClaudeCode).
	Harness persona.Harness
	// ConfigRoot is where each task's config directory is made: its own
	// emptyDir, not the harness's working directory.
	ConfigRoot string
	// TaskTokenFile is where the confirm's proxy token is written for the
	// task's lifetime (CREDENTIAL_PROXY_TASK_TOKEN_FILE, a tmpfs emptyDir),
	// for the kubectl and gcloud shims to send to the broker. Empty skips it.
	TaskTokenFile string
	// deadlineMargin is how long before the offer's deadline the harness is
	// stopped, so the worker's own deadline-exceeded terminal and its final
	// renewal reach the bus before the dispatcher's abandon. Zero is
	// defaultDeadlineMargin.
	deadlineMargin time.Duration
}

// defaultDeadlineMargin is added to the harness's kill grace: the worker's
// deadline has to leave room for the harness to be stopped and its terminal
// and final renewal sent before the dispatcher's own deadline.
const defaultDeadlineMargin = 5 * time.Second

// declineBusy is the reason a worker gives for an offer while it holds a
// task. The dispatcher tracks busy pods itself, so this is the race where
// its view lagged.
const declineBusy = "busy: this pod already holds a task"

// RunProfileWorker serves dispatched tasks until ctx ends or, with OneShot,
// after the first. A SIGTERM during a task is the eviction path execute
// already has: the task fails as worker-evicted and the final renewal goes
// out.
func RunProfileWorker(ctx context.Context, cfg Config, pw ProfileWorkerConfig) error {
	cfg.applyDefaults()
	switch {
	case !cfg.ProfileExecutor:
		return fmt.Errorf("a profile worker needs %s", lib.EnvProfileExecutor)
	case cfg.PodName == "" || cfg.BusTokenFile == "":
		return fmt.Errorf("a profile worker connects as its pod: %s and the bus token file are required", lib.EnvPodName)
	case !lib.ValidSubjectToken(cfg.Profile) || !lib.ValidSubjectToken(pw.Tier) || !lib.ValidSubjectToken(cfg.PodName):
		return fmt.Errorf("profile %q, tier %q and pod %q must each be one subject token", cfg.Profile, pw.Tier, cfg.PodName)
	}
	if err := cfg.validate(); err != nil {
		return err
	}
	if pw.deadlineMargin <= 0 {
		pw.deadlineMargin = cfg.KillGrace + defaultDeadlineMargin
	}
	b, closeBus, err := dialBus(ctx, cfg)
	if err != nil {
		return err
	}
	defer closeBus()

	w := &profileWorker{cfg: cfg, pw: pw, b: b, msgs: make(chan *nats.Msg, 16)}
	if pw.PersonaDir != "" {
		if w.persona, err = renderPersona(pw); err != nil {
			return err
		}
	}
	sub, err := b.nc.ChanSubscribe(lib.DispatchSubject(cfg.Profile, pw.Tier, cfg.PodName), w.msgs)
	if err != nil {
		return fmt.Errorf("subscribe to the dispatch subject: %w", err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	// The server has the subscription once a flush round-trips; only then
	// can an offer reach this pod.
	flushCtx, cancelFlush := context.WithTimeout(ctx, 10*time.Second)
	defer cancelFlush()
	if err := b.nc.FlushWithContext(flushCtx); err != nil {
		return fmt.Errorf("flush the dispatch subscription: %w", err)
	}
	if pw.ReadyAddr != "" {
		stop, err := w.serveReadiness(pw.ReadyAddr)
		if err != nil {
			return err
		}
		defer stop()
	}
	w.ready.Store(true)
	cfg.Logger.Info("profile worker listening", "profile", cfg.Profile, "tier", pw.Tier, "pod", cfg.PodName, "oneShot", pw.OneShot)
	return w.loop(ctx)
}

type profileWorker struct {
	cfg   Config
	pw    ProfileWorkerConfig
	b     *busConns
	msgs  chan *nats.Msg
	ready atomic.Bool
	// persona is the persona as rendered for the harness, or nil.
	persona *persona.Output
}

// renderPersona loads the profile's persona and renders it for the harness.
// It runs once: the persona is the image's and doesn't change under a
// running pod, and a persona that doesn't load stops the worker before it
// takes a task.
func renderPersona(pw ProfileWorkerConfig) (*persona.Output, error) {
	adapter, err := render.For(pw.Harness)
	if err != nil {
		return nil, err
	}
	p, err := persona.Load(pw.PersonaDir)
	if err != nil {
		return nil, fmt.Errorf("the persona at %s: %w", pw.PersonaDir, err)
	}
	out, err := adapter.Render(p, persona.Target{})
	if err != nil {
		return nil, fmt.Errorf("rendering the persona: %w", err)
	}
	if err := render.CheckOutput(out); err != nil {
		return nil, err
	}
	return &out, nil
}

// userSettingsFile is the harness's user settings, the only settings source
// it reads (--setting-sources user). Settings can carry hooks, which run
// commands, so the worker owns this file and the model never writes it.
const userSettingsFile = "settings.json"

// taskConfigDir writes the rendered persona into a fresh config directory
// for one task and returns it, with what removes it.
//
// What keeps the model from configuring its own harness is that no harness
// ever reads a config directory after a model could have touched it: each
// task gets a fresh one, written before that task's only harness start
// (the profile worker never resumes or restarts a harness on it) and
// removed after, and the harness takes its hooks and instructions at
// startup. Everything the worker writes here is also made read-only, with
// an empty settings.json, as defence in depth. That stops an in-place
// overwrite, not an unlink or a rename, since the directory itself stays
// writable for the harness's own session state.
func (w *profileWorker) taskConfigDir(task string) (string, func(), error) {
	dir, err := os.MkdirTemp(w.pw.ConfigRoot, "config-"+task+"-")
	if err != nil {
		return "", nil, err
	}
	remove := func() {
		// Give the owner back write on the read-only tree so it can be
		// removed.
		_ = filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
			if err == nil && d.IsDir() {
				_ = os.Chmod(p, 0o755)
			}
			return nil
		})
		_ = os.RemoveAll(dir)
	}
	files := append(append([]persona.File(nil), w.persona.Files...), persona.File{Path: userSettingsFile, Data: []byte("{}\n")})
	if err := render.WriteFiles(dir, files); err != nil {
		remove()
		return "", nil, err
	}
	if err := lockRendered(dir); err != nil {
		remove()
		return "", nil, err
	}
	return dir, remove, nil
}

// lockRendered makes everything under dir read-only (files 0444, or 0555
// when executable; directories 0555), leaving dir itself writable. A
// directory is chmodded after WalkDir has listed it, so the walk still
// reaches what's inside.
func lockRendered(dir string) error {
	return filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil || p == dir {
			return err
		}
		if d.IsDir() {
			return os.Chmod(p, 0o555)
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		mode := os.FileMode(0o444)
		if info.Mode()&0o100 != 0 {
			mode = 0o555
		}
		return os.Chmod(p, mode)
	})
}

// held is the task the worker has accepted: pending until the confirm, then
// running.
type held struct {
	offer   lib.DispatchMessage
	origin  *lib.Envelope
	running bool
	// stopRenew ends the renewal loop; renewDone closes when it has.
	stopRenew context.CancelFunc
	renewDone chan struct{}
	// abandon reaches the running task's supervise loop.
	abandon chan string
	// creds are the confirm's task credentials, when the dispatcher holds a
	// task-signing key (addendum 8).
	creds confirmCredentials
	// done carries the task's outcome once execute returns.
	done chan turnOutcome
}

func (w *profileWorker) loop(ctx context.Context) error {
	log := w.cfg.Logger
	var (
		cur         *held
		acceptTimer <-chan time.Time
		taskDone    <-chan turnOutcome
	)
	// release ends the held task's renewals and forgets it. It reports
	// whether the worker is done (OneShot).
	release := func() bool {
		if cur != nil {
			cur.stopRenew()
			<-cur.renewDone
		}
		cur, acceptTimer, taskDone = nil, nil, nil
		return w.pw.OneShot
	}
	for {
		select {
		case <-ctx.Done():
			if cur != nil && cur.running {
				// execute sees the same ctx and publishes worker-evicted;
				// wait for it so the final renewal still goes out.
				out := <-taskDone
				w.finish(cur, out)
			}
			if cur != nil {
				release()
			}
			return nil

		case <-acceptTimer:
			log.Warn("no confirm or abandon within the accept window; dropping the task", "task", cur.offer.TaskID)
			if release() {
				return nil
			}

		case out := <-taskDone:
			w.finish(cur, out)
			if release() {
				return nil
			}

		case msg := <-w.msgs:
			var m lib.DispatchMessage
			if err := json.Unmarshal(msg.Data, &m); err != nil {
				log.Warn("an unreadable dispatch message; ignoring it", "err", err)
				continue
			}
			if err := m.Validate(); err != nil {
				log.Warn("a malformed dispatch message; ignoring it", "err", err)
				continue
			}
			switch m.Type {
			case lib.DispatchOffer:
				if cur != nil {
					w.answer(m.TaskID, false, declineBusy)
					continue
				}
				origin, why := w.admitOffer(m)
				if why != "" {
					log.Warn("declining an offer", "task", m.TaskID, "reason", why)
					w.answer(m.TaskID, false, why)
					continue
				}
				cur = w.hold(ctx, m, origin)
				w.answer(m.TaskID, true, "")
				acceptTimer = time.After(time.Duration(m.AcceptSeconds) * time.Second)

			case lib.DispatchConfirm:
				if cur == nil || cur.running || cur.offer.TaskID != m.TaskID {
					log.Warn("a confirm for a task this pod isn't waiting on; ignoring it", "task", m.TaskID)
					continue
				}
				if time.Until(cur.offer.Deadline) <= w.pw.deadlineMargin {
					// Too late to run anything inside the deadline. Dropping
					// it lets the lease lapse; starting would spend a turn
					// on a harness killed at once.
					log.Warn("confirmed past the point the task could finish in time; dropping it", "task", m.TaskID)
					if release() {
						return nil
					}
					continue
				}
				cur.running, acceptTimer = true, nil
				_ = json.Unmarshal(msg.Data, &cur.creds)
				taskDone = w.start(ctx, cur)

			case lib.DispatchAbandon:
				if cur == nil || cur.offer.TaskID != m.TaskID {
					continue
				}
				log.Info("the dispatcher abandoned the task", "task", m.TaskID, "reason", m.Reason)
				if !cur.running {
					if release() {
						return nil
					}
					continue
				}
				// The dispatcher writes this task's terminal. Stop the
				// harness and wait for execute to return without one. If the
				// task finished first, its terminal is out, so the final
				// renewal still follows.
				cur.abandon <- m.Reason
				if out := <-taskDone; !out.res.Abandoned {
					w.finish(cur, out)
				}
				if release() {
					return nil
				}
			}
		}
	}
}

// admitOffer reads the offer's submission and checks it is the task the
// offer names, for this profile. It returns the reason to decline, or "".
func (w *profileWorker) admitOffer(m lib.DispatchMessage) (*lib.Envelope, string) {
	origin, err := lib.ParseEnvelope(m.Envelope)
	switch {
	case err != nil:
		return nil, "unreadable: the offer's envelope does not parse"
	case origin.TaskID != m.TaskID:
		return nil, fmt.Sprintf("mismatch: the envelope is for task %q, the offer for %q", origin.TaskID, m.TaskID)
	case origin.To == nil || origin.To.Session != w.cfg.Profile:
		return nil, "mismatch: the envelope is not addressed to this pod's profile"
	case time.Until(m.Deadline) <= w.pw.deadlineMargin:
		return nil, "expired: the offer's deadline has passed"
	}
	return origin, ""
}

func (w *profileWorker) answer(task string, accepted bool, reason string) {
	a := lib.DispatchAccept{TaskID: task, Accepted: accepted, Reason: reason}
	if err := w.publish(lib.DispatchAcceptSubject(w.cfg.Profile, w.pw.Tier, w.cfg.PodName), a); err != nil {
		w.cfg.Logger.Warn("could not answer an offer", "task", task, "accepted", accepted, "err", err)
	}
}

// hold takes the offered task and starts renewing its lease, which the
// contract runs from acceptance.
func (w *profileWorker) hold(ctx context.Context, m lib.DispatchMessage, origin *lib.Envelope) *held {
	renewCtx, stop := context.WithCancel(ctx)
	h := &held{offer: m, origin: origin, stopRenew: stop, renewDone: make(chan struct{}), abandon: make(chan string, 1)}
	go func() {
		defer close(h.renewDone)
		t := time.NewTicker(time.Duration(m.RenewSeconds) * time.Second)
		defer t.Stop()
		for {
			select {
			case <-renewCtx.Done():
				return
			case <-t.C:
				w.renew(m.TaskID, false)
			}
		}
	}()
	return h
}

func (w *profileWorker) renew(task string, final bool) {
	if err := w.publish(lib.DispatchLeaseSubject(w.cfg.Profile, w.pw.Tier, w.cfg.PodName), lib.LeaseRenewal{TaskID: task, Final: final}); err != nil {
		w.cfg.Logger.Warn("lease renewal not sent", "task", task, "final", final, "err", err)
	}
}

func (w *profileWorker) publish(subject string, v any) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return w.b.nc.Publish(subject, data)
}

// confirmCredentials are the two credentials a confirm carries (contract
// addendum 8). They're decoded by name here until lib.DispatchMessage has
// the fields, which the dispatcher's mint adds.
type confirmCredentials struct {
	TaskCredential string `json:"taskCredential,omitempty"`
	ProxyToken     string `json:"proxyToken,omitempty"`
}

// start runs the confirmed task through execute and returns where its
// outcome arrives. With a task credential, the task's events and input go
// on a task connection that reaches only that task; without one (a
// dispatcher with no signing key), on the pod connection.
func (w *profileWorker) start(ctx context.Context, h *held) <-chan turnOutcome {
	cfg := w.cfg
	cfg.TaskID = h.offer.TaskID
	// A copy: this task's additions must not land in the worker's slice.
	cfg.HarnessEnv = append([]string(nil), w.cfg.HarnessEnv...)
	h.done = make(chan turnOutcome, 1)
	bus, closeTaskBus := w.b, func() {}
	if h.creds.TaskCredential != "" {
		cfg.TaskStem = lib.TaskConsumerStem(cfg.PodName, cfg.TaskID)
		tb, closeTB, err := dialTaskBus(ctx, cfg, h.creds.TaskCredential)
		if err != nil {
			// Fail closed: nothing runs on the pod connection's wider
			// grants, renewals stop when the loop releases the task, and
			// the dispatcher fails it as lease-expired.
			w.cfg.Logger.Error("the task connection was refused; not running the task", "task", cfg.TaskID, "err", err)
			h.done <- turnOutcome{err: err}
			return h.done
		}
		bus, closeTaskBus = tb, closeTB
	}
	removeConfig := func() {}
	if w.persona != nil {
		dir, remove, err := w.taskConfigDir(cfg.TaskID)
		if err != nil {
			closeTaskBus()
			w.cfg.Logger.Error("could not write the task's persona config; not running the task", "task", cfg.TaskID, "err", err)
			h.done <- turnOutcome{err: err}
			return h.done
		}
		removeConfig = remove
		// The persona's flags (its tool allowlist and denylist, MCP and
		// setting sources) after the base argv, which carries none.
		cfg.HarnessCommand = append(append([]string(nil), cfg.HarnessCommand...), w.persona.Args...)
		cfg.HarnessCommandNoDelegate = nil
		cfg.HarnessEnv = append(cfg.HarnessEnv, "CLAUDE_CONFIG_DIR="+dir)
	}
	removeToken := w.writeTaskToken(h.creds.ProxyToken)
	// The worker stops short of the dispatcher's deadline, so its own
	// terminal and final renewal land first.
	cfg.TaskDeadline = time.Until(h.offer.Deadline) - w.pw.deadlineMargin
	cfg.HarnessEnv = append(cfg.HarnessEnv, taskParameterEnv(h.origin, cfg.Logger)...)
	a := newTaskAdapter(cfg, bus)
	a.dispatched = true
	a.abandon = h.abandon
	go func() {
		// Follow-ups and cancels arrive on the task's own .in; the
		// submission isn't there (it's on PROFILE_SUBMITS), so the reader
		// opens at the subject's start.
		out := a.execute(ctx, h.origin, 0, turnPlan{})
		// Cleaned up before the outcome is reported, so the final renewal,
		// and with it the next task, never overlaps this task's token or
		// config.
		removeConfig()
		removeToken()
		closeTaskBus()
		h.done <- out
	}()
	return h.done
}

// writeTaskToken writes the proxy token where the shims read it, for the
// task's lifetime, and returns what removes it. A task with no token (no
// target, or no signing key) leaves no file, so the broker gives its calls
// no cluster.
func (w *profileWorker) writeTaskToken(token string) func() {
	path := w.pw.TaskTokenFile
	if path == "" {
		return func() {}
	}
	_ = os.Remove(path)
	if token == "" {
		return func() {}
	}
	if err := os.WriteFile(path, []byte(token), 0o600); err != nil {
		w.cfg.Logger.Error("could not write the proxy token; the task's cluster calls will be refused", "path", path, "err", err)
		return func() {}
	}
	return func() { _ = os.Remove(path) }
}

// finish sends the final renewal for a task whose terminal went out. A task
// that ended without one (abandoned, or refused before any publish) gets
// none, and its lease settles the way the dispatcher decides.
func (w *profileWorker) finish(h *held, out turnOutcome) {
	if out.err != nil {
		w.cfg.Logger.Warn("the task ended with an error", "task", h.offer.TaskID, "err", out.err)
	}
	if out.res.State == "" || out.res.Abandoned {
		return
	}
	// Renewals stop before the final one, so Final is the last word.
	h.stopRenew()
	<-h.renewDone
	w.renew(h.offer.TaskID, true)
	if err := w.b.nc.Flush(); err != nil {
		w.cfg.Logger.Warn("flushing the final renewal", "task", h.offer.TaskID, "err", err)
	}
}

// taskParameterEnv is the submission's declared parameters as the harness
// reads them, PROFILE_TASK_<NAME>=value. A set that fails the bounds is
// dropped whole: the dispatcher already admitted the task on them, so this
// is a submission the worker can't trust to be the one admitted.
func taskParameterEnv(origin *lib.Envelope, log interface{ Warn(string, ...any) }) []string {
	var msg lib.Message
	if err := json.Unmarshal(origin.Payload, &msg); err != nil {
		return nil
	}
	params, ok, err := lib.TaskParametersOf(msg.Parts)
	if err != nil {
		log.Warn("the submission's task parameters are malformed; none are passed on", "task", origin.TaskID, "err", err)
		return nil
	}
	if !ok {
		return nil
	}
	env := make([]string, 0, len(params.Parameters))
	for name, value := range params.Parameters {
		env = append(env, "PROFILE_TASK_"+strings.ToUpper(name)+"="+value)
	}
	return env
}

// serveReadiness answers /readyz with 200 once the worker is listening, and
// /healthz always.
func (w *profileWorker) serveReadiness(addr string) (func(), error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, fmt.Errorf("readiness listener: %w", err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/readyz", func(rw http.ResponseWriter, _ *http.Request) {
		// Listening, and still connected: a pod that lost the bus can't
		// hear an offer, so it stops being Ready until it reconnects.
		if !w.ready.Load() || !w.b.nc.IsConnected() {
			http.Error(rw, "not listening", http.StatusServiceUnavailable)
			return
		}
		_, _ = rw.Write([]byte("ok"))
	})
	mux.HandleFunc("/healthz", func(rw http.ResponseWriter, _ *http.Request) { _, _ = rw.Write([]byte("ok")) })
	srv := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			w.cfg.Logger.Warn("readiness listener stopped", "err", err)
		}
	}()
	return func() { _ = srv.Close() }, nil
}

package workeradapter

// The session's tool-call capture: instrumentation for the evals, never
// published. The front door's activity part carries no arguments (the
// bridge's shape), so an eval that grades what a tool was called with reads
// the arguments here instead: every tool call a session pod's harness makes,
// whichever harness it is, as the driver decodes it (EventActivity), scrubbed
// and capped, held in this process's memory for the pod's life.
//
// It is in memory, not in a file, because the harness and every shell the
// agent runs share this process's uid: a file this process writes, the agent
// could rewrite, and an eval graded on it could be gamed. The bench reads it
// through a loopback port this process binds at start (TrajectoryAddr), which
// no other process in the pod can bind while this one lives, with the client
// in this binary (`worker-adapter trajectory <task>`, FetchTrajectory) run by
// kubectl exec. The port serves reads only. The agent can read its own
// session's scrubbed calls through it; it made them, so that hides nothing
// from it. Nothing here reaches the TASKS stream, a chat surface or the disk.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"slices"
	"sync"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib/answerscrub"
)

const (
	// TrajectoryCommand is the subcommand that prints a task's capture:
	// `worker-adapter trajectory <task id>`.
	TrajectoryCommand = "trajectory"
	// TrajectoryAddr is the capture's read port: loopback only, never the
	// pod IP. It is in no Service and no containerPort.
	TrajectoryAddr = "127.0.0.1:7346"
	// TrajectorySentinel heads the client's output, so a reader tells a
	// capture (even an empty one) from a failure to read one.
	TrajectorySentinel = "__SESSION_TRAJECTORY__"

	trajectoryPath = "/trajectory"
	// The caps, as the bridge route's worker trajectory has them: an input
	// past trajectoryInputCap bytes is kept as its head, a task keeps at most
	// trajectoryCallsCap calls and trajectoryTaskBytes bytes, and the store
	// keeps the last trajectoryTasksKept tasks within trajectoryStoreBytes.
	trajectoryInputCap  = 2000
	trajectoryInputHead = 1000
	// trajectoryScrubMax bounds what of one input is scrubbed at all: the
	// served head is far shorter, and scrubbing a harness line's full 8 MB
	// would hold the event loop for seconds.
	trajectoryScrubMax   = 64 << 10
	trajectoryToolMax    = 200
	trajectoryCallsCap   = 2000
	trajectoryTaskBytes  = 4 << 20
	trajectoryTasksKept  = 8
	trajectoryStoreBytes = 16 << 20
)

// TrajectoryCall is one captured tool call.
type TrajectoryCall struct {
	Seq   int             `json:"seq"`
	Tool  string          `json:"tool"`
	Input json.RawMessage `json:"input,omitempty"`
	At    time.Time       `json:"at"`
}

// TaskTrajectory is a task's capture as the port serves it. Known is false
// for a task this process holds nothing for: never seen, or evicted.
type TaskTrajectory struct {
	Task    string           `json:"task"`
	Known   bool             `json:"known"`
	Calls   []TrajectoryCall `json:"calls"`
	Dropped int              `json:"dropped,omitempty"`
}

type taskCapture struct {
	calls   []TrajectoryCall
	bytes   int
	dropped int
}

// TrajectoryStore holds the session's captured tool calls by task. The zero
// value is not usable; a nil store records nothing.
type TrajectoryStore struct {
	mu    sync.Mutex
	tasks map[string]*taskCapture
	order []string // task ids, oldest first
	bytes int
}

// NewTrajectoryStore is an empty capture.
func NewTrajectoryStore() *TrajectoryStore {
	return &TrajectoryStore{tasks: map[string]*taskCapture{}}
}

// Record captures one tool call of task: its input scrubbed (seen's values,
// then the inline shape rules, and the whole value of a key that looks like
// a secret) and capped. seen may be nil. Safe on a nil store.
func (s *TrajectoryStore) Record(task, tool string, input json.RawMessage, seen *answerscrub.Seen, at time.Time) {
	if s == nil || task == "" {
		return
	}
	in := capTrajectoryInput(scrubTrajectoryInput(input, seen))
	if len(tool) > trajectoryToolMax {
		tool = tool[:trajectoryToolMax]
	}
	size := len(tool) + len(in)
	s.mu.Lock()
	defer s.mu.Unlock()
	c := s.tasks[task]
	if c == nil {
		c = &taskCapture{}
		s.tasks[task] = c
		s.order = append(s.order, task)
		s.evictLocked(task)
	}
	if len(c.calls) >= trajectoryCallsCap || c.bytes+size > trajectoryTaskBytes {
		c.dropped++
		return
	}
	c.calls = append(c.calls, TrajectoryCall{Seq: len(c.calls) + c.dropped + 1, Tool: tool, Input: in, At: at.UTC()})
	c.bytes += size
	s.bytes += size
	s.evictLocked(task)
}

// evictLocked drops the oldest tasks past the store's caps, never current,
// the task being recorded.
func (s *TrajectoryStore) evictLocked(current string) {
	for (len(s.order) > trajectoryTasksKept || s.bytes > trajectoryStoreBytes) && len(s.order) > 1 {
		i := 0
		if s.order[0] == current {
			i = 1
		}
		id := s.order[i]
		s.bytes -= s.tasks[id].bytes
		delete(s.tasks, id)
		s.order = slices.Delete(s.order, i, i+1)
	}
}

// Task is task's capture as served.
func (s *TrajectoryStore) Task(task string) TaskTrajectory {
	out := TaskTrajectory{Task: task, Calls: []TrajectoryCall{}}
	if s == nil {
		return out
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if c := s.tasks[task]; c != nil {
		out.Known, out.Dropped = true, c.dropped
		out.Calls = slices.Clone(c.calls)
	}
	return out
}

// scrubTrajectoryInput scrubs a tool call's input in two passes. First every
// string value in it through answerscrub.Scrub (the seen values, when there
// are any, and the inline shape rules), with a secret-looking key's whole
// value replaced. Then the whole serialised input once more as one text,
// keys included, which catches what spans fields (a name and its value in an
// env list, a flag and its value in an argv array). If that second pass leaves
// text that is no longer JSON, the input is stored as Redacted. Input past
// trajectoryScrubMax, or that isn't JSON, is scrubbed as one string.
func scrubTrajectoryInput(raw json.RawMessage, seen *answerscrub.Seen) json.RawMessage {
	if len(raw) == 0 {
		return raw
	}
	asString := func(s string) json.RawMessage {
		out, _ := json.Marshal(answerscrub.Scrub(s, seen))
		return out
	}
	if len(raw) > trajectoryScrubMax {
		return asString(string(raw[:trajectoryScrubMax]))
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return asString(string(raw))
	}
	first, err := json.Marshal(scrubTrajectoryValue(v, seen))
	if err != nil {
		return redactedTrajectoryInput()
	}
	whole := answerscrub.Scrub(string(first), seen)
	if !json.Valid([]byte(whole)) {
		return redactedTrajectoryInput()
	}
	return json.RawMessage(whole)
}

func redactedTrajectoryInput() json.RawMessage {
	out, _ := json.Marshal(answerscrub.Redacted)
	return out
}

func scrubTrajectoryValue(v any, seen *answerscrub.Seen) any {
	switch t := v.(type) {
	case string:
		return answerscrub.Scrub(t, seen)
	case []any:
		for i := range t {
			t[i] = scrubTrajectoryValue(t[i], seen)
		}
		return t
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, e := range t {
			if answerscrub.SecretLookingKey(k) {
				out[k] = answerscrub.Redacted
				continue
			}
			out[k] = scrubTrajectoryValue(e, seen)
		}
		return out
	default:
		return v
	}
}

// capTrajectoryInput keeps an input past trajectoryInputCap bytes as its
// head, in the shape the bench reads as truncated.
func capTrajectoryInput(in json.RawMessage) json.RawMessage {
	if len(in) <= trajectoryInputCap {
		return in
	}
	out, _ := json.Marshal(map[string]any{"truncated": true, "bytes": len(in), "head": string(in[:trajectoryInputHead])})
	return out
}

// ServeTrajectory binds the capture's read port on addr, which must be a
// loopback address, and serves store from it until ctx ends. It returns the
// bound address. An error means the session runs with no capture.
func ServeTrajectory(ctx context.Context, store *TrajectoryStore, addr string, log *slog.Logger) (net.Addr, error) {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, err
	}
	if ip := net.ParseIP(host); ip == nil || !ip.IsLoopback() {
		return nil, fmt.Errorf("trajectory capture: %q is not a loopback address", addr)
	}
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, err
	}
	mux := http.NewServeMux()
	mux.HandleFunc(trajectoryPath, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", http.MethodGet)
			http.Error(w, "read only", http.StatusMethodNotAllowed)
			return
		}
		task := r.URL.Query().Get("task")
		if task == "" {
			http.Error(w, "task is required", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(store.Task(task))
	})
	srv := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: time.Minute}
	go func() {
		<-ctx.Done()
		_ = srv.Close()
	}()
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Warn("trajectory capture: the read port stopped; the session goes on without it", "err", err)
		}
	}()
	return ln.Addr(), nil
}

// FetchTrajectory prints task's capture from the port at addr: the sentinel
// line, then the capture as one JSON document. An error writes nothing, so a
// reader sees no sentinel and counts the capture unread.
func FetchTrajectory(ctx context.Context, addr, task string, w io.Writer) error {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+addr+trajectoryPath+"?task="+url.QueryEscape(task), nil)
	if err != nil {
		return err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("trajectory capture: %s", resp.Status)
	}
	limit := int64(trajectoryTaskBytes * 2)
	body, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return err
	}
	if int64(len(body)) > limit {
		return fmt.Errorf("trajectory capture: the answer is past %d bytes", limit)
	}
	_, err = fmt.Fprintf(w, "%s\n%s", TrajectorySentinel, body)
	return err
}

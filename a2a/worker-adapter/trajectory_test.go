package workeradapter

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/answerscrub"
)

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// The read port binds loopback and nothing else: an explicit 127.0.0.1
// address is bound as asked, and an address that isn't loopback (every
// interface, or a pod IP) is refused before anything listens.
func TestTheTrajectoryPortBindsLoopbackOnly(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	addr, err := ServeTrajectory(ctx, NewTrajectoryStore(), "127.0.0.1:0", quietLog())
	if err != nil {
		t.Fatal(err)
	}
	tcp, ok := addr.(*net.TCPAddr)
	if !ok || !tcp.IP.Equal(net.IPv4(127, 0, 0, 1)) {
		t.Fatalf("bound %v, want 127.0.0.1", addr)
	}
	for _, bad := range []string{"0.0.0.0:0", ":0", "10.0.0.5:0", "[::]:0", "localhost:0"} {
		if _, err := ServeTrajectory(ctx, NewTrajectoryStore(), bad, quietLog()); err == nil {
			t.Errorf("ServeTrajectory(%q) bound; want it refused", bad)
		}
	}
	if !strings.HasPrefix(TrajectoryAddr, "127.0.0.1:") {
		t.Fatalf("TrajectoryAddr = %q, want an explicit 127.0.0.1 address", TrajectoryAddr)
	}
}

// The port serves reads only, and a task id it holds nothing for reads as
// unknown, not as an empty capture.
func TestTheTrajectoryPortIsReadOnly(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store := NewTrajectoryStore()
	store.Record("task-a", "Bash", json.RawMessage(`{"command":"kubectl get pods"}`), nil, time.Now())
	addr, err := ServeTrajectory(ctx, store, "127.0.0.1:0", quietLog())
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range []string{http.MethodPost, http.MethodPut, http.MethodDelete, http.MethodPatch} {
		req, _ := http.NewRequest(m, "http://"+addr.String()+trajectoryPath+"?task=task-a", strings.NewReader(`{"calls":[]}`))
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusMethodNotAllowed {
			t.Fatalf("%s answered %d, want 405", m, resp.StatusCode)
		}
	}
	if got := store.Task("task-a"); len(got.Calls) != 1 {
		t.Fatalf("a write request changed the capture: %+v", got)
	}
	var buf bytes.Buffer
	if err := FetchTrajectory(ctx, addr.String(), "task-nobody", &buf); err != nil {
		t.Fatal(err)
	}
	head, body, _ := strings.Cut(buf.String(), "\n")
	var got TaskTrajectory
	if head != TrajectorySentinel || json.Unmarshal([]byte(body), &got) != nil || got.Known || len(got.Calls) != 0 {
		t.Fatalf("unknown task read as %q / %+v", head, got)
	}
}

// What the capture holds and serves never carries a seen value or a secret
// unscrubbed: a value the task saw, a value under a secret-looking key, and
// a secret in a command line are all redacted, while the command text a
// grader matches stays.
func TestTheTrajectoryNeverCarriesAnUnscrubbedSecret(t *testing.T) {
	const seenValue = "s3en-v4lue-from-a-tool-result-0f1e2d"
	const keyed = "hunter2-keyed-under-password"
	seen := &answerscrub.Seen{}
	seen.Add(seenValue)
	store := NewTrajectoryStore()
	store.Record("task-s", "Bash", json.RawMessage(fmt.Sprintf(`{"command":"gcloud container clusters list --format=%s","password":%q,"nested":[{"note":%q}]}`,
		seenValue, keyed, seenValue)), seen, time.Now())
	store.Record("task-s", "Bash", json.RawMessage(`"export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"`), seen, time.Now())

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	addr, err := ServeTrajectory(ctx, store, "127.0.0.1:0", quietLog())
	if err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	if err := FetchTrajectory(ctx, addr.String(), "task-s", &buf); err != nil {
		t.Fatal(err)
	}
	held, _ := json.Marshal(store.Task("task-s"))
	for name, out := range map[string]string{"held": string(held), "served": buf.String()} {
		for _, secret := range []string{seenValue, keyed, "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"} {
			if strings.Contains(out, secret) {
				t.Errorf("%s capture carries %q unscrubbed: %s", name, secret, out)
			}
		}
		if !strings.Contains(out, "gcloud container clusters list") {
			t.Errorf("%s capture lost the command text a grader matches: %s", name, out)
		}
	}
}

// The caps: an input past the per-input cap is kept as its head in the
// truncated shape; a task past its call cap counts what it dropped; the
// store keeps the last tasks and never evicts the task being recorded.
func TestTheTrajectoryCaps(t *testing.T) {
	store := NewTrajectoryStore()
	long := strings.Repeat("x", trajectoryInputCap*2)
	store.Record("task-big", "Write", json.RawMessage(fmt.Sprintf(`{"content":%q}`, long)), nil, time.Now())
	var cut map[string]any
	if err := json.Unmarshal(store.Task("task-big").Calls[0].Input, &cut); err != nil || cut["truncated"] != true {
		t.Fatalf("a long input = %v (%v), want the truncated shape", cut, err)
	}

	for i := 0; i < trajectoryCallsCap+3; i++ {
		store.Record("task-many", "Bash", json.RawMessage(`{"command":"true"}`), nil, time.Now())
	}
	if got := store.Task("task-many"); len(got.Calls) != trajectoryCallsCap || got.Dropped != 3 {
		t.Fatalf("calls %d dropped %d, want %d and 3", len(got.Calls), got.Dropped, trajectoryCallsCap)
	}

	store = NewTrajectoryStore()
	for i := 0; i < trajectoryTasksKept+2; i++ {
		store.Record(fmt.Sprintf("task-%d", i), "Bash", json.RawMessage(`{"command":"true"}`), nil, time.Now())
	}
	if store.Task("task-0").Known || store.Task("task-1").Known {
		t.Fatal("the oldest tasks were kept past the store's task cap")
	}
	if !store.Task(fmt.Sprintf("task-%d", trajectoryTasksKept+1)).Known {
		t.Fatal("the newest task was evicted")
	}

	// Past the store's bytes, the oldest task goes, but never the one being
	// recorded: an early task that records again after later tasks filled
	// the store keeps its capture, and the next-oldest is evicted instead.
	store = NewTrajectoryStore()
	store.Record("task-early", "Bash", json.RawMessage(`{"command":"true"}`), nil, time.Now())
	big := json.RawMessage(fmt.Sprintf(`{"c":%q}`, strings.Repeat("y", trajectoryInputCap-20)))
	for _, id := range []string{"task-f1", "task-f2", "task-f3", "task-f4"} {
		for len(store.Task(id).Calls) < trajectoryCallsCap && store.Task(id).Dropped == 0 {
			store.Record(id, "Bash", big, nil, time.Now())
		}
	}
	// The early task is the oldest and the one recording: its own calls push
	// the store over its bytes.
	for i := 0; i < trajectoryCallsCap && store.Task("task-f1").Known && store.Task("task-early").Known; i++ {
		store.Record("task-early", "Bash", big, nil, time.Now())
	}
	if !store.Task("task-early").Known {
		t.Fatal("the task being recorded was evicted")
	}
	if store.Task("task-f1").Known {
		t.Fatal("the store kept every task past its byte cap")
	}
	if store.bytes > trajectoryStoreBytes {
		t.Fatalf("store holds %d bytes, past its cap %d", store.bytes, trajectoryStoreBytes)
	}
}

// A nil store records nothing and serves an unknown task, so a session
// whose port couldn't be bound runs as before.
func TestANilTrajectoryStoreIsInert(t *testing.T) {
	var store *TrajectoryStore
	store.Record("task-x", "Bash", json.RawMessage(`{}`), nil, time.Now())
	if got := store.Task("task-x"); got.Known {
		t.Fatalf("nil store = %+v", got)
	}
}

// The adapter records each activity the harness emits, as the driver
// decodes it, into the session's capture, scrubbed.
func TestTheAdapterCapturesEachActivity(t *testing.T) {
	url := startServer(t)
	const session, taskID = "chat-otter-c0p1", "task-capture-1"
	submit(t, testClient(t, url), session, taskID, "list the clusters")
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-cap"}'
read first || exit 1
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"gcloud container clusters list","token":"tok-should-not-survive"}}]}}'
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"/scratch/notes.md"}}]}}'
echo '{"type":"result","subtype":"success","result":"two clusters"}'
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Trajectory = NewTrajectoryStore()
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	got := cfg.Trajectory.Task(taskID)
	if !got.Known || len(got.Calls) != 2 || got.Calls[0].Tool != "Bash" || got.Calls[1].Tool != "Read" {
		t.Fatalf("capture = %+v", got)
	}
	in := string(got.Calls[0].Input)
	if !strings.Contains(in, "gcloud container clusters list") || strings.Contains(in, "tok-should-not-survive") {
		t.Fatalf("first call's input = %s", in)
	}
}

// A secret that spans fields only shows as one in the whole input: a flag
// and its value as two argv entries, a name and its value as an env entry,
// and a key that is itself a token. The whole-input pass scrubs them, and
// what it leaves is still JSON.
func TestTheTrajectoryScrubsSecretsThatSpanFields(t *testing.T) {
	cases := map[string]struct{ in, secret string }{
		"argv flag and value":  {`{"argv":["mysql","--password","argvP4ssw0rdValue"]}`, "argvP4ssw0rdValue"},
		"env name and value":   {`{"env":[{"name":"DB_PASSWORD","value":"envP4ssw0rdValue"}]}`, "envP4ssw0rdValue"},
		"token as a key":       {`{"ghp_abcdefghijklmnopqrstuvwx0123":"x"}`, "ghp_abcdefghijklmnopqrstuvwx0123"},
		"argv in a long input": {`{"argv":["mysql","--password","longP4ssw0rdValue"],"pad":"` + strings.Repeat("z", trajectoryInputCap*2) + `"}`, "longP4ssw0rdValue"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			store := NewTrajectoryStore()
			store.Record("task-x", "Bash", json.RawMessage(c.in), nil, time.Now())
			got := store.Task("task-x").Calls[0].Input
			if strings.Contains(string(got), c.secret) {
				t.Fatalf("input kept %q: %s", c.secret, got)
			}
			if !json.Valid(got) {
				t.Fatalf("stored input isn't JSON: %s", got)
			}
		})
	}
}

// An input past the scrub bound is scrubbed as text, so a secret early in a
// huge input still doesn't survive into the head that's kept.
func TestTheTrajectoryScrubsALongInput(t *testing.T) {
	const secret = "ghp_abcdefghijklmnopqrstuvwx0123"
	in := fmt.Sprintf(`{"content":"token %s %s"}`, secret, strings.Repeat("z", trajectoryScrubMax*2))
	store := NewTrajectoryStore()
	store.Record("task-l", "Write", json.RawMessage(in), nil, time.Now())
	got := string(store.Task("task-l").Calls[0].Input)
	if strings.Contains(got, secret) || !strings.Contains(got, `"truncated":true`) {
		t.Fatalf("long input = %.300s", got)
	}
}

// Seq numbers each call of a task in order, counting the dropped ones, and a
// tool name past its cap is cut.
func TestTheTrajectorySeqAndToolCap(t *testing.T) {
	store := NewTrajectoryStore()
	store.Record("task-q", "Bash", json.RawMessage(`{}`), nil, time.Now())
	store.Record("task-q", strings.Repeat("T", trajectoryToolMax*3), json.RawMessage(`{}`), nil, time.Now())
	got := store.Task("task-q").Calls
	if got[0].Seq != 1 || got[1].Seq != 2 {
		t.Fatalf("seqs %d %d, want 1 2", got[0].Seq, got[1].Seq)
	}
	if len(got[1].Tool) != trajectoryToolMax {
		t.Fatalf("tool name kept %d bytes, want %d", len(got[1].Tool), trajectoryToolMax)
	}
}

// The client prints nothing for an answer past its limit, so a reader never
// takes a cut capture for a whole one.
func TestTheTrajectoryClientRefusesACutAnswer(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.CopyN(w, zeros{}, int64(trajectoryTaskBytes*2)+10)
	})}
	go srv.Serve(ln)
	defer srv.Close()
	var buf bytes.Buffer
	if err := FetchTrajectory(context.Background(), ln.Addr().String(), "task-big", &buf); err == nil {
		t.Fatal("an oversized answer was accepted")
	}
	if buf.Len() != 0 {
		t.Fatalf("wrote %d bytes on an error", buf.Len())
	}
}

type zeros struct{}

func (zeros) Read(p []byte) (int, error) {
	for i := range p {
		p[i] = '0'
	}
	return len(p), nil
}

// A long input is decoded like a short one before it's scrubbed: a Secret's
// stringData in a Write's content, whose line breaks are JSON escapes, is
// scrubbed in the kept head, and the head is the input's text, not a JSON
// string of it. "bytes" is the input's size as sent.
func TestALongInputIsScrubbedAsDecodedJSON(t *testing.T) {
	const secret = "s3cret-app-conf-value-9x8y"
	content := "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app\nstringData:\n  app.conf: " + secret + "\n" + strings.Repeat("# pad\n", trajectoryScrubMax/3)
	raw, _ := json.Marshal(map[string]string{"file_path": "/tmp/s.yaml", "content": content})
	store := NewTrajectoryStore()
	store.Record("task-y", "Write", raw, nil, time.Now())
	var got struct {
		Truncated bool   `json:"truncated"`
		Bytes     int    `json:"bytes"`
		Head      string `json:"head"`
	}
	if err := json.Unmarshal(store.Task("task-y").Calls[0].Input, &got); err != nil || !got.Truncated {
		t.Fatalf("long input = %s (%v)", store.Task("task-y").Calls[0].Input, err)
	}
	if strings.Contains(got.Head, secret) {
		t.Fatalf("the Secret's value survived in the head: %q", got.Head)
	}
	if !strings.HasPrefix(got.Head, `{"content":"apiVersion: v1`) {
		t.Fatalf("head isn't the input's own text: %q", got.Head[:60])
	}
	if got.Bytes != len(raw) {
		t.Fatalf("bytes = %d, want the input's %d", got.Bytes, len(raw))
	}
}

// Numbers are kept as written, and no cut splits a rune: a 64-bit id
// survives exactly, and a head or tool name cut beside a multi-byte
// character stays valid UTF-8 with no replacement character.
func TestTheTrajectoryKeepsNumbersAndRunes(t *testing.T) {
	store := NewTrajectoryStore()
	store.Record("task-n", "Bash", json.RawMessage(`{"id":9007199254740993}`), nil, time.Now())
	if got := string(store.Task("task-n").Calls[0].Input); got != `{"id":9007199254740993}` {
		t.Fatalf("number input = %s", got)
	}
	for pad := 0; pad < 4; pad++ {
		raw, _ := json.Marshal(map[string]string{"c": strings.Repeat("a", pad) + strings.Repeat("é", trajectoryInputCap)})
		store.Record("task-n", strings.Repeat("b", pad)+strings.Repeat("工", trajectoryToolMax), raw, nil, time.Now())
	}
	for _, c := range store.Task("task-n").Calls[1:] {
		var cut struct{ Head string }
		_ = json.Unmarshal(c.Input, &cut)
		for name, s := range map[string]string{"head": cut.Head, "tool": c.Tool} {
			if !utf8.ValidString(s) || strings.ContainsRune(s, utf8.RuneError) {
				t.Fatalf("%s split a rune: %q", name, s)
			}
		}
	}
}

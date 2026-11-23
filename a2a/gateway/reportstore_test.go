package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// reportKV is the rig's session-state bucket, for seeding and reading rows
// directly.
func reportKV(t *testing.T, r *rig) jetstream.KeyValue {
	t.Helper()
	kv, err := r.client.KV(context.Background(), lib.SessionStateBucket)
	if err != nil {
		t.Fatal(err)
	}
	return kv
}

// seedReportRow writes rec at its address as raw JSON, as another writer
// (or another version) would.
func seedReportRow(t *testing.T, kv jetstream.KeyValue, rec lib.ReportRecord) string {
	t.Helper()
	raw, err := json.Marshal(rec)
	if err != nil {
		t.Fatal(err)
	}
	key := lib.ReportKey(rec.ChatID, rec.ThreadID)
	if _, err := kv.Put(context.Background(), key, raw); err != nil {
		t.Fatal(err)
	}
	return key
}

func readReportRow(t *testing.T, kv jetstream.KeyValue, key string) (lib.ReportRecord, bool) {
	t.Helper()
	entry, err := kv.Get(context.Background(), key)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return lib.ReportRecord{}, false
	}
	if err != nil {
		t.Fatal(err)
	}
	var rec lib.ReportRecord
	if err := json.Unmarshal(entry.Value(), &rec); err != nil {
		t.Fatal(err)
	}
	return rec, true
}

// A gateway that predates report rows lists "sessions.>" and watches it,
// and reads exact keys: report rows in the bucket are neither returned,
// decoded as sessions, deleted nor an error on the registry listing, the
// busy watch or the reap pass, whose code here is the old gateways'.
func TestAnOldGatewaysSessionPathsIgnoreReportRows(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	kv := reportKV(t, r)
	// Rows shaped to decode as a session record if one were read: a key,
	// an addressee and an active task.
	var keys []string
	for i := 0; i < 3; i++ {
		raw := fmt.Sprintf(`{"text":"r%d","source":"relay","posted_at":"2026-01-01T00:00:00Z","conversation":"slack:C/%d.1","root":"","chat_id":"C","thread_id":"%d.1","key":"slack:C/%d.1","addressee":"platform","activeTask":{"taskId":"t-%d"}}`, i, i, i, i, i)
		key := lib.ReportKey("C", fmt.Sprintf("%d.1", i))
		if _, err := kv.Put(ctx, key, []byte(raw)); err != nil {
			t.Fatal(err)
		}
		keys = append(keys, key)
	}
	if err := r.g.reg.Put(ctx, &SessionRecord{Key: "discord:g/only", Addressee: "platform"}); err != nil {
		t.Fatal(err)
	}
	recs, err := r.g.reg.Sessions(ctx)
	if err != nil {
		t.Fatalf("Sessions: %v", err)
	}
	if len(recs) != 1 || recs[0].Key != "discord:g/only" {
		t.Fatalf("Sessions = %+v, want the one session record", recs)
	}
	seen := 0
	if err := r.g.reg.eachSessionRecord(ctx, func(rec *SessionRecord) {
		seen++
		if rec.Key != "discord:g/only" {
			t.Errorf("busy watch read %q", rec.Key)
		}
	}); err != nil {
		t.Fatalf("busy watch: %v", err)
	}
	if seen != 1 {
		t.Fatalf("busy watch read %d records, want 1", seen)
	}
	if n, err := r.g.fixedRouteBacklog(ctx, ""); err != nil || n != 0 {
		t.Fatalf("busy count = %d %v, want 0 and no error", n, err)
	}
	r.g.reapOnce(ctx)
	for _, key := range keys {
		if _, err := kv.Get(ctx, key); err != nil {
			t.Fatalf("report row %s after the reap pass: %v", key, err)
		}
	}
}

// With nothing in the bucket a gateway storing reports reads as one
// keeping them in memory alone: nothing for a thread or a root nothing was
// posted on, and memory's numbering and forgetting as before.
func TestAnEmptyBucketReadsAsMemoryAlone(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	g := r.g
	const thread = "slack:C9/1800.1"
	if got, n, ok := g.relayedReport(&SessionRecord{Key: thread}, ""); ok || n != 0 {
		t.Fatalf("empty bucket: %+v %d", got, n)
	}
	console := &SessionRecord{Key: "console:tab-e", Tasks: []TaskRef{{ID: "task-a"}}}
	if _, _, ok := g.relayedReport(console, "task-a"); ok {
		t.Fatal("empty bucket: a console reply found a report")
	}
	g.reports.put(thread, "", "the first card's report")
	g.reports.put(thread, "", "the second card's report")
	got, seq, ok := g.relayedReport(&SessionRecord{Key: thread}, "")
	if !ok || got.Text != "the second card's report" {
		t.Fatalf("relayedReport = %+v %v", got, ok)
	}
	g.reports.forgetThrough(thread, seq)
	if _, _, ok := g.relayedReport(&SessionRecord{Key: thread}, ""); ok {
		t.Fatal("the thread's report came back after the turn carried it")
	}
}

// A report kept by one gateway is carried by the reply under it after the
// gateway restarts: a thread's newest report once, by the thread's next
// opening turn, and a console report by every reply naming its root.
func TestAStoredReportOutlivesARestart(t *testing.T) {
	t.Parallel()
	r, _ := startRigWithSpawnerCap(t, "platform", 0, nil)
	const thread = "slack:C1/1900.1"
	r.g.reports.put(thread, "", "an older card's report")
	r.g.reports.put(thread, "task-x", "card report: np-quartz-7 is at 92%")
	r.g.reports.put("console:tab-rs", "task-root", "the console report")
	r.g.reports.put("console:tab-rs", "task-two", "another console report")

	r2, _ := restartRig(t, r)
	console := &SessionRecord{Key: "console:tab-rs", Tasks: []TaskRef{
		{ID: "task-root"},
		{ID: "task-child", Role: taskRoleChild, ParentTaskID: "task-root", RootTaskID: "task-root"},
	}}
	for i := 0; i < 2; i++ {
		got, n, ok := r2.g.relayedReport(console, "task-child")
		if !ok || n != 0 || got != (lib.PushedReport{TaskID: "task-root", Text: "the console report"}) {
			t.Fatalf("console read %d after the restart: %+v %d %v", i, got, n, ok)
		}
	}
	if _, _, ok := r2.g.relayedReport(console, "task-other"); ok {
		t.Fatal("a root nothing was posted on found a stored report")
	}

	busyTurn(r2, thread, "slack", "and the other pool?")
	origin := r2.awaitTask(t, "platform")
	got, found, err := lib.PushedReportOf(messageParts(t, origin))
	if err != nil || !found || got != (lib.PushedReport{Text: "card report: np-quartz-7 is at 92%"}) {
		t.Fatalf("pushedReport part after the restart = %+v found=%v err=%v, want the thread's newest report", got, found, err)
	}
	// The turn is on the bus: this process does not carry it again, though
	// the row stays for the next process.
	if _, _, ok := r2.g.relayedReport(&SessionRecord{Key: thread}, ""); ok {
		t.Fatal("the stored report was read back after the turn carried it")
	}
	addr, _ := lib.ReportThreadAddress(thread)
	if rec, ok := readReportRow(t, reportKV(t, r2), addr.Key()); !ok || rec.Text != "card report: np-quartz-7 is at 92%" || rec.Root != "task-x" || rec.Source != lib.ReportSourceRelay || rec.Conversation != thread {
		t.Fatalf("the thread's row after the turn: %+v %v, want it kept", rec, ok)
	}
	// A report posted since is carried as before.
	r2.g.reports.put(thread, "", "a newer card's report")
	if got, _, ok := r2.g.relayedReport(&SessionRecord{Key: thread}, ""); !ok || got.Text != "a newer card's report" {
		t.Fatalf("a newer report: %+v %v", got, ok)
	}
}

// fakeReports is a reportBackend in memory, for the pushedReports logic
// without a bus.
type fakeReports struct {
	rows  map[string]lib.ReportRecord
	reads atomic.Int32
	fail  error
}

func (f *fakeReports) PutReport(_ context.Context, rec *lib.ReportRecord) error {
	if f.fail != nil {
		return f.fail
	}
	if f.rows == nil {
		f.rows = map[string]lib.ReportRecord{}
	}
	f.rows[lib.ReportKey(rec.ChatID, rec.ThreadID)] = *rec
	return nil
}

func (f *fakeReports) GetReport(_ context.Context, addr lib.ReportAddress) (*lib.ReportRecord, error) {
	f.reads.Add(1)
	if f.fail != nil {
		return nil, f.fail
	}
	rec, ok := f.rows[addr.Key()]
	if !ok {
		return nil, nil
	}
	return &rec, nil
}

// A new process (empty memory, same rows) carries a thread's stored report
// once; a steer forgets even what only the bucket holds; a report the
// bounds evicted from memory is read back.
func TestStoredThreadReportsAndWhatThisProcessForgot(t *testing.T) {
	store := &fakeReports{}
	clock := time.Date(2026, 11, 23, 9, 0, 0, 0, time.UTC)
	tick := func() time.Time { clock = clock.Add(time.Second); return clock }
	first := &pushedReports{durable: store, now: tick}
	const thread = "gchat:spaces/A/threads/B"
	first.put(thread, "", "the report")

	second := &pushedReports{durable: store, now: tick}
	text, seq, ok := second.latestOrStored(thread)
	if !ok || text != "the report" || seq == 0 {
		t.Fatalf("after a restart: %q %d %v", text, seq, ok)
	}
	if again, seq2, _ := second.latestOrStored(thread); again != "the report" || seq2 != seq {
		t.Fatalf("a second read before the forget = %q %d, want the same report and number", again, seq2)
	}
	second.forgetThrough(thread, seq)
	if _, _, ok := second.latestOrStored(thread); ok {
		t.Fatal("read back after the turn carried it")
	}

	third := &pushedReports{durable: store, now: tick}
	third.forget(thread)
	if _, _, ok := third.latestOrStored(thread); ok {
		t.Fatal("a steer did not forget the report only the bucket held")
	}
	third.put(thread, "", "posted after the steer")
	third.mu.Lock()
	third.forgetLocked(thread) // as the bounds evict it
	third.mu.Unlock()
	if text, _, ok := third.latestOrStored(thread); !ok || text != "posted after the steer" {
		t.Fatalf("an evicted report posted after the steer: %q %v", text, ok)
	}

	// A failed read is none, and a failed write keeps memory.
	broken := &pushedReports{durable: &fakeReports{fail: errors.New("bus down")}, now: tick}
	if _, _, ok := broken.latestOrStored(thread); ok {
		t.Fatal("a failed read found a report")
	}
	broken.put(thread, "", "kept in memory")
	if text, _, ok := broken.latestOrStored(thread); !ok || text != "kept in memory" {
		t.Fatalf("after a failed write: %q %v", text, ok)
	}
}

// What is stored where: a thread at its address, a console report at
// (key, key#root), and nothing for a whole conversation, a Slack DM
// thread, or a report on no task outside a thread. A memory hit reads
// nothing from the bucket.
func TestWhatTheRelayStores(t *testing.T) {
	store := &fakeReports{}
	p := &pushedReports{durable: store}
	p.put("slack:C1/1.1", "task-a", "thread report")
	p.put("console:tab", "task-r", "console report")
	p.put("console:tab", "", "no task")
	p.put("gchat:space/spaces/A", "task-b", "whole space")
	p.put("slack:dm/D1/1.1", "", "dm thread")
	if len(store.rows) != 2 {
		t.Fatalf("rows = %+v, want two", store.rows)
	}
	if rec := store.rows[lib.ReportKey("C1", "1.1")]; rec.Text != "thread report" || rec.Root != "task-a" {
		t.Fatalf("thread row = %+v", rec)
	}
	if rec := store.rows[lib.ReportKey("console:tab", "console:tab#task-r")]; rec.Text != "console report" || rec.Root != "task-r" || rec.Source != lib.ReportSourceRelay || rec.PostedAt.IsZero() {
		t.Fatalf("console row = %+v", rec)
	}
	p.latestOrStored("slack:C1/1.1")
	p.byRootOrStored("console:tab", "task-r")
	if n := store.reads.Load(); n != 0 {
		t.Fatalf("memory hits read the bucket %d times", n)
	}
	// A thread reply naming a task reads the thread's row only when its
	// newest report was on that task.
	fresh := &pushedReports{durable: store}
	if text, ok := fresh.byRootOrStored("slack:C1/1.1", "task-a"); !ok || text != "thread report" {
		t.Fatalf("thread row by its root: %q %v", text, ok)
	}
	if _, ok := fresh.byRootOrStored("slack:C1/1.1", "task-z"); ok {
		t.Fatal("thread row read by another root")
	}
}

// A writer replaces only its own source's rows, and never with an older
// report than the stored one.
func TestPutReportReplacesOnlyItsOwnSource(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	kv := reportKV(t, r)
	at := time.Date(2026, 11, 23, 9, 0, 0, 0, time.UTC)
	foreign := lib.ReportRecord{Text: "scheduled", Source: "a2a-schedule", PostedAt: at, ChatID: "C2", ThreadID: "2.2"}
	key := seedReportRow(t, kv, foreign)
	relay := lib.ReportRecord{Text: "relayed", Source: lib.ReportSourceRelay, PostedAt: at.Add(time.Minute), ChatID: "C2", ThreadID: "2.2"}
	if err := r.g.reg.PutReport(ctx, &relay); !errors.Is(err, errReportForeign) {
		t.Fatalf("over another source's row = %v, want errReportForeign", err)
	}
	if rec, _ := readReportRow(t, kv, key); rec.Text != "scheduled" {
		t.Fatalf("the foreign row was replaced: %+v", rec)
	}
	// Through the relay: memory keeps it, the row stands.
	r.g.reports.put("slack:C2/2.2", "", "relayed through put")
	if text, _, ok := r.g.reports.latest("slack:C2/2.2"); !ok || text != "relayed through put" {
		t.Fatalf("memory after a refused write: %q %v", text, ok)
	}
	if rec, _ := readReportRow(t, kv, key); rec.Text != "scheduled" {
		t.Fatalf("put replaced the foreign row: %+v", rec)
	}

	own := lib.ReportRecord{Text: "v1", Source: lib.ReportSourceRelay, PostedAt: at, ChatID: "C3", ThreadID: "3.3"}
	if err := r.g.reg.PutReport(ctx, &own); err != nil {
		t.Fatal(err)
	}
	newer := own
	newer.Text, newer.PostedAt = "v2", at.Add(time.Second)
	if err := r.g.reg.PutReport(ctx, &newer); err != nil {
		t.Fatalf("over its own row: %v", err)
	}
	older := own
	older.Text, older.PostedAt = "v0", at.Add(-time.Second)
	if err := r.g.reg.PutReport(ctx, &older); err != nil {
		t.Fatalf("an older report: %v", err)
	}
	if got, err := r.g.reg.GetReport(ctx, lib.ReportAddress{ChatID: "C3", ThreadID: "3.3"}); err != nil || got == nil || got.Text != "v2" {
		t.Fatalf("row = %+v %v, want v2 to stand", got, err)
	}
	// A row naming another address is read as none.
	raw, _ := json.Marshal(lib.ReportRecord{Text: "x", Source: lib.ReportSourceRelay, ChatID: "elsewhere", ThreadID: "y"})
	if _, err := kv.Put(ctx, lib.ReportKey("C4", "4.4"), raw); err != nil {
		t.Fatal(err)
	}
	if got, err := r.g.reg.GetReport(ctx, lib.ReportAddress{ChatID: "C4", ThreadID: "4.4"}); err != nil || got != nil {
		t.Fatalf("a row naming another address = %+v %v", got, err)
	}
}

// A deposed gateway's report write is refused: by the term counter a newer
// term raised, and by the row a newer term wrote between the deposed
// gateway's counter read and its write. The step-down hook runs.
func TestAReportWriteFromAStaleTermIsFenced(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	old, cur := NewRegistry(r.client), NewRegistry(r.client)
	var steppedDown atomic.Int32
	old.SetOnFenced(func() { steppedDown.Add(1) })
	if _, err := old.BeginTerm(ctx); err != nil {
		t.Fatal(err)
	}
	at := time.Date(2026, 11, 23, 9, 0, 0, 0, time.UTC)
	rec := func(text string, d time.Duration) *lib.ReportRecord {
		return &lib.ReportRecord{Text: text, Source: lib.ReportSourceRelay, PostedAt: at.Add(d), ChatID: "C5", ThreadID: "5.5"}
	}
	if err := old.PutReport(ctx, rec("term 1", 0)); err != nil {
		t.Fatalf("the leader's write: %v", err)
	}
	if _, err := cur.BeginTerm(ctx); err != nil {
		t.Fatal(err)
	}
	if err := old.PutReport(ctx, rec("deposed", time.Second)); !errors.Is(err, ErrFenced) {
		t.Fatalf("after a newer term began = %v, want ErrFenced", err)
	}
	if steppedDown.Load() != 1 {
		t.Fatalf("step-down hook ran %d times, want 1", steppedDown.Load())
	}
	got, _ := cur.GetReport(ctx, lib.ReportAddress{ChatID: "C5", ThreadID: "5.5"})
	if got == nil || got.Text != "term 1" || got.Epoch != 1 {
		t.Fatalf("row = %+v, want term 1's", got)
	}

	// The race: the deposed gateway's counter read passes, then the new
	// term begins and writes the row before the deposed write lands.
	stale, fresh := NewRegistry(r.client), NewRegistry(r.client)
	if _, err := stale.BeginTerm(ctx); err != nil {
		t.Fatal(err)
	}
	once := false
	stale.afterTermRead = func() {
		if once {
			return
		}
		once = true
		if _, err := fresh.BeginTerm(ctx); err != nil {
			t.Error(err)
		}
		if err := fresh.PutReport(ctx, rec("new leader", 2*time.Second)); err != nil {
			t.Errorf("the new leader's write: %v", err)
		}
	}
	if err := stale.PutReport(ctx, rec("stale", 3*time.Second)); !errors.Is(err, ErrFenced) {
		t.Fatalf("over a newer term's row = %v, want ErrFenced", err)
	}
	got, _ = fresh.GetReport(ctx, lib.ReportAddress{ChatID: "C5", ThreadID: "5.5"})
	if got == nil || got.Text != "new leader" {
		t.Fatalf("row = %+v, want the new leader's", got)
	}

	// The prune is fenced as well: a deposed gateway deletes nothing.
	kv := reportKV(t, r)
	oldKey := seedReportRow(t, kv, lib.ReportRecord{Text: "ancient", Source: lib.ReportSourceRelay, PostedAt: at.Add(-30 * 24 * time.Hour), ChatID: "C6", ThreadID: "6.6"})
	if _, err := old.PruneReports(ctx, at); !errors.Is(err, ErrFenced) {
		t.Fatalf("a deposed prune = %v, want ErrFenced", err)
	}
	if _, ok := readReportRow(t, kv, oldKey); !ok {
		t.Fatal("a deposed prune deleted a row")
	}
}

// The prune deletes rows past the retention and rows that do not parse,
// then the oldest until the rest are within the byte budget.
func TestPruneReportsByAgeAndBytes(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	kv := reportKV(t, r)
	now := time.Date(2026, 11, 23, 9, 0, 0, 0, time.UTC)
	aged := seedReportRow(t, kv, lib.ReportRecord{Text: "aged", Source: lib.ReportSourceRelay, PostedAt: now.Add(-reportRetention - time.Minute), ChatID: "A", ThreadID: "1"})
	young := seedReportRow(t, kv, lib.ReportRecord{Text: "young", Source: lib.ReportSourceRelay, PostedAt: now.Add(-reportRetention + time.Hour), ChatID: "A", ThreadID: "2"})
	if _, err := kv.Put(ctx, lib.ReportKeyPrefix+"garbage", []byte("not json")); err != nil {
		t.Fatal(err)
	}
	if err := r.g.reg.Put(ctx, &SessionRecord{Key: "discord:g/kept", Addressee: "platform"}); err != nil {
		t.Fatal(err)
	}
	n, err := r.g.reg.PruneReports(ctx, now)
	if err != nil || n != 2 {
		t.Fatalf("prune = %d %v, want the aged row and the garbage", n, err)
	}
	if _, ok := readReportRow(t, kv, aged); ok {
		t.Fatal("the aged row survived")
	}
	if _, ok := readReportRow(t, kv, young); !ok {
		t.Fatal("the young row was pruned")
	}
	if rec, err := r.g.reg.Get(ctx, "discord:g/kept"); err != nil || rec == nil {
		t.Fatalf("the prune touched a session record: %+v %v", rec, err)
	}

	// Past the byte budget the oldest go first.
	text := strings.Repeat("x", lib.PushedReportTextCap)
	rows := reportStoreBytesMax/lib.PushedReportTextCap + 3
	var keys []string
	for i := 0; i < rows; i++ {
		keys = append(keys, seedReportRow(t, kv, lib.ReportRecord{Text: text, Source: lib.ReportSourceRelay, PostedAt: now.Add(time.Duration(i) * time.Second), ChatID: "B", ThreadID: fmt.Sprint(i)}))
	}
	n, err = r.g.reg.PruneReports(ctx, now.Add(time.Hour))
	if err != nil || n < 4 {
		t.Fatalf("prune past the budget = %d %v, want at least the young row and three of the oldest", n, err)
	}
	if _, ok := readReportRow(t, kv, young); ok {
		t.Fatal("the oldest row outlived the budget")
	}
	if _, ok := readReportRow(t, kv, keys[0]); ok {
		t.Fatal("the oldest full row outlived the budget")
	}
	if _, ok := readReportRow(t, kv, keys[len(keys)-1]); !ok {
		t.Fatal("the newest row was pruned")
	}
	total := 0
	lister, err := kv.ListKeysFiltered(ctx, lib.ReportKeysFilter)
	if err != nil {
		t.Fatal(err)
	}
	for key := range lister.Keys() {
		entry, err := kv.Get(ctx, key)
		if err != nil {
			t.Fatal(err)
		}
		total += len(entry.Value())
	}
	if total > reportStoreBytesMax {
		t.Fatalf("rows hold %d bytes after the prune, over %d", total, reportStoreBytesMax)
	}
}

// Past the row bound the oldest go first, so a pass always reads every row.
func TestPruneReportsByRows(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	kv := reportKV(t, r)
	now := time.Date(2026, 11, 23, 9, 0, 0, 0, time.UTC)
	var keys []string
	for i := 0; i < reportStoreRowsMax+2; i++ {
		keys = append(keys, seedReportRow(t, kv, lib.ReportRecord{Text: "r", Source: lib.ReportSourceRelay, PostedAt: now.Add(time.Duration(i) * time.Second), ChatID: "R", ThreadID: fmt.Sprint(i)}))
	}
	n, err := r.g.reg.PruneReports(ctx, now.Add(time.Hour))
	if err != nil || n != 2 {
		t.Fatalf("prune = %d %v, want the two oldest", n, err)
	}
	for i, key := range keys {
		_, ok := readReportRow(t, kv, key)
		if want := i >= 2; ok != want {
			t.Fatalf("row %d kept=%v, want %v", i, ok, want)
		}
	}
}

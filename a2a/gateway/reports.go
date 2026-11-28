package gateway

import (
	"context"
	"log/slog"
	"slices"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The reports the notify route posted into console conversations and
// into chat threads, kept so a reply under one carries the report to the
// executor (lib.PushedReportPart). A console conversation holds many
// chains, so its reports are kept under the root of the task each reported
// on and a reply names the one it is under, as often as it likes; a chat
// thread (a thread-keyed conversation, lib.ThreadKeyedConversation) names
// no task, so its reports are kept under the key alone and the next human
// turn that opens in the thread carries the newest, after which the gateway
// forgets the thread's reports (startTaskWith, once the submission is on
// the bus; steerTask, when a message joins the thread's running task
// instead): the executor's session keeps what it was shown, and a later
// turn is as likely to be about the next report, or the alert the thread
// hangs under, as about this one; the executor's own lookup may still serve
// it, as before this relay (a2a/docs/hermes-bridge.md).
//
// Process memory is the first read, as notifyUpdates is, and bounded. Each
// report is also written to the session-state bucket (reportstore.go), so
// it outlives the process: a thread's newest report, at the thread's
// address (lib.ReportThreadAddress), and a console report under
// (conversation, conversation#root). A read memory has nothing for falls
// back to the bucket (latestOrStored, byRootOrStored), which is how a reply
// after a restart or a failover still carries its report; a row is kept for
// reportRetention, the session-kv incident row's life.
//
// A thread keeps its newest report in the bucket after the next turn
// carried it; what stops a later turn in the same process from carrying it
// again is memory's note of what it forgot (consumed): a row posted no later
// than the newest report this process forgot for the thread is not read
// back. A new process has no such note, so the first turn in a thread after
// a restart carries the thread's newest report once, as the bridge's own
// lookup serves it to every reply there. A Slack DM thread has no thread
// address, so its reports stay in memory alone, as before.
//
// A thread on the session-pod route (SessionRecord.SessionRouted) is the
// exception to the once: every opening human turn there carries the
// thread's newest report kept within reportRetention (current), memory
// first and then the bucket, and neither the turn nor a steer forgets it,
// which is what the hermes-bridge does for every reply on the platform
// route (attachPushedReport, from the thread's incident row). A session pod
// keeps its own context across turns and frames a report it was already
// shown only once (a2a/worker-adapter), so a warm pod is not shown it
// twice and a fresh incarnation is shown it again. A row read for such a
// thread is cached for reportHitTTL (hits), so a turn does not pay a bucket
// read each time; put drops the cached row at every address it writes.
//
// Any bucket error is logged and the turn or the post goes on with memory
// alone: the relay is a help, not a condition.
const (
	// pushedReportsPerConversation bounds the reports kept per
	// conversation, the oldest forgotten first: a turn files a handful of
	// cards at most.
	pushedReportsPerConversation = 8
	// pushedReportsConversationsMax bounds the conversations kept, the one
	// least recently posted a report to forgotten first (notifyUpdates'
	// bound).
	pushedReportsConversationsMax = notifyUpdateConversationsMax
	// pushedReportsBytesMax bounds the text kept in all, the same
	// conversations forgotten first: the per-conversation bounds alone
	// allow 1024 × 8 × 64 KiB.
	pushedReportsBytesMax = 32 << 20
	// pushedReportCutNote ends a report cut to lib.PushedReportTextCap.
	pushedReportCutNote = "\n\n(cut here: the report was longer than the relay keeps)"
)

// pushedReportsConsumedMax bounds the conversations whose forgotten
// reports memory notes (consumed), the one noted least recently dropped
// first: a thread whose note is dropped may carry its newest stored report
// once more.
const pushedReportsConsumedMax = 4 * pushedReportsConversationsMax

const (
	// reportMissTTL is how long a bucket read that found no row at an
	// address stands before the address is read again, so a thread that
	// never had a report doesn't pay a bucket read on every turn. On the
	// session route only the active gateway writes report rows today, and
	// put drops the cached miss for every address it writes, so a miss can
	// go stale only through another writer; the TTL bounds how long such a
	// writer's row (a scheduled-report source, say) goes unread.
	reportMissTTL = 10 * time.Minute
	// reportMissesMax bounds the cached misses, the oldest dropped first
	// (notifyUpdates' conversation bound).
	reportMissesMax = pushedReportsConversationsMax
	// reportHitTTL is how long a row a bucket read found stands before the
	// address is read again: short, so another writer's newer row (or the
	// prune's delete) is seen within a minute, and long enough that a
	// session-routed thread's every-turn attach (current) reads the bucket
	// at most once a minute. put drops the cached row for every address it
	// writes, so this gateway's own newer report is seen at once.
	reportHitTTL = time.Minute
	// reportHitsMax bounds the cached rows, the oldest dropped first. Each
	// holds up to lib.PushedReportTextCap of text, so the bound is smaller
	// than the misses': 256 × 64 KiB is 16 MiB at the worst.
	reportHitsMax = 256
)

// pushedReports is the cache: per conversation, its last reports in order,
// each with the root of the task it reported on.
type pushedReports struct {
	mu             sync.Mutex
	byConversation map[string][]pushedReport
	order          []string
	bytes          int
	// seq numbers every report kept, in order of keeping, so a caller can
	// forget what it read (forgetThrough) by the newest report's number and
	// not by a count, which an eviction at the per-conversation cap would
	// shift onto a report kept since the read.
	seq uint64
	// consumed is, per conversation this process forgot reports for
	// (forgetThrough, forget), the posting time of the newest it forgot: a
	// stored row posted no later is not read back. consumedOrder bounds it
	// (pushedReportsConsumedMax). An eviction by the bounds notes nothing,
	// so a report the bounds forgot is still read from the bucket.
	consumed      map[string]time.Time
	consumedOrder []string
	// misses is, per row key a bucket read found empty, when the miss was
	// seen: the key is not read again until reportMissTTL has passed or
	// put writes there. missOrder bounds it (reportMissesMax).
	misses    map[string]time.Time
	missOrder []string
	// hits is, per row key a bucket read found a row at, the row and when
	// it was read: the key is not read again until reportHitTTL has passed
	// or put writes there. hitOrder bounds it (reportHitsMax).
	hits     map[string]reportHit
	hitOrder []string

	// durable is the bucket side (gatewayReports); nil keeps reports in
	// memory alone. log logs its failures; nil is slog.Default.
	durable reportBackend
	log     *slog.Logger
	// now is the clock a report's posting time is read from; nil is
	// time.Now (tests).
	now func() time.Time
}

// reportBackend stores and reads report rows (Registry.PutReport and
// GetReport).
type reportBackend interface {
	PutReport(ctx context.Context, rec *lib.ReportRecord) error
	GetReport(ctx context.Context, addr lib.ReportAddress) (*lib.ReportRecord, error)
}

// reportHit is a row a bucket read found, cached (pushedReports.hits).
type reportHit struct {
	rec    lib.ReportRecord
	readAt time.Time
}

type pushedReport struct {
	root, text string
	seq        uint64
	postedAt   time.Time
}

func (p *pushedReports) logger() *slog.Logger {
	if p.log != nil {
		return p.log
	}
	return slog.Default()
}

func (p *pushedReports) clock() time.Time {
	if p.now != nil {
		return p.now()
	}
	return time.Now()
}

// reportWriteAddress is the row a report posted into conversation on
// root's behalf is stored at: a chat thread's address, whatever the root,
// since a thread's reply names no task and is under the thread's newest
// report; else, for a report on a task, (conversation, conversation#root),
// what lib.ReportAddresses reads first for a reply naming that root. A
// whole conversation, a Slack DM thread and a report on no task outside a
// thread have none and stay in memory alone.
func reportWriteAddress(conversation, root string) (lib.ReportAddress, bool) {
	key := strings.TrimSpace(conversation)
	if lib.ThreadKeyedConversation(key) {
		return lib.ReportThreadAddress(key)
	}
	if key == "" || root == "" || lib.WholeConversationKey(key) {
		return lib.ReportAddress{}, false
	}
	return lib.ReportAddress{ChatID: key, ThreadID: key + lib.ReportReplyTaskSep + root}, true
}

// put keeps text as a report posted into conversation on root's behalf,
// cut to the cap, forgets what the bounds no longer hold, and stores the
// report in the bucket when there is one.
func (p *pushedReports) put(conversation, root, text string) {
	if len(text) > lib.PushedReportTextCap {
		// truncateRunes appends its one-rune mark, so the budget leaves room
		// for it as well as the note.
		text = truncateRunes(text, lib.PushedReportTextCap-len(pushedReportCutNote)-utf8.UTFMax) + pushedReportCutNote
	}
	postedAt := p.clock()
	p.mu.Lock()
	p.keepLocked(conversation, root, text, postedAt)
	p.mu.Unlock()
	if p.durable == nil {
		return
	}
	addr, ok := reportWriteAddress(conversation, root)
	if !ok {
		return
	}
	p.mu.Lock()
	p.dropMissLocked(addr.Key())
	p.dropHitLocked(addr.Key())
	p.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), reportStoreTimeout)
	defer cancel()
	err := p.durable.PutReport(ctx, &lib.ReportRecord{
		Text: text, Source: lib.ReportSourceRelay, PostedAt: postedAt,
		Conversation: strings.TrimSpace(conversation), Root: root,
		ChatID: addr.ChatID, ThreadID: addr.ThreadID,
	})
	if err != nil {
		p.logger().Warn("pushed report not stored; it is kept in memory alone and a restart forgets it",
			"conversation", conversation, "root", root, "key", addr.Key(), "err", err)
	}
}

// keepLocked appends a report to conversation's and forgets what the bounds
// no longer hold, returning the report's number. Under p.mu.
func (p *pushedReports) keepLocked(conversation, root, text string, postedAt time.Time) uint64 {
	if p.byConversation == nil {
		p.byConversation = map[string][]pushedReport{}
	}
	posts, known := p.byConversation[conversation]
	p.seq++
	posts = append(posts, pushedReport{root: root, text: text, seq: p.seq, postedAt: postedAt})
	p.bytes += len(text)
	if len(posts) > pushedReportsPerConversation {
		p.bytes -= len(posts[0].text)
		posts = posts[1:]
	}
	p.byConversation[conversation] = posts
	if known {
		p.order = moveToEnd(p.order, conversation)
	} else {
		p.order = append(p.order, conversation)
	}
	for len(p.order) > 1 && (len(p.order) > pushedReportsConversationsMax || p.bytes > pushedReportsBytesMax) {
		p.forgetOldest()
	}
	return p.seq
}

// forgetOldest drops the conversation least recently posted a report to.
// Under p.mu.
func (p *pushedReports) forgetOldest() {
	p.forgetLocked(p.order[0])
}

// latest is the newest report kept for conversation, whatever task it was
// on (what the next human turn that opens in a chat thread is under), and
// that report's number, for forgetThrough once that turn is on the bus.
func (p *pushedReports) latest(conversation string) (string, uint64, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	posts := p.byConversation[conversation]
	if len(posts) == 0 {
		return "", 0, false
	}
	newest := posts[len(posts)-1]
	return newest.text, newest.seq, true
}

// latestOrStored is latest, and when memory has nothing for conversation,
// the thread's stored row (lib.ReportAddresses with no task), unless this
// process already forgot a report posted as late (consumed). A row read
// back is kept in memory as the conversation's report, numbered like any
// other, so forgetThrough forgets it once the turn is on the bus.
func (p *pushedReports) latestOrStored(conversation string) (string, uint64, bool) {
	if text, seq, ok := p.latest(conversation); ok || p.durable == nil {
		return text, seq, ok
	}
	rec, ok := p.stored(conversation, "", func(*lib.ReportRecord) bool { return true }, true)
	if !ok {
		return "", 0, false
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if posts := p.byConversation[conversation]; len(posts) > 0 {
		// A report kept while the row was read is newer: memory's stands.
		newest := posts[len(posts)-1]
		return newest.text, newest.seq, true
	}
	return rec.Text, p.keepLocked(conversation, rec.Root, rec.Text, rec.PostedAt), true
}

// current is the newest report kept for a session-routed thread posted
// within reportRetention: memory's, else the thread's stored row, read
// whatever this process forgot for the thread (a thread that was on the
// platform route before may have a consumed note). Nothing is forgotten and
// nothing read back is kept in memory, so every turn reads it again; the
// bucket read is cached (hits).
func (p *pushedReports) current(conversation string) (string, bool) {
	horizon := p.clock().Add(-reportRetention)
	p.mu.Lock()
	posts := p.byConversation[conversation]
	if len(posts) > 0 {
		newest := posts[len(posts)-1]
		p.mu.Unlock()
		// A newer row than memory's newest can only be another writer's,
		// which a session-routed thread doesn't have today; memory stands,
		// as it does in latestOrStored.
		if !newest.postedAt.After(horizon) {
			return "", false
		}
		return newest.text, true
	}
	p.mu.Unlock()
	if p.durable == nil {
		return "", false
	}
	rec, ok := p.stored(conversation, "", func(rec *lib.ReportRecord) bool { return rec.PostedAt.After(horizon) }, false)
	if !ok {
		return "", false
	}
	return rec.Text, true
}

// stored is the first row at lib.ReportAddresses(conversation, "", root)
// that want accepts and, when honorConsumed is set, that was posted after
// the newest report this process forgot for conversation. A row read within
// reportHitTTL, or a miss within reportMissTTL, is answered from the cache.
// A failed read is logged and read as none.
func (p *pushedReports) stored(conversation, root string, want func(*lib.ReportRecord) bool, honorConsumed bool) (*lib.ReportRecord, bool) {
	addrs := lib.ReportAddresses(conversation, "", root)
	if len(addrs) == 0 {
		return nil, false
	}
	p.mu.Lock()
	through, consumed := p.consumed[conversation]
	p.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), reportStoreTimeout)
	defer cancel()
	consumed = consumed && honorConsumed
	for _, addr := range addrs {
		key := addr.Key()
		rec, cached := p.hitCached(key)
		if !cached {
			if p.missCached(key) {
				continue
			}
			var err error
			rec, err = p.durable.GetReport(ctx, addr)
			if err != nil {
				p.logger().Warn("stored pushed report not read; the turn runs on what memory holds",
					"conversation", conversation, "root", root, "key", key, "err", err)
				return nil, false
			}
			if rec == nil {
				p.noteMiss(key)
				continue
			}
			p.noteHit(key, rec)
		}
		if !want(rec) || (consumed && !rec.PostedAt.After(through)) {
			continue
		}
		return rec, true
	}
	return nil, false
}

// missCached reports whether a read of key found no row within the last
// reportMissTTL, dropping a miss that has expired.
func (p *pushedReports) missCached(key string) bool {
	now := p.clock()
	p.mu.Lock()
	defer p.mu.Unlock()
	seen, ok := p.misses[key]
	if !ok {
		return false
	}
	if now.Sub(seen) < reportMissTTL {
		return true
	}
	p.dropMissLocked(key)
	return false
}

// noteMiss caches a read of key that found no row.
func (p *pushedReports) noteMiss(key string) {
	now := p.clock()
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.misses == nil {
		p.misses = map[string]time.Time{}
	}
	if _, known := p.misses[key]; known {
		p.missOrder = moveToEnd(p.missOrder, key)
	} else {
		p.missOrder = append(p.missOrder, key)
	}
	p.misses[key] = now
	for len(p.missOrder) > reportMissesMax {
		delete(p.misses, p.missOrder[0])
		p.missOrder = p.missOrder[1:]
	}
}

// dropMissLocked forgets a cached miss for key. Under p.mu.
func (p *pushedReports) dropMissLocked(key string) {
	if _, ok := p.misses[key]; !ok {
		return
	}
	delete(p.misses, key)
	p.missOrder = slices.DeleteFunc(p.missOrder, func(k string) bool { return k == key })
}

// hitCached is the row a read of key found within the last reportHitTTL,
// a copy, dropping a cached row that has expired.
func (p *pushedReports) hitCached(key string) (*lib.ReportRecord, bool) {
	now := p.clock()
	p.mu.Lock()
	defer p.mu.Unlock()
	hit, ok := p.hits[key]
	if !ok {
		return nil, false
	}
	if now.Sub(hit.readAt) < reportHitTTL {
		rec := hit.rec
		return &rec, true
	}
	p.dropHitLocked(key)
	return nil, false
}

// noteHit caches the row a read of key found.
func (p *pushedReports) noteHit(key string, rec *lib.ReportRecord) {
	now := p.clock()
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.hits == nil {
		p.hits = map[string]reportHit{}
	}
	if _, known := p.hits[key]; known {
		p.hitOrder = moveToEnd(p.hitOrder, key)
	} else {
		p.hitOrder = append(p.hitOrder, key)
	}
	p.hits[key] = reportHit{rec: *rec, readAt: now}
	for len(p.hitOrder) > reportHitsMax {
		delete(p.hits, p.hitOrder[0])
		p.hitOrder = p.hitOrder[1:]
	}
}

// dropHitLocked forgets a cached row for key. Under p.mu.
func (p *pushedReports) dropHitLocked(key string) {
	if _, ok := p.hits[key]; !ok {
		return
	}
	delete(p.hits, key)
	p.hitOrder = slices.DeleteFunc(p.hitOrder, func(k string) bool { return k == key })
}

// noteConsumedLocked notes that this process forgot conversation's reports
// posted up to through. Under p.mu.
func (p *pushedReports) noteConsumedLocked(conversation string, through time.Time) {
	if p.consumed == nil {
		p.consumed = map[string]time.Time{}
	}
	prev, known := p.consumed[conversation]
	if known && prev.After(through) {
		through = prev
	}
	p.consumed[conversation] = through
	if known {
		p.consumedOrder = moveToEnd(p.consumedOrder, conversation)
	} else {
		p.consumedOrder = append(p.consumedOrder, conversation)
	}
	for len(p.consumedOrder) > pushedReportsConsumedMax {
		delete(p.consumed, p.consumedOrder[0])
		p.consumedOrder = p.consumedOrder[1:]
	}
}

// forgetThrough drops the reports kept for conversation up to and including
// the one numbered seq: the ones a turn read (latest), once that turn is on
// the bus. A report kept since the read has a higher number and stays for
// the next turn, whether or not the cap evicted older reports in between.
// The newest posting time dropped is noted (consumed), so the bucket's copy
// of what was dropped is not read back.
func (p *pushedReports) forgetThrough(conversation string, seq uint64) {
	p.mu.Lock()
	defer p.mu.Unlock()
	// Reports are kept in order of number (appended in order, evicted from
	// the front), so the first one numbered past seq starts what stays.
	posts := p.byConversation[conversation]
	first := len(posts)
	for i, post := range posts {
		if post.seq > seq {
			first = i
			break
		}
	}
	if first > 0 {
		through := posts[0].postedAt
		for _, post := range posts[:first] {
			if post.postedAt.After(through) {
				through = post.postedAt
			}
		}
		p.noteConsumedLocked(conversation, through)
	}
	if first == len(posts) {
		p.forgetLocked(conversation)
		return
	}
	for _, post := range posts[:first] {
		p.bytes -= len(post.text)
	}
	p.byConversation[conversation] = posts[first:]
}

// forget drops every report kept for conversation: a chat thread's, once a
// message was steered into the thread's running task. Every report posted
// so far is noted as forgotten (consumed), the ones only the bucket holds
// too.
func (p *pushedReports) forget(conversation string) {
	now := p.clock()
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, post := range p.byConversation[conversation] {
		if post.postedAt.After(now) {
			now = post.postedAt
		}
	}
	p.noteConsumedLocked(conversation, now)
	p.forgetLocked(conversation)
}

// forgetLocked drops conversation's reports from memory. Under p.mu.
func (p *pushedReports) forgetLocked(conversation string) {
	for _, post := range p.byConversation[conversation] {
		p.bytes -= len(post.text)
	}
	delete(p.byConversation, conversation)
	p.order = slices.DeleteFunc(p.order, func(c string) bool { return c == conversation })
}

// byRoot is the newest report kept for conversation on root's behalf.
func (p *pushedReports) byRoot(conversation, root string) (string, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	posts := p.byConversation[conversation]
	for i := len(posts) - 1; i >= 0; i-- {
		if posts[i].root == root {
			return posts[i].text, true
		}
	}
	return "", false
}

// byRootOrStored is byRoot, and when memory has nothing on root, the first
// stored row on root at lib.ReportAddresses(conversation, "", root): the
// console row (conversation, conversation#root), then the thread's own row
// when conversation is a thread and its newest report was on root. Nothing
// is kept in memory from the read; a console reply may name the same root
// again, and reads it again.
func (p *pushedReports) byRootOrStored(conversation, root string) (string, bool) {
	if text, ok := p.byRoot(conversation, root); ok || p.durable == nil {
		return text, ok
	}
	rec, ok := p.stored(conversation, root, func(rec *lib.ReportRecord) bool { return rec.Root == root }, true)
	if !ok {
		return "", false
	}
	return rec.Text, true
}

// relayedReport is the kept report a human turn on rec's conversation is
// under, when there is one. A turn that names a task (replyTo, the console
// and MCP doors) is under the report on that task, read as the root of its
// chain (observedAs), which is what a console report is kept under. A turn
// in a chat thread names no task: it is under the newest report kept for
// the thread's key, and the number returned is that report's, through
// which the caller forgets the thread's reports once the turn is on the bus
// (forgetThrough), so the turn after it is not under a report it was not
// about while one kept during the publish stays. A turn naming no task
// anywhere else (a console turn with no reply, a whole-space or DM key)
// carries none; the number is 0 for every turn but a thread's. Memory is
// read first, and the bucket for what it lacks (latestOrStored,
// byRootOrStored): by the chain's root for a turn naming a task, the root a
// console report is stored under.
//
// A thread on the session-pod route (rec.SessionRouted) is under its newest
// report within reportRetention on every turn (current), and the number is
// 0, so the turn forgets nothing: the pod frames a report once per context.
func (g *Gateway) relayedReport(rec *SessionRecord, replyTo string) (lib.PushedReport, uint64, bool) {
	if replyTo == "" {
		if !lib.ThreadKeyedConversation(rec.Key) {
			return lib.PushedReport{}, 0, false
		}
		if rec.SessionRouted {
			text, ok := g.reports.current(rec.Key)
			return lib.PushedReport{Text: text}, 0, ok
		}
		text, seq, ok := g.reports.latestOrStored(rec.Key)
		return lib.PushedReport{Text: text}, seq, ok
	}
	root, _ := rec.observedAs(replyTo)
	text, ok := g.reports.byRootOrStored(rec.Key, root)
	return lib.PushedReport{TaskID: root, Text: text}, 0, ok
}

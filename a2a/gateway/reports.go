package gateway

import (
	"slices"
	"sync"
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
// Process memory, as notifyUpdates is: bounded, and lost on a restart,
// after which the executor's own lookup (the replyTo part, the
// hermes-bridge's session-kv read) is what serves a reply.
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
}

type pushedReport struct {
	root, text string
	seq        uint64
}

// put keeps text as a report posted into conversation on root's behalf,
// cut to the cap, and forgets what the bounds no longer hold.
func (p *pushedReports) put(conversation, root, text string) {
	if len(text) > lib.PushedReportTextCap {
		// truncateRunes appends its one-rune mark, so the budget leaves room
		// for it as well as the note.
		text = truncateRunes(text, lib.PushedReportTextCap-len(pushedReportCutNote)-utf8.UTFMax) + pushedReportCutNote
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.byConversation == nil {
		p.byConversation = map[string][]pushedReport{}
	}
	posts, known := p.byConversation[conversation]
	p.seq++
	posts = append(posts, pushedReport{root: root, text: text, seq: p.seq})
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

// forgetThrough drops the reports kept for conversation up to and including
// the one numbered seq: the ones a turn read (latest), once that turn is on
// the bus. A report kept since the read has a higher number and stays for
// the next turn, whether or not the cap evicted older reports in between.
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
// message was steered into the thread's running task.
func (p *pushedReports) forget(conversation string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.forgetLocked(conversation)
}

// forgetLocked is forget under p.mu.
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
// carries none; the number is 0 for every turn but a thread's.
func (g *Gateway) relayedReport(rec *SessionRecord, replyTo string) (lib.PushedReport, uint64, bool) {
	if replyTo == "" {
		if !lib.ThreadKeyedConversation(rec.Key) {
			return lib.PushedReport{}, 0, false
		}
		text, seq, ok := g.reports.latest(rec.Key)
		return lib.PushedReport{Text: text}, seq, ok
	}
	root, _ := rec.observedAs(replyTo)
	text, ok := g.reports.byRoot(rec.Key, root)
	return lib.PushedReport{TaskID: root, Text: text}, 0, ok
}

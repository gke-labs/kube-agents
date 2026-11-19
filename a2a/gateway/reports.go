package gateway

import (
	"sync"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The reports the notify route posted into console conversations, kept so
// a reply under one carries the report to the executor
// (lib.PushedReportPart).
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
}

type pushedReport struct {
	root, text string
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
	posts = append(posts, pushedReport{root: root, text: text})
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
	oldest := p.order[0]
	for _, post := range p.byConversation[oldest] {
		p.bytes -= len(post.text)
	}
	delete(p.byConversation, oldest)
	p.order = p.order[1:]
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
// under, when there is one: the report on the task the turn named
// (replyTo, the console and MCP doors), read as the root of its chain
// (observedAs), which is what a report is kept under. A turn naming no
// task carries none: only console posts name their task today, so a
// Slack or Google Chat thread reply keeps the executor's own lookup.
func (g *Gateway) relayedReport(rec *SessionRecord, replyTo string) (lib.PushedReport, bool) {
	if replyTo == "" {
		return lib.PushedReport{}, false
	}
	root, _ := rec.observedAs(replyTo)
	text, ok := g.reports.byRoot(rec.Key, root)
	return lib.PushedReport{TaskID: root, Text: text}, ok
}

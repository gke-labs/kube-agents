package gateway

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The Home tab: a fleet dashboard published with views.publish when a member
// opens the app's Home tab (app_home_opened, tab "home"), and published again
// to whoever opened it lately when something it shows changes. No model call:
// fleet health, the running cards, the pull requests and the last audit are
// the agent pod's board snapshot (lib.Board, read over the board route), and
// "waiting on you" is the open questions this adapter already tracks, in the
// conversations the viewer asked in. Only a member the gateway would answer
// sees any of it; anyone else gets one line.
const (
	slackHomeTab = "home"

	// slackHomeBoardFresh is how long one snapshot serves every open: once a
	// read has landed, a burst of members opening the tab costs no more.
	slackHomeBoardFresh = 30 * time.Second
	// slackHomeBoardTimeout bounds one board read, the request and the
	// bridge's SQLite reads behind it.
	slackHomeBoardTimeout = 5 * time.Second
	// slackHomeViewerTTL is how long after opening the tab a member keeps
	// getting it republished on a change. Slack sends no "closed" event, so
	// this is the bound on publishing to a tab nobody is looking at.
	slackHomeViewerTTL = 10 * time.Minute
	// slackHomeRefreshDebounce gathers a burst of changes (a card's plan
	// moving a step at a time) into one republish.
	slackHomeRefreshDebounce = 10 * time.Second
	// slackHomeViewersCap bounds the members republished to; views.publish
	// is Tier 4 (100+ per minute), and the allowlist is the realistic bound.
	slackHomeViewersCap = 50

	// The panels' caps, which keep a view inside Slack's 100-block limit:
	// each listed item is a section and a context block.
	slackHomeClustersMax = 15
	slackHomeWaitingMax  = 5
	slackHomeRunningMax  = 5
	slackHomePRsMax      = 5
	// slackHomeLineMax cuts a question's text to one line of the panel.
	slackHomeLineMax = 150

	slackHomeTitleFleet   = "Fleet"
	slackHomeTitleWaiting = "Waiting on you"
	slackHomeTitleRunning = "Running"
	slackHomeTitlePRs     = "Recent pull requests"
	slackHomeTitleAudit   = "Last audit"
	slackHomeOpenThread   = "Open thread"
	slackHomeLedgerButton = "Ledger issue #%d ↗"
	slackHomeDenied       = "Kage only shows its fleet to the people this install lets ask it."

	slackHomeDot      = " · "
	slackHomeRunMark  = "⏳"
	slackHomeCritical = "🔴"
	slackHomeWarning  = "🟠"
	slackHomeHealthy  = "🟢"

	// slackDateToken is Slack's date formatting: rendered in the viewer's
	// own time zone, with the fallback shown by a client that cannot.
	slackDateToken    = "<!date^%d^%s|%s>"
	slackDateTime     = "{time}"
	slackDateDayTime  = "{date_short_pretty} {time}"
	slackDateFallback = "Jan 2 15:04 UTC"
	// slackPermalink is a message's link under the workspace URL auth.test
	// returns ("https://acme.slack.com/"): the ts without its dot.
	slackPermalink       = "%sarchives/%s/p%s"
	slackPermalinkThread = "?thread_ts=%s&cid=%s"

	slackHomeClusterCount = "%d cluster%s"
	slackHomeMoreClusters = "%d more cluster%s"
	slackHomeBoardUnread  = "Could not read the board at %s."
	slackHomeMoreFindings = " · %d more finding%s"
	slackHomeNoFindings   = "no open findings"
	slackHomeAuditCounts  = "*%s:* %d finding%s, %d critical"
	slackHomeAuditClean   = "*%s:* clean"
	slackHomeUpdated      = "updated "
	slackHomeAsked        = "asked "
	slackHomeStarted      = "started "
	slackHomeOpened       = "opened "
	// slackHomePRLink is a pull request as mrkdwn: its URL, number and title.
	slackHomePRLink = "<%s|#%d %s>"
	// slackHomeHTTPS is the scheme prRepo reads a forge URL under.
	slackHomeHTTPS = "https://"

	// findingCritical is the findings queue's top severity.
	findingCritical = "critical"
)

// BoardReader reads the agent pod's board snapshot.
type BoardReader func(ctx context.Context) (*lib.Board, error)

// HomeAdmits answers whether the gateway would answer a Slack member, by the
// same allowlist an inbound message is admitted by, and whether that answer
// rests on allow-all rather than on the member being listed: only then does
// the member's workspace still need checking.
type HomeAdmits func(member string) (admitted, byAllowAll bool)

// HomeSink is the optional extension an Adapter implements to draw a home
// view: the gateway hands it the admission rule and the board reader. Wired
// in New the way SessionLookupSink is.
type HomeSink interface {
	SetHome(admits HomeAdmits, board BoardReader)
}

// slackHome is the adapter's Home tab state, under its own lock so a board
// read never holds s.mu.
type slackHome struct {
	mu      sync.Mutex
	admits  HomeAdmits
	board   BoardReader
	cached  *lib.Board
	readAt  time.Time
	viewers map[string]time.Time
	// askers is each conversation's latest verified asker, whom its open
	// question is waiting on; askersOrder bounds it like the adapter's
	// other per-conversation rings (remember).
	askers      map[string]string
	askersOrder []string
	// refetch marks a pending republish that must read the board again,
	// not only recompute who is waiting on what.
	refetch bool
	timer   *time.Timer
}

var _ HomeSink = (*SlackAdapter)(nil)

// SetHome arms the Home tab (HomeSink). Unarmed, an app_home_opened on the
// Home tab is ignored, as it was before the tab existed.
func (s *SlackAdapter) SetHome(admits HomeAdmits, board BoardReader) {
	s.home.mu.Lock()
	defer s.home.mu.Unlock()
	s.home.admits, s.home.board = admits, board
}

// homeOpened answers the Home tab opening, off the event pump.
func (s *SlackAdapter) homeOpened(ev *slackevents.AppHomeOpenedEvent) {
	if ev.Tab != slackHomeTab || ev.User == "" {
		return
	}
	s.home.mu.Lock()
	armed := s.home.admits != nil && s.home.board != nil
	s.home.mu.Unlock()
	if !armed {
		return
	}
	s.goSession(func() { s.publishHome(ev.User, true) })
}

// publishHome draws member's Home tab and publishes it. note records them as
// a viewer to republish to on a change; a republish passes false.
func (s *SlackAdapter) publishHome(member string, note bool) {
	view := slack.HomeTabViewRequest{Type: slack.VTHomeTab}
	if !s.homeAdmitted(member) {
		view.Blocks = slack.Blocks{BlockSet: []slack.Block{
			slack.NewSectionBlock(slack.NewTextBlockObject(slack.MarkdownType, slackHomeDenied, false, false), nil, nil),
		}}
	} else {
		if note {
			s.noteViewer(member)
		}
		board, err := s.homeBoard()
		if err != nil {
			s.log.Warn("slack: home tab board read failed", "err", err)
			board = &lib.Board{Error: err.Error()}
		}
		view.Blocks = slack.Blocks{BlockSet: s.homeBlocks(board, s.waitingOn(member), time.Now())}
	}
	if _, err := s.api.PublishView(member, view, ""); err != nil {
		s.log.Warn("slack: views.publish failed", "member", member, "err", err)
	}
}

// homeAdmitted is the allowlist, and under allow-all the member's workspace
// being the bot's: allow-all admits anyone who can open the app, and a Slack
// Connect member of another organisation is not anyone. A listed member is
// the install's own word and needs no users.info (nor users:read).
func (s *SlackAdapter) homeAdmitted(member string) bool {
	s.home.mu.Lock()
	admits := s.home.admits
	s.home.mu.Unlock()
	if admits == nil {
		return false
	}
	admitted, byAllowAll := admits(member)
	if !admitted {
		return false
	}
	if !byAllowAll {
		return true
	}
	user, err := s.api.GetUserInfo(member)
	if err != nil {
		s.log.Warn("slack: home tab member lookup failed; showing no fleet data", "member", member, "err", err)
		return false
	}
	return s.teamID != "" && user.TeamID == s.teamID
}

// homeBoard is the cached snapshot, read again once it is older than
// slackHomeBoardFresh or a change invalidated it.
func (s *SlackAdapter) homeBoard() (*lib.Board, error) {
	s.home.mu.Lock()
	if s.home.cached != nil && time.Since(s.home.readAt) < slackHomeBoardFresh {
		b := s.home.cached
		s.home.mu.Unlock()
		return b, nil
	}
	read := s.home.board
	s.home.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), slackHomeBoardTimeout)
	defer cancel()
	b, err := read(ctx)
	if err != nil {
		return nil, err
	}
	s.home.mu.Lock()
	s.home.cached, s.home.readAt = b, time.Now()
	s.home.mu.Unlock()
	return b, nil
}

func (s *SlackAdapter) noteViewer(member string) {
	s.home.mu.Lock()
	defer s.home.mu.Unlock()
	if s.home.viewers == nil {
		s.home.viewers = map[string]time.Time{}
	}
	s.pruneViewersLocked(time.Now())
	if _, ok := s.home.viewers[member]; !ok && len(s.home.viewers) >= slackHomeViewersCap {
		return
	}
	s.home.viewers[member] = time.Now()
}

func (s *SlackAdapter) pruneViewersLocked(now time.Time) {
	for m, at := range s.home.viewers {
		if now.Sub(at) > slackHomeViewerTTL {
			delete(s.home.viewers, m)
		}
	}
}

// noteAsker records who asked in a conversation, so its open question is
// counted as waiting on them.
func (s *SlackAdapter) noteAsker(conversation, member string) {
	if member == "" {
		return
	}
	s.home.mu.Lock()
	defer s.home.mu.Unlock()
	if s.home.askers == nil {
		s.home.askers = map[string]string{}
	}
	s.home.askers[conversation] = member
	remember(&s.home.askersOrder, conversation, func(k string) { delete(s.home.askers, k) })
}

// homeQuestion is an open question as the Home tab lists it.
type homeQuestion struct {
	text, link string
	at         time.Time
}

// waitingOn lists the open questions in the conversations member asked in,
// newest first.
func (s *SlackAdapter) waitingOn(member string) []homeQuestion {
	s.home.mu.Lock()
	mine := map[string]bool{}
	for conv, asker := range s.home.askers {
		if asker == member {
			mine[conv] = true
		}
	}
	s.home.mu.Unlock()
	s.mu.Lock()
	defer s.mu.Unlock()
	ux := s.uxLocked()
	var out []homeQuestion
	for i := len(ux.questionsOrder) - 1; i >= 0 && len(out) < slackHomeWaitingMax; i-- {
		conv := ux.questionsOrder[i]
		q, ok := ux.questions[conv]
		if !ok || !mine[conv] || ux.answered[q.channel+"/"+q.ts] {
			continue
		}
		out = append(out, homeQuestion{text: firstLine(q.title, slackHomeLineMax), link: s.permalink(q), at: slackTSTime(q.ts)})
	}
	return out
}

// homeChanged republishes the Home tab to its recent viewers after the
// debounce, reading the board again when refetch is set (a card moved)
// rather than only recomputing questions (one was asked or answered).
func (s *SlackAdapter) homeChanged(refetch bool) {
	s.home.mu.Lock()
	defer s.home.mu.Unlock()
	if s.home.board == nil || len(s.home.viewers) == 0 {
		return
	}
	s.home.refetch = s.home.refetch || refetch
	if s.home.timer != nil {
		return
	}
	s.home.timer = time.AfterFunc(slackHomeRefreshDebounce, s.republishHome)
}

func (s *SlackAdapter) republishHome() {
	s.home.mu.Lock()
	s.home.timer = nil
	if s.home.refetch {
		s.home.cached = nil
	}
	s.home.refetch = false
	s.pruneViewersLocked(time.Now())
	viewers := make([]string, 0, len(s.home.viewers))
	for m := range s.home.viewers {
		viewers = append(viewers, m)
	}
	s.home.mu.Unlock()
	for _, m := range viewers {
		s.publishHome(m, false)
	}
}

// homeBlocks draws the dashboard: the mock's five panels, in its order.
func (s *SlackAdapter) homeBlocks(b *lib.Board, waiting []homeQuestion, now time.Time) []slack.Block {
	var blocks []slack.Block
	header := func(t string) {
		if len(blocks) > 0 {
			blocks = append(blocks, slack.NewDividerBlock())
		}
		blocks = append(blocks, slack.NewHeaderBlock(slack.NewTextBlockObject(slack.PlainTextType, t, false, false)))
	}
	section := func(text string, accessory *slack.Accessory) {
		blocks = append(blocks, slack.NewSectionBlock(slack.NewTextBlockObject(slack.MarkdownType, text, false, false), nil, accessory))
	}
	note := func(parts ...string) {
		blocks = append(blocks, slack.NewContextBlock("", slack.NewTextBlockObject(slack.MarkdownType, strings.Join(parts, slackHomeDot), false, false)))
	}
	// A link button's click reaches interactive as a link action and is only
	// acked there (Slack opens the url), as a chat message's link buttons are.
	links := 0
	link := func(text, url string) *slack.Accessory {
		id := slackChatActionPrefix + "." + slackChatLinkAction + "." + strconv.Itoa(links)
		links++
		btn := slack.NewButtonBlockElement(id, "", slack.NewTextBlockObject(slack.PlainTextType, text, false, false))
		btn.URL = url
		return slack.NewAccessory(btn)
	}

	header(slackHomeTitleFleet)
	if b.Error != "" {
		note(fmt.Sprintf(slackHomeBoardUnread, slackDate(now, slackDateTime)))
	} else {
		total := max(b.ClusterCount, len(b.Clusters))
		note(fmt.Sprintf(slackHomeClusterCount, total, plural(total)),
			slackHomeUpdated+slackDate(b.GeneratedAt, slackDateTime))
		for i, c := range b.Clusters {
			if i == slackHomeClustersMax {
				break
			}
			section(clusterLine(c), nil)
		}
		if shown := min(len(b.Clusters), slackHomeClustersMax); total > shown {
			note(fmt.Sprintf(slackHomeMoreClusters, total-shown, plural(total-shown)))
		}
	}

	if len(waiting) > 0 {
		header(slackHomeTitleWaiting)
		for _, q := range waiting {
			var acc *slack.Accessory
			if q.link != "" {
				acc = link(slackHomeOpenThread, q.link)
			}
			section(q.text, acc)
			note(slackHomeAsked + slackDate(q.at, slackDateTime))
		}
	}

	if len(b.Running) > 0 {
		header(slackHomeTitleRunning)
		for i, c := range b.Running {
			if i == slackHomeRunningMax {
				break
			}
			section(slackHomeRunMark+"  "+slackEscaper.Replace(c.Title), nil)
			var parts []string
			if c.Cluster != "" {
				parts = append(parts, slackEscaper.Replace(c.Cluster))
			}
			note(append(parts, slackHomeStarted+slackDate(c.StartedAt, slackDateTime))...)
		}
	}

	if len(b.PRs) > 0 {
		header(slackHomeTitlePRs)
		for i, p := range b.PRs {
			if i == slackHomePRsMax {
				break
			}
			section(fmt.Sprintf(slackHomePRLink, p.URL, p.Number, slackEscaper.Replace(p.Title)), nil)
			var parts []string
			if repo := prRepo(p.URL); repo != "" {
				parts = append(parts, repo)
			}
			note(append(parts, slackHomeOpened+slackDate(p.OpenedAt, slackDateDayTime))...)
		}
	}

	if a := b.Audit; a != nil {
		header(slackHomeTitleAudit)
		var acc *slack.Accessory
		if a.IssueURL != "" && a.Issue > 0 {
			acc = link(fmt.Sprintf(slackHomeLedgerButton, a.Issue), a.IssueURL)
		}
		section(auditLine(a), acc)
		note(slackDate(a.FinishedAt, slackDateDayTime))
	}
	return blocks
}

// clusterLine is one cluster: a dot for its worst open finding, its name,
// that finding's title and how many more there are.
func clusterLine(c lib.BoardCluster) string {
	name := "*" + slackEscaper.Replace(c.Name) + "*"
	if c.Findings == 0 {
		return slackHomeHealthy + " " + name + "  " + slackHomeNoFindings
	}
	dot := slackHomeWarning
	if c.Severity == findingCritical {
		dot = slackHomeCritical
	}
	line := dot + " " + name + "  " + slackEscaper.Replace(c.Top)
	if more := c.Findings - 1; more > 0 {
		line += fmt.Sprintf(slackHomeMoreFindings, more, plural(more))
	}
	return line
}

func auditLine(a *lib.BoardAudit) string {
	if a.Findings == 0 {
		return fmt.Sprintf(slackHomeAuditClean, slackEscaper.Replace(a.Name))
	}
	return fmt.Sprintf(slackHomeAuditCounts, slackEscaper.Replace(a.Name), a.Findings, plural(a.Findings), a.Critical)
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

// slackDate renders t in the viewer's time zone.
func slackDate(t time.Time, format string) string {
	return fmt.Sprintf(slackDateToken, t.Unix(), format, t.UTC().Format(slackDateFallback))
}

// slackTSTime is a Slack message ts as a time.
func slackTSTime(ts string) time.Time {
	sec, _, _ := strings.Cut(ts, ".")
	var n int64
	if _, err := fmt.Sscan(sec, &n); err != nil {
		return time.Time{}
	}
	return time.Unix(n, 0)
}

// permalink is the question's message link, in its thread; empty before
// auth.test has named the workspace.
func (s *SlackAdapter) permalink(q slackQuestion) string {
	if s.workspaceURL == "" || q.channel == "" || q.ts == "" {
		return ""
	}
	link := fmt.Sprintf(slackPermalink, s.workspaceURL, q.channel, strings.ReplaceAll(q.ts, ".", ""))
	if q.thread != "" && q.thread != q.ts {
		link += fmt.Sprintf(slackPermalinkThread, q.thread, q.channel)
	}
	return link
}

// prRepo is "owner/name" out of a forge's pull request URL
// (https://github.com/owner/name/pull/41); empty when the URL is not that
// shape.
func prRepo(url string) string {
	rest, ok := strings.CutPrefix(url, slackHomeHTTPS)
	if !ok {
		return ""
	}
	parts := strings.Split(rest, "/")
	if len(parts) < 3 || parts[1] == "" || parts[2] == "" {
		return ""
	}
	return parts[1] + "/" + parts[2]
}

package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"
)

// Agent Sessions: the thread's Working… status and its title, and the
// suggested prompts in the bot's DM. agents.sessions.setStatus takes
// processing, suspended or closed and nothing else; rename refuses a thread
// with no session yet, so the title follows the first processing that lands.
// slack-go has neither method, so they are called as Slack's JSON API
// directly, with the bot token the adapter already holds.
const (
	slackSessionProcessing = "processing"
	slackSessionSuspended  = "suspended"
	slackSessionClosed     = "closed"

	slackSessionsSetStatusMethod = "agents.sessions.setStatus"
	slackSessionsRenameMethod    = "agents.sessions.rename"

	// slackSessionRefresh is how long an unchanged processing stands before
	// the next event that recomputes it sends it again. Slack documents no
	// expiry for an agent session's status; this bounds how stale one can
	// go if it ever does (today's SESSION_REFRESH_SECONDS).
	slackSessionRefresh = 60 * time.Second

	// slackSessionsCap bounds the per-thread session state, oldest evicted
	// first, like the adapter's other maps.
	slackSessionsCap = 2048

	// slackHomeMessagesTab is app_home_opened's tab for the bot's DM.
	slackHomeMessagesTab = "messages"

	slackJSONContentType = "application/json; charset=utf-8"
	slackAuthScheme      = "Bearer "
)

// slackSuggestedPrompts are Mock 01's three asks, the prompts today's path
// offers (deploy/docker/patches/apply_slack_agent_view.py), without the dev
// fleet's cluster name: a static prompt cannot know which clusters an install
// has. Slack sends a tapped prompt's message as the user.
var slackSuggestedPrompts = []string{
	"is anything unhealthy in my clusters right now?",
	"what's on the board?",
	"which clusters are behind their release channel?",
}

// slackSessionsAPI is the Agent Sessions slice of the Web API; tests fake it.
type slackSessionsAPI interface {
	SetSessionStatus(ctx context.Context, channel, threadTS, status string) error
	RenameSession(ctx context.Context, channel, threadTS, title string) error
}

// slackSessionsClient calls agents.sessions.* as JSON with the bot token,
// bounded by the adapter's client.
type slackSessionsClient struct {
	token  string
	apiURL string
	http   *http.Client
}

func (c slackSessionsClient) SetSessionStatus(ctx context.Context, channel, threadTS, status string) error {
	return c.call(ctx, slackSessionsSetStatusMethod, map[string]string{"channel_id": channel, "thread_ts": threadTS, "status": status})
}

func (c slackSessionsClient) RenameSession(ctx context.Context, channel, threadTS, title string) error {
	return c.call(ctx, slackSessionsRenameMethod, map[string]string{"channel_id": channel, "thread_ts": threadTS, "title": title})
}

func (c slackSessionsClient) call(ctx context.Context, method string, body map[string]string) error {
	raw, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.apiURL+method, bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", slackJSONContentType)
	req.Header.Set("Authorization", slackAuthScheme+c.token)
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	var out struct {
		OK    bool   `json:"ok"`
		Error string `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return fmt.Errorf("%s: HTTP %d: %w", method, resp.StatusCode, err)
	}
	if !out.OK {
		return fmt.Errorf("%s: %s", method, out.Error)
	}
	return nil
}

// slackSession is one thread's session state. running is whether a task of
// the conversation is between its start and its chain's end (TaskObserver);
// ask and settle are the conversation's session mark (MarkObserver). sent and
// sentAt are the last status that landed; seq orders the sends, so only the
// newest wanted status is sent when several queue. ask text is the first
// verified ask, kept until the title lands.
type slackSession struct {
	running     bool
	ask, settle string
	sent        string
	sentAt      time.Time
	seq         uint64
	askText     string
	titled      bool
	send        sync.Mutex
}

// wanted is the status the thread should show. A task running holds
// processing; past the chain's end, card work still open (the ask's kind
// still marked) holds processing too, unless it waits on the user (a
// blocked settle), which is suspended; anything else is closed. That keeps
// Working… on from the ack until the cards settle, today's behaviour.
func (st *slackSession) wanted() string {
	switch {
	case st.running:
		return slackSessionProcessing
	case st.settle == markSettleBlocked:
		return slackSessionSuspended
	case st.ask != "":
		return slackSessionProcessing
	default:
		return slackSessionClosed
	}
}

// session is the conversation's state, made on first use, or nil for a
// conversation that has no thread to hold a session (a thread-less DM).
// Caller holds s.mu.
func (s *SlackAdapter) sessionLocked(conversation string) *slackSession {
	if _, thread, ok := slackChannelThread(conversation); !ok || thread == "" {
		return nil
	}
	if s.sessionStates == nil {
		s.sessionStates = map[string]*slackSession{}
	}
	st, ok := s.sessionStates[conversation]
	if !ok {
		st = &slackSession{}
		s.sessionStates[conversation] = st
		s.sessionOrder = append(s.sessionOrder, conversation)
		if len(s.sessionOrder) > slackSessionsCap {
			delete(s.sessionStates, s.sessionOrder[0])
			s.sessionOrder = s.sessionOrder[1:]
		}
	}
	return st
}

// updateSession applies change to the conversation's state and sends the
// status it then wants, off the caller's goroutine: the observer methods run
// under the gateway's session lock and must not block on Slack.
func (s *SlackAdapter) updateSession(conversation string, change func(*slackSession)) {
	s.mu.Lock()
	st := s.sessionLocked(conversation)
	if st == nil {
		s.mu.Unlock()
		return
	}
	change(st)
	st.seq++
	seq := st.seq
	s.mu.Unlock()
	s.goSession(func() { s.sendSession(conversation, st, seq) })
}

// goSession runs f on its own goroutine, or inline when a test has asked for
// it, so a test reads the result without racing the send.
func (s *SlackAdapter) goSession(f func()) {
	if s.syncSessions {
		f()
		return
	}
	go f()
}

// sendSession sends the thread's wanted status, unless a newer change has
// queued behind this one (seq) or it is what last landed and is fresh, then
// the title once a processing has landed. One thread's sends run one at a
// time (st.send), so they land in order.
func (s *SlackAdapter) sendSession(conversation string, st *slackSession, seq uint64) {
	st.send.Lock()
	defer st.send.Unlock()
	s.mu.Lock()
	if seq != st.seq {
		s.mu.Unlock()
		return
	}
	want := st.wanted()
	// A closed is sent even for a thread this process never opened: after a
	// restart the previous process's processing is still on it, and Slack
	// documents no expiry.
	fresh := st.sent == want && (want != slackSessionProcessing || s.now().Sub(st.sentAt) < slackSessionRefresh)
	api := s.sessionsAPI
	s.mu.Unlock()
	if api == nil {
		return
	}
	channel, thread, _ := slackChannelThread(conversation)
	ctx, cancel := context.WithTimeout(context.Background(), slackAPITimeout)
	defer cancel()
	if !fresh {
		if err := api.SetSessionStatus(ctx, channel, thread, want); err != nil {
			s.log.Debug("slack: agents.sessions.setStatus failed", "conversation", conversation, "status", want, "err", err)
			return
		}
		s.mu.Lock()
		changed := st.sent != want
		st.sent, st.sentAt = want, s.now()
		s.mu.Unlock()
		if changed {
			s.log.Info("slack: session status", "conversation", conversation, "status", want)
		}
	}
	s.renameSession(ctx, api, conversation, channel, thread, st, want)
}

// renameSession titles the thread from its first ask once a processing has
// landed, and only once: a follow-up ask keeps the title. A failed rename
// keeps the ask for the next processing sent.
func (s *SlackAdapter) renameSession(ctx context.Context, api slackSessionsAPI, conversation, channel, thread string, st *slackSession, status string) {
	s.mu.Lock()
	ask, titled := st.askText, st.titled
	s.mu.Unlock()
	if status != slackSessionProcessing || titled || ask == "" {
		return
	}
	title := slackSessionTitle(ask)
	if title == "" {
		// Nothing of it survives the sanitiser (an ask of emoji or a bare
		// link): let the thread's next verified ask title it instead.
		s.mu.Lock()
		if st.askText == ask {
			st.askText = ""
		}
		s.mu.Unlock()
		return
	}
	if err := api.RenameSession(ctx, channel, thread, title); err != nil {
		s.log.Debug("slack: agents.sessions.rename failed", "conversation", conversation, "err", err)
		return
	}
	s.mu.Lock()
	st.titled, st.askText = true, ""
	s.mu.Unlock()
}

// noteAsk keeps a thread's first verified ask to title its session.
func (s *SlackAdapter) noteAsk(conversation, text string) {
	if strings.TrimSpace(text) == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if st := s.sessionLocked(conversation); st != nil && !st.titled && st.askText == "" {
		st.askText = text
	}
}

// MarkChanged holds the thread's session on the conversation's session mark
// (MarkObserver): card work still open past the chain's end keeps Working…,
// and a blocked settle suspends it.
func (s *SlackAdapter) MarkChanged(conversation, ask, settle string) {
	s.updateSession(conversation, func(st *slackSession) { st.ask, st.settle = ask, settle })
}

// suggestPrompts answers assistant_thread_started and the bot's Messages tab
// opening with the three suggested prompts, off the event pump.
func (s *SlackAdapter) suggestPrompts(inner any) {
	params := slack.AssistantThreadsSetSuggestedPromptsParameters{}
	switch ev := inner.(type) {
	case *slackevents.AssistantThreadStartedEvent:
		params.ChannelID, params.ThreadTS = ev.AssistantThread.ChannelID, ev.AssistantThread.ThreadTimeStamp
	case *slackevents.AppHomeOpenedEvent:
		if ev.Tab != slackHomeMessagesTab {
			return
		}
		params.ChannelID = ev.Channel
	default:
		return
	}
	if params.ChannelID == "" {
		return
	}
	for _, p := range slackSuggestedPrompts {
		params.AddPrompt(p, p)
	}
	s.goSession(func() {
		if err := s.api.SetAssistantThreadsSuggestedPrompts(params); err != nil {
			s.log.Debug("slack: assistant.threads.setSuggestedPrompts failed", "channel", params.ChannelID, "err", err)
		}
	})
}

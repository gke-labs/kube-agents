package gateway

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/slack-go/slack"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
)

// The Home tab's channel picker moves the Slack home channel under
// spec.mode next: a conversations_select on the admitted Home view, one
// block_actions handler behind it, and the same record write the learn makes
// (slack_home_channel.go). The record stays the one source; the operator's
// projection and both pods' files follow it. No new transport, no manifest
// entry, no grammar word: the view is the member's private reply.
const (
	slackHomeTitleHome       = "Home channel"
	slackHomePickAction      = "home_channel_select"
	slackHomePickPlaceholder = "Select a channel"
	// slackContainerView is the container type of a Home tab action; a
	// message's actions carry a channel and a message ts instead.
	slackContainerView = "view"
	// slackHomePickVia tells a Home tab move from a typed one in the log.
	slackHomePickVia = "home-tab"
	// slackHomePickPendingTTL bounds how long a pick is drawn over the
	// projected file: the file follows within one reconcile and one kubelet
	// sync, and after this the file is the truth whatever it says.
	slackHomePickPendingTTL = 2 * time.Minute
	// slackHomePickFollowUpPoll paces the handler's wait for the file.
	slackHomePickFollowUpPoll = 5 * time.Second

	slackHomePickIntro       = "Where alerts, drift reports and scheduled reports post."
	slackHomeLineHome        = "Home channel: <#%s>"
	slackHomeLineNoHome      = "No home channel yet: alerts and scheduled reports are dropped. Pick a channel the app is in, or mention me in one."
	slackHomeLineNotArmed    = "The home channel is set on the PlatformAgent (`spec.integration.slack.homeChannel`, or `homeChannelPolicy: manual`)."
	slackHomePickChecking    = "Checking <#%s>…"
	slackHomePickRecorded    = "Recorded <#%s> as the home channel; it takes effect within about two minutes. A post before that may still land in <#%s>."
	slackHomePickRecordedNew = "Recorded <#%s> as the home channel; it takes effect within about two minutes."
	slackHomePickAlready     = "<#%s> is already the home channel."
	slackHomePickBusy        = "A home-channel change is in progress; pick again in a moment."
	slackHomePickConflict    = "Moved to <#%s> by <@%s> a moment ago; pick again to override."
	slackHomePickNoAPI       = "Not moved: the cluster API did not answer; pick again."
	slackHomePickNoCheck     = "Could not check <#%s> with Slack; pick again."
	slackHomePickNotApplied  = "The operator has not applied the move; check the operator."
	slackHomePickNotMember   = "Invite the app to <#%s> with /invite <@%s>, then pick it again."
	slackHomePickArchived    = "<#%s> is archived."
	slackHomePickShared      = "A channel shared through Slack Connect cannot be the home channel."
	slackHomePickMalformed   = "That is not a channel."

	// homePickedLine goes into the new home; homeMovedLine into the old one.
	// Posted raw, as homeLearnedLine is: the ids are checked channel ids.
	homePickedLine = "<#%s> is now the home channel for alerts and scheduled reports, set by <@%s> from the app's Home tab. To move it, pick another channel on the app's Home tab, or set spec.integration.slack.homeChannel on the PlatformAgent."
	homeMovedLine  = "Home channel moved to <#%s> by <@%s>."
)

// errHomeAlreadySet is moveHome's answer to a pick of the recorded channel.
var errHomeAlreadySet = errors.New("already the home channel")

// homeMoveConflict is a 409 on the move: the record changed under the write,
// and winner and by name what it says now.
type homeMoveConflict struct{ winner, by string }

func (c *homeMoveConflict) Error() string {
	return fmt.Sprintf("the record changed under the move: %s by %s", c.winner, c.by)
}

// slackHomePending is the last move made from the Home tab: drawn over the
// projected file while the file differs and the move is younger than
// slackHomePickPendingTTL, so every viewer sees the move before the kubelet
// does.
type slackHomePending struct {
	channel, member string
	at              time.Time
}

// slackHomePickStatus is one member's last pick outcome, drawn under the
// picker until their next pick or the viewer TTL.
type slackHomePickStatus struct {
	text string
	at   time.Time
}

// homePickArmed reports whether the learner holds a record client: the one
// state in which a pick can write. Off, the view draws no picker.
func (s *SlackAdapter) homePickArmed() bool {
	l := &s.homeLearn
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.record.client != nil
}

// homeForView is the home channel the Home tab shows: the pending move
// while the file has not caught up with it, else the projected file's
// channel id ("" for none).
func (s *SlackAdapter) homeForView() string {
	value, _ := s.homeLearn.file.read()
	file := ""
	if slackIsHomeChannelID(value) {
		file = value
	}
	s.home.mu.Lock()
	defer s.home.mu.Unlock()
	p := s.home.pending
	if p == nil {
		return file
	}
	if p.channel == file || time.Since(p.at) > slackHomePickPendingTTL {
		s.home.pending = nil
		return file
	}
	return p.channel
}

// homeChannelBlocks draws the Home channel panel: the picker and the line
// naming the current home for an armed learner, one fixed line otherwise,
// and the member's last pick outcome.
func (s *SlackAdapter) homeChannelBlocks(member string) []slack.Block {
	blocks := []slack.Block{
		slack.NewDividerBlock(),
		slack.NewHeaderBlock(slack.NewTextBlockObject(slack.PlainTextType, slackHomeTitleHome, false, false)),
	}
	mrk := func(text string) *slack.TextBlockObject {
		return slack.NewTextBlockObject(slack.MarkdownType, text, false, false)
	}
	if !s.homePickArmed() {
		return append(blocks, slack.NewContextBlock("", mrk(slackHomeLineNotArmed)))
	}
	picker := slack.NewOptionsSelectBlockElement(slack.OptTypeConversations,
		slack.NewTextBlockObject(slack.PlainTextType, slackHomePickPlaceholder, false, false), slackHomePickAction)
	// The filter is Slack's list, not the gate: checkHomeChannel decides.
	picker.Filter = &slack.SelectBlockElementFilter{Include: []string{"public", "private"}, ExcludeExternalSharedChannels: true}
	blocks = append(blocks, slack.NewSectionBlock(mrk(slackHomePickIntro), nil, slack.NewAccessory(picker)))
	line := slackHomeLineNoHome
	if home := s.homeForView(); home != "" {
		line = fmt.Sprintf(slackHomeLineHome, home)
	}
	blocks = append(blocks, slack.NewContextBlock("", mrk(line)))
	if status := s.pickStatus(member); status != "" {
		blocks = append(blocks, slack.NewContextBlock("", mrk(status)))
	}
	return blocks
}

func (s *SlackAdapter) pickStatus(member string) string {
	s.home.mu.Lock()
	defer s.home.mu.Unlock()
	st, ok := s.home.picks[member]
	if !ok || time.Since(st.at) > slackHomeViewerTTL {
		delete(s.home.picks, member)
		return ""
	}
	return st.text
}

func (s *SlackAdapter) setPickStatus(member, text string) {
	s.home.mu.Lock()
	defer s.home.mu.Unlock()
	if s.home.picks == nil {
		s.home.picks = map[string]slackHomePickStatus{}
	}
	s.home.picks[member] = slackHomePickStatus{text: text, at: time.Now()}
}

// homePick takes a Home tab pick off the pump: the shape check and the
// dedupe here, every Slack and API call on the handler goroutine, under the
// learner's wait group and its busy gate, so a pick and a learn never
// interleave and a write in flight lands before Run returns.
func (s *SlackAdapter) homePick(p slackClickPayload, channel string) {
	member, actionTS := p.User.ID, p.ActionTS
	if p.Channel.ID != "" || p.Container.Type != slackContainerView || member == "" || actionTS == "" || channel == "" {
		s.log.Info("slack: dropping a home pick that is not a Home tab action", "member", member, "channel", p.Channel.ID, "container", p.Container.Type)
		return
	}
	if s.alreadySeen("home:" + member + "/" + actionTS) {
		return
	}
	l := &s.homeLearn
	l.mu.Lock()
	if l.record.client == nil || l.closed {
		l.mu.Unlock()
		s.log.Info("slack: dropping a home pick: the learner is not armed or is closing", "member", member)
		return
	}
	if l.busy {
		l.mu.Unlock()
		s.log.Info("slack: a home pick arrived while a change runs", "member", member, "channel", channel)
		s.setPickStatus(member, slackHomePickBusy)
		s.goSession(func() { s.publishHome(member, true) })
		return
	}
	l.busy = true
	l.wg.Add(1)
	l.mu.Unlock()
	s.goSession(func() {
		defer l.wg.Done()
		defer func() {
			l.mu.Lock()
			l.busy = false
			l.mu.Unlock()
		}()
		s.runHomePick(member, p.User.TeamID, channel, actionTS)
	})
}

// runHomePick admits the member, checks the channel, moves the home, and
// tells the member through their view; the two public lines follow a move.
func (s *SlackAdapter) runHomePick(member, team, channel, actionTS string) {
	if s.otherWorkspace(team) {
		s.log.Info("slack: home pick refused: another workspace's member", "member", member, "team", team, "channel", channel)
		return
	}
	admitted, decided := s.homeAdmitted(member)
	if !decided {
		s.log.Info("slack: home pick dropped: the member's admission could not be decided", "member", member, "channel", channel)
		return
	}
	if !admitted {
		s.log.Info("slack: home pick refused: member not admitted", "member", member, "channel", channel)
		s.publishHome(member, false)
		return
	}
	s.setPickStatus(member, fmt.Sprintf(slackHomePickChecking, channel))
	s.publishHome(member, true)
	ctx, cancel := context.WithTimeout(context.Background(), homeLearnTimeout)
	defer cancel()
	finish := func(status string) {
		s.setPickStatus(member, status)
		s.publishHome(member, true)
	}
	refused, err := s.checkHomeChannel(ctx, channel)
	if err != nil {
		s.log.Warn("slack: home pick: the channel could not be checked", "member", member, "channel", channel, "err", err)
		finish(fmt.Sprintf(slackHomePickNoCheck, channel))
		return
	}
	if refused != "" {
		s.log.Info("slack: home pick refused", "member", member, "channel", channel, "reason", refused)
		finish(s.homePickRefusal(refused, channel))
		return
	}
	previous, err := s.moveHome(ctx, channel, member)
	var conflict *homeMoveConflict
	switch {
	case errors.Is(err, errHomeAlreadySet):
		s.log.Info("slack: home pick: already the home channel", "member", member, "channel", channel)
		finish(fmt.Sprintf(slackHomePickAlready, channel))
		return
	case errors.As(err, &conflict):
		s.log.Info("slack: home pick lost to a concurrent change", "member", member, "channel", channel, "winner", conflict.winner, "by", conflict.by)
		finish(fmt.Sprintf(slackHomePickConflict, conflict.winner, conflict.by))
		return
	case err != nil:
		s.log.Warn("slack: home pick: the record could not be written", "member", member, "channel", channel, "err", err)
		finish(slackHomePickNoAPI)
		return
	}
	s.home.mu.Lock()
	s.home.pending = &slackHomePending{channel: channel, member: member, at: time.Now()}
	s.home.mu.Unlock()
	s.log.Info("slack: home channel moved", "member", member, "old", previous, "new", channel, "actionTS", actionTS, "via", slackHomePickVia)
	status := fmt.Sprintf(slackHomePickRecordedNew, channel)
	if previous != "" {
		status = fmt.Sprintf(slackHomePickRecorded, channel, previous)
	}
	finish(status)
	s.homeChanged(false)
	s.postHomeLine(channel, fmt.Sprintf(homePickedLine, channel, member))
	if previous != "" {
		s.postHomeLine(previous, fmt.Sprintf(homeMovedLine, channel, member))
	}
	if !s.awaitHomeFile(channel) {
		s.log.Warn("slack: the projected home channel did not follow the move", "channel", channel, "waited", s.homeLearn.followUp)
		finish(slackHomePickNotApplied)
	}
}

// homePickRefusal is the member's text for a check that refused the channel.
func (s *SlackAdapter) homePickRefusal(reason, channel string) string {
	switch reason {
	case homeRefusedNotMember:
		return fmt.Sprintf(slackHomePickNotMember, channel, s.botUserID)
	case homeRefusedArchived:
		return fmt.Sprintf(slackHomePickArchived, channel)
	case homeRefusedShared:
		return slackHomePickShared
	}
	return slackHomePickMalformed
}

// postHomeLine posts one public line raw, as the learned line is; a failure
// is logged, never retried: the move itself has landed.
func (s *SlackAdapter) postHomeLine(channel, text string) {
	opts := []slack.MsgOption{slack.MsgOptionText(text, false), slack.MsgOptionDisableLinkUnfurl(), slack.MsgOptionDisableMediaUnfurl()}
	if _, _, err := s.api.PostMessage(channel, opts...); err != nil {
		s.log.Warn("slack: home channel line not posted", "channel", channel, "err", err)
	}
}

// awaitHomeFile waits, up to the learner's followUp, for the projected file
// to name channel: the operator's pass and one kubelet sync. True when it
// did, or when nothing was to be waited for.
func (s *SlackAdapter) awaitHomeFile(channel string) bool {
	deadline := time.Now().Add(s.homeLearn.followUp)
	for {
		if value, _ := s.homeLearn.file.read(); value == channel {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(min(slackHomePickFollowUpPoll, s.homeLearn.followUp))
	}
}

// moveHome writes channel as the home: one read, one merge patch under the
// read resourceVersion, no loop. The record's previous channel comes back
// for the old-home line. A pick of the recorded channel is errHomeAlreadySet
// and writes nothing; a 409 is a homeMoveConflict naming what the record
// says now. recordHome's adopt loop stays the learn's: a move replaces on
// purpose.
func (s *SlackAdapter) moveHome(ctx context.Context, channel, member string) (previous string, err error) {
	l := &s.homeLearn
	cm, err := l.record.get(ctx)
	if err != nil {
		return "", err
	}
	previous = strings.TrimSpace(cm.Data[homeRecordKeySlack])
	if previous == channel {
		return previous, errHomeAlreadySet
	}
	now := time.Now().UTC().Format(time.RFC3339)
	err = l.record.patch(ctx, cm.ResourceVersion, map[string]*string{
		homeRecordKeySlack: &channel, homeRecordKeySetBy: &member, homeRecordKeySetAt: &now,
		homeRecordKeyWriter: &l.image, homeRecordKeyRefused: nil,
	})
	if apierrors.IsConflict(err) {
		if again, readErr := l.record.get(ctx); readErr == nil {
			return previous, &homeMoveConflict{winner: strings.TrimSpace(again.Data[homeRecordKeySlack]), by: again.Data[homeRecordKeySetBy]}
		}
		return previous, err
	}
	return previous, err
}

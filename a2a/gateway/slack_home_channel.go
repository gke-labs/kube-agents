package gateway

import (
	"context"
	"errors"
	"fmt"

	"github.com/slack-go/slack"
	"k8s.io/client-go/kubernetes"
)

// Slack's half of the learned home channel (home_channel.go has the shared
// learner): the candidate is a verified, addressed, top-level channel
// mention, the check is conversations.info, and the line is posted in the
// learning thread. A direct message is never a candidate, and a group direct
// message is refused without a call.
const (
	// slackHomeLogName prefixes the learner's log lines.
	slackHomeLogName = "slack"

	// slackChannelTypeMpIM is a group direct message's channel_type. The
	// InboundMessage.Kind of a non-DM turn is slackKindGroup (slack_stop.go).
	slackChannelTypeMpIM = "mpim"

	// homeLearnedLine is the one line posted in the learning thread. It names
	// the channel and the field that overrides it, never the member.
	homeLearnedLine = "<#%s> is now the home channel for alerts and scheduled reports. To move it, pick another channel on the app's Home tab, or set spec.integration.slack.homeChannel on the PlatformAgent."
)

// SetHomeLearner hands the adapter what a learn needs: the record, the image
// to stamp as the writer, and the directory the operator mounts the home
// channel in. Called before Run, read-only after.
func (s *SlackAdapter) SetHomeLearner(client kubernetes.Interface, namespace, name, image, fileDir string) {
	s.homeLearn.arm(client, namespace, name, image, homeFile{dir: fileDir, key: homeFileKey}, homeBackend{
		name: slackHomeLogName, keys: slackHomeRecordKeys, valid: slackIsHomeChannelID, check: s.checkHomeChannel, log: s.log,
	})
	s.homeLearn.followUp = slackHomePickPendingTTL
}

// LearnedHome is the home channel the operator's projection names, and
// whether the projection is mounted at all; the Notifier's home source. A
// value that is not a channel id (a hand edit of the projection, which the
// operator rewrites within a reconcile) is no home to post into.
func (s *SlackAdapter) LearnedHome() (string, bool) {
	return s.homeLearn.projected()
}

// homeCandidate starts a learn for a verified, addressed, top-level channel
// mention (InboundVerified) while the learner is wired and idle. Off the
// inbox worker and outside the adapter's lock: the file read, the check and
// the write happen on the learn goroutine.
func (s *SlackAdapter) homeCandidate(conversation, channelType, member string) {
	l := &s.homeLearn
	if !l.begin() {
		return
	}
	s.goSession(func() {
		defer l.wg.Done()
		s.learnHome(conversation, channelType, member)
	})
}

// closeHomeLearn stops new learns and waits for the ones in flight, so a
// write started before shutdown lands and none starts after Run's wait.
func (s *SlackAdapter) closeHomeLearn() { s.homeLearn.close() }

// learnHome runs one learn (homeLearner.learn) for a channel mention and
// posts the one line when it learned the channel, so nothing is announced
// that is not recorded. A group direct message is refused here, without a
// call.
func (s *SlackAdapter) learnHome(conversation, channelType, member string) {
	l := &s.homeLearn
	defer l.release()
	channel, threadTS, ok := slackChannelThread(conversation)
	if !ok || channelType == slackChannelTypeMpIM {
		s.log.Warn("slack: a group direct message cannot be the home channel", "conversation", conversation, "member", member)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), homeLearnTimeout)
	defer cancel()
	home, learned := l.learn(ctx, channel, member, conversation)
	if !learned {
		return
	}
	// Posted raw, not through Post: toMrkdwn escapes the <#C…> mention, and
	// the id is a checked channel id, so nothing else rides in the line.
	opts := []slack.MsgOption{slack.MsgOptionText(fmt.Sprintf(homeLearnedLine, home), false),
		slack.MsgOptionTS(threadTS), slack.MsgOptionDisableLinkUnfurl(), slack.MsgOptionDisableMediaUnfurl()}
	if _, _, err := s.api.PostMessage(channel, opts...); err != nil {
		s.log.Warn("slack: home channel line not posted", "channel", channel, "err", err)
	}
}

// checkHomeChannel is the gate a candidate or a recorded channel passes: a
// channel id within the bound, then one conversations.info. refused names the
// reason a definite answer refuses it; err is a check that could not be made,
// which is never a refusal. A channel_not_found is the bot token's view of a
// channel it is not in.
func (s *SlackAdapter) checkHomeChannel(ctx context.Context, id string) (refused string, err error) {
	if !slackIsHomeChannelID(id) || len([]rune(id)) > homeRecordIDMax {
		return homeRefusedMalformed, nil
	}
	info, err := s.api.GetConversationInfoContext(ctx, &slack.GetConversationInfoInput{ChannelID: id})
	if err != nil {
		var resp slack.SlackErrorResponse
		if errors.As(err, &resp) && resp.Err == slackChannelNotFound {
			return homeRefusedNotMember, nil
		}
		return "", err
	}
	switch {
	case info.IsIM || info.IsMpIM:
		// A direct or group direct message: a Home tab pick carries no
		// channel_type, so the answer itself says what the id is.
		return homeRefusedDirectMessage, nil
	case info.IsExtShared || info.IsPendingExtShared:
		return homeRefusedShared, nil
	case info.IsArchived:
		return homeRefusedArchived, nil
	case !info.IsMember:
		return homeRefusedNotMember, nil
	}
	return "", nil
}

// VerifyHomeForPost is the check a home post passes before it goes out
// (Notifier.SetHomeVerifier): the same conversations.info a learn makes. A
// channel Slack refuses now (shared, archived, the app removed, not a channel)
// is written to slack.refused beside the record while the record still names
// it, so the operator empties the projection and HomeChannelUnset names the
// cause; a check that cannot be made is an error and writes nothing.
func (s *SlackAdapter) VerifyHomeForPost(ctx context.Context, home string) (refused string, err error) {
	refused, err = s.checkHomeChannel(ctx, home)
	if err == nil && refused != "" {
		s.homeLearn.refuseNamedHome(ctx, home, refused)
	}
	return refused, err
}

package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/slack-go/slack"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
)

// The learned home channel. Under mode next with no CR home channel and the
// policy auto, the operator arms the gateway (Config.HomeLearnArmed) to make
// the channel of the first verified, addressed, top-level channel mention the
// home channel: it checks the channel with conversations.info, records it in
// the operator's record ConfigMap (data-only merge patches; the operator owns
// the object), arms its own chat.notify route on it and says so once in the
// learning thread. The operator reads the record to route the agent's
// proactive posts and to clear the HomeChannelUnset condition, so the record
// is written before anything is announced. At boot a recorded channel is
// checked again before it is adopted, and the reader key is stamped so the
// operator can tell a record this gateway image stands behind from a stale
// one. A direct message is never a candidate, and a group direct message is
// refused without a call.
const (
	// The record's data keys: the learned channel id, who set it and when,
	// why the last candidate was refused, the gateway image that first wrote
	// it (audit) and the one that last adopted it (what the operator trusts).
	homeRecordKeySlack   = "slack"
	homeRecordKeySetBy   = "slack.setBy"
	homeRecordKeySetAt   = "slack.setAt"
	homeRecordKeyRefused = "slack.refused"
	homeRecordKeyWriter  = "writer"
	homeRecordKeyReader  = "reader"

	// The refusal vocabulary the operator maps to a condition sentence.
	homeRefusedShared    = "shared"
	homeRefusedArchived  = "archived"
	homeRefusedNotMember = "not-member"
	homeRefusedMalformed = "malformed"

	// homeRecordIDMax bounds a recorded id: the grammar alone has no upper
	// bound, and a channel id is about eleven characters.
	homeRecordIDMax = 32
	// homeLearnTimeout bounds one learn or adopt: the channel check and the
	// record read and write together.
	homeLearnTimeout = 15 * time.Second
	// homeRecordAttempts is how many times a write is tried when the record
	// changed under it: the second read adopts the winner.
	homeRecordAttempts = 2

	// slackChannelTypeMpIM is a group direct message's channel_type. The
	// InboundMessage.Kind of a non-DM turn is slackKindGroup (slack_stop.go).
	slackChannelTypeMpIM = "mpim"

	// homeLearnedLine is the one line posted in the learning thread. It names
	// the channel and the field that overrides it, never the member.
	homeLearnedLine = "<#%s> is now the home channel for alerts and scheduled reports. To move it, set spec.integration.slack.homeChannel on the PlatformAgent."
)

// homeSetter arms a chat.notify route on a learned home channel; the
// Notifier implements it.
type homeSetter interface {
	SetHome(home string) error
}

// homeRecord reads and patches the record ConfigMap.
type homeRecord struct {
	client    kubernetes.Interface
	namespace string
	name      string
}

func (r homeRecord) get(ctx context.Context) (*corev1.ConfigMap, error) {
	return r.client.CoreV1().ConfigMaps(r.namespace).Get(ctx, r.name, metav1.GetOptions{})
}

// patch writes data keys with a JSON merge patch; a nil value deletes the
// key. resourceVersion is the precondition: a record that changed since the
// read answers 409 rather than being overwritten.
func (r homeRecord) patch(ctx context.Context, resourceVersion string, data map[string]*string) error {
	body, err := json.Marshal(map[string]any{
		"metadata": map[string]string{"resourceVersion": resourceVersion},
		"data":     data,
	})
	if err != nil {
		return err
	}
	_, err = r.client.CoreV1().ConfigMaps(r.namespace).Patch(ctx, r.name, types.MergePatchType, body, metav1.PatchOptions{})
	return err
}

// slackHomeLearn is the adapter's learned-home state, under its own lock.
type slackHomeLearn struct {
	mu     sync.Mutex
	record homeRecord
	setter homeSetter
	image  string
	// armed is set once the boot read found no channel to adopt; busy while
	// a learn runs; closed once Run is shutting down, so no learn starts
	// while Run waits for the ones in flight. The refusal on the record has
	// no copy here: the record is read before every refusal write.
	armed, busy, closed bool
	// wg counts the learn and adopt goroutines, so Run waits for a write in
	// flight before the gateway exits.
	wg sync.WaitGroup
}

// SetHomeLearner hands the adapter what a learn needs: the route to arm, the
// record and the image to stamp. Called before Run, read-only after.
func (s *SlackAdapter) SetHomeLearner(setter homeSetter, client kubernetes.Interface, namespace, name, image string) {
	s.homeLearn.setter = setter
	s.homeLearn.record = homeRecord{client: client, namespace: namespace, name: name}
	s.homeLearn.image = image
}

// AdoptHomeRecord reads the record once at boot, on its own goroutine so chat
// ingress never waits on it. No recorded channel arms the learner. A recorded
// one is checked again: a pass arms the route and stamps the reader (and
// clears a stale refusal); a definite refusal is written, nothing is adopted
// and the learner is armed, so the next admitted mention replaces the channel
// (archived after it was learned is the ordinary way here); a check that
// could not be made adopts the recorded channel without a re-stamp, so the
// operator never trusts a record this boot did not confirm, and the next boot
// checks again. A record that cannot be read leaves the learn off this boot:
// nothing could be written either.
func (s *SlackAdapter) AdoptHomeRecord(ctx context.Context) {
	l := &s.homeLearn
	if l.record.client == nil || l.setter == nil {
		return
	}
	l.mu.Lock()
	if l.closed {
		l.mu.Unlock()
		return
	}
	l.wg.Add(1)
	l.mu.Unlock()
	s.goSession(func() {
		defer l.wg.Done()
		ctx, cancel := context.WithTimeout(ctx, homeLearnTimeout)
		defer cancel()
		s.adoptHome(ctx)
	})
}

func (s *SlackAdapter) adoptHome(ctx context.Context) {
	l := &s.homeLearn
	cm, err := l.record.get(ctx)
	if err != nil {
		s.log.Warn("slack: home-channel record unread; the home channel is not learned this boot", "record", l.record.name, "err", err)
		return
	}
	id := strings.TrimSpace(cm.Data[homeRecordKeySlack])
	if id == "" {
		l.mu.Lock()
		l.armed = true
		l.mu.Unlock()
		s.log.Info("slack: no home channel recorded; the first admitted channel mention sets it", "record", l.record.name)
		return
	}
	refused, err := s.checkHomeChannel(ctx, id)
	switch {
	case err != nil:
		if setErr := l.setter.SetHome(id); setErr != nil {
			s.log.Error("slack: recorded home channel not armed", "channel", id, "err", setErr)
			return
		}
		s.log.Warn("slack: recorded home channel adopted without a check; the reader key is not stamped", "channel", id, "err", err)
		return
	case refused != "":
		s.log.Warn("slack: recorded home channel refused; the first admitted channel mention replaces it", "channel", id, "reason", refused)
		// A write that fails leaves the recorded channel without its reason
		// until the next boot or the mention that replaces it; refuseHome
		// never fills it in, because the reason it holds is a candidate's.
		if cm.Data[homeRecordKeyRefused] != refused {
			s.writeRefusal(ctx, cm, refused)
		}
		l.mu.Lock()
		l.armed = true
		l.mu.Unlock()
		return
	}
	if err := l.setter.SetHome(id); err != nil {
		s.log.Error("slack: recorded home channel not armed", "channel", id, "err", err)
		return
	}
	s.log.Info("slack: home channel adopted from the record", "channel", id)
	s.restampReader(ctx, cm)
}

// restampReader marks a record this gateway image stands behind: the reader
// key set to the image, the writer kept (or set when absent) and a stale
// refusal cleared. Nothing is written when the record already says so.
func (s *SlackAdapter) restampReader(ctx context.Context, cm *corev1.ConfigMap) {
	l := &s.homeLearn
	if cm.Data[homeRecordKeyReader] == l.image && cm.Data[homeRecordKeyWriter] != "" && cm.Data[homeRecordKeyRefused] == "" {
		return
	}
	writer := cm.Data[homeRecordKeyWriter]
	if writer == "" {
		writer = l.image
	}
	if err := l.record.patch(ctx, cm.ResourceVersion, map[string]*string{
		homeRecordKeyReader: &l.image, homeRecordKeyWriter: &writer, homeRecordKeyRefused: nil,
	}); err != nil {
		s.log.Warn("slack: home-channel record not re-stamped", "record", l.record.name, "err", err)
	}
}

// homeCandidate starts a learn for a verified, addressed, top-level channel
// mention (InboundVerified) while the learner is armed and idle. Off the
// inbox worker: the check and the write are network calls.
func (s *SlackAdapter) homeCandidate(conversation, channelType, member string) {
	l := &s.homeLearn
	l.mu.Lock()
	if !l.armed || l.busy || l.closed {
		l.mu.Unlock()
		return
	}
	l.busy = true
	l.wg.Add(1)
	l.mu.Unlock()
	s.goSession(func() {
		defer l.wg.Done()
		s.learnHome(conversation, channelType, member)
	})
}

// closeHomeLearn stops new learns and waits for the ones in flight, so a
// write started before shutdown lands and none starts after Run's wait.
func (s *SlackAdapter) closeHomeLearn() {
	l := &s.homeLearn
	l.mu.Lock()
	l.closed = true
	l.mu.Unlock()
	l.wg.Wait()
}

// learnHome checks the candidate, writes the record, arms the route and posts
// the one line, in that order, so nothing is announced that is not recorded.
// A refused or unchecked candidate releases the learner for the next mention.
func (s *SlackAdapter) learnHome(conversation, channelType, member string) {
	l := &s.homeLearn
	learned := false
	defer func() {
		l.mu.Lock()
		l.busy = false
		l.armed = l.armed && !learned
		l.mu.Unlock()
	}()
	channel, threadTS, ok := slackChannelThread(conversation)
	if !ok || channelType == slackChannelTypeMpIM {
		s.log.Warn("slack: a group direct message cannot be the home channel", "conversation", conversation, "member", member)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), homeLearnTimeout)
	defer cancel()
	refused, err := s.checkHomeChannel(ctx, channel)
	if err != nil {
		s.log.Warn("slack: could not check the channel as a home channel; the next mention tries again", "channel", channel, "err", err)
		return
	}
	if refused != "" {
		s.log.Warn("slack: channel refused as the home channel", "channel", channel, "reason", refused, "member", member)
		s.refuseHome(ctx, refused)
		return
	}
	home, err := s.recordHome(ctx, channel, member)
	if err != nil {
		s.log.Error("slack: home channel not recorded; the next mention tries again", "channel", channel, "err", err)
		return
	}
	if err := l.setter.SetHome(home); err != nil {
		s.log.Error("slack: home channel recorded but not armed", "channel", home, "err", err)
		return
	}
	learned = true
	s.log.Info("slack: home channel learned", "channel", home, "setBy", member, "conversation", conversation)
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
	case info.IsExtShared || info.IsPendingExtShared:
		return homeRefusedShared, nil
	case info.IsArchived:
		return homeRefusedArchived, nil
	case !info.IsMember:
		return homeRefusedNotMember, nil
	}
	return "", nil
}

// refuseHome records why a candidate was refused. The record decides: while
// it names a channel, slack.refused describes that channel and only the boot
// check writes it, so a refused candidate writes nothing; otherwise the reason
// is written unless the record names it already, so a channel that keeps
// being refused is one log line per mention, not one write, and a read or a
// write that failed is tried again at the next refusal.
func (s *SlackAdapter) refuseHome(ctx context.Context, reason string) {
	l := &s.homeLearn
	cm, err := l.record.get(ctx)
	if err != nil {
		s.log.Warn("slack: home-channel refusal not recorded", "record", l.record.name, "err", err)
		return
	}
	if strings.TrimSpace(cm.Data[homeRecordKeySlack]) != "" || cm.Data[homeRecordKeyRefused] == reason {
		return
	}
	s.writeRefusal(ctx, cm, reason)
}

func (s *SlackAdapter) writeRefusal(ctx context.Context, cm *corev1.ConfigMap, reason string) {
	l := &s.homeLearn
	if err := l.record.patch(ctx, cm.ResourceVersion, map[string]*string{homeRecordKeyRefused: &reason}); err != nil {
		s.log.Warn("slack: home-channel refusal not recorded", "record", l.record.name, "err", err)
	}
}

// recordHome writes the learned channel, unless the record names one already:
// then that one is checked and adopted instead, with the reader re-stamped as
// at boot and nothing else written; a recorded channel the check refuses (the
// one a boot refused, or one archived since another writer learned it) is
// replaced. A write the record changed under (409) reads again and does the
// same. The answer is the channel to arm.
func (s *SlackAdapter) recordHome(ctx context.Context, channel, member string) (string, error) {
	l := &s.homeLearn
	for attempt := 0; attempt < homeRecordAttempts; attempt++ {
		cm, err := l.record.get(ctx)
		if err != nil {
			return "", err
		}
		if winner := strings.TrimSpace(cm.Data[homeRecordKeySlack]); winner != "" {
			refused, err := s.checkHomeChannel(ctx, winner)
			if err != nil {
				return "", fmt.Errorf("the record already names %q, which could not be checked: %w", winner, err)
			}
			if refused == "" {
				s.log.Info("slack: home channel recorded by another writer; adopting it", "channel", winner)
				s.restampReader(ctx, cm)
				return winner, nil
			}
			s.log.Warn("slack: recorded home channel refused; replacing it", "channel", winner, "reason", refused, "replacement", channel)
		}
		now := time.Now().UTC().Format(time.RFC3339)
		err = l.record.patch(ctx, cm.ResourceVersion, map[string]*string{
			homeRecordKeySlack: &channel, homeRecordKeySetBy: &member, homeRecordKeySetAt: &now,
			homeRecordKeyWriter: &l.image, homeRecordKeyReader: &l.image, homeRecordKeyRefused: nil,
		})
		if err == nil {
			return channel, nil
		}
		if !apierrors.IsConflict(err) {
			return "", err
		}
	}
	return "", errors.New("the record changed under every write")
}

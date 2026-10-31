package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
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
// the object) and says so once in the learning thread. The operator reads
// the record, admits it, and projects the home channel into a ConfigMap it
// mounts as a directory in this pod and in the agent pod; the notify route
// and the learner read that file, so the gateway keeps no copy of the home
// channel and a learned value reaches no pod definition. The record is
// written before anything is announced. A direct message is never a
// candidate, and a group direct message is refused without a call.
const (
	// The record's data keys: the learned channel id, who set it and when,
	// why the last candidate was refused, and the gateway image that wrote
	// it (audit).
	homeRecordKeySlack   = "slack"
	homeRecordKeySetBy   = "slack.setBy"
	homeRecordKeySetAt   = "slack.setAt"
	homeRecordKeyRefused = "slack.refused"
	homeRecordKeyWriter  = "writer"

	// HomeFileDir is where the operator mounts its projection of the home
	// channel in this pod, and homeFileKey its one file: the channel id the
	// operator admitted (the CR field, else the record), or empty. A
	// directory that is not mounted is an operator older than the projection.
	HomeFileDir = "/etc/a2a/chat-home"
	homeFileKey = "slack_home"

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

// homeFile reads the operator's projection: the trimmed file, and whether it
// could be read at all. A file that cannot be read is absent: no candidate
// and no home come from it, and the caller's log names it apart from an
// empty value.
type homeFile struct{ dir string }

func (f homeFile) read() (value string, present bool) {
	raw, err := os.ReadFile(filepath.Join(f.dir, homeFileKey))
	if err != nil {
		return "", false
	}
	return strings.TrimSpace(string(raw)), true
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

// slackHomeLearn is the adapter's learner state, under its own lock. It
// holds no home channel: the file decides whether a mention is a candidate.
type slackHomeLearn struct {
	mu     sync.Mutex
	record homeRecord
	file   homeFile
	image  string
	// busy while a learn runs; closed once Run is shutting down, so no learn
	// starts while Run waits for the ones in flight. The refusal on the
	// record has no copy here: the record is read before every refusal write.
	busy, closed bool
	// wg counts the learn goroutines, so Run waits for a write in flight
	// before the gateway exits.
	wg sync.WaitGroup
}

// SetHomeLearner hands the adapter what a learn needs: the record, the image
// to stamp as the writer, and the directory the operator mounts the home
// channel in. Called before Run, read-only after.
func (s *SlackAdapter) SetHomeLearner(client kubernetes.Interface, namespace, name, image, fileDir string) {
	s.homeLearn.record = homeRecord{client: client, namespace: namespace, name: name}
	s.homeLearn.image = image
	s.homeLearn.file = homeFile{dir: fileDir}
}

// LearnedHome is the home channel the operator's projection names, and
// whether the projection is mounted at all; the Notifier's home source. A
// value that is not a channel id (a hand edit of the projection, which the
// operator rewrites within a reconcile) is no home to post into.
func (s *SlackAdapter) LearnedHome() (string, bool) {
	value, present := s.homeLearn.file.read()
	if !slackIsHomeChannelID(value) {
		return "", present
	}
	return value, present
}

// homeCandidate starts a learn for a verified, addressed, top-level channel
// mention (InboundVerified) while the learner is wired and idle. Off the
// inbox worker and outside the adapter's lock: the file read, the check and
// the write happen on the learn goroutine.
func (s *SlackAdapter) homeCandidate(conversation, channelType, member string) {
	l := &s.homeLearn
	l.mu.Lock()
	if l.record.client == nil || l.busy || l.closed {
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

// learnHome reads the file, checks the candidate, writes the record and
// posts the one line, in that order, so nothing is announced that is not
// recorded. A mention is a candidate while the file is empty, or while the
// home it names is one Slack now refuses (a mention outside the home checks
// it once; recordHome then replaces it): a file that is absent (no
// projection mounted) learns nothing, and one that holds a hand edit the
// operator is about to rewrite means a home is about to. The window between a learn and the
// file catching up (one kubelet sync) makes the next mention a candidate
// too; recordHome then adopts the recorded channel and posts no line. A
// refused or unchecked candidate releases the learner for the next mention.
func (s *SlackAdapter) learnHome(conversation, channelType, member string) {
	l := &s.homeLearn
	defer func() {
		l.mu.Lock()
		l.busy = false
		l.mu.Unlock()
	}()
	channel, threadTS, ok := slackChannelThread(conversation)
	if !ok || channelType == slackChannelTypeMpIM {
		s.log.Warn("slack: a group direct message cannot be the home channel", "conversation", conversation, "member", member)
		return
	}
	home, present := l.file.read()
	if !present {
		s.log.Info("slack: home-channel file absent; nothing is learned until the operator mounts its projection", "dir", l.file.dir)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), homeLearnTimeout)
	defer cancel()
	if home != "" {
		// A named home stays the home unless Slack refuses it now (archived
		// since it was learned, the app removed): then this mention is a
		// candidate, and recordHome replaces the recorded channel. The
		// check runs once per top-level mention outside the home; a mention
		// in the home proves it alive, and a value that is not a channel id
		// (a hand edit of the projection) is the operator's to rewrite.
		if channel == home || !slackIsHomeChannelID(home) {
			return
		}
		refused, err := s.checkHomeChannel(ctx, home)
		if err != nil || refused == "" {
			return
		}
		s.log.Warn("slack: the home channel is refused now; this mention may replace it", "home", home, "reason", refused, "candidate", channel)
	}
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
	home, adopted, err := s.recordHome(ctx, channel, member)
	if err != nil {
		s.log.Error("slack: home channel not recorded; the next mention tries again", "channel", channel, "err", err)
		return
	}
	if adopted {
		s.log.Info("slack: home channel recorded already; the file follows the record", "channel", home)
		return
	}
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
// it names a channel, slack.refused is the post-time check's to write
// (VerifyHomeForPost), or an older gateway's, and a learn that adopts or
// replaces the channel clears it, so a refused candidate writes nothing; otherwise
// the reason is written unless the record names it already, so a channel
// that keeps being refused is one log line per mention, not one write, and a
// read or a write that failed is tried again at the next refusal.
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

// VerifyHomeForPost is the check a home post passes before it goes out
// (Notifier.SetHomeVerifier): the same conversations.info a learn makes. A
// channel Slack refuses now (shared, archived, the app removed, not a channel)
// is written to slack.refused beside the record while the record still names
// it, so the operator empties the projection and HomeChannelUnset names the
// cause; a check that cannot be made is an error and writes nothing.
func (s *SlackAdapter) VerifyHomeForPost(ctx context.Context, home string) (refused string, err error) {
	refused, err = s.checkHomeChannel(ctx, home)
	if err == nil && refused != "" {
		s.refuseNamedHome(ctx, home, refused)
	}
	return refused, err
}

// refuseNamedHome writes reason beside channel while the record still names
// channel and does not name that reason already: the sibling of refuseHome
// for a record that names a channel. A record the learn or a hand edit moved
// on is not relabelled.
func (s *SlackAdapter) refuseNamedHome(ctx context.Context, channel, reason string) {
	l := &s.homeLearn
	l.mu.Lock()
	armed := l.record.client != nil
	l.mu.Unlock()
	if !armed {
		return
	}
	cm, err := l.record.get(ctx)
	if err != nil {
		s.log.Warn("slack: home-channel refusal not recorded", "record", l.record.name, "err", err)
		return
	}
	if strings.TrimSpace(cm.Data[homeRecordKeySlack]) != channel || cm.Data[homeRecordKeyRefused] == reason {
		return
	}
	s.log.Warn("slack: the home channel is refused now; recorded so the operator and the next mention act on it", "channel", channel, "reason", reason)
	s.writeRefusal(ctx, cm, reason)
}

// recordHome writes the learned channel, unless the record names one already:
// then that one is checked and kept instead (adopted), with a refusal beside
// it (the post-time check's, or an older gateway's) cleared when the check
// passes now, so the operator admits it, and nothing else written; a
// recorded channel the check refuses (one archived since another writer
// learned it) is replaced. A write the record changed under (409) reads
// again and does the same. The answer is the channel the record names and
// whether it was there already.
func (s *SlackAdapter) recordHome(ctx context.Context, channel, member string) (home string, adopted bool, err error) {
	l := &s.homeLearn
	for attempt := 0; attempt < homeRecordAttempts; attempt++ {
		cm, err := l.record.get(ctx)
		if err != nil {
			return "", false, err
		}
		if winner := strings.TrimSpace(cm.Data[homeRecordKeySlack]); winner != "" {
			refused, err := s.checkHomeChannel(ctx, winner)
			if err != nil {
				return "", false, fmt.Errorf("the record already names %q, which could not be checked: %w", winner, err)
			}
			if refused == "" {
				if cm.Data[homeRecordKeyRefused] != "" {
					if err := l.record.patch(ctx, cm.ResourceVersion, map[string]*string{homeRecordKeyRefused: nil}); err != nil {
						return "", false, fmt.Errorf("the record names %q beside a refusal that could not be cleared: %w", winner, err)
					}
				}
				return winner, true, nil
			}
			s.log.Warn("slack: recorded home channel refused; replacing it", "channel", winner, "reason", refused, "replacement", channel)
		}
		now := time.Now().UTC().Format(time.RFC3339)
		err = l.record.patch(ctx, cm.ResourceVersion, map[string]*string{
			homeRecordKeySlack: &channel, homeRecordKeySetBy: &member, homeRecordKeySetAt: &now,
			homeRecordKeyWriter: &l.image, homeRecordKeyRefused: nil,
		})
		if err == nil {
			return channel, false, nil
		}
		if !apierrors.IsConflict(err) {
			return "", false, err
		}
	}
	return "", false, errors.New("the record changed under every write")
}

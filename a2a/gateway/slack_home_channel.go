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
// is written before anything is announced. While the gateway runs it keeps
// its home in step with the record (RunHomeReconciler): a recorded channel
// is checked before it is adopted, the reader key is stamped so the operator
// can tell a record this gateway image stands behind from a stale one, and a
// read, a check or a write that failed is tried again rather than logged
// once. A direct message is never a candidate, and a group direct message is
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
	// homeReconcileEvery is how often the record is read again while the
	// gateway runs, so a hand edit, or a write that failed, converges without
	// a restart; a settled record costs one read and no Slack call.
	homeReconcileEvery = 30 * time.Second
	// homeRetryMin and homeRetryMax bound the wait after a pass that failed
	// (a read, a check or a write), doubling from one to the other: a boot
	// blip converges in seconds and an outage costs two reads a minute.
	homeRetryMin = 2 * time.Second
	homeRetryMax = 30 * time.Second

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
	// armed is set once a pass found no channel to adopt; busy while a learn
	// runs; closed once Run is shutting down, so no learn starts while Run
	// waits for the ones in flight. The refusal on the record has no copy
	// here: the record is read before every refusal write.
	armed, busy, closed bool
	// adopted is the channel this gateway armed its route on, "" for none;
	// settled is the record state the last successful pass left behind, so
	// a pass that reads the same state does nothing more.
	adopted string
	settled homeRecordState
	// wg counts the learn and reconcile goroutines, so Run waits for a write
	// in flight before the gateway exits; done ends the reconcile loop's
	// wait; after is the loop's timer, a test's hook.
	wg    sync.WaitGroup
	done  chan struct{}
	after func(time.Duration) <-chan time.Time
}

// homeRecordState is what a pass compares: the recorded channel, the refusal
// beside it, and whether this gateway image has stamped it.
type homeRecordState struct {
	id, refused string
	stamped     bool
}

func homeStateOf(cm *corev1.ConfigMap, image string) homeRecordState {
	return homeRecordState{
		id:      strings.TrimSpace(cm.Data[homeRecordKeySlack]),
		refused: cm.Data[homeRecordKeyRefused],
		stamped: cm.Data[homeRecordKeyReader] == image && cm.Data[homeRecordKeyWriter] != "",
	}
}

// SetHomeLearner hands the adapter what a learn needs: the route to arm, the
// record and the image to stamp. Called before Run, read-only after.
func (s *SlackAdapter) SetHomeLearner(setter homeSetter, client kubernetes.Interface, namespace, name, image string) {
	s.homeLearn.setter = setter
	s.homeLearn.record = homeRecord{client: client, namespace: namespace, name: name}
	s.homeLearn.image = image
	s.homeLearn.done = make(chan struct{})
	if s.homeLearn.after == nil {
		s.homeLearn.after = time.After
	}
}

// RunHomeReconciler keeps the gateway's home channel in step with the record
// while it runs: the first pass now, on a session goroutine so chat ingress
// never waits on it, then one pass every homeReconcileEvery, and sooner after
// a pass that failed (homeRetryMin doubling to homeRetryMax), until ctx ends
// or Run shuts down. A pass is level-triggered: it reads the record and acts
// on the difference between it and what this gateway last settled, so a
// read, a check or a write that failed is tried again rather than logged
// once, a hand edit is checked within a pass, and a running gateway has
// always acted on the record it shows. Nothing is armed before a read and a
// check succeed.
func (s *SlackAdapter) RunHomeReconciler(ctx context.Context) {
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
		err := s.reconcileHomePass(ctx)
		// Counted while this goroutine still is, so Run's wait covers the
		// loop; it ends on ctx or on closeHomeLearn's done.
		l.wg.Add(1)
		go s.reconcileHomeLoop(ctx, err)
	})
}

func (s *SlackAdapter) reconcileHomeLoop(ctx context.Context, err error) {
	l := &s.homeLearn
	defer l.wg.Done()
	retry := homeRetryMin
	for {
		next := homeReconcileEvery
		if err != nil {
			next, retry = retry, min(retry*2, homeRetryMax)
			s.log.Warn("slack: home-channel record not reconciled; trying again", "record", l.record.name, "in", next, "err", err)
		} else {
			retry = homeRetryMin
		}
		select {
		case <-ctx.Done():
			return
		case <-l.done:
			return
		case <-l.after(next):
		}
		err = s.reconcileHomePass(ctx)
	}
}

// reconcileHomePass is one pass. An empty record arms the learner. One that
// names a channel is left alone when it is the state the last pass settled;
// otherwise the channel is checked and the record is made to agree: the
// route armed and the reader stamped, or the refusal written and the learner
// armed, so the next admitted mention replaces the channel (archived after
// it was learned is the ordinary way here). The error is what the loop
// retries. A pass during a learn does nothing: the learn writes the record.
func (s *SlackAdapter) reconcileHomePass(ctx context.Context) error {
	l := &s.homeLearn
	l.mu.Lock()
	busy, settled, adopted := l.busy, l.settled, l.adopted
	l.mu.Unlock()
	if busy {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, homeLearnTimeout)
	defer cancel()
	cm, err := l.record.get(ctx)
	if err != nil {
		return fmt.Errorf("record not read: %w", err)
	}
	state := homeStateOf(cm, l.image)
	if state.id == "" {
		l.mu.Lock()
		armed := l.armed
		l.armed = true
		l.mu.Unlock()
		if !armed {
			s.log.Info("slack: no home channel recorded; the first admitted channel mention sets it", "record", l.record.name)
		}
		return nil
	}
	// Settled only when the route agrees too: a pass and a learn that
	// overlapped can leave the record on the learn's channel and the route
	// on the pass's, and the next pass must then arm the record's.
	if state == settled && (state.refused != "" || adopted == state.id) {
		return nil
	}
	refused, err := s.checkHomeChannel(ctx, state.id)
	if err != nil {
		return fmt.Errorf("channel %s not checked: %w", state.id, err)
	}
	if refused != "" {
		if state.refused != refused {
			if err := l.record.patch(ctx, cm.ResourceVersion, map[string]*string{homeRecordKeyRefused: &refused}); err != nil {
				return fmt.Errorf("refusal not recorded: %w", err)
			}
			s.log.Warn("slack: recorded home channel refused; the first admitted channel mention replaces it", "channel", state.id, "reason", refused)
		}
		l.mu.Lock()
		l.armed = true
		l.settled = homeRecordState{id: state.id, refused: refused, stamped: state.stamped}
		l.mu.Unlock()
		return nil
	}
	if adopted != state.id {
		// SetHome and the adopted write under one lock, so the channel the
		// route holds is the one adopted names whichever of a pass and a
		// learn armed last.
		l.mu.Lock()
		err := l.setter.SetHome(state.id)
		if err == nil {
			l.adopted = state.id
		}
		l.mu.Unlock()
		if err != nil {
			return fmt.Errorf("channel %s not armed: %w", state.id, err)
		}
		s.log.Info("slack: home channel adopted from the record", "channel", state.id)
	}
	if err := s.restampReader(ctx, cm); err != nil {
		return err
	}
	l.mu.Lock()
	l.armed = false
	l.settled = homeRecordState{id: state.id, stamped: true}
	l.mu.Unlock()
	return nil
}

// restampReader marks a record this gateway image stands behind: the reader
// key set to the image, the writer kept (or set when absent) and a stale
// refusal cleared. Nothing is written when the record already says so.
func (s *SlackAdapter) restampReader(ctx context.Context, cm *corev1.ConfigMap) error {
	l := &s.homeLearn
	if cm.Data[homeRecordKeyReader] == l.image && cm.Data[homeRecordKeyWriter] != "" && cm.Data[homeRecordKeyRefused] == "" {
		return nil
	}
	writer := cm.Data[homeRecordKeyWriter]
	if writer == "" {
		writer = l.image
	}
	if err := l.record.patch(ctx, cm.ResourceVersion, map[string]*string{
		homeRecordKeyReader: &l.image, homeRecordKeyWriter: &writer, homeRecordKeyRefused: nil,
	}); err != nil {
		return fmt.Errorf("record not re-stamped: %w", err)
	}
	return nil
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

// closeHomeLearn stops new learns and the reconcile loop and waits for the
// pass or learn in flight, so a write started before shutdown lands and none
// starts after Run's wait.
func (s *SlackAdapter) closeHomeLearn() {
	l := &s.homeLearn
	l.mu.Lock()
	if !l.closed {
		l.closed = true
		if l.done != nil {
			close(l.done)
		}
	}
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
	home, stamped, err := s.recordHome(ctx, channel, member)
	if err != nil {
		s.log.Error("slack: home channel not recorded; the next mention tries again", "channel", channel, "err", err)
		return
	}
	// SetHome and what the next pass compares against under one lock (as
	// in reconcileHomePass): a record this image stamped, or nothing when
	// the re-stamp failed, so the pass stamps it.
	l.mu.Lock()
	err = l.setter.SetHome(home)
	if err == nil {
		l.adopted = home
		l.settled = homeRecordState{}
		if stamped {
			l.settled = homeRecordState{id: home, stamped: true}
		}
	}
	l.mu.Unlock()
	if err != nil {
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
// it names a channel, slack.refused describes that channel and only a reconcile
// pass writes it, so a refused candidate writes nothing; otherwise the reason
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
// a pass does and nothing else written; a recorded channel the check refuses
// (one a pass refused, or one archived since another writer learned it) is
// replaced. A write the record changed under (409) reads again and does the
// same. The answer is the channel to arm and whether this image stamped it.
func (s *SlackAdapter) recordHome(ctx context.Context, channel, member string) (home string, stamped bool, err error) {
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
				s.log.Info("slack: home channel recorded by another writer; adopting it", "channel", winner)
				if err := s.restampReader(ctx, cm); err != nil {
					s.log.Warn("slack: home-channel record not re-stamped; the next pass stamps it", "record", l.record.name, "err", err)
					return winner, false, nil
				}
				return winner, true, nil
			}
			s.log.Warn("slack: recorded home channel refused; replacing it", "channel", winner, "reason", refused, "replacement", channel)
		}
		now := time.Now().UTC().Format(time.RFC3339)
		err = l.record.patch(ctx, cm.ResourceVersion, map[string]*string{
			homeRecordKeySlack: &channel, homeRecordKeySetBy: &member, homeRecordKeySetAt: &now,
			homeRecordKeyWriter: &l.image, homeRecordKeyReader: &l.image, homeRecordKeyRefused: nil,
		})
		if err == nil {
			return channel, true, nil
		}
		if !apierrors.IsConflict(err) {
			return "", false, err
		}
	}
	return "", false, errors.New("the record changed under every write")
}

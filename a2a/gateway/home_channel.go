package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
)

// The learned home channel, the half every chat backend shares. Under mode
// next with no CR home channel and the policy auto, the operator arms the
// gateway (Config.HomeLearnArmed for Slack, Config.GchatHomeLearnArmed for
// Google Chat) to make the channel of the first verified, addressed,
// top-level mention outside a direct message the home channel: the backend
// checks the channel, the learner records it in the operator's record
// ConfigMap (data-only merge patches; the operator owns the object) and the
// backend says so once in the learning thread. The operator reads the record,
// admits it, and projects the home channel into a ConfigMap it mounts as a
// directory in this pod and in the agent pod, one file per backend; the
// notify route and the learner read that file, so the gateway keeps no copy
// of the home channel and a learned value reaches no pod definition. The
// record is written before anything is announced. Each backend has its own
// record keys and its own file, so a gateway that holds both keeps two homes
// in the one record.
//
// What is backend-specific stays in the adapter: which inbound turn is a
// candidate, what a channel id looks like, how the channel is checked and how
// the line is posted (slack_home_channel.go, gchat_home_channel.go).
const (
	// The record's data keys, per backend: the learned channel id, who set
	// it and when, and why the last candidate was refused. writer is the
	// gateway image that wrote last, whichever backend it wrote for (audit).
	homeRecordKeySlack   = "slack"
	homeRecordKeySetBy   = "slack.setBy"
	homeRecordKeySetAt   = "slack.setAt"
	homeRecordKeyRefused = "slack.refused"

	homeRecordKeyGchat        = "gchat"
	homeRecordKeyGchatSetBy   = "gchat.setBy"
	homeRecordKeyGchatSetAt   = "gchat.setAt"
	homeRecordKeyGchatRefused = "gchat.refused"

	homeRecordKeyWriter = "writer"

	// HomeFileDir is where the operator mounts its projection of the home
	// channels in this pod; homeFileKey and gchatHomeFileKey are its files:
	// the channel the operator admitted for the backend (the CR field, else
	// the record), or empty. A directory that is not mounted is an operator
	// older than the projection; a file that is missing from a mounted
	// directory is an operator older than that backend's learn.
	HomeFileDir      = "/etc/a2a/chat-home"
	homeFileKey      = "slack_home"
	gchatHomeFileKey = "gchat_home"

	// The refusal vocabulary the operator maps to a condition sentence
	// (homeChannelRefusedReasons in k8s-operator/internal/controller/
	// platformagent_home_channel_record.go, a module that cannot import this
	// one). TestHomeChannelRefusedReasonsMatchTheGatewaysVocabulary there
	// parses this file and collects every homeRefused* string constant, so a
	// new word is declared here, with that prefix, as a literal; one declared
	// otherwise passes that test and renders in the operator's condition as a
	// reason it does not know. Both backends write from this one list:
	// shared is a Slack Connect channel or a Chat space that admits people
	// outside the organization, and archived is Slack's alone.
	homeRefusedShared    = "shared"
	homeRefusedArchived  = "archived"
	homeRefusedNotMember = "not-member"
	homeRefusedMalformed = "malformed"
	// A well-formed id that names a DM or a group DM, not a channel; an
	// operator without the word renders it as a reason it does not know.
	homeRefusedDirectMessage = "direct-message"

	// homeRecordIDMax bounds a recorded Slack id: the grammar alone has no
	// upper bound, and a channel id is about eleven characters.
	homeRecordIDMax = 32
	// homeLearnTimeout bounds one learn or adopt: the channel check and the
	// record read and write together.
	homeLearnTimeout = 15 * time.Second
	// homeRecordAttempts is how many times a write is tried when the record
	// changed under it: the second read adopts the winner.
	homeRecordAttempts = 2
)

// HomeLearner is a chat backend that can learn its home channel: the Slack
// and Google Chat adapters. cmd/gateway arms it when the operator armed the
// learn for the backend, and points the backend's notify route at
// LearnedHome and VerifyHomeForPost.
type HomeLearner interface {
	SetHomeLearner(client kubernetes.Interface, namespace, name, image, fileDir string)
	LearnedHome() (home string, present bool)
	VerifyHomeForPost(ctx context.Context, home string) (refused string, err error)
}

var (
	_ HomeLearner = (*SlackAdapter)(nil)
	_ HomeLearner = (*GoogleChatAdapter)(nil)
)

// homeRecordKeys names one backend's keys in the record.
type homeRecordKeys struct{ channel, setBy, setAt, refused string }

var (
	slackHomeRecordKeys = homeRecordKeys{channel: homeRecordKeySlack, setBy: homeRecordKeySetBy, setAt: homeRecordKeySetAt, refused: homeRecordKeyRefused}
	gchatHomeRecordKeys = homeRecordKeys{channel: homeRecordKeyGchat, setBy: homeRecordKeyGchatSetBy, setAt: homeRecordKeyGchatSetAt, refused: homeRecordKeyGchatRefused}
)

// homeFile reads the operator's projection of one backend's home: the
// trimmed file, and whether it could be read at all. A file that cannot be
// read is absent: no candidate and no home come from it, and the caller's
// log names it apart from an empty value.
type homeFile struct{ dir, key string }

func (f homeFile) read() (value string, present bool) {
	raw, err := os.ReadFile(filepath.Join(f.dir, f.key))
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

// homeBackend is what the shared learner asks of a chat backend: its name in
// log lines, its record keys, whether a value is a channel id of its
// grammar, and the gate a candidate or a recorded channel passes (refused
// names the reason a definite answer refuses it; err is a check that could
// not be made, which is never a refusal).
type homeBackend struct {
	name  string
	keys  homeRecordKeys
	valid func(id string) bool
	check func(ctx context.Context, id string) (refused string, err error)
	log   *slog.Logger
}

// homeLearner is one adapter's learner state, under its own lock. It holds
// no home channel: the file decides whether a mention is a candidate.
type homeLearner struct {
	mu      sync.Mutex
	record  homeRecord
	file    homeFile
	image   string
	backend homeBackend
	// busy while a learn runs; closed once Run is shutting down, so no learn
	// starts while Run waits for the ones in flight. The refusal on the
	// record has no copy here: the record is read before every refusal write.
	busy, closed bool
	// wg counts the learn goroutines, so Run waits for a write in flight
	// before the gateway exits.
	wg sync.WaitGroup
	// followUp is how long a Slack Home tab move waits for the projected file
	// to follow before it says the operator has not applied it
	// (slack_home_pick.go); tests shorten it.
	followUp time.Duration
	// done closes in close, so a Home tab move's wait for the projected file
	// ends at once at shutdown.
	done chan struct{}
}

// arm hands the learner what a learn needs: the record, the image to stamp
// as the writer, the projected file and the backend's half. Called before
// Run, read-only after.
func (l *homeLearner) arm(client kubernetes.Interface, namespace, name, image string, file homeFile, backend homeBackend) {
	l.record = homeRecord{client: client, namespace: namespace, name: name}
	l.image = image
	l.file = file
	l.backend = backend
	l.done = make(chan struct{})
}

// armed reports whether the learner holds a record client.
func (l *homeLearner) armed() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.record.client != nil
}

// begin claims the learner for one learn while it is wired and idle, and
// counts the goroutine the caller starts for it; the caller's goroutine
// calls wg.Done, and release when the learn is over.
func (l *homeLearner) begin() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.record.client == nil || l.busy || l.closed {
		return false
	}
	l.busy = true
	l.wg.Add(1)
	return true
}

// release frees the learner for the next mention.
func (l *homeLearner) release() {
	l.mu.Lock()
	l.busy = false
	l.mu.Unlock()
}

// close stops new learns and waits for the ones in flight, so a write
// started before shutdown lands and none starts after Run's wait.
func (l *homeLearner) close() {
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

// projected is the home the operator's projection names, and whether the
// projection is mounted at all; the Notifier's home source. A value that is
// not a channel id of the backend's grammar (a hand edit of the projection,
// which the operator rewrites within a reconcile) is no home to post into.
func (l *homeLearner) projected() (string, bool) {
	value, present := l.file.read()
	if l.backend.valid == nil || !l.backend.valid(value) {
		return "", present
	}
	return value, present
}

// learn reads the file, checks the candidate and writes the record, in that
// order, and answers the channel it recorded and true when this call learned
// it: the caller then posts the one line, so nothing is announced that is
// not recorded. A mention is a candidate while the file is empty, or while
// the home it names is one the backend now refuses (a mention outside the
// home checks it once; recordHome then replaces it): a file that is absent
// (no projection mounted) learns nothing, and one that holds a hand edit the
// operator is about to rewrite means a home is about to. The window between a
// learn and the file catching up (one kubelet sync) makes the next mention a
// candidate too; recordHome then adopts the recorded channel and learn
// answers false. A refused or unchecked candidate leaves the learner free for
// the next mention.
func (l *homeLearner) learn(ctx context.Context, channel, member, conversation string) (string, bool) {
	b := l.backend
	home, present := l.file.read()
	if !present {
		b.log.Info(b.name+": home-channel file absent; nothing is learned until the operator mounts its projection", "dir", l.file.dir, "file", l.file.key)
		return "", false
	}
	if home != "" {
		// A named home stays the home unless the backend refuses it now
		// (archived since it was learned, the app removed): then this mention
		// is a candidate, and recordHome replaces the recorded channel. The
		// check runs once per top-level mention outside the home; a mention
		// in the home proves it alive, and a value that is not a channel id
		// (a hand edit of the projection) is the operator's to rewrite.
		if channel == home || !b.valid(home) {
			return "", false
		}
		refused, err := b.check(ctx, home)
		if err != nil || refused == "" {
			return "", false
		}
		b.log.Warn(b.name+": the home channel is refused now; this mention may replace it", "home", home, "reason", refused, "candidate", channel)
	}
	refused, err := b.check(ctx, channel)
	if err != nil {
		b.log.Warn(b.name+": could not check the channel as a home channel; the next mention tries again", "channel", channel, "err", err)
		return "", false
	}
	if refused != "" {
		b.log.Warn(b.name+": channel refused as the home channel", "channel", channel, "reason", refused, "member", member)
		l.refuseHome(ctx, refused)
		return "", false
	}
	home, adopted, err := l.recordHome(ctx, channel, member)
	if err != nil {
		b.log.Error(b.name+": home channel not recorded; the next mention tries again", "channel", channel, "err", err)
		return "", false
	}
	if adopted {
		b.log.Info(b.name+": home channel recorded already; the file follows the record", "channel", home)
		return "", false
	}
	b.log.Info(b.name+": home channel learned", "channel", home, "setBy", member, "conversation", conversation)
	return home, true
}

// refuseHome records why a candidate was refused. The record decides: while
// it names a channel, the refused key is the post-time check's to write
// (the backend's VerifyHomeForPost), or an older gateway's, and a learn that adopts or
// replaces the channel clears it, so a refused candidate writes nothing;
// otherwise the reason is written unless the record names it already, so a
// channel that keeps being refused is one log line per mention, not one
// write, and a read or a write that failed is tried again at the next
// refusal.
func (l *homeLearner) refuseHome(ctx context.Context, reason string) {
	cm, err := l.record.get(ctx)
	if err != nil {
		l.backend.log.Warn(l.backend.name+": home-channel refusal not recorded", "record", l.record.name, "err", err)
		return
	}
	keys := l.backend.keys
	if strings.TrimSpace(cm.Data[keys.channel]) != "" || cm.Data[keys.refused] == reason {
		return
	}
	l.writeRefusal(ctx, cm, reason)
}

func (l *homeLearner) writeRefusal(ctx context.Context, cm *corev1.ConfigMap, reason string) {
	if err := l.record.patch(ctx, cm.ResourceVersion, map[string]*string{l.backend.keys.refused: &reason}); err != nil {
		l.backend.log.Warn(l.backend.name+": home-channel refusal not recorded", "record", l.record.name, "err", err)
	}
}

// refuseNamedHome writes reason beside channel while the record still names
// channel and does not name that reason already: the sibling of refuseHome
// for a record that names a channel. A record the learn or a hand edit moved
// on is not relabelled.
func (l *homeLearner) refuseNamedHome(ctx context.Context, channel, reason string) {
	if !l.armed() {
		return
	}
	b := l.backend
	cm, err := l.record.get(ctx)
	if err != nil {
		b.log.Warn(b.name+": home-channel refusal not recorded", "record", l.record.name, "err", err)
		return
	}
	if strings.TrimSpace(cm.Data[b.keys.channel]) != channel || cm.Data[b.keys.refused] == reason {
		return
	}
	b.log.Warn(b.name+": the home channel is refused now; recorded so the operator and the next mention act on it", "channel", channel, "reason", reason)
	l.writeRefusal(ctx, cm, reason)
}

// recordHome writes the learned channel, unless the record names one already:
// then that one is checked and kept instead (adopted), with a refusal beside
// it (the post-time check's, or an older gateway's) cleared when the check
// passes now, so the operator admits it, and nothing else written; a
// recorded channel the check refuses (one archived since another writer
// learned it) is replaced. A write the record changed under (409) reads
// again and does the same; the other backend's learn is such a writer, and
// touches none of this backend's keys. The answer is the channel the record
// names and whether it was there already.
func (l *homeLearner) recordHome(ctx context.Context, channel, member string) (home string, adopted bool, err error) {
	b := l.backend
	for attempt := 0; attempt < homeRecordAttempts; attempt++ {
		cm, err := l.record.get(ctx)
		if err != nil {
			return "", false, err
		}
		if winner := strings.TrimSpace(cm.Data[b.keys.channel]); winner != "" {
			refused, err := b.check(ctx, winner)
			if err != nil {
				return "", false, fmt.Errorf("the record already names %q, which could not be checked: %w", winner, err)
			}
			if refused == "" {
				if cm.Data[b.keys.refused] != "" {
					if err := l.record.patch(ctx, cm.ResourceVersion, map[string]*string{b.keys.refused: nil}); err != nil {
						return "", false, fmt.Errorf("the record names %q beside a refusal that could not be cleared: %w", winner, err)
					}
				}
				return winner, true, nil
			}
			b.log.Warn(b.name+": recorded home channel refused; replacing it", "channel", winner, "reason", refused, "replacement", channel)
		}
		now := time.Now().UTC().Format(time.RFC3339)
		err = l.record.patch(ctx, cm.ResourceVersion, map[string]*string{
			b.keys.channel: &channel, b.keys.setBy: &member, b.keys.setAt: &now,
			homeRecordKeyWriter: &l.image, b.keys.refused: nil,
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

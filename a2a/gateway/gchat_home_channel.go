package gateway

import (
	"context"
	"errors"
	"net/http"

	"k8s.io/client-go/kubernetes"

	"github.com/gke-labs/kube-agents/a2a/lib/chattext"
)

// Google Chat's half of the learned home channel (home_channel.go has the
// shared learner). The candidate is a verified, typed, top-level message in
// a space that is not a direct message: Chat delivers a space message to the
// app only when it mentions the app, so every such message is addressed, and
// Chat itself says whether it was typed inside an existing thread
// (threadReply). The check is spaces.get through the relay, under the app's
// own credentials: it answers only for a space the app is a member of. The
// home is the space, never a thread of it, and the one line is posted in the
// learning thread. A direct message is never a candidate, and a group chat
// (an unnamed multi-person conversation) is refused without a call.
const (
	// gchatHomeLogName prefixes the learner's log lines.
	gchatHomeLogName = "gchat"

	// gchatHomeSpaceMax bounds a recorded space name: the grammar alone has
	// no upper bound, and "spaces/<id>" is about eighteen characters. The
	// operator admits a recorded space under the same bound
	// (gchatHomeChannelRecordIDMax).
	gchatHomeSpaceMax = 64

	// gchatSpaceNameArgument is the argument spaces.get takes: the space's
	// resource name.
	gchatSpaceNameArgument = "name"

	// gchatHomeLearnedLine is the one line posted in the learning thread. It
	// names the field that overrides the learn, never the member; Chat has no
	// space mention, and the line stands in the space it speaks of.
	gchatHomeLearnedLine = "This space is now the home channel for alerts and scheduled reports. To move it, set spec.integration.googleChat.homeChannel on the PlatformAgent."
)

// gchatHomeCandidate is a typed message that could set the home space,
// waiting for the gateway's verdict on its sender (InboundVerified).
type gchatHomeCandidate struct {
	// spaceType is the event's space type, so a group chat is refused
	// without a call; member is the sender's users/{id} resource name, the
	// record's setBy.
	spaceType, member string
}

// SetHomeLearner hands the adapter what a learn needs: the record, the image
// to stamp as the writer, and the directory the operator mounts the home
// channels in. Called before Run, read-only after.
func (a *GoogleChatAdapter) SetHomeLearner(client kubernetes.Interface, namespace, name, image, fileDir string) {
	a.homeLearn.arm(client, namespace, name, image, homeFile{dir: fileDir, key: gchatHomeFileKey}, homeBackend{
		name: gchatHomeLogName, keys: gchatHomeRecordKeys, valid: gchatIsHomeSpace, check: a.checkHomeSpace, log: a.log,
	})
}

// LearnedHome is the home space the operator's projection names, and whether
// the projection is mounted at all; the Notifier's home source.
func (a *GoogleChatAdapter) LearnedHome() (string, bool) {
	return a.homeLearn.projected()
}

// gchatIsHomeSpace reports a bare space resource name within the bound.
func gchatIsHomeSpace(id string) bool {
	return gchatIsSpaceName(id) && len([]rune(id)) <= gchatHomeSpaceMax
}

// noteHomeCandidate marks a typed message that could set the home space: a
// top-level message with words that are not a command, outside a direct
// message, while the learner is armed. A message in a space reached the app
// by mentioning it. InboundVerified acts on the mark once the gateway has
// verified the sender. The ring bounds the marks: a message whose quoted read
// fails is never handed to the gateway, so its mark is never taken.
func (a *GoogleChatAdapter) noteHomeCandidate(ev *gchatEvent, msg InboundMessage) {
	if msg.Kind == sessionKindDM || ev.Message.ThreadReply || msg.Text == "" || chattext.IsCommand(msg.Text) {
		return
	}
	if !a.homeLearn.armed() {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.homePending == nil {
		a.homePending = map[string]gchatHomeCandidate{}
	}
	a.homePending[msg.MessageID] = gchatHomeCandidate{spaceType: ev.Space.SpaceType, member: ev.Message.Sender.Name}
	remember(&a.homePendingOrder, msg.MessageID, func(k string) { delete(a.homePending, k) })
}

// homeVerified takes the mark of a message the gateway has decided on, and
// starts a learn for a verified one while the learner is idle. Off the inbox
// worker: the file read, the check and the write happen on the learn
// goroutine.
func (a *GoogleChatAdapter) homeVerified(conversation, messageID string, verified bool) {
	a.mu.Lock()
	candidate, ok := a.homePending[messageID]
	delete(a.homePending, messageID)
	forget(&a.homePendingOrder, messageID)
	a.mu.Unlock()
	if !ok || !verified {
		return
	}
	l := &a.homeLearn
	if !l.begin() {
		return
	}
	go func() {
		defer l.wg.Done()
		a.learnHome(conversation, candidate)
	}()
}

// closeHomeLearn stops new learns and waits for the ones in flight, so a
// write started before shutdown lands and none starts after Run's wait.
func (a *GoogleChatAdapter) closeHomeLearn() { a.homeLearn.close() }

// learnHome runs one learn (homeLearner.learn) for the space of a mention
// and posts the one line in the mention's thread when it learned the space,
// so nothing is announced that is not recorded. A group chat is refused
// here, without a call.
func (a *GoogleChatAdapter) learnHome(conversation string, candidate gchatHomeCandidate) {
	l := &a.homeLearn
	defer l.release()
	space, _, ok := gchatSpaceThread(conversation)
	if !ok || candidate.spaceType == gchatSpaceTypeGroupChat {
		a.log.Warn("gchat: a group chat cannot be the home channel", "conversation", conversation, "member", candidate.member)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), homeLearnTimeout)
	defer cancel()
	if _, learned := l.learn(ctx, space, candidate.member, conversation); !learned {
		return
	}
	if _, err := a.Post(conversation, gchatHomeLearnedLine); err != nil {
		a.log.Warn("gchat: home channel line not posted", "space", space, "err", err)
	}
}

// checkHomeSpace is the gate a candidate or a recorded space passes: a space
// name within the bound, then one spaces.get. refused names the reason a
// definite answer refuses it; err is a check that could not be made, which is
// never a refusal. Under app credentials Chat answers 403 or 404 for a space
// the app is not in, so either is the app's view of a space it cannot post
// into. A space that admits people outside the organization is refused as
// Slack refuses a Slack Connect channel: alerts and reports name the fleet.
func (a *GoogleChatAdapter) checkHomeSpace(ctx context.Context, id string) (refused string, err error) {
	if !gchatIsHomeSpace(id) {
		return homeRefusedMalformed, nil
	}
	var space struct {
		gchatSpace
		ExternalUserAllowed bool `json:"externalUserAllowed"`
	}
	err = a.apiCallContext(ctx, []string{gchatSpacesResource}, gchatGetMethod, map[string]any{gchatSpaceNameArgument: id}, &space)
	if err != nil {
		var apiErr *gchatAPIError
		if errors.As(err, &apiErr) && (apiErr.Status == http.StatusForbidden || apiErr.Status == http.StatusNotFound) {
			return homeRefusedNotMember, nil
		}
		return "", err
	}
	switch {
	case space.SpaceType == gchatSpaceTypeDM || space.Type == gchatLegacySpaceTypeDM || space.SpaceType == gchatSpaceTypeGroupChat:
		// A direct message or a group chat: a hand-edited record carries no
		// event, so the answer itself says what the name is.
		return homeRefusedDirectMessage, nil
	case space.ExternalUserAllowed:
		return homeRefusedShared, nil
	}
	return "", nil
}

// VerifyHomeForPost is the check a home post passes before it goes out
// (Notifier.SetHomeVerifier): the same spaces.get a learn makes. A space Chat
// refuses now (the app removed, opened to people outside the organization,
// not a space) is written to gchat.refused beside the record while the record
// still names it, so the operator empties the projection and HomeChannelUnset
// names the cause; a check that cannot be made is an error and writes
// nothing.
func (a *GoogleChatAdapter) VerifyHomeForPost(ctx context.Context, home string) (refused string, err error) {
	refused, err = a.checkHomeSpace(ctx, home)
	if err == nil && refused != "" {
		a.homeLearn.refuseNamedHome(ctx, home, refused)
	}
	return refused, err
}

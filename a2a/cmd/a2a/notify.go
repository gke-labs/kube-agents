package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"slices"
	"sort"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// notifyDefaultTimeout bounds the wait for the gateway's answer. The gateway
// answers once the first post has landed, which is one relay call (the
// gateway bounds each at 45s) plus any notify queued ahead of this one.
const notifyDefaultTimeout = 60 * time.Second

// notifyExitOutcomeUnknown is the exit status for "no answer in time": the
// post may still land, so a caller must not send it again. The agent-side
// callers read it (agents/platform/scripts/chat_notify.py,
// NOTIFY_OUTCOME_UNKNOWN); every other failure exits 1.
const notifyExitOutcomeUnknown = 3

// notifyPollInterval is how often the wait for an answer checks for a refusal.
const notifyPollInterval = 250 * time.Millisecond

// notifyPermissionsViolation is how the server spells a refused publish or
// subscribe in the error the client records (nats.go's PERMISSIONS_ERR).
const notifyPermissionsViolation = "permissions violation"

// errNotifyRefusedByBus marks a request the server refused for want of a grant.
var errNotifyRefusedByBus = errors.New("refused by the bus")

// errNotifyOutcomeUnknown marks the failure main reports with
// notifyExitOutcomeUnknown.
var errNotifyOutcomeUnknown = errors.New("outcome unknown")

// notifyExitRouteUnavailable is the exit status for "the route is not there
// right now": nothing subscribes on the subject (the gateway is restarting,
// or its route is not armed) or the bus cannot be reached. Nothing was posted,
// and unlike a refusal it says nothing about the destination, so a caller that
// counts failures against a chat can wait instead (the kanban notifier's
// stand-in does). Every other failure exits 1.
const notifyExitRouteUnavailable = 4

// errNotifyRouteUnavailable marks the failure main reports with
// notifyExitRouteUnavailable.
var errNotifyRouteUnavailable = errors.New("route unavailable")

const notifyUsage = `usage: a2a notify --platform <platform> [--thread <thread>] [--timeout <d>] [--] [text]

Post text to the install's chat home channel through the A2A gateway: a new
thread, or a reply on --thread, which must be a thread of the home channel.
Reads the text from stdin when it is omitted, or "-" with no "--" before it.
Prints the gateway's answer as JSON (message_id, the field hermes send --json
prints, and thread_id). Exits 1 when nothing was posted, 3 when the gateway
did not answer in time (the post may still land), and 4 when the route is not
there right now (nothing answering, or the bus unreachable; nothing posted).
`

func runNotify(args []string) error {
	fs := flag.NewFlagSet("notify", flag.ContinueOnError)
	fs.Usage = func() { fmt.Fprint(os.Stderr, notifyUsage) }
	platform := fs.String("platform", "", "chat platform: "+strings.Join(notifyPlatforms(), ", "))
	thread := fs.String("thread", "", "thread to reply on (default: a new thread in the home channel)")
	timeout := fs.Duration("timeout", notifyDefaultTimeout, "how long to wait for the gateway's answer")
	if err := fs.Parse(args); err != nil {
		return err
	}
	subject, ok := lib.NotifySubjects[*platform]
	if !ok {
		return fmt.Errorf("notify: --platform must be one of %s", strings.Join(notifyPlatforms(), ", "))
	}
	// flag drops the "--" it stops at, so whether one was given is read from
	// the raw arguments: after it, a lone "-" is text, not "read stdin".
	text, err := notifyText(fs.Args(), slices.Contains(args, "--"), os.Stdin)
	if err != nil {
		return err
	}
	body, err := json.Marshal(lib.NotifyRequest{Text: text, Thread: *thread})
	if err != nil {
		return err
	}

	ctx, cancel := cliContext()
	defer cancel()
	client, err := connect(ctx, "notify")
	if err != nil {
		return fmt.Errorf("notify: cannot reach the bus: %v: %w", err, errNotifyRouteUnavailable)
	}
	defer client.Close()
	nc := client.Conn()

	// The answer comes back on chat.notify.reply.agent.<id>, the one reply
	// namespace the gateway may publish to, rather than on this client's
	// _INBOX: the agent reads its JetStream replies there, and the grant
	// keeps the gateway out of it.
	reply := lib.NotifyReplyPrefix + nuid.Next()
	in, err := nc.SubscribeSync(reply)
	if err != nil {
		return fmt.Errorf("notify: subscribe %s: %w", reply, err)
	}
	defer func() { _ = in.Unsubscribe() }()
	if err := nc.PublishRequest(subject, reply, body); err != nil {
		return fmt.Errorf("notify: publish: %w", err)
	}
	if err := nc.Flush(); err != nil {
		return fmt.Errorf("notify: flush: %w", err)
	}
	msg, err := awaitAnswer(nc, in, *timeout)
	if errors.Is(err, errNotifyRefusedByBus) {
		return fmt.Errorf("notify: the bus refused it, so nothing was posted: %v", nc.LastError())
	}
	if errors.Is(err, nats.ErrTimeout) {
		return fmt.Errorf("notify: no answer from the gateway on %s within %s; the post may still land: %w",
			subject, *timeout, errNotifyOutcomeUnknown)
	}
	if errors.Is(err, nats.ErrNoResponders) {
		// Nothing subscribes on the subject, so the gateway's route is not
		// armed (no home channel, another backend, or the gateway down).
		// Nothing was posted.
		return fmt.Errorf("notify: nothing is answering on %s; the gateway's notify route is not armed: %w",
			subject, errNotifyRouteUnavailable)
	}
	if err != nil {
		return fmt.Errorf("notify: no answer from the gateway on %s: %w", subject, err)
	}
	var answer lib.NotifyReply
	if err := json.Unmarshal(msg.Data, &answer); err != nil {
		return fmt.Errorf("notify: unreadable answer: %w", err)
	}
	fmt.Println(string(msg.Data))
	if answer.Error != "" && answer.MessageID == "" {
		return fmt.Errorf("notify: the gateway posted nothing: %s", answer.Error)
	}
	if answer.Error != "" {
		fmt.Fprintf(os.Stderr, "a2a: notify: posted partly: %s\n", answer.Error)
	}
	return nil
}

// awaitAnswer waits for the gateway's answer in short slices, checking between
// them whether the server refused the request. A publish or subscribe the
// principal is not granted is dropped by the server and reported only to the
// connection's async error, never as an error from PublishRequest or
// SubscribeSync, so without this a refusal would wait out the whole timeout
// and read as "may have posted" (exit 3), which the alert path records as sent.
func awaitAnswer(nc *nats.Conn, in *nats.Subscription, timeout time.Duration) (*nats.Msg, error) {
	deadline := time.Now().Add(timeout)
	for {
		if last := nc.LastError(); errors.Is(last, nats.ErrPermissionViolation) ||
			(last != nil && strings.Contains(strings.ToLower(last.Error()), notifyPermissionsViolation)) {
			return nil, errNotifyRefusedByBus
		}
		wait := time.Until(deadline)
		if wait <= 0 {
			return nil, nats.ErrTimeout
		}
		msg, err := in.NextMsg(min(wait, notifyPollInterval))
		if errors.Is(err, nats.ErrTimeout) {
			continue
		}
		return msg, err
	}
}

// notifyText is the positional text, or stdin when it is absent, or "-" with
// no "--" before it.
func notifyText(args []string, terminated bool, stdin io.Reader) (string, error) {
	switch {
	case len(args) > 1:
		return "", errors.New("notify: one text argument (quote it), or none to read stdin")
	case len(args) == 1 && (terminated || args[0] != "-"):
		return args[0], nil
	}
	data, err := io.ReadAll(stdin)
	if err != nil {
		return "", fmt.Errorf("notify: reading stdin: %w", err)
	}
	return string(data), nil
}

func notifyPlatforms() []string {
	names := make([]string, 0, len(lib.NotifySubjects))
	for name := range lib.NotifySubjects {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

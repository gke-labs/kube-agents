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

// errNotifyOutcomeUnknown marks the failure main reports with
// notifyExitOutcomeUnknown.
var errNotifyOutcomeUnknown = errors.New("outcome unknown")

const notifyUsage = `usage: a2a notify --platform <platform> [--thread <thread>] [--timeout <d>] [--] [text]

Post text to the install's chat home channel through the A2A gateway: a new
thread, or a reply on --thread, which must be a thread of the home channel.
Reads the text from stdin when it is omitted, or "-" with no "--" before it.
Prints the gateway's answer as JSON (message_id, the field hermes send --json
prints, and thread_id). Exits 1 when nothing was posted and 3 when the gateway
did not answer in time, so the post may still land.
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
		return err
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
	msg, err := in.NextMsg(*timeout)
	if errors.Is(err, nats.ErrTimeout) {
		return fmt.Errorf("notify: no answer from the gateway on %s within %s; the post may still land: %w",
			subject, *timeout, errNotifyOutcomeUnknown)
	}
	if errors.Is(err, nats.ErrNoResponders) {
		// Nothing subscribes on the subject, so the gateway's route is not
		// armed (no home channel, another backend, or the gateway down).
		// Nothing was posted.
		return fmt.Errorf("notify: nothing is answering on %s; the gateway's notify route is not armed", subject)
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

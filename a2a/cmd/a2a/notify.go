package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// notifyDefaultTimeout bounds the wait for the gateway's answer. A long
// report goes out as several Chat posts, each a relay round trip.
const notifyDefaultTimeout = 20 * time.Second

const notifyUsage = `usage: a2a notify --platform <platform> [--thread <thread>] [--timeout <d>] [text]

Post text to the install's chat home channel through the A2A gateway: a new
thread, or a reply on --thread, which must be a thread of the home channel.
Reads the text from stdin when it is "-" or omitted. Prints the gateway's
answer as JSON ({"message_id", "thread_id"}, the shape hermes send --json
prints) and exits non-zero when nothing was posted.
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
	text, err := notifyText(fs.Args(), os.Stdin)
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
	if err != nil {
		return fmt.Errorf("notify: no answer from the gateway on %s within %s: %w", subject, *timeout, err)
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

// notifyText is the positional text, or stdin when it is "-" or absent.
func notifyText(args []string, stdin io.Reader) (string, error) {
	switch {
	case len(args) > 1:
		return "", errors.New("notify: one text argument (quote it), or none to read stdin")
	case len(args) == 1 && args[0] != "-":
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

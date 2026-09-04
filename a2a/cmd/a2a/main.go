// Command a2a is the topics client: read the current answer on a provisioned
// topic, or write one. It is the reader beat 3 needs before any session pod
// exists, and the thing the platform agent's a2a-topics skill shells out to.
//
// Playground posture: credentials are a static per-role NATS user in the
// environment, because that is what the operator renders under mode: next.
// The product answer is the auth callout minting a user per agent identity;
// the subject grants this dials into are already the real ones.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const usage = `a2a - a2a-jetstream topics client

usage:
  a2a topics list                  list the provisioned topics and their retention class
  a2a topics read <topic>          print the latest entry on a topic
  a2a topics write <topic> [flags] publish one entry to a topic

<topic> is a bare name (upgrade-readiness), a scope-qualified name
(shared.blueprint, agent.platform.upgrade-readiness), or a full subject. A bare
name that matches more than one provisioned topic is an error, not a guess.

environment:
  NATS_URL       bus address (required)
  NATS_USER      static bus user; also selects the _INBOX.<user> prefix
  NATS_PASSWORD  its password
  A2A_SESSION    from.session on writes (default: the bus user)
  A2A_PROFILE    from.profile on writes, when the writer runs as a profile
`

// cliTimeout bounds one CLI operation end to end, connect included.
const cliTimeout = 30 * time.Second

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "a2a: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, usage)
		return errors.New("no command")
	}
	switch args[0] {
	case "topics":
		return runTopics(args[1:])
	case "-h", "--help", "help":
		fmt.Print(usage)
		return nil
	default:
		fmt.Fprint(os.Stderr, usage)
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func runTopics(args []string) error {
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, usage)
		return errors.New("topics: no subcommand")
	}
	switch args[0] {
	case "list", "ls":
		return topicsList(args[1:])
	case "read", "get":
		return topicsRead(args[1:])
	case "write", "publish":
		return topicsWrite(args[1:])
	default:
		fmt.Fprint(os.Stderr, usage)
		return fmt.Errorf("topics: unknown subcommand %q", args[0])
	}
}

// connect dials the bus with the environment's static user, pinning that
// user's inbox prefix (see lib.WithUserPassword - the grant does not cover
// nats.go's default inbox).
func connect(ctx context.Context, name string) (*lib.Client, error) {
	url := os.Getenv("NATS_URL")
	if url == "" {
		return nil, errors.New("NATS_URL is not set")
	}
	// The library logs connection events at info; a CLI should say nothing
	// unless something is wrong, so only errors reach stderr.
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelError}))
	return lib.Connect(ctx, url,
		lib.WithName("a2a-cli-"+name),
		lib.WithLogger(log),
		lib.WithUserPassword(os.Getenv("NATS_USER"), os.Getenv("NATS_PASSWORD")),
	)
}

func cliContext() (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), cliTimeout)
}

func topicsList(args []string) error {
	fs := flag.NewFlagSet("topics list", flag.ContinueOnError)
	asJSON := fs.Bool("json", false, "print the registry as JSON")
	if err := fs.Parse(args); err != nil {
		return err
	}
	ctx, cancel := cliContext()
	defer cancel()
	c, err := connect(ctx, "topics-list")
	if err != nil {
		return err
	}
	defer c.Close()

	registry, err := c.TopicRegistry(ctx)
	if err != nil {
		return err
	}
	if *asJSON {
		return json.NewEncoder(os.Stdout).Encode(registry)
	}
	if len(registry) == 0 {
		fmt.Println("no topics provisioned")
		return nil
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "TOPIC\tCLASS\tWRITER SCOPE\tSUBJECT")
	for _, e := range registry {
		scope := e.Scope
		if e.Agent != "" {
			scope = e.Scope + " " + e.Agent
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\n", e.Topic, e.Class, scope, e.Subject)
	}
	return w.Flush()
}

// splitTopicName pulls the topic name out of the argument list before the
// flags are parsed. Go's flag package stops at the first non-flag argument, so
// `topics write upgrade-readiness --text ...` would otherwise parse zero flags
// and report three stray positionals - and the natural way to write the
// command puts the topic first.
func splitTopicName(args []string) (string, []string) {
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		return args[0], args[1:]
	}
	return "", args
}

// topicName resolves the name from either position: ahead of the flags, or
// after them.
func topicName(pre string, fs *flag.FlagSet, cmd string) (string, error) {
	switch {
	case pre != "" && fs.NArg() == 0:
		return pre, nil
	case pre == "" && fs.NArg() == 1:
		return fs.Arg(0), nil
	default:
		return "", fmt.Errorf("%s takes exactly one topic name", cmd)
	}
}

func topicsRead(args []string) error {
	name, rest := splitTopicName(args)
	fs := flag.NewFlagSet("topics read", flag.ContinueOnError)
	asJSON := fs.Bool("json", false, "print the whole envelope as JSON")
	if err := fs.Parse(rest); err != nil {
		return err
	}
	name, err := topicName(name, fs, "topics read")
	if err != nil {
		return err
	}
	ctx, cancel := cliContext()
	defer cancel()
	c, err := connect(ctx, "topics-read")
	if err != nil {
		return err
	}
	defer c.Close()

	entry, err := resolve(ctx, c, name)
	if err != nil {
		return err
	}
	env, err := c.ReadTopicLatest(ctx, entry.Stream, entry.Subject)
	if err != nil {
		if errors.Is(err, lib.ErrTopicEmpty) {
			// A provisioned topic nobody has written yet is a legal state, and
			// the caller is usually an agent about to answer a question from
			// it: say so in words it can relay, and exit non-zero so a script
			// does not mistake silence for content.
			fmt.Printf("topic %s (%s) is provisioned but has no entries yet\n", entry.Topic, entry.Subject)
			os.Exit(2)
		}
		return err
	}
	if *asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(env)
	}
	return printEntry(os.Stdout, entry, env)
}

// printEntry renders one topic entry for a reader that is usually a language
// model: the provenance line first (who wrote it, when, under what
// correlation), then the summary, then the structured state. Its shape is the
// skill's contract, so it stays stable.
func printEntry(w io.Writer, entry lib.TopicEntry, env *lib.Envelope) error {
	var a lib.Artifact
	if err := json.Unmarshal(env.Payload, &a); err != nil {
		return fmt.Errorf("malformed topic artifact on %s: %w", entry.Subject, err)
	}
	writer := env.From.Session
	if env.From.Profile != "" {
		writer += " (profile " + env.From.Profile + ")"
	}
	fmt.Fprintf(w, "topic:       %s (%s class, %s)\n", entry.Topic, entry.Class, entry.Subject)
	fmt.Fprintf(w, "written by:  %s at %s\n", writer, env.TS.Format(time.RFC3339))
	fmt.Fprintf(w, "correlation: %s\n", env.CorrelationID)
	if env.TaskID != "" {
		fmt.Fprintf(w, "written during task: %s\n", env.TaskID)
	}
	for _, p := range a.Parts {
		switch p.Kind {
		case "text":
			fmt.Fprintf(w, "\nsummary:\n%s\n", strings.TrimRight(p.Text, "\n"))
		case "data":
			var pretty json.RawMessage = p.Data
			var buf strings.Builder
			if err := indentJSON(&buf, p.Data); err == nil {
				fmt.Fprintf(w, "\ndata:\n%s\n", buf.String())
				continue
			}
			fmt.Fprintf(w, "\ndata:\n%s\n", pretty)
		case "file":
			fmt.Fprintf(w, "\nfile: %s\n", p.File.Name)
		}
	}
	return nil
}

func indentJSON(w *strings.Builder, raw json.RawMessage) error {
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return err
	}
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		return err
	}
	return nil
}

func topicsWrite(args []string) error {
	name, rest := splitTopicName(args)
	fs := flag.NewFlagSet("topics write", flag.ContinueOnError)
	text := fs.String("text", "", "TextPart summary")
	data := fs.String("data", "", "DataPart JSON: inline, @file, or - for stdin")
	from := fs.String("from", "", "from.session (default: A2A_SESSION, else the bus user)")
	profile := fs.String("profile", os.Getenv("A2A_PROFILE"), "from.profile, when the writer runs as a profile")
	taskID := fs.String("task-id", "", "the task this write happened in the course of")
	contextID := fs.String("context-id", "", "that task's contextId")
	correlationID := fs.String("correlation-id", "", "correlationId (default: minted for this run)")
	if err := fs.Parse(rest); err != nil {
		return err
	}
	name, err := topicName(name, fs, "topics write")
	if err != nil {
		return err
	}
	if *text == "" && *data == "" {
		return errors.New("topics write needs --text, --data, or both")
	}
	// A write in the course of a task carries that task's ids, which is the
	// audit thread from a question to the standing state it changed. Half of
	// that thread is worse than none: the envelope requires both ids together.
	if (*taskID == "") != (*contextID == "") {
		return errors.New("--task-id and --context-id go together")
	}

	var payload any
	if *data != "" {
		raw, err := readData(*data)
		if err != nil {
			return err
		}
		if err := json.Unmarshal(raw, &payload); err != nil {
			return fmt.Errorf("--data is not valid JSON: %w", err)
		}
	}

	ctx, cancel := cliContext()
	defer cancel()
	c, err := connect(ctx, "topics-write")
	if err != nil {
		return err
	}
	defer c.Close()

	entry, err := resolve(ctx, c, name)
	if err != nil {
		return err
	}
	artifact, err := lib.NewTopicArtifact(entry.Topic, *text, payload)
	if err != nil {
		return err
	}
	session := *from
	if session == "" {
		session = os.Getenv("A2A_SESSION")
	}
	if session == "" {
		session = os.Getenv("NATS_USER")
	}
	if session == "" {
		return errors.New("no writer identity: set --from, A2A_SESSION, or NATS_USER")
	}
	corr := *correlationID
	if corr == "" {
		corr = lib.NewCorrelationID()
	}
	err = c.PublishTopic(ctx, entry.Subject,
		lib.Party{Session: session, Profile: *profile},
		*taskID, *contextID, corr, artifact)
	if err != nil {
		return err
	}
	fmt.Printf("wrote %s (%s)\ncorrelation: %s\n", entry.Topic, entry.Subject, corr)
	return nil
}

func readData(spec string) ([]byte, error) {
	switch {
	case spec == "-":
		return io.ReadAll(os.Stdin)
	case strings.HasPrefix(spec, "@"):
		return os.ReadFile(strings.TrimPrefix(spec, "@"))
	default:
		return []byte(spec), nil
	}
}

func resolve(ctx context.Context, c *lib.Client, name string) (lib.TopicEntry, error) {
	registry, err := c.TopicRegistry(ctx)
	if err != nil {
		return lib.TopicEntry{}, err
	}
	return lib.ResolveTopic(registry, name)
}

package busmigrate

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
)

// The staged snapshot's layout under the staging directory: one directory per
// stream, its data and the metadata the server reported with it.
const (
	SnapshotDataFile = "stream.tar.s2"
	SnapshotMetaFile = "meta.json"
)

// DefaultTerminationLog is where bus-hash writes its digests for the operator
// to read from the pod's status (the kubelet's termination message).
const DefaultTerminationLog = "/dev/termination-log"

// connFlags are the bus connection the run's pod is given, the way the
// provision Job connects: its principal's name, its projected bus token as
// the password, TLS verified against the bus CA, and the principal's own
// inbox prefix, which is all its subscribe grant covers.
type connFlags struct {
	server, caFile, user, tokenFile string
}

func (c *connFlags) bind(fs *flag.FlagSet) {
	fs.StringVar(&c.server, "server", "", "the bus's client URL")
	fs.StringVar(&c.caFile, "ca-file", "", "the bus CA bundle")
	fs.StringVar(&c.user, "user", "", "the run's bus principal")
	fs.StringVar(&c.tokenFile, "token-file", "", "the run's projected bus token")
}

func (c *connFlags) connect() (*nats.Conn, error) {
	if c.server == "" {
		return nil, errors.New("--server is required")
	}
	opts := []nats.Option{nats.Name("bus-migrate")}
	if c.caFile != "" {
		opts = append(opts, nats.RootCAs(c.caFile))
	}
	if c.user != "" {
		token, err := os.ReadFile(c.tokenFile)
		if err != nil {
			return nil, fmt.Errorf("reading the bus token: %w", err)
		}
		opts = append(opts, nats.UserInfo(c.user, strings.TrimSpace(string(token))), nats.CustomInboxPrefix("_INBOX."+c.user))
	}
	return nats.Connect(c.server, opts...)
}

func splitStreams(s string) ([]string, error) {
	var out []string
	for _, name := range strings.Split(s, ",") {
		if name = strings.TrimSpace(name); name != "" {
			out = append(out, name)
		}
	}
	if len(out) == 0 {
		return nil, errors.New("--streams is required")
	}
	return out, nil
}

// Main runs `bus-migrate <leg> [flags]`: pin (three to one: lead every stream
// from --target and scale it to R1), unpin (undo a pin before the shrink),
// or snapshot (one to three: stage every stream under --dir). It returns the
// process's exit code.
func Main(ctx context.Context, args []string, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, "usage: bus-migrate pin|unpin|snapshot [flags]")
		return 2
	}
	leg, args := args[0], args[1:]
	fs := flag.NewFlagSet("bus-migrate "+leg, flag.ContinueOnError)
	fs.SetOutput(stderr)
	var conn connFlags
	conn.bind(fs)
	streamsFlag := fs.String("streams", "", "the provisioned streams, comma-separated")
	target := fs.String("target", "", "pin: the server every stream moves to")
	replicas := fs.Int("replicas", 3, "unpin: the replica count to restore")
	dir := fs.String("dir", "", "snapshot: the staging directory")
	settle := fs.Duration("settle", 2*time.Second, "pin: the wait after each step-down or update")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	streams, err := splitStreams(*streamsFlag)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}
	switch {
	case leg != "pin" && leg != "unpin" && leg != "snapshot":
		fmt.Fprintf(stderr, "unknown leg %q\n", leg)
		return 2
	case leg == "pin" && *target == "":
		fmt.Fprintln(stderr, "pin: --target is required")
		return 2
	case leg == "snapshot" && *dir == "":
		fmt.Fprintln(stderr, "snapshot: --dir is required")
		return 2
	}
	nc, err := conn.connect()
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	defer nc.Close()
	switch leg {
	case "pin":
		err = PinToServer(ctx, nc, *target, streams, *settle)
	case "unpin":
		err = UnpinFromServer(ctx, nc, streams, *replicas)
	case "snapshot":
		err = snapshotAll(ctx, nc, streams, *dir)
	}
	if err != nil {
		fmt.Fprintf(stderr, "bus-migrate %s: %v\n", leg, err)
		return 1
	}
	return 0
}

func snapshotAll(ctx context.Context, nc *nats.Conn, streams []string, dir string) error {
	for _, name := range streams {
		sdir := filepath.Join(dir, name)
		if err := os.MkdirAll(sdir, 0o700); err != nil {
			return err
		}
		f, err := os.Create(filepath.Join(sdir, SnapshotDataFile))
		if err != nil {
			return err
		}
		meta, err := Snapshot(ctx, nc, name, f)
		if cerr := f.Close(); err == nil {
			err = cerr
		}
		if err != nil {
			return fmt.Errorf("stream %s: %w", name, err)
		}
		b, err := json.Marshal(meta)
		if err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(sdir, SnapshotMetaFile), b, 0o600); err != nil {
			return err
		}
	}
	return nil
}

// HashMain runs `bus-hash --dir D --streams S`: the digest of every staged
// stream's data, as a JSON object, written to --termination-log, which the
// operator reads from the pod's status. It touches no bus.
func HashMain(args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("bus-hash", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dir := fs.String("dir", "", "the staging directory")
	streamsFlag := fs.String("streams", "", "the provisioned streams, comma-separated")
	out := fs.String("termination-log", DefaultTerminationLog, "where the digests are written")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	streams, err := splitStreams(*streamsFlag)
	if err != nil || *dir == "" {
		fmt.Fprintln(stderr, "bus-hash: --dir and --streams are required")
		return 2
	}
	digests, err := HashStaged(*dir, streams)
	if err != nil {
		fmt.Fprintf(stderr, "bus-hash: %v\n", err)
		return 1
	}
	b, err := json.Marshal(digests)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 1
	}
	if err := os.WriteFile(*out, b, 0o644); err != nil {
		fmt.Fprintf(stderr, "bus-hash: %v\n", err)
		return 1
	}
	return 0
}

// HashStaged digests every named stream's staged data under dir.
func HashStaged(dir string, streams []string) (map[string]string, error) {
	digests := map[string]string{}
	for _, name := range streams {
		d, err := digestOf(func() (io.ReadCloser, error) { return os.Open(filepath.Join(dir, name, SnapshotDataFile)) })
		if err != nil {
			return nil, fmt.Errorf("stream %s: %w", name, err)
		}
		digests[name] = d
	}
	return digests, nil
}

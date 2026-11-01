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
		fmt.Fprintln(stderr, "usage: bus-migrate pin|unpin|snapshot|restore [flags]")
		return 2
	}
	leg, args := args[0], args[1:]
	fs := flag.NewFlagSet("bus-migrate "+leg, flag.ContinueOnError)
	fs.SetOutput(stderr)
	var conn connFlags
	conn.bind(fs)
	streamsFlag := fs.String("streams", "", "the provisioned streams, comma-separated")
	target := fs.String("target", "", "pin: the server every stream moves to")
	replicas := fs.Int("replicas", 3, "unpin, restore: the replica count each stream ends at")
	dir := fs.String("dir", "", "snapshot, restore: the staging directory")
	runFile := fs.String("run-file", "", "restore: the operator's digests and policies (RunFile JSON)")
	report := fs.String("termination-log", DefaultTerminationLog, "restore: where its report is written")
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
	case leg != "pin" && leg != "unpin" && leg != "snapshot" && leg != "restore":
		fmt.Fprintf(stderr, "unknown leg %q\n", leg)
		return 2
	case leg == "pin" && *target == "":
		fmt.Fprintln(stderr, "pin: --target is required")
		return 2
	case leg == "snapshot" && *dir == "":
		fmt.Fprintln(stderr, "snapshot: --dir is required")
		return 2
	case leg == "restore" && (*dir == "" || *runFile == ""):
		fmt.Fprintln(stderr, "restore: --dir and --run-file are required")
		return 2
	}
	var run RunFile
	if leg == "restore" {
		b, err := os.ReadFile(*runFile)
		if err == nil {
			err = json.Unmarshal(b, &run)
		}
		if err != nil {
			fmt.Fprintf(stderr, "restore: reading --run-file: %v\n", err)
			return 2
		}
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
	case "restore":
		var warnings []string
		warnings, err = restoreAll(ctx, nc, streams, *dir, run, *replicas)
		r := Report{Warnings: warnings}
		if err != nil {
			r.Error = err.Error()
		}
		if werr := writeReport(*report, r); werr != nil && err == nil {
			err = werr
		}
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

// RunFile is what the operator gives the restore pod, from the run's
// Secret: the digests the hashing pod reported, and its own policy for
// every provisioned stream. Nothing in it comes from the staging volume.
type RunFile struct {
	Digests  map[string]StagedDigest `json:"digests"`
	Policies []StreamPolicy          `json:"policies"`
}

// Report is the restore pod's termination message: the warnings it found
// (fields outside the policy that differ from a fresh install, rendered
// subjects a stream lacks) and, when it failed, why.
type Report struct {
	Warnings []string `json:"warnings,omitempty"`
	Error    string   `json:"error,omitempty"`
}

// terminationMessageLimit is the kubelet's cap on a termination message.
const terminationMessageLimit = 4096

// writeReport writes r as JSON, keeping as many warnings as fit the
// termination message, with a last one counting the rest.
func writeReport(path string, r Report) error {
	all := r.Warnings
	for keep := len(all); keep >= 0; keep-- {
		r.Warnings = all[:keep:keep]
		if keep < len(all) {
			r.Warnings = append(r.Warnings, fmt.Sprintf("and %d more", len(all)-keep))
		}
		b, err := json.Marshal(r)
		if err != nil {
			return err
		}
		if len(b) <= terminationMessageLimit {
			return os.WriteFile(path, b, 0o644)
		}
	}
	r.Warnings = []string{fmt.Sprintf("%d warnings", len(all))}
	r.Error = r.Error[:min(len(r.Error), terminationMessageLimit/2)]
	b, err := json.Marshal(r)
	if err != nil {
		return err
	}
	return os.WriteFile(path, b, 0o644)
}

// restoreRetries bounds the attempts at one stream's restore when the server
// is not ready for it (a name the conversion's orphan cleanup still holds);
// a digest or policy refusal is never retried.
const restoreRetries = 30

// restoreAll restores every named stream from dir, checking each staged
// metadata file against the operator's digest before reading it, then
// raises each to replicas. It returns the warnings found on the way.
func restoreAll(ctx context.Context, nc *nats.Conn, streams []string, dir string, run RunFile, replicas int) ([]string, error) {
	fresh, err := LoadFreshStreams()
	if err != nil {
		return nil, err
	}
	policies := map[string]StreamPolicy{}
	for _, p := range run.Policies {
		policies[p.Name] = p
	}
	var warnings []string
	for _, name := range streams {
		want, ok := run.Digests[name]
		policy, hasPolicy := policies[name]
		if !ok || !hasPolicy {
			return warnings, fmt.Errorf("stream %s: the run file has no digest or no policy for it", name)
		}
		metaBytes, err := os.ReadFile(filepath.Join(dir, name, SnapshotMetaFile))
		if err != nil {
			return warnings, fmt.Errorf("stream %s: %w", name, err)
		}
		if Digest(metaBytes) != want.Meta {
			return warnings, fmt.Errorf("stream %s: metadata %w", name, ErrDigestMismatch)
		}
		var meta Metadata
		if err := json.Unmarshal(metaBytes, &meta); err != nil {
			return warnings, fmt.Errorf("stream %s: %w", name, err)
		}
		warnings = append(warnings, policy.MissingSubjects(meta.Config)...)
		warnings = append(warnings, fresh.OutsidePolicyDrift(meta.Config)...)
		open := func() (io.ReadCloser, error) { return os.Open(filepath.Join(dir, name, SnapshotDataFile)) }
		for attempt := 1; ; attempt++ {
			if alreadyRestored(ctx, nc, policy, meta.State) {
				// Built by an earlier attempt (a finish that timed out
				// after the server built the stream, or this pod run
				// again): the server builds a stream only on the final
				// message, which is sent only for the operator's digest.
				err = nil
				break
			}
			err = RestoreVerified(ctx, nc, meta.Config, meta.State, open, want.Data, policy)
			if err == nil || errors.Is(err, ErrDigestMismatch) || errors.Is(err, ErrOutsidePolicy) || attempt == restoreRetries {
				break
			}
			select {
			case <-ctx.Done():
				return warnings, ctx.Err()
			case <-time.After(2 * time.Second):
			}
		}
		if err != nil {
			return warnings, err
		}
		if replicas > 1 {
			if err := RaiseReplicas(ctx, nc, name, replicas); err != nil {
				return warnings, fmt.Errorf("stream %s: raising to %d replicas: %w", name, replicas, err)
			}
		}
	}
	return warnings, nil
}

// alreadyRestored reports the stream already there as this snapshot built
// it: inside its policy, with the snapshot's message count and last
// sequence.
func alreadyRestored(ctx context.Context, nc *nats.Conn, policy StreamPolicy, state nats.StreamState) bool {
	info, err := streamInfo(ctx, nc, policy.Name)
	if err != nil {
		return false
	}
	return len(policy.Check(info.Config)) == 0 && info.State.Msgs == state.Msgs && info.State.LastSeq == state.LastSeq
}

// HashMain runs `bus-hash --dir D --streams S`: the digests of every staged
// stream's data and metadata, as a JSON object keyed by stream, written to --termination-log, which the
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

// StagedDigest pins one staged stream: the digest of its data, and of the
// metadata beside it, whose config and state the restore sends. Both are
// fixed at hashing time, so neither can change on the staging volume
// between the snapshot and the restore unnoticed.
type StagedDigest struct {
	Data string `json:"data"`
	Meta string `json:"meta"`
}

// HashStaged digests every named stream's staged data and metadata under dir.
func HashStaged(dir string, streams []string) (map[string]StagedDigest, error) {
	digests := map[string]StagedDigest{}
	for _, name := range streams {
		var d StagedDigest
		for _, f := range []struct {
			file string
			into *string
		}{{SnapshotDataFile, &d.Data}, {SnapshotMetaFile, &d.Meta}} {
			sum, err := digestOf(func() (io.ReadCloser, error) { return os.Open(filepath.Join(dir, name, f.file)) })
			if err != nil {
				return nil, fmt.Errorf("stream %s: %w", name, err)
			}
			*f.into = sum
		}
		digests[name] = d
	}
	return digests, nil
}

package busmigrate

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"reflect"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
)

// The JetStream API subjects the one-to-three leg uses, beside info and
// update: snapshot (its chunks arrive on the run's own inbox and are acked on
// $JS.SNAPSHOT.ACK.<stream>.>), restore (its chunks go to
// $JS.SNAPSHOT.RESTORE.<stream>.>, a subject the server names from the
// restored stream's own name, so a grant on it is what bounds the name), and
// delete, for a restored stream whose config reads back wrong.
const (
	streamSnapshotSubject = "$JS.API.STREAM.SNAPSHOT.%s"
	streamRestoreSubject  = "$JS.API.STREAM.RESTORE.%s"
	streamDeleteSubject   = "$JS.API.STREAM.DELETE.%s"
)

// snapshotChunkSize is the chunk the server is asked to send a snapshot in.
const snapshotChunkSize = 128 * 1024

// restoreChunkSize is the chunk a restore is sent in.
const restoreChunkSize = 64 * 1024

// restoreFinishTimeout is the wait for the server to build a restored stream
// once the last chunk is in.
const restoreFinishTimeout = 10 * time.Minute

// Metadata is what a snapshot records beside its data: the stream's config
// and state as the server reported them when the snapshot began.
type Metadata struct {
	Config nats.StreamConfig `json:"config"`
	State  nats.StreamState  `json:"state"`
}

// Digest is the hex SHA-256 of a snapshot's data, the value the operator
// computes from the staged snapshot and the restore checks against.
func Digest(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// Snapshot streams name's snapshot, consumers included, into w and returns
// its metadata. The chunks arrive on a fresh inbox; each one the server asks
// to be acked is acked as it arrives, which is the server's flow control.
func Snapshot(ctx context.Context, nc *nats.Conn, name string, w io.Writer) (*Metadata, error) {
	inbox := nc.NewRespInbox()
	done := make(chan error, 1)
	finish := func(err error) {
		select {
		case done <- err:
		default:
		}
	}
	sub, err := nc.Subscribe(inbox, func(m *nats.Msg) {
		if len(m.Data) == 0 {
			if status := m.Header.Get("Status"); status != "" && !strings.HasPrefix(status, "204") {
				finish(fmt.Errorf("snapshot %s: %s %s", name, status, m.Header.Get("Description")))
				return
			}
			finish(nil)
			return
		}
		if m.Reply != "" {
			if err := m.Respond(nil); err != nil {
				finish(err)
				return
			}
		}
		if _, err := w.Write(m.Data); err != nil {
			finish(err)
		}
	})
	if err != nil {
		return nil, err
	}
	defer sub.Unsubscribe()
	if err := sub.SetPendingLimits(-1, -1); err != nil {
		return nil, err
	}
	body, err := json.Marshal(map[string]any{"deliver_subject": inbox, "no_consumers": false, "chunk_size": snapshotChunkSize})
	if err != nil {
		return nil, err
	}
	var meta Metadata
	if err := request(ctx, nc, fmt.Sprintf(streamSnapshotSubject, name), body, &meta); err != nil {
		return nil, err
	}
	if meta.Config.Name != name {
		return nil, errors.New("snapshot names another stream")
	}
	select {
	case err := <-done:
		if err != nil {
			return nil, err
		}
		return &meta, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// ErrDigestMismatch is a snapshot whose bytes are not the ones the operator
// hashed.
var ErrDigestMismatch = errors.New("snapshot digest does not match the operator's")

// RestoreVerified restores one stream from data, which the caller read once:
// it hashes exactly these bytes and refuses before any request if they are
// not the snapshot the operator hashed (wantDigest), so nothing is re-read
// between the check and the restore. The stream is created with cfg, the
// config the operator renders for it, never the snapshot's own; state is
// the snapshot's, which the server uses only to size the restore. The
// restored config is then read back, and a stream whose config is not cfg is
// deleted.
func RestoreVerified(ctx context.Context, nc *nats.Conn, cfg nats.StreamConfig, state nats.StreamState, data []byte, wantDigest string) error {
	if Digest(data) != wantDigest {
		return fmt.Errorf("stream %s: %w", cfg.Name, ErrDigestMismatch)
	}
	if err := restore(ctx, nc, cfg, state, data); err != nil {
		return fmt.Errorf("stream %s: %w", cfg.Name, err)
	}
	return verifyOrDelete(ctx, nc, cfg)
}

// verifyOrDelete reads a restored stream's config back and deletes the
// stream when it is not want.
func verifyOrDelete(ctx context.Context, nc *nats.Conn, want nats.StreamConfig) error {
	info, err := streamInfo(ctx, nc, want.Name)
	if err != nil {
		return fmt.Errorf("stream %s: reading back the restored config: %w", want.Name, err)
	}
	if diff := configDiff(want, info.Config); diff != "" {
		if derr := request(ctx, nc, fmt.Sprintf(streamDeleteSubject, want.Name), nil, nil); derr != nil {
			return fmt.Errorf("stream %s: restored config differs (%s), and deleting it failed: %w", want.Name, diff, derr)
		}
		return fmt.Errorf("stream %s: restored config differs (%s); deleted", want.Name, diff)
	}
	return nil
}

func restore(ctx context.Context, nc *nats.Conn, cfg nats.StreamConfig, state nats.StreamState, data []byte) error {
	body, err := json.Marshal(map[string]any{"config": cfg, "state": state})
	if err != nil {
		return err
	}
	var resp struct {
		DeliverSubject string `json:"deliver_subject"`
	}
	if err := request(ctx, nc, fmt.Sprintf(streamRestoreSubject, cfg.Name), body, &resp); err != nil {
		return err
	}
	if resp.DeliverSubject == "" {
		return errors.New("restore answered with no deliver subject")
	}
	r := bytes.NewReader(data)
	chunk := make([]byte, restoreChunkSize)
	for {
		n, err := r.Read(chunk)
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		if err := sendChunk(ctx, nc, resp.DeliverSubject, chunk[:n]); err != nil {
			return fmt.Errorf("sending a chunk: %w", err)
		}
	}
	fctx, cancel := context.WithTimeout(ctx, restoreFinishTimeout)
	defer cancel()
	msg, err := nc.RequestWithContext(fctx, resp.DeliverSubject, nil)
	if err != nil {
		return fmt.Errorf("finishing: %w", err)
	}
	var e apiError
	if err := json.Unmarshal(msg.Data, &e); err != nil {
		return err
	}
	if e.Error != nil {
		return fmt.Errorf("finishing: %s (%d)", e.Error.Description, e.Error.ErrCode)
	}
	return nil
}

// sendChunk sends one restore chunk and waits for the server's ack, which is
// empty, or an API error.
func sendChunk(ctx context.Context, nc *nats.Conn, subject string, chunk []byte) error {
	rctx, cancel := context.WithTimeout(ctx, apiTimeout)
	defer cancel()
	msg, err := nc.RequestWithContext(rctx, subject, chunk)
	if err != nil {
		return err
	}
	if len(msg.Data) == 0 {
		return nil
	}
	var e apiError
	if err := json.Unmarshal(msg.Data, &e); err != nil {
		return err
	}
	if e.Error != nil {
		return fmt.Errorf("%s (%d)", e.Error.Description, e.Error.ErrCode)
	}
	return nil
}

// configDiff names the fields of the restored config that are not the ones
// asked for: everything an operator render sets, which is everything that
// decides where a stream's messages come from, which subjects it captures and
// how much it may hold. "" when they match.
func configDiff(want, got nats.StreamConfig) string {
	want = withServerDefaults(want)
	var diffs []string
	check := func(field string, w, g any) {
		if !reflect.DeepEqual(w, g) {
			diffs = append(diffs, fmt.Sprintf("%s: want %v, got %v", field, w, g))
		}
	}
	check("name", want.Name, got.Name)
	check("subjects", want.Subjects, got.Subjects)
	check("retention", want.Retention, got.Retention)
	check("max_consumers", want.MaxConsumers, got.MaxConsumers)
	check("max_msgs", want.MaxMsgs, got.MaxMsgs)
	check("max_bytes", want.MaxBytes, got.MaxBytes)
	check("max_age", want.MaxAge, got.MaxAge)
	check("max_msgs_per_subject", want.MaxMsgsPerSubject, got.MaxMsgsPerSubject)
	check("max_msg_size", want.MaxMsgSize, got.MaxMsgSize)
	check("discard", want.Discard, got.Discard)
	check("storage", want.Storage, got.Storage)
	check("replicas", want.Replicas, got.Replicas)
	check("duplicates", want.Duplicates, got.Duplicates)
	check("allow_direct", want.AllowDirect, got.AllowDirect)
	check("allow_rollup_hdrs", want.AllowRollup, got.AllowRollup)
	check("deny_delete", want.DenyDelete, got.DenyDelete)
	check("deny_purge", want.DenyPurge, got.DenyPurge)
	check("sealed", false, got.Sealed)
	check("mirror", (*nats.StreamSource)(nil), got.Mirror)
	check("sources", 0, len(got.Sources))
	check("republish", (*nats.RePublish)(nil), got.RePublish)
	check("subject_transform", (*nats.SubjectTransformConfig)(nil), got.SubjectTransform)
	return strings.Join(diffs, "; ")
}

// streamDefaultDuplicates is nats-server's duplicate window for a stream that
// sets none (StreamDefaultDuplicatesWindow).
const streamDefaultDuplicates = 2 * time.Minute

// withServerDefaults fills the fields a config leaves zero the way
// nats-server's checkStreamCfg does (2.10 and later): every limit to -1 for
// none, and the duplicate window to two minutes, or the max age when that is
// shorter. A config the operator renders leaves them zero; the read-back
// compares what the server stored with this, so a zero field neither fails
// every restore nor lets a restore set it to anything.
func withServerDefaults(cfg nats.StreamConfig) nats.StreamConfig {
	for _, limit := range []*int64{&cfg.MaxMsgs, &cfg.MaxMsgsPerSubject, &cfg.MaxBytes} {
		if *limit == 0 {
			*limit = -1
		}
	}
	if cfg.MaxMsgSize == 0 {
		cfg.MaxMsgSize = -1
	}
	if cfg.MaxConsumers == 0 {
		cfg.MaxConsumers = -1
	}
	if cfg.Duplicates == 0 && cfg.Mirror == nil {
		cfg.Duplicates = streamDefaultDuplicates
		if cfg.MaxAge != 0 && cfg.MaxAge < streamDefaultDuplicates {
			cfg.Duplicates = cfg.MaxAge
		}
	}
	return cfg
}

// RaiseReplicas sets a restored stream to n replicas through an update that
// sends its full config with only the replica count changed.
func RaiseReplicas(ctx context.Context, nc *nats.Conn, name string, n int) error {
	info, err := streamInfo(ctx, nc, name)
	if err != nil {
		return err
	}
	cfg := info.Config
	cfg.Replicas = n
	return updateStream(ctx, nc, name, cfg)
}

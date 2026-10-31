package busmigrate

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
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

// ErrOutsidePolicy is a snapshot config, or a restored stream's, that does
// not pass the operator's policy for that stream.
var ErrOutsidePolicy = errors.New("config is outside the operator's policy")

// Opener opens a snapshot's data for one read from the start. The restore pod
// reads the staging volume read-only, and it is the only pod holding that
// volume (ReadWriteOncePod), so nothing writes it between two opens.
type Opener func() (io.ReadCloser, error)

// RestoreVerified restores one stream from its staged snapshot, sending
// cfg, the snapshot's own config, pinned by the operator's digest of its
// metadata. policy gates it twice, the same table both times: cfg must pass
// policy.Check before anything is sent, and the restored stream's config is
// read back and must pass it again, or the stream is deleted.
//
// It hashes the data first and sends nothing when it is not the snapshot the
// operator hashed (wantDigest), so a mismatch holds no name. It then
// restores, hashing exactly the bytes it sends, and sends the restore's
// final message, the one on which nats-server builds the stream, only when
// they match too. On a late mismatch it withholds that message and deletes
// the name, which a clustered server keeps reserved after a stalled restore.
// state is the snapshot's, which the server uses only to size the restore.
func RestoreVerified(ctx context.Context, nc *nats.Conn, cfg nats.StreamConfig, state nats.StreamState, open Opener, wantDigest string, policy StreamPolicy) error {
	if d := policy.Check(cfg); len(d) != 0 {
		return fmt.Errorf("stream %s: %w: %s", cfg.Name, ErrOutsidePolicy, strings.Join(d, "; "))
	}
	pre, err := digestOf(open)
	if err != nil {
		return fmt.Errorf("stream %s: hashing the snapshot: %w", cfg.Name, err)
	}
	if pre != wantDigest {
		return fmt.Errorf("stream %s: %w", cfg.Name, ErrDigestMismatch)
	}
	data, err := open()
	if err != nil {
		return fmt.Errorf("stream %s: %w", cfg.Name, err)
	}
	defer data.Close()
	if err := restore(ctx, nc, cfg, state, data, wantDigest); err != nil {
		if errors.Is(err, ErrDigestMismatch) {
			if derr := request(ctx, nc, fmt.Sprintf(streamDeleteSubject, cfg.Name), nil, nil); derr != nil {
				return fmt.Errorf("stream %s: %w, and freeing its name failed: %v", cfg.Name, err, derr)
			}
		}
		return fmt.Errorf("stream %s: %w", cfg.Name, err)
	}
	return verifyOrDelete(ctx, nc, policy)
}

func digestOf(open Opener) (string, error) {
	r, err := open()
	if err != nil {
		return "", err
	}
	defer r.Close()
	h := sha256.New()
	if _, err := io.Copy(h, r); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// verifyOrDelete reads a restored stream's config back and deletes the
// stream when it does not pass policy.
func verifyOrDelete(ctx context.Context, nc *nats.Conn, policy StreamPolicy) error {
	info, err := streamInfo(ctx, nc, policy.Name)
	if err != nil {
		return fmt.Errorf("stream %s: reading back the restored config: %w", policy.Name, err)
	}
	if d := policy.Check(info.Config); len(d) != 0 {
		if derr := request(ctx, nc, fmt.Sprintf(streamDeleteSubject, policy.Name), nil, nil); derr != nil {
			return fmt.Errorf("stream %s: restored config is outside its policy (%s), and deleting it failed: %w", policy.Name, strings.Join(d, "; "), derr)
		}
		return fmt.Errorf("stream %s: restored config is outside its policy (%s); deleted", policy.Name, strings.Join(d, "; "))
	}
	return nil
}

func restore(ctx context.Context, nc *nats.Conn, cfg nats.StreamConfig, state nats.StreamState, data io.Reader, wantDigest string) error {
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
	hash := sha256.New()
	chunk := make([]byte, restoreChunkSize)
	for {
		n, err := io.ReadFull(data, chunk)
		if n > 0 {
			hash.Write(chunk[:n])
			if err := sendChunk(ctx, nc, resp.DeliverSubject, chunk[:n]); err != nil {
				return fmt.Errorf("sending a chunk: %w", err)
			}
		}
		if err == io.EOF || err == io.ErrUnexpectedEOF {
			break
		}
		if err != nil {
			return err
		}
	}
	if hex.EncodeToString(hash.Sum(nil)) != wantDigest {
		return ErrDigestMismatch
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

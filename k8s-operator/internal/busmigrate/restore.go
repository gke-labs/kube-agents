package busmigrate

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"slices"
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
	// The forward restore's settle wait reads, never changes, consumers.
	consumerNamesSubject = "$JS.API.CONSUMER.NAMES.%s"
	consumerInfoSubject  = "$JS.API.CONSUMER.INFO.%s.%s"
	streamDeleteSubject   = "$JS.API.STREAM.DELETE.%s"
)

// snapshotChunkSize is the chunk the server is asked to send a snapshot in.
const snapshotChunkSize = 128 * 1024

// restoreChunkSize is the chunk a restore is sent in.
const restoreChunkSize = 64 * 1024

// restoreFinishTimeout is the wait for the server to build a restored stream
// once the last chunk is in.
const restoreFinishTimeout = 10 * time.Minute

// restoreStartTimeout is the wait for a restore's first answer. A clustered
// server answers only once the meta group has placed the stream and its
// group has a leader, which on servers that formed moments ago takes longer
// than an ordinary API call; the assignment stands either way, so a client
// that gives up early holds the name against its own retries.
const restoreStartTimeout = 30 * time.Second

// Metadata is what a snapshot records beside its data: the stream's config
// and state as the server reported them when the snapshot began.
type Metadata struct {
	Config nats.StreamConfig `json:"config"`
	State  nats.StreamState  `json:"state"`
	// TakenAt is when the snapshot pod took the snapshot, by its clock: the
	// moment State describes, which the restore measures the oldest
	// message's age from. Pinned with the rest of the file by the
	// operator's digest. Absent in a snapshot taken before the field.
	TakenAt time.Time `json:"takenAt,omitzero"`
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
	meta.TakenAt = time.Now().UTC()
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

// ErrOverRenderedLimits is a snapshot that does not fit the limits the
// operator renders for its stream: more bytes than the rendered max_bytes, or
// an oldest message older than the rendered max_age. The restore refuses it
// rather than let the server trim the stream to fit, and the run turns back.
var ErrOverRenderedLimits = errors.New("snapshot does not fit the operator's rendered limits")

// withRenderedLimits is cfg with the limits the operator renders, which the
// restore sends in place of the snapshot's own: a stream comes back with the
// caps the install renders now, whatever an earlier render gave it.
func withRenderedLimits(cfg nats.StreamConfig, policy StreamPolicy) nats.StreamConfig {
	cfg.MaxBytes = policy.MaxBytes
	cfg.MaxAge = policy.MaxAge
	return cfg
}

// fitsRenderedLimits refuses a snapshot whose state, as its pinned metadata
// records it, does not fit the rendered limits at now: the moment the
// snapshot was taken. Measured then, a stream the live bus already trims to
// the rendered max_age fits, though its oldest message ages past it during
// the freeze (the restored stream's own max_age trims that); one an earlier
// install kept longer does not. A limit of zero or less is no limit.
func fitsRenderedLimits(name string, state nats.StreamState, policy StreamPolicy, now time.Time) error {
	if policy.MaxBytes > 0 && state.Bytes > uint64(policy.MaxBytes) {
		return fmt.Errorf("stream %s: %w: the snapshot holds %d bytes, over the rendered max_bytes of %d", name, ErrOverRenderedLimits, state.Bytes, policy.MaxBytes)
	}
	if policy.MaxAge > 0 && state.Msgs > 0 && !state.FirstTime.IsZero() && now.Sub(state.FirstTime) > policy.MaxAge {
		return fmt.Errorf("stream %s: %w: its oldest message, from %s, is older than the rendered max_age of %s", name, ErrOverRenderedLimits, state.FirstTime.UTC().Format(time.RFC3339), policy.MaxAge)
	}
	return nil
}

// Opener opens a snapshot's data for one read from the start. The restore pod
// reads the staging volume read-only, and it is the only pod holding that
// volume (ReadWriteOncePod), so nothing writes it between two opens.
type Opener func() (io.ReadCloser, error)

// RestoreVerified restores one stream from its staged snapshot, sending
// cfg, the snapshot's own config, pinned by the operator's digest of its
// metadata, with the operator's rendered max_bytes and max_age in place of
// its own (withRenderedLimits). A snapshot that does not fit those limits,
// by its bytes or by the age of its oldest message, is refused before
// anything is sent (ErrOverRenderedLimits). policy gates it twice, the same
// table both times: cfg must pass policy.Check before anything is sent, and
// the restored stream's config is read back and must pass it again, or the
// stream is deleted.
//
// It hashes the data first and sends nothing when it is not the snapshot the
// operator hashed (wantDigest), so a mismatch holds no name. It then
// restores, hashing exactly the bytes it sends, and sends the restore's
// final message, the one on which nats-server builds the stream, only when
// they match too. On a late mismatch it withholds that message and deletes
// the name, which a clustered server keeps reserved after a stalled restore.
// state is the snapshot's, which the server uses only to size the restore.
func RestoreVerified(ctx context.Context, nc *nats.Conn, cfg nats.StreamConfig, state nats.StreamState, takenAt time.Time, open Opener, wantDigest string, policy StreamPolicy) error {
	_, err := RestoreVerifiedCounting(ctx, nc, cfg, state, takenAt, open, wantDigest, policy)
	return err
}

// RestoreVerifiedCounting is RestoreVerified that also returns how many
// consumers the restore brought back, as the server's final answer counts
// them: the restored store's own, before the server assigns any of them.
func RestoreVerifiedCounting(ctx context.Context, nc *nats.Conn, cfg nats.StreamConfig, state nats.StreamState, takenAt time.Time, open Opener, wantDigest string, policy StreamPolicy) (int, error) {
	cfg = withRenderedLimits(cfg, policy)
	if d := policy.Check(cfg); len(d) != 0 {
		return 0, fmt.Errorf("stream %s: %w: %s", cfg.Name, ErrOutsidePolicy, strings.Join(d, "; "))
	}
	at := takenAt
	if at.IsZero() {
		at = time.Now()
	}
	if err := fitsRenderedLimits(cfg.Name, state, policy, at); err != nil {
		return 0, err
	}
	return restoreChecked(ctx, nc, cfg, state, open, wantDigest, policy)
}

// RestoreAsItWas is the way back's restore: it puts the stream back into one
// server as the snapshot recorded it, limits included, so that a run turned
// back by a snapshot that does not fit the rendered limits (or by anything
// else) can always return the bus to where it was. It differs from
// RestoreVerified only in the limits: max_bytes and max_age are the
// snapshot's own, which its metadata digest pins, and policy holds the stream
// to those, on the way in and on read-back; every other field is held to the
// operator's policy as before, and there is no fit check.
func RestoreAsItWas(ctx context.Context, nc *nats.Conn, cfg nats.StreamConfig, state nats.StreamState, open Opener, wantDigest string, policy StreamPolicy) error {
	policy.MaxBytes, policy.MaxAge = cfg.MaxBytes, cfg.MaxAge
	if d := policy.Check(cfg); len(d) != 0 {
		return fmt.Errorf("stream %s: %w: %s", cfg.Name, ErrOutsidePolicy, strings.Join(d, "; "))
	}
	_, err := restoreChecked(ctx, nc, cfg, state, open, wantDigest, policy)
	return err
}

// restoreChecked is both restores' common path once the config has passed
// policy: the digest before anything is sent, the restore hashing what it
// sends, and the read-back against policy. It returns the restored
// consumer count from the server's final answer.
func restoreChecked(ctx context.Context, nc *nats.Conn, cfg nats.StreamConfig, state nats.StreamState, open Opener, wantDigest string, policy StreamPolicy) (int, error) {
	pre, err := digestOf(open)
	if err != nil {
		return 0, fmt.Errorf("stream %s: hashing the snapshot: %w", cfg.Name, err)
	}
	if pre != wantDigest {
		return 0, fmt.Errorf("stream %s: %w", cfg.Name, ErrDigestMismatch)
	}
	data, err := open()
	if err != nil {
		return 0, fmt.Errorf("stream %s: %w", cfg.Name, err)
	}
	defer data.Close()
	consumers, err := restore(ctx, nc, cfg, state, data, wantDigest)
	if err != nil {
		if errors.Is(err, ErrDigestMismatch) {
			if derr := request(ctx, nc, fmt.Sprintf(streamDeleteSubject, cfg.Name), nil, nil); derr != nil {
				return 0, fmt.Errorf("stream %s: %w, and freeing its name failed: %v", cfg.Name, err, derr)
			}
		}
		return 0, fmt.Errorf("stream %s: %w", cfg.Name, err)
	}
	return consumers, verifyOrDelete(ctx, nc, policy)
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
			return fmt.Errorf("stream %s: restored %w (%s), and deleting it failed: %w", policy.Name, ErrOutsidePolicy, strings.Join(d, "; "), derr)
		}
		return fmt.Errorf("stream %s: restored %w (%s); deleted", policy.Name, ErrOutsidePolicy, strings.Join(d, "; "))
	}
	return nil
}

func restore(ctx context.Context, nc *nats.Conn, cfg nats.StreamConfig, state nats.StreamState, data io.Reader, wantDigest string) (int, error) {
	body, err := json.Marshal(map[string]any{"config": cfg, "state": state})
	if err != nil {
		return 0, err
	}
	var resp struct {
		ChunkSubject string `json:"deliver_subject"`
	}
	if err := requestWithin(ctx, nc, fmt.Sprintf(streamRestoreSubject, cfg.Name), body, &resp, restoreStartTimeout); err != nil {
		return 0, err
	}
	if resp.ChunkSubject == "" {
		return 0, errors.New("restore answered with no chunk subject")
	}
	hash := sha256.New()
	chunk := make([]byte, restoreChunkSize)
	for {
		n, err := io.ReadFull(data, chunk)
		if n > 0 {
			hash.Write(chunk[:n])
			if err := sendChunk(ctx, nc, resp.ChunkSubject, chunk[:n]); err != nil {
				return 0, fmt.Errorf("sending a chunk: %w", err)
			}
		}
		if err == io.EOF || err == io.ErrUnexpectedEOF {
			break
		}
		if err != nil {
			return 0, err
		}
	}
	if hex.EncodeToString(hash.Sum(nil)) != wantDigest {
		return 0, ErrDigestMismatch
	}
	fctx, cancel := context.WithTimeout(ctx, restoreFinishTimeout)
	defer cancel()
	msg, err := nc.RequestWithContext(fctx, resp.ChunkSubject, nil)
	if err != nil {
		return 0, fmt.Errorf("finishing: %w", err)
	}
	var e apiError
	if err := json.Unmarshal(msg.Data, &e); err != nil {
		return 0, err
	}
	if e.Error != nil {
		return 0, fmt.Errorf("finishing: %s (%d)", e.Error.Description, e.Error.ErrCode)
	}
	// The final answer carries the restored stream's state, its consumer
	// count from the restored store itself.
	var fin struct {
		State struct {
			Consumers int `json:"consumer_count"`
		} `json:"state"`
	}
	if err := json.Unmarshal(msg.Data, &fin); err != nil {
		return 0, err
	}
	return fin.State.Consumers, nil
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

// restoreSettlePoll is how often WaitRestored reads the stream again.
var restoreSettlePoll = 250 * time.Millisecond

// DurableSpec is a restored durable consumer the settle wait reads, with
// the replica count the raise gives it (ExpectedConsumerReplicas).
type DurableSpec struct {
	Name     string
	Replicas int
}

// WaitRestored waits until a restored stream raised to replicas has settled:
// its config reads replicas, it has a leader, every other replica is current,
// and every durable in durables (AssignedDurables, before the raise) answers
// from its own leader with its expected replicas, all current.
// RaiseReplicas only updates the config; the new replicas catch up, and the
// restored consumers reappear on their leaders, some time after (#6174
// measured more than 2 s on CI runners), so a run that moved on at once could
// release the freeze with a consumer still answering "not found".
//
// The stream's own consumer count can't stand in for this: on a clustered
// stream, nats-server answers it with the cluster's consumer assignments
// (jetstream_api.go, clusterWideConsCount), which are recorded before a
// consumer is up on its leader. So each durable is read by name.
//
// A durable that answers "not found", or doesn't answer, is still being
// placed while the stream still lists it. One no longer listed was deleted,
// which under the migration's write freeze only the server does (a durable
// with an inactivity threshold, idle), so it is not waited for. It returns
// how long the wait took, or an error naming what was still short when ctx
// ended.
func WaitRestored(ctx context.Context, nc *nats.Conn, name string, replicas int, durables []DurableSpec) (time.Duration, error) {
	start := time.Now()
	short := "no answer yet"
	pending := slices.Clone(durables)
	for {
		why := ""
		info, err := streamInfo(ctx, nc, name)
		switch {
		case err != nil:
			why = err.Error()
		case info.Config.Replicas != replicas:
			why = fmt.Sprintf("config reads %d replicas", info.Config.Replicas)
		case info.Cluster == nil || info.Cluster.Leader == "":
			why = "no stream leader"
		case len(info.Cluster.Replicas) != replicas-1:
			why = fmt.Sprintf("%d of %d followers known", len(info.Cluster.Replicas), replicas-1)
		case !allCurrent(info.Cluster.Replicas):
			why = "a follower is not current"
		default:
			pending, why = consumersNotSettled(ctx, nc, name, pending)
			if len(pending) == 0 {
				return time.Since(start), nil
			}
		}
		// A request cut off by ctx's end says only that; keep what was
		// short before it.
		if ctx.Err() == nil || short == "no answer yet" {
			short = why
		}
		select {
		case <-ctx.Done():
			return time.Since(start), fmt.Errorf("not at R%d with its consumers after %s: %s", replicas, time.Since(start).Round(time.Second), short)
		case <-time.After(restoreSettlePoll):
		}
	}
}

// consumersNotSettled reads each pending durable and returns those not yet
// up on their leader with their expected replicas all current, and what the
// first one is short of.
func consumersNotSettled(ctx context.Context, nc *nats.Conn, stream string, pending []DurableSpec) ([]DurableSpec, string) {
	var listed []string
	read := false
	var left []DurableSpec
	short := ""
	for _, d := range pending {
		info, err := consumerInfo(ctx, nc, stream, d.Name)
		why := ""
		switch {
		case isConsumerNotFound(err):
			if !read {
				names, lerr := consumerNames(ctx, nc, stream)
				if lerr != nil {
					why = fmt.Sprintf("listing consumers: %v", lerr)
					break
				}
				listed, read = names, true
			}
			if !slices.Contains(listed, d.Name) {
				continue // deleted, so not waited for
			}
			why = fmt.Sprintf("consumer %s not found on its leader", d.Name)
		case err != nil:
			why = fmt.Sprintf("consumer %s: %v", d.Name, err)
		case info.Cluster == nil || info.Cluster.Leader == "":
			why = fmt.Sprintf("consumer %s has no leader", d.Name)
		case len(info.Cluster.Replicas) != d.Replicas-1:
			// An R1 answer (no replicas) before the raise's remap reaches
			// the consumer is not settled at three.
			why = fmt.Sprintf("consumer %s has %d of %d followers", d.Name, len(info.Cluster.Replicas), d.Replicas-1)
		case !allCurrent(info.Cluster.Replicas):
			why = fmt.Sprintf("consumer %s has a replica not current", d.Name)
		default:
			continue
		}
		left = append(left, d)
		if short == "" {
			short = why
		}
	}
	return left, short
}

// ExpectedConsumerReplicas is the replica count a consumer has once its
// stream is raised to replicas, by nats-server's own rule for a raise
// (jetstream_cluster.go, the consumer remap): a named consumer with no count
// of its own, or any consumer on a stream that isn't limits-retained,
// follows the stream; one with its own count keeps it.
func ExpectedConsumerReplicas(cfg nats.ConsumerConfig, stream nats.StreamConfig, replicas int) int {
	if cfg.Replicas == 0 || stream.Retention != nats.LimitsPolicy {
		return replicas
	}
	return cfg.Replicas
}

// AssignedDurables waits, before a just-restored stream is raised, until
// the stream lists at least restored consumers (the restore's own count;
// negative when unknown, a stream an earlier run of the pod restored) and
// every one listed answers, then returns the durables among them with the
// replica count the raise gives each. After a clustered restore the server
// proposes the restored consumers' assignments only once the restore has
// answered, so the list can lag it, and a raise sent before an assignment
// would leave that consumer out of its remap. Ephemerals are left out: the
// server deletes them once idle, and one deleted meanwhile still counts
// towards restored. Errors, a lagging or silent answer, keep it waiting
// until ctx ends.
func AssignedDurables(ctx context.Context, nc *nats.Conn, stream string, restored int, cfg nats.StreamConfig, replicas int) ([]DurableSpec, error) {
	short := "no answer yet"
	gone := map[string]bool{}
	var unanswered []string
	for {
		why := ""
		names, err := listAssigned(ctx, nc, stream)
		if err != nil {
			why = fmt.Sprintf("listing consumers: %v", err)
		} else {
			// A name that answered "not found" last time and is no longer
			// listed was deleted: an idle ephemeral.
			for _, n := range unanswered {
				if !slices.Contains(names, n) {
					gone[n] = true
				}
			}
			unanswered = nil
			if len(names)+len(gone) < restored {
				why = fmt.Sprintf("%d of %d restored consumers assigned", len(names)+len(gone), restored)
			} else {
				var specs []DurableSpec
				for _, n := range names {
					info, err := consumerInfo(ctx, nc, stream, n)
					switch {
					case err != nil:
						unanswered = append(unanswered, n)
						if why == "" {
							why = fmt.Sprintf("consumer %s: %v", n, err)
						}
					case info.Config.Durable != "":
						specs = append(specs, DurableSpec{Name: n, Replicas: ExpectedConsumerReplicas(info.Config, cfg, replicas)})
					}
				}
				if why == "" {
					return specs, nil
				}
			}
		}
		if ctx.Err() == nil || short == "no answer yet" {
			short = why
		}
		select {
		case <-ctx.Done():
			return nil, fmt.Errorf("restored consumers not assigned: %s", short)
		case <-time.After(restoreSettlePoll):
		}
	}
}

// listAssigned is AssignedDurables' read of the stream's consumer list, a
// variable so a test can make the server's assignments lag the restore.
var listAssigned = consumerNames

// consumerNames lists a stream's consumers, page by page.
func consumerNames(ctx context.Context, nc *nats.Conn, stream string) ([]string, error) {
	var names []string
	for {
		body, err := json.Marshal(map[string]int{"offset": len(names)})
		if err != nil {
			return nil, err
		}
		var page struct {
			Total     int      `json:"total"`
			Consumers []string `json:"consumers"`
		}
		if err := request(ctx, nc, fmt.Sprintf(consumerNamesSubject, stream), body, &page); err != nil {
			return nil, err
		}
		names = append(names, page.Consumers...)
		if len(page.Consumers) == 0 || len(names) >= page.Total {
			return names, nil
		}
	}
}

func consumerInfo(ctx context.Context, nc *nats.Conn, stream, name string) (*nats.ConsumerInfo, error) {
	var info nats.ConsumerInfo
	if err := request(ctx, nc, fmt.Sprintf(consumerInfoSubject, stream, name), nil, &info); err != nil {
		return nil, err
	}
	return &info, nil
}

// jsConsumerNotFound is the JetStream API's error code for a consumer the
// answering server does not hold.
const jsConsumerNotFound = 10014

func isConsumerNotFound(err error) bool {
	var e *jsAPIError
	return errors.As(err, &e) && e.code == jsConsumerNotFound
}

func allCurrent(peers []*nats.PeerInfo) bool {
	for _, p := range peers {
		if p == nil || !p.Current {
			return false
		}
	}
	return true
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

package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The relay's reports at rest (lib.ReportRecord): one row per address in
// the session-state bucket, under lib.ReportKeyPrefix, so a reply under a
// report still carries it after a restart or a failover (reports.go). The
// rows ride the verbs the gateway already holds on the bucket: a publish
// to write or delete one, DIRECT.GET to read one, and the key listing's
// consumer for the prune. Older gateways list only "sessions.>" there and
// read exact keys, so they never see a row.
const (
	// reportRetention is how long a row is kept after its report was
	// posted: the platform route's session-kv keeps its incident rows as
	// long (SESSION_KV_CLEANUP_TTL_DAYS's default in
	// agents/platform/scripts/session_kv_server.py).
	reportRetention = 14 * 24 * time.Hour
	// reportStoreBytesMax bounds the rows' bytes in all, the newest kept:
	// the memory cache's budget (pushedReportsBytesMax), well under the
	// bucket's 256 MiB, which it shares with every session record.
	reportStoreBytesMax = pushedReportsBytesMax
	// reportStoreRowsMax bounds the rows kept, the newest kept, so one
	// prune pass reads every row: a row is a thread's newest report, or a
	// console report on one root.
	reportStoreRowsMax = 4096
	// reportPruneListMax bounds the keys one pass lists. Past the row bound
	// only by what was written since the last pass; a pass that meets more
	// prunes what it read and the next takes the rest.
	reportPruneListMax = 2 * reportStoreRowsMax
	// reportPruneInterval paces the prune, which runs on the active gateway
	// only (RunWith), and reportPrunePassTimeout bounds one pass.
	reportPruneInterval    = 10 * time.Minute
	reportPrunePassTimeout = time.Minute
	// reportStoreTimeout bounds one row read or write on a turn's or a
	// post's path: the relay is a help, not a condition of either.
	reportStoreTimeout = 2 * time.Second
)

// errReportForeign is a write refused because the row at the address was
// stored by another source: a writer replaces only its own rows.
var errReportForeign = errors.New("the report row at this address belongs to another source")

// reportLabel names a row in an error.
func reportLabel(key string) string {
	return "report " + key
}

// GetReport reads the row at addr: nil when there is none, or when the row
// there names another address (a hash collision, read as none).
func (r *Registry) GetReport(ctx context.Context, addr lib.ReportAddress) (*lib.ReportRecord, error) {
	kv, err := r.kv(ctx)
	if err != nil {
		return nil, err
	}
	key := addr.Key()
	entry, err := kv.Get(ctx, key)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("%s: %w", reportLabel(key), err)
	}
	var rec lib.ReportRecord
	if err := json.Unmarshal(entry.Value(), &rec); err != nil {
		return nil, fmt.Errorf("%s: %w: %w", reportLabel(key), errRecordUndecodable, err)
	}
	if rec.ChatID != addr.ChatID || rec.ThreadID != addr.ThreadID {
		return nil, nil
	}
	return &rec, nil
}

// PutReport stores rec at its address, stamped with this gateway's term.
// It creates the row when there is none and otherwise replaces it only
// when the stored row is the same source's (errReportForeign if not) and
// no newer than rec (a newer one stays, and PutReport returns nil), on the
// revision it read, so a write landing in between is re-read and checked
// again, fencedWriteAttempts times at most. A stored row that does not
// parse names no source and is replaced, as Put replaces such a record.
//
// Fenced as Put is: above term 0 the term counter and the stored row's
// epoch are checked first (fencedKey), and a newer term's refusal is
// ErrFenced, after the step-down hook (SetOnFenced) has run.
func (r *Registry) PutReport(ctx context.Context, rec *lib.ReportRecord) error {
	kv, err := r.kv(ctx)
	if err != nil {
		return err
	}
	key := lib.ReportKey(rec.ChatID, rec.ThreadID)
	mine := r.epoch.Load()
	stamped := *rec
	stamped.Epoch = mine
	data, err := json.Marshal(&stamped)
	if err != nil {
		return err
	}
	for attempt := 0; ; attempt++ {
		var entry jetstream.KeyValueEntry
		var rev uint64
		if mine != 0 {
			entry, rev, err = r.fencedKey(ctx, kv, key, reportLabel(key), mine)
			if err != nil {
				return err
			}
		} else {
			entry, err = kv.Get(ctx, key)
			switch {
			case errors.Is(err, jetstream.ErrKeyNotFound):
				entry = nil
			case err != nil:
				return fmt.Errorf("%s: %w", reportLabel(key), err)
			default:
				rev = entry.Revision()
			}
		}
		if entry != nil {
			var stored lib.ReportRecord
			if json.Unmarshal(entry.Value(), &stored) == nil {
				if stored.Source != rec.Source {
					return fmt.Errorf("%s holds a %q row, not %q: %w", reportLabel(key), stored.Source, rec.Source, errReportForeign)
				}
				if stored.PostedAt.After(rec.PostedAt) {
					return nil
				}
			}
		}
		if r.afterCheck != nil {
			r.afterCheck()
		}
		if rev == 0 {
			_, err = kv.Create(ctx, key, data)
		} else {
			_, err = kv.Update(ctx, key, data, rev)
		}
		if err == nil {
			return nil
		}
		if !isRevisionConflict(err) || attempt+1 >= fencedWriteAttempts {
			return fmt.Errorf("%s: %w", reportLabel(key), err)
		}
	}
}

// deleteReport deletes the row at key if it is still at revision rev: a
// row rewritten since it was read is left for the next pass. Fenced as
// PutReport is, above term 0.
func (r *Registry) deleteReport(ctx context.Context, kv jetstream.KeyValue, key string, rev uint64) error {
	if mine := r.epoch.Load(); mine != 0 {
		_, now, err := r.fencedKey(ctx, kv, key, reportLabel(key), mine)
		if err != nil {
			return err
		}
		if now != rev {
			return nil
		}
	}
	err := kv.Delete(ctx, key, jetstream.LastRevision(rev))
	if err != nil && !errors.Is(err, jetstream.ErrKeyNotFound) && !isRevisionConflict(err) {
		return fmt.Errorf("%s: %w", reportLabel(key), err)
	}
	return nil
}

// reportRow is one row a prune pass read.
type reportRow struct {
	key      string
	rev      uint64
	size     int
	postedAt time.Time
}

// PruneReports deletes the rows posted more than reportRetention before
// now, then the oldest of the rest until they are within
// reportStoreRowsMax rows and reportStoreBytesMax bytes, a row's bytes
// being its stored value's. A row that does not parse is deleted: only the
// gateway writes under the prefix, so it is no one's report.
//
// The cost of a pass is one key listing (a short-lived ordered consumer on
// "report.>", reading at most reportPruneListMax keys) and one DIRECT.GET
// per key listed; each delete is one publish, after a term check and a
// read above term 0. It returns the rows deleted, and stops at the first
// error, which a refusal by a newer term (ErrFenced) is.
func (r *Registry) PruneReports(ctx context.Context, now time.Time) (int, error) {
	kv, err := r.kv(ctx)
	if err != nil {
		return 0, err
	}
	lister, err := kv.ListKeysFiltered(ctx, lib.ReportKeysFilter)
	if err != nil {
		return 0, err
	}
	var keys []string
	for key := range lister.Keys() {
		if len(keys) >= reportPruneListMax {
			break
		}
		keys = append(keys, key)
	}
	_ = lister.Stop()
	if err := ctx.Err(); err != nil {
		return 0, err
	}
	deleted := 0
	drop := func(row reportRow) error {
		if err := r.deleteReport(ctx, kv, row.key, row.rev); err != nil {
			return err
		}
		deleted++
		return nil
	}
	horizon := now.Add(-reportRetention)
	var rows []reportRow
	for _, key := range keys {
		if !strings.HasPrefix(key, lib.ReportKeyPrefix) {
			continue
		}
		entry, err := kv.Get(ctx, key)
		if errors.Is(err, jetstream.ErrKeyNotFound) {
			continue
		}
		if err != nil {
			return deleted, fmt.Errorf("%s: %w", reportLabel(key), err)
		}
		row := reportRow{key: key, rev: entry.Revision(), size: len(entry.Value())}
		var rec lib.ReportRecord
		if json.Unmarshal(entry.Value(), &rec) != nil || rec.PostedAt.Before(horizon) {
			if err := drop(row); err != nil {
				return deleted, err
			}
			continue
		}
		row.postedAt = rec.PostedAt
		rows = append(rows, row)
	}
	slices.SortFunc(rows, func(a, b reportRow) int { return a.postedAt.Compare(b.postedAt) })
	total := 0
	for _, row := range rows {
		total += row.size
	}
	for len(rows) > 0 && (len(rows) > reportStoreRowsMax || total > reportStoreBytesMax) {
		if err := drop(rows[0]); err != nil {
			return deleted, err
		}
		total -= rows[0].size
		rows = rows[1:]
	}
	return deleted, nil
}

// reportPruneLoop runs PruneReports every reportPruneInterval until ctx
// ends. RunWith starts it, so it runs on the active gateway alone.
func (g *Gateway) reportPruneLoop(ctx context.Context) {
	ticker := time.NewTicker(reportPruneInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			g.pruneReportsOnce(ctx)
		}
	}
}

func (g *Gateway) pruneReportsOnce(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, reportPrunePassTimeout)
	defer cancel()
	n, err := g.reg.PruneReports(ctx, time.Now())
	if err != nil && ctx.Err() == nil {
		g.log.Warn("report prune: the pass stopped; the next one goes on", "deleted", n, "err", err)
		return
	}
	if n > 0 {
		g.log.Info("report prune: kept reports past their age or the bounds deleted", "deleted", n)
	}
}

// gatewayReports is the durable side of the gateway's pushedReports: the
// registry's report rows, read through the gateway's registry at the time
// of each call.
type gatewayReports struct {
	g *Gateway
}

func (s gatewayReports) PutReport(ctx context.Context, rec *lib.ReportRecord) error {
	return s.g.reg.PutReport(ctx, rec)
}

func (s gatewayReports) GetReport(ctx context.Context, addr lib.ReportAddress) (*lib.ReportRecord, error) {
	return s.g.reg.GetReport(ctx, addr)
}

package gateway

// A scheduled run's report from the fleet-audit report store (#5618, design
// scheduling-on-profiles.md §4 and §1 item 4).
//
// A builtin audit's run ends with `audit_report.py finish` writing an
// envelope to the install's report store, and the envelope carries the
// chat-ready report: {"schema": "kube-agents.report/v1", "card":
// <kube-agents.chat/v1>, "text": ..., "silent": bool}. The relay reads it
// through the credential broker's a2a-reports role (GET .../publish/latest,
// the broker resolving `publish` to the install's publish key), and posts
// it in place of the run's trailing message. Anything short of a report
// this run wrote (no store, 404, a refusal, a timeout, another audit's or an
// earlier run's envelope, a malformed one) falls back to the trailing
// message, as before the store.
//
// Which report a conversation sees is decided here, not by the broker: the
// role reads any audit's report on the publish key. The audit id is the
// run's own trigger stream, recorded on its task entry at submit from the
// operator's validated submission, never chat input, and only a builtin
// trigger reads (no user schedule writes the store).

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// reportStoreSchema is the report object's schema in the envelope.
	reportStoreSchema = "kube-agents.report/v1"
	// reportStorePublishKey is the store-key segment the broker resolves to
	// the install's publish key for the a2a-reports role alone. Every real
	// key holds %2F; this one doesn't, so it can't name a repository.
	reportStorePublishKey = "publish"
	// reportStoreTimeout bounds the read, out of the post's own budget.
	reportStoreTimeout = 3 * time.Second
	// reportStoreBodyMax is the store listener's own envelope cap.
	reportStoreBodyMax = 1 << 20
	// reportStoreTokenMax bounds the projected token read.
	reportStoreTokenMax = 16 << 10
	// reportStoreCardMax bounds the card kept on the run's record; a larger
	// one is dropped and the text posts alone.
	reportStoreCardMax = 32 << 10
	// reportStoreSkew is the clock allowance between the gateway, which
	// stamped the run's start, and the worker, which stamped its finish.
	reportStoreSkew = 2 * time.Minute
)

// storedReport is the report a run wrote to the store.
type storedReport struct {
	Text   string
	Card   json.RawMessage
	Silent bool
}

// reportStoreReader reads the newest report of an audit stream; nil
// reads nothing.
type reportStoreReader interface {
	latest(ctx context.Context, auditID string) (*reportEnvelope, error)
}

// reportEnvelope is the part of a stored envelope the relay reads.
type reportEnvelope struct {
	AuditID    string `json:"audit_id"`
	FinishedAt string `json:"finished_at"`
	// StartedAt is when the run that wrote it opened, null when finish read
	// no record of it; Scheduled is finish --scheduled. An on-demand run of
	// the same audit writes the same stream, so these tie it to a scheduled
	// run that started no earlier than this one.
	StartedAt *string `json:"started_at"`
	Scheduled bool    `json:"scheduled"`
	Report    *struct {
		Schema string          `json:"schema"`
		Card   json.RawMessage `json:"card,omitempty"`
		Text   string          `json:"text"`
		Silent bool            `json:"silent,omitempty"`
	} `json:"report,omitempty"`
}

// errReportStoreMissing is the store's 404: no report for the stream.
var errReportStoreMissing = errors.New("no stored report")

// brokerReportStore reads through the credential broker, presenting the
// gateway's a2a-reports token, re-read per call (it rotates hourly).
type brokerReportStore struct {
	base      string
	tokenFile string
	client    *http.Client
}

// newBrokerReportStore is the reader for base and tokenFile, nil when
// either is unset (an install without the role reads nothing).
func newBrokerReportStore(base, tokenFile string) reportStoreReader {
	base, tokenFile = strings.TrimRight(strings.TrimSpace(base), "/"), strings.TrimSpace(tokenFile)
	if base == "" || tokenFile == "" {
		return nil
	}
	return brokerReportStore{base: base, tokenFile: tokenFile, client: &http.Client{
		Timeout: reportStoreTimeout,
		// A redirect is an answer, not followed: the token goes to the
		// broker's URL and nowhere else.
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

func (s brokerReportStore) latest(ctx context.Context, auditID string) (*reportEnvelope, error) {
	token, err := readReportStoreToken(s.tokenFile)
	if err != nil {
		return nil, err
	}
	u := s.base + "/v1/audit-reports/" + url.PathEscape(auditID) + "/" + reportStorePublishKey + "/latest"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/json")
	resp, err := s.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return nil, errReportStoreMissing
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("report store answered %d", resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, reportStoreBodyMax+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > reportStoreBodyMax {
		return nil, errors.New("report store envelope over its cap")
	}
	var env reportEnvelope
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, fmt.Errorf("report store envelope: %w", err)
	}
	return &env, nil
}

func readReportStoreToken(path string) (string, error) {
	f, err := os.Open(path) // #nosec G304 -- the operator-projected a2a-reports token (A2A_REPORT_STORE_TOKEN_FILE), not task data
	if err != nil {
		return "", err
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, reportStoreTokenMax))
	if err != nil {
		return "", err
	}
	token := string(bytes.TrimSpace(raw))
	if token == "" {
		return "", errors.New("empty a2a-reports token")
	}
	return token, nil
}

// storedReportFor is ref's run's report from the store, or nil to fall back
// to the trailing message. Only a builtin schedule's run reads, by its own
// stream, and only a scheduled run's envelope of that stream that started
// no earlier than this run is its report: latest may still be an earlier
// run's when this one's store write failed, and an on-demand run of the same
// audit writes the same stream.
func (g *Gateway) storedReportFor(ctx context.Context, ref TaskRef, logArgs []any) *storedReport {
	if g.reportStore == nil || ref.TriggerOrigin != lib.TriggerOriginBuiltin || ref.TriggerStream == "" {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, reportStoreTimeout)
	defer cancel()
	env, err := g.reportStore.latest(ctx, ref.TriggerStream)
	switch {
	case errors.Is(err, errReportStoreMissing):
		g.log.Info("scheduled report: none in the store; posting the run's result", logArgs...)
		return nil
	case err != nil:
		g.log.Warn("scheduled report: store read failed; posting the run's result", append(logArgs, "err", err)...)
		return nil
	}
	if why := g.envelopeMismatch(env, ref); why != "" {
		g.log.Info("scheduled report: stored report is not this run's; posting the run's result", append(logArgs, "why", why)...)
		return nil
	}
	out := &storedReport{Text: env.Report.Text, Silent: env.Report.Silent}
	if out.Silent && strings.TrimSpace(out.Text) == "" {
		out.Text = scheduledSilentMarker
	}
	if card := env.Report.Card; len(card) > 0 && !bytes.Equal(bytes.TrimSpace(card), []byte("null")) {
		if len(card) <= reportStoreCardMax {
			out.Card = card
		} else {
			g.log.Warn("scheduled report: stored card over its cap; posting the text", append(logArgs, "bytes", len(card))...)
		}
	}
	return out
}

// envelopeMismatch says why env is not ref's run's report, "" when it is.
func (g *Gateway) envelopeMismatch(env *reportEnvelope, ref TaskRef) string {
	if env.AuditID != ref.TriggerStream {
		return "another audit's envelope"
	}
	if env.Report == nil || env.Report.Schema != reportStoreSchema {
		return "no " + reportStoreSchema + " report"
	}
	if strings.TrimSpace(env.Report.Text) == "" && !env.Report.Silent {
		return "an empty report"
	}
	if !env.Scheduled {
		return "an on-demand run's envelope"
	}
	if ref.StartedAt.IsZero() {
		return "no recorded start for the run"
	}
	floor := ref.StartedAt.Add(-reportStoreSkew)
	if env.StartedAt == nil {
		return "no started_at"
	}
	started, err := time.Parse(time.RFC3339Nano, *env.StartedAt)
	if err != nil || started.Before(floor) {
		return "an earlier run's envelope"
	}
	finished, err := time.Parse(time.RFC3339Nano, env.FinishedAt)
	if err != nil {
		return "no finished_at"
	}
	if finished.Before(started) {
		return "a finished_at before its started_at"
	}
	if finished.After(g.scheduleClock().Add(reportStoreSkew)) {
		return "a finished_at in the future"
	}
	return ""
}

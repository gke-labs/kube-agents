package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The gateway's half of the incident route (lib.IncidentSubject): a posted
// scheduled report, kept against the thread it landed in by the bridge, the
// one process beside the agent holding session-kv's key. One request, one
// answer, on a reply subject in the gateway's own namespace, as the board
// route's (board.go). No credential beyond the gateway's bus grant.
const (
	// incidentKeepTimeout bounds one write, a quarter of the reserve the
	// writes after a post run in (scheduledKVReserve), so it cannot eat the
	// time the post's outcome is recorded in.
	incidentKeepTimeout = scheduledKVReserve / 4
	// incidentMaxReply bounds an answer: ok, or a short error.
	incidentMaxReply = 4 << 10
	// errIncidentNoBridge is what a request nothing answers means: the
	// agent pod's bridge is down, not yet started, or has no session-kv key.
	errIncidentNoBridge = "nothing answers the incident route; the agent pod's bridge is not running, or has no session-kv key and never bound the route"
)

// busIncidentWriter keeps reports over client's connection.
type busIncidentWriter struct{ client *lib.Client }

func (w busIncidentWriter) keepReportIncident(ctx context.Context, platform, chatID, threadID, report, source string) error {
	nc := w.client.Conn()
	if nc == nil {
		return errors.New("incident: no bus connection")
	}
	ctx, cancel := context.WithTimeout(ctx, incidentKeepTimeout)
	defer cancel()
	return keepIncidentOver(ctx, nc, lib.IncidentKeep{Platform: platform, ChatID: chatID, ThreadID: threadID, Report: report, Source: source})
}

// keepIncidentOver publishes one incident write and waits for the bridge's
// answer until ctx's deadline.
func keepIncidentOver(ctx context.Context, nc *nats.Conn, keep lib.IncidentKeep) error {
	body, err := json.Marshal(keep)
	if err != nil {
		return err
	}
	reply := lib.IncidentReplyPrefix + nuid.Next()
	in, err := nc.SubscribeSync(reply)
	if err != nil {
		return fmt.Errorf("incident: subscribe %s: %w", reply, err)
	}
	defer func() { _ = in.Unsubscribe() }()
	if err := nc.PublishRequest(lib.IncidentSubject, reply, body); err != nil {
		return fmt.Errorf("incident: publish: %w", err)
	}
	wait := incidentKeepTimeout
	if dl, ok := ctx.Deadline(); ok {
		wait = time.Until(dl)
	}
	msg, err := in.NextMsg(wait)
	switch {
	case errors.Is(err, nats.ErrNoResponders):
		return errors.New(errIncidentNoBridge)
	case errors.Is(err, nats.ErrTimeout):
		return fmt.Errorf("incident: no answer within %s", wait.Round(time.Millisecond))
	case err != nil:
		return fmt.Errorf("incident: %w", err)
	}
	if len(msg.Data) > incidentMaxReply {
		return fmt.Errorf("incident: answer is %d bytes; the limit is %d", len(msg.Data), incidentMaxReply)
	}
	var r lib.IncidentKeepResult
	if err := json.Unmarshal(msg.Data, &r); err != nil {
		return fmt.Errorf("incident: unreadable answer: %w", err)
	}
	if !r.OK {
		return fmt.Errorf("incident: the bridge did not keep it: %s", r.Error)
	}
	return nil
}

package lib

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/nats-io/nats.go"
)

// A stop for a profile task reaches the dispatcher as a stop record on its
// own subject, beside the cancel on the task's `…in`, which the dispatcher
// has no read on. The record is on a stream so a dispatcher that was down
// when the stop was published still finds it at admission
// (spec-profiles-dispatch.md, addendum 9 on #3655).
const (
	// DispatchStopStream holds the stop records on a2a.stop.>: limits
	// retention, one per subject, a 24h max-age (provisioned by the
	// operator).
	DispatchStopStream = "DISPATCH_STOPS"
	// DispatchStopPrefix starts the record's subject, a2a.stop.<profile>.<taskId>.
	// Its own root keeps it out of the dispatcher's a2a.dispatch.*.*.*
	// publish grant, so only the gateway can write a stop.
	DispatchStopPrefix = "a2a.stop."
	// dispatchStopVersion is the record's schema version.
	dispatchStopVersion = 1
)

// DispatchStop is the stop record: the task id and nothing else.
type DispatchStop struct {
	V      int    `json:"v"`
	TaskID string `json:"taskId"`
}

// DispatchStopSubject is where the stop record for a profile's task goes.
func DispatchStopSubject(profile, taskID string) string {
	return DispatchStopPrefix + profile + "." + taskID
}

// PublishDispatchStop publishes the stop record for a profile's task and
// waits for the stream's ack. The task id rides the subject and the body, and
// the dispatcher checks they agree.
func (c *Client) PublishDispatchStop(ctx context.Context, profile, taskID string) error {
	if !validDNS1123Label(profile) || !validDNS1123Label(taskID) {
		return &ProtocolError{Msg: fmt.Sprintf("dispatch stop for %q/%q: profile and taskId must be dot-free DNS-1123 labels", profile, taskID)}
	}
	body, err := json.Marshal(DispatchStop{V: dispatchStopVersion, TaskID: taskID})
	if err != nil {
		return err
	}
	_, js := c.conn()
	if js == nil {
		return fmt.Errorf("dispatch stop for %s: not connected", taskID)
	}
	msg := nats.NewMsg(DispatchStopSubject(profile, taskID))
	msg.Data = body
	if _, err := js.PublishMsg(ctx, msg); err != nil {
		return fmt.Errorf("dispatch stop for %s: %w", taskID, err)
	}
	return nil
}

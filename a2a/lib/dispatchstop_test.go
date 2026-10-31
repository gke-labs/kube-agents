package lib

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

func TestDispatchStopSubjectIsItsOwnRoot(t *testing.T) {
	if got, want := DispatchStopSubject("cluster", "task-0001"), "a2a.stop.cluster.task-0001"; got != want {
		t.Fatalf("subject = %q, want %q", got, want)
	}
}

// TestPublishDispatchStopWritesTheRecordTheDispatcherReads: the record lands
// on DISPATCH_STOPS under its task's subject, carries the task id and the
// version and nothing else, and a second stop for the task replaces it.
func TestPublishDispatchStopWritesTheRecordTheDispatcherReads(t *testing.T) {
	s := startServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	nc, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	stream, err := js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:              DispatchStopStream,
		Subjects:          []string{DispatchStopPrefix + ">"},
		Retention:         jetstream.LimitsPolicy,
		MaxMsgsPerSubject: 1,
		AllowDirect:       true,
	})
	if err != nil {
		t.Fatal(err)
	}
	c, err := Connect(ctx, s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()

	for range 2 {
		if err := c.PublishDispatchStop(ctx, "cluster", "task-0001"); err != nil {
			t.Fatalf("PublishDispatchStop: %v", err)
		}
	}
	msg, err := stream.GetLastMsgForSubject(ctx, DispatchStopSubject("cluster", "task-0001"))
	if err != nil {
		t.Fatalf("the record isn't on %s: %v", DispatchStopStream, err)
	}
	var fields map[string]any
	if err := json.Unmarshal(msg.Data, &fields); err != nil {
		t.Fatal(err)
	}
	if len(fields) != 2 || fields["v"] != float64(1) || fields["taskId"] != "task-0001" {
		t.Fatalf("record = %s, want exactly {\"v\":1,\"taskId\":\"task-0001\"}", msg.Data)
	}
	info, err := stream.Info(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if info.State.Msgs != 1 {
		t.Fatalf("the stream holds %d records for one task, want 1", info.State.Msgs)
	}
}

func TestPublishDispatchStopRefusesATokenThatWouldSplitTheSubject(t *testing.T) {
	c := &Client{}
	for _, tc := range []struct{ profile, taskID string }{{"clu.ster", "task-1"}, {"cluster", "task.1"}, {"", "task-1"}, {"cluster", ""}} {
		var pe *ProtocolError
		if err := c.PublishDispatchStop(context.Background(), tc.profile, tc.taskID); !errors.As(err, &pe) {
			t.Errorf("PublishDispatchStop(%q, %q) = %v, want a ProtocolError", tc.profile, tc.taskID, err)
		}
	}
}

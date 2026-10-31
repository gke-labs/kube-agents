// Package busmigrate converts an A2A bus between one NATS server and three
// without emptying it (#4225).
//
// A standalone server refuses an R3 stream's metadata on its store, and a
// server joining a cluster deletes the streams the new metadata layer does
// not know, so the conversion that only changes the server count empties the
// bus. Three to one keeps the data in place: while the bus is still a
// cluster, every stream is led by server 0 and scaled to R1, so the store
// server 0 keeps describes R1 streams it can load alone.
package busmigrate

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/nats-io/nats.go"
)

// The JetStream API subjects PinToServer uses, and the only ones the run's
// identity is granted for the three-to-one leg: info, leader step-down and
// update, each on the provisioned streams by name.
const (
	streamInfoSubject     = "$JS.API.STREAM.INFO.%s"
	streamStepDownSubject = "$JS.API.STREAM.LEADER.STEPDOWN.%s"
	streamUpdateSubject   = "$JS.API.STREAM.UPDATE.%s"
)

// stepDownAttempts bounds the leader moves per stream: nats-server 2.10 elects
// the new leader among the peers, so landing on one server is a matter of
// tries (two thirds of a step-down lands elsewhere).
const stepDownAttempts = 30

// apiTimeout is the wait for one JetStream API answer.
const apiTimeout = 5 * time.Second

// PinToServer prepares a three-server bus for its conversion to one server:
// every named stream is led by server and scaled to R1 through an update that
// sends the stream's full config with only the replica count changed, then
// read back as R1 on server alone. A stream already R1 on server is left as it
// is, so a run that failed part way can run again.
func PinToServer(ctx context.Context, nc *nats.Conn, server string, streams []string, settle time.Duration) error {
	for _, name := range streams {
		if err := pinStream(ctx, nc, server, name, settle); err != nil {
			return fmt.Errorf("stream %s: %w", name, err)
		}
	}
	return nil
}

func pinStream(ctx context.Context, nc *nats.Conn, server, name string, settle time.Duration) error {
	for attempt := 0; ; attempt++ {
		info, err := streamInfo(ctx, nc, name)
		if err != nil {
			return err
		}
		if pinned(info, server) {
			return nil
		}
		leader := ""
		if info.Cluster != nil {
			leader = info.Cluster.Leader
		}
		if leader == server {
			cfg := info.Config
			cfg.Replicas = 1
			if err := updateStream(ctx, nc, name, cfg); err != nil {
				return err
			}
		} else {
			if attempt >= stepDownAttempts {
				return fmt.Errorf("server %s did not come to lead it after %d step-downs (leader %q)", server, stepDownAttempts, leader)
			}
			if err := stepDown(ctx, nc, name); err != nil {
				return err
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(settle):
		}
	}
}

// pinned is a stream at R1 whose one peer is server.
func pinned(info *nats.StreamInfo, server string) bool {
	return info.Config.Replicas == 1 && info.Cluster != nil && info.Cluster.Leader == server && len(info.Cluster.Replicas) == 0
}

type apiError struct {
	Error *struct {
		Code        int    `json:"code"`
		ErrCode     int    `json:"err_code"`
		Description string `json:"description"`
	} `json:"error,omitempty"`
}

func request(ctx context.Context, nc *nats.Conn, subject string, body []byte, into any) error {
	rctx, cancel := context.WithTimeout(ctx, apiTimeout)
	defer cancel()
	msg, err := nc.RequestWithContext(rctx, subject, body)
	if err != nil {
		return err
	}
	var e apiError
	if err := json.Unmarshal(msg.Data, &e); err != nil {
		return err
	}
	if e.Error != nil {
		return fmt.Errorf("%s: %s (%d)", subject, e.Error.Description, e.Error.ErrCode)
	}
	if into != nil {
		return json.Unmarshal(msg.Data, into)
	}
	return nil
}

func streamInfo(ctx context.Context, nc *nats.Conn, name string) (*nats.StreamInfo, error) {
	var info nats.StreamInfo
	if err := request(ctx, nc, fmt.Sprintf(streamInfoSubject, name), nil, &info); err != nil {
		return nil, err
	}
	if info.Config.Name != name {
		return nil, errors.New("stream info names another stream")
	}
	return &info, nil
}

func stepDown(ctx context.Context, nc *nats.Conn, name string) error {
	return request(ctx, nc, fmt.Sprintf(streamStepDownSubject, name), nil, nil)
}

func updateStream(ctx context.Context, nc *nats.Conn, name string, cfg nats.StreamConfig) error {
	body, err := json.Marshal(cfg)
	if err != nil {
		return err
	}
	return request(ctx, nc, fmt.Sprintf(streamUpdateSubject, name), body, nil)
}

// UnpinFromServer undoes PinToServer for a three-to-one run that failed
// before the bus shrank: every named stream left at R1 goes back to replicas
// through an update that changes only the count, so the cluster does not keep
// running streams with one copy. A stream already at replicas is left as it
// is.
func UnpinFromServer(ctx context.Context, nc *nats.Conn, streams []string, replicas int) error {
	for _, name := range streams {
		info, err := streamInfo(ctx, nc, name)
		if err != nil {
			return fmt.Errorf("stream %s: %w", name, err)
		}
		if info.Config.Replicas == replicas {
			continue
		}
		cfg := info.Config
		cfg.Replicas = replicas
		if err := updateStream(ctx, nc, name, cfg); err != nil {
			return fmt.Errorf("stream %s: %w", name, err)
		}
	}
	return nil
}

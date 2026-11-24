package lib

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// fakeConsumer is a created consumer as the create's answer describes it.
type fakeConsumer struct {
	jetstream.Consumer
	info *jetstream.ConsumerInfo
}

func (c fakeConsumer) CachedInfo() *jetstream.ConsumerInfo { return c.info }

// placedCreator answers each create in turn from plan: "hang" blocks until the
// attempt's context ends, as a create placed on an offline peer does; "ok"
// answers with a consumer; "refuse" answers with the server's API error.
type placedCreator struct {
	mu    sync.Mutex
	plan  []string
	calls int
}

func (p *placedCreator) CreateConsumer(ctx context.Context, _ jetstream.ConsumerConfig) (jetstream.Consumer, error) {
	p.mu.Lock()
	step := p.plan[min(p.calls, len(p.plan)-1)]
	p.calls++
	p.mu.Unlock()
	switch step {
	case "ok":
		return fakeConsumer{info: &jetstream.ConsumerInfo{Name: "c1", Cluster: &jetstream.ClusterInfo{Leader: "nats-2"}}}, nil
	case "refuse":
		return nil, &jetstream.APIError{Code: 400, ErrorCode: jetstream.JSErrCodeConsumerCreate, Description: "maximum consumers limit reached"}
	case "closed":
		return nil, nats.ErrConnectionClosed
	default:
		<-ctx.Done()
		return nil, ctx.Err()
	}
}

// slots counts the slots taken and how each was released.
type slots struct {
	mu                 sync.Mutex
	taken, kept, freed int
}

func (s *slots) take() (func(bool), error) {
	s.mu.Lock()
	s.taken++
	s.mu.Unlock()
	var once sync.Once
	return func(created bool) {
		once.Do(func() {
			s.mu.Lock()
			defer s.mu.Unlock()
			if created {
				s.kept++
			} else {
				s.freed++
			}
		})
	}, nil
}

func fastOpts(s *slots) EphemeralCreateOptions {
	return EphemeralCreateOptions{AttemptTimeout: 50 * time.Millisecond, Stream: "TASKS", Slot: s.take}
}

func TestACreatePlacedOnADownServerIsRetriedWithinTheWait(t *testing.T) {
	p := &placedCreator{plan: []string{"hang", "ok"}}
	s := &slots{}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	start := time.Now()
	cons, release, err := CreateEphemeralConsumer(ctx, p, jetstream.ConsumerConfig{}, fastOpts(s))
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if took := time.Since(start); took > time.Second {
		t.Fatalf("the retry took %s, want well inside the caller's wait", took)
	}
	if cons.CachedInfo().Name != "c1" || p.calls != 2 {
		t.Fatalf("got consumer %q after %d calls, want c1 after 2", cons.CachedInfo().Name, p.calls)
	}
	// The timed-out attempt's slot stays held (its consumer may still be
	// made); the winner's is the caller's.
	if s.taken != 2 || s.kept != 1 || s.freed != 0 {
		t.Fatalf("slots taken/kept/freed = %d/%d/%d, want 2/1/0", s.taken, s.kept, s.freed)
	}
	release(true)
	if s.kept != 2 {
		t.Fatalf("the winner's release didn't reach its slot")
	}
}

func TestACreateThatAlwaysHangsFailsLoudlyAfterItsAttempts(t *testing.T) {
	p := &placedCreator{plan: []string{"hang"}}
	s := &slots{}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	start := time.Now()
	_, _, err := CreateEphemeralConsumer(ctx, p, jetstream.ConsumerConfig{}, fastOpts(s))
	if !errors.Is(err, ErrEphemeralCreateTimedOut) {
		t.Fatalf("err = %v, want ErrEphemeralCreateTimedOut", err)
	}
	if !strings.Contains(err.Error(), "TASKS") || !strings.Contains(err.Error(), "3 of 3") {
		t.Fatalf("err %q should name the stream and the attempts", err)
	}
	if took := time.Since(start); took > 2*time.Second {
		t.Fatalf("gave up after %s, want three bounded attempts, not the caller's whole wait", took)
	}
	if p.calls != EphemeralCreateMaxAttempts || s.taken != 3 || s.kept != 3 {
		t.Fatalf("calls %d, slots taken/kept %d/%d, want 3, 3/3", p.calls, s.taken, s.kept)
	}
	// Callers that map a deadline to "busy, send again" still can.
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("err %v should still wrap the attempt's deadline", err)
	}
}

// A caller whose whole deadline is about one default attempt (the MCP
// server's 10s reads, its 8s status read) still gets a retry: each attempt
// takes a share of what is left.
func TestAShortDeadlineStillGetsItsRetry(t *testing.T) {
	p := &placedCreator{plan: []string{"hang", "ok"}}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	cons, _, err := CreateEphemeralConsumer(ctx, p, jetstream.ConsumerConfig{}, EphemeralCreateOptions{Stream: "TASKS"})
	if err != nil {
		t.Fatalf("create under a 3s deadline with the default attempt timeout: %v", err)
	}
	if cons.CachedInfo().Name != "c1" || p.calls != 2 {
		t.Fatalf("got %q after %d calls, want c1 after 2", cons.CachedInfo().Name, p.calls)
	}
}

func TestAnAttemptsShareIsBoundedBothWays(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 9*time.Second)
	defer cancel()
	if got := attemptTimeout(ctx, 10*time.Second, 3); got > 3*time.Second || got < 2*time.Second {
		t.Errorf("share of 9s over 3 attempts = %s, want about 3s", got)
	}
	if got := attemptTimeout(ctx, 500*time.Millisecond, 3); got != 500*time.Millisecond {
		t.Errorf("a limit under the share = %s, want the limit", got)
	}
	short, cancelShort := context.WithTimeout(context.Background(), 1500*time.Millisecond)
	defer cancelShort()
	if got := attemptTimeout(short, 10*time.Second, 3); got < 900*time.Millisecond {
		t.Errorf("a 1.5s deadline over 3 attempts gave %s, want the 1s floor", got)
	}
	if got := attemptTimeout(context.Background(), 10*time.Second, 3); got != 10*time.Second {
		t.Errorf("no deadline gave %s, want the limit", got)
	}
}

func TestAClosedConnectionIsNotRetried(t *testing.T) {
	p := &placedCreator{plan: []string{"closed", "ok"}}
	s := &slots{}
	_, _, err := CreateEphemeralConsumer(context.Background(), p, jetstream.ConsumerConfig{}, fastOpts(s))
	if !errors.Is(err, nats.ErrConnectionClosed) {
		t.Fatalf("err = %v, want the closed connection", err)
	}
	// It may have been answered after the connection went, so its slot stays.
	if p.calls != 1 || s.kept != 1 || s.freed != 0 {
		t.Fatalf("calls %d, slots kept/freed %d/%d, want 1, 1/0", p.calls, s.kept, s.freed)
	}
}

func TestNoSlotForTheRetrySaysATimeoutCameFirst(t *testing.T) {
	p := &placedCreator{plan: []string{"hang", "ok"}}
	busy := errors.New("every reader slot is busy")
	n := 0
	o := EphemeralCreateOptions{AttemptTimeout: 50 * time.Millisecond, Stream: "TASKS", Slot: func() (func(bool), error) {
		n++
		if n > 1 {
			return nil, busy
		}
		return func(bool) {}, nil
	}}
	_, _, err := CreateEphemeralConsumer(context.Background(), p, jetstream.ConsumerConfig{}, o)
	if !errors.Is(err, busy) || !strings.Contains(err.Error(), "1 timed-out attempt") {
		t.Fatalf("err = %v, want the slot's error after a timed-out attempt", err)
	}
}

func TestACreateHangingPastTheCallersDeadlineEndsAtTheDeadline(t *testing.T) {
	p := &placedCreator{plan: []string{"hang"}}
	ctx, cancel := context.WithTimeout(context.Background(), 80*time.Millisecond)
	defer cancel()
	o := EphemeralCreateOptions{AttemptTimeout: time.Minute, Stream: "TASKS"}
	start := time.Now()
	_, _, err := CreateEphemeralConsumer(ctx, p, jetstream.ConsumerConfig{}, o)
	if !errors.Is(err, context.DeadlineExceeded) || !strings.Contains(err.Error(), "TASKS") || !strings.Contains(err.Error(), "1 attempt(s)") {
		t.Fatalf("err = %v, want the deadline, naming the stream and counting the attempt it cut short", err)
	}
	if took := time.Since(start); took > time.Second {
		t.Fatalf("returned after %s, want the caller's deadline", took)
	}
}

func TestARefusedCreateIsNotRetried(t *testing.T) {
	p := &placedCreator{plan: []string{"refuse", "ok"}}
	s := &slots{}
	_, _, err := CreateEphemeralConsumer(context.Background(), p, jetstream.ConsumerConfig{}, fastOpts(s))
	var refused *jetstream.APIError
	if !errors.As(err, &refused) {
		t.Fatalf("err = %v, want the server's refusal", err)
	}
	if p.calls != 1 || s.freed != 1 || s.kept != 0 {
		t.Fatalf("calls %d, slots freed/kept %d/%d, want 1, 1/0", p.calls, s.freed, s.kept)
	}
}

func TestASlotErrorEndsTheCreate(t *testing.T) {
	p := &placedCreator{plan: []string{"ok"}}
	busy := errors.New("every reader slot is busy")
	o := EphemeralCreateOptions{Slot: func() (func(bool), error) { return nil, busy }}
	if _, _, err := CreateEphemeralConsumer(context.Background(), p, jetstream.ConsumerConfig{}, o); !errors.Is(err, busy) {
		t.Fatalf("err = %v, want the slot's error", err)
	}
	if p.calls != 0 {
		t.Fatalf("created %d times without a slot", p.calls)
	}
}

package gateway

import (
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// A standby's doors (docs/designs/gateway-replicas.md, "Reaching the active
// replica over HTTP"). The inject and A2A doors bind the pod's loopback and
// are reached by port-forward, which can land on the standby. While a
// replica stands by, its door addresses answer every request with 503,
// naming the replica that holds the lease and the port-forward that reaches
// it.
const (
	// StandbyHolderHeader carries the lease holder's pod name on a
	// standby's 503, so a client can re-target without parsing the body.
	StandbyHolderHeader = "X-A2A-Gateway-Holder"
	// standbyReadHeaderTimeout bounds a request's headers, as the doors'
	// own servers do.
	standbyReadHeaderTimeout = 10 * time.Second
)

// standbyRetryAfter is the 503's Retry-After, in seconds: the election's
// retry period, about how long a released lease takes to be taken.
var standbyRetryAfter = strconv.Itoa(int(electionRetryPeriod / time.Second))

// LeaseView is the lease holder the election last read: every standby
// retry reads the Lease, so it is at most a retry period old.
type LeaseView struct {
	holder atomic.Pointer[string]
}

func (v *LeaseView) set(holder string) {
	v.holder.Store(&holder)
}

// Holder is the holder's pod name, or "" when no replica holds the lease or
// it hasn't been read yet.
func (v *LeaseView) Holder() string {
	h, _ := v.read()
	return h
}

// read is the holder, and whether the lease has been read at all.
func (v *LeaseView) read() (string, bool) {
	if h := v.holder.Load(); h != nil {
		return *h, true
	}
	return "", false
}

// StandbyDoors serve the 503 on the doors' addresses until Stop.
type StandbyDoors struct {
	servers  []*http.Server
	served   sync.WaitGroup
	stopOnce sync.Once
}

// standbyAnswer is the 503's body: the door's own error shape (injectError),
// with the holder and the command beside it.
type standbyAnswer struct {
	Error   string `json:"error"`
	Holder  string `json:"holder,omitempty"`
	Command string `json:"command,omitempty"`
}

// StartStandbyDoors listens on each of listens (the armed doors' addresses)
// and answers there until Stop. namespace goes into the port-forward
// command.
func StartStandbyDoors(listens []string, namespace string, view *LeaseView, log *slog.Logger) (*StandbyDoors, error) {
	s := &StandbyDoors{}
	for _, listen := range listens {
		_, port, err := net.SplitHostPort(listen)
		if err != nil {
			s.Stop()
			return nil, fmt.Errorf("standby door %s: %w", listen, err)
		}
		ln, err := net.Listen("tcp", listen)
		if err != nil {
			s.Stop()
			return nil, fmt.Errorf("standby door listen on %s: %w", listen, err)
		}
		srv := &http.Server{
			Handler:           standbyHandler(namespace, port, view),
			ReadHeaderTimeout: standbyReadHeaderTimeout,
		}
		s.servers = append(s.servers, srv)
		s.served.Add(1)
		go func() {
			defer s.served.Done()
			if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Error("standby door stopped", "listen", listen, "err", err)
			}
		}()
		log.Info("standby door answering 503 until this replica leads", "listen", listen)
	}
	return s, nil
}

// Stop closes the standby's listeners and returns once they are closed, so
// the doors can bind the same addresses. Safe to call more than once.
func (s *StandbyDoors) Stop() {
	s.stopOnce.Do(func() {
		for _, srv := range s.servers {
			_ = srv.Close()
		}
		s.served.Wait()
	})
}

func standbyHandler(namespace, port string, view *LeaseView) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Retry-After", standbyRetryAfter)
		holder, seen := view.read()
		if !seen {
			writeJSON(w, http.StatusServiceUnavailable, standbyAnswer{
				Error: "this gateway replica is the standby and hasn't read the lease yet; retry in a few seconds",
			})
			return
		}
		if holder == "" {
			writeJSON(w, http.StatusServiceUnavailable, standbyAnswer{
				Error: "this gateway replica is the standby, and no replica holds the lease right now; retry in a few seconds",
			})
			return
		}
		command := fmt.Sprintf("kubectl -n %s port-forward pod/%s %s", namespace, holder, port)
		w.Header().Set(StandbyHolderHeader, holder)
		writeJSON(w, http.StatusServiceUnavailable, standbyAnswer{
			Error:   fmt.Sprintf("this gateway replica is the standby; the active one is %s: %s", holder, command),
			Holder:  holder,
			Command: command,
		})
	})
}

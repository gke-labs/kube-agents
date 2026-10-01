package gateway

import (
	"sync"
	"testing"
	"time"
)

// startBlockedQueue returns a queue whose worker holds each batch until it
// receives on release (or release is closed), with key "k"'s first item
// already taken by the worker, so every later enqueue lands in the pending
// list deterministically. taken signals each batch the worker picks up.
func startBlockedQueue(t *testing.T) (q *keyedQueue[int], release, taken chan struct{}) {
	t.Helper()
	release = make(chan struct{})
	taken = make(chan struct{}, 16)
	q = newKeyedQueue(func(_ string, _ []int) {
		taken <- struct{}{}
		<-release
	})
	var once sync.Once
	t.Cleanup(func() { once.Do(func() { close(release) }) })
	if ok, _ := q.enqueueBounded("k", 0, 1); !ok {
		t.Fatal("the first item was refused")
	}
	waitTaken(t, taken)
	return q, release, taken
}

func waitTaken(t *testing.T, taken chan struct{}) {
	t.Helper()
	select {
	case <-taken:
	case <-time.After(5 * time.Second):
		t.Fatal("the worker never took the batch")
	}
}

// The limit counts waiting items, not the batch in flight, and a refusal
// reports first only once until the key has room again.
func TestEnqueueBoundedRefusesPastTheLimitAndReportsTheFirstRefusalOnce(t *testing.T) {
	q, _, _ := startBlockedQueue(t)
	const limit = 3
	for i := 1; i <= limit; i++ {
		if ok, _ := q.enqueueBounded("k", i, limit); !ok {
			t.Fatalf("item %d of %d was refused", i, limit)
		}
	}
	if ok, first := q.enqueueBounded("k", 99, limit); ok || !first {
		t.Fatalf("over the limit: accepted=%v first=%v, want false true", ok, first)
	}
	if ok, first := q.enqueueBounded("k", 100, limit); ok || first {
		t.Fatalf("second refusal: accepted=%v first=%v, want false false", ok, first)
	}
	// Another key is not affected by k being full.
	if ok, _ := q.enqueueBounded("other", 1, limit); !ok {
		t.Fatal("a different key was refused")
	}
}

// Once the worker drains the key, the refusal memory goes with it: the next
// fill reports first again, and nothing is left behind for an idle key.
func TestEnqueueBoundedForgetsARefusalWhenTheKeyDrains(t *testing.T) {
	q, release, _ := startBlockedQueue(t)
	if ok, _ := q.enqueueBounded("k", 1, 1); !ok {
		t.Fatal("the waiting item was refused")
	}
	if _, first := q.enqueueBounded("k", 2, 1); !first {
		t.Fatal("the first refusal was not reported as first")
	}
	release <- struct{}{} // batch [0]
	release <- struct{}{} // batch [1]; the key is empty after it
	deadline := time.Now().Add(5 * time.Second)
	for {
		q.mu.Lock()
		_, live := q.m["k"]
		refusing := len(q.refusing)
		q.mu.Unlock()
		if !live {
			if refusing != 0 {
				t.Fatalf("refusal memory outlived the key: %d entries", refusing)
			}
			return
		}
		if time.Now().After(deadline) {
			t.Fatal("the key never drained")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// A key that empties its waiting list without draining (the worker took the
// waiting batch into flight) has room again, so its next fill reports first
// again rather than staying silent until the key goes idle.
func TestEnqueueBoundedReportsARefillBeforeTheKeyDrains(t *testing.T) {
	q, release, taken := startBlockedQueue(t)
	if ok, _ := q.enqueueBounded("k", 1, 1); !ok {
		t.Fatal("the waiting item was refused")
	}
	if _, first := q.enqueueBounded("k", 2, 1); !first {
		t.Fatal("the first refusal was not reported as first")
	}
	release <- struct{}{} // finish batch [0]; the worker takes [1] and holds it
	waitTaken(t, taken)
	if ok, _ := q.enqueueBounded("k", 3, 1); !ok {
		t.Fatal("the key had room after the worker took its waiting batch, but refused")
	}
	if ok, first := q.enqueueBounded("k", 4, 1); ok || !first {
		t.Fatalf("refill refusal: accepted=%v first=%v, want false true", ok, first)
	}
}

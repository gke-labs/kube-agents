package gateway

import "sync"

// keyedQueue runs work per key in submission order, one batch at a time,
// with no cross-key blocking: a slow or rate-limited conversation stalls
// only itself, never the fleet. Batching lets the relay coalesce
// rolling-line renders when a session falls behind.
type keyedQueue[T any] struct {
	mu   sync.Mutex
	m    map[string][]T
	work func(key string, batch []T)
	// refusing marks the keys enqueueBounded has refused since they last
	// had room, so a caller can report a full queue once per fill rather
	// than once per refused item. It is cleared when the key accepts again
	// or drains, so it never outlives the key's own queue.
	refusing map[string]bool
}

func newKeyedQueue[T any](work func(key string, batch []T)) *keyedQueue[T] {
	return &keyedQueue[T]{m: map[string][]T{}, work: work}
}

// enqueue appends an item to the key's queue and starts a worker for the
// key if none is running. Workers exit when their key drains, so idle keys
// cost nothing.
func (q *keyedQueue[T]) enqueue(key string, item T) {
	q.mu.Lock()
	pending, running := q.m[key]
	q.m[key] = append(pending, item)
	q.mu.Unlock()
	if !running {
		go q.run(key)
	}
}

// enqueueBounded is enqueue that refuses the item when the key already has
// limit items waiting, not counting the batch a worker is running. It is
// for an ingress whose rate the sender sets: enqueue never blocks, so
// nothing upstream of it pushes back. first is true on the first refusal
// since the key last had room.
func (q *keyedQueue[T]) enqueueBounded(key string, item T, limit int) (accepted, first bool) {
	q.mu.Lock()
	pending, running := q.m[key]
	if len(pending) >= limit {
		if q.refusing == nil {
			q.refusing = map[string]bool{}
		}
		first = !q.refusing[key]
		q.refusing[key] = true
		q.mu.Unlock()
		return false, first
	}
	delete(q.refusing, key)
	q.m[key] = append(pending, item)
	q.mu.Unlock()
	if !running {
		go q.run(key)
	}
	return true, false
}

func (q *keyedQueue[T]) run(key string) {
	for {
		q.mu.Lock()
		batch := q.m[key]
		if len(batch) == 0 {
			delete(q.m, key)
			delete(q.refusing, key)
			q.mu.Unlock()
			return
		}
		q.m[key] = []T{} // present-but-empty marks the worker as running
		q.mu.Unlock()
		q.work(key, batch)
	}
}

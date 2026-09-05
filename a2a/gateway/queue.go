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

func (q *keyedQueue[T]) run(key string) {
	for {
		q.mu.Lock()
		batch := q.m[key]
		if len(batch) == 0 {
			delete(q.m, key)
			q.mu.Unlock()
			return
		}
		q.m[key] = []T{} // present-but-empty marks the worker as running
		q.mu.Unlock()
		q.work(key, batch)
	}
}

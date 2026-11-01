// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"context"
	"sync"

	"k8s.io/apimachinery/pkg/watch"
)

// listGate bounds how many clusters may be receiving their initial Event
// state at once. Every reflector in the process starts at the same instant,
// and each one's first request brings the cluster's whole Event backlog into
// this process at once: as the body of a List, or as the initial events of a
// watch-list stream, which the reflector buffers in full until the bookmark
// that ends them. A thousand-node cluster's backlog is tens of thousands of
// Events, so a fleet of them starting together is the fleet's entire Event
// history in flight in one heap, which is the burst that killed the container
// before this gate existed. Steady state was already bounded (the delivery
// queue, the dedup cache); this bounds the start.
//
// One gate per process, shared by every watcher, so the bound is on the heap
// and not per cluster. A slot is held from the moment the request is made
// until the backlog has been received: for a List, until the call returns;
// for a watch-list stream, until the bookmark ending its initial events
// arrives, or the stream fails or is stopped first (see gatedWatch). The
// reflector's later watches, which carry only live events, are not gated.
// Startup, the relist after a 410 and the recovery after a hold all go
// through the same two calls, so all of them queue here.
//
// Waiting costs the queued clusters time, not correctness: a reflector that
// waits for a slot has not listed yet, which is the same state as a reflector
// whose list is slow, and WaitForCacheSync has no timeout. The one clock it
// touches is initialSyncGrace in main.go, which needs one cluster synced, and
// the first slots are granted at once.
//
// The nil gate admits everything, which is how a watcher built without one
// (every test that constructs newWatcher directly) and --initial-list-concurrency=0
// behave.
type listGate struct {
	slots chan struct{}
}

// newListGate returns a gate of n slots, or nil for n <= 0 (ungated).
func newListGate(n int) *listGate {
	if n <= 0 {
		return nil
	}
	return &listGate{slots: make(chan struct{}, n)}
}

// acquire takes a slot, waiting for one or for ctx to end. It returns the
// release for the slot, which is idempotent, so the paths that may each be
// the last to see a stream (the bookmark, the channel closing, Stop) can all
// call it. On a cancelled ctx it returns ctx's error and nothing is held.
func (g *listGate) acquire(ctx context.Context) (func(), error) {
	if g == nil {
		return func() {}, nil
	}
	select {
	case g.slots <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	var once sync.Once
	return func() { once.Do(func() { <-g.slots }) }, nil
}

// inFlight is how many slots are held, for tests and the startup line.
func (g *listGate) inFlight() int {
	if g == nil {
		return 0
	}
	return len(g.slots)
}

// size is the gate's slot count, 0 for the nil gate.
func (g *listGate) size() int {
	if g == nil {
		return 0
	}
	return cap(g.slots)
}

// gatedWatch holds a slot across a watch-list stream's initial events: it
// forwards the stream unchanged through one observeWatch layer, calling
// observe on every event (the watcher's mark recorder) and release at the
// bookmark that ends the initial events, when the upstream channel closes,
// or when the reflector stops the watch, whichever is first. One layer, not
// an observer stacked on the mark recorder's: the stream carries live events
// for minutes after the bookmark, and every one would pay the second hop.
func gatedWatch(wi watch.Interface, observe func(watch.Event), release func()) watch.Interface {
	ow := observeWatch(wi, func(in watch.Event) {
		observe(in)
		if in.Type == watch.Bookmark && isInitialEventsEnd(in.Object) {
			release()
		}
	})
	go func() {
		// A stream that ends before its bookmark (an error mid-stream, a
		// 410, a stop) releases here; ended closes without draining result.
		<-ow.ended
		release()
	}()
	return &gatedWatchInterface{observedWatch: ow, release: release}
}

// gatedWatchInterface releases the slot on Stop as well, which is how the
// reflector ends a stream it gives up on before the bookmark.
type gatedWatchInterface struct {
	*observedWatch
	release func()
}

func (gw *gatedWatchInterface) Stop() {
	gw.observedWatch.Stop()
	gw.release()
}

package dispatch

import (
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Ready is the pod's readiness: false until Run holds its subscriptions and
// every listed durable is bound, false again while a durable the config
// added isn't there yet, naming it, and true once it binds.
func TestReadyWaitsForEveryListedDurable(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	ti := oneProfile("tse", 2, "tse-0")
	d, err := New(fastRegrant(), b.connect("dispatcher"), ti.src, ti.pool, &fakeVerifier{}, quietLog())
	if err != nil {
		t.Fatal(err)
	}
	if ok, why := d.Ready(); ok || why == "" {
		t.Fatalf("ready before Run: %v %q", ok, why)
	}
	d, stop := b.runDispatcher(fastRegrant(), ti, &fakeVerifier{})
	waitFor(t, 15*time.Second, "ready with tse bound", func() bool { ok, _ := d.Ready(); return ok })

	ti.update(func(c *lib.DispatchConfig) { c.Version = "v2"; c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	missing := lib.ProfileDurableName("tse2", "read", lib.SubmitKindTop)
	waitFor(t, 10*time.Second, "unready naming the missing durable", func() bool {
		ok, why := d.Ready()
		return !ok && strings.Contains(why, missing)
	})
	b.ensure("tse2", 2, 0, "read")
	waitFor(t, 30*time.Second, "ready once tse2's durables exist", func() bool { ok, _ := d.Ready(); return ok })

	stop()
	if ok, _ := d.Ready(); ok {
		t.Error("ready after Run returned")
	}
}

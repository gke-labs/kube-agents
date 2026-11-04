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
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/kubernetes/fake"
	"k8s.io/client-go/metadata"
	metadatafake "k8s.io/client-go/metadata/fake"
	k8stesting "k8s.io/client-go/testing"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/testing/fakeapiserver"
)

// quietLog keeps the phases' progress lines out of the test output.
func quietLog(t *testing.T) {
	t.Helper()
	log.SetOutput(io.Discard)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })
}

// partialObject is a core/v1 object as the metadata client lists it.
func partialObject(kind, namespace, name string) *metav1.PartialObjectMetadata {
	return &metav1.PartialObjectMetadata{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: kind},
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name},
	}
}

// newMetadataClient is a fake metadata client holding objects. Its lists
// ignore the field selector and never paginate, so a test plants only what
// the selector would match, and paging is checked on countPaged directly.
func newMetadataClient(t *testing.T, objects ...runtime.Object) *metadatafake.FakeMetadataClient {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := metav1.AddMetaToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	return metadatafake.NewSimpleMetadataClient(scheme, objects...)
}

// defaultServiceAccounts is the `default` ServiceAccount of every namespace
// of lay, as kube-controller-manager would have created them.
func defaultServiceAccounts(lay layout) []runtime.Object {
	var objs []runtime.Object
	for _, ns := range lay.namespaceNames() {
		objs = append(objs, partialObject("ServiceAccount", ns, defaultServiceAccountName))
	}
	return objs
}

// runningPods is n Pods as a count of the Running ones would list them.
func runningPods(n int) []runtime.Object {
	objs := make([]runtime.Object, 0, n)
	for i := range n {
		objs = append(objs, partialObject("Pod", "team-000", fmt.Sprintf("pod-%d", i)))
	}
	return objs
}

// newClientset is the typed fake the seed tests create against. The
// field-managed one (fake.NewClientset) spends about a second per run
// computing managed fields for the generator's full Pods, which nothing here
// reads; the plain tracker answers AlreadyExists for a duplicate name just
// the same, and that is the behaviour under test.
func newClientset(objects ...runtime.Object) *fake.Clientset {
	return fake.NewSimpleClientset(objects...)
}

func checkPhase(t *testing.T, name string, got phaseResult, created, existed, failed int64) {
	t.Helper()
	if got.Created != created || got.Existed != existed || got.Failed != failed {
		t.Errorf("%s: created %d, existed %d, failed %d; want %d, %d, %d", name, got.Created, got.Existed, got.Failed, created, existed, failed)
	}
}

func TestRunPhaseCountsCreatedAndExisted(t *testing.T) {
	quietLog(t)
	errs := &firstError{}
	gr := schema.GroupResource{Resource: podsResource}
	r := runPhase(context.Background(), phasePods, 10, 4, errs, func(_ context.Context, i int) error {
		// Indexes 0, 3, 6 and 9 are what an earlier run left.
		if i%3 == 0 {
			return apierrors.NewAlreadyExists(gr, fmt.Sprint(i))
		}
		return nil
	})
	checkPhase(t, phasePods, r, 6, 4, 0)
	if r.FirstError != "" {
		t.Errorf("first_error = %q, want none: an existing object is not a failure", r.FirstError)
	}
	if err := errs.get(); err != nil {
		t.Errorf("run-wide first error = %v, want none", err)
	}
	if r.Seconds <= 0 || r.PerSecond <= 0 {
		t.Fatalf("seconds = %v, per_second = %v, want both positive", r.Seconds, r.PerSecond)
	}
	// per_second counts objects in place, created or existed, over the
	// phase's wall time.
	if want := float64(r.Created+r.Existed) / r.Seconds; math.Abs(r.PerSecond-want) > want*1e-9 {
		t.Errorf("per_second = %v, want %v", r.PerSecond, want)
	}
}

func TestRunPhaseRecordsTheFirstFailure(t *testing.T) {
	quietLog(t)
	errs := &firstError{}
	boom := errors.New("boom")
	// One worker, so the indexes run in order and the first failure is
	// index 2 rather than whichever worker lost the race.
	first := runPhase(context.Background(), phaseNodes, 5, 1, errs, func(_ context.Context, i int) error {
		if i >= 2 {
			return boom
		}
		return nil
	})
	checkPhase(t, phaseNodes, first, 2, 0, 3)
	if first.FirstError != "nodes 2: boom" {
		t.Errorf("phase first_error = %q, want %q", first.FirstError, "nodes 2: boom")
	}
	if err := errs.get(); err == nil || err.Error() != "nodes 2: boom" || !errors.Is(err, boom) {
		t.Errorf("run-wide first error = %v, want nodes 2: boom wrapping the cause", err)
	}

	// A later phase keeps its own first failure, with nothing in place so
	// per_second stays zero, while the run-wide first error stays the
	// earliest phase's.
	second := runPhase(context.Background(), phasePods, 3, 1, errs, func(context.Context, int) error {
		return errors.New("denied")
	})
	checkPhase(t, phasePods, second, 0, 0, 3)
	if second.FirstError != "pods 0: denied" {
		t.Errorf("second phase first_error = %q, want %q", second.FirstError, "pods 0: denied")
	}
	if second.PerSecond != 0 {
		t.Errorf("second phase per_second = %v, want 0 with nothing in place", second.PerSecond)
	}
	if err := errs.get(); err == nil || err.Error() != "nodes 2: boom" {
		t.Errorf("run-wide first error = %v, want the first phase's", err)
	}
}

func TestRunPhaseStopsFeedingOnInterrupt(t *testing.T) {
	quietLog(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	// Far more indexes than the workers could get through before the test
	// gives up, so a feed that ignores the interrupt shows as a count.
	const count = 1 << 20
	var calls atomic.Int64
	done := make(chan phaseResult, 1)
	go func() {
		done <- runPhase(ctx, phasePods, count, 2, &firstError{}, func(ctx context.Context, i int) error {
			// The first create interrupts the run; any create fed after
			// that fails as a real one would once its context is gone.
			if calls.Add(1) == 1 {
				cancel()
				return nil
			}
			return ctx.Err()
		})
	}()
	select {
	case r := <-done:
		if processed := r.Created + r.Existed + r.Failed; processed >= count {
			t.Fatalf("all %d indexes were fed after the interrupt", count)
		}
		if r.Created < 1 {
			t.Errorf("created = %d, want the interrupting create counted", r.Created)
		}
		if r.Failed > 0 && !strings.Contains(r.FirstError, context.Canceled.Error()) {
			t.Errorf("first_error = %q, want the cancelled context", r.FirstError)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("runPhase did not return after its context was cancelled")
	}
}

// seedProfile is small enough to run in milliseconds against the fakes and
// large enough that every phase has more than one object.
var seedProfile = profile{Nodes: 2, Pods: 6, Namespaces: 3, Services: 2, Events: 4}

// seedFlags are the flags the seed tests run with. The Running wait is off
// unless a test turns it on: the fake metadata client lists what the test
// planted, not what kwok marked Running, so the wait would measure the test
// rather than the run. waitRunning has its own tests.
func seedFlags() flags {
	return flags{workers: 4, namespacePrefix: "team", waitTimeout: time.Minute}
}

func seedLayout(f flags, prof profile) layout {
	return layout{prefix: f.namespacePrefix, namespaces: prof.Namespaces, pods: prof.Pods}
}

// TestSeedCountsExistingObjectsAsExisted pins the documented second run: a
// cluster already holding some of the objects (the fake clientset answers
// AlreadyExists for a duplicate name, as the API server does) ends with
// those counted as existed, the rest created, nothing failed and no error.
// The Events phase runs; the Running wait is off, see seedFlags.
func TestSeedCountsExistingObjectsAsExisted(t *testing.T) {
	quietLog(t)
	ctx := context.Background()
	f := seedFlags()
	prof := seedProfile
	gen := fakeapiserver.NewGenerator(f.seed, fakeapiserver.Sizes{Nodes: prof.Nodes, Pods: prof.Pods, Events: prof.Events})
	lay := seedLayout(f, prof)

	// What an earlier run left: 2 of 3 namespaces, 1 of 2 nodes, 3 of 6
	// pods, 1 of 2 services and 2 of 4 events, prepared as that run did.
	var existing []runtime.Object
	for _, ns := range lay.namespaceNames()[:2] {
		existing = append(existing, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}})
	}
	node := gen.Node(0)
	prepareNode(node)
	existing = append(existing, node)
	for i := range 3 {
		pod := gen.Pod(i)
		lay.preparePod(pod, i)
		existing = append(existing, pod)
	}
	existing = append(existing, lay.serviceFor(0, gen.Pod(0).Labels[appLabel]))
	for i := range 2 {
		ev := gen.Event(i)
		lay.prepareEvent(ev, i)
		existing = append(existing, ev)
	}
	client := newClientset(existing...)
	metaClient := newMetadataClient(t, defaultServiceAccounts(lay)...)

	s, err := seed(ctx, client, metaClient, f, prof)
	if err != nil {
		t.Fatalf("seed: %v", err)
	}
	checkPhase(t, phaseNamespaces, s.Phases.Namespaces, 1, 2, 0)
	checkPhase(t, phaseNodes, s.Phases.Nodes, 1, 1, 0)
	checkPhase(t, phasePods, s.Phases.Pods, 3, 3, 0)
	checkPhase(t, phaseServices, s.Phases.Services, 1, 1, 0)
	checkPhase(t, phaseEvents, s.Phases.Events, 2, 2, 0)
	if s.FirstError != "" || s.failed() != 0 || s.PodsRunningAfterSeconds != 0 {
		t.Errorf("first_error %q, failed %d, pods_running_after_seconds %v; want none", s.FirstError, s.failed(), s.PodsRunningAfterSeconds)
	}
	if s.Profile != prof || s.Seed != f.seed {
		t.Errorf("summary carries profile %+v seed %d, want %+v %d", s.Profile, s.Seed, prof, f.seed)
	}
	pods, err := client.CoreV1().Pods(metav1.NamespaceAll).List(ctx, metav1.ListOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(pods.Items) != prof.Pods {
		t.Errorf("cluster holds %d pods after the run, want %d", len(pods.Items), prof.Pods)
	}

	// A run over the now complete cluster finds everything in place.
	again, err := seed(ctx, client, metaClient, f, prof)
	if err != nil {
		t.Fatalf("second seed: %v", err)
	}
	for _, row := range again.rows() {
		checkPhase(t, row.name, row.result, 0, int64(prof.counts()[row.name]), 0)
	}
	if again.FirstError != "" {
		t.Errorf("second run first_error = %q, want none", again.FirstError)
	}
}

func TestSeedReportsFailedRequestsBeforeTheWait(t *testing.T) {
	quietLog(t)
	f := seedFlags()
	// One worker, so the phase's first failure is its lowest index. The
	// wait is on with a timeout the first count already exceeds, so it
	// fails too and has to lose to the request failures.
	f.workers = 1
	f.waitRunning = true
	f.waitTimeout = time.Nanosecond
	prof := seedProfile
	client := newClientset()
	client.PrependReactor("create", "nodes", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, errors.New("boom")
	})
	metaClient := newMetadataClient(t, defaultServiceAccounts(seedLayout(f, prof))...)

	s, err := seed(context.Background(), client, metaClient, f, prof)
	if want := "2 requests failed; first: nodes 0: boom"; err == nil || err.Error() != want {
		t.Fatalf("err = %v, want %q", err, want)
	}
	checkPhase(t, phaseNodes, s.Phases.Nodes, 0, 0, 2)
	if s.Phases.Nodes.FirstError != "nodes 0: boom" || s.FirstError != "nodes 0: boom" {
		t.Errorf("phase first_error %q, run first_error %q; want nodes 0: boom for both", s.Phases.Nodes.FirstError, s.FirstError)
	}
	// The other phases ran regardless.
	checkPhase(t, phaseNamespaces, s.Phases.Namespaces, 3, 0, 0)
	checkPhase(t, phasePods, s.Phases.Pods, 6, 0, 0)
	checkPhase(t, phaseServices, s.Phases.Services, 2, 0, 0)
	checkPhase(t, phaseEvents, s.Phases.Events, 4, 0, 0)
}

func TestSeedReturnsTheWaitErrorWhenNothingFailed(t *testing.T) {
	quietLog(t)
	f := seedFlags()
	f.waitRunning = true
	f.waitTimeout = time.Nanosecond
	prof := seedProfile
	client := newClientset()
	// No Running pods planted, so the wait times out.
	metaClient := newMetadataClient(t, defaultServiceAccounts(seedLayout(f, prof))...)

	s, err := seed(context.Background(), client, metaClient, f, prof)
	want := "0 of 6 pods Running after 1ns"
	if err == nil || err.Error() != want {
		t.Fatalf("err = %v, want %q", err, want)
	}
	if s.FirstError != want {
		t.Errorf("first_error = %q, want %q", s.FirstError, want)
	}
	if s.failed() != 0 {
		t.Errorf("failed = %d, want 0", s.failed())
	}
	if s.PodsRunningAfterSeconds <= 0 {
		t.Errorf("pods_running_after_seconds = %v, want the wait's wall time", s.PodsRunningAfterSeconds)
	}
	for _, row := range s.rows() {
		checkPhase(t, row.name, row.result, int64(prof.counts()[row.name]), 0, 0)
	}
}

func TestSeedReportsAnInterruptedRun(t *testing.T) {
	quietLog(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	f := seedFlags()
	f.waitRunning = true
	prof := seedProfile
	client := newClientset()
	metaClient := newMetadataClient(t, defaultServiceAccounts(seedLayout(f, prof))...)

	// The fakes accept a create on a cancelled context, so nothing fails
	// and the interrupt is the only error; the Running wait is skipped.
	s, err := seed(ctx, client, metaClient, f, prof)
	if want := "interrupted: " + context.Canceled.Error(); err == nil || err.Error() != want || !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want %q", err, want)
	}
	if s.FirstError != err.Error() {
		t.Errorf("first_error = %q, want %q", s.FirstError, err.Error())
	}
	if s.failed() != 0 || s.PodsRunningAfterSeconds != 0 {
		t.Errorf("failed %d, pods_running_after_seconds %v; want 0 and 0", s.failed(), s.PodsRunningAfterSeconds)
	}
}

func TestWaitForDefaultServiceAccounts(t *testing.T) {
	quietLog(t)
	names := []string{"team-000", "team-001", "team-002"}

	t.Run("satisfied at once", func(t *testing.T) {
		client := newMetadataClient(t,
			partialObject("ServiceAccount", "team-000", defaultServiceAccountName),
			partialObject("ServiceAccount", "team-002", defaultServiceAccountName))
		if err := waitForDefaultServiceAccounts(context.Background(), client, names, 2, time.Hour); err != nil {
			t.Fatalf("err = %v, want none", err)
		}
	})
	t.Run("times out counting only our namespaces", func(t *testing.T) {
		// One of ours and one in a namespace the run did not create: the
		// second would satisfy want without the keep filter.
		client := newMetadataClient(t,
			partialObject("ServiceAccount", "team-000", defaultServiceAccountName),
			partialObject("ServiceAccount", "kube-system", defaultServiceAccountName))
		err := waitForDefaultServiceAccounts(context.Background(), client, names, 2, time.Nanosecond)
		want := "default ServiceAccount present in 1 of 2 namespaces after 1ns"
		if err == nil || err.Error() != want {
			t.Fatalf("err = %v, want %q", err, want)
		}
	})
	t.Run("cancelled context", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		err := waitForDefaultServiceAccounts(ctx, newMetadataClient(t), names, 3, time.Hour)
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("err = %v, want %v", err, context.Canceled)
		}
	})
	t.Run("list error", func(t *testing.T) {
		client := newMetadataClient(t)
		client.PrependReactor("list", serviceAccountsResource, func(k8stesting.Action) (bool, runtime.Object, error) {
			return true, nil, errors.New("boom")
		})
		err := waitForDefaultServiceAccounts(context.Background(), client, names, 3, time.Hour)
		if want := "count default ServiceAccounts: boom"; err == nil || err.Error() != want {
			t.Fatalf("err = %v, want %q", err, want)
		}
	})
}

func TestWaitRunning(t *testing.T) {
	quietLog(t)

	t.Run("satisfied at once", func(t *testing.T) {
		seconds, err := waitRunning(context.Background(), newMetadataClient(t, runningPods(3)...), 3, time.Hour)
		if err != nil {
			t.Fatalf("err = %v, want none", err)
		}
		if seconds < 0 {
			t.Errorf("seconds = %v, want the wait's wall time", seconds)
		}
	})
	t.Run("times out", func(t *testing.T) {
		seconds, err := waitRunning(context.Background(), newMetadataClient(t, runningPods(2)...), 3, time.Nanosecond)
		want := "2 of 3 pods Running after 1ns"
		if err == nil || err.Error() != want {
			t.Fatalf("err = %v, want %q", err, want)
		}
		if seconds <= 0 {
			t.Errorf("seconds = %v, want the wait's wall time", seconds)
		}
	})
	t.Run("cancelled context", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		_, err := waitRunning(ctx, newMetadataClient(t), 3, time.Hour)
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("err = %v, want %v", err, context.Canceled)
		}
	})
	t.Run("list error", func(t *testing.T) {
		client := newMetadataClient(t)
		client.PrependReactor("list", podsResource, func(k8stesting.Action) (bool, runtime.Object, error) {
			return true, nil, errors.New("boom")
		})
		_, err := waitRunning(context.Background(), client, 3, time.Hour)
		if want := "count Running pods: boom"; err == nil || err.Error() != want {
			t.Fatalf("err = %v, want %q", err, want)
		}
	})
}

// pagedLister is a metadata client whose lists answer from pages in order
// and keep the options of each request. The client-go metadata fake drops
// Limit and Continue from the action its reactors see, so the token's round
// trip is checked here instead.
type pagedLister struct {
	metadata.ResourceInterface
	pages []*metav1.PartialObjectMetadataList
	calls []metav1.ListOptions
}

func (p *pagedLister) Resource(schema.GroupVersionResource) metadata.Getter { return p }

func (p *pagedLister) Namespace(string) metadata.ResourceInterface { return p }

func (p *pagedLister) List(_ context.Context, opts metav1.ListOptions) (*metav1.PartialObjectMetadataList, error) {
	p.calls = append(p.calls, opts)
	if len(p.calls) > len(p.pages) {
		return nil, errors.New("no more pages")
	}
	return p.pages[len(p.calls)-1], nil
}

func page(continueToken string, items ...*metav1.PartialObjectMetadata) *metav1.PartialObjectMetadataList {
	list := &metav1.PartialObjectMetadataList{ListMeta: metav1.ListMeta{Continue: continueToken}}
	for _, item := range items {
		list.Items = append(list.Items, *item)
	}
	return list
}

func TestCountPaged(t *testing.T) {
	resource := corev1.SchemeGroupVersion.WithResource(podsResource)
	twoPages := func() *pagedLister {
		return &pagedLister{pages: []*metav1.PartialObjectMetadataList{
			page("page-2",
				partialObject("Pod", "team-000", "a"),
				partialObject("Pod", "team-001", "b"),
				partialObject("Pod", "team-000", "c")),
			page("",
				partialObject("Pod", "team-001", "d"),
				partialObject("Pod", "team-000", "e")),
		}}
	}

	t.Run("follows the continue token", func(t *testing.T) {
		lister := twoPages()
		total, err := countPaged(context.Background(), lister, resource, runningFieldSelector, nil)
		if err != nil {
			t.Fatal(err)
		}
		if total != 5 {
			t.Errorf("total = %d, want 5 across both pages", total)
		}
		if len(lister.calls) != 2 {
			t.Fatalf("%d list requests, want 2", len(lister.calls))
		}
		for i, opts := range lister.calls {
			if opts.Limit != runningPageSize || opts.FieldSelector != runningFieldSelector {
				t.Errorf("request %d: limit %d selector %q, want %d %q", i, opts.Limit, opts.FieldSelector, runningPageSize, runningFieldSelector)
			}
		}
		if lister.calls[0].Continue != "" || lister.calls[1].Continue != "page-2" {
			t.Errorf("continue tokens sent = %q, %q; want \"\" then the first page's", lister.calls[0].Continue, lister.calls[1].Continue)
		}
	})
	t.Run("keep filters every page", func(t *testing.T) {
		lister := twoPages()
		total, err := countPaged(context.Background(), lister, resource, "", func(m *metav1.PartialObjectMetadata) bool {
			return m.Namespace == "team-000"
		})
		if err != nil {
			t.Fatal(err)
		}
		if total != 3 {
			t.Errorf("total = %d, want the 3 team-000 pods across both pages", total)
		}
		if len(lister.calls) != 2 {
			t.Errorf("%d list requests, want 2", len(lister.calls))
		}
	})
	t.Run("list error", func(t *testing.T) {
		lister := twoPages()
		lister.pages = lister.pages[:1]
		_, err := countPaged(context.Background(), lister, resource, "", nil)
		if err == nil || err.Error() != "no more pages" {
			t.Fatalf("err = %v, want the second page's error", err)
		}
	})
}

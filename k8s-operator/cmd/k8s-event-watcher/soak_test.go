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

//go:build soak

// The startup soak: a fleet of fake API servers, each with a large Event
// backlog, watched from one process, with the gate and the wire format the
// binary ships. Not in CI (the build tag keeps it out); run it by hand to put
// numbers beside a change to the start path:
//
//	go test -tags soak -run TestSoak_StartupBurst -v ./cmd/k8s-event-watcher/
//
// Knobs, all environment variables: SOAK_CLUSTERS (29), SOAK_EVENTS per
// cluster (50000), SOAK_GATE slots (4; 0 ungated), SOAK_JSON=1 to force JSON
// on the wire, SOAK_MAX_RSS_BYTES (2 GiB) for the one memory assertion, which
// is made only when the gate is on. The report is the test's log: peak lists
// in flight, peak heap in use, the process's RSS high-water mark, and the time
// to every cluster synced.
package main

import (
	"bufio"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sruntime "k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/serializer/protobuf"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
)

const (
	soakDefaultClusters = 29
	soakDefaultEvents   = 50_000
	soakDefaultGate     = 4
	soakDefaultMaxRSS   = 2 << 30
	soakSyncTimeout     = 15 * time.Minute
	soakSampleInterval  = 25 * time.Millisecond
	soakEventsPath      = "/api/v1/events"
)

func soakInt(name string, def int) int {
	if v := os.Getenv(name); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			panic(name + ": " + err.Error())
		}
		return n
	}
	return def
}

// soakEventList is one cluster's backlog: n Events shaped like kubelet's,
// with a managedFields entry each, as the API server returns them.
func soakEventList(n int) *corev1.EventList {
	list := &corev1.EventList{ListMeta: metav1.ListMeta{ResourceVersion: strconv.Itoa(n + 1)}}
	list.Items = make([]corev1.Event, n)
	now := metav1.Now()
	for i := range list.Items {
		pod := fmt.Sprintf("workload-%d-%05d", i%40, i)
		list.Items[i] = corev1.Event{
			ObjectMeta: metav1.ObjectMeta{
				Name:              pod + ".18a1c2b3d4e5f607",
				Namespace:         fmt.Sprintf("team-%d", i%25),
				ResourceVersion:   strconv.Itoa(i + 1),
				UID:               types.UID(fmt.Sprintf("8f1c7b2e-%08x-4c3a-9e1d-0123456789ab", i)),
				CreationTimestamp: now,
				ManagedFields: []metav1.ManagedFieldsEntry{{
					Manager:    "kubelet",
					Operation:  metav1.ManagedFieldsOperationUpdate,
					APIVersion: "v1",
					Time:       &now,
					FieldsType: "FieldsV1",
					FieldsV1:   &metav1.FieldsV1{Raw: []byte(`{"f:count":{},"f:firstTimestamp":{},"f:involvedObject":{},"f:lastTimestamp":{},"f:message":{},"f:reason":{},"f:source":{"f:component":{},"f:host":{}},"f:type":{}}`)},
				}},
			},
			InvolvedObject: corev1.ObjectReference{
				Kind: "Pod", Namespace: fmt.Sprintf("team-%d", i%25), Name: pod,
				UID: types.UID(fmt.Sprintf("0a1b2c3d-%08x-4e5f-8a9b-fedcba987654", i)), APIVersion: "v1",
				FieldPath: "spec.containers{app}",
			},
			Reason:         "Pulled",
			Message:        "Successfully pulled image \"europe-docker.pkg.dev/example/app/service:2026.10.31-7f3a\" in 1.284s (1.284s including waiting). Image size: 184729384 bytes.",
			Source:         corev1.EventSource{Component: "kubelet", Host: fmt.Sprintf("gke-prod-default-pool-7f1c3a2b-%04d", i%1500)},
			FirstTimestamp: now,
			LastTimestamp:  now,
			Count:          1,
			Type:           corev1.EventTypeNormal,
		}
	}
	return list
}

// soakEncode renders the list once in both wire formats.
func soakEncode(t *testing.T, list *corev1.EventList) (jsonBody, protoBody []byte) {
	t.Helper()
	var err error
	jsonBody, err = k8sruntime.Encode(scheme.Codecs.LegacyCodec(corev1.SchemeGroupVersion), list)
	if err != nil {
		t.Fatal(err)
	}
	protoEnc := scheme.Codecs.EncoderForVersion(protobuf.NewSerializer(scheme.Scheme, scheme.Scheme), corev1.SchemeGroupVersion)
	protoBody, err = k8sruntime.Encode(protoEnc, list)
	if err != nil {
		t.Fatal(err)
	}
	return jsonBody, protoBody
}

// soakAPIServer is a fake control plane: the Event list in whichever format
// the client accepts, a watch that stays open and carries nothing, 404 for
// everything else (the preflight, which then reads as inconclusive and
// builds the informer, as it does through the credential proxy).
func soakAPIServer(t *testing.T, jsonBody, protoBody []byte, inFlight, peak *atomic.Int64) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != soakEventsPath {
			http.NotFound(w, r)
			return
		}
		q := r.URL.Query()
		if q.Get("watch") == "true" {
			if q.Get("sendInitialEvents") == "true" {
				// No watch-list here; the reflector falls back to the list.
				http.Error(w, `{"kind":"Status","status":"Failure","reason":"BadRequest","code":400}`, http.StatusBadRequest)
				return
			}
			w.Header().Set("Content-Type", k8sruntime.ContentTypeJSON)
			w.WriteHeader(http.StatusOK)
			w.(http.Flusher).Flush()
			<-r.Context().Done()
			return
		}
		now := inFlight.Add(1)
		defer inFlight.Add(-1)
		for {
			prev := peak.Load()
			if now <= prev || peak.CompareAndSwap(prev, now) {
				break
			}
		}
		if strings.Contains(r.Header.Get("Accept"), k8sruntime.ContentTypeProtobuf) {
			w.Header().Set("Content-Type", k8sruntime.ContentTypeProtobuf)
			_, _ = w.Write(protoBody)
			return
		}
		w.Header().Set("Content-Type", k8sruntime.ContentTypeJSON)
		_, _ = w.Write(jsonBody)
	}))
	t.Cleanup(srv.Close)
	return srv
}

// rssHighWater reads VmHWM from /proc/self/status, in bytes; 0 when it
// cannot.
func rssHighWater() int64 {
	f, err := os.Open("/proc/self/status")
	if err != nil {
		return 0
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if fields := strings.Fields(sc.Text()); len(fields) >= 2 && fields[0] == "VmHWM:" {
			kb, _ := strconv.ParseInt(fields[1], 10, 64)
			return kb << 10
		}
	}
	return 0
}

func TestSoak_StartupBurst(t *testing.T) {
	clusters := soakInt("SOAK_CLUSTERS", soakDefaultClusters)
	events := soakInt("SOAK_EVENTS", soakDefaultEvents)
	slots := soakInt("SOAK_GATE", soakDefaultGate)
	maxRSS := int64(soakInt("SOAK_MAX_RSS_BYTES", soakDefaultMaxRSS))
	forceJSON := os.Getenv("SOAK_JSON") == "1"

	jsonBody, protoBody := soakEncode(t, soakEventList(events))
	t.Logf("backlog of %d events per cluster: %d bytes as JSON, %d bytes as protobuf", events, len(jsonBody), len(protoBody))
	runtime.GC()
	var base runtime.MemStats
	runtime.ReadMemStats(&base)
	rssBefore := rssHighWater()

	var inFlight, peakInFlight atomic.Int64
	gate := newListGate(slots)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var synced atomic.Int64
	start := time.Now()
	for i := 0; i < clusters; i++ {
		srv := soakAPIServer(t, jsonBody, protoBody, &inFlight, &peakInFlight)
		cfg := &rest.Config{Host: srv.URL}
		client, err := newClusterClient(cfg)
		if err != nil {
			t.Fatal(err)
		}
		if forceJSON {
			cfg.ContentType, cfg.AcceptContentTypes = k8sruntime.ContentTypeJSON, k8sruntime.ContentTypeJSON
			if client, err = newClusterClient(cfg); err != nil {
				t.Fatal(err)
			}
			cfg.ContentType, cfg.AcceptContentTypes = k8sruntime.ContentTypeJSON, k8sruntime.ContentTypeJSON
		}
		w := newWatcher(client, nopDispatcher{}, targetCluster{Name: fmt.Sprintf("soak-%02d", i)})
		w.gate = gate
		go func() {
			_ = w.Run(ctx, func(watching bool) {
				if watching {
					synced.Add(1)
				}
			})
		}()
	}

	var peakHeap uint64
	sampling := make(chan struct{})
	go func() {
		defer close(sampling)
		var ms runtime.MemStats
		for ctx.Err() == nil {
			runtime.ReadMemStats(&ms)
			if ms.HeapInuse > peakHeap {
				peakHeap = ms.HeapInuse
			}
			time.Sleep(soakSampleInterval)
		}
	}()

	deadline := time.Now().Add(soakSyncTimeout)
	for synced.Load() < int64(clusters) {
		if time.Now().After(deadline) {
			t.Fatalf("%d of %d clusters synced within %s", synced.Load(), clusters, soakSyncTimeout)
		}
		time.Sleep(50 * time.Millisecond)
	}
	elapsed := time.Since(start)
	cancel()
	<-sampling
	rssAfter := rssHighWater()

	wire := "protobuf"
	if forceJSON {
		wire = "JSON"
	}
	t.Logf("SOAK clusters=%d events/cluster=%d gate=%d wire=%s", clusters, events, slots, wire)
	t.Logf("SOAK peak lists in flight: %d", peakInFlight.Load())
	t.Logf("SOAK peak heap in use: %d MiB (baseline before start %d MiB)", peakHeap>>20, base.HeapInuse>>20)
	t.Logf("SOAK RSS high-water mark: %d MiB (was %d MiB before the start; includes the encoded backlogs the fake servers hold)", rssAfter>>20, rssBefore>>20)
	t.Logf("SOAK all %d clusters synced in %s", clusters, elapsed.Round(time.Millisecond))

	if slots > 0 {
		if got := peakInFlight.Load(); got > int64(slots) {
			t.Errorf("peak lists in flight = %d; want at most the gate's %d", got, slots)
		}
		if rssAfter > maxRSS {
			t.Errorf("RSS high-water mark %d bytes exceeds %d", rssAfter, maxRSS)
		}
	}
}

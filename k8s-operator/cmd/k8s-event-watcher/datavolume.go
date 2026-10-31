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
	"log"
	"sync"
	"syscall"

	"github.com/prometheus/client_golang/prometheus"
)

// The agent's data volume, as the operator reads it: the watcher is the one
// process in the gateway pod that already serves a /metrics the operator
// scrapes and already mounts the volume, so it reports the volume's size and
// free space beside its own series. The operator folds them into the CR's
// status.storageStatus (internal/controller/usage_counters_scrape.go).
const (
	dataVolumeMetric = "kubeagents_data_volume_bytes"
	dataVolumeHelp   = "Size and free space of the agent's data volume, read with statfs at each scrape. kind=total is the filesystem's size, kind=free what an unprivileged writer can still use."
	dataVolumeKind   = "kind"
	dataVolumeTotal  = "total"
	dataVolumeFree   = "free"
)

// dataVolumeStatfs reads the filesystem holding path: its size, and the space
// left to a writer that is not root, which is what the agent is.
func dataVolumeStatfs(path string) (total, free uint64, err error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, 0, err
	}
	bsize := uint64(st.Bsize) // #nosec G115 -- a block size is positive
	return st.Blocks * bsize, st.Bavail * bsize, nil
}

// dataVolumeCollector exports the volume at scrape time rather than on a
// timer, so a reading is never older than the scrape that carries it. A failed
// statfs exports nothing: no series is the operator's "no reading", where a
// zero would read as a full volume.
type dataVolumeCollector struct {
	path   string
	desc   *prometheus.Desc
	statfs func(string) (uint64, uint64, error)

	// failing is whether the last read failed, so a standing failure is
	// logged once when it starts and once when it clears, not every scrape.
	mu      sync.Mutex
	failing bool
}

func newDataVolumeCollector(path string, statfs func(string) (uint64, uint64, error)) *dataVolumeCollector {
	return &dataVolumeCollector{
		path:   path,
		desc:   prometheus.NewDesc(dataVolumeMetric, dataVolumeHelp, []string{dataVolumeKind}, nil),
		statfs: statfs,
	}
}

func (c *dataVolumeCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.desc
}

func (c *dataVolumeCollector) Collect(ch chan<- prometheus.Metric) {
	total, free, err := c.statfs(c.path)
	c.mu.Lock()
	wasFailing := c.failing
	c.failing = err != nil
	c.mu.Unlock()
	if err != nil {
		if !wasFailing {
			log.Printf("k8s-event-watcher: cannot read the data volume at %s (%v); %s is not exported until it can be", c.path, err, dataVolumeMetric)
		}
		return
	}
	if wasFailing {
		log.Printf("k8s-event-watcher: the data volume at %s is readable again", c.path)
	}
	ch <- prometheus.MustNewConstMetric(c.desc, prometheus.GaugeValue, float64(total), dataVolumeTotal)
	ch <- prometheus.MustNewConstMetric(c.desc, prometheus.GaugeValue, float64(free), dataVolumeFree)
}
